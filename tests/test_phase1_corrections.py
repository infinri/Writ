"""Phase 1 corrections (foundation-independent): one-level orchestration + PRECEDES order.

Content-guards on the methodology SOURCE (bible/methodology/*.md), which is the source of
truth for methodology per Phase 0 (the graph is derived from it). These are corrections with
a known-correct target state (the one-level model, verified in WRIT-BLUEPRINT 3.3 and the
fanout-nesting-impossible finding):

  1.1  PBK-PROC-AUDIT-FANOUT-001 + PHA-FANOUT-002/003 must not teach worker-spawns-worker
       recursion (sub-agents have no Task tool; nested dispatch cannot execute).
  1.2  PBK-PROC-ORCHESTRATOR-001 must carry the no-nesting constraint as explicit rationale.
  1.3a SKL-PROC-EXEC must NOT PRECEDE SKL-PROC-PLAN (reversed); PLAN must PRECEDE EXEC.

These are foundation-independent: they need neither the INVOKES edge type (1.3b) nor the
routing-as-data fields (1.6-1.8).
"""
from __future__ import annotations

from pathlib import Path

from tests._bible_guard import requires_bible

pytestmark = requires_bible


BIBLE = Path(__file__).resolve().parent.parent / "bible" / "methodology"


def _read(node_id: str) -> str:
    return (BIBLE / f"{node_id}.md").read_text(encoding="utf-8")


class TestFanoutIsOneLevel:
    """1.1: the audit fan-out playbook must not teach recursion."""

    FORBIDDEN = [
        "worker sub-agents, recursively",
        "recursing (re-estimate, re-partition, spawn)",
        "## The recursion",
    ]

    def test_fanout_node_drops_recursion_language(self) -> None:
        text = _read("PBK-PROC-AUDIT-FANOUT-001")
        hits = [p for p in self.FORBIDDEN if p in text]
        assert not hits, f"PBK-PROC-AUDIT-FANOUT-001 still teaches recursion: {hits}"

    def test_fanout_node_asserts_one_level(self) -> None:
        text = _read("PBK-PROC-AUDIT-FANOUT-001").lower()
        assert (
            "one level" in text
            or "main chat is the only orchestrator" in text
            or "workers never spawn" in text
        ), "PBK-PROC-AUDIT-FANOUT-001 does not state the one-level / no-nesting constraint"

    def test_fanout_phase_nodes_drop_recursion(self) -> None:
        for nid in ("PHA-FANOUT-001", "PHA-FANOUT-002", "PHA-FANOUT-003", "PHA-FANOUT-004"):
            assert "recurs" not in _read(nid).lower(), f"{nid} still uses recursion language"

    def test_fanout_family_drops_lead_orchestrator_model(self) -> None:
        """The one-level model has a main chat + workers, never a 'lead' sub-agent."""
        family = ["PBK-PROC-AUDIT-FANOUT-001", "PHA-FANOUT-001", "PHA-FANOUT-002",
                  "PHA-FANOUT-003", "PHA-FANOUT-004"]
        offenders = [nid for nid in family if "lead" in _read(nid).lower()]
        assert not offenders, f"fan-out family still references the 'lead' orchestrator model: {offenders}"


class TestOrchestratorNoNesting:
    """1.2: the orchestrator playbook must state the no-nesting constraint as rationale."""

    def test_orchestrator_states_no_nesting_constraint(self) -> None:
        text = _read("PBK-PROC-ORCHESTRATOR-001").lower()
        assert (
            "never spawn" in text
            or "cannot spawn sub-agents" in text
            or "have no task tool" in text
            or "have no `task` tool" in text
        ), "PBK-PROC-ORCHESTRATOR-001 lacks the explicit no-nesting (workers never spawn) constraint"


class TestPlanPrecedesExec:
    """1.3a: PLAN precedes EXEC, not the reverse."""

    def test_exec_does_not_precede_plan(self) -> None:
        text = _read("SKL-PROC-EXEC-001")
        assert "target: SKL-PROC-PLAN-001, type: PRECEDES" not in text, (
            "SKL-PROC-EXEC-001 still declares `PRECEDES SKL-PROC-PLAN-001` (reversed)"
        )

    def test_plan_precedes_exec(self) -> None:
        text = _read("SKL-PROC-PLAN-001")
        assert "target: SKL-PROC-EXEC-001, type: PRECEDES" in text, (
            "missing the corrected `SKL-PROC-PLAN-001 PRECEDES SKL-PROC-EXEC-001` edge"
        )
