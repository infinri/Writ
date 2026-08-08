#!/usr/bin/env bash
# Writ SessionEnd hook -- fires once at session close (1.5s timeout)
#
# Consolidates session-level operations that previously ran on every Stop:
# 1. auto-feedback: correlate rules with analysis outcomes, POST to Writ
# 2. coverage: compute rule coverage report
# 3. gate metrics: write context metrics for recently approved gates (replaces log-session-metrics.sh)
# 4. session_end rollup: log final session totals to friction log
#
# Hook type: SessionEnd
# Exit: always 0

set -euo pipefail

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
SESSION_HELPER="$WRIT_DIR/bin/lib/writ-session.py"
FA="$WRIT_DIR/bin/lib/friction-append.py"
source "$WRIT_DIR/bin/lib/common.sh"

HOOK_START_NS=$(hook_timer_start)

# Session ID: from the stdin envelope (agent_id or session_id) and nowhere else.
# load_hook_env no longer synthesizes one from PPID or md5(cwd:user); it leaves the
# variable empty.
load_hook_env
SESSION_ID="$HOOK_SESSION_ID"

# THIS HOOK IS A ROLLUP OF ONE SESSION, so with no session id there is nothing to roll up.
# auto-feedback and coverage MUTATE the cache, and _cache_path() has no empty-id guard, so
# they would create a cache file named for the empty string; the two log redirects would
# open "/tmp/writ-feedback-.log" and "/tmp/writ-coverage-.log"; and the buffer flush would
# drain the wrong buffer. Record the broken invariant and stop.
if [ -z "$SESSION_ID" ]; then
    writ_critical writ-session-end "no session_id in hook payload; skipping the end-of-session rollup"
    exit 0
fi

# 0. Drain any hook_execution rows a turn left behind. This is the backstop for a turn
# that never reached Stop (crash, kill, disconnect): those rows are already on disk, and
# without this they would sit there until the session id happened to come round again.
# ERR-GRACEFUL-002: shutdown completes the in-progress work rather than dropping it.
writ_event_buffer_flush "$SESSION_ID" || true

# 1. Auto-feedback: correlate rules-in-context with analysis outcomes
_writ_session auto-feedback "$SESSION_ID" \
    >> "/tmp/writ-feedback-${SESSION_ID}.log" 2>/dev/null || true

# 2. Coverage report
_writ_session coverage "$SESSION_ID" \
    >> "/tmp/writ-coverage-${SESSION_ID}.log" 2>/dev/null || true

# 3. Gate metrics (replaces log-session-metrics.sh)
PROJECT_ROOT=$(detect_project_root "$(pwd)")
if [ -n "$PROJECT_ROOT" ]; then
    GATE_DIR="$PROJECT_ROOT/.claude/gates"
    METRICS_FILE="$PROJECT_ROOT/.claude/session-metrics.md"
    if [ -d "$GATE_DIR" ]; then
        for gate_file in "$GATE_DIR"/*.approved; do
            [ ! -f "$gate_file" ] && continue
            GATE_NAME=$(basename "$gate_file" .approved)
            TIMESTAMP=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
            mkdir -p "$(dirname "$METRICS_FILE")"
            printf '\n## Gate: %s -- %s\n' \
                "$GATE_NAME" "$TIMESTAMP" \
                >> "$METRICS_FILE" 2>/dev/null || true
        done
    fi
fi

# 4. Session end rollup: log final totals to friction log
CACHE=$(_writ_session read "$SESSION_ID" 2>/dev/null || echo '{}')
python3 -c "
import json, sys, os
from datetime import datetime, timezone

try:
    cache = json.loads(sys.argv[1])
except Exception:
    cache = {}

rules_loaded = len(cache.get('loaded_rule_ids', []))
violations = len(cache.get('pending_violations', []))
files_written = len(cache.get('files_written', []))
queries = cache.get('queries', 0)
mode = cache.get('mode')
phase = cache.get('current_phase')

entry = {
    'ts': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    'session': sys.argv[2],
    'mode': mode,
    'event': 'session_end',
    'rules_loaded': rules_loaded,
    'total_violations': violations,
    'files_written': files_written,
    'queries': queries,
    'final_phase': phase,
}

print(json.dumps(entry))
" "$CACHE" "$SESSION_ID" 2>/dev/null | python3 "$FA" --stdin-json 2>/dev/null || true

# Mode for hook_execution telemetry (audit #5): reuse $CACHE, no extra round-trip.
MODE=$(echo "$CACHE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('mode') or '')" 2>/dev/null || echo "")
hook_timer_end "$HOOK_START_NS" "writ-session-end" "$SESSION_ID" "${MODE:-}"
exit 0
