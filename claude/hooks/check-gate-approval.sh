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

FILE=$(echo "$CLAUDE_TOOL_INPUT" | python3 -c \
  "import sys,json; d=json.load(sys.stdin); print(d.get('file_path',''))" 2>/dev/null)

if [ -z "$FILE" ]; then exit 0; fi

PROJECT_ROOT=$(detect_project_root "$FILE")
GATE_DIR="$PROJECT_ROOT/.claude/gates"
mkdir -p "$GATE_DIR"

export _FILE="$FILE"
export _GATE_DIR="$GATE_DIR"
export _PROJECT_ROOT="$PROJECT_ROOT"
export _CATEGORIES_PATH="$SKILL_DIR/bin/lib/gate-categories.json"

RESULT=$(python3 << 'PYTHON_SCRIPT'
import json, os, re, sys

file_path = os.environ['_FILE']
gate_dir = os.environ['_GATE_DIR']
project_root = os.environ['_PROJECT_ROOT']
categories_path = os.environ['_CATEGORIES_PATH']

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
if [ $EXIT_CODE -ne 0 ]; then
  echo "$RESULT"
  exit 1
fi

exit 0
