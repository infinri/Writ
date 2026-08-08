#!/usr/bin/env bash
# Phase 3: dispatch discipline -- hot-swap generic sub-agent dispatches to the named Writ role.
#
# PreToolUse on Task. In work, investigate, and mode-unset sessions, if the dispatched
# subagent_type is generic (general-purpose / Explore / claude / plan / empty) and the
# prompt carries no escape marker ([general-purpose] / [writ:dispatch-ok]), REWRITE the
# dispatch in place via updatedInput to the Writ role keyword-mapped from the prompt,
# disclosing the swap in additionalContext; a prompt that maps to no role confidently
# is denied with the role menu instead ("workflow-subagent" is exempt: the Workflow
# engine's structured-output contracts would break under a role swap).
# Generic agents are the exception, not the default (SKL-PROC-DISPATCH-001): they carry
# no role prompt and run outside the Writ session (mode/gates/RAG).
#
# Hook type: PreToolUse (matcher: Task)
# Exit: always 0 (denial is expressed via permissionDecision in stdout JSON, not exit code)
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
source "$WRIT_DIR/bin/lib/common.sh"
hook_instrument "writ-dispatch-discipline"

# Capture stderr (Python tracebacks etc.) to debug log so next-occurrence diagnostics
# are readable. tee preserves stderr propagation so behavior is unchanged. Gated behind
# WRIT_DEBUG (default OFF): the sink is /dev/null unless WRIT_DEBUG=1.
exec 2> >(tee -a "$(_writ_debug_enabled && echo "${WRIT_HOOK_LOG:-/tmp/writ-hook-debug.log}" || echo /dev/null)" >&2)

load_hook_env
SESSION_ID="$HOOK_SESSION_ID"
[ -z "$SESSION_ID" ] && exit 0

# Enforce in the governed DISPATCH modes: work (orchestrated build), investigate
# (audit/explore), AND unset/None (empty string). Unset is included because real
# engineering work routinely runs with no mode explicitly set -- that was the gap
# that let general-purpose agents through ungoverned (observed in a real client project:
# the large majority of dispatches ran mode=None). The deliberately-chosen non-build
# modes (conversation/debug/review) stay ungoverned; the [general-purpose] escape
# hatch in the prompt always overrides. is_work_mode only checks work, so read the
# mode file-direct (authoritative, same as Fix C) and case on it.
DISPATCH_MODE=$(python3 "$WRIT_DIR/bin/lib/writ-session.py" mode get "$SESSION_ID" 2>/dev/null | tr -d '[:space:]')
case "$DISPATCH_MODE" in
    work|investigate|"") ;;
    *) exit 0 ;;
esac

# Pass the normalized envelope via env var rather than heredoc substitution: raw JSON
# substituted into a heredoc body preserves embedded control chars that json.loads
# rejects (same bug class fixed in writ-sdd-review-order.sh). Quoted '<<PY' delimiter =
# no shell substitution inside; pure stdlib, no module import needed.
DECISION=$(WRIT_PARSED_ENVELOPE="$HOOK_ENVELOPE" python3 <<'PY'
import json, os, sys
raw = os.environ.get("WRIT_PARSED_ENVELOPE", "")
try:
    parsed = json.loads(raw)
except (json.JSONDecodeError, ValueError) as _e:
    sys.stderr.write(f'[writ-hook json.loads recovery] writ-dispatch-discipline.sh: {_e}\n')
    sys.exit(0)
ti = parsed.get("tool_input") or {}
st = (ti.get("subagent_type") or "").strip().lower()
prompt = ti.get("prompt") or ti.get("description") or ""

# "plan" (the harness Plan architect) is governed too: plan-shaped work must flow
# through writ-planner so plan.md exists for the phase-a gate. "workflow-subagent"
# is deliberately ungoverned: the Workflow engine dispatches it with structured
# -output contracts a role swap would break.
GENERIC = {"", "general-purpose", "explore", "claude", "plan"}
if st not in GENERIC:
    sys.exit(0)  # already a named (writ-*) role -> allow

if "[general-purpose]" in prompt or "[writ:dispatch-ok]" in prompt:
    sys.exit(0)  # explicit escape hatch -> allow

p = prompt.lower()


def role():
    # Order matters: exploration is the dominant generic-agent misuse, so its strong
    # leading verbs win first; implement is checked after plan so "plan the implementation"
    # routes to the planner, while "implement the plan" (no plan-lead phrase) falls to it.
    # Returns "" when the task does not map confidently -> we ask instead of forcing a role.
    if any(k in p for k in ("explore", "investigate", "understand the", "map the",
                            "find where", "locate", "survey", "look around", "audit",
                            "research", "look into")):
        return "writ-explorer"
    if any(k in p for k in ("test skeleton", "write tests", "write failing", "failing test")):
        return "writ-test-writer"
    if "review" in p:
        return "writ-reviewer"
    if any(k in p for k in ("decompose", "design the implementation",
                            "create a plan", "plan the implementation")):
        return "writ-planner"
    if any(k in p for k in ("implement", "write the code", "apply the",
                            "fix the", "edit the", "refactor")):
        return "writ-implementer"
    return ""


shown = st or "general-purpose"
r = role()
if r:
    # Confident classification: REWRITE the dispatch to the governed Writ role via
    # updatedInput so the model proceeds with it directly. Deny-based steering is reserved
    # for the ambiguous branch below because a denial depends on the agent re-dispatching,
    # which it largely does not (observed 92% generic dispatches). additionalContext makes
    # the swap visible so the agent can re-issue with '[general-purpose]' if generic was
    # genuinely intended.
    new_ti = dict(ti)
    new_ti["subagent_type"] = r
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": new_ti,
            "additionalContext": (
                f"[Writ dispatch discipline | SKL-PROC-DISPATCH-001] Routed the generic "
                f"'{shown}' dispatch to the governed Writ role '{r}' (carries the role prompt + "
                f"runs inside the Writ session: mode/gates/RAG). To force generic, re-issue the "
                f"Task with '[general-purpose]' in the prompt."
            ),
        }
    }))
