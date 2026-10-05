"""Program item 1d: the no-good-match gate reads what survives the filter.

Before: the vector search returned the top 10 hits and the abstention gate read the best
raw cosine among them BEFORE the Stage 1 filter (session exclusions, domain, node type,
route, project scope). A hit the filter was about to drop could hold the gate open for
weak survivors, and only 10 vector candidates ever reached the filter.

After: the vector hits are filtered first, the gate reads the best SURVIVING raw cosine,
and VECTOR_CANDIDATE_LIMIT is 50. Stub stores only: no graph, no model. No ranking-quality
claim is made here; that is scripts/measure_retrieval.py against the isolated graph.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pytest

from writ.retrieval import pipeline as pipeline_mod
from writ.retrieval.embeddings import ScoredResult
from writ.retrieval.pipeline import RULE_INJECTION_ABSTENTION_THRESHOLD, RetrievalPipeline
from writ.retrieval.traversal import AdjacencyCache


class _TopKStore:
    """Honors k the way the real vector store does: the k best of a fixed hit list."""

    def __init__(self, hits: list[ScoredResult]) -> None:
        self._hits = sorted(hits, key=lambda h: h.score, reverse=True)
        self.k_requested: list[int] = []

    def search(self, vector, k: int) -> list[ScoredResult]:
        self.k_requested.append(k)
        return self._hits[:k]


def _meta(domain: str = "security") -> dict:
    return {
        "node_type": "Rule", "routes": ["semantic"], "domain": domain,
        "severity": "high", "confidence": "production-validated",
        "statement": "s.", "trigger": "t.", "project": "writ",
    }


def _pipeline(hits, metadata, bm25=None, threshold=RULE_INJECTION_ABSTENTION_THRESHOLD):
    keyword = MagicMock()
    keyword.search.return_value = list(bm25 or [])
    encoder = MagicMock()
    encoder.encode.return_value = np.zeros(384, dtype=np.float32)
    store = _TopKStore(hits)
    pipe = RetrievalPipeline(
        keyword_index=keyword, vector_store=store, adjacency_cache=AdjacencyCache(),
        embedding_model=encoder, rule_metadata=metadata, abstention_threshold=threshold,
    )
    return pipe, store


def _ids(result: dict) -> list[str]:
    return [r["rule_id"] for r in result["rules"]]


class TestGateReadsTheBestSurvivor:
    def test_an_excluded_strong_hit_does_not_hold_the_gate_open(self):
        pipe, _ = _pipeline(
            [ScoredResult(rule_id="SEC-SHOWN-001", score=0.90),
             ScoredResult(rule_id="SEC-WEAK-001", score=0.20)],
            {"SEC-SHOWN-001": _meta(), "SEC-WEAK-001": _meta()},
            bm25=[{"rule_id": "SEC-WEAK-001", "score": 3.0}],
        )
        result = pipe.query("q", exclude_rule_ids=["SEC-SHOWN-001"])
        assert result["mode"] == "abstained"
        assert result["rules"] == []
        assert result["total_candidates"] == 0
        assert result["abstain_signal"] == pytest.approx(0.20)

    def test_a_hit_outside_the_requested_domain_does_not_hold_the_gate_open(self):
        pipe, _ = _pipeline(
            [ScoredResult(rule_id="TEST-OTHER-001", score=0.90),
             ScoredResult(rule_id="SEC-WEAK-001", score=0.20)],
            {"TEST-OTHER-001": _meta(domain="testing"), "SEC-WEAK-001": _meta(domain="security")},
        )
        result = pipe.query("q", domain="security")
        assert result["mode"] == "abstained"
        assert result["abstain_signal"] == pytest.approx(0.20)

    def test_a_strong_survivor_clears_the_gate_and_is_the_signal(self):
        pipe, _ = _pipeline(
            [ScoredResult(rule_id="SEC-SHOWN-001", score=0.90),
             ScoredResult(rule_id="SEC-GOOD-001", score=0.60)],
            {"SEC-SHOWN-001": _meta(), "SEC-GOOD-001": _meta()},
        )
        result = pipe.query("q", exclude_rule_ids=["SEC-SHOWN-001"])
        assert result["mode"] != "abstained"
        assert _ids(result) == ["SEC-GOOD-001"]
        assert result["abstain_signal"] == pytest.approx(0.60)

    def test_nothing_surviving_abstains_with_zero_signal(self):
        pipe, _ = _pipeline([ScoredResult(rule_id="SEC-SHOWN-001", score=0.90)],
                            {"SEC-SHOWN-001": _meta()})
        result = pipe.query("q", exclude_rule_ids=["SEC-SHOWN-001"])
        assert result["mode"] == "abstained"
        assert result["abstain_signal"] == 0.0

    def test_gate_off_never_abstains_even_when_nothing_survives(self):
        pipe, _ = _pipeline([ScoredResult(rule_id="SEC-SHOWN-001", score=0.90)],
                            {"SEC-SHOWN-001": _meta()}, threshold=0.0)
        result = pipe.query("q", exclude_rule_ids=["SEC-SHOWN-001"])
        assert result["mode"] != "abstained"


class TestVectorCandidateLimit:
    def test_limit_is_fifty(self):
        assert pipeline_mod.VECTOR_CANDIDATE_LIMIT == 50

    def test_the_search_asks_for_the_limit(self):
        pipe, store = _pipeline([ScoredResult(rule_id="SEC-A-001", score=0.9)], {"SEC-A-001": _meta()})
        pipe.query("q")
        assert store.k_requested == [pipeline_mod.VECTOR_CANDIDATE_LIMIT]

    def test_a_good_hit_ranked_eleventh_reaches_the_filter(self):
        shown = [ScoredResult(rule_id=f"SEC-SHOWN-{i:03d}", score=0.95 - i * 0.01) for i in range(10)]
        target = ScoredResult(rule_id="SEC-TARGET-001", score=0.80)
        metadata = {h.rule_id: _meta() for h in shown + [target]}
        pipe, _ = _pipeline(shown + [target], metadata)
        result = pipe.query("q", exclude_rule_ids=[h.rule_id for h in shown])
        assert result["mode"] != "abstained"
        assert _ids(result) == ["SEC-TARGET-001"]
