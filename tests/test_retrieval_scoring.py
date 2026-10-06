"""Unit tests for the shared retrieval scorers (Plan commit 1, DRY-DUP-001;
KG step 0b retrieval-harness-v2).

Pins the math of `mrr_at_5`, `hit_rate_at_5`, `per_query_reciprocal_ranks`,
`per_query_ndcg10`, `ndcg_at_10`, and `paired_sign_test`, which live (or will
live) in `tests/fixtures/retrieval_scoring.py`, against a fake, duck-typed
pipeline with FIXED query output. No Neo4j, no corpus, no pytest-fixture
coupling -- these are pure functions over
`pipeline.query(text) -> {"rules": [{"rule_id": ...}, ...]}` and a list of
query dicts exposing "query" / "expected_rule_id" / "id" (or, for
`paired_sign_test`, plain lists of floats).

Per-test local imports of the not-yet-existing symbols
(`per_query_reciprocal_ranks`, `per_query_ndcg10`, `ndcg_at_10`,
`paired_sign_test`) are intentional: they do not exist in
`tests/fixtures/retrieval_scoring.py` yet, so importing inside each test
(rather than at module scope) makes the missing symbol fail that one test
with a clear ImportError instead of failing collection for the whole file.
"""
from __future__ import annotations

import math

import pytest


class FakePipeline:
    """Duck-typed stand-in for RetrievalPipeline.query(text) -> {"rules": [...]}.

    Deterministic: query text is looked up in a fixed dict of rule-id lists,
    so scorer math is pinned with no corpus, no embeddings, no Neo4j.
    """

    def __init__(self, responses: dict[str, list[str]]) -> None:
        self._responses = responses

    def query(self, text: str) -> dict:
        rule_ids = self._responses.get(text, [])
        return {"rules": [{"rule_id": rid} for rid in rule_ids]}


# ---------------------------------------------------------------------------
# mrr_at_5
# ---------------------------------------------------------------------------

class TestMrrAt5:

    def test_expected_at_rank1_scores_full_reciprocal_rank(self) -> None:
        from tests.fixtures.retrieval_scoring import mrr_at_5  # noqa: PLC0415

        pipeline = FakePipeline({"q-rank1": ["RULE-A", "RULE-B", "RULE-C"]})
        queries = [{"query": "q-rank1", "expected_rule_id": "RULE-A", "id": "Q1"}]

        mrr5, miss_ids = mrr_at_5(pipeline, queries)

        assert mrr5 == pytest.approx(1.0, abs=1e-9)
        assert miss_ids == []

    def test_expected_at_rank3_scores_one_third(self) -> None:
        from tests.fixtures.retrieval_scoring import mrr_at_5  # noqa: PLC0415

        pipeline = FakePipeline(
            {"q-rank3": ["RULE-X", "RULE-Y", "RULE-Z", "RULE-W", "RULE-V"]}
        )
        queries = [{"query": "q-rank3", "expected_rule_id": "RULE-Z", "id": "Q2"}]

        mrr5, miss_ids = mrr_at_5(pipeline, queries)

        assert mrr5 == pytest.approx(1.0 / 3.0, abs=1e-9)
        assert miss_ids == []

    def test_expected_absent_from_top5_scores_zero_and_is_a_miss(self) -> None:
        from tests.fixtures.retrieval_scoring import mrr_at_5  # noqa: PLC0415

        pipeline = FakePipeline(
            {"q-miss": ["RULE-1", "RULE-2", "RULE-3", "RULE-4", "RULE-5"]}
        )
        queries = [{"query": "q-miss", "expected_rule_id": "RULE-MISSING", "id": "Q3"}]

        mrr5, miss_ids = mrr_at_5(pipeline, queries)

        assert mrr5 == pytest.approx(0.0, abs=1e-9)
        assert miss_ids == ["Q3"]

    def test_expected_at_rank6_counts_as_a_miss_not_a_hit(self) -> None:
        """Only the top-5 are scored; rank 6 must NOT count as a hit."""
        from tests.fixtures.retrieval_scoring import mrr_at_5  # noqa: PLC0415

        pipeline = FakePipeline({"q-rank6": ["R1", "R2", "R3", "R4", "R5", "R6"]})
        queries = [{"query": "q-rank6", "expected_rule_id": "R6", "id": "Q4"}]

        mrr5, miss_ids = mrr_at_5(pipeline, queries)

        assert mrr5 == pytest.approx(0.0, abs=1e-9)
        assert miss_ids == ["Q4"]

    def test_mixed_queries_mean_and_exact_miss_ids(self) -> None:
        """MRR@5 is the MEAN reciprocal rank over the whole query list."""
        from tests.fixtures.retrieval_scoring import mrr_at_5  # noqa: PLC0415

        pipeline = FakePipeline(
            {
                "q-rank1": ["RULE-A", "RULE-B", "RULE-C"],
                "q-rank3": ["RULE-X", "RULE-Y", "RULE-Z", "RULE-W", "RULE-V"],
                "q-miss": ["RULE-1", "RULE-2", "RULE-3", "RULE-4", "RULE-5"],
                "q-rank6": ["R1", "R2", "R3", "R4", "R5", "R6"],
            }
        )
        queries = [
            {"query": "q-rank1", "expected_rule_id": "RULE-A", "id": "Q1"},
            {"query": "q-rank3", "expected_rule_id": "RULE-Z", "id": "Q2"},
            {"query": "q-miss", "expected_rule_id": "RULE-MISSING", "id": "Q3"},
            {"query": "q-rank6", "expected_rule_id": "R6", "id": "Q4"},
        ]

        mrr5, miss_ids = mrr_at_5(pipeline, queries)

        # (1.0 + 1/3 + 0.0 + 0.0) / 4 == 1/3
        assert mrr5 == pytest.approx(1.0 / 3.0, abs=1e-9)
        assert miss_ids == ["Q3", "Q4"]


