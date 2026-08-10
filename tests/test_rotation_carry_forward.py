"""PIECE 2: carry_forward_mode(session_id, cwd, prev_session_id, source) -- rotation
mode-only carry-forward, with a same-project guard and gates NEVER inherited.

Contract (plan.md):
  1. Read the new session's cache; if mode already set, no-op (nothing to do).
  2. source == "startup" -> brand-new session -> no-op (no carry).
  3. prev_session_id empty -> no-op (nothing to carry from).
  4. Candidate (prev_session_id) cache has no mode -> no-op (nothing to carry).
  5. SAME-PROJECT GUARD: only carry when candidate's project_root == cwd; otherwise
     warn and do NOT carry.
  6. Call mode_engine._mode_init(session_id, prev_mode) and emit a loud one-line
     notice to stderr: "[Writ] session rotated; carried mode <X> forward; gates
     reset - re-approve each gate."
  gates_approved is NEVER inherited (always lands []) and current_phase always
  lands at the mode's initial (planning, for work) phase -- this falls out of
  _mode_init delegating to _mode_set, which is exercised directly here too.

`writ.session.rotation` does not exist yet -- this file is RED until PIECE 2 lands.
Per TEST-TDD-001: skeletons approved before implementation.

Hermetic: WRIT_CACHE_DIR -> tmp_path (matches test_pol6b2_cache_dir_env.py); no env
mutation needed (cwd/prev_session_id/source are passed as explicit args); capsys
captures the loud notice.
"""

from __future__ import annotations

import os

import pytest

from writ.session import cache, mode_engine

# autouse: pins cwd to a sandbox so `mode set` cannot delete THIS repo's gate artifacts.
from tests.fixtures.session_state import sandbox_cwd  # noqa: F401


@pytest.fixture(autouse=True)
def _isolated_cache_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
    yield


def _seed_cache(session_id: str, *, mode: str | None, project_root: str | None) -> None:
    data = cache._read_cache(session_id)
    data["mode"] = mode
    if project_root is not None:
        data["project_root"] = project_root
    cache._write_cache(session_id, data)


class TestModuleExists:
    def test_rotation_module_importable(self):
        from writ.session import rotation

        assert callable(rotation.carry_forward_mode)


class TestSameProjectCarry:
    def test_carries_mode_when_project_matches(self, tmp_path):
        from writ.session import rotation

        project = str(tmp_path / "myproject")
        _seed_cache("prev-sid", mode="work", project_root=project)

        rotation.carry_forward_mode(
            "new-sid", cwd=project, prev_session_id="prev-sid", source="resume"
        )

        new_cache = cache._read_cache("new-sid")
        assert new_cache["mode"] == "work"

    def test_carried_session_lands_at_initial_phase(self, tmp_path):
        from writ.session import rotation

        project = str(tmp_path / "myproject")
        _seed_cache("prev-sid-2", mode="work", project_root=project)

        rotation.carry_forward_mode(
            "new-sid-2", cwd=project, prev_session_id="prev-sid-2", source="resume"
        )

        new_cache = cache._read_cache("new-sid-2")
        assert new_cache["current_phase"] == mode_engine._initial_phase_for_mode("work")
        assert new_cache["current_phase"] == "planning"

    def test_non_work_mode_carries_with_no_initial_phase(self, tmp_path):
        from writ.session import rotation

        project = str(tmp_path / "myproject")
        _seed_cache("prev-sid-3", mode="review", project_root=project)

        rotation.carry_forward_mode(
            "new-sid-3", cwd=project, prev_session_id="prev-sid-3", source="resume"
        )

        new_cache = cache._read_cache("new-sid-3")
        assert new_cache["mode"] == "review"


class TestCrossProjectRefusal:
    def test_does_not_carry_when_project_root_differs(self, tmp_path, capsys):
        from writ.session import rotation

        prev_project = str(tmp_path / "project-a")
        other_cwd = str(tmp_path / "project-b")
        _seed_cache("prev-sid-4", mode="work", project_root=prev_project)

        rotation.carry_forward_mode(
            "new-sid-4", cwd=other_cwd, prev_session_id="prev-sid-4", source="resume"
        )

        new_cache = cache._read_cache("new-sid-4")
        assert new_cache["mode"] is None

    def test_cross_project_refusal_emits_a_warning(self, tmp_path, capsys):
        from writ.session import rotation

        prev_project = str(tmp_path / "project-a")
        other_cwd = str(tmp_path / "project-b")
        _seed_cache("prev-sid-5", mode="work", project_root=prev_project)

        rotation.carry_forward_mode(
            "new-sid-5", cwd=other_cwd, prev_session_id="prev-sid-5", source="resume"
        )

        captured = capsys.readouterr()
        assert captured.err.strip() != ""

    def test_missing_project_root_on_candidate_is_treated_as_unknown_and_not_carried(self, tmp_path):
        from writ.session import rotation

        cwd = str(tmp_path / "project-c")
        _seed_cache("prev-sid-6", mode="work", project_root=None)

        rotation.carry_forward_mode(
            "new-sid-6", cwd=cwd, prev_session_id="prev-sid-6", source="resume"
        )

        new_cache = cache._read_cache("new-sid-6")
        assert new_cache["mode"] is None


