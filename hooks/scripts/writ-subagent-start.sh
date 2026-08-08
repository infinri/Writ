#!/usr/bin/env bash
# SubagentStart hook -- creates isolated session cache for each sub-agent worker.
#
# When a sub-agent spawns, this hook:
# 1. Reads the parent's session state (mode, phase, gates)
# 2. Creates a fresh session cache keyed by agent_id
# 3. Pre-populates with parent's gate state but fresh RAG budget
# 4. Queries Writ for phase-specific rules
# 5. Injects rules + state via additionalContext
#
# Hook type: SubagentStart
# Exit: always 0

set -euo pipefail

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
SESSION_HELPER="$WRIT_DIR/bin/lib/writ-session.py"

# Source shared helpers (log_friction_event, _writ_session) BEFORE any use.
# Previously this was sourced near the end (line ~169); the empty-agent_type
# fallback branch below calls log_friction_event, so under `set -e` the hook
# died at that call -- never logging subagent_start, never creating the session
# cache, never injecting rules. Sourcing first is the keystone fix.
source "$WRIT_DIR/bin/lib/common.sh"

# Phase 4c: capture stderr (Python tracebacks etc.) to debug log so
# next-occurrence diagnostics are readable. tee preserves stderr
# propagation so behavior is unchanged. Gated behind WRIT_DEBUG (default OFF):
# the sink is /dev/null unless WRIT_DEBUG=1.
exec 2> >(tee -a "$(_writ_debug_enabled && echo "${WRIT_HOOK_LOG:-/tmp/writ-hook-debug.log}" || echo /dev/null)" >&2)

WRIT_HOST="${WRIT_HOST:-localhost}"
WRIT_PORT="${WRIT_PORT:-8765}"

# Read stdin envelope
STDIN_JSON=$(cat)
printf '%s' "$STDIN_JSON" | blackbox_log in writ-subagent-start

# Bounded capture of the raw SubagentStart envelope so we can learn whether/where
# Claude Code sends agent_type (Phase 3.2 input). /tmp, capped at 50 lines,
# fire-and-forget -- never affects the hook outcome.
_WRIT_PAYLOAD_CAP=/tmp/writ-subagent-payloads.jsonl
if [ "$(wc -l < "$_WRIT_PAYLOAD_CAP" 2>/dev/null || echo 0)" -lt 50 ]; then
    printf '%s\n' "$STDIN_JSON" >> "$_WRIT_PAYLOAD_CAP" 2>/dev/null || true
fi

# Extract agent metadata and parent session
AGENT_ID=$(parsed_field "$STDIN_JSON" "agent_id")
AGENT_TYPE=$(parsed_field "$STDIN_JSON" "agent_type")
PARENT_SESSION=$(parsed_field "$STDIN_JSON" "session_id")

if [ -z "$AGENT_ID" ]; then
    exit 0
fi

# Fallback: some Claude Code versions / nested sub-agents omit agent_type.
# Default to "general-purpose" and log the fallback so we can track frequency.
if [ -z "$AGENT_TYPE" ]; then
    AGENT_TYPE="general-purpose"
    log_friction_event "$AGENT_ID" "" "subagent_type_fallback" \
        "{\"hook\":\"writ-subagent-start\",\"parent_session\":\"$PARENT_SESSION\"}"
fi

# Read parent's current state from the AUTHORITATIVE file cache (where
# `writ-session.py mode set` writes), NOT via _writ_session (daemon-first). The
# daemon's in-memory / cache-dir view can diverge from the file cache and return a
# stale mode=None, which the sub-agent would then inherit -- running gate-less.
# `writ-session.py read` is file-direct (cmd_read -> _read_cache), so it is correct
# even when the daemon is up with a divergent view (Phase 3 Fix C).
if [ -n "$PARENT_SESSION" ]; then
    PARENT_STATE=$(python3 "$SESSION_HELPER" read "$PARENT_SESSION" 2>/dev/null || echo '{}')
