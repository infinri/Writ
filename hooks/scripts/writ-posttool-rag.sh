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

# WRIT_HOOK_LOG stderr breadcrumb sink, gated by WRIT_DEBUG: /dev/null when unset,
# ${WRIT_HOOK_LOG:-/tmp/writ-hooks.log} when WRIT_DEBUG=1 (single source: common.sh).
WRIT_HOOK_LOG_SINK="$(hook_log_sink)"

WRIT_HOST="${WRIT_HOST:-localhost}"
WRIT_PORT="${WRIT_PORT:-8765}"
WRIT_URL="http://${WRIT_HOST}:${WRIT_PORT}/query"

HOOK_START_NS=$(hook_timer_start)

# Read stdin once
STDIN_DATA=$(cat)

# Extract session ID. json_transform, not a python spawn: this hook fires on every
# write and an interpreter start costs ~15ms against jq's ~3ms. The `if/else` spells out
# python's `or`, which falls through on the empty string where jq's `//` would keep it.
SESSION_ID=$(printf '%s' "$STDIN_DATA" | json_transform \
    'if (.agent_id // "") != "" then .agent_id else (.session_id // "") end' \
    "d.get('agent_id','') or d.get('session_id','')" 2>/dev/null)
# NO SYNTHESIZED ID. This used to call `detect_session_id ""`, which invented one from
# PPID or md5(cwd:user). Every remaining step here is session-keyed -- the budget read,
# the should-skip check, and the `update` that banks this turn's rule ids -- and
# _cache_path() has no empty-id guard, so continuing would file all of it under a cache
# named for the empty string. This hook injects rules; it gates nothing, so stopping
# costs an injection and denies nothing.
if [ -z "$SESSION_ID" ]; then
    writ_critical writ-posttool-rag "no session_id in hook payload; skipping post-write rule injection"
    exit 0
fi

# A4: ONE session-cache read for the whole hook (was two -- this orchestrator
# check, then a second read for budget/exclusion/mode at the query step). The
# cache is not mutated before the update at the end, so $CACHE is reused. The
# orchestrator early-exit derives from it via the jq-first parsed_bool helper (no
# python spawn); a server-down read yields '{}' -> not orchestrator.
CACHE=$(_writ_session read "$SESSION_ID" 2>/dev/null || echo '{}')
if parsed_bool "$CACHE" "is_orchestrator"; then
    exit 0  # orchestrator writes are metadata-only: no RAG
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