else:
    # Ambiguous: no confident role to rewrite to -> ask rather than force a possibly-wrong one.
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"[Writ dispatch discipline | SKL-PROC-DISPATCH-001] You dispatched the generic "
                f"'{shown}' agent and the task did not map to a specific Writ role. Re-dispatch "
                f"with writ-explorer (read-only) or writ-implementer, or add '[general-purpose]' "
                f"to the prompt to override."
            ),
        }
    }))
PY
)

[ -n "$DECISION" ] && printf '%s' "$DECISION" | blackbox_log out writ-dispatch-discipline "$SESSION_ID"
[ -n "$DECISION" ] && echo "$DECISION"

# THE AUDIT ROW SAYS WHAT WAS EMITTED, read back out of the JSON above rather than
# inferred from "did we emit anything".
#
# This block used to log "deny" whenever $DECISION was non-empty. The non-empty case is
# BOTH branches, and the dominant one is the reroute -- permissionDecision=allow with
# updatedInput -- which is this hook's entire job. So every successful reroute was filed
# in the audit stream (governance record, 365-day retention) as a denial, and the record
# was lying about the one mechanism that closes the general-purpose sub-agent gap.
# Reproduced live 2026-08-08: subagent_type "general-purpose" + an exploration prompt
# produced updatedInput and permissionDecision=allow beside an audit row saying "deny".
#
# An empty $DECISION still means the dispatch was left alone, which is the allow branch.
#
# The audit target is the agent type that was ASKED FOR -- "general-purpose" on a reroute,
# the named role on a pass-through. It was `${AGENT_TYPE:-}`, a variable this hook never
# assigns, so every row filed an empty target and the stream could not say WHICH dispatch
# it had rerouted, refused, or waved through. The value lives in tool_input, which only the
# embedded python above reads, so it is lifted out here rather than inferred; the jq-first
# seam keeps that to ~2ms beside the two interpreter starts this hook already pays.
# NOT HOOK_AGENT_TYPE from load_hook_env: that is the type of the agent DOING the
# dispatching, which is empty on the main session and confidently wrong inside a sub-agent.
REQUESTED_AGENT_TYPE=$(printf '%s' "$HOOK_ENVELOPE" | json_transform \
    '.tool_input.subagent_type' \
    "(d.get('tool_input') or {}).get('subagent_type')")

if [ -n "$DECISION" ]; then
    # NOT defaulted to allow or deny. An unparseable payload is neither, and asserting
    # one of them anyway is precisely the defect being fixed; "unknown" is the honest
    # label and log_gate_decision already routes any non-"allow" value down its
    # synchronous, non-lossy path. json_transform is the jq-first seam, so the rare
    # intervening dispatch pays ~2ms rather than an interpreter start.
    EMITTED_DECISION=$(printf '%s' "$DECISION" | json_transform \
        '.hookSpecificOutput.permissionDecision' \
        "(d.get('hookSpecificOutput') or {}).get('permissionDecision')")
    log_gate_decision "dispatch-discipline" "${EMITTED_DECISION:-unknown}" \
        "$DECISION" "${REQUESTED_AGENT_TYPE:-}"
else
    log_gate_decision "dispatch-discipline" "allow" "dispatch not intercepted" "${REQUESTED_AGENT_TYPE:-}"
fi
exit 0
