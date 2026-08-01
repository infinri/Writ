#!/usr/bin/env bash
# Plugin lifecycle: Shutdown -- stop Writ server gracefully.
# Called automatically when Claude Code unloads the plugin.
# Does NOT stop Neo4j (it may be shared with other tools).

set -euo pipefail

# Resolve install paths for both install modes. WRIT_DIR/VENV_DIR are exported
# for potential future hooks; stop-server.sh itself only needs WRIT_PORT to
# locate the running daemon.
if [ -n "${CLAUDE_PLUGIN_ROOT:-}" ]; then
    WRIT_DIR="${CLAUDE_PLUGIN_ROOT}"
    VENV_DIR="${CLAUDE_PLUGIN_DATA:-$HOME/.cache/writ}/.venv"
else
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    WRIT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
    VENV_DIR="$WRIT_DIR/.venv"
fi
export WRIT_DIR VENV_DIR

WRIT_HOST="${WRIT_HOST:-localhost}"
WRIT_PORT="${WRIT_PORT:-8765}"

# Under the systemd user service, killing the PID does not stop anything:
# systemd restarts the daemon immediately and the caller walks away believing
# it is down. Stop through systemd instead, preserving the caller's intent.
if command -v systemctl >/dev/null 2>&1 \
   && systemctl --user is-active --quiet writ-server 2>/dev/null; then
    systemctl --user stop writ-server
    echo "[Writ] Server stopped via systemd (writ-server unit). To restart: systemctl --user restart writ-server" >&2
    exit 0
fi

# Find Writ server process by port
WRIT_PID=$(lsof -ti :"$WRIT_PORT" 2>/dev/null | head -1)

if [ -n "$WRIT_PID" ]; then
    kill "$WRIT_PID" 2>/dev/null || true
    # Wait up to 2s for clean shutdown
    for i in $(seq 1 20); do
        if ! kill -0 "$WRIT_PID" 2>/dev/null; then
            echo "[Writ] Server stopped (PID $WRIT_PID)" >&2
            exit 0
        fi
        sleep 0.1
    done
    # Force kill if still running
    kill -9 "$WRIT_PID" 2>/dev/null || true
    echo "[Writ] Server force-stopped (PID $WRIT_PID)" >&2
else
    echo "[Writ] No server running on port $WRIT_PORT" >&2
fi

exit 0
