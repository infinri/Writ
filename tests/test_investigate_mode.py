"""INV-1: the unified 'investigate' mode + source_type/gate_strictness lens.

Audit, explore, research (and debug) are one evidence-grounded investigation
process. INV-1 establishes the engine seam: ONE new MODE_CONFIG entry plus two
config fields (source_type, gate_strictness) and their accessors -- the same
move that added Debug as a config of one engine. No new branches; the lenses
(code/web/runtime) and gate decisions arrive in later increments.

Loads writ-session.py as a module (mirrors tests/test_mode_engine.py).
"""
from __future__ import annotations

import importlib.util
import io
import json
import os

import pytest

# autouse: pins cwd to a sandbox so `mode set` cannot delete THIS repo's gate artifacts.
from tests.fixtures.session_state import sandbox_cwd  # noqa: F401

HELPER_PATH = os.path.join(os.path.dirname(__file__), os.pardir, "bin", "lib", "writ-session.py")
_spec = importlib.util.spec_from_file_location("writ_session_investigate", HELPER_PATH)
writ_session = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(writ_session)

SID = "test-investigate-mode"

EXPECTED_WORK_GATE_SEQUENCE = ["phase-a", "test-skeletons"]


@pytest.fixture()
def session_id(tmp_path, monkeypatch):
    monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
    return SID


class TestInvestigateConfig:
    def test_investigate_entry_exists_with_fields(self) -> None:
        cfg = writ_session.MODE_CONFIG.get("investigate")
        assert cfg is not None, "MODE_CONFIG must have an 'investigate' entry"
        assert cfg.get("initial_phase") is None
        assert list(cfg.get("gate_sequence", ["x"])) == []
        assert dict(cfg.get("phase_after_gate", {"x": 1})) == {}
        assert "source_type" in cfg, "investigate config must carry a source_type field"
        assert "gate_strictness" in cfg, "investigate config must carry a gate_strictness field"

    def test_valid_modes_includes_investigate(self) -> None:
        assert "investigate" in writ_session.VALID_MODES
        assert isinstance(writ_session.VALID_MODES, set)

    def test_investigate_is_no_gate(self) -> None:
        assert list(writ_session._gate_sequence_for_mode("investigate")) == []
        assert writ_session._initial_phase_for_mode("investigate") is None


class TestLensAccessors:
    def test_accessors_exist(self) -> None:
        assert hasattr(writ_session, "_source_type_for_mode") and callable(writ_session._source_type_for_mode)
        assert hasattr(writ_session, "_gate_strictness_for_mode") and callable(writ_session._gate_strictness_for_mode)

    def test_gate_strictness_reads_config_with_safe_default(self) -> None:
        assert hasattr(writ_session, "_gate_strictness_for_mode"), "accessor not defined yet"
        # investigate's configured default is advisory (research overrides to hard later, INV-7)
        assert writ_session._gate_strictness_for_mode("investigate") == "advisory"
        # modes without the field get the safe default, not a crash
        assert writ_session._gate_strictness_for_mode("work") == "advisory"

    def test_source_type_reads_config_with_safe_default(self) -> None:
        assert hasattr(writ_session, "_source_type_for_mode"), "accessor not defined yet"
        # accessor returns whatever MODE_CONFIG holds for investigate (lens set later)
        assert writ_session._source_type_for_mode("investigate") == writ_session.MODE_CONFIG["investigate"].get("source_type")
        # modes without the field -> None, no crash
        assert writ_session._source_type_for_mode("work") is None


class TestModeSetRoundTrip:
    def test_set_investigate_round_trips(self, session_id, capsys) -> None:
        writ_session.cmd_mode(session_id, "set", "investigate")
        capsys.readouterr()
        writ_session.cmd_mode(session_id, "get")
        assert capsys.readouterr().out.strip() == "investigate"


class TestRegressionExistingModes:
    """Work/debug/review/conversation config is byte-unchanged."""

    def test_work_gate_sequence_unchanged(self) -> None:
        assert list(writ_session._gate_sequence_for_mode("work")) == EXPECTED_WORK_GATE_SEQUENCE
        assert writ_session._initial_phase_for_mode("work") == "planning"

    def test_nonwork_modes_unchanged(self) -> None:
        for m in ("debug", "review", "conversation"):
            assert list(writ_session._gate_sequence_for_mode(m)) == []
            assert writ_session._initial_phase_for_mode(m) is None
