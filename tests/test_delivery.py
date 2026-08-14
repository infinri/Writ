"""Task #7A: classify_delivery -- single source of the verified delivery rule.

Encodes the CC 2.1.183 contract (docs/reference/claude-code-blackbox.md): hook output reaches
the model only via additionalContext (on the events whose hookSpecificOutput schema accepts
it) / permissionDecisionReason (any event) or plain stdout on UserPromptSubmit /
UserPromptExpansion / SessionStart.

Cycle G correction: a real /compact on 2026-08-14 showed CC's hook-output validator reject
writ-postcompact.sh's hookSpecificOutput.additionalContext reply outright ("(root): Invalid
input") -- PostCompact is not an accepted hookEventName variant for that shape. The prior
"additionalContext reaches the model on ANY event" claim (and this file's test of it) is
exactly what that rejection falsifies. additionalContext now only credits the observed-valid
ADDITIONAL_CONTEXT_EVENTS; everything else (PostCompact, PreCompact) lands in the new REJECTED
bucket, which INERT_DELIVERIES groups with DEBUG_LOG for friction accounting.
"""
from __future__ import annotations

from writ.shared.delivery import (
    MODEL, DEBUG_LOG, USER, STATE, UNKNOWN, REJECTED,
    STDOUT_TO_MODEL_EVENTS, ADDITIONAL_CONTEXT_EVENTS, INERT_DELIVERIES,
    classify_delivery, reaches_model,
)


class TestClassifyDelivery:
    def test_additional_context_reaches_model_on_member_events(self) -> None:
        # The observed-valid variants from the 2026-08-14 validator error, plus the two
        # documented-but-unobserved events (SessionStart, Setup) the plan includes rather
        # than risk a false linter finding on a documented event.
        for ev in ADDITIONAL_CONTEXT_EVENTS:
            assert classify_delivery(ev, "additionalContext") == MODEL, (
                f"additionalContext must reach the model on {ev!r} (a member of "
                "ADDITIONAL_CONTEXT_EVENTS)"
            )

    def test_additional_context_is_rejected_on_postcompact_and_precompact(self) -> None:
        # The exact defect this cycle fixes: CC's validator rejects PostCompact's
        # hookSpecificOutput.additionalContext outright, and PreCompact has no model
        # channel at all. Neither is in ADDITIONAL_CONTEXT_EVENTS.
        assert classify_delivery("PostCompact", "additionalContext") == REJECTED
        assert classify_delivery("PreCompact", "additionalContext") == REJECTED

    def test_additional_context_is_rejected_on_an_arbitrary_non_member_event(self) -> None:
        # Non-membership is the rule, not an allowlist of two named exceptions: any event
        # outside ADDITIONAL_CONTEXT_EVENTS is REJECTED, this hook payload's schema was
        # never validated against.
        assert classify_delivery("Foo", "additionalContext") == REJECTED

    def test_permission_decision_reason_reaches_model(self) -> None:
        assert classify_delivery("PreToolUse", "permissionDecisionReason") == MODEL

    def test_permission_decision_reason_reaches_model_on_any_event(self) -> None:
        # Unlike additionalContext, permissionDecisionReason's "any event" credit is
        # unchanged by this cycle -- pinned explicitly so a future regression that widens
        # the additionalContext fix onto this mechanism is caught here.
        for ev in ("PreToolUse", "PostCompact", "PreCompact", "Foo"):
            assert classify_delivery(ev, "permissionDecisionReason") == MODEL

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

    def test_reaches_model_helper_is_false_for_rejected_additional_context(self) -> None:
        # reaches_model is a thin == MODEL check; pinned separately because a helper
        # that special-cased REJECTED as truthy would be the exact regression this
        # cycle exists to prevent.
        assert reaches_model("PostCompact", "additionalContext") is False
        assert reaches_model("PreCompact", "additionalContext") is False


class TestAdditionalContextEventsContract:
    """ADDITIONAL_CONTEXT_EVENTS is the exact set from the plan: the seven
    observed-valid variants from the 2026-08-14 validator error, plus SessionStart
    and Setup (documented at claude-code-blackbox.md but not independently observed
    that day -- excluding a documented event would produce a false linter finding,
    the more expensive error per the plan)."""

    def test_membership_matches_the_plan_exactly(self) -> None:
        assert ADDITIONAL_CONTEXT_EVENTS == frozenset({
            "PreToolUse", "PostToolUse", "PostToolBatch", "UserPromptSubmit",
            "Stop", "SubagentStop", "SubagentStart", "SessionStart", "Setup",
        })

    def test_postcompact_and_precompact_are_not_members(self) -> None:
        assert "PostCompact" not in ADDITIONAL_CONTEXT_EVENTS
        assert "PreCompact" not in ADDITIONAL_CONTEXT_EVENTS


class TestInertDeliveries:
    """INERT_DELIVERIES lets friction.py count rejected output as inert alongside
    debug-log, without conflating the two failure modes (debug-log was accepted and
    filed; rejected was thrown away with a validator error)."""

    def test_inert_deliveries_is_debug_log_and_rejected(self) -> None:
        assert INERT_DELIVERIES == frozenset({DEBUG_LOG, REJECTED})

    def test_model_user_state_unknown_are_not_inert(self) -> None:
        assert INERT_DELIVERIES.isdisjoint({MODEL, USER, STATE, UNKNOWN})


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

    def test_postcompact_additional_context_is_rejected_not_model(self) -> None:
        # The exact case from this cycle's plan: the 2026-08-14 /compact validator
        # error ("(root): Invalid input") on writ-postcompact.sh's
        # hookSpecificOutput.additionalContext reply.
        assert classify_delivery("PostCompact", "additionalContext") == REJECTED
