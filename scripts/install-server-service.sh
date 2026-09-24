#!/usr/bin/env bash
# Install the Writ FastAPI daemon as a systemd USER service so it starts at boot
# (with lingering) and auto-restarts on crash -- so every Claude session finds it
# already up, instead of relying on a SessionStart hook to lazy-start it.
#
# The SessionStart hook (session-start-bootstrap.sh) is KEPT as a portable,
# zero-config fallback: it health-checks first and no-ops when this service is up,
# and it still bootstraps the daemon on machines without systemd / lingering.
#
# Idempotent. Re-run after changing the install path or venv.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Resolve the venv `writ` console script through the shared resolver (absolute path: bare `writ`
# is unsafe after cd-ing into WRIT_DIR, which contains a `writ/` package dir).
# shellcheck source=bin/lib/writ-venv.sh
source "$WRIT_DIR/bin/lib/writ-venv.sh"
if ! writ_resolve_venv "$WRIT_DIR" || [ ! -x "$VENV_DIR/bin/writ" ]; then
    echo "[install] ERROR: no executable venv 'writ' at $VENV_DIR/bin/writ (resolution order: bin/lib/writ-venv.sh). Run the bootstrap first, or set WRIT_VENV." >&2
    exit 1
fi
VENV_WRIT="$VENV_DIR/bin/writ"

WRIT_HOST="${WRIT_HOST:-localhost}"
WRIT_PORT="${WRIT_PORT:-8765}"
NEO4J_PORT="${NEO4J_PORT:-7687}"
# The unit pins the state locations this shell resolves (bin/lib/common.sh), because a systemd
# user manager's environment usually lacks the XDG_STATE_HOME a login shell exports, and a
# daemon resolving a different root than the hooks serves every gate from the wrong caches.
# shellcheck source=bin/lib/common.sh
source "$WRIT_DIR/bin/lib/common.sh"
STATE_CACHE_DIR="$(writ_session_cache_dir)"
STATE_LOG_ROOT="${WRIT_LOG_ROOT:-$_WRIT_STATE_ROOT/logs}"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/writ-server.service"

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

echo "[install] WRIT_DIR=$WRIT_DIR"
echo "[install] writ=$VENV_WRIT  port=$WRIT_PORT"

mkdir -p "$UNIT_DIR"
cat > "$UNIT" <<UNIT_EOF
[Unit]
Description=Writ RAG server (rule retrieval + workflow gates for Claude Code)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$WRIT_DIR
Environment=WRIT_HOST=$WRIT_HOST
Environment=WRIT_PORT=$WRIT_PORT
Environment=WRIT_CACHE_DIR=$STATE_CACHE_DIR
Environment=WRIT_LOG_ROOT=$STATE_LOG_ROOT
# Pinned to the durable state root resolved at install time, never /tmp: systemd EMPTIES /tmp
# at boot (\`D /tmp\` in tmpfiles.d), which once destroyed every session cache. Re-run this
# installer after changing XDG_STATE_HOME.
# Boot ordering: Neo4j runs in docker (docker.service enabled + restart:unless-stopped),
# but may still be coming up. Wait up to ~60s for its bolt port; fail-open after (Restart retries).
ExecStartPre=/bin/bash -c 'for i in \$(seq 1 120); do (echo >/dev/tcp/$WRIT_HOST/$NEO4J_PORT) 2>/dev/null && exit 0; sleep 0.5; done; exit 0'
ExecStart=$VENV_WRIT serve --port $WRIT_PORT --host $WRIT_HOST
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
UNIT_EOF
echo "[install] wrote $UNIT"

# Free the port: stop any daemon started the old (nohup) way so systemd can bind it.
bash "$SCRIPT_DIR/stop-server.sh" >/dev/null 2>&1 || true

systemctl --user daemon-reload
systemctl --user enable --now writ-server.service

# Health probe. Uses the install module's stdlib http-get rather than curl: curl is an
# optional accelerator, and on a machine without it the probe was always false, so a
# perfectly good systemd install reported "/health not responding yet".
ok=0
for _ in $(seq 1 50); do
    if python3 "$WRIT_DIR/bin/lib/writ_install.py" http-get \
        "http://$WRIT_HOST:$WRIT_PORT/health" --fail --timeout 1 >/dev/null 2>&1; then
        ok=1; break
    fi
    sleep 0.2
done
if [ "$ok" = 1 ]; then
    echo "[install] OK: writ-server.service is active and healthy on $WRIT_HOST:$WRIT_PORT"
else
    echo "[install] WARNING: service enabled but /health not responding yet -- check: journalctl --user -u writ-server -n 40" >&2
fi

# --- P2: install + enable the daily log-rotation timer -------------------------
# Bounds the typed audit/friction/metrics streams (rotate -> gzip -> prune ->
# scratch sweep) via `writ logs rotate`. Fail-open: when systemd is unavailable
# this step is SKIPPED, never fatal, so the writ-server.service install above
# still stands (mirrors the lingering guidance below).
ROTATE_SYSTEMD_SRC="$SCRIPT_DIR/systemd"
if command -v systemctl >/dev/null 2>&1; then
    # Subshell + `|| echo`: any failing step (missing loginctl/session bus, a
    # failed sed) aborts the subshell (it inherits set -e) and is caught here,
    # so it never propagates to abort the already-working writ-server install.
    (
        sed "s#__WRIT_BIN__#$VENV_WRIT#g" \
            "$ROTATE_SYSTEMD_SRC/writ-logs-rotate.service" \
            > "$UNIT_DIR/writ-logs-rotate.service"
        cp "$ROTATE_SYSTEMD_SRC/writ-logs-rotate.timer" \
            "$UNIT_DIR/writ-logs-rotate.timer"
        systemctl --user daemon-reload
        systemctl --user enable --now writ-logs-rotate.timer
        echo "[install] OK: writ-logs-rotate.timer enabled (daily log sweep)"
    ) || echo "[install] WARNING: writ-logs-rotate.timer install skipped (a systemd step failed)" >&2
else
    echo "[install] skip: systemctl not found -- writ-logs-rotate.timer not installed (fail-open)"
fi

cat <<MSG

[install] DONE. The service starts at LOGIN now. For BOOT-time start (before/without
[install] an interactive login) and survival across logout, enable lingering once:

    sudo loginctl enable-linger $USER

[install] (lingering needs root/polkit, so run that line yourself.)
[install] Restart after code changes:   systemctl --user restart writ-server
[install] Status / logs:                 systemctl --user status writ-server   |   journalctl --user -u writ-server -f
MSG
