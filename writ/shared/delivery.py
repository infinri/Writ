"""Single source of truth for hook-output delivery classification (CC 2.1.183).

Encodes the verified delivery rule from docs/reference/claude-code-blackbox.md: which hook
outputs actually reach the MODEL versus landing in the CC debug log, going to the
user, or only mutating state. The friction analyzer (read-side telemetry) and the
static hooks linter both import this, so the rule lives in exactly ONE place and
stays re-runnable per CC version.

Verified empirically (session af5d7263): plain stdout reaches the model only on
UserPromptSubmit / UserPromptExpansion / SessionStart; on every other event it
goes to the CC debug log and the model never sees it. additionalContext and
permissionDecisionReason reach the model on any event.
"""

# Delivery buckets -- the possible return values of classify_delivery.
MODEL = "model"
DEBUG_LOG = "debug-log"
USER = "user"
STATE = "state"
UNKNOWN = "unknown"

# The ONLY events on which plain stdout reaches the model. On every other event,
# plain stdout goes to the CC debug log (OBSERVED for UserPromptSubmit; [doc] for
# the other two). Keep this set authoritative -- it is the crux of the rule.
STDOUT_TO_MODEL_EVENTS = frozenset({
    "UserPromptSubmit",
    "UserPromptExpansion",
    "SessionStart",
})

# Mechanisms that reach the model regardless of which event fired.
_MODEL_MECHANISMS = frozenset({"additionalContext", "permissionDecisionReason"})


def classify_delivery(event_name: str | None, mechanism: str | None) -> str:
    """Where does a hook's emitted output go?

    Returns one of: "model" | "debug-log" | "user" | "state" | "unknown".

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
    if m in _MODEL_MECHANISMS:
        return MODEL
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
