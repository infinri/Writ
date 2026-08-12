"""Hermetic unit tests for the P1 logging router (`writ/shared/logging.py`).

RED PHASE: writ/shared/logging.py does not exist yet. Every test here imports
the router's public API directly (emit, STREAM_MAP, stream_for, read_streams,
resolve_project, log_root, stream_path); until the module is created these
tests fail on import/AttributeError. That failure IS the expected outcome.

Pins every "## Capabilities" line in plan.md that concerns the router itself
(schema, STREAM_MAP classification, fallback durability, WRIT_FRICTION_LOG
back-compat, CR/LF sanitization, path sanitization, never-raises).

Hermetic: WRIT_LOG_ROOT is monkeypatched to tmp_path in every test; no live
Neo4j, no daemon, no real git subprocess (derive_project_identity is
monkeypatched where project resolution matters).
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

# Import the router's public API directly -- RED until writ/shared/logging.py exists.
# The import is wrapped so collection succeeds even when the module is entirely
# absent (a module-level ImportError would abort collection of this whole file
# AND any sibling file in the same pytest invocation, per pytest's
# "Interrupted: N errors during collection" behavior with no
# --continue-on-collection-errors flag). A missing module must still produce
# genuine per-test RED failures, not a suite-wide collection abort.
try:
    from writ.shared.logging import (
        emit,
        STREAM_MAP,
        stream_for,
        read_streams,
        resolve_project,
        log_root,
        stream_path,
    )
except ModuleNotFoundError as _import_error:

    class _MissingRouterModule:
        """Raises on every use so each test fails with a genuine, descriptive
        error (not a silent pass) until writ/shared/logging.py exists."""

        def __init__(self, error: Exception) -> None:
            self._error = error

        def _raise(self, *_args, **_kwargs):
            raise ModuleNotFoundError(
                "writ.shared.logging does not exist yet"
            ) from self._error

        __call__ = _raise
        __getitem__ = _raise
        __contains__ = _raise
        __iter__ = _raise

        def items(self):
            self._raise()

    emit = _MissingRouterModule(_import_error)
    STREAM_MAP = _MissingRouterModule(_import_error)
    stream_for = _MissingRouterModule(_import_error)
    read_streams = _MissingRouterModule(_import_error)
    resolve_project = _MissingRouterModule(_import_error)
    log_root = _MissingRouterModule(_import_error)
    stream_path = _MissingRouterModule(_import_error)


@pytest.fixture(autouse=True)
def _hermetic_log_env(tmp_path, monkeypatch):
    """Every test gets its own log root and a clean WRIT_* env slate."""
    monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path / "logs"))
    monkeypatch.delenv("WRIT_FRICTION_LOG", raising=False)
    monkeypatch.delenv("WRIT_LOG_PROJECT", raising=False)
    return tmp_path


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


# --- log_root / stream_path -------------------------------------------------


def test_log_root_honors_writ_log_root_env(tmp_path, monkeypatch):
    custom = tmp_path / "custom-logs"
    monkeypatch.setenv("WRIT_LOG_ROOT", str(custom))
    assert log_root() == custom


def test_log_root_defaults_to_skill_var_logs(monkeypatch):
    """The default root now lives under the skill install dir's `var/logs`
    (co-located with the install: `<skill_root>/var/logs`, derived from
    `writ/shared/logging.py`'s own `__file__`), not a fixed
    `~/.claude/writ/logs` path. TEST-REGRESSION-001: pins the relocated
    default so it cannot silently revert to the old home-relative path."""
    monkeypatch.delenv("WRIT_LOG_ROOT", raising=False)
    import writ.shared.logging as logging_module

    expected = Path(logging_module.__file__).resolve().parents[2] / "var" / "logs"
    result = log_root()
    assert result == expected
    assert result.parts[-2:] == ("var", "logs")
    assert result != Path.home() / ".claude" / "writ" / "logs"


def test_stream_path_joins_root_project_and_stream(tmp_path, monkeypatch):
    monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path))
    path = stream_path("my-project", "audit")
    assert path == tmp_path / "my-project" / "audit.jsonl"


# --- STREAM_MAP classification (## Analysis STREAM_MAP taxonomy) -----------


@pytest.mark.parametrize("event", [
    "write_attempt", "gate_denial", "gate_deny", "gate_denied_then_approved",
    "mode_change", "phase_advance", "phase_transition",
    "agent_self_approval_blocked", "candidate_promoted", "quality_judgment",
    "memory_policy_deny", "committed_file_not_in_plan", "read_blocked",
    "exitplanmode_allow", "exitplanmode_denial",
    "debug_gate_root_cause_populated", "debug_gate_source_edit_denied",
    "tier_escalated", "session_end",
])
def test_stream_map_classifies_audit_events(event):
    assert STREAM_MAP[event] == "audit"
    assert stream_for(event) == "audit"


@pytest.mark.parametrize("event", [
    "repeated_denial", "hallucinated_rule_ids", "approval_pattern_miss",
    "approval_pattern_match", "subagent_type_fallback",
    "decision_capture_failed", "commit_capture_failed", "memory_capture_failed",
    "recall_failed", "git_hooks_auto_install_failed", "debug_to_work_handoff",
])
def test_stream_map_classifies_friction_events(event):
    assert STREAM_MAP[event] == "friction"
    assert stream_for(event) == "friction"


# Audit item 6 (taxonomy cleanup): these three are emitted in production but
# reached their stream only through _DEFAULT_STREAM. pre/post_compaction keep
# the same destination, stated rather than inferred; subagent_rules_injected is
# per-dispatch telemetry and belongs in metrics, not friction.
@pytest.mark.parametrize("event,expected", [
    ("pre_compaction", "friction"),
    ("post_compaction", "friction"),
    ("subagent_rules_injected", "metrics"),
])
def test_previously_unmapped_live_events_are_mapped_explicitly(event, expected):
    assert STREAM_MAP[event] == expected
    assert stream_for(event) == expected


def test_instructions_loaded_is_dropped_from_the_map():
    """No emitter exists anywhere in the tree; a mapped event implies a producer."""
    assert "instructions_loaded" not in STREAM_MAP


# The errors stream (audit item 4): a dedicated stream so an OSError in the
# cache writer is not filed as workflow friction.
def test_errors_is_a_known_stream_with_its_own_retention():
    from writ.session.log_rotation import RETENTION_DAYS

    assert RETENTION_DAYS["errors"] == 365


def test_exception_event_routes_to_the_errors_stream():
    assert stream_for("exception") == "errors"


# Audit item C1: both events are unmapped. write_failure's only emitter
# (track-failed-writes.sh) was removed as dead because PostToolUseFailure
# Write|Edit never fires, and pre_write_decision was retired in 1.3. They keep
# routing to friction through _DEFAULT_STREAM, so any late-arriving row is
# still captured; they just no longer claim to be a measured signal.
@pytest.mark.parametrize("event", ["write_failure", "pre_write_decision"])
def test_retired_events_are_unmapped_but_still_route_to_friction(event):
    assert event not in STREAM_MAP
    assert stream_for(event) == "friction"


@pytest.mark.parametrize("event", [
    "hook_execution", "rag_query", "always_on_inject", "subagent_start",
    "subagent_complete", "playbook_step_complete", "phase_token_summary",
    "phase_transition_time", "token_snapshot", "pressure_audit",
    "cwd_changed", "methodology_push", "subagent_rules_injected",
])
def test_stream_map_classifies_metrics_events(event):
    assert STREAM_MAP[event] == "metrics"
    assert stream_for(event) == "metrics"


def test_stream_for_unknown_event_defaults_to_friction():
    assert "totally_unclassified_event_xyz" not in STREAM_MAP
    assert stream_for("totally_unclassified_event_xyz") == "friction"


def test_stream_map_has_no_event_mapped_to_two_streams():
    """Every event maps to exactly one stream, and that stream is a known one.

    The valid set is derived from RETENTION_DAYS rather than hardcoded, so adding a
    stream cannot leave this check silently pinned to the old vocabulary (it was,
    until `errors` was added).
    """
    from writ.session.log_rotation import RETENTION_DAYS

    for event, stream in STREAM_MAP.items():
        assert stream in RETENTION_DAYS, (event, stream)


# --- emit(): base schema + classification + write -----------------------


def test_emit_with_explicit_stream_writes_base_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path))
    monkeypatch.setenv("WRIT_LOG_PROJECT", "proj-a")
    emit("audit", "write_attempt", "sid-1", "work", file_path="/a/b.py", result="allow")
    rows = _read_jsonl(stream_path("proj-a", "audit"))
    assert len(rows) == 1
    row = rows[0]
    assert row["session"] == "sid-1"
    assert row["mode"] == "work"
    assert row["event"] == "write_attempt"
    assert row["file_path"] == "/a/b.py"
    assert row["result"] == "allow"
    assert "ts" in row and row["ts"]


def test_emit_drops_none_valued_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path))
    monkeypatch.setenv("WRIT_LOG_PROJECT", "proj-a")
    emit("friction", "write_failure", "sid-2", None, reason=None, rule_id="ENF-1")
    rows = _read_jsonl(stream_path("proj-a", "friction"))
    assert len(rows) == 1
    assert "reason" not in rows[0]
    assert rows[0]["rule_id"] == "ENF-1"
    assert rows[0]["mode"] is None


def test_emit_none_stream_classifies_via_stream_map(tmp_path, monkeypatch):
    monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path))
    monkeypatch.setenv("WRIT_LOG_PROJECT", "proj-b")
    emit(None, "mode_change", "sid-3", "work", to="work")
    # mode_change classifies to audit per STREAM_MAP; must NOT land in friction/metrics.
    assert _read_jsonl(stream_path("proj-b", "audit")) != []
    assert _read_jsonl(stream_path("proj-b", "friction")) == []
    assert _read_jsonl(stream_path("proj-b", "metrics")) == []


def test_emit_unknown_event_defaults_to_friction_stream(tmp_path, monkeypatch):
    monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path))
    monkeypatch.setenv("WRIT_LOG_PROJECT", "proj-c")
    emit(None, "some_brand_new_event", "sid-4", "work")
    assert len(_read_jsonl(stream_path("proj-c", "friction"))) == 1
    assert _read_jsonl(stream_path("proj-c", "audit")) == []
    assert _read_jsonl(stream_path("proj-c", "metrics")) == []


def test_emit_writes_one_line_per_call(tmp_path, monkeypatch):
    monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path))
    monkeypatch.setenv("WRIT_LOG_PROJECT", "proj-d")
    emit(None, "hook_execution", "sid-5", "work", hook_name="a", duration_ms=1)
    emit(None, "hook_execution", "sid-5", "work", hook_name="b", duration_ms=2)
    rows = _read_jsonl(stream_path("proj-d", "metrics"))
    assert len(rows) == 2
    assert [r["hook_name"] for r in rows] == ["a", "b"]


def test_emit_creates_project_dir_when_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path))
    monkeypatch.setenv("WRIT_LOG_PROJECT", "brand-new-project")
    assert not (tmp_path / "brand-new-project").exists()
    emit(None, "session_end", "sid-6", "work")
    assert (tmp_path / "brand-new-project").is_dir()
    assert stream_path("brand-new-project", "audit").is_file()


# --- resolve_project() -------------------------------------------------------


def test_resolve_project_honors_writ_log_project_override(monkeypatch):
    monkeypatch.setenv("WRIT_LOG_PROJECT", "explicit-override")
    assert resolve_project() == "explicit-override"


def test_resolve_project_uses_derive_project_identity_name(monkeypatch):
    monkeypatch.delenv("WRIT_LOG_PROJECT", raising=False)
    monkeypatch.setattr(
        "writ.shared.logging.derive_project_identity",
        lambda cwd: ("/repo/root", "git@github.com:org/repo.git", "github.com/org/repo"),
    )
    assert resolve_project(cwd="/repo/root/sub") == "github.com/org/repo"


def test_resolve_project_returns_unresolved_marker_when_not_in_repo(monkeypatch):
    """The literal-'writ' fallback was removed on purpose (same hardcoded fail-open
    removed from retrieval's resolve_project_for_cwd earlier in this cycle): filing
    an ungitted project's rows under the string "writ" put them in the Writ skill's
    OWN log space, where that project's owner would never look -- exactly what made
    a real reported incident invisible. resolve_project() must now answer the
    explicit UNRESOLVED_PROJECT marker instead, so the row is still RECORDED but
    never MISLABELLED as another project's."""
    from writ.session.git_identity import NotInRepoError
    from writ.shared.logging import UNRESOLVED_PROJECT

    monkeypatch.delenv("WRIT_LOG_PROJECT", raising=False)

    def _raise(cwd):
        raise NotInRepoError("not a repo")

    monkeypatch.setattr("writ.shared.logging.derive_project_identity", _raise)
    result = resolve_project(cwd="/tmp/not-a-repo")

    assert result == UNRESOLVED_PROJECT, (
        f"expected the UNRESOLVED_PROJECT marker (imported from writ.shared.logging, "
        f"not hardcoded here, so a future rename cannot silently pass) for a cwd with "
        f"no .git anywhere above it; got {result!r}"
    )
    assert result != "writ", (
        "resolve_project() returned the literal 'writ' for an ungitted cwd. That "
        "string names the Writ skill's OWN log scope, so this project's row would be "
        "mislabelled into it and read indistinguishably from a genuine Writ-repo "
        "event -- this is the specific defect this test exists to catch, not a "
        "hypothetical one."
    )


def test_resolve_project_returns_unresolved_marker_on_os_error(monkeypatch):
    """Same marker for the sibling except-arm: an unreadable/vanished cwd means the
    identity could not be TAKEN, which is not evidence that this is Writ either."""
    from writ.shared.logging import UNRESOLVED_PROJECT

    monkeypatch.delenv("WRIT_LOG_PROJECT", raising=False)

    def _raise(cwd):
        raise OSError("cwd vanished")

    monkeypatch.setattr("writ.shared.logging.derive_project_identity", _raise)
    result = resolve_project(cwd="/tmp/vanished-cwd")

    assert result == UNRESOLVED_PROJECT, (
        f"expected the UNRESOLVED_PROJECT marker for a cwd whose identity raised "
        f"OSError; got {result!r}"
    )
    assert result != "writ", (
        "resolve_project() returned the literal 'writ' on an OSError from "
        "derive_project_identity -- the same mislabelling defect as the "
        "NotInRepoError arm, just reached through the sibling except clause."
    )


def test_resolve_project_sanitizes_slash_and_dotdot(monkeypatch):
    """SEC-INJ-PATH-001: a resolved name containing '/' or '..' must not escape
    the log root when joined into a path."""
    monkeypatch.delenv("WRIT_LOG_PROJECT", raising=False)
    monkeypatch.setattr(
        "writ.shared.logging.derive_project_identity",
        lambda cwd: ("/repo", None, "../../etc/passwd"),
    )
    name = resolve_project(cwd="/repo")
    assert "/" not in name
    assert ".." not in name


def test_resolve_project_sanitizes_crlf(monkeypatch):
    monkeypatch.delenv("WRIT_LOG_PROJECT", raising=False)
    monkeypatch.setattr(
        "writ.shared.logging.derive_project_identity",
        lambda cwd: ("/repo", None, "evil\r\nname"),
    )
    name = resolve_project(cwd="/repo")
    assert "\r" not in name
    assert "\n" not in name


def test_resolve_project_env_override_is_also_sanitized(monkeypatch):
    """WRIT_LOG_PROJECT is user/operator-controlled but still passes through the
    same path-segment sanitizer as the derived name (SEC-INJ-PATH-001)."""
    monkeypatch.setenv("WRIT_LOG_PROJECT", "../escape")
    name = resolve_project()
    assert "/" not in name
    assert ".." not in name


# --- _sanitize_segment(): a name that sanitizes to nothing (last hardcoded "writ")--
#
# The literal 'writ' default was removed from resolve_project's not-in-a-repo and
# OSError arms earlier in this cycle (see test_resolve_project_returns_unresolved_marker_*
# above); _sanitize_segment's own fallback was the last place it still hid. Measured
# directly against both the pre-fix and post-fix function: a single segment made only
# of characters the sanitizer strips (e.g. "..." or "___") never reaches either
# fallback -- it takes the "safe" per-segment branch and defaults to the placeholder
# "_", in both versions, so those inputs pin nothing about this fix. The genuinely
# reachable case is the "unsafe" branch: a name whose split-on-'/' produces an empty,
# '.', or '..' component (an empty string, bare '.', '..', '/', or a traversal shape
# like '..//..') collapses to nothing and used to answer 'writ'. The sibling
# "safe"-branch fallback (`result or ...`) can never fire, because every safe segment
# already defaults to a non-empty placeholder before the join -- one live defect, not
# two; noted here rather than pinned as its own test, since a test cannot exercise an
# unreachable branch.


@pytest.mark.parametrize(
    "hostile_name", ["", ".", "..", "/", "..//..", "\n\n", "./."]
)
def test_sanitize_segment_of_a_hostile_or_empty_name_returns_the_unresolved_marker(
    hostile_name,
):
    """Each of these previously sanitized down to the literal 'writ'. That string
    names the Writ skill's OWN log scope, so an unrelated project's rows -- or a
    caller whose identity could not be read at all -- would file into it, exactly
    the mislabelling that made a real reported symptom undiagnosable. The fix answers
    the explicit UNRESOLVED_PROJECT marker instead, imported here rather than
    hardcoded, so a future rename of the marker cannot silently pass this test."""
    from writ.shared.logging import UNRESOLVED_PROJECT, _sanitize_segment

    result = _sanitize_segment(hostile_name)

    assert result != "writ", (
        f"_sanitize_segment({hostile_name!r}) returned the literal 'writ' -- the "
        f"specific wrong answer this test exists to catch; got {result!r}"
    )
    assert result == UNRESOLVED_PROJECT


def test_sanitize_segment_marker_is_unforgeable_from_user_input():
    """The marker cannot be aliased by a caller-supplied name: _sanitize_segment
    strips a leading '_' per segment, so the literal string UNRESOLVED_PROJECT
    itself sanitizes to a DIFFERENT, distinct value rather than to itself. A
    project genuinely named "_unresolved" therefore never collides with, and is
    never mistaken for, an unresolved row."""
    from writ.shared.logging import UNRESOLVED_PROJECT, _sanitize_segment

    result = _sanitize_segment(UNRESOLVED_PROJECT)

    assert result != UNRESOLVED_PROJECT
    assert result == "unresolved"


def test_sanitize_segment_of_an_ordinary_derived_identity_is_unchanged():
    """Control: the fix must not turn every name into the marker. An ordinary
    clone-stable identity (the shape derive_project_identity actually returns)
    passes through unchanged, so this suite cannot pass against an
    implementation that returns UNRESOLVED_PROJECT unconditionally."""
    from writ.shared.logging import UNRESOLVED_PROJECT, _sanitize_segment

    result = _sanitize_segment("github.com/infinri/Writ")

    assert result == "github.com/infinri/Writ"
    assert result != UNRESOLVED_PROJECT


def test_sanitize_segment_of_the_literal_writ_name_still_maps_to_itself():
    """The fix removes 'writ' as a FALLBACK default, not as a legitimate project
    name: a project genuinely named 'writ' must still resolve to 'writ', not to
    the unresolved marker."""
    from writ.shared.logging import UNRESOLVED_PROJECT, _sanitize_segment

    result = _sanitize_segment("writ")

    assert result == "writ"
    assert result != UNRESOLVED_PROJECT


# --- Fallback durability -----------------------------------------------------


def test_emit_falls_back_to_fallback_jsonl_when_primary_unwritable(tmp_path, monkeypatch):
    """A security event like memory_policy_deny must never be silently lost:
    when the primary stream file can't be written, it lands in
    <root>/_fallback.jsonl instead."""
    root = tmp_path / "logs"
    root.mkdir()
    monkeypatch.setenv("WRIT_LOG_ROOT", str(root))
    monkeypatch.setenv("WRIT_LOG_PROJECT", "locked-proj")
    project_dir = root / "locked-proj"
    project_dir.mkdir()
    # Make the project dir unwritable so appending audit.jsonl raises OSError.
    project_dir.chmod(stat.S_IREAD | stat.S_IEXEC)
    try:
        emit(None, "memory_policy_deny", "sid-7", "work", rule_id="MEM-1")
    finally:
        project_dir.chmod(stat.S_IRWXU)
    fallback_rows = _read_jsonl(root / "_fallback.jsonl")
    assert len(fallback_rows) == 1
    assert fallback_rows[0]["event"] == "memory_policy_deny"
    assert fallback_rows[0]["rule_id"] == "MEM-1"


def test_emit_fallback_path_is_off_tmp(monkeypatch):
    """The PRODUCTION default log root (and thus the fallback) is durable: under
    the user's home, never a hardcoded /tmp. Exercises log_root() with no env
    override (the prior version asserted against pytest's own tmp_path string,
    which is itself under /tmp, so it tested the harness, not the code)."""
    from writ.shared.logging import log_root

    monkeypatch.delenv("WRIT_LOG_ROOT", raising=False)
    root = log_root()
    assert "/tmp/" not in str(root)
    assert str(root).startswith(str(Path.home()))
    assert (root / "_fallback.jsonl").name == "_fallback.jsonl"


def test_emit_never_raises_on_unwritable_primary(tmp_path, monkeypatch, capsys):
    """ERR-GRACEFUL-001: emit is fire-and-forget. It must not raise even when
    both the primary AND the fallback directory are unwritable; a caller/hook
    must never be blocked by a logging failure."""
    root = tmp_path / "logs"
    root.mkdir()
    root.chmod(stat.S_IREAD | stat.S_IEXEC)
    monkeypatch.setenv("WRIT_LOG_ROOT", str(root))
    monkeypatch.setenv("WRIT_LOG_PROJECT", "unwritable-proj")
    try:
        emit(None, "memory_policy_deny", "sid-8", "work")  # must not raise
    finally:
        root.chmod(stat.S_IRWXU)
    captured = capsys.readouterr()
    assert captured.err != "" or captured.out != ""  # a single stderr/stdout note is emitted


def test_emit_writes_one_stderr_line_on_fallback(tmp_path, monkeypatch, capsys):
    root = tmp_path / "logs"
    root.mkdir()
    monkeypatch.setenv("WRIT_LOG_ROOT", str(root))
    monkeypatch.setenv("WRIT_LOG_PROJECT", "locked-proj2")
    project_dir = root / "locked-proj2"
    project_dir.mkdir()
    project_dir.chmod(stat.S_IREAD | stat.S_IEXEC)
    try:
        emit(None, "memory_policy_deny", "sid-9", "work")
    finally:
        project_dir.chmod(stat.S_IRWXU)
    captured = capsys.readouterr()
    stderr_lines = [ln for ln in captured.err.splitlines() if ln.strip()]
    assert len(stderr_lines) == 1


# --- WRIT_FRICTION_LOG back-compat -------------------------------------------


def test_writ_friction_log_routes_every_stream_to_one_file(tmp_path, monkeypatch):
    """When WRIT_FRICTION_LOG is set, audit/friction/metrics events all land in
    that single file, ignoring the split (preserves conftest isolation fixture
    behavior for the existing test suite)."""
    single_log = tmp_path / "workflow-friction.log"
    monkeypatch.setenv("WRIT_FRICTION_LOG", str(single_log))
    monkeypatch.setenv("WRIT_LOG_PROJECT", "irrelevant-when-friction-log-set")

    emit(None, "mode_change", "sid-10", "work")       # audit
    emit(None, "write_failure", "sid-10", "work")     # friction
    emit(None, "hook_execution", "sid-10", "work")    # metrics

    rows = _read_jsonl(single_log)
    assert len(rows) == 3
    assert {r["event"] for r in rows} == {"mode_change", "write_failure", "hook_execution"}


def test_writ_friction_log_set_produces_no_split_stream_files(tmp_path, monkeypatch):
    root = tmp_path / "logs"
    single_log = tmp_path / "workflow-friction.log"
    monkeypatch.setenv("WRIT_LOG_ROOT", str(root))
    monkeypatch.setenv("WRIT_FRICTION_LOG", str(single_log))
    monkeypatch.setenv("WRIT_LOG_PROJECT", "proj-e")

    emit(None, "gate_denial", "sid-11", "work")

    assert single_log.is_file()
    assert not stream_path("proj-e", "audit").exists()


# --- CR/LF sanitization (SEC-INJ-LOG-001) ------------------------------------


def test_emit_strips_raw_crlf_from_string_field_value(tmp_path, monkeypatch):
    """A crafted field value with embedded CR/LF must not be able to forge a
    second JSON line -- raw \\r\\n are stripped from the value before
    serialization."""
    monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path))
    monkeypatch.setenv("WRIT_LOG_PROJECT", "proj-f")
    malicious = 'legit text\r\n{"event": "forged_admin_event"}'
    emit(None, "write_attempt", "sid-12", "work", file_path=malicious)

    raw_lines = stream_path("proj-f", "audit").read_text().splitlines()
    assert len(raw_lines) == 1  # the forged line was never allowed to become a second line
    row = json.loads(raw_lines[0])
    assert "\r" not in row["file_path"]
    assert "\n" not in row["file_path"]


