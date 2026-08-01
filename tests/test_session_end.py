"""Tests for the SessionEnd hook and Stop hook simplification (Cycle B, Item 7).

Per TEST-TDD-001: skeletons approved before implementation.
Covers: writ-session-end.sh behavior, writ-context-tracker.sh simplification,
log-session-metrics.sh removal, and settings.json registration.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from typing import Any

import pytest
from pathlib import Path

SKILL_DIR = str(Path(__file__).resolve().parent.parent)
SESSION_END_HOOK = f"{SKILL_DIR}/hooks/scripts/writ-session-end.sh"
HOOKS_JSON = Path(__file__).resolve().parent.parent / "hooks" / "hooks.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_session_cache(
    mode: str = "work",
    files_written: list[str] | None = None,
    pending_violations: list[dict[str, Any]] | None = None,
    loaded_rule_ids: list[str] | None = None,
    gates_approved: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "session_id": "test-session-end",
        "mode": mode,
        "current_phase": "implementation",
        "remaining_budget": 4000,
        "context_percent": 55,
        "loaded_rule_ids": loaded_rule_ids or ["ARCH-ORG-001", "PY-IMPORT-001"],
        "loaded_rules": [],
        "loaded_rule_ids_by_phase": {},
        "queries": 8,
        "pending_violations": pending_violations or [],
        "escalation": {"needed": False},
        "invalidation_history": {},
        "failed_writes": [],
        "files_written": files_written or ["writ/server.py", "tests/test_foo.py"],
        "gates_approved": gates_approved or ["phase-a", "test-skeletons"],
        "analysis_results": {},
        "feedback_sent": [],
    }


def _run_hook(
    hook_path: str,
    cache: dict[str, Any],
    env_overrides: dict[str, str] | None = None,
    stdin_payload: dict[str, Any] | None = None,
    timeout: int = 10,
) -> subprocess.CompletedProcess[str]:
    """Write a session cache to a temp dir, then invoke the hook via subprocess."""
    session_id = cache["session_id"]
    with tempfile.TemporaryDirectory() as tmpdir:
        cache_path = os.path.join(tmpdir, f"writ-session-{session_id}.json")
        with open(cache_path, "w") as f:
            json.dump(cache, f)

        env = os.environ.copy()
        env["WRIT_SESSION_ID"] = session_id
        env["WRIT_CACHE_DIR"] = tmpdir
        env["WRIT_PORT"] = "19999"  # nothing listening; ensures local-only behavior
        if env_overrides:
            env.update(env_overrides)

        stdin_data = json.dumps(stdin_payload or {})
        result = subprocess.run(
            ["bash", hook_path],
            input=stdin_data,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )
    return result


# ---------------------------------------------------------------------------
# TestSessionEndHookBehavior -- writ-session-end.sh
# ---------------------------------------------------------------------------


class TestSessionEndHookBehavior:
    """writ-session-end.sh calls auto-feedback, coverage, and logs session_end rollup."""

    def test_session_end_hook_file_exists(self) -> None:
        """writ-session-end.sh exists at the expected path."""
        assert os.path.exists(SESSION_END_HOOK), (
            f"writ-session-end.sh must exist at {SESSION_END_HOOK}"
        )

    def test_session_end_hook_sources_common_sh(self) -> None:
        """writ-session-end.sh sources common.sh for _writ_session helper."""
        with open(SESSION_END_HOOK) as f:
            source = f.read()
        assert "common.sh" in source, (
            "writ-session-end.sh must source common.sh"
        )

    def test_session_end_hook_calls_auto_feedback(self) -> None:
        """writ-session-end.sh invokes the auto-feedback subcommand."""
        with open(SESSION_END_HOOK) as f:
            source = f.read()
        assert "auto-feedback" in source, (
            "writ-session-end.sh must call auto-feedback"
        )

    def test_session_end_hook_calls_coverage(self) -> None:
        """writ-session-end.sh invokes the coverage subcommand."""
        with open(SESSION_END_HOOK) as f:
            source = f.read()
        assert "coverage" in source, (
            "writ-session-end.sh must call coverage"
        )

    def test_session_end_hook_logs_session_end_event(self) -> None:
        """writ-session-end.sh logs a session_end event (to friction log or via _writ_session)."""
        with open(SESSION_END_HOOK) as f:
            source = f.read()
        assert "session_end" in source, (
            "writ-session-end.sh must log a session_end rollup event"
        )

    def test_session_end_hook_exits_0(self) -> None:
        """writ-session-end.sh exits 0 under normal conditions."""
        cache = _build_session_cache()
        result = _run_hook(SESSION_END_HOOK, cache)
        assert result.returncode == 0, (
            f"writ-session-end.sh must exit 0. stderr: {result.stderr}"
        )

    def test_session_end_hook_completes_within_timeout(self) -> None:
        """writ-session-end.sh completes within 1.5s (SessionEnd hook timeout)."""
        cache = _build_session_cache()
        start = time.monotonic()
        try:
            result = _run_hook(SESSION_END_HOOK, cache, timeout=3)
        except subprocess.TimeoutExpired:
            pytest.fail("writ-session-end.sh exceeded 3s process timeout")
        elapsed = time.monotonic() - start
        assert elapsed < 1.5, (
            f"writ-session-end.sh took {elapsed:.2f}s -- must complete within 1.5s"
        )


# ---------------------------------------------------------------------------
# TestLogSessionMetricsRemoval -- log-session-metrics.sh deleted
# ---------------------------------------------------------------------------


class TestLogSessionMetricsRemoval:
    """log-session-metrics.sh must be removed from Stop hooks in hooks.json."""

    def _load_settings(self) -> dict[str, Any]:
        with open(HOOKS_JSON) as f:
            return json.load(f)

    def test_log_session_metrics_not_in_stop_hooks(self) -> None:
        """hooks.json Stop event must not include log-session-metrics.sh."""
        settings = self._load_settings()
        hooks = settings.get("hooks", {})
        stop_hooks = hooks.get("Stop", [])
        hook_commands: list[str] = []
        for entry in stop_hooks:
            if isinstance(entry, dict):
                if "command" in entry:
                    hook_commands.append(entry["command"])
                for hook in entry.get("hooks", []):
                    if isinstance(hook, dict):
                        hook_commands.append(hook.get("command", ""))
                    elif isinstance(hook, str):
                        hook_commands.append(hook)
            elif isinstance(entry, str):
                hook_commands.append(entry)
        assert not any("log-session-metrics" in cmd for cmd in hook_commands), (
            "log-session-metrics.sh must be removed from Stop hooks in settings.json"
        )

    def test_log_session_metrics_bash_permission_removed(self) -> None:
        """settings.json Bash permission for log-session-metrics.sh must be removed."""
        home = os.path.expanduser("~")
        settings_path = os.path.join(home, ".claude", "settings.json")
        if not os.path.exists(settings_path):
            pytest.skip(
                "asserts on the HOST's installed ~/.claude/settings.json; "
                "absent on machines (CI) where Writ's global config was never patched"
            )
        with open(settings_path) as f:
            settings = json.load(f)
        permissions = settings.get("permissions", {})
        allowed_tools = permissions.get("allow", [])
        bash_allows = [t for t in allowed_tools if isinstance(t, str) and "log-session-metrics" in t]
        assert len(bash_allows) == 0, (
            "Bash permission for log-session-metrics.sh must be removed from settings.json"
        )


# ---------------------------------------------------------------------------
# TestSessionEndRegistration -- settings.json
# ---------------------------------------------------------------------------


class TestSessionEndRegistration:
    """writ-session-end.sh must be registered under SessionEnd in hooks.json."""

    def _load_settings(self) -> dict[str, Any]:
        with open(HOOKS_JSON) as f:
            return json.load(f)

    def test_session_end_hook_registered_in_settings(self) -> None:
        """hooks.json SessionEnd event includes writ-session-end.sh."""
        settings = self._load_settings()
        hooks = settings.get("hooks", {})
        session_end_hooks = hooks.get("SessionEnd", [])
        hook_commands: list[str] = []
        for entry in session_end_hooks:
            if isinstance(entry, dict):
                if "command" in entry:
                    hook_commands.append(entry["command"])
                for hook in entry.get("hooks", []):
                    if isinstance(hook, dict):
                        hook_commands.append(hook.get("command", ""))
                    elif isinstance(hook, str):
                        hook_commands.append(hook)
            elif isinstance(entry, str):
                hook_commands.append(entry)
        assert any("writ-session-end.sh" in cmd for cmd in hook_commands), (
            "settings.json SessionEnd must include writ-session-end.sh"
        )
