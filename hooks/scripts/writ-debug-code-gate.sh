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

DECISION_JSON=$(printf '%s' "$STDIN_DATA" \
    | python3 "$SESSION_HELPER" can-read-code "$SID" --skill-dir "$SKILL_DIR" 2>/dev/null || echo "")
[ -n "$DECISION_JSON" ] || exit 0

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

# Decision record on BOTH branches. The `decision` field is read back out of the
# same DECISION_JSON the block above acted on, so the record can never disagree
# with what the gate actually did.
GATE_DECISION=$(printf '%s' "$DECISION_JSON" | python3 -c "
import sys, json
try:
    print('deny' if json.load(sys.stdin).get('decision') == 'deny' else 'allow')
except Exception:
    print('allow')
" 2>/dev/null || echo "allow")
GATE_REASON=$(printf '%s' "$DECISION_JSON" | python3 -c "
import sys, json
try:
    print(json.load(sys.stdin).get('reason') or '')
except Exception:
    print('')
" 2>/dev/null || echo "")
log_gate_decision "debug-code-read" "$GATE_DECISION" "$GATE_REASON" "${SID}"

exit 0