def test_emit_sanitizes_multiple_string_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path))
    monkeypatch.setenv("WRIT_LOG_PROJECT", "proj-g")
    emit(
        None, "write_failure", "sid-13", "work",
        prompt="line1\nline2",
        reason="bad\rreason",
    )
    row = _read_jsonl(stream_path("proj-g", "friction"))[0]
    assert "\n" not in row["prompt"]
    assert "\r" not in row["reason"]


def test_emit_leaves_non_string_fields_untouched(tmp_path, monkeypatch):
    """Sanitization targets string field VALUES only; ints/bools/lists pass through."""
    monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path))
    monkeypatch.setenv("WRIT_LOG_PROJECT", "proj-h")
    emit(None, "hook_execution", "sid-14", "work", duration_ms=42, rule_ids=["A", "B"])
    row = _read_jsonl(stream_path("proj-h", "metrics"))[0]
    assert row["duration_ms"] == 42
    assert row["rule_ids"] == ["A", "B"]


# --- mode_source: kept as an explicit null (_KEEP_WHEN_NULL) ----------------
#
# Same treatment as from_mode -- test_mode_switch_midsession.py::TestFromModeAlwaysRecorded
# pins the identical contract one layer up, through cmd_mode. An omitted key reads
# exactly like a row written before the field existed at all, and that specific
# ambiguity is what made a real telemetry row undiagnosable during a past incident.
# The key's PRESENCE with a null value is the assertion here, not merely that emit
# did not raise on a None value.


