"""Increment 5: MODE_CONFIG engine consolidation (pure refactor).

Per-mode gate behavior becomes data: a single MODE_CONFIG table feeds the
helpers and cmd_advance_phase, and the dead _GATE_VALIDATORS registry is
populated and dispatched. This is a behavior-preserving extraction, so:

- Snapshot oracle: MODE_CONFIG / VALID_MODES / legacy aliases / _GATE_VALIDATORS
  match the captured pre-refactor literals (RED until the refactor lands).
- Golden-replay oracle: a full Work cycle produces identical transitions and
  gate artifacts (GREEN on current code; must stay GREEN after the refactor).

Loads writ-session.py as a module, mirroring tests/test_mode_infrastructure.py.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import secrets

import pytest

# autouse: pins cwd to a sandbox so `mode set` cannot delete THIS repo's gate artifacts.
from tests.fixtures.session_state import sandbox_cwd, write_bound_gate_token  # noqa: F401

HELPER_PATH = os.path.join(os.path.dirname(__file__), os.pardir, "bin", "lib", "writ-session.py")
_spec = importlib.util.spec_from_file_location("writ_session_engine", HELPER_PATH)
writ_session = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(writ_session)

# Captured pre-refactor literals -- the parity baseline.
EXPECTED_WORK_GATE_SEQUENCE = ["phase-a", "test-skeletons"]
EXPECTED_WORK_PHASE_AFTER = {"phase-a": "testing", "test-skeletons": "implementation"}
EXPECTED_VALID_MODES = {"conversation", "debug", "review", "work", "investigate"}

PLAN_CONTENT = """\
## Files
- service.py

## Analysis
Implement the thing with care and verify behavior.

## Rules Applied
- TEST-CI-001: all tests pass before merge.

## Capabilities
- [ ] the thing works
"""


@pytest.fixture()
def session_id(tmp_path, monkeypatch):
    monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
    return "test-mode-engine"


@pytest.fixture()
def project_root(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / ".git").mkdir()
    (root / ".claude" / "gates").mkdir(parents=True)
    return root


class TestModeConfigSnapshot:
    """The refactor must preserve the engine's data byte-for-byte."""

    def test_mode_config_exists(self) -> None:
        assert hasattr(writ_session, "MODE_CONFIG"), "MODE_CONFIG must be the per-mode SSOT"

    def test_work_config_matches_literals(self) -> None:
        assert hasattr(writ_session, "MODE_CONFIG"), "MODE_CONFIG not defined yet"
        work = writ_session.MODE_CONFIG["work"]
        assert list(work["gate_sequence"]) == EXPECTED_WORK_GATE_SEQUENCE
        assert dict(work["phase_after_gate"]) == EXPECTED_WORK_PHASE_AFTER
        assert work["initial_phase"] == "planning"

    def test_debug_gate_sequence_is_empty(self) -> None:
        assert hasattr(writ_session, "MODE_CONFIG"), "MODE_CONFIG not defined yet"
        assert list(writ_session.MODE_CONFIG["debug"]["gate_sequence"]) == []

    def test_valid_modes_unchanged_and_is_set(self) -> None:
        assert set(writ_session.VALID_MODES) == EXPECTED_VALID_MODES
        assert isinstance(writ_session.VALID_MODES, set)

    def test_legacy_aliases_unchanged(self) -> None:
        assert list(writ_session.GATE_SEQUENCE_WORK) == EXPECTED_WORK_GATE_SEQUENCE
        assert dict(writ_session._PHASE_AFTER_GATE_WORK) == EXPECTED_WORK_PHASE_AFTER

    def test_gate_validators_populated_and_mapped(self) -> None:
        gv = writ_session._GATE_VALIDATORS
        assert gv.get("phase-a") is writ_session._validate_phase_a
        assert gv.get("test-skeletons") is writ_session._validate_test_skeletons


class TestHelperParity:
    """Helpers read MODE_CONFIG; their outputs match today's behavior."""

    def test_gate_sequence_for_mode(self) -> None:
        assert list(writ_session._gate_sequence_for_mode("work")) == EXPECTED_WORK_GATE_SEQUENCE
        for m in ("debug", "review", "conversation"):
            assert list(writ_session._gate_sequence_for_mode(m)) == []
        assert list(writ_session._gate_sequence_for_mode(None)) == []

    def test_initial_phase_for_mode(self) -> None:
        assert writ_session._initial_phase_for_mode("work") == "planning"
        for m in ("debug", "review", "conversation", None):
            assert writ_session._initial_phase_for_mode(m) is None


