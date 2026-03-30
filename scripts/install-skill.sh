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
for hook in claude/hooks/writ-rag-inject.sh claude/hooks/writ-context-tracker.sh; do
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
    f"Bash(bash {writ_dir}/claude/hooks/writ-rag-inject.sh)",
    f"Bash(bash {writ_dir}/claude/hooks/writ-context-tracker.sh)",
]

existing = set(settings["permissions"]["allow"])
added_perms = 0
for perm in new_permissions:
    if perm not in existing:
        settings["permissions"]["allow"].append(perm)
        added_perms += 1

# --- Hooks ---

# UserPromptSubmit hook
ups_hooks = settings["hooks"].setdefault("UserPromptSubmit", [])
writ_rag_cmd = f"bash {writ_dir}/claude/hooks/writ-rag-inject.sh"

# Check if already registered
rag_exists = False
for entry in ups_hooks:
    for h in entry.get("hooks", []):
        if h.get("command") == writ_rag_cmd:
            rag_exists = True
            break

if not rag_exists:
    ups_hooks.append({
        "matcher": "",
        "hooks": [
            {
                "type": "command",
                "command": writ_rag_cmd,
            }
        ]
    })

# Stop hook
stop_hooks = settings["hooks"].setdefault("Stop", [])
writ_ctx_cmd = f"bash {writ_dir}/claude/hooks/writ-context-tracker.sh"

ctx_exists = False
for entry in stop_hooks:
    for h in entry.get("hooks", []):
        if h.get("command") == writ_ctx_cmd:
            ctx_exists = True
            break

if not ctx_exists:
    stop_hooks.append({
        "matcher": "",
        "hooks": [
            {
                "type": "command",
                "command": writ_ctx_cmd,
            }
        ]
    })

# Write updated settings
with open(settings_file, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")

print(f"Added {added_perms} permission(s)")
print(f"UserPromptSubmit hook: {'already registered' if rag_exists else 'added'}")
print(f"Stop hook: {'already registered' if ctx_exists else 'added'}")
PYEOF

echo ""
echo "Done. Restart Claude Code for hooks to take effect."
echo ""
echo "Prerequisites:"
echo "  - Writ server must be running: cd $WRIT_DIR && writ serve"
echo "  - Neo4j must be running: docker run -d -p 7687:7687 -e NEO4J_AUTH=neo4j/writdevpass neo4j:5"
