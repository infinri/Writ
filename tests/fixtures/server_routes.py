"""Shared FastAPI-route pytest fixtures for server-route tests.

Consolidates the byte-identical `client` and `isolated_cache` fixtures
formerly duplicated in the decision-memory capture and commit test files
(Wave-5 Cycle 5.3b), and owns the ONE `/always-on` driver, the ONE real-`_db`
install and the ONE mandatory-id query shared by
tests/test_always_on_methodology.py and tests/test_phase36a_mandatory_coupling.py.
These are imported EXPLICITLY into each consuming test module
(`from tests.fixtures.server_routes import client, isolated_cache`), never
registered in a root conftest, so they cannot leak into the other test files
that define their own divergent `client` fixture. For `route_db` that
discipline is load-bearing a second time: it installs a live graph connection
at a process global, and a root-conftest registration would put that within
reach of every module in the run.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

import writ.server as _server
from writ.graph.db import Neo4jConnection
from writ.graph.predicates import INJECTION_RULE_WHERE
from writ.server import app

from tests._graph import connection, resolved_uri

# The one rule eight of the twelve /always-on mode-strip cases turn on. It is
# named in the precondition because its absence is ASYMMETRIC: the two presence
# cases fail while the two absence cases pass VACUOUSLY, so a corpus problem
# would read as half a green run.
_DEBUG_DOCTRINE_RULE_ID = "ENF-PROC-DEBUG-001"

# The four populations /always-on selects from. Three are derived from
# INJECTION_RULE_WHERE (writ/graph/predicates.py:16), the same string the route
# interpolates at query.py:705, so this precondition cannot drift from what the
# tests select. MEASURED on the isolated instance 2026-09-10: 35, 2, 32, 1.
# Nothing is pinned above zero: those totals are facts about today's dump, and
# MIN_RULES belongs to tests/_corpus.py.
#
# forbidden_responses is NOT redundant with `corpus_ready`: tests/_corpus.py's
# completeness predicate is `Rule >= MIN_RULES` plus EXPECTED's four methodology
# labels (line 22), and ForbiddenResponse is in neither, so a graph with zero FRB
# nodes reads "complete", ensure_corpus no-ops, and
# test_always_on_includes_existing_rule_and_frb_nodes fails as an empty result.
_POPULATION_QUERIES = {
    "injection_rules": (
        f"MATCH (r:Rule) WHERE {INJECTION_RULE_WHERE} RETURN count(r) AS c"
    ),
    "forbidden_responses": "MATCH (n:ForbiddenResponse) RETURN count(n) AS c",
    "mandatory_rules": "MATCH (r:Rule) WHERE r.mandatory = true RETURN count(r) AS c",
    "debug_doctrine_rule": (
        "MATCH (r:Rule) WHERE r.rule_id = $rule_id AND r.always_on = true "
        "AND toLower(coalesce(r.domain, '')) = 'process' "
        "AND coalesce(r.mandatory, false) = false RETURN count(r) AS c"
    ),
}

_MANDATORY_IDS_QUERY = (
    "MATCH (r:Rule) WHERE r.mandatory = true RETURN r.rule_id AS rule_id"
)


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate WRIT_CACHE_DIR and WRIT_FRICTION_LOG for server-route tests."""
    cache_dir = tmp_path / "writ-cache"
    cache_dir.mkdir()
    log_path = tmp_path / "workflow-friction.log"
    monkeypatch.setenv("WRIT_CACHE_DIR", str(cache_dir))
    monkeypatch.setenv("WRIT_FRICTION_LOG", str(log_path))
    return tmp_path


