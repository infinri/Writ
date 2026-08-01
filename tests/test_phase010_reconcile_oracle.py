"""0.10-B/C: the reconcile oracle and its cross-Neo4j safety pin.

compute_expected_graph(bible) is the in-memory oracle: the node-ids and edge
tuples a CLEAN ingest of the bible SHOULD produce, derived from the SAME
parse_source + derive_edges the writer uses. 0.10-D (destructive reconcile)
deletes live-minus-oracle, so the oracle MUST be trustworthy before reconcile
ships. 0.10-C pins it by clean-ingesting the bible into Neo4j and asserting the
read-back equals the oracle -- crossing the Neo4j boundary (a derive-vs-derive
compare would be tautological; this catches value coercion + create_edge's
MATCH..MERGE endpoint semantics).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
from writ.graph.db import Neo4jConnection
from writ.graph.methodology_ingest import (


    _dedupe_dual_location,
    compute_expected_graph,
    ingest_path,
)

from tests._bible_guard import requires_bible

pytestmark = requires_bible


BIBLE = Path(__file__).resolve().parent.parent / "bible"

# Same coalesce as db.get_all_edges_cross_type, for reading back a node's
# primary id regardless of label.
_ID_COALESCE = (
    "coalesce(n.rule_id, n.skill_id, n.playbook_id, n.technique_id, "
    "n.antipattern_id, n.forbidden_id, n.phase_id, n.rationalization_id, "
    "n.scenario_id, n.example_id, n.role_id, n.category_id, n.abstraction_id)"
)


@pytest_asyncio.fixture()
async def clean_ingested_db():
    """Clean-ingest the full bible into Neo4j; yield the connection.

    Leaves the graph holding the full corpus (this ingest IS the restore), so it
    is safe in the shared single-DB test environment.
    """
    db = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
    # Reachability check via await (tests._corpus.neo4j_reachable uses
    # asyncio.run, which raises inside this already-running event loop).
    try:
        async with db._driver.session(database=db._database) as s:
            res = await s.run("RETURN 1 AS ok")
            await res.consume()
    except Exception:
        await db.close()
        pytest.skip("Neo4j unreachable")
    await db.clear_all()
    await ingest_path(BIBLE, db)
    yield db
    await db.close()


async def _readback_node_ids(db: Neo4jConnection) -> set[str]:
    async with db._driver.session(database=db._database) as s:
        result = await s.run(f"MATCH (n) RETURN {_ID_COALESCE} AS id")
        return {rec["id"] async for rec in result if rec["id"] is not None}


async def _readback_edge_tuples(db: Neo4jConnection) -> set[tuple[str, str, str]]:
    rows = await db.get_all_edges_cross_type()
    return {
        (r["type"], r["source_id"], r["target_id"])
        for r in rows
        if r.get("source_id") and r.get("target_id")
    }


class TestOracleStructure:
    """0.10-B: the in-memory oracle is well-formed and deterministic."""

    def test_oracle_nonempty_and_deterministic(self) -> None:
        nodes1, edges1 = compute_expected_graph(BIBLE)
        nodes2, edges2 = compute_expected_graph(BIBLE)
        assert nodes1 and edges1, "oracle produced an empty graph"
        assert nodes1 == nodes2 and edges1 == edges2, "oracle is non-deterministic"

    def test_oracle_contains_known_nodes(self) -> None:
        nodes, _ = compute_expected_graph(BIBLE)
        assert "SEC-INJ-SQL-001" in nodes
        assert any(n.startswith("CAT-") for n in nodes), "no Category nodes in oracle"

    def test_oracle_edges_have_present_endpoints(self) -> None:
        nodes, edges = compute_expected_graph(BIBLE)
        for etype, src, tgt in edges:
            assert src in nodes and tgt in nodes, f"oracle edge to absent node: {etype} {src}->{tgt}"
        assert any(etype == "BELONGS_TO" for etype, _, _ in edges), "no BELONGS_TO edges derived"

    def test_dedup_prefers_front_matter(self) -> None:
        nodes = [
            {"node_type": "Rule", "rule_id": "X-001", "_source_format": "rule-block", "scope": "a"},
            {"node_type": "Rule", "rule_id": "X-001", "_source_format": "front-matter", "scope": "b"},
        ]
        out = _dedupe_dual_location(nodes)
        assert len(out) == 1
        assert out[0]["_source_format"] == "front-matter"


class TestOracleMatchesNeo4j:
    """0.10-C: the oracle equals what a clean ingest actually persists."""

    @pytest.mark.asyncio
    async def test_oracle_node_ids_equal_readback(self, clean_ingested_db) -> None:
        oracle_nodes, _ = compute_expected_graph(BIBLE)
        readback_nodes = await _readback_node_ids(clean_ingested_db)
        only_oracle = oracle_nodes - readback_nodes
        only_graph = readback_nodes - oracle_nodes
        assert not only_oracle and not only_graph, (
            f"node-id divergence: oracle-only={sorted(only_oracle)[:10]} "
            f"graph-only={sorted(only_graph)[:10]}"
        )

    @pytest.mark.asyncio
    async def test_oracle_edges_equal_readback(self, clean_ingested_db) -> None:
        _, oracle_edges = compute_expected_graph(BIBLE)
        readback_edges = await _readback_edge_tuples(clean_ingested_db)
        only_oracle = oracle_edges - readback_edges
        only_graph = readback_edges - oracle_edges
        assert not only_oracle and not only_graph, (
            f"edge divergence: oracle-only={sorted(only_oracle)[:10]} "
            f"graph-only={sorted(only_graph)[:10]}"
        )

    @pytest.mark.asyncio
    async def test_pin_is_non_vacuous(self, clean_ingested_db) -> None:
        # A tampered oracle must NOT equal read-back -- proves the pin detects
        # divergence rather than trivially comparing empty/equal sets.
        _, oracle_edges = compute_expected_graph(BIBLE)
        assert oracle_edges
        tampered = oracle_edges | {("RELATED_TO", "SEC-INJ-SQL-001", "DOES-NOT-EXIST-999")}
        readback = await _readback_edge_tuples(clean_ingested_db)
        assert readback != tampered, "pin cannot distinguish a tampered oracle"
