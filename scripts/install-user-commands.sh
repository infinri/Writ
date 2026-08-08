#!/usr/bin/env bash
# Install Writ slash commands at user level (~/.claude/commands/).
#
# Why: Claude Code discovers slash commands from ~/.claude/commands/
# (user-level) and <project>/.claude/commands/ (project-level). The
# Writ skill's own .claude/commands/ directory is only discovered when
# the active session's cwd is the skill itself. Running this script
# once after install propagates the commands so /writ-approve etc.
# work from any project directory.
#
# Idempotent: safe to re-run; copies overwrite previous installs so a
# changed command propagates on next run.
#
# THIS IS A SHIM over `bin/lib/writ_install.py commands`, which owns the copy so
# one module holds every install-time write (settings.json, CLAUDE.md, hook
# registrations, slash commands). The per-file "installed:" output, the
# USER_COMMANDS_DIR override and the exit codes are unchanged.
#
# Usage:
#   bash scripts/install-user-commands.sh           # default: ~/.claude/commands
#   USER_COMMANDS_DIR=/path bash scripts/install-user-commands.sh
#
# Exit codes:
#   0  installed (or nothing to install)
#   1  templates/commands is missing
#   2  write failure

set -euo pipefail

SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_MODULE="$SKILL_DIR/bin/lib/writ_install.py"
TARGET_DIR="${USER_COMMANDS_DIR:-${HOME}/.claude/commands}"

if [ ! -f "$INSTALL_MODULE" ]; then
    echo "error: install module missing: $INSTALL_MODULE" >&2
    exit 1
fi

exec python3 "$INSTALL_MODULE" commands --target "$TARGET_DIR" --skill-dir "$SKILL_DIR" "$@"
