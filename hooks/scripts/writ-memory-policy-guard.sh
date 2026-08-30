#!/usr/bin/env bash
# Phase 4b: intercept rule-weakening memory writes.
#
# Finding from PSR-003 (docs/pressure-runs/2026-04-22/PSR-003/analysis.md):
# the model accepts durable rule-weakening framed as "going forward when X,
# do Y" and silently persists it to auto-memory. This hook pattern-matches
# the file content of Write tool calls targeting `~/.claude/projects/*/memory/**`
# and denies with a directive when rule-weakening phrases are detected
# without an explicit override marker.
#
# Runs in ALL modes: rule-weakening memories are mode-independent.
# Hook type: PreToolUse on Write
# Exit 0 + permissionDecision JSON on stdout to deny.

set -euo pipefail

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
FA="$WRIT_DIR/bin/lib/friction-append.py"
source "$WRIT_DIR/bin/lib/common.sh"

load_hook_env

TOOL="$HOOK_TOOL_NAME"
if [ "$TOOL" != "Write" ]; then
    exit 0
fi

# File path of the pending write.
FILE_PATH="$HOOK_FILE_PATH"
if [ -z "$FILE_PATH" ]; then
    exit 0
fi

# Only watch ~/.claude/projects/*/memory/ paths (any project's auto-memory).
case "$FILE_PATH" in
    */.claude/projects/*/memory/*) ;;
    *) exit 0 ;;
esac

# Content of the pending write.
CONTENT=$(echo "$HOOK_ENVELOPE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('content',''))" 2>/dev/null)
if [ -z "$CONTENT" ]; then
    # Empty-content writes pass through (unusual but not the failure mode).
    exit 0
fi

# Detect override marker. If present, allow through with no check.
# Two accepted forms:
#   YAML: explicit_rule_override: true
#   Body: override authorized by: <name>
OVERRIDE_MATCHED=$(python3 <<PY
import sys, re
content = $(python3 -c "import sys,json; print(json.dumps(sys.argv[1]))" "$CONTENT" 2>/dev/null)
if re.search(r'explicit_rule_override\s*:\s*true', content, re.IGNORECASE):
    print('yes')
elif re.search(r'override\s+authorized\s+by\s*:', content, re.IGNORECASE):
    print('yes')
else:
    print('no')
PY
) || OVERRIDE_MATCHED="no"

if [ "$OVERRIDE_MATCHED" = "yes" ]; then
    exit 0
fi

# Pattern match rule-weakening phrases. Checks the content (case-insensitive).
# If ANY pattern matches, deny. Patterns are tuned to PSR-003 phrasing plus
# reasonable variants; false-positives can be escaped via the override marker.
MATCHED=$(python3 <<PY
import sys, re
content = $(python3 -c "import sys,json; print(json.dumps(sys.argv[1]))" "$CONTENT" 2>/dev/null)
patterns = [
    # Skip / no verification variants
    r'\bskip\s+(?:the\s+)?(?:verification|verify|test\s+run|tests?|check|checks|validation|validate)\b',
    r'\bno\s+(?:verification|verify|re-?run|re-?runs?|fresh\s+verification)\b',
    r'\bnever\s+(?:re-?run|verify|test)\b',
    r'\bdon\'?t\s+(?:re-?run|verify|re-?verify)\b',
    # Face-value / trust-as-bypass
    r'take\s+(?:the\s+)?[\w\s\-]{0,40}?(?:report|claim|output|result|answer)\s+at\s+face\s+value',
    r'\btrust\s+[\w\s\-]{0,20}?(?:source|sub-?agent|implementer|worker|report)\s*=\s*(?:no|skip|never|face)',
    # Rule-override / bypass language outside an authorized marker
    r'\b(?:override|bypass|weaken|suspend|disable)\s+[\w\s\-]{0,20}?(?:ENF-|rule|verify|discipline|verification)',
    # PSR-003 exact phrasing
    r'["\']?i\s+trust\s+you["\']?[^\n]{0,120}(?:skip|no|never|face\s+value|move\s+on)',
    r'take\s+[\w\s\-]{0,40}?\s+at\s+face\s+value\s+and\s+move\s+on',
]
matched = []
for p in patterns:
    m = re.search(p, content, re.IGNORECASE)
    if m:
        matched.append(m.group(0)[:80])
if matched:
    import json as _j
    print(_j.dumps(matched))
else:
    print('')
PY
) || MATCHED=""

if [ -z "$MATCHED" ]; then
    exit 0
fi

# Friction log: memory_policy_deny event. Hardening (PSR-003c follow-up):
# - Pipe matched-JSON through stdin instead of heredoc interpolation
#   (single quotes, triple quotes, backslashes in regex matches no longer
#   break Python parsing).
# - Path resolution (env var / project root) is owned by friction-append.py.
SESSION_ID="${HOOK_SESSION_ID:-unknown}"
SESSION_ID="$SESSION_ID" \
    FILE_PATH="$FILE_PATH" \
    MATCHED_RAW="$MATCHED" \
    python3 <<'PY' 2>/dev/null | python3 "$FA" --stdin-json 2>/dev/null || true
import json, os
matched_raw = os.environ.get("MATCHED_RAW", "").strip()
try:
    matched = json.loads(matched_raw) if matched_raw else []
except json.JSONDecodeError:
    matched = [matched_raw[:80]]
from datetime import datetime, timezone
entry = {
    "ts": datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    "session": os.environ.get("SESSION_ID", "unknown"),
    "event": "memory_policy_deny",
    "file_path": os.environ.get("FILE_PATH", ""),
    "matched_patterns": matched,
}
print(json.dumps(entry))
PY

# Emit deny directive. The assistant should either (a) revise the memory
# to not encode a rule bypass, or (b) add an explicit override marker
# with authorization.
DENY_REPLY=$(python3 <<'PY'
import json
reason = (
    "[Writ: memory rule-weakening blocked] This memory write would persist "
    "a rule-bypass policy across sessions. Detected patterns suggest the "
    "memory codifies skipping verification/tests/checks (ENF-PROC-VERIFY-001 "
    "or similar). Do NOT persist rule bypasses via memory."
    "\n\n"
    "Legitimate paths:\n"
    "  (a) Rewrite the memory to NOT encode a rule bypass. Narrow exceptions "
    "belong in the rule itself, not in memory.\n"
    "  (b) If this is a deliberate narrow override, include an explicit "
    "marker: YAML 'explicit_rule_override: true' or body line "
    "'override authorized by: <name> (<date>)' — and scope it narrowly "
    "(e.g., 'for test suite X only, per incident Y'), never globally on a "
    "trigger phrase.\n\n"
    "The appropriate response is to decline the user's policy framing and "
    "offer per-session override instead of persistent memory."
)
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }
}))
PY
) || DENY_REPLY=""
emit_hook_reply "$DENY_REPLY"
exit 0
