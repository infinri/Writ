#!/usr/bin/env bash
# PreToolUse RAG query -- fires before every Write/Edit
#
# Constructs a file-context query from the target file path and queries
# Writ for domain-specific rules that the broad UserPromptSubmit query
# may have missed. This closes the retrieval gap where Claude makes
# implementation decisions without Writ being consulted.
#
# Hook type: PreToolUse (matcher: Write|Edit)
# Exit: always 0 (never block writes -- this is advisory injection only)

set -euo pipefail

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
SESSION_HELPER="$WRIT_DIR/bin/lib/writ-session.py"
source "$WRIT_DIR/bin/lib/common.sh"

WRIT_HOST="${WRIT_HOST:-localhost}"
WRIT_PORT="${WRIT_PORT:-8765}"
WRIT_URL="http://${WRIT_HOST}:${WRIT_PORT}/query"

# Read stdin once
STDIN_DATA=$(cat)

# Extract session ID
SESSION_ID=$(echo "$STDIN_DATA" | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null)
if [ -z "$SESSION_ID" ]; then
    SESSION_ID=$(detect_session_id "")
fi

# Skip if budget exhausted or context pressure high
if python3 "$SESSION_HELPER" should-skip "$SESSION_ID" 2>/dev/null; then
    exit 0
fi

# Extract file path from the envelope
FILE_PATH=$(echo "$STDIN_DATA" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    # Try tool_input.file_path first (Write/Edit envelope)
    ti = data.get('tool_input', {})
    fp = ti.get('file_path', '')
    if not fp:
        fp = data.get('file_path', '')
    print(fp)
except Exception:
    print('')
" 2>/dev/null)

if [ -z "$FILE_PATH" ]; then
    exit 0
fi

# Skip non-source files (configs, docs, test fixtures, etc.)
LANG=$(detect_language "$FILE_PATH")
if [ "$LANG" = "unknown" ]; then
    exit 0
fi

# Build a file-context query from the path
QUERY=$(python3 -c "
import sys, os

file_path = sys.argv[1]
lang = sys.argv[2]

# Extract meaningful context from the file path
parts = file_path.split('/')
basename = os.path.basename(file_path)
name_no_ext = os.path.splitext(basename)[0]

# Detect framework/domain signals from the path
signals = []

# Language
signals.append(lang)

# Magento-specific path signals
magento_patterns = {
    'Controller': 'controller endpoint',
    'Model': 'model service',
    'Api': 'interface contract',
    'Observer': 'event observer',
    'Plugin': 'plugin interceptor',
    'Block': 'view block',
    'Helper': 'helper class',
    'Setup': 'database schema migration',
    'Cron': 'cron job',
    'Console': 'CLI command',
    'Queue': 'message queue consumer',
    'etc': 'module configuration',
}
for pattern, signal in magento_patterns.items():
    if f'/{pattern}/' in file_path or file_path.endswith(f'/{pattern}'):
        signals.append(signal)
        break

# Python-specific signals
if lang == 'python':
    if 'server' in basename.lower() or 'endpoint' in basename.lower():
        signals.append('FastAPI endpoint')
    if 'test' in basename.lower():
        signals.append('test')

# XML config signals
if lang == 'xml':
    xml_types = {
        'di.xml': 'dependency injection',
        'webapi.xml': 'REST API endpoint',
        'crontab.xml': 'cron schedule',
        'system.xml': 'admin configuration',
        'communication.xml': 'message queue',
        'queue_topology.xml': 'queue topology',
        'queue_consumer.xml': 'queue consumer',
        'events.xml': 'event observer',
        'db_schema.xml': 'database schema',
    }
    for xml_file, signal in xml_types.items():
        if basename == xml_file:
            signals.append(signal)
            break

# Class/file name (split CamelCase)
import re
words = re.findall(r'[A-Z][a-z]+|[a-z]+', name_no_ext)
signals.extend(w.lower() for w in words if len(w) > 3)

# Deduplicate and join
seen = set()
unique = []
for s in signals:
    if s not in seen:
        seen.add(s)
        unique.append(s)

print(' '.join(unique[:15]))
" "$FILE_PATH" "$LANG" 2>/dev/null)

if [ -z "$QUERY" ] || [ ${#QUERY} -lt 5 ]; then
    exit 0
fi

# Read session cache for exclusion and budget
CACHE=$(python3 "$SESSION_HELPER" read "$SESSION_ID" 2>/dev/null || echo '{"loaded_rule_ids":[],"remaining_budget":8000}')
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

# Cap budget for PreToolUse queries (use at most 1/4 of remaining budget per query)
MAX_PRETOOL_BUDGET=1500
PRETOOL_BUDGET=$((REMAINING_BUDGET < MAX_PRETOOL_BUDGET ? REMAINING_BUDGET : MAX_PRETOOL_BUDGET))

if [ "$PRETOOL_BUDGET" -lt 200 ]; then
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
" "$QUERY" "$PRETOOL_BUDGET" "$LOADED_RULE_IDS" 2>/dev/null)

if [ -z "$REQUEST" ]; then
    exit 0
fi

# Query Writ -- tight timeout since this is in the write path
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

# Check if any results have scores above threshold
HAS_RELEVANT=$(echo "$RESPONSE" | python3 -c "
import sys, json
try:
    resp = json.load(sys.stdin)
    rules = resp.get('rules', [])
    # Only inject if at least one rule scores above 0.4
    relevant = [r for r in rules if r.get('score', 0) >= 0.4]
    print('yes' if relevant else 'no')
except Exception:
    print('no')
" 2>/dev/null || echo "no")

if [ "$HAS_RELEVANT" != "yes" ]; then
    exit 0
fi

# Format and inject
FORMAT_OUTPUT=$(echo "$RESPONSE" | python3 "$SESSION_HELPER" format 2>/dev/null) || true

RULES_TEXT=""
META_LINE=""
if [ -n "$FORMAT_OUTPUT" ]; then
    RULES_TEXT=$(echo "$FORMAT_OUTPUT" | grep -v "^WRIT_META:")
    META_LINE=$(echo "$FORMAT_OUTPUT" | grep "^WRIT_META:" | head -1)
fi

if [ -n "$RULES_TEXT" ]; then
    # Prefix to distinguish from UserPromptSubmit injection
    echo ""
    echo "[Writ: file-context rules for $(basename "$FILE_PATH")]"
    echo "$RULES_TEXT"
fi

# Update session cache
if [ -n "$META_LINE" ]; then
    META_JSON="${META_LINE#WRIT_META:}"
    NEW_RULE_IDS=$(echo "$META_JSON" | python3 -c "import sys,json; print(json.dumps(json.load(sys.stdin).get('rule_ids',[])))" 2>/dev/null || echo '[]')
    COST=$(echo "$META_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('cost',0))" 2>/dev/null || echo '0')

    python3 "$SESSION_HELPER" update "$SESSION_ID" \
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
        python3 "$SESSION_HELPER" update "$SESSION_ID" \
            --add-rule-objects "$RULE_OBJECTS" 2>/dev/null || true
    fi

    # Record file path for PostToolUse gap-only firing
    python3 "$SESSION_HELPER" update "$SESSION_ID" \
        --add-pretool-file "$FILE_PATH" 2>/dev/null || true
fi

exit 0
