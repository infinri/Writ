"""P1 logging router: one classify-and-append seam for every Writ writer.

Every event flows through `emit(stream, event, session_id, mode, **fields)`, which
classifies the event into exactly one typed stream (audit / friction / metrics) via
`STREAM_MAP`, builds the base record from `writ.shared.friction.base_friction_entry`
(single-source schema), sanitizes user-derived string values (SEC-INJ-LOG-001), and
appends one JSON line to `<root>/<project>/<stream>.jsonl` under a central Writ-owned
root (`WRIT_LOG_ROOT`, default `<skill>/var/logs`).

Project scope reuses the decision-memory identity (`derive_project_identity`) rather
than re-deriving git identity inline (ARCH-BOUNDARY-002); it stays stdlib-only and
daemon-free so fire-and-forget hook writers never touch Neo4j.

The router never raises on a write failure (ERR-GRACEFUL-001): an OSError on the
primary write degrades to a durable `<root>/_fallback.jsonl` (ERR-HANDLE-003) plus a
single stderr note, so a security event such as `memory_policy_deny` is never lost and
no hook is ever blocked.

`WRIT_FRICTION_LOG` set routes every stream to that one file (back-compat with the
test-suite isolation fixture and single-log operators).

stdlib only; lowest shared layer (like `writ.shared.friction`) -- session, analysis,
and bin writers import DOWN into it (ARCH-LAYER-001).
"""

from __future__ import annotations

import contextvars
import fcntl
import json
import os
import re
import sys
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from writ.session.git_identity import NotInRepoError, derive_project_identity
from writ.shared.friction import base_friction_entry

# Single-source rotation threshold (~50 MB) shared by the router's source-side
# roll and the scheduled sweep (writ.session.log_rotation imports it DOWN, so the
# two can never drift; DRY-CONFIG-001 / CLEAN-NAME-001). A stream file at or over
# this size is rolled into archive/ before the next append.
ROTATE_SIZE_BYTES = 50 * 1024 * 1024

# Cap on the traceback recorded by emit_exception. Tail-biased when applied, so the
# innermost frames survive; an uncapped traceback is how the errors stream would
# become the next unbounded log.
_TRACEBACK_MAX_CHARS = 2000