# ---------------------------------------------------------------------------
# hit_rate_at_5
# ---------------------------------------------------------------------------

class TestHitRateAt5:

    def test_expected_in_top5_counts_as_a_hit(self) -> None:
        from tests.fixtures.retrieval_scoring import hit_rate_at_5  # noqa: PLC0415

        pipeline = FakePipeline({"q-rank3": ["RULE-X", "RULE-Y", "RULE-Z"]})
        queries = [{"query": "q-rank3", "expected_rule_id": "RULE-Z", "id": "Q2"}]

        hit_rate, miss_ids = hit_rate_at_5(pipeline, queries)

        assert hit_rate == pytest.approx(1.0, abs=1e-9)
        assert miss_ids == []

    def test_expected_at_rank6_is_a_miss(self) -> None:
        """Only the top-5 are checked for presence; rank 6 does not count."""
        from tests.fixtures.retrieval_scoring import hit_rate_at_5  # noqa: PLC0415

        pipeline = FakePipeline({"q-rank6": ["R1", "R2", "R3", "R4", "R5", "R6"]})
        queries = [{"query": "q-rank6", "expected_rule_id": "R6", "id": "Q4"}]

        hit_rate, miss_ids = hit_rate_at_5(pipeline, queries)

        assert hit_rate == pytest.approx(0.0, abs=1e-9)
        assert miss_ids == ["Q4"]

    def test_mixed_queries_exact_hit_rate_and_miss_ids(self) -> None:
        from tests.fixtures.retrieval_scoring import hit_rate_at_5  # noqa: PLC0415

        pipeline = FakePipeline(
            {
                "q-rank1": ["RULE-A", "RULE-B", "RULE-C"],
                "q-rank3": ["RULE-X", "RULE-Y", "RULE-Z", "RULE-W", "RULE-V"],
                "q-miss": ["RULE-1", "RULE-2", "RULE-3", "RULE-4", "RULE-5"],
                "q-rank6": ["R1", "R2", "R3", "R4", "R5", "R6"],
            }
        )
        queries = [
            {"query": "q-rank1", "expected_rule_id": "RULE-A", "id": "Q1"},
            {"query": "q-rank3", "expected_rule_id": "RULE-Z", "id": "Q2"},
            {"query": "q-miss", "expected_rule_id": "RULE-MISSING", "id": "Q3"},
            {"query": "q-rank6", "expected_rule_id": "R6", "id": "Q4"},
        ]

        hit_rate, miss_ids = hit_rate_at_5(pipeline, queries)

        assert hit_rate == pytest.approx(0.5, abs=1e-9)
        assert miss_ids == ["Q3", "Q4"]


# ---------------------------------------------------------------------------
# per_query_reciprocal_ranks (KG step 0b -- does not exist yet)
# ---------------------------------------------------------------------------