else
    # NO POINTER FALLBACK. /tmp/writ-current-session names whichever Claude Code session on
    # this machine took a turn most recently, which for a sub-agent means inheriting mode
    # and gate state from an unrelated parent. That is worse than inheriting nothing: the
    # sub-agent would run governed by another session's phase, and the mismatch is
    # invisible from either side.
    #
    # Empty state is the honest answer, and it is recorded rather than assumed.
    # Filed under AGENT_ID, which is known non-empty here. Omitting it sent the row to
    # session "unknown", so the one record saying a sub-agent started ungoverned could not
    # be traced back to which sub-agent that was.
    writ_critical writ-subagent-start \
        "no parent session in payload; sub-agent starts with no inherited gate state" \
        "$AGENT_ID"
    PARENT_STATE='{}'
fi

# Create isolated session for the sub-agent with parent's gate state but fresh budget
python3 -c "
import sys, json, os
sys.path.insert(0, '$WRIT_DIR/bin/lib')
from importlib import util
spec = util.spec_from_file_location('writ_session', '$SESSION_HELPER')
mod = util.module_from_spec(spec)
spec.loader.exec_module(mod)

parent = json.loads(sys.argv[1])
agent_id = sys.argv[2]

# Create fresh cache with parent's structural state but clean operational state
# Locked read-modify-write via mutate_cache (creates the default if not exists),
# so a duplicate SubagentStart or a concurrent writer for the same agent_id cannot
# lose this initialization -- consistent with the layer-2 serialized-writer discipline.
with mod.mutate_cache(agent_id) as cache:
    # Null-safe (audit P1): parent.get('mode', 'work') returns None when the key EXISTS
    # with a null value (a mode-unset parent), so the child would inherit None and run
    # mode-less. The 'or' coalesces both the missing-key and null-value cases to the default.
    cache['mode'] = parent.get('mode') or 'work'
    cache['current_phase'] = parent.get('current_phase') or 'planning'
    cache['gates_approved'] = parent.get('gates_approved') or []
    cache['remaining_budget'] = mod.DEFAULT_SESSION_BUDGET  # telemetry only; see cmd_should_skip
    cache['is_subagent'] = True  # bypass budget-based skips; sub-agents get unlimited injection
    cache['loaded_rule_ids'] = []
    cache['loaded_rule_ids_by_phase'] = {}
    cache['loaded_rules'] = []
    cache['denial_counts'] = {}
    cache['queries'] = 0
    cache['context_percent'] = 0
    cache['files_written'] = []
    cache['analysis_results'] = {}
    cache['pending_violations'] = []
    cache['feedback_sent'] = []
    cache['pretool_queried_files'] = []
    cache['token_snapshots'] = []
" "$PARENT_STATE" "$AGENT_ID" 2>/dev/null || true

# The sub-agent's mode, read once here rather than at the bottom of the hook. It used
# to be resolved just before the subagent_start friction row (the last thing this hook
# does), which is AFTER the manual-test-grant decision below -- so that gate row, a
# governance record, was stamped with an empty mode while the value was one command
# away. This hook has no SESSION_ID, so common.sh's session-cache fallback has nothing
# to fall back to: the answer has to come from here. Same command, same value, earlier.
CURRENT_MODE=$(_writ_session "mode get" "$AGENT_ID" 2>/dev/null || echo "")
CURRENT_MODE=$(echo "$CURRENT_MODE" | tr -d '[:space:]')

