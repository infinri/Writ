#!/usr/bin/env bash
# Validate plan.md before allowing exit from plan mode.
#
# PreToolUse hook, matcher: ExitPlanMode
#
# When Claude tries to exit /plan mode, this hook validates that plan.md
# exists and contains all required sections (## Files, ## Analysis,
# ## Rules Applied, ## Capabilities). If validation fails, the hook
# denies the exit and Claude stays in plan mode to fix the plan.
#
# This hook validates FORMAT only. It does NOT create the phase-a gate.
# The user must say "approved" after reviewing the plan's substance.
# auto-approve-gate.sh creates the gate on user approval.

set -euo pipefail

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
SESSION_HELPER="$WRIT_DIR/bin/lib/writ-session.py"
source "$WRIT_DIR/bin/lib/common.sh"

# Read stdin envelope
STDIN_JSON=$(cat)

SESSION_ID=$(echo "$STDIN_JSON" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print(data.get('agent_id', '') or data.get('session_id', ''))
except Exception:
    print('')
" 2>/dev/null) || true

# Fallback session ID
if [ -z "$SESSION_ID" ]; then
    if [ -f /tmp/writ-current-session ]; then
        SESSION_ID=$(cat /tmp/writ-current-session 2>/dev/null | tr -d '[:space:]')
    fi
fi
if [ -z "$SESSION_ID" ]; then
    SESSION_ID=$(echo "${PWD}:${USER}" | md5sum | cut -c1-12)-$(date +%Y%m%d)
fi

# Check if mode is Work. Only Work mode requires plan validation.
# If no mode or non-work mode, allow exit -- Writ doesn't gate /plan usage.
CURRENT_MODE=$(_writ_session "mode get" "$SESSION_ID" 2>/dev/null || echo "")
CURRENT_MODE=$(echo "$CURRENT_MODE" | tr -d '[:space:]')

if [ "$CURRENT_MODE" != "work" ]; then
    # Not in Work mode -- allow exit, no plan validation needed
    exit 0
fi

# Detect project root
PROJECT_ROOT=$(python3 -c "
import os
markers = ['composer.json','package.json','Cargo.toml','go.mod','pyproject.toml','.git']
path = os.getcwd()
while path != '/':
    if any(os.path.exists(os.path.join(path, m)) for m in markers):
        print(path); break
    path = os.path.dirname(path)
" 2>/dev/null)

if [ -z "$PROJECT_ROOT" ]; then
    exit 0
fi

# Validate plan.md using the same validator as phase-a gate
VALIDATION_ERROR=$(python3 -c "
import sys
sys.path.insert(0, '$WRIT_DIR/bin/lib')
from importlib import util
spec = util.spec_from_file_location('writ_session', '$SESSION_HELPER')
mod = util.module_from_spec(spec)
spec.loader.exec_module(mod)
error = mod._validate_phase_a('$PROJECT_ROOT', '$SESSION_ID')
if error:
    print(error)
" 2>/dev/null) || true

if [ -n "$VALIDATION_ERROR" ]; then
    # Log exitplanmode_denial
    python3 -c "
import json, sys, os
from datetime import datetime, timezone
entry = json.dumps({
    'ts': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    'session': sys.argv[1],
    'mode': 'work',
    'event': 'exitplanmode_denial',
    'reason': sys.argv[2][:200],
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
" "$SESSION_ID" "$VALIDATION_ERROR" 2>/dev/null || true

    # Deny exit -- Claude stays in plan mode to fix the plan
    python3 -c "
import json, sys
result = {
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': sys.argv[1]
    }
}
print(json.dumps(result))
" "$VALIDATION_ERROR"
    exit 0
fi

# Log exitplanmode_allow
python3 -c "
import json, sys, os
from datetime import datetime, timezone
entry = json.dumps({
    'ts': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    'session': sys.argv[1],
    'mode': 'work',
    'event': 'exitplanmode_allow',
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
" "$SESSION_ID" 2>/dev/null || true

# Plan is valid -- allow exit from /plan mode.
# The phase-a gate is NOT created here. The user must review the plan
# and say "approved" for auto-approve-gate.sh to create the gate.
cat <<'WRIT_DIRECTIVE'
[WRIT WORKFLOW -- MANDATORY] Plan format validated. You are NOT approved to write code.
NEXT STEPS IN ORDER:
1. Present a brief plan summary to the user
2. Say "Say approved to proceed"
3. WAIT -- do not call Write or Edit until the user says "approved"
Attempting to write before approval WILL be denied.
WRIT_DIRECTIVE

exit 0
