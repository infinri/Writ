"""Two sessions on one project must not share one plan.md.

The plan artifact was resolved as `<project_root>/plan.md` with no session component
(locators._find_plan_md), and every approval decision keys on its fingerprint
(locators.plan_md_hash). So one session saving a plan changed another session's approval
state: the mode-switch restore path compared its paused fingerprint against a fresh read of
the shared file, took the pivot branch, cleared gates_approved, and the work gate then
refused every write in a session whose operator had changed nothing.

Gate artifacts were already scoped per session at `.claude/gates/<session_id>/`. These tests
pin the same treatment for the plan: a `.claude/plans/<session_id>/plan.md` tier that wins
when present, with the pre-existing root-plan and module-glob tiers unchanged beneath it.

TWO IMPORT STYLES ON PURPOSE. `plan_dir` and `plan_path` do not exist yet, so importing
them by name would be a collection-time ImportError that fails this whole module at once
and hides which behavior is missing. They are reached through the module object instead, so
each unimplemented behavior fails in its own test. Names that already exist are imported
directly, matching the rest of the suite.

Two capabilities in capabilities.md are deliberately not tested here, so nobody hunts for a
test that was never meant to exist: "the existing test suite passes with no test file
edited" is verified by running the suite, and the live two-session check is marked
(operational) because it needs two real Claude Code sessions.
"""
from __future__ import annotations

import os

import pytest

from writ.session import locators
from writ.session.cache import mutate_cache
from writ.session.locators import (
    PLAN_HASH_UNREADABLE,
    _find_plan_md,
    gate_dir,
    plan_md_hash,
)
from writ.session.mode_engine import _mode_switch

SID_A = "aaaaaaaa-1111-4222-8333-444444444444"
SID_B = "bbbbbbbb-5555-4666-8777-888888888888"


def _write_scoped_plan(root: str, session_id: str, body: str) -> str:
    """Create `<root>/.claude/plans/<session_id>/plan.md` and return its path."""
    directory = os.path.join(root, ".claude", "plans", session_id)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "plan.md")
    with open(path, "w") as f:
        f.write(body)
    return path


class TestPlanPathConstruction:
    def test_plan_path_is_the_session_scoped_plan(self, tmp_path):
        expected = os.path.join(str(tmp_path), ".claude", "plans", SID_A, "plan.md")
        assert locators.plan_path(str(tmp_path), SID_A) == expected

    def test_plan_dir_is_the_session_scoped_directory(self, tmp_path):
        expected = os.path.join(str(tmp_path), ".claude", "plans", SID_A)
        assert locators.plan_dir(str(tmp_path), SID_A) == expected

    def test_empty_project_root_has_no_plan_path(self):
        assert locators.plan_path("", SID_A) == ""
        assert locators.plan_dir("", SID_A) == ""

    @pytest.mark.parametrize("bad", [".", "..", "a/b", "../escape", "", "with space"])
    def test_invalid_session_component_has_no_plan_path(self, tmp_path, bad):
        """Same validation contract as gate_dir: "" means no path, never join to it."""
        assert locators.plan_path(str(tmp_path), bad) == ""
        assert locators.plan_dir(str(tmp_path), bad) == ""

    def test_trailing_separator_produces_identical_bytes(self, tmp_path):
        root = str(tmp_path)
        assert locators.plan_dir(root + "/", SID_A) == locators.plan_dir(root, SID_A)

    def test_trailing_separator_behaviour_matches_gate_dir(self, tmp_path):
        """The new tier mirrors the precedent it copies, so the two cannot drift."""
        root = str(tmp_path)
        plan_same = locators.plan_dir(root + "/", SID_A) == locators.plan_dir(root, SID_A)
        gate_same = gate_dir(root + "/", SID_A) == gate_dir(root, SID_A)
        assert plan_same == gate_same


class TestResolutionTiers:
    def test_scoped_plan_wins_when_it_exists(self, tmp_path):
        (tmp_path / "plan.md").write_text("root plan\n")
        scoped = _write_scoped_plan(str(tmp_path), SID_A, "session A plan\n")
        assert _find_plan_md(str(tmp_path), SID_A) == scoped

    def test_falls_back_to_root_plan_when_no_scoped_plan(self, tmp_path):
        root_plan = tmp_path / "plan.md"
        root_plan.write_text("root plan\n")
        assert _find_plan_md(str(tmp_path), SID_A) == str(root_plan)

    def test_falls_back_to_module_glob_when_neither_exists(self, tmp_path):
        module = tmp_path / "src" / "thing"
        module.mkdir(parents=True)
        module_plan = module / "plan.md"
        module_plan.write_text("module plan\n")
        assert _find_plan_md(str(tmp_path), SID_A) == str(module_plan)

    def test_returns_none_when_no_plan_exists_at_any_tier(self, tmp_path):
        assert _find_plan_md(str(tmp_path), SID_A) is None

    def test_omitted_session_id_resolves_the_root_plan_as_before(self, tmp_path):
        """The unscoped call is the pre-change contract and must not move."""
        root_plan = tmp_path / "plan.md"
        root_plan.write_text("root plan\n")
        _write_scoped_plan(str(tmp_path), SID_A, "session A plan\n")
        assert _find_plan_md(str(tmp_path)) == str(root_plan)

    def test_omitted_session_id_resolves_the_module_glob_as_before(self, tmp_path):
        module = tmp_path / "src" / "thing"
        module.mkdir(parents=True)
        module_plan = module / "plan.md"
        module_plan.write_text("module plan\n")
        assert _find_plan_md(str(tmp_path)) == str(module_plan)

    def test_invalid_session_id_falls_through_to_the_root_plan(self, tmp_path):
        """A rejected session component must not become a resolution failure."""
        root_plan = tmp_path / "plan.md"
        root_plan.write_text("root plan\n")
        assert _find_plan_md(str(tmp_path), "..") == str(root_plan)


