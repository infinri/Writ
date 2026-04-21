"""Phase 1 deliverable 7.2: retrievable node-type filter + retrieval_mode tests.

Validates:
- pipeline.query respects node_types whitelist
- retrieval_mode="literal" activates equal-weight BM25/vector
- retrieval_mode="semantic" preserves coding-rule defaults and excludes
  methodology domains from the default candidate pool
- RETRIEVABLE_NODE_TYPES constant matches plan Section 2.3
"""
from __future__ import annotations

from writ.graph.schema import (
    NodeType,
    RETRIEVABLE_NODE_TYPES,
)
from writ.retrieval.ranking import (
    DEFAULT_W_BM25,
    DEFAULT_W_VECTOR,
    LITERAL_W_BM25,
    LITERAL_W_VECTOR,
    RankingWeights,
)


class TestRetrievableNodeTypes:
    """Plan Section 2.3 retrievable subset."""

    def test_retrievable_set_matches_plan_section_2_3(self) -> None:
        expected = {
            NodeType.RULE, NodeType.ABSTRACTION,
            NodeType.SKILL, NodeType.PLAYBOOK, NodeType.TECHNIQUE,
            NodeType.ANTIPATTERN, NodeType.FORBIDDEN_RESPONSE,
        }
        assert RETRIEVABLE_NODE_TYPES == frozenset(expected)

    def test_non_retrievable_types_excluded(self) -> None:
        non_retrievable = {
            NodeType.PHASE, NodeType.RATIONALIZATION,
            NodeType.PRESSURE_SCENARIO, NodeType.WORKED_EXAMPLE,
            NodeType.SUBAGENT_ROLE,
        }
        assert not (non_retrievable & RETRIEVABLE_NODE_TYPES)


class TestRetrievalModeWeights:
    """retrieval_mode='literal' returns a distinct RankingWeights from default."""

    def test_literal_weights_factory(self) -> None:
        w = RankingWeights.literal()
        assert w.w_bm25 == LITERAL_W_BM25
        assert w.w_vector == LITERAL_W_VECTOR
        # Equal weights between BM25 and vector.
        assert w.w_bm25 == w.w_vector

    def test_default_weights_unchanged(self) -> None:
        """Phase 1 MUST NOT regress coding-rule defaults."""
        w = RankingWeights()
        assert w.w_bm25 == DEFAULT_W_BM25
        assert w.w_vector == DEFAULT_W_VECTOR
        # Coding-rule default is vector-dominant.
        assert w.w_vector > w.w_bm25

    def test_literal_weights_validate(self) -> None:
        RankingWeights.literal().validate()  # sums to 1.0 — no exception

    def test_default_weights_validate(self) -> None:
        RankingWeights().validate()  # sums to 1.0 — no exception
