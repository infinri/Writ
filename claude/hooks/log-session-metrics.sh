#!/bin/bash
# Context metrics logger — Stop hook
# Fires when Claude finishes a response.
# Checks if a gate approval file was touched in this turn.
# If so, appends context metrics to session-metrics.md automatically (ENF-CTX-004).

SKILL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
source "$SKILL_DIR/bin/lib/common.sh"

PROJECT_ROOT=$(detect_project_root "$(pwd)")
if [ -z "$PROJECT_ROOT" ]; then exit 0; fi

GATE_DIR="$PROJECT_ROOT/.claude/gates"
METRICS_FILE="$PROJECT_ROOT/.claude/session-metrics.md"

if [ ! -d "$GATE_DIR" ]; then exit 0; fi

# Check if any gate file was modified in the last 30 seconds (this turn)
RECENT_GATE=""
for gate_file in "$GATE_DIR"/*.approved; do
    [ ! -f "$gate_file" ] && continue
    if find "$gate_file" -newermt '30 seconds ago' 2>/dev/null | grep -q .; then
        RECENT_GATE=$(basename "$gate_file" .approved)
        break
    fi
done

if [ -z "$RECENT_GATE" ]; then exit 0; fi

TIMESTAMP=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
CONTEXT_PCT="${CLAUDE_CONTEXT_PERCENT:-unknown}"
CONTEXT_TOKENS="${CLAUDE_CONTEXT_TOKENS:-unknown}"

mkdir -p "$(dirname "$METRICS_FILE")"

cat >> "$METRICS_FILE" << EOF

## Gate: ${RECENT_GATE} — ${TIMESTAMP}
Context: ${CONTEXT_PCT}% (${CONTEXT_TOKENS} tokens)
EOF

json_finding "false" "ENF-CTX-004" "Gate ${RECENT_GATE} metrics logged to session-metrics.md" "${METRICS_FILE}" ""
exit 0