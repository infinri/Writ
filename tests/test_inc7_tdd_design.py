"""INC-7: TDD design-feedback + GREEN over-engineering + RED verification (ABS: test-driven-development).

The existing TDD corpus is large (PBK-PROC-TDD-001, ENF-PROC-TDD-001, ANT-001..005, RAT-TDD-001,
EXM-TDD-001, PBK-PROC-DIAGNOSE-FAILING-TEST-001). The dedup gate (WRIT-BLUEPRINT.md Section 2)
narrowed this absorption to three genuinely-absent nodes and DROPPED the planned ANT-PROC-TDD-007
(over-mocking) as redundant with ANT-002 + ANT-004:

  - SKL-PROC-TDD-DESIGN-FEEDBACK-001 -- difficulty writing the test is *design feedback*
    (folds in the over-mocking-as-smell angle, so ANT-007 is unnecessary).
  - ANT-PROC-TDD-006 -- GREEN-phase over-engineering (more than the minimal code to pass).
  - TEC-PROC-RED-VERIFY-001 -- a test that *errors* has not achieved RED.

Pure corpus assertions (always run) + one live dedup gate (corpus_ready): each new node must not
be a near-duplicate (cosine >= 0.95) of any existing node.
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

SKILL_ID = "SKL-PROC-TDD-DESIGN-FEEDBACK-001"
ANT_ID = "ANT-PROC-TDD-006"
TEC_ID = "TEC-PROC-RED-VERIFY-001"

# node_id -> (file, node_type, id_field)
NEW_NODES = {
    SKILL_ID: (METH / f"{SKILL_ID}.md", "Skill", "skill_id"),
    ANT_ID: (METH / f"{ANT_ID}.md", "AntiPattern", "antipattern_id"),
    TEC_ID: (METH / f"{TEC_ID}.md", "Technique", "technique_id"),
}

# Per-type word budgets (ENF-META-CONCISE-001).
BUDGET = {"Skill": BODY_WORD_BUDGET, "AntiPattern": 200, "Technique": BODY_WORD_BUDGET}


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

    @pytest.mark.parametrize("node_id", sorted(NEW_NODES))
    def test_within_word_budget(self, node_id: str) -> None:
        node_type = NEW_NODES[node_id][1]
        words = word_count(_body(node_id))
        assert words <= BUDGET[node_type], (
            f"{node_id} body {words} words > {BUDGET[node_type]} (ENF-META-CONCISE-001)"
        )


class TestDesignFeedbackSkill:
    def test_carries_design_signal_vocabulary(self) -> None:
        t = _text(SKILL_ID)
        # the general principle + the concrete signals (incl. the over-mocking-as-smell fold-in)
        for term in ("design feedback", "mock", "coupl", "responsib", "interface", "hard to test"):
            assert term in t, f"{SKILL_ID} omits design-signal term '{term}'"

    @pytest.mark.parametrize(
        "target", ["PBK-PROC-TDD-001", "ANT-PROC-TDD-002", "ANT-PROC-TDD-003"]
    )
    def test_related_to_edges_resolve(self, target: str) -> None:
        assert any(
            e.get("type") == "RELATED_TO" and e.get("target") == target for e in _edges(SKILL_ID)
        ), f"{SKILL_ID} missing RELATED_TO -> {target}"
        assert (METH / f"{target}.md").exists(), f"edge target {target} has no node file"


class TestGreenOverEngineering:
    def test_carries_over_engineering_vocabulary(self) -> None:
        t = _text(ANT_ID)
        assert "minimal" in t, f"{ANT_ID} omits 'minimal'"
        assert ("over-engineer" in t or "over-engineering" in t or "yagni" in t), (
            f"{ANT_ID} omits the over-engineering / YAGNI framing"
        )
        assert ("green" in t), f"{ANT_ID} does not name the GREEN phase"

    def test_counters_tdd_playbook(self) -> None:
        assert any(
            e.get("type") == "COUNTERS" and e.get("target") == "PBK-PROC-TDD-001"
            for e in _edges(ANT_ID)
        ), f"{ANT_ID} missing COUNTERS -> PBK-PROC-TDD-001"


class TestRedVerify:
    def test_carries_error_vs_assertion_vocabulary(self) -> None:
        t = _text(TEC_ID)
        assert "error" in t, f"{TEC_ID} omits 'error'"
        assert "assertion" in t, f"{TEC_ID} omits 'assertion'"
        assert "right reason" in t, f"{TEC_ID} omits 'right reason'"

    @pytest.mark.parametrize("target", ["PBK-PROC-TDD-001", "ANT-PROC-TDD-001"])
    def test_related_to_edges_resolve(self, target: str) -> None:
        assert any(
            e.get("type") == "RELATED_TO" and e.get("target") == target for e in _edges(TEC_ID)
        ), f"{TEC_ID} missing RELATED_TO -> {target}"
        assert (METH / f"{target}.md").exists(), f"edge target {target} has no node file"


class TestNoReverseEdges:
    """Canonical-only edges (INC-2): the targets must not declare a reverse edge back."""

    @pytest.mark.parametrize(
        "source,target",
        [
            (SKILL_ID, "PBK-PROC-TDD-001"),
            (SKILL_ID, "ANT-PROC-TDD-002"),
            (SKILL_ID, "ANT-PROC-TDD-003"),
            (TEC_ID, "PBK-PROC-TDD-001"),
            (TEC_ID, "ANT-PROC-TDD-001"),
            (ANT_ID, "PBK-PROC-TDD-001"),
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
    """WRIT-BLUEPRINT Section 2: each new node must not near-duplicate an existing one.

    Uses the shared session-scoped `live_pipeline` fixture (conftest, POL-2/POL-3).
    """

    @pytest.mark.parametrize("node_id", sorted(NEW_NODES))
    def test_not_a_near_duplicate(self, node_id: str, live_pipeline) -> None:
        from writ.authoring import check_redundancy

        n = _node(node_id)
        flagged = check_redundancy(
            {"trigger": n.get("trigger", ""), "statement": n.get("statement", "")},
            live_pipeline,
        )
        # check_redundancy does not exclude the node itself; drop the self-match.
        others = [f for f in flagged if f["rule_id"] != node_id]
        assert not others, (
            f"{node_id} is a near-duplicate (cosine >= 0.95) of: "
            f"{[(f['rule_id'], f['similarity']) for f in others]}"
        )
