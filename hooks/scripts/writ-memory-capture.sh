#!/usr/bin/env bash
# Auto-memory graph mirror -- PostToolUse hook (matcher: Write|Edit).
#
# Every memory Claude Code writes to ~/.claude/projects/<project>/memory/<name>.md is
# mirrored into the Writ graph as a Memory record, so each memory exists twice: once in
# Claude Code's own per-project store (loaded into context by the harness) and once in
# the graph (cross-project, recallable, analyzable).
#
# PostToolUse is the load-bearing choice, not a convenience: it fires ONLY on writes
# that succeeded, so anything writ-memory-policy-guard.sh denied on PreToolUse never
# reaches this hook and never becomes a node. It is also why this hook is fail-open --
# the memory is already on disk, so a dead daemon must cost a friction event, never the
# write. `writ memory backfill` re-covers every recorded miss.
#
# Exit: always 0.
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
source "$SKILL_DIR/bin/lib/common.sh"
hook_instrument "writ-memory-capture"

MEMORY_CAPTURE="$SKILL_DIR/bin/lib/memory_capture.py"
WRIT_HOST="${WRIT_HOST:-localhost}"
WRIT_PORT="${WRIT_PORT:-8765}"
MEMORY_URL="http://${WRIT_HOST}:${WRIT_PORT}/memory-record"

load_hook_env

# A failed tool call must not re-mirror stale on-disk content with a fresh
# updated_at. PostToolUse firing only on success is OBSERVED behavior, not a
# documented guarantee, so guard is_error explicitly like the sibling
# PostToolUse hooks (validate-file.sh, writ-mark-pending-test.sh).
[ "${HOOK_IS_ERROR:-}" = "1" ] && exit 0

case "$HOOK_TOOL_NAME" in
  Write|Edit) ;;
  *) exit 0 ;;
esac

FILE_PATH="$HOOK_FILE_PATH"
[ -n "$FILE_PATH" ] || exit 0

# Same path family writ-memory-policy-guard.sh watches (any project's auto-memory).
case "$FILE_PATH" in
  */.claude/projects/*/memory/*.md) ;;
  *) exit 0 ;;
esac

# MEMORY.md is the index (a table of contents), not a memory. Excluded HERE, before any
# daemon attempt, so an index write never even tries to mirror and so a down daemon
# produces no friction event for it.
case "$(basename "$FILE_PATH")" in
  MEMORY.md) exit 0 ;;
esac

SESSION_ID="${HOOK_SESSION_ID:-unknown}"

# One friction event name for every way the mirror can miss, so "which memories are
# not in the graph" is answerable from the log alone. file_path/reason are JSON-encoded
# by python (a path may contain quotes), never interpolated into the JSON by the shell.
_record_miss() {
  local reason="$1" extra
  extra=$(WRIT_MEM_FILE="$FILE_PATH" WRIT_MEM_REASON="$reason" python3 -c \
    'import json, os; print(json.dumps({"file_path": os.environ.get("WRIT_MEM_FILE", ""), "error": os.environ.get("WRIT_MEM_REASON", "")}))' \
    2>/dev/null) || extra='{}'
  log_friction_event "$SESSION_ID" "" "memory_capture_failed" "$extra"
}

# Parse + payload build live in bin/lib/memory_capture.py, which the backfill CLI also
# binds to, so the two writers cannot drift. Status: 0 payload, 1 nothing to mirror
# (no frontmatter, no name), 2 the file could not be read.
set +e
PAYLOAD=$(python3 "$MEMORY_CAPTURE" payload "$FILE_PATH" "$SESSION_ID" 2>/dev/null)
PAYLOAD_STATUS=$?
set -e

if [ "$PAYLOAD_STATUS" -eq 2 ]; then
  # The write reportedly succeeded yet the file is unreadable: the mirror missed a
  # memory that exists, which is exactly what the backfill needs to know about.
  _record_miss "memory file unreadable"
  exit 0
fi

if [ "$PAYLOAD_STATUS" -ne 0 ] || [ -z "$PAYLOAD" ]; then
  exit 0
fi

if ! curl -sf --connect-timeout 0.2 --max-time 2 \
    -X POST "$MEMORY_URL" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD" >/dev/null 2>&1; then
  _record_miss "daemon unreachable at ${MEMORY_URL}"
fi

exit 0
