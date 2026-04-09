#!/bin/bash
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

SESSION_HELPER="$SKILL_DIR/bin/lib/writ-session.py"
WRIT_HOST="${WRIT_HOST:-localhost}"
WRIT_PORT="${WRIT_PORT:-8765}"
ANALYZE_URL="http://${WRIT_HOST}:${WRIT_PORT}/analyze"

# Parse the Claude Code hook stdin envelope
PARSED=$(parse_hook_stdin)
FILE=$(parsed_field "$PARSED" "file_path")

if [ -z "$FILE" ]; then exit 0; fi

# Skip if the write itself failed
if parsed_bool "$PARSED" "is_error"; then exit 0; fi
if [ ! -f "$FILE" ]; then exit 0; fi

# Detect session ID
SESSION_ID=$(detect_session_id "$PARSED")
if [ -z "$SESSION_ID" ]; then exit 0; fi

# Check tier -- skip for Tier 0 (research, no code)
TIER=$(python3 "$SESSION_HELPER" tier get "$SESSION_ID" 2>/dev/null || echo "")
TIER=$(echo "$TIER" | tr -d '[:space:]')
if [ "$TIER" = "0" ]; then exit 0; fi

# Read session cache
CACHE=$(python3 "$SESSION_HELPER" read "$SESSION_ID" 2>/dev/null || echo '{}')

