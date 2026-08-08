#!/bin/bash
# Mode workflow injector -- PostToolUse hook for Bash
#
# Fires after every Bash tool call. Detects a "mode set" command and
# immediately injects the workflow instructions for the declared mode.
#
# This closes the timing gap where the mode is set mid-turn but the
# workflow reminder only fires on the next UserPromptSubmit.
#
# Hook type: PostToolUse (matcher: Bash)
# Exit: always 0 (informational only, never blocks)

set -euo pipefail

SKILL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
source "$SKILL_DIR/bin/lib/common.sh"
hook_instrument "inject-tier-workflow"

# WRIT_HOOK_LOG stderr breadcrumb sink, gated by WRIT_DEBUG: /dev/null when unset,
# ${WRIT_HOOK_LOG:-/tmp/writ-hooks.log} when WRIT_DEBUG=1 (single source: common.sh).
WRIT_HOOK_LOG_SINK="$(hook_log_sink)"

# Parse hook stdin (one python3 spawn). tool_output is read from $HOOK_ENVELOPE
# only inside the branches that need it (debug capture; mode-set success check).
load_hook_env

# #2: accumulate model-facing text and deliver it via additionalContext on EXIT.
# Plain stdout on PostToolUse reaches only the CC debug log (verified delivery
# rule); additionalContext reaches the model. CC parses ONE JSON per invocation,
# so accumulate into AC_TEXT and emit once in a trap that covers every exit path.
AC_TEXT=""
_emit_ac() {
    [ -z "$AC_TEXT" ] && return 0
    WRIT_AC="$AC_TEXT" python3 <<'PY' 2>>"$WRIT_HOOK_LOG_SINK" || true
import json, os
print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": os.environ.get("WRIT_AC", "")}}))
PY
}
# writ_on_exit, NOT `trap _emit_ac EXIT`. bash allows one EXIT trap, so installing one
# here replaced hook_instrument's trap and this hook silently stopped recording its
# telemetry row. Pinned by tests/test_exit_trap_ownership.py.
writ_on_exit _emit_ac

# Increment 7a: in debug mode, auto-capture this Bash run (command + output
# excerpt + exit code) into the bounded session command_log as evidence. This is
# what makes debug "## Evidence" verifiable (observed) rather than self-reported
# -- no MCP. No-op in every other mode.
CAPTURE_SID="$HOOK_SESSION_ID"
if [ -n "$CAPTURE_SID" ]; then
    CAPTURE_MODE=$(_writ_session "mode get" "$CAPTURE_SID" 2>/dev/null | tr -d '[:space:]' || echo "")
    if [ "$CAPTURE_MODE" = "debug" ] && [ -n "$HOOK_COMMAND" ]; then
        RUN_JSON=$(echo "$HOOK_ENVELOPE" | python3 -c "
import json, sys
d = json.load(sys.stdin)
out = d.get('tool_output')
out = out if isinstance(out, str) else (json.dumps(out) if out is not None else '')
print(json.dumps({
    'command': d.get('command', ''),
    'exit_code': 1 if d.get('is_error') else 0,
    'output_excerpt': out[:500],
}))
" 2>/dev/null || echo "")
        if [ -n "$RUN_JSON" ]; then
            _writ_session update "$CAPTURE_SID" --add-command-run "$RUN_JSON" 2>/dev/null || true
        fi
    fi
fi

# 1.8b push-by-action (PostToolUse Bash): re-surface methodology AT the git
# command that signals an action -- worktree (git worktree add) -> SKL/TEC-PROC-
# WORKTREE; finish (git push|merge, gh pr create) -> PBK-PROC-FINISH. Echoed as
# context (this hook's stdout convention). Fail-open; never blocks (exit 0).
if [ -n "${HOOK_COMMAND:-}" ]; then
    PUSH_ACTION=""
    if echo "$HOOK_COMMAND" | grep -qE 'git +worktree +add'; then
        PUSH_ACTION="worktree"
    elif echo "$HOOK_COMMAND" | grep -qE 'git +push|git +merge|gh +pr +create'; then
        PUSH_ACTION="finish"
    fi
    if [ -n "$PUSH_ACTION" ]; then
        ACTION_PUSH_TEXT=$(writ_action_push "$HOOK_SESSION_ID" "$PUSH_ACTION" || true)
        if [ -n "$ACTION_PUSH_TEXT" ]; then
            AC_TEXT="[Writ: methodology -- $PUSH_ACTION]
$ACTION_PUSH_TEXT"
        fi
    fi
fi

# Detect "mode set <mode>" in the Bash command -- the only stdout/injection path.
MODE=""
if echo "$HOOK_COMMAND" | grep -qE 'mode set (conversation|debug|investigate|review|work)'; then
    MODE=$(echo "$HOOK_COMMAND" | grep -oP 'mode set \K(conversation|debug|investigate|review|work)' || echo "")
fi

if [ -z "$MODE" ]; then
    exit 0
fi

# Verify the command succeeded by checking its output (from the envelope).
if ! echo "$HOOK_ENVELOPE" | python3 -c "import sys,json; o=json.load(sys.stdin).get('tool_output'); sys.stdout.write(o if isinstance(o,str) else '')" 2>/dev/null | grep -qE 'set: '; then
    exit 0
fi

# Detect project root and check if gates already exist
PROJECT_ROOT=$(detect_project_root "$(pwd)")

case "$MODE" in
    conversation)
        AC_TEXT="[Writ: Conversation mode. Rules injected for context. No code generation expected.]"
        ;;
    debug)
        AC_TEXT="[Writ: Debug mode. Investigate the problem. No code generation -- switch to Work mode when fix is identified.]"
        ;;
    review)
        AC_TEXT="[Writ: Review mode. Evaluate code against Writ rules. Produce structured findings per file.]"
        ;;
    investigate)
        AC_TEXT="[Writ: Investigate mode. Evidence-grounded, read-heavy. Follow the investigation workflow for the selected source type (code/web/runtime); ground every claim in observed evidence. No code generation -- switch to Work mode to implement.]"
        ;;
    work)
        # Check if plan gate already exists
        if [ -n "$PROJECT_ROOT" ] && [ -f "$PROJECT_ROOT/.claude/gates/phase-a.approved" ]; then
            exit 0
        fi

        AC_TEXT=$(cat << 'WORKFLOW'
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
)
        ;;
esac

exit 0
