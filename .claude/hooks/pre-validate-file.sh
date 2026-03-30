#!/bin/bash
# Pre-write validation hook — validates content BEFORE the file is written.
# PreToolUse: fires before every Write/Edit/MultiEdit.
# Exit non-zero = BLOCKS the write. Errors are injected back into Claude's context.
# Output: structured JSON per finding.
#
# Creates a temp file with the proposed content, runs analysis, cleans up.

SKILL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
source "$SKILL_DIR/bin/lib/common.sh"

TMPFILE=""
cleanup() { [ -n "$TMPFILE" ] && rm -f "$TMPFILE"; }
trap cleanup EXIT

TMPFILE=$(echo "$CLAUDE_TOOL_INPUT" | python3 -c "
import sys, json, os, tempfile

data = json.load(sys.stdin)
fp = data.get('file_path', '')
if not fp:
    sys.exit(0)

ext = os.path.splitext(fp)[1]

if 'content' in data:
    content = data['content']
elif 'old_string' in data and os.path.exists(fp):
    with open(fp) as f:
        content = f.read()
    content = content.replace(data['old_string'], data.get('new_string', ''), 1)
else:
    sys.exit(0)

tf = tempfile.mktemp(suffix=ext, prefix='claude-preval-', dir='/tmp')
with open(tf, 'w') as f:
    f.write(content)
print(tf)
" 2>/dev/null)

if [ -z "$TMPFILE" ] || [ ! -f "$TMPFILE" ]; then exit 0; fi

FILE=$(echo "$CLAUDE_TOOL_INPUT" | python3 -c \
  "import sys,json; d=json.load(sys.stdin); print(d.get('file_path',''))" 2>/dev/null)

# Check if the file type is one we analyze
lang=$(detect_language "$FILE")
if [ "$lang" = "unknown" ]; then exit 0; fi

PROJECT_ROOT=$(detect_project_root "$FILE")

# Run analysis on the temp file
OUTPUT=$("$SKILL_DIR/bin/run-analysis.sh" --project-root "$PROJECT_ROOT" "$TMPFILE" 2>&1)
EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
  # Rewrite temp file paths back to the real file path in output
  echo "$OUTPUT" | python3 -c "
import json, sys
try:
    findings = json.load(sys.stdin)
    for f in findings:
        if f.get('severity') == 'error':
            msg = f.get('message', '').replace('$TMPFILE', '$FILE')
            print(json.dumps({
                'error': True,
                'rule': f.get('rule', 'ENF-POST-007'),
                'message': f'{f.get(\"tool\", \"unknown\")}: {msg}',
                'file': '$FILE',
                'fix': 'Fix all errors before writing this file'
            }))
except (json.JSONDecodeError, Exception):
    print(json.dumps({
        'error': True,
        'rule': 'ENF-POST-007',
        'message': 'Pre-write validation failed',
        'file': '$FILE',
        'fix': 'Check proposed content for errors'
    }))
" 2>/dev/null
  exit 1
fi

exit 0