# Check analysis_results for this file -- skip if not yet passed static analysis
ANALYSIS_STATUS=$(echo "$CACHE" | python3 -c "
import sys, json
try:
    cache = json.load(sys.stdin)
    results = cache.get('analysis_results', {})
    print(results.get(sys.argv[1], 'absent'))
except Exception:
    print('absent')
" "$FILE" 2>/dev/null)

if [ "$ANALYSIS_STATUS" != "pass" ]; then
    exit 0
fi

# Build context from file path + project markers (deterministic, not free-form)
PROJECT_ROOT=$(detect_project_root "$FILE")
CONTEXT=$(python3 -c "
import sys, os

file_path = sys.argv[1]
project_root = sys.argv[2] if len(sys.argv) > 2 else ''

# Language from extension
ext_map = {
    '.php': 'PHP', '.py': 'Python', '.js': 'JavaScript', '.ts': 'TypeScript',
    '.go': 'Go', '.rs': 'Rust', '.java': 'Java', '.rb': 'Ruby',
    '.xml': 'XML', '.graphqls': 'GraphQL',
}
ext = os.path.splitext(file_path)[1]
lang = ext_map.get(ext, 'unknown')

# Framework from project markers
framework = ''
if project_root:
    markers = {
        'composer.json': 'Magento 2' if os.path.isdir(os.path.join(project_root, 'app/code')) else 'PHP',
        'package.json': 'Node.js',
        'pyproject.toml': 'Python',
        'Cargo.toml': 'Rust',
        'go.mod': 'Go',
    }
    for marker, fw in markers.items():
        if os.path.exists(os.path.join(project_root, marker)):
            framework = fw
            break

# Architectural role from path
role = 'source file'
path_lower = file_path.lower()
if '/test/' in path_lower or '/tests/' in path_lower:
    role = 'unit test'
elif '/api/data/' in path_lower:
    role = 'DTO interface'
elif '/api/' in path_lower:
    role = 'service contract'
elif '/model/data/' in path_lower:
    role = 'DTO implementation'
elif '/model/' in path_lower:
    role = 'service implementation'
elif '/etc/' in path_lower:
    if 'di.xml' in path_lower:
        role = 'dependency injection configuration'
    elif 'webapi.xml' in path_lower:
        role = 'REST API route configuration'
    elif 'module.xml' in path_lower:
        role = 'module declaration'
    else:
        role = 'configuration'
elif '/controller/' in path_lower or '/controllers/' in path_lower:
    role = 'controller'
elif '/plugin/' in path_lower:
    role = 'plugin interceptor'
elif '/observer/' in path_lower:
    role = 'event observer'

parts = [p for p in [lang, framework, role] if p]
print(' '.join(parts))
" "$FILE" "$PROJECT_ROOT" 2>/dev/null || echo "unknown")

# Determine phase from session gate state
PHASE="code_generation"
if [ -n "$PROJECT_ROOT" ]; then
    GATE_DIR="$PROJECT_ROOT/.claude/gates"
    if [ ! -f "$GATE_DIR/phase-a.approved" ] 2>/dev/null; then
        PHASE="planning"
    elif [ ! -f "$GATE_DIR/test-skeletons.approved" ] 2>/dev/null; then
        PHASE="code_generation"
    else
        PHASE="testing"
    fi
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

if [ "$VERDICT" = "pass" ] || [ "$VERDICT" = "error" ]; then
    # Check for unplanned file warning (still runs even on pass)
    PLAN_FILE=""
    if [ -n "$PROJECT_ROOT" ]; then
        PLAN_FILE=$(python3 -c "
import os, glob, sys
root = sys.argv[1]
candidates = []
candidates += glob.glob(os.path.join(root, 'app/code/*/*/plan.md'))
candidates += glob.glob(os.path.join(root, 'src/*/plan.md'))
candidates += glob.glob(os.path.join(root, 'bin/plan.md'))
candidates += glob.glob(os.path.join(root, '*/plan.md'))
found = [c for c in candidates if os.path.isfile(c)]
if found:
    found.sort(key=os.path.getmtime, reverse=True)
    print(found[0])
" "$PROJECT_ROOT" 2>/dev/null)
    fi

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

# Emit summary to stderr (Claude sees it)
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

# Phase-boundary detection and gate routing
# (only in boundary mode: all planned files written + all passed static analysis)
PLAN_FILE=""
if [ -n "$PROJECT_ROOT" ]; then
    PLAN_FILE=$(python3 -c "
import os, glob, sys
root = sys.argv[1]
candidates = []
candidates += glob.glob(os.path.join(root, 'app/code/*/*/plan.md'))
candidates += glob.glob(os.path.join(root, 'src/*/plan.md'))
candidates += glob.glob(os.path.join(root, 'bin/plan.md'))
candidates += glob.glob(os.path.join(root, '*/plan.md'))
found = [c for c in candidates if os.path.isfile(c)]
if found:
    found.sort(key=os.path.getmtime, reverse=True)
    print(found[0])
" "$PROJECT_ROOT" 2>/dev/null)
fi

if [ -z "$PLAN_FILE" ]; then
    # No plan.md -> warning mode only (Tier 1 behavior)
    exit 1
fi

# Check boundary condition: all planned files written + all passed static analysis
BOUNDARY_MODE=$(python3 -c "
import sys, json, re, os

plan_path = sys.argv[1]
cache = json.loads(sys.argv[2])

try:
    with open(plan_path) as f:
        plan_content = f.read()
except OSError:
    print('warning'); sys.exit(0)

planned_files = set()
in_files_section = False
for line in plan_content.split('\n'):
    if re.match(r'^##\s+Files', line):
        in_files_section = True
        continue
    if in_files_section and line.startswith('## '):
        break
    if in_files_section:
        for m in re.finditer(r'\x60([^\x60]+\.\w+)\x60', line):
            planned_files.add(m.group(1))
        for m in re.finditer(r'\|\s*([^\|]+\.\w+)\s*\|', line):
            path = m.group(1).strip().strip('\x60')
            if '/' in path or '.' in path:
                planned_files.add(path)

if not planned_files:
    print('warning'); sys.exit(0)

written = set(cache.get('files_written', []))
analysis = cache.get('analysis_results', {})

written_suffixes = set()
for w in written:
    parts = w.split('/')
    for i in range(len(parts)):
        written_suffixes.add('/'.join(parts[i:]))

all_written = all(
    any(p.endswith(planned) or planned in written_suffixes for p in written)
    or planned in written_suffixes
    for planned in planned_files
)

# Check all planned files passed static analysis
all_passed = True
for w in written:
    if analysis.get(w) == 'fail':
        all_passed = False
        break

print('boundary' if (all_written and all_passed) else 'warning')
" "$PLAN_FILE" "$CACHE" 2>/dev/null)

if [ "$BOUNDARY_MODE" != "boundary" ]; then
    # Per-write warning mode -- findings already emitted above
    exit 1
fi

# Phase-boundary mode: route violations using loaded_rules
echo "$RESPONSE" | python3 -c "
import sys, json, subprocess, hashlib

resp = json.load(sys.stdin)
findings = resp.get('findings', [])
session_id = sys.argv[1]
helper = sys.argv[2]
plan_file = sys.argv[3]
project_root = sys.argv[4]
cache = json.loads(sys.argv[5])

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
" "$SESSION_ID" "$SESSION_HELPER" "$PLAN_FILE" "$PROJECT_ROOT" "$CACHE" "$FILE" 2>&1 >&2

# Clear pending violations after phase-boundary scan
python3 "$SESSION_HELPER" clear-pending-violations "$SESSION_ID" 2>/dev/null || true

exit 1
