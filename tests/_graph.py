"""The single path the test suite takes to reach Neo4j (cycle 8).

WHY THIS MODULE EXISTS
----------------------
On 2026-08-08 a full suite run with `WRIT_NEO4J_URI=bolt://localhost:7688`
exported still emptied the PRODUCTION corpus on 7687 (Rule 287 -> 0). The
mechanism was not a missed env export. Two modules
(`tests/test_import_markdown_unified.py`, `tests/test_compress_on_ingest.py`)
resolved their target from a container NAME:

    docker exec ${WRIT_TEST_NEO4J_CONTAINER:-writ-neo4j} cypher-shell ...

`WRIT_TEST_NEO4J_CONTAINER` is set nowhere in the repository, so the default
always won, and the default names the production container. Every driver
connection honoured the env override while those two files DETACH DELETEd the
corpus on the real instance. The suite had TWO ways to name one server, and the
one nobody was watching pointed at production.

So the fix is not "teach the second transport about isolation" -- that adds a
special case to a class of bug instead of removing the class. The fix is that
there is exactly ONE transport, and it resolves its target from `writ.config`
and from nothing else. That is what this module is. A single env assignment in
`tests/conftest.py` then moves the entire suite, including subprocesses, which
inherit `os.environ` and re-resolve through the same config path.

Do not add a second way to pick a server here. `connection()` reading
`writ.config` is the whole property that makes `pytest_sessionstart`'s
one-time preflight cover every transport at once: an in-process driver and a
`writ ...` subprocess are checked by the same read because they perform the
same read.

MODULE TOP LEVEL IS STDLIB ONLY. `tests/conftest.py` imports
`apply_isolation_env` at ITS module top level, before pytest imports a single
test module, so this import must not pull in the neo4j driver (or fail on a
machine that has not installed it). Every `writ.*` import lives inside a
function.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

# --------------------------------------------------------------------------
# The disposable instance.
# --------------------------------------------------------------------------
# Neo4j Community serves exactly one database per server, so "use a scratch
# database" does not exist: isolation can only be a separate INSTANCE on a
# different bolt port. 7688 is that port, and the port is the load-bearing
# part -- writ/graph/db/_safety.py identifies an instance by (host, port), so
# a differing port is the only thing that makes `full_wipe_allowed` true.
# The same number is published by scripts/test-graph.sh; the two halves are
# compared by a parity test (tests/test_cycle8_graph_isolation.py) because
# nothing else would notice them drifting apart.
ISOLATED_NEO4J_URI = "bolt://localhost:7688"
ISOLATED_NEO4J_USER = "neo4j"
# Matches the container recipe in writ/graph/db/_safety.py::how_to_run_safely
# and in scripts/test-graph.sh (NEO4J_AUTH=neo4j/writtestpass). Forcing the
# password matters as much as forcing the URI: a local writ.toml carries the
# PRODUCTION password, so redirecting the URI alone produces an auth failure
# that reads as "Neo4j unreachable" and mass-skips instead of failing.
ISOLATED_NEO4J_PASSWORD = "writtestpass"

# The opt-out. Exactly "1", the same spelling convention as WRIT_NO_AUTOSTART
# and WRIT_TEST_GRAPH, so there is one spelling to teach and no near-miss value
# ("yes", "TRUE") that quietly reads as consent.
ISOLATION_OPT_OUT_ENV_VAR = "WRIT_TEST_NO_ISOLATION"
ISOLATION_OPT_OUT_VALUE = "1"

# The four states classify_isolation can report. Named so the preflight and its
# tests refer to one spelling rather than four string literals each.
STATE_OPTED_OUT = "opted-out"
STATE_PRODUCTION_TARGET = "production-target"
STATE_UNREACHABLE = "unreachable"
STATE_ISOLATED = "isolated"

DUMP_FILENAME = "writ-corpus.cypher"

_REPO_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------
# Pure helpers: no I/O, no driver, no graph.
# --------------------------------------------------------------------------


def isolation_opted_out(env) -> bool:
    """True when the caller explicitly asked for the pre-isolation behaviour.

    Exact-"1" only, for the reason stated on ISOLATION_OPT_OUT_VALUE: a
    tolerant comparison turns a typo into consent.
    """
    return env.get(ISOLATION_OPT_OUT_ENV_VAR, "").strip() == ISOLATION_OPT_OUT_VALUE


def apply_isolation_env(env) -> bool:
    """Point `env` at the disposable instance. Returns True when isolation is on.

    setdefault, not assignment, for the three connection settings: an outer
    environment that already names an instance wins, which is how CI points at
    its own service and how an operator keeps a scratch instance somewhere
    else. What is NOT optional is that the suite stops inheriting the
    production values by accident, which is exactly what an unset
    WRIT_NEO4J_URI resolves to (writ.toml, i.e. the interactive instance).

    WRIT_TEST_GRAPH is an assignment rather than a setdefault, and is set only
    on this path: an operator pointing at their own scratch instance still
    needs the marker that lets `clear_all` treat the connected graph as
    disposable. Setting it cannot authorize a wipe of production, because the
    other half of that permission is env-blind: `full_wipe_allowed` compares
    the connected instance against `get_production_neo4j_uri()`, which reads
    writ.toml and ignores WRIT_NEO4J_URI entirely (config.py, _safety.py).

    Opted out, this returns False having mutated NOTHING -- not even
    WRIT_TEST_GRAPH. A run that was told to leave the environment alone must
    not come back carrying a wipe permission it did not have before.
    """
    if isolation_opted_out(env):
        return False
    env.setdefault("WRIT_NEO4J_URI", ISOLATED_NEO4J_URI)
    env.setdefault("WRIT_NEO4J_USER", ISOLATED_NEO4J_USER)
    env.setdefault("WRIT_NEO4J_PASSWORD", ISOLATED_NEO4J_PASSWORD)
    env["WRIT_TEST_GRAPH"] = "1"
    return True


def classify_isolation(*, opted_out: bool, is_production: bool, reachable: bool) -> str:
    """Name the isolation state: the whole session-start decision, with no I/O.

    Returns exactly one of STATE_OPTED_OUT, STATE_PRODUCTION_TARGET,
    STATE_UNREACHABLE or STATE_ISOLATED.

    The precedence is the point, and the middle rung is the one worth stating:
    a PRODUCTION target is reported as such even when it answers. From inside
    the process a leaking run looks exactly like a healthy one -- a graph that
    responds -- so a classifier that consulted reachability first would call
    the production instance "isolated" and would still pass every other case
    here. Reachability is only ever asked about an instance already known not
    to be production.

    Keeping this pure is deliberate. The claim being tested is which SERVER a
    process talks to, and a mocked driver answers whatever the test told it to;
    the tripwire this cycle deletes proved exactly that by reading green for
    four days. So the decision lives in a function that can be checked against
    all eight input combinations with no database at all, and the three facts
    it consumes are gathered separately.
    """
    if opted_out:
        return STATE_OPTED_OUT
    if is_production:
        return STATE_PRODUCTION_TARGET
    if not reachable:
        return STATE_UNREACHABLE
    return STATE_ISOLATED


def isolation_refusal_message(resolved_uri: str, counts=None) -> str:
    """The one refusal text, shared by all three session-start refusals.

    One helper rather than three messages at three raise sites: the reasons
    differ (the target is production / it does not answer / the replay left it
    incomplete) but the remedy is identical, and three copies of a remedy is
    how two of them go stale. `counts` is supplied only for the incomplete
    case, and only then does the message report a census -- the other two
    refusals never read one, so they must not print one they do not have.
    """
    lines = [
        "Refusing to start: this run has no isolated Neo4j instance.",
        "",
        f"    resolved graph URI: {resolved_uri}",
        "",
        "The suite runs against its OWN disposable Neo4j instance, so a fixture that",
        "wipes the graph cannot destroy the interactive corpus or the runtime records",
        "(memories, decisions, file changes, commits) that have no file to rebuild from.",
        "That instance has to be answering, has to be a different (host, port) than the",
        "configured production one, and has to hold the complete methodology corpus",
        "before the first test runs.",
    ]
    if counts is not None:
        lines += [
            "",
            "It answered, but the corpus replay left it incomplete. Live counts by label:",
            *[f"        {label} = {n}" for label, n in sorted(counts.items())],
        ]
    lines += [
        "",
        "Pick one:",
        f"    make test-graph-up          start and warm the instance at {ISOLATED_NEO4J_URI}",
        f"    {ISOLATION_OPT_OUT_ENV_VAR}={ISOLATION_OPT_OUT_VALUE}   run against the configured"
        " graph instead",
        "",
        "This refuses rather than skipping on purpose. A suite-wide skip is cheap to",
        "produce and indistinguishable from a green run, and an empty graph reading as a",
        "skip has already masked a real regression here more than once. The opt-out",
        "restores the previous behaviour exactly -- no override, no preflight, the",
        "end-of-suite corpus restore back on -- and it does not make a whole-graph wipe",
        "safe: that still needs an instance that is not the production one.",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Target resolution. Every reader below goes through writ.config, so the
# session-start preflight validates every transport with one read.
# --------------------------------------------------------------------------


def resolved_uri() -> str:
    """The graph URI this process will actually use, however it was set.

    A one-line wrapper on purpose: it gives the preflight and the tests a
    single name for "the target", so nobody is tempted to re-derive it from
    os.environ and get a different answer than the driver does (env, then
    writ.toml, then the built-in default -- three layers, one resolver).
    """
    from writ.config import get_neo4j_uri

    return get_neo4j_uri()


def targets_production(uri: str | None = None) -> bool:
    """True when `uri` addresses the instance this install treats as production.

    Delegates to the wipe guard's own comparison (`is_production_instance`,
    by (host, port), never by URI string) so the preflight and `clear_all`
    cannot disagree about what production is. An unidentifiable URI counts as
    production, which is the safe direction and the guard's existing posture.
    """
    from writ.graph.db._safety import is_production_instance

    return is_production_instance(resolved_uri() if uri is None else uri)


def connection():
    """The one Neo4jConnection factory the suite uses. Resolves through writ.config.

    Every graph access in `tests/` arrives here, including `tests/_corpus.py`'s
    helpers. Callers close it (or use one of the wrappers below).
    """
    from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
    from writ.graph.db import Neo4jConnection

    return Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())


def count(query: str, **params) -> int:
    """Run a single-value counting query and return the integer.

    Replaces the `docker exec <container> cypher-shell --format plain` helper
    two modules used to parse an integer out of stdout. Same contract, one
    transport: the driver returns a typed value, so there is no output to parse
    and no container name that can name a different server than the URI does.
    It is also cheaper -- each former call spawned a process (roughly 100-300ms)
    and those modules make several dozen calls.
    """

    async def _q() -> int:
        db = connection()
        try:
            async with db._driver.session(database=db._database) as s:
                res = await s.run(query, **params)
                row = await res.single()
                return int(list(row.values())[0])
        finally:
            await db.close()

    return asyncio.run(_q())


def wipe_corpus() -> None:
    """Delete the corpus, sparing runtime records. Routes through clear_all().

    `clear_all`'s record-preserving default (writ/graph/db/maintenance_store.py)
    is the single source for WHAT survives a corpus wipe, so the ingest modules
    stop spelling out their own RECORD_LABELS preserve clause -- they each had a
    copy, and one of the copies was a copy of the other. No `preserve_labels`
    argument is passed here by design: an everything-wipe is a different
    operation with its own permission check, and this helper is not it.
    """

    async def _q() -> None:
        db = connection()
        try:
            await db.clear_all()
        finally:
            await db.close()

    asyncio.run(_q())


def replay_dump(root: Path | None = None) -> bool:
    """Replay the tracked `writ-corpus.cypher` into the resolved instance.

    The portable warm start: `bible/` is gitignored, so a clean checkout and a
    fresh disposable instance have none, while the dump is tracked and is
    already what CI migrates from. Shells out to `writ import-cypher` rather
    than re-implementing the replay, because that command is the canonical
    import path and it resolves its own target through `writ.config` -- the
    child inherits os.environ, so it lands on the same instance the parent
    resolved. Returns False when there is no dump to replay (not a writ
    checkout) or the command failed; the caller decides what that means.
    """
    import subprocess

    from tests._writ_cmd import WRIT_CMD_PREFIX

    base = _REPO_ROOT if root is None else Path(root)
    if not (base / DUMP_FILENAME).exists():
        return False
    try:
        result = subprocess.run(
            [*WRIT_CMD_PREFIX, "import-cypher", DUMP_FILENAME],
            cwd=str(base),
            capture_output=True,
            timeout=120,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return result.returncode == 0
