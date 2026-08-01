"""INC-9: receiving code review (ABS: receiving-code-review).

DEDUP CATCH: a receiving-review skill ALREADY existed -- SKL-PROC-REVRECV-001 ("REVRECV" =
review-receive, domain communication). The benchmark ground truth (P32/P33) already pointed at
it. So INC-9 does NOT add a node; it ENRICHES the existing one with the genuinely-missing pieces
the superpowers receiving-code-review skill has and Writ lacked:
  - external reviewers are suggestions to evaluate, not orders (the vetting questions),
  - the YAGNI check (grep for usage before building out a "do it properly" suggestion),
  - strengthen unclear-handling to "clarify ALL items before implementing ANY".
Performative-agreement is left to FRB-COMMS-001 / ENF-COMMS-001 (already owned).

Pure corpus assertions (always run) + a live dedup gate + a bundle_completeness guard
(corpus_ready; the INC-8 lesson). Census is UNCHANGED (no new node).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.fixtures.md_helpers import BODY_WORD_BUDGET, frontmatter_body, text_lower, word_count
from writ.graph.ingest import parse_edges_from_file, parse_nodes_from_file, validate_parsed_node

from tests._bible_guard import requires_bible

pytestmark = requires_bible


WRIT_ROOT = Path(__file__).resolve().parent.parent
METH = WRIT_ROOT / "bible" / "methodology"

SKILL_ID = "SKL-PROC-REVRECV-001"
NODE = METH / f"{SKILL_ID}.md"
# The duplicate this increment must NOT have introduced.
FORBIDDEN_DUP = METH / "SKL-PROC-RECEIVE-REVIEW-001.md"


def _node() -> dict:
    assert NODE.exists(), f"{SKILL_ID}.md missing"
    nodes = parse_nodes_from_file(NODE)
    return nodes[0] if nodes else {}


def _text() -> str:
    assert NODE.exists(), f"{SKILL_ID}.md missing"
    return text_lower(NODE)


def _body() -> str:
    assert NODE.exists(), f"{SKILL_ID}.md missing"
    return frontmatter_body(NODE)


def _edges() -> list[dict]:
    return parse_edges_from_file(NODE)


class TestNoDuplicate:
    def test_no_duplicate_receiving_review_node(self) -> None:
        assert not FORBIDDEN_DUP.exists(), (
            "SKL-PROC-RECEIVE-REVIEW-001 is a duplicate of the existing SKL-PROC-REVRECV-001; "
            "INC-9 enriches the existing node instead of adding one."
        )


class TestNodeValid:
    def test_parses_and_validates_as_skill(self) -> None:
        n = _node()
        assert n.get("node_type") == "Skill", f"{SKILL_ID} is not a Skill"
        assert n.get("skill_id") == SKILL_ID, f"{SKILL_ID} skill_id mismatch"
        try:
            validate_parsed_node(n)
        except (ValueError, ValidationError) as e:
            pytest.fail(f"{SKILL_ID} invalid: {e}")

    def test_within_word_budget(self) -> None:
        words = word_count(_body())
        assert words <= BODY_WORD_BUDGET, (
            f"{SKILL_ID} body {words} words > {BODY_WORD_BUDGET} (ENF-META-CONCISE-001)"
        )


class TestEnrichedContent:
    """The pieces INC-9 added on top of the pre-existing reception workflow."""

    def test_external_reviewer_skepticism(self) -> None:
        t = _text()
        assert "suggestions to evaluate" in t or "not orders" in t, (
            f"{SKILL_ID} missing 'external suggestions to evaluate, not orders'"
        )
        # at least one of the vetting questions
        assert ("breaks existing" in t or "full context" in t or "reason for the current" in t), (
            f"{SKILL_ID} missing the external-reviewer vetting questions"
        )

    def test_yagni_check(self) -> None:
        t = _text()
        assert "yagni" in t, f"{SKILL_ID} missing the YAGNI check"
        assert ("grep" in t or "unused" in t or "usage" in t), (
            f"{SKILL_ID} missing the grep-for-usage YAGNI check"
        )

    def test_unclear_halts_all(self) -> None:
        t = _text()
        assert "unclear" in t, f"{SKILL_ID} omits the unclear case"
        # strengthened: clarify ALL before implementing ANY
        assert ("all items before implementing any" in t or "clarify all" in t), (
            f"{SKILL_ID} did not strengthen unclear-handling to 'clarify ALL before implementing ANY'"
        )

    def test_retains_reception_workflow(self) -> None:
        # regression guard: the pre-existing content survives the enrichment.
        t = _text()
        assert "verify against this codebase" in t, f"{SKILL_ID} lost verify-against-codebase"
        assert "push back" in t, f"{SKILL_ID} lost the pushback guidance"


class TestEdges:
    def test_teaches_enf_comms_retained(self) -> None:
        # the pre-existing edge must survive the body enrichment.
        assert any(
            e.get("type") == "TEACHES" and e.get("target") == "ENF-COMMS-001" for e in _edges()
        ), f"{SKILL_ID} lost its TEACHES -> ENF-COMMS-001 edge"
        assert (METH / "ENF-COMMS-001.md").exists()


class TestLiveGates:
    """Live dedup gate + bundle_completeness benchmark guard.

    Uses the shared session-scoped `live_pipeline` + methodology_* fixtures (conftest, POL-2).
    """

    def test_not_a_near_duplicate(self, live_pipeline) -> None:
        # After deleting the dup, no OTHER node should be >= 0.95 to REVRECV.
        from writ.authoring import check_redundancy

        n = _node()
        flagged = check_redundancy(
            {"trigger": n.get("trigger", ""), "statement": n.get("statement", "")},
            live_pipeline,
        )
        others = [f for f in flagged if f["rule_id"] != SKILL_ID]
        assert not others, (
            f"{SKILL_ID} near-duplicate (cosine >= 0.95) of: "
            f"{[(f['rule_id'], f['similarity']) for f in others]}"
        )

    def test_bundle_completeness_holds(
        self,
        corpus_ready,
        methodology_ground_truth,
        methodology_kindex,
        methodology_model,
        methodology_node_vectors,
        methodology_adjacency,
    ) -> None:
        # INC-8 lesson: enriching a retrievable node's body can shift BM25 rankings.
        from tests.fixtures.benchmark_harness import BLOCKER_COMPLETENESS, bundle_completeness

        completeness = bundle_completeness(
            methodology_ground_truth, methodology_kindex, methodology_model,
            methodology_node_vectors, methodology_adjacency,
        )
        assert completeness >= BLOCKER_COMPLETENESS, (
            f"bundle_completeness {completeness:.4f} < {BLOCKER_COMPLETENESS} after enriching "
            f"{SKILL_ID}; the body change perturbed retrieval (see INC-8)."
        )
