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
# This is THE one command a plugin install runs. Besides the runtime it also patches
# ~/.claude (permissions + statusLine + CLAUDE.md) and installs the user-level slash
# commands, so there is no separate post-install step to miss.
#
# Usage: open Claude Code once after `claude plugin install writ@writ` and copy the
# absolute command the SessionStart hook prints, or run it from the install directory:
#   bash "$CLAUDE_PLUGIN_ROOT/scripts/bootstrap-plugin.sh"
#   bash scripts/bootstrap-plugin.sh --preflight   # prerequisite checks only, no install
#
# (There is no `claude plugin path` subcommand; `claude plugin list --json` carries the
# installPath if you need to read it by eye.)

set -euo pipefail

# --preflight runs ONLY the tool-presence and Python-version checks and exits, so the
# prerequisite contract is testable without a real install (Docker, pip, ONNX export).
# It deliberately stops before the `docker info` probe: the question it answers is
# "are the required tools here", not "is the Docker daemon running".
PREFLIGHT=0
for arg in "$@"; do
    case "$arg" in
        --preflight) PREFLIGHT=1 ;;
        *) echo "Unknown argument: $arg (accepted: --preflight)" >&2; exit 1 ;;
    esac
done

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
if [ $missing -ne 0 ]; then
    err "One or more prerequisites missing. See messages above."
    exit 1
fi

# jq and curl are OPTIONAL accelerators, never requirements: every jq read has a python3
# fallback (parsed_field / parsed_bool) and every curl call has a urllib fallback
# (writ_http_get / writ_http_post, bin/lib/writ_install.py http-*). The gettext
# substitution step is gone entirely: both of its single-variable substitutions are
# done in Python now.
optional_tool() {
    local tool="$1"
    if command -v "$tool" >/dev/null 2>&1; then
        ok "$tool (optional accelerator, present)"
    else
        warn "$tool not found: optional accelerator only, Writ falls back to Python stdlib"
    fi
}
optional_tool jq
optional_tool curl

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

if [ "$PREFLIGHT" = "1" ]; then
    ok "preflight complete: prerequisites satisfied (no install performed)"
    exit 0
fi

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
# Health probes go through the install module's stdlib http-get: curl is optional, and a
# probe that is always false on a curl-less machine would report a healthy daemon as down.
daemon_healthy() {
    python3 "$WRIT_DIR/bin/lib/writ_install.py" http-get "$DAEMON_URL" --fail --timeout 1 \
        >/dev/null 2>&1
}
if daemon_healthy; then
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
        if daemon_healthy; then
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

# ── 8. Global config patch + slash commands ────────────────────────────────
# Absorbed into this script so a plugin install is ONE command. These are the two things
# a plugin manifest cannot ship (the permission allowlist + statusLine + CLAUDE.md, and
# user-level slash commands), and leaving them as separate documented steps is exactly
# what users were missing. Both are idempotent. Non-fatal: the runtime above is already
# up, so a config-patch problem is reported with its re-run command rather than
# discarding a successful bootstrap.
step "Patching ~/.claude (permissions + statusLine + CLAUDE.md)"
if bash "$WRIT_DIR/scripts/patch-global-config.sh"; then
    ok "global config patched"
else
    warn "global config patch reported a problem; re-run: bash $WRIT_DIR/scripts/patch-global-config.sh"
fi

step "Installing user-level slash commands"
if bash "$WRIT_DIR/scripts/install-user-commands.sh"; then
    ok "slash commands installed"
else
    warn "slash command install reported a problem; re-run: bash $WRIT_DIR/scripts/install-user-commands.sh"
fi

# ── 9. Ready banner ────────────────────────────────────────────────────────
RULE_COUNT=$(python3 "$WRIT_DIR/bin/lib/writ_install.py" http-get "http://localhost:8765/stats" --fail 2>/dev/null \
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
printf "  Global config  : ~/.claude/settings.json, ~/.claude/CLAUDE.md, ~/.claude/commands/\n"
printf "\n"
printf "  Verify         : python3 %s/bin/lib/writ_install.py http-get http://localhost:8765/health\n" "$WRIT_DIR"
printf "\n"
printf "${YELLOW}!${RESET} Restart Claude Code for the hooks to take effect.\n"
