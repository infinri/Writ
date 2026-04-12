"""Tests for mode infrastructure in bin/lib/writ-session.py.

Covers: mode set/get/switch, Work state preservation, friction events,
tier facade mapping, and mode-based gate enforcement.

Per TEST-TDD-001: skeletons approved before implementation.
"""

from __future__ import annotations

import importlib.util
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
    return "test-mode-session"


@pytest.fixture()
def project_root(tmp_path):
    """Create a minimal project root with .git marker and gates dir."""
    root = tmp_path / "project"
    root.mkdir()
    (root / ".git").mkdir()
    (root / ".claude" / "gates").mkdir(parents=True)
    return root


def _read_raw_cache(tmp_path, session_id: str) -> dict:
    path = os.path.join(str(tmp_path), f"writ-session-{session_id}.json")
    with open(path) as f:
        return json.load(f)


# ===========================================================================
# mode get: reading mode from session cache
# ===========================================================================

class TestModeGet:

    def test_get_returns_empty_when_no_mode_set(self, session_id, capsys):
        """Fresh session has no mode -- get should output empty string."""
        writ_session.cmd_mode(session_id, "get")
        out = capsys.readouterr().out
        assert out.strip() == ""

    def test_get_returns_mode_after_set(self, session_id, capsys):
        """After setting mode=work, get should output 'work'."""
        writ_session.cmd_mode(session_id, "set", "work")
        capsys.readouterr()
        writ_session.cmd_mode(session_id, "get")
        out = capsys.readouterr().out
        assert out.strip() == "work"

    def test_get_returns_plain_string_not_json(self, session_id, capsys):
        """Output is plain 'work', not '"work"' or '{"mode": "work"}'."""
        writ_session.cmd_mode(session_id, "set", "work")
        capsys.readouterr()
        writ_session.cmd_mode(session_id, "get")
        out = capsys.readouterr().out.strip()
        assert out == "work"
        assert '"' not in out
        assert "{" not in out


# ===========================================================================
# mode set: writing mode to session cache with fresh state
# ===========================================================================

