#!/usr/bin/env bash
# Writ PostCompact hook -- fires after context window compaction
#
# Clears loaded_rule_ids_by_phase[current_phase] so rules will be
# re-injected on the next UserPromptSubmit. Resets remaining_budget
# to DEFAULT_SESSION_BUDGET (8000). This is the authoritative compaction
# signal; the Cycle A heuristic in writ-rag-inject.sh stays as fallback.
#
# Hook type: PostCompact
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

# Reset phase exclusion list and budget
_writ_session reset-after-compaction "$SESSION_ID" \
    >> "/tmp/writ-postcompact-${SESSION_ID}.log" 2>/dev/null || true

hook_timer_end "$HOOK_START_NS" "writ-postcompact" "$SESSION_ID" ""
exit 0
