"""0.10-D: reconcile prunes graph artifacts absent from the source-of-truth.

Reconcile imports the bible then deletes live nodes/edges NOT in the oracle,
EXEMPTING graph-authored nodes and edges incident to one. This closes the
upsert-only no-prune gap (a renamed/deleted source node leaves a stale graph
artifact forever -- the 1.3a class). Destructive, so gated behind the 0.10-C
oracle pin. Each test seeds a scenario, reconciles against the real bible, and
asserts; the fixture clean-ingests for a known baseline and sweeps test seeds.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
from writ.graph.db import Neo4jConnection
from writ.graph.integrity import IntegrityChecker
from writ.graph.methodology_ingest import compute_expected_graph, ingest_path, reconcile

from tests._bible_guard import requires_bible

pytestmark = requires_bible


BIBLE = Path(__file__).resolve().parent.parent / "bible"

STALE = "ZZZ-STALE-RECONCILE-001"      # ingested, absent from bible -> pruned
GAUTH = "ZZZ-GAUTH-RECONCILE-001"      # graph-authored, absent from bible -> kept
REAL_A = "SEC-INJ-SQL-001"
REAL_B = "DOC-ONBOARD-001"


def _rule(rid: str) -> dict:
    return {
        "rule_id": rid, "domain": "Testing", "severity": "high", "scope": "slice",
        "trigger": "t", "statement": "s", "violation": "v", "pass_example": "p",
        "enforcement": "e", "rationale": "r", "last_validated": "2026-03-15",
    }


async def _edge_exists(db: Neo4jConnection, etype: str, src: str, tgt: str) -> bool:
    async with db._driver.session(database=db._database) as s:
        res = await s.run(
            f"MATCH (a)-[r:`{etype}`]->(b) "
            "WHERE (a.rule_id = $src OR a.category_id = $src) "
            "AND (b.rule_id = $tgt OR b.category_id = $tgt) RETURN count(r) AS c",
            src=src, tgt=tgt,
        )
        return (await res.single())["c"] > 0


@pytest_asyncio.fixture()
async def db_corpus():
    db = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
    try:
        async with db._driver.session(database=db._database) as s:
            await (await s.run("RETURN 1 AS ok")).consume()
    except Exception:
        await db.close()
        pytest.skip("Neo4j unreachable")
    await db.clear_all()
    await ingest_path(BIBLE, db)
    yield db
    # Sweep any surviving test seeds, then leave the full corpus intact.
    async with db._driver.session(database=db._database) as s:
        await s.run("MATCH (n) WHERE n.rule_id IN $ids DETACH DELETE n", ids=[STALE, GAUTH])
    await db.close()


class TestReconcile:
    @pytest.mark.asyncio
    async def test_deletes_stale_ingested_node(self, db_corpus: Neo4jConnection) -> None:
        await db_corpus.create_rule(_rule(STALE), source_origin="ingest")
        result = await reconcile(BIBLE, db_corpus)
        assert STALE in result["deleted_nodes"]
        assert await db_corpus.get_rule(STALE) is None

    @pytest.mark.asyncio
    async def test_keeps_graph_authored_node(self, db_corpus: Neo4jConnection) -> None:
        await db_corpus.create_rule(_rule(GAUTH), source_origin="graph-authored")
        result = await reconcile(BIBLE, db_corpus)
        assert GAUTH not in result["deleted_nodes"]
        assert await db_corpus.get_rule(GAUTH) is not None

    @pytest.mark.asyncio
    async def test_deletes_stale_edge_between_real_nodes(self, db_corpus: Neo4jConnection) -> None:
        _, oracle_edges = compute_expected_graph(BIBLE)
        assert ("COUNTERS", REAL_A, REAL_B) not in oracle_edges  # precondition
        await db_corpus.create_edge("COUNTERS", REAL_A, REAL_B)
        assert await _edge_exists(db_corpus, "COUNTERS", REAL_A, REAL_B)
        result = await reconcile(BIBLE, db_corpus)
        assert ("COUNTERS", REAL_A, REAL_B) in result["deleted_edges"]
        assert not await _edge_exists(db_corpus, "COUNTERS", REAL_A, REAL_B)

    @pytest.mark.asyncio
    async def test_keeps_edge_incident_to_graph_authored(self, db_corpus: Neo4jConnection) -> None:
        await db_corpus.create_rule(_rule(GAUTH), source_origin="graph-authored")
        await db_corpus.create_edge("RELATED_TO", GAUTH, REAL_A)
        result = await reconcile(BIBLE, db_corpus)
        assert ("RELATED_TO", GAUTH, REAL_A) not in result["deleted_edges"]
        assert await _edge_exists(db_corpus, "RELATED_TO", GAUTH, REAL_A)

    @pytest.mark.asyncio
    async def test_preserves_real_corpus_and_is_idempotent(self, db_corpus: Neo4jConnection) -> None:
        nodes, edges = compute_expected_graph(BIBLE)
        result = await reconcile(BIBLE, db_corpus)
        assert not (set(result["deleted_nodes"]) & nodes), "reconcile deleted real nodes"
        assert not (set(result["deleted_edges"]) & edges), "reconcile deleted real edges"
        # Second pass on an already-reconciled graph deletes nothing.
        result2 = await reconcile(BIBLE, db_corpus)
        assert result2["deleted_nodes"] == []
        assert result2["deleted_edges"] == []

    @pytest.mark.asyncio
    async def test_refuses_empty_oracle(self, db_corpus: Neo4jConnection, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            await reconcile(tmp_path, db_corpus)


class TestEdgeParity:
    """0.10-E: detect_edge_parity flags live/oracle edge drift in writ validate."""

    @pytest.mark.asyncio
    async def test_clean_corpus_is_in_parity(self, db_corpus: Neo4jConnection) -> None:
        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
        assert await checker.detect_edge_parity(BIBLE) is None

    @pytest.mark.asyncio
    async def test_flags_stale_edge(self, db_corpus: Neo4jConnection) -> None:
        await db_corpus.create_edge("COUNTERS", REAL_A, REAL_B)
        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
        result = await checker.detect_edge_parity(BIBLE)
        assert result is not None
        assert ("COUNTERS", REAL_A, REAL_B) in result["stale"]

    @pytest.mark.asyncio
    async def test_exempts_graph_authored_edge(self, db_corpus: Neo4jConnection) -> None:
        await db_corpus.create_rule(_rule(GAUTH), source_origin="graph-authored")
        await db_corpus.create_edge("RELATED_TO", GAUTH, REAL_A)
        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
        result = await checker.detect_edge_parity(BIBLE)
        # The graph-authored edge must NOT be reported as stale.
        stale = result["stale"] if result else []
        assert ("RELATED_TO", GAUTH, REAL_A) not in stale

    @pytest.mark.asyncio
    async def test_reconcile_restores_parity(self, db_corpus: Neo4jConnection) -> None:
        await db_corpus.create_edge("COUNTERS", REAL_A, REAL_B)
        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
        assert await checker.detect_edge_parity(BIBLE) is not None  # drift present
        await reconcile(BIBLE, db_corpus)
        assert await checker.detect_edge_parity(BIBLE) is None      # restored

    @pytest.mark.asyncio
    async def test_stale_edge_fails_run_all_checks(self, db_corpus: Neo4jConnection) -> None:
        """5.2c item 4a: a seeded stale edge must drive exit_code==1 via run_all_checks.

        GREEN-now: run_all_checks already routes edge_parity into has_issues
        (integrity.py:1330-1331). This test locks that wiring -- it closes the gap
        where test_flags_stale_edge verified the detector but never called
        run_all_checks.
        """
        await db_corpus.create_edge("COUNTERS", REAL_A, REAL_B)
        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
        findings = await checker.run_all_checks(bible_dir=BIBLE)
        assert findings["exit_code"] == 1, (
            f"exit_code should be 1 when edge_parity is truthy; "
            f"got {findings.get('exit_code')}"
        )
        assert findings["edge_parity"] is not None, (
            "findings['edge_parity'] should be non-None after seeding a stale edge"
        )
