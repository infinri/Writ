"""Commit-time capture orchestrator for decision-memory Phase 1d.

Two seams, isolated from server.py (the routes) and the git hooks (bash) so the
fail-open logic is testable in one place:

  resolve_reasons_for_files (for prepare-commit-msg): per staged file, the reason
  the planner already wrote in the most-recent OPEN Decision claim for that path,
  with its decision id and governing rules. No match -> blank reason, no decision
  id (never invented).

  capture_commit (for post-commit): register the Project (never the bare 'writ'
  fallback), create the Commit and one FileChange per changed file, wire
  INCLUDES / MOTIVATED_BY / REALIZES / HAS_COMMIT / HAS_CHANGE, and resolve the
  claim on every planning Decision for each committed path. Idempotent via a
  deterministic change_id so a re-run on the same commit re-MERGEs the same nodes.
"""

from __future__ import annotations

import hashlib
import json
import os

from writ.session.registration import ensure_project_registered

# Field separator for the deterministic change_id hash input. A NUL byte cannot
# appear in a project name, commit hash, or path, so it is an unambiguous joiner.
_HASH_SEP = "\x00"
# Length of the hex digest prefix kept as the change_id.
_CHANGE_ID_LEN = 16
# Recency window used only when the parent-commit time is unavailable (first
# commit, or git failed on a repo that does have history). The path+recency merge
# then admits sub-agent caches modified within this window of now, never an
# unbounded 0.0 bound that would leak a stale cross-conversation cache.
_RECENCY_FALLBACK_WINDOW_S = 6 * 3600


def _derive_change_id(project: str, commit_hash: str, path: str) -> str:
    """Stable change_id for a (project, commit_hash, path) triple.

    The same commit re-run (amend that keeps the hash, post-rewrite replay)
    derives the same change_id, so create_filechange re-MERGEs the same node
    instead of minting a duplicate.
    """
    raw = f"{project}{_HASH_SEP}{commit_hash}{_HASH_SEP}{path}".encode()
    return hashlib.sha1(raw).hexdigest()[:_CHANGE_ID_LEN]


def _queried_for_path(queried_by_file: dict, cwd: str, path: str) -> list[str]:
    """Queried rule ids for a committed file, reconciling the two key formats.

    The write hook keys the cache by normalize_path of the ABSOLUTE write path,
    while the commit passes the git repo-relative path. Reconstruct the absolute
    key from cwd + the relative path; fall back to the relative key for callers
    (the harvester) that key relatively. Empty when neither matches.
    """
    if not queried_by_file:
        return []
    from writ.session.remote_parse import normalize_path
    abs_key = normalize_path(os.path.join(cwd, path))
    return queried_by_file.get(abs_key) or queried_by_file.get(normalize_path(path)) or []


