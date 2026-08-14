#!/usr/bin/env bash
# The disposable Neo4j instance the test suite runs against. (cycle 8)
#
# One home for the container recipe. It used to be retyped in three places
# (docs/reference/testing.md, writ/graph/db/_safety.py::how_to_run_safely and
# whatever a contributor pasted into their shell), and a retyped recipe is how a
# "test" instance ends up on the production port.
#
#   up      create the container when absent, start it when stopped, wait for
#           bolt to answer, replay writ-corpus.cypher when it had to create or
#           start something, then apply the schema and wait until every index is
#           ONLINE. Idempotent: an already-serving instance skips the replay but
#           still re-checks the schema, because that check is about the run that
#           follows, not about what this script did.
#   down    stop the container and KEEP its data. Nothing here destroys the
#           container or its volume, so the next `up` starts warm.
#   status  report what exists, what is running, and what the graph holds.
#           Touches nothing.
#
# Never names the production container `writ-neo4j`, and refuses to publish the
# production bolt port at all (see the SAFETY block below).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# The container name is hardcoded on purpose. The env-switchable name is the
# mechanism that failed on 2026-08-08: two test modules read a container name
# from WRIT_TEST_NEO4J_CONTAINER, nothing ever set it, and its default named the
# production container, so a suite redirected to 7688 still wiped 7687. A second
# way to name the target is the defect, not the feature.
CONTAINER_NAME="writ-test-neo4j"
NEO4J_IMAGE="neo4j:5"
NEO4J_USER="neo4j"
NEO4J_PASSWORD="writtestpass"

# The ONE place the disposable instance's bolt mapping is written. Everything
# else (the guard, the readiness probe, the URI this script prints and warms)
# derives the host half from it, so docker cannot publish one port while the
# rest of the script talks about another. tests/_graph.py::ISOLATED_NEO4J_URI
# holds the same fact for the python side, and a parity test reads both files.
BOLT_PUBLISH="7688:7687"
HTTP_PUBLISH="7475:7474"
HOST_BOLT_PORT="${BOLT_PUBLISH%%:*}"
ISOLATED_URI="bolt://localhost:${HOST_BOLT_PORT}"

CORPUS_DUMP="$REPO_ROOT/writ-corpus.cypher"
PYTHON="${PYTHON:-$REPO_ROOT/.venv/bin/python3}"
BOLT_WAIT_SECONDS="${WRIT_TEST_GRAPH_WAIT:-90}"

log() { echo "[writ-test-graph] $*"; }
fail() { echo "[writ-test-graph] $*" >&2; }

# ── SAFETY ──────────────────────────────────────────────────────────────────
# Host bolt 7687 is the production instance the interactive daemon serves. If
# this script ever published there, the "disposable" container would BE the real
# graph: the wipe guard compares instances by (host, port), so every
# destructive test would be aimed at production while reading as isolated. This
# refusal is checked before any docker verb runs.
if [ "$HOST_BOLT_PORT" = "7687" ]; then
    fail "refusing: the disposable instance must never publish host bolt port 7687."
    fail "That port is the production Neo4j the interactive daemon serves."
    fail "Fix BOLT_PUBLISH in $0 (and keep it equal to tests/_graph.py::ISOLATED_NEO4J_URI)."
    exit 2
fi

# The opt-out does nothing at all, deliberately: no docker verb, no probe, no
# message a caller has to parse, and exit 0 so `make test` still runs the
# non-graph half of the suite on a machine with no docker daemon. Same exact-"1"
# spelling as WRIT_NO_AUTOSTART and WRIT_TEST_GRAPH, so there is one convention
# and no near-miss value that quietly reads as consent.
if [ "${WRIT_TEST_NO_ISOLATION:-}" = "1" ]; then
    log "WRIT_TEST_NO_ISOLATION=1: doing nothing (the suite runs against the configured instance)."
    exit 0
fi

usage() {
    cat <<'USAGE'
Usage: scripts/test-graph.sh <up|down|status>

  up      Create or start the disposable Neo4j test instance, wait for bolt,
          replay writ-corpus.cypher if it had to create or start it, then apply
          the schema and wait until every index is ONLINE. Idempotent: an
          already-serving instance skips the replay but still re-checks the
          schema, because that check is about the run that follows.
  down    Stop the instance. Data is kept, so the next `up` starts warm.
  status  Report container and graph state. Changes nothing.

Environment:
  WRIT_TEST_NO_ISOLATION=1   This script does nothing and exits 0.
  PYTHON=/path/to/python     Interpreter used for the probe and the replay.
  WRIT_TEST_GRAPH_WAIT=90    Seconds to wait for bolt to answer.
USAGE
}

