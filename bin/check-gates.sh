#!/bin/bash
# Gate status checker — deterministic replacement for AI file-reading.
# Returns structured JSON with boolean status for each gate.
#
# Usage:
#   bin/check-gates.sh /path/to/project
#   bin/check-gates.sh              # auto-detect from cwd
#
# Output: { "gates": { "phase-a": true, ... }, "all_passed": false, "missing": ["phase-b", ...] }

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/common.sh"

PROJECT_ROOT="${1:-}"
if [ -z "$PROJECT_ROOT" ]; then
  PROJECT_ROOT=$(detect_project_root "$(pwd)")
fi

if [ -z "$PROJECT_ROOT" ]; then
  echo '{"error": "Cannot detect project root. Pass it as argument or run from within a project."}'
  exit 2
fi

GATE_DIR="$PROJECT_ROOT/.claude/gates"

python3 -c "
import json, os

gate_dir = '$GATE_DIR'
required_gates = [
    'phase-a',
    'phase-b',
    'phase-c',
    'phase-d',
    'test-skeletons',
    'gate-final'
]

gates = {}
missing = []

for gate in required_gates:
    path = os.path.join(gate_dir, gate + '.approved')
    exists = os.path.exists(path)
    gates[gate] = exists
    if not exists:
        missing.append(gate)

result = {
    'project_root': '$PROJECT_ROOT',
    'gate_dir': gate_dir,
    'gates': gates,
    'all_passed': len(missing) == 0,
    'missing': missing
}

print(json.dumps(result, indent=2))
" 2>/dev/null
