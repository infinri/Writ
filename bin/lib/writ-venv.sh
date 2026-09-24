#!/usr/bin/env bash
# Where Writ's venv is, for every bash caller: bin/writ, both bootstraps, ensure-server.sh,
# stop-server.sh, install-server-service.sh and the SessionStart and UserPromptSubmit hooks.
# Functions only, no top-level side effects. writ_resolve_venv forks nothing and runs no external
# command: the UserPromptSubmit hook resolves the venv on every prompt, and the bootstraps source
# this before their preflight, which runs under a stripped PATH.
#
# Resolution order (writ_resolve_venv), explicit beats implicit:
#   1. $WRIT_VENV                    the caller said exactly where
#   2. $CLAUDE_PLUGIN_DATA/.venv     the plugin loader said where (hooks only: Claude Code's
#                                    Bash tool exports neither plugin variable)
#   3. <install>/.venv               if it exists: a clone owns its venv
#   4. <plugin data dir>/.venv       if it exists, for an install at
#                                    <root>/plugins/cache/<marketplace>/<plugin>/<version>: the
#                                    directory the loader would have named
#   5. ~/.cache/writ/.venv           if it exists, for that same plugin-shaped install only: where
#                                    bootstrap-plugin.sh put the venv when CLAUDE_PLUGIN_DATA was
#                                    unset. Never a clone's, or bootstrap.sh would install a clone
#                                    into the plugin's venv.
# With none present VENV_DIR names where the venv belongs (4 for a plugin install, else 3) and
# the function returns 1, so the caller's message points at the right place.

# Sets _WRIT_PLUGIN_DATA to <root>/plugins/data/<id> for an install under
# <root>/plugins/cache/<marketplace>/<plugin>/<version>, where <id> is "<plugin>@<marketplace>"
# with every character outside A-Z a-z 0-9 _ - replaced by "-" (Claude Code's own rule for
# CLAUDE_PLUGIN_DATA: cache/writ/writ/1.8.0 pairs with data/writ-writ). Keyed on the path shape,
# not on $HOME, so a relocated Claude config dir derives its own sibling. Returns 1, with
# _WRIT_PLUGIN_DATA empty, for any other path.
_writ_plugin_data_dir() {
    _WRIT_PLUGIN_DATA=""
    local root="${1%/}" base rest market plugin version id
    case "$root" in
        */plugins/cache/*/*/*) ;;
        *) return 1 ;;
    esac
    base="${root%/plugins/cache/*}"
    rest="${root#"$base"/plugins/cache/}"
    market="${rest%%/*}"
    rest="${rest#*/}"
    plugin="${rest%%/*}"
    version="${rest#*/}"
    case "$version" in
        "" | */*) return 1 ;;
    esac
    [ -n "$market" ] && [ -n "$plugin" ] || return 1
    id="${plugin}@${market}"
    _WRIT_PLUGIN_DATA="$base/plugins/data/${id//[^A-Za-z0-9_-]/-}"
}

writ_resolve_venv() {
    local root="${1%/}"
    if [ -n "${WRIT_VENV:-}" ]; then
        VENV_DIR="${WRIT_VENV%/}"
    elif [ -n "${CLAUDE_PLUGIN_DATA:-}" ]; then
        VENV_DIR="${CLAUDE_PLUGIN_DATA%/}/.venv"
    elif [ -x "$root/.venv/bin/python3" ]; then
        VENV_DIR="$root/.venv"
    elif _writ_plugin_data_dir "$root"; then
        VENV_DIR="$_WRIT_PLUGIN_DATA/.venv"
        # Tilde, not ${HOME}: every hook runs under `set -u`, and common.sh resolves the state
        # root the same way.
        if [ ! -x "$VENV_DIR/bin/python3" ] && [ -x ~/.cache/writ/.venv/bin/python3 ]; then
            VENV_DIR=~/.cache/writ/.venv
        fi
    else
        VENV_DIR="$root/.venv"
    fi
    [ -x "$VENV_DIR/bin/python3" ]
}

# True iff <venv>'s `writ` package is <install>/writ. Leaves what it found in
# _WRIT_VENV_WRIT_FROM (empty when the import failed) for the caller's message. -I because
# `python -c` otherwise puts the cwd first on sys.path, and a session opened inside a Writ
# checkout would import that checkout and report a false match; -I still processes the
# site-packages .pth files, so an editable install's finder is honored. One python start: never
# call this on the per-prompt path.
writ_venv_serves() {
    local want
    want="$(cd "$2" 2>/dev/null && pwd -P)/writ"
    _WRIT_VENV_WRIT_FROM="$("$1/bin/python3" -I -c 'import os, writ; print(os.path.dirname(os.path.realpath(writ.__file__)))' 2>/dev/null)" \
        || _WRIT_VENV_WRIT_FROM=""
    [ -n "$_WRIT_VENV_WRIT_FROM" ] && [ "$_WRIT_VENV_WRIT_FROM" = "$want" ]
}

# SessionStart only. The venv is shared across version dirs, so after an upgrade it still imports
# the previous version (or nothing, once that dir is pruned), and a venv bootstrapped from a
# developer checkout imports the checkout. Either way the daemon would run code this install does
# not ship. Reinstalls the editable package from <install> without touching dependencies (a few
# seconds, offline when setuptools is in the venv). A WRIT_VENV venv belongs to whoever set it,
# so that case only warns. Returns 0 when the venv serves <install> afterwards.
writ_venv_repoint() {
    local venv="$1" root="$2"
    writ_venv_serves "$venv" "$root" && return 0
    if [ -n "${WRIT_VENV:-}" ]; then
        echo "[Writ] Warning: WRIT_VENV=$venv imports writ from ${_WRIT_VENV_WRIT_FROM:-nowhere}, not $root/writ. Repoint it yourself: $venv/bin/python3 -m pip install -e $root" >&2
        return 1
    fi
    echo "[Writ] $venv imports writ from ${_WRIT_VENV_WRIT_FROM:-nowhere}, not this install; reinstalling it from $root" >&2
    # Serialized per venv: two windows opened together after an upgrade would otherwise both run
    # pip into the same site-packages. The loser waits, re-checks, and finds nothing to do. Each
    # pip is time-boxed (WRIT_REPOINT_TIMEOUT, default 120s) because the fallback can fetch a
    # build backend, and an offline machine must not stall SessionStart on it.
    local t="${WRIT_REPOINT_TIMEOUT:-120}"
    if (
        flock -w $((t * 2 + 10)) 9 || exit 1
        writ_venv_serves "$venv" "$root" && exit 0
        timeout "$t" "$venv/bin/python3" -m pip install --quiet --no-deps --no-build-isolation -e "$root" >/dev/null 2>&1 \
            || timeout "$t" "$venv/bin/python3" -m pip install --quiet --no-deps -e "$root" >&2
    ) 9>"$venv/.writ-repoint.lock"; then
        if writ_venv_serves "$venv" "$root"; then
            echo "[Writ] Repointed. A daemon that was already running still serves the old code: restart it (systemctl --user restart writ-server, or scripts/stop-server.sh then scripts/ensure-server.sh). If this release changed dependencies, run: bash $root/scripts/bootstrap-plugin.sh" >&2
            return 0
        fi
    fi
    echo "[Writ] Warning: could not repoint $venv at this install; run: bash $root/scripts/bootstrap-plugin.sh" >&2
    return 1
}
