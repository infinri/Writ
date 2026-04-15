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
source "$WRIT_DIR/bin/lib/common.sh"

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
        if [ -f "$WRIT_DIR/.venv/bin/python3" ]; then
            (
                cd "$WRIT_DIR"
                nohup .venv/bin/python3 -m uvicorn writ.server:app --host 0.0.0.0 --port "$WRIT_PORT" >>/tmp/writ-server.log 2>&1 &
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
# The prompt is cleaned before use: code blocks, markdown chrome, and tool
# output are stripped so the RAG query contains only the user's intent.
PARSED=$(echo "$STDIN_JSON" | python3 -c "
import sys, json, re

MAX_KEYWORDS = 25

# Common English stopwords + conversational filler
STOPWORDS = frozenset(
    'a an the is are was were be been being have has had do does did will would '
    'shall should may might can could of in to for on with at by from as into '
    'through during before after above below between out off over under again '
    'further then once here there when where why how all each every both few '
    'more most other some such no nor not only own same so than too very just '
    'also about up its it i me my we our you your he him his she her they them '
    'their what which who whom this that these those am let get got if but and '
    'or because until while although since even though however still yet already '
    'please dont im ive weve youre theyre doesnt didnt wont cant isnt arent '
    'seems like think want need know see look make sure something anything '
    'everything nothing really actually probably maybe already going doing '
    'using used way things stuff lot much many well right now here there '
    'also another first last next new old good bad big small long short give '
    'take come go say tell ask try keep start stop run work help show move '
    'yes no ok okay hey hi hello thanks thank sorry'.split()
)

def extract_keywords(raw: str) -> str:
    # Strip fenced code blocks but keep language hints.
    langs = re.findall(r'\x60\x60\x60(\w+)', raw)
    text = re.sub(r'\x60\x60\x60[\s\S]*?\x60\x60\x60', ' ', raw)
    # Strip inline code spans.
    text = re.sub(r'\x60[^\x60]+\x60', ' ', text)
    # Strip markdown/table/tool chrome.
    text = re.sub(r'[│┌┐└┘├┤┬┴┼─━┃╌╍╎╏═║╔╗╚╝╠╣╦╩╬|]', ' ', text)
    text = re.sub(r'●[^\n]*', ' ', text)
    text = re.sub(r'⎿.*', ' ', text)
    text = re.sub(r'[✻◆▐▛▜▌▝▘]+[^\n]*', ' ', text)
    # Strip URLs
    text = re.sub(r'https?://\S+', ' ', text)
    # Strip non-alphanumeric except hyphens and underscores (preserve technical terms)
    text = re.sub(r'[^a-zA-Z0-9_\-/.\s]', ' ', text)
    # Tokenize
    words = text.split()
    # Filter: keep technical terms, remove stopwords and short noise
    keywords = []
    seen = set()
    for w in words:
        lower = w.lower().strip('.-/')
        if not lower or len(lower) < 3:
            continue
        if lower in STOPWORDS:
            continue
        if lower in seen:
            continue
        seen.add(lower)
        # Prefer: capitalized words, words with underscores/hyphens, file-like patterns
        keywords.append(w if (w[0].isupper() or '_' in w or '-' in w or '.' in w) else lower)
    # Add language hints from code fences
    for lang in set(langs):
        if lang.lower() not in seen:
            keywords.append(lang)
            seen.add(lang.lower())
    # Cap and join
    return ' '.join(keywords[:MAX_KEYWORDS])

try:
    data = json.load(sys.stdin)
    sid = data.get('agent_id', '') or data.get('session_id', '')
    agent_id = data.get('agent_id', '')
    raw = data.get('prompt', data.get('message', data.get('content', '')))
    prompt = extract_keywords(raw) if len(raw) > 300 else raw
    print(f'{sid}\n{prompt}\n{agent_id}')
except Exception as e:
    print(f'\n\n')
" 2>/dev/null) || true

SESSION_ID=$(echo "$PARSED" | head -1)
PROMPT=$(echo "$PARSED" | sed -n '2p')
AGENT_ID=$(echo "$PARSED" | sed -n '3p')

# Fallback session ID if not provided by Claude Code
if [ -z "$SESSION_ID" ]; then
    SESSION_ID=$(ps -o ppid= -p $PPID 2>/dev/null | tr -d ' ')
fi
if [ -z "$SESSION_ID" ]; then
    SESSION_ID=$(echo "${PWD}:${USER}" | md5sum | cut -c1-12)-$(date +%Y%m%d)
fi

# Publish session ID so Stop hooks (friction-logger) can find it
# Do NOT overwrite when inside a sub-agent -- protect parent's session file
if [ -z "$AGENT_ID" ]; then
    echo "$SESSION_ID" > /tmp/writ-current-session
fi

debug "session=$SESSION_ID prompt_len=${#PROMPT}"

# 1. Check skip conditions (budget exhausted or context pressure > 75%)
if _writ_session should-skip "$SESSION_ID" 2>/dev/null; then
    debug "skipped: budget or context pressure"
    exit 0
fi

# 1a. Compaction recovery runs on the real PostCompact event via
# writ-postcompact.sh. The previous heuristic here relied on a
# non-existent env var and was removed.

# 1b. Check current mode (for post-rules directive injection)
CURRENT_MODE=$(_writ_session "mode get" "$SESSION_ID" 2>/dev/null || echo "")
CURRENT_MODE=$(echo "$CURRENT_MODE" | tr -d '[:space:]')
debug "mode=$CURRENT_MODE"

# 1c. Orchestrator suppression: skip /query, emit compact status line only
IS_ORCHESTRATOR=$(python3 -c "
import sys, json, os, tempfile
cache_dir = os.environ.get('WRIT_CACHE_DIR', tempfile.gettempdir())
path = os.path.join(cache_dir, f'writ-session-${SESSION_ID}.json')
try:
    with open(path) as f:
        print('true' if json.load(f).get('is_orchestrator') else 'false')
except Exception:
    print('false')
" 2>/dev/null || echo "false")

if [ "$IS_ORCHESTRATOR" = "true" ]; then
    debug "orchestrator mode: skipping /query, emitting status line"
    # Still emit mode-classification directive if no mode set
    if [ -z "$CURRENT_MODE" ]; then
        cat << MODE_DIRECTIVE

[Writ: set mode before proceeding]
Conversation: discussion, no code. Debug: investigating a problem, no code.
Review: evaluating code against rules, no code. Work: building/modifying code (full workflow).
Declare: python3 $SESSION_HELPER mode set <conversation|debug|review|work> $SESSION_ID
Full definitions: see SKILL.md "Mode system" section.
MODE_DIRECTIVE
    fi
    # Compact status line for orchestrator
    CACHE_DATA=$(_writ_session read "$SESSION_ID" 2>/dev/null || echo '{}')
    STATUS_LINE=$(echo "$CACHE_DATA" | python3 -c "
import sys, json
try:
    c = json.load(sys.stdin)
    phase = c.get('current_phase', 'unknown')
    gates = c.get('gates_approved', [])
    violations = len(c.get('pending_violations', []))
    mode = c.get('mode', 'unknown')
    print(f'[Writ: mode={mode}, phase={phase}, gates={gates}, violations={violations}]')
except Exception:
    print('[Writ: orchestrator mode active]')
" 2>/dev/null)
    echo "$STATUS_LINE"
    exit 0
fi

# 2. Minimum query length gate
if [ ${#PROMPT} -lt $MIN_QUERY_LENGTH ]; then
    debug "skipped: prompt too short (${#PROMPT} < $MIN_QUERY_LENGTH)"
    exit 0
fi

# 3. Read session cache
CACHE=$(_writ_session read "$SESSION_ID" 2>/dev/null || echo '{"loaded_rule_ids":[],"remaining_budget":8000}')
# Phase 3: only exclude current-phase rule IDs (historical IDs can be re-injected)
LOADED_RULE_IDS=$(echo "$CACHE" | python3 -c "
import sys, json
cache = json.load(sys.stdin)
by_phase = cache.get('loaded_rule_ids_by_phase', {})
current_phase = cache.get('current_phase', '')
if by_phase and current_phase:
    print(json.dumps(by_phase.get(current_phase, [])))
else:
    # Fallback: use flat list for pre-Phase-3 sessions
    print(json.dumps(cache.get('loaded_rule_ids', [])))
" 2>/dev/null || echo '[]')
REMAINING_BUDGET=$(echo "$CACHE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('remaining_budget',8000))" 2>/dev/null || echo '8000')

# 3b. Extract sticky rules preference (Cycle C)
PREFER_RULE_IDS=$(echo "$CACHE" | python3 -c "import sys,json; print(json.dumps(json.load(sys.stdin).get('last_injected_rule_ids',[])))" 2>/dev/null || echo '[]')

# 3c. Extract detected domain from CwdChanged hook (Cycle C)
DETECTED_DOMAIN=$(echo "$CACHE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('detected_domain','') or '')" 2>/dev/null || echo '')

# 3d. Extract instructions_rule_ids from InstructionsLoaded hook (Cycle C)
# Merge into the exclusion list to avoid re-injecting rules already in CLAUDE.md
INSTRUCTIONS_RULE_IDS=$(echo "$CACHE" | python3 -c "import sys,json; print(json.dumps(json.load(sys.stdin).get('instructions_rule_ids',[])))" 2>/dev/null || echo '[]')

# 4. Build request JSON
REQUEST=$(python3 -c "
import json, sys

query = sys.argv[1]
budget = int(sys.argv[2])
exclude_ids = json.loads(sys.argv[3])
prefer_ids = json.loads(sys.argv[4])
detected_domain = sys.argv[5]
instructions_ids = json.loads(sys.argv[6])

# Merge instructions_rule_ids into exclude list (deduplicated)
all_excludes = list(set(exclude_ids) | set(instructions_ids))

req = {
    'query': query,
    'budget_tokens': budget,
    'exclude_rule_ids': all_excludes,
}

# Sticky rules: pass previous injection order as preference
if prefer_ids:
    req['prefer_rule_ids'] = prefer_ids

# Domain hint from CwdChanged: only pass non-null, non-universal values
if detected_domain and detected_domain != 'universal':
    req['domain'] = detected_domain

print(json.dumps(req))
" "$PROMPT" "$REMAINING_BUDGET" "$LOADED_RULE_IDS" "$PREFER_RULE_IDS" "$DETECTED_DOMAIN" "$INSTRUCTIONS_RULE_IDS" 2>/dev/null)

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
FORMAT_OUTPUT=$(echo "$RESPONSE" | _writ_session format 2>/dev/null) || true

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

# 9. Inject mode classification directive if no mode set yet
if [ -z "$CURRENT_MODE" ]; then
    cat << MODE_DIRECTIVE

[Writ: set mode before proceeding]
Conversation: discussion, no code. Debug: investigating a problem, no code.
Review: evaluating code against rules, no code. Work: building/modifying code (full workflow).
Declare: python3 $SESSION_HELPER mode set <conversation|debug|review|work> $SESSION_ID
Full definitions: see SKILL.md "Mode system" section.
MODE_DIRECTIVE
    debug "injected mode classification directive"
fi

# 9b. Inject mode-specific reminders
case "$CURRENT_MODE" in
    conversation)
        echo ""
        echo "[Writ: Conversation mode. Rules injected as context. No code generation expected.]"
        debug "injected conversation mode reminder"
        ;;
    debug)
        echo ""
        echo "[Writ: Debug mode. Rules injected for investigation. No code generation -- recommend Work mode when fix is identified.]"
        debug "injected debug mode reminder"
        ;;
    review)
        echo ""
        echo "[Writ: Review mode. Evaluate code against injected rules. Output structured findings per file.]"
        debug "injected review mode reminder"
        ;;
    work)
        # Work mode: inject workflow reminder based on gate state
        _PROJECT_ROOT=$(python3 -c "
import os, sys
markers = ['composer.json','package.json','Cargo.toml','go.mod','pyproject.toml','.git']
path = os.getcwd()
while path != '/':
    if any(os.path.exists(os.path.join(path, m)) for m in markers):
        print(path); sys.exit(0)
    path = os.path.dirname(path)
print('')
" 2>/dev/null)

        if [ -n "$_PROJECT_ROOT" ]; then
            _GATE_DIR="$_PROJECT_ROOT/.claude/gates"
            _PHASE_A="$_GATE_DIR/phase-a.approved"
            _TEST_SKEL="$_GATE_DIR/test-skeletons.approved"

            if [ ! -f "$_PHASE_A" ]; then
                echo ""
                echo "[Writ: Work mode -- plan gate pending. Enter /plan, write plan.md, exit, present, wait for approval.]"
                debug "injected work mode state (plan)"
            elif [ ! -f "$_TEST_SKEL" ]; then
                echo ""
                echo "[Writ: Work mode -- test-skeletons gate pending. Write test files to disk, present, wait for approval.]"
                debug "injected work mode state (test-skeletons)"
            fi
        fi
        ;;
esac

# 10. Append proposal nudge if low relevance (only when tier is set -- don't mix directives)
if [ "$PROPOSAL_NUDGE" = "NO_RULES" ]; then
    echo ""
    echo "[Writ: no matching rules found for this task. If you discover a pattern, constraint, or gotcha during this work that would help future tasks, propose it via POST /propose. See SKILL.md for the format and trigger conditions.]"
elif [ "$PROPOSAL_NUDGE" = "LOW_SCORES" ]; then
    echo ""
    echo "[Writ: retrieved rules have low relevance scores (< $LOW_RELEVANCE_THRESHOLD). The knowledge base may not cover this area well. If you discover a pattern worth codifying, propose it via POST /propose.]"
fi

# 11. Update session cache (rule IDs + full rule objects)
if [ -n "$META_LINE" ]; then
    META_JSON="${META_LINE#WRIT_META:}"
    NEW_RULE_IDS=$(echo "$META_JSON" | python3 -c "import sys,json; print(json.dumps(json.load(sys.stdin).get('rule_ids',[])))" 2>/dev/null || echo '[]')
    COST=$(echo "$META_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('cost',0))" 2>/dev/null || echo '0')

    _writ_session update "$SESSION_ID" \
        --add-rules "$NEW_RULE_IDS" \
        --cost "$COST" \
        --inc-queries 2>/dev/null || true

    # 11b. Save returned rule IDs as sticky preference for next turn (Cycle C)
    python3 -c "
import sys, json, os, tempfile
session_id = sys.argv[1]
new_rule_ids = json.loads(sys.argv[2])
cache_dir = os.environ.get('WRIT_CACHE_DIR', tempfile.gettempdir())
path = os.path.join(cache_dir, f'writ-session-{session_id}.json')
try:
    with open(path) as f:
        cache = json.load(f)
    cache['last_injected_rule_ids'] = new_rule_ids
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(cache, f)
    os.rename(tmp, path)
except Exception:
    pass
" "$SESSION_ID" "$NEW_RULE_IDS" 2>/dev/null || true

    # C1: Store full rule objects for downstream compliance checking
    RULE_OBJECTS=$(echo "$RESPONSE" | python3 -c "
import sys, json
try:
    resp = json.load(sys.stdin)
    rules = resp.get('rules', [])
    # Extract the fields needed for violation pattern matching
    objects = []
    for r in rules:
        objects.append({
            'rule_id': r.get('rule_id', ''),
            'trigger': r.get('trigger', ''),
            'statement': r.get('statement', ''),
            'violation': r.get('violation', ''),
            'pass_example': r.get('pass_example', ''),
            'enforcement': r.get('enforcement', ''),
            'domain': r.get('domain', ''),
            'severity': r.get('severity', ''),
        })
    print(json.dumps(objects))
except Exception:
    print('[]')
" 2>/dev/null || echo '[]')

    if [ "$RULE_OBJECTS" != "[]" ]; then
        _writ_session update "$SESSION_ID" \
            --add-rule-objects "$RULE_OBJECTS" 2>/dev/null || true
        debug "stored ${#RULE_OBJECTS} bytes of rule objects"
    fi

    # Log rag_query event
    python3 -c "
import json, sys, os
from datetime import datetime, timezone
entry = json.dumps({
    'ts': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    'session': sys.argv[1],
    'mode': sys.argv[2] if sys.argv[2] else None,
    'event': 'rag_query',
    'query_source': 'broad',
    'tokens_injected': int(sys.argv[3]),
    'rules_returned_count': len(json.loads(sys.argv[4])),
    'rule_ids': json.loads(sys.argv[4]),
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
" "$SESSION_ID" "${CURRENT_MODE:-}" "$COST" "$NEW_RULE_IDS" 2>/dev/null || true
fi

# 12. Read session cache for escalation and backward context checks
CACHE=$(_writ_session read "$SESSION_ID" 2>/dev/null || echo '{}')

# Check for escalation and inject backward context
ESCALATION=$(_writ_session check-escalation "$SESSION_ID" 2>/dev/null || echo '{"needed":false}')
ESC_NEEDED=$(echo "$ESCALATION" | python3 -c "import sys,json; print('yes' if json.load(sys.stdin).get('needed') else 'no')" 2>/dev/null || echo "no")

if [ "$ESC_NEEDED" = "yes" ]; then
    ESC_GATE=$(echo "$ESCALATION" | python3 -c "import sys,json; print(json.load(sys.stdin).get('gate','?'))" 2>/dev/null)
    ESC_DIAG=$(echo "$ESCALATION" | python3 -c "import sys,json; print(json.load(sys.stdin).get('diagnosis','?'))" 2>/dev/null)
    ESC_CYCLES=$(echo "$ESCALATION" | python3 -c "import sys,json; print(json.load(sys.stdin).get('cycles',0))" 2>/dev/null)

    # Build failure history from invalidation records
    FAILURE_HISTORY=$(python3 -c "
import sys, json

cache_str = sys.argv[1]
gate = sys.argv[2]
diagnosis = sys.argv[3]

try:
    cache = json.loads(cache_str)
except Exception:
    cache = {}

records = cache.get('invalidation_history', {}).get(gate, [])
lines = []
for r in records:
    lines.append(f\"  Cycle {r['cycle']}: {r['rule_id']} violated in {r['file']} ({r.get('evidence', 'no evidence')[:120]})\")

if diagnosis == 'same-rule':
    lines.append('')
    lines.append('  Same rule triggered all cycles. Possible causes:')
    lines.append('    1. Plan repeatedly fails to address this rule')
    lines.append('    2. Rule violation pattern is over-broad for this context')
    lines.append('    3. Task requires an exception to this rule')
elif diagnosis == 'different-rules':
    lines.append('')
    lines.append('  Different rule each cycle. Plan is broadly missing rule coverage.')
else:
    lines.append('')
    lines.append('  Mixed pattern. Specific gaps in the plan.')

print('\n'.join(lines))
" "$CACHE" "$ESC_GATE" "$ESC_DIAG" 2>/dev/null)

    cat << ESCALATION_MSG

[Writ: ESCALATION -- ${ESC_GATE} invalidated ${ESC_CYCLES} times]

Failure history:
${FAILURE_HISTORY}

User action needed: review the rule definitions or re-scope the task.
Do NOT proceed with automated work until the user responds.
ESCALATION_MSG
    debug "injected escalation for $ESC_GATE ($ESC_DIAG, $ESC_CYCLES cycles)"

    # C10: Post enriched negative feedback (once per escalation)
    ESC_FB_SENT=$(echo "$ESCALATION" | python3 -c "import sys,json; d=json.load(sys.stdin); print('yes' if d.get('feedback_sent') else 'no')" 2>/dev/null || echo "no")
    if [ "$ESC_FB_SENT" != "yes" ]; then
        python3 -c "
import sys, json

cache_str = sys.argv[1]
gate = sys.argv[2]

try:
    cache = json.loads(cache_str)
except Exception:
    sys.exit(0)

records = cache.get('invalidation_history', {}).get(gate, [])
rule_ids = set(r['rule_id'] for r in records)

import urllib.request, urllib.error
for rid in rule_ids:
    payload = json.dumps({'rule_id': rid, 'signal': 'negative'}).encode()
    req = urllib.request.Request(
        'http://localhost:8765/feedback',
        data=payload,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        urllib.request.urlopen(req, timeout=0.3)
    except (urllib.error.URLError, OSError):
        break
" "$CACHE" "$ESC_GATE" 2>/dev/null || true

        # Mark feedback as sent in escalation
        python3 -c "
import sys, json, os, tempfile

session_id = sys.argv[1]
cache_dir = tempfile.gettempdir()
path = os.path.join(cache_dir, f'writ-session-{session_id}.json')
try:
    with open(path) as f:
        cache = json.load(f)
    cache.setdefault('escalation', {})['feedback_sent'] = True
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(cache, f)
    os.rename(tmp, path)
except Exception:
    pass
" "$SESSION_ID" 2>/dev/null || true
        debug "sent enriched negative feedback for escalation"
    fi

    exit 0
fi

# 13. Check for gate invalidation (backward context without escalation)
# Only relevant in Work mode
if [ "$CURRENT_MODE" = "work" ]; then
    if [ -n "$_PROJECT_ROOT" ]; then
        _GATE_DIR="${_GATE_DIR:-$_PROJECT_ROOT/.claude/gates}"

        # Check if any gate was invalidated (records exist but .approved file missing)
        BACKWARD_CTX=$(python3 -c "
import sys, json, os

cache_str = sys.argv[1]
gate_dir = sys.argv[2]

try:
    cache = json.loads(cache_str)
except Exception:
    sys.exit(0)

history = cache.get('invalidation_history', {})
for gate_name, records in history.items():
    if not records:
        continue
    gate_file = os.path.join(gate_dir, f'{gate_name}.approved')
    if not os.path.exists(gate_file):
        # Gate was invalidated and not yet re-approved
        latest = records[-1]
        cycle = len(records)
        max_cycles = 3
        plan_hash = latest.get('prior_plan_hash', 'unknown')
        lines = []
        lines.append(f'[Writ: {gate_name} INVALIDATED -- cycle {cycle} of {max_cycles}]')
        lines.append('Previous plan failed validation:')
        for r in records:
            lines.append(f'  - {r[\"rule_id\"]} violated in {r[\"file\"]} ({r.get(\"evidence\", \"\")[:120]})')
        lines.append(f'Revise the plan to address these gaps.')
        lines.append(f'Previous plan hash: {plan_hash} (do not resubmit unchanged)')
        print('\n'.join(lines))
        break  # Only inject for the first invalidated gate
" "$CACHE" "$_GATE_DIR" 2>/dev/null)

        if [ -n "$BACKWARD_CTX" ]; then
            echo ""
            echo "$BACKWARD_CTX"
            debug "injected backward context for invalidated gate"
        fi
    fi
fi

exit 0