def test_emit_keeps_mode_source_as_explicit_null_when_provenance_is_unknown(
    tmp_path, monkeypatch
):
    """A mode_change row for a session whose provenance is genuinely unknown (a mode
    carried forward from a pre-mode_source cache, say) must record `mode_source` as
    an explicit JSON null, not drop the key. A dropped key is indistinguishable from
    a row written before the field existed, which is the exact ambiguity mode_source
    exists to end."""
    monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path))
    monkeypatch.setenv("WRIT_LOG_PROJECT", "proj-mode-source")
    emit(None, "mode_change", "sid-ms-1", "work", to_mode="work", mode_source=None)

    rows = _read_jsonl(stream_path("proj-mode-source", "audit"))
    assert len(rows) == 1
    assert "mode_source" in rows[0], "the key must be present even when the value is null"
    assert rows[0]["mode_source"] is None


def test_emit_records_a_real_mode_source_value_unchanged(tmp_path, monkeypatch):
    """Control for the null-retention test above: a real provenance value must still
    round-trip unchanged, so the null-handling path cannot be satisfied by an
    implementation that always nulls the field out."""
    monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path))
    monkeypatch.setenv("WRIT_LOG_PROJECT", "proj-mode-source-2")
    emit(None, "mode_change", "sid-ms-2", "work", to_mode="work", mode_source="explicit")

    rows = _read_jsonl(stream_path("proj-mode-source-2", "audit"))
    assert rows[0]["mode_source"] == "explicit"


