"""INC-12: verification evidence-map + parallel dispatch-prompt (ABS: verification + dispatching-parallel-agents).

Both topics are heavily covered (SKL/ANT/ENF-PROC-VERIFY-001, SKL/ANT-PROC-PARALLEL-001), and
both skills are referenced by the retrieval benchmark's ground truth. The dedup gate (with the
INC-9 lesson) narrowed this absorption to two new TECs -- no edits to the benchmark-sensitive
skills:

  - TEC-PROC-VERIFY-EVIDENCE-MAP-001: the claim-type -> required-evidence map.
  - TEC-PROC-PARALLEL-PROMPT-001: the dispatch-prompt template + context-isolation principle.

Pure corpus assertions (always run) + live dedup gate + bundle_completeness guard (corpus_ready).
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

EVIDENCE_ID = "TEC-PROC-VERIFY-EVIDENCE-MAP-001"
PROMPT_ID = "TEC-PROC-PARALLEL-PROMPT-001"

# node_id -> (file, related_target)
NEW_NODES = {
    EVIDENCE_ID: (METH / f"{EVIDENCE_ID}.md", "SKL-PROC-VERIFY-001"),
    PROMPT_ID: (METH / f"{PROMPT_ID}.md", "SKL-PROC-PARALLEL-001"),
}


def _file(nid: str) -> Path:
    return NEW_NODES[nid][0]


def _node(nid: str) -> dict:
    p = _file(nid)
    assert p.exists(), f"{nid}.md missing"
    nodes = parse_nodes_from_file(p)
    return nodes[0] if nodes else {}


def _text(nid: str) -> str:
    p = _file(nid)
    assert p.exists(), f"{nid}.md missing"
    return text_lower(p)


def _body(nid: str) -> str:
    p = _file(nid)
    assert p.exists(), f"{nid}.md missing"
    return frontmatter_body(p)


def _edges(nid: str) -> list[dict]:
    return parse_edges_from_file(_file(nid))


class TestNodesValid:
    @pytest.mark.parametrize("nid", sorted(NEW_NODES))
    def test_parses_and_validates(self, nid: str) -> None:
        n = _node(nid)
        assert n.get("node_type") == "Technique", f"{nid} is not a Technique"
        assert n.get("technique_id") == nid, f"{nid} technique_id mismatch"
        try:
            validate_parsed_node(n)
        except (ValueError, ValidationError) as e:
            pytest.fail(f"{nid} invalid: {e}")

    @pytest.mark.parametrize("nid", sorted(NEW_NODES))
    def test_within_word_budget(self, nid: str) -> None:
        words = word_count(_body(nid))
        assert words <= BODY_WORD_BUDGET, (
            f"{nid} body {words} words > {BODY_WORD_BUDGET} (ENF-META-CONCISE-001)"
        )


class TestEvidenceMap:
    def test_carries_claim_to_evidence_mapping(self) -> None:
        t = _text(EVIDENCE_ID)
        assert "evidence" in t, f"{EVIDENCE_ID} omits 'evidence'"
        assert ("exit" in t or "0 failures" in t or "diff" in t), (
            f"{EVIDENCE_ID} omits concrete required-evidence (exit code / 0 failures / diff)"
        )
        # at least two distinct claim types from the map
        claim_terms = [c for c in ("build", "regression", "agent", "requirement") if c in t]
        assert len(claim_terms) >= 2, (
            f"{EVIDENCE_ID} names too few claim types: {claim_terms}"
        )


class TestParallelPrompt:
    def test_carries_template_and_isolation(self) -> None:
        t = _text(PROMPT_ID)
        # the template structure
        assert "scope" in t, f"{PROMPT_ID} omits scope"
        assert ("constraint" in t or "return" in t), f"{PROMPT_ID} omits constraints / return shape"
        # the context-isolation principle
        assert ("isolat" in t or "never inherit" in t or "construct" in t), (
            f"{PROMPT_ID} omits the context-isolation principle"
        )


class TestEdges:
    @pytest.mark.parametrize("nid", sorted(NEW_NODES))
    def test_related_to_resolves(self, nid: str) -> None:
        target = NEW_NODES[nid][1]
        assert any(
            e.get("type") == "RELATED_TO" and e.get("target") == target for e in _edges(nid)
        ), f"{nid} missing RELATED_TO -> {target}"
        assert (METH / f"{target}.md").exists(), f"edge target {target} has no node file"

    @pytest.mark.parametrize("nid", sorted(NEW_NODES))
    def test_no_reverse(self, nid: str) -> None:
        target = NEW_NODES[nid][1]
        all_edges = []
        for f in METH.glob("*.md"):
            all_edges.extend(parse_edges_from_file(f))
        reverse = [
            e for e in all_edges
            if e.get("source") == target and e.get("target") == nid
        ]
        assert not reverse, f"reverse edge {target} -> {nid} present (non-canonical)"


class TestCensus:
    def test_tec_count(self) -> None:
        n = len(list(METH.glob("TEC-*.md")))
        assert n == 14, f"expected 14 TEC-*.md, found {n} (+3 cycle F: CONDITION-WAIT, DEFENSE-DEPTH, TEST-POLLUTION)"


class TestLiveGates:
    """Uses the shared session-scoped `live_pipeline` + methodology_* fixtures (conftest, POL-2)."""

    @pytest.mark.parametrize("nid", sorted(NEW_NODES))
    def test_not_a_near_duplicate(self, nid: str, live_pipeline) -> None:
        from writ.authoring import check_redundancy

        n = _node(nid)
        flagged = check_redundancy(
            {"trigger": n.get("trigger", ""), "statement": n.get("statement", "")},
            live_pipeline,
        )
        others = [f for f in flagged if f["rule_id"] != nid]
        assert not others, (
            f"{nid} near-duplicate (cosine >= 0.95) of: "
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
        from tests.fixtures.benchmark_harness import BLOCKER_COMPLETENESS, bundle_completeness

        completeness = bundle_completeness(
            methodology_ground_truth, methodology_kindex, methodology_model,
            methodology_node_vectors, methodology_adjacency,
        )
        assert completeness >= BLOCKER_COMPLETENESS, (
            f"bundle_completeness {completeness:.4f} < {BLOCKER_COMPLETENESS} after INC-12 TECs."
        )