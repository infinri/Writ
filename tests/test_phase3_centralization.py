"""Tests for mode-based centralization commands in bin/lib/writ-session.py.

Tests cmd_can_write, cmd_advance_phase, cmd_current_phase, and
phase-aware loaded_rule_ids -- updated for v2 mode system.

Per TEST-TDD-001: skeletons approved before implementation.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import secrets
import sys
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
    return "test-phase3-session"


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
    capsys.readouterr()  # clear any prior output
    envelope = json.dumps({"tool_input": {"file_path": file_path}})
    monkeypatch.setattr("sys.stdin", io.StringIO(envelope))
    writ_session.cmd_can_write(session_id, skill_dir or SKILL_DIR)
    out = capsys.readouterr().out.strip()
    return json.loads(out)


def _call_advance_phase(
    session_id: str, prompt: str, project_root: str, monkeypatch, capsys
) -> dict:
    """Call cmd_advance_phase with a gate token and return the JSON result."""
    # Create gate token (simulates auto-approve-gate.sh)
    token = secrets.token_hex(16)
    token_path = os.path.join(tempfile.gettempdir(), f"writ-gate-token-{session_id}")
    with open(token_path, "w") as f:
        f.write(token)

    capsys.readouterr()  # clear any prior output
    monkeypatch.setattr("sys.stdin", io.StringIO(prompt))
    writ_session.cmd_advance_phase(session_id, project_root, token)
    out = capsys.readouterr().out.strip()
    return json.loads(out)


def _call_current_phase(session_id: str, capsys) -> dict:
    """Call cmd_current_phase and return the JSON result."""
    capsys.readouterr()  # clear any prior output
    writ_session.cmd_current_phase(session_id)
    out = capsys.readouterr().out.strip()
    return json.loads(out)


VALID_PLAN_MD = """\
# Test Plan

## Files

| File | Action |
|------|--------|
| src/service.py | Create |

## Analysis

This feature adds a new service endpoint. It validates input via Pydantic
models, queries the repository, and returns filtered results.

## Rules Applied

- PERF-IO-001: No sync I/O in the hot path. Data loaded at startup.
- ARCH-ORG-001: Service logic separated from data access.

## Capabilities

- [ ] Service returns filtered results for valid input
- [ ] Service returns 400 for invalid input
- [ ] No sync I/O in request handler
"""

import re as _re
# Extract rule IDs from the plan fixture so tests don't hardcode them separately
VALID_PLAN_RULE_IDS = _re.findall(
    r'[A-Z][A-Z0-9]+(?:-[A-Z][A-Z0-9]+)*-\d{3}', VALID_PLAN_MD
)

PLAN_MD_NO_MATCH = """\
# Test Plan

## Files

| File | Action |
|------|--------|
| src/config.py | Modify |

## Analysis

Minor config adjustment for logging levels.

## Rules Applied

No matching rules. Domain: infrastructure/logging configuration.

## Capabilities

