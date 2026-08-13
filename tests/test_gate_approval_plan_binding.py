"""An approval must not survive the plan it was granted against.

THE DEFECT, observed live. `gates_approved` (written at
approval_workflow.py:322, read at mode_engine.py:125) is a bare list of gate
NAMES. Nothing records which plan each approval covered, so rewriting plan.md
leaves every prior approval standing.

It happened in this repo's own session. Cycle 6a finished with
gates_approved=["phase-a", "test-skeletons"]. The router paused work mode, then
restored it, and mode_engine.py:460-495 correctly compared plan.md's fingerprint
across that pause and found it unchanged, because the planner had not run yet.
plan.md was then overwritten with cycle 7. Nothing re-checks, so a fresh cycle
began already holding both of the previous cycle's approvals, and the user's
genuine approval of the new plan advanced nothing ("no gate is pending").

WHY THE EXISTING FINGERPRINT IS NOT ENOUGH. mode_engine's check is bound to a
MODE SWITCH, so it answers "did the plan change while this session was away?".
The question that governs a gate is "does this approval cover the plan in front
of me right now?", and only the second one is safe against a rewrite that
happens between the switch and the gate.

THE DECISION THIS PINS. A gate whose recorded hash is missing counts as NOT
approved. On a governance gate the safe default when we cannot prove what an
approval covered is to ask again, and the cost is one re-approval per stale
session. The alternative, honoring unfingerprinted entries, leaves exactly the
hole this closes and leaves it open indefinitely.
"""
from __future__ import annotations

import pytest

from writ.session.locators import plan_md_hash
from writ.session.mode_engine import _next_pending_gate


def _work_cache(tmp_path, gates, gates_plan=None) -> dict:
    cache = {
        "mode": "work",
        "current_phase": "implementation",
        "gates_approved": list(gates),
        "project_root": str(tmp_path),
    }
    if gates_plan is not None:
        cache["gates_approved_plan"] = dict(gates_plan)
    return cache


def _write_plan(tmp_path, body: str) -> str:
    (tmp_path / "plan.md").write_text(body, encoding="utf-8")
    return plan_md_hash(str(tmp_path))


class TestAnApprovalIsBoundToItsPlan:
    def test_an_approval_survives_while_the_plan_is_unchanged(self, tmp_path) -> None:
        """The half that must NOT regress. Binding approvals is worthless if it
        re-asks on every gate check against an untouched plan."""
        h = _write_plan(tmp_path, "# Plan: cycle A\n")
        cache = _work_cache(tmp_path, ["phase-a"], {"phase-a": h})

        assert _next_pending_gate(cache) != "phase-a", (
            "an approval granted against THIS plan must still stand"
        )

    def test_an_approval_does_not_survive_a_plan_rewrite(self, tmp_path) -> None:
        """The defect. Same gate, same session, plan.md replaced."""
        h_a = _write_plan(tmp_path, "# Plan: cycle A\n")
        cache = _work_cache(tmp_path, ["phase-a"], {"phase-a": h_a})

        _write_plan(tmp_path, "# Plan: cycle B, an entirely different cycle\n")

        assert _next_pending_gate(cache) == "phase-a", (
            "an approval granted against cycle A's plan must not cover cycle "
            "B's; this is the live failure where a finished cycle's approvals "
            "carried into the next one"
        )

    def test_an_approval_with_no_recorded_plan_is_not_honored(self, tmp_path) -> None:
        """The decision, pinned. An entry predating the fingerprint cannot prove
        what it covered, so it re-arms rather than being grandfathered."""
        _write_plan(tmp_path, "# Plan: cycle A\n")
        cache = _work_cache(tmp_path, ["phase-a"])  # no gates_approved_plan at all

        assert _next_pending_gate(cache) == "phase-a", (
            "an approval with no recorded plan hash must re-arm; honoring it "
            "leaves the hole this change exists to close"
        )

    def test_only_the_stale_gate_re_arms(self, tmp_path) -> None:
        """Two gates, one rewritten under it. The still-valid one must not be
        collateral: a change that re-armed EVERY gate on any plan edit would
        pass the rewrite test above and make the workflow unusable."""
        h_a = _write_plan(tmp_path, "# Plan: cycle A\n")
        cache = _work_cache(
            tmp_path,
            ["phase-a", "test-skeletons"],
            {"phase-a": h_a, "test-skeletons": h_a},
        )
        assert _next_pending_gate(cache) is None

        h_b = _write_plan(tmp_path, "# Plan: cycle B\n")
        cache["gates_approved_plan"]["test-skeletons"] = h_b

        assert _next_pending_gate(cache) == "phase-a", (
            "phase-a was granted against the OLD plan and must re-arm; "
            "test-skeletons was re-granted against the current one and must not"
        )


class TestTheGrantRecordsTheHash:
    def test_apply_gate_advance_records_the_plan_it_approved(self, tmp_path) -> None:
        """Without this the enforcement above never sees a hash and every gate
        re-arms forever, which reads as 'approval is broken' rather than
        'approval is bound'."""
        from writ.session.approval_workflow import apply_phase_advance

        h = _write_plan(tmp_path, "# Plan: cycle A\n")
        cache = {"mode": "work", "current_phase": "planning", "project_root": str(tmp_path)}

        apply_phase_advance(
            cache,
            target_gate="phase-a",
            old_phase="planning",
            new_phase="testing",
            trigger="approval",
            mode="work",
        )

        assert "phase-a" in cache["gates_approved"]
        assert cache.get("gates_approved_plan", {}).get("phase-a") == h, (
            "the grant must fingerprint the plan it approved"
        )

    def test_a_cleared_phase_clears_the_fingerprints_too(self, tmp_path) -> None:
        """A stale fingerprint map outliving its gate list would let a
        re-approved gate name silently inherit an old binding."""
        from writ.session.mode_engine import _apply_mode_set

        h = _write_plan(tmp_path, "# Plan: cycle A\n")
        cache = _work_cache(tmp_path, ["phase-a"], {"phase-a": h})

        _apply_mode_set(cache, "work", mode_source="explicit")

        assert cache["gates_approved"] == []
        assert cache.get("gates_approved_plan", {}) == {}, (
            "clearing gates_approved must clear the fingerprints with it"
        )
