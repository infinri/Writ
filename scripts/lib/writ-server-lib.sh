#!/usr/bin/env bash
# Shared, singleton-safe Writ server start. Sourced by scripts/ensure-server.sh and
# hooks/scripts/session-start-bootstrap.sh -- this file defines functions only, no top-level
# side effects. writ_ensure_server() guards the check-then-start critical section with flock so
# concurrent SessionStarts (two Claude windows opening together, or both callers firing close in
# time) launch the daemon EXACTLY once. Port-bind remains the final backstop.
#
# Inputs (env, defaulted at call time): WRIT_HOST, WRIT_PORT, WRIT_DIR, VENV_DIR, WRIT_LOG.
# Optional:
#   WRIT_REALIGN_CACHE=1  restart a cache-dir-misaligned daemon (FIX-2); off by default.
#   WRIT_SERVE_CMD        override the serve command (single token or space-separated, no quotes);
#                         default `writ serve --port $WRIT_PORT --host $WRIT_HOST`. Test injection.
#   WRIT_HEALTH_CMD       override the health probe; default curls /health. Test injection.

# writ_session_cache_dir lives in bin/lib/common.sh (the single bash-side definition of
# the session-cache location). Callers that already sourced common.sh keep their copy; the
# rest get it here, so the cache pin below cannot fall back to a stale local default.
# Guarded because this file must stay side-effect-free, and common.sh is functions plus one
# path derivation.
if ! declare -F writ_session_cache_dir >/dev/null 2>&1; then
    _WRIT_LIB_COMMON="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/bin/lib/common.sh"
    # shellcheck source=bin/lib/common.sh
    [ -f "$_WRIT_LIB_COMMON" ] && source "$_WRIT_LIB_COMMON"
fi

# True when the daemon answers /health (or the injected probe succeeds).
writ_server_health() {
    : "${WRIT_HOST:=localhost}" "${WRIT_PORT:=8765}"
    if [ -n "${WRIT_HEALTH_CMD:-}" ]; then
        ${WRIT_HEALTH_CMD} >/dev/null 2>&1
    else
        curl -s --connect-timeout 0.1 "http://${WRIT_HOST}:${WRIT_PORT}/health" >/dev/null 2>&1
    fi
}

# Critical section, run only while holding the flock. Always returns 0 (graceful).
_writ_start_locked() {
    if writ_server_health; then
        if [ "${WRIT_REALIGN_CACHE:-0}" = "1" ]; then
            # "already running" is only good if the daemon's cache dir AND friction-log
            # match ours (FIX-2 + audit #4). A daemon born under a divergent TMPDIR, or
            # carrying a stale WRIT_FRICTION_LOG, silently blackholes its telemetry until
            # restarted. Read both from /health in one probe.
            # Fail-safe: a value we cannot READ (empty), or an expectation we do not hold
            # (env unset), is treated as aligned -- we never restart a healthy daemon on
            # missing evidence.
            local health running_cache running_friction
            health=$(curl -s --connect-timeout 0.3 "http://${WRIT_HOST}:${WRIT_PORT}/health" 2>/dev/null || echo "")
            running_cache=$(printf '%s' "$health" | python3 -c "import sys,json; print(json.load(sys.stdin).get('cache_dir') or '')" 2>/dev/null || echo "")
            running_friction=$(printf '%s' "$health" | python3 -c "import sys,json; print(json.load(sys.stdin).get('friction_log') or '')" 2>/dev/null || echo "")
            local cache_mismatch=0 friction_mismatch=0
            if [ -n "$running_cache" ] && [ -n "${WRIT_CACHE_DIR:-}" ] && [ "$running_cache" != "$WRIT_CACHE_DIR" ]; then
                cache_mismatch=1
            fi
            if [ -n "$running_friction" ] && [ -n "${WRIT_FRICTION_LOG:-}" ] && [ "$running_friction" != "$WRIT_FRICTION_LOG" ]; then
                friction_mismatch=1
            fi
            if [ "$cache_mismatch" = 0 ] && [ "$friction_mismatch" = 0 ]; then
                echo "[Writ] Server already running on port $WRIT_PORT (cache_dir=${running_cache:-unknown})" >&2
                return 0
            fi
            echo "[Writ] Server on $WRIT_PORT misaligned (cache_dir=$running_cache vs ${WRIT_CACHE_DIR:-unset}; friction_log=$running_friction vs ${WRIT_FRICTION_LOG:-unset}); restarting to realign" >&2
            WRIT_PORT="$WRIT_PORT" WRIT_HOST="$WRIT_HOST" bash "${WRIT_DIR}/scripts/stop-server.sh" >/dev/null 2>&1 || true
            # fall through to start a correctly-pinned daemon
        else
            echo "[Writ] Server already running on port $WRIT_PORT" >&2
            return 0
        fi
    fi

    if [ -n "${VENV_DIR:-}" ] && [ -f "$VENV_DIR/bin/activate" ]; then
        # shellcheck disable=SC1091
        . "$VENV_DIR/bin/activate" 2>/dev/null || true
    fi

    # cd into the install dir so `writ serve` reads writ.toml from there, not the user's cwd.
    # Safe: this runs inside the flock subshell, so it never changes the caller's cwd.
    if [ -n "${WRIT_DIR:-}" ] && [ -d "$WRIT_DIR" ]; then
        cd "$WRIT_DIR" 2>/dev/null || true
    fi

    # Launch via the venv's ABSOLUTE console script. Bare `writ` is unsafe here: we
    # just cd'd into WRIT_DIR (which contains a `writ/` package directory) and PATH can
    # carry an empty component (= cwd) ahead of bin/, so `writ` may resolve to the
    # directory -> `nohup: failed to run command 'writ': Permission denied`. The absolute
    # path removes that ambiguity regardless of cwd / PATH / whether activation won.
    local serve_cmd="${WRIT_SERVE_CMD:-}"
    if [ -z "$serve_cmd" ]; then
        if [ -n "${VENV_DIR:-}" ] && [ -x "$VENV_DIR/bin/writ" ]; then
            serve_cmd="$VENV_DIR/bin/writ serve --port $WRIT_PORT --host $WRIT_HOST"
        else
            serve_cmd="writ serve --port $WRIT_PORT --host $WRIT_HOST"
        fi
    fi
    # 9>&- closes the inherited flock fd in the daemon child. Without it the long-lived daemon
    # would hold the lock for its entire life, so every later writ_ensure_server would block on
    # flock for the full timeout (and a realign could never re-acquire the lock to restart).
    nohup $serve_cmd > "$WRIT_LOG" 2>&1 9>&- &
    local pid=$!

    # Wait up to 5s for startup (Writ cold start is ~0.6s at 80 rules).
    local i
    for i in $(seq 1 50); do
        if writ_server_health; then
            echo "[Writ] Server started (PID $pid, log: $WRIT_LOG)" >&2
            return 0
        fi
        sleep 0.1
    done
    echo "[Writ] Warning: server did not respond within 5s (PID $pid, check $WRIT_LOG)" >&2
    return 0
}

