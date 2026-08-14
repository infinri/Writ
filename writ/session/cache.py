"""Per-session cache I/O for the session helper.

POL-6b extracted the cache layer out of bin/lib/writ-session.py. POL-6b-2 resolves the
cache directory from WRIT_CACHE_DIR at call time (_cache_dir), so the functions take only
the session id (+data): submodules call them directly with no facade dependency or cycle,
and tests override with monkeypatch.setenv("WRIT_CACHE_DIR", ...). CACHE_DIR is an
import-time snapshot kept ONLY for the server health endpoint / ensure-server.sh desync
display (server.py reads getattr(writ_session, "CACHE_DIR")); it does not drive I/O. stdlib only.
"""

import fcntl
import glob
import json
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from writ.session.config import (
    DEFAULT_ALWAYS_ON_CAP,
    DEFAULT_SESSION_BUDGET,
    _CITATION_LOG_MAX,
)


# Default session-state root: the skill install's `var/session`, derived from this
# module's own __file__ so it follows the install rather than assuming a fixed home
# layout. parents[2] is the skill dir containing the `writ` package
# (parents[0]=writ/session, parents[1]=writ, parents[2]=<skill>). Identical
# derivation to writ/shared/logging.py::_DEFAULT_LOG_ROOT.
#
# NOT tempfile.gettempdir(). /usr/lib/tmpfiles.d/tmp.conf declares `D /tmp`, and the
# capital D means systemd EMPTIES the directory at boot -- so every session cache was
# destroyed on reboot and a resumed conversation silently lost its mode, gates, and
# loaded_rule_ids (the "mode=None" wipe). Measured 2026-07-23: 341 session caches
# existed, every one postdating the boot, zero predating it. The loss was invisible
# because a MISSING cache is not an error: _read_cache returns _default_cache()
# before its try block, so nothing raised and nothing logged.
# Built with os.path, not pathlib: this module is on the per-hook hot path and
# importing pathlib here costs ~5.6ms per spawn (it pulls urllib.parse + ipaddress),
# which would undo the import-cost fix made for exactly this reason. `os` is already
# imported. The three dirnames walk writ/session/cache.py -> writ/session -> writ -> <skill>.
_SKILL_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
_DEFAULT_CACHE_DIR = os.path.join(_SKILL_ROOT, "var", "session")


def _cache_dir() -> str:
    """Resolve the session-cache directory from WRIT_CACHE_DIR at call time."""
    return os.environ.get("WRIT_CACHE_DIR", _DEFAULT_CACHE_DIR)


def resolve_current_session_id() -> str | None:
    """Resolve the CURRENT session id from the process's own environment, or None.

    Order (first non-empty wins):
      1. $CLAUDE_SESSION_ID          (per-process; authoritative if CC sets it)
      2. basename($CLAUDE_JOB_DIR)   (per-job; concurrency-safe; trailing / stripped)
    Returns None when neither resolves. Each signal is individually guarded so a bad
    value never propagates: the resolver NEVER raises, and it never guesses.

    THERE IS NO THIRD TIER, and the two that were here were wrong in the same way. Tier 3
    read /tmp/writ-current-session, ONE file per machine that every Claude Code session's
    UserPromptSubmit hook overwrites, so it named whichever session took a turn most
    recently. Tier 4 took the newest writ-session-*.json by mtime out of a cache directory
    every project shares. Both answered "which session am I" with "whichever one moved
    last", and both did so live: the user's magento project reported `work` mode it had
    never set, and a stray test run left the pointer naming
    `tier-embed-directive-597898df`, a session with no cache anywhere, which the mtime
    glob could not tell apart from a real id. A wrong answer here is not a failed read, it
    is a mode, an approval or an audit coverage scope written into somebody else's
    session, and nothing in any log shows it happened. Callers get None and fail loud
    instead (the CLI exits 2 naming the two env vars; the doctor reports no session).
    """
    # BOTH TIERS BELOW ARE UNREACHABLE FROM A BASH TOOL CALL, and the variable that WOULD
    # answer is the one you must not read. Measured 2026-08-11 against Claude Code 2.1.227:
    # CLAUDE_SESSION_ID and CLAUDE_JOB_DIR are NEVER exported, so this resolver returns
    # None in practice and callers fail loud. CLAUDE_CODE_SESSION_ID *is* exported, and in
    # a main session it equals the id hooks write state under, which makes it look like the
    # fix. It is not: probed from inside a real sub-agent it still holds the PARENT's id,
    # while Writ keys a sub-agent by its agent_id. Reading it would let a sub-agent resolve
    # to its parent and approve or clear the PARENT's gates -- the same class of
    # cross-session write that deleting tiers 3 and 4 closed. CLAUDE_CODE_CHILD_SESSION=1
    # is a flag, not an id, and is set in the main session too, so it cannot tell parent
    # from child either. The refusal therefore stands: the remedy is the explicit session
    # argument at the call site, NOT another environment read.
    #
    # 1. per-process env id (empty string is treated as unset)
    try:
        env_sid = os.environ.get("CLAUDE_SESSION_ID", "")
        if env_sid:
            return env_sid
    except Exception:
        pass

    # 2. basename of the per-job dir (trailing slash stripped first)
    try:
        job_dir = os.environ.get("CLAUDE_JOB_DIR", "")
        if job_dir:
            base = os.path.basename(job_dir.rstrip("/"))
            if base:
                return base
    except Exception:
        pass

    return None