class TestPerQueryReciprocalRanks:

    def test_rank1_scores_full_reciprocal_rank(self) -> None:
        from tests.fixtures.retrieval_scoring import (  # noqa: PLC0415
            per_query_reciprocal_ranks,
        )

        pipeline = FakePipeline({"q-rank1": ["RULE-A", "RULE-B", "RULE-C"]})
        queries = [{"query": "q-rank1", "expected_rule_id": "RULE-A", "id": "Q1"}]

        rrs = per_query_reciprocal_ranks(pipeline, queries)

        assert rrs == pytest.approx([1.0], abs=1e-9)

    def test_rank3_scores_one_third(self) -> None:
        from tests.fixtures.retrieval_scoring import (  # noqa: PLC0415
            per_query_reciprocal_ranks,
        )

        pipeline = FakePipeline(
            {"q-rank3": ["RULE-X", "RULE-Y", "RULE-Z", "RULE-W", "RULE-V"]}
        )
        queries = [{"query": "q-rank3", "expected_rule_id": "RULE-Z", "id": "Q2"}]

        rrs = per_query_reciprocal_ranks(pipeline, queries)

        assert rrs == pytest.approx([1.0 / 3.0], abs=1e-9)

    def test_rank5_scores_one_fifth(self) -> None:
        from tests.fixtures.retrieval_scoring import (  # noqa: PLC0415
            per_query_reciprocal_ranks,
        )

        pipeline = FakePipeline({"q-rank5": ["R1", "R2", "R3", "R4", "R5"]})
        queries = [{"query": "q-rank5", "expected_rule_id": "R5", "id": "Q3"}]

        rrs = per_query_reciprocal_ranks(pipeline, queries)

        assert rrs == pytest.approx([0.2], abs=1e-9)

    def test_rank6_is_outside_top5_and_scores_zero(self) -> None:
        from tests.fixtures.retrieval_scoring import (  # noqa: PLC0415
            per_query_reciprocal_ranks,
        )

        pipeline = FakePipeline({"q-rank6": ["R1", "R2", "R3", "R4", "R5", "R6"]})
        queries = [{"query": "q-rank6", "expected_rule_id": "R6", "id": "Q4"}]

        rrs = per_query_reciprocal_ranks(pipeline, queries)

        assert rrs == pytest.approx([0.0], abs=1e-9)

    def test_absent_scores_zero(self) -> None:
        from tests.fixtures.retrieval_scoring import (  # noqa: PLC0415
            per_query_reciprocal_ranks,
        )

        pipeline = FakePipeline(
            {"q-miss": ["RULE-1", "RULE-2", "RULE-3", "RULE-4", "RULE-5"]}
        )
        queries = [{"query": "q-miss", "expected_rule_id": "RULE-MISSING", "id": "Q5"}]

        rrs = per_query_reciprocal_ranks(pipeline, queries)

        assert rrs == pytest.approx([0.0], abs=1e-9)

    def test_returns_one_entry_per_query_in_order(self) -> None:
        from tests.fixtures.retrieval_scoring import (  # noqa: PLC0415
            per_query_reciprocal_ranks,
        )

        pipeline = FakePipeline(
            {
                "q-rank1": ["RULE-A", "RULE-B", "RULE-C"],
                "q-rank3": ["RULE-X", "RULE-Y", "RULE-Z", "RULE-W", "RULE-V"],
                "q-miss": ["RULE-1", "RULE-2", "RULE-3", "RULE-4", "RULE-5"],
            }
        )
        queries = [
            {"query": "q-rank1", "expected_rule_id": "RULE-A", "id": "Q1"},
            {"query": "q-rank3", "expected_rule_id": "RULE-Z", "id": "Q2"},
            {"query": "q-miss", "expected_rule_id": "RULE-MISSING", "id": "Q3"},
        ]

        rrs = per_query_reciprocal_ranks(pipeline, queries)

        assert len(rrs) == len(queries)
        assert rrs == pytest.approx([1.0, 1.0 / 3.0, 0.0], abs=1e-9)


# ---------------------------------------------------------------------------
# per_query_ndcg10 (KG step 0b -- does not exist yet)
# ---------------------------------------------------------------------------

