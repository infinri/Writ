"""INC-11: apply-surfaced-methodology + instruction priority (ABS: using-superpowers).

The Writ reframe: superpowers' using-superpowers is about remembering to INVOKE skills, but
Writ's rag-inject hook surfaces methodology automatically. Writ's gaps are (1) application
discipline -- honoring a surfaced rule -- and (2) instruction priority. The dedup gate (with the
INC-9 lesson) found only SKL-PROC-MODE-001 nearby; these three are genuinely absent and form one
bundle (skill + rule + rationalization, the corpus's TDD-style pattern):

  - SKL-PROC-METHODOLOGY-CHECK-001 (Skill): apply what Writ surfaces; an injected rule is
    mandatory to address, not optional.
  - ENF-PROC-PRIORITY-001 (Rule, advisory): user/CLAUDE.md > methodology > default.
  - RAT-PROC-SKILLCHECK-001 (Rationalization): the bypass thoughts + counter.

Pure corpus assertions (always run) + live dedup gate (retrievable SKL + Rule) +
bundle_completeness guard (corpus_ready).
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

SKILL_ID = "SKL-PROC-METHODOLOGY-CHECK-001"
RULE_ID = "ENF-PROC-PRIORITY-001"
RAT_ID = "RAT-PROC-SKILLCHECK-001"

# node_id -> (file, node_type, id_field, body_budget|None)
NEW_NODES = {
    SKILL_ID: (METH / f"{SKILL_ID}.md", "Skill", "skill_id", BODY_WORD_BUDGET),
    RULE_ID: (METH / f"{RULE_ID}.md", "Rule", "rule_id", 200),
    RAT_ID: (METH / f"{RAT_ID}.md", "Rationalization", "rationalization_id", None),
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
        _, node_type, id_field, _budget = NEW_NODES[nid]
        n = _node(nid)
        assert n.get("node_type") == node_type, f"{nid} node_type != {node_type}"
        assert n.get(id_field) == nid, f"{nid} {id_field} mismatch"
        try:
            validate_parsed_node(n)
        except (ValueError, ValidationError) as e:
            pytest.fail(f"{nid} invalid: {e}")

    @pytest.mark.parametrize("nid", sorted(NEW_NODES))
    def test_within_budget(self, nid: str) -> None:
        budget = NEW_NODES[nid][3]
        if budget is None:
            pytest.skip("no body budget for this type")
        words = word_count(_body(nid))
        assert words <= budget, f"{nid} body {words} words > {budget} (ENF-META-CONCISE-001)"


class TestMethodologyCheckSkill:
    def test_apply_surfaced_not_optional(self) -> None:
        t = _text(SKILL_ID)
        assert ("surface" in t or "inject" in t or "retriev" in t), (
            f"{SKILL_ID} omits the 'Writ surfaces methodology' framing"
        )
        assert ("apply" in t), f"{SKILL_ID} omits 'apply' the surfaced rule"
        assert ("not optional" in t or "mandatory" in t or "not advisory" in t), (
            f"{SKILL_ID} omits 'a surfaced rule is not optional'"
        )

    def test_edges(self) -> None:
        edges = _edges(SKILL_ID)
        assert any(
            e.get("type") == "RELATED_TO" and e.get("target") == "SKL-PROC-MODE-001" for e in edges
        ), f"{SKILL_ID} missing RELATED_TO -> SKL-PROC-MODE-001"
        assert any(
            e.get("type") == "TEACHES" and e.get("target") == RULE_ID for e in edges
        ), f"{SKILL_ID} missing TEACHES -> {RULE_ID}"


class TestPriorityRule:
    def test_priority_order(self) -> None:
        t = _text(RULE_ID)
        assert "user" in t, f"{RULE_ID} omits user instructions"
        assert "default" in t, f"{RULE_ID} omits default behavior"
        assert ("precedence" in t or "priority" in t or "wins" in t or "override" in t), (
            f"{RULE_ID} omits the precedence relation"
        )


class TestRationalization:
    def test_edges_attached_and_counters(self) -> None:
        edges = _edges(RAT_ID)
        assert any(
            e.get("type") == "ATTACHED_TO" and e.get("target") == SKILL_ID for e in edges
        ), f"{RAT_ID} missing ATTACHED_TO -> {SKILL_ID}"
        assert any(
            e.get("type") == "COUNTERS" and e.get("target") == SKILL_ID for e in edges
        ), f"{RAT_ID} missing COUNTERS -> {SKILL_ID}"

    def test_carries_a_bypass_thought(self) -> None:
        t = _text(RAT_ID)
        assert ("simple" in t or "overkill" in t or "already know" in t or "quickly" in t), (
            f"{RAT_ID} omits a canonical bypass thought"
        )


class TestNoReverseEdges:
    @pytest.mark.parametrize(
        "source,target",
        [
            (SKILL_ID, "SKL-PROC-MODE-001"),
            (SKILL_ID, RULE_ID),
            (RAT_ID, SKILL_ID),
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


class TestCensus:
    def test_skill_enf_rat_counts(self) -> None:
        # Phase 0: 14 -> 15 SKL after adding SKL-PROC-DEBUG-001 (debug skill).
        # 15 -> 16 when SKL-PROC-WRIT-FAILURE-001.md landed in b115730 (absorbing
        # SKILL.md and the workflow rules into methodology nodes). The census was not
        # bumped then, so this assert had been failing on a full run since. Pre-existing
        # and unrelated to the work it was found by; recorded here rather than left,
        # because a red test everyone learns to expect is how the next real one hides.
        assert len(list(METH.glob("SKL-*.md"))) == 16, "expected 16 SKL-*.md"
        assert len(list(METH.glob("ENF-*.md"))) == 12, "expected 12 ENF-*.md (+1 Phase4-A3: ENF-COMMS-OUTPUT-001; +1 cycle F: ENF-PROC-FIXLOOP-001)"
        assert len(list(METH.glob("RAT-*.md"))) == 4, "expected 4 RAT-*.md"


class TestLiveGates:
    """Uses the shared session-scoped `live_pipeline` + methodology_* fixtures (conftest, POL-2)."""

    @pytest.mark.parametrize("nid", [SKILL_ID, RULE_ID])
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
            f"bundle_completeness {completeness:.4f} < {BLOCKER_COMPLETENESS} after INC-11 nodes."
        )
