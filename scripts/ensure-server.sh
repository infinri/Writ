#!/usr/bin/env bash
# Plugin lifecycle: Init -- ensure Writ server and Neo4j are running.
# Called automatically when Claude Code loads the plugin.
# Non-fatal: if anything fails, hooks fall back gracefully (server unavailable).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

WRIT_HOST="${WRIT_HOST:-localhost}"
WRIT_PORT="${WRIT_PORT:-8765}"
WRIT_URL="http://${WRIT_HOST}:${WRIT_PORT}/health"

NEO4J_PORT="${NEO4J_PORT:-7687}"

# ── Check Neo4j ─────────────────────────────────────────────────────────────
neo4j_running() {
    # Quick TCP check on bolt port
    (echo > /dev/tcp/"$WRIT_HOST"/"$NEO4J_PORT") 2>/dev/null
}

if ! neo4j_running; then
    echo "[Writ] Neo4j not reachable on port $NEO4J_PORT -- attempting docker compose up" >&2
    if command -v docker &>/dev/null; then
        (cd "$WRIT_DIR" && docker compose up -d neo4j) >/dev/null 2>&1 || true
        # Wait up to 10s for Neo4j bolt port
        for i in $(seq 1 20); do
            if neo4j_running; then break; fi
            sleep 0.5
        done
        if neo4j_running; then
            echo "[Writ] Neo4j started" >&2
        else
            echo "[Writ] Warning: Neo4j did not become reachable within 10s" >&2
            echo "[Writ] Check logs: docker compose -f $WRIT_DIR/docker-compose.yml logs neo4j" >&2
        fi
    else
        echo "[Writ] Warning: docker not found, cannot start Neo4j" >&2
    fi
fi

# ── Check Writ server ───────────────────────────────────────────────────────
writ_running() {
    curl -s --connect-timeout 0.1 "$WRIT_URL" >/dev/null 2>&1
}

if writ_running; then
    echo "[Writ] Server already running on port $WRIT_PORT" >&2
    exit 0
fi

# Activate venv if present
if [ -f "$WRIT_DIR/.venv/bin/activate" ]; then
    source "$WRIT_DIR/.venv/bin/activate"
fi

# Start Writ server in background
WRIT_LOG="/tmp/writ-server.log"
nohup writ serve > "$WRIT_LOG" 2>&1 &
WRIT_PID=$!

# Wait up to 5s for startup (Writ cold start is ~0.6s at 80 rules)
for i in $(seq 1 50); do
    if writ_running; then
        echo "[Writ] Server started (PID $WRIT_PID, log: $WRIT_LOG)" >&2
        exit 0
    fi
    sleep 0.1
done

echo "[Writ] Warning: server did not respond within 5s (PID $WRIT_PID, check $WRIT_LOG)" >&2
# Non-fatal: hooks will show "server unavailable" and proceed gracefully
exit 0
