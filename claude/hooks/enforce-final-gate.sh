#!/bin/bash
# Blocks completion marker writes until ENF-GATE-FINAL has been verified.
# PreToolUse — runs alongside check-gate-approval.sh.
# Output: structured JSON.

SKILL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
source "$SKILL_DIR/bin/lib/common.sh"

FILE=$(echo "$CLAUDE_TOOL_INPUT" | python3 -c \
  "import sys,json; d=json.load(sys.stdin); print(d.get('file_path',''))" 2>/dev/null)

if [ -z "$FILE" ]; then exit 0; fi

PROJECT_ROOT=$(detect_project_root "$FILE")

if [[ "$FILE" == */plan.md || "$FILE" == *COMPLETE* ]]; then

  # --- Check 1: pending ENF-POST items in plan.md content -----------------
  PENDING_CONTENT=$(echo "$CLAUDE_TOOL_INPUT" | python3 -c "
import sys, json, os
data = json.load(sys.stdin)
fp = data.get('file_path', '')
if 'content' in data:
    print(data['content'])
elif 'old_string' in data and 'new_string' in data and os.path.exists(fp):
    with open(fp) as f:
        content = f.read()
    print(content.replace(data['old_string'], data.get('new_string', ''), 1))
" 2>/dev/null)

  if [ -n "$PENDING_CONTENT" ]; then
    if echo "$PENDING_CONTENT" | grep -iE "(static.analysis|ENF-POST-007|linter|type.check|integration.test)" | grep -qi "pending"; then
      json_finding "true" "ENF-POST-007" \
        "plan.md contains pending ENF-POST items — static analysis cannot be deferred" \
        "$FILE" \
        "Complete static analysis (ENF-POST-007) before writing plan.md as complete"
      exit 1
    fi
  fi

  # --- Check 2: gate-final.approved must exist ----------------------------
  if [ ! -f "$PROJECT_ROOT/.claude/gates/gate-final.approved" ]; then
    json_finding "true" "ENF-GATE-FINAL" \
      "Cannot mark module complete without ENF-GATE-FINAL verification" \
      "$FILE" \
      "Run plan-guardian to verify ALL slices, confirm zero MISSING rows, then: touch $PROJECT_ROOT/.claude/gates/gate-final.approved"
    exit 1
  fi
fi

exit 0