class TestPerQueryNdcg10:

    def test_rank1_scores_1_0(self) -> None:
        from tests.fixtures.retrieval_scoring import per_query_ndcg10  # noqa: PLC0415

        pipeline = FakePipeline({"q-rank1": ["RULE-A", "RULE-B", "RULE-C"]})
        queries = [{"query": "q-rank1", "expected_rule_id": "RULE-A", "id": "Q1"}]

        values = per_query_ndcg10(pipeline, queries)

        assert values == pytest.approx([1.0], abs=1e-9)

    def test_rank2_scores_1_over_log2_3(self) -> None:
        from tests.fixtures.retrieval_scoring import per_query_ndcg10  # noqa: PLC0415

        pipeline = FakePipeline({"q-rank2": ["RULE-A", "RULE-B"]})
        queries = [{"query": "q-rank2", "expected_rule_id": "RULE-B", "id": "Q2"}]

        values = per_query_ndcg10(pipeline, queries)

        assert values == pytest.approx([1.0 / math.log2(3)], abs=1e-9)
        # ~0.6309297536, per plan
        assert values[0] == pytest.approx(0.6309297536, abs=1e-9)

    def test_rank5_scores_1_over_log2_6(self) -> None:
        from tests.fixtures.retrieval_scoring import per_query_ndcg10  # noqa: PLC0415

        pipeline = FakePipeline({"q-rank5": ["R1", "R2", "R3", "R4", "R5"]})
        queries = [{"query": "q-rank5", "expected_rule_id": "R5", "id": "Q3"}]

        values = per_query_ndcg10(pipeline, queries)

        assert values == pytest.approx([1.0 / math.log2(6)], abs=1e-9)
        # ~0.3868528072, per plan
        assert values[0] == pytest.approx(0.3868528072, abs=1e-9)

    def test_rank10_scores_1_over_log2_11(self) -> None:
        from tests.fixtures.retrieval_scoring import per_query_ndcg10  # noqa: PLC0415

        ids = [f"R{i}" for i in range(1, 11)]
        pipeline = FakePipeline({"q-rank10": ids})
        queries = [{"query": "q-rank10", "expected_rule_id": "R10", "id": "Q4"}]

        values = per_query_ndcg10(pipeline, queries)

        assert values == pytest.approx([1.0 / math.log2(11)], abs=1e-9)
        # ~0.2890648264, per plan
        assert values[0] == pytest.approx(0.2890648264, abs=1e-9)

    def test_rank11_is_outside_top10_and_scores_zero(self) -> None:
        from tests.fixtures.retrieval_scoring import per_query_ndcg10  # noqa: PLC0415

        ids = [f"R{i}" for i in range(1, 12)]
        pipeline = FakePipeline({"q-rank11": ids})
        queries = [{"query": "q-rank11", "expected_rule_id": "R11", "id": "Q5"}]

        values = per_query_ndcg10(pipeline, queries)

        assert values == pytest.approx([0.0], abs=1e-9)

    def test_absent_scores_zero(self) -> None:
        from tests.fixtures.retrieval_scoring import per_query_ndcg10  # noqa: PLC0415

        pipeline = FakePipeline(
            {"q-miss": ["RULE-1", "RULE-2", "RULE-3", "RULE-4", "RULE-5"]}
        )
        queries = [{"query": "q-miss", "expected_rule_id": "RULE-MISSING", "id": "Q6"}]

        values = per_query_ndcg10(pipeline, queries)

        assert values == pytest.approx([0.0], abs=1e-9)


# ---------------------------------------------------------------------------
# ndcg_at_10 (KG step 0b -- does not exist yet)
# ---------------------------------------------------------------------------

