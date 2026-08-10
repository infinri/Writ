"""POL-6d: locators.py (file-locators) + mode_engine.py extraction.

A low-level writ/session/locators.py (the 3 pure file-locators) breaks the would-be
mode_engine<->gates cycle. writ/session/mode_engine.py holds MODE_CONFIG/VALID_MODES, the
resolvers, the debug->work handoff, and _mode_set/_mode_switch/cmd_mode -- importing only
cache/friction/locators (no facade). The facade re-exports both surfaces, so the gate /
approval / investigation callers still resolve the names unchanged.

Per TEST-TDD-001: skeletons approved before implementation. RED until the move lands.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import subprocess
import sys

import pytest

# autouse: pins cwd to a sandbox so `mode set` cannot delete THIS repo's gate artifacts.
from tests.fixtures.session_state import sandbox_cwd  # noqa: F401

SKILL_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
FACADE_PATH = os.path.join(SKILL_ROOT, "bin", "lib", "writ-session.py")
LOCATORS_PATH = os.path.join(SKILL_ROOT, "writ", "session", "locators.py")
MODE_ENGINE_PATH = os.path.join(SKILL_ROOT, "writ", "session", "mode_engine.py")


def _load_facade():
    spec = importlib.util.spec_from_file_location("writ_session_pol6d", FACADE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _imp(name):
    if SKILL_ROOT not in sys.path:
        sys.path.insert(0, SKILL_ROOT)
    return importlib.import_module(name)


class TestModulesExist:
    def test_locators_file_exists(self):
        assert os.path.isfile(LOCATORS_PATH)

    def test_mode_engine_file_exists(self):
        assert os.path.isfile(MODE_ENGINE_PATH)

    def test_locators_imports(self):
        assert _imp("writ.session.locators") is not None

    def test_mode_engine_imports(self):
        assert _imp("writ.session.mode_engine") is not None


class TestLocators:
    """The three file-locators are pure path walks."""

    def test_find_plan_md_at_root(self, tmp_path):
        loc = _imp("writ.session.locators")
        (tmp_path / "plan.md").write_text("# plan\n")
        assert loc._find_plan_md(str(tmp_path)) == str(tmp_path / "plan.md")

    def test_find_debug_md_walks_to_marker(self, tmp_path):
        loc = _imp("writ.session.locators")
        (tmp_path / ".git").mkdir()
        (tmp_path / "debug.md").write_text("# debug\n")
        sub = tmp_path / "a" / "b"
        sub.mkdir(parents=True)
        assert loc._find_debug_md(str(sub / "x.py")) == str(tmp_path / "debug.md")

    def test_find_debug_md_none_without_marker(self, tmp_path):
        loc = _imp("writ.session.locators")
        assert loc._find_debug_md(str(tmp_path / "deep" / "x.py")) is None


class TestModeEngineNoFacadeImport:
    """mode_engine must not import the facade (acyclic dependency graph)."""

    def test_mode_engine_does_not_import_facade(self):
        with open(MODE_ENGINE_PATH) as f:
            lines = f.read().splitlines()
        # Only import statements matter -- the docstring may name the source file.
        import_lines = "\n".join(l for l in lines if l.strip().startswith(("import ", "from ")))
        assert "writ_session" not in import_lines
        assert "writ-session" not in import_lines
        assert "spec_from_file_location" not in "\n".join(lines)

    def test_mode_engine_imports_lower_layers(self):
        with open(MODE_ENGINE_PATH) as f:
            src = f.read()
        assert "from writ.session.cache import" in src
        assert "from writ.session.friction import" in src
        assert "from writ.session.locators import" in src


class TestResolvers:
    def test_initial_phase_for_work_is_planning(self):
        me = _imp("writ.session.mode_engine")
        assert me._initial_phase_for_mode("work") == "planning"
        assert me._initial_phase_for_mode("review") is None

    def test_gate_sequence_for_work(self):
        me = _imp("writ.session.mode_engine")
        assert me._gate_sequence_for_mode("work") == ["phase-a", "test-skeletons"]
        assert me._gate_sequence_for_mode("conversation") == []

    def test_next_pending_gate(self):
        me = _imp("writ.session.mode_engine")
        assert me._next_pending_gate({"mode": "work", "gates_approved": []}) == "phase-a"
        assert me._next_pending_gate({"mode": "work", "gates_approved": ["phase-a"]}) == "test-skeletons"
        assert me._next_pending_gate({"mode": "work", "gates_approved": ["phase-a", "test-skeletons"]}) is None
        assert me._next_pending_gate({"mode": "review"}) is None

    def test_valid_modes_membership(self):
        me = _imp("writ.session.mode_engine")
        assert {"work", "debug", "review", "conversation", "investigate"} <= me.VALID_MODES


class TestFacadeReExports:
    """The facade re-exports the moved surface so existing callers resolve unchanged."""

    @pytest.mark.parametrize("name", [
        "MODE_CONFIG", "VALID_MODES", "GATE_SEQUENCE_WORK", "_initial_phase_for_mode",
        "_gate_sequence_for_mode", "_source_type_for_mode", "_gate_strictness_for_mode",
        "_next_pending_gate", "_extract_root_cause", "_promote_root_cause_to_plan",
        "_mode_set", "_mode_switch", "cmd_mode",
        "_find_debug_md", "_find_plan_md",
    ])
    def test_facade_exposes(self, name):
        assert hasattr(_load_facade(), name)


class TestModeBehaviorViaFacade:
    """End-to-end mode behavior through the facade, on an isolated cache dir."""

    def test_set_work_lands_in_planning_with_fresh_gates(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
        facade = _load_facade()
        facade._mode_set("sid-w", "work")
        c = facade._read_cache("sid-w")
        assert c["mode"] == "work"
        assert c["current_phase"] == "planning"
        assert c["gates_approved"] == []

    def test_switch_saves_and_restores_work_state(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
        facade = _load_facade()
        facade._mode_set("sid-s", "work")
        c = facade._read_cache("sid-s")
        c["current_phase"] = "implementation"
        c["gates_approved"] = ["phase-a", "test-skeletons"]
        facade._write_cache("sid-s", c)
        facade._mode_switch("sid-s", "review")  # leave work -> paused
        assert facade._read_cache("sid-s")["paused_work_state"] is not None
        facade._mode_switch("sid-s", "work")    # return -> restore
        back = facade._read_cache("sid-s")
        assert back["current_phase"] == "implementation"
        assert back["gates_approved"] == ["phase-a", "test-skeletons"]

    def test_cmd_mode_invalid_exits_nonzero(self, tmp_path):
        env = dict(os.environ, WRIT_CACHE_DIR=str(tmp_path))
        r = subprocess.run(
            [sys.executable, FACADE_PATH, "mode", "set", "bogus", "sid-x"],
            capture_output=True, text=True, env=env,
        )
        assert r.returncode != 0

    def test_debug_to_work_promotes_root_cause(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".git").mkdir()
        (tmp_path / "debug.md").write_text("## Root cause\nThe widget was null.\n")
        facade = _load_facade()
        facade._mode_set("sid-d", "debug")
        facade._mode_set("sid-d", "work")  # debug -> work handoff
        plan = (tmp_path / "plan.md")
        assert plan.is_file()
        assert "Root Cause Evidence" in plan.read_text()
        assert "The widget was null." in plan.read_text()


class TestSourceShape:
    def test_facade_no_inline_mode_engine_defs(self):
        with open(FACADE_PATH) as f:
            src = f.read()
        assert "def cmd_mode(" not in src
        assert "MODE_CONFIG: dict[str, dict] = {" not in src
        assert "def _find_debug_md(" not in src
        assert "from writ.session.mode_engine import" in src
        assert "from writ.session.locators import" in src

    def test_mode_engine_defines_cmd_mode_and_config(self):
        with open(MODE_ENGINE_PATH) as f:
            src = f.read()
        assert "def cmd_mode(" in src
        assert "MODE_CONFIG" in src

    def test_locators_defines_find_debug_md(self):
        with open(LOCATORS_PATH) as f:
            src = f.read()
        assert "def _find_debug_md(" in src
