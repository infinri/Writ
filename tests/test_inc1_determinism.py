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
    EXPECTED,
    classify_corpus_state,
    clear_label,
    ensure_corpus,
    methodology_counts,
    neo4j_reachable,
)

from tests._bible_guard import requires_bible

pytestmark = requires_bible


def _floor() -> dict[str, int]:
    """The corpus floor, derived from the tracked writ-corpus.cypher.

    THE FIVE HAND-WRITTEN LITERALS THAT USED TO LIVE HERE ARE GONE (plan.md
    2412ba38-51e1-4b73-895b-7b240a3c21d3, defect 1). They floored five labels out of the
    thirteen the corpus declares, so a graph holding zero AntiPattern, Technique,
    ForbiddenResponse, PressureScenario, Rationalization or WorkedExample passed this
    module's live check, and they were a second copy of the numbers in tests/_corpus.py
    besides. Still asserted with >=, so adding methodology nodes never false-fails this
    determinism test; a count dropping below the floor still fails, which is the
    wiped-or-partial graph this module exists to catch.
    """
    try:
        from tests._inventory import corpus_floor
    except ImportError as exc:
        pytest.fail(
            "skeleton: tests/_inventory.py has no corpus_floor() yet (plan.md ## Files "
            f"assigns the source-derived corpus population there): {exc}",
            pytrace=False,
        )
    floor = corpus_floor()
    assert floor, "corpus_floor() derived no labels; the live check below would be vacuous"
    return floor


# --- 1. Anti-masking classifier (pure, always runs) --------------------------


class TestClassifier:
    def test_unreachable_is_skippable(self) -> None:
        # Neo4j down: the only legitimate reason to skip a graph test.
        assert classify_corpus_state(reachable=False, rule_count=0, subagent_count=0) == "unreachable"

    def test_reachable_but_empty_is_not_skippable(self) -> None:
        # The masking condition: graph reachable but corpus absent -> 'empty' (tests FAIL).
        # The rule count is the FLOOR, not a literal: written as 283 it used to isolate
        # "rules present, subagents absent", and a floor that moved past it would have
        # left this case reading 'empty' because BOTH inputs were short, which is a
        # different claim wearing the same green.
        assert classify_corpus_state(reachable=True, rule_count=0, subagent_count=0) == "empty"
        assert classify_corpus_state(
            reachable=True, rule_count=EXPECTED["Rule"], subagent_count=0
        ) == "empty"

    def test_complete_corpus_is_ready(self) -> None:
        # Both inputs derived, for the reason the five deleted literals above were: a
        # hand-written "complete enough" count is pinned to whatever the floor happened
        # to be the day it was typed, and the floor now comes from the tracked dump.
        assert classify_corpus_state(
            reachable=True,
            rule_count=EXPECTED["Rule"],
            subagent_count=EXPECTED["SubagentRole"],
        ) == "ready"

    def test_empty_never_classifies_as_unreachable(self) -> None:
        # Regression guard against the FIX-5 masking: empty must never look like 'unreachable'.
        assert classify_corpus_state(reachable=True, rule_count=0, subagent_count=0) != "unreachable"


# --- 2. corpus_ready guarantees a complete graph (live) ----------------------


class TestCorpusReadyCompleteness:
    def test_all_methodology_types_present(self, corpus_ready) -> None:
        """Capability 10: the live census meets the DERIVED floor for every declared
        label, not for the five somebody wrote down.

        The verdict is a MAP keyed by label, so a corpus that decayed in one label fails
        BY NAME with its live and required count, instead of as an arithmetic mismatch
        that leaves the reader to work out which label moved.
        """
        if not neo4j_reachable():
            pytest.skip("Neo4j unreachable")
        floor = _floor()
        c = methodology_counts()
        short = {
            label: (c.get(label, 0), required)
            for label, required in floor.items()
            if c.get(label, 0) < required
        }
        assert not short, (
            f"the live graph is below the corpus floor in {sorted(short)}: "
            f"{ {label: f'{live} of {required}' for label, (live, required) in short.items()} }"
        )


# --- 3. ensure_corpus self-heals a wiped graph (live) ------------------------


class TestSelfHeal:
    def test_ensure_corpus_restores_wiped_methodology(self, corpus_ready) -> None:
        if not neo4j_reachable():
            pytest.skip("Neo4j unreachable")
        # Simulate the masking precondition: wipe SubagentRole nodes.
        deleted = clear_label("SubagentRole")
        assert deleted >= EXPECTED["SubagentRole"]
        assert methodology_counts().get("SubagentRole", 0) == 0
        # ensure_corpus must detect the gap and re-import.
        ensure_corpus()
        assert methodology_counts().get("SubagentRole", 0) == EXPECTED["SubagentRole"]


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