- [ ] Log level changes take effect without restart
"""


def _write_plan(project_root, content: str = VALID_PLAN_MD) -> None:
    (project_root / "plan.md").write_text(content)


def _load_plan_rules(session_id: str) -> None:
    """Load rule IDs that VALID_PLAN_MD references so validation passes."""
    writ_session.cmd_update(session_id, ["--add-rules", json.dumps(VALID_PLAN_RULE_IDS)])


def _approve_gate(session_id: str, gate: str) -> None:
    """Directly add a gate to the session cache (simulates advance-phase)."""
    cache = writ_session._read_cache(session_id)
    approved = set(cache.get("gates_approved", []))
    approved.add(gate)
    cache["gates_approved"] = sorted(approved)
    phase = writ_session._PHASE_AFTER_GATE_WORK.get(gate, "implementation")
    cache["current_phase"] = phase
    writ_session._write_cache(session_id, cache)


# ===========================================================================
# cmd_can_write (via tier facade -- hooks still use tier set)
# ===========================================================================

class TestCanWrite:

    def test_deny_no_mode_except_plan_md(self, session_id, project_root, monkeypatch, capsys):
        """No mode declared: deny all files except plan.md."""
        # plan.md is allowed
        result = _call_can_write(session_id, str(project_root / "plan.md"), monkeypatch, capsys)
        assert result["decision"] == "allow"

        # .py file is denied
        result = _call_can_write(session_id, str(project_root / "main.py"), monkeypatch, capsys)
        assert result["decision"] == "deny"
        assert "ENF-GATE-MODE" in result["reason"]

    def test_deny_plan_md_during_implementation_phase(self, session_id, project_root, monkeypatch, capsys):
        """Post test-skeletons gate, plan.md writes are blocked in Work mode."""
        _set_mode(session_id, "work")
        capsys.readouterr()
        _approve_gate(session_id, "phase-a")
        _approve_gate(session_id, "test-skeletons")

        result = _call_can_write(session_id, str(project_root / "plan.md"), monkeypatch, capsys)
        assert result["decision"] == "deny"
        assert "ENF-GATE-PLAN" in result["reason"]

    def test_work_deny_without_phase_a(self, session_id, project_root, monkeypatch, capsys):
        """Work mode: source file denied before phase-a gate."""
        _set_mode(session_id, "work")
        capsys.readouterr()

        result = _call_can_write(session_id, str(project_root / "service.py"), monkeypatch, capsys)
        assert result["decision"] == "deny"
        assert "ENF-GATE-PLAN" in result["reason"]

    def test_work_deny_after_plan_before_test_skeletons(self, session_id, project_root, monkeypatch, capsys):
        """Work mode: source file denied after phase-a but before test-skeletons."""
        _set_mode(session_id, "work")
        capsys.readouterr()
        _approve_gate(session_id, "phase-a")

        result = _call_can_write(session_id, str(project_root / "service.py"), monkeypatch, capsys)
        assert result["decision"] == "deny"
        assert "ENF-GATE-TEST" in result["reason"]

    def test_work_deny_stale_gate(self, session_id, project_root, monkeypatch, capsys):
        """Work mode: gates_approved in cache is the authority -- no disk fallback."""
        _set_mode(session_id, "work")
        capsys.readouterr()

        # Write a gate file on disk but DON'T add to cache
        gate_file = project_root / ".claude" / "gates" / "phase-a.approved"
        gate_file.write_text("different-session-id\n")

        result = _call_can_write(session_id, str(project_root / "service.py"), monkeypatch, capsys)
        assert result["decision"] == "deny"

    def test_work_two_gate_enforcement(self, session_id, project_root, monkeypatch, capsys):
        """Work mode: source files allowed only after both gates."""
        _set_mode(session_id, "work")
        capsys.readouterr()
        _approve_gate(session_id, "phase-a")
        _approve_gate(session_id, "test-skeletons")

        result = _call_can_write(session_id, str(project_root / "service.py"), monkeypatch, capsys)
        assert result["decision"] == "allow"

    def test_skip_writ_infrastructure(self, session_id, monkeypatch, capsys):
        """Files in the skill directory bypass gate checks."""
        # No mode set -- would normally deny
        skill_file = os.path.join(SKILL_DIR, "bin", "lib", "writ-session.py")
        result = _call_can_write(session_id, skill_file, monkeypatch, capsys)
        assert result["decision"] == "allow"

    def test_exclusions_from_categories(self, session_id, project_root, monkeypatch, capsys):
        """Test files and conftest.py bypass gate checks."""
        _set_mode(session_id, "work")
        capsys.readouterr()
        # No gates approved -- but test files are excluded from gate checks

        result = _call_can_write(session_id, str(project_root / "tests" / "test_foo.py"), monkeypatch, capsys)
        assert result["decision"] == "allow"

        result = _call_can_write(session_id, str(project_root / "conftest.py"), monkeypatch, capsys)
        assert result["decision"] == "allow"


# ===========================================================================
# cmd_advance_phase
# ===========================================================================

class TestAdvancePhase:

    def test_advance_creates_gate_file_with_session_id(
        self, session_id, project_root, monkeypatch, capsys
    ):
        """advance-phase writes session ID into the gate file."""
        _set_mode(session_id, "work")
        _load_plan_rules(session_id)
        capsys.readouterr()
        _write_plan(project_root)

        result = _call_advance_phase(session_id, "approved", str(project_root), monkeypatch, capsys)
        assert result["advanced"] is True
        assert result["gate"] == "phase-a"

        gate_file = project_root / ".claude" / "gates" / "phase-a.approved"
        assert gate_file.exists()
        assert gate_file.read_text().strip() == session_id

    def test_advance_fails_without_plan_md(self, session_id, project_root, monkeypatch, capsys):
        """phase-a advance fails when plan.md doesn't exist."""
        _set_mode(session_id, "work")
        capsys.readouterr()

        result = _call_advance_phase(session_id, "approved", str(project_root), monkeypatch, capsys)
        assert result["advanced"] is False
        assert "plan.md" in result["reason"].lower()

    def test_advance_fails_missing_section(self, session_id, project_root, monkeypatch, capsys):
        """phase-a advance fails when plan.md is missing ## Analysis."""
        _set_mode(session_id, "work")
        _load_plan_rules(session_id)
        capsys.readouterr()

        incomplete_plan = """\
## Files
- src/foo.py (create)

## Rules Applied
- PERF-IO-001: applies here.

## Capabilities
- [ ] Works correctly
"""
        _write_plan(project_root, incomplete_plan)

        result = _call_advance_phase(session_id, "approved", str(project_root), monkeypatch, capsys)
        assert result["advanced"] is False
        assert "Analysis" in result["reason"]

    def test_advance_accepts_no_match_declaration(self, session_id, project_root, monkeypatch, capsys):
        """## Rules Applied with 'No matching rules. Domain: ...' passes validation."""
        _set_mode(session_id, "work")
        capsys.readouterr()
        _write_plan(project_root, PLAN_MD_NO_MATCH)

        result = _call_advance_phase(session_id, "approved", str(project_root), monkeypatch, capsys)
        assert result["advanced"] is True

    def test_advance_clears_current_phase_rule_ids(self, session_id, project_root, monkeypatch, capsys):
        """Phase transition moves current loaded_rule_ids to historical set."""
        _set_mode(session_id, "work")
        capsys.readouterr()

        # Add rules that match the plan fixture
        _load_plan_rules(session_id)

        cache = _read_cache(session_id)
        for rid in VALID_PLAN_RULE_IDS:
            assert rid in cache["loaded_rule_ids_by_phase"].get("planning", [])

        # Advance phase
        _write_plan(project_root)
        _call_advance_phase(session_id, "approved", str(project_root), monkeypatch, capsys)

        cache = _read_cache(session_id)
        # Planning phase IDs should be cleared
        assert cache["loaded_rule_ids_by_phase"].get("planning", []) == []
        # Historical set should have the old IDs
        historical = cache["loaded_rule_ids_by_phase"].get("_historical", [])
        for rid in VALID_PLAN_RULE_IDS:
            assert rid in historical

    def test_advance_logs_transition_to_audit_trail(self, session_id, project_root, monkeypatch, capsys):
        """Audit trail entry created with from, to, ts, trigger, artifacts."""
        _set_mode(session_id, "work")
        _load_plan_rules(session_id)
        capsys.readouterr()
        _write_plan(project_root)
        _call_advance_phase(session_id, "approved", str(project_root), monkeypatch, capsys)

        cache = _read_cache(session_id)
        transitions = cache["phase_transitions"]
        # First is mode-set, second is the advance
        assert len(transitions) >= 2
        advance = transitions[-1]
        assert advance["trigger"] == "user-approved"
        assert advance["from"] == "planning"
        assert advance["to"] == "testing"
        assert "ts" in advance
        assert advance.get("gate") == "phase-a"

    def test_advance_test_skeletons_requires_test_file(
        self, session_id, project_root, monkeypatch, capsys
    ):
        """test-skeletons advance requires at least one test file with method sig."""
        _set_mode(session_id, "work")
        capsys.readouterr()
        _approve_gate(session_id, "phase-a")

        # No test files exist
        result = _call_advance_phase(session_id, "approved", str(project_root), monkeypatch, capsys)
        assert result["advanced"] is False
        assert "test" in result["reason"].lower()

    def test_advance_no_op_when_all_gates_approved(self, session_id, project_root, monkeypatch, capsys):
        """Advancing when all gates exist returns a no-op result."""
        _set_mode(session_id, "work")
        capsys.readouterr()
        _approve_gate(session_id, "phase-a")
        _approve_gate(session_id, "test-skeletons")

        result = _call_advance_phase(session_id, "approved", str(project_root), monkeypatch, capsys)
        assert result["advanced"] is False
        assert "already" in result["reason"].lower()