# Classification taxonomy (## Analysis STREAM_MAP): audit = governance decisions
# (immutable compliance), friction = "worth fixing" signal, metrics = high-volume
# operational telemetry. Every event maps to exactly one stream.
STREAM_MAP: dict[str, str] = {
    # audit
    "write_attempt": "audit",
    "gate_denial": "audit",
    "gate_deny": "audit",
    "gate_denied_then_approved": "audit",
    "mode_change": "audit",
    "phase_advance": "audit",
    "phase_transition": "audit",
    "agent_self_approval_blocked": "audit",
    "candidate_promoted": "audit",
    "quality_judgment": "audit",
    "memory_policy_deny": "audit",
    # Partial vector-cache degeneracy. On audit rather than metrics for the same
    # reason as the evidence rows below: this IS the oversight record. It lived
    # only in a _logger.warning, invisible at default level, which is how 313
    # zero-norm vectors served noise through the heaviest ranking signal for six
    # days and still read as a healthy start.
    "index_degeneracy": "audit",
    # Transport census (E2a). One row per state-touching request that arrived over
    # the PUBLIC transport, which is the list the socket migration has to cover.
    # Audit rather than metrics: it is evidence about who can write to the daemon.
    "daemon_tcp_write": "audit",
    # Evidence, on audit rather than metrics: these ARE the oversight record. Both lived
    # only in the session cache, and citation_log is additionally trimmed to a cap, so the
    # proof behind a completion claim was the most perishable data Writ held.
    "verification_evidence": "audit",
    "citation_recorded": "audit",
    "committed_file_not_in_plan": "audit",
    "read_blocked": "audit",
    # The runtime-lens read/search refusal (writ/session/gates.py::_can_read_code_check).
    # A SEPARATE event from read_blocked, deliberately: writ/analysis/token_audit.py::
    # attribute_prevented sums prevented_tokens_floor and counts blocked_count over
    # read_blocked rows, and a lens deny carries no byte estimate, so reusing that name
    # would inflate a published number with zero-token rows. Registered here EXPLICITLY
    # because an unregistered event falls to _DEFAULT_STREAM (friction), which would file
    # a governance refusal where nobody audits.
    "read_denied": "audit",
    # Every gate's allow/deny, emitted on BOTH branches by log_gate_decision.
    "gate_decision": "audit",
    "exitplanmode_allow": "audit",
    "exitplanmode_denial": "audit",
    "debug_gate_root_cause_populated": "audit",
    "debug_gate_source_edit_denied": "audit",
    "tier_escalated": "audit",
    "session_end": "audit",
    # A human re-opening planning, and the refusals of that request. Audit, not friction:
    # the row IS the oversight record for a destructive governance act (two approved gates
    # cleared on the strength of one typed phrase), and it is the only durable proof that
    # the user asked for it rather than the agent. Registered here EXPLICITLY because an
    # unregistered event falls to _DEFAULT_STREAM (friction), which would file the
    # governance record where nobody audits.
    "plan_reopened": "audit",
    "plan_reopen_refused": "audit",
    # ── The approval-credential family: one family, ONE stream ──────────────
    # Every refusal that decides whether a human approved an action, plus the two
    # authority changes those refusals guard. Registered here EXPLICITLY because an
    # unregistered event falls to _DEFAULT_STREAM (friction), which would file a
    # governance decision where nobody audits.
    #
    # An exact `approved` with no approval request in the preceding assistant turn: no
    # mint, no advance, one question. THIS ROW IS THE RECORD OF THE TURN, and it is the
    # only one: the existing approval_pattern_match friction row is deliberately not
    # written alongside it, because that row's `outcome` vocabulary describes what an
    # advance attempt did and no advance was attempted.
    "approval_evidence_missing": "audit",
    # The `approved anyway` waiver. Recorded so a systemic transcript failure that forces
    # every user onto the override phrase is MEASURABLE rather than invisible.
    "approval_evidence_override": "audit",
    # The rule-promotion path in the CLI (`writ review <rule_id> --promote`), one event
    # per refusal class so a fail-closed gate can never read like an absent one, plus the
    # successful authority change.
    "rule_promotion_gate_bound": "audit",
    "rule_promotion_claim_lost": "audit",
    "rule_promoted": "audit",
    # THE FOUR PRE-EXISTING BINDING REFUSALS, registered with their new sibling rather
    # than left on the friction default. Leaving them there while the newest member of the
    # same family is audited would mean a reader has to know which of the family is
    # compliance-grade and which is signal. gate_token_rule_mismatch is the new one; the
    # other four have been emitted by the two gate routes and the CLI since cycle 1.
    "gate_token_unbound": "audit",
    "gate_token_gate_mismatch": "audit",
    "gate_token_plan_drift": "audit",
    "gate_token_candidate_mismatch": "audit",
    "gate_token_rule_mismatch": "audit",
    "candidate_promotion_gate_bound": "audit",
    # friction
    "repeated_denial": "friction",
    "hallucinated_rule_ids": "friction",
    "approval_pattern_miss": "friction",
    "approval_pattern_match": "friction",
    "subagent_type_fallback": "friction",
    # A user-role entry in a sub-agent's transcript carries a free-text block it should not
    # have: the structural signature of Claude Code splicing a queued user keystroke
    # into the sub-agent's pending turn instead of holding it for the parent. Emitted by
    # hooks/scripts/writ-subagent-stop.sh, attributed to the PARENT session, and carrying
    # structure only (path, line, digest, counts) -- never the foreign text. Friction, not
    # errors: it is a harness defect Writ cannot fix, only refuse to let pass unrecorded.
    # The same finding also reaches the errors stream via writ_critical.
    "foreign_input_in_subagent_turn": "friction",
    "decision_capture_failed": "friction",
    "commit_capture_failed": "friction",
    "memory_capture_failed": "friction",
    "recall_failed": "friction",
    "git_hooks_auto_install_failed": "friction",
    "debug_to_work_handoff": "friction",
    # Emitted from writ/session/session_lifecycle.py; they reached friction only
    # via _DEFAULT_STREAM. Same destination, stated rather than inferred.
    "pre_compaction": "friction",
    "post_compaction": "friction",
    # errors: a caught exception is not workflow friction. Own stream, own
    # retention (see log_rotation.RETENTION_DAYS).
    "exception": "errors",
    # A CRITICAL condition that is NOT an exception: an invariant the code depends on
    # turned out to be false, and the operation was abandoned rather than guessed.
    #
    # Added because Writ had no way to say this. Hooks that could not identify their
    # session quietly fell back to a global pointer file or to a synthesized id, so a
    # wrong-session read looked exactly like a normal run. The fallbacks are gone; the
    # hook now records this and does nothing, which is loud instead of silently wrong.
    "critical_error": "errors",
    # metrics
    "hook_execution": "metrics",
    # One row per daemon HTTP request (route/status/duration), from the server's
    # _request_telemetry middleware. Declared here even though the emitter passes an
    # explicit stream, so the map stays a complete inventory of live events (audit C3
    # was live events missing from it).
    "daemon_request": "metrics",
    # Retrieval quality, from the daemon's /query call site: mode (including the S4
    # "abstained"), rule_count, abstain_signal. Emitted for every query so an abstention
    # RATE is computable, not just its occurrences.
    "retrieval_result": "metrics",
    # HNSW index cache outcome, once per pipeline build. A miss means the process is
    # about to bulk-encode the whole corpus.
    "hnsw_cache": "metrics",
    # BM25 index cache outcome, same cadence and reason as hnsw_cache.
    "bm25_cache": "metrics",
    # Which writ.toml was resolved and what it contributed, once per process per path.
    # Key NAMES only: that file holds neo4j.password and bitbucket.token.
    "config_resolved": "metrics",
    "rag_query": "metrics",
    "always_on_inject": "metrics",
    "subagent_start": "metrics",
    "subagent_complete": "metrics",
    # Emitted when a hook running inside a sub-agent seeds a cache SubagentStart never
    # created. Metrics, beside its lifecycle siblings, because the governance census
    # counts lazily seeded agents from it and reads that stream.
    "subagent_seeded": "metrics",
    "playbook_step_complete": "metrics",
    "phase_token_summary": "metrics",
    "phase_transition_time": "metrics",
    "token_snapshot": "metrics",
    "pressure_audit": "metrics",
    "cwd_changed": "metrics",
    "methodology_push": "metrics",
    # Per-dispatch telemetry from hooks/scripts/writ-subagent-start.sh; it was the
    # largest event reaching friction only through _DEFAULT_STREAM.
    "subagent_rules_injected": "metrics",
}