# Import-time snapshot for the server health endpoint / desync display only (not the I/O
# source). The daemon's env is fixed at startup, so this equals _cache_dir() there.
CACHE_DIR = _cache_dir()


def _ensure_cache_dir() -> str:
    """Return the cache dir, creating it if absent.

    /tmp always existed, so nothing on the write path ever had to create this. The
    default now lives under the skill install, which does NOT exist on a fresh
    checkout -- and a failed write would land right back in the silent-blank-session
    behavior this move exists to remove. Read paths deliberately do not call this: a
    missing dir there is just "no cache yet".
    """
    d = _cache_dir()
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass  # Surfaced by the caller's own write failure, not swallowed here.
    return d


def _cache_path(session_id: str) -> str:
    return os.path.join(_cache_dir(), f"writ-session-{session_id}.json")


def _migrate_command_log(data: dict) -> None:
    """INV-2: fold a legacy 7a command_log into the unified citation_log.

    Old command rows {command, exit_code, output_excerpt, ts} become
    command-type citations {artifact_type, ref, excerpt, exit_code, ts}.
    Runs only when a command_log key is present; idempotent thereafter
    (the key is removed once migrated).
    """
    legacy = data.pop("command_log", None)
    if not legacy:
        return
    log = data.setdefault("citation_log", [])
    for row in legacy:
        if not isinstance(row, dict):
            continue
        log.append({
            "artifact_type": "command",
            "ref": str(row.get("command", "")),
            "excerpt": str(row.get("output_excerpt", "")),
            "exit_code": row.get("exit_code", 0),
            "ts": row.get("ts", ""),
        })
    data["citation_log"] = log[-_CITATION_LOG_MAX:]


def _default_cache() -> dict:
    """The full session-cache schema with defaults -- the SINGLE source of truth.

    _read_cache uses this both as the no-file / parse-error return and to fill any
    missing key on a loaded cache, so a new session and a reread session always carry
    an identical keyset (previously current_phase/gates_approved/loaded_rule_ids_by_phase
    were only added on the file path, so new-session caches drifted without them). A
    fresh dict is built per call, so its mutable list/dict values are never aliased
    across sessions.
    """
    return {
        "loaded_rule_ids": [],
        # Rules injected by the always-on channel. Kept SEPARATE from loaded_rule_ids
        # because that field doubles as the ranked query's exclude list: 5 of the 12
        # always-on rules live in the ranked pool, so recording them there would stop
        # them being retrieved by relevance. _validate_phase_a unions both when checking
        # cited rule IDs, which is what stops the gate calling its own injected rules
        # hallucinated.
        "always_on_rule_ids": [],
        "loaded_rules": [],
        "remaining_budget": DEFAULT_SESSION_BUDGET,
        "context_percent": 0,
        "queries": 0,
        "mode": None,
        # WHO chose that mode: "explicit" (a human named it via `mode set`) or "auto" (the
        # writ-rag-inject.sh classifier chose it via `mode init`). None means the question
        # cannot be answered for this session -- a cache written before the field existed,
        # or a mode carried forward from one -- and every consumer must treat that like
        # "explicit" and leave the mode alone, never like "auto".
        #
        # It lives HERE, in the one function that defines the cache's shape, so a reread
        # cache and a fresh one carry the key alike; the values are stamped by
        # mode_engine._apply_mode_set (MODE_SOURCE_EXPLICIT / MODE_SOURCE_AUTO), never by a
        # call site. Without it, the two paths were indistinguishable after the fact: both
        # emit a mode_change row with change_type "set" and from_mode null.
        "mode_source": None,
        "is_subagent": False,
        "files_written": [],
        "analysis_results": {},
        "feedback_sent": [],
        "pending_violations": [],
        "invalidation_history": {},
        "escalation": {"gate": None, "needed": False, "diagnosis": None, "feedback_sent": False},
        "current_phase": None,
        "gates_approved": [],
        "loaded_rule_ids_by_phase": {},
        "pretool_queried_files": [],
        "paused_work_state": None,
        "is_orchestrator": False,
        # Cycle G one-shot: cmd_reset_after_compaction sets this on the real PostCompact
        # event and writ-rag-inject.sh clears it after emitting the verify-discipline
        # directive on the next UserPromptSubmit. It lives here, in the single schema
        # source, so a cache written before this cycle reads back False and simply never
        # fires, which is the correct degradation for a queue nobody filled.
        "post_compact_pending": False,
        "last_injected_rule_ids": [],
        "detected_domain": None,
        # Phase 1 additions per plan Section 6.1 deliverable 5. Track playbook
        # execution state for SDD/brainstorm workflows, verification evidence
        # for Gate 5 Tier 1, review ordering for SDD two-stage review, and
        # quality-judgment scores for Gate 5 Tier 2 (self-scored: the session
        # judges its own artifact; hooks/scripts/writ-quality-judge.sh emits the
        # directive).
        "active_playbook": None,
        "active_phase": None,
        "playbook_phase_history": [],
        "review_ordering_state": {},
        "verification_evidence": {},
        "citation_log": [],  # INV-2: unified artifact ledger (command/file/url citations)
        "coverage_scope": None,  # INV-4: frozen file-level denominator for the coverage map
        "source_type": None,  # INV-8: the investigation lens (code|web|runtime) for investigate mode
        # Phase 3: per-session phase-advance audit trail with confirmation_source
        # per plan Section 8 deliverable 3.
        "phase_transitions": [],
        "quality_judgment_state": {},
        "quality_override_count": 0,
        # Cycle 9: the latest writ-reviewer verdict, recorded at SubagentStop by
        # infrastructure rather than reported by the orchestrator. The Bash gate
        # reads it to confirm before a commit while CRITICAL findings stand.
        # None (not {}) means no reviewer has run, which is NOT a blocking state.
        "review_findings_state": None,
        "always_on_budget": DEFAULT_ALWAYS_ON_CAP,
        "always_on_tokens_used": 0,
        "queried_rules_by_file": {},
        "parent_session_id": "",
        "agent_type": "",
        # Project where the mode was declared (stamped at mode-set). Enables the
        # rotation carry's same-project guard; "" means "unknown project".
        "project_root": "",
    }


