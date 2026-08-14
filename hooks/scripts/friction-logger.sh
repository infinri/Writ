#!/usr/bin/env bash
# Friction logger -- Stop hook
#
# Fires at the end of every Claude response. Reads session state and appends
# friction events to the env-aware friction log (resolved by friction-append.py)
# when detected.
#
# Events captured:
#   gate_denied_then_approved  -- gate was invalidated then re-approved
#   phase_transition_time      -- elapsed seconds between gate approvals
#   phase_transition           -- phase changed (from session audit trail)
#
# mode_change events are logged by writ-session.py directly (not here).
# approval_pattern_miss is logged by auto-approve-gate.sh (has the prompt).
#
# Hook type: Stop
# Exit: always 0 (fire-and-forget, never blocks)

set -euo pipefail

SKILL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
SESSION_HELPER="$SKILL_DIR/bin/lib/writ-session.py"
FA="$SKILL_DIR/bin/lib/friction-append.py"
source "$SKILL_DIR/bin/lib/common.sh"

# Loop-breaker (CC Stop-hook contract): if CC is re-invoking us after a prior
# block, stop_hook_active is true -- exit 0 now so this Stop hook can never loop
# to CC's block cap. Matches the sibling Stop hooks (enforce-violations.sh,
# writ-comms-output-gate.sh).
STDIN_JSON=$(cat 2>/dev/null || echo '{}')
if stop_hook_active "$STDIN_JSON"; then
    exit 0
fi

# THE PAYLOAD IS AUTHORITATIVE, the pointer file is only a fallback.
#
# /tmp/writ-current-session is ONE global file, rewritten by writ-rag-inject.sh on every
# UserPromptSubmit in EVERY Claude Code session on this machine, so the most recent session
# to take a turn owns it. Reading it unconditionally meant this Stop hook drained whichever
# session happened to be last, not the one it was invoked for.
#
# That was survivable while every hook wrote its telemetry directly. It stopped being
# survivable when hook_execution and gate_decision rows moved to a per-session buffer
# drained here: draining the wrong session strands this one's rows on disk. Measured on a
# live session, 2026-08-08: 443 undrained rows, because the pointer had been taken over by
# another session (cdp-12096297) and this session's buffer was never the drain target.
#
# CC sends session_id in the Stop payload, which is captured above for the loop-breaker, so
# the right answer was already in hand.
# NO POINTER FALLBACK. The first fix here still consulted the pointer when the payload had
# no session_id, which keeps the failure mode alive in the rare case rather than removing
# it. A drain aimed at the wrong session is worse than a drain that does not run, because
# the rows it skips look identical to rows that never existed. Unresolvable now records a
# critical error and this hook does nothing.
SESSION_ID=$(writ_require_session "$STDIN_JSON" friction-logger) || exit 0

# Drain this turn's buffered hook_execution rows in ONE interpreter start, replacing the
# 8 python spawns a single file write used to pay. Placed BEFORE the mode gate below on
# purpose: hooks buffer rows in every mode, so draining after that gate would strand
# them whenever the session is not in a tracked mode.
# Rows survive the paths that skip this: the buffer is keyed by SESSION, not by turn, so
# a turn that exits early (stop_hook_active above) drains on the next Stop or at
# SessionEnd. Never fails this hook.
writ_event_buffer_flush "$SESSION_ID" || true

# Read current mode
MODE=$(_writ_session "mode get" "$SESSION_ID" 2>/dev/null || echo "")
MODE=$(echo "$MODE" | tr -d '[:space:]')

# No mode = nothing to track yet
if [ -z "$MODE" ]; then
    exit 0
fi

# Detect project root from cwd
PROJECT_ROOT=$(detect_project_root "$(pwd -P)")

if [ -z "$PROJECT_ROOT" ]; then
    exit 0
fi

