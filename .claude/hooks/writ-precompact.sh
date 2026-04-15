#!/usr/bin/env bash
# Writ PreCompact hook -- fires before context window compaction
#
# Clears loaded_rules (full rule objects) from session cache to reduce
# Writ's footprint before compression. Keeps loaded_rule_ids and
# loaded_rule_ids_by_phase for feedback/coverage and exclusion logic.
#
# Hook type: PreCompact
# Exit: always 0 (cannot block compaction)

set -euo pipefail

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
SESSION_HELPER="$WRIT_DIR/bin/lib/writ-session.py"
source "$WRIT_DIR/bin/lib/common.sh"

HOOK_START_NS=$(hook_timer_start)

# Session ID: grandparent PID = the claude process
SESSION_ID=$(ps -o ppid= -p $PPID 2>/dev/null | tr -d ' ')
if [ -z "$SESSION_ID" ]; then
    SESSION_ID=$(echo "${PWD}:${USER}" | md5sum | cut -c1-12)-$(date +%Y%m%d)
fi

# Clear full rule objects, keep IDs
_writ_session clear-rules-for-compaction "$SESSION_ID" \
    >> "/tmp/writ-precompact-${SESSION_ID}.log" 2>/dev/null || true

hook_timer_end "$HOOK_START_NS" "writ-precompact" "$SESSION_ID" ""
exit 0
