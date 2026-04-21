"""Phase 1 deliverable 7.3: bundle cohesion + 2-hop bundle traversal tests."""
from __future__ import annotations

import pytest

from writ.retrieval.ranking import RankingWeights, compute_score
from writ.retrieval.traversal import AdjacencyCache


class TestBundleCohesionScoring:
    """compute_score accepts bundle_cohesion and applies w_bundle_cohesion."""

    def test_default_bundle_cohesion_zero(self) -> None:
        s = compute_score(
            bm25_norm=1.0, vector_norm=1.0, severity="high",
            confidence="production-validated", bundle_cohesion=0.0,
        )
        assert s > 0

    def test_bundle_cohesion_contributes_when_weight_set(self) -> None:
        weights_no_cohesion = RankingWeights(
            w_bm25=0.198, w_vector=0.594, w_severity=0.099, w_confidence=0.099,
            w_graph=0.01, w_bundle_cohesion=0.0,
        )
        weights_with_cohesion = RankingWeights(
            w_bm25=0.178, w_vector=0.574, w_severity=0.099, w_confidence=0.099,
            w_graph=0.01, w_bundle_cohesion=0.04,
        )
        base_args = dict(
            bm25_norm=0.5, vector_norm=0.5, severity="high",
            confidence="production-validated", bundle_cohesion=1.0,
        )
        s_no = compute_score(**base_args, weights=weights_no_cohesion)
        s_with = compute_score(**base_args, weights=weights_with_cohesion)
        # With cohesion weight active and bundle_cohesion=1.0, score is higher.
        assert s_with > s_no

    def test_weights_validate_with_bundle_cohesion(self) -> None:
        w = RankingWeights(
            w_bm25=0.178, w_vector=0.574, w_severity=0.099, w_confidence=0.099,
            w_graph=0.01, w_bundle_cohesion=0.04,
        )
        w.validate()  # sums to 1.0 — no exception

    def test_weights_reject_non_unit_sum(self) -> None:
        w = RankingWeights(
            w_bm25=0.198, w_vector=0.594, w_severity=0.099, w_confidence=0.099,
            w_graph=0.01, w_bundle_cohesion=0.5,
        )
        with pytest.raises(ValueError):
            w.validate()


class TestAdjacencyBundle:
    """AdjacencyCache.get_bundle supports multi-hop traversal."""

    def _cache_with_edges(self, edges: list[tuple[str, str, str]]) -> AdjacencyCache:
        cache = AdjacencyCache()
        for src, tgt, et in edges:
            cache._neighbors.setdefault(src, []).append({"rule_id": tgt, "edge_type": et, "direction": "outgoing"})
            cache._neighbors.setdefault(tgt, []).append({"rule_id": src, "edge_type": et, "direction": "incoming"})
        return cache

    def test_bundle_depth_1_returns_direct_neighbors(self) -> None:
        c = self._cache_with_edges([("A", "B", "TEACHES"), ("B", "C", "GATES")])
        bundle = c.get_bundle("A", max_depth=1)
        assert bundle == {"A", "B"}

    def test_bundle_depth_2_reaches_further(self) -> None:
        c = self._cache_with_edges([("A", "B", "TEACHES"), ("B", "C", "GATES")])
        bundle = c.get_bundle("A", max_depth=2)
        assert bundle == {"A", "B", "C"}

    def test_bundle_respects_max_depth(self) -> None:
        c = self._cache_with_edges([
            ("A", "B", "TEACHES"), ("B", "C", "GATES"), ("C", "D", "COUNTERS"),
        ])
        bundle_1 = c.get_bundle("A", max_depth=1)
        bundle_2 = c.get_bundle("A", max_depth=2)
        bundle_3 = c.get_bundle("A", max_depth=3)
        assert bundle_1 == {"A", "B"}
        assert bundle_2 == {"A", "B", "C"}
        assert bundle_3 == {"A", "B", "C", "D"}

    def test_bundle_handles_cycles(self) -> None:
        c = self._cache_with_edges([("A", "B", "TEACHES"), ("B", "A", "COUNTERS")])
        bundle = c.get_bundle("A", max_depth=5)
        assert bundle == {"A", "B"}

    def test_bundle_default_depth_is_2(self) -> None:
        c = self._cache_with_edges([
            ("A", "B", "TEACHES"), ("B", "C", "GATES"), ("C", "D", "COUNTERS"),
        ])
        assert c.get_bundle("A") == {"A", "B", "C"}

    def test_bundle_isolated_node_returns_self(self) -> None:
        c = AdjacencyCache()
        assert c.get_bundle("ISOLATED") == {"ISOLATED"}
