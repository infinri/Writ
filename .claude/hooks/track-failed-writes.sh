#!/usr/bin/env bash
# PostToolUseFailure hook: track failed Write/Edit operations.
# Exit codes: always 0 (telemetry only, never blocks)
#
# Records {file, reason, timestamp} to the session cache failed_writes list.
# Logs write_failure event to friction log. Filters to Write/Edit tools only.
#
# Hook type: PostToolUseFailure
# Depends on: writ-session.py, bin/lib/common.sh

set -euo pipefail

SKILL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
source "$SKILL_DIR/bin/lib/common.sh"

HOOK_START_NS=$(hook_timer_start)

# Parse stdin envelope
PARSED=$(parse_hook_stdin)
TOOL_NAME=$(parsed_field "$PARSED" "tool_name")

# Only process Write and Edit failures
case "$TOOL_NAME" in
    Write|Edit) ;;
    *) exit 0 ;;
esac

# Extract file path and error reason
FILE_PATH=$(echo "$PARSED" | python3 -c "
import sys, json
d = json.load(sys.stdin)
ti = d.get('tool_input', {})
if isinstance(ti, str):
    import json as j2
    try: ti = j2.loads(ti)
    except: ti = {}
print(ti.get('file_path', ti.get('path', '')))
" 2>/dev/null || echo "")

ERROR_REASON=$(parsed_field "$PARSED" "error")
if [ -z "$ERROR_REASON" ]; then
    ERROR_REASON="unknown"
fi

# Read session ID
SESSION_ID="${WRIT_SESSION_ID:-}"
if [ -z "$SESSION_ID" ] && [ -f /tmp/writ-current-session ]; then
    SESSION_ID=$(cat /tmp/writ-current-session 2>/dev/null | tr -d '[:space:]')
fi
if [ -z "$SESSION_ID" ]; then
    exit 0
fi

# Build failed write record with ISO 8601 timestamp
TIMESTAMP=$(python3 -c "from datetime import datetime, timezone; print(datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))" 2>/dev/null || date -u '+%Y-%m-%dT%H:%M:%SZ')

RECORD=$(python3 -c "
import json, sys
print(json.dumps({
    'file': sys.argv[1],
    'reason': sys.argv[2],
    'timestamp': sys.argv[3],
}))
" "$FILE_PATH" "$ERROR_REASON" "$TIMESTAMP" 2>/dev/null || echo '{}')

# Append to session cache
_writ_session update "$SESSION_ID" --add-failed-write "$RECORD" 2>/dev/null || true

# Read mode for friction log
MODE=$(_writ_session "mode get" "$SESSION_ID" 2>/dev/null || echo "")
MODE=$(echo "$MODE" | tr -d '[:space:]')

# Log to friction log
log_friction_event "$SESSION_ID" "$MODE" "write_failure" \
    "{\"file\":\"$FILE_PATH\",\"reason\":\"$ERROR_REASON\",\"tool\":\"$TOOL_NAME\"}" 2>/dev/null || true

hook_timer_end "$HOOK_START_NS" "track-failed-writes" "$SESSION_ID" "$MODE"
exit 0
