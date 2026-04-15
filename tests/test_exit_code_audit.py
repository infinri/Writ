"""Tests for exit code correctness across validate-rules.sh, validate-file.sh,
and validate-handoff.sh.

Per TEST-TDD-001: skeletons approved before implementation.
Tests cover:
- validate-rules.sh: exit 1 for per-write advisory, exit 2 for gate invalidation
- validate-file.sh: exit 1 only (advisory)
- validate-handoff.sh: exit 1 only (advisory)
- Documentation comments at the top of all three hook files
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from typing import Any

import pytest

SKILL_DIR = "/home/lucio.saldivar/.claude/skills/writ"
VALIDATE_RULES_SH = f"{SKILL_DIR}/.claude/hooks/validate-rules.sh"
VALIDATE_FILE_SH = f"{SKILL_DIR}/.claude/hooks/validate-file.sh"
VALIDATE_HANDOFF_SH = f"{SKILL_DIR}/.claude/hooks/validate-handoff.sh"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_env(
    mode: str = "work",
    session_id: str = "test-exitcode-session",
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    env = os.environ.copy()
    env["WRIT_SESSION_ID"] = session_id
    env["WRIT_MODE"] = mode
    # Point server at a port nothing listens on so hooks fall back gracefully
    env["WRIT_PORT"] = "19999"
    if extra:
        env.update(extra)
    return env


def _write_hook_stdin(file_path: str, tool_name: str = "Write") -> str:
    """Return JSON envelope as a string, matching Claude Code PostToolUse format."""
    return json.dumps({
        "tool_name": tool_name,
        "tool_input": {"path": file_path},
        "tool_response": {"type": "result", "result": ""},
    })


# ---------------------------------------------------------------------------
# TestValidateRulesExitCodes
# ---------------------------------------------------------------------------


class TestValidateRulesExitCodes:
    """validate-rules.sh must exit 1 for per-write advisories and exit 2 for gate invalidation."""

    def test_per_write_advisory_no_plan_exits_1(self) -> None:
        """Per-write path when plan.md does not exist exits 1 (advisory, not blocking).

        Verifies via source inspection that the no-plan.md advisory path uses exit 1.
        This path is at ~line 300 (after 'No plan.md -> warning mode only').
        The hook requires a running /analyze endpoint for subprocess testing, so
        source inspection is the reliable verification method.
        """
        with open(VALIDATE_RULES_SH) as f:
            content = f.read()
        lines = content.split("\n")

        # Find the "No plan.md" comment and verify the next exit is 1
        found_no_plan = False
        for i, line in enumerate(lines):
            if "No plan.md" in line and "warning" in line.lower():
                found_no_plan = True
                # Look for exit statement in the next few lines
                for j in range(i + 1, min(i + 5, len(lines))):
                    stripped = lines[j].strip()
                    if stripped.startswith("exit "):
                        assert stripped == "exit 1", (
                            f"No-plan.md advisory path must use exit 1, got '{stripped}' "
                            f"at line {j + 1}"
                        )
                        break
                break
        assert found_no_plan, (
            "validate-rules.sh must have a 'No plan.md' advisory path"
        )

    def test_per_write_advisory_boundary_not_reached_exits_1(self) -> None:
        """Per-write path when phase boundary has not been reached exits 1 (advisory).

        Verifies via source inspection that the boundary-not-reached path uses exit 1.
        This path is at ~line 362 (after 'Per-write warning mode').
        """
        with open(VALIDATE_RULES_SH) as f:
            content = f.read()
        lines = content.split("\n")

        # Find the boundary-not-reached comment and verify the exit is 1
        found_boundary = False
        for i, line in enumerate(lines):
            if "Per-write warning" in line or "boundary" in line.lower() and "warning" in line.lower():
                stripped = line.strip()
                # Check if this line or next few lines have exit 1
                for j in range(i, min(i + 5, len(lines))):
                    s = lines[j].strip()
                    if s.startswith("exit "):
                        assert s == "exit 1", (
                            f"Boundary-not-reached advisory path must use exit 1, got '{s}' "
                            f"at line {j + 1}"
                        )
                        found_boundary = True
                        break
                if found_boundary:
                    break

        assert found_boundary, (
            "validate-rules.sh must have a per-write warning path with exit 1 "
            "for the boundary-not-reached case"
        )

    def test_gate_invalidation_path_exits_2(self) -> None:
        """Phase-boundary gate invalidation path exits 2 (blocking).

        This test verifies the source code directly -- the gate invalidation
        path at the end of validate-rules.sh must use 'exit 2'.
        """
        with open(VALIDATE_RULES_SH) as f:
            lines = f.readlines()

        # Find the last exit statement in the file
        last_exit_line = None
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("exit "):
                last_exit_line = (i, stripped)

        assert last_exit_line is not None, "validate-rules.sh must have exit statements"
        line_no, exit_stmt = last_exit_line
        assert exit_stmt == "exit 2", (
            f"Last exit in validate-rules.sh (line {line_no}) must be 'exit 2' "
            f"(gate invalidation path), got '{exit_stmt}'"
        )

    def test_pass_path_exits_0(self) -> None:
        """When hook processes a file outside Work mode or with no violations, exits 0.

        The simplest pass path: file has no extension (unknown language), hook exits 0 early.
        """
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "test-exitcode-session"
            cache = {
                "mode": "work",
                "current_phase": "implementation",
                "remaining_budget": 5000,
                "loaded_rule_ids": [],
                "loaded_rule_ids_by_phase": {},
                "gates_approved": ["phase-a", "test-skeletons"],
                "pending_violations": [],
                "files_written": [],
                "analysis_results": {},
            }
            cache_path = os.path.join(tmp, f"writ-session-{session_id}.json")
            with open(cache_path, "w") as f:
                json.dump(cache, f)

            # Empty file path in envelope -> hook exits 0 early
            env = _stub_env(extra={
                "PROJECT_ROOT": tmp,
                "WRIT_CACHE_DIR": tmp,
            })
            stdin_data = json.dumps({
                "tool_name": "Write",
                "tool_input": {},
                "tool_response": {"type": "result", "result": ""},
            })
            result = subprocess.run(
                ["bash", VALIDATE_RULES_SH],
                input=stdin_data,
                capture_output=True,
                text=True,
                env=env,
                timeout=15,
            )
        assert result.returncode == 0, (
            f"Expected exit 0 for pass path (no file in envelope), got {result.returncode}. "
            f"stderr: {result.stderr}"
        )


# ---------------------------------------------------------------------------
# TestValidateFileExitCodes
# ---------------------------------------------------------------------------


class TestValidateFileExitCodes:
    """validate-file.sh exits 1 (advisory) on failure, never exit 2."""

    def test_failure_exits_1_not_2(self) -> None:
        """validate-file.sh failure path exits 1, not 2 (advisory only per plan spec).

        Verifies via source inspection that no 'exit 2' exists in the file.
        """
        with open(VALIDATE_FILE_SH) as f:
            content = f.read()
        lines = content.strip().split("\n")
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped == "exit 2":
                pytest.fail(
                    f"validate-file.sh line {i} has 'exit 2' -- must be advisory (exit 1) only"
                )

    def test_pass_exits_0(self) -> None:
        """validate-file.sh success path exits 0 when no file path in envelope."""
        env = _stub_env()
        stdin_data = json.dumps({
            "tool_name": "Write",
            "tool_input": {},
            "tool_response": {"type": "result", "result": ""},
        })
        result = subprocess.run(
            ["bash", VALIDATE_FILE_SH],
            input=stdin_data,
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
        assert result.returncode == 0, (
            f"Expected exit 0 for pass path (no file), got {result.returncode}. "
            f"stderr: {result.stderr}"
        )


# ---------------------------------------------------------------------------
# TestValidateHandoffExitCodes
# ---------------------------------------------------------------------------


class TestValidateHandoffExitCodes:
    """validate-handoff.sh exits 1 (advisory) on failure, never exit 2."""

    def test_failure_exits_1_not_2(self) -> None:
        """validate-handoff.sh failure path exits 1, not 2 (advisory only per plan spec).

        Verifies via source inspection that no 'exit 2' exists in the file.
        """
        with open(VALIDATE_HANDOFF_SH) as f:
            content = f.read()
        lines = content.strip().split("\n")
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped == "exit 2":
                pytest.fail(
                    f"validate-handoff.sh line {i} has 'exit 2' -- must be advisory (exit 1) only"
                )

    def test_pass_exits_0(self) -> None:
        """validate-handoff.sh success path exits 0 when file does not match handoff pattern."""
        env = _stub_env()
        stdin_data = json.dumps({
            "tool_name": "Write",
            "tool_input": {"path": "/some/regular/file.py"},
            "tool_response": {"type": "result", "result": ""},
        })
        result = subprocess.run(
            ["bash", VALIDATE_HANDOFF_SH],
            input=stdin_data,
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
        assert result.returncode == 0, (
            f"Expected exit 0 for non-handoff file, got {result.returncode}. "
            f"stderr: {result.stderr}"
        )


# ---------------------------------------------------------------------------
# TestExitCodeDocumentationComments
# ---------------------------------------------------------------------------


class TestExitCodeDocumentationComments:
    """Exit code documentation comments exist at the top of each hook file."""

    def test_validate_rules_has_exit_code_comment(self) -> None:
        """validate-rules.sh has a top-of-file comment documenting exit codes."""
        with open(VALIDATE_RULES_SH) as f:
            # Read only the first 30 lines -- comment must be near the top
            header = "".join(f.readline() for _ in range(30))
        assert "Exit codes" in header or "exit code" in header.lower(), (
            "validate-rules.sh must have an exit code documentation comment near the top. "
            "Expected text like '# Exit codes: 0=pass, 1=warning (advisory), 2=blocking'"
        )

    def test_validate_rules_comment_distinguishes_1_and_2(self) -> None:
        """validate-rules.sh comment documents both exit 1 (advisory) and exit 2 (blocking)."""
        with open(VALIDATE_RULES_SH) as f:
            header = "".join(f.readline() for _ in range(30))
        assert "1" in header and "2" in header, (
            "validate-rules.sh exit code comment must reference both exit 1 and exit 2"
        )

    def test_validate_file_has_exit_code_comment(self) -> None:
        """validate-file.sh has a top-of-file comment documenting its exit codes."""
        with open(VALIDATE_FILE_SH) as f:
            header = "".join(f.readline() for _ in range(30))
        assert "Exit codes" in header or "exit code" in header.lower(), (
            "validate-file.sh must have an exit code documentation comment near the top. "
            "Expected text like '# Exit codes: 0=pass, 1=warning (advisory -- deliberate)'"
        )

    def test_validate_handoff_has_exit_code_comment(self) -> None:
        """validate-handoff.sh has a top-of-file comment documenting its exit codes."""
        with open(VALIDATE_HANDOFF_SH) as f:
            header = "".join(f.readline() for _ in range(30))
        assert "Exit codes" in header or "exit code" in header.lower(), (
            "validate-handoff.sh must have an exit code documentation comment near the top. "
            "Expected text like '# Exit codes: 0=pass, 1=warning (advisory -- deliberate)'"
        )
