"""Mid-session mode switching: leave work, investigate, come back.

The auto-router classified every prompt but only ever acted on the first one:
`writ-rag-inject.sh` called `mode init`, which no-ops once a mode exists, so five weeks
of logs contain zero `change_type: switch` rows and the paused-state restore path in
`_mode_switch` had never executed.

The contract these tests pin: an auto-route MAY change the mode, but it must never
destroy work state. Going out is always `mode switch` (which saves the approved gates),
and coming back is decided by comparing a fingerprint of plan.md rather than by asking
the model -- same plan restores the gates, changed plan re-arms for fresh approval.

Per TEST-TDD-001: skeletons approved before implementation.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys

import pytest

# ruff: noqa: F811 -- shared fixtures are consumed as test-method parameters.
from tests.fixtures.session_state import (  # noqa: F401
    project_root,
    session_id,
)
from writ.session.locators import PLAN_HASH_UNREADABLE

HELPER_PATH = os.path.join(
    os.path.dirname(__file__), os.pardir, "bin", "lib", "writ-session.py"
)
spec = importlib.util.spec_from_file_location("writ_session", HELPER_PATH)
writ_session = importlib.util.module_from_spec(spec)
spec.loader.exec_module(writ_session)

HOOK = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, "hooks", "scripts", "writ-rag-inject.sh")
)
HELPER = os.path.abspath(HELPER_PATH)

# Investigate-shaped prompt, reused from the auto-route suite's canonical case.
INVESTIGATE_PROMPT = "audit the codebase for security issues"


def _read_raw_cache(tmp_path, session_id: str) -> dict:
    path = os.path.join(str(tmp_path), f"writ-session-{session_id}.json")
    with open(path) as f:
        return json.load(f)


@pytest.fixture()
def work_project(project_root, monkeypatch):
    """A project root that is also the process cwd, so `mode set` stamps it as
    cache['project_root'] and plan.md lookups resolve inside it."""
    monkeypatch.chdir(project_root)
    return project_root


def _start_work(session_id, work_project, plan_text="# Plan: original\n", gates=("phase-a",)):
    """Put the session into work mode mid-cycle: a plan on disk, a gate approved."""
    if plan_text is not None:
        (work_project / "plan.md").write_text(plan_text)
    writ_session.cmd_mode(session_id, "set", "work")
    cache = writ_session._read_cache(session_id)
    cache["gates_approved"] = list(gates)
    cache["current_phase"] = "testing"
    writ_session._write_cache(session_id, cache)


# ===========================================================================
# Pausing work captures the plan fingerprint
# ===========================================================================

class TestPauseCapturesPlanFingerprint:

    def test_leaving_work_stores_plan_hash(self, session_id, work_project, tmp_path):
        """Switching out of work records a fingerprint of plan.md alongside the gates."""
        _start_work(session_id, work_project)
        writ_session.cmd_mode(session_id, "switch", "investigate")

        paused = _read_raw_cache(tmp_path, session_id)["paused_work_state"]
        assert paused is not None
        assert paused["gates_approved"] == ["phase-a"]
        assert paused["phase"] == "testing"
        assert paused.get("plan_hash"), "pausing work must fingerprint plan.md"

    def test_leaving_work_without_plan_stores_null_hash(
        self, session_id, work_project, tmp_path
    ):
        """No plan.md means no fingerprint, recorded explicitly rather than omitted."""
        _start_work(session_id, work_project, plan_text=None)
        writ_session.cmd_mode(session_id, "switch", "investigate")

        paused = _read_raw_cache(tmp_path, session_id)["paused_work_state"]
        assert "plan_hash" in paused, "the key must exist even when there is no plan"
        assert paused["plan_hash"] is None

    def test_leaving_work_with_an_unreadable_plan_is_not_recorded_as_absent(
        self, session_id, work_project, tmp_path
    ):
        """A plan that exists but cannot be read is a THIRD state, not the absent one.

        Both used to fingerprint as None, and None == None on the way back, so a plan that
        was unreadable at both ends restored the approved gates without a single byte of it
        ever being compared.
        """
        if os.geteuid() == 0:
            pytest.skip("root can read mode-000 files; the failure cannot be staged")
        _start_work(session_id, work_project)
        plan = work_project / "plan.md"
        plan.chmod(0o000)
        try:
            writ_session.cmd_mode(session_id, "switch", "investigate")
        finally:
            plan.chmod(0o600)

        paused = _read_raw_cache(tmp_path, session_id)["paused_work_state"]
        assert paused["plan_hash"] == PLAN_HASH_UNREADABLE
        assert paused["plan_hash"] is not None, "unreadable must not read as absent"


# ===========================================================================
# Returning to work: restore vs re-arm, decided by the fingerprint
# ===========================================================================

class TestReturnRestoresWhenPlanUnchanged:

    def test_unchanged_plan_restores_phase_and_gates(
        self, session_id, work_project, tmp_path
    ):
        """Same plan means the same work, so the approved gates survive the detour."""
        _start_work(session_id, work_project)
        writ_session.cmd_mode(session_id, "switch", "investigate")
        writ_session.cmd_mode(session_id, "switch", "work")

        data = _read_raw_cache(tmp_path, session_id)
        assert data["mode"] == "work"
        assert data["current_phase"] == "testing"
        assert data["gates_approved"] == ["phase-a"]
        assert data["paused_work_state"] is None, "paused state is consumed on restore"

    def test_plan_absent_at_both_ends_restores(self, session_id, work_project, tmp_path):
        """No plan at either end means nothing pivoted, so this is a restore."""
        _start_work(session_id, work_project, plan_text=None)
        writ_session.cmd_mode(session_id, "switch", "investigate")
        writ_session.cmd_mode(session_id, "switch", "work")

        data = _read_raw_cache(tmp_path, session_id)
        assert data["current_phase"] == "testing"
        assert data["gates_approved"] == ["phase-a"]


class TestReturnReArmsWhenPlanChanged:

    def test_changed_plan_rearms_to_planning(self, session_id, work_project, tmp_path):
        """A rewritten plan is a pivot: the old approvals no longer cover this work."""
        _start_work(session_id, work_project)
        writ_session.cmd_mode(session_id, "switch", "investigate")
        (work_project / "plan.md").write_text("# Plan: pivoted after the investigation\n")
        writ_session.cmd_mode(session_id, "switch", "work")

        data = _read_raw_cache(tmp_path, session_id)
        assert data["mode"] == "work"
        assert data["current_phase"] == "planning"
        assert data["gates_approved"] == [], "a pivoted plan must earn fresh approval"
        assert data["paused_work_state"] is None

    def test_plan_appearing_during_excursion_rearms(
        self, session_id, work_project, tmp_path
    ):
        """Absent at pause, present at return: unequal, and re-arming is the safe side."""
        _start_work(session_id, work_project, plan_text=None)
        writ_session.cmd_mode(session_id, "switch", "investigate")
        (work_project / "plan.md").write_text("# Plan: written during the excursion\n")
        writ_session.cmd_mode(session_id, "switch", "work")

        data = _read_raw_cache(tmp_path, session_id)
        assert data["current_phase"] == "planning"
        assert data["gates_approved"] == []

    def test_unreadable_plan_at_both_ends_rearms(
        self, session_id, work_project, tmp_path
    ):
        """Two failed reads are not a match: nothing was compared, so nothing is proven.

        Absent at both ends restores, because no plan means nothing pivoted. Unreadable at
        both ends must NOT, because the file is there and its bytes may have changed while
        the session was away. One needless re-approval is the cheap side of that error.
        """
        if os.geteuid() == 0:
            pytest.skip("root can read mode-000 files; the failure cannot be staged")
        _start_work(session_id, work_project)
        plan = work_project / "plan.md"
        plan.chmod(0o000)
        try:
            writ_session.cmd_mode(session_id, "switch", "investigate")
            writ_session.cmd_mode(session_id, "switch", "work")
        finally:
            plan.chmod(0o600)

        data = _read_raw_cache(tmp_path, session_id)
        assert data["mode"] == "work"
        assert data["current_phase"] == "planning"
        assert data["gates_approved"] == [], "an unchecked plan must earn fresh approval"
        assert data["paused_work_state"] is None

    def test_whitespace_change_counts_as_a_pivot(self, session_id, work_project, tmp_path):
        """The comparison is a byte fingerprint, not a semantic diff."""
        _start_work(session_id, work_project)
        writ_session.cmd_mode(session_id, "switch", "investigate")
        (work_project / "plan.md").write_text("# Plan: original\n\n")
        writ_session.cmd_mode(session_id, "switch", "work")

        assert _read_raw_cache(tmp_path, session_id)["gates_approved"] == []


# ===========================================================================
# Ticking a capability box is progress, not a pivot
# ===========================================================================

# A plan shaped like templates/plan-template.md: prose sections plus the capability
# checklist. Reused whole so each test below changes exactly one thing about it.
PLAN_WITH_CAPABILITIES = """\
# Plan: the export endpoint

