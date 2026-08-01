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
#      skip-if-identical).
#
# Why this exists. The plugin manifest schema has no permissions field,
# hooks/hooks.json only registers hook events, and the plugin lifecycle does not
# touch ~/.claude/CLAUDE.md. Without this patch, users would hit a permission
# prompt for every read-only Writ command and miss the mandatory-workflow
# instructions Writ relies on.
#
# Settings handling. The allow/deny patterns use wildcards (*writ/...) so a
# single entry matches both the plugin (${CLAUDE_PLUGIN_ROOT}/...) and the
# dev/repo run path ($HOME/.claude/skills/writ/...). Existing user entries are
# preserved in their original order; only missing entries are appended.
#
# CLAUDE.md handling. If the existing file matches the template byte-for-byte,
# nothing is written. Otherwise the existing file is backed up to
# CLAUDE.md.bak.<utc-timestamp> and replaced with the template. The template
# contains no env-var references; envsubst is invoked anyway for consistency.
#
# Usage:
#   bash scripts/patch-global-config.sh             # patch
#   bash scripts/patch-global-config.sh --dry-run   # preview, no write
#
# Overrides:
#   WRIT_SETTINGS_TARGET=/path/to/settings.json
#   WRIT_CLAUDE_MD_TARGET=/path/to/CLAUDE.md
#
# Exit codes:
#   0  patched, already up to date, or dry-run success
#   1  missing prerequisite (jq, envsubst) or missing template
#   2  write failure

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TEMPLATES_DIR="$SKILL_DIR/templates"

SETTINGS_TARGET="${WRIT_SETTINGS_TARGET:-$HOME/.claude/settings.json}"
CLAUDE_MD_TARGET="${WRIT_CLAUDE_MD_TARGET:-$HOME/.claude/CLAUDE.md}"
CLAUDE_MD_TEMPLATE="$TEMPLATES_DIR/CLAUDE.md"

# Concrete statusLine command baked at patch time. writ-statusline.sh self-resolves
# its own dir from $0 (no ${CLAUDE_PLUGIN_ROOT} dependency) and degrades cleanly when
# the server is down, so an absolute-path invocation works in any context.
SL_CMD="bash $SKILL_DIR/hooks/scripts/writ-statusline.sh"

SETTINGS_TEMPLATE="$TEMPLATES_DIR/settings.json"

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

# Cross-mode allow rules. Wildcards match both standalone and plugin paths.
ALLOW=(
    "Bash(python3 *writ-session.py *)"
    "Bash(bash *writ/bin/check-gates.sh*)"
    "Bash(bash *writ/bin/verify-files.sh*)"
    "Bash(bash *writ/bin/scan-deps.sh*)"
    "Bash(bash *writ/bin/run-analysis.sh*)"
    "Bash(bash *writ/bin/validate-handoff.sh*)"
    "Bash(*writ/bin/writ query *)"
    "Bash(*writ/bin/writ status*)"
    "Bash(*writ/bin/writ role-prompt *)"
    "Bash(*writ/bin/writ validate*)"
    "Bash(*writ/bin/writ analyze-friction*)"
    "Bash(*writ/bin/writ audit-session*)"
    "Bash(bash *writ/scripts/bootstrap.sh*)"
    "Bash(bash *writ/scripts/bootstrap-plugin.sh*)"
    "Bash(bash *writ/scripts/ensure-server.sh*)"
    "Bash(bash *writ/scripts/install-user-commands.sh*)"
    "Bash(bash *writ/scripts/stop-server.sh*)"
    # Self-edit + self-run entries are DERIVED from the resolved install dir, so a
    # Writ checked out anywhere (/opt/writ, ~/src/writ, a second worktree) gets the
    # right paths. They used to be hardcoded to one developer's home, which meant
    # every other install silently ran without them and prompted on each self-edit.
    "Edit($SKILL_DIR/**)"
    "Bash($SKILL_DIR/*)"
)

# Superseded location-tied entries this script installed in earlier versions.
# Removed on every run so a moved install does not accumulate dead allow rules.
LEGACY_ALLOW=(
    "Bash(*/.claude/skills/writ/*)"
    "Bash(*/analysis/Writ/*)"
)

DENY=(
    "AskUserQuestion"
    # Gate-approval boundary: the agent must never write a .claude/gates/*.approved
    # file directly -- that would self-approve a human-oversight gate (the north star).
    # Scoped to the gates dir so unrelated paths (e.g. a test file named
    # *gates_approved*) are not collaterally blocked.
    "Bash(touch */.claude/gates/*)"
    "Bash(*>.claude/gates/*)"
    "Bash(*/.claude/gates/*approve*)"
)