class TestModeSet:

    def test_set_conversation(self, session_id, capsys):
        """'conversation' is a valid mode."""
        writ_session.cmd_mode(session_id, "set", "conversation")
        out = capsys.readouterr().out
        assert "set: conversation" in out

    def test_set_debug(self, session_id, capsys):
        """'debug' is a valid mode."""
        writ_session.cmd_mode(session_id, "set", "debug")
        out = capsys.readouterr().out
        assert "set: debug" in out

    def test_set_review(self, session_id, capsys):
        """'review' is a valid mode."""
        writ_session.cmd_mode(session_id, "set", "review")
        out = capsys.readouterr().out
        assert "set: review" in out

    def test_set_work(self, session_id, capsys):
        """'work' is a valid mode."""
        writ_session.cmd_mode(session_id, "set", "work")
        out = capsys.readouterr().out
        assert "set: work" in out

    def test_set_rejects_invalid_mode(self, session_id):
        """Invalid mode name must fail."""
        with pytest.raises(SystemExit, match="1"):
            writ_session.cmd_mode(session_id, "set", "invalid")

    def test_set_rejects_tier_number(self, session_id):
        """Tier numbers are not valid mode values."""
        with pytest.raises(SystemExit, match="1"):
            writ_session.cmd_mode(session_id, "set", "2")

    def test_set_persists_to_cache(self, session_id, tmp_path):
        """Mode value must be readable from the JSON cache file on disk."""
        writ_session.cmd_mode(session_id, "set", "work")
        data = _read_raw_cache(tmp_path, session_id)
        assert data["mode"] == "work"

    def test_set_work_initializes_planning_phase(self, session_id, tmp_path):
        """Setting work mode starts in 'planning' phase with empty gates."""
        writ_session.cmd_mode(session_id, "set", "work")
        data = _read_raw_cache(tmp_path, session_id)
        assert data["current_phase"] == "planning"
        assert data["gates_approved"] == []

    def test_set_nonwork_has_no_phase(self, session_id, tmp_path):
        """Non-work modes have no phase (None)."""
        writ_session.cmd_mode(session_id, "set", "debug")
        data = _read_raw_cache(tmp_path, session_id)
        assert data["current_phase"] is None

    def test_set_clears_previous_work_state(self, session_id, tmp_path):
        """Setting mode always starts fresh -- previous gates/phase cleared."""
        # First set work mode and approve a gate
        writ_session.cmd_mode(session_id, "set", "work")
        cache = writ_session._read_cache(session_id)
        cache["gates_approved"] = ["phase-a"]
        cache["current_phase"] = "testing"
        writ_session._write_cache(session_id, cache)

        # Set work mode again -- should be fresh
        writ_session.cmd_mode(session_id, "set", "work")
        data = _read_raw_cache(tmp_path, session_id)
        assert data["gates_approved"] == []
        assert data["current_phase"] == "planning"

    def test_set_preserves_non_workflow_cache_fields(self, session_id, tmp_path):
        """Setting mode must not clobber loaded_rule_ids, remaining_budget, etc."""
        writ_session.cmd_update(session_id, [
            "--add-rules", '["PERF-IO-001", "SEC-UNI-002"]',
            "--cost", "240",
            "--context-percent", "30",
        ])
        writ_session.cmd_mode(session_id, "set", "work")
        data = _read_raw_cache(tmp_path, session_id)
        assert data["mode"] == "work"
        assert "PERF-IO-001" in data["loaded_rule_ids"]
        assert data["remaining_budget"] == 8000 - 240

    def test_set_same_mode_resets_state(self, session_id, tmp_path):
        """Re-setting same mode resets workflow state (fresh start)."""
        writ_session.cmd_mode(session_id, "set", "work")
        cache = writ_session._read_cache(session_id)
        cache["gates_approved"] = ["phase-a"]
        writ_session._write_cache(session_id, cache)

        writ_session.cmd_mode(session_id, "set", "work")
        data = _read_raw_cache(tmp_path, session_id)
        assert data["gates_approved"] == []

    def test_set_clears_paused_work_state(self, session_id, tmp_path):
        """mode set clears any paused work state (no accidental resume)."""
        writ_session.cmd_mode(session_id, "set", "work")
        cache = writ_session._read_cache(session_id)
        cache["paused_work_state"] = {"phase": "testing", "gates_approved": ["phase-a"]}
        writ_session._write_cache(session_id, cache)

        writ_session.cmd_mode(session_id, "set", "debug")
        data = _read_raw_cache(tmp_path, session_id)
        assert data["paused_work_state"] is None


# ===========================================================================
# mode switch: preserving Work state across mode transitions
# ===========================================================================

