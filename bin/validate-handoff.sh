#!/bin/bash
# Handoff JSON validator — standalone version of the hook.
# Can be called on-demand by plan-guardian or any agent.
#
# Usage:
#   bin/validate-handoff.sh /path/to/.claude/handoffs/slice-1.json
#
# Output: { "valid": bool, "errors": [...], "summary": "..." }

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/common.sh"

FILE="${1:-}"

if [ -z "$FILE" ]; then
  echo '{"error": "No handoff file specified. Usage: validate-handoff.sh path/to/slice-N.json"}'
  exit 2
fi

if [ ! -f "$FILE" ]; then
  echo "{\"valid\": false, \"errors\": [{\"rule\": \"ENF-GATE-006\", \"message\": \"File not found: $FILE\"}]}"
  exit 1
fi

python3 -c "
import json, sys, os

filepath = sys.argv[1]

with open(filepath) as f:
    try:
        data = json.load(f)
    except json.JSONDecodeError as e:
        print(json.dumps({
            'valid': False,
            'errors': [{'rule': 'ENF-GATE-006', 'message': f'Invalid JSON: {e}'}],
            'summary': 'Handoff file is not valid JSON'
        }))
        sys.exit(1)

errors = []

required_keys = ['slice', 'files', 'interfaces', 'invariants_satisfied', 'plan_deviations', 'open_items']
missing = [k for k in required_keys if k not in data]
if missing:
    errors.append({'rule': 'ENF-GATE-006', 'message': f'Missing required keys: {missing}'})

if 'files' in data:
    if not isinstance(data['files'], list) or len(data['files']) == 0:
        errors.append({'rule': 'ENF-GATE-006', 'message': 'files must be a non-empty array'})

if 'slice' in data:
    if not isinstance(data['slice'], int) or data['slice'] < 1:
        errors.append({'rule': 'ENF-GATE-006', 'message': f'slice must be a positive integer, got: {data.get(\"slice\")}'})

raw = json.dumps(data)
if 'I cannot verify' in raw:
    errors.append({'rule': 'ENF-POST-006', 'message': 'Contains unresolved \"I cannot verify\" — must be resolved before handoff'})

if 'open_items' in data and isinstance(data['open_items'], list):
    for i, item in enumerate(data['open_items']):
        if isinstance(item, str) and len(item.strip()) > 0:
            errors.append({'rule': 'ENF-GATE-006', 'message': f'open_items[{i}] is a bare string — must be object with item + justification'})
        elif isinstance(item, dict) and item.get('item') and not item.get('justification'):
            errors.append({'rule': 'ENF-GATE-006', 'message': f'open_items[{i}] missing justification key'})

valid = len(errors) == 0
slice_num = data.get('slice', '?')
file_count = len(data.get('files', []))
inv_count = len(data.get('invariants_satisfied', []))

summary = f'slice-{slice_num}.json: {file_count} files, {inv_count} invariants'
if valid:
    summary += ' — VALID'
else:
    summary += f' — {len(errors)} error(s)'

print(json.dumps({
    'valid': valid,
    'errors': errors,
    'summary': summary
}, indent=2))

sys.exit(0 if valid else 1)
" "$FILE"
