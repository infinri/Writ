"""Tests for pre-sub-agent hardening: denial messages, repeated denial
detection, friction event logging, and phase-specific advance messages.

Per TEST-TDD-001: skeletons approved before implementation.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import secrets
import tempfile

import pytest

# ---------------------------------------------------------------------------
# Import the session helper as a module (it's not in a package)
# ---------------------------------------------------------------------------

HELPER_PATH = os.path.join(
    os.path.dirname(__file__), os.pardir, "bin", "lib", "writ-session.py"
)

spec = importlib.util.spec_from_file_location("writ_session", HELPER_PATH)
writ_session = importlib.util.module_from_spec(spec)
spec.loader.exec_module(writ_session)

SKILL_DIR = os.path.join(os.path.dirname(__file__), os.pardir)


@pytest.fixture()
def session_id(tmp_path, monkeypatch):
    """Provide a unique session ID and redirect cache to tmp_path."""
    monkeypatch.setattr(writ_session, "CACHE_DIR", str(tmp_path))
    return "test-hardening-session"


@pytest.fixture()
def project_root(tmp_path):
    """Create a minimal project root with .git marker and gates dir."""
    root = tmp_path / "project"
    root.mkdir()
    (root / ".git").mkdir()
    (root / ".claude" / "gates").mkdir(parents=True)
    return root


def _set_mode(session_id: str, mode: str) -> None:
    writ_session.cmd_mode(session_id, "set", mode)


def _read_cache(session_id: str) -> dict:
    return writ_session._read_cache(session_id)


def _call_can_write(
    session_id: str, file_path: str, monkeypatch, capsys, skill_dir: str = ""
) -> dict:
    """Call cmd_can_write with a synthetic tool envelope and return the JSON result."""
    capsys.readouterr()
    envelope = json.dumps({"tool_input": {"file_path": file_path}})
    monkeypatch.setattr("sys.stdin", io.StringIO(envelope))
    writ_session.cmd_can_write(session_id, skill_dir or SKILL_DIR)
    out = capsys.readouterr().out.strip()
    return json.loads(out)


def _call_advance_phase(
    session_id: str, prompt: str, project_root: str, monkeypatch, capsys
) -> dict:
    """Call cmd_advance_phase with a gate token and return the JSON result."""
    token = secrets.token_hex(16)
    token_path = os.path.join(tempfile.gettempdir(), f"writ-gate-token-{session_id}")
    with open(token_path, "w") as f:
        f.write(token)
    capsys.readouterr()
    monkeypatch.setattr("sys.stdin", io.StringIO(prompt))
    writ_session.cmd_advance_phase(session_id, project_root, token)
    out = capsys.readouterr().out.strip()
    return json.loads(out)


VALID_PLAN_MD = """\
# Test Plan

## Files

| File | Action |
|------|--------|
| src/service.py | Create |

## Analysis

This feature adds a new service endpoint.

## Rules Applied

- PERF-IO-001: No sync I/O in the hot path.

## Capabilities

