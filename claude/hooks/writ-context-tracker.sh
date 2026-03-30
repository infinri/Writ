#!/usr/bin/env bash
# Writ RAG Bridge -- Stop hook (context pressure tracker)
#
# Fires after every Claude response. Records CLAUDE_CONTEXT_PERCENT into
# the session cache so the UserPromptSubmit hook can skip when context is tight.
#
# Hook type: Stop
# Exit: always 0

set -euo pipefail

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
SESSION_HELPER="$WRIT_DIR/bin/lib/writ-session.py"

# Session ID: grandparent PID = the claude process.
SESSION_ID=$(ps -o ppid= -p $PPID 2>/dev/null | tr -d ' ')
if [ -z "$SESSION_ID" ]; then
    SESSION_ID=$(echo "${PWD}:${USER}" | md5sum | cut -c1-12)-$(date +%Y%m%d)
fi

CONTEXT_PCT="${CLAUDE_CONTEXT_PERCENT:-0}"

python3 "$SESSION_HELPER" update "$SESSION_ID" \
    --context-percent "$CONTEXT_PCT" 2>/dev/null || true

# Auto-feedback: correlate rules-in-context with analysis outcomes, POST to Writ.
# Runs every stop, but only sends feedback when there are new analysis results
# and rules to correlate. Idempotent per rule per session (feedback_sent cache).
python3 "$SESSION_HELPER" auto-feedback "$SESSION_ID" \
    >> "/tmp/writ-feedback-${SESSION_ID}.log" 2>/dev/null || true

# Log coverage report to /tmp for later analysis (non-blocking, silent)
python3 "$SESSION_HELPER" coverage "$SESSION_ID" \
    >> "/tmp/writ-coverage-${SESSION_ID}.log" 2>/dev/null || true

exit 0
