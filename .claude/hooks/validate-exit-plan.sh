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
# This replaces the manual phase-a approval cycle. The user no longer
# needs to say "approved" -- the hook validates automatically.

set -euo pipefail

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
SESSION_HELPER="$WRIT_DIR/bin/lib/writ-session.py"

# Read stdin envelope
STDIN_JSON=$(cat)

SESSION_ID=$(echo "$STDIN_JSON" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print(data.get('session_id', ''))
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

# Check if a tier is declared. If no tier, allow exit -- Writ enforcement
# isn't active and we shouldn't block unrelated /plan usage.
CURRENT_TIER=$(python3 "$SESSION_HELPER" tier get "$SESSION_ID" 2>/dev/null || echo "")
CURRENT_TIER=$(echo "$CURRENT_TIER" | tr -d '[:space:]')

if [ -z "$CURRENT_TIER" ]; then
    # No tier declared -- allow exit, Writ isn't managing this session
    exit 0
fi

# Tier 0 and 1 have no plan requirement
if [ "$CURRENT_TIER" -lt 2 ] 2>/dev/null; then
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

# Plan is valid -- allow exit and advance the phase-a gate
GATE_TOKEN_FILE="/tmp/writ-gate-token-${SESSION_ID}"
if [ ! -f "$GATE_TOKEN_FILE" ]; then
    python3 -c "import secrets; print(secrets.token_hex(16))" > "$GATE_TOKEN_FILE" 2>/dev/null
    chmod 600 "$GATE_TOKEN_FILE" 2>/dev/null || true
fi
GATE_TOKEN=$(cat "$GATE_TOKEN_FILE" 2>/dev/null)

# Advance phase-a gate automatically
RESULT=$(echo "approved" | python3 "$SESSION_HELPER" advance-phase "$SESSION_ID" --token "$GATE_TOKEN" 2>/dev/null) || true

exit 0
