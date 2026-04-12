#!/usr/bin/env bash
# SubagentStart hook -- creates isolated session cache for each sub-agent worker.
#
# When a sub-agent spawns, this hook:
# 1. Reads the parent's session state (mode, phase, gates)
# 2. Creates a fresh session cache keyed by agent_id
# 3. Pre-populates with parent's gate state but fresh RAG budget
# 4. Queries Writ for phase-specific rules
# 5. Injects rules + state via additionalContext
#
# Hook type: SubagentStart
# Exit: always 0

set -euo pipefail

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
SESSION_HELPER="$WRIT_DIR/bin/lib/writ-session.py"

WRIT_HOST="${WRIT_HOST:-localhost}"
WRIT_PORT="${WRIT_PORT:-8765}"

# Read stdin envelope
STDIN_JSON=$(cat)

# Extract agent metadata and parent session
AGENT_ID=$(echo "$STDIN_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('agent_id',''))" 2>/dev/null || echo "")
AGENT_TYPE=$(echo "$STDIN_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('agent_type',''))" 2>/dev/null || echo "")
PARENT_SESSION=$(echo "$STDIN_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || echo "")

if [ -z "$AGENT_ID" ]; then
    exit 0
fi

# Read parent's current state
if [ -n "$PARENT_SESSION" ]; then
    PARENT_STATE=$(python3 "$SESSION_HELPER" read "$PARENT_SESSION" 2>/dev/null || echo '{}')
else
    # Try the published session file
    if [ -f /tmp/writ-current-session ]; then
        PARENT_SESSION=$(cat /tmp/writ-current-session 2>/dev/null | tr -d '[:space:]')
        PARENT_STATE=$(python3 "$SESSION_HELPER" read "$PARENT_SESSION" 2>/dev/null || echo '{}')
    else
        PARENT_STATE='{}'
    fi
fi

# Create isolated session for the sub-agent with parent's gate state but fresh budget
python3 -c "
import sys, json, os
sys.path.insert(0, '$WRIT_DIR/bin/lib')
from importlib import util
spec = util.spec_from_file_location('writ_session', '$SESSION_HELPER')
mod = util.module_from_spec(spec)
spec.loader.exec_module(mod)

parent = json.loads(sys.argv[1])
agent_id = sys.argv[2]

# Create fresh cache with parent's structural state but clean operational state
cache = mod._read_cache(agent_id)  # creates default if not exists
cache['mode'] = parent.get('mode', 'work')
cache['current_phase'] = parent.get('current_phase', 'planning')
cache['gates_approved'] = parent.get('gates_approved', [])
cache['remaining_budget'] = mod.DEFAULT_SESSION_BUDGET  # fresh budget
cache['loaded_rule_ids'] = []
cache['loaded_rule_ids_by_phase'] = {}
cache['loaded_rules'] = []
cache['denial_counts'] = {}
cache['queries'] = 0
cache['context_percent'] = 0
cache['files_written'] = []
cache['analysis_results'] = {}
cache['pending_violations'] = []
cache['feedback_sent'] = []
cache['pretool_queried_files'] = []
cache['token_snapshots'] = []
mod._write_cache(agent_id, cache)

print(json.dumps({
    'mode': cache['mode'],
    'phase': cache['current_phase'],
    'gates': cache['gates_approved'],
    'budget': cache['remaining_budget'],
}))
" "$PARENT_STATE" "$AGENT_ID" 2>/dev/null || true

# Query Writ for rules if server is available
ADDITIONAL_CONTEXT=""
HEALTH=$(curl -sf --connect-timeout 0.5 --max-time 1 "http://${WRIT_HOST}:${WRIT_PORT}/health" 2>/dev/null || echo "")
if [ -n "$HEALTH" ]; then
    # Extract the agent's prompt/description for a targeted Writ query
    AGENT_PROMPT=$(echo "$STDIN_JSON" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    # Try various fields that might contain the task description
    prompt = d.get('prompt', d.get('description', d.get('message', '')))
    print(prompt[:500] if prompt else '')
except Exception:
    print('')
" 2>/dev/null || echo "")

    if [ -n "$AGENT_PROMPT" ] && [ ${#AGENT_PROMPT} -gt 10 ]; then
        RESPONSE=$(python3 -c "
import json, sys
print(json.dumps({
    'query': sys.argv[1][:500],
    'budget_tokens': 2000,
    'exclude_rule_ids': [],
}))
" "$AGENT_PROMPT" 2>/dev/null | \
            curl -s --connect-timeout 0.5 --max-time 2 \
                -X POST "http://${WRIT_HOST}:${WRIT_PORT}/query" \
                -H "Content-Type: application/json" \
                -d @- 2>/dev/null) || true

        if [ -n "$RESPONSE" ]; then
            RULES_TEXT=$(echo "$RESPONSE" | python3 "$SESSION_HELPER" format 2>/dev/null) || true
            if [ -n "$RULES_TEXT" ]; then
                RULES_ONLY=$(echo "$RULES_TEXT" | grep -v "^WRIT_META:")
                ADDITIONAL_CONTEXT="$RULES_ONLY"
            fi
        fi
    fi
fi

# Get the parent's phase state for context injection
PHASE_INFO=$(python3 -c "
import sys, json
parent = json.loads(sys.argv[1])
mode = parent.get('mode', 'work')
phase = parent.get('current_phase', 'planning')
gates = parent.get('gates_approved', [])
print(f'[Writ sub-agent: mode={mode}, phase={phase}, gates={\",\".join(gates) if gates else \"none\"}]')
" "$PARENT_STATE" 2>/dev/null || echo "[Writ sub-agent: isolated session]")

# Inject via additionalContext
if [ -n "$ADDITIONAL_CONTEXT" ] || [ -n "$PHASE_INFO" ]; then
    python3 -c "
import json, sys
ctx = sys.argv[1]
if sys.argv[2]:
    ctx = sys.argv[2] + '\n' + ctx
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'SubagentStart',
        'additionalContext': ctx,
    }
}))
" "$ADDITIONAL_CONTEXT" "$PHASE_INFO" 2>/dev/null
fi

# Log sub-agent start to friction log
source "$WRIT_DIR/bin/lib/common.sh"
CURRENT_MODE=$(python3 "$SESSION_HELPER" mode get "$AGENT_ID" 2>/dev/null || echo "")
CURRENT_MODE=$(echo "$CURRENT_MODE" | tr -d '[:space:]')
log_friction_event "$AGENT_ID" "$CURRENT_MODE" "subagent_start" \
    "{\"agent_type\":\"$AGENT_TYPE\",\"parent_session\":\"$PARENT_SESSION\"}"

exit 0
