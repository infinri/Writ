#!/usr/bin/env bash
# Writ CwdChanged hook -- fires when working directory changes
#
# Detects the project domain from marker files in the new cwd and stores
# it in the session cache as detected_domain. The RAG injection hook reads
# this field to pass a domain hint to the /query endpoint.
#
# Domain detection is heuristic (file-existence checks, no parsing).
#
# Hook type: CwdChanged
# Exit: always 0 (advisory only)

set -euo pipefail

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
SESSION_HELPER="$WRIT_DIR/bin/lib/writ-session.py"
source "$WRIT_DIR/bin/lib/common.sh"

HOOK_START_NS=$(hook_timer_start)

# Read stdin JSON envelope
STDIN_JSON=$(cat)

# Extract session_id and cwd from envelope
PARSED=$(echo "$STDIN_JSON" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    sid = data.get('agent_id', '') or data.get('session_id', '')
    cwd = data.get('cwd', '')
    print(f'{sid}\n{cwd}')
except Exception:
    print('\n')
" 2>/dev/null) || true

SESSION_ID=$(echo "$PARSED" | head -1)
NEW_CWD=$(echo "$PARSED" | sed -n '2p')

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

# Detect domain from marker files in the new cwd
# Priority order: composer.json, pyproject.toml, package.json, Cargo.toml, go.mod
DETECTED_DOMAIN="universal"
if [ -n "$NEW_CWD" ]; then
    if [ -f "$NEW_CWD/composer.json" ]; then
        DETECTED_DOMAIN="php"
    elif [ -f "$NEW_CWD/pyproject.toml" ]; then
        DETECTED_DOMAIN="python"
    elif [ -f "$NEW_CWD/package.json" ]; then
        DETECTED_DOMAIN="javascript"
    elif [ -f "$NEW_CWD/Cargo.toml" ]; then
        DETECTED_DOMAIN="rust"
    elif [ -f "$NEW_CWD/go.mod" ]; then
        DETECTED_DOMAIN="go"
    fi
fi

# Update session cache with detected domain
python3 -c "
import sys, json, os, tempfile
session_id = sys.argv[1]
domain = sys.argv[2]
cache_dir = os.environ.get('WRIT_CACHE_DIR', tempfile.gettempdir())
path = os.path.join(cache_dir, f'writ-session-{session_id}.json')
try:
    with open(path) as f:
        cache = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    cache = {}
cache['detected_domain'] = domain
tmp = path + '.tmp'
with open(tmp, 'w') as f:
    json.dump(cache, f)
os.rename(tmp, path)
" "$SESSION_ID" "$DETECTED_DOMAIN" 2>/dev/null || true

# Log friction event
log_friction_event "$SESSION_ID" "${CURRENT_MODE:-}" "cwd_changed" \
    "{\"detected_domain\":\"$DETECTED_DOMAIN\",\"cwd\":\"$NEW_CWD\"}"

hook_timer_end "$HOOK_START_NS" "writ-cwd-changed" "$SESSION_ID" "${CURRENT_MODE:-}"
exit 0