class TestNdcgAt10:

    def test_gold_at_rank1_scores_full_ndcg(self) -> None:
        from tests.fixtures.retrieval_scoring import ndcg_at_10  # noqa: PLC0415

        pipeline = FakePipeline({"q-rank1": ["RULE-A", "RULE-B", "RULE-C"]})
        queries = [{"query": "q-rank1", "expected_rule_id": "RULE-A", "id": "Q1"}]

        ndcg10, miss_ids = ndcg_at_10(pipeline, queries)

        assert ndcg10 == pytest.approx(1.0, abs=1e-9)
        assert miss_ids == []

    def test_gold_at_rank2_matches_log2_discount(self) -> None:
        from tests.fixtures.retrieval_scoring import ndcg_at_10  # noqa: PLC0415

        pipeline = FakePipeline({"q-rank2": ["RULE-A", "RULE-B"]})
        queries = [{"query": "q-rank2", "expected_rule_id": "RULE-B", "id": "Q2"}]

        ndcg10, miss_ids = ndcg_at_10(pipeline, queries)

        assert ndcg10 == pytest.approx(1.0 / math.log2(3), abs=1e-9)
        assert miss_ids == []

    def test_gold_at_rank5_matches_log2_discount(self) -> None:
        from tests.fixtures.retrieval_scoring import ndcg_at_10  # noqa: PLC0415

        pipeline = FakePipeline({"q-rank5": ["R1", "R2", "R3", "R4", "R5"]})
        queries = [{"query": "q-rank5", "expected_rule_id": "R5", "id": "Q3"}]

        ndcg10, miss_ids = ndcg_at_10(pipeline, queries)

        assert ndcg10 == pytest.approx(1.0 / math.log2(6), abs=1e-9)
        assert miss_ids == []

    def test_gold_at_rank10_matches_log2_discount(self) -> None:
        from tests.fixtures.retrieval_scoring import ndcg_at_10  # noqa: PLC0415

        ids = [f"R{i}" for i in range(1, 11)]
        pipeline = FakePipeline({"q-rank10": ids})
        queries = [{"query": "q-rank10", "expected_rule_id": "R10", "id": "Q4"}]

        ndcg10, miss_ids = ndcg_at_10(pipeline, queries)

        assert ndcg10 == pytest.approx(1.0 / math.log2(11), abs=1e-9)
        assert miss_ids == []

    def test_gold_at_rank11_scores_zero_and_is_a_miss(self) -> None:
        from tests.fixtures.retrieval_scoring import ndcg_at_10  # noqa: PLC0415

        ids = [f"R{i}" for i in range(1, 12)]
        pipeline = FakePipeline({"q-rank11": ids})
        queries = [{"query": "q-rank11", "expected_rule_id": "R11", "id": "Q5"}]

        ndcg10, miss_ids = ndcg_at_10(pipeline, queries)

        assert ndcg10 == pytest.approx(0.0, abs=1e-9)
        assert miss_ids == ["Q5"]

    def test_gold_absent_scores_zero_and_is_a_miss(self) -> None:
        from tests.fixtures.retrieval_scoring import ndcg_at_10  # noqa: PLC0415

        pipeline = FakePipeline(
            {"q-miss": ["RULE-1", "RULE-2", "RULE-3", "RULE-4", "RULE-5"]}
        )
        queries = [{"query": "q-miss", "expected_rule_id": "RULE-MISSING", "id": "Q6"}]

        ndcg10, miss_ids = ndcg_at_10(pipeline, queries)

        assert ndcg10 == pytest.approx(0.0, abs=1e-9)
        assert miss_ids == ["Q6"]

    def test_mixed_queries_exact_mean_and_miss_ids(self) -> None:
        """ndcg_at_10 is the MEAN per-query nDCG@10 over the whole query list."""
        from tests.fixtures.retrieval_scoring import ndcg_at_10  # noqa: PLC0415

        pipeline = FakePipeline(
            {
                "q-rank1": ["RULE-A", "RULE-B", "RULE-C"],
                "q-rank5": ["R1", "R2", "R3", "R4", "R5"],
                "q-rank11": [f"X{i}" for i in range(1, 12)],
                "q-miss": ["RULE-1", "RULE-2", "RULE-3", "RULE-4", "RULE-5"],
            }
        )
        queries = [
            {"query": "q-rank1", "expected_rule_id": "RULE-A", "id": "Q1"},
            {"query": "q-rank5", "expected_rule_id": "R5", "id": "Q2"},
            {"query": "q-rank11", "expected_rule_id": "X11", "id": "Q3"},
            {"query": "q-miss", "expected_rule_id": "RULE-MISSING", "id": "Q4"},
        ]

        ndcg10, miss_ids = ndcg_at_10(pipeline, queries)

        expected_mean = (1.0 + (1.0 / math.log2(6)) + 0.0 + 0.0) / 4.0
        assert ndcg10 == pytest.approx(expected_mean, abs=1e-9)
        assert miss_ids == ["Q3", "Q4"]

    def test_empty_queries_returns_zero_and_empty_miss_ids(self) -> None:
        from tests.fixtures.retrieval_scoring import ndcg_at_10  # noqa: PLC0415

        pipeline = FakePipeline({})

        ndcg10, miss_ids = ndcg_at_10(pipeline, [])

        assert ndcg10 == pytest.approx(0.0, abs=1e-9)
        assert miss_ids == []


