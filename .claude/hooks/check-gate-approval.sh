#!/bin/bash
# Gate sequencing hook -- blocks writes until phase approval markers exist.
# PreToolUse: fires before every Write/Edit/MultiEdit.
# Category-based: classifies files by purpose, checks the gate for that category.
# Three-tier pattern matching:
#   1. _any    -- cross-language directory patterns (always checked)
#   2. language -- file extension patterns (php, python, go, etc.)
#   3. framework -- detected from project markers (magento2, django, rails, etc.)
# Gate markers are ALWAYS project-specific: {PROJECT_ROOT}/.claude/gates/
# Output: structured JSON.
#
# Gate sequence: A -> B -> C -> [D] -> test-skeletons
# Each gate enforces all prior gates in the chain (sequential ordering).

SKILL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
source "$SKILL_DIR/bin/lib/common.sh"

# Parse the Claude Code hook stdin envelope (consumes stdin once)
PARSED=$(parse_hook_stdin)
FILE=$(parsed_field "$PARSED" "file_path")

if [ -z "$FILE" ]; then exit 0; fi

# Skip gate checks for Writ skill infrastructure and global settings
case "$FILE" in
    "$SKILL_DIR"/*|"$HOME/.claude/settings"*) exit 0 ;;
esac

PROJECT_ROOT=$(detect_project_root "$FILE")
GATE_DIR="$PROJECT_ROOT/.claude/gates"
mkdir -p "$GATE_DIR"

# Read tier from session cache
SESSION_HELPER="$SKILL_DIR/bin/lib/writ-session.py"
SESSION_ID=$(detect_session_id "$PARSED")
TIER=$(python3 "$SESSION_HELPER" tier get "$SESSION_ID" 2>/dev/null || echo "")
TIER=$(echo "$TIER" | tr -d '[:space:]')

# Tier 0 (Research) and Tier 1 (Patch): bypass all gates
if [ "$TIER" = "0" ] || [ "$TIER" = "1" ]; then
    exit 0
fi

# No tier declared: block writes and require classification first
if [ -z "$TIER" ]; then
    python3 -c "
import json, sys
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': sys.argv[1]
    }
}))
" "[ENF-GATE-TIER] No task tier declared. You must classify the task tier BEFORE writing any code. Run: python3 $SESSION_HELPER tier set <0-3> $SESSION_ID -- Tier 0 (Research): no code generation. Tier 1 (Patch): <=3 files, no new contracts. Tier 2 (Standard): new class/interface, single domain. Tier 3 (Complex): multi-domain, concurrency, queues. After setting the tier, follow the workflow for that tier (plan.md, gates, etc.) before writing code."
    exit 0
fi

export _FILE="$FILE"
export _GATE_DIR="$GATE_DIR"
export _PROJECT_ROOT="$PROJECT_ROOT"
export _CATEGORIES_PATH="$SKILL_DIR/bin/lib/gate-categories.json"
export _TIER="${TIER:-}"

RESULT=$(python3 << 'PYTHON_SCRIPT'
import json, os, re, sys

file_path = os.environ['_FILE']
gate_dir = os.environ['_GATE_DIR']
project_root = os.environ['_PROJECT_ROOT']
categories_path = os.environ['_CATEGORIES_PATH']
tier_str = os.environ.get('_TIER', '')
tier = int(tier_str) if tier_str.isdigit() else None

# ── Load category definitions ────────────────────────────────────────────────
with open(categories_path) as f:
    config = json.load(f)

# ── Pattern matching (bash-style: * matches /) ──────────────────────────────
def glob_match(path, pattern):
    regex = re.escape(pattern).replace(r'\*', '.*').replace(r'\?', '.')
    return bool(re.fullmatch(regex, path))

def matches_any(path, patterns):
    basename = os.path.basename(path)
    for p in patterns:
        if glob_match(path, p) or glob_match(basename, p):
            return True
    return False

# ── Check exclusions ────────────────────────────────────────────────────────
if matches_any(file_path, config.get('exclusions', [])):
    sys.exit(0)

# ── Detect language from file extension ─────────────────────────────────────
ext_map = {
    '.php': 'php', '.xml': 'xml',
    '.js': 'javascript', '.jsx': 'javascript',
    '.ts': 'typescript', '.tsx': 'typescript',
    '.py': 'python', '.rs': 'rust', '.go': 'go',
    '.java': 'java', '.rb': 'ruby',
    '.graphqls': 'graphql', '.graphql': 'graphql',
}
ext = os.path.splitext(file_path)[1]
lang = ext_map.get(ext, 'unknown')

# ── Detect frameworks from project markers ──────────────────────────────────
# Checks for marker files defined in framework_detection.
# A project can also declare its framework explicitly via .claude/framework.
detected_frameworks = []

# Explicit declaration takes priority
explicit_path = os.path.join(project_root, '.claude', 'framework')
if os.path.isfile(explicit_path):
    with open(explicit_path) as f:
        for line in f:
            fw = line.strip()
            if fw and not fw.startswith('#'):
                detected_frameworks.append(fw)
else:
    # Auto-detect from marker files
    for fw, markers in config.get('framework_detection', {}).items():
        for marker in markers:
            if os.path.exists(os.path.join(project_root, marker)):
                detected_frameworks.append(fw)
                break

# ── Tier 2 gate remapping ───────────────────────────────────────────────────
# Tier 2 (Standard): Phases A-C combined into one approval (phase-a gate).
# Only phase-a and test-skeletons gates are required.
# Concurrency category auto-escalates to tier 3 (not remapped).
import copy

if tier == 2:
    config = copy.deepcopy(config)
    for cat in config['categories']:
        if cat['id'] == 'concurrency':
            # Concurrency in tier 2 means the task should be tier 3.
            # Don't remap -- let the full gate sequence block it with
            # a message that naturally prompts escalation.
            pass
        elif cat['id'] == 'implementation':
            cat['gate'] = 'test-skeletons'
            cat['prior_gates'] = ['phase-a']
        else:
            cat['gate'] = 'phase-a'
            cat['prior_gates'] = []

# ── Classify and gate ───────────────────────────────────────────────────────
# Categories are ordered by gate sequence (A -> B -> C -> D -> test-skeletons).
# A file can match multiple categories. We check each in order and block on
# the first missing gate, so the earliest unmet requirement is reported first.
#
# For each category, patterns are collected from three tiers:
#   _any      -- always checked (cross-language directory conventions)
#   {lang}    -- checked when file extension maps to a known language
#   {fw}      -- checked for each detected framework

def gate_exists(gate_name):
    return os.path.isfile(os.path.join(gate_dir, gate_name + '.approved'))

def emit_finding(rule, message, fix_gate):
    print(json.dumps({
        'error': True,
        'rule': rule,
        'message': message,
        'file': file_path,
        'gate': fix_gate,
        'fix': 'touch ' + os.path.join(gate_dir, fix_gate + '.approved')
    }))

for cat in config['categories']:
    patterns = cat.get('patterns', {})
    matched = False

    # Tier 1: cross-language directory patterns
    if matches_any(file_path, patterns.get('_any', [])):
        matched = True

    # Tier 2: language-specific patterns
    if not matched and lang != 'unknown':
        if matches_any(file_path, patterns.get(lang, [])):
            matched = True

    # Tier 3: framework-specific patterns
    if not matched:
        for fw in detected_frameworks:
            if matches_any(file_path, patterns.get(fw, [])):
                matched = True
                break

    if not matched:
        continue

    # Enforce sequential ordering: all prior gates must exist
    for prior in cat.get('prior_gates', []):
        if not gate_exists(prior):
            emit_finding(
                'ENF-GATE-004',
                '%s requires %s approval first (sequential gate ordering)' % (cat['id'], prior),
                prior
            )
            sys.exit(1)

    # Enforce this category's gate
    gate = cat['gate']
    if not gate_exists(gate):
        emit_finding(cat['rule'], cat['message'], gate)
        sys.exit(1)

# No category matched, or all matched gates are approved
sys.exit(0)
PYTHON_SCRIPT
)

EXIT_CODE=$?
if [ $EXIT_CODE -ne 0 ] && [ -n "$RESULT" ]; then
  # Extract the message from the JSON finding for the denial reason
  REASON=$(echo "$RESULT" | python3 -c "
import sys, json, os

try:
    d = json.load(sys.stdin)
    msg = d.get('message', 'Gate approval required')
    fix = d.get('fix', '')
    rule = d.get('rule', '')
    parts = []
    if rule: parts.append('[' + rule + ']')
    parts.append(msg)
    if fix: parts.append('Fix: ' + fix)

    # Check for invalidation context
    gate_name = d.get('gate', '')
    session_id = sys.argv[1] if len(sys.argv) > 1 else ''
    helper = sys.argv[2] if len(sys.argv) > 2 else ''
    if gate_name and session_id and helper:
        import subprocess
        result = subprocess.run(
            ['python3', helper, 'read', session_id],
            capture_output=True, text=True, timeout=1,
        )
        if result.returncode == 0:
            cache = json.loads(result.stdout)
            records = cache.get('invalidation_history', {}).get(gate_name, [])
            if records:
                latest = records[-1]
                parts.append(
                    f'Gate was invalidated: {latest[\"rule_id\"]} violated in {latest[\"file\"]} ({latest.get(\"evidence\", \"\")[:100]}). Revise the plan to address this gap.'
                )
    print(' '.join(parts))
except Exception:
    print('Gate approval required')
" "$SESSION_ID" "$SESSION_HELPER" 2>/dev/null)
  python3 -c "
import json, sys
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': sys.argv[1]
    }
}))
" "$REASON"
  exit 0
fi

exit 0
