"""`writ mode ...` forwards to the session helper; messages print it; mode set warns on a wrong id."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

# autouse: pins cwd to a sandbox so `mode set` cannot delete THIS repo's gate artifacts.
from tests.fixtures.session_state import sandbox_cwd  # noqa: F401

REPO = Path(__file__).resolve().parent.parent
SHIM = REPO / "bin" / "writ"
RAG = REPO / "hooks" / "scripts" / "writ-rag-inject.sh"
COMMON = REPO / "bin" / "lib" / "common.sh"


def _env(tmp_path: Path) -> dict:
    return {**os.environ, "WRIT_CACHE_DIR": str(tmp_path / "cache"),
            "WRIT_FRICTION_LOG": str(tmp_path / "friction.log"),
            "WRIT_LOG_ROOT": str(tmp_path / "logs")}


def _run(tmp_path: Path, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    work = cwd or (tmp_path / "proj")
    work.mkdir(parents=True, exist_ok=True)
    return subprocess.run([str(SHIM), *args], env=_env(tmp_path), cwd=str(work),
                          capture_output=True, text=True, timeout=30)


def _cache(tmp_path: Path, sid: str) -> dict:
    return json.loads((tmp_path / "cache" / f"writ-session-{sid}.json").read_text())


class TestForwarding:
    def test_set_writes_the_mode(self, tmp_path):
        r = _run(tmp_path, "mode", "set", "work", "wm-set")
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "set: work"
        assert _cache(tmp_path, "wm-set")["mode"] == "work"

    def test_get_reads_it_back(self, tmp_path):
        _run(tmp_path, "mode", "set", "debug", "wm-get")
        assert _run(tmp_path, "mode", "get", "wm-get").stdout.strip() == "debug"

    def test_the_orchestrator_flag_is_forwarded(self, tmp_path):
        _run(tmp_path, "mode", "set", "work", "wm-orch", "--orchestrator")
        assert _cache(tmp_path, "wm-orch")["is_orchestrator"] is True

    def test_init_is_forwarded(self, tmp_path):
        assert _run(tmp_path, "mode", "init", "investigate", "wm-init").stdout.strip() == "init: investigate"
        assert _cache(tmp_path, "wm-init")["mode_source"] == "auto"

    def test_the_mode_branch_runs_before_the_venv_check(self):
        text = SHIM.read_text()
        assert text.index('if [ "${1:-}" = "mode" ]') < text.index('writ_resolve_venv "$SKILL_DIR"')
        assert 'exec python3 "$SKILL_DIR/bin/lib/writ-session.py" "$@"' in text


class TestUnfamiliarSessionWarning:
    def test_set_on_a_session_with_no_cache_warns_with_the_path(self, tmp_path):
        r = _run(tmp_path, "mode", "set", "work", "wm-new")
        assert r.returncode == 0 and r.stdout.strip() == "set: work"
        assert "creating a NEW session cache" in r.stderr
        assert str(tmp_path / "cache" / "writ-session-wm-new.json") in r.stderr

    def test_switch_on_a_session_with_no_cache_warns(self, tmp_path):
        r = _run(tmp_path, "mode", "switch", "work", "wm-sw")
        assert r.returncode == 0 and "creating a NEW session cache" in r.stderr

    def test_init_never_warns(self, tmp_path):
        assert "warning" not in _run(tmp_path, "mode", "init", "work", "wm-i").stderr

    def test_an_existing_session_in_this_project_is_silent(self, tmp_path):
        _run(tmp_path, "mode", "set", "work", "wm-known")
        assert "warning" not in _run(tmp_path, "mode", "set", "debug", "wm-known").stderr

    def test_a_subdirectory_of_the_recorded_project_is_silent(self, tmp_path):
        _run(tmp_path, "mode", "set", "work", "wm-sub")
        sub = tmp_path / "proj" / "src"
        sub.mkdir(parents=True)
        assert "warning" not in _run(tmp_path, "mode", "set", "work", "wm-sub", cwd=sub).stderr

    def test_a_session_declared_in_another_project_warns(self, tmp_path):
        cache = tmp_path / "cache"
        cache.mkdir(parents=True)
        (cache / "writ-session-wm-foreign.json").write_text(
            json.dumps({"mode": "work", "project_root": "/elsewhere/project"}))
        r = _run(tmp_path, "mode", "set", "work", "wm-foreign")
        assert r.returncode == 0
        assert "was last declared in /elsewhere/project" in r.stderr

    def test_an_invalid_mode_claims_to_create_nothing(self, tmp_path):
        r = _run(tmp_path, "mode", "set", "bogus", "wm-bad")
        assert r.returncode == 1 and "creating" not in r.stderr


class TestMessagesNameThePathFreeCommand:
    def test_the_rag_hook_prints_writ_mode_set(self):
        text = RAG.read_text()
        assert "python3 $SESSION_HELPER mode set" not in text
        assert "writ mode set conversation $SESSION_ID" in text
        assert "writ mode set <conversation|debug|review|work|investigate> $SESSION_ID" in text

    def test_the_mode_directive_declares_with_writ_mode_set(self):
        assert ("Declare: writ mode set <conversation|debug|review|work|investigate> ${session_id}"
                in COMMON.read_text())

    @pytest.mark.parametrize("rel", [
        "hooks/scripts/auto-approve-gate.sh", "writ/session/approval_workflow.py",
        "writ/server/routes/gate.py", "writ/session/project_boundary.py",
        "rules/writ-orchestrator.md",
    ])
    def test_no_user_facing_text_names_the_helper_path(self, rel):
        text = (REPO / rel).read_text()
        assert "writ-session.py mode set" not in text, rel
        assert "writ mode set work" in text, rel

    def test_the_installer_allows_writ_mode(self):
        spec = importlib.util.spec_from_file_location("writ_install_wm", REPO / "bin" / "lib" / "writ_install.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert "Bash(writ mode *)" in module.BASE_ALLOW
