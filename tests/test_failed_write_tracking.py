"""Tests for track-failed-writes.sh PostToolUseFailure hook and related cache schema.

Per TEST-TDD-001: skeletons approved before implementation.
Covers: session cache schema (failed_writes field), hook subprocess behavior,
and failed write record structure.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

SKILL_DIR = "/home/lucio.saldivar/.claude/skills/writ"
HOOK_PATH = f"{SKILL_DIR}/.claude/hooks/track-failed-writes.sh"
WRIT_SESSION_PY = f"{SKILL_DIR}/bin/lib/writ-session.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_hook_stdin(
    tool_name: str,
    file_path: str,
    error: str,
) -> str:
    """Return JSON envelope mimicking the Claude Code PostToolUseFailure payload."""
    return json.dumps({
        "tool_name": tool_name,
        "tool_input": {"path": file_path},
        "error": error,
    })


def _run_hook(
    stdin_payload: str,
    session_cache: dict[str, Any] | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    """Run the track-failed-writes.sh hook with controlled stdin and session cache.

    Returns (completed_process, updated_cache) so callers can inspect the cache after.
    """
    with tempfile.TemporaryDirectory() as tmp:
        session_id = "test-failed-writes-session"
        cache_path = os.path.join(tmp, f"writ-session-{session_id}.json")

        if session_cache is not None:
            with open(cache_path, "w") as f:
                json.dump(session_cache, f)

        env = os.environ.copy()
        env["WRIT_SESSION_ID"] = session_id
        env["WRIT_CACHE_DIR"] = tmp
        env["WRIT_PORT"] = "19999"  # No server listening

        result = subprocess.run(
            ["bash", HOOK_PATH],
            input=stdin_payload,
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )

        # Read updated cache if present
        updated_cache: dict[str, Any] = {}
        if os.path.exists(cache_path):
            try:
                with open(cache_path) as f:
                    updated_cache = json.load(f)
            except json.JSONDecodeError:
                pass

    return result, updated_cache


# ---------------------------------------------------------------------------
# TestFailedWritesCacheSchema
# ---------------------------------------------------------------------------


class TestFailedWritesCacheSchema:
    """failed_writes field exists in default cache schema from _read_cache."""

    def test_failed_writes_field_in_default_cache(self) -> None:
        """_read_cache returns a dict with 'failed_writes' key defaulting to empty list."""
        import importlib.util

        spec = importlib.util.spec_from_file_location("writ_session", WRIT_SESSION_PY)
        assert spec is not None and spec.loader is not None, "Could not load writ-session.py"
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]

        cache = mod._read_cache("nonexistent-session-xyz987")
        assert "failed_writes" in cache, (
            "_read_cache default must include 'failed_writes' key"
        )
        assert cache["failed_writes"] == [], (
            "Default 'failed_writes' must be an empty list"
        )

    def test_failed_writes_defaults_to_empty_list_on_existing_cache_without_field(self) -> None:
        """_read_cache sets failed_writes to [] via setdefault on a cache missing the field."""
        import importlib.util

        with tempfile.TemporaryDirectory() as tmp:
            session_id = "schema-test-session"
            cache_path = os.path.join(tmp, f"writ-session-{session_id}.json")
            # Write a cache without the failed_writes field
            old_cache = {"mode": "work", "remaining_budget": 8000, "loaded_rule_ids": []}
            with open(cache_path, "w") as f:
                json.dump(old_cache, f)

            spec = importlib.util.spec_from_file_location("writ_session", WRIT_SESSION_PY)
            assert spec is not None and spec.loader is not None
            mod = importlib.util.module_from_spec(spec)

            # Patch CACHE_DIR to our temp dir
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            original_cache_dir = mod.CACHE_DIR
            mod.CACHE_DIR = tmp
            try:
                cache = mod._read_cache(session_id)
            finally:
                mod.CACHE_DIR = original_cache_dir

        assert "failed_writes" in cache, (
            "_read_cache must add 'failed_writes' via setdefault on cache missing the field"
        )


# ---------------------------------------------------------------------------
# TestTrackFailedWritesHook
# ---------------------------------------------------------------------------


class TestTrackFailedWritesHook:
    """Behavioral tests for track-failed-writes.sh."""

    def test_write_failure_recorded_in_session_cache(self) -> None:
        """A Write tool failure is appended to the failed_writes list in session cache."""
        stdin = _build_hook_stdin("Write", "/src/app.py", "gate denied")
        initial_cache: dict[str, Any] = {
            "mode": "work",
            "remaining_budget": 5000,
            "loaded_rule_ids": [],
            "pending_violations": [],
            "failed_writes": [],
        }
        result, updated_cache = _run_hook(stdin, initial_cache)
        failed = updated_cache.get("failed_writes", [])
        assert len(failed) == 1, (
            f"Expected 1 entry in failed_writes after Write failure, got {len(failed)}"
        )

    def test_failed_write_record_has_required_fields(self) -> None:
        """Each failed_writes entry contains 'file', 'reason', and 'timestamp' fields."""
        stdin = _build_hook_stdin("Write", "/src/app.py", "permission denied")
        initial_cache: dict[str, Any] = {
            "mode": "work",
            "remaining_budget": 5000,
            "loaded_rule_ids": [],
            "pending_violations": [],
            "failed_writes": [],
        }
        result, updated_cache = _run_hook(stdin, initial_cache)
        failed = updated_cache.get("failed_writes", [])
        assert len(failed) >= 1
        record = failed[0]
        assert "file" in record, "failed_writes record must have 'file' field"
        assert "reason" in record, "failed_writes record must have 'reason' field"
        assert "timestamp" in record, "failed_writes record must have 'timestamp' field"

    def test_failed_write_timestamp_is_iso8601(self) -> None:
        """The 'timestamp' field in a failed_writes record is ISO 8601 format."""
        stdin = _build_hook_stdin("Write", "/src/app.py", "disk full")
        initial_cache: dict[str, Any] = {
            "mode": "work",
            "remaining_budget": 5000,
            "loaded_rule_ids": [],
            "pending_violations": [],
            "failed_writes": [],
        }
        result, updated_cache = _run_hook(stdin, initial_cache)
        failed = updated_cache.get("failed_writes", [])
        assert len(failed) >= 1
        ts = failed[0].get("timestamp", "")
        # ISO 8601 basic check: contains T and Z or offset
        iso8601_pattern = re.compile(
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
        )
        assert iso8601_pattern.match(ts), (
            f"timestamp '{ts}' does not match ISO 8601 format"
        )

    def test_edit_failure_is_also_recorded(self) -> None:
        """An Edit tool failure is appended to failed_writes (not just Write)."""
        stdin = _build_hook_stdin("Edit", "/src/config.py", "gate denied")
        initial_cache: dict[str, Any] = {
            "mode": "work",
            "remaining_budget": 5000,
            "loaded_rule_ids": [],
            "pending_violations": [],
            "failed_writes": [],
        }
        result, updated_cache = _run_hook(stdin, initial_cache)
        failed = updated_cache.get("failed_writes", [])
        assert len(failed) == 1, (
            f"Expected 1 entry in failed_writes after Edit failure, got {len(failed)}"
        )

    def test_non_write_edit_tool_failure_is_ignored(self) -> None:
        """A failure on a non-Write/Edit tool (e.g., Bash) exits 0 and does not update cache."""
        stdin = _build_hook_stdin("Bash", "", "command not found")
        initial_cache: dict[str, Any] = {
            "mode": "work",
            "remaining_budget": 5000,
            "loaded_rule_ids": [],
            "pending_violations": [],
            "failed_writes": [],
        }
        result, updated_cache = _run_hook(stdin, initial_cache)
        assert result.returncode == 0, (
            f"Hook must exit 0 for non-Write/Edit tool failure, got {result.returncode}"
        )
        failed = updated_cache.get("failed_writes", [])
        assert len(failed) == 0, (
            f"Non-Write/Edit failure must not append to failed_writes, found {failed}"
        )

    def test_hook_always_exits_0(self) -> None:
        """track-failed-writes.sh always exits 0 regardless of input (telemetry only)."""
        for tool in ("Write", "Edit", "Bash", "Read"):
            stdin = _build_hook_stdin(tool, "/some/path.py", "some error")
            result, _ = _run_hook(stdin, {})
            assert result.returncode == 0, (
                f"Hook must always exit 0, got {result.returncode} for tool={tool}"
            )

    def test_multiple_failures_accumulate_in_list(self) -> None:
        """Successive Write failures append to failed_writes without replacing previous entries.

        This test runs the hook twice in sequence by including the first result
        as the initial cache for the second run.
        """
        # For accumulation, we need to run the hook in the same temp dir.
        # Instead, run once, capture cache, run again with that cache.
        stdin1 = _build_hook_stdin("Write", "/src/a.py", "gate denied")
        initial_cache: dict[str, Any] = {
            "mode": "work",
            "remaining_budget": 5000,
            "loaded_rule_ids": [],
            "pending_violations": [],
            "failed_writes": [],
        }
        _, updated_after_first = _run_hook(stdin1, initial_cache)

        # Second failure using the updated cache
        stdin2 = _build_hook_stdin("Edit", "/src/b.py", "permission denied")
        _, updated_after_second = _run_hook(stdin2, updated_after_first)

        failed = updated_after_second.get("failed_writes", [])
        assert len(failed) == 2, (
            f"Expected 2 entries in failed_writes after two failures, got {len(failed)}"
        )


# ---------------------------------------------------------------------------
# TestTrackFailedWritesFrictionLog
# ---------------------------------------------------------------------------


class TestTrackFailedWritesFrictionLog:
    """Friction log receives write_failure event entries."""

    def test_friction_log_gets_write_failure_event(self) -> None:
        """After a Write failure, workflow-friction.log contains an entry with event='write_failure'."""
        with tempfile.TemporaryDirectory() as tmp:
            # Create a fake project root with pyproject.toml so log_friction_event can find it
            pyproject = os.path.join(tmp, "pyproject.toml")
            with open(pyproject, "w") as f:
                f.write("[tool.poetry]\nname = 'test'\n")

            session_id = "test-friction-session"
            cache_path = os.path.join(tmp, f"writ-session-{session_id}.json")
            initial_cache: dict[str, Any] = {
                "mode": "work",
                "remaining_budget": 5000,
                "loaded_rule_ids": [],
                "pending_violations": [],
                "failed_writes": [],
            }
            with open(cache_path, "w") as f:
                json.dump(initial_cache, f)

            env = os.environ.copy()
            env["WRIT_SESSION_ID"] = session_id
            env["WRIT_CACHE_DIR"] = tmp
            env["WRIT_PORT"] = "19999"

            stdin = _build_hook_stdin("Write", "/src/app.py", "gate denied")
            subprocess.run(
                ["bash", HOOK_PATH],
                input=stdin,
                capture_output=True,
                text=True,
                env=env,
                timeout=10,
                cwd=tmp,
            )

            friction_log = os.path.join(tmp, "workflow-friction.log")
            if not os.path.exists(friction_log):
                pytest.fail(
                    "workflow-friction.log not written by hook"
                )

            with open(friction_log) as f:
                lines = f.readlines()

            events = []
            for line in lines:
                try:
                    entry = json.loads(line.strip())
                    events.append(entry.get("event", ""))
                except json.JSONDecodeError:
                    continue

            assert "write_failure" in events, (
                f"workflow-friction.log must contain an entry with event='write_failure'. "
                f"Found events: {events}"
            )
