"""Mechanical decision-memory harvester (capture redesign).

Reads git (authoritative for commits + files) and the Claude Code session
transcripts (the only record of the approved plan) to write Decision / FileChange
/ Commit records. Capability A (git) is the stable backbone; capability B
(transcript plan reasons) enriches it and FAILS SOFT to the commit subject when a
transcript/plan is missing or unparseable, so an Anthropic transcript-format
change degrades reason quality but never drops a commit or file. Idempotent: the
same commit/plan re-MERGEs the same nodes (deterministic ids).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from writ.session.cache import _merge_queried_by_file
from writ.session.commit_capture import _derive_change_id, _queried_for_path
from writ.session.friction import _log_friction_event
# The ~/.claude/projects/<encoded> derivation lives in locators now (stdlib-only, below
# every caller) because the WRITE PATH reads it too. Imported rather than redefined so the
# encoding has one definition, and re-exported from this module so
# harvester._project_transcript_dir stays the patchable seam this module's tests use and
# commit_capture's lazy import keeps resolving.
from writ.session.locators import _project_transcript_dir
from writ.session.name_status import parse_name_status
from writ.session.plan_harvest import harvest_plan
from writ.session.registration import ensure_project_registered
from writ.session.remote_parse import normalize_path

_DEC_SEP = "\x00"
_GIT_TIMEOUT = 30

# PreToolUse hook names whose file-context attachment blocks carry the rules the
# AI was shown for a file. PostToolUse blocks share the structure and are
# excluded (they would double-count). Read is included for parity with the live
# cache, which unions the writ-read-rag.sh add-queried-rules-for-file path.
_FILE_CONTEXT_HOOKS = frozenset(
    {"PreToolUse:Read", "PreToolUse:Edit", "PreToolUse:Write"}
)
# Tool names whose tool_use input carries a writable/readable file path.
_FILE_TOOLS = frozenset({"Read", "Edit", "Write", "NotebookEdit", "MultiEdit"})
# Header that begins every file-context rule block (writ-pre-write-dispatch.sh /
# writ-read-rag.sh emit "[Writ: file-context rules for <basename>]\n<rules>").
_FILE_CONTEXT_HEADER = "[Writ: file-context rules for "
# Standard rule id and the [ABSTRACT: <id>] form. U+2028 is normalized to \n
# before matching. [UNKNOWN] has no hyphen segment so the standard pattern skips
# it. Abstracts are captured as the BARE id so they join rule statements in
# db.get_rule_statements (an "ABSTRACT:"-prefixed id would never match a node).
_RULE_ID_RE = re.compile(r"\[([A-Z][A-Z0-9]*(?:-[A-Z0-9]+){1,4})\]")
_ABSTRACT_ID_RE = re.compile(r"\[ABSTRACT: ([A-Z][A-Z0-9]*(?:-[A-Z0-9]+){1,4})\]")


def _run_git(repo: str, args: list[str]) -> str:
    """Run a git command in `repo` and return stdout. Raises on non-zero exit."""
    result = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True, text=True, timeout=_GIT_TIMEOUT, check=True,
    )
    return result.stdout


def _resolve_rev(repo: str, rev: str) -> None:
    """Validate that `rev` names a commit in `repo`; raise a clear error if not.

    `--since` must be a git revision (commit hash, branch, or tag), NOT a date
    string. A non-rev `since` would make `git log <since>..HEAD` exit non-zero and
    crash _run_git with CalledProcessError; this turns that into an actionable
    ValueError the CLI surfaces as a clean message. Uses --verify --quiet with
    check=False so a non-rev returns non-zero instead of raising from subprocess.
    """
    result = subprocess.run(
        ["git", "-C", repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"],
        capture_output=True, text=True, timeout=_GIT_TIMEOUT, check=False,
    )
    if result.returncode != 0:
        raise ValueError(
            f"--since must be a git revision (commit hash, branch, or tag), "
            f"not a date: {rev!r} does not resolve to a commit."
        )


def _parse_ts(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp (transcript 'Z' or git '+00:00') to aware UTC."""
    if not ts:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _git_commits(repo: str, since: str | None) -> list[dict]:
    """Commits oldest-first as {hash, subject, body, author, branch, ts, files}.

    Range is <since>..HEAD when since is given, else all reachable from HEAD.
    Files come from `git show --name-status` (capability A). Merge commits are
    excluded (--no-merges) since their name-status is empty without -m.
    """
    if since:
        _resolve_rev(repo, since)
    rev_range = f"{since}..HEAD" if since else "HEAD"
    branch = _run_git(repo, ["rev-parse", "--abbrev-ref", "HEAD"]).strip()
    hashes = [h for h in _run_git(
        repo, ["log", "--reverse", "--no-merges", "--format=%H", rev_range]
    ).splitlines() if h.strip()]
    commits: list[dict] = []
    for h in hashes:
        meta = _run_git(repo, ["show", "-s", "--format=%H%x00%s%x00%b%x00%an%x00%aI", h])
        parts = (meta.split("\x00") + ["", "", "", "", ""])[:5]
        chash, subject, body, author, ts = parts
        files = parse_name_status(
            _run_git(repo, ["show", "--name-status", "--format=", h])
        )
        commits.append({
            "hash": chash.strip(), "subject": subject.strip(), "body": body,
            "author": author.strip(), "branch": branch, "ts": ts.strip(),
            "files": files,
        })
    return commits


