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

# Session ID: prefer the stdin envelope (Claude Code passes session_id), fall
# back to grandparent PID, then to the deterministic cwd+user hash.
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

# Reset phase exclusion list and budget so rules re-inject after the window
# is freed. The statusLine maintains context_percent independently. Capture the
# result (mode + phase) so we can re-state the workflow position below.
RESET_OUT=$(_writ_session reset-after-compaction "$SESSION_ID" 2>/dev/null || echo '{}')
echo "$RESET_OUT" >> "/tmp/writ-postcompact-${SESSION_ID}.log" 2>/dev/null || true

# State-rehydration: the next rag-inject only fires on the next user prompt, so
# an agent resuming autonomously post-compact has no workflow bearings. Re-state
# mode + phase (one python3 spawn -> two lines). Emitted only when a mode is set.
STATE_FIELDS=$(echo "$RESET_OUT" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
print(d.get('mode') or '')
print(d.get('phase') or '')
" 2>/dev/null)
PC_MODE=$(echo "$STATE_FIELDS" | sed -n '1p')
PC_PHASE=$(echo "$STATE_FIELDS" | sed -n '2p')
if [ -n "$PC_MODE" ]; then
    echo "[Writ: post-compact workflow state] mode=$PC_MODE, phase=$PC_PHASE. The next user turn re-injects the full rule set; treat this as your current workflow position."
    echo
fi

# Phase 4c (PSR-004 follow-up): emit a verify-discipline directive so
# the model treats recalled verification output as second-hand after
# compaction. Without this, "Is it working?" post-compact gets answered
# from pre-compact memory ("last run was N tests passing") rather than
# fresh evidence. See docs/pressure-runs/2026-04-22/PSR-004/analysis.md.
cat <<'DIRECTIVE'
[Writ: context compacted]
Until the next compaction, treat any pre-compact verification output (test counts,
"passing" claims, file reads) as second-hand evidence.

If asked "is it working?" / "is it done?" / "did it pass?":
  1. Re-run the relevant verification (tests, lint, typecheck, smoke command) FIRST.
  2. If the re-run is BLOCKED (tool rejection, permission denied, env unavailable):
       STOP. Do NOT answer "yes", "passing", or "should be working".
       Respond instead: "Re-verification was blocked by [reason]. I cannot confirm
       post-compact. Pre-compact context says X but I have no fresh evidence.
       Want me to verify another way?"
  3. Only answer affirmatively with fresh test/lint output cited inline.

Saying "yes" / "passing" / "all good" without fresh evidence is a forbidden response
in this state. Recalled output is not fresh evidence.
DIRECTIVE

# Mode for hook_execution telemetry (audit #5).
MODE=$(_writ_session "mode get" "$SESSION_ID" 2>/dev/null || echo "")
MODE=$(echo "$MODE" | tr -d '[:space:]')
hook_timer_end "$HOOK_START_NS" "writ-postcompact" "$SESSION_ID" "${MODE:-}"
exit 0
