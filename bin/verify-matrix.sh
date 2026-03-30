#!/bin/bash
# Completion matrix verifier — deterministic replacement for plan-guardian Pass 1.
# Reads the machine-readable capabilities block from plan.md,
# cross-references against the file manifest on disk.
# Returns a completion matrix with PRESENT/MISSING status per capability.
#
# Usage:
#   bin/verify-matrix.sh /path/to/plan.md file1.php file2.php ...
#   bin/verify-matrix.sh /path/to/plan.md --manifest-stdin < file-list.txt
#
# Requires plan.md to contain a fenced capabilities block:
#
#   ```capabilities
#   - id: CAP-001
#     phase: A
#     description: REST endpoint for order accrual
#     files:
#       - Api/AccrualInterface.php
#       - Model/AccrualService.php
#     verifications:
#       - type: file_exists
#       - type: class_implements
#         class: AccrualService
#         interface: AccrualInterface
#   ```
#
# Output: { "matrix": [...], "missing": [...], "total": N, "present": N, "complete": bool }

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/common.sh"

PLAN_PATH=""
PROJECT_ROOT=""
FILES=()
USE_STDIN=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --manifest-stdin) USE_STDIN=true; shift ;;
    --project-root) PROJECT_ROOT="$2"; shift 2 ;;
    *)
      if [ -z "$PLAN_PATH" ]; then
        PLAN_PATH="$1"
      else
        FILES+=("$1")
      fi
      shift
      ;;
  esac
done

if [ -z "$PLAN_PATH" ]; then
  echo '{"error": "No plan.md path specified. Usage: verify-matrix.sh plan.md file1 [file2 ...]"}'
  exit 2
fi

if [ ! -f "$PLAN_PATH" ]; then
  echo "{\"error\": \"plan.md not found at: $PLAN_PATH\"}"
  exit 2
fi

if $USE_STDIN; then
  while IFS= read -r line; do
    [ -n "$line" ] && FILES+=("$line")
  done
fi

# Detect project root from plan.md location if not specified
if [ -z "$PROJECT_ROOT" ]; then
  PROJECT_ROOT=$(detect_project_root "$PLAN_PATH")
fi
FILE_LIST=$(printf '%s\n' "${FILES[@]}")

export _PLAN_PATH="$PLAN_PATH"
export _PROJECT_ROOT="$PROJECT_ROOT"
export _FILE_LIST="$FILE_LIST"

python3 << 'PYTHON_SCRIPT'
import json, os, sys, re
try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

plan_path = os.environ.get('_PLAN_PATH', '')
project_root = os.environ.get('_PROJECT_ROOT', '')
file_list_raw = os.environ.get('_FILE_LIST', '')

manifest_files = [f.strip() for f in file_list_raw.strip().split('\n') if f.strip()]

# ── Extract capabilities block from plan.md ──────────────────────────────────
with open(plan_path) as f:
    plan_content = f.read()

# Look for fenced capabilities block
cap_match = re.search(r'```capabilities\s*\n(.*?)```', plan_content, re.DOTALL)
if not cap_match:
    # Also try YAML front-matter style
    fm_match = re.search(r'^---\s*\n(.*?)^---', plan_content, re.DOTALL | re.MULTILINE)
    if fm_match:
        fm_content = fm_match.group(1)
        if 'capabilities:' in fm_content:
            cap_match = type('Match', (), {'group': lambda self, n: fm_content})()

if not cap_match:
    print(json.dumps({
        'error': 'No capabilities block found in plan.md. Expected ```capabilities ... ``` fenced block or YAML front-matter with capabilities key.',
        'help': 'See bin/verify-matrix.sh header or docs/plan-format.md for the required format.'
    }))
    sys.exit(2)

cap_text = cap_match.group(1) if hasattr(cap_match, 'group') else cap_match.group(1)

# ── Parse capabilities ───────────────────────────────────────────────────────
capabilities = []

