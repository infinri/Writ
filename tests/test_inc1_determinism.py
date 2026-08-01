"""INC-1: Neo4j test determinism (NRV-0).

The full suite must not be able to report green while a graph-dependent test silently
skips a real failure (the FIX-5 masking class). The fix introduces, in tests/_corpus.py:

  - classify_corpus_state(reachable, rule_count, subagent_count) -> 'unreachable' | 'empty'
    | 'ready'  -- the anti-masking classifier: a reachable-but-empty graph is 'empty'
    (tests must FAIL, not skip); only an unreachable Neo4j is a legitimate skip.
  - ensure_corpus() -- if the live graph is missing methodology nodes, re-import bible/ so
    it is complete (self-heal); idempotent.
  - methodology_counts() / neo4j_reachable() / clear_label() -- live helpers.

and a `corpus_ready` fixture (tests/conftest.py) that calls ensure_corpus() and skips ONLY
on a true connection error.

The classifier test is pure (always runs). Live tests skip only when Neo4j is unreachable.
"""
from __future__ import annotations

import pytest

from tests._corpus import (


    classify_corpus_state,
    clear_label,
    ensure_corpus,
    methodology_counts,
    neo4j_reachable,
)

from tests._bible_guard import requires_bible

pytestmark = requires_bible


# Floor counts for the "corpus is present/complete" check (NOT an exact census -- exact
# per-type counts are pinned by tests/test_phase6efg_corpus_promotion.py). Asserted with >=
# so adding methodology nodes never false-fails this determinism test; a count DROPPING below
# the floor still fails (signals a wiped/partial graph, the masking condition this guards).
EXPECT_SUBAGENT = 5
EXPECT_PLAYBOOK = 15
EXPECT_SKILL = 13
EXPECT_PHASE = 20
MIN_RULES = 280


# --- 1. Anti-masking classifier (pure, always runs) --------------------------


class TestClassifier:
    def test_unreachable_is_skippable(self) -> None:
        # Neo4j down: the only legitimate reason to skip a graph test.
        assert classify_corpus_state(reachable=False, rule_count=0, subagent_count=0) == "unreachable"

    def test_reachable_but_empty_is_not_skippable(self) -> None:
        # The masking condition: graph reachable but corpus absent -> 'empty' (tests FAIL).
        assert classify_corpus_state(reachable=True, rule_count=0, subagent_count=0) == "empty"
        assert classify_corpus_state(reachable=True, rule_count=283, subagent_count=0) == "empty"

    def test_complete_corpus_is_ready(self) -> None:
        assert classify_corpus_state(reachable=True, rule_count=283, subagent_count=6) == "ready"

    def test_empty_never_classifies_as_unreachable(self) -> None:
        # Regression guard against the FIX-5 masking: empty must never look like 'unreachable'.
        assert classify_corpus_state(reachable=True, rule_count=0, subagent_count=0) != "unreachable"


# --- 2. corpus_ready guarantees a complete graph (live) ----------------------


class TestCorpusReadyCompleteness:
    def test_all_methodology_types_present(self, corpus_ready) -> None:
        if not neo4j_reachable():
            pytest.skip("Neo4j unreachable")
        c = methodology_counts()
        assert c.get("Rule", 0) >= MIN_RULES, c
        assert c.get("SubagentRole", 0) >= EXPECT_SUBAGENT, c
        assert c.get("Playbook", 0) >= EXPECT_PLAYBOOK, c
        assert c.get("Skill", 0) >= EXPECT_SKILL, c
        assert c.get("Phase", 0) >= EXPECT_PHASE, c


# --- 3. ensure_corpus self-heals a wiped graph (live) ------------------------


class TestSelfHeal:
    def test_ensure_corpus_restores_wiped_methodology(self, corpus_ready) -> None:
        if not neo4j_reachable():
            pytest.skip("Neo4j unreachable")
        # Simulate the masking precondition: wipe SubagentRole nodes.
        deleted = clear_label("SubagentRole")
        assert deleted >= EXPECT_SUBAGENT
        assert methodology_counts().get("SubagentRole", 0) == 0
        # ensure_corpus must detect the gap and re-import.
        ensure_corpus()
        assert methodology_counts().get("SubagentRole", 0) == EXPECT_SUBAGENT


# --- 4. the previously-skip-prone tests now run when reachable ---------------


class TestNoMaskingRemains:
    def test_graph_present_means_graph_tests_run(self, corpus_ready) -> None:
        """When Neo4j is reachable, the corpus is complete, so FIX-5/FIX-6/phase3b graph
        tests have no empty-graph escape hatch -- they must run and assert."""
        if not neo4j_reachable():
            pytest.skip("Neo4j unreachable")
        state = classify_corpus_state(
            reachable=True,
            rule_count=methodology_counts().get("Rule", 0),
            subagent_count=methodology_counts().get("SubagentRole", 0),
        )
        assert state == "ready", f"corpus_ready left the graph in state={state}"
