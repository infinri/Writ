"""Tests for Phase 3 centralization commands in bin/lib/writ-session.py.

Tests cmd_can_write, cmd_advance_phase, cmd_current_phase, and
phase-aware loaded_rule_ids.

Per TEST-TDD-001: skeletons approved before implementation.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys

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


def _set_tier(session_id: str, tier: int) -> None:
    writ_session.cmd_tier(session_id, "set", str(tier))


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
    """Call cmd_advance_phase with a prompt and return the JSON result."""
    capsys.readouterr()  # clear any prior output
    monkeypatch.setattr("sys.stdin", io.StringIO(prompt))
    writ_session.cmd_advance_phase(session_id, project_root)
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


def _approve_gate(session_id: str, gate: str) -> None:
    """Directly add a gate to the session cache (simulates advance-phase)."""
    cache = writ_session._read_cache(session_id)
    approved = set(cache.get("gates_approved", []))
    approved.add(gate)
    cache["gates_approved"] = sorted(approved)
    phase = writ_session._PHASE_AFTER_GATE.get(gate, "implementation")
    cache["current_phase"] = phase
    writ_session._write_cache(session_id, cache)


# ===========================================================================
# cmd_can_write
# ===========================================================================

class TestCanWrite:

    def test_allow_any_file_tier_0(self, session_id, project_root, monkeypatch, capsys):
        """Tier 0 (research): all writes allowed, no gates."""
        _set_tier(session_id, 0)
        result = _call_can_write(session_id, str(project_root / "anything.py"), monkeypatch, capsys)
        assert result["decision"] == "allow"

    def test_allow_any_file_tier_1(self, session_id, project_root, monkeypatch, capsys):
        """Tier 1 (patch): all writes allowed, no gates."""
        _set_tier(session_id, 1)
        result = _call_can_write(session_id, str(project_root / "service.py"), monkeypatch, capsys)
        assert result["decision"] == "allow"

    def test_deny_no_tier_except_plan_md(self, session_id, project_root, monkeypatch, capsys):
        """No tier declared: deny all files except plan.md."""
        # plan.md is allowed
        result = _call_can_write(session_id, str(project_root / "plan.md"), monkeypatch, capsys)
        assert result["decision"] == "allow"

        # .py file is denied
        result = _call_can_write(session_id, str(project_root / "main.py"), monkeypatch, capsys)
        assert result["decision"] == "deny"
        assert "ENF-GATE-TIER" in result["reason"]

    def test_deny_plan_md_during_implementation_phase(self, session_id, project_root, monkeypatch, capsys):
        """Post test-skeletons gate, plan.md writes are blocked (Tier 2+)."""
        _set_tier(session_id, 2)
        capsys.readouterr()
        _approve_gate(session_id, "phase-a")
        _approve_gate(session_id, "test-skeletons")

        result = _call_can_write(session_id, str(project_root / "plan.md"), monkeypatch, capsys)
        assert result["decision"] == "deny"
        assert "ENF-GATE-PLAN" in result["reason"]

    def test_tier_2_deny_without_phase_a(self, session_id, project_root, monkeypatch, capsys):
        """Tier 2: source file denied before phase-a gate exists."""
        _set_tier(session_id, 2)
        capsys.readouterr()

        result = _call_can_write(session_id, str(project_root / "service.py"), monkeypatch, capsys)
        assert result["decision"] == "deny"
        assert "Phase A" in result["reason"] or "phase-a" in result["reason"].lower()

    def test_tier_2_allow_after_phase_a(self, session_id, project_root, monkeypatch, capsys):
        """Tier 2: source file allowed after phase-a gate in session cache."""
        _set_tier(session_id, 2)
        capsys.readouterr()
        _approve_gate(session_id, "phase-a")

        # .py files match any_source_file -> phase-a gate (remapped for tier 2)
        result = _call_can_write(session_id, str(project_root / "service.py"), monkeypatch, capsys)
        assert result["decision"] == "allow"

    def test_tier_2_deny_stale_gate(self, session_id, project_root, monkeypatch, capsys):
        """Tier 2: gates_approved in cache is the authority -- no disk fallback."""
        _set_tier(session_id, 2)
        capsys.readouterr()

        # Write a gate file on disk but DON'T add to cache
        gate_file = project_root / ".claude" / "gates" / "phase-a.approved"
        gate_file.write_text("different-session-id\n")

        result = _call_can_write(session_id, str(project_root / "service.py"), monkeypatch, capsys)
        assert result["decision"] == "deny"

    def test_tier_3_sequential_gate_enforcement(self, session_id, project_root, monkeypatch, capsys):
        """Tier 3: validation files denied without phase-b, even if phase-a exists."""
        _set_tier(session_id, 3)
        capsys.readouterr()
        _approve_gate(session_id, "phase-a")

        # A validator file requires phase-b (which isn't approved)
        validator_path = str(project_root / "validators" / "input_validator.py")
        result = _call_can_write(session_id, validator_path, monkeypatch, capsys)
        assert result["decision"] == "deny"
        assert "phase-b" in result["reason"].lower() or "ENF-GATE" in result["reason"]

    def test_skip_writ_infrastructure(self, session_id, monkeypatch, capsys):
        """Files in the skill directory bypass gate checks."""
        # No tier set -- would normally deny
        skill_file = os.path.join(SKILL_DIR, "bin", "lib", "writ-session.py")
        result = _call_can_write(session_id, skill_file, monkeypatch, capsys)
        assert result["decision"] == "allow"

    def test_exclusions_from_categories(self, session_id, project_root, monkeypatch, capsys):
        """Test files and conftest.py bypass gate checks."""
        _set_tier(session_id, 2)
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
        _set_tier(session_id, 2)
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
        _set_tier(session_id, 2)
        capsys.readouterr()

        result = _call_advance_phase(session_id, "approved", str(project_root), monkeypatch, capsys)
        assert result["advanced"] is False
        assert "plan.md" in result["reason"].lower()

    def test_advance_fails_missing_section(self, session_id, project_root, monkeypatch, capsys):
        """phase-a advance fails when plan.md is missing ## Analysis."""
        _set_tier(session_id, 2)
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
        _set_tier(session_id, 2)
        capsys.readouterr()
        _write_plan(project_root, PLAN_MD_NO_MATCH)

        result = _call_advance_phase(session_id, "approved", str(project_root), monkeypatch, capsys)
        assert result["advanced"] is True

    def test_advance_clears_current_phase_rule_ids(self, session_id, project_root, monkeypatch, capsys):
        """Phase transition moves current loaded_rule_ids to historical set."""
        _set_tier(session_id, 2)
        capsys.readouterr()

        # Add rules to the planning phase
        writ_session.cmd_update(session_id, ["--add-rules", '["PERF-IO-001", "ARCH-ORG-001"]'])

        cache = _read_cache(session_id)
        assert "PERF-IO-001" in cache["loaded_rule_ids_by_phase"].get("planning", [])

        # Advance phase
        _write_plan(project_root)
        _call_advance_phase(session_id, "approved", str(project_root), monkeypatch, capsys)

        cache = _read_cache(session_id)
        # Planning phase IDs should be cleared
        assert cache["loaded_rule_ids_by_phase"].get("planning", []) == []
        # Historical set should have the old IDs
        historical = cache["loaded_rule_ids_by_phase"].get("_historical", [])
        assert "PERF-IO-001" in historical
        assert "ARCH-ORG-001" in historical

    def test_advance_logs_transition_to_audit_trail(self, session_id, project_root, monkeypatch, capsys):
        """Audit trail entry created with from, to, ts, trigger, artifacts."""
        _set_tier(session_id, 2)
        capsys.readouterr()
        _write_plan(project_root)
        _call_advance_phase(session_id, "approved", str(project_root), monkeypatch, capsys)

        cache = _read_cache(session_id)
        transitions = cache["phase_transitions"]
        # First is tier-set, second is the advance
        assert len(transitions) >= 2
        advance = transitions[-1]
        assert advance["trigger"] == "user-approved"
        assert advance["from"] == "planning"
        assert advance["to"] == "testing"
        assert "ts" in advance
        assert advance.get("gate") == "phase-a"

    def test_advance_skips_phase_d_by_default(self, session_id, project_root, monkeypatch, capsys):
        """Tier 3: 'approved' after phase-c skips to test-skeletons."""
        _set_tier(session_id, 3)
        capsys.readouterr()
        _approve_gate(session_id, "phase-a")
        _approve_gate(session_id, "phase-b")
        _approve_gate(session_id, "phase-c")

        # Write a test file so test-skeletons validation passes
        test_dir = project_root / "tests"
        test_dir.mkdir()
        (test_dir / "test_foo.py").write_text("def test_placeholder():\n    pass\n")

        result = _call_advance_phase(session_id, "approved", str(project_root), monkeypatch, capsys)
        assert result["advanced"] is True
        assert result["gate"] == "test-skeletons"

    def test_advance_phase_d_when_explicit(self, session_id, project_root, monkeypatch, capsys):
        """Tier 3: 'phase d approved' creates phase-d gate."""
        _set_tier(session_id, 3)
        capsys.readouterr()
        _approve_gate(session_id, "phase-a")
        _approve_gate(session_id, "phase-b")
        _approve_gate(session_id, "phase-c")

        # Write plan.md with ## Concurrency
        plan_with_concurrency = VALID_PLAN_MD + "\n## Concurrency\n\nSingle-threaded consumer model.\n"
        _write_plan(project_root, plan_with_concurrency)

        result = _call_advance_phase(session_id, "phase d approved", str(project_root), monkeypatch, capsys)
        assert result["advanced"] is True
        assert result["gate"] == "phase-d"

    def test_advance_test_skeletons_requires_test_file(
        self, session_id, project_root, monkeypatch, capsys
    ):
        """test-skeletons advance requires at least one test file with method sig."""
        _set_tier(session_id, 2)
        capsys.readouterr()
        _approve_gate(session_id, "phase-a")

        # No test files exist
        result = _call_advance_phase(session_id, "approved", str(project_root), monkeypatch, capsys)
        assert result["advanced"] is False
        assert "test" in result["reason"].lower()

    def test_advance_no_op_when_all_gates_approved(self, session_id, project_root, monkeypatch, capsys):
        """Advancing when all gates exist returns a no-op result."""
        _set_tier(session_id, 2)
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

    def test_no_tier_returns_unclassified(self, session_id, capsys):
        """No tier set: phase is 'unclassified'."""
        result = _call_current_phase(session_id, capsys)
        assert result["phase"] == "unclassified"
        assert result["tier"] is None

    def test_tier_0_returns_research(self, session_id, capsys):
        """Tier 0: phase is always 'research'."""
        _set_tier(session_id, 0)
        capsys.readouterr()
        result = _call_current_phase(session_id, capsys)
        assert result["phase"] == "research"

    def test_tier_1_returns_implementation(self, session_id, capsys):
        """Tier 1: phase is always 'implementation' (no gates)."""
        _set_tier(session_id, 1)
        capsys.readouterr()
        result = _call_current_phase(session_id, capsys)
        assert result["phase"] == "implementation"

    def test_tier_2_planning_before_phase_a(self, session_id, project_root, capsys):
        """Tier 2 without phase-a: phase is 'planning'."""
        _set_tier(session_id, 2)
        capsys.readouterr()
        result = _call_current_phase(session_id, capsys)
        assert result["phase"] == "planning"

    def test_tier_2_testing_after_phase_a(self, session_id, project_root, capsys):
        """Tier 2 with phase-a but no test-skeletons: phase is 'testing'."""
        _set_tier(session_id, 2)
        capsys.readouterr()
        _approve_gate(session_id, "phase-a")
        result = _call_current_phase(session_id, capsys)
        assert result["phase"] == "testing"

    def test_tier_2_implementation_after_test_skeletons(
        self, session_id, project_root, capsys
    ):
        """Tier 2 with both gates: phase is 'implementation'."""
        _set_tier(session_id, 2)
        capsys.readouterr()
        _approve_gate(session_id, "phase-a")
        _approve_gate(session_id, "test-skeletons")
        result = _call_current_phase(session_id, capsys)
        assert result["phase"] == "implementation"

    def test_tier_3_phase_progression(self, session_id, project_root, capsys):
        """Tier 3: phase progresses through planning -> testing -> implementation."""
        _set_tier(session_id, 3)
        capsys.readouterr()

        result = _call_current_phase(session_id, capsys)
        assert result["phase"] == "planning"

        _approve_gate(session_id, "phase-a")
        result = _call_current_phase(session_id, capsys)
        assert result["phase"] == "testing"

        _approve_gate(session_id, "phase-b")
        result = _call_current_phase(session_id, capsys)
        assert result["phase"] == "integration"

        _approve_gate(session_id, "phase-c")
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
        _set_tier(session_id, 2)
        writ_session.cmd_update(session_id, ["--add-rules", '["PERF-IO-001"]'])

        cache = _read_cache(session_id)
        by_phase = cache["loaded_rule_ids_by_phase"]
        assert "PERF-IO-001" in by_phase.get("planning", [])

    def test_current_phase_ids_returned(self, session_id):
        """Reading loaded_rule_ids_by_phase[current_phase] gives current IDs."""
        _set_tier(session_id, 2)
        writ_session.cmd_update(session_id, ["--add-rules", '["PERF-IO-001", "ARCH-ORG-001"]'])

        cache = _read_cache(session_id)
        current_phase = cache["current_phase"]
        phase_ids = cache["loaded_rule_ids_by_phase"].get(current_phase, [])
        assert "PERF-IO-001" in phase_ids
        assert "ARCH-ORG-001" in phase_ids

    def test_historical_ids_not_excluded(self, session_id, project_root, monkeypatch, capsys):
        """After phase transition, historical IDs are not in the current-phase exclude list."""
        _set_tier(session_id, 2)
        capsys.readouterr()

        # Add rules in planning phase
        writ_session.cmd_update(session_id, ["--add-rules", '["PERF-IO-001"]'])

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
        _set_tier(session_id, 2)
        capsys.readouterr()

        writ_session.cmd_update(session_id, ["--add-rules", '["SEC-UNI-002", "ARCH-DRY-001"]'])
        _write_plan(project_root)
        _call_advance_phase(session_id, "approved", str(project_root), monkeypatch, capsys)

        cache = _read_cache(session_id)
        # Old planning IDs cleared
        assert cache["loaded_rule_ids_by_phase"].get("planning", []) == []
        # Historical has them
        hist = cache["loaded_rule_ids_by_phase"]["_historical"]
        assert "SEC-UNI-002" in hist
        assert "ARCH-DRY-001" in hist
        # Flat list still has all (for feedback/coverage)
        assert "SEC-UNI-002" in cache["loaded_rule_ids"]


