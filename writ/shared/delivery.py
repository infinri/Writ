"""Single source of truth for hook-output delivery classification (CC 2.1.183).

Encodes the verified delivery rule from docs/reference/claude-code-blackbox.md: which hook
outputs actually reach the MODEL versus landing in the CC debug log, going to the
user, or only mutating state. The friction analyzer (read-side telemetry) and the
static hooks linter both import this, so the rule lives in exactly ONE place and
stays re-runnable per CC version.

Verified empirically (session af5d7263): plain stdout reaches the model only on
UserPromptSubmit / UserPromptExpansion / SessionStart; on every other event it
goes to the CC debug log and the model never sees it. permissionDecisionReason
reaches the model on any event.

additionalContext is event-scoped, corrected 2026-08-14 (cycle G): a real /compact showed
CC's hook-output validator reject writ-postcompact.sh's
`{"hookSpecificOutput": {"hookEventName": "PostCompact", "additionalContext": ...}}` with
"(root): Invalid input" and discard the whole reply. The prior "any event" claim credited
that payload as delivered for two cycles. additionalContext now counts only on
ADDITIONAL_CONTEXT_EVENTS; on every other event it is REJECTED (thrown away with an error),
which is NOT the same failure as DEBUG_LOG (accepted and filed). INERT_DELIVERIES groups the
two for token accounting without conflating them.
"""

# Delivery buckets -- the possible return values of classify_delivery.
MODEL = "model"
DEBUG_LOG = "debug-log"
USER = "user"
STATE = "state"
UNKNOWN = "unknown"
# Emitted on an event whose hookSpecificOutput schema does not accept the mechanism: CC's
# validator errors and discards the ENTIRE hook reply, so nothing at all is delivered.
REJECTED = "rejected"

# The ONLY events on which plain stdout reaches the model. On every other event,
# plain stdout goes to the CC debug log (OBSERVED for UserPromptSubmit; [doc] for
# the other two). Keep this set authoritative -- it is the crux of the rule.
STDOUT_TO_MODEL_EVENTS = frozenset({
    "UserPromptSubmit",
    "UserPromptExpansion",
    "SessionStart",
})

# The events whose hookSpecificOutput schema accepts additionalContext. The first seven are
# the accepted hookEventName variants CC's own validator listed on 2026-08-14 when it
# rejected the PostCompact payload (SubagentStart independently probe-confirmed live the
# same day). SessionStart and Setup are on the documented listing
# (docs/reference/claude-code-blackbox.md) but were not in that error text; they are included
# because a false linter finding on a documented event is the more expensive error.
ADDITIONAL_CONTEXT_EVENTS = frozenset({
    "PreToolUse",
    "PostToolUse",
    "PostToolBatch",
    "UserPromptSubmit",
    "Stop",
    "SubagentStop",
    "SubagentStart",
    "SessionStart",
    "Setup",
})

# Buckets where the tokens were paid for and the model saw nothing. Read-side telemetry
# (writ.analysis.friction) sums these as inert; they stay separate buckets so the next
# instance of the rejected-payload bug is findable.
INERT_DELIVERIES = frozenset({DEBUG_LOG, REJECTED})


def classify_delivery(event_name: str | None, mechanism: str | None) -> str:
    """Where does a hook's emitted output go?

    Returns one of: "model" | "debug-log" | "rejected" | "user" | "state" | "unknown".

    event_name: the CC hook event (UserPromptSubmit, PreToolUse, PostToolUse,
        Stop, SubagentStart, PostCompact, ...).
    mechanism: how the hook emitted -- one of {additionalContext,
        permissionDecisionReason, stdout, systemMessage, state}.

    An empty/unknown mechanism returns "unknown" (never "model"), so events
    logged before this telemetry existed are never over-credited as having
    reached the model.
    """
    m = (mechanism or "").strip()
    ev = (event_name or "").strip()
    if not m:
        return UNKNOWN
    if m == "permissionDecisionReason":
        return MODEL
    if m == "additionalContext":
        # Non-membership is the rule, not an allowlist of named exceptions: an event this
        # payload's schema was never validated against is a rejection, not a delivery.
        return MODEL if ev in ADDITIONAL_CONTEXT_EVENTS else REJECTED
    if m == "systemMessage":
        return USER
    if m == "state":
        return STATE
    if m == "stdout":
        return MODEL if ev in STDOUT_TO_MODEL_EVENTS else DEBUG_LOG
    return UNKNOWN


def reaches_model(event_name: str | None, mechanism: str | None) -> bool:
    """True iff classify_delivery says this output reaches the model."""
    return classify_delivery(event_name, mechanism) == MODEL