require_docker() {
    if ! command -v docker >/dev/null 2>&1; then
        fail "docker not found on PATH, so the disposable Neo4j cannot be managed."
        fail "Install docker, or run the suite with WRIT_TEST_NO_ISOLATION=1."
        exit 1
    fi
}

container_exists() {
    docker container inspect "$CONTAINER_NAME" >/dev/null 2>&1
}

container_running() {
    local state
    state="$(docker container inspect -f '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null || true)"
    [ "$state" = "true" ]
}

# One probe, two uses: its exit status answers "is this instance serving?" and
# its stdout answers "how many Rule nodes does it hold?". The three connection
# values are set on the command itself rather than read from the environment, so
# an operator who already exported WRIT_NEO4J_URI for something else cannot make
# this script probe -- or warm -- a different instance.
probe_graph() {
    WRIT_NEO4J_URI="$ISOLATED_URI" \
    WRIT_NEO4J_USER="$NEO4J_USER" \
    WRIT_NEO4J_PASSWORD="$NEO4J_PASSWORD" \
    "$PYTHON" - <<'PY'
import asyncio

from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
from writ.graph.db import Neo4jConnection


async def main() -> None:
    db = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
    try:
        print(await db.count_rules())
    finally:
        await db.close()


asyncio.run(main())
PY
}

# Readiness is an answered query, not an open socket: Neo4j accepts a bolt
# connection before it will serve one, and a replay fired into that window fails
# with an error that reads like a bad password. The driver path is the same one
# the suite resolves through, so "ready" here means ready for pytest. The TCP
# fallback exists only for a checkout with no venv yet; it cannot tell a healthy
# instance from an auth failure, and `up` says so before it trusts it.
bolt_ready() {
    if [ -x "$PYTHON" ]; then
        if probe_graph >/dev/null 2>&1; then
            return 0
        fi
        return 1
    fi
    if (echo > "/dev/tcp/localhost/$HOST_BOLT_PORT") 2>/dev/null; then
        return 0
    fi
    return 1
}

wait_for_bolt() {
    local waited=0
    while [ "$waited" -lt "$BOLT_WAIT_SECONDS" ]; do
        if bolt_ready; then
            return 0
        fi
        sleep 1
        waited=$((waited + 1))
    done
    return 1
}

# Replays the tracked dump, which is what CI migrates from too
# (.github/actions/setup-writ). import_cypher_dump replaces the corpus and
# PRESERVES runtime records, so this is safe to re-run and needs no
# WRIT_TEST_GRAPH marker: the marker only authorizes an everything-wipe, and a
# dump that ever did require one would refuse loudly rather than delete quietly.
warm_corpus() {
    if [ ! -f "$CORPUS_DUMP" ]; then
        fail "corpus dump not found at $CORPUS_DUMP, so the instance cannot be warmed."
        return 1
    fi
    if [ ! -x "$PYTHON" ]; then
        fail "no interpreter at $PYTHON, so the corpus cannot be replayed."
        fail "Run 'bash scripts/bootstrap.sh', or set PYTHON=/path/to/python."
        return 1
    fi
    log "replaying $(basename "$CORPUS_DUMP") into $ISOLATED_URI"
    WRIT_NEO4J_URI="$ISOLATED_URI" \
    WRIT_NEO4J_USER="$NEO4J_USER" \
    WRIT_NEO4J_PASSWORD="$NEO4J_PASSWORD" \
    "$PYTHON" -m writ.cli import-cypher "$CORPUS_DUMP"
}

# Applies the schema, then waits until every index reports state=ONLINE.
#
# The disposable instance never had a schema applied to it. writ-corpus.cypher
# carries nodes and edges only (its single CREATE INDEX line is example text
# inside a rule), and import_cypher_dump does not call apply_constraints, so a
# fresh container ran the suite with NO uniqueness constraints at all -- which is
# exactly what makes a MERGE on a non-constrained key race (create_project,
# create_memory). Applying it here is the fix; waiting for ONLINE is what makes
# "ready" mean ready rather than "a query answered".
#
# `state` lives ONLY on SHOW INDEXES rows; SHOW CONSTRAINTS has no state field, so
# a constraint's readiness is read through its ownedIndex. Zero indexes is not
# ready. The wait loop runs INSIDE python so a 90s wait costs one interpreter
# start and one driver connection instead of ninety.
ensure_schema() {
    WRIT_NEO4J_URI="$ISOLATED_URI" \
    WRIT_NEO4J_USER="$NEO4J_USER" \
    WRIT_NEO4J_PASSWORD="$NEO4J_PASSWORD" \
    WRIT_SCHEMA_WAIT="$BOLT_WAIT_SECONDS" \
    "$PYTHON" - <<'PY'
import asyncio
import json
import os
import time

from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
from writ.graph.db import Neo4jConnection


async def main() -> int:
    deadline = time.monotonic() + float(os.environ.get("WRIT_SCHEMA_WAIT", "90"))
    db = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
    try:
        await db.apply_constraints()
        while True:
            state = await db.schema_readiness()
            if state["ready"]:
                print(json.dumps(state))
                return 0
            if time.monotonic() >= deadline:
                print(json.dumps(state))
                return 1
            await asyncio.sleep(1)
    finally:
        await db.close()


raise SystemExit(asyncio.run(main()))
PY
}