class TestBrandNewSessionNoCarry:
    def test_startup_source_never_carries_even_with_matching_project(self, tmp_path):
        from writ.session import rotation

        project = str(tmp_path / "myproject")
        _seed_cache("prev-sid-7", mode="work", project_root=project)

        rotation.carry_forward_mode(
            "new-sid-7", cwd=project, prev_session_id="prev-sid-7", source="startup"
        )

        new_cache = cache._read_cache("new-sid-7")
        assert new_cache["mode"] is None

    def test_empty_prev_session_id_never_carries(self, tmp_path):
        from writ.session import rotation

        project = str(tmp_path / "myproject")

        rotation.carry_forward_mode(
            "new-sid-8", cwd=project, prev_session_id="", source="resume"
        )

        new_cache = cache._read_cache("new-sid-8")
        assert new_cache["mode"] is None

    def test_candidate_cache_with_no_mode_never_carries(self, tmp_path):
        from writ.session import rotation

        project = str(tmp_path / "myproject")
        _seed_cache("prev-sid-9", mode=None, project_root=project)

        rotation.carry_forward_mode(
            "new-sid-9", cwd=project, prev_session_id="prev-sid-9", source="resume"
        )

        new_cache = cache._read_cache("new-sid-9")
        assert new_cache["mode"] is None

    def test_new_session_already_has_a_mode_is_a_noop(self, tmp_path):
        from writ.session import rotation

        project = str(tmp_path / "myproject")
        _seed_cache("prev-sid-10", mode="work", project_root=project)
        _seed_cache("new-sid-10", mode="debug", project_root=project)

        rotation.carry_forward_mode(
            "new-sid-10", cwd=project, prev_session_id="prev-sid-10", source="resume"
        )

        new_cache = cache._read_cache("new-sid-10")
        assert new_cache["mode"] == "debug", "an already-set mode must never be overwritten"


class TestGatesNeverInherited:
    def test_carried_session_has_empty_gates_approved_even_when_prev_had_approvals(self, tmp_path):
        from writ.session import rotation

        project = str(tmp_path / "myproject")
        prev = cache._read_cache("prev-sid-11")
        prev["mode"] = "work"
        prev["project_root"] = project
        prev["gates_approved"] = ["phase-a", "test-skeletons"]
        prev["current_phase"] = "implementation"
        cache._write_cache("prev-sid-11", prev)

        rotation.carry_forward_mode(
            "new-sid-11", cwd=project, prev_session_id="prev-sid-11", source="resume"
        )

        new_cache = cache._read_cache("new-sid-11")
        assert new_cache["gates_approved"] == []
        assert new_cache["current_phase"] == "planning"

    def test_mid_cycle_phase_is_never_carried(self, tmp_path):
        from writ.session import rotation

        project = str(tmp_path / "myproject")
        prev = cache._read_cache("prev-sid-12")
        prev["mode"] = "work"
        prev["project_root"] = project
        prev["current_phase"] = "testing"
        prev["gates_approved"] = ["phase-a"]
        cache._write_cache("prev-sid-12", prev)

        rotation.carry_forward_mode(
            "new-sid-12", cwd=project, prev_session_id="prev-sid-12", source="resume"
        )

        new_cache = cache._read_cache("new-sid-12")
        assert new_cache["current_phase"] != "testing"
        assert new_cache["current_phase"] == "planning"


class TestLoudNotice:
    def test_successful_carry_emits_notice_naming_the_mode(self, tmp_path, capsys):
        from writ.session import rotation

        project = str(tmp_path / "myproject")
        _seed_cache("prev-sid-13", mode="work", project_root=project)

        rotation.carry_forward_mode(
            "new-sid-13", cwd=project, prev_session_id="prev-sid-13", source="resume"
        )

        captured = capsys.readouterr()
        assert "work" in captured.err
        assert "carried" in captured.err.lower()

    def test_successful_carry_notice_mentions_gates_reset(self, tmp_path, capsys):
        from writ.session import rotation

        project = str(tmp_path / "myproject")
        _seed_cache("prev-sid-14", mode="work", project_root=project)

        rotation.carry_forward_mode(
            "new-sid-14", cwd=project, prev_session_id="prev-sid-14", source="resume"
        )

        captured = capsys.readouterr()
        assert "gate" in captured.err.lower()
        assert "re-approve" in captured.err.lower() or "reset" in captured.err.lower()

    def test_notice_is_a_single_line(self, tmp_path, capsys):
        from writ.session import rotation

        project = str(tmp_path / "myproject")
        _seed_cache("prev-sid-15", mode="work", project_root=project)

        rotation.carry_forward_mode(
            "new-sid-15", cwd=project, prev_session_id="prev-sid-15", source="resume"
        )

        captured = capsys.readouterr()
        lines = [ln for ln in captured.err.strip().splitlines() if ln.strip()]
        assert len(lines) == 1

    def test_no_op_paths_emit_no_carry_notice(self, tmp_path, capsys):
        from writ.session import rotation

        rotation.carry_forward_mode(
            "new-sid-16", cwd=str(tmp_path), prev_session_id="", source="resume"
        )

        captured = capsys.readouterr()
        assert "carried" not in captured.err.lower()
