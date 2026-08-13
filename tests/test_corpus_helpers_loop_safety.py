"""tests/_corpus.py's sync wrappers must give the same answer from inside a
running event loop as from outside one.

THE DEFECT. `neo4j_reachable`, `methodology_counts` and `clear_label` each end in
`asyncio.run(_q())`. `asyncio.run` RAISES `RuntimeError` when a loop is already
running in the thread, and the coroutine it was handed is then dropped unawaited
(`RuntimeWarning: coroutine '..._q' was never awaited`). `neo4j_reachable` wrapped
that in `except Exception: return False`, so the probe answered "the graph is not
reachable" when what actually happened was "I was called from the wrong context
and never asked".

Measured before the fix, against the same reachable graph, one call each:
    outside a loop -> True
    inside a loop  -> False   + RuntimeWarning: coroutine never awaited

WHY IT IS WORSE THAN A STRAY WARNING. `classify_corpus_state` (tests/_corpus.py:31-49)
makes 'unreachable' the ONE legitimate reason a graph test may skip, written that way
because an empty graph once masked a real regression as a skip. A reachability probe
that returns False on its own internal dispatch failure therefore hands every caller
the single answer licensed to skip, for a reason that is not true.

Observed in the wild via tests/test_export.py::TestExportGraphToMarkdown, which
reached `neo4j_reachable` from inside a running loop in both a half-suite and a
full-suite run.
"""
from __future__ import annotations

import asyncio
import warnings

import pytest

from tests import _corpus


def _reachable_outside() -> bool:
    return _corpus.neo4j_reachable()


class TestNeo4jReachableIsLoopAgnostic:
    """The load-bearing pair. Both calls hit the SAME graph, so any difference
    between them is the wrapper inventing an answer rather than reporting one."""

    def test_same_answer_inside_and_outside_a_running_loop(self) -> None:
        outside = _reachable_outside()

        async def _inside() -> bool:
            return _corpus.neo4j_reachable()

        inside = asyncio.run(_inside())

        assert inside == outside, (
            "neo4j_reachable must not depend on whether a loop is already "
            f"running: outside={outside}, inside={inside}. A False from inside "
            "a loop is asyncio.run raising, not the graph being unreachable"
        )

    def test_no_coroutine_is_dropped_unawaited(self) -> None:
        """The warning is the fingerprint of the bug, so pin its absence
        directly. Without this, a fix that swallowed the RuntimeError more
        quietly would still pass the test above whenever the graph happens to
        be unreachable and both answers are False."""
        async def _inside() -> bool:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                _corpus.neo4j_reachable()
                dropped = [
                    w for w in caught
                    if issubclass(w.category, RuntimeWarning)
                    and "never awaited" in str(w.message)
                ]
            return not dropped

        assert asyncio.run(_inside()), (
            "a coroutine was created and never awaited inside a running loop; "
            "asyncio.run cannot dispatch there"
        )


class TestOtherSyncWrappersDispatchFromALoop:
    """methodology_counts and clear_label end in the same `asyncio.run(_q())`.
    Neither is wrapped in a broad except, so from inside a loop they raise
    RuntimeError rather than lying, which is less dangerous and still wrong."""

    def test_methodology_counts_works_from_inside_a_loop(self) -> None:
        if not _reachable_outside():
            pytest.skip("graph unreachable, so there is no count to compare")

        outside = _corpus.methodology_counts()

        async def _inside() -> dict:
            return _corpus.methodology_counts()

        assert asyncio.run(_inside()) == outside

    def test_clear_label_is_callable_from_inside_a_loop(self) -> None:
        """Uses a label that does not exist, so the count is 0 and the DETACH
        DELETE matches nothing. This asserts the DISPATCH works from a loop and
        deliberately deletes nothing: the suite shares one graph between
        modules, so a test that wipes a real label to prove a wrapper works
        would be trading a bug for a worse one."""
        if not _reachable_outside():
            pytest.skip("graph unreachable")

        async def _inside() -> int:
            return _corpus.clear_label("ZzzNoSuchLabelCycle8LoopSafety")

        assert asyncio.run(_inside()) == 0
