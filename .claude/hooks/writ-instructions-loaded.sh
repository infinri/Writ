#!/usr/bin/env bash
# Writ InstructionsLoaded hook -- fires when CLAUDE.md files are loaded
#
# Scans the instructions content for Writ rule ID patterns and rule-like
# keywords. Detected rule IDs are stored in the session cache as
# instructions_rule_ids. The RAG injection hook merges these into the
# exclusion list to prevent re-injecting rules already present in CLAUDE.md.
#
# Rule ID pattern: [A-Z]+-[A-Z]+-\d{3} (e.g., ARCH-ORG-001, ENF-POST-007)
# Keywords: WHEN:, RULE:, VIOLATION:, TRIGGER:
#
# Hook type: InstructionsLoaded
# Exit: always 0 (advisory only)

set -euo pipefail

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
SESSION_HELPER="$WRIT_DIR/bin/lib/writ-session.py"
source "$WRIT_DIR/bin/lib/common.sh"

HOOK_START_NS=$(hook_timer_start)

# Read stdin JSON envelope
STDIN_JSON=$(cat)

# Extract session_id, instructions content, and scan for rule IDs
PARSED=$(echo "$STDIN_JSON" | python3 -c "
import sys, json, re

try:
    data = json.load(sys.stdin)
    sid = data.get('agent_id', '') or data.get('session_id', '')

    # Extract instructions content from envelope
    instructions = data.get('instructions', data.get('content', ''))

    # Scan for rule ID patterns: [A-Z]+-[A-Z]+-\d{3}
    # Also matches multi-segment compound IDs like FW-M2-RT-003
    # Segments can contain digits after the first uppercase letter (e.g., M2)
    rule_ids = re.findall(r'[A-Z][A-Z0-9]*(?:-[A-Z][A-Z0-9]*)*-[A-Z][A-Z0-9]*-\d{3}', instructions)
    all_ids = list(dict.fromkeys(rule_ids))  # deduplicate, preserve order

    # Detect rule-like keywords as a secondary signal
    keywords_found = []
    for kw in ['WHEN:', 'RULE:', 'VIOLATION:', 'TRIGGER:']:
        if kw in instructions:
            keywords_found.append(kw)

    print(json.dumps({
        'session_id': sid,
        'rule_ids': all_ids,
        'keywords': keywords_found,
        'has_rule_content': bool(all_ids or keywords_found),
    }))
except Exception:
    print(json.dumps({
        'session_id': '',
        'rule_ids': [],
        'keywords': [],
        'has_rule_content': False,
    }))
" 2>/dev/null) || PARSED='{"session_id":"","rule_ids":[],"keywords":[],"has_rule_content":false}'

SESSION_ID=$(echo "$PARSED" | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || echo "")
RULE_IDS=$(echo "$PARSED" | python3 -c "import sys,json; print(json.dumps(json.load(sys.stdin).get('rule_ids',[])))" 2>/dev/null || echo "[]")
RULE_COUNT=$(echo "$PARSED" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('rule_ids',[])))" 2>/dev/null || echo "0")

# Fallback session ID
if [ -z "$SESSION_ID" ]; then
    SESSION_ID=$(ps -o ppid= -p $PPID 2>/dev/null | tr -d ' ')
fi
if [ -z "$SESSION_ID" ]; then
    SESSION_ID=$(echo "${PWD}:${USER}" | md5sum | cut -c1-12)-$(date +%Y%m%d)
fi

# Read current mode for friction logging
CURRENT_MODE=$(_writ_session "mode get" "$SESSION_ID" 2>/dev/null || echo "")
CURRENT_MODE=$(echo "$CURRENT_MODE" | tr -d '[:space:]')

# Store detected rule IDs in session cache (replace, not append)
python3 -c "
import sys, json, os, tempfile
session_id = sys.argv[1]
rule_ids = json.loads(sys.argv[2])
cache_dir = os.environ.get('WRIT_CACHE_DIR', tempfile.gettempdir())
path = os.path.join(cache_dir, f'writ-session-{session_id}.json')
try:
    with open(path) as f:
        cache = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    cache = {}
cache['instructions_rule_ids'] = rule_ids
tmp = path + '.tmp'
with open(tmp, 'w') as f:
    json.dump(cache, f)
os.rename(tmp, path)
" "$SESSION_ID" "$RULE_IDS" 2>/dev/null || true

# Log friction event with count of detected rule IDs
log_friction_event "$SESSION_ID" "${CURRENT_MODE:-}" "instructions_loaded" \
    "{\"rule_ids_count\":$RULE_COUNT,\"rule_ids\":$RULE_IDS}"

hook_timer_end "$HOOK_START_NS" "writ-instructions-loaded" "$SESSION_ID" "${CURRENT_MODE:-}"
exit 0