# --- read_streams() ----------------------------------------------------------


def test_read_streams_unions_named_stream_files(tmp_path, monkeypatch):
    monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path))
    monkeypatch.setenv("WRIT_LOG_PROJECT", "proj-i")
    emit(None, "mode_change", "sid-15", "work")      # audit
    emit(None, "write_failure", "sid-15", "work")    # friction
    emit(None, "hook_execution", "sid-15", "work")   # metrics

    events = read_streams("proj-i", ["audit", "friction", "metrics"])
    assert {e["event"] for e in events} == {"mode_change", "write_failure", "hook_execution"}


def test_read_streams_skips_malformed_lines(tmp_path, monkeypatch):
    monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path))
    project = "proj-j"
    path = stream_path(project, "audit")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"event": "mode_change", "session": "s"}\nNOT JSON\n\n')
    events = read_streams(project, ["audit"])
    assert len(events) == 1
    assert events[0]["event"] == "mode_change"


def test_read_streams_missing_file_returns_no_rows_for_that_stream(tmp_path, monkeypatch):
    monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path))
    project = "proj-k"
    # Only write to "audit"; "metrics" file never exists.
    emit(None, "mode_change", "sid-16", "work", stream=None)
    events = read_streams(project, ["audit", "metrics", "friction"])
    assert isinstance(events, list)  # missing files contribute zero rows, no error


