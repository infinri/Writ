#!/usr/bin/env bash
# Plugin lifecycle: Shutdown -- stop Writ server gracefully.
# Called automatically when Claude Code unloads the plugin.
# Does NOT stop Neo4j (it may be shared with other tools).

set -euo pipefail

WRIT_HOST="${WRIT_HOST:-localhost}"
WRIT_PORT="${WRIT_PORT:-8765}"

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