- [ ] Service returns filtered results
- [ ] Service returns 400 for invalid input
"""

VALID_PLAN_RULE_IDS = ["PERF-IO-001"]


# ===========================================================================
# Part 1: Denial message content
# ===========================================================================


class TestDenialMessageContent:
    """Verify ENF-GATE-PLAN and ENF-GATE-TEST contain the stronger messages."""

    def test_gate_plan_denial_says_all_writes_blocked(
        self, session_id, monkeypatch, capsys
    ):
        """ENF-GATE-PLAN denial must contain 'ALL writes blocked'."""
        _set_mode(session_id, "work")
        result = _call_can_write(
            session_id, "/tmp/project/src/service.py", monkeypatch, capsys
        )
        assert result["decision"] == "deny"
        assert "ALL writes blocked" in result["reason"]

    def test_gate_plan_denial_says_do_not_attempt(
        self, session_id, monkeypatch, capsys
    ):
        """ENF-GATE-PLAN denial must tell Claude to stop attempting writes."""
        _set_mode(session_id, "work")
        result = _call_can_write(
            session_id, "/tmp/project/src/service.py", monkeypatch, capsys
        )
        assert "DO NOT attempt more writes" in result["reason"]

    def test_gate_test_denial_says_all_writes_blocked(
        self, session_id, project_root, monkeypatch, capsys
    ):
        """ENF-GATE-TEST denial must contain 'ALL writes blocked'."""
        _set_mode(session_id, "work")
        # Write plan and advance past phase-a
        (project_root / "plan.md").write_text(VALID_PLAN_MD)
        cache = _read_cache(session_id)
        cache["loaded_rule_ids"] = VALID_PLAN_RULE_IDS
        cache["loaded_rule_ids_by_phase"] = {"planning": VALID_PLAN_RULE_IDS}
        writ_session._write_cache(session_id, cache)
        _call_advance_phase(
            session_id, "approved", str(project_root), monkeypatch, capsys
        )
        # Now try to write a non-test file -- should hit ENF-GATE-TEST
        result = _call_can_write(
            session_id, "/tmp/project/src/service.py", monkeypatch, capsys
        )
        assert result["decision"] == "deny"
        assert "ALL writes blocked" in result["reason"]

    def test_gate_test_denial_says_do_not_attempt(
        self, session_id, project_root, monkeypatch, capsys
    ):
        """ENF-GATE-TEST denial must tell Claude to stop attempting writes."""
        _set_mode(session_id, "work")
        (project_root / "plan.md").write_text(VALID_PLAN_MD)
        cache = _read_cache(session_id)
        cache["loaded_rule_ids"] = VALID_PLAN_RULE_IDS
        cache["loaded_rule_ids_by_phase"] = {"planning": VALID_PLAN_RULE_IDS}
        writ_session._write_cache(session_id, cache)
        _call_advance_phase(
            session_id, "approved", str(project_root), monkeypatch, capsys
        )
        result = _call_can_write(
            session_id, "/tmp/project/src/service.py", monkeypatch, capsys
        )
        assert "DO NOT attempt more writes" in result["reason"]

    def test_gate_plan_denial_contains_enf_gate_plan_tag(
        self, session_id, monkeypatch, capsys
    ):
        """Denial reason must still include the [ENF-GATE-PLAN] tag for log parsing."""
        _set_mode(session_id, "work")
        result = _call_can_write(
            session_id, "/tmp/project/src/service.py", monkeypatch, capsys
        )
        assert "[ENF-GATE-PLAN]" in result["reason"]


# ===========================================================================
# Part 2: Repeated denial detection
# ===========================================================================


class TestRepeatedDenialDetection:
    """Verify denial_counts tracking and repeated_denial event logging."""

    def test_first_denial_sets_count_to_one(
        self, session_id, monkeypatch, capsys
    ):
        """First denial for a gate should set denial_counts[gate] = 1."""
        _set_mode(session_id, "work")
        _call_can_write(
            session_id, "/tmp/project/src/foo.py", monkeypatch, capsys
        )
        cache = _read_cache(session_id)
        counts = cache.get("denial_counts", {})
        assert counts.get("phase-a", 0) == 1

    def test_second_denial_increments_count(
        self, session_id, monkeypatch, capsys
    ):
        """Second denial for same gate should increment to 2."""
        _set_mode(session_id, "work")
        _call_can_write(
            session_id, "/tmp/project/src/foo.py", monkeypatch, capsys
        )
        _call_can_write(
            session_id, "/tmp/project/src/bar.py", monkeypatch, capsys
        )
        cache = _read_cache(session_id)
        assert cache["denial_counts"]["phase-a"] == 2

    def test_denial_counts_reset_on_gate_advance(
        self, session_id, project_root, monkeypatch, capsys
    ):
        """Advancing a gate should reset denial_counts for that gate."""
        _set_mode(session_id, "work")
        # Trigger a denial to set count
        _call_can_write(
            session_id, "/tmp/project/src/foo.py", monkeypatch, capsys
        )
        assert _read_cache(session_id).get("denial_counts", {}).get("phase-a", 0) == 1
        # Advance phase-a
        (project_root / "plan.md").write_text(VALID_PLAN_MD)
        cache = _read_cache(session_id)
        cache["loaded_rule_ids"] = VALID_PLAN_RULE_IDS
        cache["loaded_rule_ids_by_phase"] = {"planning": VALID_PLAN_RULE_IDS}
        writ_session._write_cache(session_id, cache)
        _call_advance_phase(
            session_id, "approved", str(project_root), monkeypatch, capsys
        )
        cache = _read_cache(session_id)
        assert cache.get("denial_counts", {}).get("phase-a", 0) == 0

    def test_denial_counts_reset_on_mode_set(
        self, session_id, monkeypatch, capsys
    ):
        """Setting a new mode should clear all denial_counts."""
        _set_mode(session_id, "work")
        _call_can_write(
            session_id, "/tmp/project/src/foo.py", monkeypatch, capsys
        )
        assert _read_cache(session_id).get("denial_counts", {}).get("phase-a", 0) == 1
        _set_mode(session_id, "work")  # fresh set
        cache = _read_cache(session_id)
        assert cache.get("denial_counts", {}) == {}


# ===========================================================================
# Part 3: Friction event logging from cmd_can_write
# ===========================================================================


class TestCanWriteFrictionEvents:
    """Verify gate_denial and write_attempt events are logged."""

    def test_gate_denial_event_logged(
        self, session_id, monkeypatch, capsys, tmp_path
    ):
        """A denied write should log a gate_denial event to friction log."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".git").mkdir(exist_ok=True)
        _set_mode(session_id, "work")
        _call_can_write(
            session_id, str(tmp_path / "src" / "foo.py"), monkeypatch, capsys
        )
        log_path = tmp_path / "workflow-friction.log"
        assert log_path.exists()
        events = [json.loads(line) for line in log_path.read_text().splitlines()
                  if line.strip() and not line.startswith("#")]
        gate_denials = [e for e in events if e["event"] == "gate_denial"]
        assert len(gate_denials) >= 1
        assert gate_denials[-1]["gate"] == "phase-a"

    def test_write_attempt_event_logged_on_allow(
        self, session_id, monkeypatch, capsys, tmp_path
    ):
        """An allowed write should log a write_attempt event."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".git").mkdir(exist_ok=True)
        _set_mode(session_id, "conversation")
        _call_can_write(
            session_id, str(tmp_path / "src" / "foo.py"), monkeypatch, capsys
        )
        log_path = tmp_path / "workflow-friction.log"
        assert log_path.exists()
        events = [json.loads(line) for line in log_path.read_text().splitlines()
                  if line.strip() and not line.startswith("#")]
        attempts = [e for e in events if e["event"] == "write_attempt"]
        assert len(attempts) >= 1
        assert attempts[-1]["result"] == "allow"

    def test_repeated_denial_event_on_second_deny(
        self, session_id, monkeypatch, capsys, tmp_path
    ):
        """Second denial for same gate should log a repeated_denial event."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".git").mkdir(exist_ok=True)
        _set_mode(session_id, "work")
        _call_can_write(
            session_id, str(tmp_path / "src" / "foo.py"), monkeypatch, capsys
        )
        _call_can_write(
            session_id, str(tmp_path / "src" / "bar.py"), monkeypatch, capsys
        )
        log_path = tmp_path / "workflow-friction.log"
        events = [json.loads(line) for line in log_path.read_text().splitlines()
                  if line.strip() and not line.startswith("#")]
        repeated = [e for e in events if e["event"] == "repeated_denial"]
        assert len(repeated) >= 1
        assert repeated[-1]["gate"] == "phase-a"
        assert repeated[-1]["denial_count"] >= 2

    def test_gate_denial_event_includes_file_path(
        self, session_id, monkeypatch, capsys, tmp_path
    ):
        """gate_denial event should include the file_path that was denied."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".git").mkdir(exist_ok=True)
        _set_mode(session_id, "work")
        target = str(tmp_path / "src" / "service.py")
        _call_can_write(session_id, target, monkeypatch, capsys)
        log_path = tmp_path / "workflow-friction.log"
        events = [json.loads(line) for line in log_path.read_text().splitlines()
                  if line.strip() and not line.startswith("#")]
        gate_denials = [e for e in events if e["event"] == "gate_denial"]
        assert gate_denials[-1]["file_path"] == target


# ===========================================================================
# Part 4: Phase-specific advance messages
# ===========================================================================


class TestPhaseSpecificAdvanceMessages:
    """Verify auto-approve-gate.sh injects different messages per gate.

    These test the writ-session.py advance-phase output, which auto-approve-gate.sh
    parses. The shell hook's message formatting is tested via integration tests;
    these unit tests verify the underlying data (gate name, phase) is correct.
    """

    def test_phase_a_advance_returns_testing_phase(
        self, session_id, project_root, monkeypatch, capsys
    ):
        """Advancing phase-a should set phase to 'testing'."""
        _set_mode(session_id, "work")
        (project_root / "plan.md").write_text(VALID_PLAN_MD)
        cache = _read_cache(session_id)
        cache["loaded_rule_ids"] = VALID_PLAN_RULE_IDS
        cache["loaded_rule_ids_by_phase"] = {"planning": VALID_PLAN_RULE_IDS}
        writ_session._write_cache(session_id, cache)
        result = _call_advance_phase(
            session_id, "approved", str(project_root), monkeypatch, capsys
        )
        assert result["advanced"] is True
        assert result["gate"] == "phase-a"
        assert result["phase"] == "testing"

    def test_test_skeletons_advance_returns_implementation_phase(
        self, session_id, project_root, monkeypatch, capsys
    ):
        """Advancing test-skeletons should set phase to 'implementation'."""
        _set_mode(session_id, "work")
        (project_root / "plan.md").write_text(VALID_PLAN_MD)
        cache = _read_cache(session_id)
        cache["loaded_rule_ids"] = VALID_PLAN_RULE_IDS
        cache["loaded_rule_ids_by_phase"] = {"planning": VALID_PLAN_RULE_IDS}
        writ_session._write_cache(session_id, cache)
        # Advance phase-a first
        _call_advance_phase(
            session_id, "approved", str(project_root), monkeypatch, capsys
        )
        # Write a test file so test-skeletons validation passes
        test_dir = project_root / "tests"
        test_dir.mkdir()
        (test_dir / "test_service.py").write_text(
            "def test_returns_filtered_results():\n    assert True\n"
        )
        cache = _read_cache(session_id)
        cache["files_written"] = [str(test_dir / "test_service.py")]
        writ_session._write_cache(session_id, cache)
        result = _call_advance_phase(
            session_id, "approved", str(project_root), monkeypatch, capsys
        )
        assert result["advanced"] is True
        assert result["gate"] == "test-skeletons"
        assert result["phase"] == "implementation"


# ===========================================================================
# Part 5: Token snapshot in session cache
# ===========================================================================


class TestTokenSnapshotCache:
    """Verify token_snapshots list is maintained in session cache."""

    def test_token_snapshots_starts_empty(self, session_id):
        """Fresh session should have empty token_snapshots list."""
        _set_mode(session_id, "work")
        cache = _read_cache(session_id)
        assert cache.get("token_snapshots", []) == []

    def test_update_adds_token_snapshot(self, session_id):
        """Calling update with --token-snapshot should append to the list."""
        _set_mode(session_id, "work")
        snapshot_json = json.dumps({"context_percent": 25, "context_tokens": 250000})
        writ_session.cmd_update(session_id, ["--token-snapshot", snapshot_json])
        cache = _read_cache(session_id)
        snapshots = cache.get("token_snapshots", [])
        assert len(snapshots) == 1
        assert snapshots[0]["context_percent"] == 25
        assert snapshots[0]["context_tokens"] == 250000


# ===========================================================================
# Part 6: Rule coverage analysis in metrics
# ===========================================================================


class TestRuleCoverageMetrics:
    """Verify cmd_metrics produces rule coverage analysis from friction log events."""

    def test_rule_coverage_with_impl_writes_and_rag_queries(self, tmp_path):
        """Metrics should produce per-file rule coverage and first/second half trend."""
        sid = "test-coverage-session"
        log_path = tmp_path / "workflow-friction.log"

        # Simulate 6 implementation writes with RAG queries
        events = []
        for i in range(6):
            ts = f"2026-04-11T18:00:{10 + i * 5:02d}Z"
            # RAG query just before each write
            rules_count = 10 - i  # decreasing rules per file
            events.append(json.dumps({
                "ts": ts, "session": sid, "mode": "work",
                "event": "rag_query", "query_source": "file-write-pre",
                "tokens_injected": rules_count * 40,
                "rules_returned_count": rules_count,
                "rule_ids": [f"RULE-{j:03d}" for j in range(rules_count)],
            }))
            events.append(json.dumps({
                "ts": ts, "session": sid, "mode": "work",
                "event": "write_attempt",
                "file_path": f"/project/src/File{i + 1}.php",
                "result": "allow", "gate_status": "all_approved",
                "phase": "implementation",
            }))

        log_path.write_text("\n".join(events) + "\n")

        import sys as _sys
        import io as _io
        old_stdout = _sys.stdout
        _sys.stdout = _io.StringIO()
        try:
            writ_session.cmd_metrics(str(log_path))
            output = _sys.stdout.getvalue()
        finally:
            _sys.stdout = old_stdout

        report = json.loads(output)
        rc = report.get("rule_coverage")
        assert rc is not None
        assert rc["total_files_analyzed"] == 6
        assert rc["sessions_analyzed"] == 1
        trend = rc["per_session_trends"][0]
        assert trend["files_count"] == 6
        # First half (files 1-3) avg rules > second half (files 4-6) avg rules
        assert trend["first_half_avg_rules"] > trend["second_half_avg_rules"]
        assert trend["pct_change"] < 0  # negative = degradation

    def test_rule_coverage_null_when_no_impl_writes(self, tmp_path):
        """Metrics should return null rule_coverage when no implementation writes exist."""
        log_path = tmp_path / "workflow-friction.log"
        events = [json.dumps({
            "ts": "2026-04-11T18:00:10Z", "session": "s1", "mode": "work",
            "event": "mode_change", "change_type": "set",
            "from_mode": None, "to_mode": "work",
        })]
        log_path.write_text("\n".join(events) + "\n")

        import sys as _sys
        import io as _io
        old_stdout = _sys.stdout
        _sys.stdout = _io.StringIO()
        try:
            writ_session.cmd_metrics(str(log_path))
            output = _sys.stdout.getvalue()
        finally:
            _sys.stdout = old_stdout

        report = json.loads(output)
        assert report.get("rule_coverage") is None