def test_read_streams_returns_empty_list_for_project_with_no_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path))
    assert read_streams("never-logged-project", ["audit", "friction", "metrics"]) == []


# --- read_streams() archive awareness (D1a) ----------------------------------
# RED until read_streams unions <project>/archive/<stream>-<date>.jsonl.gz with
# the live stream file. Rotation moves history into those archives, so a reader
# that only opens the live file reports an empty corpus the day after a sweep
# (measured live 2026-07-22: 15729 archived rows, one project visible-count 0).


def _write_archive(project: str, stream: str, day: str, rows: list[dict]) -> Path:
    """Write a gzipped archive generation the way log_rotation produces it."""
    import gzip

    from writ.shared.logging import archive_dir

    arc = archive_dir(project)
    arc.mkdir(parents=True, exist_ok=True)
    dest = arc / f"{stream}-{day}.jsonl.gz"
    with gzip.open(dest, "wt", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return dest


def test_read_streams_includes_archived_rows(tmp_path, monkeypatch):
    """A rotated generation still counts: its rows come back alongside live ones."""
    monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path))
    project = "proj-arc-1"
    _write_archive(project, "audit", "2026-07-21",
                   [{"ts": "2026-07-21T01:00:00Z", "event": "mode_change", "session": "s1"}])
    path = stream_path(project, "audit")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"ts": "2026-07-22T01:00:00Z", "event": "gate_denial", "session": "s2"}\n')

    events = read_streams(project, ["audit"])
    assert {e["event"] for e in events} == {"mode_change", "gate_denial"}


