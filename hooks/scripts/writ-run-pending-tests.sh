#!/usr/bin/env bash
# Stop hook. Runs tests for files marked by writ-mark-pending-test.sh.
# Silent on pass. On failure, emits one-line summary via emit-summary.py.
# All test-path knowledge lives in bin/lib/test_paths.py (config-driven).
set -euo pipefail
HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
source "$WRIT_DIR/bin/lib/common.sh"
TEST_PATHS_HELPER="$WRIT_DIR/bin/lib/test_paths.py"

# Loop-breaker (CC Stop-hook contract): if CC is re-invoking us after a prior
# block, stop_hook_active is true -- exit 0 now so this Stop hook can never loop
# to CC's block cap. Capture stdin once, then feed it to load_hook_env (which
# reads the envelope from stdin) via a here-string so the parse still works.
STDIN_JSON=$(cat 2>/dev/null || echo '{}')
if stop_hook_active "$STDIN_JSON"; then
    exit 0
fi

load_hook_env <<< "$STDIN_JSON"
SESSION_ID="$HOOK_SESSION_ID"
[ -z "$SESSION_ID" ] && exit 0
is_work_mode "$SESSION_ID" || exit 0

# THE ROOT FOLLOWS WRIT_CACHE_DIR, and must resolve to the same directory
# writ-mark-pending-test.sh writes: this hook is the only reader of that marker, so the two
# expressions moving apart would silently stop the end-of-turn test run with both hooks
# still exiting 0. Hardcoded, both wrote into the live checkout under an isolated run.
CACHE_ROOT="${WRIT_CACHE_DIR:-$WRIT_DIR/cache}"
MARKER="$CACHE_ROOT/$SESSION_ID/pending-tests.txt"
[ -f "$MARKER" ] || exit 0

# Resolve every marker entry to a test file (or empty) via the helper.
TEST_FILES=$(while IFS= read -r p; do
    [ -z "$p" ] && continue
    python3 "$TEST_PATHS_HELPER" resolve-test "$p" 2>/dev/null
done < "$MARKER" | awk 'NF && !seen[$0]++')

: > "$MARKER"

RESOLVED_COUNT=$(printf '%s' "$TEST_FILES" | awk 'NF' | wc -l)

if [ -z "$TEST_FILES" ]; then
    log_friction_event "$SESSION_ID" "work" "hook_execution" \
        "{\"hook_name\":\"writ-run-pending-tests\",\"result_code\":0,\"resolved_count\":0}" \
        2>/dev/null || true
    exit 0
fi

LOG_DIR="$CACHE_ROOT/$SESSION_ID"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/last-test-run.log"
: > "$LOG"

# Group test files by runner. Bash 4+ associative arrays.
declare -A RUNNER_CMD RUNNER_CFG RUNNER_FILES RUNNER_FMT
while IFS= read -r tf; do
    [ -z "$tf" ] && continue
    INFO=$(python3 "$TEST_PATHS_HELPER" runner-for "$tf" 2>/dev/null)
    CMD=$(echo "$INFO" | sed -n '1p')
    CFG=$(echo "$INFO" | sed -n '2p')
    [ -z "$CMD" ] && continue
    KEY="${CMD}|${CFG}"
    RUNNER_CMD["$KEY"]="$CMD"
    RUNNER_CFG["$KEY"]="$CFG"
    RUNNER_FILES["$KEY"]="${RUNNER_FILES["$KEY"]:-}${RUNNER_FILES["$KEY"]:+ }$tf"
    case "$CMD" in
        *pytest*)    RUNNER_FMT["$KEY"]=pytest  ;;
        *phpunit*)   RUNNER_FMT["$KEY"]=phpunit ;;
        *"go test"*) RUNNER_FMT["$KEY"]=gotest  ;;
        *)           RUNNER_FMT["$KEY"]=pytest  ;;
    esac
done <<< "$TEST_FILES"

OVERALL_RC=0
SUMMARY_FMT=""

run_group() {
    local fmt="$1"; shift
    local cmd="$*"
    # `|| rc=$?` puts the redirected group into a `||` chain, which suspends
    # `set -e`. Without it, a non-zero exit from the runner (PHPUnit warnings,
    # test failures, etc.) aborts the script BEFORE we capture the rc -- so
    # the friction event and summary-emission below never run.
    local rc=0
    {
        echo "===== $fmt: $cmd ====="
        timeout 60s bash -c "$cmd" 2>&1
    } >> "$LOG" || rc=$?
    if [ $rc -ne 0 ]; then
        OVERALL_RC=$rc
        [ -z "$SUMMARY_FMT" ] && SUMMARY_FMT="$fmt"
    fi
}

for KEY in "${!RUNNER_CMD[@]}"; do
    CMD="${RUNNER_CMD[$KEY]}"
    CFG="${RUNNER_CFG[$KEY]}"
    FILES="${RUNNER_FILES[$KEY]}"
    FMT="${RUNNER_FMT[$KEY]}"
    if [ -n "$CFG" ]; then
        CMDLINE="$CMD -c $CFG $FILES"
    else
        CMDLINE="$CMD $FILES"
    fi
    run_group "$FMT" "$CMDLINE"
done

log_friction_event "$SESSION_ID" "work" "hook_execution" \
    "{\"hook_name\":\"writ-run-pending-tests\",\"result_code\":$OVERALL_RC,\"resolved_count\":$RESOLVED_COUNT}" \
    2>/dev/null || true

[ $OVERALL_RC -eq 0 ] && exit 0

# Before implementation begins, failing tests are EXPECTED (TDD: RED skeletons are
# written and approved RED, then made GREEN during implementation). The hook_execution
# telemetry above already recorded the result_code; do not surface those failures as a
# Stop "error" until the code lands. Only an implementation/complete-phase failure is a
# real regression worth nagging. (POL-5e: emit only when actionable now.)
#
# Read the phase FILE-DIRECT, not daemon-first: a divergent/cold daemon (e.g. after a
# crash or cache reset) can report a stale/empty phase, which previously let RED
# skeletons nag during the testing phase. The file cache is authoritative (Fix C).
SESSION_HELPER="$WRIT_DIR/bin/lib/writ-session.py"
PHASE=$(python3 "$SESSION_HELPER" current-phase "$SESSION_ID" 2>/dev/null | python3 -c "
import sys, json
try:
    print(json.load(sys.stdin).get('phase', ''))
except Exception:
    print('')
" 2>/dev/null || echo "")
case "$PHASE" in
    implementation|complete) ;;   # fall through: a failure here is a real regression
    *) exit 0 ;;                  # planning/testing/unclassified: RED is expected, stay silent
esac

# Decide pass/fail by parsing the log, not by trusting the runner's exit code.
# PHPUnit (and pytest --strict-warnings) can exit non-zero for environment
# warnings even when no actual tests failed. emit-summary.py prints only when
# it finds real failures; if its stderr is empty, treat as pass.
SUMMARY=$(python3 "$WRIT_DIR/bin/lib/emit-summary.py" \
    --format "${SUMMARY_FMT:-pytest}" \
    --log "$LOG" \
    --rule "ENF-TEST-001" \
    --label "test failure(s)" 2>&1)
[ -z "$SUMMARY" ] && exit 0
echo "$SUMMARY" >&2
exit 1
