"""Phase 3b: auto-approve-gate.sh emits ask-prompt, never silently advances.

Plan Section 8.1: pattern-match 'approved' must NOT trigger a silent
advance. It emits a directive pointing the assistant at /writ-approve,
which performs the authoritative advance with confirmation_source="tool".
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

WRIT_ROOT = Path(__file__).resolve().parent.parent
HOOK = WRIT_ROOT / ".claude" / "hooks" / "auto-approve-gate.sh"


def _run_hook(prompt: str, session_id: str = "phase3b-test") -> tuple[str, int]:
    stdin = json.dumps({"session_id": session_id, "prompt": prompt})
    proc = subprocess.run(
        [str(HOOK)],
        input=stdin, capture_output=True, text=True,
        cwd=str(WRIT_ROOT),
    )
    return proc.stdout, proc.returncode


class TestApprovalPatternEmitsAskPrompt:
    """When an approval pattern matches, the hook emits a directive, not a silent advance."""

    def test_approved_emits_writ_approve_directive(self) -> None:
        stdout, code = _run_hook("approved")
        assert code == 0
        assert "/writ-approve" in stdout, "Hook must steer the assistant to /writ-approve"
        assert "[Writ: approval pattern detected]" in stdout

    def test_lgtm_emits_directive(self) -> None:
        stdout, code = _run_hook("lgtm")
        assert code == 0
        assert "/writ-approve" in stdout

    def test_proceed_emits_directive(self) -> None:
        stdout, code = _run_hook("proceed")
        assert code == 0
        assert "/writ-approve" in stdout


class TestApprovalPatternDoesNotSilentlyAdvance:
    """The hook must not output phase-advance confirmation strings itself."""

    @pytest.mark.parametrize("prompt", ["approved", "lgtm", "proceed", "yes", "go ahead"])
    def test_no_phase_advance_confirmation(self, prompt: str) -> None:
        stdout, _ = _run_hook(prompt)
        # The old silent-advance path printed these phrases.
        assert "Phase: testing" not in stdout
        assert "Phase: implementation" not in stdout
        assert "Gate approved:" not in stdout
        assert "Phase advanced." not in stdout


class TestNonApprovalNoDirective:
    """Prompts that aren't approvals don't get a directive."""

    @pytest.mark.parametrize("prompt", [
        "refactor the database module",
        "how do I fix this bug?",
        "where does this function go in the architecture?",
    ])
    def test_no_directive_on_non_approval(self, prompt: str) -> None:
        stdout, code = _run_hook(prompt)
        assert code == 0
        assert "approval pattern detected" not in stdout
        assert "/writ-approve" not in stdout


class TestHookExecutableAndValid:
    def test_hook_exists_and_executable(self) -> None:
        import os
        assert HOOK.exists()
        assert os.access(HOOK, os.X_OK)

    def test_hook_syntax(self) -> None:
        proc = subprocess.run(["bash", "-n", str(HOOK)], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr


class TestNoLegacySilentAdvanceCall:
    """The rewrapped hook must not call advance-phase directly.

    Defense-in-depth: pattern match signals intent but never performs the
    advance. The /writ-approve slash command is the only path.
    """

    def test_hook_does_not_call_advance_phase(self) -> None:
        content = HOOK.read_text()
        # The old hook had: _writ_session advance-phase "$SESSION_ID" --token
        # Phase 3b removes this. Grep for the pattern.
        pattern = re.compile(r"_writ_session\s+advance-phase", re.IGNORECASE)
        assert not pattern.search(content), (
            "auto-approve-gate.sh must NOT call advance-phase directly (plan Section 8.1). "
            "Use /writ-approve for tool-confirmed advance."
        )
