"""INC-6: SDD implementer contract (ABS: subagent-driven-development).

The implementer subagent previously had no structured status protocol, no clarifying-questions
instruction, and no "escalate when stuck" guidance; the SDD controller had no handling matrix
for implementer statuses. This increment upgrades the implementer contract (authored in
.claude/agents/writ-implementer.md, mirrored byte-identical into ROL-IMPLEMENTER-001.prompt_template
per the FIX-5 render lock) and adds the controller status-handling matrix to PBK-PROC-SDD-001.

Pure corpus assertions; always run. The live export --check is covered by test_phase3b /
test_fix5 under corpus_ready.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

from writ.graph.ingest import parse_nodes_from_file

WRIT_ROOT = Path(__file__).resolve().parent.parent
METH = WRIT_ROOT / "bible" / "methodology"
AGENTS_DIR = WRIT_ROOT / "agents"
ROL = METH / "ROL-IMPLEMENTER-001.md"
AGENT = AGENTS_DIR / "writ-implementer.md"
SDD = METH / "PBK-PROC-SDD-001.md"
EXPORT_SCRIPT = WRIT_ROOT / "scripts" / "export_subagent_roles.py"

STATUS_CODES = ["DONE", "DONE_WITH_CONCERNS", "BLOCKED", "NEEDS_CONTEXT"]


def _role_node() -> dict:
    nodes = parse_nodes_from_file(ROL)
    return nodes[0] if nodes else {}


def _prompt_template() -> str:
    return _role_node().get("prompt_template", "")


def _load_render():
    spec = importlib.util.spec_from_file_location("export_subagent_roles", EXPORT_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["export_subagent_roles"] = mod
    spec.loader.exec_module(mod)
    return mod.render_agent_md


def _sdd_body() -> str:
    m = re.match(r"^---\n.*?\n---\n(.*)$", SDD.read_text(encoding="utf-8"), re.S)
    return m.group(1) if m else ""


class TestStatusProtocol:
    """The implementer prompt carries all four status codes + a report instruction."""

    def test_all_four_status_codes_present(self) -> None:
        pt = _prompt_template()
        missing = [c for c in STATUS_CODES if c not in pt]
        assert not missing, f"implementer prompt_template omits status codes: {missing}"

    def test_report_status_section_present(self) -> None:
        pt = _prompt_template().lower()
        assert "report status" in pt, "implementer prompt lacks a 'Report status' section"
        # the contract: never silently ship unsure work.
        assert "never silently ship" in pt or "performative" in pt, (
            "implementer prompt does not forbid silently shipping unsure work"
        )


class TestClarifyingAndEscalation:
    """Clarifying-questions (before + during) and the 'over your head' escalation."""

    def test_clarifying_questions_before_and_during(self) -> None:
        pt = _prompt_template().lower()
        assert "ask, do not guess" in pt, "missing 'ask, do not guess' clarifying instruction"
        assert "before" in pt and "while you work" in pt, (
            "clarifying instruction must cover both before starting and during work"
        )

    def test_escalation_section_present(self) -> None:
        pt = _prompt_template().lower()
        assert "over your head" in pt, "missing 'in over your head' escalation section"
        assert "bad work is worse than no work" in pt, "missing the escalation principle"
        # escalation routes to the two non-success status codes.
        assert "blocked or\nneeds_context" in pt or "blocked or needs_context" in pt, (
            "escalation must route to BLOCKED or NEEDS_CONTEXT"
        )


class TestRenderLock:
    """FIX-5 render lock: the node still renders byte-identical to its agent file."""

    def test_node_renders_to_agent_file(self) -> None:
        render = _load_render()
        node = _role_node()
        assert render(node) == AGENT.read_text(encoding="utf-8"), (
            "ROL-IMPLEMENTER-001 no longer byte-matches writ-implementer.md; "
            "export --check would report drift."
        )


class TestControllerMatrix:
    """PBK-PROC-SDD-001 documents handling for each status + the no-blind-retry rule."""

    def test_handles_each_status(self) -> None:
        body = _sdd_body()
        missing = [c for c in STATUS_CODES if c not in body]
        assert not missing, f"SDD playbook omits handling for statuses: {missing}"

    def test_never_redispatch_unchanged(self) -> None:
        body = _sdd_body().lower()
        assert "unchanged" in body and "re-dispatch" in body, (
            "SDD playbook missing the 'never re-dispatch the same model unchanged' rule"
        )

    def test_continuous_execution(self) -> None:
        body = _sdd_body().lower()
        assert "continuous" in body or "do not pause" in body, (
            "SDD playbook missing the continuous-execution rule"
        )


class TestCensusUnchanged:
    """INC-6 adds no nodes: counts of the touched types are unchanged."""

    def test_role_count(self) -> None:
        n = len(list(METH.glob("ROL-*.md")))
        assert n == 5, f"expected 5 ROL-*.md, found {n}"

    def test_sdd_playbook_count(self) -> None:
        n = len(list(METH.glob("PBK-PROC-SDD-*.md")))
        assert n == 1, f"expected 1 PBK-PROC-SDD-*.md, found {n}"