async def require_injection_population() -> dict[str, int]:
    """Assert the corpus /always-on selects from is present. Returns the counts.

    THE REPAIR IS NOT HERE, AND IS NOT REIMPLEMENTED. `corpus_ready`
    (tests/conftest.py:643-654) has already called tests/_corpus.py::ensure_corpus,
    the one owner of "refill a wiped graph" (bible/ first, MERGE-only, the tracked
    dump as fallback), and `route_db` takes that fixture. A second
    count-and-replay path here would be the duplicate 65a47f6 corrected in
    tests/_prompt_turn.py, where a first draft called `replay_dump` directly and
    so could never take the source-of-truth path. This function is the LOUD half
    only, which is the part ensure_corpus deliberately does not do: it returns
    SILENTLY when it cannot heal and leaves the verdict to the caller.

    IT FAILS RATHER THAN SKIPS, per tests/_corpus.py's stated contract (lines
    32-50): Neo4j unreachable is the ONLY legitimate skip and that skip belongs to
    `corpus_ready`, which probes before anything else runs. Reachable-but-empty
    must FAIL, because an empty graph reading as a skip has masked a real
    regression in this repo more than once, and every absence assertion in these
    two modules passes vacuously on an empty bundle. The counts travel in the
    message so a future empty graph reports the numbers it saw rather than
    surfacing as an empty-list assertion three layers away.

    It is a COROUTINE because it runs on the test's own loop: tests/_graph.py's
    sync `count` helper ends in `asyncio.run`, MEASURED raising
    "asyncio.run() cannot be called from a running event loop" when called from an
    async fixture body.
    """
    conn = connection()
    counts: dict[str, int] = {}
    try:
        async with conn._driver.session(database=conn._database) as session:
            for name, query in _POPULATION_QUERIES.items():
                result = await session.run(query, rule_id=_DEBUG_DOCTRINE_RULE_ID)
                row = await result.single()
                counts[name] = int(row["c"])
    finally:
        await conn.close()

    absent = sorted(name for name, n in counts.items() if n == 0)
    if absent:
        pytest.fail(
            f"corpus precondition unmet for /always-on: {', '.join(absent)} absent "
            f"from the graph at {resolved_uri()}. Measured "
            f"injection_rules={counts['injection_rules']}, "
            f"forbidden_responses={counts['forbidden_responses']}, "
            f"mandatory_rules={counts['mandatory_rules']}, "
            f"debug_doctrine_rule={counts['debug_doctrine_rule']} "
            f"({_DEBUG_DOCTRINE_RULE_ID} with always_on=true, domain=process and "
            f"not mandatory). Every absence assertion over this endpoint passes "
            f"vacuously on an empty bundle, so this reports the missing "
            f"PRECONDITION instead. Restore with tests/_corpus.py::ensure_corpus() "
            f"or any pytest session (its session-start preflight rebuilds).",
            pytrace=False,
        )
    return counts


@pytest_asyncio.fixture()
async def route_db(
    corpus_ready, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Neo4jConnection]:
    """A live Neo4jConnection installed at `writ.server._db` for ONE test.

    `writ.server._db` is a module global set only inside `lifespan`
    (writ/server/__init__.py:147, 203) and it is the suite's sanctioned seam:
    tests/test_server_split_seam.py:273-292 exists to assert routes read
    `server._db` LIVE rather than as a load-time snapshot, precisely so a test can
    install one, and tests/test_memory_capture.py:944-963 already installs a real
    connection there and drives the app against it.

    FUNCTION-SCOPED FOR THE RESTORE, not for tidiness. `monkeypatch` is
    function-scoped, so a module-scoped version could not use it and would have to
    hand-roll save-and-restore around a process global that every other module in
    the run also reads; a hand-rolled restore that a failure path skips leaves a
    CLOSED connection installed at `writ.server._db`, and the next module to drive
    the app in-process gets a driver error instead of a clean
    {"error": "Database not connected."}. One connection per test is already this
    cluster's convention (test_phase36a_mandatory_coupling.py:52-56).

    The connection comes from tests/_graph.py::connection(), the suite's single
    factory, which resolves through writ.config onto the isolated instance
    tests/conftest.py forces at import. Nothing here names a URI or a credential.
    """
    await require_injection_population()
    conn = connection()
    monkeypatch.setattr(_server, "_db", conn)
    yield conn
    await conn.close()