def test_read_streams_orders_archives_oldest_first(tmp_path, monkeypatch):
    """Callers take events[0]/events[-1] as first/last ts, so order is a contract."""
    monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path))
    project = "proj-arc-2"
    _write_archive(project, "audit", "2026-07-20",
                   [{"ts": "2026-07-20T01:00:00Z", "event": "older", "session": "s"}])
    _write_archive(project, "audit", "2026-07-21",
                   [{"ts": "2026-07-21T01:00:00Z", "event": "newer", "session": "s"}])

    events = read_streams(project, ["audit"])
    assert [e["event"] for e in events] == ["older", "newer"]


def test_read_streams_places_live_rows_after_archived_rows(tmp_path, monkeypatch):
    """The live file is always the newest generation for its stream."""
    monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path))
    project = "proj-arc-3"
    _write_archive(project, "audit", "2026-07-21",
                   [{"ts": "2026-07-21T01:00:00Z", "event": "archived", "session": "s"}])
    path = stream_path(project, "audit")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"ts": "2026-07-22T01:00:00Z", "event": "live", "session": "s"}\n')

    events = read_streams(project, ["audit"])
    assert [e["event"] for e in events] == ["archived", "live"]


def test_read_streams_skips_corrupt_archive_without_raising(tmp_path, monkeypatch):
    """Same fail-soft contract as a malformed line: skip it, keep the rest."""
    monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path))
    project = "proj-arc-4"
    from writ.shared.logging import archive_dir

    arc = archive_dir(project)
    arc.mkdir(parents=True, exist_ok=True)
    (arc / "audit-2026-07-20.jsonl.gz").write_bytes(b"this is not gzip data")
    _write_archive(project, "audit", "2026-07-21",
                   [{"ts": "2026-07-21T01:00:00Z", "event": "survivor", "session": "s"}])

    events = read_streams(project, ["audit"])
    assert [e["event"] for e in events] == ["survivor"]


