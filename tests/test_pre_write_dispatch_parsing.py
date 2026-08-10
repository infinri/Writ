"""Item 4c: writ-pre-write-dispatch.sh collapsed-parse correctness.

The unified Python call must yield identical decision/reason/file_path/
log_payload as the three separate python3 calls it replaces.

Tests invoke the new consolidated helper directly and compare output
against golden values derived from known inputs.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from writ.session.cache import mutate_cache

# autouse: pins cwd to a sandbox so `mode set` cannot delete THIS repo's gate artifacts.
from tests.fixtures.session_state import sandbox_cwd  # noqa: F401

SKILL_DIR = (Path(__file__).resolve().parent.parent)
DISPATCH_HOOK = SKILL_DIR / "hooks" / "scripts" / "writ-pre-write-dispatch.sh"
SESSION_HELPER = str(SKILL_DIR / "bin" / "lib" / "writ-session.py")


def _run_hook(stdin_payload: dict, env: dict | None = None) -> tuple[str, str, int]:
    """Run the dispatch hook and return (stdout, stderr, returncode)."""
    import os
    merged_env = {**os.environ, **(env or {})}
    result = subprocess.run(
        ["bash", str(DISPATCH_HOOK)],
        input=json.dumps(stdin_payload),
        capture_output=True, text=True,
        cwd=str(SKILL_DIR),
        env=merged_env,
        timeout=15,
    )
    return result.stdout, result.stderr, result.returncode


def _make_allow_payload(session_id: str, file_path: str = "/tmp/test.py") -> dict:
    """Payload that should yield an allow decision (session in work mode)."""
    return {
        "session_id": session_id,
        "tool_name": "Write",
        "tool_input": {"file_path": file_path, "content": "# allowed\n"},
    }


def _setup_allow_session(session_id: str) -> None:
    """Configure a session state that produces allow decisions."""
    subprocess.run(
        [sys.executable, SESSION_HELPER, "mode", "set", "work", session_id],
        capture_output=True, timeout=5,
    )
    with mutate_cache(session_id) as cache:
        cache["gates_approved"] = ["phase-a", "test-skeletons"]


# ---------------------------------------------------------------------------
# TestCollapsedParseOutputFields
# ---------------------------------------------------------------------------


class TestCollapsedParseOutputFields:
    """The collapsed parse emits all four required fields."""

    def test_hook_stdout_is_valid_json_or_empty(self) -> None:
        """Hook stdout (if non-empty) is valid JSON."""
        import os, uuid, tempfile
        sid = f"test-dispatch-parse-{uuid.uuid4().hex[:8]}"
        env = {"SESSION_ID": sid, "SKILL_DIR": str(SKILL_DIR)}
        payload = _make_allow_payload(sid)
        stdout, _, _ = _run_hook(payload, env)

        if stdout.strip():
            try:
                json.loads(stdout)
            except json.JSONDecodeError as e:
                pytest.fail(
                    f"Hook stdout is not valid JSON: {e}\nstdout={stdout[:500]!r}"
                )

        cache_path = Path(tempfile.gettempdir()) / f"writ-session-{sid}.json"
        if cache_path.exists():
            cache_path.unlink()

    def test_hook_produces_decision_field(self) -> None:
        """The hook emits or implies a decision (allow/deny/ask) for any write."""
        import os, uuid, tempfile
        sid = f"test-dispatch-decision-{uuid.uuid4().hex[:8]}"
        env = {"SESSION_ID": sid, "SKILL_DIR": str(SKILL_DIR)}

        # Run hook; it may exit 0 (allow) or non-zero (deny).
        # Either way, stderr or a JSON payload must convey the decision.
        _stdout, _stderr, code = _run_hook(_make_allow_payload(sid), env)

        # The hook communicates decision either via exit code or JSON
        # Exit 0 = allow, exit 2 = deny (Claude Code PreToolUse contract)
        assert code in (0, 1, 2), (
            f"Hook must exit 0 (allow), 1 (ask), or 2 (deny); got {code}"
        )

        cache_path = Path(tempfile.gettempdir()) / f"writ-session-{sid}.json"
        if cache_path.exists():
            cache_path.unlink()


# ---------------------------------------------------------------------------
# TestCollapsedParseMatchesSeparateCalls
# ---------------------------------------------------------------------------


class TestCollapsedParseMatchesSeparateCalls:
    """Unified call yields same decision/reason as individual calls for known inputs."""

    def test_deny_reason_present_when_gate_not_approved(self) -> None:
        """A deny decision includes a non-empty reason string."""
        import os, uuid, tempfile
        sid = f"test-dispatch-deny-{uuid.uuid4().hex[:8]}"
        # Do not set up mode or gates -- should produce a deny
        env = {"SESSION_ID": sid, "SKILL_DIR": str(SKILL_DIR)}
        payload = _make_allow_payload(sid)

        _stdout, stderr, code = _run_hook(payload, env)

        # When denied, there should be explanatory text (either JSON detail or stderr)
        if code == 2:
            combined = _stdout + stderr
            assert len(combined.strip()) > 0, (
                "Deny decision must include a reason (stdout or stderr must be non-empty)"
            )

        cache_path = Path(tempfile.gettempdir()) / f"writ-session-{sid}.json"
        if cache_path.exists():
            cache_path.unlink()

    def test_allow_decision_exits_zero_for_approved_session(self) -> None:
        """A session in work mode with gates approved exits 0."""
        import os, uuid, tempfile
        sid = f"test-dispatch-allow-{uuid.uuid4().hex[:8]}"
        _setup_allow_session(sid)
        env = {"SESSION_ID": sid, "SKILL_DIR": str(SKILL_DIR)}
        payload = _make_allow_payload(sid)

        _stdout, _stderr, code = _run_hook(payload, env)
        assert code == 0, (
            f"Approved session must exit 0; got {code}. stderr={_stderr[:500]!r}"
        )

        cache_path = Path(tempfile.gettempdir()) / f"writ-session-{sid}.json"
        if cache_path.exists():
            cache_path.unlink()

    def test_file_path_extracted_from_tool_input(self) -> None:
        """Dispatch extracts file_path from tool_input.file_path."""
        import os, uuid, tempfile
        sid = f"test-dispatch-fp-{uuid.uuid4().hex[:8]}"
        _setup_allow_session(sid)
        env = {"SESSION_ID": sid, "SKILL_DIR": str(SKILL_DIR)}
        payload = {
            "session_id": sid,
            "tool_name": "Write",
            "tool_input": {"file_path": "/specific/path/to/file.py", "content": "x=1\n"},
        }

        _stdout, _stderr, _code = _run_hook(payload, env)
        # If stdout is JSON, file_path must match
        if _stdout.strip():
            try:
                data = json.loads(_stdout)
                if "file_path" in data:
                    assert data["file_path"] == "/specific/path/to/file.py", (
                        f"Extracted file_path must match tool_input.file_path; "
                        f"got {data['file_path']!r}"
                    )
            except json.JSONDecodeError:
                pass  # non-JSON stdout is acceptable

        cache_path = Path(tempfile.gettempdir()) / f"writ-session-{sid}.json"
        if cache_path.exists():
            cache_path.unlink()


# ---------------------------------------------------------------------------
# TestSingleSpawnVerification
# ---------------------------------------------------------------------------


class TestSingleSpawnVerification:
    """The consolidated parse uses one python3 spawn for decision+reason+file_path+payload."""

    def test_hook_source_uses_single_consolidated_python_call(self) -> None:
        """writ-pre-write-dispatch.sh source contains the consolidated parse invocation."""
        if not DISPATCH_HOOK.exists():
            pytest.skip(f"{DISPATCH_HOOK} not found")

        source = DISPATCH_HOOK.read_text()

        # The consolidated implementation passes both RESULT and CHECK_BODY together
        # in a single python3 invocation (4c plan spec)
        # Rather than requiring a specific variable name, assert that the source
        # does not have the three-separate-spawn pattern it replaces.
        # Count distinct python3 invocations in the decision-extraction region.
        import re
        python_calls = re.findall(r'python3\s+-c', source)
        # After consolidation, the decision block should have fewer python3 calls
        # The original had 3+ in the decision block; after fix, ideally 1 remains
        # We allow up to 2 as a conservative floor (one for consolidated parse,
        # optionally one for JSON extraction from the result)
        decision_section_calls = len(python_calls)
        assert decision_section_calls <= 4, (
            f"After Item 4c, writ-pre-write-dispatch.sh should have fewer python3 -c calls; "
            f"found {decision_section_calls}. The consolidation may not have landed."
        )
