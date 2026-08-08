#!/usr/bin/env bash
# Stop hook: enforce pending violations in Work mode.
# Exit codes: 0=pass, 2=blocking (unresolved violations in Work mode)
#
# In Work mode, if pending_violations is non-empty at the end of a turn,
# exit 2 forces Claude to continue and address the violations.
# Non-work modes and empty violations always exit 0.
#
# Hook type: Stop
# Depends on: writ-session.py, bin/lib/common.sh

set -euo pipefail

SKILL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
source "$SKILL_DIR/bin/lib/common.sh"

# Loop-breaker (CC Stop-hook contract): if CC is re-invoking us after a prior
# block, stop_hook_active is true -- allow the stop now (the first Stop already
# surfaced the violations via exit 2). Re-blocking here loops to CC's block cap.
STDIN_JSON=$(cat 2>/dev/null || echo '{}')
if stop_hook_active "$STDIN_JSON"; then
    exit 0
fi

HOOK_START_NS=$(hook_timer_start)

# Session identity comes from the payload, with NO fallback.
#
# This read WRIT_SESSION_ID first (an env var Claude Code never sets, so it was always
# empty) and then /tmp/writ-current-session, making the global pointer the effective
# primary. That pointer names whichever Claude Code session on this machine took a turn
# most recently, so this hook could enforce one session's violations against another, or
# clear them there. Enforcement aimed at the wrong session is a governance failure, not a
# logging one.
SESSION_ID=$(writ_require_session "$STDIN_JSON" enforce-violations) || exit 0
if [ -z "$SESSION_ID" ]; then
    exit 0
fi

# Read current mode
MODE=$(_writ_session "mode get" "$SESSION_ID" 2>/dev/null || echo "")
MODE=$(echo "$MODE" | tr -d '[:space:]')

# Only enforce in Work mode
if [ "$MODE" != "work" ]; then
    hook_timer_end "$HOOK_START_NS" "enforce-violations" "$SESSION_ID" "$MODE"
    exit 0
fi

# Read session cache and extract pending_violations
CACHE=$(_writ_session read "$SESSION_ID" 2>/dev/null || echo '{"pending_violations":[]}')

VIOLATION_COUNT=$(echo "$CACHE" | python3 -c "
import sys, json
cache = json.load(sys.stdin)
violations = cache.get('pending_violations', [])
print(len(violations))
" 2>/dev/null || echo "0")

if [ "$VIOLATION_COUNT" -gt 0 ]; then
    # Extract rule IDs for the message
    RULE_IDS=$(echo "$CACHE" | python3 -c "
import sys, json
cache = json.load(sys.stdin)
violations = cache.get('pending_violations', [])
ids = [v.get('rule_id', 'unknown') for v in violations]
print(', '.join(ids))
" 2>/dev/null || echo "unknown")

    hook_timer_end "$HOOK_START_NS" "enforce-violations" "$SESSION_ID" "$MODE"
    echo "You have $VIOLATION_COUNT unresolved violations: [$RULE_IDS]. Fix these before completing." >&2
    exit 2
fi

hook_timer_end "$HOOK_START_NS" "enforce-violations" "$SESSION_ID" "$MODE"
exit 0
