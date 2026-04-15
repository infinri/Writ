#!/usr/bin/env bash
# SubagentStop hook -- logs sub-agent completion metrics.
#
# When a sub-agent completes, this hook logs the event to the friction log
# for observability and rule coverage analysis.
#
# Hook type: SubagentStop
# Exit: always 0

set -euo pipefail

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
source "$WRIT_DIR/bin/lib/common.sh"

SESSION_HELPER="$WRIT_DIR/bin/lib/writ-session.py"

# Read stdin envelope
STDIN_JSON=$(cat)

AGENT_ID=$(echo "$STDIN_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('agent_id',''))" 2>/dev/null || echo "")
AGENT_TYPE=$(echo "$STDIN_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('agent_type',''))" 2>/dev/null || echo "")
PARENT_SESSION=$(echo "$STDIN_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || echo "")

if [ -z "$AGENT_ID" ]; then
    exit 0
fi

# Fallback: some Claude Code versions / nested sub-agents omit agent_type.
# Default to "general-purpose" and log the fallback so we can track frequency.
if [ -z "$AGENT_TYPE" ]; then
    AGENT_TYPE="general-purpose"
    log_friction_event "$AGENT_ID" "" "subagent_type_fallback" \
        "{\"hook\":\"writ-subagent-stop\",\"parent_session\":\"$PARENT_SESSION\"}"
fi

# Read the agent's session cache for summary metrics
CACHE=$(_writ_session read "$AGENT_ID" 2>/dev/null || echo '{}')

python3 -c "
import sys, json, os
from datetime import datetime, timezone

cache = json.loads(sys.argv[1])
agent_id = sys.argv[2]
agent_type = sys.argv[3]
parent_session = sys.argv[4]

entry = json.dumps({
    'ts': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    'session': agent_id,
    'mode': cache.get('mode'),
    'event': 'subagent_complete',
    'agent_id': agent_id,
    'agent_type': agent_type,
    'parent_session': parent_session,
    'files_written': len(cache.get('files_written', [])),
    'rules_loaded': len(cache.get('loaded_rule_ids', [])),
    'queries': cache.get('queries', 0),
    'remaining_budget': cache.get('remaining_budget', 0),
    'denial_count': sum(cache.get('denial_counts', {}).values()),
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
" "$CACHE" "$AGENT_ID" "$AGENT_TYPE" "$PARENT_SESSION" 2>/dev/null || true

exit 0
