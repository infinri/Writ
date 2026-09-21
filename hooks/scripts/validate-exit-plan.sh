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

# GOVERN THE AGENT BEFORE THE FIRST EARLY EXIT. ExitPlanMode fires only this hook, and this
# hook cannot call load_hook_env because $STDIN_JSON has already consumed stdin, so a
# sub-agent confined to this tool reached no seeder at all. `parsed_fields` takes the JSON as
# an argument rather than from stdin, which is what makes the three fields readable here.
#
# ABOVE writ_require_session, and above the mode check below, so neither can skip the seed:
# a child that never inherits its parent's mode is exactly the agent whose mode reads empty.
# Both variables are initialized first because an unparseable envelope makes parsed_fields
# print nothing and `set -u` would then abort the hook.
#
# The RAW session id is the parent argument, never the collapsed identity writ_require_session
# resolves below: seeding needs both halves of the pair. Seeding grants nothing, because the
# cache is marked lazy_seed and the write gate resolves that mode as absent.
SEED_AGENT_ID=""
SEED_SESSION_ID=""
SEED_AGENT_TYPE=""
eval "$(parsed_fields "$STDIN_JSON" SEED_AGENT_ID=agent_id SEED_SESSION_ID=session_id \
    SEED_AGENT_TYPE=agent_type)"
writ_seed_subagent_from_fields "$SEED_AGENT_ID" "$SEED_SESSION_ID" "$SEED_AGENT_TYPE"

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

# The working directory, captured in its own assignment instead of inline below.
# `pwd -P` FAILS when the directory has been deleted out from under the process, and
# inside the command substitution below that non-zero status would abort the whole hook
# under `set -e` before it could record anything. An empty CWD is a legitimate answer
# here ("this process has no resolvable directory"), handled by the skip branch.
CWD=$(pwd -P 2>/dev/null || printf '')

# Validate plan.md with the same validator as the phase-a gate, AT THE SAME ROOT that
# gate resolves. The root is resolved inside this python call, not by common.sh's bash
# marker walk, and that is the point of the whole block:
#
#   - auto-approve-gate.sh:446-458 already defers this exact question to
#     locators.resolve_project_root when it advances the phase-a gate, because the
#     marker list exists in BOTH implementations (common.sh's walk and python's
#     PROJECT_ROOT_MARKERS), and resolving in python keeps ONE of them authoritative
#     for the gate decision, so a future drift cannot change which plan is approved.
#     This hook judges the SAME artifact for the SAME gate, so it must ask the same
#     resolver or the two can disagree about which plan.md is the one being approved.
#   - The bash walk answers "" for any directory with no composer.json / package.json /
#     Cargo.toml / go.mod / pyproject.toml / .git, and this hook then did a bare
#     `exit 0`. The plan-format gate was silently OFF in every unmarked directory.
#     resolve_project_root falls back to the start directory itself for exactly that
#     case, which is also what the advance route already does at approval time.
#
# THE ROOT AND SESSION ID TRAVEL THROUGH argv, never interpolated into the python
# source. The previous version built a python string literal out of '$PROJECT_ROOT': a
# directory name may legally contain an apostrophe, which would have closed the literal
# and run the rest of the path as code.
#
# Exit 3 means "no root resolved", which is a SKIP and not a judgment. The guard is not
# padding: an empty root reaches _validate_phase_a(""), whose _find_plan_md would join
# "plan.md" as a RELATIVE path and judge whatever file happens to sit in this hook
# process's own working directory, the implicit-cwd hazard resolve_project_root's
# docstring exists to prevent.
RC=0
VALIDATION_ERROR=$(python3 -c "
import sys
sys.path.insert(0, '$WRIT_DIR/bin/lib')
from importlib import util
spec = util.spec_from_file_location('writ_session', '$SESSION_HELPER')
mod = util.module_from_spec(spec)
spec.loader.exec_module(mod)
root, _tier = mod.resolve_project_root(start=sys.argv[2])
if not root:
    sys.exit(3)
error = mod._validate_phase_a(root, sys.argv[1])
if error:
    print(error)
" "$SESSION_ID" "$CWD" 2>/dev/null) || RC=$?

if [ "$RC" != "0" ]; then
    # NOT `[ "$RC" = "3" ] && REASON=no_root` as a bare statement: under `set -e` an
    # && list whose left side is false returns non-zero and aborts the hook, which
    # would lose the very row this branch exists to write.
    REASON="validator_error"
    if [ "$RC" = "3" ]; then
        REASON="no_root"
    fi

    # ONE ROW, THEN exit 0. The row is the entire difference between this branch and
    # the defect it replaces: the hook already records exitplanmode_allow and
    # exitplanmode_denial, and it was the missing third outcome that let a silent
    # skip look exactly like an allow. "validator_error" covers what the old `|| true`
    # swallowed whole, a crashed validator, at no extra cost.
    log_friction_event "$SESSION_ID" "work" "exitplanmode_skipped" \
        "{\"reason\":\"$REASON\"}"

    # A SKIP, not a deny, for two reasons. The daemon still fails closed: this same
    # validator is _GATE_VALIDATORS["phase-a"], and writ/server/routes/gate.py:151-177
    # re-runs it at advance time and refuses an unresolvable root WITHOUT spending the
    # approval token, so the last judge holds either way. And a deny here would name no
    # way out: this hook reads the Claude Code process's working directory, which
    # nothing the agent does inside the turn can change, so a permanent deny would be a
    # deadlock. Failing open LOUDLY is right for a hook whose header says it validates
    # FORMAT only; failing closed is right for the route that spends the approval.
    exit 0
fi

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
    DENY_REPLY=$(python3 -c "
import json, sys
result = {
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': sys.argv[1]
    }
}
print(json.dumps(result))
" "$VALIDATION_ERROR") || DENY_REPLY=""
    emit_hook_reply "$DENY_REPLY"
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
ALLOW_REPLY=$(python3 <<'PY'
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
) || ALLOW_REPLY=""
emit_hook_reply "$ALLOW_REPLY"

exit 0
