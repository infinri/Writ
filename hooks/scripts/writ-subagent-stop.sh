#!/usr/bin/env bash
# SubagentStop hook -- drains the sub-agent's telemetry buffer, runs the transcript
# tripwire, records a reviewer verdict, and logs completion metrics.
#
# NOT metrics-only (it was, until 2026-08-11). Three of the four jobs above are
# governance: the buffer drain is the only one a sub-agent ever gets, the reviewer
# verdict has to be couriered by infrastructure rather than by the author, and the
# transcript tripwire is the only read window on a sub-agent transcript at all
# (Claude Code deletes those when the session ends).
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

# TWO transcript paths arrive here and they are NOT interchangeable. Measured
# 2026-08-11 over 42 captured SubagentStop envelopes: `agent_transcript_path` names the
# sub-agent's OWN transcript (42/42) and `transcript_path` names the PARENT session's
# transcript (42/42). Both are read only to decide whether there is anything for the
# tripwire to try; which one wins, and the refusal when they name the same file, are the
# resolver's job in Python (see the tripwire block below).
AGENT_TRANSCRIPT=$(parsed_field "$STDIN_JSON" "agent_transcript_path")
PARENT_TRANSCRIPT=$(parsed_field "$STDIN_JSON" "transcript_path")

# THE SUB-AGENT'S TELEMETRY BUFFER DRAINS HERE, because nothing else ever will.
#
# Rows are buffered per session and released by writ_event_buffer_flush, which is called
# from friction-logger.sh (Stop) and writ-session-end.sh (SessionEnd). A sub-agent gets
# NEITHER of those events; it gets this one. So every sub-agent buffer had no active
# drain trigger at all and survived only on the passive 24h _sweep_abandoned in
# writ-flush-events.py. Measured 2026-08-08: 12 buffers holding ~1378 stranded
# hook_execution rows, 8 of them keyed to sub-agent ids from the running session.
#
# AGENT_ID IS THE RIGHT KEY, not the parent's session id: a sub-agent's hooks resolve
# HOOK_SESSION_ID as `agent_id // session_id` (load_hook_env), so the buffer this hook
# has to claim is named after the agent. It comes from the payload and nowhere else --
# no /tmp pointer, no PID or md5 synthesis -- because a drain aimed at a guessed id
# takes another session's rows and strands these.
#
# BEFORE the early exit below, so a payload this hook declines to process further still
# releases its rows, and `|| true` because this hook must exit 0 on every path
# (friction-logger.sh drains under the same guarantee).
if [ -n "$AGENT_ID" ]; then
    writ_event_buffer_flush "$AGENT_ID" || true
else
    # Recorded rather than silent. Live SubagentStop payloads always carry agent_id
    # (verified against captured envelopes), so its absence is a broken invariant, and
    # without the id there is no buffer name to drain -- a state otherwise
    # indistinguishable from a sub-agent that buffered nothing.
    writ_critical "writ-subagent-stop" \
        "no agent_id in SubagentStop payload; this sub-agent's telemetry buffer cannot be named and will wait for the abandoned-session sweep" \
        "${PARENT_SESSION:-unknown}"
fi

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