cmd_up() {
    require_docker

    # "Did I have to touch it?" is what decides whether the corpus is replayed.
    # A running instance the suite has been using keeps its graph as-is; a cold
    # one gets warmed once, here, instead of by every pytest session.
    local touched=0
    if ! container_exists; then
        log "creating $CONTAINER_NAME ($NEO4J_IMAGE, bolt $BOLT_PUBLISH, http $HTTP_PUBLISH)"
        docker run -d --name "$CONTAINER_NAME" \
            -p "$BOLT_PUBLISH" \
            -p "$HTTP_PUBLISH" \
            -e NEO4J_AUTH="$NEO4J_USER/$NEO4J_PASSWORD" \
            "$NEO4J_IMAGE" >/dev/null
        touched=1
    elif ! container_running; then
        log "starting existing $CONTAINER_NAME"
        docker start "$CONTAINER_NAME" >/dev/null
        touched=1
    fi

    if [ "$touched" -eq 0 ] && bolt_ready; then
        log "$CONTAINER_NAME already serving $ISOLATED_URI"
    else
        log "waiting up to ${BOLT_WAIT_SECONDS}s for bolt on $ISOLATED_URI"
        if ! wait_for_bolt; then
            fail "$ISOLATED_URI did not answer within ${BOLT_WAIT_SECONDS}s."
            fail "Inspect it with: docker logs $CONTAINER_NAME"
            return 1
        fi
        if [ "$touched" -ne 0 ] && ! warm_corpus; then
            fail "the instance is up but its corpus is incomplete, so pytest will refuse the run."
            return 1
        fi
    fi

    # After the replay, never before: applying the DDL first would put the
    # replay's CREATEs into the window where the new constraints' backing indexes
    # are still populating, and population over an already-loaded graph happens
    # once instead of incrementally. The cost accepted is that a dump carrying a
    # duplicate business key now fails here, at constraint creation, rather than
    # on the offending row. apply_constraints is idempotent, so paying it on every
    # `up` buys the guarantee for the run that follows.
    local schema
    if ! schema="$(ensure_schema)"; then
        fail "the schema did not come ONLINE within ${BOLT_WAIT_SECONDS}s: ${schema:-no readiness report}"
        fail "Inspect it with: docker logs $CONTAINER_NAME"
        return 1
    fi
    log "schema ready: $schema"
    log "ready: $ISOLATED_URI (user $NEO4J_USER)"
}

cmd_down() {
    require_docker
    if ! container_exists; then
        log "$CONTAINER_NAME does not exist, nothing to stop"
        return 0
    fi
    if ! container_running; then
        log "$CONTAINER_NAME is already stopped"
        return 0
    fi
    docker stop "$CONTAINER_NAME" >/dev/null
    # Stop, never destroy. The data stays on the container's own storage so the
    # next `up` skips the replay, and there is deliberately no subcommand that
    # deletes the container or its volume: a lifecycle script whose vocabulary
    # includes "delete the graph" is one typo away from being the incident.
    log "stopped $CONTAINER_NAME (data kept; 'up' starts it again)"
}

cmd_status() {
    require_docker
    if ! container_exists; then
        log "container:  absent ($CONTAINER_NAME)"
        log "run 'make test-graph-up' to create it"
        return 0
    fi
    if container_running; then
        log "container:  running ($CONTAINER_NAME, bolt $BOLT_PUBLISH)"
    else
        log "container:  stopped ($CONTAINER_NAME)"
        log "run 'make test-graph-up' to start it"
        return 0
    fi

    local rules=""
    if [ -x "$PYTHON" ]; then
        rules="$(probe_graph 2>/dev/null || true)"
    fi
    if [ -n "$rules" ]; then
        log "graph:      $ISOLATED_URI answering, $rules Rule nodes"
    else
        log "graph:      $ISOLATED_URI not answering yet"
    fi
}

case "${1:-}" in
    up) cmd_up ;;
    down) cmd_down ;;
    status) cmd_status ;;
    -h | --help | help) usage ;;
    "")
        usage >&2
        exit 64
        ;;
    *)
        fail "unknown subcommand: $1"
        usage >&2
        exit 64
        ;;
esac
