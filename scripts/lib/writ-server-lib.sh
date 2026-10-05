#!/usr/bin/env bash
# Shared, singleton-safe Writ server start, plus the Neo4j container start. Sourced by
# scripts/ensure-server.sh, both bootstraps and hooks/scripts/session-start-bootstrap.sh; this file defines functions only, no top-level
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
#
# Goes through writ_http_get (curl-first, urllib fallback) rather than a raw curl on
# purpose: with curl absent this probe was ALWAYS false, which is not daemon-down
# equivalent. It means "daemon down while actually up", so every SessionStart fired a
# doomed second `writ serve` against an already-bound port.
#
# The body is kept in _WRIT_LAST_HEALTH (empty for an injected probe or a failed fetch) so
# the already-running branch below reads cache_dir from it instead of fetching /health
# again. The status is the assignment's, which is writ_http_get's.
writ_server_health() {
    : "${WRIT_HOST:=localhost}" "${WRIT_PORT:=8765}"
    _WRIT_LAST_HEALTH=""
    if [ -n "${WRIT_HEALTH_CMD:-}" ]; then
        ${WRIT_HEALTH_CMD} >/dev/null 2>&1
    else
        _WRIT_LAST_HEALTH=$(WRIT_HTTP_CONNECT_TIMEOUT=0.1 WRIT_HTTP_TIMEOUT=1 \
            writ_http_get "http://${WRIT_HOST}:${WRIT_PORT}/health" 2>/dev/null)
    fi
}

# The password the compose file should interpolate for a container it is about to CREATE: the
# caller's exported WRIT_NEO4J_PASSWORD when set, else the one stored in writ.toml through the
# venv CLI. That CLI prints nothing when only the refused development default is configured, so
# Compose then falls back to it, which is what a brand-new volume needs before set-password.
_writ_compose_password() {
    if [ -n "${WRIT_NEO4J_PASSWORD:-}" ]; then
        printf '%s' "$WRIT_NEO4J_PASSWORD"
        return 0
    fi
    if [ -n "${VENV_DIR:-}" ] && [ -x "$VENV_DIR/bin/writ" ]; then
        "$VENV_DIR/bin/writ" neo4j password 2>/dev/null || true
    fi
}

# Start the production Neo4j container, surfacing docker's error instead of swallowing it.
#
# Container first, compose second. container_name is fixed (writ-neo4j), and until
# docker-compose.yml declared `name: writ` the Compose project came from the install directory
# (1.8.0 gave "180"), so `compose up` from any other version dir failed on the name conflict and
# an `|| true` hid it. `docker start` needs no project and is a no-op on a running container, so a
# container created under any project keeps working. Compose runs only when there is no
# container at all. Returns docker's status; stdout is dropped, stderr passes through.
writ_neo4j_start() {
    if docker container inspect writ-neo4j >/dev/null 2>&1; then
        docker start writ-neo4j >/dev/null
    else
        WRIT_NEO4J_PASSWORD="$(_writ_compose_password)" docker compose -f "$1" up -d neo4j >/dev/null
    fi
}

# Move an install off the published development password, once (program item 3).
#   $1  docker-compose.yml     $2  the venv's writ executable
# Returns 0 when the password was already private (or the operator opted into the default) and
# the container needs nothing, 10 when the container was re-created and is healthy on the stored
# password, 11 when the container belongs to an older Compose project and was left alone
# (re-homing it is a documented manual step), and 1 when the password could not be replaced
# (set-password then guarantees writ.toml is unchanged) or the re-created container never
# reported healthy.
#
# Idempotent and resumable. A private password alone does not mean the job is done: a run that
# changed it and then failed to re-create the container leaves one that still publishes its ports
# on every interface or whose healthcheck holds the old password, and a re-run must finish that.
# So a passing check is followed by a look at the container itself (_writ_neo4j_needs_recreate).
# --if-default makes set-password re-check under its lock, so two bootstraps racing here rotate
# the password once, not twice. The lock wait (180s) exceeds the winner's boot wait (90s) plus
# the change, so the loser waits for the winner instead of timing out.
writ_neo4j_secure() {
    local compose_file="$1" writ_bin="$2"
    if "$writ_bin" neo4j check >/dev/null 2>&1; then
        _writ_neo4j_needs_recreate || return 0
    else
        # stdout dropped: its next steps are what this function does itself. Errors stay on stderr.
        "$writ_bin" neo4j set-password --wait 90 --lock-timeout 180 --if-default >/dev/null || return 1
    fi
    local project
    project="$(docker container inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' writ-neo4j 2>/dev/null || true)"
    if [ "$project" != "writ" ]; then
        echo "[Writ] writ-neo4j was created by an older Writ (Compose project '${project:-none}')." >&2
        echo "[Writ] Its healthcheck password and loopback-only ports change only when it is" >&2
        echo "[Writ] re-created: see docs/install.md, \"Securing an existing install\"." >&2
        return 11
    fi
    WRIT_NEO4J_PASSWORD="$("$writ_bin" neo4j password)" docker compose -f "$compose_file" up -d neo4j >/dev/null || return 1
    # Healthy, not merely listening: the healthcheck authenticates with the password Compose
    # just interpolated, so this also proves the re-created container carries the new one.
    local limit="${WRIT_NEO4J_HEALTHY_WAIT:-120}" waited=0 status=""
    while [ "$waited" -lt "$limit" ]; do
        status="$(docker container inspect -f '{{.State.Health.Status}}' writ-neo4j 2>/dev/null || true)"
        [ "$status" = "healthy" ] && return 10
        sleep 2
        waited=$((waited + 2))
    done
    echo "[Writ] writ-neo4j did not report healthy within ${limit}s (last status: ${status:-unknown}); check: docker logs writ-neo4j" >&2
    return 1
}

