#!/usr/bin/env bash
# Writ RAG Bridge -- UserPromptSubmit hook
#
# Fires at the start of every user turn. Queries Writ for relevant rules
# and injects them into Claude's context via stdout.
#
# Hook type: UserPromptSubmit
# Exit: always 0 (never block user prompt)

set -euo pipefail

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
SESSION_HELPER="$WRIT_DIR/bin/lib/writ-session.py"

WRIT_HOST="${WRIT_HOST:-localhost}"
WRIT_PORT="${WRIT_PORT:-8765}"
WRIT_URL="http://${WRIT_HOST}:${WRIT_PORT}/query"
WRIT_HEALTH_URL="http://${WRIT_HOST}:${WRIT_PORT}/health"
WRIT_LOCKFILE="/tmp/writ-server-starting.lock"

MIN_QUERY_LENGTH=10

# Auto-start: ensure Neo4j and Writ server are running.
# Uses a lockfile to prevent multiple hooks from racing to start the server.
if ! curl -sf --connect-timeout 0.1 "$WRIT_HEALTH_URL" >/dev/null 2>&1; then
    # Acquire lock (non-blocking; if another hook is already starting, wait for it)
    if ( set -o noclobber; echo $$ > "$WRIT_LOCKFILE" ) 2>/dev/null; then
        trap 'rm -f "$WRIT_LOCKFILE"' EXIT

        # Ensure Neo4j is running (docker restart is a no-op if already up)
        if command -v docker >/dev/null 2>&1; then
            docker start writ-neo4j >/dev/null 2>&1 || true
            # Wait up to 8s for Neo4j bolt port
            for _i in $(seq 1 16); do
                if curl -sf --connect-timeout 0.1 http://localhost:7474 >/dev/null 2>&1; then
                    break
                fi
                sleep 0.5
            done
        fi

        # Start Writ server in background
        if [ -f "$WRIT_DIR/.venv/bin/activate" ]; then
            (
                cd "$WRIT_DIR"
                source .venv/bin/activate
                nohup writ serve >>/tmp/writ-server.log 2>&1 &
            )
            # Wait up to 5s for Writ health endpoint
            for _i in $(seq 1 10); do
                if curl -sf --connect-timeout 0.1 "$WRIT_HEALTH_URL" >/dev/null 2>&1; then
                    break
                fi
                sleep 0.5
            done
        fi

        rm -f "$WRIT_LOCKFILE"
        trap - EXIT
    else
        # Another process is starting the server; wait for it
        for _i in $(seq 1 20); do
            if curl -sf --connect-timeout 0.1 "$WRIT_HEALTH_URL" >/dev/null 2>&1; then
                break
            fi
            sleep 0.5
        done
    fi
fi

# Session ID: grandparent PID = the claude process.
# $PPID is the ephemeral bash shell; its parent is the stable claude PID.
SESSION_ID=$(ps -o ppid= -p $PPID 2>/dev/null | tr -d ' ')
if [ -z "$SESSION_ID" ]; then
    # Fallback: project+date hash
    SESSION_ID=$(echo "${PWD}:${USER}" | md5sum | cut -c1-12)-$(date +%Y%m%d)
fi

# 1. Check skip conditions (budget exhausted or context pressure > 75%)
if python3 "$SESSION_HELPER" should-skip "$SESSION_ID" 2>/dev/null; then
    exit 0
fi

