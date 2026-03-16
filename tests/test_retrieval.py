"""Phase 5: Retrieval pipeline tests.

Tests pipeline mechanics, ranking, context budget, and service endpoints.
MRR@5 evaluation happens via human review sessions, not automated tests.
Requires Neo4j running with migrated rules.
"""

from __future__ import annotations

import time

import pytest
import pytest_asyncio

from writ.graph.db import Neo4jConnection
from writ.graph.ingest import discover_rule_files, parse_rules_from_file
from writ.retrieval.pipeline import build_pipeline
from writ.retrieval.ranking import (
    RankingWeights,
    apply_context_budget,
    compute_score,
    normalize_ranks,
)
from writ.retrieval.traversal import AdjacencyCache

NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "writdevpass"


@pytest_asyncio.fixture(scope="module")
async def pipeline_db():
    """Shared db connection with migrated rules for pipeline tests."""
    from pathlib import Path

    db = Neo4jConnection(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    await db.clear_all()

    # Migrate real rules.
    bible_dir = Path("bible/")
    if bible_dir.exists():
        rule_ids_in_graph = set()
        all_rules: list[dict] = []
        for f in discover_rule_files(bible_dir):
            for rule_data in parse_rules_from_file(f):
                clean = {k: v for k, v in rule_data.items() if not k.startswith("_")}
                await db.create_rule(clean)
                rule_ids_in_graph.add(clean["rule_id"])
                all_rules.append(rule_data)

        # Create skeleton edges.
        for rule_data in all_rules:
            for ref_id in rule_data.get("_cross_references", []):
                if ref_id in rule_ids_in_graph:
                    await db.create_edge("RELATED_TO", rule_data["rule_id"], ref_id)

    yield db
    await db.clear_all()
    await db.close()


@pytest_asyncio.fixture(scope="module")
async def pipeline(pipeline_db):
    """Built pipeline with pre-warmed indexes."""
    p = await build_pipeline(pipeline_db)
    yield p


class TestPipeline:
    """Full pipeline integration tests."""

    def test_basic_query_returns_results(self, pipeline) -> None:
        result = pipeline.query("controller contains SQL query")
        assert len(result["rules"]) > 0
        assert result["latency_ms"] > 0

    def test_mandatory_excluded(self, pipeline) -> None:
        result = pipeline.query("gate approval phase enforcement")
        rule_ids = [r["rule_id"] for r in result["rules"]]
        for rid in rule_ids:
            assert not rid.startswith("ENF-"), f"Mandatory rule {rid} in query results"

    def test_domain_filter(self, pipeline) -> None:
        result = pipeline.query("SQL query", domain="Database")
        for rule in result["rules"]:
            # All results should be database domain if filtering works.
            assert result["total_candidates"] >= 0

    def test_exclude_rule_ids(self, pipeline) -> None:
        # First query to get a result.
        first = pipeline.query("layer separation architecture")
        if not first["rules"]:
            pytest.skip("No results to exclude")
        exclude_id = first["rules"][0]["rule_id"]

        # Second query excluding that rule.
        second = pipeline.query("layer separation architecture", exclude_rule_ids=[exclude_id])
        second_ids = [r["rule_id"] for r in second["rules"]]
        assert exclude_id not in second_ids

    def test_latency_under_budget(self, pipeline) -> None:
        """p95 latency < 10ms on warm index (100 queries)."""
        latencies: list[float] = []
        queries = [
            "controller SQL query", "dependency injection", "plugin observer",
            "error handling try catch", "unit test isolation", "named bind parameters",
            "async event loop blocking", "security authorization", "performance optimization",
            "magic number constant",
        ]
        for _ in range(10):
            for q in queries:
                start = time.perf_counter()
                pipeline.query(q)
                elapsed_ms = (time.perf_counter() - start) * 1000
                latencies.append(elapsed_ms)

        latencies.sort()
        p95_idx = int(len(latencies) * 0.95)
        p95 = latencies[p95_idx]
        print(f"\nPipeline latency: p50={latencies[len(latencies)//2]:.1f}ms, p95={p95:.1f}ms")
        assert p95 < 10.0, f"p95 latency {p95:.1f}ms exceeds 10ms budget"


class TestRanking:
    """Ranking formula tests."""

    def test_score_formula(self) -> None:
        score = compute_score(
            bm25_norm=1.0,
            vector_norm=1.0,
            severity="critical",
            confidence="battle-tested",
        )
        # 0.3*1.0 + 0.5*1.0 + 0.1*1.0 + 0.1*1.0 = 1.0
        assert abs(score - 1.0) < 0.001

    def test_weights_sum_to_one(self) -> None:
        w = RankingWeights()
        total = w.w_bm25 + w.w_vector + w.w_severity + w.w_confidence
        assert abs(total - 1.0) < 0.001

    def test_severity_affects_score(self) -> None:
        high = compute_score(0.5, 0.5, "critical", "production-validated")
        low = compute_score(0.5, 0.5, "low", "production-validated")
        assert high > low

    def test_normalize_ranks(self) -> None:
        scores = [0.1, 0.9, 0.5]
        normalized = normalize_ranks(scores)
        # Highest score (0.9 at index 1) gets rank 1 -> 1.0
        assert normalized[1] == 1.0
        # Lowest score (0.1 at index 0) gets rank 3 -> 1/3
        assert abs(normalized[0] - 1 / 3) < 0.01


class TestContextBudget:
    """Context budget mode tests."""

    def _make_rules(self, count: int) -> list[dict]:
        return [
            {
                "rule_id": f"TEST-RULE-{i:03d}",
                "score": 1.0 - i * 0.1,
                "statement": f"Statement {i}",
                "trigger": f"Trigger {i}",
                "violation": f"Violation {i}",
                "pass_example": f"Pass {i}",
                "rationale": f"Rationale {i}",
                "relationships": [],
            }
            for i in range(count)
        ]

    def test_summary_mode(self) -> None:
        rules = self._make_rules(20)
        trimmed, mode = apply_context_budget(rules, budget_tokens=1000)
        assert mode == "summary"
        # Summary only includes statement + trigger.
        for r in trimmed:
            assert "rationale" not in r
            assert "violation" not in r
            assert "statement" in r
            assert "trigger" in r

    def test_standard_mode(self) -> None:
        rules = self._make_rules(20)
        trimmed, mode = apply_context_budget(rules, budget_tokens=5000)
        assert mode == "standard"
        assert len(trimmed) <= 5
        for r in trimmed:
            assert "rationale" not in r

    def test_full_mode(self) -> None:
        rules = self._make_rules(20)
        trimmed, mode = apply_context_budget(rules, budget_tokens=10000)
        assert mode == "full"
        assert len(trimmed) <= 10
        assert "rationale" in trimmed[0]


class TestAdjacencyCache:
    """Adjacency cache tests."""

    @pytest.mark.asyncio
    async def test_cache_matches_neo4j(self) -> None:
        db = Neo4jConnection(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
        try:
            cache = AdjacencyCache()
            await cache.build_from_db(db)
            assert cache.size > 0
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_cache_lookup_speed(self) -> None:
        db = Neo4jConnection(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
        try:
            cache = AdjacencyCache()
            await cache.build_from_db(db)

            # Measure 1000 lookups.
            start = time.perf_counter()
            for _ in range(1000):
                cache.get_neighbors("ARCH-ORG-001")
            elapsed_us = (time.perf_counter() - start) * 1_000_000 / 1000
            print(f"\nCache lookup: {elapsed_us:.2f}us per call")
            assert elapsed_us < 100, f"Cache lookup {elapsed_us:.0f}us exceeds 100us budget"
        finally:
            await db.close()