class TestFingerprint:
    def test_fingerprints_the_scoped_plan(self, tmp_path):
        _write_scoped_plan(str(tmp_path), SID_A, "session A plan\n")
        assert plan_md_hash(str(tmp_path), SID_A) is not None

    def test_scoped_and_root_fingerprints_differ_when_the_files_differ(self, tmp_path):
        (tmp_path / "plan.md").write_text("root plan\n")
        _write_scoped_plan(str(tmp_path), SID_A, "a materially different plan\n")
        assert plan_md_hash(str(tmp_path), SID_A) != plan_md_hash(str(tmp_path))

    def test_none_when_no_plan_exists_at_any_tier(self, tmp_path):
        assert plan_md_hash(str(tmp_path), SID_A) is None

    def test_unreadable_scoped_plan_is_distinguished_from_absent(self, tmp_path):
        path = _write_scoped_plan(str(tmp_path), SID_A, "session A plan\n")
        os.chmod(path, 0o000)
        try:
            assert plan_md_hash(str(tmp_path), SID_A) == PLAN_HASH_UNREADABLE
        finally:
            os.chmod(path, 0o644)

    def test_checkbox_tick_state_is_still_normalised_away(self, tmp_path):
        """The scoped tier inherits the existing normalisation, not a second rule."""
        unticked = _write_scoped_plan(str(tmp_path), SID_A, "- [ ] a capability\n")
        before = plan_md_hash(str(tmp_path), SID_A)
        with open(unticked, "w") as f:
            f.write("- [x] a capability\n")
        assert plan_md_hash(str(tmp_path), SID_A) == before


class TestTwoSessionIsolation:
    def test_one_sessions_plan_write_leaves_the_others_fingerprint_unmoved(self, tmp_path):
        """The reproduced defect, stated as an assertion."""
        root = str(tmp_path)
        _write_scoped_plan(root, SID_B, "session B plan\n")
        before = plan_md_hash(root, SID_B)
        _write_scoped_plan(root, SID_A, "session A plan for entirely other work\n")
        assert plan_md_hash(root, SID_B) == before

    def test_two_sessions_resolve_to_different_plan_files(self, tmp_path):
        root = str(tmp_path)
        a = _write_scoped_plan(root, SID_A, "session A plan\n")
        b = _write_scoped_plan(root, SID_B, "session B plan\n")
        assert (_find_plan_md(root, SID_A), _find_plan_md(root, SID_B)) == (a, b)

    def test_root_plan_fingerprint_is_unchanged_by_the_new_parameter(self, tmp_path):
        """A live session mid-cycle keeps its fingerprint across the deploy."""
        (tmp_path / "plan.md").write_text("the approved plan\n")
        assert plan_md_hash(str(tmp_path), SID_A) == plan_md_hash(str(tmp_path))


class TestModeSwitchRestore:
    def test_restore_survives_the_other_sessions_plan_write(self, tmp_path):
        """Session B detours out of work and back while session A saves its plan.

        Before the change this cleared B's gates_approved and reset B's phase to planning,
        after which the work gate refused every write in B.

        Session A writes the ROOT plan.md, not a scoped one, because that is where the
        planner writes today and therefore what the pre-change resolver fingerprints. With A
        writing only a scoped plan this test passes vacuously: both of B's fingerprints are
        None, None equals None, and the restore happens for the wrong reason.
        """
        root = str(tmp_path)
        root_plan = tmp_path / "plan.md"
        root_plan.write_text("session A plan, at the shared root\n")
        _write_scoped_plan(root, SID_B, "session B plan\n")

        with mutate_cache(SID_B) as cache:
            cache["project_root"] = root
            cache["mode"] = "work"
            cache["current_phase"] = "implementation"
            cache["gates_approved"] = ["phase-a", "test-skeletons"]

        _mode_switch(SID_B, "investigate")
        root_plan.write_text("session A plan, rewritten mid-detour\n")
        _mode_switch(SID_B, "work")

        with mutate_cache(SID_B) as cache:
            restored = (cache.get("current_phase"), sorted(cache.get("gates_approved", [])))
        assert restored == ("implementation", ["phase-a", "test-skeletons"])

    def test_own_plan_edit_still_re_arms(self, tmp_path):
        """The pivot behaviour is preserved for the session that actually changed its plan."""
        root = str(tmp_path)
        _write_scoped_plan(root, SID_B, "session B plan\n")

        with mutate_cache(SID_B) as cache:
            cache["project_root"] = root
            cache["mode"] = "work"
            cache["current_phase"] = "implementation"
            cache["gates_approved"] = ["phase-a", "test-skeletons"]

        _mode_switch(SID_B, "investigate")
        _write_scoped_plan(root, SID_B, "session B pivoted to different work\n")
        _mode_switch(SID_B, "work")

        with mutate_cache(SID_B) as cache:
            pivoted = (cache.get("current_phase"), cache.get("gates_approved"))
        assert pivoted == ("planning", [])
