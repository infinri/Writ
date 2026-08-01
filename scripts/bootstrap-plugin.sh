#!/usr/bin/env bash
# Writ plugin bootstrap -- end-to-end setup for a Claude Code plugin install.
#
# Plugin-aware variant of scripts/bootstrap.sh. Creates a Python venv at
# ${CLAUDE_PLUGIN_DATA:-$HOME/.cache/writ}/.venv so the venv survives plugin
# upgrades that rewrite ${CLAUDE_PLUGIN_ROOT}. Installs the package via
# `pip install -e ${CLAUDE_PLUGIN_ROOT}` (editable) so subsequent upgrades
# rebind imports to the new install path. Brings up Neo4j via docker
# compose, ingests the rule corpus, and starts the Writ daemon.
# Idempotent -- safe to re-run on every plugin upgrade.
#
# Usage (there is no `claude plugin path` subcommand; read installPath instead):
#   WRIT_DIR=$(claude plugin list --json | python3 -c "import json,sys; \
#     print(next(p['installPath'] for p in json.load(sys.stdin) \
#     if p['id'].split('@')[0] == 'writ'))")
#   bash "$WRIT_DIR/scripts/bootstrap-plugin.sh"

set -euo pipefail

# ── Tunables (named constants per ARCH-CONST-001) ───────────────────────────
readonly NEO4J_WAIT_SECONDS=60   # Max wait for Neo4j bolt port after `compose up`
readonly DAEMON_WAIT_SECONDS=10  # Max wait for writ serve /health after launch
readonly MIN_PYTHON_MAJOR=3
readonly MIN_PYTHON_MINOR=11

# ── Paths ───────────────────────────────────────────────────────────────────
# Plugin install root: prefer ${CLAUDE_PLUGIN_ROOT} (set by Claude Code when
# the plugin is loaded). Fall back to a dirname walk so the script also works
# when invoked from a checked-out repo without the plugin env set.
if [ -n "${CLAUDE_PLUGIN_ROOT:-}" ]; then
    WRIT_DIR="${CLAUDE_PLUGIN_ROOT}"
else
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    WRIT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
fi

# Persistent data dir survives plugin upgrades.
WRIT_DATA="${CLAUDE_PLUGIN_DATA:-$HOME/.cache/writ}"
VENV_DIR="${WRIT_DATA}/.venv"
COMPOSE_FILE="${WRIT_DIR}/docker-compose.yml"

# ── Colors (ANSI, degrade gracefully on dumb terminals) ─────────────────────
if [ -t 1 ] && [ "${TERM:-dumb}" != "dumb" ]; then
    GREEN='\033[0;32m'
    YELLOW='\033[0;33m'
    RED='\033[0;31m'
    BOLD='\033[1m'
    RESET='\033[0m'
else
    GREEN=''; YELLOW=''; RED=''; BOLD=''; RESET=''
fi

ok()   { printf "${GREEN}✓${RESET} %s\n" "$*"; }
warn() { printf "${YELLOW}!${RESET} %s\n" "$*"; }
err()  { printf "${RED}✗${RESET} %s\n" "$*" >&2; }
step() { printf "\n${BOLD}→ %s${RESET}\n" "$*"; }

# ── 1. Prerequisite checks ──────────────────────────────────────────────────
step "Checking prerequisites"

require_tool() {
    local tool="$1"
    local hint="$2"
    if ! command -v "$tool" >/dev/null 2>&1; then
        err "Missing required tool: $tool"
        echo "   $hint" >&2
        return 1
    fi
    ok "$tool"
}

missing=0
require_tool python3 "Install Python 3.11+ (e.g., apt install python3 python3-venv / brew install python@3.11)." || missing=1
require_tool docker  "Install Docker (https://docs.docker.com/get-docker/) and ensure Docker Desktop is running." || missing=1
require_tool jq      "Install jq (apt install jq / brew install jq)." || missing=1
require_tool curl    "Install curl (apt install curl / brew install curl)." || missing=1
require_tool envsubst "Install gettext (apt install gettext-base / brew install gettext)." || missing=1
if [ $missing -ne 0 ]; then
    err "One or more prerequisites missing. See messages above."
    exit 1
fi