class TestModeSwitch:

    def test_switch_work_to_debug_preserves_state(self, session_id, tmp_path):
        """Switching from Work to Debug saves Work state in paused_work_state."""
        writ_session.cmd_mode(session_id, "set", "work")
        cache = writ_session._read_cache(session_id)
        cache["gates_approved"] = ["phase-a"]
        cache["current_phase"] = "testing"
        cache["loaded_rule_ids_by_phase"] = {"planning": ["R-001"], "testing": ["R-002"]}
        writ_session._write_cache(session_id, cache)

        writ_session.cmd_mode(session_id, "switch", "debug")
        data = _read_raw_cache(tmp_path, session_id)
        assert data["mode"] == "debug"
        assert data["paused_work_state"] is not None
        assert data["paused_work_state"]["gates_approved"] == ["phase-a"]
        assert data["paused_work_state"]["phase"] == "testing"

    def test_switch_debug_to_work_restores_state(self, session_id, tmp_path):
        """Switching back to Work from Debug restores the paused state."""
        # Set up Work with a gate approved, then switch away
        writ_session.cmd_mode(session_id, "set", "work")
        cache = writ_session._read_cache(session_id)
        cache["gates_approved"] = ["phase-a"]
        cache["current_phase"] = "testing"
        writ_session._write_cache(session_id, cache)

        writ_session.cmd_mode(session_id, "switch", "debug")
        writ_session.cmd_mode(session_id, "switch", "work")

        data = _read_raw_cache(tmp_path, session_id)
        assert data["mode"] == "work"
        assert data["current_phase"] == "testing"
        assert "phase-a" in data["gates_approved"]
        assert data["paused_work_state"] is None  # consumed on restore

    def test_switch_to_work_without_paused_state_is_fresh(self, session_id, tmp_path):
        """Switching to Work with no paused state behaves like mode set."""
        writ_session.cmd_mode(session_id, "set", "conversation")
        writ_session.cmd_mode(session_id, "switch", "work")

        data = _read_raw_cache(tmp_path, session_id)
        assert data["mode"] == "work"
        assert data["current_phase"] == "planning"
        assert data["gates_approved"] == []

    def test_switch_between_nonwork_modes_no_state_preservation(self, session_id, tmp_path):
        """Switching between non-work modes doesn't save/restore state."""
        writ_session.cmd_mode(session_id, "set", "conversation")
        writ_session.cmd_mode(session_id, "switch", "debug")

        data = _read_raw_cache(tmp_path, session_id)
        assert data["mode"] == "debug"
        assert data["paused_work_state"] is None

    def test_switch_nonwork_to_nonwork_clears_phase(self, session_id, tmp_path):
        """Switching from debug to review should have no phase."""
        writ_session.cmd_mode(session_id, "set", "debug")
        writ_session.cmd_mode(session_id, "switch", "review")

        data = _read_raw_cache(tmp_path, session_id)
        assert data["mode"] == "review"
        assert data["current_phase"] is None


# ===========================================================================
# Friction events: mode_change
# ===========================================================================

class TestModeChangeFrictionEvents:

    def test_mode_set_logs_friction_event(self, session_id, tmp_path, monkeypatch):
        """mode set should log a mode_change event with type=set."""
        log_path = tmp_path / "workflow-friction.log"
        # Patch _log_friction_event to write to our temp log
        original_log = writ_session._log_friction_event
        logged_events = []

        def capture_event(sid, mode, event, **extra):
            logged_events.append({"session": sid, "mode": mode, "event": event, **extra})

        monkeypatch.setattr(writ_session, "_log_friction_event", capture_event)

        writ_session.cmd_mode(session_id, "set", "work")
        assert len(logged_events) == 1
        assert logged_events[0]["event"] == "mode_change"
        assert logged_events[0]["mode"] == "work"

    def test_mode_switch_logs_friction_event_with_type(self, session_id, tmp_path, monkeypatch):
        """mode switch should log mode_change with type=switch."""
        logged_events = []

        def capture_event(sid, mode, event, **extra):
            logged_events.append({"session": sid, "mode": mode, "event": event, **extra})

        monkeypatch.setattr(writ_session, "_log_friction_event", capture_event)

        writ_session.cmd_mode(session_id, "set", "work")
        writ_session.cmd_mode(session_id, "switch", "debug")

        switch_events = [e for e in logged_events if e.get("change_type") == "switch"]
        assert len(switch_events) == 1
        assert switch_events[0]["from_mode"] == "work"
        assert switch_events[0]["to_mode"] == "debug"


# ===========================================================================
# Tier facade: maps tier set/get to mode internally
# ===========================================================================