# Fail-safe default for any event absent from STREAM_MAP: friction (never dropped,
# never polluting the high-volume metrics or the compliance audit stream).
_DEFAULT_STREAM = "friction"

# Events whose top-level `mode` is NOT the session's governance mode.
#
# `mode` normally comes from base_friction_entry and means work / investigate / debug /
# review / conversation. `retrieval_result` reuses the key for the retrieval DELIVERY
# mode -- "standard", "summary", "full", "abstained" -- and any reader that takes `mode`
# off every event silently mixes the two. It did: the metrics report's session-mode
# histogram came out as {'work': 145, 'standard': 30, 'abstained': 3, ...}, and because
# the streams are concatenated with metrics last, a delivery mode also OVERWROTE the real
# mode of any session that had queried.
#
# Declared beside STREAM_MAP, the schema that creates the collision, so the next event to
# reuse the key is registered where its author is already looking. Named as the exception
# rather than listing the ~40 governance events, because a missing entry there would
# silently drop a real mode -- the same class of failure, pointed the other way. The JSON
# key itself is deliberately not renamed: `mode` is published on a live event.
NON_GOVERNANCE_MODE_EVENTS: frozenset[str] = frozenset({"retrieval_result"})

# Default log root: the Writ skill install's `var/logs`, so logs are co-located
# with the install and follow it wherever it lives (standard `var/` runtime
# convention). Derived from this module's own `__file__` -- not a fixed
# `Path.home()` layout -- so it tracks the install location. `parents[2]` is the
# skill dir that CONTAINS the `writ` package: parents[0]=writ/shared,
# parents[1]=writ (the package), parents[2]=<skill> (e.g. ~/.claude/skills/writ),
# resolving to <skill>/var/logs. Evaluated once at import; the `WRIT_LOG_ROOT`
# env override still wins (checked first in `log_root`).
_DEFAULT_LOG_ROOT = Path(__file__).resolve().parents[2] / "var" / "logs"


def log_root() -> Path:
    """The central Writ-owned log root: `WRIT_LOG_ROOT` env or `<skill>/var/logs`."""
    override = os.environ.get("WRIT_LOG_ROOT")
    if override:
        return Path(override)
    return _DEFAULT_LOG_ROOT


def stream_path(project: str, stream: str) -> Path:
    """The target file for a project's stream: `<root>/<project>/<stream>.jsonl`."""
    return log_root() / project / f"{stream}.jsonl"


def archive_dir(project: str) -> Path:
    """The per-project archive dir for rolled generations: `<root>/<project>/archive/`.

    The `project` segment is the one already sanitized by `resolve_project` /
    `_sanitize_segment`, so a hostile derived identity can never let the archive
    dir escape the log root (SEC-INJ-PATH-001).
    """
    return log_root() / project / "archive"


