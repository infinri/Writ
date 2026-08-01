#!/bin/bash
# Exit codes: 0=pass, 1=warning (advisory), 2=blocking (stops execution)
#
# Rule compliance validation hook -- calls POST /analyze on the Writ server.
# PostToolUse: fires after every Write/Edit.
#
# Two modes:
#   Per-write (intermediate): emit warnings, log pending violations
#   Phase-boundary (all planned files written + analysis passed): route violations
#
# The hook is a thin client. Compliance judgment is done by the server via /analyze.
# The hook owns workflow orchestration: warn, gate-invalidate, escalate.
#
# Depends on: writ-session.py, /analyze endpoint
# Does NOT depend on validate-file.sh execution order (reads analysis_results defensively).

SKILL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
source "$SKILL_DIR/bin/lib/common.sh"

HOOK_START_NS=$(hook_timer_start)

SESSION_HELPER="$SKILL_DIR/bin/lib/writ-session.py"
WRIT_HOST="${WRIT_HOST:-localhost}"
WRIT_PORT="${WRIT_PORT:-8765}"
ANALYZE_URL="http://${WRIT_HOST}:${WRIT_PORT}/analyze"

# Parse the Claude Code hook stdin envelope (one python3 spawn)
load_hook_env
FILE="$HOOK_FILE_PATH"

# Session ID (needed for sentinel-path lookup even before file checks)
SESSION_ID="$HOOK_SESSION_ID"

# Sentinel-driven gate-invalidation signal: a prior run (or the boundary-mode
# block below) writes a per-session sentinel when it routes a finding to
# invalidate-gate. Honor that signal as the only path to exit 2; remove the
# sentinel after reading so a follow-up run starts clean. This replaces the
# unconditional exit 2 that produced the cosmetic "0 potential issues found
# but unconfirmed" non-blocking banner on no-violation boundary scans.
# Check the sentinel BEFORE early exits so the gate-invalidation signal is
# delivered even when the current write does not pass the other filters.
if [ -n "$SESSION_ID" ]; then
    SENTINEL_PATH="${TMPDIR:-/tmp}/writ-validate-rules-invalidated-${SESSION_ID}"
    if [ -f "$SENTINEL_PATH" ]; then
        rm -f "$SENTINEL_PATH"
        exit 2
    fi
fi

if [ -z "$FILE" ]; then exit 0; fi

# Skip if the write itself failed
if [ "$HOOK_IS_ERROR" = "1" ]; then exit 0; fi
if [ ! -f "$FILE" ]; then exit 0; fi

if [ -z "$SESSION_ID" ]; then exit 0; fi

# Skip for non-work modes (no code generation)
MODE=$(_writ_session "mode get" "$SESSION_ID" 2>/dev/null || echo "")
MODE=$(echo "$MODE" | tr -d '[:space:]')
if [ "$MODE" != "work" ]; then exit 0; fi

# Read session cache
CACHE=$(_writ_session read "$SESSION_ID" 2>/dev/null || echo '{}')

# Item 4b: single helper invocation emits should_proceed + context + phase +
# plan_file + boundary_mode in one JSON blob. Was four sequential python3 -c
# spawns (analysis status read, context builder, plan-file glob, boundary
# detect) in v1.1.0; helper computes them all from one cache dict.
PROJECT_ROOT=$(detect_project_root "$FILE")
HELPER_PRE=$(python3 "$SKILL_DIR/bin/lib/validate-rules-helper.py" pre-analyze \
    --session-id "$SESSION_ID" \
    --file "$FILE" \
    --project-root "$PROJECT_ROOT" \
    --cache-json "$CACHE" 2>/dev/null || echo '{}')

# Parse helper output with one python3 spawn (reads multiple fields at once).
eval "$(echo "$HELPER_PRE" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
print('SHOULD_PROCEED=' + ('1' if d.get('should_proceed') else '0'))
print('CONTEXT=' + repr(d.get('context', '')))
print('PHASE=' + repr(d.get('phase', 'code_generation')))
print('HELPER_PLAN_FILE=' + repr(d.get('plan_file', '')))
print('BOUNDARY_MODE=' + repr(d.get('boundary_mode', 'warning')))
" 2>/dev/null)"

if [ "$SHOULD_PROCEED" != "1" ]; then
    exit 0
fi

# Read file content
CODE=$(cat "$FILE" 2>/dev/null || echo "")
if [ -z "$CODE" ]; then exit 0; fi

