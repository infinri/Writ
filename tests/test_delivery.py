"""Task #7A: classify_delivery -- single source of the verified delivery rule.

Encodes the CC 2.1.183 contract (docs/reference/claude-code-blackbox.md): hook output reaches
the model only via additionalContext / permissionDecisionReason (any event) or
plain stdout on UserPromptSubmit / UserPromptExpansion / SessionStart.
"""
from __future__ import annotations

from writ.shared.delivery import (
    MODEL, DEBUG_LOG, USER, STATE, UNKNOWN,
    STDOUT_TO_MODEL_EVENTS, classify_delivery, reaches_model,
)


class TestClassifyDelivery:
    def test_additional_context_reaches_model_on_any_event(self) -> None:
        for ev in ("PreToolUse", "PostToolUse", "Stop", "SubagentStart", "Foo"):
            assert classify_delivery(ev, "additionalContext") == MODEL

    def test_permission_decision_reason_reaches_model(self) -> None:
        assert classify_delivery("PreToolUse", "permissionDecisionReason") == MODEL

    def test_stdout_reaches_model_only_on_special_events(self) -> None:
        for ev in STDOUT_TO_MODEL_EVENTS:
            assert classify_delivery(ev, "stdout") == MODEL

    def test_stdout_is_debug_log_on_non_special_events(self) -> None:
        for ev in ("PreToolUse", "PostToolUse", "Stop", "PostCompact", "SubagentStop"):
            assert classify_delivery(ev, "stdout") == DEBUG_LOG

    def test_system_message_goes_to_user(self) -> None:
        assert classify_delivery("PostToolUse", "systemMessage") == USER

    def test_state_mechanism(self) -> None:
        assert classify_delivery("PreToolUse", "state") == STATE

    def test_empty_mechanism_is_unknown_not_model(self) -> None:
        # Pre-instrument events lack a mechanism; must never be over-credited.
        assert classify_delivery("UserPromptSubmit", None) == UNKNOWN
        assert classify_delivery("PreToolUse", "") == UNKNOWN

    def test_unknown_mechanism_is_unknown(self) -> None:
        assert classify_delivery("PreToolUse", "telepathy") == UNKNOWN

    def test_reaches_model_helper(self) -> None:
        assert reaches_model("PostToolUse", "additionalContext") is True
        assert reaches_model("PreToolUse", "stdout") is False
        assert reaches_model("UserPromptSubmit", "stdout") is True


class TestAcceptanceKnownCases:
    """The exact cases from the #7 plan, tied to observed hooks."""

    def test_read_rag_pretooluse_stdout_is_inert(self) -> None:
        assert classify_delivery("PreToolUse", "stdout") == DEBUG_LOG

    def test_posttool_additional_context_is_model(self) -> None:
        assert classify_delivery("PostToolUse", "additionalContext") == MODEL

    def test_rag_inject_userpromptsubmit_stdout_is_model(self) -> None:
        assert classify_delivery("UserPromptSubmit", "stdout") == MODEL

    def test_deny_permission_reason_is_model(self) -> None:
        assert classify_delivery("PreToolUse", "permissionDecisionReason") == MODEL
