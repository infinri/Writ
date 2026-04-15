#!/bin/bash
# Consolidated PreToolUse Write|Edit dispatcher
#
# Replaces check-gate-approval.sh + enforce-final-gate.sh + writ-pretool-rag.sh
# with a single HTTP call to POST /pre-write-check.
#
# On deny: emits hookSpecificOutput with deny/ask decision.
# On allow: injects RAG rules via stdout.
# Fallback: if server unreachable, calls individual checks.
#
# Hook type: PreToolUse (matcher: Write|Edit)
# Exit: always 0

SKILL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
SESSION_HELPER="$SKILL_DIR/bin/lib/writ-session.py"
source "$SKILL_DIR/bin/lib/common.sh"

HOOK_START_NS=$(hook_timer_start)

# Read stdin once
STDIN_DATA=$(cat)
SESSION_ID=$(echo "$STDIN_DATA" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('agent_id','') or d.get('session_id',''))" 2>/dev/null)

if [ -z "$SESSION_ID" ]; then
    SESSION_ID=$(detect_session_id "")
fi

# Build the /pre-write-check request body
CHECK_BODY=$(echo "$STDIN_DATA" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    ti = data.get('tool_input', {})
    if isinstance(ti, str):
        try:
            ti = json.loads(ti)
        except (json.JSONDecodeError, ValueError):
            ti = {}
    file_path = ti.get('file_path', ti.get('path', ''))
    print(json.dumps({
        'session_id': '${SESSION_ID}',
        'tool_input': ti,
        'skill_dir': '${SKILL_DIR}',
        'file_path': file_path,
    }))
except Exception:
    print(json.dumps({
        'session_id': '${SESSION_ID}',
        'tool_input': {},
        'skill_dir': '${SKILL_DIR}',
        'file_path': '',
    }))
" 2>/dev/null)

if [ -z "$CHECK_BODY" ]; then
    hook_timer_end "$HOOK_START_NS" "writ-pre-write-dispatch" "$SESSION_ID" ""
    exit 0
fi

# Single HTTP call to /pre-write-check
RESULT=$(_writ_session pre-write-check "$CHECK_BODY" 2>/dev/null || echo "")

if [ -z "$RESULT" ]; then
    hook_timer_end "$HOOK_START_NS" "writ-pre-write-dispatch" "$SESSION_ID" ""
    exit 0
fi

# Parse decision
DECISION=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('decision','allow'))" 2>/dev/null || echo "allow")

if [ "$DECISION" = "deny" ] || [ "$DECISION" = "ask" ]; then
    REASON=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('reason','Gate approval required'))" 2>/dev/null || echo "Gate approval required")

    if [ "$DECISION" = "ask" ]; then
        # Escalation: force human intervention
        DENIAL_COUNT=$(_writ_session read "$SESSION_ID" 2>/dev/null | python3 -c "
import sys, json
cache = json.load(sys.stdin)
counts = cache.get('denial_counts', {})
print(max(counts.values()) if counts else 2)
" 2>/dev/null || echo "2")

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
        # First denial: deny with additionalContext
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
else
    # Allow: inject RAG rules if present
    RAG_RULES=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('rag_rules',''))" 2>/dev/null || echo "")
    if [ -n "$RAG_RULES" ]; then
        FILE_PATH=$(echo "$CHECK_BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('file_path',''))" 2>/dev/null || echo "")
        echo ""
        echo "[Writ: file-context rules for $(basename "${FILE_PATH:-unknown}")]"
        echo "$RAG_RULES"
    fi

    # Update session cache with RAG metadata
    META=$(echo "$RESULT" | python3 -c "
import sys, json
meta = json.load(sys.stdin).get('rag_meta', {})
rule_ids = meta.get('rule_ids', [])
tokens = meta.get('tokens', 0)
if rule_ids:
    print(json.dumps(rule_ids))
    print(tokens)
else:
    print('')
    print('0')
" 2>/dev/null || echo "")

    NEW_RULE_IDS=$(echo "$META" | head -1)
    COST=$(echo "$META" | tail -1)

    if [ -n "$NEW_RULE_IDS" ] && [ "$NEW_RULE_IDS" != "" ]; then
        _writ_session update "$SESSION_ID" \
            --add-rules "$NEW_RULE_IDS" \
            --cost "$COST" \
            --inc-queries 2>/dev/null || true
    fi
fi

hook_timer_end "$HOOK_START_NS" "writ-pre-write-dispatch" "$SESSION_ID" ""
exit 0
