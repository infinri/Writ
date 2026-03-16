"""Phase 2: Infrastructure integration tests.

Tests Neo4j CRUD, Tantivy indexing, hnswlib search, and graph traversal.
Requires running Neo4j instance.
Each test is isolated with its own data (TEST-ISO-001).
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from writ.graph.db import Neo4jConnection
from writ.retrieval.embeddings import HnswlibStore
from writ.retrieval.keyword import KeywordIndex
from writ.retrieval.traversal import GraphTraverser

NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "writdevpass"


@pytest_asyncio.fixture()
async def db():
    """Provide a clean Neo4j connection for each test."""
    conn = Neo4jConnection(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    await conn.clear_all()
    yield conn
    await conn.clear_all()
    await conn.close()


def _make_rule(rule_id: str, mandatory: bool = False) -> dict:
    """Helper to build a minimal rule dict for testing."""
    return {
        "rule_id": rule_id,
        "domain": "Architecture",
        "severity": "high",
        "scope": "module",
        "trigger": f"Trigger for {rule_id}",
        "statement": f"Statement for {rule_id}",
        "violation": "Bad example.",
        "pass_example": "Good example.",
        "enforcement": "Code review.",
        "rationale": "Because correctness matters.",
        "mandatory": mandatory,
        "confidence": "production-validated",
        "evidence": "doc:original-bible",
        "staleness_window": 365,
        "last_validated": "2026-03-15",
    }


class TestNeo4jCrud:
    """Rule node CRUD against live Neo4j."""

    @pytest.mark.asyncio
    async def test_create_and_read_rule(self, db: Neo4jConnection) -> None:
        rule_data = _make_rule("ARCH-ORG-001")
        created_id = await db.create_rule(rule_data)
        assert created_id == "ARCH-ORG-001"

        fetched = await db.get_rule("ARCH-ORG-001")
        assert fetched is not None
        assert fetched["rule_id"] == "ARCH-ORG-001"
        assert fetched["domain"] == "Architecture"
        assert fetched["severity"] == "high"

    @pytest.mark.asyncio
    async def test_create_edge(self, db: Neo4jConnection) -> None:
        await db.create_rule(_make_rule("ARCH-ORG-001"))
        await db.create_rule(_make_rule("ARCH-DI-001"))
        await db.create_edge("DEPENDS_ON", "ARCH-ORG-001", "ARCH-DI-001")

        neighbors = await db.traverse_neighbors("ARCH-ORG-001", hops=1)
        neighbor_ids = [n["rule_id"] for n in neighbors]
        assert "ARCH-DI-001" in neighbor_ids

    @pytest.mark.asyncio
    async def test_merge_is_idempotent(self, db: Neo4jConnection) -> None:
        rule_data = _make_rule("ARCH-ORG-001")
        await db.create_rule(rule_data)
        await db.create_rule(rule_data)

        count = await db.count_rules()
        assert count == 1

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_none(self, db: Neo4jConnection) -> None:
        result = await db.get_rule("DOES-NOT-EXIST")
        assert result is None


class TestTraversal:
    """Graph traversal queries."""

    @pytest.mark.asyncio
    async def test_one_hop_returns_neighbor(self, db: Neo4jConnection) -> None:
        await db.create_rule(_make_rule("RULE-A-001"))
        await db.create_rule(_make_rule("RULE-B-001"))
        await db.create_edge("DEPENDS_ON", "RULE-A-001", "RULE-B-001")

        traverser = GraphTraverser(db)
        neighbors = await traverser.get_neighbors("RULE-A-001", hops=1)
        neighbor_ids = [n["rule_id"] for n in neighbors]
        assert "RULE-B-001" in neighbor_ids

    @pytest.mark.asyncio
    async def test_two_hop_returns_transitive(self, db: Neo4jConnection) -> None:
        await db.create_rule(_make_rule("RULE-A-001"))
        await db.create_rule(_make_rule("RULE-B-001"))
        await db.create_rule(_make_rule("RULE-C-001"))
        await db.create_edge("DEPENDS_ON", "RULE-A-001", "RULE-B-001")
        await db.create_edge("SUPPLEMENTS", "RULE-B-001", "RULE-C-001")

        traverser = GraphTraverser(db)
        neighbors = await traverser.get_neighbors("RULE-A-001", hops=2)
        neighbor_ids = [n["rule_id"] for n in neighbors]
        assert "RULE-C-001" in neighbor_ids

    @pytest.mark.asyncio
    async def test_traversal_returns_edge_types(self, db: Neo4jConnection) -> None:
        await db.create_rule(_make_rule("RULE-A-001"))
        await db.create_rule(_make_rule("RULE-B-001"))
        await db.create_edge("CONFLICTS_WITH", "RULE-A-001", "RULE-B-001")

        traverser = GraphTraverser(db)
        neighbors = await traverser.get_neighbors("RULE-A-001", hops=1)
        assert len(neighbors) > 0
        assert "edge_type" in neighbors[0]
        assert neighbors[0]["edge_type"] == "CONFLICTS_WITH"


class TestTantivyIndex:
    """BM25 keyword index build and query."""

    def test_build_and_query(self) -> None:
        rules = [
            _make_rule("ARCH-ORG-001"),
            _make_rule("DB-SQL-001"),
        ]
        rules[0]["trigger"] = "Controller contains SQL query"
        rules[1]["trigger"] = "Raw SQL query with positional placeholders"

        index = KeywordIndex()
        count = index.build(rules)
        assert count == 2

        results = index.search("controller SQL")
        assert len(results) > 0
        assert results[0]["rule_id"] == "ARCH-ORG-001"

    def test_mandatory_excluded(self) -> None:
        rules = [
            _make_rule("ARCH-ORG-001"),
            _make_rule("ENF-GATE-001", mandatory=True),
        ]
        rules[0]["trigger"] = "Layer separation violation"
        rules[1]["trigger"] = "Gate approval required"

        index = KeywordIndex()
        count = index.build(rules)
        assert count == 1

        results = index.search("gate approval")
        result_ids = [r["rule_id"] for r in results]
        assert "ENF-GATE-001" not in result_ids


class TestHnswlibSearch:
    """Vector search via hnswlib."""

    def test_build_and_search(self) -> None:
        store = HnswlibStore(dimensions=3)
        rule_ids = ["RULE-A-001", "RULE-B-001", "RULE-C-001"]
        vectors = [
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ]
        store.build_index(rule_ids, vectors)

        results = store.search([1.0, 0.0, 0.0], k=1)
        assert len(results) == 1
        assert results[0].rule_id == "RULE-A-001"

    def test_mandatory_excluded(self) -> None:
        """Caller excludes mandatory rules before building -- verify exclusion works."""
        non_mandatory = ["ARCH-ORG-001", "DB-SQL-001"]
        vectors = [
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
        ]
        store = HnswlibStore(dimensions=3)
        store.build_index(non_mandatory, vectors)

        results = store.search([0.5, 0.5, 0.0], k=10)
        result_ids = [r.rule_id for r in results]
        assert "ENF-GATE-001" not in result_ids
        assert len(result_ids) == 2
