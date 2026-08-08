#!/usr/bin/env bash
# PostToolUse on Write|Edit. Marks src/test files for end-of-turn test run.
# Companion hook: writ-run-pending-tests.sh (Stop) reads the marker.
set -euo pipefail
HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
source "$WRIT_DIR/bin/lib/common.sh"

load_hook_env
# Key the marker on the parent session id (raw session_id), not the worker's
# agent_id. In sub-agents the master orchestrator's Stop hook is the one that
# fires and reads cache/<parent-sid>/pending-tests.txt; HOOK_SESSION_ID prefers
# agent_id and would land the marker in the worker's cache, where no Stop hook
# ever reads it. HOOK_SESSION_ID_RAW is the raw envelope session_id.
PARENT_SID="$HOOK_SESSION_ID_RAW"
[ -z "$PARENT_SID" ] && exit 0
is_work_mode "$PARENT_SID" || exit 0
[ "$HOOK_IS_ERROR" = "1" ] && exit 0

FILE="$HOOK_FILE_PATH"
[ -z "$FILE" ] && exit 0

# All path knowledge lives in bin/lib/test_paths.py (config-driven via
# bundled defaults + optional .claude/writ.json per project).
TEST_PATHS_HELPER="$WRIT_DIR/bin/lib/test_paths.py"
MATCH=$(python3 "$TEST_PATHS_HELPER" match-src "$FILE" 2>/dev/null)
[ -z "$MATCH" ] && MATCH=$(python3 "$TEST_PATHS_HELPER" match-test "$FILE" 2>/dev/null)
[ -z "$MATCH" ] && exit 0

# THE ROOT FOLLOWS WRIT_CACHE_DIR, same as validate-file.sh. Hardcoded to the script's own
# location, this wrote into the live checkout however the caller redirected Writ's cache, so
# a test or an audit running against a throwaway directory still left cache/<sid>/ sitting in
# the repository. writ-run-pending-tests.sh reads this exact path and moved with it.
MARKER_DIR="${WRIT_CACHE_DIR:-$WRIT_DIR/cache}/$PARENT_SID"
mkdir -p "$MARKER_DIR"
echo "$FILE" >> "$MARKER_DIR/pending-tests.txt"

# Friction-log telemetry so "did the hook fire?" is answerable from the log.
log_friction_event "$PARENT_SID" "work" "hook_execution" \
    "{\"hook_name\":\"writ-mark-pending-test\",\"file_path\":\"$FILE\"}" \
    2>/dev/null || true
exit 0
