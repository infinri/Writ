#!/bin/bash
# Exit codes: 0=pass, 1=warning (advisory -- deliberate, not blocking)
#
# Universal validation hook -- routes by file extension.
# PostToolUse: fires after every Write/Edit/MultiEdit.
# Exit non-zero = Claude Code injects error into context. Must fix before continuing.
# Output: structured JSON per finding.
#
# Delegates to bin/run-analysis.sh for the actual analysis.
# This hook extracts the file path from tool input and calls the shared script.

SKILL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
source "$SKILL_DIR/bin/lib/common.sh"
hook_instrument "validate-file"

# Parse the Claude Code hook stdin envelope (one python3 spawn)
load_hook_env
FILE="$HOOK_FILE_PATH"

if [ -z "$FILE" ]; then exit 0; fi

# Skip validation if the write itself failed (tool_result_is_error = true).
# No point validating old file content when the write didn't land.
if [ "$HOOK_IS_ERROR" = "1" ]; then exit 0; fi

if [ ! -f "$FILE" ]; then exit 0; fi

# Check if the file type is one we analyze
lang=$(detect_language "$FILE")
if [ "$lang" = "unknown" ]; then exit 0; fi

PROJECT_ROOT=$(detect_project_root "$FILE")

# Run analysis via the shared script
OUTPUT=$("$SKILL_DIR/bin/run-analysis.sh" --project-root "$PROJECT_ROOT" "$FILE" 2>&1)
EXIT_CODE=$?

# Track file write for coverage analysis
SESSION_ID="$HOOK_SESSION_ID"
ANALYSIS_RESULT="pass"
if [ $EXIT_CODE -ne 0 ]; then
    ANALYSIS_RESULT="fail"
fi
# COVERAGE TRACKING IS SESSION-KEYED; THE ANALYSIS BELOW IS NOT. _cache_path() has no
# empty-id guard, so `update ""` would create a cache file named for the empty string and
# record this file against a session nothing can read back. Skip the bookkeeping, record
# why, and fall through -- the static-analysis deny at the end of this hook does not need
# a session id and must still fire.
if [ -n "$SESSION_ID" ]; then
    python3 "$SKILL_DIR/bin/lib/writ-session.py" update "$SESSION_ID" \
        --add-file "$FILE" \
        --add-file-result "$FILE" "$ANALYSIS_RESULT" 2>/dev/null || true
else
    writ_critical validate-file \
        "no session_id in hook payload; static analysis still runs, but this file is not recorded for coverage"
fi

if [ $EXIT_CODE -ne 0 ]; then
  # Full linter output -> per-session log; terse summary -> stderr.
  # emit-summary.py reads the log, surfaces the first error, references the
  # log path. Claude reads the full log only if the first error is ambiguous.
  SAFE_NAME=$(echo "$FILE" | tr '/' '_')
  # With no session id this used to be "$SKILL_DIR/cache/" plus a synthesized id; the id
  # is gone, and an unguarded "$SKILL_DIR/cache/$SESSION_ID" would resolve to the cache
  # ROOT and drop lint output beside the per-session directories. The literal name says
  # what happened, and emit-summary still gets a real path to point the agent at.
  #
  # THE ROOT FOLLOWS WRIT_CACHE_DIR. It was hardcoded to the live skill directory, so a
  # run that redirected Writ's cache somewhere disposable -- a test, an audit, a
  # sandboxed session -- still wrote into the real repo: an audit on 2026-08-08 set
  # WRIT_CACHE_DIR to a throwaway dir and afterwards found cache/test-audit-session-0001/
  # sitting in the checkout, written from this line. Isolation that one writer ignores is
  # not isolation.
  #
  # Deliberately NOT writ_session_cache_dir() from bin/lib/common.sh, whose fallback is
  # <skill>/var/session: that directory holds gate state (mode, approvals, the
  # manual-testing grant) and is what writ-state-write-gate.sh and the bash gate's
  # STATE_DIR_GUARD exist to protect. Lint output does not belong there, and emit-summary
  # hands this path to the agent to read. So: honour the env var, keep cache/ as the
  # default, which is also where the sibling pending-test hooks still write.
  LOG_DIR="${WRIT_CACHE_DIR:-$SKILL_DIR/cache}/${SESSION_ID:-no-session}"
  mkdir -p "$LOG_DIR"
  LOG_FILE="$LOG_DIR/${SAFE_NAME}.lint.json"
  echo "$OUTPUT" > "$LOG_FILE"
  python3 "$SKILL_DIR/bin/lib/emit-summary.py" \
    --format json \
    --log "$LOG_FILE" \
    --rule "ENF-POST-007" \
    --label "static-analysis errors in $FILE"
  log_gate_decision "static-analysis" "deny" "static-analysis errors in $FILE" "$FILE"
  exit 1
fi

log_gate_decision "static-analysis" "allow" "no static-analysis errors" "${FILE:-}"
exit 0