class TestTierFacade:

    def test_tier_set_0_maps_to_conversation(self, session_id, tmp_path, capsys):
        """tier set 0 should set mode to conversation."""
        writ_session.cmd_tier(session_id, "set", "0")
        out = capsys.readouterr().out
        assert "set: 0" in out
        data = _read_raw_cache(tmp_path, session_id)
        assert data["mode"] == "conversation"

    def test_tier_set_1_maps_to_work(self, session_id, tmp_path, capsys):
        """tier set 1 should set mode to work."""
        writ_session.cmd_tier(session_id, "set", "1")
        out = capsys.readouterr().out
        assert "set: 1" in out
        data = _read_raw_cache(tmp_path, session_id)
        assert data["mode"] == "work"

    def test_tier_set_2_maps_to_work(self, session_id, tmp_path, capsys):
        """tier set 2 should set mode to work."""
        writ_session.cmd_tier(session_id, "set", "2")
        out = capsys.readouterr().out
        assert "set: 2" in out
        data = _read_raw_cache(tmp_path, session_id)
        assert data["mode"] == "work"

    def test_tier_set_3_maps_to_work(self, session_id, tmp_path, capsys):
        """tier set 3 should set mode to work."""
        writ_session.cmd_tier(session_id, "set", "3")
        out = capsys.readouterr().out
        assert "set: 3" in out
        data = _read_raw_cache(tmp_path, session_id)
        assert data["mode"] == "work"

    def test_tier_get_returns_0_for_conversation(self, session_id, capsys):
        """When mode=conversation, tier get should return '0'."""
        writ_session.cmd_mode(session_id, "set", "conversation")
        capsys.readouterr()
        writ_session.cmd_tier(session_id, "get")
        out = capsys.readouterr().out.strip()
        assert out == "0"

    def test_tier_get_returns_2_for_work(self, session_id, capsys):
        """When mode=work, tier get should return '2'."""
        writ_session.cmd_mode(session_id, "set", "work")
        capsys.readouterr()
        writ_session.cmd_tier(session_id, "get")
        out = capsys.readouterr().out.strip()
        assert out == "2"

    def test_tier_get_returns_0_for_debug(self, session_id, capsys):
        """When mode=debug, tier get should return '0' (no gates, like old tier 0)."""
        writ_session.cmd_mode(session_id, "set", "debug")
        capsys.readouterr()
        writ_session.cmd_tier(session_id, "get")
        out = capsys.readouterr().out.strip()
        assert out == "0"

    def test_tier_get_returns_empty_when_no_mode(self, session_id, capsys):
        """No mode set: tier get returns empty (hooks see 'no tier declared')."""
        writ_session.cmd_tier(session_id, "get")
        out = capsys.readouterr().out.strip()
        assert out == ""

    def test_tier_set_still_rejects_invalid_values(self, session_id):
        """Tier facade must still reject invalid tier numbers."""
        with pytest.raises(SystemExit, match="1"):
            writ_session.cmd_tier(session_id, "set", "5")

    def test_tier_no_escalation_enforcement(self, session_id, capsys):
        """Tier facade no longer enforces escalation (modes switch freely)."""
        writ_session.cmd_tier(session_id, "set", "2")
        capsys.readouterr()
        # In v1 this would fail (downgrade 2->0). In v2 it maps to mode set.
        writ_session.cmd_tier(session_id, "set", "0")
        out = capsys.readouterr().out
        assert "set: 0" in out


# ===========================================================================
# Mode-based can-write enforcement
# ===========================================================================

