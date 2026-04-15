#!/usr/bin/env bash
# PostToolUse RAG query -- fires after every Write/Edit
#
# Queries Writ with code-derived patterns from the content Claude just wrote.
# Only fires when PreToolUse did NOT already query for the same file (gap-only).
#
# Hook type: PostToolUse (matcher: Write|Edit)
# Exit: always 0 (advisory injection only, never blocks)

set -euo pipefail

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
SESSION_HELPER="$WRIT_DIR/bin/lib/writ-session.py"
source "$WRIT_DIR/bin/lib/common.sh"

WRIT_HOST="${WRIT_HOST:-localhost}"
WRIT_PORT="${WRIT_PORT:-8765}"
WRIT_URL="http://${WRIT_HOST}:${WRIT_PORT}/query"

HOOK_START_NS=$(hook_timer_start)

# Read stdin once
STDIN_DATA=$(cat)

# Extract session ID
SESSION_ID=$(echo "$STDIN_DATA" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('agent_id','') or d.get('session_id',''))" 2>/dev/null)
if [ -z "$SESSION_ID" ]; then
    SESSION_ID=$(detect_session_id "")
fi

# Orchestrator suppression: no RAG for orchestrator writes (metadata only)
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
    exit 0
fi

# Skip if budget exhausted or context pressure high
if _writ_session should-skip "$SESSION_ID" 2>/dev/null; then
    exit 0
fi

# Extract file path, check language, check gap-only, and build query in one Python call.
# Outputs two lines: file_path and query. Exits non-zero to signal skip.
QUERY_RESULT=$(echo "$STDIN_DATA" | python3 -c "
import sys, json, re, os

MAX_KEYWORDS = 20

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)

ti = data.get('tool_input', {})
if isinstance(ti, str):
    try:
        ti = json.loads(ti)
    except (json.JSONDecodeError, ValueError):
        sys.exit(1)

file_path = ti.get('file_path', '')
content = ti.get('content', '') or ti.get('new_string', '')

if not file_path or not content:
    sys.exit(1)

# Detect language
ext_map = {
    '.php': 'php', '.xml': 'xml',
    '.js': 'javascript', '.jsx': 'javascript',
    '.ts': 'typescript', '.tsx': 'typescript',
    '.py': 'python', '.rs': 'rust', '.go': 'go',
    '.java': 'java', '.rb': 'ruby',
    '.graphqls': 'graphql', '.graphql': 'graphql',
}
ext = os.path.splitext(file_path)[1]
lang = ext_map.get(ext, 'unknown')
if lang == 'unknown':
    sys.exit(1)

# Build query from content
signals = [lang]

if lang == 'xml':
    # Class references in XML attributes
    class_refs = re.findall(r'(?:class|type|instance|name)=\x22([^\x22]+)\x22', content)
    for ref in class_refs:
        parts = ref.replace('\\\\\\\\', '\\\\').split('\\\\')
        if len(parts) > 1:
            signals.append(parts[-1])
            for p in parts[:-1]:
                if len(p) > 3 and p[0].isupper():
                    signals.append(p)
    # Plugin method names
    methods = re.findall(r'method=\x22(\w+)\x22', content)
    signals.extend(methods)
    # Event names
    events = re.findall(r'<event\s+name=\x22([^\x22]+)\x22', content)
    signals.extend(events)
    # Route URLs
    routes = re.findall(r'url=\x22([^\x22]+)\x22', content)
    for r in routes:
        signals.extend(p for p in r.strip('/').split('/') if len(p) > 3)
else:
    # Source code: class names, function names
    classes = re.findall(r'class\s+(\w+)', content)
    signals.extend(classes)
    functions = re.findall(r'(?:function|def|func|fn)\s+(\w+)', content)
    signals.extend(f for f in functions if len(f) > 3)

    # Import lines: extract capitalized words (class/module names)
    for line in content.split('\n'):
        if re.match(r'\s*(?:import|use|from|require)', line):
            words = re.findall(r'[A-Z]\w{2,}', line)
            signals.extend(words)

    # Type references
    type_refs = re.findall(r':\s*([A-Z]\w{2,})', content)
    signals.extend(type_refs)

    # PHP repository/factory patterns
    if lang == 'php':
        repo_calls = re.findall(r'->(\w+Repository|\w+Factory)\b', content)
        signals.extend(repo_calls)

    # Python decorators
    if lang == 'python':
        decorators = re.findall(r'@(\w+)', content)
        signals.extend(d for d in decorators if len(d) > 3)

# Deduplicate and cap
seen = set()
unique = []
for s in signals:
    lower = s.lower()
    if lower in seen or len(lower) < 3:
        continue
    seen.add(lower)
    unique.append(s)

query = ' '.join(unique[:MAX_KEYWORDS])
if len(query) < 5:
    sys.exit(1)

print(file_path)
print(query)
" 2>/dev/null) || exit 0

FILE_PATH=$(echo "$QUERY_RESULT" | head -1)
QUERY=$(echo "$QUERY_RESULT" | tail -n +2)

if [ -z "$FILE_PATH" ] || [ -z "$QUERY" ]; then
    exit 0
