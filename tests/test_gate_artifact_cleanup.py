"""Clearing gate state must also clear the gate files on disk.

Two components disagreed about where gate state lives. Enforcement reads the cache
(`writ/session/gates.py:404`), but the work-mode reminder in `writ-rag-inject.sh` reads
`<project_root>/.claude/gates/*.approved` straight off disk. `_apply_mode_set` only ever
cleared the cache, so the files outlived every re-arm: in the Writ repo itself they sat
approved for eighteen days, and for all that time the "plan gate pending" reminder could
not fire no matter what the session state said.

Writes were still blocked correctly, because enforcement reads the cache. What was lost
was the message telling the user which gate they were stuck behind.

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


def _approve_gates_on_disk(root, names=GATE_NAMES):
    gate_dir = root / ".claude" / "gates"
    gate_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (gate_dir / f"{name}.approved").write_text("token-from-an-earlier-cycle\n")
    return gate_dir


def _approved_files(root):
    gate_dir = root / ".claude" / "gates"
    if not gate_dir.exists():
        return []
    return sorted(p.name for p in gate_dir.glob("*.approved"))


class TestModeSetClearsGateFiles:

    def test_mode_set_deletes_approved_files(self, session_id, work_project):
        """`mode set` empties gates_approved, so the disk must agree with it."""
        _approve_gates_on_disk(work_project)
        writ_session.cmd_mode(session_id, "set", "work")

        assert _approved_files(work_project) == [], (
            "a re-arm left stale .approved files, so the gate reminder stays silent"
        )

    def test_mode_set_leaves_unrelated_files_alone(self, session_id, work_project):
        """Only the gate artifacts are removed, not the directory or its neighbours."""
        gate_dir = _approve_gates_on_disk(work_project)
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

        gate_dir = _approve_gates_on_disk(work_project, names=("test-skeletons",))
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


class TestSwitchAndGateFiles:

    def _start_work(self, session_id, root, plan_text="# Plan: original\n"):
        (root / "plan.md").write_text(plan_text)
        writ_session.cmd_mode(session_id, "set", "work")
        cache = writ_session._read_cache(session_id)
        cache["gates_approved"] = ["phase-a"]
        cache["current_phase"] = "testing"
        writ_session._write_cache(session_id, cache)
        _approve_gates_on_disk(root, names=("phase-a",))

    def test_restore_keeps_the_gate_files(self, session_id, work_project):
        """An unchanged plan restores the approvals, so their artifacts stay valid."""
        self._start_work(session_id, work_project)
        writ_session.cmd_mode(session_id, "switch", "investigate")
        writ_session.cmd_mode(session_id, "switch", "work")

        assert _approved_files(work_project) == ["phase-a.approved"], (
            "a restore must not delete approvals it just restored"
        )

    def test_pivot_rearm_deletes_the_gate_files(self, session_id, work_project, tmp_path):
        """A changed plan discards the approvals, so their artifacts must go too."""
        self._start_work(session_id, work_project)
        writ_session.cmd_mode(session_id, "switch", "investigate")
        (work_project / "plan.md").write_text("# Plan: pivoted\n")
        writ_session.cmd_mode(session_id, "switch", "work")

        assert _read_raw_cache(tmp_path, session_id)["gates_approved"] == []
        assert _approved_files(work_project) == [], (
            "a pivot re-armed the cache but left the approval files on disk"
        )

    def test_leaving_work_keeps_the_gate_files(self, session_id, work_project):
        """Pausing is not re-arming: the approvals are saved, not discarded."""
        self._start_work(session_id, work_project)
        writ_session.cmd_mode(session_id, "switch", "investigate")

        assert _approved_files(work_project) == ["phase-a.approved"]
