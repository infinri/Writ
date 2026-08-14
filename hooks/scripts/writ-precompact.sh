#!/usr/bin/env bash
# Writ PreCompact hook -- fires before context window compaction
#
# Drops the now-stale loaded_rules full objects from the session cache,
# keeping loaded_rule_ids and loaded_rule_ids_by_phase for feedback/coverage
# and exclusion logic.
#
# NOTE: the session cache is a separate /tmp file. It is
# not part of the compacted context, so this does not shrink what the
# summarizer compresses; it is boundary hygiene (the conversation those
# objects annotated is being summarized away). A PreCompact hook also cannot
# steer compaction: its stdout is not injected into the summary and it has no
# additionalContext. PostCompact (writ-postcompact.sh) is the only hook whose
# output reaches the next turn, and it does so via
# hookSpecificOutput.additionalContext -- its bare stdout goes to the CC debug log
# like any other non-special event.
#
# Hook type: PreCompact
# Exit: always 0 (cannot block compaction)

set -euo pipefail

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
SESSION_HELPER="$WRIT_DIR/bin/lib/writ-session.py"
source "$WRIT_DIR/bin/lib/common.sh"

HOOK_START_NS=$(hook_timer_start)

# Session ID: from the stdin envelope (agent_id or session_id) and nowhere else.
# load_hook_env no longer synthesizes one from PPID or md5(cwd:user); it leaves the
# variable empty, and everything this hook does is keyed on it.
load_hook_env
SESSION_ID="$HOOK_SESSION_ID"

# Every line below writes or reads under $SESSION_ID: clear-rules-for-compaction MUTATES
# the cache, and _cache_path() has no empty-id guard, so it would create a cache named
# for the empty string; the log redirect would open "/tmp/writ-precompact-.log". Compaction
# is not blockable from here in any case (PreCompact stdout reaches nothing), so the
# honest response to an unidentifiable session is to record it and do nothing.
if [ -z "$SESSION_ID" ]; then
    writ_critical writ-precompact "no session_id in hook payload; stale rule objects are left in place"
    exit 0
fi

# Clear full rule objects, keep IDs
_writ_session clear-rules-for-compaction "$SESSION_ID" \
    >> "/tmp/writ-precompact-${SESSION_ID}.log" 2>/dev/null || true

# Mode for hook_execution telemetry (audit #5).
MODE=$(_writ_session "mode get" "$SESSION_ID" 2>/dev/null || echo "")
MODE=$(echo "$MODE" | tr -d '[:space:]')
hook_timer_end "$HOOK_START_NS" "writ-precompact" "$SESSION_ID" "${MODE:-}"
exit 0
