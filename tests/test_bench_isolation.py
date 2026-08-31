"""RED test skeleton for Wave1 Cycle6 Target 4 (TEST-ISOLATE-001).

benchmarks/bench_targets.py::TestIngestionBenchmark.test_single_rule_ingestion
creates the synthetic BENCH-INGEST-001 rule in a plain `for` loop and only
cleans it up (DETACH DELETE) AFTER the loop completes. `db.create_rule`
MERGEs on rule_id, so a raise on any iteration after the first leaves
BENCH-INGEST-001 permanently persisted in the SHARED Neo4j graph -- the
cleanup code is simply never reached.

Fix (not implemented by this skeleton): wrap the create loop in `try` and
move the cleanup DETACH DELETE into `finally`, without swallowing the
original exception (precedent: benchmarks/run_benchmarks.py, commit
1419fd6).

This test hits the LIVE shared Neo4j graph (same `db` contract as
benchmarks/bench_targets.py's module-scoped `db` fixture: skip when
unreachable or when the corpus is empty). It deliberately drives the REAL
`TestIngestionBenchmark.test_single_rule_ingestion` method (not a
reimplementation) so the RED/GREEN transition is pinned to the actual
production code path, not a parallel copy that could drift.

The benchmark module is imported as `bench_targets` (not
`from ... import TestIngestionBenchmark`) so pytest's collector does not ALSO
pick up the imported class as a second, independent test under this module.
pytest only auto-collects `Test*` classes bound directly as top-level names
in the module it is collecting; importing the parent module and referencing
`bench_targets.TestIngestionBenchmark` via attribute access (only inside the
test function body below) keeps that name out of this module's namespace,
so it is invoked exactly once, explicitly, against our controlled `db` --
never auto-collected a second time. (Verified: a top-level
`from benchmarks.bench_targets import TestIngestionBenchmark` DOES get
double-collected and double-run by pytest; this import shape does not.)

CRITICAL: because the fix is not yet implemented, the RED run of this test
WILL leak BENCH-INGEST-001 into the shared graph (that leak is exactly the
bug being pinned). The `db` fixture's teardown unconditionally DETACH
DELETEs BENCH-INGEST-001 (fixture teardown always runs), regardless of
whether the test itself passed, failed, or the production code already
cleaned up -- so this test never poisons the shared graph even while RED.

Run with: .venv/bin/python -m pytest tests/test_bench_isolation.py -v
"""
from __future__ import annotations

import pytest
import pytest_asyncio

from benchmarks import bench_targets
from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
from writ.graph.db import Neo4jConnection

pytestmark = pytest.mark.asyncio

NEO4J_URI = get_neo4j_uri()
NEO4J_USER = get_neo4j_user()
NEO4J_PASSWORD = get_neo4j_password()

BENCH_RULE_ID = "BENCH-INGEST-001"


@pytest_asyncio.fixture()
async def db():
    """Live Neo4j connection. Unreachable skips; reachable-but-empty HEALS, then FAILS.

    The stated goal of the original fixture, degrading the same way in an
    environment without a live graph, is preserved exactly: the no-graph case
    is the UNREACHABLE one and it still skips. What changed is the other
    branch. An empty corpus used to skip too, and that is the masking class
    `tests/_corpus.py::classify_corpus_state` was written to forbid: reachable
    but empty is 'empty', and tests must FAIL, because that exact state has
    already hidden a real regression here by reading as a skip.

    An empty corpus is also reachable from a green run rather than only from a
    broken environment. Modules that call `clear_all()` with no reimport sort
    before this one alphabetically (`test_abstraction_artifact_validate.py`,
    `test_authoring.py`, and `test_infrastructure.py:26-29` is the clearest
    shape of the class), so the graph can legitimately be empty by the time
    this fixture runs. So the fixture heals first, through the same
    `ensure_corpus` every other graph-dependent test uses, and only a graph
    still empty AFTER the heal is a finding. That finding fails, carrying the
    per-label census, rather than skipping with no cause attached.

    Teardown ALWAYS runs the BENCH-INGEST-001 cleanup itself, independent of
    whether the production code under test has its own (buggy or fixed)
    cleanup path: this is what keeps the RED run from poisoning the
    shared graph.
    """
    from tests._corpus import ensure_corpus, methodology_counts

    conn = Neo4jConnection(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    reachable = True
    count = 0
    try:
        count = await conn.count_rules()
    except Exception:  # noqa: BLE001 -- any connectivity failure means "skip"
        reachable = False
    if not reachable:
        await conn.close()
        pytest.skip("Neo4j unreachable")
    if count == 0:
        ensure_corpus()
        count = await conn.count_rules()
    if count == 0:
        census = methodology_counts()
        await conn.close()
        pytest.fail(
            "Neo4j is reachable and answered, but holds zero rules after "
            "ensure_corpus() was given a chance to heal it. A reachable-but-empty "
            "graph must FAIL, never skip (tests/_corpus.py::classify_corpus_state): "
            "an empty graph reading as a skip has already masked a real regression "
            f"in this suite. Live counts by label: {sorted(census.items())}"
        )

    yield conn

    try:
        async with conn._driver.session(database=conn._database) as session:
            await session.run(
                "MATCH (r:Rule {rule_id: $rule_id}) DETACH DELETE r",
                rule_id=BENCH_RULE_ID,
            )
    finally:
        await conn.close()


class TestBenchIngestionIsolation:
    """TEST-ISOLATE-001: a mid-loop failure must never leak the synthetic
    benchmark rule into the shared graph.
    """

    async def test_ingestion_cleanup_runs_on_midloop_failure(self, db) -> None:
        """RED today: `db.create_rule` is patched to raise on the 2nd call
        (mid-loop, after the 1st call has already MERGEd BENCH-INGEST-001
        into the graph). The current implementation has no try/finally
        around the create loop, so:
          (a) the exception propagates out of test_single_rule_ingestion
              (this part already "passes" today -- an uncaught raise always
              propagates), but
          (b) the cleanup DETACH DELETE below the loop is never reached, so
              BENCH-INGEST-001 is left behind in the shared graph.

        This assertion pins (b): after the fix lands (try/finally around the
        loop), the exception must STILL propagate (finally does not swallow
        it) AND the node must be gone.
        """
        call_count = {"n": 0}
        real_create_rule = db.create_rule

        async def flaky_create_rule(rule_data, *args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise RuntimeError("synthetic mid-loop failure (RED test)")
            return await real_create_rule(rule_data, *args, **kwargs)

        db.create_rule = flaky_create_rule

        bench = bench_targets.TestIngestionBenchmark()
        with pytest.raises(RuntimeError, match="synthetic mid-loop failure"):
            await bench.test_single_rule_ingestion(db)

        assert call_count["n"] >= 2, (
            "the patched create_rule must have been invoked at least twice "
            f"for this to be a genuine mid-loop failure; got {call_count['n']} calls"
        )

        query = "MATCH (r:Rule {rule_id: $rule_id}) RETURN r"
        async with db._driver.session(database=db._database) as session:
            result = await session.run(query, rule_id=BENCH_RULE_ID)
            record = await result.single()

        assert record is None, (
            "BENCH-INGEST-001 must not be leaked into the shared graph after "
            "a mid-loop failure in the ingestion benchmark's create loop"
        )
