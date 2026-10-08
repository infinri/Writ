"""Change C: retire APPLIES_TO and JUSTIFIED_BY edge types.

These two edge types are dead:
  - APPLIES_TO is superseded by the `scope` property on Rule nodes.
  - JUSTIFIED_BY requires an Evidence-node layer that is not yet wired.

The incoming change removes both from ALLOWED_EDGE_TYPES (count 19 -> 17) and
drops the AppliesTo/JustifiedBy Pydantic models from writ/graph/schema.py.

All three tests are RED today because both types are present and the count is 19.
"""
from __future__ import annotations


class TestEdgeTypeRetirement:
    """Pin that the two retired edge types are absent; the total has since grown as
    later features added edge types (e.g. the decision-memory record edges), so the
    count tripwire tracks the current reviewed baseline rather than the retirement-era 17.
    """

    def test_applies_to_retired(self) -> None:
        """APPLIES_TO must not be in ALLOWED_EDGE_TYPES after the retirement.

        RED today: 'APPLIES_TO' is in ALLOWED_EDGE_TYPES (pre-existing type).
        """
        from writ.graph.db import ALLOWED_EDGE_TYPES

        assert "APPLIES_TO" not in ALLOWED_EDGE_TYPES, (
            "APPLIES_TO is still in ALLOWED_EDGE_TYPES; it must be removed. "
            "The `scope` property on Rule nodes supersedes this edge type."
        )

    def test_justified_by_retired(self) -> None:
        """JUSTIFIED_BY must not be in ALLOWED_EDGE_TYPES after the retirement.

        RED today: 'JUSTIFIED_BY' is in ALLOWED_EDGE_TYPES (pre-existing type).
        """
        from writ.graph.db import ALLOWED_EDGE_TYPES

        assert "JUSTIFIED_BY" not in ALLOWED_EDGE_TYPES, (
            "JUSTIFIED_BY is still in ALLOWED_EDGE_TYPES; it must be removed. "
            "The Evidence-node layer it requires is not yet wired."
        )

    def test_edge_count_matches_allowed_set(self) -> None:
        """ALLOWED_EDGE_TYPES must contain exactly 25 entries: the 17 that remained
        after APPLIES_TO and JUSTIFIED_BY were retired, plus the 7 record-memory edge
        types the decision-memory feature added later, plus ABOUT (open questions,
        program item 7a, 2026-10-08). Drift tripwire pinned to the
        current reviewed baseline.
        """
        from writ.graph.db import ALLOWED_EDGE_TYPES

        assert len(ALLOWED_EDGE_TYPES) == 25, (
            f"ALLOWED_EDGE_TYPES has {len(ALLOWED_EDGE_TYPES)} entries; expected 25 "
            "(the current reviewed baseline: 17 post-retirement + 7 record-memory edges + ABOUT). "
            f"Current set: {sorted(ALLOWED_EDGE_TYPES)!r}"
        )
