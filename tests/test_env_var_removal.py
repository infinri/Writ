"""Regression tests: the fabricated env vars CLAUDE_CONTEXT_PERCENT/TOKENS
must not be referenced by Writ hooks. These env vars don't exist in Claude
Code v2.1.109 and always return 0. The authoritative compaction mechanism
is writ-postcompact.sh on the real PostCompact event.

Preserves: cmd_detect_compaction subcommand, /session/.../detect-compaction
route, and common.sh case. These stay for test coverage and future use.
"""

from __future__ import annotations

from pathlib import Path

import pytest


HOOKS_DIR = Path(__file__).resolve().parent.parent / ".claude" / "hooks"
FABRICATED_ENV_VARS = ("CLAUDE_CONTEXT_PERCENT", "CLAUDE_CONTEXT_TOKENS")


def _read_hook(name: str) -> str:
    path = HOOKS_DIR / name
    if not path.exists():
        pytest.fail(f"skeleton — expected {path} to exist")
    return path.read_text()


class TestFabricatedEnvVarsRemoved:
    """No hook should reference CLAUDE_CONTEXT_PERCENT or CLAUDE_CONTEXT_TOKENS."""

    def test_writ_rag_inject_has_no_context_env_vars(self) -> None:
        content = _read_hook("writ-rag-inject.sh")
        for var in FABRICATED_ENV_VARS:
            assert var not in content, (
                f"{var} should be removed from writ-rag-inject.sh (always returns 0)"
            )

    def test_writ_context_tracker_has_no_context_env_vars(self) -> None:
        content = _read_hook("writ-context-tracker.sh")
        for var in FABRICATED_ENV_VARS:
            assert var not in content, (
                f"{var} should be removed from writ-context-tracker.sh"
            )

    def test_writ_session_end_has_no_context_env_vars(self) -> None:
        content = _read_hook("writ-session-end.sh")
        for var in FABRICATED_ENV_VARS:
            assert var not in content, (
                f"{var} should be removed from writ-session-end.sh"
            )


class TestDeadHookRemoved:
    """log-session-metrics.sh should be deleted."""

    def test_log_session_metrics_file_deleted(self) -> None:
        path = HOOKS_DIR / "log-session-metrics.sh"
        assert not path.exists(), (
            "log-session-metrics.sh should be deleted (superseded by writ-session-end.sh)"
        )


class TestDetectCompactionCallRemoved:
    """writ-rag-inject.sh should no longer call _writ_session detect-compaction.
    The heuristic can never fire (the input env var is always 0).
    PostCompact hook is the authoritative mechanism.
    """

    def test_writ_rag_inject_does_not_call_detect_compaction(self) -> None:
        content = _read_hook("writ-rag-inject.sh")
        assert "detect-compaction" not in content, (
            "writ-rag-inject.sh should no longer call detect-compaction; "
            "PostCompact hook handles compaction recovery"
        )


class TestTokenSnapshotLoggingRemoved:
    """writ-context-tracker.sh should not log token_snapshot events.
    The values are always 0 so the log entries are noise.
    """

    def test_writ_context_tracker_no_token_snapshot(self) -> None:
        content = _read_hook("writ-context-tracker.sh")
        assert "token_snapshot" not in content, (
            "token_snapshot logging should be removed (values are always 0)"
        )


class TestPreservedInfrastructure:
    """The subcommand/route/helper for detect-compaction stay.
    They're not called by any hook but remain available for future use
    (if/when Claude Code exposes context state) and for test coverage.
    """

    def test_detect_compaction_subcommand_still_exists(self) -> None:
        session_py = Path(__file__).resolve().parent.parent / "bin" / "lib" / "writ-session.py"
        content = session_py.read_text()
        assert "cmd_detect_compaction" in content, (
            "cmd_detect_compaction subcommand must be preserved for test coverage"
        )

    def test_detect_compaction_route_still_exists(self) -> None:
        server_py = Path(__file__).resolve().parent.parent / "writ" / "server.py"
        content = server_py.read_text()
        assert "detect-compaction" in content or "detect_compaction" in content, (
            "POST /session/.../detect-compaction route must be preserved"
        )