def _collect_plan_writes(transcript_dir: Path) -> list[dict]:
    """Return [{ts, plan_text}] for every successful Write to a *plan.md, all sessions.

    Capability B (transcript-coupled). Returns [] on a missing dir or any
    read/parse error (FAIL SOFT: the caller then falls back to commit subjects).
    """
    plans: list[dict] = []
    if not transcript_dir.is_dir():
        return plans
    for jsonl in sorted(transcript_dir.glob("*.jsonl")):
        try:
            for line in jsonl.read_text().splitlines():
                if '"tool_use"' not in line or "plan.md" not in line:
                    continue
                entry = json.loads(line)
                if entry.get("type") != "assistant":
                    continue
                for item in entry.get("message", {}).get("content", []):
                    if (isinstance(item, dict) and item.get("type") == "tool_use"
                            and item.get("name") == "Write"):
                        inp = item.get("input", {})
                        if str(inp.get("file_path", "")).endswith("plan.md"):
                            plans.append({
                                "ts": entry.get("timestamp", ""),
                                "plan_text": inp.get("content", ""),
                            })
        except (OSError, ValueError):
            continue
    plans.sort(key=lambda p: _parse_ts(p["ts"]))
    return plans


def _governing_plan(plans: list[dict], commit_ts: str) -> dict | None:
    """The latest plan write at or before commit_ts (plans must be ts-ascending)."""
    ct = _parse_ts(commit_ts)
    chosen: dict | None = None
    for p in plans:
        if _parse_ts(p["ts"]) <= ct:
            chosen = p
        else:
            break
    return chosen


def _decision_id(name: str, plan_text: str) -> str:
    """Deterministic Decision id from plan content (one Decision per unique plan)."""
    h = hashlib.sha1(f"{name}{_DEC_SEP}{plan_text}".encode()).hexdigest()[:12]
    return f"DEC-{name}-{h}"


def _file_context_block_text(attachment: dict) -> str:
    """Extract the file-context block text from one attachment, all 3 variants.

    Format C (hook_additional_context): content[0]. Format A (hook_success,
    string content): content. Format B (hook_success, empty content): the
    additionalContext inside the JSON stdout. Returns "" when none matches or
    the text is not a file-context block (FAIL SOFT)."""
    a_type = attachment.get("type")
    text = ""
    if a_type == "hook_additional_context":
        content = attachment.get("content")
        if isinstance(content, list) and content:
            text = content[0] if isinstance(content[0], str) else ""
    elif a_type == "hook_success":
        content = attachment.get("content")
        if isinstance(content, str) and content:
            text = content
        else:
            try:
                stdout = json.loads(attachment.get("stdout") or "{}")
                text = (
                    stdout.get("hookSpecificOutput", {}).get("additionalContext")
                    or ""
                )
            except (ValueError, TypeError):
                text = ""
    if not isinstance(text, str) or _FILE_CONTEXT_HEADER not in text:
        return ""
    return text


def _rule_ids_from_block(text: str) -> list[str]:
    """Sorted unique rule ids in a file-context block (standard + ABSTRACT forms).

    Normalizes U+2028 to \\n first. Abstracts are captured as the bare id.
    [UNKNOWN] is excluded (no hyphen segment matches the grammar)."""
    norm = text.replace(" ", "\n")
    ids = set(_RULE_ID_RE.findall(norm)) | set(_ABSTRACT_ID_RE.findall(norm))
    return sorted(ids)