# ===========================================================================
# Audit trail
# ===========================================================================

class TestAuditTrail:

    def test_tier_set_creates_initial_transition(self, session_id):
        """Setting a tier logs the first transition (null -> phase)."""
        _set_tier(session_id, 2)

        cache = _read_cache(session_id)
        transitions = cache["phase_transitions"]
        assert len(transitions) == 1
        assert transitions[0]["from"] is None
        assert transitions[0]["to"] == "planning"
        assert transitions[0]["trigger"] == "tier-set"
        assert transitions[0]["tier"] == 2

    def test_advance_appends_transition(self, session_id, project_root, monkeypatch, capsys):
        """Each advance-phase appends to phase_transitions list."""
        _set_tier(session_id, 2)
        capsys.readouterr()
        _write_plan(project_root)
        _call_advance_phase(session_id, "approved", str(project_root), monkeypatch, capsys)

        cache = _read_cache(session_id)
        transitions = cache["phase_transitions"]
        assert len(transitions) == 2  # tier-set + advance

    def test_transition_contains_required_fields(self, session_id, project_root, monkeypatch, capsys):
        """Each transition has: from, to, ts, trigger, tier."""
        _set_tier(session_id, 2)
        capsys.readouterr()
        _write_plan(project_root)
        _call_advance_phase(session_id, "approved", str(project_root), monkeypatch, capsys)

        cache = _read_cache(session_id)
        advance_transition = cache["phase_transitions"][-1]
        assert "from" in advance_transition
        assert "to" in advance_transition
        assert "ts" in advance_transition
        assert "trigger" in advance_transition
        assert "tier" in advance_transition

    def test_artifacts_validated_field_present(self, session_id, project_root, monkeypatch, capsys):
        """Transitions from user-approved advances include artifacts_validated."""
        _set_tier(session_id, 2)
        capsys.readouterr()
        _write_plan(project_root)
        _call_advance_phase(session_id, "approved", str(project_root), monkeypatch, capsys)

        cache = _read_cache(session_id)
        advance_transition = cache["phase_transitions"][-1]
        assert "artifacts_validated" in advance_transition
        assert isinstance(advance_transition["artifacts_validated"], list)
        assert any("plan.md" in a for a in advance_transition["artifacts_validated"])