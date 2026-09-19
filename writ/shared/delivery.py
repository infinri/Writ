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

import json
from pathlib import Path

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

# The two EXIT-CODE mechanisms. The table had no term for an exit code at all, so the
# single most important question about a refusal (what the harness does with a Stop hook that
# exits non-zero) could not even be asked in the vocabulary the linter and the
# friction analyzer already share. Both classify as USER rather than MODEL: a hook's stderr
# on a non-zero exit is surfaced by the harness, and no captured record shows it arriving in
# the model's context. That is why delivery_provenance exists beside this: the answer here
# is the best reading of the documented contract, and until a captured record backs it, an
# entry is `unproven` and the drill reports it instead of asserting it.
EXIT_MECHANISMS = frozenset({"exit2_stderr", "exit_nonzero_stderr"})

# The checked-in census artifact that substantiates a provenance claim. Resolved from the
# skill root because this module lives at writ/shared/, two levels below it. Read FRESH on
# every delivery_provenance call, never bound at import: a caller (and the drill) must be
# able to point this at a different artifact, and a regenerated artifact must take effect
# without restarting the daemon.
CENSUS_PATH = (
    Path(__file__).resolve().parent.parent.parent / "docs" / "reference" / "blackbox-census.json"
)

# Provenance verdicts: the possible return values of delivery_provenance.
OBSERVED = "observed"
UNPROVEN = "unproven"


def classify_delivery(event_name: str | None, mechanism: str | None) -> str:
    """Where does a hook's emitted output go?

    Returns one of: "model" | "debug-log" | "rejected" | "user" | "state" | "unknown".

    event_name: the CC hook event (UserPromptSubmit, PreToolUse, PostToolUse,
        Stop, SubagentStart, PostCompact, ...).
    mechanism: how the hook emitted -- one of {additionalContext,
        permissionDecisionReason, stdout, systemMessage, state, exit2_stderr,
        exit_nonzero_stderr}.

    An empty/unknown mechanism returns "unknown" (never "model"), so events
    logged before this telemetry existed are never over-credited as having
    reached the model.

    Every answer for a mechanism that existed before the two exit-code names is
    UNCHANGED, by construction: the new arm is a membership test on EXIT_MECHANISMS
    added ahead of the final "unknown", and no existing branch is touched.
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
    if m in EXIT_MECHANISMS:
        return USER
    return UNKNOWN


def reaches_model(event_name: str | None, mechanism: str | None) -> bool:
    """True iff classify_delivery says this output reaches the model."""
    return classify_delivery(event_name, mechanism) == MODEL


def _census_record_classes() -> dict:
    """The census artifact's per-(event, hook, direction) record classes, or {}.

    CENSUS_PATH is read through the module attribute on every call, so a caller that
    repoints it is honoured. An absent, unreadable or malformed artifact yields {},
    which makes every entry unproven, because absence of evidence is never evidence.

    ONE key, `records`, which is what `writ.analysis.blackbox.build_census` writes.
    An earlier version also accepted `events` because a test fixture used that name;
    accepting two spellings for one artifact means a typo in either producer reads as
    an empty census, which tags genuinely observed entries unproven and looks like
    missing evidence rather than a broken reader. The fixture was corrected instead.
    """
    try:
        data = json.loads(Path(CENSUS_PATH).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    classes = data.get("records")
    return classes if isinstance(classes, dict) else {}


def delivery_provenance(event_name: str | None, mechanism: str | None) -> str:
    """Is this (event, mechanism) classification backed by a CAPTURED record?

    Returns "observed" only when the census artifact holds at least one record class
    for `event_name` whose observed mechanisms include `mechanism`; "unproven"
    otherwise. This is the difference between a measured fact and a documentation
    claim, which classify_delivery alone cannot express: its answers are flat, so a
    consumer could not tell the two apart and the drill would assert doc prose.

    A record class is keyed "<event>|<hook>|<direction>" and carries a `mechanisms`
    collection; membership works for both the list the artifact's fixtures use and the
    mechanism-to-count mapping the writer produces.
    """
    ev = (event_name or "").strip()
    m = (mechanism or "").strip()
    if not ev or not m:
        return UNPROVEN
    for key, entry in _census_record_classes().items():
        if not isinstance(entry, dict):
            continue
        if str(key).split("|")[0] != ev and str(entry.get("event") or "") != ev:
            continue
        if not entry.get("count"):
            continue
        mechanisms = entry.get("mechanisms") or ()
        if m in mechanisms:
            return OBSERVED
    return UNPROVEN