# Preconditions
if ! command -v jq >/dev/null 2>&1; then
    echo "ERROR: jq is required but not found on PATH." >&2
    echo "Install jq (apt/brew/dnf install jq) and retry." >&2
    exit 1
fi

if ! command -v envsubst >/dev/null 2>&1; then
    echo "ERROR: envsubst is required but not found on PATH." >&2
    echo "Install the gettext package (apt/brew/dnf install gettext) and retry." >&2
    exit 1
fi

if [ ! -f "$CLAUDE_MD_TEMPLATE" ]; then
    echo "ERROR: template missing: $CLAUDE_MD_TEMPLATE" >&2
    exit 1
fi

timestamp() { date -u '+%Y%m%d%H%M%S'; }

# Allow entries that pre-approve edits/runs inside a Writ tree that is NOT this
# install and no longer exists on disk -- the residue of a moved or renamed
# checkout. Printed one per line for the jq merge to subtract.
#
# Two guards keep an unrelated user entry from being pruned: the path basename must
# be writ/Writ, and the directory must be absent. A live second checkout is kept.
stale_self_entries() {
    printf '%s\n' "${LEGACY_ALLOW[@]}"
    [ -f "$SETTINGS_TARGET" ] || return 0
    jq -r '(.permissions.allow // [])[]' "$SETTINGS_TARGET" 2>/dev/null | \
    while IFS= read -r entry; do
        local dir
        dir=$(printf '%s' "$entry" | sed -nE 's#^(Edit|Write|Bash)\((/[^*?]+)/\*\*?\)$#\2#p')
        [ -n "$dir" ] || continue
        [ "$dir" != "$SKILL_DIR" ] || continue
        case "$(basename "$dir")" in writ|Writ) ;; *) continue ;; esac
        [ -d "$dir" ] || printf '%s\n' "$entry"
    done
}