# Manual-testing grant inherits exactly like gates_approved above: the user's
# concession was given to the orchestrating session, and a dispatched worker acts
# on its behalf, so it is not re-typed per worker. The child gets the REMAINING
# TTL (never refreshed) and an inherited_from stamp for the audit trail. Keyed on
# PARENT_SESSION (the payload session id), NOT /tmp/writ-current-session -- the
# pointer churns across concurrent sessions and could leak another session's grant.
GRANT_LIB="$WRIT_DIR/bin/lib/manual_test_grant.py"
if [ -f "$GRANT_LIB" ] && [ -n "$PARENT_SESSION" ] && [ "$PARENT_SESSION" != "$AGENT_ID" ]; then
    if python3 "$GRANT_LIB" inherit "$PARENT_SESSION" "$AGENT_ID" 2>/dev/null; then
        log_gate_decision "manual-test-grant" "inherit" \
            "sub-agent inherited the parent session's live manual-testing grant" "$AGENT_ID"
    fi
fi

# Link this sub-agent to its parent so commit_capture's enumeration
# (_collect_subagent_queried_rules) can merge this child's queried rules at commit time.
# The link is the PAYLOAD's session id and nothing else.
#
# THIS USED TO PREFER /tmp/writ-current-session, and that was the same defect as
# everywhere else, with a worse blast radius: the pointer is ONE file rewritten by every
# Claude Code session on the machine, so a sub-agent spawned while another session had
# just taken a turn was stamped as a child of THAT session -- its queried rules merged
# into a stranger's commit, and this session's commit lost them. Invisible from both ends.
#
# WHAT THE POINTER BOUGHT, AND WHY LOSING IT IS SURVIVABLE: it named the committing
# session directly, so a deeply nested sub-agent linked straight to the root rather than
# to its immediate parent. Without it, a grandchild's parent_session_id is its immediate
# parent and the parent_match arm of _collect_subagent_queried_rules misses it. The
# path + recency arm still catches it: that cache holds queried rules for a committed
# file and was modified inside the window, which is exactly the case that arm was
# written for (writ/session/cache.py:409). So a nested worker's rules are merged by
# content instead of by linkage -- one arm narrower, and never wrong.
PARENT="$PARENT_SESSION"
if [ -n "$PARENT" ] && [ "$PARENT" != "$AGENT_ID" ]; then
    _writ_session update "$AGENT_ID" --parent-session-id "$PARENT" --agent-type "$AGENT_TYPE"
fi