# Python version check
PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=${PY_VER%.*}
PY_MINOR=${PY_VER#*.}
if [ "$PY_MAJOR" -lt "$MIN_PYTHON_MAJOR" ] \
   || { [ "$PY_MAJOR" -eq "$MIN_PYTHON_MAJOR" ] && [ "$PY_MINOR" -lt "$MIN_PYTHON_MINOR" ]; }; then
    err "python3 version is $PY_VER; need >= $MIN_PYTHON_MAJOR.$MIN_PYTHON_MINOR"
    echo "   Install a newer Python (pyenv is a clean way to manage versions)." >&2
    exit 1
fi
ok "python3 $PY_VER"

# ── 2. Docker daemon reachable ──────────────────────────────────────────────
step "Checking Docker daemon"
if ! docker info >/dev/null 2>&1; then
    err "Docker daemon not reachable."
    echo "   Start Docker Desktop (or run \`sudo systemctl start docker\`) and retry." >&2
    exit 1
fi
ok "docker daemon reachable"

# ── 3. Python venv at ${CLAUDE_PLUGIN_DATA}/.venv ───────────────────────────
step "Setting up Python virtualenv at $VENV_DIR"
mkdir -p "$WRIT_DATA"
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
    ok "created $VENV_DIR"
else
    ok "venv already exists"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# ── 4. Install Python deps (editable; rebinds on plugin upgrade) ────────────
step "Installing Python dependencies"
pip install --quiet --upgrade pip
# Install with [dev] extras so optimum (ONNX export tool) is available
# for the export step. The SentenceTransformer fallback library lives
# in the separate [fallback] group and is NOT installed by default --
# production daemons running on ONNX never need it.
pip install --quiet -e "${WRIT_DIR}[dev]"
ok "writ package installed (editable from ${WRIT_DIR}, with dev extras)"

# ── 4b. Export ONNX embedding model ─────────────────────────────────────────
ONNX_MODEL_PATH="${HOME}/.cache/writ/models/onnx/model.onnx"
step "Ensuring ONNX embedding model is exported"
if [ -f "$ONNX_MODEL_PATH" ]; then
    ok "ONNX model already present at $ONNX_MODEL_PATH (skipping export)"
else
    (cd "${WRIT_DIR}" && python scripts/export_onnx.py)
    ok "ONNX model exported to $ONNX_MODEL_PATH"
fi

# ── 5. Start Neo4j via docker compose ──────────────────────────────────────
step "Starting Neo4j via docker compose"
# Idempotent across container provenance: a writ-neo4j container created
# outside this compose project (manual docker run, an older checkout's
# compose) makes `compose up` fail on the name conflict. If the container
# exists, reuse it: start it if stopped, leave it if running.
if docker inspect writ-neo4j >/dev/null 2>&1; then
    if [ "$(docker inspect -f '{{.State.Running}}' writ-neo4j 2>/dev/null)" = "true" ]; then
        ok "neo4j container already running (pre-existing, reused)"
    else
        docker start writ-neo4j >/dev/null
        ok "neo4j container started (pre-existing, reused)"
    fi
else
    docker compose -f "${COMPOSE_FILE}" up -d neo4j >/dev/null
    ok "neo4j container started"
fi

printf "   waiting for bolt port 7687 "
waited=0
while [ $waited -lt $NEO4J_WAIT_SECONDS ]; do
    if (echo > /dev/tcp/127.0.0.1/7687) 2>/dev/null; then
        printf "\n"
        ok "Neo4j bolt port ready"
        break
    fi
    printf "."
    sleep 1
    waited=$((waited + 1))
done
if [ $waited -ge $NEO4J_WAIT_SECONDS ]; then
    printf "\n"
    err "Neo4j did not become reachable within ${NEO4J_WAIT_SECONDS}s"
    echo "   Check logs: docker compose -f $COMPOSE_FILE logs neo4j" >&2
    exit 1
fi

# ── 6. Ingest rules (cd into WRIT_DIR so writ-corpus.cypher resolves) ──────
step "Ingesting rule corpus from writ-corpus.cypher"
if (cd "${WRIT_DIR}" && writ import-cypher 2>&1 | tail -5); then
    ok "rules ingested"
else
    warn "ingestion reported errors; daemon will serve whatever made it into Neo4j"
fi

# ── 7. Start Writ daemon ───────────────────────────────────────────────────
step "Starting Writ daemon"
DAEMON_URL="http://localhost:8765/health"
if curl -sf --connect-timeout 0.5 "$DAEMON_URL" >/dev/null 2>&1; then
    ok "writ serve already running"
else
    # Resolved by the shared owner (writ_default_server_log), whose plugin branch yields
    # ${CLAUDE_PLUGIN_DATA}/server.log -- the same path this line hardcoded. Sourcing it
    # keeps this fifth start path from drifting from the other four.
    source "${WRIT_DIR}/scripts/lib/writ-server-lib.sh"
    WRIT_LOG="$(writ_default_server_log)"
    mkdir -p "$(dirname "$WRIT_LOG")" 2>/dev/null || true
    (cd "${WRIT_DIR}" && nohup writ serve > "$WRIT_LOG" 2>&1 &)
    printf "   waiting for /health "
    waited=0
    while [ $waited -lt $DAEMON_WAIT_SECONDS ]; do
        if curl -sf --connect-timeout 0.5 "$DAEMON_URL" >/dev/null 2>&1; then
            printf "\n"
            ok "daemon ready (log $WRIT_LOG)"
            break
        fi
        printf "."
        sleep 1
        waited=$((waited + 1))
    done
    if [ $waited -ge $DAEMON_WAIT_SECONDS ]; then
        printf "\n"
        err "daemon did not become healthy within ${DAEMON_WAIT_SECONDS}s"
        echo "   Check log: $WRIT_LOG" >&2
        exit 1
    fi
fi

# ── 8. Ready banner ────────────────────────────────────────────────────────
RULE_COUNT=$(curl -sf "http://localhost:8765/stats" 2>/dev/null \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('rule_count','?'))" 2>/dev/null \
    || echo "?")

printf "\n${GREEN}${BOLD}════════════════════════════════════════════${RESET}\n"
printf "${GREEN}${BOLD}  Writ plugin is ready${RESET}\n"
printf "${GREEN}${BOLD}════════════════════════════════════════════${RESET}\n"
printf "  Plugin root    : %s\n" "$WRIT_DIR"
printf "  Venv           : %s\n" "$VENV_DIR"
printf "  Neo4j          : bolt://localhost:7687\n"
printf "  Writ daemon    : http://localhost:8765\n"
printf "  Rules loaded   : %s\n" "$RULE_COUNT"
printf "  Daemon log     : %s/server.log\n" "$WRIT_DATA"
printf "\n"
printf "  Verify         : curl http://localhost:8765/health\n"
printf "\n"
printf "${YELLOW}!${RESET} Restart Claude Code for the hooks to take effect.\n"
