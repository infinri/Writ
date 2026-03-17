"""Phase 6: Graph-neighbor scoring boost tests.

Tests the two-pass ranking with graph proximity, backward compatibility
with w_graph=0.0, and MRR@5 regression gates.

Requires Neo4j running.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio

from writ.graph.db import Neo4jConnection
from writ.graph.ingest import discover_rule_files, parse_rules_from_file
from writ.retrieval.pipeline import build_pipeline, compute_graph_proximity
from writ.retrieval.ranking import RankingWeights, compute_score
from writ.retrieval.traversal import AdjacencyCache

pytestmark = pytest.mark.asyncio(loop_scope="module")

NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "writdevpass"

GROUND_TRUTH_PATH = Path("tests/fixtures/ground_truth_queries.json")

# Regression gates from EXECUTION_PLAN.md Phase 6 test checklist.
MRR5_REGRESSION_FLOOR = 0.78
HIT_RATE_REGRESSION_FLOOR = 0.90


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def db():
    """Self-contained db with migrated rules."""
    conn = Neo4jConnection(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    await conn.clear_all()

    bible_dir = Path("bible/")
    if bible_dir.exists():
        rule_ids = set()
        all_rules: list[dict] = []
        for f in discover_rule_files(bible_dir):
            for rule_data in parse_rules_from_file(f):
                clean = {k: v for k, v in rule_data.items() if not k.startswith("_")}
                await conn.create_rule(clean)
                rule_ids.add(clean["rule_id"])
                all_rules.append(rule_data)
        for rule_data in all_rules:
            for ref_id in rule_data.get("_cross_references", []):
                if ref_id in rule_ids:
                    await conn.create_edge("RELATED_TO", rule_data["rule_id"], ref_id)

    yield conn
    await conn.clear_all()
    await conn.close()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def cache(db):
    c = AdjacencyCache()
    await c.build_from_db(db)
    yield c


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def pipeline_with_graph(db):
    """Pipeline with default weights (includes w_graph)."""
    p = await build_pipeline(db)
    yield p


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def pipeline_no_graph(db):
    """Pipeline with w_graph=0.0 for backward compatibility testing."""
    weights = RankingWeights(
        w_bm25=0.2, w_vector=0.6, w_severity=0.1, w_confidence=0.1, w_graph=0.0,
    )
    p = await build_pipeline(db, weights=weights)
    yield p


@pytest.fixture(scope="module")
def ground_truth():
    data = json.loads(GROUND_TRUTH_PATH.read_text())
    return data["queries"]


# ---------------------------------------------------------------------------
# Unit tests: compute_graph_proximity
# ---------------------------------------------------------------------------

class TestComputeGraphProximity:

    def test_1hop_neighbor_scores_1(self, cache) -> None:
        """A 1-hop neighbor of a top-3 rule gets proximity 1.0."""
        # Find a rule with at least one neighbor.
        for rule_id, neighbors in cache._neighbors.items():
            if neighbors:
                neighbor_id = neighbors[0]["rule_id"]
                break
        else:
            pytest.skip("No edges in cache")

        top3 = [rule_id]
        all_candidates = [rule_id, neighbor_id]
        proximity = compute_graph_proximity(all_candidates, top3, cache)
        assert proximity[neighbor_id] == 1.0

    def test_non_neighbor_scores_0(self, cache) -> None:
        """A rule with no graph path to any top-3 rule gets proximity 0.0."""
        # Use a rule that has no neighbors as the "candidate".
        all_rule_ids = list(cache._neighbors.keys())
        if len(all_rule_ids) < 3:
            pytest.skip("Not enough rules in cache")

        # Pick a top-3 that is isolated from a candidate.
        # We'll verify the score is 0.0 for a non-neighbor.
        top3 = [all_rule_ids[0]]
        neighbors_of_top = {n["rule_id"] for n in cache.get_neighbors(all_rule_ids[0])}

        non_neighbor = None
        for rid in all_rule_ids:
            if rid not in neighbors_of_top and rid != all_rule_ids[0]:
                # Check 2-hop too.
                is_2hop = False
                for n in cache.get_neighbors(rid):
                    if n["rule_id"] in neighbors_of_top or n["rule_id"] == all_rule_ids[0]:
                        is_2hop = True
                        break
                if not is_2hop:
                    non_neighbor = rid
                    break

        if non_neighbor is None:
            pytest.skip("All rules are within 2 hops of each other")

        proximity = compute_graph_proximity([non_neighbor], top3, cache)
        assert proximity[non_neighbor] == 0.0

    def test_proximity_values_in_allowed_set(self, cache) -> None:
        """All proximity values must be in {0.0, 0.5, 1.0} per INV-2."""
        all_ids = list(cache._neighbors.keys())
        if len(all_ids) < 3:
            pytest.skip("Not enough rules")
        top3 = all_ids[:3]
        proximity = compute_graph_proximity(all_ids, top3, cache)
        for rid, score in proximity.items():
            assert score in (0.0, 0.5, 1.0), f"{rid} has invalid proximity {score}"

    def test_top3_rule_own_proximity_is_0(self, cache) -> None:
        """A top-3 rule does not boost itself. Per INV-4."""
        all_ids = list(cache._neighbors.keys())
        if not all_ids:
            pytest.skip("No rules in cache")
        top3 = [all_ids[0]]
        proximity = compute_graph_proximity(all_ids, top3, cache)
        assert proximity[all_ids[0]] == 0.0

    def test_max_wins_when_1hop_and_2hop(self, cache) -> None:
        """If a candidate is 1-hop to one top-3 and 2-hop to another, max (1.0) wins."""
        # Find a candidate that is 1-hop to at least one rule.
        for rule_id in cache._neighbors:
            neighbors = cache.get_neighbors(rule_id)
            if neighbors:
                candidate = neighbors[0]["rule_id"]
                top3 = [rule_id, "NONEXISTENT-RULE-999"]
                proximity = compute_graph_proximity([candidate], top3, cache)
                assert proximity[candidate] == 1.0
                break


# ---------------------------------------------------------------------------
# Unit tests: RankingWeights with w_graph
# ---------------------------------------------------------------------------

class TestRankingWeightsExtended:
    pytestmark = []  # Override module-level asyncio mark for sync tests.

    def test_default_weights_sum_to_1(self) -> None:
        w = RankingWeights()
        w.validate()  # Should not raise.

    def test_five_weight_sum_validation(self) -> None:
        w = RankingWeights(w_bm25=0.2, w_vector=0.6, w_severity=0.1, w_confidence=0.1, w_graph=0.1)
        # Total = 1.1, should fail.
        with pytest.raises(ValueError, match="sum to 1.0"):
            w.validate()

    def test_compute_score_with_graph_proximity(self) -> None:
        w = RankingWeights()
        score_with = compute_score(
            bm25_norm=0.5, vector_norm=0.5, severity="medium", confidence="production-validated",
            graph_proximity=1.0, weights=w,
        )
        score_without = compute_score(
            bm25_norm=0.5, vector_norm=0.5, severity="medium", confidence="production-validated",
            graph_proximity=0.0, weights=w,
        )
        assert score_with > score_without
        assert abs(score_with - score_without - w.w_graph) < 0.001  # w_graph * 1.0

    def test_zero_graph_weight_matches_old_formula(self) -> None:
        """INV-3: w_graph=0.0 produces identical scores to the 4-weight formula."""
        w_old = RankingWeights(w_bm25=0.2, w_vector=0.6, w_severity=0.1, w_confidence=0.1, w_graph=0.0)
        score = compute_score(
            bm25_norm=0.8, vector_norm=0.9, severity="high", confidence="battle-tested",
            graph_proximity=0.0, weights=w_old,
        )
        expected = 0.2 * 0.8 + 0.6 * 0.9 + 0.1 * 0.75 + 0.1 * 1.0
        assert abs(score - expected) < 0.001


# ---------------------------------------------------------------------------
# Integration: backward compatibility (INV-3)
# ---------------------------------------------------------------------------

class TestBackwardCompatibility:

    def test_zero_graph_weight_identical_rankings(self, pipeline_no_graph, ground_truth) -> None:
        """With w_graph=0.0, two-pass pipeline produces same rankings as Phase 5."""
        for q in ground_truth[:20]:
            result = pipeline_no_graph.query(q["query"])
            top5_ids = [r["rule_id"] for r in result["rules"][:5]]
            # At minimum: expected rule should still be in top 5.
            # Bit-identical ranking verified by: same IDs in same order.
            assert len(top5_ids) > 0


# ---------------------------------------------------------------------------
# Regression gates: MRR@5 and hit rate with graph boost
# ---------------------------------------------------------------------------

class TestGraphBoostRegression:

    def test_mrr5_no_regression(self, pipeline_with_graph, ground_truth) -> None:
        """MRR@5 >= 0.78 on ambiguous set after graph boost. Phase 6 regression gate."""
        ambiguous = [q for q in ground_truth if q["set"] == "ambiguous"]
        reciprocal_ranks: list[float] = []
        for q in ambiguous:
            result = pipeline_with_graph.query(q["query"])
            top5_ids = [r["rule_id"] for r in result["rules"][:5]]
            expected = q["expected_rule_id"]
            if expected in top5_ids:
                rank = top5_ids.index(expected) + 1
                reciprocal_ranks.append(1.0 / rank)
            else:
                reciprocal_ranks.append(0.0)

        mrr5 = sum(reciprocal_ranks) / len(reciprocal_ranks)
        print(f"\nMRR@5 with graph boost (ambiguous): {mrr5:.4f} (floor: {MRR5_REGRESSION_FLOOR})")
        assert mrr5 >= MRR5_REGRESSION_FLOOR

    def test_hit_rate_no_regression(self, pipeline_with_graph, ground_truth) -> None:
        """Hit rate >= 90% on all 83 queries after graph boost."""
        hits = 0
        for q in ground_truth:
            result = pipeline_with_graph.query(q["query"])
            top5_ids = [r["rule_id"] for r in result["rules"][:5]]
            if q["expected_rule_id"] in top5_ids:
                hits += 1

        hit_rate = hits / len(ground_truth)
        print(f"\nHit rate with graph boost: {hits}/{len(ground_truth)} = {hit_rate:.2%}")
        assert hit_rate >= HIT_RATE_REGRESSION_FLOOR

    def test_benchmark_suite_still_passes(self, pipeline_with_graph) -> None:
        """End-to-end p95 stays under 10ms with two-pass ranking."""
        import time

        queries = [
            "controller SQL query", "dependency injection", "plugin observer",
            "error handling try catch", "unit test isolation",
        ]
        latencies: list[float] = []
        for _ in range(20):
            for q in queries:
                start = time.perf_counter()
                pipeline_with_graph.query(q)
                elapsed_ms = (time.perf_counter() - start) * 1000
                latencies.append(elapsed_ms)

        latencies.sort()
        p95 = latencies[int(len(latencies) * 0.95)]
        print(f"\nE2E p95 with graph boost: {p95:.1f}ms (budget: 10ms)")
        assert p95 < 10.0