def _collect_file_context_rules(transcript_dir: Path) -> list[dict]:
    """Return [{ts, file_path, rule_ids}] for every PreToolUse file-context block.

    The mechanical 3b backfill source for FileChange.queried_rule_ids: the rules
    the AI was shown for a file, recovered from the session transcripts (zero
    LLM, zero DB). Mirrors _collect_plan_writes's {ts, ...} shape.

    Scans transcript_dir.rglob("*.jsonl") (top-level + sub-agent transcripts).
    Per file it builds two indexes keyed by attachment.toolUseID:
      - file_path:  from assistant tool_use Read/Edit/Write records
                    (message.content[*] where id == toolUseID), reading
                    input.file_path / input.path / input.notebook_path. CC
                    passes ABSOLUTE paths.
      - rule_ids:   from PreToolUse file-context attachment blocks (all 3 format
                    variants), DEDUPED by toolUseID preferring Format C
                    (hook_additional_context) over Format B.
    Joins the two by toolUseID and emits one entry per (toolUseID) with the
    NORMALIZED absolute path as file_path (normalize_path strips the leading
    '/'), matching the key form commit_capture._queried_for_path reconstructs.

    FAIL SOFT: a missing dir -> []; an unreadable/garbled file is skipped; a
    bad line/record is skipped. A transcript-format change degrades queried
    coverage but never raises and never drops a commit."""
    entries: list[dict] = []
    if not transcript_dir.is_dir():
        return entries
    for jsonl in sorted(transcript_dir.rglob("*.jsonl")):
        try:
            lines = jsonl.read_text().splitlines()
        except (OSError, UnicodeError):
            continue
        path_by_tool: dict[str, str] = {}
        block_by_tool: dict[str, dict] = {}  # toolUseID -> {ts, rule_ids, fmt_c}
        for line in lines:
            if '"toolUseID"' not in line and '"tool_use"' not in line:
                continue
            try:
                rec = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(rec, dict):
                continue
            rtype = rec.get("type")
            if rtype == "assistant":
                for item in rec.get("message", {}).get("content", []):
                    if (isinstance(item, dict) and item.get("type") == "tool_use"
                            and item.get("name") in _FILE_TOOLS):
                        tuid = item.get("id")
                        inp = item.get("input", {})
                        fp = (inp.get("file_path") or inp.get("path")
                              or inp.get("notebook_path"))
                        if tuid and fp:
                            path_by_tool[str(tuid)] = str(fp)
            elif rtype == "attachment":
                att = rec.get("attachment", {})
                if not isinstance(att, dict):
                    continue
                if att.get("hookName") not in _FILE_CONTEXT_HOOKS:
                    continue
                tuid = att.get("toolUseID")
                if not tuid:
                    continue
                text = _file_context_block_text(att)
                if not text:
                    continue
                is_c = att.get("type") == "hook_additional_context"
                prev = block_by_tool.get(str(tuid))
                # Dedupe by toolUseID preferring Format C; a later C overrides a
                # B, a B never overrides a C.
                if prev is None or (is_c and not prev.get("fmt_c")):
                    block_by_tool[str(tuid)] = {
                        "ts": rec.get("timestamp", ""),
                        "rule_ids": _rule_ids_from_block(text),
                        "fmt_c": is_c,
                    }
        for tuid, blk in block_by_tool.items():
            fp = path_by_tool.get(tuid)
            if not fp or not blk["rule_ids"]:
                continue
            entries.append({
                "ts": blk["ts"],
                "file_path": normalize_path(fp),
                "rule_ids": blk["rule_ids"],
            })
    entries.sort(key=lambda e: _parse_ts(e["ts"]))
    return entries


def _governing_file_context_rules(
    entries: list[dict], since_ts: datetime, until_ts: datetime
) -> dict:
    """Merge file-context entries in (since_ts, until_ts] into a queried_by_file map.

    Mirrors _governing_plan's timestamp-selection structure. Window is
    strict-left / inclusive-right so a commit owns the rule blocks shown AFTER
    the previous commit and AT OR BEFORE its own time; the first commit uses a
    datetime.min floor so it admits everything at or before it. Per file the
    value is the set-union of rule ids across every in-window block for that path
    (via cache._merge_queried_by_file), keyed by normalize_path(abs).

    APPROXIMATION (documented intent): this is a MULTI-TURN UNION. A file shown
    rules across several tool uses in the window contributes the union of all
    those rule sets, with no way to know which write produced the committed
    content. queried_rule_ids is therefore a SUPERSET of the rules shown at the
    decisive write -- an over-attribution that never yields a false 'not shown'
    negative, acceptable for an audit aid."""
    merged: dict = {}
    for e in entries:
        ts = _parse_ts(e["ts"])
        if since_ts < ts <= until_ts:
            merged = _merge_queried_by_file(
                merged, {e["file_path"]: e["rule_ids"]}
            )
    return merged