class TestModeCanWrite:

    def _call_can_write(self, session_id, file_path, monkeypatch, capsys):
        import io
        capsys.readouterr()
        envelope = json.dumps({"tool_input": {"file_path": file_path}})
        monkeypatch.setattr("sys.stdin", io.StringIO(envelope))
        writ_session.cmd_can_write(session_id, SKILL_DIR)
        out = capsys.readouterr().out.strip()
        return json.loads(out)

    def test_no_mode_denies_source_files(self, session_id, project_root, monkeypatch, capsys):
        """No mode set: deny source files."""
        result = self._call_can_write(session_id, str(project_root / "main.py"), monkeypatch, capsys)
        assert result["decision"] == "deny"

    def test_no_mode_allows_plan_md(self, session_id, project_root, monkeypatch, capsys):
        """No mode set: plan.md is always allowed."""
        result = self._call_can_write(session_id, str(project_root / "plan.md"), monkeypatch, capsys)
        assert result["decision"] == "allow"

    def test_conversation_allows_all(self, session_id, project_root, monkeypatch, capsys):
        """Conversation mode: all writes allowed."""
        writ_session.cmd_mode(session_id, "set", "conversation")
        result = self._call_can_write(session_id, str(project_root / "anything.py"), monkeypatch, capsys)
        assert result["decision"] == "allow"

    def test_debug_allows_all(self, session_id, project_root, monkeypatch, capsys):
        """Debug mode: all writes allowed (enforcement is in CLAUDE.md, not hooks)."""
        writ_session.cmd_mode(session_id, "set", "debug")
        result = self._call_can_write(session_id, str(project_root / "service.py"), monkeypatch, capsys)
        assert result["decision"] == "allow"

    def test_review_allows_all(self, session_id, project_root, monkeypatch, capsys):
        """Review mode: all writes allowed (enforcement is in CLAUDE.md, not hooks)."""
        writ_session.cmd_mode(session_id, "set", "review")
        result = self._call_can_write(session_id, str(project_root / "service.py"), monkeypatch, capsys)
        assert result["decision"] == "allow"

    def test_work_denies_before_plan_gate(self, session_id, project_root, monkeypatch, capsys):
        """Work mode: source file denied before phase-a gate."""
        writ_session.cmd_mode(session_id, "set", "work")
        result = self._call_can_write(session_id, str(project_root / "service.py"), monkeypatch, capsys)
        assert result["decision"] == "deny"

    def test_work_denies_after_plan_before_test_skeletons(self, session_id, project_root, monkeypatch, capsys):
        """Work mode: source file denied after phase-a but before test-skeletons."""
        writ_session.cmd_mode(session_id, "set", "work")
        cache = writ_session._read_cache(session_id)
        cache["gates_approved"] = ["phase-a"]
        cache["current_phase"] = "testing"
        writ_session._write_cache(session_id, cache)

        result = self._call_can_write(session_id, str(project_root / "service.py"), monkeypatch, capsys)
        assert result["decision"] == "deny"

    def test_work_allows_after_both_gates(self, session_id, project_root, monkeypatch, capsys):
        """Work mode: source file allowed after both phase-a and test-skeletons."""
        writ_session.cmd_mode(session_id, "set", "work")
        cache = writ_session._read_cache(session_id)
        cache["gates_approved"] = ["phase-a", "test-skeletons"]
        cache["current_phase"] = "implementation"
        writ_session._write_cache(session_id, cache)

        result = self._call_can_write(session_id, str(project_root / "service.py"), monkeypatch, capsys)
        assert result["decision"] == "allow"

    def test_work_allows_test_files_without_gates(self, session_id, project_root, monkeypatch, capsys):
        """Work mode: test files bypass gate checks (in exclusions list)."""
        writ_session.cmd_mode(session_id, "set", "work")
        result = self._call_can_write(
            session_id, str(project_root / "tests" / "test_foo.py"), monkeypatch, capsys
        )
        assert result["decision"] == "allow"

    def test_work_plan_md_allowed_during_planning(self, session_id, project_root, monkeypatch, capsys):
        """Work mode: plan.md writable during planning phase (before gates)."""
        writ_session.cmd_mode(session_id, "set", "work")
        result = self._call_can_write(session_id, str(project_root / "plan.md"), monkeypatch, capsys)
        assert result["decision"] == "allow"

    def test_work_plan_md_allowed_during_testing(self, session_id, project_root, monkeypatch, capsys):
        """Work mode: plan.md writable during testing phase (after plan gate, before test-skeletons)."""
        writ_session.cmd_mode(session_id, "set", "work")
        cache = writ_session._read_cache(session_id)
        cache["gates_approved"] = ["phase-a"]
        cache["current_phase"] = "testing"
        writ_session._write_cache(session_id, cache)

        result = self._call_can_write(session_id, str(project_root / "plan.md"), monkeypatch, capsys)
        assert result["decision"] == "allow"

    def test_work_plan_md_locked_during_implementation(self, session_id, project_root, monkeypatch, capsys):
        """Work mode: plan.md blocked during implementation phase."""
        writ_session.cmd_mode(session_id, "set", "work")
        cache = writ_session._read_cache(session_id)
        cache["gates_approved"] = ["phase-a", "test-skeletons"]
        cache["current_phase"] = "implementation"
        writ_session._write_cache(session_id, cache)

        result = self._call_can_write(session_id, str(project_root / "plan.md"), monkeypatch, capsys)
        assert result["decision"] == "deny"
        assert "ENF-GATE-PLAN" in result["reason"]

    def test_work_capabilities_md_always_allowed(self, session_id, project_root, monkeypatch, capsys):
        """Work mode: capabilities.md is always writable."""
        writ_session.cmd_mode(session_id, "set", "work")
        result = self._call_can_write(
            session_id, str(project_root / "capabilities.md"), monkeypatch, capsys
        )
        assert result["decision"] == "allow"


