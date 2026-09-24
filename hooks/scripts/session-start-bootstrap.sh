#!/usr/bin/env bash
# SessionStart hook: probes the plugin's runtime prerequisites and starts
# the Writ FastAPI daemon if everything is in place. Graceful-degrades
# in every failure branch (exits 0 so the session is never blocked).
#
# Lives at hooks/scripts/, not .claude/hooks/, so dirname walks resolve
# wrong; uses ${CLAUDE_PLUGIN_ROOT} directly instead.

set -u

# 0. Capture the SessionStart payload once (session_id, cwd, source). CC sends it on
#    stdin; read it before anything else so the carry-forward step (step 5) can use it.
#    Guarded: an empty / unreadable payload leaves the fields blank and the carry no-ops.
STDIN_JSON="$(cat 2>/dev/null || true)"

# 0b. Carry session state out of the install-relative var/session earlier releases used, into
#     the durable state root (bin/lib/writ_state_migrate.py says why and how). It runs BEFORE
#     the CLAUDE_PLUGIN_ROOT exit below, because a clone whose hooks were seeded into
#     settings.json has no CLAUDE_PLUGIN_ROOT and would otherwise never migrate, and before the
#     venv probe, because it needs only the system python3. The path is this script's own
#     location (hooks/scripts -> <skill>), the walk writ-rag-inject.sh uses outside the plugin
#     loader. Best-effort and silent: a failed carry leaves the session exactly as it was.
_SSB_SKILL_DIR="$(cd "$(dirname "$0")/../.." 2>/dev/null && pwd)" || _SSB_SKILL_DIR=""
if [ -n "$_SSB_SKILL_DIR" ] && [ -f "$_SSB_SKILL_DIR/bin/lib/writ_state_migrate.py" ]; then
  python3 "$_SSB_SKILL_DIR/bin/lib/writ_state_migrate.py" >/dev/null 2>&1 || true
fi

# 1. Resolve install root and persistent-data dir. The plugin loader sets
#    CLAUDE_PLUGIN_ROOT; if unset, we're not running under the loader so
#    there's nothing to bootstrap.
if [ -z "${CLAUDE_PLUGIN_ROOT:-}" ]; then
  exit 0
fi
WRIT_DIR="${CLAUDE_PLUGIN_ROOT}"
# Instrumented from HERE, not the top of the file: the early `exit 0` above fires
# when CLAUDE_PLUGIN_ROOT is unset, which means we are not running under the plugin
# loader and there is nothing to bootstrap -- a genuine no-op not worth a row, and
# WRIT_DIR is not resolvable there anyway (dirname walks are unreliable for this
# hook, see the header). Guarded so bootstrap never breaks on a missing common.sh.
source "$WRIT_DIR/bin/lib/common.sh" 2>/dev/null || true
type hook_instrument >/dev/null 2>&1 && hook_instrument "session-start-bootstrap"

# THIS HOOK'S OWN TELEMETRY IS KEYED HERE, from the payload captured in step 0. It is
# keyed at the TOP and not down in step 5, because the exit trap armed above reads
# SESSION_ID when the script EXITS and steps 2 and 3 both `exit 0` long before step 5
# runs -- so the venv-missing and Neo4j-unreachable rows, exactly the ones a broken
# install produces, would be the unattributed ones. The trap files under
# `${SESSION_ID:-${HOOK_SESSION_ID:-}}` and this hook set neither, so its rows landed
# under the literal session id "unknown" (measured 2026-08-11: 17 rows). load_hook_env
# is not usable here -- it reads stdin, which step 0 has already consumed -- and neither
# is the venv python, which step 2 has not yet proven exists.
#
# The id is the payload's and only the payload's (agent_id first, so a sub-agent's rows
# are not filed under its parent). It is never synthesized: an id the payload did not
# carry stays EMPTY, leaving a visible gap rather than a silently wrong record. Step 5
# keeps its own parse because it needs cwd and source as well.
SESSION_ID="$(printf '%s' "${STDIN_JSON}" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print(''); sys.exit(0)
print((d.get('agent_id') or d.get('session_id') or '').strip())
" 2>/dev/null || echo "")"

WRIT_DATA="${CLAUDE_PLUGIN_DATA:-$HOME/.cache/writ}"
# The venv comes from the shared resolver (bin/lib/writ-venv.sh). Under the loader that is
# $CLAUDE_PLUGIN_DATA/.venv, outside ${CLAUDE_PLUGIN_ROOT}, so an upgrade does not orphan it.
# The loader's ${WRIT_DIR} copy wins, per this file's header: a dirname walk can land in a staged
# or stale tree. The walk (step 0b) is only the fallback for a plugin root that holds nothing yet,
# so step 2's message still prints there instead of the hook dying on an unbound VENV_DIR.
_SSB_VENV_LIB="${WRIT_DIR}/bin/lib/writ-venv.sh"
[ -f "$_SSB_VENV_LIB" ] || _SSB_VENV_LIB="${_SSB_SKILL_DIR:-${WRIT_DIR}}/bin/lib/writ-venv.sh"
# shellcheck source=bin/lib/writ-venv.sh
source "$_SSB_VENV_LIB"
writ_resolve_venv "${WRIT_DIR}"
NEO4J_HOST="${WRIT_NEO4J_HOST:-localhost}"
NEO4J_PORT="${WRIT_NEO4J_PORT:-7687}"

