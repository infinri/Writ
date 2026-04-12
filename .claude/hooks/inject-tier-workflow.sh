#!/bin/bash
# Mode/tier workflow injector -- PostToolUse hook for Bash
#
# Fires after every Bash tool call. Detects "mode set" or "tier set" commands
# and immediately injects the workflow instructions for the declared mode.
#
# This closes the timing gap where the mode is set mid-turn but the
# workflow reminder only fires on the next UserPromptSubmit.
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

# Detect mode set or tier set commands
MODE=""

# Check for "mode set <mode>" in command
if echo "$TOOL_INPUT" | grep -qE 'mode set (conversation|debug|review|work)'; then
    MODE=$(echo "$TOOL_INPUT" | grep -oP 'mode set \K(conversation|debug|review|work)' || echo "")
fi

# Check for "tier set [0-3]" in command (facade maps to mode)
if [ -z "$MODE" ] && echo "$TOOL_INPUT" | grep -qE 'tier set [0-3]'; then
    TIER=$(echo "$TOOL_INPUT" | grep -oP 'tier set \K[0-3]' || echo "")
    case "$TIER" in
        0) MODE="conversation" ;;
        1|2|3) MODE="work" ;;
    esac
fi

if [ -z "$MODE" ]; then
    exit 0
fi

# Verify the command succeeded by checking output
if ! echo "$TOOL_OUTPUT" | grep -qE 'set: '; then
    exit 0
fi

# Detect project root and check if gates already exist
PROJECT_ROOT=$(detect_project_root "$(pwd)")

case "$MODE" in
    conversation)
        echo ""
        echo "[Writ: Conversation mode. Rules injected for context. No code generation expected.]"
        ;;
    debug)
        echo ""
        echo "[Writ: Debug mode. Investigate the problem. No code generation -- switch to Work mode when fix is identified.]"
        ;;
    review)
        echo ""
        echo "[Writ: Review mode. Evaluate code against Writ rules. Produce structured findings per file.]"
        ;;
    work)
        # Check if plan gate already exists
        if [ -n "$PROJECT_ROOT" ] && [ -f "$PROJECT_ROOT/.claude/gates/phase-a.approved" ]; then
            exit 0
        fi

        cat << 'WORKFLOW'

[Writ: Work mode declared -- workflow instructions]
STOP. Do NOT write any code yet. You must first:
1. Enter /plan mode
2. Write plan.md with: ## Files, ## Analysis, ## Rules Applied, ## Capabilities
3. Write capabilities.md with the same checkbox items
4. Exit /plan (ExitPlanMode validates format automatically)
5. Present the plan to the user. STOP. Say: "Say **approved** to proceed."
6. After approval, write test skeletons. STOP. Say: "Say **approved** to proceed to implementation."
7. After test-skeletons approval, write implementation code.
Do NOT write any code files until the user approves. Do NOT create gate files yourself.
WORKFLOW
        ;;
esac

exit 0
