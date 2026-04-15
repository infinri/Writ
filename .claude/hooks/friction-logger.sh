#!/usr/bin/env bash
# Friction logger -- Stop hook
#
# Fires at the end of every Claude response. Reads session state and appends
# friction events to workflow-friction.log when detected.
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
source "$SKILL_DIR/bin/lib/common.sh"

# Read session ID published by writ-rag-inject.sh (fires on every UserPromptSubmit)
SESSION_ID=""
if [ -f /tmp/writ-current-session ]; then
    SESSION_ID=$(cat /tmp/writ-current-session 2>/dev/null | tr -d '[:space:]')
fi
if [ -z "$SESSION_ID" ]; then
    exit 0
fi

# Read current mode
MODE=$(_writ_session "mode get" "$SESSION_ID" 2>/dev/null || echo "")
MODE=$(echo "$MODE" | tr -d '[:space:]')

# No mode = nothing to track yet
if [ -z "$MODE" ]; then
    exit 0
fi

# Detect project root from cwd
PROJECT_ROOT=$(python3 -c "
import os
markers = ['composer.json','package.json','Cargo.toml','go.mod','pyproject.toml','.git']
path = os.getcwd()
while path != '/':
    if any(os.path.exists(os.path.join(path, m)) for m in markers):
        print(path); break
    path = os.path.dirname(path)
" 2>/dev/null)

if [ -z "$PROJECT_ROOT" ]; then
    exit 0
fi

FRICTION_LOG="$PROJECT_ROOT/workflow-friction.log"
GATE_DIR="$PROJECT_ROOT/.claude/gates"
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

history = cache.get('invalidation_history', {})
for gate_name, records in history.items():
    if not records:
        continue
    gate_file = os.path.join(gate_dir, f'{gate_name}.approved')
    if os.path.exists(gate_file):
        entry = json.dumps({
            'ts': ts,
            'session': session_id,
            'mode': mode,
            'event': 'gate_denied_then_approved',
            'gate': gate_name,
            'denials': len(records),
        })
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
            with open(log_path, 'a') as f:
                f.write(entry + '\n')
" "$CACHE" "$GATE_DIR" "$SESSION_ID" "$MODE" "$TS" "$FRICTION_LOG" 2>/dev/null || true

# ── Event 2: phase_transition_time ────────────────────────────────────────
# Compare mtimes of consecutive gate files to measure time in approval limbo.
# Only relevant in Work mode.
if [ "$MODE" = "work" ]; then
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
    entry = json.dumps({
        'ts': ts,
        'session': session_id,
        'mode': mode,
        'event': 'phase_transition_time',
        'from_gate': from_gate,
        'to_gate': to_gate,
        'elapsed_seconds': elapsed,
    })
    with open(log_path, 'a') as f:
        f.write(entry + '\n')
" "$SESSION_ID" "$MODE" "$TS" "$FRICTION_LOG" "$GATE_DIR" 2>/dev/null || true
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
    entry = json.dumps({
        'ts': ts,
        'session': session_id,
        'mode': mode,
        'event': 'phase_transition',
        'from_phase': t.get('from'),
        'to_phase': t.get('to'),
        'trigger': t.get('trigger', 'unknown'),
        'gate': t.get('gate', ''),
    })
    with open(log_path, 'a') as f:
        f.write(entry + '\n')

# Update cache with new logged count
import importlib.util
spec = importlib.util.spec_from_file_location('writ_session', helper)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
c = mod._read_cache(session_id)
c['phase_transitions_logged'] = len(transitions)
mod._write_cache(session_id, c)
" "$SESSION_ID" "$MODE" "$TS" "$FRICTION_LOG" "$CACHE" "$SESSION_HELPER" 2>/dev/null || true

exit 0
