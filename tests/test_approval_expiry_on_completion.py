"""An approval should not outlive the work it was granted for.

DEMONSTRATED 2026-09-21 on a live session. Session 2412ba38 still carried
`gates_approved: ['phase-a', 'test-skeletons']` bound to plan hash fb84122192e7,
the 2026-09-18 "sponsor button" cycle. Three days and an unrelated task later that
approval was still live and source writes would have been allowed under it. Gates
clear on `mode set`, on a replan, and on plan drift, and on nothing else.

THE SIGNAL IS REAL AND WAS VERIFIED BEFORE THIS WAS WRITTEN: 129 Decision records,
all carrying planned_files, 1,265 claim entries, 610 resolved, 52 plans already
fully resolved. An earlier query said zero and was wrong: it passed `writ` as the
project key when the real key is the slug `github.com/infinri/Writ`. Absence in the
wrong container is not absence, and an expiry hung on a dead signal never expires.

THE CONDITION IS "EVERY DECLARED PATH", NOT "ANY", so a multi-commit cycle keeps
its approval until the last declared file lands. TestPartialPlanDoesNotExpire is
the half that pins that, and it is the one that would make this feature hostile if
it broke.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).resolve().parent.parent
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))


def _cc():
    return importlib.import_module("writ.session.commit_capture")


PLAN = """# Plan: something real

## Files

- `writ/a.py` (modify) -- the first reason
- `writ/b.py` (create) -- the second reason

## Analysis

Prose naming `writ/never_declared.py` which must not count as declared.

## Rules Applied

- none

## Capabilities

- [ ] one
"""


@pytest.fixture()
def cache_dir(tmp_path, monkeypatch):
    d = tmp_path / "session-cache"
    d.mkdir()
    monkeypatch.setenv("WRIT_CACHE_DIR", str(d))
    return d


def _approved_cache(**over) -> dict:
    base = {
        "mode": "work",
        "current_phase": "implementation",
        "gates_approved": ["phase-a", "test-skeletons"],
        "gates_approved_plan": {"phase-a": "abc123", "test-skeletons": "abc123"},
    }
    base.update(over)
    return base


class TestPlanCompletionPredicate:
    """The pure half: given a plan and the set of resolved paths, is the plan done?"""

    def test_all_declared_paths_resolved_is_complete(self):
        cc = _cc()
        assert cc._plan_is_complete(PLAN, {"writ/a.py", "writ/b.py"}) is True

    def test_extra_resolved_paths_do_not_matter(self):
        cc = _cc()
        assert cc._plan_is_complete(PLAN, {"writ/a.py", "writ/b.py", "writ/z.py"}) is True

    def test_a_plan_with_no_declared_files_cannot_be_judged(self):
        cc = _cc()
        assert cc._plan_is_complete("# Plan\n\n## Analysis\n\nnothing\n", {"writ/a.py"}) is None

    def test_empty_plan_text_cannot_be_judged(self):
        cc = _cc()
        assert cc._plan_is_complete("", {"writ/a.py"}) is None

    def test_prose_only_mention_is_not_a_declaration(self):
        """`writ/never_declared.py` appears only in ## Analysis. Resolving it must
        not complete a plan whose ## Files still has unresolved entries."""
        cc = _cc()
        assert cc._plan_is_complete(PLAN, {"writ/never_declared.py"}) is False


class TestPartialPlanDoesNotExpire:
    """The half that keeps this feature from being hostile: a cycle that commits
    one of its two declared files keeps its approval."""

    def test_one_of_two_resolved_is_not_complete(self):
        cc = _cc()
        assert cc._plan_is_complete(PLAN, {"writ/a.py"}) is False

    def test_none_resolved_is_not_complete(self):
        cc = _cc()
        assert cc._plan_is_complete(PLAN, set()) is False


class TestExpiryClearsTheApproval:
    def test_a_completed_plan_clears_both_gates(self, cache_dir, tmp_path):
        cc = _cc()
        from writ.session.cache import _read_cache, _write_cache
        root = tmp_path / "proj"
        (root / ".claude" / "plans" / "sess-done").mkdir(parents=True)
        (root / ".claude" / "plans" / "sess-done" / "plan.md").write_text(PLAN)
        _write_cache("sess-done", _approved_cache())

        cc._expire_completed_approval("sess-done", str(root), {"writ/a.py", "writ/b.py"})

        after = _read_cache("sess-done")
        assert after.get("gates_approved") == []
        assert not after.get("gates_approved_plan")

    def test_a_partial_plan_leaves_the_approval_alone(self, cache_dir, tmp_path):
        cc = _cc()
        from writ.session.cache import _read_cache, _write_cache
        root = tmp_path / "proj"
        (root / ".claude" / "plans" / "sess-partial").mkdir(parents=True)
        (root / ".claude" / "plans" / "sess-partial" / "plan.md").write_text(PLAN)
        _write_cache("sess-partial", _approved_cache())

        cc._expire_completed_approval("sess-partial", str(root), {"writ/a.py"})

        assert _read_cache("sess-partial").get("gates_approved") == [
            "phase-a", "test-skeletons"]


