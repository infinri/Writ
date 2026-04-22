#!/usr/bin/env bash
# Auto-approve gate -- pattern-match defense-in-depth for approval detection.
# UserPromptSubmit: fires at the start of every user turn.
#
# Phase 3b (plan Section 8.1): pattern match does NOT advance the phase.
# It only emits an ask-prompt directive that steers the assistant to the
# /writ-approve slash command, which performs a tool-confirmed advance
# with confirmation_source="tool" in the audit trail.
#
# Why: silent pattern-path advances left no auditable intent record and
# could fire on ambiguous phrasing. The tool path requires the assistant
# to positively confirm via a slash command. Pattern match remains as a
# hint to the assistant, not the primary advance mechanism.
#
# Hook type: UserPromptSubmit
# Exit: always 0 (never block user prompt). Directive (if any) is on stdout.

set -euo pipefail

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
SESSION_HELPER="$WRIT_DIR/bin/lib/writ-session.py"
source "$WRIT_DIR/bin/lib/common.sh"

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

# Project root helper (used for friction logging)
PROJECT_ROOT=$(python3 -c "
import os
markers = ['composer.json','package.json','Cargo.toml','go.mod','pyproject.toml','.git']
path = os.getcwd()
while path != '/':
    if any(os.path.exists(os.path.join(path, m)) for m in markers):
        print(path); break
    path = os.path.dirname(path)
" 2>/dev/null)

CURRENT_MODE=$(_writ_session "mode get" "$SESSION_ID" 2>/dev/null || echo "")
CURRENT_MODE=$(echo "$CURRENT_MODE" | tr -d '[:space:]')

# Friction logging: approval_pattern_miss (unchanged)
if [ "$IS_APPROVAL" != "yes" ] && [ ${#PROMPT} -gt 0 ] && [ ${#PROMPT} -lt 120 ]; then
    LOOKS_LIKE_APPROVAL=$(python3 -c "
import sys
prompt = sys.argv[1].lower()
approval_words = ['approv', 'proceed', 'accept', 'lgtm', 'good', 'go', 'yes', 'ok']
print('yes' if any(w in prompt for w in approval_words) else 'no')
" "$PROMPT_LOWER" 2>/dev/null || echo "no")

    if [ "$LOOKS_LIKE_APPROVAL" = "yes" ] && [ -n "$PROJECT_ROOT" ]; then
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

if [ "$IS_APPROVAL" != "yes" ]; then
    # Permanent debug prompt log (zero-cost observation tool)
    echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') session=$SESSION_ID prompt=$(echo "$PROMPT" | head -c 200)" \
        >> "/tmp/writ-prompt-debug.log" 2>/dev/null || true
    exit 0
fi

# ---- Pattern matched. Phase 3b: steer to /writ-approve, do NOT advance. ----

# Ensure gate token exists. /writ-approve (via its Bash POST) reads this
# token file to authenticate the advance request.
GATE_TOKEN_FILE="/tmp/writ-gate-token-${SESSION_ID}"
if [ ! -f "$GATE_TOKEN_FILE" ]; then
    python3 -c "import secrets; print(secrets.token_hex(16))" > "$GATE_TOKEN_FILE" 2>/dev/null
    chmod 600 "$GATE_TOKEN_FILE" 2>/dev/null || true
fi

# Friction log: approval_pattern_match (pattern detected; tool confirm pending)
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
    'confirmation_source': 'pattern',
    'outcome': 'ask-prompt-emitted',
})
with open(sys.argv[4], 'a') as f:
    f.write(entry + '\n')
" "$SESSION_ID" "${CURRENT_MODE:-}" "$PROMPT" "$PROJECT_ROOT/workflow-friction.log" 2>/dev/null || true
fi

# Emit ask-prompt directive. The assistant must invoke /writ-approve to
# actually advance the phase (confirmation_source="tool"). This directive
# is injected into the next-turn context via UserPromptSubmit stdout.
cat <<'DIRECTIVE'
[Writ: approval pattern detected]
Your last prompt reads as approval, but pattern-match is defense-in-depth only (plan Section 8.1).
To advance the workflow phase, invoke the /writ-approve slash command. It records
confirmation_source="tool" in the session's phase_transitions audit trail.
If you did not intend to advance, ignore this directive.
DIRECTIVE

# Permanent debug prompt log (zero-cost observation tool)
echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') session=$SESSION_ID prompt=$(echo "$PROMPT" | head -c 200)" \
    >> "/tmp/writ-prompt-debug.log" 2>/dev/null || true

exit 0