def _parent_commit_ts(cwd: str, commit_hash: str, runner=None) -> float:
    """Lower bound for 'sub-agent work that fed THIS commit': the parent commit's
    unix time. When the parent time is unavailable (first commit, or git fails on a
    repo that does have history) fall back to a tight recent window, NOT 0.0: a 0.0
    bound disables the recency fence and would let a stale cross-conversation cache
    leak; the window admits only caches from the current work session.

    The per-cache recency signal is the cache file mtime, which a finished sub-agent
    does not rewrite, so it tracks that sub-agent's last activity. A cwd that
    resolves through a symlink differently than the write-time path is a known
    unhandled edge: path-match misses and it falls back to parent-match."""
    import subprocess
    import time
    run = runner or subprocess.run
    try:
        cp = run(
            ["git", "show", "-s", "--format=%ct", f"{commit_hash}^"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
        if cp.returncode == 0 and cp.stdout.strip():
            return float(cp.stdout.strip())
    except Exception:
        pass
    return time.time() - _RECENCY_FALLBACK_WINDOW_S


def _commit_ts(cwd: str, commit_hash: str, runner=None) -> str:
    """The commit's own author time as an ISO-8601 string, for governing-plan
    selection. Empty string when git is unavailable (then _governing_plan, given
    the datetime.min floor, selects no plan -> fail-open to the commit subject)."""
    import subprocess
    run = runner or subprocess.run
    try:
        cp = run(
            ["git", "show", "-s", "--format=%aI", commit_hash],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
        if cp.returncode == 0 and cp.stdout.strip():
            return cp.stdout.strip()
    except Exception:
        pass
    return ""


def _reason_for_path(decision_row: dict, path: str) -> str:
    """The reason text from a Decision's planned_files entry matching `path`."""
    raw = decision_row.get("planned_files")
    if isinstance(raw, str):
        try:
            claims = json.loads(raw)
        except (ValueError, TypeError):
            claims = []
    elif isinstance(raw, list):
        claims = raw
    else:
        claims = []
    for claim in claims:
        if isinstance(claim, dict) and claim.get("path") == path:
            return claim.get("reason") or ""
    return ""


async def resolve_reasons_for_files(db, project: str, files: list[dict]) -> list[dict]:
    """Attach the most-recent open Decision's reason + ids to each file entry.

    Each input entry is {path, change_type, ...}. Each output entry carries
    reason, decision_id, and governing_rule_ids. A file with no matching open
    Decision gets a blank reason and decision_id=None.
    """
    resolved: list[dict] = []
    for entry in files:
        path = entry.get("path", "")
        open_decisions = await db.get_open_decisions_for_path(project, path)
        out = {"path": path, "change_type": entry.get("change_type", "")}
        if open_decisions:
            top = open_decisions[0]
            out["reason"] = _reason_for_path(top, path)
            out["decision_id"] = top.get("decision_id")
            out["governing_rule_ids"] = top.get("governing_rule_ids") or []
        else:
            out["reason"] = ""
            out["decision_id"] = None
            out["governing_rule_ids"] = []
        resolved.append(out)
    return resolved


async def capture_commit(
    db, cwd: str, commit_hash: str, subject: str, author: str, branch: str,
    files: list[dict], runner=None, session_id: str = "",
):
    """Create the Commit + per-file FileChange records, MECHANICALLY create the
    governing Decision from the session transcript, and wire everything in.

    Phase 3a: the reliable, AI-independent trigger. The post-commit hook curls the
    daemon, which calls this. When the session transcript has a plan.md Write at or
    before the commit, harvest_one_commit creates the governing Decision (content-
    hash id, deduped with the manual backfill) and wires GOVERNED_BY / MOTIVATED_BY
    / REALIZES; otherwise it falls open to a Commit + FileChange with the commit
    subject as the reason. Fail-open: a missing/unreadable transcript creates no
    Decision and never blocks the commit. Idempotent: a re-run re-MERGEs.
    """
    name = await ensure_project_registered(db, cwd, runner=runner)
    if name is None:
        return None

    # Pre-existing OPEN-Decision claims per committed path (e.g. a Decision the live
    # approval path created). Supplies the FileChange reason + cited_rule_ids
    # snapshot and the MOTIVATED_BY / REALIZES wiring when no transcript plan
    # governs the path. Best-effort: a backend without get_open_decisions_for_path
    # (or any read failure) yields no prior claims and capture proceeds fail-open.
    try:
        prior_claims = await resolve_reasons_for_files(db, name, files)
    except Exception:
        prior_claims = []

    # Per-file queried-rule map: merge what THIS session captured with what every
    # relevant sub-agent captured, linked by committed FILE PATH within the parent-
    # commit recency window (NOT the churning global session link). Best-effort.
    from writ.session.remote_parse import normalize_path
    committed_keys = set()
    for entry in files:
        p = entry["path"]
        committed_keys.add(normalize_path(os.path.join(cwd, p)))
        committed_keys.add(normalize_path(p))
    since_ts = _parent_commit_ts(cwd, commit_hash, runner)

    queried_by_file: dict = {}
    try:
        from writ.session.cache import (
            _read_cache,
            _merge_queried_by_file,
            _collect_subagent_queried_rules,
        )
        parent_queried = (
            _read_cache(session_id).get("queried_rules_by_file", {}) or {}
        ) if session_id else {}
        queried_by_file = _merge_queried_by_file(
            parent_queried,
            _collect_subagent_queried_rules(session_id, committed_keys, since_ts),
        )
    except Exception:
        queried_by_file = {}

    # Recover the governing plan, PREFERRING the on-disk plan.md (the exact bytes
    # the approval gate read, located by the SAME _find_plan_md). In the gated
    # workflow the writ-planner SUBAGENT writes plan.md, so the plan Write lives in
    # the subagent transcript, not the parent's; the transcript scan then returns a
    # stale plan or None. Read disk FIRST and use it whenever present; the transcript
    # scan is only the fallback when no plan.md is on disk. Zero LLM, zero DB. Any
    # failure -> no plan -> harvest_one_commit falls open to the commit subject; the
    # disk read is wrapped fail-open so a missing/unreadable plan.md never blocks the
    # commit. Imported lazily so a transcript-format change degrades reason quality
    # but never breaks the import path.
    plan_text = None
    plan_ts = None
    commit_ts = ""
    try:
        from pathlib import Path
        from writ.session.harvester import (
            _collect_plan_writes,
            _governing_plan,
            _project_transcript_dir,
        )
        from writ.session.locators import _find_plan_md
        commit_ts = _commit_ts(cwd, commit_hash, runner)
        plan_path = _find_plan_md(os.path.abspath(cwd), session_id)
        if plan_path:
            with open(plan_path) as fh:
                plan_text = fh.read()
            plan_ts = commit_ts
        else:
            home = Path.home() / ".claude"
            plans = _collect_plan_writes(
                _project_transcript_dir(os.path.abspath(cwd), home)
            )
            chosen = _governing_plan(plans, commit_ts)
            if chosen:
                plan_text = chosen.get("plan_text")
                plan_ts = chosen.get("ts")
    except Exception:
        plan_text, plan_ts = None, None

    from writ.session.harvester import harvest_one_commit
    stats = await harvest_one_commit(
        db, name,
        commit_hash=commit_hash, subject=subject, author=author, branch=branch,
        commit_ts=commit_ts,
        files=files,
        plan_text=plan_text, plan_ts=plan_ts,
        queried_by_file=queried_by_file, prior_claims=prior_claims, cwd=cwd,
    )

    # A committed file the governing plan did not itemize is recorded in
    # stats["unplanned_files"]; emit one auditable committed_file_not_in_plan
    # friction event per path. Best-effort: a friction-log failure must never
    # block the commit (preserves capture_commit's fail-open contract).
    try:
        from writ.session.friction import _log_friction_event
        for path in stats.get("unplanned_files", []):
            _log_friction_event(
                session_id, None, "committed_file_not_in_plan",
                file_path=path, commit_hash=commit_hash, project=name,
            )
    except Exception:
        pass

    # Resolve any pre-existing OPEN claims for the committed paths (e.g. a Decision
    # the live approval path created before it was retired). Idempotent no-op when
    # none are open; harvest_one_commit already resolved the claims it created.
    for entry in files:
        await db.resolve_file_claims(name, entry["path"])

    return name
