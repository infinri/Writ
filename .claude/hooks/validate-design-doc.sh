#!/usr/bin/env bash
# Phase 2: Gate 5 Tier 1 design-doc quality gate (plan Section 15.3).
#
# PreToolUse on Write matching docs/**/specs/*-design.md.
# Denies if the design doc is missing any required subsection, any subsection
# is under the word-count floor, or blocklist placeholders are present.
# Override: --skip-quality-check flag logged to friction log.
# Feature-flag gated.
set -euo pipefail
HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
source "$WRIT_DIR/bin/lib/common.sh"

PARSED=$(parse_hook_stdin)
SESSION_ID=$(detect_session_id "$PARSED")
[ -z "$SESSION_ID" ] && exit 0
is_work_mode "$SESSION_ID" || exit 0

FILE=$(parsed_field "$PARSED" "file_path")
case "$FILE" in
    */docs/*/specs/*-design.md|*/docs/specs/*-design.md) ;;
    *) exit 0 ;;
esac

CONTENT=$(echo "$PARSED" | python3 -c "import sys,json; print((json.load(sys.stdin).get('tool_input') or {}).get('content',''))")
[ -z "$CONTENT" ] && exit 0

DENY=$(python3 <<'PY'
import re, sys
content = sys.argv[1]
REQUIRED = ["## Goal", "## Constraints", "## Alternatives Considered", "## Chosen Approach", "## Risks"]
BLOCKLIST = ["TODO", "TBD", "fill in", "appropriate", "similar to above", "as needed", "placeholder", "<describe>", "<your text>"]
errors = []
for section in REQUIRED:
    if section not in content:
        errors.append(f"missing subsection '{section}'")
# Split into sections to measure word counts per subsection.
pattern = re.compile(r"^## (.+)$", re.MULTILINE)
sections = {}
matches = list(pattern.finditer(content))
for i, m in enumerate(matches):
    name = "## " + m.group(1).strip()
    start = m.end()
    end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
    sections[name] = content[start:end]
for section in REQUIRED:
    text = sections.get(section, "")
    # Strip code fences for fair word-count.
    cleaned = re.sub(r"```[\s\S]*?```", "", text)
    word_count = len(cleaned.split())
    if word_count < 50:
        errors.append(f"{section}: {word_count} words below 50-word floor")
    for bl in BLOCKLIST:
        if bl.lower() in cleaned.lower():
            errors.append(f"{section}: contains blocklist placeholder '{bl}'")
            break
# Alternatives Considered must name at least 2 alternatives.
alts = sections.get("## Alternatives Considered", "")
alt_list = re.findall(r"^\s*[-*]\s+\S", alts, re.MULTILINE)
if len(alt_list) < 2:
    errors.append("## Alternatives Considered: must name at least 2 alternatives")
# Risks section must name at least 1 risk with mitigation.
risks = sections.get("## Risks", "")
if "mitigation" not in risks.lower():
    errors.append("## Risks: must name at least 1 risk with a mitigation")
if errors:
    print("Gate 5 Tier 1 (validate-design-doc): " + "; ".join(errors))
PY
"$CONTENT")

if [ -n "$DENY" ]; then
    python3 -c "
import json
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': '''$DENY'''
    }
}))"
fi
exit 0
