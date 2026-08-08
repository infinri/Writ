"""INC-10: worktrees -- detect-existing-isolation + prefer-native-tools (ABS: using-git-worktrees).

Writ's worktree cluster (SKL/TEC/ENF-PROC-WORKTREE-001) already owns directory selection,
mandatory gitignore safety (mechanical gate), and baseline-on-red. The dedup gate (with the
INC-9 lesson: grep abbreviations + ground_truth) narrowed this absorption to two genuinely-absent
superpowers concepts:

  - TEC-PROC-WORKTREE-001 enriched (body): Step 0 detect-existing-isolation (skip if already in a
    linked worktree) + prefer native tools (EnterWorktree / --worktree) over `git worktree add`.
  - ANT-PROC-WORKTREE-001 (new): fighting the harness -- raw git worktree add when a native tool
    exists, or a nested worktree without the Step 0 check.

Pure corpus assertions (always run) + live dedup gate + bundle_completeness guard (corpus_ready;
ground_truth has worktree queries -- the INC-8/INC-9 lesson).
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

TEC = METH / "TEC-PROC-WORKTREE-001.md"
ANT_ID = "ANT-PROC-WORKTREE-001"
ANT = METH / f"{ANT_ID}.md"


def _text(p: Path) -> str:
    assert p.exists(), f"{p.name} missing"
    return text_lower(p)


def _body(p: Path) -> str:
    assert p.exists(), f"{p.name} missing"
    return frontmatter_body(p)


def _node(p: Path) -> dict:
    assert p.exists(), f"{p.name} missing"
    nodes = parse_nodes_from_file(p)
    return nodes[0] if nodes else {}


def _edges(p: Path) -> list[dict]:
    return parse_edges_from_file(p)


class TestTecEnriched:
    def test_step0_detect_existing_isolation(self) -> None:
        t = _text(TEC)
        assert "step 0" in t, "TEC-PROC-WORKTREE-001 missing the Step 0 detect-existing section"
        assert ("already in" in t or "already isolated" in t), (
            "TEC-PROC-WORKTREE-001 missing the 'already in a worktree -> skip' guidance"
        )
        assert ("git-common-dir" in t or "common-dir" in t or "git rev-parse" in t), (
            "TEC-PROC-WORKTREE-001 missing the detection mechanism"
        )

    def test_prefer_native_tools(self) -> None:
        t = _text(TEC)
        assert "native" in t, "TEC-PROC-WORKTREE-001 missing the prefer-native-tools step"
        assert ("enterworktree" in t or "--worktree" in t or "worktree tool" in t), (
            "TEC-PROC-WORKTREE-001 does not name a native worktree tool"
        )

    def test_retains_existing_procedure(self) -> None:
        # regression guard: the pre-existing how-to survives the enrichment.
        t = _text(TEC)
        assert "gitignore" in t or "check-ignore" in t or "ignored" in t, "TEC lost gitignore safety"
        assert "baseline" in t, "TEC lost the baseline-tests step"

    def test_within_budget(self) -> None:
        words = word_count(_body(TEC))
        assert words <= BODY_WORD_BUDGET, (
            f"TEC-PROC-WORKTREE-001 body {words} words > {BODY_WORD_BUDGET} (ENF-META-CONCISE-001)"
        )


class TestAntValid:
    def test_parses_and_validates(self) -> None:
        n = _node(ANT)
        assert n.get("node_type") == "AntiPattern", f"{ANT_ID} is not an AntiPattern"
        assert n.get("antipattern_id") == ANT_ID, f"{ANT_ID} id mismatch"
        try:
            validate_parsed_node(n)
        except (ValueError, ValidationError) as e:
            pytest.fail(f"{ANT_ID} invalid: {e}")

    def test_within_budget(self) -> None:
        words = word_count(_body(ANT))
        assert words <= 200, f"{ANT_ID} body {words} words > 200 (ENF-META-CONCISE-001)"

    def test_carries_vocabulary(self) -> None:
        t = _text(ANT)
        assert "native" in t, f"{ANT_ID} omits the native-tool failure mode"
        assert "nested" in t, f"{ANT_ID} omits the nested-worktree failure mode"

    def test_counters_skill_and_relates_to_tec(self) -> None:
        # COUNTERS target must be canonical {Skill, Playbook, Rule} (INC-2): counter the SKILL,
        # like ANT-PROC-PLAN-001 -> SKL-PROC-PLAN-001. Link the Technique via ungoverned RELATED_TO.
        edges = _edges(ANT)
        assert any(
            e.get("type") == "COUNTERS" and e.get("target") == "SKL-PROC-WORKTREE-001" for e in edges
        ), f"{ANT_ID} missing COUNTERS -> SKL-PROC-WORKTREE-001"
        assert any(
            e.get("type") == "RELATED_TO" and e.get("target") == "TEC-PROC-WORKTREE-001"
            for e in edges
        ), f"{ANT_ID} missing RELATED_TO -> TEC-PROC-WORKTREE-001"

    def test_no_reverse(self) -> None:
        all_edges = []
        for f in METH.glob("*.md"):
            all_edges.extend(parse_edges_from_file(f))
        for target in ("SKL-PROC-WORKTREE-001", "TEC-PROC-WORKTREE-001"):
            reverse = [
                e for e in all_edges
                if e.get("source") == target and e.get("target") == ANT_ID
            ]
            assert not reverse, f"reverse edge {target} -> {ANT_ID} present (non-canonical)"


class TestCensus:
    def test_ant_count(self) -> None:
        # 13 -> 14 on 2026-08-08 with ANT-PROC-TDD-007 (an absence claim whose search scope
        # is narrower than the universe it claims to cover). Deliberate addition, not drift:
        # this is the only ANT-*.md census in the suite, so it is the one that has to move.
        n = len(list(METH.glob("ANT-*.md")))
        assert n == 14, f"expected 14 ANT-*.md, found {n}"


class TestLiveGates:
    """Uses the shared session-scoped `live_pipeline` + methodology_* fixtures (conftest, POL-2)."""

    def test_ant_not_a_near_duplicate(self, live_pipeline) -> None:
        from writ.authoring import check_redundancy

        n = _node(ANT)
        flagged = check_redundancy(
            {"trigger": n.get("trigger", ""), "statement": n.get("statement", "")},
            live_pipeline,
        )
        others = [f for f in flagged if f["rule_id"] != ANT_ID]
        assert not others, (
            f"{ANT_ID} near-duplicate (cosine >= 0.95) of: "
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
            f"bundle_completeness {completeness:.4f} < {BLOCKER_COMPLETENESS} after the "
            f"worktree TEC edit + new ANT; retrieval perturbed (see INC-8/INC-9)."
        )
