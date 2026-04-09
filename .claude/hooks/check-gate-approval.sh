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

# Read stdin once and tee it -- we need it for both session ID and can-write
STDIN_DATA=$(cat)
SESSION_ID=$(echo "$STDIN_DATA" | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null)

if [ -z "$SESSION_ID" ]; then
    SESSION_ID=$(detect_session_id "")
fi

RESULT=$(echo "$STDIN_DATA" | python3 "$SESSION_HELPER" can-write "$SESSION_ID" --skill-dir "$SKILL_DIR" 2>/dev/null)

DECISION=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('decision','allow'))" 2>/dev/null || echo "allow")

if [ "$DECISION" = "deny" ]; then
    REASON=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('reason','Gate approval required'))" 2>/dev/null || echo "Gate approval required")
    python3 -c "
import json, sys
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': sys.argv[1]
    }
}))
" "$REASON"
fi

exit 0