def archive_path(project: str, stream: str, day: date) -> Path:
    """The archive target for one dated generation: `archive/<stream>-<date>.jsonl`.

    `day` is a `datetime.date` (its ISO `str()` -- e.g. `2025-06-01` -- forms the
    date token). `stream` and `day` are internal, never user-derived, so only the
    `project` segment carries any traversal risk, and it is pre-sanitized.
    """
    return archive_dir(project) / f"{stream}-{day}.jsonl"


def stream_for(event: str) -> str:
    """Classify an event to its stream, defaulting unknown events to friction."""
    return STREAM_MAP.get(event, _DEFAULT_STREAM)


def _sanitize_segment(name: str) -> str:
    """Reduce an identity to a safe, non-escaping log-path scope (SEC-INJ-PATH-001).

    Strips CR/LF first. A clone-stable identity like `github.com/org/repo` is a
    legitimate nested scope, so interior '/' is preserved WHEN every segment is safe
    (no empty, '.', or '..' component and no leading slash). If any component is a
    traversal component ('..', '.', empty) the name is treated as hostile and fully
    flattened: '/' and '..' are removed so it can never escape the log root. Falls
    back to `UNRESOLVED_PROJECT` when nothing readable remains, which means EVERY
    segment degraded and not merely one of them: a mixed name keeps its readable
    segments (see the all-or-nothing note on that arm).

    BOTH fallbacks below used to be the literal 'writ'. That is the SAME default this
    cycle removed from `resolve_project`'s not-in-a-repo arms and from retrieval's
    `resolve_project_for_cwd` -- one decision, not three coincidences. A name that
    sanitizes down to nothing (an all-punctuation `WRIT_LOG_PROJECT` override, say)
    is an identity we failed to read, not evidence that this is Writ; filing its rows
    under the Writ skill's own scope is where its owner will never look for them.
    The row is still RECORDED, just never MISLABELLED: an unattributed row is a
    visible gap, a misattributed one is a silently wrong record.
    """
    cleaned = name.replace("\r", "").replace("\n", "")

    segments = cleaned.split("/")
    unsafe = any(seg in ("", ".", "..") for seg in segments)

    if unsafe:
        # Hostile / traversal name: collapse to a single flat segment.
        flat = re.sub(r"[^A-Za-z0-9._-]", "_", cleaned)
        flat = flat.replace("..", "_")
        flat = flat.strip("._-")
        # `strip("._-")` cannot leave a leading '_', so `flat` can never itself BE the
        # marker -- only an explicit arm (this one, or the all-degraded one below) ever
        # returns it, never a sanitized value that happens to look like it.
        return flat or UNRESOLVED_PROJECT

    # Safe nested identity: keep '/' separators, sanitize each segment's chars.
    safe_segments = []
    degraded = 0
    for seg in segments:
        seg = re.sub(r"[^A-Za-z0-9._-]", "_", seg)
        stripped = seg.strip("._-")
        if not stripped:
            # Nothing readable survived this component; '_' is a placeholder standing in
            # for a segment we could not read, not a name.
            degraded += 1
            stripped = "_"
        safe_segments.append(stripped)

    # ALL segments degraded: there is no identity left to file under, so answer with the
    # marker for the same reason the branch above and `resolve_project` do. Without this,
    # an all-punctuation name (`...`, `___`, `WRIT_LOG_PROJECT=%%%`) landed under the
    # literal scope '_' while a name that failed EARLIER landed under '_unresolved' --
    # two buckets for one condition, so "the rows we could not attribute" meant reading
    # both and knowing to.
    #
    # THE ESCALATION IS ALL-OR-NOTHING ON PURPOSE, and the tempting simplification
    # (escalate as soon as ANY segment degrades) is the one that loses information: in a
    # MIXED name like `___/writ`, the surviving segment is a real, readable part of the
    # identity, and `_/writ` still says which project this is while `_unresolved` throws
    # that away. Only when every component degrades is there nothing to preserve.
    if degraded == len(safe_segments):
        return UNRESOLVED_PROJECT

    result = "/".join(safe_segments)
    # Defensive only: every segment above is either real content or the '_' placeholder,
    # so `result` cannot be falsy here. Kept honest anyway.
    return result or UNRESOLVED_PROJECT


# The log scope for a row whose project could not be resolved at all.
#
# Rows must not be DROPPED when the project is unknown -- an unattributed row is a visible
# gap someone can go and explain, and losing it is losing evidence -- but they must not be
# attributed either. This scope keeps them, under a name that is plainly not a project.
#
# The leading underscore is what makes it uncollidable: derive_project_identity only ever
# yields `host/org/repo` or a directory name. That is also why this literal is returned
# WITHOUT passing through _sanitize_segment, which strips leading '._-' per segment and
# would quietly turn it into a plausible project name.
UNRESOLVED_PROJECT = "_unresolved"