# ===========================================================================
# cmd_current_phase
# ===========================================================================

class TestCurrentPhase:

    def test_no_mode_returns_unclassified(self, session_id, capsys):
        """No mode set: phase is 'unclassified'."""
        result = _call_current_phase(session_id, capsys)
        assert result["phase"] == "unclassified"
        assert result["mode"] is None

    def test_conversation_returns_no_phase(self, session_id, capsys):
        """Conversation mode: phase is None (returned as unclassified)."""
        _set_mode(session_id, "conversation")
        capsys.readouterr()
        result = _call_current_phase(session_id, capsys)
        assert result["phase"] == "unclassified"

    def test_work_planning_before_phase_a(self, session_id, project_root, capsys):
        """Work mode without phase-a: phase is 'planning'."""
        _set_mode(session_id, "work")
        capsys.readouterr()
        result = _call_current_phase(session_id, capsys)
        assert result["phase"] == "planning"

    def test_work_testing_after_phase_a(self, session_id, project_root, capsys):
        """Work mode with phase-a but no test-skeletons: phase is 'testing'."""
        _set_mode(session_id, "work")
        capsys.readouterr()
        _approve_gate(session_id, "phase-a")
        result = _call_current_phase(session_id, capsys)
        assert result["phase"] == "testing"

    def test_work_implementation_after_test_skeletons(
        self, session_id, project_root, capsys
    ):
        """Work mode with both gates: phase is 'implementation'."""
        _set_mode(session_id, "work")
        capsys.readouterr()
        _approve_gate(session_id, "phase-a")
        _approve_gate(session_id, "test-skeletons")
        result = _call_current_phase(session_id, capsys)
        assert result["phase"] == "implementation"

    def test_work_phase_progression(self, session_id, project_root, capsys):
        """Work mode: phase progresses planning -> testing -> implementation."""
        _set_mode(session_id, "work")
        capsys.readouterr()

        result = _call_current_phase(session_id, capsys)
        assert result["phase"] == "planning"

        _approve_gate(session_id, "phase-a")
        result = _call_current_phase(session_id, capsys)
        assert result["phase"] == "testing"

        _approve_gate(session_id, "test-skeletons")
        result = _call_current_phase(session_id, capsys)
        assert result["phase"] == "implementation"


