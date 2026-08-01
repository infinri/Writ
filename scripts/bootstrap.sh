#!/usr/bin/env bash
# Writ bootstrap -- end-to-end setup for a new machine.
#
# Runs prerequisite checks, creates a Python venv, installs the package,
# renders the harness config from templates, symlinks rules and agent
# definitions, brings up Neo4j via docker compose, ingests the rule
# corpus, and starts the Writ daemon. Idempotent -- safe to re-run.
#
# Usage:
#   cd ~/.claude/skills/writ
#   bash scripts/bootstrap.sh

set -euo pipefail

# ── Tunables (named constants per ARCH-CONST-001) ───────────────────────────
readonly NEO4J_WAIT_SECONDS=60   # Max wait for Neo4j bolt port after `compose up`
readonly DAEMON_WAIT_SECONDS=10  # Max wait for writ serve /health after launch
readonly MIN_PYTHON_MAJOR=3
readonly MIN_PYTHON_MINOR=11

# ── Paths ───────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$WRIT_DIR/.venv"
COMPOSE_FILE="$WRIT_DIR/docker-compose.yml"

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
require_tool git     "Install git (apt install git / brew install git)." || missing=1
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

# ── 3. Python venv ──────────────────────────────────────────────────────────
step "Setting up Python virtualenv"
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
    ok "created $VENV_DIR"
else
    ok "venv already exists"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# ── 4. Install Python deps ──────────────────────────────────────────────────
step "Installing Python dependencies"
pip install --quiet --upgrade pip
# Install with [dev] extras so optimum (ONNX export tool) is available
# for the next step. The SentenceTransformer fallback library lives in
# the separate [fallback] group and is NOT installed by default --
# production daemons running on ONNX never need it. Operators who want
# to exercise the WRIT_ALLOW_EMBEDDING_FALLBACK=1 path can install it
# explicitly with `pip install -e '.[fallback]'`.
(cd "$WRIT_DIR" && pip install --quiet -e '.[dev]')
ok "writ package installed (editable, with dev extras)"

# ── 4b. Export ONNX embedding model ─────────────────────────────────────────
ONNX_MODEL_PATH="$HOME/.cache/writ/models/onnx/model.onnx"
step "Ensuring ONNX embedding model is exported"
if [ -f "$ONNX_MODEL_PATH" ]; then
    ok "ONNX model already present at $ONNX_MODEL_PATH (skipping export)"
else
    (cd "$WRIT_DIR" && python scripts/export_onnx.py)
    ok "ONNX model exported to $ONNX_MODEL_PATH"
fi

# ── 5. Global config patch ──────────────────────────────────────────────────
# Hooks come from the plugin (hooks/hooks.json); this patches the parts a plugin
# manifest cannot ship: the Writ permission allowlist, the statusLine, and CLAUDE.md.
step "Patching ~/.claude (permissions + statusLine + CLAUDE.md)"
bash "$SCRIPT_DIR/patch-global-config.sh"

# ── 6. Symlinks for rules + agents ──────────────────────────────────────────
step "Linking rules and agent definitions into ~/.claude/"
mkdir -p "$HOME/.claude/rules" "$HOME/.claude/agents"

link_all() {
    local src_dir="$1"
    local dst_dir="$2"
    for src in "$src_dir"/*.md; do
        [ -f "$src" ] || continue
        local name
        name=$(basename "$src")
        local target="$dst_dir/$name"
        if [ -L "$target" ] || [ ! -e "$target" ]; then
            ln -sf "$src" "$target"
        fi
    done
}

link_all "$WRIT_DIR/rules" "$HOME/.claude/rules"
# Role files moved to agents/ (the plugin root's documented location). Linking the old
# .claude/agents path would recreate symlinks pointing at deleted files; link_all relinks
# any target that is already a symlink, so re-running this repairs an upgraded install.
link_all "$WRIT_DIR/agents" "$HOME/.claude/agents"
ok "rules and agents linked"

# ── 7. Start Neo4j via docker compose ──────────────────────────────────────
step "Starting Neo4j via docker compose"
(cd "$WRIT_DIR" && docker compose up -d neo4j) >/dev/null
ok "neo4j container started"

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

# ── 8. Ingest rules ────────────────────────────────────────────────────────
step "Ingesting rule corpus from writ-corpus.cypher"
if writ import-cypher 2>&1 | tail -5; then
    ok "rules ingested"
else
    warn "ingestion reported errors; daemon will serve whatever made it into Neo4j"
fi

# ── 9. Start Writ daemon ───────────────────────────────────────────────────
step "Starting Writ daemon"
DAEMON_URL="http://localhost:8765/health"
if curl -sf --connect-timeout 0.5 "$DAEMON_URL" >/dev/null 2>&1; then
    ok "writ serve already running"
else
    # Same resolver the hooks and ensure-server use, so a bootstrap-started daemon
    # writes where every other start path expects to find it.
    source "$WRIT_DIR/scripts/lib/writ-server-lib.sh"
    WRIT_LOG="$(writ_default_server_log)"
    mkdir -p "$(dirname "$WRIT_LOG")" 2>/dev/null || true
    nohup writ serve > "$WRIT_LOG" 2>&1 &
    DAEMON_PID=$!
    printf "   waiting for /health "
    waited=0
    while [ $waited -lt $DAEMON_WAIT_SECONDS ]; do
        if curl -sf --connect-timeout 0.5 "$DAEMON_URL" >/dev/null 2>&1; then
            printf "\n"
            ok "daemon ready (pid $DAEMON_PID, log $WRIT_LOG)"
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

# ── 9b. Symlink the writ CLI onto PATH ──────────────────────────────────────
mkdir -p "$HOME/.local/bin"
ln -sf "$WRIT_DIR/bin/writ" "$HOME/.local/bin/writ"
if ! printf '%s' "$PATH" | tr ':' '\n' | grep -qx "$HOME/.local/bin"; then
    printf "${YELLOW}!${RESET} %s is not on your PATH; add it so 'writ' resolves in new shells.\n" "$HOME/.local/bin"
fi

# ── 10. Ready banner ───────────────────────────────────────────────────────
RULE_COUNT=$(curl -sf "http://localhost:8765/stats" 2>/dev/null \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('rule_count','?'))" 2>/dev/null \
    || echo "?")

printf "\n${GREEN}${BOLD}════════════════════════════════════════════${RESET}\n"
printf "${GREEN}${BOLD}  Writ is ready${RESET}\n"
printf "${GREEN}${BOLD}════════════════════════════════════════════${RESET}\n"
printf "  Neo4j          : bolt://localhost:7687\n"
printf "  Writ daemon    : http://localhost:8765\n"
printf "  Rules loaded   : %s\n" "$RULE_COUNT"
printf "  Daemon log     : /tmp/writ-server.log\n"
printf "  Harness config : ~/.claude/settings.json, ~/.claude/CLAUDE.md\n"
printf "\n"
printf "${YELLOW}!${RESET} Restart Claude Code for the hooks to take effect.\n"