async def harvest_one_commit(
    db, name: str, *,
    commit_hash: str, subject: str, author: str, branch: str,
    commit_ts: str,
    files: list[dict],
    plan_text: str | None,
    plan_ts: str | None,
    queried_by_file: dict | None = None,
    prior_claims: list[dict] | None = None,
    cwd: str = "",
) -> dict:
    """Materialize ONE commit's decision-memory records and edges.

    Shared by the manual backfill (harvest) and the post-commit path
    (commit_capture.capture_commit) so both produce byte-identical nodes/edges and
    the SAME content-hash Decision id (dedup: a re-run by either path re-MERGEs,
    never duplicates).

    name is the already-resolved project (caller runs ensure_project_registered).
    files is the git-authoritative changed-files list and is the phantom-path guard
    set for the prose plan parser. plan_text is the governing plan or None: None ->
    no Decision, Commit + FileChange still written with the commit subject as the
    fallback reason (fail-open). queried_by_file maps a path to its queried rule ids
    (post-commit path); None leaves queried_rule_ids empty (deferred to 3b).

    prior_claims (post-commit path only) is the resolve_reasons_for_files output:
    per-path {reason, decision_id, governing_rule_ids} from a pre-existing OPEN
    Decision (e.g. one the live approval path created). When a path has no governing
    plan Decision, its FileChange takes the prior claim's reason, snapshots the
    prior governing rules as cited_rule_ids, and wires MOTIVATED_BY / REALIZES to
    the prior Decision. The backfill caller passes None (no prior-claim context).
    """
    stats = {"decisions": 0, "filechanges": 0, "with_plan_reason": 0,
             "fallback_reason": 0, "decision_id": None, "unplanned_files": []}

    allowed_paths = {f["path"] for f in files}
    decision_id = None
    reason_by_path: dict[str, str] = {}
    plan_cited_rules: list[str] = []

    # Pre-existing open-Decision context indexed by path (post-commit path).
    prior_by_path = {c.get("path"): c for c in (prior_claims or []) if c.get("path")}

    if plan_text:
        harvested = harvest_plan(plan_text)
        # Phantom-path guard: keep only files the commit actually touched. Done here
        # (not inside harvest_plan) so the existing harvester tests that monkeypatch
        # harvest_plan with a one-arg lambda keep working; the post-commit and
        # phantom-path-guard tests exercise the real parser through this intersection.
        parsed_files = [
            f for f in harvested.get("files", [])
            if f.get("path") and f["path"] in allowed_paths
        ]
        if parsed_files:
            decision_id = _decision_id(name, plan_text)
            plan_cited_rules = harvested.get("cited_rules", [])
            reason_by_path = {f["path"]: (f.get("reason") or "")
                              for f in parsed_files if f.get("path")}
            await db.create_decision(
                decision_id=decision_id, project=name,
                title=(harvested.get("rationale", "")[:80] or "harvested plan"),
                rationale=harvested.get("rationale", ""),
                planned_files=[
                    {"path": f["path"], "reason": f.get("reason") or "", "resolved": False}
                    for f in parsed_files
                ],
                governing_rule_ids=harvested.get("cited_rules", []),
                phase="harvested", session_id="",
                ts=(plan_ts or commit_ts),
            )
            await db.wire_has_decision(name, decision_id, name)
            for rid in harvested.get("cited_rules", []):
                await db.wire_governed_by(decision_id, rid, name)
            stats["decisions"] = 1
            stats["decision_id"] = decision_id

    await db.create_commit(
        commit_hash=commit_hash, project=name, subject=subject,
        author=author, branch=branch,
    )
    await db.wire_has_commit(name, commit_hash, name)

    qbf = queried_by_file or {}
    for f in files:
        path = f["path"]
        prior = prior_by_path.get(path) or {}
        plan_reason = reason_by_path.get(path, "")
        if plan_reason:
            reason = plan_reason
            stats["with_plan_reason"] += 1
        elif prior.get("reason"):
            reason = prior["reason"]
            stats["fallback_reason"] += 1
        else:
            if decision_id is not None:
                reason = f"(not itemized in the plan; commit-level context) {subject}"
                stats["unplanned_files"].append(path)
            else:
                reason = subject
            stats["fallback_reason"] += 1
        # cited_rule_ids is the commit-time snapshot of the GOVERNING Decision's
        # rules: the plan Decision when it governs this path, else the pre-existing
        # open Decision's governing rules (post-commit), else [].
        if decision_id is not None and path in reason_by_path:
            cited_rule_ids = list(plan_cited_rules)
        else:
            cited_rule_ids = list(prior.get("governing_rule_ids") or [])
        change_id = _derive_change_id(name, commit_hash, path)
        await db.create_filechange(
            change_id=change_id, project=name, path=path,
            change_type=f["change_type"], commit_hash=commit_hash, reason=reason,
            queried_rule_ids=_queried_for_path(qbf, cwd, path) if qbf else [],
            cited_rule_ids=cited_rule_ids,
        )
        await db.wire_includes(commit_hash, change_id, name)
        await db.wire_has_change(name, change_id, name)
        if decision_id is not None and path in reason_by_path:
            await db.wire_motivated_by(change_id, decision_id, name)
            await db.wire_realizes(commit_hash, decision_id, name)
            await db.resolve_file_claims(name, path)
        elif prior.get("decision_id"):
            await db.wire_motivated_by(change_id, prior["decision_id"], name)
            await db.wire_realizes(commit_hash, prior["decision_id"], name)
        stats["filechanges"] += 1

    return stats