# ===========================================================================
# Phase-aware loaded_rule_ids
# ===========================================================================

class TestPhaseAwareRuleIds:

    def test_rule_ids_stored_by_phase(self, session_id):
        """Rule IDs added via update are tagged with the current phase."""
        _set_mode(session_id, "work")
        writ_session.cmd_update(session_id, ["--add-rules", '["PERF-IO-001"]'])

        cache = _read_cache(session_id)
        by_phase = cache["loaded_rule_ids_by_phase"]
        assert "PERF-IO-001" in by_phase.get("planning", [])

    def test_current_phase_ids_returned(self, session_id):
        """Reading loaded_rule_ids_by_phase[current_phase] gives current IDs."""
        _set_mode(session_id, "work")
        writ_session.cmd_update(session_id, ["--add-rules", '["PERF-IO-001", "ARCH-ORG-001"]'])

        cache = _read_cache(session_id)
        current_phase = cache["current_phase"]
        phase_ids = cache["loaded_rule_ids_by_phase"].get(current_phase, [])
        assert "PERF-IO-001" in phase_ids
        assert "ARCH-ORG-001" in phase_ids

    def test_historical_ids_not_excluded(self, session_id, project_root, monkeypatch, capsys):
        """After phase transition, historical IDs are not in the current-phase exclude list."""
        _set_mode(session_id, "work")
        capsys.readouterr()

        # Add rules in planning phase
        writ_session.cmd_update(session_id, ["--add-rules", '["PERF-IO-001", "ARCH-ORG-001"]'])

        # Advance to testing
        _write_plan(project_root)
        _call_advance_phase(session_id, "approved", str(project_root), monkeypatch, capsys)

        cache = _read_cache(session_id)
        current_phase = cache["current_phase"]
        assert current_phase == "testing"

        # Current phase should have empty rule IDs
        current_ids = cache["loaded_rule_ids_by_phase"].get(current_phase, [])
        assert current_ids == []

        # PERF-IO-001 should be in historical, not blocking re-injection
        historical = cache["loaded_rule_ids_by_phase"].get("_historical", [])
        assert "PERF-IO-001" in historical

    def test_rule_ids_cleared_on_advance(self, session_id, project_root, monkeypatch, capsys):
        """advance-phase moves current IDs to historical, starts fresh."""
        _set_mode(session_id, "work")
        capsys.readouterr()

        _load_plan_rules(session_id)
        _write_plan(project_root)
        _call_advance_phase(session_id, "approved", str(project_root), monkeypatch, capsys)

        cache = _read_cache(session_id)
        # Old planning IDs cleared
        assert cache["loaded_rule_ids_by_phase"].get("planning", []) == []
        # Historical has them
        hist = cache["loaded_rule_ids_by_phase"]["_historical"]
        for rid in VALID_PLAN_RULE_IDS:
            assert rid in hist
        # Flat list still has all (for feedback/coverage)
        for rid in VALID_PLAN_RULE_IDS:
            assert rid in cache["loaded_rule_ids"]


