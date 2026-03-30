#!/bin/bash
# Universal validation hook — routes by file extension.
# PostToolUse: fires after every Write/Edit/MultiEdit.
# Exit non-zero = Claude Code injects error into context. Must fix before continuing.
# Output: structured JSON per finding.
#
# Delegates to bin/run-analysis.sh for the actual analysis.
# This hook extracts the file path from tool input and calls the shared script.

SKILL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
source "$SKILL_DIR/bin/lib/common.sh"

FILE=$(echo "$CLAUDE_TOOL_INPUT" | python3 -c \
  "import sys,json; d=json.load(sys.stdin); print(d.get('file_path',''))" 2>/dev/null)

if [ -z "$FILE" ]; then exit 0; fi
if [ ! -f "$FILE" ]; then exit 0; fi

# Check if the file type is one we analyze
lang=$(detect_language "$FILE")
if [ "$lang" = "unknown" ]; then exit 0; fi

PROJECT_ROOT=$(detect_project_root "$FILE")

# Run analysis via the shared script
OUTPUT=$("$SKILL_DIR/bin/run-analysis.sh" --project-root "$PROJECT_ROOT" "$FILE" 2>&1)
EXIT_CODE=$?

# Track file write for coverage analysis
SESSION_ID=$(ps -o ppid= -p $PPID 2>/dev/null | tr -d ' ')
if [ -z "$SESSION_ID" ]; then
    SESSION_ID=$(echo "${PWD}:${USER}" | md5sum | cut -c1-12)-$(date +%Y%m%d)
fi
ANALYSIS_RESULT="pass"
if [ $EXIT_CODE -ne 0 ]; then
    ANALYSIS_RESULT="fail"
fi
python3 "$SKILL_DIR/bin/lib/writ-session.py" update "$SESSION_ID" \
    --add-file "$FILE" \
    --add-file-result "$FILE" "$ANALYSIS_RESULT" 2>/dev/null || true

if [ $EXIT_CODE -ne 0 ]; then
  # Convert the structured JSON array into the hook's expected format
  # (one JSON object per line for Claude Code to consume)
  echo "$OUTPUT" | python3 -c "
import json, sys
try:
    findings = json.load(sys.stdin)
    for f in findings:
        if f.get('severity') == 'error':
            print(json.dumps({
                'error': True,
                'rule': f.get('rule', 'ENF-POST-007'),
                'message': f'{f.get(\"tool\", \"unknown\")}: {f.get(\"message\", \"\")}',
                'file': f.get('file', ''),
                'fix': 'Fix all static analysis errors'
            }))
        elif f.get('severity') == 'warning':
            print(json.dumps({
                'error': False,
                'rule': f.get('rule', 'ENF-POST-007'),
                'message': f'{f.get(\"tool\", \"unknown\")}: {f.get(\"message\", \"\")}',
                'file': f.get('file', ''),
                'fix': f.get('message', '')
            }))
except (json.JSONDecodeError, Exception) as e:
    # Fallback: pass through raw output
    print(json.dumps({
        'error': True,
        'rule': 'ENF-POST-007',
        'message': 'Static analysis failed (could not parse output)',
        'file': '$FILE',
        'fix': 'Check bin/run-analysis.sh output manually'
    }))
" 2>/dev/null
  exit 1
fi

exit 0
