"""The durable state root: one definition per language, and both give the same answer.

Session state used to live under the install directory, and a plugin install path carries the
version, so every upgrade orphaned every live session's mode and approvals. These pin the new
contract: $XDG_STATE_HOME/writ (absolute values only), else ~/.local/state/writ, never the install
dir and never the temp dir, with WRIT_CACHE_DIR still winning for the session dir. Every path
here is RESOLVED; only the last class writes, and only inside tmp_path.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from writ.shared import state_root as sr

# autouse: pins cwd to a sandbox so `mode set` cannot delete THIS repo's gate artifacts.
from tests.fixtures.session_state import sandbox_cwd  # noqa: F401

REPO = Path(__file__).resolve().parent.parent
COMMON = REPO / "bin" / "lib" / "common.sh"
HELPER = REPO / "bin" / "lib" / "writ-session.py"
MARK_HOOK = REPO / "hooks" / "scripts" / "writ-mark-pending-test.sh"


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _env(home: str, xdg: str | None, cache: str | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("WRIT_CACHE_DIR", "XDG_STATE_HOME")}
    env["HOME"] = home
    if xdg is not None:
        env["XDG_STATE_HOME"] = xdg
    if cache is not None:
        env["WRIT_CACHE_DIR"] = cache
    return env


def _bash(snippet: str, env: dict) -> str:
    r = subprocess.run(["bash", "-c", f'set -euo pipefail; source "{COMMON}"; {snippet}'],
                       capture_output=True, text=True, timeout=20, env=env)
    assert r.returncode == 0, r.stderr
    return r.stdout


@pytest.fixture()
def clean_env(monkeypatch, tmp_path):
    monkeypatch.delenv("WRIT_CACHE_DIR", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return tmp_path


class TestPythonResolution:
    def test_an_absolute_xdg_state_home_is_used(self, clean_env, monkeypatch):
        monkeypatch.setenv("XDG_STATE_HOME", str(clean_env / "xdg"))
        assert sr.state_root() == str(clean_env / "xdg" / "writ")

    def test_unset_xdg_falls_back_to_home_local_state(self, clean_env):
        assert sr.state_root() == str(clean_env / "home" / ".local" / "state" / "writ")

    def test_a_relative_xdg_is_ignored_as_the_spec_requires(self, clean_env, monkeypatch):
        monkeypatch.setenv("XDG_STATE_HOME", "relative/state")
        assert sr.state_root() == str(clean_env / "home" / ".local" / "state" / "writ")

    def test_session_dir_defaults_under_the_state_root(self, clean_env):
        assert sr.session_dir() == os.path.join(sr.state_root(), "session")

    def test_writ_cache_dir_still_wins(self, clean_env, monkeypatch):
        monkeypatch.setenv("WRIT_CACHE_DIR", "/some/where")
        assert sr.session_dir() == "/some/where"

    def test_an_empty_writ_cache_dir_counts_as_unset(self, clean_env, monkeypatch):
        monkeypatch.setenv("WRIT_CACHE_DIR", "")
        assert sr.session_dir() == os.path.join(sr.state_root(), "session")

    def test_the_default_log_root_is_under_the_state_root(self, clean_env):
        assert sr.default_log_root() == os.path.join(sr.state_root(), "logs")

    def test_the_default_is_neither_the_install_dir_nor_the_temp_dir(self, monkeypatch):
        monkeypatch.delenv("WRIT_CACHE_DIR", raising=False)
        resolved = Path(sr.session_dir()).resolve()
        assert REPO.resolve() not in resolved.parents, resolved
        tmp = Path(tempfile.gettempdir()).resolve()
        assert tmp not in resolved.parents and resolved != tmp, resolved


class TestBashAgreesWithPython:
    @pytest.mark.parametrize("xdg", [None, "", "relative/state", "ABS", "ABS/"])
    def test_same_answer_under_the_same_environment(self, tmp_path, monkeypatch, xdg):
        home = str(tmp_path / "home")
        value = xdg.replace("ABS", str(tmp_path / "xdg")) if xdg and xdg.startswith("ABS") else xdg
        monkeypatch.delenv("WRIT_CACHE_DIR", raising=False)
        monkeypatch.setenv("HOME", home)
        if value is None:
            monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        else:
            monkeypatch.setenv("XDG_STATE_HOME", value)
        assert _bash("writ_session_cache_dir", _env(home, value)) == sr.session_dir()

    def test_same_answer_when_home_is_unset(self, monkeypatch):
        """Minimal containers and some systemd units export no HOME. Both sides must then
        fall back to the password database, or hooks and the daemon split onto two trees."""
        for var in ("WRIT_CACHE_DIR", "XDG_STATE_HOME", "HOME"):
            monkeypatch.delenv(var, raising=False)
        env = {k: v for k, v in os.environ.items()}
        assert _bash("writ_session_cache_dir", env) == sr.session_dir()
        assert not sr.session_dir().startswith("/.local/")
        socket = _bash('printf %s "$WRIT_SESSION_SOCKET"', env)
        assert socket == os.path.join(os.path.expanduser("~"), ".cache/writ/run/writ.sock")

    def test_the_override_agrees(self, tmp_path):
        assert _bash("writ_session_cache_dir", _env(str(tmp_path), None, cache="/over/ride")) == "/over/ride"

    def test_the_bash_root_costs_no_subshell(self):
        """common.sh is sourced by every hook, so the root must be parameter expansion."""
        body = COMMON.read_text()
        start = body.index('case "${XDG_STATE_HOME:-}" in')
        block = body[start:body.index("esac", start)]
        assert "$(" not in block and "`" not in block, block


class TestEveryReaderUsesTheOneResolver:
    def test_the_cache_module(self, clean_env):
        from writ.session import cache
        assert cache._cache_dir() == sr.session_dir()
        assert cache._default_cache_dir() == os.path.join(sr.state_root(), "session")

    def test_the_manual_test_grant(self, clean_env):
        assert _load("mtg_sr", "bin/lib/manual_test_grant.py").cache_dir() == sr.session_dir()

    def test_the_pointer_session_helper(self, clean_env):
        assert _load("ps_sr", "bin/lib/pointer_session.py")._cache_dir() == sr.session_dir()

    def test_the_event_drain_reads_where_bash_appends(self, tmp_path, monkeypatch):
        home, xdg = str(tmp_path / "home"), str(tmp_path / "xdg")
        monkeypatch.delenv("WRIT_CACHE_DIR", raising=False)
        monkeypatch.setenv("HOME", home)
        monkeypatch.setenv("XDG_STATE_HOME", xdg)
        drain = _load("flush_sr", "bin/lib/writ-flush-events.py")._buffer_path("s1")
        appender = _bash('writ_event_buffer_path "s1"', _env(home, xdg))
        assert drain == appender == os.path.join(xdg, "writ", "session", "writ-events-s1.buf")

    def test_the_log_root(self, clean_env, monkeypatch):
        monkeypatch.delenv("WRIT_LOG_ROOT", raising=False)
        from writ.shared.logging import log_root
        assert str(log_root()) == sr.default_log_root()


class TestScratchFilesFollowTheStateRoot:
    @pytest.mark.parametrize("hook", [
        "writ-mark-pending-test.sh", "writ-run-pending-tests.sh", "validate-file.sh",
    ])
    def test_the_default_is_the_state_root_cache(self, hook):
        text = (REPO / "hooks" / "scripts" / hook).read_text()
        assert "${WRIT_CACHE_DIR:-$_WRIT_STATE_ROOT/cache}" in text, hook

    def test_the_pending_test_marker_lands_under_the_state_root(self, tmp_path):
        env = _env(str(tmp_path / "home"), str(tmp_path / "xdg"))
        env["WRIT_FRICTION_LOG"] = str(tmp_path / "friction.log")
        env["WRIT_LOG_ROOT"] = str(tmp_path / "logs")
        sid = "sr-marker"
        subprocess.run([sys.executable, str(HELPER), "mode", "set", "work", sid],
                       env=env, check=True, capture_output=True, text=True, cwd=str(tmp_path))
        envelope = {"session_id": sid, "hook_event_name": "PostToolUse", "tool_name": "Write",
                    "tool_input": {"file_path": "/proj/src/widget.py"}}
        r = subprocess.run(["bash", str(MARK_HOOK)], input=json.dumps(envelope), env=env,
                           capture_output=True, text=True, timeout=30, cwd=str(tmp_path))
        assert r.returncode == 0, r.stderr
        marker = tmp_path / "xdg" / "writ" / "cache" / sid / "pending-tests.txt"
        assert marker.is_file(), r.stderr
        assert not (REPO / "cache" / sid).exists()