# ===========================================================================
# Audit trail
# ===========================================================================

class TestAuditTrail:

    def test_mode_set_creates_initial_transition(self, session_id):
        """Setting a mode logs the first transition (None -> phase)."""
        _set_mode(session_id, "work")

        cache = _read_cache(session_id)
        transitions = cache["phase_transitions"]
        assert len(transitions) == 1
        assert transitions[0]["from"] is None
        assert transitions[0]["to"] == "planning"
        assert transitions[0]["trigger"] == "mode-set"
        assert transitions[0]["mode"] == "work"

    def test_advance_appends_transition(self, session_id, project_root, monkeypatch, capsys):
        """Each advance-phase appends to phase_transitions list."""
        _set_mode(session_id, "work")
        _load_plan_rules(session_id)
        capsys.readouterr()
        _write_plan(project_root)
        _call_advance_phase(session_id, "approved", str(project_root), monkeypatch, capsys)

        cache = _read_cache(session_id)
        transitions = cache["phase_transitions"]
        assert len(transitions) == 2  # mode-set + advance

    def test_transition_contains_required_fields(self, session_id, project_root, monkeypatch, capsys):
        """Each transition has: from, to, ts, trigger, mode."""
        _set_mode(session_id, "work")
        _load_plan_rules(session_id)
        capsys.readouterr()
        _write_plan(project_root)
        _call_advance_phase(session_id, "approved", str(project_root), monkeypatch, capsys)

        cache = _read_cache(session_id)
        advance_transition = cache["phase_transitions"][-1]
        assert "from" in advance_transition
        assert "to" in advance_transition
        assert "ts" in advance_transition
        assert "trigger" in advance_transition
        assert "mode" in advance_transition

    def test_artifacts_validated_field_present(self, session_id, project_root, monkeypatch, capsys):
        """Transitions from user-approved advances include artifacts_validated."""
        _set_mode(session_id, "work")
        _load_plan_rules(session_id)
        capsys.readouterr()
        _write_plan(project_root)
        _call_advance_phase(session_id, "approved", str(project_root), monkeypatch, capsys)

        cache = _read_cache(session_id)
        advance_transition = cache["phase_transitions"][-1]
        assert "artifacts_validated" in advance_transition
        assert isinstance(advance_transition["artifacts_validated"], list)
        assert any("plan.md" in a for a in advance_transition["artifacts_validated"])