class TestAbstentions:
    """Every state where nothing establishes that THIS session's work is done."""

    def test_no_session_id_is_a_no_op(self, cache_dir, tmp_path):
        cc = _cc()
        cc._expire_completed_approval("", str(tmp_path), {"writ/a.py"})

    def test_a_session_with_no_plan_keeps_its_approval(self, cache_dir, tmp_path):
        cc = _cc()
        from writ.session.cache import _read_cache, _write_cache
        _write_cache("sess-noplan", _approved_cache())
        cc._expire_completed_approval("sess-noplan", str(tmp_path), {"writ/a.py"})
        assert _read_cache("sess-noplan").get("gates_approved") == [
            "phase-a", "test-skeletons"]

    @pytest.mark.parametrize("mode", ["conversation", "review", "investigate", None])
    def test_a_session_not_in_work_mode_is_untouched(self, cache_dir, tmp_path, mode):
        cc = _cc()
        from writ.session.cache import _read_cache, _write_cache
        sid = f"sess-mode-{mode}"
        root = tmp_path / "proj"
        (root / ".claude" / "plans" / sid).mkdir(parents=True)
        (root / ".claude" / "plans" / sid / "plan.md").write_text(PLAN)
        _write_cache(sid, _approved_cache(mode=mode))
        cc._expire_completed_approval(sid, str(root), {"writ/a.py", "writ/b.py"})
        assert _read_cache(sid).get("gates_approved") == ["phase-a", "test-skeletons"]

    def test_a_session_with_no_gates_is_a_no_op(self, cache_dir, tmp_path):
        cc = _cc()
        from writ.session.cache import _read_cache, _write_cache
        root = tmp_path / "proj"
        (root / ".claude" / "plans" / "sess-nogates").mkdir(parents=True)
        (root / ".claude" / "plans" / "sess-nogates" / "plan.md").write_text(PLAN)
        _write_cache("sess-nogates", _approved_cache(gates_approved=[]))
        cc._expire_completed_approval("sess-nogates", str(root),
                                      {"writ/a.py", "writ/b.py"})
        assert _read_cache("sess-nogates").get("gates_approved") == []


class TestItCanOnlyRemoveAnApproval:
    def test_it_never_adds_a_gate(self, cache_dir, tmp_path):
        """A completed plan on a session with one gate must not end up with two."""
        cc = _cc()
        from writ.session.cache import _read_cache, _write_cache
        root = tmp_path / "proj"
        (root / ".claude" / "plans" / "sess-one").mkdir(parents=True)
        (root / ".claude" / "plans" / "sess-one" / "plan.md").write_text(PLAN)
        _write_cache("sess-one", _approved_cache(gates_approved=["phase-a"]))
        cc._expire_completed_approval("sess-one", str(root), {"writ/a.py", "writ/b.py"})
        after = _read_cache("sess-one").get("gates_approved")
        assert after == [], after


class TestMutationProof:
    def test_neutralising_the_predicate_leaves_the_approval(self, cache_dir, tmp_path,
                                                            monkeypatch):
        """Without this, an expiry that cleared unconditionally would satisfy every
        positive test above for the wrong reason."""
        cc = _cc()
        from writ.session.cache import _read_cache, _write_cache
        root = tmp_path / "proj"
        (root / ".claude" / "plans" / "sess-mut").mkdir(parents=True)
        (root / ".claude" / "plans" / "sess-mut" / "plan.md").write_text(PLAN)
        _write_cache("sess-mut", _approved_cache())

        monkeypatch.setattr(cc, "_plan_is_complete", lambda *a, **k: None)
        cc._expire_completed_approval("sess-mut", str(root), {"writ/a.py", "writ/b.py"})
        assert _read_cache("sess-mut").get("gates_approved") == [
            "phase-a", "test-skeletons"], "the clear must be driven by the predicate"
