"""POL-2b: suite-time perf -- warm-graph skip (E3) + N+1 count batch (E5).

E5: tests/_corpus.methodology_counts() issued one Cypher query per label (N+1, on the
corpus_ready path of every graph-dependent test); rewritten to a single UNWIND-labels scan.
E3: conftest.pytest_sessionstart imported the corpus unconditionally every suite; now skips the
redundant import when the graph is already complete (graph_is_warm()).

Correctness assertions (the perf is a side effect verified by the unchanged full suite). The
graph-touching checks run under corpus_ready (skip only when Neo4j is unreachable).
"""
from __future__ import annotations

import re
from pathlib import Path

from tests._bible_guard import requires_bible

pytestmark = requires_bible


WRIT_ROOT = Path(__file__).resolve().parent.parent
CORPUS_PY = WRIT_ROOT / "tests" / "_corpus.py"
CONFTEST = WRIT_ROOT / "tests" / "conftest.py"


class TestE5CountsBatched:
    def test_methodology_counts_correct(self, corpus_ready) -> None:
        import asyncio

        from tests._corpus import _connection, methodology_counts

        counts = methodology_counts()
        # every expected label present with an int value
        from tests._corpus import _LABELS

        for lbl in _LABELS:
            assert lbl in counts and isinstance(counts[lbl], int), f"{lbl} missing/non-int"

        # spot-check one label against an independent direct count -- proves the UNWIND rewrite
        # counts identically to a per-label query.
        async def _direct_rule_count() -> int:
            db = _connection()
            try:
                async with db._driver.session(database=db._database) as s:
                    res = await s.run("MATCH (n:Rule) RETURN count(n) AS c")
                    return [r async for r in res][0]["c"]
            finally:
                await db.close()

        assert counts["Rule"] == asyncio.run(_direct_rule_count()), "Rule count drifted from direct query"

    def test_no_per_label_count_loop(self) -> None:
        src = CORPUS_PY.read_text(encoding="utf-8")
        # the N+1 was: `for lbl in _LABELS:` wrapping a per-label `MATCH (n:{lbl}) ... count`.
        assert not re.search(r"for\s+lbl\s+in\s+_LABELS\s*:", src), (
            "_corpus.py still loops per label for counts (the N+1 E5 removed)"
        )
        assert "unwind labels(n)" in src.lower(), (
            "_corpus.methodology_counts should use a single UNWIND labels(n) scan"
        )


class TestE3WarmSkip:
    def test_graph_is_warm_true_on_complete_graph(self, corpus_ready) -> None:
        from tests._corpus import graph_is_warm

        assert graph_is_warm() is True, (
            "graph_is_warm() should be True after corpus_ready guarantees a complete graph"
        )

    def test_sessionstart_wires_warm_skip_and_keeps_daemon_align(self) -> None:
        src = CONFTEST.read_text(encoding="utf-8")
        assert "graph_is_warm" in src, "pytest_sessionstart does not use the warm-graph skip"
        # regression: the daemon-alignment step must survive the edit.
        assert "ensure_daemon_aligned" in src, "pytest_sessionstart lost ensure_daemon_aligned"
