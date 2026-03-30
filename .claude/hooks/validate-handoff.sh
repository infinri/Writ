#!/bin/bash
# Handoff JSON schema validation — PostToolUse
# Fires after any write to .claude/handoffs/*.json
# Validates required keys, non-empty files, no unresolved ENF-POST-006 violations.
# Exit non-zero = blocks the write receipt, Claude must fix before proceeding.

FILE=$(echo "$CLAUDE_TOOL_INPUT" | python3 -c \
  "import sys,json; d=json.load(sys.stdin); print(d.get('file_path',''))" 2>/dev/null)

if [ -z "$FILE" ]; then exit 0; fi

# Only validate handoff JSON files
case "$FILE" in
  */.claude/handoffs/slice-*.json) ;;
  *) exit 0 ;;
esac

if [ ! -f "$FILE" ]; then exit 0; fi

RESULT=$(python3 -c "
import json, sys

with open('$FILE') as f:
    try:
        data = json.load(f)
    except json.JSONDecodeError as e:
        print(json.dumps({
            'error': True,
            'rule': 'ENF-GATE-006',
            'message': f'Handoff file is not valid JSON: {e}',
            'file': '$FILE',
            'fix': 'Write valid JSON with all required keys'
        }))
        sys.exit(1)

errors = []

required_keys = ['slice', 'files', 'interfaces', 'invariants_satisfied', 'plan_deviations', 'open_items']
missing = [k for k in required_keys if k not in data]
if missing:
    errors.append({
        'error': True,
        'rule': 'ENF-GATE-006',
        'message': f'Missing required keys: {missing}',
        'file': '$FILE',
        'fix': f'Add missing keys to handoff JSON: {missing}'
    })

if 'files' in data:
    if not isinstance(data['files'], list) or len(data['files']) == 0:
        errors.append({
            'error': True,
            'rule': 'ENF-GATE-006',
            'message': 'files array is empty or not a list',
            'file': '$FILE',
            'fix': 'files must be a non-empty array of generated file paths'
        })

if 'slice' in data:
    if not isinstance(data['slice'], int) or data['slice'] < 1:
        errors.append({
            'error': True,
            'rule': 'ENF-GATE-006',
            'message': f'slice must be a positive integer, got: {data.get(\"slice\")}',
            'file': '$FILE',
            'fix': 'Set slice to the current slice number (1, 2, 3, ...)'
        })

raw = json.dumps(data)
if 'I cannot verify' in raw:
    errors.append({
        'error': True,
        'rule': 'ENF-POST-006',
        'message': 'Handoff contains unresolved \"I cannot verify\" — all items must be verified or flagged for human review before handoff',
        'file': '$FILE',
        'fix': 'Resolve all \"I cannot verify\" items: verify them, get human confirmation, or remove the unverifiable claim'
    })

if 'open_items' in data and isinstance(data['open_items'], list):
    for i, item in enumerate(data['open_items']):
        if isinstance(item, str) and len(item.strip()) > 0:
            errors.append({
                'error': True,
                'rule': 'ENF-GATE-006',
                'message': f'open_items[{i}] is a bare string without justification',
                'file': '$FILE',
                'fix': 'Convert open_items entries to objects with \"item\" and \"justification\" keys, or resolve them'
            })
        elif isinstance(item, dict) and item.get('item') and not item.get('justification'):
            errors.append({
                'error': True,
                'rule': 'ENF-GATE-006',
                'message': f'open_items[{i}] has no justification key',
                'file': '$FILE',
                'fix': 'Add a \"justification\" key explaining why this item is open'
            })

if errors:
    for e in errors:
        print(json.dumps(e))
    sys.exit(1)

print(json.dumps({
    'error': False,
    'rule': 'ENF-GATE-006',
    'message': f'Handoff slice-{data[\"slice\"]}.json validated: {len(data[\"files\"])} files, {len(data[\"invariants_satisfied\"])} invariants',
    'file': '$FILE'
}))
sys.exit(0)
" 2>&1)

EXIT_CODE=$?

echo "$RESULT"
exit $EXIT_CODE
