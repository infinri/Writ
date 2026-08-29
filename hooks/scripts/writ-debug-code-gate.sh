#!/bin/bash
# INV-9: defer code-reading until runtime evidence -- PreToolUse gate.
#
# In the runtime (debug) lens, blocks code search/reading until debug.md has
# Evidence + Narrowing content (DEBUG-MODE-PROPOSAL.md line 126, hook #2). Reading
# debug.md / logs / non-code stays allowed; runtime data via Bash is untouched.
# Decision is computed by `writ-session.py can-read-code` (fail-open). Emits a deny
# permissionDecision only when that check says deny.
#
# Hook type: PreToolUse (matcher: Grep|Read). Exit: always 0.
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
SESSION_HELPER="$SKILL_DIR/bin/lib/writ-session.py"
# This gate did not previously source common.sh; it is sourced here solely for
# hook_instrument / log_gate_decision. Guarded so a missing common.sh degrades to
# an uninstrumented (but still working) gate rather than breaking the run.
source "$SKILL_DIR/bin/lib/common.sh" 2>/dev/null || true
type hook_instrument >/dev/null 2>&1 && hook_instrument "writ-debug-code-gate"

STDIN_DATA=$(cat)

SID=$(printf '%s' "$STDIN_DATA" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print(''); sys.exit(0)
print((d.get('agent_id') or d.get('session_id') or '').strip())
" 2>/dev/null || echo "")

# THIS GATE'S OWN TELEMETRY IS KEYED HERE.
#
# hook_instrument's exit trap and log_gate_decision both file their rows under
# `${SESSION_ID:-${HOOK_SESSION_ID:-}}`, and this gate set neither: load_hook_env is
# unusable here because it reads stdin, which $STDIN_DATA has already consumed. So every
# row this hook produced landed under the literal session id "unknown" -- measured
# 2026-08-08, writ-events-unknown.buf held ~153 hook_execution rows dominated by this
# gate, none of them attributable to the session that produced them.
#
# SID is the payload's identity and only the payload's (agent_id first, so a sub-agent's
# reads are not filed under its parent). It is never synthesized, so an id the payload
# did not carry stays empty and the hook no-ops below rather than inventing one.
SESSION_ID="$SID"

[ -n "$SID" ] || exit 0

# THE EXPENSIVE CHECK IS SKIPPED WHEN THE LENS PROVABLY CANNOT DENY.
#
# `can-read-code` pays a python interpreter start plus a `writ` package import on EVERY
# Read, Grep and Glob, and the runtime lens can only deny when the session's effective
# source type is "runtime". writ_runtime_lens_check_required answers that question from
# the session cache in one jq process, and answers "required" for every state it cannot
# resolve, so uncertainty pays the full cost and never grants a read.
#
# THE `type` GUARD IS NOT DECORATION. An undefined function means common.sh did not
# source, and without the guard `set -e` would abort the hook here, before it could
# refuse anything. With it, a missing library falls through to the expensive path and
# to today's behaviour, including today's ability to deny.
#
# THE ALLOW ROW IS STILL WRITTEN HERE. A gate that gets faster by disappearing from the
# audit stream is exactly the regression the decision record below exists to prevent,
# so this arm logs the same gate name, decision and target the un-skipped allow arm
# logs (tests/test_debug_lens_predicate.py pins the two rows equal).
if type writ_runtime_lens_check_required >/dev/null 2>&1 \
    && ! writ_runtime_lens_check_required "$SID"; then
    log_gate_decision "debug-code-read" "allow" "" "${SID}"
    exit 0
fi

DECISION_JSON=$(printf '%s' "$STDIN_DATA" \
    | python3 "$SESSION_HELPER" can-read-code "$SID" --skill-dir "$SKILL_DIR" 2>/dev/null || echo "")
[ -n "$DECISION_JSON" ] || exit 0

# ONE PARSE FOR BOTH FIELDS, and it happens BEFORE the emitter below so the emitter's
# interpreter start is paid only when the gate actually refuses. Two inline `python3 -c`
# parses of the same small document became one `parsed_fields` call, whose semantics are
# parsed_field's field for field: absent and null both give "", which is what the
# `.get('decision')` / `.get('reason') or ''` arms they replaced produced.
#
# BOTH VARIABLES ARE INITIALIZED FIRST. An unparseable document makes parsed_fields
# print nothing at all, and `set -u` would then abort the hook on an unset variable.
GATE_DECISION=""
GATE_REASON=""
eval "$(parsed_fields "$DECISION_JSON" GATE_DECISION=decision GATE_REASON=reason)"

# The emitter below runs unless the parse positively said "allow". Testing for "not
# allow" rather than "is deny" holds the refusal to the same fail direction the
# predicate above uses: if common.sh never sourced, or the document did not parse,
# GATE_DECISION is "" and the emitter still runs and still decides for itself from the
# same JSON, exactly as it did when it ran unconditionally. The block itself is
# unchanged; it is only reached less often.
if [ "$GATE_DECISION" != "allow" ]; then
printf '%s' "$DECISION_JSON" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if d.get('decision') != 'deny':
    sys.exit(0)
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': d.get('reason') or 'Code reading blocked: gather runtime evidence first.',
        'additionalContext': 'Runtime (debug) lens: read debug.md / logs / non-code and gather runtime evidence via Bash first, record Evidence + Narrowing in debug.md, then read code.',
    }
}))
" 2>/dev/null || true
fi

# Decision record on BOTH branches, including the skip arm above. The `decision` field
# is read back out of the same DECISION_JSON the block above acted on, so the record can
# never disagree with what the gate actually did. Anything that is not exactly `deny` is
# an allow, which reproduces the `except: print('allow')` arm this replaced.
[ "$GATE_DECISION" = "deny" ] || GATE_DECISION="allow"
log_gate_decision "debug-code-read" "$GATE_DECISION" "$GATE_REASON" "${SID}"

exit 0