def _read_cache(session_id: str) -> dict:
    path = _cache_path(session_id)
    if not os.path.exists(path):
        return _default_cache()
    try:
        with open(path) as f:
            data = json.load(f)
        # INV-2: fold any legacy 7a command_log into the unified citation_log
        # (command-type rows) so 7a sessions carry over with zero loss.
        _migrate_command_log(data)
        # Fill any missing key from the single-source schema so a loaded cache always
        # carries the full keyset. Each _default_cache() call is fresh, so no mutable
        # default object is aliased into `data`.
        for key, value in _default_cache().items():
            data.setdefault(key, value)
        return data
    except (json.JSONDecodeError, OSError) as exc:
        # Stay fail-soft, but say so: this fallback presents downstream as a session
        # that lost its mode and gates, which is indistinguishable from a genuinely
        # new session. That ambiguity is what made the mode=None class of bug so
        # expensive to diagnose.
        from writ.shared.logging import emit_exception

        emit_exception("session.cache.read", exc, session_id, None, cache_path=path)
        return _default_cache()


def _write_cache(session_id: str, data: dict) -> None:
    """Atomically replace the session cache.

    Writes to a per-call UNIQUE temp file (tempfile.mkstemp, O_CREAT|O_EXCL) so
    two concurrent writers of the same session id -- the CLI subprocess racing
    the daemon, or the burst of hook processes a /compact or 'continue' retry
    spawns -- never share a temp path and never corrupt each other's write.
    fsync before the atomic os.rename makes the replacement crash-durable; a
    failed write unlinks its temp and leaves the prior cache intact
    (all-or-nothing). A torn read at `path` is therefore impossible, so
    _read_cache never falls back to the mode=None default over a live session.
    The temp name ends in .tmp, so it never matches the writ-session-*.json
    enumeration glob.
    """
    path = _cache_path(session_id)
    _ensure_cache_dir()  # fresh install: the default var/session tree may not exist yet
    dir_ = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(
        dir=dir_, prefix=f"writ-session-{session_id}.json.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _lock_path(session_id: str) -> str:
    """Advisory-lock file paired with the session cache (never a *.json name)."""
    return _cache_path(session_id) + ".lock"


# Per-thread set of session ids whose lock this thread already holds, so a nested
# mutate_cache(same id) in one thread reuses the held cache instead of taking a
# second fcntl.flock on the same lock (which would self-deadlock -- flock is keyed
# on the open file description, not the thread). Enables migrating writers that call
# one another (e.g. _mode_init -> _mode_set) onto mutate_cache.
_reentrant = threading.local()


@contextmanager
def mutate_cache(session_id: str):
    """Atomic, reentrant read-modify-write of one session cache.

    Acquires an exclusive advisory lock (fcntl.flock on a per-session .lock file),
    reads the cache, yields it for in-place mutation, then writes it back and releases
    the lock. flock is keyed on the open file description, so this serializes concurrent
    writers BOTH across daemon threads AND across the CLI subprocess (a threading.Lock
    could not do the latter). The write happens only on a clean exit: an exception in the
    body skips the write (partial mutations are never persisted) and propagates, matching
    the prior read/mutate/write-at-end call sites. Plain _read_cache stays lock-free so
    the per-prompt hot path is unaffected. The .lock file never matches the
    writ-session-*.json cache glob used by enumeration elsewhere.

    Reentrant per thread: a nested acquisition of the SAME session id yields the
    already-locked cache dict and defers the single write to the outermost block,
    so a migrated writer that calls another migrated writer cannot self-deadlock.
    A body exception still propagates and skips the write at every level.
    """
    held = getattr(_reentrant, "held", None)
    if held is None:
        held = {}
        _reentrant.held = held
    if session_id in held:
        # Reentrant: share the outer block's cache; the outermost owns the write.
        yield held[session_id]
        return
    # The lock is opened BEFORE _write_cache runs, so it needs the dir to exist too.
    _ensure_cache_dir()
    lock_fd = os.open(_lock_path(session_id), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        cache = _read_cache(session_id)
        held[session_id] = cache
        try:
            yield cache
            _write_cache(session_id, cache)
        finally:
            del held[session_id]
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _merge_queried_by_file(a: dict, b: dict) -> dict:
    """Union two queried_rules_by_file maps into a NEW dict (inputs untouched).

    Per path key the value is sorted(set(a) | set(b)); keys from both sides are
    covered. Used at commit time to combine the main session cache with the
    enumerated sub-agent caches.
    """
    out: dict = {}
    for key in set(a) | set(b):
        out[key] = sorted(set(a.get(key, [])) | set(b.get(key, [])))
    return out


def _collect_subagent_queried_rules(
    parent_session_id: str,
    committed_keys: set | None = None,
    since_ts: float = 0.0,
) -> dict:
    """Union queried_rules_by_file across sub-agent caches relevant to a commit.

    A sub-agent cache (is_subagent) contributes when EITHER:
      - its parent_session_id matches parent_session_id (the clean-session fast
        path, recency bypassed), OR
      - committed_keys is given, the cache was modified at or after since_ts, and
        it holds queried rules for at least one key in committed_keys (the path +
        recency fallback: robust when the global /tmp/writ-current-session link
        churns across CC processes or a restart, so parent_session_id no longer
        matches the committing session).
    When committed_keys is given, only those keys are attached (never the
    sub-agent's other files). Fail-open per child (skip a corrupt/mid-write file).
    Returns {} when nothing matches.
    """
    merged: dict = {}
    pattern = os.path.join(_cache_dir(), "writ-session-*.json")
    for path in glob.glob(pattern):
        if not re.fullmatch(r"writ-session-(.+)\.json", os.path.basename(path)):
            continue
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        qbf = data.get("queried_rules_by_file") or {}
        if not qbf:
            continue
        parent_match = (
            bool(parent_session_id)
            and data.get("is_subagent")
            and data.get("parent_session_id") == parent_session_id
        )
        path_match = False
        if committed_keys and not parent_match:
            try:
                recent = os.path.getmtime(path) >= since_ts
            except OSError:
                recent = False
            path_match = recent and bool(set(qbf) & committed_keys)
        if not (parent_match or path_match):
            continue
        contrib = (
            {k: v for k, v in qbf.items() if k in committed_keys}
            if committed_keys
            else qbf
        )
        merged = _merge_queried_by_file(merged, contrib)
    return merged


def record_transition(
    cache: dict,
    *,
    from_phase: str | None,
    to_phase: str | None,
    trigger: str,
    mode: str | None,
    **extra,
) -> None:
    """Append a phase-transition audit record to cache['phase_transitions'].

    Single source for the audit-trail entry shape (from/to/ts/trigger/mode)
    appended by mode set, mode switch (+restore), and user-approved gate advance.
    The caller still owns _write_cache; `extra` carries surface-specific fields
    (the approval path adds gate + artifacts_validated). Does not mutate the
    cache otherwise.
    """
    cache.setdefault("phase_transitions", []).append({
        "from": from_phase,
        "to": to_phase,
        "ts": datetime.now(timezone.utc).isoformat(),
        "trigger": trigger,
        "mode": mode,
        **extra,
    })
