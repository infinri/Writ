#!/bin/bash
# Gate status checker — deterministic replacement for AI file-reading.
# Returns structured JSON with boolean status for each gate.
#
# Usage:
#   bin/check-gates.sh --session SID /path/to/project
#   bin/check-gates.sh --session SID          # project root auto-detected from cwd
#   CLAUDE_SESSION_ID=SID bin/check-gates.sh  # identity from the environment instead
#
# Output: { "gates": { "phase-a": true, ... }, "all_passed": false, "missing": ["phase-b", ...] }

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/common.sh"

SID=""
PROJECT_ROOT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --session)
      SID="${2:-}"
      shift
      if [ $# -gt 0 ]; then shift; fi
      ;;
    *)
      if [ -z "$PROJECT_ROOT" ]; then PROJECT_ROOT="$1"; fi
      shift
      ;;
  esac
done

# WHICH SESSION IS ASKING, checked BEFORE the project root and never inferred from it.
#
# Gate approvals are per session, and this script reports whether a gate is approved. With
# no identity it can only answer "somebody in this project approved it", which is how one
# session's approval came to read as every session's in the same repo. Auto-detecting the
# project root from cwd is a convenience about WHERE to look; it says nothing about WHO is
# asking, so it must not double as an identity. That is why this check comes first: the two
# are orthogonal, and ordering them the other way would let `cd project && check-gates.sh`
# answer for a session it never established.
#
# No pointer fallback and no newest-cache guess, matching the resolver in
# writ/session/cache.py: both named whichever session on this machine took a turn most
# recently, and a gate answer for the wrong session is worse than no answer at all.
if [ -z "$SID" ]; then
  SID="${CLAUDE_SESSION_ID:-}"
fi
if [ -z "$SID" ]; then
  echo '{"error": "No session id: pass --session SID or export CLAUDE_SESSION_ID. Gate approvals are per session, so this script will not report gate state for an identity it had to guess."}'
  exit 2
fi

if [ -z "$PROJECT_ROOT" ]; then
  PROJECT_ROOT=$(detect_project_root "$(pwd)")
fi

if [ -z "$PROJECT_ROOT" ]; then
  echo '{"error": "Cannot detect project root. Pass it as argument or run from within a project."}'
  exit 2
fi

# The artifacts live under THIS SESSION's own directory
# (<project_root>/.claude/gates/<session_id>/), so the identity checked above is what
# locates them. writ_gate_dir is the pure-shell mirror of writ/session/locators.gate_dir;
# an empty answer means the id is not a usable path component, and refusing is the same
# loud shape as the no-session refusal above rather than a silent all-false report.
GATE_DIR=$(writ_gate_dir "$PROJECT_ROOT" "$SID")

if [ -z "$GATE_DIR" ]; then
  echo '{"error": "Unusable session id: a session id must match [A-Za-z0-9._-]{1,128} to name a gate directory. This script will not report gate state for an identity it cannot resolve to a path."}'
  exit 2
fi

# The three values reach python through the ENVIRONMENT, not through string interpolation
# into the program text. SID is operator-supplied and PROJECT_ROOT can hold any path
# character, so an embedded quote would previously have ended the literal and turned the
# rest of the value into code (SEC-INJ-CMD-001).
WRIT_GATE_DIR="$GATE_DIR" WRIT_PROJECT_ROOT="$PROJECT_ROOT" WRIT_SESSION="$SID" python3 -c "
import json, os

gate_dir = os.environ['WRIT_GATE_DIR']
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

# The session is echoed back so the answer says WHO it is for, and gate_dir now CONTAINS
# that id: two instances in one repo get different answers from the same project at the
# same moment, which is the whole point of the session-scoped path.
result = {
    'session': os.environ['WRIT_SESSION'],
    'project_root': os.environ['WRIT_PROJECT_ROOT'],
    'gate_dir': gate_dir,
    'gates': gates,
    'all_passed': len(missing) == 0,
    'missing': missing
}

print(json.dumps(result, indent=2))
" 2>/dev/null
