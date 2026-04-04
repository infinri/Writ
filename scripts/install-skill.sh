#!/usr/bin/env bash
# Writ RAG Bridge -- Install script
#
# Patches ~/.claude/settings.json to wire Writ hooks.
# Additive: does not remove existing hooks (Phaselock, etc.).
#
# Usage: bash scripts/install-skill.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SETTINGS_FILE="$HOME/.claude/settings.json"

echo "Writ RAG Bridge installer"
echo "Writ directory: $WRIT_DIR"
echo "Settings file:  $SETTINGS_FILE"
echo ""

# Verify hook files exist
HOOKS=(
    .claude/hooks/writ-rag-inject.sh
    .claude/hooks/writ-context-tracker.sh
    .claude/hooks/check-gate-approval.sh
    .claude/hooks/pre-validate-file.sh
    .claude/hooks/enforce-final-gate.sh
    .claude/hooks/validate-file.sh
    .claude/hooks/validate-handoff.sh
    .claude/hooks/log-session-metrics.sh
)
for hook in "${HOOKS[@]}"; do
    if [ ! -f "$WRIT_DIR/$hook" ]; then
        echo "ERROR: Missing hook: $WRIT_DIR/$hook" >&2
        exit 1
    fi
done

# Verify session helper exists
if [ ! -f "$WRIT_DIR/bin/lib/writ-session.py" ]; then
    echo "ERROR: Missing session helper: $WRIT_DIR/bin/lib/writ-session.py" >&2
    exit 1
fi

# Ensure settings file exists
if [ ! -f "$SETTINGS_FILE" ]; then
    mkdir -p "$(dirname "$SETTINGS_FILE")"
    echo '{}' > "$SETTINGS_FILE"
fi

# Backup current settings
BACKUP="${SETTINGS_FILE}.bak.$(date +%Y%m%d%H%M%S)"
cp "$SETTINGS_FILE" "$BACKUP"
echo "Backed up settings to: $BACKUP"

# Use Python to merge hooks into settings.json (preserves existing config)
python3 << PYEOF
import json
import sys

settings_file = "$SETTINGS_FILE"
writ_dir = "$WRIT_DIR"

with open(settings_file) as f:
    settings = json.load(f)

# Ensure structure exists
settings.setdefault("permissions", {})
settings["permissions"].setdefault("allow", [])
settings["permissions"].setdefault("deny", [])
settings.setdefault("hooks", {})

# --- Permissions ---

new_permissions = [
    f"Bash(bash {writ_dir}/.claude/hooks/writ-rag-inject.sh)",
    f"Bash(bash {writ_dir}/.claude/hooks/writ-context-tracker.sh)",
    f"Bash(bash {writ_dir}/.claude/hooks/check-gate-approval.sh)",
    f"Bash(bash {writ_dir}/.claude/hooks/pre-validate-file.sh)",
    f"Bash(bash {writ_dir}/.claude/hooks/enforce-final-gate.sh)",
    f"Bash(bash {writ_dir}/.claude/hooks/validate-file.sh)",
    f"Bash(bash {writ_dir}/.claude/hooks/validate-handoff.sh)",
    f"Bash(bash {writ_dir}/.claude/hooks/log-session-metrics.sh)",
]

existing = set(settings["permissions"]["allow"])
added_perms = 0
for perm in new_permissions:
    if perm not in existing:
        settings["permissions"]["allow"].append(perm)
        added_perms += 1

# --- Hooks ---

# Helper: register a hook if not already present
def register_hook(event, command, matcher=""):
    event_hooks = settings["hooks"].setdefault(event, [])
    for entry in event_hooks:
        for h in entry.get("hooks", []):
            if h.get("command") == command:
                return True  # already exists
    event_hooks.append({
        "matcher": matcher,
        "hooks": [{"type": "command", "command": command}]
    })
    return False  # newly added

# Hook definitions: (event, script, tool_matcher)
hook_defs = [
    # RAG injection -- every prompt
    ("UserPromptSubmit", "writ-rag-inject.sh", ""),
    # Context tracking -- every response
    ("Stop", "writ-context-tracker.sh", ""),
    # Gate enforcement -- before file writes
    ("PreToolUse", "check-gate-approval.sh", "Write|Edit"),
    # Pre-write static analysis
    ("PreToolUse", "pre-validate-file.sh", "Write|Edit"),
    # Final gate enforcement
    ("PreToolUse", "enforce-final-gate.sh", "Write|Edit"),
    # Post-write static analysis
    ("PostToolUse", "validate-file.sh", "Write|Edit"),
    # Handoff validation
    ("PostToolUse", "validate-handoff.sh", "Write|Edit"),
    # Session metrics -- every response
    ("Stop", "log-session-metrics.sh", ""),
]

results = []
for event, script, matcher in hook_defs:
    cmd = f"bash {writ_dir}/.claude/hooks/{script}"
    existed = register_hook(event, cmd, matcher)
    status = "already registered" if existed else "added"
    results.append(f"  {event} -> {script}: {status}")

# Write updated settings
with open(settings_file, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")

print(f"Added {added_perms} permission(s)")
print("Hooks:")
for line in results:
    print(line)
PYEOF

echo ""
echo "Done. Restart Claude Code for hooks to take effect."
echo ""
echo "Prerequisites:"
echo "  - Writ server must be running: cd $WRIT_DIR && writ serve"
echo "  - Neo4j must be running: docker run -d -p 7687:7687 -e NEO4J_AUTH=neo4j/writdevpass neo4j:5"
