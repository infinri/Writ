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
import pathlib

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

    # THE APPROVAL IS SPENT WHEN ITS WORK IS DONE. This is the only place that
    # learns a plan COMPLETED, so it is the only place that can retire the approval
    # granted for it; gates otherwise clear only on `mode set`, a replan, or drift,
    # which is how an approval outlived its plan by three days (see the function).
    # The resolved set is every path this project has resolved, not just this
    # commit's, so a plan finished across several commits completes on the last one.
    try:
        resolved = await _resolved_paths_for_project(db, name)
        _expire_completed_approval(session_id or "", cwd or "", resolved)
    except Exception:
        pass

    return name


def _plan_is_complete(plan_text: str, resolved_paths: set[str]) -> bool | None:
    """Is every file this plan DECLARED now resolved? None when unjudgeable.

    `None` means "nothing here establishes completion" (no plan text, or a plan
    that declares no files) and is NOT the same as False, which means "declared
    files remain unresolved". The caller must treat only True as completion:
    absence is not a policy.

    THE DECLARED LIST COMES FROM THE CANONICAL PARSER. `plan_harvest._extract_files`
    owns the `## Files` regexes and its own comment records that a third copy of
    them is a bill this repo has already paid twice. A path a plan only mentions in
    `## Analysis` is therefore not a declaration, which is what keeps a prose
    mention from completing a plan whose real entries are still open.
    """
    if not plan_text:
        return None
    from writ.session.plan_harvest import _extract_files

    declared = {e.get("path") for e in _extract_files(plan_text) if e.get("path")}
    if not declared:
        return None
    return declared.issubset(resolved_paths)


def _expire_completed_approval(session_id: str, project_root: str,
                               resolved_paths: set[str]) -> bool:
    """Spend the approval whose work is finished. Returns True when it cleared.

    WHY THIS EXISTS. Gates clear on `mode set`, on a replan, and on plan drift, and
    on nothing else, so an approval outlives the work it was granted for. Measured
    2026-09-21: session 2412ba38 still carried both gates bound to plan hash
    fb84122192e7 from the 2026-09-18 cycle, three days and an unrelated task later,
    and source writes would have been allowed under it.

    ENF-SYS-002. The authoritative decision is the human typing the approval phrase,
    recorded in `gates_approved`. This does not re-decide that and can never grant
    anything: it observes that the work the approval was granted FOR is complete and
    SPENDS it. The post-commit path is the only place that learns work finished.

    THE CONDITION IS "EVERY DECLARED PATH", NOT "ANY", so a multi-commit cycle keeps
    its approval until the last declared file lands. That bound is structural rather
    than a judgement call, and it is what stops this being hostile.

    ABSTAINS on every state where nothing establishes that THIS session's work is
    done: no session id, no resolvable plan.md, a plan declaring no files, a session
    not in work mode (no gates exist to spend), and a session with nothing approved.
    Best effort throughout: a commit is never blocked by this.
    """
    if not session_id or not project_root:
        return False
    try:
        from writ.session.cache import _read_cache, _write_cache
        from writ.session.locators import _find_plan_md

        cache = _read_cache(session_id)
        if not cache or str(cache.get("mode") or "") != "work":
            return False
        if not (cache.get("gates_approved") or []):
            return False

        plan_path = _find_plan_md(os.path.abspath(project_root), session_id)
        if not plan_path:
            return False
        try:
            plan_text = pathlib.Path(plan_path).read_text(errors="replace")
        except OSError:
            return False

        if _plan_is_complete(plan_text, set(resolved_paths)) is not True:
            return False

        cache["gates_approved"] = []
        cache["gates_approved_plan"] = {}
        cache["current_phase"] = "planning"
        _write_cache(session_id, cache)
        try:
            from writ.session.friction import _log_friction_event
            _log_friction_event(session_id, cache.get("mode"), "approval_expired",
                                plan_path=str(plan_path))
        except Exception:
            pass
        return True
    except Exception:
        return False


async def _resolved_paths_for_project(db, project: str) -> set[str]:
    """Every path this project has a RESOLVED claim for, across all its Decisions.

    Read from the graph rather than from this commit's file list, because a plan is
    routinely finished across several commits and only the union completes it. Any
    failure returns an empty set, which makes `_plan_is_complete` answer False and
    the approval survive: the fail-open direction here is to keep the approval, never
    to spend one on missing data.
    """
    out: set[str] = set()
    try:
        rows = await db._run(
            "MATCH (d:Decision) WHERE d.project = $p AND d.planned_files IS NOT NULL "
            "RETURN d.planned_files AS pf", p=project)
    except Exception:
        return out
    for row in rows or []:
        pf = row.get("pf")
        if isinstance(pf, str):
            try:
                pf = json.loads(pf)
            except Exception:
                continue
        for entry in pf or []:
            if isinstance(entry, dict) and entry.get("resolved") and entry.get("path"):
                out.add(entry["path"])
    return out