# True (0) when writ-neo4j exists but was not re-created after the password change: a published
# port bound anywhere but 127.0.0.1 (the compose file before item 3), or a healthcheck reporting
# unhealthy (it still authenticates with the password the container was created with). No
# container, or no published ports, is nothing to finish.
_writ_neo4j_needs_recreate() {
    local ports
    ports="$(docker port writ-neo4j 2>/dev/null)" || return 1
    if [ -n "$ports" ] && printf '%s\n' "$ports" | grep -qv -- '-> 127\.0\.0\.1:'; then
        return 0
    fi
    [ "$(docker container inspect -f '{{.State.Health.Status}}' writ-neo4j 2>/dev/null || true)" = "unhealthy" ]
}

# True (0) when the daemon must not be launched because Neo4j is configured with the published
# development password, which `writ serve` refuses (exit 78); prints the one-time migration
# notice. Fail-open: no venv CLI, or any outcome other than that exact refusal, returns 1 and
# lets `writ serve` decide, because this saves a doomed start and its 5s wait; it is not the guard.
writ_dev_password_blocks_start() {
    [ -n "${VENV_DIR:-}" ] && [ -x "$VENV_DIR/bin/writ" ] || return 1
    local rc=0
    "$VENV_DIR/bin/writ" neo4j check >/dev/null 2>&1 || rc=$?
    [ "$rc" -eq 78 ] || return 1
    cat >&2 <<MSG
[Writ] Not starting the daemon: Neo4j is configured with the published development password,
[Writ] which Writ now refuses. One-time fix (changes it in Neo4j and saves it to writ.toml):
[Writ]   $VENV_DIR/bin/writ neo4j set-password
[Writ] then follow the steps it prints. A throwaway local instance can opt out instead:
[Writ]   export WRIT_ALLOW_DEV_PASSWORD=1
MSG
    return 0
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
            # Not restarting: systemd owns restarts (tests/test_fix2_cache_alignment.py pins
            # this off by default). But a daemon reading a different session directory serves
            # every gate from the wrong caches, which is exactly what a daemon started before an
            # upgrade does, so say so once, with the command that fixes it. The body is the
            # one writ_server_health just fetched, in this same subshell.
            local running_nr
            running_nr=$(printf '%s' "${_WRIT_LAST_HEALTH:-}" | json_transform '.cache_dir // ""' "d.get('cache_dir') or ''" || true)
            if [ -n "$running_nr" ] && [ -n "${WRIT_CACHE_DIR:-}" ] && [ "$running_nr" != "$WRIT_CACHE_DIR" ]; then
                echo "[Writ] Warning: the daemon on port $WRIT_PORT reads session state from $running_nr, but this install uses $WRIT_CACHE_DIR. Restart it (systemctl --user restart writ-server, or scripts/stop-server.sh then scripts/ensure-server.sh) so gate decisions see this session's mode." >&2
            fi
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

    # Program item 3: an install still on the development password gets the migration notice
    # instead of a daemon launch that would exit 78. Skipped for an injected serve command.
    if [ -z "${WRIT_SERVE_CMD:-}" ] && writ_dev_password_blocks_start; then
        return 0
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
    # The ONE owner of the daemon-log path (logging blueprint, in git history: "collapse
    # WRIT_LOG's two defaults into one router-owned path"). There were three: this
    # library's /tmp default plus two different caller assignments, so which file the
    # daemon's stdout landed in depended on which script happened to start it.
    #
    # Resolution order, explicit beats implicit:
    #   1. $WRIT_LOG                       -- caller said exactly where
    #   2. $WRIT_LOG_ROOT/server.log       -- the same override the Python router honors
    #   3. $CLAUDE_PLUGIN_DATA/server.log  -- plugin install: survives an upgrade that
    #                                         rewrites CLAUDE_PLUGIN_ROOT
    #   4. <state_root>/logs/server.log    (standalone; the root comes from bin/lib/common.sh)
    #
    # Off /tmp deliberately. systemd's tmpfiles.d declares `D /tmp`, which EMPTIES it at
    # boot; that is exactly how the session caches were lost (see the mode-wipe root
    # cause), and a daemon log destroyed on every reboot is the one you want after a
    # reboot-triggered failure. Resolved in bash from the state root common.sh computed
    # rather than by asking writ.shared.logging, because this runs on the per-prompt hook path via
    # writ-rag-inject.sh and a python spawn there costs ~26ms.
    if [ -n "${WRIT_LOG:-}" ]; then
        printf '%s' "$WRIT_LOG"
    elif [ -n "${WRIT_LOG_ROOT:-}" ]; then
        printf '%s/server.log' "$WRIT_LOG_ROOT"
    elif [ -n "${CLAUDE_PLUGIN_ROOT:-}" ]; then
        printf '%s/server.log' "${CLAUDE_PLUGIN_DATA:-$HOME/.cache/writ}"
    else
        printf '%s/logs/server.log' "${_WRIT_STATE_ROOT:-${HOME:-}/.local/state/writ}"
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