# TRANSCRIPT TRIPWIRE: refuse to let a queued-input misdelivery be invisible.
#
# Claude Code can splice a user keystroke queued during a dispatch into the SUB-AGENT's
# pending turn instead of holding it for the parent. Writ cannot fix the harness; it can
# only make a recurrence leave a record. The signature is structural, never textual: a
# user-role entry in the sub-agent's transcript carrying a free-text block it has no
# business carrying (the known 2026-08-10 finding is text sitting in the SAME user
# envelope as a returned tool_result: text_count=1, tool_result_count=1). Measured
# 2026-08-11: 2 matches in ~10,446 user messages across 130 local transcript files
# (0.02%), none of them a benign harness sentinel -- a rate low enough and clean enough
# to wire into the hook rather than leave behind a CLI subcommand nobody runs.
#
# HERE AND NOWHERE ELSE, because sub-agent transcripts are not durable: Claude Code
# removes them when the session ends, so SubagentStop is the only reliable read window.
#
# NO additionalContext ON ANY PATH. A Stop-family hook's additionalContext is treated by
# Claude Code as a turn BLOCK -- a verified live incident in this repo (fix 779c92e) --
# and a tripwire that halts turns on a structural heuristic would do more damage than the
# bug it reports. The finding goes to the logs; the session is never interrupted.
#
# Cost: at most one bounded scan of exactly one file per sub-agent completion, and
# nothing at all on the per-prompt path.
#
# EITHER key is enough to try. The guard that used to sit here -- a shell test that
# AGENT_TRANSCRIPT differed from PARENT_TRANSCRIPT -- moved into
# resolve_subagent_transcript, for two reasons. It gated the whole tripwire on
# `agent_transcript_path` being present, which made the resolver's documented fallbacks
# (the flat derivation and the workflow-nested glob) dead code from here: a payload
# without that key wrote nothing even when its sub-agent transcript was derivable and
# malformed. And a string comparison in shell cannot see through a symlink or a
# relative-versus-absolute spelling of the same file, so it was the weaker form of the
# check. The resolver now compares resolved paths and answers None when the candidate
# IS the parent transcript, so a collapse still scans nothing.
if [ -n "$AGENT_TRANSCRIPT" ] || [ -n "$PARENT_TRANSCRIPT" ]; then
    # Every failure mode degrades to a silent no-op (`|| true` under set -euo pipefail):
    # a missing module, a deleted transcript, an unparseable line. A tripwire is not
    # worth a hook failure.
    TRIPWIRE_OUT=$(printf '%s' "$STDIN_JSON" | python3 -c '
import json, sys
sys.path.insert(0, sys.argv[1])
try:
    from writ.session.transcript_tripwire import resolve_subagent_transcript, scan_transcript
except Exception:
    sys.exit(0)
try:
    payload = json.load(sys.stdin)
except Exception:
    sys.exit(0)
try:
    path = resolve_subagent_transcript(payload)
    if path is None:
        sys.exit(0)
    findings = scan_transcript(path)
except Exception:
    sys.exit(0)
total = len(findings)
# Capped output: a pathological transcript must not turn one completion into thousands
# of rows. The count survives on every emitted record as findings_total.
for finding in findings[:5]:
    try:
        record = dict(finding.to_record())
    except Exception:
        continue
    record["hook"] = "writ-subagent-stop"
    record["agent_id"] = sys.argv[2]
    record["agent_type"] = sys.argv[3]
    record["findings_total"] = total
    print(json.dumps(record, separators=(",", ":"), default=str))
' "$WRIT_DIR" "$AGENT_ID" "$AGENT_TYPE" 2>/dev/null || true)

    if [ -n "$TRIPWIRE_OUT" ]; then
        # ATTRIBUTED TO THE PARENT, not to the sub-agent. The finding is about foreign
        # input delivered INTO this agent's turn, so letting the agent file it would make
        # the suspect the courier, and the agent's session is a throwaway nobody reads.
        # The session id is passed explicitly on argv: rows that rely on an ambient
        # ${SESSION_ID:-${HOOK_SESSION_ID:-}} land under the literal id `unknown` in a
        # hook like this one, which sets neither (that already stranded thousands of rows
        # for another gate).
        TRIPWIRE_SESSION="${PARENT_SESSION:-unknown}"
        while IFS= read -r TRIPWIRE_REC; do
            [ -n "$TRIPWIRE_REC" ] || continue
            # STRUCTURE ONLY -- to_record() carries path, line, timestamp, digest and
            # three counts, never the text. The foreign text is the one thing that must
            # not be copied into a log to prove it was there.
            log_friction_event "$TRIPWIRE_SESSION" "" "foreign_input_in_subagent_turn" \
                "$TRIPWIRE_REC"
            # ALSO to the errors stream, because friction is a file an operator greps
            # later and this is a finding they need to see now.
            writ_critical "writ-subagent-stop" \
                "foreign input in sub-agent turn: a user-role entry in this sub-agent's transcript carries a free-text block it should not have (Claude Code queued-input misdelivery). Structural evidence only, text withheld: $TRIPWIRE_REC" \
                "$TRIPWIRE_SESSION"
        done <<TRIPWIRE_EOF
$TRIPWIRE_OUT
TRIPWIRE_EOF
    fi
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
