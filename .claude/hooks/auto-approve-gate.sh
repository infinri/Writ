#!/usr/bin/env bash
# Auto-approve gate -- UserPromptSubmit hook
#
# When the user's prompt matches an approval pattern ("approved", "lgtm",
# "proceed", etc.), this hook creates the next pending gate file.
#
# Tier-aware: only creates gates that apply to the current tier.
#   Tier 0-1: no gates, hook exits immediately.
#   Tier 2:   phase-a, test-skeletons
#   Tier 3:   phase-a, phase-b, phase-c, phase-d, test-skeletons
#
# The hook outputs which gate was approved so Claude sees it in context.
#
# Hook type: UserPromptSubmit
# Exit: always 0 (never block user prompt)

set -euo pipefail

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
SESSION_HELPER="$WRIT_DIR/bin/lib/writ-session.py"

# Read stdin once -- Claude Code sends JSON with prompt, session_id, etc.
STDIN_JSON=$(cat)

# Extract session_id and prompt
PARSED=$(echo "$STDIN_JSON" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    sid = data.get('session_id', '')
    prompt = data.get('prompt', data.get('message', data.get('content', '')))
    print(f'{sid}\n{prompt}')
except Exception:
    print('\n')
" 2>/dev/null) || true

SESSION_ID=$(echo "$PARSED" | head -1)
PROMPT=$(echo "$PARSED" | tail -n +2)

# Fallback session ID
if [ -z "$SESSION_ID" ]; then
    SESSION_ID=$(ps -o ppid= -p $PPID 2>/dev/null | tr -d ' ')
fi
if [ -z "$SESSION_ID" ]; then
    SESSION_ID=$(echo "${PWD}:${USER}" | md5sum | cut -c1-12)-$(date +%Y%m%d)
fi

# Check if prompt matches an approval pattern
# Must be a short, clear approval -- not a long message that happens to contain "approve"
PROMPT_LOWER=$(echo "$PROMPT" | tr '[:upper:]' '[:lower:]' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')

IS_APPROVAL=$(python3 -c "
import re, sys

prompt = sys.argv[1]

# Exact matches (the whole prompt is just an approval)
exact = {
    'approved', 'approve', 'lgtm', 'proceed', 'go ahead',
    'looks good', 'ship it', 'yes', 'yep', 'y', 'ok', 'okay',
    'go', 'do it', 'continue', 'accepted', 'accept',
}

# Strip trailing punctuation for matching
clean = re.sub(r'[.!,]+$', '', prompt.strip())

if clean in exact:
    print('yes')
    sys.exit(0)

# Fuzzy match: Levenshtein distance <= 2 from any approval word.
# Catches typos like 'apporved', 'aproved', 'approvd', 'approed'.
def levenshtein(s1, s2):
    if len(s1) < len(s2):
        return levenshtein(s2, s1)
    if len(s2) == 0:
        return len(s1)
    prev = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        curr = [i + 1]
        for j, c2 in enumerate(s2):
            curr.append(min(prev[j + 1] + 1, curr[j] + 1, prev[j] + (c1 != c2)))
        prev = curr
    return prev[-1]

fuzzy_targets = ['approved', 'approve', 'proceed', 'accepted', 'accept']
if len(clean) <= 12:
    for target in fuzzy_targets:
        if levenshtein(clean, target) <= 2:
            print('yes')
            sys.exit(0)

# Pattern matches (short prompts that are clearly approval)
# Only match if prompt is short (< 60 chars) to avoid false positives
if len(prompt) < 60:
    patterns = [
        r'^(?:yes|yep|yeah),?\s*(?:approved?|proceed|go ahead|looks? good|lgtm)',
        r'^(?:approved?|proceed|go ahead)\s*[.!]*$',
        r'^(?:phase\s*[a-d]|test.skeletons?)\s*(?:approved?|lgtm)\s*[.!]*$',
        r'^(?:approve|create)\s+(?:phase|gate)',
    ]
    for p in patterns:
        if re.match(p, prompt):
            print('yes')
            sys.exit(0)

print('no')
" "$PROMPT_LOWER" 2>/dev/null || echo "no")

if [ "$IS_APPROVAL" != "yes" ]; then
    exit 0
fi

# Get current tier
CURRENT_TIER=$(python3 "$SESSION_HELPER" tier get "$SESSION_ID" 2>/dev/null || echo "")
CURRENT_TIER=$(echo "$CURRENT_TIER" | tr -d '[:space:]')

# Tier 0-1: no gates to approve
if [ "$CURRENT_TIER" = "0" ] || [ "$CURRENT_TIER" = "1" ]; then
    exit 0
fi

# No tier declared: can't approve gates without knowing which apply
if [ -z "$CURRENT_TIER" ]; then
    exit 0
fi

# Detect project root
PROJECT_ROOT=$(python3 -c "
import os, sys
markers = ['composer.json','package.json','Cargo.toml','go.mod','pyproject.toml','.git']
path = os.getcwd()
while path != '/':
    if any(os.path.exists(os.path.join(path, m)) for m in markers):
        print(path); sys.exit(0)
    path = os.path.dirname(path)
print('')
" 2>/dev/null)

if [ -z "$PROJECT_ROOT" ]; then
    exit 0
fi

GATE_DIR="$PROJECT_ROOT/.claude/gates"
mkdir -p "$GATE_DIR"

# Define gate sequences per tier
if [ "$CURRENT_TIER" = "2" ]; then
    GATES="phase-a test-skeletons"
elif [ "$CURRENT_TIER" = "3" ]; then
    GATES="phase-a phase-b phase-c phase-d test-skeletons"
else
    exit 0
fi

# Find the first missing gate and create it
for gate in $GATES; do
    gate_file="$GATE_DIR/${gate}.approved"
    if [ ! -f "$gate_file" ]; then
        # For phase-d in tier 3: only create if there's a reason to
        # (concurrency/queue work declared). Skip to next gate if not needed.
        # Users can still explicitly approve phase-d by typing "phase d approved".
        if [ "$gate" = "phase-d" ]; then
            # Check if user explicitly mentioned phase-d
            if echo "$PROMPT_LOWER" | grep -qE 'phase.?d'; then
                touch "$gate_file"
                echo "[Gate approved: $gate] Created $gate_file"
                exit 0
            fi
            # Skip phase-d, try test-skeletons next
            continue
        fi

        # Enforce artifacts exist before gate approval
        if [ "$gate" = "phase-a" ]; then
            # plan.md must exist in a module directory (not project root) with required sections
            PLAN_CHECK=$(python3 -c "
import os, glob, sys, re

root = sys.argv[1]

# Search module directories only -- not project root
candidates = []
candidates += glob.glob(os.path.join(root, 'app/code/*/*/plan.md'))
candidates += glob.glob(os.path.join(root, 'src/*/plan.md'))
candidates += glob.glob(os.path.join(root, 'bin/plan.md'))
# Also check one level deep (for non-standard layouts)
candidates += glob.glob(os.path.join(root, '*/plan.md'))

plan_files = [c for c in candidates if os.path.isfile(c)]

if not plan_files:
    print('MISSING: plan.md not found in any module directory. Write it to the module directory (e.g., app/code/Vendor/Module/plan.md), not the project root.')
    sys.exit(0)

# Check the most recently modified plan.md
plan_files.sort(key=os.path.getmtime, reverse=True)
plan_path = plan_files[0]

with open(plan_path) as f:
    content = f.read()

missing = []

# Check for ## Files section
if not re.search(r'^##\s+Files', content, re.MULTILINE):
    missing.append('## Files section (list files to create/modify)')

# Check for ## Rules Applied or ## Rules applied with at least one rule ID
rules_match = re.search(r'^##\s+Rules\s+[Aa]pplied', content, re.MULTILINE)
if not rules_match:
    missing.append('## Rules Applied section')
else:
    # Check for at least one rule ID pattern after the section header
    section_start = rules_match.end()
    rest = content[section_start:]
    next_section = re.search(r'^## ', rest, re.MULTILINE)
    section_text = rest[:next_section.start()] if next_section else rest
    if not re.search(r'[A-Z]+-[A-Z]+-\d{3}', section_text):
        missing.append('at least one rule ID (e.g., SEC-UNI-003) in ## Rules Applied')

# Check for ## Capabilities with at least one checkbox
caps_match = re.search(r'^##\s+Capabilities', content, re.MULTILINE)
if not caps_match:
    missing.append('## Capabilities section with checkbox items')
else:
    section_start = caps_match.end()
    rest = content[section_start:]
    next_section = re.search(r'^## ', rest, re.MULTILINE)
    section_text = rest[:next_section.start()] if next_section else rest
    if not re.search(r'\[[ x]\]', section_text):
        missing.append('at least one checkbox item (- [ ] ...) in ## Capabilities')

if missing:
    print('INCOMPLETE: plan.md at ' + plan_path + ' is missing: ' + '; '.join(missing))
else:
    print('OK')
" "$PROJECT_ROOT" 2>/dev/null || echo "MISSING: plan.md check failed")

            if [ "${PLAN_CHECK:0:2}" != "OK" ]; then
                echo "[Gate blocked: $gate] $PLAN_CHECK"
                exit 0
            fi
        fi

        # Test skeleton files must exist before test-skeletons approval
        # Scoped to the module directory (derived from plan.md location)
        if [ "$gate" = "test-skeletons" ]; then
            TEST_FOUND=$(python3 -c "
import os, glob, sys

root = sys.argv[1]

# Find plan.md to determine module directory
candidates = []
candidates += glob.glob(os.path.join(root, 'app/code/*/*/plan.md'))
candidates += glob.glob(os.path.join(root, 'src/*/plan.md'))
candidates += glob.glob(os.path.join(root, 'bin/plan.md'))
candidates += glob.glob(os.path.join(root, '*/plan.md'))
plan_files = [c for c in candidates if os.path.isfile(c)]

if not plan_files:
    # No plan.md -- fall back to project-wide search but exclude vendor/setup
    print('no')
    sys.exit(0)

plan_files.sort(key=os.path.getmtime, reverse=True)
module_dir = os.path.dirname(plan_files[0])

# Search for test files scoped to the module directory
patterns = [
    os.path.join(module_dir, '**/Test/**/*Test.php'),
    os.path.join(module_dir, '**/tests/**/*test*.py'),
    os.path.join(module_dir, '**/test/**/*test*.py'),
    os.path.join(module_dir, '**/__tests__/**/*.test.*'),
    os.path.join(module_dir, '**/tests/**/*_test.go'),
    os.path.join(module_dir, '**/test/**/*_test.rs'),
]
found = False
for p in patterns:
    if glob.glob(p, recursive=True):
        found = True
        break
print('yes' if found else 'no')
" "$PROJECT_ROOT" 2>/dev/null || echo "no")

            if [ "$TEST_FOUND" != "yes" ]; then
                echo "[Gate blocked: $gate] Test skeleton files must be written before test-skeletons can be approved. Write the test files first."
                exit 0
            fi
        fi

        touch "$gate_file"
        echo "[Gate approved: $gate] Created $gate_file"
        exit 0
    fi
done

# All gates already approved
echo "[All gates already approved for Tier $CURRENT_TIER]"
exit 0
