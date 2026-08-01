"""INC-8: planning -- file-structure design + worked example (ABS: writing-plans).

The existing planning corpus already covers no-placeholders (SKL/ANT/ENF-PROC-PLAN-001, with a
mechanical gate), map-file-structure-first (PBK step 1), bite-sized tasks, self-review, and
TDD-per-task. The dedup gate (WRIT-BLUEPRINT.md Section 2) narrowed this absorption to:

  - TEC-PROC-FILE-STRUCTURE-001 -- the file-structure *design principles* (split by
    responsibility not technical layer; context-sized files; follow existing patterns).
  - EXM-PLAN-001 -- a worked plan task showing the 2-5 minute micro-cycle, bundle-reachable
    from PBK-PROC-PLAN-001 via its DEMONSTRATES edge.

The standalone "2-5 min micro-cycle" node was dropped (folded into the worked example). The
planned one-line edit to PBK-PROC-PLAN-001's body was also dropped: it added planning keywords
that tipped a knife-edge BM25 tie on benchmark query P5 ("create an implementation plan"),
knocking SKL-PROC-PLAN-001 out of rank 0 and dropping bundle_completeness 0.8667 -> 0.8417,
below the 0.85 blocker. The micro-cycle reaches the playbook via the EXM DEMONSTRATES edge
instead, with no retrieval perturbation. Pure corpus assertions (always run) + one live dedup
gate (corpus_ready) for the only retrievable new node (the TEC; the WorkedExample is
non-retrievable, so not a pipeline candidate).
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

TEC_ID = "TEC-PROC-FILE-STRUCTURE-001"
EXM_ID = "EXM-PLAN-001"

# node_id -> (file, node_type, id_field)
NEW_NODES = {
    TEC_ID: (METH / f"{TEC_ID}.md", "Technique", "technique_id"),
    EXM_ID: (METH / f"{EXM_ID}.md", "WorkedExample", "example_id"),
}

BUDGET = {"Technique": BODY_WORD_BUDGET}  # ENF-META-CONCISE-001; WorkedExample has no body budget


def _file(node_id: str) -> Path:
    return NEW_NODES[node_id][0]


def _node(node_id: str) -> dict:
    p = _file(node_id)
    assert p.exists(), f"{node_id}.md missing"
    nodes = parse_nodes_from_file(p)
    return nodes[0] if nodes else {}


def _text(node_id: str) -> str:
    p = _file(node_id)
    assert p.exists(), f"{node_id}.md missing"
    return text_lower(p)


def _body(node_id: str) -> str:
    p = _file(node_id)
    assert p.exists(), f"{node_id}.md missing"
    return frontmatter_body(p)


def _edges(node_id: str) -> list[dict]:
    return parse_edges_from_file(_file(node_id))


class TestNodesValid:
    @pytest.mark.parametrize("node_id", sorted(NEW_NODES))
    def test_parses_and_validates(self, node_id: str) -> None:
        _, node_type, id_field = NEW_NODES[node_id]
        n = _node(node_id)
        assert n.get("node_type") == node_type, f"{node_id} node_type != {node_type}"
        assert n.get(id_field) == node_id, f"{node_id} {id_field} mismatch"
        try:
            validate_parsed_node(n)
        except (ValueError, ValidationError) as e:
            pytest.fail(f"{node_id} invalid: {e}")

    def test_tec_within_word_budget(self) -> None:
        words = word_count(_body(TEC_ID))
        assert words <= BUDGET["Technique"], (
            f"{TEC_ID} body {words} words > {BUDGET['Technique']} (ENF-META-CONCISE-001)"
        )


class TestFileStructureTec:
    def test_carries_design_vocabulary(self) -> None:
        t = _text(TEC_ID)
        for term in ("responsib", "interface", "context"):
            assert term in t, f"{TEC_ID} omits file-structure term '{term}'"
        # the genuinely-new content vs PBK step 1: split by responsibility not layer
        assert ("technical layer" in t or "change together" in t), (
            f"{TEC_ID} omits 'split by responsibility, not technical layer / change together'"
        )
        assert "restructure" in t, f"{TEC_ID} omits the 'do not unilaterally restructure' guidance"

    @pytest.mark.parametrize(
        "target", ["PBK-PROC-PLAN-001", "SKL-PROC-TDD-DESIGN-FEEDBACK-001"]
    )
    def test_related_to_edges_resolve(self, target: str) -> None:
        assert any(
            e.get("type") == "RELATED_TO" and e.get("target") == target for e in _edges(TEC_ID)
        ), f"{TEC_ID} missing RELATED_TO -> {target}"
        assert (METH / f"{target}.md").exists(), f"edge target {target} has no node file"


class TestPlanWorkedExample:
    def test_carries_micro_cycle_terms(self) -> None:
        t = _text(EXM_ID)
        for term in ("failing test", "minimal", "commit"):
            assert term in t, f"{EXM_ID} omits micro-cycle term '{term}'"
        # the cycle's pass/fail observation
        assert ("fail" in t and "pass" in t), f"{EXM_ID} omits the run-fail / run-pass steps"

    def test_demonstrates_plan_playbook(self) -> None:
        assert any(
            e.get("type") == "DEMONSTRATES" and e.get("target") == "PBK-PROC-PLAN-001"
            for e in _edges(EXM_ID)
        ), f"{EXM_ID} missing DEMONSTRATES -> PBK-PROC-PLAN-001"


class TestNoReverseEdges:
    @pytest.mark.parametrize(
        "source,target",
        [
            (TEC_ID, "PBK-PROC-PLAN-001"),
            (TEC_ID, "SKL-PROC-TDD-DESIGN-FEEDBACK-001"),
            (EXM_ID, "PBK-PROC-PLAN-001"),
        ],
    )
    def test_no_reverse(self, source: str, target: str) -> None:
        all_edges = []
        for f in METH.glob("*.md"):
            all_edges.extend(parse_edges_from_file(f))
        reverse = [
            e for e in all_edges
            if e.get("source") == target and e.get("target") == source
        ]
        assert not reverse, f"reverse edge {target} -> {source} present (non-canonical): {reverse}"


class TestDedupGateLive:
    """WRIT-BLUEPRINT Section 2: the retrievable new node must not near-duplicate an existing one.

    Only TEC-PROC-FILE-STRUCTURE-001 is checked: WorkedExample is non-retrievable, so it is not a
    pipeline candidate and (by design) demonstrates the playbook it is meant to duplicate.

    Uses the shared session-scoped `live_pipeline` fixture (conftest, POL-2).
    """

    def test_tec_not_a_near_duplicate(self, live_pipeline) -> None:
        from writ.authoring import check_redundancy

        n = _node(TEC_ID)
        flagged = check_redundancy(
            {"trigger": n.get("trigger", ""), "statement": n.get("statement", "")},
            live_pipeline,
        )
        others = [f for f in flagged if f["rule_id"] != TEC_ID]
        assert not others, (
            f"{TEC_ID} is a near-duplicate (cosine >= 0.95) of: "
            f"{[(f['rule_id'], f['similarity']) for f in others]}"
        )