# Query Writ for rules if server is available
ADDITIONAL_CONTEXT=""
HEALTH=$(curl -sf --connect-timeout 0.5 --max-time 1 "http://${WRIT_HOST}:${WRIT_PORT}/health" 2>/dev/null || echo "")
if [ -n "$HEALTH" ]; then
    # Build the retrieval query. Newer Claude Code sends the delegated task in a
    # `task` field; use it when present. CC 2.1.181's SubagentStart payload carries
    # ONLY agent_type (no task/prompt/description) -- verified from captured live
    # envelopes -- so without a fallback the query never runs and ZERO rules reach
    # the sub-agent. Fall back to a role-descriptive phrase keyed on agent_type
    # (a bare agent_type like "writ-explorer" retrieves poorly; a descriptive
    # phrase returns role-relevant rules).
    AGENT_PROMPT=$(echo "$STDIN_JSON" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    prompt = d.get('task') or d.get('prompt') or d.get('description') or d.get('message') or ''
    print(prompt[:500])
except Exception:
    print('')
" 2>/dev/null || echo "")
    QUERY_SOURCE="task"

    if [ -z "$AGENT_PROMPT" ] || [ ${#AGENT_PROMPT} -le 10 ]; then
        QUERY_SOURCE="agent_type"
        case "$AGENT_TYPE" in
            *explor*)    AGENT_PROMPT="explore and understand codebase architecture, structure, conventions, and existing patterns" ;;
            *planner*)   AGENT_PROMPT="design an implementation plan with architecture decisions and trade-offs" ;;
            *implement*) AGENT_PROMPT="implement production code with correct error handling and project conventions" ;;
            *test*)      AGENT_PROMPT="write tests: skeletons, assertions, fixtures, isolation, and coverage" ;;
            *review*)    AGENT_PROMPT="review code for correctness, quality, security, and spec compliance" ;;
            *)           AGENT_PROMPT="software engineering best practices: clean code, correctness, security, testing, error handling" ;;
        esac
    fi

    if [ -n "$AGENT_PROMPT" ] && [ ${#AGENT_PROMPT} -gt 10 ]; then
        RESPONSE=$(python3 -c "
import json, sys
print(json.dumps({
    'query': sys.argv[1][:500],
    'budget_tokens': 2000,
    'exclude_rule_ids': [],
}))
" "$AGENT_PROMPT" 2>/dev/null | \
            curl -s --connect-timeout 0.5 --max-time 2 \
                -X POST "http://${WRIT_HOST}:${WRIT_PORT}/query" \
                -H "Content-Type: application/json" \
                -d @- 2>/dev/null) || true

        if [ -n "$RESPONSE" ]; then
            RULES_TEXT=$(echo "$RESPONSE" | _writ_session format 2>/dev/null) || true
            if [ -n "$RULES_TEXT" ]; then
                RULES_ONLY=$(echo "$RULES_TEXT" | grep -v "^WRIT_META:" || true)
                ADDITIONAL_CONTEXT="$RULES_ONLY"
                # Observability: record that rules were injected into this sub-agent
                # (count + ids + whether the query came from the real task or the
                # agent_type fallback). The ABSENCE of this event for an agent now
                # means it got 0 rules -- the gap that was previously invisible.
                RULES_INJECTED_EXTRA=$(echo "$RESPONSE" | AGENT_TYPE="$AGENT_TYPE" QSRC="$QUERY_SOURCE" python3 -c "
import sys, json, os
try:
    rs = json.load(sys.stdin).get('rules', [])
except Exception:
    rs = []
print(json.dumps({
    'agent_type': os.environ.get('AGENT_TYPE', ''),
    'query_source': os.environ.get('QSRC', ''),
    'rule_count': len(rs),
    'rule_ids': [r.get('rule_id', '') for r in rs],
}))
" 2>/dev/null || echo '{}')
                log_friction_event "$AGENT_ID" "" "subagent_rules_injected" "$RULES_INJECTED_EXTRA"
            fi
        fi
    fi
fi

# Get the parent's phase state for context injection
PHASE_INFO=$(python3 -c "
import sys, json
parent = json.loads(sys.argv[1])
mode = parent.get('mode', 'work')
phase = parent.get('current_phase', 'planning')
gates = parent.get('gates_approved', [])
print(f'[Writ sub-agent: mode={mode}, phase={phase}, gates={\",\".join(gates) if gates else \"none\"}]')
" "$PARENT_STATE" 2>/dev/null || echo "[Writ sub-agent: isolated session]")

# Inject via additionalContext
if [ -n "$ADDITIONAL_CONTEXT" ] || [ -n "$PHASE_INFO" ]; then
    SA_OUTPUT=$(python3 -c "
import json, sys
ctx = sys.argv[1]
if sys.argv[2]:
    ctx = sys.argv[2] + '\n' + ctx
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'SubagentStart',
        'additionalContext': ctx,
    }
}))
" "$ADDITIONAL_CONTEXT" "$PHASE_INFO" 2>/dev/null)
    if [ -n "$SA_OUTPUT" ]; then
        printf '%s\n' "$SA_OUTPUT"
        printf '%s' "$SA_OUTPUT" | blackbox_log out writ-subagent-start "$AGENT_ID"
    fi
fi

# Log sub-agent start to friction log (common.sh already sourced at top).
# CURRENT_MODE was resolved right after the sub-agent's cache was created, so the
# manual-test-grant gate row above carries it too. Nothing between there and here
# changes this agent's mode.
log_friction_event "$AGENT_ID" "$CURRENT_MODE" "subagent_start" \
    "{\"agent_type\":\"$AGENT_TYPE\",\"parent_session\":\"$PARENT_SESSION\"}"

exit 0