def resolve_project(cwd: str | None = None) -> str:
    """Resolve the project scope name for the log path.

    Order: `WRIT_LOG_PROJECT` env override -> `derive_project_identity(cwd).name`
    (the clone-stable decision-memory identity) -> `UNRESOLVED_PROJECT`. A derived name is
    always sanitized to a safe path segment (SEC-INJ-PATH-001).

    THE UNRESOLVED CASE USED TO RETURN THE LITERAL 'writ', and that is the same
    hardcoded fail-open removed from `resolve_project_for_cwd` (which now answers "" for an
    unregistered cwd rather than defaulting to Writ): a project with no `.git` yet had
    every row of its own telemetry filed into the WRIT SKILL's log space, where its owner
    would never look for them and where they read as Writ's own history. That is how a
    reported symptom became invisible -- the rows existed, under another project's name.
    Being unfindable under an honest scope is a gap; being findable under the wrong one is
    a wrong record, so the marker replaces the default here for the same reason it did
    there. Readers resolve the same way they write (metrics, `writ logs`, the friction
    analysis all call this function), so a non-repo cwd reads back its own rows.
    """
    override = os.environ.get("WRIT_LOG_PROJECT")
    if override:
        return _sanitize_segment(override)

    try:
        _repo_root, _remote_url, name = derive_project_identity(cwd or os.getcwd())
    except NotInRepoError:
        return UNRESOLVED_PROJECT
    except OSError:
        # Unreadable / vanished cwd: the identity could not be TAKEN, which is not evidence
        # that this is Writ. Same answer as not-in-a-repo, for the same reason.
        return UNRESOLVED_PROJECT

    return _sanitize_segment(name)


# The project root the CURRENT request is about, or None when the caller's own cwd is the
# right answer. Read by `emit` and set only by `request_project_scope` below.
#
# WHY A CONTEXT VARIABLE AND NOT A PARAMETER ON emit(). `resolve_project()` derives the log
# scope from `os.getcwd()`, which is correct for every hook-side writer -- a hook is a
# subprocess launched in the user's project -- and wrong for the daemon, whose cwd is pinned
# to the skill directory by `WorkingDirectory` in its unit file. So a daemon route serving a
# write for project X filed X's rows under Writ's own log space. Measured before this was
# added: 33 of 51 `write_attempt` rows in Writ's audit stream carried paths belonging to
# another repo, while that repo's own audit stream showed no write-gate activity at all, so
# reading it to ask "is the gate running here" answered a confident no. That is the same
# harm `resolve_project`'s own docstring describes one layer down, and for the same reason:
# a row findable under the wrong scope is worse than one that is merely hard to find.
#
# The correct scope is a property of the CALLER, not of the emitting function, and the
# emitting functions (gates.py, mode_engine.py, approval_workflow.py) are reached from BOTH
# the daemon and the CLI helper. Threading a keyword through them would therefore have to be
# repeated at all 34 call sites and would fail in the wrong direction: a future site that
# omits it misfiles silently, which is precisely the defect being closed, and a misfiled row
# still reads as a valid row in someone else's log. Setting it once at the request boundary
# makes correct attribution the default for everything downstream.
_REQUEST_PROJECT_ROOT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "writ_request_project_root", default=None,
)


@contextmanager
def request_project_scope(project_root: str | None):
    """Attribute every event emitted in this block to the project at `project_root`.

    For daemon request handlers: the route knows which project it is serving (the session
    cache's `project_root`), and nothing beneath it does. A blank or None value is "no
    scope", leaving the existing cwd resolution in place, so an un-threaded caller and a
    request with no resolvable root behave exactly as they did before.

    `contextvars` rather than a plain attribute because the value must survive the thread
    hop: `/pre-write-check` evaluates the whole gate inside `asyncio.to_thread`, which
    copies the caller's context into the worker thread, so the `write_attempt` emitted deep
    inside `_can_write_check` inherits this scope with nothing threaded through. That
    propagation is load-bearing, so it is pinned by
    tests/test_log_project_scope_daemon.py::TestScopeCrossesTheThreadBoundary rather than
    assumed.

    Always restores the previous value, including when the body raises: a scope stranded by
    an exception would misattribute every later request in the process, which is a worse
    version of the bug this closes.
    """
    token = _REQUEST_PROJECT_ROOT.set(_clean_scope(project_root))
    try:
        yield
    finally:
        _REQUEST_PROJECT_ROOT.reset(token)


def _clean_scope(project_root: str | None) -> str | None:
    """Normalize a caller-supplied scope: blank and non-string both mean "no scope"."""
    if not isinstance(project_root, str):
        return None
    return project_root.strip() or None