# ─────────────────────────────────────────────────────────────────────────────
# 1. Patch settings.json (permissions)
# ─────────────────────────────────────────────────────────────────────────────
patch_settings() {
    if [ ! -f "$SETTINGS_TARGET" ]; then
        echo "[settings] ERROR: target settings file not found: $SETTINGS_TARGET" >&2
        echo "[settings] Hint: pass WRIT_SETTINGS_TARGET=/path/to/settings.json if it lives elsewhere." >&2
        return 1
    fi

    local tmp
    tmp=$(mktemp)
    local rc=0

    local allow_json deny_json drop_json
    allow_json=$(printf '%s\n' "${ALLOW[@]}" | jq -R . | jq -s .)
    deny_json=$(printf '%s\n' "${DENY[@]}" | jq -R . | jq -s .)
    drop_json=$(stale_self_entries | jq -R . | jq -s .)

    # Inform (do not act) when a non-Writ statusLine is already configured: the
    # merge below leaves it untouched, so point the user at the opt-in command.
    local existing_sl
    existing_sl=$(jq -r '.statusLine.command // ""' "$SETTINGS_TARGET" 2>/dev/null || echo "")
    if [ -n "$existing_sl" ] && ! printf '%s' "$existing_sl" | grep -q 'writ-statusline\.sh'; then
        echo "[settings] An existing (non-Writ) statusLine is configured; leaving it untouched."
        echo "[settings] To use the Writ context meter, set statusLine.command to: $SL_CMD"
    fi

    jq --argjson new_allow "$allow_json" --argjson new_deny "$deny_json" \
       --argjson drop_allow "$drop_json" --arg sl_cmd "$SL_CMD" '
        # Append only entries not already present. existing/incoming are bound
        # to values (not filters) so they survive the map/select context switch
        # where . becomes a single string from incoming.
        def append_new($existing; $incoming):
            $existing + ($incoming | map(select(. as $i | ($existing | index($i)) | not)));
        def drop($existing; $unwanted):
            $existing | map(select(. as $e | ($unwanted | index($e)) | not));
        .permissions = (.permissions // {}) |
        .permissions.allow = append_new(drop(.permissions.allow // []; $drop_allow); $new_allow) |
        .permissions.deny  = append_new(.permissions.deny  // []; $new_deny) |
        # statusLine: add when absent, refresh when it is already a writ-statusline.sh
        # (upgrade-safe), leave a foreign statusLine untouched.
        .statusLine = (
            if (.statusLine == null) then {"type": "command", "command": $sl_cmd}
            elif ((.statusLine.command // "") | test("writ-statusline\\.sh")) then {"type": "command", "command": $sl_cmd}
            else .statusLine end
        )
    ' "$SETTINGS_TARGET" > "$tmp"

    if cmp -s "$SETTINGS_TARGET" "$tmp"; then
        echo "[settings] No changes needed: $SETTINGS_TARGET already contains the Writ permission + statusLine entries."
        rm -f "$tmp"
        return 0
    fi

    if [ "$DRY_RUN" = "1" ]; then
        echo "[settings] [dry-run] would write merged settings to $SETTINGS_TARGET. Diff:"
        diff -u "$SETTINGS_TARGET" "$tmp" || true
        rm -f "$tmp"
        return 0
    fi

    local backup="${SETTINGS_TARGET}.bak.$(timestamp)"
    cp "$SETTINGS_TARGET" "$backup" || { echo "[settings] ERROR: failed to create backup at $backup" >&2; rm -f "$tmp"; return 2; }
    mv "$tmp" "$SETTINGS_TARGET" || { echo "[settings] ERROR: failed to write $SETTINGS_TARGET" >&2; return 2; }
    echo "[settings] Patched $SETTINGS_TARGET"
    echo "[settings] Backup:  $backup"
    return $rc
}

# ─────────────────────────────────────────────────────────────────────────────
# 2. Install/refresh CLAUDE.md
# ─────────────────────────────────────────────────────────────────────────────
patch_claude_md() {
    local rendered
    rendered=$(mktemp)
    # Only $HOME is substituted; the template currently uses no env vars, but
    # mirror the standalone installer so future template edits stay compatible.
    envsubst '$HOME' < "$CLAUDE_MD_TEMPLATE" > "$rendered"

    if [ -f "$CLAUDE_MD_TARGET" ] && cmp -s "$CLAUDE_MD_TARGET" "$rendered"; then
        echo "[CLAUDE.md] No changes needed: $CLAUDE_MD_TARGET already matches the Writ template."
        rm -f "$rendered"
        return 0
    fi

    if [ "$DRY_RUN" = "1" ]; then
        if [ -f "$CLAUDE_MD_TARGET" ]; then
            echo "[CLAUDE.md] [dry-run] would replace $CLAUDE_MD_TARGET. Diff:"
            diff -u "$CLAUDE_MD_TARGET" "$rendered" || true
        else
            echo "[CLAUDE.md] [dry-run] would create $CLAUDE_MD_TARGET from template ($(wc -l < "$rendered") lines)."
        fi
        rm -f "$rendered"
        return 0
    fi

    mkdir -p "$(dirname "$CLAUDE_MD_TARGET")" 2>/dev/null || true

    if [ -f "$CLAUDE_MD_TARGET" ]; then
        local backup="${CLAUDE_MD_TARGET}.bak.$(timestamp)"
        cp "$CLAUDE_MD_TARGET" "$backup" || { echo "[CLAUDE.md] ERROR: failed to create backup at $backup" >&2; rm -f "$rendered"; return 2; }
        mv "$rendered" "$CLAUDE_MD_TARGET" || { echo "[CLAUDE.md] ERROR: failed to write $CLAUDE_MD_TARGET" >&2; return 2; }
        echo "[CLAUDE.md] Replaced $CLAUDE_MD_TARGET"
        echo "[CLAUDE.md] Backup:   $backup"
    else
        mv "$rendered" "$CLAUDE_MD_TARGET" || { echo "[CLAUDE.md] ERROR: failed to write $CLAUDE_MD_TARGET" >&2; return 2; }
        echo "[CLAUDE.md] Created $CLAUDE_MD_TARGET"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# 3. Seed hook registrations (--hooks only)
#
# ONLY for an install nothing auto-discovers. Hooks are already global for a normal
# install: ~/.claude/skills/writ loads as the user-scope plugin writ@skills-dir, so its
# 12 hooks fire in every project with no settings.json entry at all. Writ at any other
# path, without a marketplace install, is discovered by nothing -- hooks/hooks.json is
# never read and NO hooks load, so there is no gate, no rule injection, no enforcement.
#
# REFUSES when a loaded plugin resolves to this install. Two registration surfaces would
# very likely fire all 12 events twice: doubled rule injection, doubled gate evaluation,
# duplicated telemetry, and a single-use gate token one path could consume before the
# other reads it. That double-fire is not empirically proven, so this fails safe --
# refusing costs nothing if Claude Code deduplicates, and prevents a bad failure if not.
# ─────────────────────────────────────────────────────────────────────────────

# Install paths of currently loaded plugins, one per line. WRIT_PLUGIN_LIST_CMD is a test
# injection seam (same pattern as WRIT_HEALTH_CMD / WRIT_SERVE_CMD in writ-server-lib.sh).
loaded_plugin_paths() {
    local raw
    if [ -n "${WRIT_PLUGIN_LIST_CMD:-}" ]; then
        raw=$(eval "$WRIT_PLUGIN_LIST_CMD" 2>/dev/null || echo "[]")
    elif command -v claude >/dev/null 2>&1; then
        raw=$(claude plugin list --json 2>/dev/null || echo "[]")
    else
        raw="[]"
    fi
    printf '%s' "$raw" | jq -r '.[]? | select(.enabled != false) | .installPath // empty' 2>/dev/null || true
}

patch_hooks() {
    if [ ! -f "$SETTINGS_TEMPLATE" ]; then
        echo "[hooks] ERROR: template not found: $SETTINGS_TEMPLATE" >&2
        echo "[hooks] Generate it: python3 scripts/render-settings-template.py" >&2
        return 1
    fi
    if [ ! -f "$SETTINGS_TARGET" ]; then
        echo "[hooks] ERROR: target settings file not found: $SETTINGS_TARGET" >&2
        return 1
    fi

    local loaded
    loaded=$(loaded_plugin_paths)
    if printf '%s\n' "$loaded" | grep -qxF "$SKILL_DIR"; then
        echo "[hooks] REFUSING: this install ($SKILL_DIR) is already loaded as a Claude Code plugin," >&2
        echo "[hooks] so its 12 hooks are registered by the plugin loader and fire in every project." >&2
        echo "[hooks] Seeding settings.json too would register them a SECOND time." >&2
        echo "[hooks] The --hooks step is only for an install that no marketplace or skills dir" >&2
        echo "[hooks] discovers. Nothing was written." >&2
        return 1
    fi

    # $WRIT_DIR is the only variable in the template; render it to this install's path.
    local rendered
    rendered=$(WRIT_DIR="$SKILL_DIR" envsubst '$WRIT_DIR' < "$SETTINGS_TEMPLATE")

    local tmp
    tmp=$(mktemp)
    # Merge per event, appending only commands not already registered, so the step is
    # idempotent and a user's own hook on the same event survives.
    if ! jq -n --argjson cur "$(cat "$SETTINGS_TARGET")" --argjson tpl "$rendered" '
        def merge_event($existing; $incoming):
            ($existing // []) + ($incoming | map(
                . as $group
                | select(
                    [($existing // [])[]?.hooks[]?.command] as $have
                    | ([$group.hooks[]?.command] | all(. as $c | ($have | index($c)) | not))
                  )
            ));
        $cur
        | .hooks = (
            reduce ($tpl.hooks | keys_unsorted[]) as $e
              ((.hooks // {}); .[$e] = merge_event(.[$e]; $tpl.hooks[$e]))
          )
    ' > "$tmp" 2>/dev/null; then
        echo "[hooks] ERROR: failed to merge hook registrations" >&2
        rm -f "$tmp"
        return 2
    fi

    if cmp -s "$tmp" "$SETTINGS_TARGET"; then
        echo "[hooks] Already registered (12 events); no change."
        rm -f "$tmp"
        return 0
    fi
    if [ "$DRY_RUN" = "1" ]; then
        echo "[hooks] [dry-run] would merge 12 hook events into $SETTINGS_TARGET. Diff:"
        diff -u "$SETTINGS_TARGET" "$tmp" || true
        rm -f "$tmp"
        return 0
    fi

    local backup="${SETTINGS_TARGET}.bak.$(timestamp)"
    cp "$SETTINGS_TARGET" "$backup" || { echo "[hooks] ERROR: backup failed" >&2; rm -f "$tmp"; return 2; }
    mv "$tmp" "$SETTINGS_TARGET" || { echo "[hooks] ERROR: write failed" >&2; return 2; }
    echo "[hooks] Registered 12 hook events in $SETTINGS_TARGET"
    echo "[hooks] Backup: $backup"
}

# ─────────────────────────────────────────────────────────────────────────────
# Run the phases. Each is independent; surface a non-zero exit if any fails.
# ─────────────────────────────────────────────────────────────────────────────
overall=0
patch_settings || overall=$?
patch_claude_md || overall=$?
if [ "$SEED_HOOKS" = "1" ]; then
    patch_hooks || overall=$?
fi
exit $overall