# Idempotent, singleton-safe entry point. Always returns 0 so a caller's `set -e` never trips
# and hooks degrade gracefully (server unavailable) on any failure.
writ_default_server_log() {
    # The ONE owner of the daemon-log path (LOGGING-BLUEPRINT section 6: "collapse
    # WRIT_LOG's two defaults into one router-owned path"). There were three: this
    # library's /tmp default plus two different caller assignments, so which file the
    # daemon's stdout landed in depended on which script happened to start it.
    #
    # Resolution order, explicit beats implicit:
    #   1. $WRIT_LOG                       -- caller said exactly where
    #   2. $WRIT_LOG_ROOT/server.log       -- the same override the Python router honors
    #   3. $CLAUDE_PLUGIN_DATA/server.log  -- plugin install: survives an upgrade that
    #                                         rewrites CLAUDE_PLUGIN_ROOT
    #   4. <skill>/var/logs/server.log     -- standalone, co-located with the install
    #
    # Off /tmp deliberately. systemd's tmpfiles.d declares `D /tmp`, which EMPTIES it at
    # boot; that is exactly how the session caches were lost (see the mode-wipe root
    # cause), and a daemon log destroyed on every reboot is the one you want after a
    # reboot-triggered failure. Resolved in bash from WRIT_DIR rather than by asking
    # writ.shared.logging, because this runs on the per-prompt hook path via
    # writ-rag-inject.sh and a python spawn there costs ~26ms.
    if [ -n "${WRIT_LOG:-}" ]; then
        printf '%s' "$WRIT_LOG"
    elif [ -n "${WRIT_LOG_ROOT:-}" ]; then
        printf '%s/server.log' "$WRIT_LOG_ROOT"
    elif [ -n "${CLAUDE_PLUGIN_ROOT:-}" ]; then
        printf '%s/server.log' "${CLAUDE_PLUGIN_DATA:-$HOME/.cache/writ}"
    else
        printf '%s/var/logs/server.log' "${WRIT_DIR:-$HOME/.claude/skills/writ}"
    fi
}

writ_ensure_server() {
    : "${WRIT_HOST:=localhost}" "${WRIT_PORT:=8765}"
    WRIT_LOG="$(writ_default_server_log)"
    # The redirect below creates the FILE but not its directory, so an unwritable parent
    # would fail the launch outright rather than degrade. A fresh install has no var/logs
    # until something writes there, and $CLAUDE_PLUGIN_DATA may not exist before bootstrap.
    mkdir -p "$(dirname "$WRIT_LOG")" 2>/dev/null || true
    # FIX-2: pin the daemon's session-cache dir deterministically (not ambient TMPDIR), so every
    # start path agrees and /health can report it. Exported here so `writ serve` inherits it.
    # The value comes from the shared resolver: this line used to default to gettempdir(),
    # which after 152e722 pinned the daemon to a directory holding no session caches at all,
    # so it served mode=None for every session and re-created the boot-wipe.
    export WRIT_CACHE_DIR="$(writ_session_cache_dir)"

    local lock="/tmp/writ-server-${WRIT_PORT}.lock"
    (
        if ! flock -w 15 9; then
            # Could not acquire in 15s: another starter is bringing the server up. Best-effort
            # health check, then yield -- do not start a competing daemon.
            writ_server_health || true
            exit 0
        fi
        _writ_start_locked
    ) 9>"$lock" || true
    return 0
}
