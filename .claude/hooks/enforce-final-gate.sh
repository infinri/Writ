#!/bin/bash
# Blocks completion marker writes until ENF-GATE-FINAL has been verified.
# PreToolUse -- runs alongside check-gate-approval.sh.
# Output: structured JSON (Claude Code hookSpecificOutput contract).

SKILL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
source "$SKILL_DIR/bin/lib/common.sh"

deny_with_reason() {
  local reason="$1"
  python3 -c "
import json, sys
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': sys.argv[1]
    }
}))
" "$reason"
  exit 0
}

# Parse the Claude Code hook stdin envelope (consumes stdin once)
PARSED=$(parse_hook_stdin)
FILE=$(parsed_field "$PARSED" "file_path")

if [ -z "$FILE" ]; then exit 0; fi

PROJECT_ROOT=$(detect_project_root "$FILE")

if [[ "$FILE" == */plan.md || "$FILE" == *COMPLETE* ]]; then

  # --- Check 1: pending ENF-POST items in plan.md content -----------------
  PENDING_CONTENT=$(echo "$PARSED" | python3 -c "
import sys, json, os
data = json.load(sys.stdin)
ti = data.get('tool_input', {})
fp = data.get('file_path', '')
content = data.get('content', '')
if content:
    print(content)
elif ti.get('old_string') and os.path.exists(fp):
    with open(fp) as f:
        orig = f.read()
    print(orig.replace(ti['old_string'], ti.get('new_string', ''), 1))
" 2>/dev/null)

  if [ -n "$PENDING_CONTENT" ]; then
    if echo "$PENDING_CONTENT" | grep -iE "(static.analysis|ENF-POST-007|linter|type.check|integration.test)" | grep -qi "pending"; then
      deny_with_reason "[ENF-POST-007] plan.md contains pending ENF-POST items -- static analysis cannot be deferred. Fix: Complete static analysis before writing plan.md as complete"
    fi
  fi

  # --- Check 2: gate-final.approved must exist ----------------------------
  if [ ! -f "$PROJECT_ROOT/.claude/gates/gate-final.approved" ]; then
    deny_with_reason "[ENF-GATE-FINAL] Cannot mark module complete without ENF-GATE-FINAL verification. Fix: Run plan-guardian to verify ALL slices, confirm zero MISSING rows, then: touch $PROJECT_ROOT/.claude/gates/gate-final.approved"
  fi
fi

exit 0