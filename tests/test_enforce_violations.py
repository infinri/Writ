"""Tests for enforce-violations.sh Stop hook behavior.

Per TEST-TDD-001: skeletons approved before implementation.
The hook is a shell script; tests invoke it via subprocess with controlled
stdin and environment variables, or test the Python logic it calls.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

SKILL_DIR = "/home/lucio.saldivar/.claude/skills/writ"
HOOK_PATH = f"{SKILL_DIR}/.claude/hooks/enforce-violations.sh"
WRIT_SESSION_PY = f"{SKILL_DIR}/bin/lib/writ-session.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_cache(
    mode: str | None,
    pending_violations: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "session_id": "test-enforce-session",
        "mode": mode,
        "current_phase": "implementation",
        "remaining_budget": 5000,
        "context_percent": 30,
        "loaded_rule_ids": [],
        "queries": 0,
        "pending_violations": pending_violations,
        "escalation": {"needed": False},
        "invalidation_history": {},
        "failed_writes": [],
    }


def _run_hook(
    cache: dict[str, Any],
    env_overrides: dict[str, str] | None = None,
    stdin_payload: dict[str, Any] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Write cache to a temp session file, then invoke the hook via subprocess."""
    session_id = "test-enforce-session"

    with tempfile.TemporaryDirectory() as tmpdir:
        # Write cache at the path writ-session.py expects
        cache_path = os.path.join(tmpdir, f"writ-session-{session_id}.json")
        with open(cache_path, "w") as f:
            json.dump(cache, f)

        env = os.environ.copy()
        env["WRIT_SESSION_ID"] = session_id
        env["WRIT_CACHE_DIR"] = tmpdir
        # Point server at a port nothing listens on so hooks fall back gracefully
        env["WRIT_PORT"] = "19999"
        if env_overrides:
            env.update(env_overrides)

        stdin_data = json.dumps(stdin_payload or {})

        result = subprocess.run(
            ["bash", HOOK_PATH],
            input=stdin_data,
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )

    return result


# ---------------------------------------------------------------------------
# TestEnforceViolationsExitCodes
# ---------------------------------------------------------------------------


class TestEnforceViolationsExitCodes:
    """Exit code correctness for enforce-violations.sh across modes and violation states."""

    def test_work_mode_with_pending_violations_exits_2(self) -> None:
        """Work mode + non-empty pending_violations -> exit code 2 (blocking Stop)."""
        violations = [{"rule_id": "ARCH-ORG-001", "file": "foo.py", "line": None, "evidence": "mixed layers"}]
        cache = _build_cache(mode="work", pending_violations=violations)
        result = _run_hook(cache)
        assert result.returncode == 2, (
            f"Expected exit 2 in Work mode with violations, got {result.returncode}. "
            f"stderr: {result.stderr}"
        )

    def test_work_mode_with_empty_violations_exits_0(self) -> None:
        """Work mode + empty pending_violations -> exit code 0."""
        cache = _build_cache(mode="work", pending_violations=[])
        result = _run_hook(cache)
        assert result.returncode == 0, (
            f"Expected exit 0 in Work mode without violations, got {result.returncode}. "
            f"stderr: {result.stderr}"
        )

    def test_conversation_mode_with_pending_violations_exits_0(self) -> None:
        """Conversation mode with violations -> exit code 0 (non-Work modes are never blocking)."""
        violations = [{"rule_id": "ARCH-ORG-001", "file": "foo.py", "line": None, "evidence": "x"}]
        cache = _build_cache(mode="conversation", pending_violations=violations)
        result = _run_hook(cache)
        assert result.returncode == 0, (
            f"Expected exit 0 in Conversation mode, got {result.returncode}. "
            f"stderr: {result.stderr}"
        )

    def test_debug_mode_with_pending_violations_exits_0(self) -> None:
        """Debug mode with violations -> exit code 0."""
        violations = [{"rule_id": "PY-IMPORT-001", "file": "bar.py", "line": 10, "evidence": "x"}]
        cache = _build_cache(mode="debug", pending_violations=violations)
        result = _run_hook(cache)
        assert result.returncode == 0, (
            f"Expected exit 0 in Debug mode, got {result.returncode}. "
            f"stderr: {result.stderr}"
        )

    def test_review_mode_with_pending_violations_exits_0(self) -> None:
        """Review mode with violations -> exit code 0."""
        violations = [{"rule_id": "SEC-AUTH-001", "file": "auth.py", "line": None, "evidence": "x"}]
        cache = _build_cache(mode="review", pending_violations=violations)
        result = _run_hook(cache)
        assert result.returncode == 0, (
            f"Expected exit 0 in Review mode, got {result.returncode}. "
            f"stderr: {result.stderr}"
        )

    def test_no_mode_set_with_pending_violations_exits_0(self) -> None:
        """No mode set (None) with violations -> exit code 0 (safe default)."""
        violations = [{"rule_id": "ARCH-ORG-001", "file": "foo.py", "line": None, "evidence": "x"}]
        cache = _build_cache(mode=None, pending_violations=violations)
        result = _run_hook(cache)
        assert result.returncode == 0, (
            f"Expected exit 0 when mode is None, got {result.returncode}. "
            f"stderr: {result.stderr}"
        )


# ---------------------------------------------------------------------------
# TestEnforceViolationsOutputMessage
# ---------------------------------------------------------------------------


class TestEnforceViolationsOutputMessage:
    """Output message content when enforce-violations.sh exits 2."""

    def test_output_includes_rule_ids_from_pending_violations(self) -> None:
        """Exit-2 message lists the rule IDs present in pending_violations."""
        violations = [
            {"rule_id": "ARCH-ORG-001", "file": "foo.py", "line": None, "evidence": "x"},
            {"rule_id": "PY-IMPORT-001", "file": "bar.py", "line": 5, "evidence": "y"},
        ]
        cache = _build_cache(mode="work", pending_violations=violations)
        result = _run_hook(cache)
        combined_output = result.stdout + result.stderr
        assert "ARCH-ORG-001" in combined_output, (
            "Output must include rule ID ARCH-ORG-001"
        )
        assert "PY-IMPORT-001" in combined_output, (
            "Output must include rule ID PY-IMPORT-001"
        )

    def test_output_includes_violation_count(self) -> None:
        """Exit-2 message includes the number of unresolved violations."""
        violations = [
            {"rule_id": "ARCH-ORG-001", "file": "foo.py", "line": None, "evidence": "x"},
            {"rule_id": "PY-IMPORT-001", "file": "bar.py", "line": 5, "evidence": "y"},
            {"rule_id": "SEC-AUTH-001", "file": "auth.py", "line": 12, "evidence": "z"},
        ]
        cache = _build_cache(mode="work", pending_violations=violations)
        result = _run_hook(cache)
        combined_output = result.stdout + result.stderr
        assert "3" in combined_output, (
            "Output must include the violation count (3 in this case)"
        )
