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
CONTEXT_TOKENS="${CLAUDE_CONTEXT_TOKENS:-0}"

# Diagnostic: log available CLAUDE_* env vars once per session
DIAG_FLAG="/tmp/writ-env-diag-${SESSION_ID}"
if [ ! -f "$DIAG_FLAG" ]; then
    env | grep -i "^CLAUDE" > "/tmp/writ-env-diag-${SESSION_ID}.log" 2>/dev/null || true
    touch "$DIAG_FLAG" 2>/dev/null || true
fi

python3 "$SESSION_HELPER" update "$SESSION_ID" \
    --context-percent "$CONTEXT_PCT" 2>/dev/null || true

# Log token_snapshot and store in session cache (always -- zeros are data)
SNAPSHOT_JSON="{\"context_percent\":$CONTEXT_PCT,\"context_tokens\":$CONTEXT_TOKENS}"
python3 "$SESSION_HELPER" update "$SESSION_ID" \
    --token-snapshot "$SNAPSHOT_JSON" 2>/dev/null || true

python3 -c "
import json, sys, os
from datetime import datetime, timezone
entry = json.dumps({
    'ts': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    'session': sys.argv[1],
    'mode': None,
    'event': 'token_snapshot',
    'context_percent': int(sys.argv[2]),
    'context_tokens': int(sys.argv[3]),
})
markers = ['composer.json','package.json','Cargo.toml','go.mod','pyproject.toml','.git']
path = os.getcwd()
while path != '/':
    if any(os.path.exists(os.path.join(path, m)) for m in markers):
        try:
            with open(os.path.join(path, 'workflow-friction.log'), 'a') as f:
                f.write(entry + '\n')
        except OSError:
            pass
        break
    path = os.path.dirname(path)
" "$SESSION_ID" "$CONTEXT_PCT" "$CONTEXT_TOKENS" 2>/dev/null || true

# Auto-feedback: correlate rules-in-context with analysis outcomes, POST to Writ.
# Runs every stop, but only sends feedback when there are new analysis results
# and rules to correlate. Idempotent per rule per session (feedback_sent cache).
python3 "$SESSION_HELPER" auto-feedback "$SESSION_ID" \
    >> "/tmp/writ-feedback-${SESSION_ID}.log" 2>/dev/null || true

# Log coverage report to /tmp for later analysis (non-blocking, silent)
python3 "$SESSION_HELPER" coverage "$SESSION_ID" \
    >> "/tmp/writ-coverage-${SESSION_ID}.log" 2>/dev/null || true

exit 0
