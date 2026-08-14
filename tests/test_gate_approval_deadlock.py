"""Cycle A2: the gate can arm itself into a state nothing can clear.

THE DEFECT, reproduced live in this repo. Splicing a plan.md edit into the
implementation phase wedges a session: every write is denied and the user's
"approved" answers with "No approval gate was advanced".

Enforcement re-arms correctly. mode_engine._next_pending_gate compares
plan_md_hash(project_root) against gates_approved_plan[gate] and returns
"phase-a" when they differ (measured live: stored 6a82b8b34bb8, live
df06096a4c33). That behaviour is the entire point of binding an approval to
the plan it approved, and it is not the thing to fix here.

The approval path cannot SEE the re-arm. hooks/scripts/auto-approve-gate.sh:128
parses `.phase`, `.mode`, `.next_gate` and `.plan_hash` out of the server's
reply, but session_current_phase (writ/server/routes/session_state.py:184-193)
returns only `{"phase": phase}`. Three of the four fields the hook parses have
never existed, so it falls back to inferring the gate from the phase, and
"implementation" maps to nothing -- a re-arm during implementation is
therefore structurally invisible to the only mechanism that can clear it.

These tests drive `session_current_phase()` in-process (no daemon, no HTTP
client), matching tests/test_advance_gate_validation_parity.py's convention
for the sibling gate route. WRIT_CACHE_DIR is pointed at a pytest tmp_path for
every test in this file and every session id is synthetic
(`a2-deadlock-<random>`): this suite runs against the SAME cache machinery a
live operator's session uses, and a test that wrote to the real cache
directory could clear an operator's own gates mid-session. See
writ/session/cache.py:36-53 for how the cache directory resolves from that env
var at call time.

Two ways NOT to fix this, both pinned below:
  - deleting the plan-hash re-arm clears the deadlock but reopens the hole
    approval-binding closed (a finished cycle's approvals would cover a
    rewritten plan again);
  - reporting a next_gate unconditionally whenever current_phase ==
    "implementation" manufactures a gate where none is pending.
"""
from __future__ import annotations

import uuid

import pytest

from writ.server.routes.session_state import session_current_phase
from writ.session.cache import _read_cache, _write_cache
from writ.session.locators import plan_md_hash
from writ.session.mode_engine import _next_pending_gate


@pytest.fixture(autouse=True)
def _cache_dir(tmp_path, monkeypatch):
    """Every cache read/write in this file is isolated from var/session/.

    Per the HARD SAFETY CONSTRAINT: this endpoint can compute a gate re-arm,
    so a test that wrote to the real cache directory could clear an
    operator's own gates mid-session -- a genuine incident, not a flaky test.
    """
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setenv("WRIT_CACHE_DIR", str(cache_dir))


def _session_id() -> str:
    """A synthetic id that cannot collide with a real session's cache file."""
    return f"a2-deadlock-{uuid.uuid4().hex[:8]}"


def _write_plan(root, body: str) -> str:
    """Write a synthetic plan.md and return its fingerprint, so no test here
    depends on the real plan.md's content, which changes constantly."""
    (root / "plan.md").write_text(body, encoding="utf-8")
    return plan_md_hash(str(root))


def _seed(session_id: str, **overrides) -> None:
    """Minimal cache: read the schema-complete default, overlay only what the
    test needs, write it back. Matches
    tests/test_advance_gate_validation_parity.py's `_seed` helper.
    """
    cache = _read_cache(session_id)
    cache.update({
        "mode": "work",
        "current_phase": "planning",
        "gates_approved": [],
        "gates_approved_plan": {},
        "project_root": "",
    })
    cache.update(overrides)
    _write_cache(session_id, cache)


class TestSessionCurrentPhaseReturnsWhatItsCallerParses:
    """Capability 1 (plan.md ## Capabilities, cycle A2): the endpoint must
    stop returning a strict subset of the four fields
    auto-approve-gate.sh:128 parses."""

    @pytest.mark.asyncio
    async def test_response_includes_next_gate_mode_and_plan_hash(self, tmp_path) -> None:
        """Direct dict-index access (not .get): a missing key must raise
        KeyError, which is the correct RED for this endpoint today."""
        sid = _session_id()
        root = tmp_path / "project"
        root.mkdir()
        expected_hash = _write_plan(root, "# Plan: cycle A2\n")
        _seed(
            sid,
            current_phase="planning",
            gates_approved=[],
            gates_approved_plan={},
            project_root=str(root),
        )

        response = await session_current_phase(sid)

        assert response["phase"] == "planning", (
            f"expected phase 'planning', got {response.get('phase')!r}"
        )
        assert response["mode"] == "work", (
            f"expected mode 'work', got {response.get('mode')!r}"
        )
        assert response["plan_hash"] == expected_hash, (
            f"expected plan_hash {expected_hash!r}, got {response.get('plan_hash')!r}"
        )
        assert response["next_gate"] == "phase-a", (
            "a fresh work session with nothing approved must report "
            f"next_gate 'phase-a', got {response.get('next_gate')!r}"
        )


