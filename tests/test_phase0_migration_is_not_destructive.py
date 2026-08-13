"""A spent one-time migration must not flatten hand-authored corrections.

THE DEFECT. `scripts/migrate_phase0_categories.py::apply_plan` writes all 22
Category files and SKL-PROC-DEBUG-001 with an unconditional `path.write_text`,
from a hardcoded template (`_category_node_text`, :411-437). The Phase-0
migration it performs already ran, so every one of those files has since been
edited by hand, and re-running the script would overwrite the edits.

Measured against the live CAT-DISC-001 before this change, one line diverged:

    live:   trigger: "Routing metadata for the Discipline counters category; the
            Category node itself is not a retrieval candidate, and its members
            surface through the pull channel."
    script: trigger: "Routing metadata for the Discipline counters category; not
            retrievable."

That correction is not cosmetic. CAT-DISC-001 moved onto the `pull` route in
cycle 6a precisely so its 14 AntiPatterns could be delivered, and the template
still describes the category as unreachable. A re-apply would restore prose that
contradicts the routing data next to it.

WHY SKIP RATHER THAN FIX THE TEMPLATE. Making the template reproduce today's
prose would be correct exactly until the next hand edit, which is the same trap
one layer along. The script's job is to CREATE these files where they do not
exist; owning their contents forever is not its job.

This is the seventh instance this session of fixing an instance and leaving what
regenerates it: cycle 6a fixed the seed's ROUTES (CATEGORY_DEFS carries
["pull"] and both dead routes are gone) and left the seed's TEXT.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "migrate_phase0_categories.py"


def _load_script():
    """Import the script as a module.

    Safe: it guards its entry point with `if __name__ == "__main__"` (:635), so
    importing runs no migration. Verified before this test was written, because
    importing a module that mutates disk at import time is how a test suite
    rewrites a corpus.
    """
    spec = importlib.util.spec_from_file_location("_migrate_phase0", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_migrate_phase0"] = mod
    spec.loader.exec_module(mod)
    return mod


class TestApplyPlanPreservesExistingFiles:
    def test_an_existing_category_file_is_not_overwritten(self, tmp_path, monkeypatch) -> None:
        mod = _load_script()
        monkeypatch.setattr(mod, "METHODOLOGY", tmp_path)
        monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)

        category_id = mod.CATEGORY_DEFS[0][0]
        existing = tmp_path / f"{category_id}.md"
        hand_edited = "---\ncategory_id: KEEP-ME\n---\n\nHand-authored, do not clobber.\n"
        existing.write_text(hand_edited, encoding="utf-8")

        mod.apply_plan(mod.Plan(nodes=[]))

        assert existing.read_text(encoding="utf-8") == hand_edited, (
            "apply_plan overwrote a file that already existed; a spent migration "
            "must not own the contents of files people have since edited"
        )

    def test_a_missing_category_file_is_still_created(self, tmp_path, monkeypatch) -> None:
        """The other half. A skip-if-exists rule that also skipped when the file
        was ABSENT would make the script useless for its actual job, and every
        assertion above would still pass."""
        mod = _load_script()
        monkeypatch.setattr(mod, "METHODOLOGY", tmp_path)
        monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)

        mod.apply_plan(mod.Plan(nodes=[]))

        for category_id, *_ in mod.CATEGORY_DEFS:
            created = tmp_path / f"{category_id}.md"
            assert created.is_file(), f"{category_id}.md was never created"
            assert "category_id" in created.read_text(encoding="utf-8")

    def test_the_skl_node_is_also_preserved(self, tmp_path, monkeypatch) -> None:
        mod = _load_script()
        monkeypatch.setattr(mod, "METHODOLOGY", tmp_path)
        monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)

        skl = tmp_path / "SKL-PROC-DEBUG-001.md"
        hand_edited = "---\nskill_id: SKL-PROC-DEBUG-001\n---\n\nEdited since Phase 0.\n"
        skl.write_text(hand_edited, encoding="utf-8")

        mod.apply_plan(mod.Plan(nodes=[]))

        assert skl.read_text(encoding="utf-8") == hand_edited, (
            "SKL-PROC-DEBUG-001 is authored from a hardcoded constant and has the "
            "same overwrite problem as the category files"
        )


class TestTheTemplateDivergenceIsReal:
    """Pins the reason this matters, so a future reader does not conclude the
    skip is precautionary. If the template ever DOES match the live file again,
    this test fails and the docstring above needs rewriting rather than quietly
    becoming false."""

    def test_the_template_still_disagrees_with_the_live_category(self) -> None:
        live_path = (
            Path(__file__).resolve().parent.parent
            / "bible" / "methodology" / "CAT-DISC-001.md"
        )
        if not live_path.exists():
            pytest.skip("requires the untracked bible/ tree")

        mod = _load_script()
        generated = mod._category_node_text(
            "CAT-DISC-001", "Discipline counters", ["pull"], None
        )
        live = live_path.read_text(encoding="utf-8")

        assert generated != live, (
            "the template now reproduces the live file exactly; the overwrite "
            "risk this module documents has changed shape and the reasoning "
            "above should be revisited"
        )
        assert "not retrievable." in generated
        assert "not retrievable." not in live, (
            "the live CAT-DISC-001 should carry cycle 6a's corrected trigger, "
            "which no longer claims the category is unreachable"
        )
