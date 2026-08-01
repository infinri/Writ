"""POL-1b: tier->mode migration completeness (Wave 4 coherence cleanup).

POL-1 removed the server deny + vestigial playbooks + the ai-checklist tier block. POL-1b removes
the last dead Phase A-D / tier-gate references:
  - bin/lib/gate-categories.json: the `categories` array (Phase A-D tier model carrying the
    deleted ENF-GATE-000/001/002/003/005) is vestigial -- the live gate reads only `exclusions`
    (+ `framework_detection` at writ-session.py:1884). Removed; the two live keys are kept.
  - writ/graph/schema.py: ENF-GATE-FINAL was a rule-id-regex format example -> live ENF-GATE-007.
  - bin/run-analysis.sh: a finding tagged with the deleted ENF-GATE-FINAL -> retagged.

Verified-live and KEPT: ENF-GATE-006/007 (rules) and ENF-GATE-PLAN/TEST/MODE (gate labels the
machine emits). Pure filesystem/source assertions (always run; no daemon). docs/ is excluded as
intentional historical record.
"""
from __future__ import annotations

import json
from pathlib import Path

from tests._bible_guard import requires_bible

pytestmark = requires_bible


WRIT_ROOT = Path(__file__).resolve().parent.parent
GATE_CATEGORIES = WRIT_ROOT / "bin" / "lib" / "gate-categories.json"
SCHEMA = WRIT_ROOT / "writ" / "graph" / "schema.py"
RUN_ANALYSIS = WRIT_ROOT / "bin" / "run-analysis.sh"
SESSION = WRIT_ROOT / "bin" / "lib" / "writ-session.py"
REASONING = WRIT_ROOT / "bible" / "enforcement" / "reasoning-discipline.md"

# Deleted tier-gate ids that must not remain anywhere in the live tree.
DELETED_TIER_IDS = [
    "ENF-GATE-000", "ENF-GATE-001", "ENF-GATE-002", "ENF-GATE-003",
    "ENF-GATE-005", "ENF-GATE-FINAL", "ENF-ROUTE-001",
]
# Live tree to sweep (docs/ excluded as historical audit record).
LIVE_TREE = [WRIT_ROOT / "writ", WRIT_ROOT / "bin", WRIT_ROOT / "bible"]


class TestGateCategoriesTrimmed:
    def test_parses_and_keeps_live_keys(self) -> None:
        config = json.loads(GATE_CATEGORIES.read_text(encoding="utf-8"))
        assert config.get("exclusions"), "gate-categories.json lost its live `exclusions`"
        assert config.get("framework_detection"), (
            "gate-categories.json lost its live `framework_detection`"
        )

    def test_vestigial_categories_removed(self) -> None:
        config = json.loads(GATE_CATEGORIES.read_text(encoding="utf-8"))
        # The Phase A-D categories array is removed (key absent) or emptied.
        assert not config.get("categories"), (
            "gate-categories.json still carries the vestigial Phase A-D `categories` array"
        )

    def test_no_deleted_ids_in_file(self) -> None:
        text = GATE_CATEGORIES.read_text(encoding="utf-8")
        present = [rid for rid in DELETED_TIER_IDS if rid in text]
        assert not present, f"gate-categories.json still references deleted ids: {present}"


class TestStaleExampleRefsFixed:
    def test_schema_example_not_deleted_rule(self) -> None:
        text = SCHEMA.read_text(encoding="utf-8")
        assert "ENF-GATE-FINAL" not in text, (
            "writ/graph/schema.py still uses the deleted ENF-GATE-FINAL as a format example"
        )

    def test_run_analysis_retagged(self) -> None:
        text = RUN_ANALYSIS.read_text(encoding="utf-8")
        assert "ENF-GATE-FINAL" not in text, (
            "bin/run-analysis.sh still tags a finding with the deleted ENF-GATE-FINAL"
        )


class TestNoDeletedTierIdInLiveTree:
    def test_live_tree_clean(self) -> None:
        offenders: list[str] = []
        for root in LIVE_TREE:
            for p in root.rglob("*"):
                if not p.is_file() or p.suffix not in (".py", ".sh", ".json", ".md"):
                    continue
                try:
                    text = p.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    continue
                for rid in DELETED_TIER_IDS:
                    if rid in text:
                        offenders.append(f"{p.relative_to(WRIT_ROOT)} -> {rid}")
        assert not offenders, (
            "deleted tier-gate ids still present in the live tree:\n  " + "\n  ".join(offenders)
        )


class TestLiveIdsPreserved:
    """Guard against over-deletion: the live rules + gate labels must survive."""

    def test_live_rules_still_defined(self) -> None:
        text = REASONING.read_text(encoding="utf-8")
        for rid in ("ENF-GATE-006", "ENF-GATE-007"):
            assert rid in text, f"{rid} (a live rule) was lost from reasoning-discipline.md"

    def test_live_gate_labels_present(self) -> None:
        # POL-6e moved the write-gate logic (and its labels) into writ/session/gates.py.
        text = (WRIT_ROOT / "writ" / "session" / "gates.py").read_text(encoding="utf-8")
        for label in ("ENF-GATE-PLAN", "ENF-GATE-TEST"):
            assert label in text, f"{label} (a live gate label) was lost from gates.py"
