#!/usr/bin/env bash
# Phase 2: enforce spec-review-before-code-quality-review (ENF-PROC-SDD-001).
#
# PreToolUse on Task when the dispatched subagent is a code reviewer.
# Denies dispatch if spec-reviewer for the current task hasn't completed.
# Feature-flag gated.
set -euo pipefail
HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
SESSION_HELPER="$WRIT_DIR/bin/lib/writ-session.py"
source "$WRIT_DIR/bin/lib/common.sh"

is_superpowers_absorb_enabled || exit 0

PARSED=$(parse_hook_stdin)
SESSION_ID=$(detect_session_id "$PARSED")
[ -z "$SESSION_ID" ] && exit 0

is_work_mode "$SESSION_ID" || exit 0

DENY=$(python3 <<PY
import json, sys
sys.path.insert(0, "$WRIT_DIR/bin/lib")
from importlib import util
spec = util.spec_from_file_location('writ_session', "$SESSION_HELPER")
mod = util.module_from_spec(spec); spec.loader.exec_module(mod)
parsed = json.loads('''$PARSED''')
ti = parsed.get("tool_input") or {}
agent_type = (ti.get("subagent_type") or "").lower()
if "code-review" not in agent_type and agent_type != "writ-code-reviewer":
    sys.exit(0)
session = mod._read_cache("$SESSION_ID")
state = session.get("review_ordering_state") or {}
# Default task key if not specified: use the current active task id or 'default'
task_id = ti.get("task_id") or session.get("active_phase") or "default"
if not state.get(task_id, {}).get("spec_reviewer_completed", False):
    print(f"ENF-PROC-SDD-001: code-quality review dispatched before spec-compliance review completed for task '{task_id}'. Run writ-spec-reviewer first, record its completion via /session/{{sid}}/review-ordering, then dispatch writ-code-reviewer.")
PY
)

if [ -n "$DENY" ]; then
    python3 -c "
import json
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': '''$DENY'''
    }
}))"
fi
exit 0
