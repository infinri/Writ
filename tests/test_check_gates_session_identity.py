"""Part 1 of the isolation cycle: bin/check-gates.sh gains a session identity and
refuses to run without one.

Today (verify on read) check-gates.sh has NO session concept at all: it takes an optional
positional PROJECT_ROOT, defaults to the cwd via detect_project_root, and reads the flat
`<project_root>/.claude/gates/*.approved` files with no notion of which session is asking.
That is what makes it possible for one session's approval to read as every session's
approval in the same project -- the exact interference this cycle exists to remove.

Part 1 adds the identity half only: `--session SID` or `$CLAUDE_SESSION_ID` becomes
REQUIRED, and neither present exits 2 with a JSON error naming the flag. This file tests
ONLY that identity gate -- reading gate state from the session-scoped artifact path is
Part 2's own capability (`check-gates.sh --session A` vs `--session B` disagreeing about
the SAME project), and is deliberately not asserted here so this file stays true to what
Part 1 alone claims.

RED today: check-gates.sh exits 0 (or errors only on a missing project root) regardless of
whether a session was supplied, because it does not look for one at all yet. Per
TEST-TDD-001: skeletons approved before implementation.

REAL subprocess (bash) against a REAL project directory in tmp_path, per ENF-SYS-005 --
this claim is about a caller refusing to guess an identity, and a mocked subprocess would
only prove the mock understood the assignment.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "bin" / "check-gates.sh"


@pytest.fixture()
def project_root(tmp_path) -> Path:
    root = tmp_path / "project"
    (root / ".claude" / "gates").mkdir(parents=True)
    (root / ".git").mkdir()
    return root


def _run(args: list[str], cwd: Path, **extra_env: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_SESSION_ID"}
    env.update(extra_env)
    return subprocess.run(
        ["bash", str(SCRIPT), *args], cwd=str(cwd), env=env,
        capture_output=True, text=True, timeout=30,
    )


class TestNoSessionAndNoEnvExitsLoud:
    """Capability 7: bin/check-gates.sh with no --session and no env id exits 2 with a
    JSON error naming the flag."""

    def test_exits_2(self, project_root):
        proc = _run([str(project_root)], project_root)
        assert proc.returncode == 2, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"

    def test_stdout_is_a_json_error(self, project_root):
        proc = _run([str(project_root)], project_root)
        payload = json.loads(proc.stdout)
        assert "error" in payload and payload["error"]

    def test_error_names_the_session_flag(self, project_root):
        proc = _run([str(project_root)], project_root)
        payload = json.loads(proc.stdout)
        text = payload.get("error", "")
        assert "--session" in text, (
            f"the error must name the --session flag explicitly: {text!r}"
        )

    def test_auto_detected_cwd_project_root_does_not_bypass_the_session_requirement(
        self, project_root, monkeypatch
    ):
        """The current script auto-detects PROJECT_ROOT from cwd when no positional arg
        is given. That convenience must not let a session-less call slip past the new
        gate: cwd-detection and identity are orthogonal checks."""
        proc = _run([], project_root)
        assert proc.returncode == 2, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"


class TestSessionSuppliedPassesTheIdentityGate:
    """Positive side, deliberately narrow: only that supplying a session gets PAST the
    identity check, not what the resulting gate booleans say (Part 2's own claim, once the
    session-scoped artifact path exists)."""

    def test_explicit_session_flag_does_not_trigger_the_missing_session_error(
        self, project_root
    ):
        proc = _run(["--session", "sid-a", str(project_root)], project_root)
        assert proc.returncode != 2 or "error" not in json.loads(proc.stdout or "{}"), (
            f"a supplied --session must not be treated as absent: "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )

    def test_claude_session_id_env_does_not_trigger_the_missing_session_error(
        self, project_root
    ):
        proc = _run([str(project_root)], project_root, CLAUDE_SESSION_ID="sid-env-b")
        assert proc.returncode != 2 or "error" not in json.loads(proc.stdout or "{}"), (
            f"$CLAUDE_SESSION_ID must be an accepted fallback: "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )
