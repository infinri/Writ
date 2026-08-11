"""Clearing gate state must also clear the gate files on disk -- session-scoped (Part 2).

Two components disagreed about where gate state lives. Enforcement reads the cache
(`writ/session/gates.py:404`), but the work-mode reminder in `writ-rag-inject.sh` reads
`<project_root>/.claude/gates/*.approved` straight off disk. `_apply_mode_set` only ever
cleared the cache, so the files outlived every re-arm: in the Writ repo itself they sat
approved for eighteen days, and for all that time the "plan gate pending" reminder could
not fire no matter what the session state said.

Writes were still blocked correctly, because enforcement reads the cache. What was lost
was the message telling the user which gate they were stuck behind.

PART 2 UPDATE (isolation cycle, plan_hash 033bb1595c2c): the artifact itself is moving
from the flat `<project_root>/.claude/gates/<gate>.approved` to the session-scoped
`<project_root>/.claude/gates/<session_id>/<gate>.approved`, so `_approve_gates_on_disk`
and `_approved_files` below now build and read the SESSION-scoped path -- exactly the
change plan.md's own Part 2 file list calls for this file. `_clear_gate_artifacts` must
therefore clear the CALLING session's own subdirectory (never a sibling's) and sweep any
lingering flat `*.approved` file left by the pre-Part-2 writer, since abandoned flat
files must not keep reading as "approved" for every session in the repo forever.

Per TEST-TDD-001: skeletons approved before implementation.
"""

from __future__ import annotations

import importlib.util
import json
import os

import pytest

# ruff: noqa: F811 -- shared fixtures are consumed as test-method parameters.
from tests.fixtures.session_state import (  # noqa: F401
    project_root,
    session_id,
)

HELPER_PATH = os.path.join(
    os.path.dirname(__file__), os.pardir, "bin", "lib", "writ-session.py"
)
spec = importlib.util.spec_from_file_location("writ_session", HELPER_PATH)
writ_session = importlib.util.module_from_spec(spec)
spec.loader.exec_module(writ_session)

GATE_NAMES = ("phase-a", "test-skeletons")


def _read_raw_cache(tmp_path, session_id: str) -> dict:
    path = os.path.join(str(tmp_path), f"writ-session-{session_id}.json")
    with open(path) as f:
        return json.load(f)


@pytest.fixture()
def work_project(project_root, monkeypatch):
    """Project root that is also the cwd, so `mode set` stamps it into the cache."""
    monkeypatch.chdir(project_root)
    return project_root


def _approve_gates_on_disk(root, session_id, names=GATE_NAMES):
    """Seed the SESSION-scoped shape Part 2 introduces:
    `<root>/.claude/gates/<session_id>/<name>.approved`."""
    gate_dir = root / ".claude" / "gates" / session_id
    gate_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (gate_dir / f"{name}.approved").write_text("token-from-an-earlier-cycle\n")
    return gate_dir


def _approve_gate_flat(root, names=GATE_NAMES):
    """Seed the LEGACY flat shape the pre-Part-2 writer leaves:
    `<root>/.claude/gates/<name>.approved`, no session component at all."""
    gate_dir = root / ".claude" / "gates"
    gate_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (gate_dir / f"{name}.approved").write_text("legacy-flat-approval\n")
    return gate_dir


def _approved_files(root, session_id):
    """The SESSION-scoped `*.approved` files for `session_id` only."""
    gate_dir = root / ".claude" / "gates" / session_id
    if not gate_dir.exists():
        return []
    return sorted(p.name for p in gate_dir.glob("*.approved"))


def _flat_approved_files(root):
    gate_dir = root / ".claude" / "gates"
    if not gate_dir.exists():
        return []
    return sorted(p.name for p in gate_dir.glob("*.approved"))


