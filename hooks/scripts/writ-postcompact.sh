#!/usr/bin/env bash
# Writ PostCompact hook -- fires after context window compaction
#
# Clears loaded_rule_ids_by_phase[current_phase] so rules will be
# re-injected on the next UserPromptSubmit. Resets remaining_budget
# to DEFAULT_SESSION_BUDGET (8000). This is the authoritative compaction
# signal, and the only one: the writ-rag-inject.sh heuristic that once
# guessed at compaction was removed (it read an env var CC does not set).
#
# THIS HOOK EMITS NOTHING. It writes state and queues delivery; it does not
# deliver. CC's hook-output validator rejects a PostCompact hookSpecificOutput
# reply outright ("(root): Invalid input", observed on a real /compact
# 2026-08-14) and discards it, and bare stdout on PostCompact reaches only the
# CC debug log. cmd_reset_after_compaction therefore sets post_compact_pending,
# and writ-rag-inject.sh emits the state line + verify-discipline directive on
# the next UserPromptSubmit, which is a confirmed model channel.
#
# Hook type: PostCompact
# Exit: always 0 (cannot block compaction)

set -euo pipefail

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
SESSION_HELPER="$WRIT_DIR/bin/lib/writ-session.py"
source "$WRIT_DIR/bin/lib/common.sh"

HOOK_START_NS=$(hook_timer_start)

# Session ID: the stdin envelope only (Claude Code passes session_id). There is
# no fallback; an empty id is recorded and the hook stops (see below).
STDIN_DATA=$(cat 2>/dev/null || true)
SESSION_ID=""
if [ -n "$STDIN_DATA" ]; then
    SESSION_ID=$(echo "$STDIN_DATA" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
except Exception:
    data = {}
print((data.get('agent_id') or data.get('session_id') or '').strip())
" 2>/dev/null || echo "")
fi
# NO SYNTHESIZED SESSION ID. This used to fall back to the parent PID and then to
# md5(cwd:user)+date. Neither can ever equal the id Claude Code uses, so state written
# under one is written to a session that does not exist and is simply never read again,
# while the hook reports success. Claude Code documents session_id as universal and
# authoritative on every hook event, so an empty one is a broken invariant, not a case to
# paper over: record it and stop.
if [ -z "$SESSION_ID" ]; then
    writ_critical writ-postcompact "no session_id in hook payload; refusing to synthesize one"
    exit 0
fi

# Reset phase exclusion list and budget so rules re-inject after the window is freed, and
# queue the post-compaction directive (cmd_reset_after_compaction sets post_compact_pending
# in the same cache write, so queueing costs this hook no extra spawn). The statusLine
# maintains context_percent independently. The result is logged, not printed: stdout on this
# event is either discarded by the validator or debug-log-only, never a model channel.
RESET_OUT=$(_writ_session reset-after-compaction "$SESSION_ID" 2>/dev/null || echo '{}')
echo "$RESET_OUT" >> "/tmp/writ-postcompact-${SESSION_ID}.log" 2>/dev/null || true

# Mode for hook_execution telemetry (audit #5).
MODE=$(_writ_session "mode get" "$SESSION_ID" 2>/dev/null || echo "")
MODE=$(echo "$MODE" | tr -d '[:space:]')
hook_timer_end "$HOOK_START_NS" "writ-postcompact" "$SESSION_ID" "${MODE:-}"
exit 0
