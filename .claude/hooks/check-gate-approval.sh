#!/bin/bash
# Gate sequencing hook -- thin client for writ-session.py can-write.
# PreToolUse: fires before every Write/Edit/MultiEdit.
#
# Phase 3: all file classification, gate checking, and tier routing
# is handled by writ-session.py can-write. This hook pipes the raw
# stdin envelope and enforces the allow/deny result.

SKILL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
SESSION_HELPER="$SKILL_DIR/bin/lib/writ-session.py"
source "$SKILL_DIR/bin/lib/common.sh"

HOOK_START_NS=$(hook_timer_start)

# Read stdin once and tee it -- we need it for both session ID and can-write
STDIN_DATA=$(cat)
SESSION_ID=$(echo "$STDIN_DATA" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('agent_id','') or d.get('session_id',''))" 2>/dev/null)

if [ -z "$SESSION_ID" ]; then
    SESSION_ID=$(detect_session_id "")
fi

RESULT=$(echo "$STDIN_DATA" | _writ_session can-write "$SESSION_ID" --skill-dir "$SKILL_DIR" 2>/dev/null)

DECISION=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('decision','allow'))" 2>/dev/null || echo "allow")

if [ "$DECISION" = "deny" ]; then
    # Read denial count from can-write result (set by _log_gate_denial in writ-session.py)
    DENIAL_COUNT=$(_writ_session read "$SESSION_ID" 2>/dev/null | python3 -c "
import sys, json
cache = json.load(sys.stdin)
counts = cache.get('denial_counts', {})
# Find the highest count across all gates
print(max(counts.values()) if counts else 1)
" 2>/dev/null || echo "1")

    REASON=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('reason','Gate approval required'))" 2>/dev/null || echo "Gate approval required")

    if [ "$DENIAL_COUNT" -ge 2 ] 2>/dev/null; then
        # Escalation: force human intervention via ask
        python3 -c "
import json, sys
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'ask',
        'permissionDecisionReason': '[Writ: repeated gate violation #' + sys.argv[2] + '] ' + sys.argv[1]
    }
}))
" "$REASON" "$DENIAL_COUNT"
    else
        # First denial: deny with additionalContext explaining the workflow
        python3 -c "
import json, sys
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': sys.argv[1],
        'additionalContext': 'IMPORTANT: This write was denied by a Writ gate. Do NOT attempt more writes to other files -- the denial applies to ALL files until the gate advances. Read the denial reason and follow the workflow: present your work to the user and wait for approval.'
    }
}))
" "$REASON"
    fi
fi

hook_timer_end "$HOOK_START_NS" "check-gate-approval" "$SESSION_ID" ""
exit 0
