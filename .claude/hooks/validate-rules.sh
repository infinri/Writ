#!/bin/bash
# Rule compliance validation hook -- checks written code against stored Writ rules.
# PostToolUse: fires after every Write/Edit.
#
# Two modes:
#   Per-write (intermediate): emit warnings, log pending violations
#   Phase-boundary (all planned files written): route violations to gate invalidation
#
# Depends on: writ-session.py (loaded_rules, analysis_results, files_written, pending_violations)
# Does NOT depend on validate-file.sh execution order (reads analysis_results defensively).

SKILL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
source "$SKILL_DIR/bin/lib/common.sh"

SESSION_HELPER="$SKILL_DIR/bin/lib/writ-session.py"

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

# Extract loaded rules and check for violations
VIOLATIONS=$(echo "$CACHE" | python3 -c "
import sys, json, re

cache = json.load(sys.stdin)
rules = cache.get('loaded_rules', [])
file_path = sys.argv[1]

if not rules:
    sys.exit(0)

try:
    with open(file_path) as f:
        content = f.read()
        lines = content.split('\n')
except OSError:
    sys.exit(0)

findings = []

for rule in rules:
    violation = rule.get('violation', '')
    if not violation:
        continue

    # Extract code patterns from violation examples.
    # Look for method calls, function patterns, anti-patterns.
    patterns = []

    # Match method calls like ->toArray(), ->getData(), ::load(
    for m in re.finditer(r'(?:->|::)(\w+)\s*\(', violation):
        call = m.group(1)
        # Skip very common methods that would false-positive everywhere
        if call in ('get', 'set', 'create', 'build', 'run', 'execute', 'process'):
            continue
        patterns.append((rf'(?:->|::){re.escape(call)}\s*\(', call))

    # Match new SomeClass( instantiation patterns
    for m in re.finditer(r'\bnew\s+(\w+)\s*\(', violation):
        cls = m.group(1)
        patterns.append((rf'\bnew\s+{re.escape(cls)}\s*\(', f'new {cls}('))

    # Match hardcoded secrets patterns (sk_live_, AKIA, password =)
    for m in re.finditer(r\"'(sk_live_|AKIA|password\s*=)[^']*'\", violation):
        patterns.append((re.escape(m.group(1)), m.group(1)))

    # Match Factory->create()->load( pattern
    if 'Factory->create()->load(' in violation or 'Factory->create()->loadBy' in violation:
        patterns.append((r'Factory->create\(\)->load', 'Factory->create()->load'))

    for pattern, label in patterns:
        for line_num, line_text in enumerate(lines, 1):
            if re.search(pattern, line_text):
                findings.append({
                    'rule_id': rule['rule_id'],
                    'file': file_path,
                    'line': line_num,
                    'evidence': f'{label} at line {line_num}: {line_text.strip()[:120]}',
                    'statement': rule.get('statement', '')[:200],
                })

json.dump(findings, sys.stdout)
" "$FILE" 2>/dev/null) || true

if [ -z "$VIOLATIONS" ] || [ "$VIOLATIONS" = "[]" ]; then
    exit 0
fi

VIOLATION_COUNT=$(echo "$VIOLATIONS" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "0")

if [ "$VIOLATION_COUNT" = "0" ]; then
    exit 0
fi

# Log each violation as pending
echo "$VIOLATIONS" | python3 -c "
import sys, json, subprocess

violations = json.load(sys.stdin)
session_id = sys.argv[1]
helper = sys.argv[2]

for v in violations:
    cmd = [
        'python3', helper, 'add-pending-violation', session_id,
        '--rule', v['rule_id'],
        '--file', v['file'],
        '--evidence', v['evidence'],
    ]
    if v.get('line'):
        cmd.extend(['--line', str(v['line'])])
    subprocess.run(cmd, capture_output=True)
" "$SESSION_ID" "$SESSION_HELPER" 2>/dev/null

# Determine mode: per-write (warning) or phase-boundary (gate routing)
PROJECT_ROOT=$(detect_project_root "$FILE")
PLAN_FILE=""
if [ -n "$PROJECT_ROOT" ]; then
    # Search for plan.md in module directories first, then project root
    PLAN_FILE=$(python3 -c "
import os, glob, sys
root = sys.argv[1]
candidates = []
candidates += glob.glob(os.path.join(root, 'app/code/*/*/plan.md'))
candidates += glob.glob(os.path.join(root, 'src/*/plan.md'))
candidates += glob.glob(os.path.join(root, 'bin/plan.md'))
candidates += glob.glob(os.path.join(root, '*/plan.md'))
candidates += [os.path.join(root, 'plan.md')]
found = [c for c in candidates if os.path.isfile(c)]
if found:
    found.sort(key=os.path.getmtime, reverse=True)
    print(found[0])
" "$PROJECT_ROOT" 2>/dev/null)
fi

# Check if current file is in the plan -- warn if not
if [ -n "$PLAN_FILE" ] && [ -f "$PLAN_FILE" ]; then
    FILE_IN_PLAN=$(python3 -c "
import sys, re, os

plan_path = sys.argv[1]
written_file = sys.argv[2]

try:
    with open(plan_path) as f:
        content = f.read()
except OSError:
    print('unknown')
    sys.exit(0)

# Extract all file paths from the plan
plan_paths = set()
for m in re.finditer(r'\x60([^\x60]+\.\w+)\x60', content):
    plan_paths.add(m.group(1))
for m in re.finditer(r'\|\s*([^\|]+\.\w+)\s*\|', content):
    path = m.group(1).strip().strip('\x60')
    if '/' in path or '.' in path:
        plan_paths.add(path)

if not plan_paths:
    print('unknown')
    sys.exit(0)

# Check if written file matches any planned path (by suffix)
basename_parts = written_file.split('/')
for planned in plan_paths:
    if written_file.endswith(planned) or planned in '/'.join(basename_parts[-3:]):
        print('yes')
        sys.exit(0)

print('no')
" "$PLAN_FILE" "$FILE" 2>/dev/null || echo "unknown")

    if [ "$FILE_IN_PLAN" = "no" ]; then
        echo "[Writ: unplanned file] $FILE is not listed in plan.md. Update the plan if this file is needed, or remove it if not." >&2
    fi
fi

# No plan.md -> warning mode only (Tier 1 or plan not yet written)
if [ -z "$PLAN_FILE" ]; then
    echo "$VIOLATIONS" | python3 -c "
import sys, json
for v in json.load(sys.stdin):
    rid = v['rule_id']
    evidence = v['evidence']
    statement = v.get('statement', '')
    print(f'[Writ rule warning] {rid}: {evidence}', file=sys.stderr)
    if statement:
        print(f'  Rule: {statement}', file=sys.stderr)
" 2>&1 >&2
    exit 0
fi

# Check if all planned files are written (phase-boundary detection)
BOUNDARY_MODE=$(python3 -c "
import sys, json, re

plan_path = sys.argv[1]
cache = json.loads(sys.argv[2])

# Extract file paths from plan.md ## Files section
try:
    with open(plan_path) as f:
        plan_content = f.read()
except OSError:
    print('warning')
    sys.exit(0)

# Parse file paths from markdown table or backtick references
planned_files = set()
in_files_section = False
for line in plan_content.split('\n'):
    if re.match(r'^##\s+Files', line):
        in_files_section = True
        continue
    if in_files_section and line.startswith('## '):
        break
    if in_files_section:
        # Match backtick-quoted paths or table cell paths
        for m in re.finditer(r'\x60([^\x60]+\.\w+)\x60', line):
            planned_files.add(m.group(1))
        # Match paths in table cells (between pipes)
        for m in re.finditer(r'\|\s*([^\|]+\.\w+)\s*\|', line):
            path = m.group(1).strip().strip('\x60')
            if '/' in path or '.' in path:
                planned_files.add(path)

if not planned_files:
    print('warning')
    sys.exit(0)

written = set(cache.get('files_written', []))

# Check if written files (by basename or suffix) cover all planned files
# Plans often use relative paths; written files are absolute
written_suffixes = set()
for w in written:
    parts = w.split('/')
    for i in range(len(parts)):
        written_suffixes.add('/'.join(parts[i:]))

all_covered = all(
    any(p.endswith(planned) or planned in written_suffixes for p in written)
    or planned in written_suffixes
    for planned in planned_files
)

print('boundary' if all_covered else 'warning')
" "$PLAN_FILE" "$CACHE" 2>/dev/null)

if [ "$BOUNDARY_MODE" != "boundary" ]; then
    # Per-write warning mode
    echo "$VIOLATIONS" | python3 -c "
import sys, json
for v in json.load(sys.stdin):
    rid = v['rule_id']
    evidence = v['evidence']
    statement = v.get('statement', '')
    print(f'[Writ rule warning] {rid}: {evidence}', file=sys.stderr)
    if statement:
        print(f'  Rule: {statement}', file=sys.stderr)
" 2>&1 >&2
    exit 0
fi

# Phase-boundary mode: route violations
# Read ## Rules Applied from plan.md
PLAN_RULES=$(python3 -c "
import sys, re

try:
    with open(sys.argv[1]) as f:
        content = f.read()
except OSError:
    print('[]')
    sys.exit(0)

rule_ids = set()
in_rules_section = False
for line in content.split('\n'):
    if re.match(r'^##\s+Rules\s+[Aa]pplied', line):
        in_rules_section = True
        continue
    if in_rules_section and line.startswith('## '):
        break
    if in_rules_section:
        for m in re.finditer(r'[A-Z]+-[A-Z]+-\d{3}', line):
            rule_ids.add(m.group(0))

import json
print(json.dumps(sorted(rule_ids)))
" "$PLAN_FILE" 2>/dev/null || echo '[]')

# Route each violation: planning gap or implementation error
HAS_ERRORS=0
echo "$VIOLATIONS" | python3 -c "
import sys, json, subprocess, hashlib

violations = json.load(sys.stdin)
plan_rules = set(json.loads(sys.argv[1]))
session_id = sys.argv[2]
helper = sys.argv[3]
plan_file = sys.argv[4]
project_root = sys.argv[5]

# Compute plan hash
try:
    with open(plan_file) as f:
        plan_hash = hashlib.md5(f.read().encode()).hexdigest()[:12]
except OSError:
    plan_hash = 'unknown'

planning_gaps = []
impl_errors = []

for v in violations:
    rid = v['rule_id']
    if rid in plan_rules:
        impl_errors.append(v)
    else:
        planning_gaps.append(v)

# Planning gaps: invalidate phase-a gate
for v in planning_gaps:
    cmd = [
        'python3', helper, 'invalidate-gate', session_id, 'phase-a',
        '--rule', v['rule_id'],
        '--file', v['file'],
        '--evidence', v['evidence'],
        '--plan-hash', plan_hash,
        '--project-root', project_root,
    ]
    subprocess.run(cmd, capture_output=True)
    print(f'[Writ PLANNING GAP] {v[\"rule_id\"]} violated in {v[\"file\"]}:{v.get(\"line\", \"?\")}', file=sys.stderr)
    print(f'  {v[\"evidence\"]}', file=sys.stderr)
    print(f'  Rule not in plan -- phase-a gate invalidated.', file=sys.stderr)

# Implementation errors: emit as errors, no gate invalidation
for v in impl_errors:
    print(f'[Writ IMPLEMENTATION ERROR] {v[\"rule_id\"]} violated in {v[\"file\"]}:{v.get(\"line\", \"?\")}', file=sys.stderr)
    print(f'  {v[\"evidence\"]}', file=sys.stderr)
    if v.get('statement'):
        print(f'  Rule: {v[\"statement\"]}', file=sys.stderr)

# Report
if impl_errors:
    print(f'impl_errors:{len(impl_errors)}')
if planning_gaps:
    print(f'planning_gaps:{len(planning_gaps)}')
" "$PLAN_RULES" "$SESSION_ID" "$SESSION_HELPER" "$PLAN_FILE" "$PROJECT_ROOT" 2>&1

# Check for implementation errors in output (exit 1 to signal errors to Claude)
if echo "$VIOLATIONS" | python3 -c "
import sys, json
violations = json.load(sys.stdin)
plan_rules = set(json.loads(sys.argv[1]))
has_impl_errors = any(v['rule_id'] in plan_rules for v in violations)
sys.exit(0 if not has_impl_errors else 1)
" "$PLAN_RULES" 2>/dev/null; then
    true
else
    HAS_ERRORS=1
fi

# Clear pending violations after phase-boundary scan
python3 "$SESSION_HELPER" clear-pending-violations "$SESSION_ID" 2>/dev/null || true

exit $HAS_ERRORS
