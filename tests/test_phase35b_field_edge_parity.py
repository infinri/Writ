"""Phase 3.5b: field-vs-edge parity for denormalized list fields.

RED-FIRST. Two denormalized caches drift as strict subsets of their edges (the
edges are explicitly declared = source of truth):
  - AntiPattern.counter_nodes  vs  outgoing COUNTERS edges
  - SubagentRole.dispatched_by vs  incoming DISPATCHES edges

Live drift (4 nodes): ANT-PROC-DEBUG-001 / ANT-PROC-SDD-001 omit their ENF-
counter; ROL-EXPLORER-001 omits PBK-PROC-AUDIT-FANOUT-001; ROL-IMPLEMENTER-001
omits PBK-PROC-ORCHESTRATOR-001. Fix = add the missing id to each frontmatter
field. Each test is isolated (TEST-ISO-001).
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import pytest
import pytest_asyncio

from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
from writ.graph.db import Neo4jConnection
from writ.graph.ingest import parse_edges_from_file, parse_nodes_from_file
from writ.graph.integrity import IntegrityChecker

NEO4J_URI = get_neo4j_uri()
NEO4J_USER = get_neo4j_user()
NEO4J_PASSWORD = get_neo4j_password()

METHODOLOGY_DIR = Path(__file__).resolve().parent.parent / "bible" / "methodology"


@pytest_asyncio.fixture()
async def db():
    conn = Neo4jConnection(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    await conn.clear_all()
    yield conn
    await conn.clear_all()
    await conn.close()
    # Restore rather than abandon: leaving the shared graph empty after this
    # module starves any corpus-reading module collected later in the same
    # session. Pre-existing defect, fixed alongside its copy in
    # test_dispatch_prose_parity.py (which mirrored this fixture, bug included).
    from tests._corpus import ensure_corpus
    ensure_corpus()


@pytest.fixture()
def checker(db: Neo4jConnection) -> IntegrityChecker:
    return IntegrityChecker(db._driver, db._database)


def _source_parity() -> tuple[dict, dict]:
    """Compute (counter_nodes, dispatched_by) drift directly from source."""
    counters_by_src: dict[str, set] = defaultdict(set)
    dispatches_by_tgt: dict[str, set] = defaultdict(set)
    anti: dict[str, set] = {}
    roles: dict[str, set] = {}
    for f in sorted(METHODOLOGY_DIR.glob("*.md")):
        for n in parse_nodes_from_file(f):
            if "antipattern_id" in n:
                anti[n["antipattern_id"]] = set(n.get("counter_nodes") or [])
            elif "role_id" in n:
                roles[n["role_id"]] = set(n.get("dispatched_by") or [])
        for e in parse_edges_from_file(f):
            if e["type"] == "COUNTERS":
                counters_by_src[e["source"]].add(e["target"])
            elif e["type"] == "DISPATCHES":
                dispatches_by_tgt[e["target"]].add(e["source"])
    cn = {aid: sorted(counters_by_src[aid] ^ field)
          for aid, field in anti.items() if field != counters_by_src[aid]}
    dby = {rid: sorted(dispatches_by_tgt[rid] ^ field)
           for rid, field in roles.items() if field != dispatches_by_tgt[rid]}
    return cn, dby


class TestCounterNodesParity:
    @pytest.mark.asyncio
    async def test_clean_when_field_equals_edges(self, db, checker) -> None:
        await db.create_methodology_node("AntiPattern",
            {"antipattern_id": "ANT-T-001", "counter_nodes": ["PBK-T-001", "ENF-T-001"]})
        await db.create_methodology_node("Playbook", {"playbook_id": "PBK-T-001"})
        await _mk_rule(db, "ENF-T-001")
        await db.create_edge("COUNTERS", "ANT-T-001", "PBK-T-001")
        await db.create_edge("COUNTERS", "ANT-T-001", "ENF-T-001")
        assert await checker.detect_counter_nodes_parity() is None

    @pytest.mark.asyncio
    async def test_drift_flagged_when_field_subset(self, db, checker) -> None:
        await db.create_methodology_node("AntiPattern",
            {"antipattern_id": "ANT-T-002", "counter_nodes": ["PBK-T-001"]})
        await db.create_methodology_node("Playbook", {"playbook_id": "PBK-T-001"})
        await _mk_rule(db, "ENF-T-001")
        await db.create_edge("COUNTERS", "ANT-T-002", "PBK-T-001")
        await db.create_edge("COUNTERS", "ANT-T-002", "ENF-T-001")
        result = await checker.detect_counter_nodes_parity()
        assert result is not None
        row = next(r for r in result if r["node_id"] == "ANT-T-002")
        assert row["missing_from_field"] == ["ENF-T-001"]


class TestDispatchedByParity:
    @pytest.mark.asyncio
    async def test_clean_when_field_equals_edges(self, db, checker) -> None:
        await db.create_methodology_node("SubagentRole",
            {"role_id": "ROL-T-001", "dispatched_by": ["PBK-T-001"]})
        await db.create_methodology_node("Playbook", {"playbook_id": "PBK-T-001"})
        await db.create_edge("DISPATCHES", "PBK-T-001", "ROL-T-001")
        assert await checker.detect_dispatched_by_parity() is None

    @pytest.mark.asyncio
    async def test_drift_flagged_when_field_subset(self, db, checker) -> None:
        await db.create_methodology_node("SubagentRole",
            {"role_id": "ROL-T-002", "dispatched_by": ["PBK-T-001"]})
        await db.create_methodology_node("Playbook", {"playbook_id": "PBK-T-001"})
        await db.create_methodology_node("Playbook", {"playbook_id": "PBK-T-002"})
        await db.create_edge("DISPATCHES", "PBK-T-001", "ROL-T-002")
        await db.create_edge("DISPATCHES", "PBK-T-002", "ROL-T-002")
        result = await checker.detect_dispatched_by_parity()
        assert result is not None
        row = next(r for r in result if r["node_id"] == "ROL-T-002")
        assert row["missing_from_field"] == ["PBK-T-002"]


class TestWiring:
    @pytest.mark.asyncio
    async def test_parity_in_findings_and_exit_code(self, db, checker) -> None:
        await db.create_methodology_node("AntiPattern",
            {"antipattern_id": "ANT-T-003", "counter_nodes": []})
        await db.create_methodology_node("Playbook", {"playbook_id": "PBK-T-001"})
        await db.create_edge("COUNTERS", "ANT-T-003", "PBK-T-001")
        findings = await checker.run_all_checks(skip_redundancy=True)
        assert "counter_nodes_parity" in findings
        assert "dispatched_by_parity" in findings
        assert findings["counter_nodes_parity"]
        assert findings["exit_code"] == 1


class TestLiveCorpusParity:
    """The shipped corpus must have ZERO counter_nodes / dispatched_by drift.

    RED until the 4 frontmatter fields are completed. Reads source directly.
    """

    def test_counter_nodes_in_parity(self) -> None:
        cn, _ = _source_parity()
        assert cn == {}, f"counter_nodes drift: {cn}"

    def test_dispatched_by_in_parity(self) -> None:
        _, dby = _source_parity()
        assert dby == {}, f"dispatched_by drift: {dby}"


async def _mk_rule(db: Neo4jConnection, rule_id: str) -> None:
    from datetime import date
    await db.create_rule({
        "rule_id": rule_id, "domain": "process", "severity": "medium",
        "scope": "file", "trigger": "t", "statement": "s", "violation": "v",
        "pass_example": "p", "enforcement": "e", "rationale": "r",
        "mandatory": False, "confidence": "production-validated",
        "evidence": "doc:original-bible", "staleness_window": 365,
        "last_validated": date.today().isoformat(),
    })
