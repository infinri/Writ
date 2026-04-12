"""Tests for writ-read-rag.sh PreToolUse hook behavior.

Since the hook is a shell script that queries an HTTP endpoint, these tests
verify the mode-filtering logic by testing the session helper commands the
hook depends on. The hook's mode check (`mode get` -> skip if not review/debug)
is the critical behavior to verify.

Per TEST-TDD-001: skeletons approved before implementation.
"""

from __future__ import annotations

import importlib.util
import json
import os

import pytest

HELPER_PATH = os.path.join(
    os.path.dirname(__file__), os.pardir, "bin", "lib", "writ-session.py"
)

spec = importlib.util.spec_from_file_location("writ_session", HELPER_PATH)
writ_session = importlib.util.module_from_spec(spec)
spec.loader.exec_module(writ_session)


@pytest.fixture()
def session_id(tmp_path, monkeypatch):
    monkeypatch.setattr(writ_session, "CACHE_DIR", str(tmp_path))
    return "test-read-rag"


class TestReadRagModeFilter:
    """The hook should only fire in review and debug modes."""

    def test_review_mode_should_fire(self, session_id, capsys):
        """Review mode: hook should proceed (mode get returns 'review')."""
        writ_session.cmd_mode(session_id, "set", "review")
        capsys.readouterr()
        writ_session.cmd_mode(session_id, "get")
        out = capsys.readouterr().out.strip()
        assert out == "review"

    def test_debug_mode_should_fire(self, session_id, capsys):
        """Debug mode: hook should proceed (mode get returns 'debug')."""
        writ_session.cmd_mode(session_id, "set", "debug")
        capsys.readouterr()
        writ_session.cmd_mode(session_id, "get")
        out = capsys.readouterr().out.strip()
        assert out == "debug"

    def test_conversation_mode_should_skip(self, session_id, capsys):
        """Conversation mode: hook should exit early."""
        writ_session.cmd_mode(session_id, "set", "conversation")
        capsys.readouterr()
        writ_session.cmd_mode(session_id, "get")
        out = capsys.readouterr().out.strip()
        assert out == "conversation"
        assert out not in ("review", "debug")

    def test_work_mode_should_skip(self, session_id, capsys):
        """Work mode: hook should exit early (pretool-rag handles writes)."""
        writ_session.cmd_mode(session_id, "set", "work")
        capsys.readouterr()
        writ_session.cmd_mode(session_id, "get")
        out = capsys.readouterr().out.strip()
        assert out == "work"
        assert out not in ("review", "debug")

    def test_no_mode_should_skip(self, session_id, capsys):
        """No mode set: hook should exit early."""
        writ_session.cmd_mode(session_id, "get")
        out = capsys.readouterr().out.strip()
        assert out == ""


class TestReadRagBudgetRespect:
    """The hook should respect budget and context pressure limits."""

    def test_budget_exhausted_should_skip(self, session_id):
        """When remaining budget is 0, should-skip returns exit 0 (skip)."""
        writ_session.cmd_mode(session_id, "set", "review")
        writ_session.cmd_update(session_id, ["--cost", "8000"])
        # should-skip exits 0 when budget exhausted (meaning: skip)
        with pytest.raises(SystemExit, match="0"):
            writ_session.cmd_should_skip(session_id)

    def test_context_pressure_high_should_skip(self, session_id):
        """When context pressure > 75%, should-skip returns exit 0 (skip)."""
        writ_session.cmd_mode(session_id, "set", "review")
        writ_session.cmd_update(session_id, ["--context-percent", "80"])
        with pytest.raises(SystemExit, match="0"):
            writ_session.cmd_should_skip(session_id)

    def test_budget_available_should_proceed(self, session_id):
        """When budget is available, should-skip returns exit 1 (proceed)."""
        writ_session.cmd_mode(session_id, "set", "review")
        with pytest.raises(SystemExit, match="1"):
            writ_session.cmd_should_skip(session_id)


class TestReadRagHookSyntax:
    """Verify the hook file exists and has valid shell syntax."""

    def test_hook_file_exists(self):
        hook_path = os.path.join(
            os.path.dirname(__file__), os.pardir,
            ".claude", "hooks", "writ-read-rag.sh"
        )
        assert os.path.exists(hook_path), f"Hook file not found: {hook_path}"

    def test_hook_has_valid_shell_syntax(self):
        import subprocess
        hook_path = os.path.join(
            os.path.dirname(__file__), os.pardir,
            ".claude", "hooks", "writ-read-rag.sh"
        )
        result = subprocess.run(
            ["bash", "-n", hook_path],
            capture_output=True, text=True
        )
        assert result.returncode == 0, f"Shell syntax error: {result.stderr}"
