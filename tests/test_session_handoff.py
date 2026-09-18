"""Session handoff (roadmap 18c): the document is DERIVED, never summarized.

Every section comes from state Writ already holds, so these tests drive the pure
renderer over a synthetic cache and plan text. No graph, no daemon, no live
session (TEST-ISOLATE-001). The empty cases are the likely ones at a real
compaction boundary, not edge trivia (TEST-EDGE-001).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from writ.session.handoff import HANDOFF_DIR, handoff_path, render_handoff

_PLAN = """# Plan: something

## Files

- `a/b.py` (modify) -- the reason
- `c/d.py` (create) -- another reason

## Analysis

Prose that must not leak into the handoff's file list.

## Capabilities

- [x] This one is done
- [ ] This one is not
- [ ] Neither is this
"""


def _cache(**over) -> dict:
    base = {
        "mode": "work",
        "current_phase": "implementation",
        "gates_approved": ["phase-a", "test-skeletons"],
        "loaded_rule_ids": ["API-ERROR-001", "CLEAN-DEAD-001"],
        "files_written": ["/repo/x.py", "/repo/y.py"],
    }
    base.update(over)
    return base


class TestHandoffIsDerivedFromExistingState:
    def test_mode_phase_and_gates_appear(self):
        doc = render_handoff("sess-1", _cache(), _PLAN)
        assert "work" in doc
        assert "implementation" in doc
        assert "phase-a" in doc and "test-skeletons" in doc

    def test_files_written_appear(self):
        doc = render_handoff("sess-1", _cache(), _PLAN)
        assert "/repo/x.py" in doc and "/repo/y.py" in doc

    def test_loaded_rule_ids_appear(self):
        doc = render_handoff("sess-1", _cache(), _PLAN)
        assert "API-ERROR-001" in doc and "CLEAN-DEAD-001" in doc

    def test_the_rule_list_does_not_dominate_the_document(self):
        """Rendering a real session put 220 of 260 lines into a one-per-line rule
        dump, burying every section a successor needs. The ids stay; the shape
        does not. Found by reading the output, not by a fixture."""
        many = [f"RULE-{i:03d}" for i in range(220)]
        doc = render_handoff("sess-1", _cache(loaded_rule_ids=many), _PLAN)
        assert "220 rules" in doc
        bullets = [ln for ln in doc.splitlines() if ln.startswith("- RULE-")]
        assert bullets == [], "ids must be wrapped, not one bullet per line"
        assert len(doc.splitlines()) < 90, len(doc.splitlines())
        assert "RULE-000" in doc and "RULE-219" in doc

    def test_wrapping_never_splits_a_rule_id(self):
        """textwrap breaks on hyphens by default, which turns ABS-SECURITY-031 into
        two half-ids across a line break: not copy-pasteable, and it reads as
        corruption. Both break_on_hyphens and break_long_words must stay off."""
        ids = [f"ABS-VERY-LONG-DOMAIN-NAME-{i:03d}" for i in range(40)]
        doc = render_handoff("sess-1", _cache(loaded_rule_ids=ids), _PLAN)
        for rid in ids:
            assert rid in doc, f"{rid} was split across a line break"

    def test_the_session_id_is_named(self):
        assert "sess-1" in render_handoff("sess-1", _cache(), _PLAN)

    def test_no_prose_is_invented(self):
        """The renderer must not summarize. Analysis prose is not a handoff field,
        so its text must not appear anywhere in the document."""
        doc = render_handoff("sess-1", _cache(), _PLAN)
        assert "must not leak" not in doc


class TestPlanFilesAreCarried:
    def test_plan_file_entries_appear(self):
        doc = render_handoff("sess-1", _cache(), _PLAN)
        assert "a/b.py" in doc and "c/d.py" in doc


class TestOpenItemsComeFromUncheckedBoxes:
    def test_unchecked_boxes_are_open_items(self):
        doc = render_handoff("sess-1", _cache(), _PLAN)
        assert "This one is not" in doc
        assert "Neither is this" in doc

    def test_checked_boxes_are_not_open_items(self):
        doc = render_handoff("sess-1", _cache(), _PLAN)
        assert "This one is done" not in doc


class TestEmptyCasesProduceAValidDocument:
    def test_no_plan_at_all(self):
        doc = render_handoff("sess-1", _cache(), "")
        assert "sess-1" in doc
        assert doc.strip()

    def test_nothing_written_yet(self):
        doc = render_handoff("sess-1", _cache(files_written=[]), _PLAN)
        assert "sess-1" in doc
        assert doc.strip()

    def test_every_box_checked(self):
        plan = _PLAN.replace("- [ ]", "- [x]")
        doc = render_handoff("sess-1", _cache(), plan)
        assert doc.strip()
        assert "This one is not" not in doc

    def test_an_empty_cache(self):
        doc = render_handoff("sess-1", {}, "")
        assert "sess-1" in doc
        assert doc.strip()


class TestHandoffPath:
    def test_path_is_session_scoped_and_not_a_slice_file(self, tmp_path):
        p = handoff_path("sess-1", str(tmp_path))
        assert p.name == "session-sess-1.md"
        # Must NOT match the slice validator's glob (.claude/handoffs/slice-*.json),
        # which is registered at hooks/hooks.json:373 for a different artifact.
        assert not p.name.startswith("slice-")
        assert p.suffix != ".json"
        assert p.parent.name == "handoffs"


class TestGeneratedHandoffsAreNotCommittable:
    """A generated directory holding absolute paths from the developer's machine
    must not be stageable. Its siblings .claude/gates/, .claude/plans/ and
    .claude/debug/ are ignored for the same reason."""

    def test_the_handoff_directory_is_gitignored(self):
        repo = Path(__file__).resolve().parent.parent
        probe = f"{HANDOFF_DIR}/session-probe.md"
        r = subprocess.run(
            ["git", "check-ignore", "-q", probe],
            cwd=repo, capture_output=True,
        )
        assert r.returncode == 0, (
            f"{probe} is not gitignored, so a generated handoff carrying absolute "
            f"local paths can be staged and committed."
        )