## Files

- `src/export.py` (create) -- the streaming export endpoint

## Analysis

The endpoint streams rather than buffering, because the largest export is 400 MB and
buffering it costs more memory than the worker has.

## Capabilities

- [ ] The endpoint streams the export instead of buffering it.
- [ ] A missing report id returns 404 rather than an empty file.
"""

TICKED_PLAN = PLAN_WITH_CAPABILITIES.replace("- [ ]", "- [x]")


class TestCapabilityTickStateIsNotAPivot:
    """templates/plan-template.md says the boxes "are ticked off after the implementation
    proves them". Hashing the raw file made following that instruction look like a pivot,
    so completing a cycle the documented way cost a re-approval of an unchanged plan.
    """

    def _detour(self, session_id, work_project, before, after):
        """Pause work against `before`, rewrite plan.md as `after`, come back."""
        _start_work(session_id, work_project, plan_text=before)
        writ_session.cmd_mode(session_id, "switch", "investigate")
        (work_project / "plan.md").write_text(after)
        writ_session.cmd_mode(session_id, "switch", "work")

    @pytest.mark.parametrize("mark", ["x", "X"], ids=["lowercase", "uppercase"])
    def test_ticking_a_capability_box_restores_instead_of_rearming(
        self, session_id, work_project, tmp_path, mark
    ):
        """The behavior the user actually wants: proving a capability keeps the approval."""
        self._detour(
            session_id, work_project,
            before=PLAN_WITH_CAPABILITIES,
            after=PLAN_WITH_CAPABILITIES.replace("- [ ]", f"- [{mark}]", 1),
        )

        data = _read_raw_cache(tmp_path, session_id)
        assert data["current_phase"] == "testing"
        assert data["gates_approved"] == ["phase-a"], (
            "ticking a proven capability must not cost the plan its approval"
        )
        assert data["paused_work_state"] is None

    def test_unticking_a_capability_box_is_likewise_hash_neutral(
        self, session_id, work_project, tmp_path
    ):
        """Normalization is symmetric: the digest ignores the state, not one value of it."""
        self._detour(
            session_id, work_project,
            before=TICKED_PLAN,
            after=PLAN_WITH_CAPABILITIES,
        )

        data = _read_raw_cache(tmp_path, session_id)
        assert data["current_phase"] == "testing"
        assert data["gates_approved"] == ["phase-a"]

    def test_reworded_analysis_still_rearms_with_the_boxes_untouched(
        self, session_id, work_project, tmp_path
    ):
        """The counterweight: normalizing tick state must not mean the plan stopped mattering.

        Not one checkbox differs between the two versions; only a sentence of ## Analysis
        does, and that is enough to require a fresh approval.
        """
        reworded = PLAN_WITH_CAPABILITIES.replace(
            "the largest export is 400 MB", "the largest export is 4 GB"
        )
        assert "- [x]" not in reworded and "- [X]" not in reworded, (
            "precondition: this case must differ in prose only"
        )
        self._detour(
            session_id, work_project, before=PLAN_WITH_CAPABILITIES, after=reworded
        )

        data = _read_raw_cache(tmp_path, session_id)
        assert data["current_phase"] == "planning"
        assert data["gates_approved"] == [], "a reworded plan must earn fresh approval"

    def test_an_added_capability_line_rearms(self, session_id, work_project, tmp_path):
        """Which capabilities the plan claims is substance, not progress against them."""
        self._detour(
            session_id, work_project,
            before=PLAN_WITH_CAPABILITIES,
            after=PLAN_WITH_CAPABILITIES + "- [ ] An export larger than 4 GB is refused.\n",
        )

        data = _read_raw_cache(tmp_path, session_id)
        assert data["current_phase"] == "planning"
        assert data["gates_approved"] == [], "a new capability is work nobody approved"

    def test_a_removed_capability_line_rearms(self, session_id, work_project, tmp_path):
        """Dropping a promised capability is a pivot too, ticked or not."""
        dropped = TICKED_PLAN.replace(
            "- [x] A missing report id returns 404 rather than an empty file.\n", ""
        )
        self._detour(session_id, work_project, before=TICKED_PLAN, after=dropped)

        data = _read_raw_cache(tmp_path, session_id)
        assert data["current_phase"] == "planning"
        assert data["gates_approved"] == []


# ===========================================================================
# from_mode is always present on a mode_change row
# ===========================================================================

class TestFromModeAlwaysRecorded:

    def _events(self, log_path):
        with open(log_path) as f:
            return [json.loads(line) for line in f if line.strip()]

    def test_first_set_records_from_mode_as_explicit_null(
        self, session_id, work_project, tmp_path, monkeypatch
    ):
        """A consumer must be able to tell 'no previous mode' from 'not recorded'."""
        log = tmp_path / "friction.log"
        monkeypatch.setenv("WRIT_FRICTION_LOG", str(log))
        writ_session.cmd_mode(session_id, "set", "work")

        rows = [e for e in self._events(log) if e["event"] == "mode_change"]
        assert rows, "mode set must emit a mode_change row"
        assert "from_mode" in rows[0], "the key must be present on a first set"
        assert rows[0]["from_mode"] is None
        assert rows[0]["to_mode"] == "work"

    def test_switch_records_the_mode_it_came_from(
        self, session_id, work_project, tmp_path, monkeypatch
    ):
        """Switching away names the mode being left."""
        _start_work(session_id, work_project)
        log = tmp_path / "friction.log"
        monkeypatch.setenv("WRIT_FRICTION_LOG", str(log))
        writ_session.cmd_mode(session_id, "switch", "investigate")

        rows = [
            e for e in self._events(log)
            if e["event"] == "mode_change" and e.get("change_type") == "switch"
        ]
        assert rows, "switch must emit a mode_change row with change_type=switch"
        assert rows[-1]["from_mode"] == "work"
        assert rows[-1]["to_mode"] == "investigate"


# ===========================================================================
# End-to-end through the hook: the auto-route must not destroy work state
# ===========================================================================

class TestAutoRouteMidSession:
    """Runs the real UserPromptSubmit hook against a dead daemon port (file fallback)."""

    def _env(self, tmp_path):
        env = os.environ.copy()
        env["WRIT_CACHE_DIR"] = str(tmp_path)
        env["WRIT_PORT"] = "59997"  # dead port -> file-direct fallback
        env["WRIT_HOST"] = "localhost"
        env["WRIT_FRICTION_LOG"] = str(tmp_path / "friction.log")
        env["WRIT_NO_AUTOSTART"] = "1"
        return env

    def _sandbox(self, tmp_path):
        """The cwd every subprocess below runs in, never the repo root.

        `mode set` stamps cache["project_root"] from the process cwd, and clearing gate
        state deletes <project_root>/.claude/gates/*.approved, so inheriting pytest's cwd
        made this suite delete the REAL repo's approval artifacts on every run.
        """
        sandbox = tmp_path / "sandbox"
        (sandbox / ".claude" / "gates").mkdir(parents=True, exist_ok=True)
        (sandbox / ".git").mkdir(exist_ok=True)
        return sandbox

    def _hook(self, env, sandbox, sid, prompt):
        return subprocess.run(
            ["bash", HOOK], input=json.dumps({"session_id": sid, "prompt": prompt}),
            capture_output=True, text=True, env=env, timeout=30, cwd=str(sandbox),
        )

    def _seed_gates(self, tmp_path, sid, gates):
        path = os.path.join(str(tmp_path), f"writ-session-{sid}.json")
        with open(path) as f:
            cache = json.load(f)
        cache["gates_approved"] = list(gates)
        with open(path, "w") as f:
            json.dump(cache, f)

    def _run(self, tmp_path, prompt, seed_mode, sid="midsession-e2e", seed_gates=None):
        env = self._env(tmp_path)
        sandbox = self._sandbox(tmp_path)
        subprocess.run(
            [sys.executable, HELPER, "mode", "set", seed_mode, sid],
            env=env, check=True, capture_output=True, text=True, cwd=str(sandbox),
        )
        if seed_gates:
            self._seed_gates(tmp_path, sid, seed_gates)
        r = self._hook(env, sandbox, sid, prompt)
        mode = subprocess.run(
            [sys.executable, HELPER, "mode", "get", sid],
            env=env, capture_output=True, text=True,
        ).stdout.strip()
        return r, mode, sid

    def test_investigate_prompt_midwork_switches_mode(self, tmp_path):
        """The stated case: mid-work, something turns up that needs investigating."""
        r, mode, _ = self._run(tmp_path, INVESTIGATE_PROMPT, seed_mode="work")
        assert r.returncode == 0, r.stderr
        assert mode == "investigate", f"expected a mid-session switch, got {mode!r}"

    def test_switching_midwork_preserves_the_approved_gates(self, tmp_path):
        """The safety property that replaces 'an explicit mode is never overridden'."""
        r, mode, sid = self._run(
            tmp_path, INVESTIGATE_PROMPT, seed_mode="work", seed_gates=["phase-a"]
        )
        assert mode == "investigate"
        paused = _read_raw_cache(tmp_path, sid)["paused_work_state"]
        assert paused is not None, "an auto-route out of work must save the work state"
        assert paused["gates_approved"] == ["phase-a"]

    @pytest.mark.parametrize("seeded", ["debug", "review", "conversation"])
    def test_explicit_specialist_mode_is_left_alone(self, tmp_path, seeded):
        """Only work and investigate are auto-routed between; the rest are the user's."""
        r, mode, _ = self._run(tmp_path, INVESTIGATE_PROMPT, seed_mode=seeded)
        assert r.returncode == 0, r.stderr
        assert mode == seeded, f"auto-route must not touch an explicit {seeded} mode"

    def test_returning_to_work_announces_the_restore_not_a_fresh_plan(self, tmp_path):
        """The announcement has to name the branch that actually ran.

        Coming back into work restores the gates approved before the detour, but the hook
        printed the fresh-start text either way: it told a session holding an approved plan
        to go write plan.md and present it for approval, which is work the cache still had.
        """
        env = self._env(tmp_path)
        sandbox = self._sandbox(tmp_path)
        sid = "midsession-restore"
        (sandbox / "plan.md").write_text("# Plan: unchanged across the detour\n")
        subprocess.run(
            [sys.executable, HELPER, "mode", "set", "work", sid],
            env=env, check=True, capture_output=True, text=True, cwd=str(sandbox),
        )
        self._seed_gates(tmp_path, sid, ["phase-a"])

        self._hook(env, sandbox, sid, INVESTIGATE_PROMPT)
        r = self._hook(
            env, sandbox, sid, "implement the export endpoint from the approved plan"
        )

        assert _read_raw_cache(tmp_path, sid)["gates_approved"] == ["phase-a"], (
            "precondition: an unchanged plan must have restored the approval"
        )
        assert "paused work mode restored automatically" in r.stdout, (
            f"the hook did not report the restore; stdout:\n{r.stdout[-800:]}"
        )
        assert "present them for approval" not in r.stdout, (
            "the hook told a session whose approvals were just restored to earn them again"
        )

    def test_a_first_route_into_work_still_asks_for_a_plan(self, tmp_path):
        """The restore wording must not leak onto a session that has nothing to restore."""
        r, mode, _ = self._run(
            tmp_path, "implement the export endpoint from the approved plan",
            seed_mode="investigate",
        )
        assert mode == "work"
        assert "present them for approval" in r.stdout, (
            f"a work route with no paused state must still ask for a plan; stdout:\n{r.stdout[-800:]}"
        )
        assert "restored automatically" not in r.stdout

    def test_same_mode_hint_does_not_re_switch(self, tmp_path):
        """A work-shaped prompt during work is a no-op, not a state-clearing re-set."""
        r, mode, sid = self._run(
            tmp_path, "implement the export endpoint from the approved plan",
            seed_mode="work", seed_gates=["phase-a"],
        )
        assert mode == "work"
        data = _read_raw_cache(tmp_path, sid)
        assert data["gates_approved"] == ["phase-a"], "a same-mode hint must not re-arm"


# ===========================================================================
# Part 6: mode_source provenance -- who chose the mode, not just what it is
#
# from_mode is null on BOTH the explicit and the automatic first-set path
# (mode_engine.py:371 guards that the auto path's old_mode is always None), so
# TestFromModeAlwaysRecorded above cannot tell a human's `mode set` apart from
# the classifier's `mode init`. mode_source is the only field that can, and it
# must survive a `mode switch` unchanged in either direction.
# ===========================================================================

class TestModeSourceStamping:
    def test_mode_set_stamps_explicit(self, session_id, work_project, tmp_path):
        writ_session.cmd_mode(session_id, "set", "work")

        data = _read_raw_cache(tmp_path, session_id)
        assert data["mode_source"] == "explicit"

    def test_mode_init_stamps_auto(self, session_id, work_project, tmp_path):
        writ_session.cmd_mode(session_id, "init", "investigate")

        data = _read_raw_cache(tmp_path, session_id)
        assert data["mode_source"] == "auto"

    def _events(self, log_path):
        with open(log_path) as f:
            return [json.loads(line) for line in f if line.strip()]

    def test_explicit_source_reaches_the_mode_change_telemetry_row(
        self, session_id, work_project, tmp_path, monkeypatch
    ):
        """The unreadable row this contract exists to fix: a mode_change row must
        say WHO chose the mode, not just what it became."""
        log = tmp_path / "friction.log"
        monkeypatch.setenv("WRIT_FRICTION_LOG", str(log))
        writ_session.cmd_mode(session_id, "set", "work")

        rows = [e for e in self._events(log) if e["event"] == "mode_change"]
        assert rows, "mode set must emit a mode_change row"
        assert rows[-1]["mode_source"] == "explicit"

    def test_auto_source_reaches_the_mode_change_telemetry_row(
        self, session_id, work_project, tmp_path, monkeypatch
    ):
        log = tmp_path / "friction.log"
        monkeypatch.setenv("WRIT_FRICTION_LOG", str(log))
        writ_session.cmd_mode(session_id, "init", "investigate")

        rows = [e for e in self._events(log) if e["event"] == "mode_change"]
        assert rows, "mode init must emit a mode_change row"
        assert rows[-1]["mode_source"] == "auto"


class TestModeSourcePreservedBySwitch:
    """`_mode_switch` changes the mode, never the story of who chose it. Both
    directions are covered: a test that only checks one cannot catch an
    implementation that hardcodes either value.
    """

    def test_switching_an_auto_routed_session_stays_auto(
        self, session_id, work_project, tmp_path
    ):
        writ_session.cmd_mode(session_id, "init", "investigate")
        writ_session.cmd_mode(session_id, "switch", "work")

        data = _read_raw_cache(tmp_path, session_id)
        assert data["mode"] == "work"
        assert data["mode_source"] == "auto", (
            "switching an auto-routed session must not relabel it explicit"
        )

    def test_switching_an_explicitly_set_session_stays_explicit(
        self, session_id, work_project, tmp_path
    ):
        writ_session.cmd_mode(session_id, "set", "work")
        writ_session.cmd_mode(session_id, "switch", "investigate")

        data = _read_raw_cache(tmp_path, session_id)
        assert data["mode"] == "investigate"
        assert data["mode_source"] == "explicit", (
            "switching an explicitly-set session must not relabel it auto"
        )


# ===========================================================================
# Part 6: reroute eligibility has TWO arms, not one explicit/auto split.
#
# Arm 1: current mode is work or investigate -> the reroute fires exactly as
# it did before mode_source existed, REGARDLESS of source. Explicit does not
# protect here -- this is the pre-existing guarantee that keeps an approved
# plan and approved tests from being lost to a misclassified prompt, and it
# must not regress.
#
# Arm 2: current mode is conversation, debug or review -> the reroute is
# eligible ONLY when mode_source == "auto". An explicitly chosen specialist
# mode stays put; an auto-set session that has since drifted into one of
# these (via a manual `mode switch`) is still fair game. This arm is the
# actual widening: before mode_source existed, an auto-routed session could
# never reach conversation/debug/review at all.
# ===========================================================================

class TestAutoRouteEligibilityByModeSource:
    def _env(self, tmp_path):
        env = os.environ.copy()
        env["WRIT_CACHE_DIR"] = str(tmp_path)
        env["WRIT_PORT"] = "59997"
        env["WRIT_HOST"] = "localhost"
        env["WRIT_FRICTION_LOG"] = str(tmp_path / "friction.log")
        env["WRIT_NO_AUTOSTART"] = "1"
        return env

    def _sandbox(self, tmp_path):
        sandbox = tmp_path / "sandbox"
        (sandbox / ".claude" / "gates").mkdir(parents=True, exist_ok=True)
        (sandbox / ".git").mkdir(exist_ok=True)
        return sandbox

    def _hook(self, env, sandbox, sid, prompt):
        return subprocess.run(
            ["bash", HOOK], input=json.dumps({"session_id": sid, "prompt": prompt}),
            capture_output=True, text=True, env=env, timeout=30, cwd=str(sandbox),
        )

    def _mode_get(self, env, sid):
        return subprocess.run(
            [sys.executable, HELPER, "mode", "get", sid],
            env=env, capture_output=True, text=True,
        ).stdout.strip()

    def test_auto_set_session_reroutes_whatever_it_has_since_become(self, tmp_path):
        """The widened half. Auto-route into investigate, then manually switch to
        debug (switch preserves the auto source across the manual move); a
        work-shaped prompt must still pull it into work, because mode_source --
        not the CURRENT mode -- decides eligibility.
        """
        env = self._env(tmp_path)
        sandbox = self._sandbox(tmp_path)
        sid = "eligibility-auto-drift"
        subprocess.run(
            [sys.executable, HELPER, "mode", "init", "investigate", sid],
            env=env, check=True, capture_output=True, text=True, cwd=str(sandbox),
        )
        subprocess.run(
            [sys.executable, HELPER, "mode", "switch", "debug", sid],
            env=env, check=True, capture_output=True, text=True, cwd=str(sandbox),
        )
        precondition = _read_raw_cache(tmp_path, sid)
        assert precondition["mode"] == "debug"
        assert precondition["mode_source"] == "auto", (
            "precondition: a manual switch must preserve the auto source, or "
            "this test proves nothing about the widened path"
        )

        r = self._hook(
            env, sandbox, sid, "implement the export endpoint from the approved plan"
        )
        assert r.returncode == 0, r.stderr

        assert self._mode_get(env, sid) == "work", (
            "an auto-sourced session must be eligible for reroute regardless of "
            "the mode it has since drifted to ('debug')"
        )

    def test_explicit_investigate_session_is_still_rerouted_to_work(self, tmp_path):
        """Arm 1: work/investigate reroute regardless of mode_source. An
        explicitly-set investigate session must still be pulled into work by a
        build-shaped prompt -- explicit does not protect here, because this is
        the pre-existing guarantee (an approved plan/tests must survive a
        misclassified prompt), not a new restriction mode_source introduces.
        """
        env = self._env(tmp_path)
        sandbox = self._sandbox(tmp_path)
        sid = "eligibility-explicit-investigate"
        subprocess.run(
            [sys.executable, HELPER, "mode", "set", "investigate", sid],
            env=env, check=True, capture_output=True, text=True, cwd=str(sandbox),
        )
        precondition = _read_raw_cache(tmp_path, sid)
        assert precondition["mode_source"] == "explicit"

        r = self._hook(
            env, sandbox, sid, "implement the export endpoint from the approved plan"
        )
        assert r.returncode == 0, r.stderr

        assert self._mode_get(env, sid) == "work", (
            "arm 1: work/investigate reroute must fire regardless of "
            "mode_source -- explicit does not protect a work/investigate "
            "session, only conversation/debug/review"
        )

    @pytest.mark.parametrize("seeded", ["conversation", "debug", "review"])
    def test_explicit_specialist_mode_is_not_rerouted(self, tmp_path, seeded):
        """Arm 2 (protection half): an explicitly chosen conversation, debug,
        or review session is never rerouted by the classifier. This is the
        NEW protection mode_source adds -- before it existed, an explicit
        specialist mode was safe only because the classifier's hint could
        never match it either.
        """
        env = self._env(tmp_path)
        sandbox = self._sandbox(tmp_path)
        sid = f"eligibility-explicit-{seeded}"
        subprocess.run(
            [sys.executable, HELPER, "mode", "set", seeded, sid],
            env=env, check=True, capture_output=True, text=True, cwd=str(sandbox),
        )
        precondition = _read_raw_cache(tmp_path, sid)
        assert precondition["mode_source"] == "explicit"

        r = self._hook(
            env, sandbox, sid, "implement the export endpoint from the approved plan"
        )
        assert r.returncode == 0, r.stderr

        assert self._mode_get(env, sid) == seeded, (
            f"arm 2: an explicitly-set {seeded} session must never be "
            f"rerouted by the classifier"
        )


class TestAutoRerouteNeverDestroysApprovals:
    """`mode set` runs _apply_mode_set, which clears gates_approved and
    paused_work_state; `mode switch` saves them instead. Proving the gates
    SURVIVE a reroute is a stronger guarantee than asserting which verb ran --
    it is the actual property `mode set` would destroy.
    """

    def _env(self, tmp_path):
        env = os.environ.copy()
        env["WRIT_CACHE_DIR"] = str(tmp_path)
        env["WRIT_PORT"] = "59997"
        env["WRIT_HOST"] = "localhost"
        env["WRIT_FRICTION_LOG"] = str(tmp_path / "friction.log")
        env["WRIT_NO_AUTOSTART"] = "1"
        return env

    def _sandbox(self, tmp_path):
        sandbox = tmp_path / "sandbox"
        (sandbox / ".claude" / "gates").mkdir(parents=True, exist_ok=True)
        (sandbox / ".git").mkdir(exist_ok=True)
        return sandbox

    def _hook(self, env, sandbox, sid, prompt):
        return subprocess.run(
            ["bash", HOOK], input=json.dumps({"session_id": sid, "prompt": prompt}),
            capture_output=True, text=True, env=env, timeout=30, cwd=str(sandbox),
        )

    def _seed_gates(self, tmp_path, sid, gates, phase=None):
        path = os.path.join(str(tmp_path), f"writ-session-{sid}.json")
        with open(path) as f:
            cache = json.load(f)
        cache["gates_approved"] = list(gates)
        if phase is not None:
            cache["current_phase"] = phase
        with open(path, "w") as f:
            json.dump(cache, f)

    def test_auto_reroute_out_of_work_pauses_the_approved_gate_instead_of_wiping_it(
        self, tmp_path
    ):
        env = self._env(tmp_path)
        sandbox = self._sandbox(tmp_path)
        sid = "reroute-preserves-approval"
        subprocess.run(
            [sys.executable, HELPER, "mode", "init", "work", sid],
            env=env, check=True, capture_output=True, text=True, cwd=str(sandbox),
        )
        self._seed_gates(tmp_path, sid, ["phase-a"], phase="testing")

        r = self._hook(env, sandbox, sid, "audit the codebase for security issues")
        assert r.returncode == 0, r.stderr

        after = _read_raw_cache(tmp_path, sid)
        assert after["mode"] == "investigate"
        assert after["paused_work_state"] is not None, (
            "a reroute that used `mode set` would have wiped this instead of "
            "pausing it"
        )
        assert after["paused_work_state"]["gates_approved"] == ["phase-a"], (
            "the approved gate must survive the reroute, not be cleared by "
            "`mode set`"
        )

    def test_auto_reroute_back_into_work_restores_rather_than_resets(self, tmp_path):
        """If the return leg used `mode set`, phase would land back at 'planning'
        and gates_approved would be wiped -- exactly the two invariants the
        switch-back restore path (unchanged plan.md) instead preserves.
        """
        env = self._env(tmp_path)
        sandbox = self._sandbox(tmp_path)
        sid = "reroute-restore-not-reset"
        (sandbox / "plan.md").write_text("# Plan: unchanged across the detour\n")
        subprocess.run(
            [sys.executable, HELPER, "mode", "init", "work", sid],
            env=env, check=True, capture_output=True, text=True, cwd=str(sandbox),
        )
        self._seed_gates(tmp_path, sid, ["phase-a"], phase="testing")

        self._hook(env, sandbox, sid, "audit the codebase for security issues")
        self._hook(
            env, sandbox, sid, "implement the export endpoint from the approved plan"
        )

        after = _read_raw_cache(tmp_path, sid)
        assert after["mode"] == "work"
        assert after["current_phase"] == "testing", (
            "`mode set` would have reset this to 'planning'; the restore must not"
        )
        assert after["gates_approved"] == ["phase-a"], (
            "`mode set` would have wiped this; the restore must keep it"
        )
