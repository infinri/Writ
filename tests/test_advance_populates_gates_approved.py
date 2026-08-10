"""Regression: a phase advance must record the approved gate in cache['gates_approved'].

Root cause (debug 2026-06-30): two phase-advance paths diverged. The CLI path
(approval_workflow._apply_gate_advance) records the approved gate in
cache['gates_approved'] AND sets current_phase. The live HTTP path
(server.py _advance) advanced current_phase but never touched gates_approved.
The work-mode write gate (gates._check_work_gate) authorizes writes from
gates_approved (deny ENF-GATE-PLAN when 'phase-a' is absent), so on the live
path gates_approved stayed [] no matter how many times the user approved, and
every non-skill-exempt, non-excluded production write was blocked. The writ repo
hid the bug because its writes are skill_exempt; other projects hit the wall.

Fix: both paths now delegate ALL five cache mutations to the shared
apply_phase_advance function in approval_workflow.py, which records the gate via
sorted set-union, sets current_phase, clears denial_counts[target_gate], rolls
loaded_rule_ids_by_phase, and appends a full phase_transitions record.

TestRecordApprovedGate is hermetic (no daemon, no network). The three
add/sorted/preserve cases now drive apply_phase_advance's gates_approved behavior
directly. The non-work and all-approved no-op cases are reframed at the level
where the no-advance decision actually lives: _next_pending_gate returning None
is the signal both callers use to skip apply_phase_advance entirely.

TestAdvancePopulatesGatesApproved is an end-to-end check against the live daemon
(skips if unreachable; needs a restart to pick up the server.py change) proving
_advance actually calls apply_phase_advance and persists gates_approved.
"""

from __future__ import annotations

import json
import os
import tempfile
import urllib.error
import urllib.request
import uuid

import pytest

from tests._daemon import _port


# ---------------------------------------------------------------------------
# Hermetic: gates_approved behavior of apply_phase_advance (repointed from
# the removed record_approved_gate helper)
# ---------------------------------------------------------------------------

class TestRecordApprovedGate:
    """Repointed from the removed mode_engine.record_approved_gate.

    Cases (a): gates_approved add/sorted/preserve -- now assert
    apply_phase_advance's gates_approved behavior.
    Cases (b): non-work and all-gates-approved no-op -- now assert that
    _next_pending_gate returns None (the no-advance signal used by both
    callers to skip apply_phase_advance).
    """

    # (a) apply_phase_advance gates_approved behavior -------------------------

    def test_fresh_work_session_records_phase_a_gate(self):
        """apply_phase_advance adds phase-a to an empty gates_approved list."""
        from writ.session.approval_workflow import apply_phase_advance

        cache = {"gates_approved": [], "phase_transitions": []}
        apply_phase_advance(
            cache, "phase-a", "planning", "testing",
            trigger="user-approved", mode="work",
        )
        assert cache["gates_approved"] == ["phase-a"]

    def test_after_phase_a_adding_test_skeletons_preserves_prior_gate(self):
        """apply_phase_advance adds test-skeletons while keeping phase-a present
        and the result sorted."""
        from writ.session.approval_workflow import apply_phase_advance

        cache = {"gates_approved": ["phase-a"], "phase_transitions": []}
        apply_phase_advance(
            cache, "test-skeletons", "testing", "implementation",
            trigger="user-approved", mode="work",
        )
        assert cache["gates_approved"] == ["phase-a", "test-skeletons"]

    def test_gates_approved_result_is_sorted_after_advance(self):
        """The set-union add produces a sorted list regardless of insertion order."""
        from writ.session.approval_workflow import apply_phase_advance

        cache = {"phase_transitions": []}
        apply_phase_advance(
            cache, "phase-a", "planning", "testing",
            trigger="user-approved", mode="work",
        )
        assert cache["gates_approved"] == sorted(cache["gates_approved"])

    # (b) _next_pending_gate returns None -- the no-advance decision in the
    # caller that prevents apply_phase_advance from being called at all -------

    def test_non_work_mode_next_pending_gate_returns_none(self):
        """For a non-work mode the no-advance decision lives in the caller:
        _next_pending_gate returns None, so apply_phase_advance is never called
        and gates_approved is not touched."""
        from writ.session.mode_engine import _next_pending_gate

        cache = {"mode": "conversation", "gates_approved": []}
        assert _next_pending_gate(cache) is None
        assert cache["gates_approved"] == []

    def test_all_gates_approved_next_pending_gate_returns_none(self):
        """When both work-mode gates are already in gates_approved,
        _next_pending_gate returns None -- the caller returns a no-advance
        result and does not call apply_phase_advance again."""
        from writ.session.mode_engine import _next_pending_gate

        cache = {
            "mode": "work",
            "gates_approved": ["phase-a", "test-skeletons"],
        }
        assert _next_pending_gate(cache) is None
        assert cache["gates_approved"] == ["phase-a", "test-skeletons"]


# ---------------------------------------------------------------------------
# Integration: the live /advance-phase route persists gates_approved
# ---------------------------------------------------------------------------

SERVER = f"http://localhost:{_port()}"


def _server_up() -> bool:
    try:
        with urllib.request.urlopen(f"{SERVER}/health", timeout=2):
            return True
    except (urllib.error.URLError, OSError):
        return False


def _cache_path(session_id: str) -> str:
    return os.path.join(tempfile.gettempdir(), f"writ-session-{session_id}.json")


def _token_path(session_id: str) -> str:
    return os.path.join(tempfile.gettempdir(), f"writ-gate-token-{session_id}")


def _post_advance(session_id: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{SERVER}/session/{session_id}/advance-phase",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


class TestAdvancePopulatesGatesApproved:
    def test_advance_from_testing_records_test_skeletons_gate(self):
        if not _server_up():
            pytest.skip("Writ server unreachable")
        sid = f"gatesync-{uuid.uuid4().hex[:8]}"
        token = uuid.uuid4().hex
        # A realistic work session at the testing gate: planning already approved.
        with open(_cache_path(sid), "w") as f:
            json.dump(
                {"mode": "work", "current_phase": "testing", "gates_approved": ["phase-a"]},
                f,
            )
        # A BOUND token: line 1 the secret, line 2 the gate it authorizes, line 3 the plan
        # fingerprint. The route claims through claim_gate_token with no unbound fallback,
        # so a bare one-line secret is refused before any gate logic runs. The two binding
        # lines are written literally rather than derived from this process's cache,
        # because the DAEMON recomputes them from ITS cache read: the gate is the one the
        # seed above leaves pending, and the fingerprint is empty because the seeded cache
        # carries no project_root for plan_md_hash to hash.
        with open(_token_path(sid), "w") as f:
            f.write(f"{token}\ntest-skeletons\n\n")
        try:
            result = _post_advance(sid, {"confirmation_source": "tool", "token": token})
            # The advance itself must have happened (rules out a trivial pass via refusal).
            assert result.get("phase") == "implementation", (
                f"advance must reach implementation; got {result}"
            )
            with open(_cache_path(sid)) as f:
                cache = json.load(f)
            assert cache.get("current_phase") == "implementation", cache
            assert "test-skeletons" in cache.get("gates_approved", []), (
                "advancing the live path from testing must record the test-skeletons "
                f"gate in gates_approved; got {cache.get('gates_approved')!r}"
            )
            # The prior approval must be preserved, not clobbered.
            assert "phase-a" in cache.get("gates_approved", []), cache
        finally:
            for p in (_cache_path(sid), _token_path(sid)):
                try:
                    os.remove(p)
                except OSError:
                    pass