# ---------------------------------------------------------------------------
# paired_sign_test (KG step 0b -- does not exist yet)
# ---------------------------------------------------------------------------

class TestPairedSignTest:

    def test_seven_positive_two_negative_matches_exact_binomial_p(self) -> None:
        from tests.fixtures.retrieval_scoring import paired_sign_test  # noqa: PLC0415

        # 7 wins for a, 2 wins for b, 3 exact ties (dropped).
        scores_a = [1.0] * 7 + [0.0] * 2 + [0.5] * 3
        scores_b = [0.0] * 7 + [1.0] * 2 + [0.5] * 3

        n_pos, n_neg, p_value = paired_sign_test(scores_a, scores_b)

        # n = 9, k_min = 2: p = min(1, 2*(C(9,0)+C(9,1)+C(9,2))*0.5**9)
        #   = min(1, 2*46/512) = 92/512 = 0.1796875
        assert n_pos == 7
        assert n_neg == 2
        assert p_value == pytest.approx(92.0 / 512.0, abs=1e-9)

    def test_all_ties_returns_zero_zero_one(self) -> None:
        from tests.fixtures.retrieval_scoring import paired_sign_test  # noqa: PLC0415

        scores_a = [0.5, 0.5, 0.5]
        scores_b = [0.5, 0.5, 0.5]

        n_pos, n_neg, p_value = paired_sign_test(scores_a, scores_b)

        assert (n_pos, n_neg) == (0, 0)
        assert p_value == pytest.approx(1.0, abs=1e-9)

    def test_empty_lists_returns_zero_zero_one(self) -> None:
        from tests.fixtures.retrieval_scoring import paired_sign_test  # noqa: PLC0415

        n_pos, n_neg, p_value = paired_sign_test([], [])

        assert (n_pos, n_neg) == (0, 0)
        assert p_value == pytest.approx(1.0, abs=1e-9)

    def test_swapping_arguments_swaps_counts_and_preserves_p_value(self) -> None:
        from tests.fixtures.retrieval_scoring import paired_sign_test  # noqa: PLC0415

        scores_a = [1.0] * 7 + [0.0] * 2 + [0.5] * 3
        scores_b = [0.0] * 7 + [1.0] * 2 + [0.5] * 3

        n_pos_ab, n_neg_ab, p_ab = paired_sign_test(scores_a, scores_b)
        n_pos_ba, n_neg_ba, p_ba = paired_sign_test(scores_b, scores_a)

        assert (n_pos_ba, n_neg_ba) == (n_neg_ab, n_pos_ab)
        assert p_ba == pytest.approx(p_ab, abs=1e-9)

    def test_mismatched_lengths_raises_value_error(self) -> None:
        from tests.fixtures.retrieval_scoring import paired_sign_test  # noqa: PLC0415

        with pytest.raises(ValueError):
            paired_sign_test([1.0, 2.0], [1.0])


# ---------------------------------------------------------------------------
# Import-safety / no pytest-fixture coupling
# ---------------------------------------------------------------------------

class TestScorersAreFixtureFree:

    def test_both_scorers_run_with_zero_pytest_fixtures(self) -> None:
        """Both scorers are plain functions over a duck-typed pipeline + a
        query list -- no pytest fixture, database, or corpus dependency, so
        they are import-safe from both tests/ and benchmarks/."""
        from tests.fixtures.retrieval_scoring import (  # noqa: PLC0415
            hit_rate_at_5,
            mrr_at_5,
        )

        pipeline = FakePipeline({"solo": ["RULE-A"]})
        queries = [{"query": "solo", "expected_rule_id": "RULE-A", "id": "SOLO"}]

        mrr5, mrr_misses = mrr_at_5(pipeline, queries)
        hit_rate, hit_misses = hit_rate_at_5(pipeline, queries)

        assert mrr5 == pytest.approx(1.0, abs=1e-9)
        assert mrr_misses == []
        assert hit_rate == pytest.approx(1.0, abs=1e-9)
        assert hit_misses == []

    def test_new_metrics_run_with_zero_pytest_fixtures(self) -> None:
        """per_query_reciprocal_ranks, per_query_ndcg10, ndcg_at_10, and
        paired_sign_test are all plain functions over a duck-typed pipeline
        (or plain float lists) -- no pytest fixture, database, or corpus
        dependency, so they are import-safe from tests/, benchmarks/, and
        scripts/."""
        from tests.fixtures.retrieval_scoring import (  # noqa: PLC0415
            ndcg_at_10,
            paired_sign_test,
            per_query_ndcg10,
            per_query_reciprocal_ranks,
        )

        pipeline = FakePipeline({"solo": ["RULE-A"]})
        queries = [{"query": "solo", "expected_rule_id": "RULE-A", "id": "SOLO"}]

        rrs = per_query_reciprocal_ranks(pipeline, queries)
        ndcgs = per_query_ndcg10(pipeline, queries)
        ndcg10, miss_ids = ndcg_at_10(pipeline, queries)
        n_pos, n_neg, p_value = paired_sign_test([1.0], [0.0])

        assert rrs == pytest.approx([1.0], abs=1e-9)
        assert ndcgs == pytest.approx([1.0], abs=1e-9)
        assert ndcg10 == pytest.approx(1.0, abs=1e-9)
        assert miss_ids == []
        assert (n_pos, n_neg) == (1, 0)
        assert p_value == pytest.approx(1.0, abs=1e-9)