# Call /analyze endpoint
RESPONSE=$(python3 -c "
import sys, json

request = {
    'code': sys.argv[1][:50000],  # cap at 50k chars
    'file_path': sys.argv[2],
    'phase': sys.argv[3],
    'context': sys.argv[4],
}
print(json.dumps(request))
" "$CODE" "$FILE" "$PHASE" "$CONTEXT" 2>/dev/null | \
    curl -s --connect-timeout 0.5 --max-time 15 \
        -X POST "$ANALYZE_URL" \
        -H "Content-Type: application/json" \
        -d @- 2>/dev/null) || true

if [ -z "$RESPONSE" ]; then
    exit 0
fi

# Parse verdict
VERDICT=$(echo "$RESPONSE" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    if 'error' in d:
        print('error')
    else:
        print(d.get('verdict', 'pass'))
except Exception:
    print('error')
" 2>/dev/null || echo "error")

# A server-side analysis error must not masquerade as a clean pass: say so once,
# visibly, before taking the same non-blocking path (the check is best-effort).
if [ "$VERDICT" = "error" ]; then
    echo "[Writ] /analyze errored for this write; compliance check skipped (not a pass)." >&2
fi

if [ "$VERDICT" = "pass" ] || [ "$VERDICT" = "error" ]; then
    # Item 4b: helper already computed plan_file; reuse it.
    PLAN_FILE="$HELPER_PLAN_FILE"

    if [ -n "$PLAN_FILE" ] && [ -f "$PLAN_FILE" ]; then
        FILE_IN_PLAN=$(python3 -c "
import sys, re, os
plan_path = sys.argv[1]
written_file = sys.argv[2]
try:
    with open(plan_path) as f:
        content = f.read()
except OSError:
    print('unknown'); sys.exit(0)
plan_paths = set()
for m in re.finditer(r'\x60([^\x60]+\.\w+)\x60', content):
    plan_paths.add(m.group(1))
for m in re.finditer(r'\|\s*([^\|]+\.\w+)\s*\|', content):
    path = m.group(1).strip().strip('\x60')
    if '/' in path or '.' in path:
        plan_paths.add(path)
if not plan_paths:
    print('unknown'); sys.exit(0)
basename_parts = written_file.split('/')
for planned in plan_paths:
    if written_file.endswith(planned) or planned in '/'.join(basename_parts[-3:]):
        print('yes'); sys.exit(0)
print('no')
" "$PLAN_FILE" "$FILE" 2>/dev/null || echo "unknown")

        if [ "$FILE_IN_PLAN" = "no" ]; then
            echo "[Writ: unplanned file] $FILE is not listed in plan.md. Update the plan if this file is needed, or remove it if not." >&2
        fi
    fi

    hook_timer_end "$HOOK_START_NS" "validate-rules" "$SESSION_ID" "$MODE"
    exit 0
fi

# Violations found (verdict = fail or warn)
# Log findings as pending violations
echo "$RESPONSE" | python3 -c "
import sys, json, subprocess

resp = json.load(sys.stdin)
findings = resp.get('findings', [])
session_id = sys.argv[1]
helper = sys.argv[2]

for f in findings:
    if f.get('status') != 'violated':
        continue
    cmd = [
        'python3', helper, 'add-pending-violation', session_id,
        '--rule', f['rule_id'],
        '--file', sys.argv[3],
        '--evidence', f.get('evidence', '')[:200],
    ]
    if f.get('line'):
        cmd.extend(['--line', str(f['line'])])
    subprocess.run(cmd, capture_output=True)
" "$SESSION_ID" "$SESSION_HELPER" "$FILE" 2>/dev/null

# Count confirmed (status=="violated") findings. A "warn" verdict with zero
# confirmed violations (only "uncertain" findings -> "No confirmed violations;
# N uncertain finding(s)") is not actionable: stay silent and exit 0 rather than
# surface a non-blocking hook "error" on every edit. WARN_EXIT drives the
# per-write warning-mode exits below.
VIOLATED_COUNT=$(echo "$RESPONSE" | python3 -c "
import sys, json
try:
    resp = json.load(sys.stdin)
    print(sum(1 for f in resp.get('findings', []) if f.get('status') == 'violated'))
except Exception:
    print(0)
" 2>/dev/null || echo 0)
if [ "${VIOLATED_COUNT:-0}" -gt 0 ]; then WARN_EXIT=1; else WARN_EXIT=0; fi

# Emit the compliance summary + findings only when there are confirmed violations.
if [ "${VIOLATED_COUNT:-0}" -gt 0 ]; then
SUMMARY=$(echo "$RESPONSE" | python3 -c "
import sys, json
resp = json.load(sys.stdin)
print(resp.get('summary', 'Rule compliance check completed.'))
" 2>/dev/null || echo "Rule compliance check completed.")

echo "[Writ rule compliance] $SUMMARY" >&2

# Emit individual findings
echo "$RESPONSE" | python3 -c "
import sys, json
resp = json.load(sys.stdin)
for f in resp.get('findings', []):
    if f.get('status') == 'violated':
        print(f'  {f[\"rule_id\"]}: {f.get(\"evidence\", \"\")}', file=sys.stderr)
        if f.get('suggestion'):
            print(f'    Fix: {f[\"suggestion\"]}', file=sys.stderr)
" 2>&1 >&2
fi

# Item 4b: helper already computed plan_file + boundary_mode in pre-analyze.
PLAN_FILE="$HELPER_PLAN_FILE"

if [ -z "$PLAN_FILE" ]; then
    # No plan.md -> warning mode only (Tier 1 behavior). Silent (exit 0) when
    # there are no confirmed violations; advisory (exit 1) when there are.
    hook_timer_end "$HOOK_START_NS" "validate-rules" "$SESSION_ID" "$MODE"
    exit "$WARN_EXIT"
fi

# Boundary mode already computed by the helper above. BOUNDARY_MODE is set
# from the helper's JSON blob ("boundary" or "warning"). Per-write warning
# mode emits findings to stderr above; only "boundary" continues to gate
# routing.
if [ "$BOUNDARY_MODE" != "boundary" ]; then
    exit "$WARN_EXIT"
fi

# Phase-boundary mode: route violations using loaded_rules.
# The gate-invalidation Python block emits the per-session sentinel when at
# least one finding is routed to invalidate-gate. The hook's final exit is
# driven by the sentinel check at the top of the script on the NEXT run; this
# run's tail-end check below catches sentinels written on this turn.
echo "$RESPONSE" | python3 -c "
import sys, json, subprocess, hashlib, os

resp = json.load(sys.stdin)
findings = resp.get('findings', [])
session_id = sys.argv[1]
helper = sys.argv[2]
plan_file = sys.argv[3]
project_root = sys.argv[4]
cache = json.loads(sys.argv[5])
sentinel_path = sys.argv[7]

loaded_rule_ids = {r['rule_id'] for r in cache.get('loaded_rules', [])}

try:
    with open(plan_file) as f:
        plan_hash = hashlib.md5(f.read().encode()).hexdigest()[:12]
except OSError:
    plan_hash = 'unknown'

for f in findings:
    if f.get('status') != 'violated':
        continue
    rid = f['rule_id']

    if rid not in loaded_rule_ids:
        # New finding -- rule wasn't available at planning time
        print(f'[Writ: new finding] {rid} not in session rules -- warning only.', file=sys.stderr)
        continue

    # Rule was available at planning time -- gate invalidation
    cmd = [
        'python3', helper, 'invalidate-gate', session_id, 'phase-a',
        '--rule', rid,
        '--file', sys.argv[6],
        '--evidence', f.get('evidence', '')[:200],
        '--plan-hash', plan_hash,
        '--project-root', project_root,
    ]
    subprocess.run(cmd, capture_output=True)
    print(f'[Writ PLANNING GAP] {rid} violated in {sys.argv[6]}. Phase-a gate invalidated.', file=sys.stderr)
    try:
        with open(sentinel_path, 'w') as sf:
            sf.write('invalidated')
    except OSError:
        pass
" "$SESSION_ID" "$SESSION_HELPER" "$PLAN_FILE" "$PROJECT_ROOT" "$CACHE" "$FILE" "$SENTINEL_PATH" 2>&1 >&2

# Clear pending violations after phase-boundary scan
_writ_session clear-pending-violations "$SESSION_ID" 2>/dev/null || true

hook_timer_end "$HOOK_START_NS" "validate-rules" "$SESSION_ID" "$MODE"

# Sentinel-driven final exit. Exit 2 only when the gate-invalidation block
# wrote the sentinel; remove it after reading so the next run starts clean.
if [ -f "$SENTINEL_PATH" ]; then
    rm -f "$SENTINEL_PATH"
    exit 2
fi
exit 0