# Extract budget + exclusion list + mode from the SINGLE $CACHE read at the top
# (A4: no second daemon round-trip). One python3 spawn emits the three fields on
# separate stdout lines.
CACHE_PARSE=$(echo "$CACHE" | python3 -c "
import sys, json
sys.path.insert(0, '$WRIT_DIR/bin/lib')
from writ_phase_scoped_rules import phase_scoped_ids
try:
    cache = json.load(sys.stdin)
except Exception:
    cache = {}
rule_ids = phase_scoped_ids(cache)
print(json.dumps(rule_ids))
print(cache.get('remaining_budget', 8000))
print(cache.get('mode') or '')
" 2>/dev/null)
LOADED_RULE_IDS=$(echo "$CACHE_PARSE" | sed -n '1p')
LOADED_RULE_IDS="${LOADED_RULE_IDS:-[]}"
REMAINING_BUDGET=$(echo "$CACHE_PARSE" | sed -n '2p')
REMAINING_BUDGET="${REMAINING_BUDGET:-8000}"
# L2: mode comes from the cache read above; no separate `mode get` round-trip
# (this hook fires on every write, so it drops one daemon call per write).
CURRENT_MODE=$(echo "$CACHE_PARSE" | sed -n '3p')
CURRENT_MODE=$(echo "$CURRENT_MODE" | tr -d '[:space:]')

MAX_POSTTOOL_BUDGET=1500
POSTTOOL_BUDGET=$((REMAINING_BUDGET < MAX_POSTTOOL_BUDGET ? REMAINING_BUDGET : MAX_POSTTOOL_BUDGET))

if [ "$POSTTOOL_BUDGET" -lt 200 ]; then
    exit 0
fi

# The project owning the file just written, so the daemon scopes retrieval to that
# project's records. Derived from the FILE, not from the hook's cwd: a write can land
# in another checkout, and the rules that govern it belong to the file's project.
# detect_project_root is pure bash, so this adds no interpreter start to a path that
# runs on every write.
PROJECT_ROOT=$(detect_project_root "$FILE_PATH")

# Build the /query request and POST it (shared helper; see bin/lib/common.sh).
RESPONSE=$(rag_query "$QUERY" "$POSTTOOL_BUDGET" "$LOADED_RULE_IDS" "$PROJECT_ROOT")

if [ -z "$RESPONSE" ]; then
    exit 0
fi

# Combined error + relevance check, via json_transform rather than an interpreter start.
# A malformed or empty response yields no output instead of the literal "error", and the
# next line treats anything other than "relevant" as "stop", so the branch taken is the
# same one the python spawn produced.
RESPONSE_CHECK=$(printf '%s' "$RESPONSE" | json_transform \
    'if has("error") then "error"
     elif ([.rules[]? | select((.score // 0) >= 0.4)] | length) > 0 then "relevant"
     else "irrelevant" end' \
    "'error' if 'error' in d else ('relevant' if [r for r in d.get('rules',[]) if r.get('score',0) >= 0.4] else 'irrelevant')" \
    2>/dev/null || echo "error")

if [ "$RESPONSE_CHECK" != "relevant" ]; then
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
    # #2: deliver via additionalContext. Plain stdout on PostToolUse reaches only
    # the CC debug log (verified delivery rule); additionalContext reaches the
    # model (OBSERVED via writ-bible-authoring-push). Additive, no decision.
    # jq builds this envelope when it can; the python heredoc stays as the fallback.
    # The value is passed with --arg / the environment, never spliced into the program
    # text, so a rule body containing quotes or backslashes cannot forge JSON.
    # basename is bash parameter expansion here: the fork cost 1.3ms against 0.018ms.
    # The two differ on a trailing slash (`/a/b/` gives "" where basename gives "b"),
    # which cannot occur for a written file path and would only alter this label.
    WRIT_AC="[Writ: post-write rules for ${FILE_PATH##*/}]
$RULES_TEXT"
    if [ -z "${WRIT_NO_JQ:-}" ] && command -v jq >/dev/null 2>&1; then
        # No breadcrumb sink on this arm: the python fallback below keeps one, and a
        # second redirect made this hook carry three where the debug-gating contract
        # counts two. jq -n with --arg cannot fail on input it is not given.
        jq -n -c --arg ac "$WRIT_AC" \
            '{hookSpecificOutput:{hookEventName:"PostToolUse",additionalContext:$ac}}' \
            2>/dev/null || true
    else
        WRIT_AC="$WRIT_AC" python3 <<'PY' 2>>"$WRIT_HOOK_LOG_SINK" || true
import json, os
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": os.environ.get("WRIT_AC", ""),
}}))
PY
    fi
fi

# Update session cache
if [ -n "$META_LINE" ]; then
    META_JSON="${META_LINE#WRIT_META:}"
    META_FIELDS=$(echo "$META_JSON" | parse_writ_meta)
    NEW_RULE_IDS=$(echo "$META_FIELDS" | sed -n '1p'); NEW_RULE_IDS="${NEW_RULE_IDS:-[]}"
    COST=$(echo "$META_FIELDS" | sed -n '2p'); COST="${COST:-0}"

    # A3: full rule objects (data prep), then ONE combined update -- was two
    # _writ_session update spawns (add-rules and a conditional add-rule-objects);
    # an empty [] rule-objects payload is a no-op in cmd_update.
    RULE_OBJECTS=$(echo "$RESPONSE" | extract_rule_objects)

    _writ_session update "$SESSION_ID" \
        --add-rules "$NEW_RULE_IDS" \
        --cost "$COST" \
        --inc-queries \
        --add-rule-objects "$RULE_OBJECTS" 2>>"$WRIT_HOOK_LOG_SINK" || true

    # Log rag_query event via the shared helper (centralizes JSON-array quoting and
    # restores the json.loads recovery this copy previously lacked -- D-RAGLOG).
    # CURRENT_MODE was derived from the cache read above (no extra `mode get` round-trip).
    log_rag_query_event "$SESSION_ID" "${CURRENT_MODE:-}" "file-write-post" "$COST" "$NEW_RULE_IDS" "" "PostToolUse" "additionalContext"
fi

hook_timer_end "$HOOK_START_NS" "writ-posttool-rag" "$SESSION_ID" "${CURRENT_MODE:-}"
exit 0