fi

# Read session cache for budget and exclusion list
CACHE=$(_writ_session read "$SESSION_ID" 2>/dev/null || echo '{}')

LOADED_RULE_IDS=$(echo "$CACHE" | python3 -c "
import sys, json
cache = json.load(sys.stdin)
by_phase = cache.get('loaded_rule_ids_by_phase', {})
current_phase = cache.get('current_phase', '')
if by_phase and current_phase:
    print(json.dumps(by_phase.get(current_phase, [])))
else:
    print(json.dumps(cache.get('loaded_rule_ids', [])))
" 2>/dev/null || echo '[]')
REMAINING_BUDGET=$(echo "$CACHE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('remaining_budget',8000))" 2>/dev/null || echo '8000')

MAX_POSTTOOL_BUDGET=1500
POSTTOOL_BUDGET=$((REMAINING_BUDGET < MAX_POSTTOOL_BUDGET ? REMAINING_BUDGET : MAX_POSTTOOL_BUDGET))

if [ "$POSTTOOL_BUDGET" -lt 200 ]; then
    exit 0
fi

# Build request
REQUEST=$(python3 -c "
import json, sys
print(json.dumps({
    'query': sys.argv[1],
    'budget_tokens': int(sys.argv[2]),
    'exclude_rule_ids': json.loads(sys.argv[3]),
    'top_k': 3,
}))
" "$QUERY" "$POSTTOOL_BUDGET" "$LOADED_RULE_IDS" 2>/dev/null)

if [ -z "$REQUEST" ]; then
    exit 0
fi

# Query Writ -- tight timeout (in the write path)
RESPONSE=$(curl -s --connect-timeout 0.3 --max-time 1 \
    -X POST "$WRIT_URL" \
    -H "Content-Type: application/json" \
    -d "$REQUEST" 2>/dev/null) || true

if [ -z "$RESPONSE" ]; then
    exit 0
fi

# Check for errors
HAS_ERROR=$(echo "$RESPONSE" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print('yes' if 'error' in d else 'no')
except Exception:
    print('yes')
" 2>/dev/null || echo "yes")

if [ "$HAS_ERROR" = "yes" ]; then
    exit 0
fi

# Check relevance threshold
HAS_RELEVANT=$(echo "$RESPONSE" | python3 -c "
import sys, json
try:
    resp = json.load(sys.stdin)
    rules = resp.get('rules', [])
    relevant = [r for r in rules if r.get('score', 0) >= 0.4]
    print('yes' if relevant else 'no')
except Exception:
    print('no')
" 2>/dev/null || echo "no")

if [ "$HAS_RELEVANT" != "yes" ]; then
    exit 0
fi

# Format and inject
FORMAT_OUTPUT=$(echo "$RESPONSE" | _writ_session format 2>/dev/null) || true

RULES_TEXT=""
META_LINE=""
if [ -n "$FORMAT_OUTPUT" ]; then
    RULES_TEXT=$(echo "$FORMAT_OUTPUT" | grep -v "^WRIT_META:")
    META_LINE=$(echo "$FORMAT_OUTPUT" | grep "^WRIT_META:" | head -1)
fi

if [ -n "$RULES_TEXT" ]; then
    echo ""
    echo "[Writ: post-write rules for $(basename "$FILE_PATH")]"
    echo "$RULES_TEXT"
fi

# Update session cache
if [ -n "$META_LINE" ]; then
    META_JSON="${META_LINE#WRIT_META:}"
    NEW_RULE_IDS=$(echo "$META_JSON" | python3 -c "import sys,json; print(json.dumps(json.load(sys.stdin).get('rule_ids',[])))" 2>/dev/null || echo '[]')
    COST=$(echo "$META_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('cost',0))" 2>/dev/null || echo '0')

    _writ_session update "$SESSION_ID" \
        --add-rules "$NEW_RULE_IDS" \
        --cost "$COST" \
        --inc-queries 2>/dev/null || true

    # Store full rule objects for compliance checking
    RULE_OBJECTS=$(echo "$RESPONSE" | python3 -c "
import sys, json
try:
    resp = json.load(sys.stdin)
    rules = resp.get('rules', [])
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
    fi

    # Log rag_query event (direct Python call to avoid shell quoting issues with JSON arrays)
    CURRENT_MODE=$(_writ_session "mode get" "$SESSION_ID" 2>/dev/null || echo "")
    CURRENT_MODE=$(echo "$CURRENT_MODE" | tr -d '[:space:]')
    python3 -c "
import json, sys, os
from datetime import datetime, timezone
rule_ids = json.loads(sys.argv[4])
entry = json.dumps({
    'ts': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    'session': sys.argv[1],
    'mode': sys.argv[2] if sys.argv[2] else None,
    'event': 'rag_query',
    'query_source': 'file-write-post',
    'tokens_injected': int(sys.argv[3]),
    'rules_returned_count': len(rule_ids),
    'rule_ids': rule_ids,
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

hook_timer_end "$HOOK_START_NS" "writ-posttool-rag" "$SESSION_ID" ""
exit 0
