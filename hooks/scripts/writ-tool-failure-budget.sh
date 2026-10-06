#!/usr/bin/env bash
# Program item 7c, the REFUSING half of the tool-failure budget, on two events:
#
#   PreToolUse  (matcher .*)  refuse the call when THIS agent's last three failures of THIS
#                             tool were the identical call and this call is that call again.
#   PostToolUse (matcher .*)  a SUCCESSFUL call ends the streak: the same tool clears its own
#                             entry; a successful Write, Edit or NotebookEdit clears every entry
#                             this agent holds, because it changed a file the failing call reads.
#
# The counting half is writ-tool-failure-record.sh (PostToolUseFailure). Key, hash and count
# live in bin/lib/writ_tool_failure.py; design and rejected alternatives in
# docs/adr/ADR-tool-failure-budget.md.
#
# PARSED WITH _writ_parse_hook_stdin PLUS eval, the writ-blackbox-capture.sh pattern, NOT
# load_hook_env: this hook runs on every tool call, and load_hook_env would run the sub-agent
# seeder each time. The parser yields HOOK_SESSION_ID (the cache file), HOOK_AGENT_ID,
# HOOK_TOOL_NAME, HOOK_EVENT and HOOK_ENVELOPE, whose normalized tool_input is what the helper
# hashes on both sides.
#
# THE HOT PATH STARTS NO PYTHON, and the read that makes that true is NEW here, not reuse:
# `$(<file)` plus a literal `case` test for this agent's key, which forks no external program.
# (writ_session_mode_pair in bin/lib/common.sh reads the same file but through json_transform,
# which spawns the JSON filter or python; that cost is what this avoids.) Only a key that is
# PRESENT (this tool failed and has not succeeded since) spends the python start that compares
# the input hash. Pinned by tests/test_tool_failure_budget.py::TestTheHotPathStartsNoPython.
#
# FAIL OPEN. No session id, no cache file, an unreadable cache, a key spelled in a way the
# literal test does not see, or a helper that crashes all ALLOW. This is a loop breaker, not a
# security boundary: the worst case of a miss is one more identical failure.
#
# NO SECOND IRREVERSIBILITY CLASSIFIER. An exact repeat of an irreversible Bash call is already
# refused by writ-bash-write-gate.sh, which refuses every command its classifier matches on
# every attempt; nothing here tracks or re-detects those commands.
#
# Hook type: PreToolUse + PostToolUse. Exit: always 0 (refusal via emit_deny JSON).
set -euo pipefail
HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
source "$WRIT_DIR/bin/lib/common.sh"
hook_instrument "writ-tool-failure-budget"

eval "$(_writ_parse_hook_stdin)"
SESSION_ID="${HOOK_SESSION_ID:-}"
TOOL_NAME="${HOOK_TOOL_NAME:-}"
[ -n "$SESSION_ID" ] || exit 0
[ -n "$TOOL_NAME" ] || exit 0

STREAK_KEY="${HOOK_AGENT_ID:-main}|${TOOL_NAME}"
CACHE_FILE="$(writ_session_cache_dir)/writ-session-${SESSION_ID}.json"
[ -f "$CACHE_FILE" ] || exit 0
CACHE_TEXT="$(<"$CACHE_FILE")" || CACHE_TEXT=""

# json.dump's default separators put one space after the colon; the compact spelling is
# matched too, so a writer that ever changes that is not a silent miss. Quoted pattern text
# is literal in a case pattern, so the `|` inside the key is not an alternation.
_streak_key_present() {
    case "$CACHE_TEXT" in
        *"\"$STREAK_KEY\": {"* | *"\"$STREAK_KEY\":{"*) return 0 ;;
    esac
    return 1
}
_any_streak_present() {
    case "$CACHE_TEXT" in
        *'"tool_failure_streak": {"'* | *'"tool_failure_streak":{"'*) return 0 ;;
    esac
    return 1
}

case "${HOOK_EVENT:-}" in
    PreToolUse)
        _streak_key_present || exit 0
        BUDGET_REASON=$(printf '%s' "${HOOK_ENVELOPE:-}" \
            | python3 "$WRIT_DIR/bin/lib/writ_tool_failure.py" check "$SESSION_ID" \
                "${HOOK_AGENT_ID:-}" 2>/dev/null) || BUDGET_REASON=""
        if [ -n "$BUDGET_REASON" ]; then
            log_gate_decision "tool-budget" "deny" "$BUDGET_REASON" "$TOOL_NAME"
            emit_deny "$BUDGET_REASON"
        fi
        ;;
    PostToolUse)
        case "$TOOL_NAME" in
            Write|Edit|NotebookEdit) _any_streak_present || exit 0 ;;
            *) _streak_key_present || exit 0 ;;
        esac
        printf '%s' "${HOOK_ENVELOPE:-}" \
            | python3 "$WRIT_DIR/bin/lib/writ_tool_failure.py" reset "$SESSION_ID" \
                "${HOOK_AGENT_ID:-}" >/dev/null 2>&1 || true
        ;;
esac
exit 0