# ===========================================================================
# Mode-based advance-phase
# ===========================================================================

class TestModeAdvancePhase:

    PLAN_CONTENT = """\
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
"""
    import re as _re
    PLAN_RULE_IDS = _re.findall(
        r'[A-Z][A-Z0-9]+(?:-[A-Z][A-Z0-9]+)*-\d{3}', PLAN_CONTENT
    )

    def _write_plan(self, project_root, content=None):
        (project_root / "plan.md").write_text(content or self.PLAN_CONTENT)

    def _load_plan_rules(self, session_id):
        """Load rule IDs from the plan fixture so validation passes."""
        writ_session.cmd_update(session_id, ["--add-rules", json.dumps(self.PLAN_RULE_IDS)])

    def _call_advance_phase(self, session_id, prompt, project_root, monkeypatch, capsys):
        import io
        import secrets
        import tempfile

        # Create gate token (simulates what auto-approve-gate.sh does)
        token = secrets.token_hex(16)
        token_path = os.path.join(tempfile.gettempdir(), f"writ-gate-token-{session_id}")
        with open(token_path, "w") as f:
            f.write(token)

        capsys.readouterr()
        monkeypatch.setattr("sys.stdin", io.StringIO(prompt))
        writ_session.cmd_advance_phase(session_id, str(project_root), token)
        out = capsys.readouterr().out.strip()
        return json.loads(out)

    def test_advance_denied_for_nonwork_mode(self, session_id, project_root, monkeypatch, capsys):
        """advance-phase should fail when mode is not work."""
        writ_session.cmd_mode(session_id, "set", "debug")
        self._write_plan(project_root)
        result = self._call_advance_phase(session_id, "approved", project_root, monkeypatch, capsys)
        assert result["advanced"] is False

    def _load_plan_rules(self, session_id):
        """Load rule IDs referenced in the plan fixture so validation passes."""
        import re
        plan = self._write_plan.__func__.__defaults__  # not needed -- just extract from known content
        # The plan fixture references PERF-IO-001
        rule_ids = re.findall(r'[A-Z][A-Z0-9]+(?:-[A-Z][A-Z0-9]+)*-\d{3}', """\
- PERF-IO-001: No sync I/O in the hot path.
""")
        writ_session.cmd_update(session_id, ["--add-rules", json.dumps(rule_ids)])

    def test_advance_phase_a_in_work_mode(self, session_id, project_root, monkeypatch, capsys):
        """Work mode: advance-phase creates phase-a gate when plan is valid."""
        writ_session.cmd_mode(session_id, "set", "work")
        self._load_plan_rules(session_id)
        self._write_plan(project_root)
        capsys.readouterr()
        result = self._call_advance_phase(session_id, "approved", project_root, monkeypatch, capsys)
        assert result["advanced"] is True
        assert result["gate"] == "phase-a"

    def test_advance_test_skeletons_after_phase_a(self, session_id, project_root, monkeypatch, capsys):
        """Work mode: after phase-a, next gate is test-skeletons."""
        writ_session.cmd_mode(session_id, "set", "work")
        cache = writ_session._read_cache(session_id)
        cache["gates_approved"] = ["phase-a"]
        cache["current_phase"] = "testing"
        writ_session._write_cache(session_id, cache)

        # Create a test file with a method signature
        test_dir = project_root / "tests"
        test_dir.mkdir()
        (test_dir / "test_service.py").write_text("def test_service_works():\n    pass\n")

        result = self._call_advance_phase(session_id, "approved", project_root, monkeypatch, capsys)
        assert result["advanced"] is True
        assert result["gate"] == "test-skeletons"

    def test_no_phase_d_in_work_mode(self, session_id, project_root, monkeypatch, capsys):
        """Work mode has no phase-d gate -- sequence is phase-a, test-skeletons only."""
        writ_session.cmd_mode(session_id, "set", "work")
        cache = writ_session._read_cache(session_id)
        cache["gates_approved"] = ["phase-a"]
        cache["current_phase"] = "testing"
        writ_session._write_cache(session_id, cache)

        test_dir = project_root / "tests"
        test_dir.mkdir()
        (test_dir / "test_service.py").write_text("def test_service_works():\n    pass\n")

        # Even with "phase d" in prompt, test-skeletons is the next gate
        result = self._call_advance_phase(session_id, "phase d approved", project_root, monkeypatch, capsys)
        assert result["advanced"] is True
        assert result["gate"] == "test-skeletons"