class TestModeSetClearsGateFiles:

    def test_mode_set_deletes_approved_files(self, session_id, work_project):
        """`mode set` empties gates_approved, so the disk must agree with it.

        RED reason: `_clear_gate_artifacts` (mode_engine.py:197-237) globs only the flat
        `<gate_dir>/*.approved` today. It never looks inside a session subdirectory, so
        the session-scoped files this test seeds survive the re-arm untouched.
        """
        _approve_gates_on_disk(work_project, session_id)
        writ_session.cmd_mode(session_id, "set", "work")

        assert _approved_files(work_project, session_id) == [], (
            "a re-arm left stale session-scoped .approved files, so the gate reminder "
            "stays silent for THIS session"
        )

    def test_mode_set_leaves_unrelated_files_alone(self, session_id, work_project):
        """Only the gate artifacts are removed, not the directory or its neighbours."""
        gate_dir = _approve_gates_on_disk(work_project, session_id)
        (gate_dir / "notes.md").write_text("keep me\n")
        writ_session.cmd_mode(session_id, "set", "work")

        assert gate_dir.exists()
        assert (gate_dir / "notes.md").exists()

    def test_mode_set_survives_missing_gates_dir(self, session_id, work_project):
        """A project that has never approved a gate must not raise on mode set."""
        gate_dir = work_project / ".claude" / "gates"
        for p in gate_dir.glob("*"):
            p.unlink()
        gate_dir.rmdir()

        writ_session.cmd_mode(session_id, "set", "work")
        assert writ_session._read_cache(session_id)["mode"] == "work"

    def test_mode_set_does_not_touch_a_sibling_sessions_directory(
        self, session_id, work_project
    ):
        """The isolation half of this cleanup, pinned at the unit level (the real-process
        proof of the same claim lives in
        test_session_scoped_gate_artifacts.TestTwoSessionsSameProjectDoNotShareApprovals).

        RED reason: today's glob has no session concept AT ALL, so it cannot
        distinguish "this session's directory" from "a sibling's" -- it only ever
        touches the flat top level. This test seeds ONLY the sibling's session-scoped
        directory (no flat file), so today's `mode set` finds nothing to delete and the
        sibling survives by accident, not by the session-scoped clear this capability
        requires. Paired with the test above (which proves THIS session's own directory
        DOES get cleared) so neither test passes by a cleanup that simply does nothing.
        """
        sibling = "sibling-" + session_id
        sibling_dir = _approve_gates_on_disk(work_project, sibling)

        writ_session.cmd_mode(session_id, "set", "work")

        assert _approved_files(work_project, sibling) == list(
            sorted(f"{n}.approved" for n in GATE_NAMES)
        ), (
            f"session {session_id}'s mode set touched sibling session {sibling}'s "
            f"gate directory: {sorted(p.name for p in sibling_dir.glob('*'))}"
        )


class TestLegacyFlatArtifactsAreSweptButNotSessionScopedOnes:
    """Capability 18: legacy flat `<gate_dir>/*.approved` files (the shape every
    approval wrote before Part 2) are swept at the next re-arm, so a reader this plan
    missed reports gate-pending rather than approved for a session nobody re-verified.
    A session-scoped artifact belonging to ANOTHER session must survive that same sweep.
    """

    def test_a_legacy_flat_artifact_is_removed_on_rearm(self, session_id, work_project):
        """RED reason: this is the ONE case that already passes today (the current
        `_clear_gate_artifacts` only ever looks at the flat path) -- kept here as the
        anti-vacuity companion to the test below, not as a claim about new behavior.
        Isolated as its own test so a future refactor that accidentally stops sweeping
        the flat path is caught by name."""
        _approve_gate_flat(work_project)
        writ_session.cmd_mode(session_id, "set", "work")

        assert _flat_approved_files(work_project) == [], (
            "a legacy flat .approved file survived a re-arm; some reader will still "
            "read it as an approval nobody re-verified"
        )

    def test_the_sweep_does_not_touch_a_session_scoped_artifact(
        self, session_id, work_project
    ):
        """RED reason: today's sweep already deletes anything matching the flat glob, so
        this test seeds a DIFFERENT session's session-scoped artifact (a path today's
        code cannot even construct) and re-arms THIS session. Nothing today looks inside
        the sibling's subdirectory, so this half accidentally holds already -- it is
        paired with the legacy-removal test above precisely so "the sweep changed
        nothing" cannot make both halves pass for the same wrong reason once a real
        session-aware sweep lands: a session-aware implementation that also swept
        session subdirectories indiscriminately would fail THIS test specifically.
        """
        other_sid = "other-" + session_id
        _approve_gates_on_disk(work_project, other_sid)
        _approve_gate_flat(work_project)  # the legacy file that must still go

        writ_session.cmd_mode(session_id, "set", "work")

        assert _approved_files(work_project, other_sid) == list(
            sorted(f"{n}.approved" for n in GATE_NAMES)
        ), "the legacy sweep reached into another session's own gate directory"
        assert _flat_approved_files(work_project) == [], (
            "the legacy flat file survived alongside the session-scoped sweep check"
        )


class TestModeInitClearsOwnArtifactsAndStaysANoOp:
    """Capability 17: `mode init` clears the ROUTED session's OWN artifacts when it
    routes a session whose mode was lost but whose directory still holds approvals
    (the documented `mode=None` wipe class), and remains a no-op when a mode is already
    set -- it must never reset a live gate cycle just because the classifier re-fires.
    """

    def test_mode_init_clears_its_own_stale_artifacts_when_mode_was_lost(
        self, session_id, work_project
    ):
        """RED reason: `_mode_init` (mode_engine.py:312-347) only calls
        `_clear_gate_artifacts(project_root)` on the ROUTING path, and that helper globs
        the flat path -- it has no session-scoped directory to clear yet, so a
        session-scoped artifact left behind by a lost mode survives `mode init`
        untouched.
        """
        _approve_gates_on_disk(work_project, session_id)
        # mode was lost (cache has no "mode" key) while the directory still holds
        # approvals -- the documented mode=None wipe class.
        writ_session.cmd_mode(session_id, "init", "work")

        assert _approved_files(work_project, session_id) == [], (
            "mode init routed a session whose mode was lost but left its own stale "
            "gate artifacts in place"
        )
        assert writ_session._read_cache(session_id)["mode"] == "work"

    def test_mode_init_is_a_no_op_when_a_mode_is_already_set(
        self, session_id, work_project
    ):
        """Anti-regression companion: a session that already has a mode must never be
        reset by the router, whether or not it holds approvals. This already passes
        today (`_mode_init`'s locked check returns before touching anything), and stays
        here so the no-op guarantee is pinned next to the clearing behavior above rather
        than assumed."""
        _approve_gates_on_disk(work_project, session_id)
        writ_session.cmd_mode(session_id, "set", "work")
        cache = writ_session._read_cache(session_id)
        cache["gates_approved"] = ["phase-a"]
        writ_session._write_cache(session_id, cache)
        _approve_gates_on_disk(work_project, session_id, names=("phase-a",))

        writ_session.cmd_mode(session_id, "init", "investigate")

        assert writ_session._read_cache(session_id)["mode"] == "work", (
            "mode init reset a session that already had a mode"
        )
        assert _approved_files(work_project, session_id) == ["phase-a.approved"], (
            "mode init's no-op path must not touch existing artifacts either"
        )


class TestCleanupCannotEscapeTheProject:
    """glob + unlink FOLLOW symlinks, so the containment the docstring promises had to be
    enforced on resolved paths. Without it, a `.claude/gates` symlink pointed anywhere on
    the filesystem turned an ordinary `mode set` into a deletion of someone else's files."""

    def test_symlinked_gates_dir_is_not_followed(
        self, session_id, work_project, tmp_path
    ):
        """A gates directory that resolves outside the project is left entirely alone."""
        outside = tmp_path / "someone-elses-project"
        (outside / ".claude" / "gates").mkdir(parents=True)
        victim = outside / ".claude" / "gates" / "phase-a.approved"
        victim.write_text("another project's approval\n")

        claude_dir = work_project / ".claude"
        real_gates = claude_dir / "gates"
        for p in real_gates.glob("*"):
            p.unlink()
        real_gates.rmdir()
        real_gates.symlink_to(outside / ".claude" / "gates", target_is_directory=True)

        writ_session.cmd_mode(session_id, "set", "work")

        assert victim.exists(), (
            "the cleanup followed a symlinked gates dir and deleted outside the project"
        )
        assert writ_session._read_cache(session_id)["mode"] == "work", (
            "refusing to delete must stay fail-soft: the mode change still succeeds"
        )

    def test_an_artifact_link_is_removed_and_its_target_outside_is_not(
        self, session_id, work_project, tmp_path
    ):
        """A GUARD, not a fixed live bug: unlink never followed a final-component link.

        It pins the rule that keeps both halves true at once. The entry must go, because
        every reader of these files stats them with a symlink-FOLLOWING call
        (`_derive_phase` uses os.path.isfile), so a surviving `phase-a.approved` link would
        keep reporting the session past the plan gate the cache just cleared. The target
        must survive, because os.unlink removes the directory entry and nothing else.
        """
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        victim = outside / "phase-a.approved"
        victim.write_text("a file that merely shares the suffix\n")

        gate_dir = _approve_gates_on_disk(
            work_project, session_id, names=("test-skeletons",)
        )
        escaping = gate_dir / "phase-a.approved"
        escaping.symlink_to(victim)

        writ_session.cmd_mode(session_id, "set", "work")

        assert victim.exists(), "the cleanup reached through a link and deleted outside"
        assert not os.path.lexists(escaping), (
            "the link kept claiming an approval the cache no longer holds"
        )
        assert not (gate_dir / "test-skeletons.approved").exists(), (
            "a real artifact in the same directory must still be cleared"
        )

    # Capability 19 (a symlinked SESSION subdirectory must not be followed outside the
    # project, the session-scoped twin of test_symlinked_gates_dir_is_not_followed above)
    # has DELIBERATELY NO TEST HERE. A first attempt seeded a symlinked
    # `<gate_dir>/<session_id>` pointing outside the project and asserted the outside
    # file survived `mode set` -- and it passed today, for the wrong reason: today's
    # `_clear_gate_artifacts` has no session-scoped subdirectory concept at all, so it
    # never walks into `<gate_dir>/<session_id>` in the first place, symlinked or not.
    # The victim file "survives" because nothing ever goes looking for it, which is a
    # test that passes before implementation and asserts nothing (the exact flaw this
    # cycle's brief calls out by name). A real test needs the session-aware clear to
    # exist first, so that a version of it WITHOUT the containment check has something
    # to be caught doing wrong; it belongs with that implementation, not in this
    # skeleton pass. Flagged here rather than shipped silently passing.


class TestSwitchAndGateFiles:

    def _start_work(self, session_id, root, plan_text="# Plan: original\n"):
        (root / "plan.md").write_text(plan_text)
        writ_session.cmd_mode(session_id, "set", "work")
        cache = writ_session._read_cache(session_id)
        cache["gates_approved"] = ["phase-a"]
        cache["current_phase"] = "testing"
        writ_session._write_cache(session_id, cache)
        _approve_gates_on_disk(root, session_id, names=("phase-a",))

    def test_restore_keeps_the_gate_files(self, session_id, work_project):
        """An unchanged plan restores the approvals, so their artifacts stay valid."""
        self._start_work(session_id, work_project)
        writ_session.cmd_mode(session_id, "switch", "investigate")
        writ_session.cmd_mode(session_id, "switch", "work")

        assert _approved_files(work_project, session_id) == ["phase-a.approved"], (
            "a restore must not delete approvals it just restored"
        )

    def test_pivot_rearm_deletes_the_gate_files(self, session_id, work_project, tmp_path):
        """A changed plan discards the approvals, so their artifacts must go too."""
        self._start_work(session_id, work_project)
        writ_session.cmd_mode(session_id, "switch", "investigate")
        (work_project / "plan.md").write_text("# Plan: pivoted\n")
        writ_session.cmd_mode(session_id, "switch", "work")

        assert _read_raw_cache(tmp_path, session_id)["gates_approved"] == []
        assert _approved_files(work_project, session_id) == [], (
            "a pivot re-armed the cache but left the approval files on disk"
        )

    def test_leaving_work_keeps_the_gate_files(self, session_id, work_project):
        """Pausing is not re-arming: the approvals are saved, not discarded."""
        self._start_work(session_id, work_project)
        writ_session.cmd_mode(session_id, "switch", "investigate")

        assert _approved_files(work_project, session_id) == ["phase-a.approved"]
