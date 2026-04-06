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
WRIT_DEBUG_LOG="/tmp/writ-rag-debug.log"

MIN_QUERY_LENGTH=10

debug() {
    echo "[$(date '+%H:%M:%S')] $*" >> "$WRIT_DEBUG_LOG"
}

# Capture stdin once -- Claude Code sends JSON with prompt, session_id, etc.
STDIN_JSON=$(cat)
debug "stdin: ${STDIN_JSON:0:200}"

# Auto-start: ensure Neo4j and Writ server are running.
# Uses a lockfile to prevent multiple hooks from racing to start the server.
if ! curl -sf --connect-timeout 0.2 "$WRIT_HEALTH_URL" >/dev/null 2>&1; then
    debug "server down, attempting auto-start"
    # Acquire lock (non-blocking; if another hook is already starting, wait for it)
    if ( set -o noclobber; echo $$ > "$WRIT_LOCKFILE" ) 2>/dev/null; then
        trap 'rm -f "$WRIT_LOCKFILE"' EXIT

        # Ensure Neo4j is running (docker restart is a no-op if already up)
        if command -v docker >/dev/null 2>&1; then
            docker start writ-neo4j >/dev/null 2>&1 || true
            # Wait up to 8s for Neo4j HTTP port
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
                if curl -sf --connect-timeout 0.2 "$WRIT_HEALTH_URL" >/dev/null 2>&1; then
                    debug "server started"
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
            if curl -sf --connect-timeout 0.2 "$WRIT_HEALTH_URL" >/dev/null 2>&1; then
                break
            fi
            sleep 0.5
        done
    fi
fi

# Extract session_id and prompt from the captured stdin JSON.
# Claude Code provides: session_id, prompt, cwd, hook_event_name, etc.
PARSED=$(echo "$STDIN_JSON" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    sid = data.get('session_id', '')
    prompt = data.get('prompt', data.get('message', data.get('content', '')))
    print(f'{sid}\n{prompt}')
except Exception as e:
    print(f'\n')
" 2>/dev/null) || true

SESSION_ID=$(echo "$PARSED" | head -1)
PROMPT=$(echo "$PARSED" | tail -n +2)

# Fallback session ID if not provided by Claude Code
if [ -z "$SESSION_ID" ]; then
    SESSION_ID=$(ps -o ppid= -p $PPID 2>/dev/null | tr -d ' ')
fi
if [ -z "$SESSION_ID" ]; then
    SESSION_ID=$(echo "${PWD}:${USER}" | md5sum | cut -c1-12)-$(date +%Y%m%d)
fi

debug "session=$SESSION_ID prompt_len=${#PROMPT}"

# 1. Check skip conditions (budget exhausted or context pressure > 75%)
if python3 "$SESSION_HELPER" should-skip "$SESSION_ID" 2>/dev/null; then
    debug "skipped: budget or context pressure"
    exit 0
fi

# 1b. Check if tier is declared (for post-rules directive injection)
CURRENT_TIER=$(python3 "$SESSION_HELPER" tier get "$SESSION_ID" 2>/dev/null || echo "")
CURRENT_TIER=$(echo "$CURRENT_TIER" | tr -d '[:space:]')
debug "tier=$CURRENT_TIER"

# 2. Minimum query length gate
if [ ${#PROMPT} -lt $MIN_QUERY_LENGTH ]; then
    debug "skipped: prompt too short (${#PROMPT} < $MIN_QUERY_LENGTH)"
    exit 0
fi

# 3. Read session cache
CACHE=$(python3 "$SESSION_HELPER" read "$SESSION_ID" 2>/dev/null || echo '{"loaded_rule_ids":[],"remaining_budget":8000}')
LOADED_RULE_IDS=$(echo "$CACHE" | python3 -c "import sys,json; print(json.dumps(json.load(sys.stdin).get('loaded_rule_ids',[])))" 2>/dev/null || echo '[]')
REMAINING_BUDGET=$(echo "$CACHE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('remaining_budget',8000))" 2>/dev/null || echo '8000')

# 4. Build request JSON
REQUEST=$(python3 -c "
import json, sys
print(json.dumps({
    'query': sys.argv[1],
    'budget_tokens': int(sys.argv[2]),
    'exclude_rule_ids': json.loads(sys.argv[3])
}))
" "$PROMPT" "$REMAINING_BUDGET" "$LOADED_RULE_IDS" 2>/dev/null)

if [ -z "$REQUEST" ]; then
    debug "skipped: failed to build request JSON"
    exit 0
fi

# 5. POST to Writ server
# --connect-timeout 0.5: 500ms for connection (generous for localhost)
# --max-time 2: 2s total timeout (covers cold-start query warming)
RESPONSE=$(curl -s --connect-timeout 0.5 --max-time 2 \
    -X POST "$WRIT_URL" \
    -H "Content-Type: application/json" \
    -d "$REQUEST" 2>/dev/null) || true

if [ -z "$RESPONSE" ]; then
    debug "failed: empty response from server"
    echo "[Writ: server unavailable, proceeding without rules]"
    exit 0
fi

debug "response_len=${#RESPONSE}"

# Check for error response
HAS_ERROR=$(echo "$RESPONSE" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print('yes' if 'error' in d else 'no')
except Exception as e:
    # JSON parse failure -- likely truncated response
    print('parse_error: ' + str(e), file=sys.stderr)
    print('yes')
" 2>&1)

# Separate stderr debug info from the actual result
ERROR_RESULT=$(echo "$HAS_ERROR" | grep -v '^parse_error:' | head -1)
ERROR_DEBUG=$(echo "$HAS_ERROR" | grep '^parse_error:' || true)

if [ -n "$ERROR_DEBUG" ]; then
    debug "error check: $ERROR_DEBUG response_preview=${RESPONSE:0:200}"
fi

if [ "${ERROR_RESULT:-yes}" = "yes" ]; then
    debug "failed: error in response or parse failure"
    echo "[Writ: query failed, proceeding without rules]"
    exit 0
fi

# 6. Check for low-relevance response (proposal trigger)
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

# 7. Format response and capture metadata
FORMAT_OUTPUT=$(echo "$RESPONSE" | python3 "$SESSION_HELPER" format 2>/dev/null) || true

# Split: everything before WRIT_META: goes to stdout (Claude sees it).
# The WRIT_META: line is parsed for cache updates.
RULES_TEXT=""
META_LINE=""
if [ -n "$FORMAT_OUTPUT" ]; then
    RULES_TEXT=$(echo "$FORMAT_OUTPUT" | grep -v "^WRIT_META:")
    META_LINE=$(echo "$FORMAT_OUTPUT" | grep "^WRIT_META:" | head -1)
fi

# 8. Inject rules into Claude's context
if [ -n "$RULES_TEXT" ]; then
    echo "$RULES_TEXT"
    debug "injected rules"
fi

# 9. Inject tier classification directive if no tier declared yet
if [ -z "$CURRENT_TIER" ]; then
    cat << TIER_DIRECTIVE

[Writ: classify this task before proceeding]
Tier 0 (Research): no code generation. Tier 1 (Patch): <=3 files, no new contracts.
Tier 2 (Standard): new class/interface, single domain. Tier 3 (Complex): multi-domain, concurrency, queues.
Declare: python3 $SESSION_HELPER tier set <0-3> $SESSION_ID
Full definitions: see .claude/CLAUDE.md "Tier definitions" section.
TIER_DIRECTIVE
    debug "injected tier classification directive"
fi

# 10. Append proposal nudge if low relevance (only when tier is set -- don't mix directives)
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
