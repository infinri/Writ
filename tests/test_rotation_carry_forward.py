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


def _seed_cache(
    session_id: str,
    *,
    mode: str | None,
    project_root: str | None,
    mode_source: str | None = None,
) -> None:
    data = cache._read_cache(session_id)
    data["mode"] = mode
    if project_root is not None:
        data["project_root"] = project_root
    if mode_source is not None:
        data["mode_source"] = mode_source
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


# ===========================================================================
# Part 6: mode_source carries alongside the mode
#
# carry_forward_mode moves the mode across a session-id rotation; it must
# move the SAME mode_source the previous session actually had (not re-derive
# it as "auto" just because the carry itself goes through mode_engine), and
# the same-project refusal that withholds the mode must withhold the source
# too -- a refused carry must never leak the source while denying the mode.
# ===========================================================================

class TestModeSourceCarriesToo:
    def test_carries_explicit_source_alongside_the_mode(self, tmp_path):
        from writ.session import rotation

        project = str(tmp_path / "myproject")
        _seed_cache(
            "prev-sid-17", mode="work", project_root=project, mode_source="explicit"
        )

        rotation.carry_forward_mode(
            "new-sid-17", cwd=project, prev_session_id="prev-sid-17", source="resume"
        )

        new_cache = cache._read_cache("new-sid-17")
        assert new_cache["mode"] == "work"
        assert new_cache["mode_source"] == "explicit"

    def test_carries_auto_source_alongside_the_mode(self, tmp_path):
        from writ.session import rotation

        project = str(tmp_path / "myproject")
        _seed_cache(
            "prev-sid-18", mode="investigate", project_root=project, mode_source="auto"
        )

        rotation.carry_forward_mode(
            "new-sid-18", cwd=project, prev_session_id="prev-sid-18", source="resume"
        )

        new_cache = cache._read_cache("new-sid-18")
        assert new_cache["mode"] == "investigate"
        assert new_cache["mode_source"] == "auto"

    def test_cross_project_refusal_withholds_the_source_too(self, tmp_path):
        """The same-project guard fails closed on BOTH fields together: a
        refused carry must not leak the source while withholding the mode.
        """
        from writ.session import rotation

        prev_project = str(tmp_path / "project-a")
        other_cwd = str(tmp_path / "project-b")
        _seed_cache(
            "prev-sid-19", mode="work", project_root=prev_project, mode_source="explicit"
        )

        rotation.carry_forward_mode(
            "new-sid-19", cwd=other_cwd, prev_session_id="prev-sid-19", source="resume"
        )

        new_cache = cache._read_cache("new-sid-19")
        assert new_cache["mode"] is None
        assert new_cache.get("mode_source") is None, (
            "a refused carry must not carry the source either, even partially"
        )

    def test_new_session_already_having_a_mode_source_is_not_overwritten(
        self, tmp_path
    ):
        from writ.session import rotation

        project = str(tmp_path / "myproject")
        _seed_cache(
            "prev-sid-20", mode="work", project_root=project, mode_source="auto"
        )
        _seed_cache(
            "new-sid-20", mode="debug", project_root=project, mode_source="explicit"
        )

        rotation.carry_forward_mode(
            "new-sid-20", cwd=project, prev_session_id="prev-sid-20", source="resume"
        )

        new_cache = cache._read_cache("new-sid-20")
        assert new_cache["mode"] == "debug", "an already-set mode must never be overwritten"
        assert new_cache["mode_source"] == "explicit"


class TestNonePreFieldProvenanceStaysNone:
    """PIECE 2 mode_source: a session cache written BEFORE the `mode_source` field
    existed carries a real mode but a genuinely `None` provenance -- there is
    nothing to derive a value from, so carry_forward_mode passes that `None`
    straight through, and cache._read_cache's own `setdefault` backfill leaves it
    alone rather than inventing one.

    The direction is load-bearing, not incidental: `None` is documented (see
    cache._default_cache's `mode_source` comment) as "unknown, treat as explicit",
    which means a session with `mode_source is None` is NOT eligible for the
    auto-router's mid-session re-route (that eligibility check requires
    `mode_source == "auto"`). If a future change started defaulting a missing/None
    provenance to "auto" instead, every pre-existing session already on disk would
    silently become re-routable, and a user's deliberately chosen mode could be
    overwritten by a classifier's guess on the very next prompt -- with nothing
    here to object, since "None carries through as None" is today correct only by
    inspection.
    """

    def test_carried_mode_source_stays_none_when_candidate_predates_the_field(
        self, tmp_path
    ):
        from writ.session import rotation

        project = str(tmp_path / "myproject")
        # No mode_source kwarg: this is exactly what a pre-field session on disk
        # looks like -- a real mode, no provenance ever recorded for it.
        _seed_cache("prev-sid-21", mode="work", project_root=project)
        candidate = cache._read_cache("prev-sid-21")
        assert candidate["mode_source"] is None, (
            "setup sanity: the candidate must have a genuinely unrecorded "
            "provenance, or this test is not exercising the pre-field case"
        )

        rotation.carry_forward_mode(
            "new-sid-21", cwd=project, prev_session_id="prev-sid-21", source="resume"
        )

        new_cache = cache._read_cache("new-sid-21")
        assert new_cache["mode"] == "work"
        assert new_cache["mode_source"] is None, (
            "a pre-field candidate's unrecorded provenance must carry through as "
            "None, never be defaulted to \"auto\": None means the mode is treated "
            "like an explicit human choice and is left alone by the auto-router, so "
            "silently defaulting it here would make every pre-existing session on "
            "disk re-routable and let a classifier's guess overwrite a user's "
            "deliberately chosen mode."
        )