@pytest_asyncio.fixture()
async def always_on(
    route_db: Neo4jConnection,
) -> AsyncIterator[Callable[..., Awaitable[dict[str, Any]]]]:
    """GET /always-on against the real app in-process. `await always_on(mode=...)`.

    THE ONE /always-on DRIVER. Both consuming modules carried their own nine-line
    urllib helper whose `except (URLError, OSError)` fell into `pytest.skip`
    against a port nothing answers on (tests/conftest.py:519-524 deliberately
    starts no session-wide daemon), so one predicate produced twelve tests that had
    never executed. A daemon PROCESS buys them nothing here: the endpoint reads
    `server._db` and nothing else (query.py:695), touches neither `_pipeline` nor
    `_trigger_index`, and runs two plain Cypher reads.

    The request goes over the URL and query-parameter contract rather than calling
    `always_on_bundle(mode=...)` directly, because a direct call stays green if
    `app.include_router(query_router)` (writ/server/__init__.py:329) is dropped or
    the path renamed, while every real consumer (writ-rag-inject.sh included) curls
    the URL. The lifespan is NOT run: it builds the retrieval pipeline and the
    methodology trigger index (writ/server/__init__.py:205-211) for an endpoint
    that reads neither.

    Three assertions live HERE so no caller repeats them, and each closes a way an
    absence assertion could otherwise read green:

    1. status 200. A 500 renders a JSON body with no `rules` key, which would
       satisfy every "no such id appeared" test in both modules.
    2. no `error` key. Line 695's sentinel is a 200 with an `error` key and no
       `rules`, which is exactly what a mis-wired `_db` produces.
    3. `mode_scope` echoes the mode requested (query.py:792). Without it, a request
       that silently lost its mode parameter would hand all five parametrized cases
       the universal bundle and they would agree for the wrong reason. That is the
       "the address you SET is not the one used" shape, one query parameter instead
       of one socket, closed the same way: read the scope back out of what the
       dependency itself reports.

    Callers index `data["rules"]` directly. `data.get("rules", [])` is the shape
    that lets an error payload satisfy an absence.
    """

    async def get(mode: str | None = None) -> dict[str, Any]:
        params = {} if mode is None else {"mode": mode}
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.get("/always-on", params=params)
        assert resp.status_code == 200, (
            f"GET /always-on (mode={mode!r}) returned {resp.status_code}, not 200: "
            f"{resp.text}"
        )
        data = resp.json()
        assert "error" not in data, (
            f"GET /always-on (mode={mode!r}) returned the error sentinel instead of "
            f"a bundle, so writ.server._db is not the connection this fixture "
            f"installed: {data}"
        )
        assert data["mode_scope"] == (mode or "universal"), (
            f"GET /always-on (mode={mode!r}) applied mode_scope="
            f"{data['mode_scope']!r}: the request did not carry the mode it asked "
            f"for, so this case is reading another mode's bundle"
        )
        return data

    yield get


@pytest_asyncio.fixture()
async def mandatory_rule_ids(route_db: Neo4jConnection) -> AsyncIterator[set[str]]:
    """`{rule_id}` for every Rule with `mandatory = true`, over route_db's connection.

    Replaces test_phase36a_mandatory_coupling.py's local `mandatory_ids` fixture so
    the mandatory query exists once: test_always_on_methodology.py's mode table
    needs the same set for the companion positive signal beside each absence case.
    Mandatory rules are EXEMPT from the process-domain strip (query.py:753-758),
    one predicate arm away from the row being stripped, which is why their presence
    in the SAME response proves the query ran and the strip is scoped rather than
    total.
    """
    async with route_db._driver.session(database=route_db._database) as session:
        result = await session.run(_MANDATORY_IDS_QUERY)
        ids = {record["rule_id"] async for record in result}
    yield ids
