#!/usr/bin/env bash
# Writ global-config patcher -- THE post-install step for ~/.claude/
#
# Hooks are owned by the plugin (hooks/hooks.json, the single source of truth);
# the standalone settings.json hook seeder was sunset. This script delivers the
# two things a plugin manifest cannot ship, plus the global instructions:
#   1. Merges the Writ-specific cross-mode allow/deny entries into
#      ~/.claude/settings.json (idempotent, ordering preserved). Does NOT touch
#      the hooks block -- the plugin registers hooks.
#   2. Merges the Writ statusLine into ~/.claude/settings.json. A plugin cannot
#      ship the main statusLine (the manifest has no field; a plugin settings.json
#      honors only agent/subagentStatusLine; no hook can set it), so this patch is
#      the delivery path. Policy: add when absent, refresh when it already points
#      at a writ-statusline.sh (survives plugin-upgrade path changes), and leave a
#      foreign statusLine untouched (never clobber the user's choice).
#   3. Renders templates/CLAUDE.md into ~/.claude/CLAUDE.md (backup-if-exists,
#      skip-if-identical). A missing settings.json is CREATED, not an error.
#
# Why this exists. The plugin manifest schema has no permissions field,
# hooks/hooks.json only registers hook events, and the plugin lifecycle does not
# touch ~/.claude/CLAUDE.md. Without this patch, users would hit a permission
# prompt for every read-only Writ command and miss the mandatory-workflow
# instructions Writ relies on.
#
# THIS IS A SHIM. The merges themselves live in bin/lib/writ_install.py (stdlib
# only, runs under bare system python3). They used to be jq programs plus two
# gettext variable-substitution calls in this file, which made jq and gettext install
# prerequisites for nothing but string substitution and a JSON merge. The behavior is ported
# one-for-one; see that module's docstring for what is preserved and the single
# deliberate change (create-if-absent). This file keeps its flags, its overrides
# and its exit codes, because docs, bootstrap.sh and the test suite all name it.
#
# Usage:
#   bash scripts/patch-global-config.sh             # patch
#   bash scripts/patch-global-config.sh --dry-run   # preview, no write
#   bash scripts/patch-global-config.sh --hooks     # also seed hook registrations
#
# Overrides:
#   WRIT_SETTINGS_TARGET=/path/to/settings.json
#   WRIT_CLAUDE_MD_TARGET=/path/to/CLAUDE.md
#
# Exit codes:
#   0  patched, already up to date, or dry-run success
#   1  missing template, or the plugin-install refusal (--hooks)
#   2  write failure

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
INSTALL_MODULE="$SKILL_DIR/bin/lib/writ_install.py"

SETTINGS_TARGET="${WRIT_SETTINGS_TARGET:-$HOME/.claude/settings.json}"
CLAUDE_MD_TARGET="${WRIT_CLAUDE_MD_TARGET:-$HOME/.claude/CLAUDE.md}"

# Flags are scanned across all positions: this used to inspect only $1, so any other
# argument was silently ignored -- which is why --hooks had to be added here rather than
# appearing to work while doing nothing.
DRY_RUN=0
SEED_HOOKS=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --hooks)   SEED_HOOKS=1 ;;
        *) echo "[patch] Unknown argument: $arg (accepted: --dry-run, --hooks)" >&2; exit 1 ;;
    esac
done

if [ ! -f "$INSTALL_MODULE" ]; then
    echo "[patch] ERROR: install module missing: $INSTALL_MODULE" >&2
    echo "[patch] This script is a shim over bin/lib/writ_install.py; the install tree is incomplete." >&2
    exit 1
fi

# Single token, no spaces, so an unquoted expansion is safe and an empty value adds
# no argument at all.
DRY_FLAG=""
[ "$DRY_RUN" = "1" ] && DRY_FLAG="--dry-run"

# Each phase is independent; surface a non-zero exit if any fails.
overall=0
# shellcheck disable=SC2086  # DRY_FLAG is a single bare token by construction
python3 "$INSTALL_MODULE" settings \
    --target "$SETTINGS_TARGET" --skill-dir "$SKILL_DIR" $DRY_FLAG || overall=$?
# shellcheck disable=SC2086
python3 "$INSTALL_MODULE" claude-md \
    --target "$CLAUDE_MD_TARGET" --skill-dir "$SKILL_DIR" $DRY_FLAG || overall=$?
if [ "$SEED_HOOKS" = "1" ]; then
    # shellcheck disable=SC2086
    python3 "$INSTALL_MODULE" hooks \
        --target "$SETTINGS_TARGET" --skill-dir "$SKILL_DIR" $DRY_FLAG || overall=$?
fi
exit $overall
