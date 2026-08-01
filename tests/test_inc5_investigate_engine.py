"""INC-5: unified investigate-engine node (NRV-3).

INV-1..9 unified audit/explore/research/debug into one engine selected by a source_type
switch -- in code only. SKL-PROC-INVESTIGATE-001 is its retrievable description: declare
source_type (code|web|runtime) -> lens -> enforcing gate. The node must mirror the code's
_LENS_TABLE vocabulary (so they cannot drift) and link to the three lens playbooks.

Pure corpus assertions; always run.
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
NODE = METH / "SKL-PROC-INVESTIGATE-001.md"

LENS_PLAYBOOKS = ["PBK-PROC-RESEARCH-001", "PBK-PROC-DEBUG-001", "PBK-PROC-AUDIT-FANOUT-001"]
# the code _LENS_TABLE vocabulary the node must carry, so node and code can't drift
SOURCE_TYPES = ["code", "web", "runtime"]
LENS_WORDS = ["audit", "explore", "research", "debug"]
GATE_WORDS = ["synthesis", "triangulation", "root-cause"]
KEYWORDS = ["investigate", "source_type", "lens"]


def _node() -> dict:
    nodes = parse_nodes_from_file(NODE)
    return nodes[0] if nodes else {}


def _text() -> str:
    return text_lower(NODE)


def _body() -> str:
    return frontmatter_body(NODE)


class TestNodeValid:
    def test_parses_and_validates_as_skill(self) -> None:
        assert NODE.exists(), "SKL-PROC-INVESTIGATE-001.md missing"
        n = _node()
        assert n.get("node_type") == "Skill" and n.get("skill_id") == "SKL-PROC-INVESTIGATE-001"
        try:
            validate_parsed_node(n)
        except (ValueError, ValidationError) as e:
            pytest.fail(f"SKL-PROC-INVESTIGATE-001 invalid: {e}")


class TestMirrorsLensTable:
    @pytest.mark.parametrize("term", SOURCE_TYPES + LENS_WORDS + GATE_WORDS)
    def test_body_carries_lens_table_term(self, term: str) -> None:
        assert term in _text(), (
            f"SKL-PROC-INVESTIGATE-001 omits lens-table term '{term}'; node and code _LENS_TABLE "
            "would drift"
        )


class TestKeywordRichAndLean:
    def test_keyword_rich(self) -> None:
        missing = [k for k in KEYWORDS if k not in _text()]
        assert not missing, f"SKL-PROC-INVESTIGATE-001 omits searchable terms: {missing}"

    def test_within_budget(self) -> None:
        words = word_count(_body())
        assert words <= BODY_WORD_BUDGET, f"body {words} words > {BODY_WORD_BUDGET}"


class TestEdges:
    @pytest.mark.parametrize("target", LENS_PLAYBOOKS)
    def test_related_to_lens_playbook(self, target: str) -> None:
        edges = parse_edges_from_file(NODE)
        assert any(e.get("type") == "RELATED_TO" and e.get("target") == target for e in edges), (
            f"SKL-PROC-INVESTIGATE-001 missing RELATED_TO -> {target}"
        )
        assert (METH / f"{target}.md").exists(), f"lens playbook {target} has no node file"

    def test_no_reverse_or_cycle(self) -> None:
        all_edges = []
        for f in METH.glob("*.md"):
            all_edges.extend(parse_edges_from_file(f))
        for target in LENS_PLAYBOOKS:
            assert not any(
                e.get("type") == "RELATED_TO" and e.get("source") == target
                and e.get("target") == "SKL-PROC-INVESTIGATE-001"
                for e in all_edges
            ), f"reverse RELATED_TO {target} -> SKL-PROC-INVESTIGATE-001 present (redundant)"


class TestCensus:
    def test_skill_count(self) -> None:
        # Floor, not exact: the exact SKL census lives in test_phase6efg_corpus_promotion
        # (the single census authority). This test only sanity-checks the corpus is present,
        # so it must not break every time a later increment adds a Skill (INC-1 floors lesson).
        n = len(list(METH.glob("SKL-*.md")))
        assert n >= 11, f"expected at least 11 SKL-*.md, found {n}"