async def harvest(
    db, repo_cwd: str, *, since: str | None = None,
    claude_home: Path | None = None, runner=None,
) -> dict:
    """Harvest commits in <since>..HEAD into Decision/FileChange/Commit records.

    Capability A: commits + files from git (always). Capability B: per-file reasons
    from the approved plan recovered from the session transcripts, plus Decision +
    GOVERNED_BY/MOTIVATED_BY/REALIZES wiring. B fails soft: a file with no plan
    reason gets the commit subject. All writes MERGE-idempotent.
    """
    name = await ensure_project_registered(db, repo_cwd, runner=runner)
    if name is None:
        return {"project": None, "commits": 0, "filechanges": 0, "decisions": 0,
                "with_plan_reason": 0, "fallback_reason": 0}

    home = claude_home or (Path.home() / ".claude")
    # Claude Code names the projects/ transcript dir by the ABSOLUTE session cwd,
    # so a relative repo_cwd (e.g. ".") must be resolved or capability B is skipped.
    abs_cwd = os.path.abspath(repo_cwd)
    transcript_dir = _project_transcript_dir(abs_cwd, home)
    plans = _collect_plan_writes(transcript_dir)
    # 3b: per-file "rules the AI was shown", recovered mechanically from the same
    # transcripts. Collected ONCE; windowed per commit below. Fail-soft to [].
    file_ctx = _collect_file_context_rules(transcript_dir)
    commits = _git_commits(repo_cwd, since)

    stats = {"project": name, "commits": 0, "filechanges": 0, "decisions": 0,
             "with_plan_reason": 0, "fallback_reason": 0}
    seen: set[str] = set()

    # Align the produced queried key to the transcript's ABSOLUTE path: the key is
    # normalize_path(os.path.join(cwd, rel_path)) inside _queried_for_path, and the
    # transcript path is realpath-resolved, so cwd must be realpath too or the
    # symlinked-cwd join misses (the known path-key-mismatch edge).
    key_cwd = os.path.abspath(os.path.realpath(repo_cwd))

    prev_ts = datetime.min.replace(tzinfo=timezone.utc)
    for c in commits:
        plan = _governing_plan(plans, c["ts"])
        commit_dt = _parse_ts(c["ts"])
        queried_by_file = _governing_file_context_rules(
            file_ctx, prev_ts, commit_dt
        )
        per = await harvest_one_commit(
            db, name,
            commit_hash=c["hash"], subject=c["subject"], author=c["author"],
            branch=c["branch"], commit_ts=c["ts"], files=c["files"],
            plan_text=(plan["plan_text"] if plan else None),
            plan_ts=(plan.get("ts") if plan else None),
            queried_by_file=queried_by_file, cwd=key_cwd,
        )
        prev_ts = commit_dt
        for path in per.get("unplanned_files", []):
            _log_friction_event(
                "", None, "committed_file_not_in_plan",
                file_path=path, commit_hash=c["hash"], project=name,
            )
        stats["commits"] += 1
        stats["filechanges"] += per["filechanges"]
        stats["with_plan_reason"] += per["with_plan_reason"]
        stats["fallback_reason"] += per["fallback_reason"]
        if per["decision_id"] is not None and per["decision_id"] not in seen:
            seen.add(per["decision_id"])
            stats["decisions"] += 1

    return stats