# ===========================================================================
# cmd_current_phase with mode
# ===========================================================================

class TestCurrentPhaseWithMode:

    def test_returns_mode_field(self, session_id, capsys):
        """current-phase should return mode instead of tier."""
        writ_session.cmd_mode(session_id, "set", "work")
        capsys.readouterr()
        writ_session.cmd_current_phase(session_id)
        out = capsys.readouterr().out.strip()
        result = json.loads(out)
        assert result["mode"] == "work"
        assert result["phase"] == "planning"

    def test_returns_unclassified_when_no_mode(self, session_id, capsys):
        """No mode set: phase is 'unclassified'."""
        writ_session.cmd_current_phase(session_id)
        out = capsys.readouterr().out.strip()
        result = json.loads(out)
        assert result["phase"] == "unclassified"


# ===========================================================================
# cmd_metrics with mode
# ===========================================================================

class TestMetricsWithMode:

    def test_metrics_reports_mode_distribution(self, tmp_path, monkeypatch):
        """Metrics should report mode_distribution instead of tier_distribution."""
        log_path = tmp_path / "workflow-friction.log"
        events = [
            {"session": "s1", "mode": "work", "event": "mode_change"},
            {"session": "s1", "mode": "work", "event": "phase_transition", "from_phase": "planning", "to_phase": "testing"},
            {"session": "s2", "mode": "conversation", "event": "mode_change"},
        ]
        log_path.write_text("\n".join(json.dumps(e) for e in events) + "\n")

        monkeypatch.setattr(writ_session, "CACHE_DIR", str(tmp_path))
        import io
        capsys_out = io.StringIO()
        monkeypatch.setattr("sys.stdout", capsys_out)
        writ_session.cmd_metrics(str(log_path))
        result = json.loads(capsys_out.getvalue())

        assert "mode_distribution" in result
        assert result["mode_distribution"]["work"] >= 1

    def test_metrics_handles_legacy_tier_events(self, tmp_path, monkeypatch):
        """Old friction events with tier field should be mapped to mode_distribution."""
        log_path = tmp_path / "workflow-friction.log"
        events = [
            {"session": "s1", "tier": 2, "event": "phase_transition", "from_phase": "planning", "to_phase": "testing"},
            {"session": "s2", "tier": 0, "event": "approval_pattern_miss", "prompt": "go"},
        ]
        log_path.write_text("\n".join(json.dumps(e) for e in events) + "\n")

        monkeypatch.setattr(writ_session, "CACHE_DIR", str(tmp_path))
        import io
        capsys_out = io.StringIO()
        monkeypatch.setattr("sys.stdout", capsys_out)
        writ_session.cmd_metrics(str(log_path))
        result = json.loads(capsys_out.getvalue())

        assert "mode_distribution" in result
        # tier 2 maps to work, tier 0 maps to conversation
        assert result["mode_distribution"]["work"] >= 1
        assert result["mode_distribution"]["conversation"] >= 1