def test_read_streams_reads_only_the_requested_streams_archives(tmp_path, monkeypatch):
    """An archive for a stream that was not asked for must not leak into results."""
    monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path))
    project = "proj-arc-5"
    _write_archive(project, "audit", "2026-07-21",
                   [{"ts": "2026-07-21T01:00:00Z", "event": "wanted", "session": "s"}])
    _write_archive(project, "metrics", "2026-07-21",
                   [{"ts": "2026-07-21T01:00:00Z", "event": "unwanted", "session": "s"}])

    events = read_streams(project, ["audit"])
    assert [e["event"] for e in events] == ["wanted"]


def test_read_streams_with_archives_but_no_live_file(tmp_path, monkeypatch):
    """The exact post-rotation state: live file gone, everything in archive."""
    monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path))
    project = "proj-arc-6"
    _write_archive(project, "friction", "2026-07-21",
                   [{"ts": "2026-07-21T01:00:00Z", "event": "repeated_denial", "session": "s"}])

    events = read_streams(project, ["friction"])
    assert [e["event"] for e in events] == ["repeated_denial"]


def test_read_streams_skips_malformed_lines_inside_an_archive(tmp_path, monkeypatch):
    """Malformed-line skipping applies inside archives, not just the live file."""
    monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path))
    project = "proj-arc-7"
    import gzip

    from writ.shared.logging import archive_dir

    arc = archive_dir(project)
    arc.mkdir(parents=True, exist_ok=True)
    with gzip.open(arc / "audit-2026-07-21.jsonl.gz", "wt", encoding="utf-8") as fh:
        fh.write('{"ts": "2026-07-21T01:00:00Z", "event": "good", "session": "s"}\n')
        fh.write("NOT JSON\n\n")

    events = read_streams(project, ["audit"])
    assert [e["event"] for e in events] == ["good"]