# 2. Probe venv. If missing, instruct user and exit 0.
#
# The bootstrap command is printed on its own line with NO "[Writ] " prefix and with
# ${CLAUDE_PLUGIN_ROOT} already expanded, so it is a clean copy-paste. That single line
# IS the documented install step: it is why the docs no longer carry a WRIT_DIR
# discovery incantation, and why nothing else has to be run after it.
if [ ! -x "${VENV_DIR}/bin/python3" ]; then
  cat >&2 <<MSG
[Writ] Plugin venv not bootstrapped at ${VENV_DIR}.
[Writ] Run this one command, then restart Claude Code:

bash ${WRIT_DIR}/scripts/bootstrap-plugin.sh

[Writ] It creates the venv, brings up Neo4j, seeds the rule corpus, starts the daemon,
[Writ] patches ~/.claude/settings.json + CLAUDE.md, and installs the slash commands.
[Writ] Writ hooks will degrade gracefully until bootstrap completes.
MSG
  exit 0
fi

# 2b. The venv must import writ from THIS install. After an upgrade the shared venv still imports
#     the previous version dir, and a venv bootstrapped from a developer checkout imports the
#     checkout; either way the daemon would serve code this install does not ship. Here and only
#     here: the probe is a python start (~50ms), which the per-prompt path must not pay.
#     Non-fatal: a failed repoint prints the bootstrap command and the session carries on.
writ_venv_repoint "${VENV_DIR}" "${WRIT_DIR}" || true

# 3. Probe Neo4j bolt port 7687. If unreachable, instruct user and exit 0.
# timeout-wrapped: a bare /dev/tcp connect to a black-holed host blocks for the
# kernel SYN timeout (minutes) and would stall every SessionStart with it.
if ! timeout 2 bash -c "exec 3<>/dev/tcp/${NEO4J_HOST}/${NEO4J_PORT}" 2>/dev/null; then
  cat >&2 <<MSG
[Writ] Neo4j not reachable at ${NEO4J_HOST}:${NEO4J_PORT}.
[Writ] Start it with:
[Writ]   docker start writ-neo4j
[Writ] or, when no writ-neo4j container exists yet:
[Writ]   docker compose -f ${WRIT_DIR}/docker-compose.yml up -d neo4j
[Writ] Writ hooks will degrade gracefully until Neo4j is up.
MSG
  exit 0
fi
exec 3<&- 2>/dev/null || true
exec 3>&- 2>/dev/null || true

# 4. Ensure the Writ server is up via the shared, flock-guarded singleton routine. This is the
#    SAME routine scripts/ensure-server.sh uses, so the plugin SessionStart and the init path
#    cannot race each other into two `writ serve` launches. The lib probes
#    http://localhost:8765/health, starts the daemon under flock if it is down, and pins
#    WRIT_CACHE_DIR. Graceful: it always returns 0.
WRIT_HOST="localhost"
WRIT_PORT="8765"
# WRIT_LOG intentionally unset: the library resolves it, and its plugin branch picks
# ${CLAUDE_PLUGIN_DATA}/server.log -- the same path this line used to hardcode.
# shellcheck source=scripts/lib/writ-server-lib.sh
source "${WRIT_DIR}/scripts/lib/writ-server-lib.sh"
# WRIT_NO_AUTOSTART (set by tests / CI) suppresses the auto-start, the same check
# writ-rag-inject.sh makes, so running this hook against a throwaway WRIT_PORT does not
# spawn (and leak) a real daemon on that port. Unset in production, so the call stands.
[ -z "${WRIT_NO_AUTOSTART:-}" ] && writ_ensure_server

# 5. Session-id rotation carry-forward. If the harness rotated the session id, the fresh
#    cache has mode=None and every write is denied [ENF-GATE-MODE]. Parse the payload
#    (session_id, cwd, source), read the PRE-rotation id from /tmp/writ-current-session
#    (still holding the old id at SessionStart, before the next turn overwrites it), and
#    let writ-session.py carry-forward-mode decide (same-project-guarded, mode-only, gates
#    reset). Fully guarded: every branch is best-effort and exits 0; never blocks the session.
SESSION_HELPER="${WRIT_DIR}/bin/lib/writ-session.py"
if [ -n "${STDIN_JSON}" ] && [ -f "${SESSION_HELPER}" ]; then
  PARSED="$(printf '%s' "${STDIN_JSON}" | "${VENV_DIR}/bin/python3" -c "
import sys, json
try:
    d = json.load(sys.stdin)
    sid = str(d.get('session_id', '') or '').strip()
    cwd = str(d.get('cwd', '') or '').strip()
    source = str(d.get('source', '') or '').strip()
    print(sid)
    print(cwd)
    print(source)
except Exception:
    print('')
    print('')
    print('')
" 2>/dev/null || printf '\n\n\n')"
  SID="$(printf '%s' "${PARSED}" | sed -n '1p')"
  CWD="$(printf '%s' "${PARSED}" | sed -n '2p')"
  SOURCE="$(printf '%s' "${PARSED}" | sed -n '3p')"
  PREV=""
  if [ -f /tmp/writ-current-session ]; then
    PREV="$(tr -d '[:space:]' < /tmp/writ-current-session 2>/dev/null || true)"
  fi
  if [ -n "${SID}" ] && [ -n "${CWD}" ]; then
    "${VENV_DIR}/bin/python3" "${SESSION_HELPER}" carry-forward-mode \
      "${SID}" "${CWD}" "${PREV}" "${SOURCE}" >/dev/null 2>&1 || true
  fi
fi

exit 0
