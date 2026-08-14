#!/usr/bin/env bash
# Validate plan.md before allowing exit from plan mode.
#
# PreToolUse hook, matcher: ExitPlanMode
#
# When Claude tries to exit /plan mode, this hook validates that plan.md
# exists and contains all required sections (## Files, ## Analysis,
# ## Rules Applied, ## Capabilities). If validation fails, the hook
# denies the exit and Claude stays in plan mode to fix the plan.
#
# This hook validates FORMAT only. It does NOT create the phase-a gate.
# The user must say "approved" after reviewing the plan's substance.
# auto-approve-gate.sh creates the gate on user approval.

set -euo pipefail

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
SESSION_HELPER="$WRIT_DIR/bin/lib/writ-session.py"
FA="$WRIT_DIR/bin/lib/friction-append.py"
source "$WRIT_DIR/bin/lib/common.sh"

# WRIT_HOOK_LOG stderr breadcrumb sink, gated by WRIT_DEBUG: /dev/null when unset,
# ${WRIT_HOOK_LOG:-/tmp/writ-hooks.log} when WRIT_DEBUG=1 (single source: common.sh).
WRIT_HOOK_LOG_SINK="$(hook_log_sink)"

# Read stdin envelope
STDIN_JSON=$(cat)

# Payload only, NO fallback. This had both of the failure shapes: /tmp/writ-current-session
# names whichever Claude Code session on this machine took a turn most recently, and
# md5(cwd:user)+date names a session that has never existed. This hook decides whether a
# PLAN is approved, so approving against the wrong session, or against a phantom one, is a
# governance failure that leaves no trace. The shared helper also replaces an inline
# python3 -c that cost an interpreter start to read one field.
SESSION_ID=$(writ_require_session "$STDIN_JSON" validate-exit-plan) || exit 0

# Check if mode is Work. Only Work mode requires plan validation.
# If no mode or non-work mode, allow exit -- Writ doesn't gate /plan usage.
CURRENT_MODE=$(_writ_session "mode get" "$SESSION_ID" 2>/dev/null || echo "")
CURRENT_MODE=$(echo "$CURRENT_MODE" | tr -d '[:space:]')

if [ "$CURRENT_MODE" != "work" ]; then
    # Not in Work mode -- allow exit, no plan validation needed
    exit 0
fi

# Detect project root
PROJECT_ROOT=$(detect_project_root "$(pwd -P)")

if [ -z "$PROJECT_ROOT" ]; then
    exit 0
fi

# Validate plan.md using the same validator as phase-a gate
VALIDATION_ERROR=$(python3 -c "
import sys
sys.path.insert(0, '$WRIT_DIR/bin/lib')
from importlib import util
spec = util.spec_from_file_location('writ_session', '$SESSION_HELPER')
mod = util.module_from_spec(spec)
spec.loader.exec_module(mod)
error = mod._validate_phase_a('$PROJECT_ROOT', '$SESSION_ID')
if error:
    print(error)
" 2>/dev/null) || true

if [ -n "$VALIDATION_ERROR" ]; then
    # Log exitplanmode_denial
    python3 -c "
import json, sys, os
from datetime import datetime, timezone
entry = {
    'ts': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    'session': sys.argv[1],
    'mode': 'work',
    'event': 'exitplanmode_denial',
    'reason': sys.argv[2][:200],
}
print(json.dumps(entry))
" "$SESSION_ID" "$VALIDATION_ERROR" 2>/dev/null | python3 "$FA" --stdin-json 2>/dev/null || true

    # Deny exit -- Claude stays in plan mode to fix the plan
    python3 -c "
import json, sys
result = {
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': sys.argv[1]
    }
}
print(json.dumps(result))
" "$VALIDATION_ERROR"
    exit 0
fi

# Reset task phase: ExitPlanMode validation succeeded, so this is the
# canonical "fresh plan = fresh task" signal. Without this, current_phase
# can carry over from a prior task (e.g. `implementation` or `complete`)
# and the next /advance-phase silently consumes the wrong transition.
# See tests/test_exit_plan_phase_reset.py and commit 33e0adc for context.
_writ_session update "$SESSION_ID" \
    --reset-task-phase 2>>"$WRIT_HOOK_LOG_SINK" || true

# Log exitplanmode_allow
log_friction_event "$SESSION_ID" "work" "exitplanmode_allow"

# Plan is valid -- allow exit from /plan mode.
# The phase-a gate is NOT created here. The user must review the plan
# and say "approved" for auto-approve-gate.sh to create the gate.
# PreToolUse plain stdout reaches only the CC debug log (writ/shared/delivery.py::
# STDOUT_TO_MODEL_EVENTS), so this directive rode a dead channel while the deny
# path above used permissionDecisionReason and did reach the model. Same
# additionalContext shape as writ-read-rag.sh, the PreToolUse precedent. No
# permissionDecision key: absent means no decision is expressed, so ExitPlanMode
# still proceeds exactly as it does today.
python3 <<'PY' || true
import json

print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "additionalContext": (
            "[WRIT WORKFLOW -- MANDATORY] Plan format validated. "
            "You are NOT approved to write code.\n"
            "NEXT STEPS IN ORDER:\n"
            "1. Present a brief plan summary to the user\n"
            "2. Say \"Say approved to proceed\"\n"
            "3. WAIT -- do not call Write or Edit until the user says \"approved\"\n"
            "Attempting to write before approval WILL be denied."
        ),
    }
}))
PY

exit 0