if HAS_YAML:
    try:
        parsed = yaml.safe_load(cap_text)
        if isinstance(parsed, dict) and 'capabilities' in parsed:
            capabilities = parsed['capabilities']
        elif isinstance(parsed, list):
            capabilities = parsed
    except yaml.YAMLError:
        pass

if not capabilities:
    # Fallback: simple regex-based YAML parser for the subset we need
    current_cap = None
    in_files = False
    in_verifications = False

    for line in cap_text.split('\n'):
        stripped = line.strip()

        if stripped.startswith('- id:'):
            if current_cap:
                capabilities.append(current_cap)
            current_cap = {
                'id': stripped.split(':', 1)[1].strip(),
                'phase': '',
                'description': '',
                'files': [],
                'verifications': []
            }
            in_files = False
            in_verifications = False
        elif current_cap:
            if stripped.startswith('phase:'):
                current_cap['phase'] = stripped.split(':', 1)[1].strip()
                in_files = False
                in_verifications = False
            elif stripped.startswith('description:'):
                current_cap['description'] = stripped.split(':', 1)[1].strip()
                in_files = False
                in_verifications = False
            elif stripped == 'files:':
                in_files = True
                in_verifications = False
            elif stripped == 'verifications:':
                in_verifications = True
                in_files = False
            elif in_files and stripped.startswith('- '):
                current_cap['files'].append(stripped[2:].strip())
            elif in_verifications and stripped.startswith('- type:'):
                current_cap['verifications'].append({
                    'type': stripped.split(':', 1)[1].strip()
                })
            elif stripped.startswith('- id:') or (not stripped.startswith('-') and ':' in stripped):
                in_files = False
                in_verifications = False

    if current_cap:
        capabilities.append(current_cap)

if not capabilities:
    print(json.dumps({
        'error': 'Capabilities block found but could not parse any capabilities from it.',
        'raw_block': cap_text[:500]
    }))
    sys.exit(2)

# ── Build completion matrix ──────────────────────────────────────────────────
matrix = []
missing = []

def file_exists_on_disk(rel_path):
    """Check if a file exists, trying multiple resolution strategies."""
    if os.path.isabs(rel_path) and os.path.exists(rel_path):
        return True
    if project_root:
        candidates = [
            os.path.join(project_root, rel_path),
        ]
        # Add framework-specific resolution paths only if they exist
        for src_dir in ['app/code', 'src', 'lib', 'packages', 'internal', 'pkg']:
            if os.path.isdir(os.path.join(project_root, src_dir)):
                candidates.append(os.path.join(project_root, src_dir, rel_path))
        # Also try the module directory (where plan.md lives)
        plan_dir = os.path.dirname(plan_path)
        candidates.append(os.path.join(plan_dir, rel_path))

        for c in candidates:
            if os.path.exists(c):
                return True
    # Check manifest
    return rel_path in manifest_files

for cap in capabilities:
    cap_id = cap.get('id', 'UNKNOWN')
    phase = cap.get('phase', '?')
    desc = cap.get('description', '')
    cap_files = cap.get('files', [])

    if not cap_files:
        # Capability with no files — flag as needing attention
        entry = {
            'id': cap_id,
            'phase': phase,
            'description': desc,
            'status': 'NO_FILES_DECLARED',
            'files': {}
        }
        matrix.append(entry)
        missing.append(entry)
        continue

    file_statuses = {}
    all_present = True

    for f in cap_files:
        exists = file_exists_on_disk(f)
        file_statuses[f] = 'PRESENT' if exists else 'MISSING'
        if not exists:
            all_present = False

    entry = {
        'id': cap_id,
        'phase': phase,
        'description': desc,
        'status': 'PRESENT' if all_present else 'MISSING',
        'files': file_statuses
    }
    matrix.append(entry)
    if not all_present:
        missing.append(entry)

total = len(matrix)
present = total - len(missing)

output = {
    'plan_path': plan_path,
    'project_root': project_root,
    'matrix': matrix,
    'missing': missing,
    'total': total,
    'present': present,
    'complete': len(missing) == 0
}

print(json.dumps(output, indent=2))

if missing:
    sys.exit(1)
PYTHON_SCRIPT
