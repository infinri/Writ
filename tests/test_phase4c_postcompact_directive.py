"""Phase 4c D3: writ-postcompact.sh emits verify-discipline directive.

PSR-004 finding: after /compact, the model treats recalled verification
output (e.g. "last run was 6 tests, 13 assertions, all passing") as
fresh evidence. The architectural defense is to make the existing
PostCompact hook emit a directive into the next-turn context that
forces a re-verify mindset.

Cycle B correction: bare stdout on PostCompact reaches only the Claude
Code debug log (writ/shared/delivery.py::STDOUT_TO_MODEL_EVENTS lists
only UserPromptSubmit, UserPromptExpansion, SessionStart). Reading raw
stdout is what let the directive sit on a dead channel while these
tests stayed green -- it only ever looked correct because a human-typed
/compact echoes hook stdout into the transcript via an incidental
<local-command-stdout> channel that does not exist for an automatic
compaction. Tests now go through hookSpecificOutput.additionalContext,
the channel documented to reach the model on any event.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

WRIT_ROOT = Path(__file__).resolve().parent.parent
HOOK = WRIT_ROOT / "hooks" / "scripts" / "writ-postcompact.sh"


def _run_hook(stdin_json: dict) -> tuple[str, str, int]:
    proc = subprocess.run(
        [str(HOOK)],
        input=json.dumps(stdin_json),
        capture_output=True, text=True,
        cwd=str(WRIT_ROOT),
    )
    return proc.stdout, proc.stderr, proc.returncode


def _additional_context(stdin_json: dict) -> str:
    """The hook's model-facing payload: the additionalContext field, not stdout.

    Reading stdout directly is what let the directive sit on a dead channel while
    these tests stayed green -- bare stdout on PostCompact reaches the CC debug log
    only (writ/shared/delivery.py). Parsing here means a regression back to bare
    text fails on the json.loads, not on a phrase that happens to still be present.
    """
    stdout, _, code = _run_hook(stdin_json)
    assert code == 0, "PostCompact hook must exit 0"
    payload = json.loads(stdout)
    hso = payload["hookSpecificOutput"]
    assert hso["hookEventName"] == "PostCompact"
    return hso["additionalContext"]


class TestPostCompactDirective:
    """The hook emits a verify-discipline directive via additionalContext after compact."""

    def test_directive_delivered_via_additional_context(self) -> None:
        ctx = _additional_context({"session_id": "diag", "event": "compact"})
        assert ctx.strip(), "Hook must emit a non-empty directive via additionalContext"

    def test_directive_mentions_compaction(self) -> None:
        ctx = _additional_context({"session_id": "diag", "event": "compact"})
        assert "compact" in ctx.lower(), (
            "Directive must reference the compaction event so the model "
            "knows why this directive is firing"
        )

    def test_directive_mentions_recalled_or_second_hand_evidence(self) -> None:
        ctx = _additional_context({"session_id": "diag", "event": "compact"})
        signals = ["recalled", "second-hand", "second hand", "remembered", "pre-compact"]
        assert any(s in ctx.lower() for s in signals), (
            "Directive must signal that pre-compact memory is now "
            f"second-hand evidence (one of {signals!r})"
        )

    def test_directive_instructs_reverify(self) -> None:
        ctx = _additional_context({"session_id": "diag", "event": "compact"})
        signals = ["re-run", "rerun", "re-verify", "reverify", "verify"]
        assert any(s in ctx.lower() for s in signals), (
            f"Directive must instruct re-verification (one of {signals!r})"
        )

    def test_directive_handles_blocked_reverification(self) -> None:
        """PSR-004b finding: when re-run is rejected by tool permissions,
        the model must surface the gap, not collapse to 'yes'. The
        directive needs an explicit blocked-case clause."""
        ctx = _additional_context({"session_id": "diag", "event": "compact"})
        assert "blocked" in ctx.lower(), (
            "Directive must address the blocked/rejected re-verification "
            "case explicitly (PSR-004b regression)"
        )

    def test_directive_uses_stop_language(self) -> None:
        """The blocked case needs imperative STOP language so the model
        does not slide from 'blocked' into a confident affirmative."""
        ctx = _additional_context({"session_id": "diag", "event": "compact"})
        assert "STOP" in ctx, (
            "Directive must include STOP language for the blocked case "
            "to interrupt the rejection-as-confirmation reflex"
        )

    def test_directive_forbids_yes_without_evidence(self) -> None:
        """Explicitly forbidden response language (PSR-004b option-a fix)."""
        ctx = _additional_context({"session_id": "diag", "event": "compact"})
        assert "forbidden" in ctx.lower(), (
            "Directive must use 'forbidden' framing so the model recognizes "
            "answering 'yes' without re-verify as a hard rule, not advice"
        )

    def test_directive_mentions_fresh_evidence(self) -> None:
        """The directive must distinguish recalled output from fresh evidence."""
        ctx = _additional_context({"session_id": "diag", "event": "compact"})
        assert "fresh evidence" in ctx.lower(), (
            "Directive must contrast recalled output with 'fresh evidence' "
            "so the model knows what counts as a valid affirmative"
        )

    def test_stdout_is_exactly_one_json_object_with_no_text_outside_it(self) -> None:
        """The state line and the directive travel INSIDE additionalContext, not
        beside it: CC parses exactly one JSON object per invocation
        (hooks/scripts/inject-tier-workflow.sh:29), so any text before or after
        the object (a second print, a stray echo) either breaks the parse or is
        silently dropped."""
        stdout, _, code = _run_hook({"session_id": "diag", "event": "compact"})
        assert code == 0
        text = stdout.strip()
        obj, end = json.JSONDecoder().raw_decode(text)
        assert end == len(text), (
            "stdout must be exactly one JSON object with nothing outside it; "
            f"found trailing content: {text[end:]!r}"
        )
        assert "hookSpecificOutput" in obj


class TestHookExecutability:
    """The hook is executable and bash-syntax-valid."""

    def test_hook_exists(self) -> None:
        assert HOOK.exists(), f"{HOOK} does not exist"

    def test_hook_executable(self) -> None:
        import os
        assert os.access(HOOK, os.X_OK), f"{HOOK} is not executable"

    def test_hook_syntax(self) -> None:
        proc = subprocess.run(
            ["bash", "-n", str(HOOK)],
            capture_output=True, text=True,
        )
        assert proc.returncode == 0, f"Syntax error: {proc.stderr}"


class TestExistingBehaviorPreserved:
    """Adding the directive must not break existing PostCompact logic."""

    def test_hook_does_not_throw_on_minimal_input(self) -> None:
        """A minimal/empty stdin should not crash the hook."""
        stdout, stderr, code = _run_hook({})
        assert code == 0, (
            f"Hook must handle minimal input gracefully. stderr={stderr!r}"
        )

    def test_hook_does_not_emit_deny_decision(self) -> None:
        """Hook is informational, not blocking."""
        stdout, _, _ = _run_hook({"session_id": "diag", "event": "compact"})
        compact_stdout = stdout.replace(" ", "")
        assert '"permissionDecision":"deny"' not in compact_stdout
        assert '"permissionDecision": "deny"' not in stdout