# ---------------------------------------------------------------------------
# split_by_channel / routed_target_findings (moved from benchmarks/bench_targets.py)
# ---------------------------------------------------------------------------

ROUTES = {"R-SEM": ["semantic"], "R-STATE": ["state"], "M-SEM": ["semantic"],
          "R-BOTH": ["semantic", "state"], "R-EMPTY": []}
CHANNEL_GOLD = [
    {"id": "Q1", "expected_rule_id": "M-SEM"},
    {"id": "Q2", "expected_rule_id": "R-STATE"},
    {"id": "Q3", "expected_rule_id": "R-BOTH"},
    {"id": "Q4", "expected_rule_id": "R-NONE"},
    {"id": "Q5", "expected_rule_id": "R-SEM"},
    {"id": "Q6", "expected_rule_id": "R-EMPTY"},
]


class TestSplitByChannel:

    def test_partition(self) -> None:
        from tests.fixtures.retrieval_scoring import split_by_channel  # noqa: PLC0415

        eligible, always_on, routed = split_by_channel(CHANNEL_GOLD, {"M-SEM"}, ROUTES)
        assert [q["id"] for q in eligible] == ["Q3", "Q4", "Q5"]
        assert [q["id"] for q in always_on] == ["Q1"]
        assert [q["id"] for q in routed] == ["Q2", "Q6"]

    def test_no_route_data_makes_every_non_mandatory_target_eligible(self) -> None:
        from tests.fixtures.retrieval_scoring import split_by_channel  # noqa: PLC0415

        eligible, always_on, routed = split_by_channel(CHANNEL_GOLD, set(), {})
        assert [q["id"] for q in eligible] == [q["id"] for q in CHANNEL_GOLD]
        assert always_on == [] and routed == []


class _FakeResult:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for row in self._rows:
            yield row


class _FakeSession:
    """Answers the BELONGS_TO count query from a fixed {rule_id: edge_count} map; a rule
    absent from the map has no node, so it yields no row (the real MATCH behaviour)."""

    def __init__(self, edges: dict[str, int]) -> None:
        self._edges = edges
        self.params: list[dict] = []

    async def run(self, query: str, **params) -> _FakeResult:
        self.params.append(params)
        return _FakeResult([{"rid": rid, "edges": self._edges[rid]}
                            for rid in params["ids"] if rid in self._edges])


class TestRoutedTargetFindings:

    @pytest.mark.asyncio
    async def test_orphans_and_channelless_are_named(self) -> None:
        from tests.fixtures.retrieval_scoring import routed_target_findings  # noqa: PLC0415

        session = _FakeSession({"P-001": 1, "P-002": 0, "P-003": 1})
        got = await routed_target_findings(
            session, ["P-003", "P-001", "P-002", "P-001"],
            {"P-001": ["state"], "P-002": ["state"], "P-003": []},
        )
        assert got == {"orphans": ["P-002"], "channelless": ["P-003"]}
        assert session.params == [{"ids": ["P-001", "P-002", "P-003"]}]

    @pytest.mark.asyncio
    async def test_clean_when_every_target_is_a_deliberate_member(self) -> None:
        from tests.fixtures.retrieval_scoring import routed_target_findings  # noqa: PLC0415

        got = await routed_target_findings(_FakeSession({"P-001": 2}), ["P-001"],
                                           {"P-001": ["state"]})
        assert got == {"orphans": [], "channelless": []}
