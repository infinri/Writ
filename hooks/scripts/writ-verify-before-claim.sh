#!/usr/bin/env bash
# ENF-PROC-VERIFY-001: surface completion claims that lack verification.
#
# Re-anchored (#1) from the dead `PreToolUse TodoWrite` gate -- TodoWrite does not
# exist in CC 2.1.183, and TaskCreate/TaskUpdate fire NO PreToolUse hook -- to the
# Stop event, the live "end of turn = completion claim" surface.
#
# Loop-safe by construction: surfaces via stderr + `exit 1` (the writ-run-pending-
# tests pattern), NEVER hookSpecificOutput.additionalContext on Stop -- CC treats a
# Stop hook's additionalContext as turn-continue, which loops to the Stop block cap.
# Also guarded by stop_hook_active so a continuation Stop is a no-op. POL-5e: emits
# ONLY when actionable.
#
# Check: any artifact whose Gate 5 quality self-review scored < 3 and was not
# overridden blocks a clean completion claim. The feeder is writ-quality-judge,
# which (since #2) delivers its self-review directive via additionalContext, so the
# agent actually receives it and POSTs the score to /session/{sid}/quality-judgment.
# The "tests must pass" half of verification is enforced by writ-run-pending-tests
# (ENF-TEST-001); claim-phrase discipline by FRB-COMMS-002.
#
# Hook type: Stop (matcher: none). Work mode only.
set -euo pipefail
HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
SESSION_HELPER="$WRIT_DIR/bin/lib/writ-session.py"
source "$WRIT_DIR/bin/lib/common.sh"
hook_instrument "writ-verify-before-claim"

# WRIT_HOOK_LOG stderr breadcrumb sink, gated by WRIT_DEBUG: /dev/null when unset,
# ${WRIT_HOOK_LOG:-/tmp/writ-hooks.log} when WRIT_DEBUG=1 (single source: common.sh).
WRIT_HOOK_LOG_SINK="$(hook_log_sink)"

STDIN_JSON=$(cat)
stop_hook_active "$STDIN_JSON" && exit 0

SESSION_ID=$(detect_session_id "$STDIN_JSON")
[ -z "$SESSION_ID" ] && exit 0
is_work_mode "$SESSION_ID" || exit 0

SURFACE=$(WRIT_DIR_ABS="$WRIT_DIR" \
    WRIT_SESSION_HELPER="$SESSION_HELPER" \
    WRIT_SESSION_ID="$SESSION_ID" \
    python3 <<'PY' 2>>"$WRIT_HOOK_LOG_SINK"
import os, sys
from importlib import util
writ_dir = os.environ.get("WRIT_DIR_ABS", "")
session_helper = os.environ.get("WRIT_SESSION_HELPER", "")
session_id = os.environ.get("WRIT_SESSION_ID", "")
sys.path.insert(0, os.path.join(writ_dir, "bin", "lib"))
spec = util.spec_from_file_location("writ_session", session_helper)
mod = util.module_from_spec(spec); spec.loader.exec_module(mod)
session = mod._read_cache(session_id)
judgments = session.get("quality_judgment_state") or {}
failing = [
    path for path, j in judgments.items()
    if isinstance(j, dict) and j.get("score", 5) < 3 and not j.get("overridden")
]
if failing:
    print("ENF-PROC-VERIFY-001 / Gate 5: do not claim this work complete, these "
          "artifacts scored < 3 on quality self-review and were not overridden: "
          + ", ".join(failing)
          + ". Fix them (re-review and re-POST the score) or pass "
          "--override-quality-judge, then verify.")
PY
)

if [ -n "$SURFACE" ]; then
    log_gate_decision "verify-before-claim" "deny" "$SURFACE" "${SESSION_ID:-}"
    echo "$SURFACE" >&2
    exit 1
fi
log_gate_decision "verify-before-claim" "allow" "no failing quality judgments" "${SESSION_ID:-}"
exit 0
