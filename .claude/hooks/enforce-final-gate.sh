#!/bin/bash
# Blocks completion marker writes until ENF-GATE-FINAL has been verified.
# PreToolUse -- runs alongside check-gate-approval.sh.
# Output: structured JSON (Claude Code hookSpecificOutput contract).
#
# plan.md can be freely created/edited during planning phases.
# Only blocks plan.md when it contains completion markers (COMPLETE, DONE, FINISHED).
# Always blocks files with COMPLETE in their path.

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

# ENF-GATE-FINAL only applies to Tier 3. Skip for Tier 0-2.
SESSION_HELPER="$SKILL_DIR/bin/lib/writ-session.py"
SESSION_ID=$(detect_session_id "$PARSED")
TIER=$(python3 "$SESSION_HELPER" tier get "$SESSION_ID" 2>/dev/null || echo "")
TIER=$(echo "$TIER" | tr -d '[:space:]')
if [ -n "$TIER" ] && [ "$TIER" != "3" ]; then
    exit 0
fi

PROJECT_ROOT=$(detect_project_root "$FILE")

# Files with COMPLETE in the path are always gated
if [[ "$FILE" == *COMPLETE* ]]; then
  if [ ! -f "$PROJECT_ROOT/.claude/gates/gate-final.approved" ]; then
    deny_with_reason "[ENF-GATE-FINAL] Cannot mark module complete without ENF-GATE-FINAL verification. Fix: Run plan-guardian to verify ALL slices, confirm zero MISSING rows, then: touch $PROJECT_ROOT/.claude/gates/gate-final.approved"
  fi
fi

# plan.md: only gate if the content contains completion markers or pending ENF-POST items
if [[ "$FILE" == */plan.md ]]; then
  CONTENT=$(echo "$PARSED" | python3 -c "
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

  if [ -n "$CONTENT" ]; then
    # Check for pending ENF-POST items
    if echo "$CONTENT" | grep -iE "(static.analysis|ENF-POST-007|linter|type.check|integration.test)" | grep -qi "pending"; then
      deny_with_reason "[ENF-POST-007] plan.md contains pending ENF-POST items -- static analysis cannot be deferred. Fix: Complete static analysis before writing plan.md as complete"
    fi

    # Check for completion markers -- only then require gate-final
    if echo "$CONTENT" | grep -qiE "(status:\s*(complete|done|finished)|##\s*(complete|done|finished))"; then
      if [ ! -f "$PROJECT_ROOT/.claude/gates/gate-final.approved" ]; then
        deny_with_reason "[ENF-GATE-FINAL] Cannot mark plan.md as complete without ENF-GATE-FINAL verification. Fix: Run plan-guardian to verify ALL slices, confirm zero MISSING rows, then: touch $PROJECT_ROOT/.claude/gates/gate-final.approved"
      fi
    fi
  fi
fi

exit 0