# Resolve the stream paths through the P1 logging router. This hook's three
# events split across TWO streams per STREAM_MAP (writ/shared/logging.py):
# gate_denied_then_approved and phase_transition are AUDIT-classified, but
# phase_transition_time is METRICS-classified. Each event's dedup read below
# MUST target the same stream its append lands in, or it never finds the prior
# row and re-appends a duplicate on every Stop fire. Appends further down still
# pipe through friction-append.py (which routes via the same router), so these
# paths are used ONLY for the dedup reads. Guarded to be NON-FATAL: any failure
# degrades to an empty path so `set -e` never aborts the hook; an empty/missing
# path makes the dedup input empty (the event is still appended, at worst
# re-logged once).
AUDIT_LOG=$(python3 -c "
import sys
sys.path.insert(0, '$SKILL_DIR')
from writ.shared.logging import stream_path, resolve_project
print(stream_path(resolve_project(), 'audit'))
" 2>/dev/null || echo "")
METRICS_LOG=$(python3 -c "
import sys
sys.path.insert(0, '$SKILL_DIR')
from writ.shared.logging import stream_path, resolve_project
print(stream_path(resolve_project(), 'metrics'))
" 2>/dev/null || echo "")
# THIS SESSION's own gate directory. Both events below correlate on-disk artifacts with
# THIS session's cache (invalidation_history for Event 1, gate mtimes for Event 2), so the
# project-wide path made a sibling session's approval satisfy the disk half of a correlation
# whose other half was this session's alone: a "denied then approved" row for an approval
# this session never received. An empty answer means no directory to correlate against, and
# both events are skipped rather than measured against another session's files.
GATE_DIR=$(writ_gate_dir "$PROJECT_ROOT" "$SESSION_ID")
TS=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

# ── Event 1: gate_denied_then_approved ─────────────────────────────────────
# If invalidation_history has records for a gate that now has an .approved file,
# the gate was denied and then re-approved.
CACHE=$(_writ_session read "$SESSION_ID" 2>/dev/null || echo '{}')

python3 -c "
import sys, json, os

cache = json.loads(sys.argv[1])
gate_dir = sys.argv[2]
session_id = sys.argv[3]
mode = sys.argv[4]
ts = sys.argv[5]
log_path = sys.argv[6]

# No session-scoped gate directory (unusable session id): nothing to correlate. Guarded
# here rather than in the caller so the empty case cannot be joined into a relative path,
# which would read '<cwd>/phase-a.approved' as an approval.
if not gate_dir:
    sys.exit(0)

history = cache.get('invalidation_history', {})
for gate_name, records in history.items():
    if not records:
        continue
    gate_file = os.path.join(gate_dir, f'{gate_name}.approved')
    if os.path.exists(gate_file):
        entry = {
            'ts': ts,
            'session': session_id,
            'mode': mode,
            'event': 'gate_denied_then_approved',
            'gate': gate_name,
            'denials': len(records),
        }
        already_logged = False
        try:
            with open(log_path) as f:
                import collections
                recent = collections.deque(f, maxlen=100)
                for line in recent:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    try:
                        existing = json.loads(line)
                        if (existing.get('session') == session_id
                                and existing.get('event') == 'gate_denied_then_approved'
                                and existing.get('gate') == gate_name
                                and existing.get('denials') == len(records)):
                            already_logged = True
                            break
                    except (json.JSONDecodeError, KeyError):
                        continue
        except FileNotFoundError:
            pass

        if not already_logged:
            print(json.dumps(entry))
" "$CACHE" "$GATE_DIR" "$SESSION_ID" "$MODE" "$TS" "$AUDIT_LOG" 2>/dev/null | python3 "$FA" --stdin-json 2>/dev/null || true

# ── Event 2: phase_transition_time ────────────────────────────────────────
# Compare mtimes of consecutive gate files to measure time in approval limbo.
# Only relevant in Work mode.
if [ "$MODE" = "work" ] && [ -n "$GATE_DIR" ]; then
    python3 -c "
import sys, json, os, collections

session_id = sys.argv[1]
mode = sys.argv[2]
ts = sys.argv[3]
log_path = sys.argv[4]
gate_dir = sys.argv[5]

gates = ['phase-a', 'test-skeletons']

gate_times = []
for g in gates:
    path = os.path.join(gate_dir, f'{g}.approved')
    if os.path.exists(path):
        gate_times.append((g, os.path.getmtime(path)))

if len(gate_times) < 2:
    sys.exit(0)

gate_times.sort(key=lambda x: x[1])

logged_transitions = set()
try:
    with open(log_path) as f:
        recent = collections.deque(f, maxlen=100)
        for line in recent:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            try:
                entry = json.loads(line)
                if (entry.get('session') == session_id
                        and entry.get('event') == 'phase_transition_time'):
                    key = (entry.get('from_gate', ''), entry.get('to_gate', ''))
                    logged_transitions.add(key)
            except (json.JSONDecodeError, KeyError):
                continue
except FileNotFoundError:
    pass

for i in range(1, len(gate_times)):
    from_gate = gate_times[i-1][0]
    to_gate = gate_times[i][0]
    key = (from_gate, to_gate)
    if key in logged_transitions:
        continue
    elapsed = int(gate_times[i][1] - gate_times[i-1][1])
    entry = {
        'ts': ts,
        'session': session_id,
        'mode': mode,
        'event': 'phase_transition_time',
        'from_gate': from_gate,
        'to_gate': to_gate,
        'elapsed_seconds': elapsed,
    }
    print(json.dumps(entry))
" "$SESSION_ID" "$MODE" "$TS" "$METRICS_LOG" "$GATE_DIR" 2>/dev/null \
        | while IFS= read -r _line; do
              [ -n "$_line" ] && printf '%s' "$_line" | python3 "$FA" --stdin-json 2>/dev/null
          done || true
fi

# ── Event 3: phase_transition (audit trail) ───────────────────────────────
# Log new phase transitions from the session cache audit trail.
# Tracks logged count in session cache to avoid re-logging on subsequent Stop fires.
python3 -c "
import sys, json, os

session_id = sys.argv[1]
mode = sys.argv[2]
ts = sys.argv[3]
log_path = sys.argv[4]
cache_str = sys.argv[5]
helper = sys.argv[6]

try:
    cache = json.loads(cache_str)
except (json.JSONDecodeError, ValueError):
    sys.exit(0)

transitions = cache.get('phase_transitions', [])
if not transitions:
    sys.exit(0)

# Use cache-tracked count instead of scanning log (prevents duplicates across Stop fires)
logged_count = cache.get('phase_transitions_logged', 0)
new_transitions = transitions[logged_count:]
if not new_transitions:
    sys.exit(0)

for t in new_transitions:
    entry = {
        'ts': ts,
        'session': session_id,
        'mode': mode,
        'event': 'phase_transition',
        'from_phase': t.get('from'),
        'to_phase': t.get('to'),
        'trigger': t.get('trigger', 'unknown'),
        'gate': t.get('gate', ''),
    }
    print(json.dumps(entry))

# Update cache with new logged count
import importlib.util
spec = importlib.util.spec_from_file_location('writ_session', helper)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
with mod.mutate_cache(session_id) as c:
    c['phase_transitions_logged'] = len(transitions)
" "$SESSION_ID" "$MODE" "$TS" "$AUDIT_LOG" "$CACHE" "$SESSION_HELPER" 2>/dev/null \
    | while IFS= read -r _line; do
          [ -n "$_line" ] && printf '%s' "$_line" | python3 "$FA" --stdin-json 2>/dev/null
      done || true

exit 0
