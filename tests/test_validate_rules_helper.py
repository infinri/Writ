"""Item 4b: bin/lib/validate-rules-helper.py single-spawn correctness.

The helper is called once before /analyze (to derive context, phase,
plan-file) and once after (to consume the analyze response and emit
routing decisions). Both invocations must match the inline Python logic
they replace.

PART 2 ADDITION (isolation cycle, plan_hash 033bb1595c2c): `_derive_phase` is the
non-test reader capabilities.md calls out by name as mattering most --
`bin/lib/validate-rules-helper.py:111-120` derived the WORKFLOW PHASE from flat
gate-artifact presence, with no session component at all, and a stale artifact from a
DIFFERENT session misreported phase for eighteen days in this very repo (see
test_gate_artifact_cleanup.py's module docstring for that incident). `TestDerivePhase`
below pins the session-scoped fix directly against the function; `TestPreAnalyzePhaseIsSessionScoped`
pins the same claim through the real `pre-analyze` CLI subprocess `validate-rules.sh`
actually invokes, with `--session-id` (already supplied there per plan.md's own
annotation).
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SKILL_DIR = (Path(__file__).resolve().parent.parent)
HELPER = SKILL_DIR / "bin" / "lib" / "validate-rules-helper.py"
SESSION_HELPER = str(SKILL_DIR / "bin" / "lib" / "writ-session.py")


def _load_helper():
    """Load validate-rules-helper.py as a Python module."""
    spec = importlib.util.spec_from_file_location("validate_rules_helper", str(HELPER))
    assert spec is not None and spec.loader is not None, f"{HELPER} not found"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_helper(*args: str, stdin_data: str = "") -> subprocess.CompletedProcess:
    """Invoke validate-rules-helper.py as a subprocess."""
    return subprocess.run(
        [sys.executable, str(HELPER), *args],
        input=stdin_data,
        capture_output=True, text=True,
        cwd=str(SKILL_DIR),
        timeout=15,
    )


# ---------------------------------------------------------------------------
# TestHelperExists
# ---------------------------------------------------------------------------


class TestHelperExists:
    """The helper script exists and is importable."""

    def test_helper_file_exists(self) -> None:
        """bin/lib/validate-rules-helper.py must exist on disk."""
        assert HELPER.exists(), (
            f"{HELPER} does not exist -- Item 4b requires this script"
        )

    def test_helper_is_valid_python(self) -> None:
        """validate-rules-helper.py contains valid Python syntax."""
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", str(HELPER)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, (
            f"Syntax error in {HELPER}: {result.stderr}"
        )


# ---------------------------------------------------------------------------
# TestHelperPreAnalyzeOutput
# ---------------------------------------------------------------------------


class TestHelperPreAnalyzeOutput:
    """Pre-analyze invocation emits the expected JSON blob."""

    def test_pre_analyze_output_is_valid_json(self) -> None:
        """Helper pre-analyze mode produces valid JSON on stdout."""
        result = _run_helper("pre-analyze", "--session-id", "test-helper-pre-001",
                             "--file", "/tmp/test.py")
        assert result.returncode == 0, f"Helper exited non-zero: {result.stderr}"
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            pytest.fail(f"Helper output is not valid JSON: {e}\nstdout={result.stdout!r}")
        assert isinstance(data, dict)

    def test_pre_analyze_output_has_should_proceed(self) -> None:
        """Pre-analyze output includes should_proceed boolean."""
        result = _run_helper("pre-analyze", "--session-id", "test-helper-pre-002",
                             "--file", "/tmp/test.py")
        data = json.loads(result.stdout)
        assert "should_proceed" in data, (
            f"Pre-analyze output must include 'should_proceed'; got {data!r}"
        )
        assert isinstance(data["should_proceed"], bool)

    def test_pre_analyze_output_has_context(self) -> None:
        """Pre-analyze output includes context string (<lang> <fw> <role> format)."""
        result = _run_helper("pre-analyze", "--session-id", "test-helper-pre-003",
                             "--file", "/tmp/server.py")
        data = json.loads(result.stdout)
        assert "context" in data, f"Pre-analyze output must include 'context'; got {data!r}"
        assert isinstance(data["context"], str)

    def test_pre_analyze_output_has_phase(self) -> None:
        """Pre-analyze output includes phase string."""
        result = _run_helper("pre-analyze", "--session-id", "test-helper-pre-004",
                             "--file", "/tmp/test.py")
        data = json.loads(result.stdout)
        assert "phase" in data, f"Pre-analyze output must include 'phase'; got {data!r}"
        valid_phases = {"planning", "code_generation", "testing"}
        assert data["phase"] in valid_phases, (
            f"phase must be one of {valid_phases}; got {data['phase']!r}"
        )

    def test_pre_analyze_output_has_boundary_mode(self) -> None:
        """Pre-analyze output includes boundary_mode string."""
        result = _run_helper("pre-analyze", "--session-id", "test-helper-pre-005",
                             "--file", "/tmp/test.py")
        data = json.loads(result.stdout)
        assert "boundary_mode" in data, (
            f"Pre-analyze output must include 'boundary_mode'; got {data!r}"
        )

    def test_pre_analyze_output_has_plan_file(self) -> None:
        """Pre-analyze output includes plan_file path (may be empty string)."""
        result = _run_helper("pre-analyze", "--session-id", "test-helper-pre-006",
                             "--file", "/tmp/test.py")
        data = json.loads(result.stdout)
        assert "plan_file" in data, (
            f"Pre-analyze output must include 'plan_file'; got {data!r}"
        )


# ---------------------------------------------------------------------------
# TestDerivePhase (Part 2, capability 16)
# ---------------------------------------------------------------------------


class TestDerivePhase:
    """Capability 16: `_derive_phase` takes the session id and reports THIS session's
    own phase, never a sibling's.

    RED reason: `_derive_phase(project_root)` (validate-rules-helper.py:111-120) today
    takes ONE argument and checks the flat `<project_root>/.claude/gates/phase-a.approved`
    path. Every call below passes a second, session_id argument the current signature
    does not accept, so they fail with a TypeError before the assertion is even reached
    -- the correct RED for a capability whose signature has not changed yet.
    """

    def test_returns_planning_with_no_artifact_at_all(self, tmp_path) -> None:
        mod = _load_helper()
        assert mod._derive_phase(str(tmp_path), "any-session") == "planning"

    def test_returns_planning_for_a_session_with_no_artifact_of_its_own(
        self, tmp_path
    ) -> None:
        """The exact capability wording: a sibling session directory holds
        `phase-a.approved`, but the session actually asking has no artifact of its own,
        so the honest answer is still `planning`.
        """
        mod = _load_helper()
        sibling_dir = tmp_path / ".claude" / "gates" / "sibling-session"
        sibling_dir.mkdir(parents=True)
        (sibling_dir / "phase-a.approved").write_text("sibling-session\n")

        assert mod._derive_phase(str(tmp_path), "asking-session") == "planning", (
            "a sibling session's approval was read as this session's own"
        )

    def test_returns_code_generation_for_its_own_approved_phase_a(self, tmp_path) -> None:
        mod = _load_helper()
        own_dir = tmp_path / ".claude" / "gates" / "asking-session"
        own_dir.mkdir(parents=True)
        (own_dir / "phase-a.approved").write_text("asking-session\n")

        assert mod._derive_phase(str(tmp_path), "asking-session") == "code_generation"

    def test_returns_testing_once_both_of_its_own_gates_are_approved(
        self, tmp_path
    ) -> None:
        mod = _load_helper()
        own_dir = tmp_path / ".claude" / "gates" / "asking-session"
        own_dir.mkdir(parents=True)
        (own_dir / "phase-a.approved").write_text("asking-session\n")
        (own_dir / "test-skeletons.approved").write_text("asking-session\n")

        assert mod._derive_phase(str(tmp_path), "asking-session") == "testing"

    def test_a_sibling_reaching_testing_does_not_advance_this_session(
        self, tmp_path
    ) -> None:
        """Anti-vacuity companion to the sibling test above: even a FULLY advanced
        sibling (both gates approved) must not move this session past `planning`."""
        mod = _load_helper()
        sibling_dir = tmp_path / ".claude" / "gates" / "sibling-session"
        sibling_dir.mkdir(parents=True)
        (sibling_dir / "phase-a.approved").write_text("sibling-session\n")
        (sibling_dir / "test-skeletons.approved").write_text("sibling-session\n")

        assert mod._derive_phase(str(tmp_path), "asking-session") == "planning"


class TestPreAnalyzePhaseIsSessionScoped:
    """The same capability, through the real `pre-analyze` CLI subprocess
    `validate-rules.sh` actually invokes with `--session-id` (plan.md's own annotation:
    "already supplied by validate-rules.sh as --session-id"). No mock of the helper's
    internals -- a real process reads a real project directory.
    """

    def test_pre_analyze_phase_ignores_a_sibling_sessions_approval(self, tmp_path) -> None:
        """RED reason: `cmd_pre_analyze` calls `_derive_phase(project_root)` today with
        no session argument at all, so the CLI's own `--session-id` value never reaches
        the phase derivation -- a sibling's flat-looking approval (seeded here under the
        FUTURE session-scoped shape) is invisible to it either way, but so is a sibling's
        approval seeded at the CURRENT flat path, which today's code WOULD wrongly honor.
        This test seeds the flat path (today's real writer shape) to reproduce that
        exact misreport.
        """
        gates = tmp_path / ".claude" / "gates"
        gates.mkdir(parents=True)
        (gates / "phase-a.approved").write_text("some-other-session\n")

        result = _run_helper(
            "pre-analyze", "--session-id", "asking-session-cli",
            "--file", str(tmp_path / "service.py"),
            "--project-root", str(tmp_path),
        )
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["phase"] == "planning", (
            f"the CLI reported a sibling session's flat approval as this session's own "
            f"progress: {data!r}"
        )
