#!/usr/bin/env bash
# Phase 2: enforce verification-before-completion (ENF-PROC-VERIFY-001).
#
# PreToolUse on TodoWrite + Stop.
# Deny completion claims without fresh verification evidence recorded via
# POST /session/{sid}/verification-evidence. Feature-flag gated.
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

TOOL=$(parsed_field "$PARSED" "tool_name")

# Only evaluate on TodoWrite marking a todo completed, or on Stop events.
# For TodoWrite: parse the tool_input.todos and check if any transition to
# "completed" lacks verification_evidence.
DENY_REASON=""
if [ "$TOOL" = "TodoWrite" ]; then
    DENY_REASON=$(python3 <<PY
import json, sys
from pathlib import Path
# Load session state
sys.path.insert(0, "$WRIT_DIR/bin/lib")
from importlib import util
spec = util.spec_from_file_location('writ_session', "$SESSION_HELPER")
mod = util.module_from_spec(spec); spec.loader.exec_module(mod)
session = mod._read_cache("$SESSION_ID")
evidence = session.get("verification_evidence") or {}
parsed = json.loads('''$PARSED''')
tool_input = parsed.get("tool_input") or {}
todos = tool_input.get("todos") or []
for t in todos:
    tid = t.get("id") or t.get("content", "")[:40]
    status = t.get("status") or ""
    if status == "completed" and tid not in evidence:
        print(f"ENF-PROC-VERIFY-001: completion claim for '{tid}' has no verification_evidence. Run the check, then POST /session/{sid}/verification-evidence before marking completed.")
        break
PY
    )
fi

if [ -n "$DENY_REASON" ]; then
    python3 -c "
import json
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': '''$DENY_REASON'''
    }
}))"
fi
exit 0
