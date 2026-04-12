#!/usr/bin/env bash
# Auto-approve gate -- thin client for writ-session.py advance-phase.
# UserPromptSubmit: fires at the start of every user turn.
#
# Phase 3: all artifact validation, gate file creation, and rule ID
# clearing is handled by writ-session.py advance-phase. This hook
# detects approval patterns and delegates.
#
# Hook type: UserPromptSubmit
# Exit: always 0 (never block user prompt)

set -euo pipefail

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
SESSION_HELPER="$WRIT_DIR/bin/lib/writ-session.py"

# Read stdin once
STDIN_JSON=$(cat)

# Extract session_id and prompt
PARSED=$(echo "$STDIN_JSON" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    sid = data.get('agent_id', '') or data.get('session_id', '')
    agent_id = data.get('agent_id', '')
    prompt = data.get('prompt', data.get('message', data.get('content', '')))
    print(f'{sid}\n{prompt}\n{agent_id}')
except Exception:
    print('\n\n')
" 2>/dev/null) || true

SESSION_ID=$(echo "$PARSED" | head -1)
PROMPT=$(echo "$PARSED" | sed -n '2p')
AGENT_ID=$(echo "$PARSED" | sed -n '3p')

# Fallback session ID
if [ -z "$SESSION_ID" ]; then
    SESSION_ID=$(ps -o ppid= -p $PPID 2>/dev/null | tr -d ' ')
fi
if [ -z "$SESSION_ID" ]; then
    SESSION_ID=$(echo "${PWD}:${USER}" | md5sum | cut -c1-12)-$(date +%Y%m%d)
fi

# Publish session ID as backup -- skip inside sub-agents
if [ -z "$AGENT_ID" ]; then
    echo "$SESSION_ID" > /tmp/writ-current-session
fi

# Check approval pattern
PROMPT_LOWER=$(echo "$PROMPT" | tr '[:upper:]' '[:lower:]' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')

IS_APPROVAL=$(python3 -c "
import re, sys

prompt = sys.argv[1]

exact = {
    'approved', 'approve', 'lgtm', 'proceed', 'go ahead',
    'looks good', 'ship it', 'yes', 'yep', 'y', 'ok', 'okay',
    'go', 'do it', 'continue', 'accepted', 'accept',
}

clean = re.sub(r'[.!,]+$', '', prompt.strip())

if clean in exact:
    print('yes'); sys.exit(0)

# Strip common prefix words and re-check exact match
prefixes = ('ok ', 'okay ', 'sure ', 'yeah ', 'yes ', 'yep ', 'alright ')
stripped = clean
for p in prefixes:
    if clean.startswith(p):
        stripped = re.sub(r'^' + re.escape(p) + r'[,]?\s*', '', clean)
        break
if stripped != clean and stripped in exact:
    print('yes'); sys.exit(0)

def levenshtein(s1, s2):
    if len(s1) < len(s2): return levenshtein(s2, s1)
    if len(s2) == 0: return len(s1)
    prev = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        curr = [i + 1]
        for j, c2 in enumerate(s2):
            curr.append(min(prev[j + 1] + 1, curr[j] + 1, prev[j] + (c1 != c2)))
        prev = curr
    return prev[-1]

fuzzy_targets = ['approved', 'approve', 'proceed', 'accepted', 'accept']
if len(clean) <= 12:
    for target in fuzzy_targets:
        if levenshtein(clean, target) <= 2:
            print('yes'); sys.exit(0)

if len(prompt) < 120:
    approval_words = r'(?:approved?|proceed|go ahead|continue|accept(?:ed)?|lgtm|looks? good|ship it)'
    prefix_words = r'(?:ok|okay|sure|yeah|yes|yep|alright)'
    patterns = [
        r'^(?:yes|yep|yeah),?\s*' + approval_words,
        r'^' + approval_words + r'\s*[.!]*$',
        r'^(?:phase\s*[a-d]|test.skeletons?)\s*(?:approved?|lgtm)\s*[.!]*$',
        r'^(?:approve|create)\s+(?:phase|gate)',
        # Prefix word + optional comma/space + approval word (+ optional trailing context)
        r'^' + prefix_words + r'[,.]?\s+' + approval_words,
    ]
    for p in patterns:
        if re.match(p, prompt):
            print('yes'); sys.exit(0)

print('no')
" "$PROMPT_LOWER" 2>/dev/null || echo "no")

# Friction logging: approval_pattern_miss
if [ "$IS_APPROVAL" != "yes" ] && [ ${#PROMPT} -gt 0 ] && [ ${#PROMPT} -lt 120 ]; then
    LOOKS_LIKE_APPROVAL=$(python3 -c "
import sys
prompt = sys.argv[1].lower()
approval_words = ['approv', 'proceed', 'accept', 'lgtm', 'good', 'go', 'yes', 'ok']
print('yes' if any(w in prompt for w in approval_words) else 'no')
" "$PROMPT_LOWER" 2>/dev/null || echo "no")

    if [ "$LOOKS_LIKE_APPROVAL" = "yes" ]; then
        PROJECT_ROOT=$(python3 -c "
import os
markers = ['composer.json','package.json','Cargo.toml','go.mod','pyproject.toml','.git']
path = os.getcwd()
while path != '/':
    if any(os.path.exists(os.path.join(path, m)) for m in markers):
        print(path); break
    path = os.path.dirname(path)
" 2>/dev/null)
        if [ -n "$PROJECT_ROOT" ]; then
            CURRENT_MODE=$(python3 "$SESSION_HELPER" mode get "$SESSION_ID" 2>/dev/null || echo "")
            CURRENT_MODE=$(echo "$CURRENT_MODE" | tr -d '[:space:]')
            python3 -c "
import json, sys
from datetime import datetime, timezone
entry = json.dumps({
    'ts': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    'session': sys.argv[1],
    'mode': sys.argv[2] if sys.argv[2] else None,
    'event': 'approval_pattern_miss',
    'prompt': sys.argv[3][:120],
})
with open(sys.argv[4], 'a') as f:
    f.write(entry + '\n')
" "$SESSION_ID" "${CURRENT_MODE:-}" "$PROMPT" "$PROJECT_ROOT/workflow-friction.log" 2>/dev/null || true
        fi
    fi
fi

if [ "$IS_APPROVAL" != "yes" ]; then
    exit 0
fi

# Ensure gate token exists (created once per session, used to block agent self-approval)
GATE_TOKEN_FILE="/tmp/writ-gate-token-${SESSION_ID}"
if [ ! -f "$GATE_TOKEN_FILE" ]; then
    python3 -c "import secrets; print(secrets.token_hex(16))" > "$GATE_TOKEN_FILE" 2>/dev/null
    chmod 600 "$GATE_TOKEN_FILE" 2>/dev/null || true
fi
GATE_TOKEN=$(cat "$GATE_TOKEN_FILE" 2>/dev/null)

# Delegate to advance-phase -- pass prompt via stdin for phase-d detection
RESULT=$(echo "$PROMPT_LOWER" | python3 "$SESSION_HELPER" advance-phase "$SESSION_ID" --token "$GATE_TOKEN" 2>/dev/null) || true

if [ -z "$RESULT" ]; then
    exit 0
fi

ADVANCED=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('advanced', False))" 2>/dev/null || echo "False")
GATE=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('gate', ''))" 2>/dev/null || echo "")
REASON=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('reason', ''))" 2>/dev/null || echo "")

if [ "$ADVANCED" = "True" ]; then
    # Log approval_pattern_match
    CURRENT_MODE=$(python3 "$SESSION_HELPER" mode get "$SESSION_ID" 2>/dev/null || echo "")
    CURRENT_MODE=$(echo "$CURRENT_MODE" | tr -d '[:space:]')
    PROJECT_ROOT=$(python3 -c "
import os
markers = ['composer.json','package.json','Cargo.toml','go.mod','pyproject.toml','.git']
path = os.getcwd()
while path != '/':
    if any(os.path.exists(os.path.join(path, m)) for m in markers):
        print(path); break
    path = os.path.dirname(path)
" 2>/dev/null)
    if [ -n "$PROJECT_ROOT" ]; then
        python3 -c "
import json, sys
from datetime import datetime, timezone
entry = json.dumps({
    'ts': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    'session': sys.argv[1],
    'mode': sys.argv[2] if sys.argv[2] else None,
    'event': 'approval_pattern_match',
    'matched_prompt': sys.argv[3][:120],
    'gate': sys.argv[4],
})
with open(sys.argv[5], 'a') as f:
    f.write(entry + '\n')
" "$SESSION_ID" "${CURRENT_MODE:-}" "$PROMPT" "$GATE" "$PROJECT_ROOT/workflow-friction.log" 2>/dev/null || true
    fi

    # Phase-specific next-step messages
    if [ "$GATE" = "phase-a" ]; then
        cat <<'PHASE_MSG'
[Writ: Plan approved. Phase: testing]
NEXT STEP: Write test skeleton files, present them, and say "Say approved to proceed."
Do NOT write implementation files yet -- they will be denied.
PHASE_MSG
    elif [ "$GATE" = "test-skeletons" ]; then
        echo "[Writ: Test skeletons approved. Phase: implementation] You may now write implementation files."
    else
        echo "[Gate approved: $GATE] Phase advanced."
    fi
else
    if [ -n "$REASON" ]; then
        echo "[Gate blocked: $GATE] $REASON"
    fi
fi

# Permanent debug prompt log (zero-cost observation tool)
echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') session=$SESSION_ID prompt=$(echo "$PROMPT" | head -c 200)" \
    >> "/tmp/writ-prompt-debug.log" 2>/dev/null || true

exit 0