def set_request_project_scope(project_root: str | None) -> None:
    """Set the request scope for the REST of the current context, with no restore.

    Safe ONLY where the caller already runs inside its own context copy, which covers both
    daemon shapes:

      * a synchronous worker under `asyncio.to_thread`, which runs its target via
        `contextvars.copy_context().run(...)`;
      * an async request handler, which runs as an `asyncio.Task`, and a Task copies the
        context at creation.

    In both cases the copy IS the isolation: a value set inside cannot reach the event
    loop's own context or any other request, so the missing restore costs nothing. That is
    also exactly why this must not be called from module scope or from a plain synchronous
    helper on the shared context -- there the value would persist and misattribute
    everything after it.

    Prefer `request_project_scope` anywhere a `with` block is practical. This exists because
    both daemon routes learn their project root deep inside long bodies with several early
    returns -- `/pre-write-check` a hundred lines into the worker it would have to wrap --
    and paying a second thread hop, a second cache read on the hottest gate path, or a
    hundred-line reindent to hoist that out is a worse trade than one documented setter.
    That the isolation actually holds is pinned by
    tests/test_log_project_scope_daemon.py::TestRouteScopeDoesNotEscape for both shapes,
    rather than left as an argument in a comment.
    """
    _REQUEST_PROJECT_ROOT.set(_clean_scope(project_root))


def _sanitize_value(value):
    """Strip raw CR/LF from string field values (SEC-INJ-LOG-001).

    A crafted value with embedded CR/LF must not be able to forge a second JSON log
    line; non-string values pass through untouched.
    """
    if isinstance(value, str):
        return value.replace("\r", "").replace("\n", "")
    return value


# Fields that survive a None value as an explicit JSON null instead of being dropped.
# `from_mode` is a mode_change row's provenance: dropped, the first mode set of a session
# (no previous mode) is indistinguishable from a row where the field was never recorded,
# and no consumer can tell the two apart afterwards. `mode_source` is the same field class
# and fails the same way: a switch row for a session whose mode predates the field carries
# None, and dropping it would make "we looked and there was no provenance" identical to
# "this row was written before provenance was recorded at all" -- which is exactly the
# ambiguity mode_source exists to end. Deliberately a narrow allowlist -- every other field
# still disappears when None, because many event types rely on that.
_KEEP_WHEN_NULL = ("from_mode", "mode_source")


def _build_entry(event: str, session_id: str, mode: str | None, fields: dict) -> dict:
    """Base schema {ts, session, mode, event} plus sanitized non-None fields.

    _KEEP_WHEN_NULL names the exceptions, which are written as an explicit null.
    """
    entry = base_friction_entry(session_id, mode, event)
    for key, value in fields.items():
        if value is None and key not in _KEEP_WHEN_NULL:
            continue
        entry[key] = _sanitize_value(value)
    return entry


