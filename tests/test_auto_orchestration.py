"""An auto-routed work session runs as an orchestrator and is told which workers to dispatch."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "scripts" / "writ-rag-inject.sh"
HELPER = REPO / "bin" / "lib" / "writ-session.py"
BUILD = "implement the export endpoint from the approved plan"
AUDIT = "audit the codebase for security issues"
WORKERS = ("writ-planner", "writ-test-writer", "writ-implementer", "writ-reviewer")


def _env(tmp_path: Path) -> dict:
    return {**os.environ, "WRIT_CACHE_DIR": str(tmp_path / "cache"), "WRIT_PORT": "59995",
            "WRIT_HOST": "localhost", "WRIT_FRICTION_LOG": str(tmp_path / "friction.log"),
            "WRIT_LOG_ROOT": str(tmp_path / "logs"), "WRIT_NO_AUTOSTART": "1"}


def _sandbox(tmp_path: Path) -> Path:
    sandbox = tmp_path / "sandbox"
    (sandbox / ".claude" / "gates").mkdir(parents=True, exist_ok=True)
    (sandbox / ".git").mkdir(exist_ok=True)
    return sandbox


def _helper(tmp_path, *args):
    subprocess.run([sys.executable, str(HELPER), *args], env=_env(tmp_path), check=True,
                   capture_output=True, text=True, cwd=str(_sandbox(tmp_path)))


def _hook(tmp_path, sid, prompt) -> subprocess.CompletedProcess:
    r = subprocess.run(["bash", str(HOOK)], input=json.dumps({"session_id": sid, "prompt": prompt}),
                       env=_env(tmp_path), capture_output=True, text=True, timeout=30,
                       cwd=str(_sandbox(tmp_path)))
    assert r.returncode == 0, r.stderr
    return r


def _cache(tmp_path, sid) -> dict:
    return json.loads((tmp_path / "cache" / f"writ-session-{sid}.json").read_text())


class TestTheHookOrchestratesWork:
    def test_a_first_route_into_work_marks_the_session_orchestrator(self, tmp_path):
        _hook(tmp_path, "ao-first", BUILD)
        cache = _cache(tmp_path, "ao-first")
        assert cache["mode"] == "work" and cache["is_orchestrator"] is True

    def test_an_investigate_route_does_not(self, tmp_path):
        _hook(tmp_path, "ao-inv", AUDIT)
        cache = _cache(tmp_path, "ao-inv")
        assert cache["mode"] == "investigate" and cache["is_orchestrator"] is False

    def test_a_switch_into_work_marks_the_session_orchestrator(self, tmp_path):
        _helper(tmp_path, "mode", "set", "investigate", "ao-switch")
        _hook(tmp_path, "ao-switch", BUILD)
        cache = _cache(tmp_path, "ao-switch")
        assert cache["mode"] == "work" and cache["is_orchestrator"] is True

    def test_the_announcement_names_the_four_workers_in_order(self, tmp_path):
        out = _hook(tmp_path, "ao-order", BUILD).stdout
        positions = [out.index(w) for w in WORKERS]
        assert positions == sorted(positions), positions
        assert "present them for approval" in out

    def test_the_override_is_the_path_free_command_with_the_real_id(self, tmp_path):
        out = _hook(tmp_path, "ao-override", BUILD).stdout
        assert "writ mode set conversation ao-override" in out

    def test_the_restore_announcement_names_the_next_workers(self, tmp_path):
        sid = "ao-restore"
        sandbox = _sandbox(tmp_path)
        (sandbox / "plan.md").write_text("# Plan: unchanged across the detour\n")
        _helper(tmp_path, "mode", "set", "work", sid)
        path = tmp_path / "cache" / f"writ-session-{sid}.json"
        data = json.loads(path.read_text())
        data["gates_approved"] = ["phase-a"]
        path.write_text(json.dumps(data))
        _hook(tmp_path, sid, AUDIT)
        out = _hook(tmp_path, sid, BUILD).stdout
        assert "paused work mode restored automatically" in out
        assert "writ-test-writer" in out and "writ-implementer" in out and "writ-reviewer" in out
        assert "present them for approval" not in out
        assert "writ mode set conversation ao-restore" in out


class TestTheModeEngineHonoursTheFlag:
    @pytest.fixture(autouse=True)
    def _iso(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
        monkeypatch.chdir(_sandbox(tmp_path))

    def test_switch_with_the_flag_sets_it(self):
        from writ.session import mode_engine
        from writ.session.cache import _read_cache
        mode_engine._mode_set("me-1", "investigate")
        mode_engine._mode_switch("me-1", "work", is_orchestrator=True)
        assert _read_cache("me-1")["is_orchestrator"] is True

    @pytest.mark.parametrize("before", [True, False])
    def test_switch_without_the_flag_leaves_it_alone(self, before):
        from writ.session import mode_engine
        from writ.session.cache import _read_cache
        mode_engine._mode_set("me-2", "investigate", is_orchestrator=before)
        mode_engine._mode_switch("me-2", "work")
        assert _read_cache("me-2")["is_orchestrator"] is before

    def test_an_init_that_declines_does_not_touch_it(self):
        from writ.session import mode_engine
        from writ.session.cache import _read_cache
        mode_engine._mode_set("me-3", "work")
        mode_engine._mode_init("me-3", "work", is_orchestrator=True)
        assert _read_cache("me-3")["is_orchestrator"] is False

    def test_the_cli_forwards_the_flag_for_switch(self, tmp_path):
        from writ.session.cache import _read_cache
        _helper(tmp_path, "mode", "set", "investigate", "me-4")
        _helper(tmp_path, "mode", "switch", "work", "me-4", "--orchestrator")
        assert _read_cache("me-4")["is_orchestrator"] is True