class TestGoldenWorkCycle:
    """Behavior-preservation oracle: a full Work cycle is unchanged."""

    def _advance(self, session_id, project_root, monkeypatch, capsys, prompt="approved"):
        # A BOUND token (gate + plan fingerprint) derived from the seeded cache, as the
        # production mint derives it: cmd_advance_phase refuses an unbound one-line token.
        token = write_bound_gate_token(session_id, secrets.token_hex(16))
        capsys.readouterr()
        monkeypatch.setattr("sys.stdin", io.StringIO(prompt))
        writ_session.cmd_advance_phase(session_id, str(project_root), token)
        return json.loads(capsys.readouterr().out.strip())

    def test_full_work_cycle_transitions_and_artifacts(self, session_id, project_root, monkeypatch, capsys):
        writ_session.cmd_mode(session_id, "set", "work")
        writ_session.cmd_update(session_id, ["--add-rules", json.dumps(["TEST-CI-001"])])
        (project_root / "plan.md").write_text(PLAN_CONTENT)

        r1 = self._advance(session_id, project_root, monkeypatch, capsys)
        assert r1["advanced"] is True
        assert r1["gate"] == "phase-a"
        assert r1["phase"] == "testing"

        (project_root / "tests").mkdir()
        (project_root / "tests" / "test_service.py").write_text("def test_service():\n    pass\n")

        r2 = self._advance(session_id, project_root, monkeypatch, capsys)
        assert r2["advanced"] is True
        assert r2["gate"] == "test-skeletons"
        assert r2["phase"] == "implementation"

        cache = writ_session._read_cache(session_id)
        assert cache["gates_approved"] == ["phase-a", "test-skeletons"]
        # Session-scoped artifact path (Part 2, isolation cycle): the advance writes under
        # <gates>/<session_id>/, so two instances in one repo cannot read each other's
        # approvals. Only the path construction changed; both artifacts are still asserted.
        session_gates = project_root / ".claude" / "gates" / session_id
        assert (session_gates / "phase-a.approved").exists()
        assert (session_gates / "test-skeletons.approved").exists()

    def test_advance_returns_no_gates_for_debug(self, session_id, project_root, monkeypatch, capsys):
        """Non-work modes have no gate sequence -> advance is a no-op (unchanged)."""
        writ_session.cmd_mode(session_id, "set", "debug")
        result = self._advance(session_id, project_root, monkeypatch, capsys)
        assert result["advanced"] is False


class TestModeInitIdempotent:
    """Gate-reset bug (2026-06-29): the auto-classifier must route an UNSET
    session without ever wiping a live cycle. `mode init` sets the mode only if
    unset and is a NO-OP once set, so a re-fire on a transient empty cache read
    cannot reset current_phase / gates_approved. Explicit `mode set` still resets
    (the new-task contract -- see test_phase_machine_reset)."""

    def test_init_sets_mode_when_unset(self, session_id):
        writ_session.cmd_mode(session_id, "init", "work")
        after = writ_session._read_cache(session_id)
        assert after["mode"] == "work"
        assert after["current_phase"] == "planning"

    def test_init_is_noop_when_mode_already_set(self, session_id):
        writ_session.cmd_mode(session_id, "set", "work")
        cache = writ_session._read_cache(session_id)
        cache["current_phase"] = "implementation"
        cache["gates_approved"] = ["phase-a", "test-skeletons"]
        writ_session._write_cache(session_id, cache)

        # The spurious auto-classifier re-fire: must NOT reset a live cycle.
        writ_session.cmd_mode(session_id, "init", "work")

        after = writ_session._read_cache(session_id)
        assert after["mode"] == "work"
        assert after["current_phase"] == "implementation"
        assert after["gates_approved"] == ["phase-a", "test-skeletons"]

    def test_explicit_set_still_resets_even_when_same_mode(self, session_id):
        # The other half of the contract: an EXPLICIT `mode set work` is the
        # new-task signal and resets even from an in-progress same-mode cycle.
        writ_session.cmd_mode(session_id, "set", "work")
        cache = writ_session._read_cache(session_id)
        cache["current_phase"] = "implementation"
        cache["gates_approved"] = ["phase-a"]
        writ_session._write_cache(session_id, cache)

        writ_session.cmd_mode(session_id, "set", "work")

        after = writ_session._read_cache(session_id)
        assert after["current_phase"] == "planning"
        assert after["gates_approved"] == []
