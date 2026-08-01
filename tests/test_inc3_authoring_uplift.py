"""INC-3: authoring uplift (ABS: writing-skills).

Adds the how-to-author-effective-nodes methodology Writ lacked: keyword coverage for
retrieval, token conciseness, and persuasion principles -- plus pressure-scenario craft and
meta-testing upgrades to PBK-AUTHOR-001. The new nodes must practice what they preach
(keyword-rich + lean), and link into the authoring cluster with INC-2-canonical edges.

Pure corpus assertions; always run.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.fixtures.md_helpers import BODY_WORD_BUDGET, frontmatter_body, text_lower, word_count
from writ.graph.ingest import (


    NODE_ID_FIELDS,
    parse_edges_from_file,
    parse_nodes_from_file,
    validate_parsed_node,
)

from tests._bible_guard import requires_bible

pytestmark = requires_bible


WRIT_ROOT = Path(__file__).resolve().parent.parent
METH = WRIT_ROOT / "bible" / "methodology"

# new node -> (node_type, id_field, required searchable terms it prescribes/must itself carry)
NEW_NODES = {
    "TEC-META-KEYWORDS-001": ("Technique", "technique_id", ["bm25", "synonym", "symptom", "retriev"]),
    "TEC-META-PERSUASION-001": ("Technique", "technique_id", ["authority", "commitment", "social"]),
    "ENF-META-CONCISE-001": ("Rule", "rule_id", ["token", "budget", "concise"]),
}

# New edges, in INC-2-canonical direction, and which file declares each.
EXPECTED_EDGES = [
    ("PBK-AUTHOR-001.md", "PBK-AUTHOR-001", "TEACHES", "TEC-META-KEYWORDS-001"),
    ("PBK-AUTHOR-001.md", "PBK-AUTHOR-001", "TEACHES", "TEC-META-PERSUASION-001"),
    ("ENF-META-CONCISE-001.md", "ENF-META-CONCISE-001", "GATES", "PBK-AUTHOR-001"),
]


def _body(path: Path) -> str:
    return frontmatter_body(path)


# --- 1. New nodes parse + validate -------------------------------------------


class TestNewNodesValid:
    @pytest.mark.parametrize("node_id", sorted(NEW_NODES))
    def test_parses_and_validates(self, node_id: str) -> None:
        node_type, id_field, _ = NEW_NODES[node_id]
        path = METH / f"{node_id}.md"
        assert path.exists(), f"{node_id}.md missing"
        nodes = parse_nodes_from_file(path)
        assert len(nodes) == 1 and nodes[0]["node_type"] == node_type
        assert nodes[0].get(id_field) == node_id
        try:
            validate_parsed_node(nodes[0])
        except (ValueError, ValidationError) as e:
            pytest.fail(f"{node_id} invalid: {e}")


# --- 2. Self-consistency: keyword-rich ---------------------------------------


class TestKeywordRich:
    @pytest.mark.parametrize("node_id", sorted(NEW_NODES))
    def test_node_carries_its_prescribed_terms(self, node_id: str) -> None:
        _, _, terms = NEW_NODES[node_id]
        blob = text_lower(METH / f"{node_id}.md")
        missing = [t for t in terms if t not in blob]
        assert not missing, f"{node_id} omits the searchable terms it prescribes: {missing}"


# --- 3. Self-consistency: lean (the conciseness rule's author obeys it) -------


class TestConcise:
    @pytest.mark.parametrize("node_id", sorted(NEW_NODES))
    def test_body_within_budget(self, node_id: str) -> None:
        words = word_count(_body(METH / f"{node_id}.md"))
        assert words <= BODY_WORD_BUDGET, (
            f"{node_id} body is {words} words (> {BODY_WORD_BUDGET}); ENF-META-CONCISE-001 "
            "applies to its own author"
        )


# --- 4. Edges canonical + resolve --------------------------------------------


class TestEdges:
    @pytest.mark.parametrize("fname,src,etype,tgt", EXPECTED_EDGES)
    def test_expected_edge_present_and_resolves(self, fname, src, etype, tgt) -> None:
        edges = parse_edges_from_file(METH / fname)
        assert any(e.get("source") == src and e.get("type") == etype and e.get("target") == tgt
                   for e in edges), f"missing edge {src} --{etype}--> {tgt} in {fname}"
        assert (METH / f"{tgt}.md").exists(), f"edge target {tgt} has no node file"

    def test_no_reverse_of_new_edges(self) -> None:
        # Canonical direction guard: the reverse of each new edge must not exist anywhere.
        all_edges = []
        for f in METH.glob("*.md"):
            all_edges.extend(parse_edges_from_file(f))
        for _, src, etype, tgt in EXPECTED_EDGES:
            assert not any(e.get("type") == etype and e.get("source") == tgt and e.get("target") == src
                           for e in all_edges), f"reverse of {src} --{etype}--> {tgt} present (non-canonical)"


# --- 5. PBK-AUTHOR-001 upgraded ----------------------------------------------


class TestAuthorPlaybookUpgraded:
    def test_step1_has_pressure_taxonomy(self) -> None:
        body = _body(METH / "PBK-AUTHOR-001.md").lower()
        # >=3 combined pressures from an enumerated taxonomy
        taxonomy = ["sunk cost", "authority", "exhaustion"]
        present = [t for t in taxonomy if t in body]
        assert len(present) >= 3, f"PBK-AUTHOR-001 Step 1 lacks the pressure taxonomy (found {present})"

    def test_step5_has_meta_testing(self) -> None:
        body = _body(METH / "PBK-AUTHOR-001.md").lower()
        assert "meta-test" in body or "how the node should have" in body, (
            "PBK-AUTHOR-001 Step 5 lacks the meta-testing routing"
        )


# --- 6. Census ---------------------------------------------------------------


class TestCensus:
    def test_tec_and_enf_counts(self) -> None:
        # Floors, not exact: the exact census lives in test_phase6efg_corpus_promotion
        # (the single census authority). This test only verifies INC-3's TEC/ENF additions
        # survived, so it must not break when a later increment adds a node (INC-1 floors lesson).
        tec = len(list(METH.glob("TEC-*.md")))
        enf = len(list(METH.glob("ENF-*.md")))
        assert tec >= 7, f"expected at least 7 TEC-*.md, found {tec}"
        assert enf >= 9, f"expected at least 9 ENF-*.md, found {enf}"