def _append_line(path: Path, line: str) -> None:
    """Append one line to path, creating the parent dir. Raises OSError on failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)


def _archive_taken(path: Path) -> bool:
    """True when an archive generation already occupies this path, as either the
    uncompressed `.jsonl` or the gzipped `.jsonl.gz` the sweep may have produced."""
    return path.exists() or path.with_suffix(path.suffix + ".gz").exists()


def _unique_archive_dest(arc_dir: Path, stream: str, day: date) -> Path:
    """A collision-safe archive destination inside `arc_dir`: appends a numeric
    suffix when a `<stream>-<date>` generation already exists (as `.jsonl` or the
    gzipped `.jsonl.gz`), so a second roll on the same UTC day never clobbers the
    first.

    Shared, single-source collision logic (DRY-CONFIG-001): the router's
    source-side roll and the scheduled sweep
    (`writ.session.log_rotation._rotate_live`) both reach this through
    `locked_archive_rename`, so the same-day suffixing can never drift between
    them. Call it through that function rather than directly: the name it returns
    is only free until someone renames, so picking here and renaming in the
    caller is the race this helper cannot fix on its own.
    """
    base = arc_dir / f"{stream}-{day}.jsonl"
    if not _archive_taken(base):
        return base
    i = 1
    while True:
        candidate = arc_dir / f"{stream}-{day}-{i}.jsonl"
        if not _archive_taken(candidate):
            return candidate
        i += 1


@contextmanager
def archive_lock(arc_dir: Path):
    """Exclusive advisory lock over one archive dir, for the duration of a move.

    `fcntl.flock` on a `.rotate.lock` file, the same mechanism
    writ/session/cache.py uses to serialize session-cache writers. Fail-open on
    an unlockable directory (ERR-GRACEFUL-001): rotation must never block a hook,
    so a lock that cannot be taken degrades to the old unsynchronized behavior
    rather than dropping the roll.
    """
    lock_fd = None
    try:
        arc_dir.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(str(arc_dir / ".rotate.lock"), os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
    except OSError:
        if lock_fd is not None:
            os.close(lock_fd)
            lock_fd = None
    try:
        yield
    finally:
        if lock_fd is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)


def locked_archive_rename(src: Path, arc_dir: Path, stream: str, day: date) -> Path:
    """Move `src` into `arc_dir` under a free `<stream>-<day>` name, atomically.

    PICK AND RENAME ARE ONE STEP, and that is the whole point. Splitting them is
    what loses data: `_unique_archive_dest` returns a name that is free at the
    moment it looks, and the caller renames afterwards, so two rotators both
    resolve `<stream>-<day>.jsonl`, the first moves the old data there, the
    router recreates the live file, and the second moves THAT over the first's
    archive. A whole generation is gone, and `os.rename` reports nothing.

    Note that racing the same source is NOT the dangerous case: one rename wins
    and the loser's source is already gone, so it fails with ENOENT and the
    append proceeds. The loss needs the live file recreated in between, which is
    exactly what a busy logger does. Measured with two processes rolling one
    stream 15 times each before this existed: 15 of 30 generations lost.

    A lock around the pick alone would not have helped, since two sequential
    picks still return the same free name. Returns the destination the file now
    occupies.
    """
    with archive_lock(arc_dir):
        dest = _unique_archive_dest(arc_dir, stream, day)
        os.rename(src, dest)
        return dest


def _roll_if_oversize(project: str, stream: str, target: Path) -> None:
    """Source-side size cap: rename an at/over-threshold live stream file into
    `archive/<stream>-<UTCdate>.jsonl` before the caller appends, so no single
    stream file grows without bound.

    The size decision is a single `os.stat` -- never a read of the body
    (PERF-IO-001). Fail-open (ERR-GRACEFUL-001): a missing file, an uncreatable
    archive dir, or a failed rename is swallowed and the caller's append still
    proceeds -- rotation must never block a hook or drop an event.
    """
    try:
        size = os.stat(target).st_size
    except OSError:
        return  # nothing on disk yet (or unstattable): no roll needed
    if size < ROTATE_SIZE_BYTES:
        return
    try:
        today = datetime.now(timezone.utc).date()
        locked_archive_rename(target, archive_dir(project), stream, today)
    except OSError:
        return  # roll failed: leave the live file in place, append proceeds


def emit(
    stream: str | None,
    event: str,
    session_id: str,
    mode: str | None,
    /,
    **fields,
) -> None:
    """Classify and append one event; fire-and-forget, never raises.

    When `stream` is None it is resolved from STREAM_MAP (unknown -> friction). With
    `WRIT_FRICTION_LOG` set, every stream collapses to that single file (back-compat).
    On an OSError writing the primary, the event is preserved in `<root>/_fallback.jsonl`
    with a single stderr note, so no event is lost and no caller is blocked.
    """
    entry = _build_entry(event, session_id, mode, fields)
    # default=str: a caller-supplied field that is not JSON-native must not turn a
    # fire-and-forget log call into a TypeError at the call site. The docstring's
    # "never raises" is the contract every hook and converted except-handler relies on.
    line = json.dumps(entry, default=str) + "\n"

    friction_log = os.environ.get("WRIT_FRICTION_LOG")
    if friction_log:
        try:
            _append_line(Path(friction_log), line)
        except OSError:
            _fallback(line, event)
        return

    resolved_stream = stream if stream is not None else stream_for(event)
    # The request's project when a daemon route declared one, else this process's cwd.
    # See _REQUEST_PROJECT_ROOT on why the scope has to come from the caller. Passed
    # through resolve_project rather than used as a path segment directly, so the
    # sanitizing and the NotInRepoError fallback stay in one place (SEC-INJ-PATH-001).
    project = resolve_project(_REQUEST_PROJECT_ROOT.get())
    target = stream_path(project, resolved_stream)

    _roll_if_oversize(project, resolved_stream, target)

    try:
        _append_line(target, line)
    except OSError:
        _fallback(line, event)


def emit_exception(
    component: str,
    exc: BaseException,
    session_id: str = "",
    mode: str | None = None,
    **ctx,
) -> None:
    """Record a caught exception on the `errors` stream. Never raises.

    For handlers that must keep swallowing (a hook may not be blocked by a logging
    concern) but must stop being invisible. `component` is a stable dotted label for
    the failing site, e.g. `session.cache.read`; it is what makes an errors row
    greppable without parsing the traceback.

    Adds a record and nothing else: the caller keeps its own control flow and return
    value, so converting a handler cannot change behavior. Delegates to `emit`, so it
    inherits classification, the durable `_fallback.jsonl`, CR/LF sanitization, and
    the `WRIT_FRICTION_LOG` single-file collapse.

    The traceback is tail-truncated to `_TRACEBACK_MAX_CHARS`: an uncapped traceback
    in a JSON-lines file is the realistic way this stream becomes the next unbounded
    log, and the innermost frames (at the tail) are the ones worth keeping.
    """
    try:
        exc_type = type(exc).__name__
        message = str(exc)
    except Exception:
        exc_type, message = "UnknownError", ""
    try:
        # Imported HERE, not at module scope: `traceback` costs ~2ms to import and
        # this module is imported by friction-append.py on EVERY instrumented hook
        # spawn, while an exception is logged only rarely. At module scope it was a
        # measured ~2ms tax on every hook run for a path almost never taken.
        import traceback

        tb = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )
    except Exception:
        # A non-exception argument (or one with no __traceback__) still deserves a row.
        tb = ""
    emit(
        "errors",
        "exception",
        session_id,
        mode,
        component=component,
        exc_type=exc_type,
        message=message,
        traceback=tb[-_TRACEBACK_MAX_CHARS:],
        **ctx,
    )


def _fallback(line: str, event: str) -> None:
    """Preserve an event whose primary write failed (ERR-HANDLE-003).

    Appends to the durable `<root>/_fallback.jsonl` (off /tmp) and emits exactly one
    stderr note. If even the fallback is unwritable, emit the single note and return
    without raising (ERR-GRACEFUL-001) so a hook is never blocked by logging.
    """
    fallback_path = log_root() / "_fallback.jsonl"
    try:
        _append_line(fallback_path, line)
    except OSError:
        pass
    sys.stderr.write(
        f"writ.logging: primary write failed for event {event!r}; "
        f"appended to {fallback_path}\n"
    )


def _generation_lines(path: Path) -> list[str]:
    """Read one generation's raw lines, transparently decompressing `.gz`.

    Fail-soft on the whole file for the same reason the per-line parse is fail-soft:
    every caller is a read-only analyzer that today cannot fail, so a truncated or
    corrupt archive must cost its own rows and nothing else. `OSError` covers an
    unreadable file, `EOFError`/`gzip.BadGzipFile` a corrupt or non-gzip archive
    (BadGzipFile subclasses OSError, so it is already caught; EOFError is not).
    """
    import gzip

    try:
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                return fh.read().splitlines()
        return path.read_text(encoding="utf-8").splitlines()
    except (OSError, EOFError, UnicodeDecodeError):
        return []


def _stream_generations(project: str, stream: str) -> list[Path]:
    """Every readable generation of one stream, oldest first.

    Rotation moves history into `archive/<stream>-<date>.jsonl.gz` and leaves a fresh
    live file behind, so a reader that opens only `stream_path` reports an empty
    corpus the day after a sweep. Archives sort by their ISO date token, which is
    lexicographically ordered, and the live file is always the newest generation, so
    it goes last. Callers depend on this: `render_audit_json` reads `events[0]` and
    `events[-1]` as the first and last timestamps.

    The glob is anchored to `<stream>-` so one stream never picks up another's
    archives (`audit-*.jsonl.gz` cannot match `metrics-2026-07-21.jsonl.gz`).
    """
    generations: list[Path] = []
    arc = archive_dir(project)
    if arc.is_dir():
        generations.extend(sorted(arc.glob(f"{stream}-*.jsonl.gz")))
    live = stream_path(project, stream)
    if live.is_file():
        generations.append(live)
    return generations


def read_streams(project: str, streams) -> list[dict]:
    """Union + JSON-parse every generation of the named streams for a project.

    Covers both the live stream file and its rotated `archive/*.jsonl.gz`
    generations, so rotation never removes history from the analyzers' view. Skips
    malformed lines, unreadable files, and corrupt archives (each contributes zero
    rows); a project with no logs yields an empty list. Used by the analyzers/metrics
    readers to merge audit + friction + metrics without knowing the on-disk layout
    (SOLID-DIP-002).
    """
    events: list[dict] = []
    for stream in streams:
        for path in _stream_generations(project, stream):
            for raw in _generation_lines(path):
                if not raw.strip():
                    continue
                try:
                    events.append(json.loads(raw))
                except (json.JSONDecodeError, ValueError):
                    continue
    return events
