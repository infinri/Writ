#!/bin/bash
# Pre-write validation hook — validates content BEFORE the file is written.
# PreToolUse: fires before every Write/Edit/MultiEdit.
# Exit non-zero = BLOCKS the write. Errors are injected back into Claude's context.
# Output: structured JSON per finding.
#
# Creates a temp file with the proposed content, runs analysis, cleans up.

SKILL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
source "$SKILL_DIR/bin/lib/common.sh"
hook_instrument "pre-validate-file"

# Parse the Claude Code hook stdin envelope (one python3 spawn)
load_hook_env
FILE="$HOOK_FILE_PATH"

if [ -z "$FILE" ]; then exit 0; fi

# Seed scripts under scripts/seed_*.py contain rule definitions whose
# violation/pass_example fields carry literal bad-code samples from the
# public rulebook. The security analyzers would flag every example as a
# violation. Skip pre-validation for these documentation-bearing scripts.
case "$FILE" in
  */scripts/seed_*.py|scripts/seed_*.py) exit 0 ;;
esac

TMPFILE=""
cleanup() { [ -n "$TMPFILE" ] && rm -f "$TMPFILE"; }
trap cleanup EXIT

TMPFILE=$(echo "$HOOK_ENVELOPE" | python3 -c "
import sys, json, os, tempfile

data = json.load(sys.stdin)
fp = data.get('file_path', '')
if not fp:
    sys.exit(0)

ext = os.path.splitext(fp)[1]
ti = data.get('tool_input', {})

if data.get('content'):
    content = data['content']
elif ti.get('old_string') and os.path.exists(fp):
    with open(fp) as f:
        content = f.read()
    content = content.replace(ti['old_string'], ti.get('new_string', ''), 1)
else:
    sys.exit(0)

tf = tempfile.mktemp(suffix=ext, prefix='claude-preval-', dir='/tmp')
with open(tf, 'w') as f:
    f.write(content)
print(tf)
" 2>/dev/null)

if [ -z "$TMPFILE" ] || [ ! -f "$TMPFILE" ]; then exit 0; fi

# Check if the file type is one we analyze
lang=$(detect_language "$FILE")
if [ "$lang" = "unknown" ]; then exit 0; fi

PROJECT_ROOT=$(detect_project_root "$FILE")

# Run analysis on the temp file
OUTPUT=$("$SKILL_DIR/bin/run-analysis.sh" --project-root "$PROJECT_ROOT" "$TMPFILE" 2>&1)
EXIT_CODE=$?

# Pre-existing violations in untouched code must not block an unrelated edit to
# the same file. Re-analyze the file as it stands and keep only what this edit
# ADDS. See bin/lib/filter-new-findings.py for the fingerprinting rationale.
if [ $EXIT_CODE -ne 0 ] && [ -f "$FILE" ]; then
  BASE_OUT=$("$SKILL_DIR/bin/run-analysis.sh" --project-root "$PROJECT_ROOT" "$FILE" 2>/dev/null || true)
  printf '%s' "$OUTPUT"   > "$TMPFILE.new.json"
  printf '%s' "$BASE_OUT" > "$TMPFILE.base.json"
  OUTPUT=$(FILE_NEW="$TMPFILE" FILE_OLD="$FILE" python3 \
    "$SKILL_DIR/bin/lib/filter-new-findings.py" "$TMPFILE.new.json" "$TMPFILE.base.json")
  rm -f "$TMPFILE.new.json" "$TMPFILE.base.json"
  echo "$OUTPUT" | grep -q '"severity": "error"' || EXIT_CODE=0
fi

if [ $EXIT_CODE -ne 0 ]; then
  # Build denial reason from analysis output
  REASON=$(echo "$OUTPUT" | python3 -c "
import json, sys
try:
    findings = json.load(sys.stdin)
    errors = []
    for f in findings:
        if f.get('severity') == 'error':
            tool = f.get('tool', 'unknown')
            msg = f.get('message', '').replace('$TMPFILE', '$FILE')
            errors.append(f'{tool}: {msg}')
    if errors:
        print('[ENF-POST-007] Pre-write validation errors in $FILE: ' + '; '.join(errors[:5]))
    else:
        print('[ENF-POST-007] Pre-write validation failed for $FILE')
except Exception:
    print('[ENF-POST-007] Pre-write validation failed for $FILE')
" 2>/dev/null)
  python3 -c "
import json, sys
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': sys.argv[1]
    }
}))
" "${REASON:-Pre-write validation failed}"
  log_gate_decision "pre-write-validation" "deny" "${REASON:-Pre-write validation failed}" "${FILE_PATH:-}"
  exit 0
fi

log_gate_decision "pre-write-validation" "allow" "pre-write validation passed" "${FILE_PATH:-}"
exit 0