class TestNextGateSharesEnforcementsDerivation:
    """Capability 2: next_gate must be computed by _next_pending_gate, the
    SAME function enforcement uses, so the two derivations cannot diverge
    again."""

    @pytest.mark.asyncio
    async def test_next_gate_equals_next_pending_gate_for_the_same_cache(self, tmp_path) -> None:
        """Compares against _next_pending_gate(cache) for the cache the
        endpoint itself read, rather than an independently re-derived
        expected value -- re-deriving would let both drift together and
        still pass."""
        sid = _session_id()
        root = tmp_path / "project"
        root.mkdir()
        plan_hash = _write_plan(root, "# Plan: cycle A2\n")
        _seed(
            sid,
            current_phase="testing",
            gates_approved=["phase-a"],
            gates_approved_plan={"phase-a": plan_hash},
            project_root=str(root),
        )

        response = await session_current_phase(sid)
        cache_the_endpoint_read = _read_cache(sid)
        expected = _next_pending_gate(cache_the_endpoint_read)

        assert response["next_gate"] == expected, (
            f"endpoint next_gate {response.get('next_gate')!r} must equal "
            f"_next_pending_gate(cache) {expected!r} computed against the "
            "same cache; enforcement and the approval path must share one "
            "derivation, not two"
        )


class TestThePlanEditDuringImplementationDeadlock:
    """Capability 3, THE CENTERPIECE: a plan.md edit during the
    implementation phase must leave a gate the approval path can see and
    advance, instead of a session that blocks every write with no way out."""

    @pytest.mark.asyncio
    async def test_plan_edit_during_implementation_leaves_a_visible_gate(self, tmp_path) -> None:
        """Reproduces the live incident: current_phase == "implementation",
        both work-mode gates already approved, and gates_approved_plan holds
        a hash that does NOT match the plan.md now on disk. This must fail
        today for the right reason: session_current_phase returns only
        {"phase": phase}, so response["next_gate"] raises KeyError."""
        sid = _session_id()
        root = tmp_path / "project"
        root.mkdir()
        stale_hash = _write_plan(root, "# Plan: cycle E and F, as approved\n")
        _seed(
            sid,
            current_phase="implementation",
            gates_approved=["phase-a", "test-skeletons"],
            gates_approved_plan={"phase-a": stale_hash, "test-skeletons": stale_hash},
            project_root=str(root),
        )
        # The edit: plan.md is rewritten out from under the approved gates,
        # exactly like splicing a new cycle into plan.md mid-implementation.
        _write_plan(root, "# Plan: cycle E and F, PLUS cycle A2 spliced in\n")

        response = await session_current_phase(sid)

        assert response["next_gate"], (
            "a plan.md edit during implementation must leave a non-empty "
            f"next_gate for the approval path to see and advance; got "
            f"{response.get('next_gate')!r}, which reproduces the live "
            "deadlock ('No approval gate was advanced')"
        )


class TestTheFixDoesNotOvercorrect:
    """Capabilities 4 and 5: the two ways to break this while appearing to
    fix it. Removing the plan-hash re-arm clears the deadlock but reopens the
    hole approval-binding closed; reporting a gate unconditionally during
    implementation manufactures one where none is pending."""

    @pytest.mark.asyncio
    async def test_the_plan_hash_rearm_still_fires_through_the_endpoint(self, tmp_path) -> None:
        """Capability 4: the re-arm must still fire after the fix, proven in
        a phase OTHER than implementation and for a SPECIFIC gate name --
        not merely a truthy value in the one reported scenario. A change
        that deleted the plan-hash comparison to clear the deadlock would
        fail this test."""
        sid = _session_id()
        root = tmp_path / "project"
        root.mkdir()
        stale_hash = _write_plan(root, "# Plan: cycle A2, first draft\n")
        _seed(
            sid,
            current_phase="testing",
            gates_approved=["phase-a"],
            gates_approved_plan={"phase-a": stale_hash},
            project_root=str(root),
        )
        _write_plan(root, "# Plan: cycle A2, rewritten while testing\n")

        response = await session_current_phase(sid)

        assert response["next_gate"] == "phase-a", (
            "phase-a was approved against a plan that no longer exists on "
            f"disk, so it must re-arm; got next_gate={response.get('next_gate')!r}. "
            "A None/empty result here means the plan-hash re-arm was removed "
            "while fixing the endpoint, reopening the hole approval-binding closed."
        )

    @pytest.mark.asyncio
    async def test_an_approval_against_an_unchanged_plan_advances_nothing(self, tmp_path) -> None:
        """Capability 5: during implementation, with every approved gate's
        recorded hash matching the CURRENT plan.md, next_gate must be empty.
        A fix that reports a gate unconditionally whenever current_phase ==
        "implementation" would pass the centerpiece test above and must
        fail this one."""
        sid = _session_id()
        root = tmp_path / "project"
        root.mkdir()
        current_hash = _write_plan(root, "# Plan: cycle A2, final\n")
        _seed(
            sid,
            current_phase="implementation",
            gates_approved=["phase-a", "test-skeletons"],
            gates_approved_plan={"phase-a": current_hash, "test-skeletons": current_hash},
            project_root=str(root),
        )

        response = await session_current_phase(sid)
        next_gate = response["next_gate"]

        assert not next_gate, (
            "every approved gate's recorded plan hash matches the plan on "
            f"disk, so nothing is pending; got next_gate={next_gate!r}, which "
            "means the fix manufactured a gate where none is pending"
        )
