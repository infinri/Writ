#!/usr/bin/env bash
# PreToolUse RAG query -- fires before every Read
#
# Queries Writ with file-context rules when Claude reads a file in a read-heavy
# evaluation mode: Review, Debug, or Investigate (audit/explore/research). Skips
# in Work (RAG arrives via broad inject + the write path), Conversation, no mode.
#
# Hook type: PreToolUse (matcher: Read)
# Exit: always 0 (advisory injection only, never blocks)

set -euo pipefail

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
SESSION_HELPER="$WRIT_DIR/bin/lib/writ-session.py"
source "$WRIT_DIR/bin/lib/common.sh"
hook_instrument "writ-read-rag"

# WRIT_HOOK_LOG stderr breadcrumb sink, gated by WRIT_DEBUG: /dev/null when unset,
# ${WRIT_HOOK_LOG:-/tmp/writ-hooks.log} when WRIT_DEBUG=1 (single source: common.sh).
WRIT_HOOK_LOG_SINK="$(hook_log_sink)"

WRIT_HOST="${WRIT_HOST:-localhost}"
WRIT_PORT="${WRIT_PORT:-8765}"
WRIT_URL="http://${WRIT_HOST}:${WRIT_PORT}/query"

# Read stdin once
STDIN_DATA=$(cat)

# Extract session ID and file path in ONE interpreter: field 1 is the resolved id
# (agent_id, else session_id), field 2 is tool_input.file_path (else top-level file_path).
# These were two python3 starts on every governed Read; merging them paid for the
# record-the-Read update below without growing the per-Read cost. Fields are
# NUL-terminated (not newline-separated) so a file_path containing a newline survives
# intact; a failed parse or missing field leaves the variable empty.
SESSION_ID=""
FILE_PATH=""
{ IFS= read -r -d '' SESSION_ID || true; IFS= read -r -d '' FILE_PATH || true; } < <(
    echo "$STDIN_DATA" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
ti = d.get('tool_input') or {}
fp = ti.get('file_path', '') if isinstance(ti, dict) else ''
sys.stdout.write(str(d.get('agent_id', '') or d.get('session_id', '')) + '\0')
sys.stdout.write(str(fp or d.get('file_path', '')) + '\0')
" 2>/dev/null || true
)
# NO SYNTHESIZED ID. This used to call `detect_session_id ""`, which invented one from
# PPID or md5(cwd:user). Everything below is session-keyed (the mode filter that decides
# whether this hook runs at all, the budget check, and the `update` that banks the rule
# ids), and _cache_path() has no empty-id guard, so continuing would write a cache named
# for the empty string. This hook injects rules on Read and gates nothing.
if [ -z "$SESSION_ID" ]; then
    writ_critical writ-read-rag "no session_id in hook payload; skipping per-read rule injection"
    exit 0
fi

# Mode filter: fire in the read-heavy evaluation modes -- review, debug, and
# investigate (the unified audit/explore/research mode). investigate was added
# after this hook (INV-1) and was silently excluded, so read-only audit/explore
# agents received zero per-read RAG. Work/conversation still skip: work gets RAG
# via the broad inject + the PostToolUse write path; conversation is not code work.
MODE=$(_writ_session "mode get" "$SESSION_ID" 2>/dev/null || echo "")
MODE=$(echo "$MODE" | tr -d '[:space:]')
if [ "$MODE" != "review" ] && [ "$MODE" != "debug" ] && [ "$MODE" != "investigate" ]; then
    exit 0
fi

if [ -z "$FILE_PATH" ]; then
    exit 0
fi

# Record this Read as examined, on EVERY path from here on. The only update this hook
# used to run sat inside the WRIT_META block, so a Read that missed /query, scored no
# rule, hit an unknown-language file, or ran out of budget was never credited, and
# synthesis-gate reported 0 examined files after a fan-out that read every one of them.
# One exit handler runs ONE update carrying the file plus whatever the rag path adds
# below (writ_on_exit, not `trap ... EXIT`: hook_instrument owns the one EXIT trap).
READ_UPDATE_ARGS=(--add-pretool-file "$FILE_PATH")
_writ_record_read() {
    _writ_session update "$SESSION_ID" "${READ_UPDATE_ARGS[@]}" 2>>"$WRIT_HOOK_LOG_SINK" || true
}
writ_on_exit _writ_record_read

# Skip if budget exhausted or context pressure high. Below the record registration on
# purpose: a budget-exhausted master Read is still a Read.
if _writ_session should-skip "$SESSION_ID" 2>/dev/null; then
    exit 0
