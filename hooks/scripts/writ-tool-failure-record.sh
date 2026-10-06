#!/usr/bin/env bash
# Program item 7c, the COUNTING half of the tool-failure budget (PostToolUseFailure, matcher
# .*). A failed tool call extends this agent's streak for that tool in the session cache when
# it repeats the last failure's input exactly, and restarts it at 1 when it does not.
# writ-tool-failure-budget.sh reads the streak and refuses the fourth identical call. The
# count, the key and the input hash live in bin/lib/writ_tool_failure.py.
#
# THE writ-blackbox-capture.sh SHAPE (its lines 43 to 46): read stdin once, parse it with
# _writ_parse_hook_stdin plus eval. NOT load_hook_env, which would run the sub-agent seeder:
# a failure counter must not become a writer of governance state. The parser's HOOK_SESSION_ID
# picks the cache file and its HOOK_ENVELOPE is what the helper hashes, exactly as the budget
# hook does, so both sides hash the same normalized input. The raw text is kept for one thing
# only: `is_interrupt`, which the normalized envelope does not carry. A call the USER
# interrupted did not fail; a spelling the literal probe misses is counted, the conservative
# direction.
#
# FAIL OPEN: a session or sub-agent with no cache records nothing, and the helper never creates
# or rewrites a cache it could not read. Payloads cross on stdin only
# (docs/adr/ADR-hook-exec-argument-boundary.md); argv carries the parser's bounded ids.
#
# Hook type: PostToolUseFailure. Exit: always 0. Stdout: always empty.
set -euo pipefail
HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
source "$WRIT_DIR/bin/lib/common.sh"
hook_instrument "writ-tool-failure-record"

STDIN_DATA=$(cat)
eval "$(printf '%s' "$STDIN_DATA" | _writ_parse_hook_stdin)"
SESSION_ID="${HOOK_SESSION_ID:-}"
[ -n "$SESSION_ID" ] || exit 0
case "$STDIN_DATA" in
    *'"is_interrupt":true'* | *'"is_interrupt": true'*) exit 0 ;;
esac
[ -f "$(writ_session_cache_dir)/writ-session-${SESSION_ID}.json" ] || exit 0

printf '%s' "${HOOK_ENVELOPE:-}" \
    | python3 "$WRIT_DIR/bin/lib/writ_tool_failure.py" record "$SESSION_ID" "${HOOK_AGENT_ID:-}" \
    >/dev/null 2>&1 || true
exit 0
