#!/usr/bin/env bash
# Plugin lifecycle: Init -- ensure Writ server and Neo4j are running.
# Called automatically when Claude Code loads the plugin.
# Non-fatal: if anything fails, hooks fall back gracefully (server unavailable).

set -euo pipefail

if [ -n "${CLAUDE_PLUGIN_ROOT:-}" ]; then
    WRIT_DIR="${CLAUDE_PLUGIN_ROOT}"
else
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    WRIT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
fi
# shellcheck source=bin/lib/writ-venv.sh
source "$WRIT_DIR/bin/lib/writ-venv.sh"
writ_resolve_venv "$WRIT_DIR" || true

# The flock + health-check + start logic and the Neo4j start live in the shared lib, so this
# init path and the plugin's SessionStart bootstrap cannot race each other into two
# `writ serve` launches.
# shellcheck source=scripts/lib/writ-server-lib.sh
source "$WRIT_DIR/scripts/lib/writ-server-lib.sh"

WRIT_HOST="${WRIT_HOST:-localhost}"
WRIT_PORT="${WRIT_PORT:-8765}"
NEO4J_PORT="${NEO4J_PORT:-7687}"

# ── Check Neo4j ─────────────────────────────────────────────────────────────
neo4j_running() {
    # Quick TCP check on bolt port
    (echo > /dev/tcp/"$WRIT_HOST"/"$NEO4J_PORT") 2>/dev/null
}

if ! neo4j_running; then
    echo "[Writ] Neo4j not reachable on port $NEO4J_PORT; starting the writ-neo4j container" >&2
    if command -v docker &>/dev/null; then
        # docker's own error reaches stderr; the script carries on so hooks degrade gracefully.
        if ! writ_neo4j_start "$WRIT_DIR/docker-compose.yml"; then
            echo "[Writ] Warning: could not start Neo4j (docker's error is above)" >&2
        fi
        # Wait up to 10s for Neo4j bolt port
        for i in $(seq 1 20); do
            if neo4j_running; then break; fi
            sleep 0.5
        done
        if neo4j_running; then
            echo "[Writ] Neo4j started" >&2
        else
            echo "[Writ] Warning: Neo4j did not become reachable within 10s" >&2
            echo "[Writ] Check logs: docker logs writ-neo4j" >&2
        fi
    else
        echo "[Writ] Warning: docker not found, cannot start Neo4j" >&2
    fi
fi

# ── Ensure the Writ server (singleton-safe, flock-guarded shared routine) ────
# WRIT_LOG intentionally unset: writ-server-lib.sh::writ_default_server_log owns the
# resolution (one path, four callers). Export WRIT_LOG to override.
# Start-only fallback (no cache-realign restart). The systemd user service
# (scripts/install-server-service.sh) now owns the daemon's lifecycle and
# auto-restarts it; this init path must never kill+restart a running daemon, or
# it races systemd. Realign a misaligned daemon with `systemctl --user restart
# writ-server`, not a hook. The realign capability stays in the lib (off by
# default) for any non-systemd install that still wants it.
writ_ensure_server
exit 0