fi

# Skip non-source files
LANG=$(detect_language "$FILE_PATH")
if [ "$LANG" = "unknown" ]; then
    exit 0
fi

# Build a file-context query from the path
QUERY=$(python3 -c "
import sys, os, re

file_path = sys.argv[1]
lang = sys.argv[2]

parts = file_path.split('/')
basename = os.path.basename(file_path)
name_no_ext = os.path.splitext(basename)[0]

signals = [lang]

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
words = re.findall(r'[A-Z][a-z]+|[a-z]+', name_no_ext)
signals.extend(w.lower() for w in words if len(w) > 3)

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
CACHE=$(_writ_session read "$SESSION_ID" 2>/dev/null || echo '{"loaded_rule_ids":[],"remaining_budget":8000}')
LOADED_RULE_IDS=$(echo "$CACHE" | python3 "$WRIT_DIR/bin/lib/writ_phase_scoped_rules.py" 2>/dev/null || echo '[]')
REMAINING_BUDGET=$(echo "$CACHE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('remaining_budget',8000))" 2>/dev/null || echo '8000')

# Cap budget (at most 1/4 of remaining per query)
MAX_PRETOOL_BUDGET=1500
PRETOOL_BUDGET=$((REMAINING_BUDGET < MAX_PRETOOL_BUDGET ? REMAINING_BUDGET : MAX_PRETOOL_BUDGET))

if [ "$PRETOOL_BUDGET" -lt 200 ]; then
    exit 0
fi

# The project owning the file being read, so the daemon scopes retrieval to that
# project's records. Derived from the FILE, not from the hook's cwd: a read can reach
# into another checkout, and the rules that govern it belong to the file's project.
# detect_project_root is pure bash, so this adds no interpreter start to a path that
# runs on every file read.
PROJECT_ROOT=$(detect_project_root "$FILE_PATH")

# Build the /query request and POST it (shared helper; see bin/lib/common.sh).
RESPONSE=$(rag_query "$QUERY" "$PRETOOL_BUDGET" "$LOADED_RULE_IDS" "$PROJECT_ROOT")

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
    RULES_TEXT=$(echo "$FORMAT_OUTPUT" | grep -v "^WRIT_META:" || true)
    META_LINE=$(echo "$FORMAT_OUTPUT" | grep "^WRIT_META:" | head -1 || true)
fi

if [ -n "$RULES_TEXT" ]; then
    # #2: deliver via additionalContext. Plain stdout on PreToolUse reaches only
    # the CC debug log (verified delivery rule); additionalContext reaches the
    # model and is purely additive -- no permissionDecision, so it does NOT touch
    # the Read gate other hooks (writ-debug-code-gate) may set.
    AC_REPLY=$(WRIT_AC="[Writ: file-context rules for $(basename "$FILE_PATH")]
$RULES_TEXT" python3 <<'PY' 2>>"$WRIT_HOOK_LOG_SINK"
import json, os
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "additionalContext": os.environ.get("WRIT_AC", ""),
}}))
PY
) || AC_REPLY=""
    emit_hook_reply "$AC_REPLY" "" "$SESSION_ID"
fi

# Update session cache
if [ -n "$META_LINE" ]; then
    META_JSON="${META_LINE#WRIT_META:}"
    META_FIELDS=$(echo "$META_JSON" | parse_writ_meta)
    NEW_RULE_IDS=$(echo "$META_FIELDS" | sed -n '1p'); NEW_RULE_IDS="${NEW_RULE_IDS:-[]}"
    COST=$(echo "$META_FIELDS" | sed -n '2p'); COST="${COST:-0}"

    # A3: full rule objects (data prep), folded into the ONE combined update the
    # record-the-Read exit handler runs; an empty [] rule-objects payload is a no-op.
    RULE_OBJECTS=$(echo "$RESPONSE" | extract_rule_objects)

    READ_UPDATE_ARGS+=(
        --add-rules "$NEW_RULE_IDS"
        --cost "$COST"
        --inc-queries
        --add-rule-objects "$RULE_OBJECTS"
    )

    # Log rag_query event via the shared helper (reuse $MODE from the gate check
    # above; mode is fixed within a single hook invocation). Centralizes the parse
    # and restores the json.loads recovery this copy lacked (D-RAGLOG).
    CURRENT_MODE="$MODE"
    log_rag_query_event "$SESSION_ID" "${CURRENT_MODE:-}" "file-read" "$COST" "$NEW_RULE_IDS" "" "PreToolUse" "additionalContext"
fi

exit 0
