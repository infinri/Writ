"""INC-4: phase model (NRV-2 / N1 / N4).

`Phase` was vestigial: only PBK-PROC-BRAIN-001 had real Phase nodes, while three core
playbooks declared bare-string phase_ids that resolved to nothing. INC-4 adopts the BRAIN
pattern -- 11 content-bearing Phase nodes for WORK/ORCHESTRATOR/AUDIT-FANOUT, CONTAINS-linked,
with phase_ids pointing at them. This locks: every playbook's phase_ids resolves to a real
Phase node, CONTAINS is canonical and complete, and no Phase is orphaned.

Pure corpus assertions; always run.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.fixtures.md_helpers import BODY_WORD_BUDGET, frontmatter_body, word_count
from writ.graph.ingest import parse_edges_from_file, parse_nodes_from_file, validate_parsed_node

from tests._bible_guard import requires_bible

pytestmark = requires_bible


WRIT_ROOT = Path(__file__).resolve().parent.parent
METH = WRIT_ROOT / "bible" / "methodology"

PLAYBOOK_PHASES = {
    "PBK-PROC-WORK-WORKFLOW-001": ["PHA-WORK-001", "PHA-WORK-002", "PHA-WORK-003"],
    "PBK-PROC-ORCHESTRATOR-001": ["PHA-ORCH-001", "PHA-ORCH-002", "PHA-ORCH-003", "PHA-ORCH-004"],
    "PBK-PROC-AUDIT-FANOUT-001": ["PHA-FANOUT-001", "PHA-FANOUT-002", "PHA-FANOUT-003", "PHA-FANOUT-004"],
}
NEW_PHASES = [p for ps in PLAYBOOK_PHASES.values() for p in ps]


def _frontmatter(path: Path) -> dict:
    nodes = parse_nodes_from_file(path)
    return nodes[0] if nodes else {}


def _body(path: Path) -> str:
    return frontmatter_body(path)


def _all_playbook_files() -> list[Path]:
    return sorted(METH.glob("PBK-*.md"))


# --- 1. Every playbook's phase_ids resolves to a real Phase node (N1 lock) ----


class TestAllPhaseIdsResolve:
    def test_no_unresolved_phase_ids_anywhere(self) -> None:
        unresolved = []
        for f in _all_playbook_files():
            fm = _frontmatter(f)
            for pid in (fm.get("phase_ids") or []):
                if not (METH / f"{pid}.md").exists():
                    unresolved.append(f"{fm.get('playbook_id')}: phase_id '{pid}' has no Phase node")
        assert not unresolved, "unresolved phase_ids (bare strings):\n" + "\n".join(unresolved)


# --- 2. The 11 new Phase nodes parse + validate ------------------------------


class TestNewPhasesValid:
    @pytest.mark.parametrize("pid", NEW_PHASES)
    def test_phase_valid(self, pid: str) -> None:
        path = METH / f"{pid}.md"
        assert path.exists(), f"{pid}.md missing"
        fm = _frontmatter(path)
        assert fm.get("node_type") == "Phase" and fm.get("phase_id") == pid
        for field in ("position", "name", "description", "parent_playbook_id"):
            assert fm.get(field) not in (None, ""), f"{pid} missing {field}"
        try:
            validate_parsed_node(fm)
        except (ValueError, ValidationError) as e:
            pytest.fail(f"{pid} invalid: {e}")


# --- 3. CONTAINS canonical + complete; parent matches; positions contiguous --


class TestContains:
    @pytest.mark.parametrize("playbook,phases", sorted(PLAYBOOK_PHASES.items()))
    def test_contains_complete_and_parent_matches(self, playbook: str, phases: list[str]) -> None:
        edges = parse_edges_from_file(METH / f"{playbook}.md")
        contained = {e["target"] for e in edges if e.get("type") == "CONTAINS"}
        for pid in phases:
            assert pid in contained, f"{playbook} missing CONTAINS -> {pid}"
            fm = _frontmatter(METH / f"{pid}.md")
            assert fm.get("parent_playbook_id") == playbook, (
                f"{pid} parent_playbook_id != {playbook}"
            )
        positions = sorted(_frontmatter(METH / f"{pid}.md").get("position") for pid in phases)
        assert positions == list(range(1, len(phases) + 1)), (
            f"{playbook} phase positions not contiguous 1..{len(phases)}: {positions}"
        )


# --- 4. No orphan Phase nodes ------------------------------------------------


class TestNoOrphanPhases:
    def test_every_phase_is_contained(self) -> None:
        contained = set()
        for f in METH.glob("*.md"):
            for e in parse_edges_from_file(f):
                if e.get("type") == "CONTAINS":
                    contained.add(e["target"])
        orphans = [p.stem for p in METH.glob("PHA-*.md") if p.stem not in contained]
        assert not orphans, f"orphan Phase nodes (no CONTAINS edge): {orphans}"


# --- 5. Conciseness ----------------------------------------------------------


class TestConcise:
    @pytest.mark.parametrize("pid", NEW_PHASES)
    def test_body_within_budget(self, pid: str) -> None:
        words = word_count(_body(METH / f"{pid}.md"))
        assert words <= BODY_WORD_BUDGET, f"{pid} body {words} words > {BODY_WORD_BUDGET}"


# --- 6. Census ---------------------------------------------------------------


class TestCensus:
    def test_phase_count(self) -> None:
        n = len(list(METH.glob("PHA-*.md")))
        assert n == 20, f"expected 20 PHA-*.md (9 BRAIN + 11 new), found {n}"
