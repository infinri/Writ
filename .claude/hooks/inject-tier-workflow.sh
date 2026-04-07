#!/bin/bash
# Tier workflow injector -- PostToolUse hook for Bash
#
# Fires after every Bash tool call. Detects "tier set" commands and
# immediately injects the workflow instructions for the declared tier.
#
# This closes the timing gap where tier is classified mid-turn but the
# workflow reminder only fires on the next UserPromptSubmit. Without this,
# Claude can set a tier and attempt writes in the same response before
# seeing the workflow instructions.
#
# Hook type: PostToolUse (matcher: Bash)
# Exit: always 0 (informational only, never blocks)

set -euo pipefail

SKILL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
source "$SKILL_DIR/bin/lib/common.sh"

# Parse hook stdin
PARSED=$(parse_hook_stdin)
TOOL_INPUT=$(parsed_field "$PARSED" "tool_input")
TOOL_OUTPUT=$(parsed_field "$PARSED" "tool_output")

# Only fire when the Bash command was a tier set command
# Check both the input (command) and output (confirmation)
if ! echo "$TOOL_INPUT" | grep -qE 'tier set [0-3]'; then
    exit 0
fi

# Extract the tier from the output ("set: N")
TIER=$(echo "$TOOL_OUTPUT" | grep -oP 'set: \K[0-3]' || echo "")
if [ -z "$TIER" ]; then
    # Fallback: extract from the command itself
    TIER=$(echo "$TOOL_INPUT" | grep -oP 'tier set \K[0-3]' || echo "")
fi

if [ -z "$TIER" ]; then
    exit 0
fi

# Tier 0-1: no gates, no workflow reminder needed
if [ "$TIER" = "0" ] || [ "$TIER" = "1" ]; then
    exit 0
fi

# Detect project root and check if gates already exist
PROJECT_ROOT=$(detect_project_root "$(pwd)")
if [ -n "$PROJECT_ROOT" ]; then
    GATE_DIR="$PROJECT_ROOT/.claude/gates"
    if [ -f "$GATE_DIR/phase-a.approved" ]; then
        # Phase A already approved, no need to remind
        exit 0
    fi
fi

# Inject workflow instructions immediately
if [ "$TIER" = "2" ]; then
    cat << 'WORKFLOW'

[Writ: Tier 2 declared -- workflow instructions (injected immediately)]
STOP. Do NOT write any code yet. You must first:
1. Write plan.md in the module directory with a combined Phase A-C analysis:
   A) What the feature does and why, files to create/modify, call path
   B) Interfaces, type contracts, domain invariants
   C) API contracts, DI wiring, integration seam justification
   Include: applicable Writ rules and query budget (if DB calls involved).
2. Present the plan to the user in your response.
3. STOP and WAIT. Tell the user: "Say **approved** to proceed."
   The user's approval will automatically create the gate file.
4. After approval, write test skeletons. Then STOP and WAIT again for approval.
5. After test-skeletons approval, write implementation code.
Do NOT write any code files until the user approves. Do NOT create gate files yourself.
WORKFLOW
fi

if [ "$TIER" = "3" ]; then
    cat << 'WORKFLOW'

[Writ: Tier 3 declared -- workflow instructions (injected immediately)]
STOP. Do NOT write any code yet. Full sequential phase protocol required:
Phase A: Write plan.md with design and call-path declaration. Present and WAIT.
  Tell the user: "Say **approved** to proceed to Phase B."
Phase B: Present domain invariants and validation. WAIT.
  Tell the user: "Say **approved** to proceed to Phase C."
Phase C: Present integration points and seam justification. WAIT.
  Tell the user: "Say **approved** to proceed."
Phase D (if concurrency): Present concurrency modeling. WAIT.
  Tell the user: "Say **phase d approved** to proceed."
Test skeletons: Write skeletons, WAIT.
  Tell the user: "Say **approved** to proceed to implementation."
Implementation: Only then write code.
Do NOT write any code files until the user approves. Do NOT create gate files yourself.
WORKFLOW
fi

# Inject planning checklist if applicable
CHECKLISTS_FILE="$SKILL_DIR/bin/lib/checklists.json"
if [ -f "$CHECKLISTS_FILE" ]; then
    CHECKLIST_OUTPUT=$(python3 -c "
import sys, json

tier = int(sys.argv[1])
checklist_path = sys.argv[2]

try:
    with open(checklist_path) as f:
        checklists = json.load(f)
except Exception:
    sys.exit(0)

checklist = checklists.get('planning', {})
tier_min = checklist.get('tier_min', 99)
if tier < tier_min:
    sys.exit(0)

criteria = checklist.get('exit_criteria', [])
if not criteria:
    sys.exit(0)

lines = ['[Writ: planning exit criteria]']
lines.append('Before presenting the plan for approval, verify:')
for c in criteria:
    lines.append(f'  - {c[\"id\"]}: {c[\"check\"]}')
print('\n'.join(lines))
" "$TIER" "$CHECKLISTS_FILE" 2>/dev/null || true)

    if [ -n "$CHECKLIST_OUTPUT" ]; then
        echo ""
        echo "$CHECKLIST_OUTPUT"
    fi
fi

exit 0
