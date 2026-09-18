"""The two properties benchmarks/NEO4J-ABLATION-2026-09-18.md asserts, pinned.

Both are derived from code constants rather than written as literals, so a change
to either source constant fails HERE by name instead of quietly agreeing with a
changed system. Neither test asserts on documentation prose (see the standing rule
against doc-content tests); they read only constants and synthetic rule dicts, so
they need no graph, no session cache and no machine-specific log (TEST-ISOLATE-001).
"""

from __future__ import annotations

from writ.retrieval.ranking import (
    DEFAULT_W_BM25,
    DEFAULT_W_CONFIDENCE,
    DEFAULT_W_GRAPH,
    DEFAULT_W_SEVERITY,
    DEFAULT_W_VECTOR,
    LITERAL_W_GRAPH,
    STANDARD_THRESHOLD,
    RankingWeights,
    apply_context_budget,
)
from writ.session.config import DEFAULT_SESSION_BUDGET

# The pre-rebalance ratios the sweep holds fixed while varying w_graph
# (scripts/sweep_ranking.py::_rebalanced_weights). The shipped defaults must BE
# that rebalance at the shipped w_graph, or the constants are a point nobody measured.
_BASE = {"bm25": 0.198, "vector": 0.594, "severity": 0.099, "confidence": 0.099}
_BASE_SUM = sum(_BASE.values())

# The do-no-harm winner of the 2026-09-18 sweep: the only arm improving MRR@5,
# hit-rate@5 and nDCG@10 together while losing zero queries (4 wins, 0 losses,
# p=0.1250). Changing this without re-running the sweep is the failure this pins.
MEASURED_W_GRAPH = 0.05


def _rebalanced(w_graph: float) -> dict[str, float]:
    scale = (1.0 - w_graph) / _BASE_SUM
    return {k: v * scale for k, v in _BASE.items()}


def _rule_with_relationships() -> dict:
    return {
        "rule_id": "R1", "node_type": "Rule", "score": 1.0,
        "statement": "s", "trigger": "t", "violation": "v",
        "pass_example": "p", "rationale": "r",
        "relationships": [{"rule_id": "N1"}, {"rule_id": "N2"}],
    }


class TestGraphWeightIsTheMeasuredArm:
    def test_default_w_graph_is_the_measured_winner(self):
        assert DEFAULT_W_GRAPH == MEASURED_W_GRAPH

    def test_non_graph_defaults_are_the_rebalance_at_that_weight(self):
        want = _rebalanced(DEFAULT_W_GRAPH)
        got = {
            "bm25": DEFAULT_W_BM25, "vector": DEFAULT_W_VECTOR,
            "severity": DEFAULT_W_SEVERITY, "confidence": DEFAULT_W_CONFIDENCE,
        }
        for name, expected in want.items():
            assert abs(got[name] - expected) < 1e-9, (
                f"w_{name} is {got[name]}, but rebalancing the base ratios at "
                f"w_graph={DEFAULT_W_GRAPH} gives {expected}. Raising w_graph "
                f"without rebalancing ships a weight vector the sweep never ran."
            )

    def test_default_weights_still_sum_to_one(self):
        RankingWeights().validate()

    def test_literal_preset_was_not_measured_and_did_not_move(self):
        assert LITERAL_W_GRAPH == 0.01
        RankingWeights.literal().validate()


class TestFullRenderModeReachabilityAtTheDefaultBudget:
    def test_full_mode_requires_strictly_more_than_the_standard_threshold(self):
        _, at = apply_context_budget([_rule_with_relationships()], STANDARD_THRESHOLD)
        _, above = apply_context_budget([_rule_with_relationships()], STANDARD_THRESHOLD + 1)
        assert at == "standard"
        assert above == "full"

    def test_default_session_budget_does_not_reach_full_mode(self):
        out, mode = apply_context_budget(
            [_rule_with_relationships()], DEFAULT_SESSION_BUDGET
        )
        assert mode == "standard", (
            f"DEFAULT_SESSION_BUDGET={DEFAULT_SESSION_BUDGET} now selects {mode!r}. "
            f"If that is intended, the RELATED: line starts reaching the model on "
            f"every turn for the first time; update the ablation write-up."
        )
        assert "relationships" not in out[0]

    def test_one_more_token_than_the_default_budget_does_reach_full(self):
        out, mode = apply_context_budget(
            [_rule_with_relationships()], DEFAULT_SESSION_BUDGET + 1
        )
        assert mode == "full"
        assert out[0]["relationships"] == [{"rule_id": "N1"}, {"rule_id": "N2"}]

    def test_the_default_budget_sits_exactly_on_the_boundary(self):
        assert DEFAULT_SESSION_BUDGET == STANDARD_THRESHOLD

    def test_absent_budget_is_the_one_path_that_reaches_full(self):
        out, mode = apply_context_budget([_rule_with_relationships()], None)
        assert mode == "full"
        assert "relationships" in out[0]


class TestStandardModeDropsEnrichmentOutput:
    def test_relationships_survive_only_in_full_mode(self):
        for budget, expected in (
            (1000, False), (DEFAULT_SESSION_BUDGET, False),
            (DEFAULT_SESSION_BUDGET + 1, True),
        ):
            out, _ = apply_context_budget([_rule_with_relationships()], budget)
            assert ("relationships" in out[0]) is expected

    def test_a_rule_with_no_enrichment_reaches_full_mode_with_an_empty_list(self):
        bare = _rule_with_relationships()
        del bare["relationships"]
        out, mode = apply_context_budget([bare], DEFAULT_SESSION_BUDGET + 1)
        assert mode == "full"
        assert out[0]["relationships"] == []