# 2. Extract prompt from stdin JSON
PROMPT=$(python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print(data.get('message', data.get('content', '')))
except Exception:
    print('')
" 2>/dev/null)

# 3. Minimum query length gate
if [ ${#PROMPT} -lt $MIN_QUERY_LENGTH ]; then
    exit 0
fi

# 4. Read session cache
CACHE=$(python3 "$SESSION_HELPER" read "$SESSION_ID" 2>/dev/null || echo '{"loaded_rule_ids":[],"remaining_budget":8000}')
LOADED_RULE_IDS=$(echo "$CACHE" | python3 -c "import sys,json; print(json.dumps(json.load(sys.stdin).get('loaded_rule_ids',[])))" 2>/dev/null || echo '[]')
REMAINING_BUDGET=$(echo "$CACHE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('remaining_budget',8000))" 2>/dev/null || echo '8000')

# 5. Build request JSON
REQUEST=$(python3 -c "
import json, sys
print(json.dumps({
    'query': sys.argv[1],
    'budget_tokens': int(sys.argv[2]),
    'exclude_rule_ids': json.loads(sys.argv[3])
}))
" "$PROMPT" "$REMAINING_BUDGET" "$LOADED_RULE_IDS" 2>/dev/null)

if [ -z "$REQUEST" ]; then
    exit 0
fi

# 6. POST to Writ server
# --connect-timeout 0.05: 50ms catches "server not running" instantly
# --max-time 0.2: 200ms total for slow queries (Writ p95 is <1ms)
RESPONSE=$(curl -s --connect-timeout 0.05 --max-time 0.2 \
    -X POST "$WRIT_URL" \
    -H "Content-Type: application/json" \
    -d "$REQUEST" 2>/dev/null) || true

if [ -z "$RESPONSE" ]; then
    echo "[Writ: server unavailable, proceeding without rules]"
    exit 0
fi

# Check for error response
HAS_ERROR=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print('yes' if 'error' in d else 'no')" 2>/dev/null || echo "yes")
if [ "$HAS_ERROR" = "yes" ]; then
    echo "[Writ: query failed, proceeding without rules]"
    exit 0
fi

# 7. Check for low-relevance response (proposal trigger)
# If no rules returned or all scores below threshold, append a proposal nudge.
LOW_RELEVANCE_THRESHOLD=0.3
PROPOSAL_NUDGE=$(echo "$RESPONSE" | python3 -c "
import sys, json
try:
    resp = json.load(sys.stdin)
    rules = resp.get('rules', [])
    threshold = float(sys.argv[1])
    if not rules:
        print('NO_RULES')
    elif all(r.get('score', 0) < threshold for r in rules):
        print('LOW_SCORES')
    else:
        print('')
except Exception:
    print('')
" "$LOW_RELEVANCE_THRESHOLD" 2>/dev/null || echo "")

# 8. Format response and capture metadata
FORMAT_OUTPUT=$(echo "$RESPONSE" | python3 "$SESSION_HELPER" format 2>/dev/null) || true

# Split: everything before WRIT_META: goes to stdout (Claude sees it).
# The WRIT_META: line is parsed for cache updates.
RULES_TEXT=""
META_LINE=""
if [ -n "$FORMAT_OUTPUT" ]; then
    RULES_TEXT=$(echo "$FORMAT_OUTPUT" | grep -v "^WRIT_META:")
    META_LINE=$(echo "$FORMAT_OUTPUT" | grep "^WRIT_META:" | head -1)
fi

# 9. Inject rules into Claude's context
if [ -n "$RULES_TEXT" ]; then
    echo "$RULES_TEXT"
fi

# 10. Append proposal nudge if low relevance
if [ "$PROPOSAL_NUDGE" = "NO_RULES" ]; then
    echo ""
    echo "[Writ: no matching rules found for this task. If you discover a pattern, constraint, or gotcha during this work that would help future tasks, propose it via POST /propose. See .claude/CLAUDE.md for the format and trigger conditions.]"
elif [ "$PROPOSAL_NUDGE" = "LOW_SCORES" ]; then
    echo ""
    echo "[Writ: retrieved rules have low relevance scores (< $LOW_RELEVANCE_THRESHOLD). The knowledge base may not cover this area well. If you discover a pattern worth codifying, propose it via POST /propose.]"
fi

# 11. Update session cache
if [ -n "$META_LINE" ]; then
    META_JSON="${META_LINE#WRIT_META:}"
    NEW_RULE_IDS=$(echo "$META_JSON" | python3 -c "import sys,json; print(json.dumps(json.load(sys.stdin).get('rule_ids',[])))" 2>/dev/null || echo '[]')
    COST=$(echo "$META_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('cost',0))" 2>/dev/null || echo '0')

    python3 "$SESSION_HELPER" update "$SESSION_ID" \
        --add-rules "$NEW_RULE_IDS" \
        --cost "$COST" \
        --inc-queries 2>/dev/null || true
fi

exit 0
