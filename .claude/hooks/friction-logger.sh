#!/usr/bin/env bash
# Friction logger -- Stop hook
#
# Fires at the end of every Claude response. Reads session state and appends
# friction events to workflow-friction.log when detected.
#
# Events captured:
#   gate_denied_then_approved  -- gate was invalidated then re-approved
#   tier_escalated             -- tier changed mid-session
#   phase_transition_time      -- elapsed seconds between gate approvals
#
# Event 3 (approval_pattern_miss) is logged by auto-approve-gate.sh, not here,
# because that hook has the user prompt and match result.
#
# Hook type: Stop
# Exit: always 0 (fire-and-forget, never blocks)

set -euo pipefail

SKILL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
SESSION_HELPER="$SKILL_DIR/bin/lib/writ-session.py"

# Detect session ID from environment (Stop hooks don't get stdin envelope)
SESSION_ID="${CLAUDE_SESSION_ID:-}"
if [ -z "$SESSION_ID" ]; then
    SESSION_ID=$(ps -o ppid= -p $PPID 2>/dev/null | tr -d ' ')
fi
if [ -z "$SESSION_ID" ]; then
    exit 0
fi

# Read session state
TIER=$(python3 "$SESSION_HELPER" tier get "$SESSION_ID" 2>/dev/null || echo "")
TIER=$(echo "$TIER" | tr -d '[:space:]')

# No tier = nothing to track yet
if [ -z "$TIER" ]; then
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

# Helper: append a JSON line to the friction log
log_event() {
    echo "$1" >> "$FRICTION_LOG"
}

# ── Event 1: gate_denied_then_approved ─────────────────────────────────────
# If invalidation_history has records for a gate that now has an .approved file,
# the gate was denied and then re-approved.
CACHE=$(python3 "$SESSION_HELPER" read "$SESSION_ID" 2>/dev/null || echo '{}')

python3 -c "
import sys, json, os

cache = json.loads(sys.argv[1])
gate_dir = sys.argv[2]
session_id = sys.argv[3]
tier = sys.argv[4]
ts = sys.argv[5]
log_path = sys.argv[6]

history = cache.get('invalidation_history', {})
for gate_name, records in history.items():
    if not records:
        continue
    gate_file = os.path.join(gate_dir, f'{gate_name}.approved')
    if os.path.exists(gate_file):
        # Gate was denied (invalidated) then re-approved
        entry = json.dumps({
            'ts': ts,
            'session': session_id,
            'tier': int(tier),
            'event': 'gate_denied_then_approved',
            'gate': gate_name,
            'denials': len(records),
        })
        # Check we haven't already logged this exact gate+session+denial count
        already_logged = False
        try:
            with open(log_path) as f:
                # Scoped read: only check recent lines for this session
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
" "$CACHE" "$GATE_DIR" "$SESSION_ID" "$TIER" "$TS" "$FRICTION_LOG" 2>/dev/null || true

# ── Event 2: tier_escalated ────────────────────────────────────────────────
# Scan recent friction log entries for this session. If any have a different
# tier value than the current one, an escalation occurred.
python3 -c "
import sys, json, collections

session_id = sys.argv[1]
current_tier = int(sys.argv[2])
ts = sys.argv[3]
log_path = sys.argv[4]

# Read recent entries for this session
prior_tiers = set()
try:
    with open(log_path) as f:
        recent = collections.deque(f, maxlen=100)
        for line in recent:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            try:
                entry = json.loads(line)
                if entry.get('session') == session_id:
                    t = entry.get('tier')
                    if t is not None:
                        prior_tiers.add(int(t))
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
except FileNotFoundError:
    pass

# Check for escalation: prior entries had a lower tier
lower_tiers = {t for t in prior_tiers if t < current_tier}
if not lower_tiers:
    sys.exit(0)

old_tier = max(lower_tiers)  # most recent lower tier

# Check we haven't already logged this specific escalation
try:
    with open(log_path) as f:
        recent = collections.deque(f, maxlen=100)
        for line in recent:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            try:
                existing = json.loads(line)
                if (existing.get('session') == session_id
                        and existing.get('event') == 'tier_escalated'
                        and existing.get('new_tier') == current_tier
                        and existing.get('old_tier') == old_tier):
                    sys.exit(0)  # already logged
            except (json.JSONDecodeError, KeyError):
                continue
except FileNotFoundError:
    pass

entry = json.dumps({
    'ts': ts,
    'session': session_id,
    'tier': current_tier,
    'event': 'tier_escalated',
    'old_tier': old_tier,
    'new_tier': current_tier,
})
with open(log_path, 'a') as f:
    f.write(entry + '\n')
" "$SESSION_ID" "$TIER" "$TS" "$FRICTION_LOG" 2>/dev/null || true

# ── Event 4: phase_transition_time ─────────────────────────────────────────
# Compare mtimes of consecutive gate files to measure time in approval limbo.
python3 -c "
import sys, json, os, collections

session_id = sys.argv[1]
tier = int(sys.argv[2])
ts = sys.argv[3]
log_path = sys.argv[4]
gate_dir = sys.argv[5]

# Define gate sequences per tier
if tier == 2:
    gates = ['phase-a', 'test-skeletons']
elif tier == 3:
    gates = ['phase-a', 'phase-b', 'phase-c', 'phase-d', 'test-skeletons']
else:
    sys.exit(0)

# Collect existing gate files with mtimes
gate_times = []
for g in gates:
    path = os.path.join(gate_dir, f'{g}.approved')
    if os.path.exists(path):
        gate_times.append((g, os.path.getmtime(path)))

if len(gate_times) < 2:
    sys.exit(0)

# Sort by mtime
gate_times.sort(key=lambda x: x[1])

# Check which transitions we've already logged
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

# Log new transitions
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
        'tier': tier,
        'event': 'phase_transition_time',
        'from_gate': from_gate,
        'to_gate': to_gate,
        'elapsed_seconds': elapsed,
    })
    with open(log_path, 'a') as f:
        f.write(entry + '\n')
" "$SESSION_ID" "$TIER" "$TS" "$FRICTION_LOG" "$GATE_DIR" 2>/dev/null || true

# ── Event 5: phase_transition (Phase 3 audit trail) ───────────────────────
# Log new phase transitions from the session cache audit trail.
python3 -c "
import sys, json, collections

session_id = sys.argv[1]
tier = int(sys.argv[2])
ts = sys.argv[3]
log_path = sys.argv[4]
cache_str = sys.argv[5]

try:
    cache = json.loads(cache_str)
except (json.JSONDecodeError, ValueError):
    sys.exit(0)

transitions = cache.get('phase_transitions', [])
if not transitions:
    sys.exit(0)

# Check which transitions we've already logged
logged_count = 0
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
                        and entry.get('event') == 'phase_transition'):
                    logged_count += 1
            except (json.JSONDecodeError, KeyError):
                continue
except FileNotFoundError:
    pass

# Log new transitions (those beyond what we've already logged)
for t in transitions[logged_count:]:
    entry = json.dumps({
        'ts': ts,
        'session': session_id,
        'tier': tier,
        'event': 'phase_transition',
        'from_phase': t.get('from'),
        'to_phase': t.get('to'),
        'trigger': t.get('trigger', 'unknown'),
        'gate': t.get('gate', ''),
    })
    with open(log_path, 'a') as f:
        f.write(entry + '\n')
" "$SESSION_ID" "$TIER" "$TS" "$FRICTION_LOG" "$CACHE" 2>/dev/null || true

exit 0
