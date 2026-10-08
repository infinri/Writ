"""Wave 1 Cycle 4 S5: writ add offers the single-source corpus edge set, not a
stale hardcoded copy that rejects valid types (PRECEDES/SUPERSEDES/TEACHES).

writ/graph/db.py's ALLOWED_EDGE_TYPES holds 24 types (17 corpus + 7 record-only
edges that attach runtime Decision/FileChange/Commit records, never rule-to-rule).
writ/cli.py's `add` command gates on a stale local 4-item list
(["DEPENDS_ON", "SUPPLEMENTS", "CONFLICTS_WITH", "RELATED_TO"]) instead of a
CORPUS_EDGE_TYPES derived constant. RED today: CORPUS_EDGE_TYPES does not exist
(ImportError) and cli.py still carries the stale 4-item list.

Per TEST-TDD-001: skeletons approved before implementation.
"""
from __future__ import annotations

import os

SKILL_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


class TestCorpusEdgeTypesSingleSource:
    def test_corpus_is_allowed_minus_record(self) -> None:
        from writ.graph.db import ALLOWED_EDGE_TYPES, CORPUS_EDGE_TYPES, RECORD_EDGE_TYPES
        assert CORPUS_EDGE_TYPES == ALLOWED_EDGE_TYPES - RECORD_EDGE_TYPES

    def test_corpus_includes_previously_rejected_valid_types(self) -> None:
        from writ.graph.db import CORPUS_EDGE_TYPES
        for t in ("PRECEDES", "SUPERSEDES", "TEACHES", "DEPENDS_ON", "RELATED_TO"):
            assert t in CORPUS_EDGE_TYPES, f"{t} must be offered by writ add"

    def test_corpus_excludes_record_edges(self) -> None:
        from writ.graph.db import CORPUS_EDGE_TYPES
        for t in ("HAS_DECISION", "HAS_CHANGE", "HAS_COMMIT",
                  "MOTIVATED_BY", "GOVERNED_BY", "INCLUDES", "REALIZES", "ABOUT"):
            assert t not in CORPUS_EDGE_TYPES, (
                f"{t} is a record-only edge; it must not be offered for "
                f"rule-to-rule authoring"
            )

    def test_record_edge_types_has_eight_entries(self) -> None:
        from writ.graph.db import RECORD_EDGE_TYPES
        assert len(RECORD_EDGE_TYPES) == 8

    def test_corpus_and_allowed_agree_on_total_count(self) -> None:
        """25 allowed total = 17 corpus + 8 record; guards a future edge-type
        addition from accidentally landing in neither/both sets."""
        from writ.graph.db import ALLOWED_EDGE_TYPES, CORPUS_EDGE_TYPES, RECORD_EDGE_TYPES
        assert len(ALLOWED_EDGE_TYPES) == len(CORPUS_EDGE_TYPES) + len(RECORD_EDGE_TYPES)
        assert CORPUS_EDGE_TYPES & RECORD_EDGE_TYPES == set(), (
            "corpus and record edge sets must be disjoint"
        )


class TestCliAddUsesSingleSource:
    def test_cli_add_uses_single_source_not_stale_copy(self) -> None:
        src = open(os.path.join(SKILL_ROOT, "writ", "cli.py")).read()
        assert "CORPUS_EDGE_TYPES" in src, (
            "writ/cli.py must import CORPUS_EDGE_TYPES from writ.graph.db"
        )
        assert '["DEPENDS_ON", "SUPPLEMENTS", "CONFLICTS_WITH", "RELATED_TO"]' not in src, (
            "the stale hardcoded 4-item edge-type list must be removed"
        )
