#!/usr/bin/env bash
# SubagentStop hook -- logs sub-agent completion metrics.
#
# When a sub-agent completes, this hook logs the event to the friction log
# for observability and rule coverage analysis.
#
# Hook type: SubagentStop
# Exit: always 0

set -euo pipefail

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
source "$WRIT_DIR/bin/lib/common.sh"

# Phase 4c: capture stderr (Python tracebacks etc.) to debug log so
# next-occurrence diagnostics are readable. tee preserves stderr
# propagation so behavior is unchanged. Gated behind WRIT_DEBUG (default OFF):
# the sink is /dev/null unless WRIT_DEBUG=1.
exec 2> >(tee -a "$(_writ_debug_enabled && echo "${WRIT_HOOK_LOG:-/tmp/writ-hook-debug.log}" || echo /dev/null)" >&2)

SESSION_HELPER="$WRIT_DIR/bin/lib/writ-session.py"
FA="$WRIT_DIR/bin/lib/friction-append.py"

# Read stdin envelope
STDIN_JSON=$(cat)

# Phase 3 diagnostic: bounded capture of the raw SubagentStop envelope so we can
# compare its agent_id/agent_type against the SubagentStart capture and pinpoint why
# built-in agents (Explore) fail to correlate to their start-created session. /tmp,
# capped at 50, fire-and-forget -- never affects the hook outcome.
_WRIT_STOP_CAP=/tmp/writ-subagent-stop-payloads.jsonl
if [ "$(wc -l < "$_WRIT_STOP_CAP" 2>/dev/null || echo 0)" -lt 50 ]; then
    printf '%s\n' "$STDIN_JSON" >> "$_WRIT_STOP_CAP" 2>/dev/null || true
fi

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
        "{\"hook\":\"writ-subagent-stop\",\"parent_session\":\"$PARENT_SESSION\"}"
fi

# Cycle 9: record a reviewer's verdict HERE, in infrastructure, rather than letting
# the orchestrator report it. The reviewer's JSON otherwise reaches only the agent
# whose code was reviewed, which leaves the author adjudicating the critic. The
# harness hands us the reviewer's own final text, so the author is never the courier.
# Recorded against the PARENT session (the one that will run `git commit`), not the
# agent's own throwaway session. Fire-and-forget: never changes the hook outcome.
if [ "$AGENT_TYPE" = "writ-reviewer" ] && [ -n "$PARENT_SESSION" ]; then
    REVIEW_MSG=$(parsed_field "$STDIN_JSON" "last_assistant_message")
    # Recorded UNCONDITIONALLY, including when the message is empty: an empty
    # message parses as unparseable, which counts as blocking. Skipping the record
    # would leave "no verdict", which does NOT block, so a reviewer that stopped
    # without a final word would silently read as approval.
    # Message on stdin, not argv: a review runs to thousands of characters and
    # would otherwise land on the process table and risk ARG_MAX.
    if ! printf '%s' "$REVIEW_MSG" \
            | python3 "$WRIT_DIR/bin/lib/review_findings.py" record \
                "$PARENT_SESSION" "$AGENT_ID" >/dev/null 2>&1; then
        # Never fatal, but never silent either: a regression in the recording path
        # is otherwise indistinguishable from "no reviewer ran", which is exactly
        # the state that does not block.
        log_friction_event "$PARENT_SESSION" "" "review_verdict_record_failed" \
            "{\"hook\":\"writ-subagent-stop\",\"agent_id\":\"$AGENT_ID\"}"
    fi
fi

# Read the agent's session cache for summary metrics
CACHE=$(_writ_session read "$AGENT_ID" 2>/dev/null || echo '{}')

python3 -c "
import sys, json, os
from datetime import datetime, timezone

cache = json.loads(sys.argv[1])
agent_id = sys.argv[2]
agent_type = sys.argv[3]
parent_session = sys.argv[4]

entry = {
    'ts': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    'session': agent_id,
    'mode': cache.get('mode'),
    'event': 'subagent_complete',
    'agent_id': agent_id,
    'agent_type': agent_type,
    'parent_session': parent_session,
    'files_written': len(cache.get('files_written', [])),
    'rules_loaded': len(cache.get('loaded_rule_ids', [])),
    'queries': cache.get('queries', 0),
    'remaining_budget': cache.get('remaining_budget', 0),
    'denial_count': sum(cache.get('denial_counts', {}).values()),
}

print(json.dumps(entry))
" "$CACHE" "$AGENT_ID" "$AGENT_TYPE" "$PARENT_SESSION" 2>/dev/null | python3 "$FA" --stdin-json 2>/dev/null || true

exit 0
