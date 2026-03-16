"""Phase 4: Integrity check tests.

Tests that IntegrityChecker detects known problems in crafted fixtures.
Requires Neo4j running with test data.
Each test is isolated (TEST-ISO-001).
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
import pytest_asyncio

from writ.graph.db import Neo4jConnection
from writ.graph.integrity import IntegrityChecker

NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "writdevpass"


def _make_rule(
    rule_id: str,
    mandatory: bool = False,
    trigger: str = "Default trigger",
    statement: str = "Default statement",
    last_validated: str | None = None,
    staleness_window: int = 365,
) -> dict:
    if last_validated is None:
        last_validated = date.today().isoformat()
    return {
        "rule_id": rule_id,
        "domain": "Test",
        "severity": "medium",
        "scope": "file",
        "trigger": trigger,
        "statement": statement,
        "violation": "Bad.",
        "pass_example": "Good.",
        "enforcement": "Review.",
        "rationale": "Testing.",
        "mandatory": mandatory,
        "confidence": "production-validated",
        "evidence": "doc:original-bible",
        "staleness_window": staleness_window,
        "last_validated": last_validated,
    }


@pytest_asyncio.fixture()
async def db():
    conn = Neo4jConnection(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    await conn.clear_all()
    yield conn
    await conn.clear_all()
    await conn.close()


@pytest.fixture()
def checker(db: Neo4jConnection) -> IntegrityChecker:
    return IntegrityChecker(db._driver, db._database)


class TestConflictDetection:
    """CONFLICTS_WITH edge detection."""

    @pytest.mark.asyncio
    async def test_conflict_detected(self, db: Neo4jConnection, checker: IntegrityChecker) -> None:
        await db.create_rule(_make_rule("RULE-A-001"))
        await db.create_rule(_make_rule("RULE-B-001"))
        await db.create_edge("CONFLICTS_WITH", "RULE-A-001", "RULE-B-001")

        conflicts = await checker.detect_conflicts()
        assert len(conflicts) == 1
        pair = conflicts[0]
        assert pair["rule_a"] == "RULE-A-001"
        assert pair["rule_b"] == "RULE-B-001"

    @pytest.mark.asyncio
    async def test_no_false_positives(self, db: Neo4jConnection, checker: IntegrityChecker) -> None:
        await db.create_rule(_make_rule("RULE-A-001"))
        await db.create_rule(_make_rule("RULE-B-001"))
        await db.create_edge("DEPENDS_ON", "RULE-A-001", "RULE-B-001")

        conflicts = await checker.detect_conflicts()
        assert len(conflicts) == 0


class TestOrphanDetection:
    """Rules with zero edges."""

    @pytest.mark.asyncio
    async def test_orphan_flagged(self, db: Neo4jConnection, checker: IntegrityChecker) -> None:
        await db.create_rule(_make_rule("ORPHAN-RULE-001"))

        orphans = await checker.detect_orphans()
        assert "ORPHAN-RULE-001" in orphans

    @pytest.mark.asyncio
    async def test_connected_rule_not_orphan(self, db: Neo4jConnection, checker: IntegrityChecker) -> None:
        await db.create_rule(_make_rule("RULE-A-001"))
        await db.create_rule(_make_rule("RULE-B-001"))
        await db.create_edge("RELATED_TO", "RULE-A-001", "RULE-B-001")

        orphans = await checker.detect_orphans()
        assert "RULE-A-001" not in orphans
        assert "RULE-B-001" not in orphans


class TestStalenessDetection:
    """Rules past staleness window."""

    @pytest.mark.asyncio
    async def test_stale_rule_flagged(self, db: Neo4jConnection, checker: IntegrityChecker) -> None:
        old_date = (date.today() - timedelta(days=400)).isoformat()
        await db.create_rule(_make_rule("STALE-RULE-001", last_validated=old_date, staleness_window=365))

        stale = await checker.detect_stale()
        stale_ids = [s["rule_id"] for s in stale]
        assert "STALE-RULE-001" in stale_ids

    @pytest.mark.asyncio
    async def test_fresh_rule_not_flagged(self, db: Neo4jConnection, checker: IntegrityChecker) -> None:
        today = date.today().isoformat()
        await db.create_rule(_make_rule("FRESH-RULE-001", last_validated=today))

        stale = await checker.detect_stale()
        stale_ids = [s["rule_id"] for s in stale]
        assert "FRESH-RULE-001" not in stale_ids


class TestRedundancyDetection:
    """Near-identical rule content detection."""

    @pytest.mark.asyncio
    async def test_near_identical_flagged(self, db: Neo4jConnection, checker: IntegrityChecker) -> None:
        await db.create_rule(_make_rule(
            "DUP-A-001",
            trigger="Controller must not contain SQL queries directly",
            statement="All data access must go through repository layer",
        ))
        await db.create_rule(_make_rule(
            "DUP-B-001",
            trigger="Controller must not contain SQL queries directly",
            statement="All data access must go through repository layer",
        ))

        redundant = await checker.detect_redundant()
        assert len(redundant) >= 1
        pair = redundant[0]
        ids = {pair["rule_a"], pair["rule_b"]}
        assert ids == {"DUP-A-001", "DUP-B-001"}

    @pytest.mark.asyncio
    async def test_different_rules_clean(self, db: Neo4jConnection, checker: IntegrityChecker) -> None:
        await db.create_rule(_make_rule(
            "DIFF-A-001",
            trigger="SQL query uses positional placeholders",
            statement="Use named bind parameters instead of positional",
        ))
        await db.create_rule(_make_rule(
            "DIFF-B-001",
            trigger="Class hierarchy exceeds 2 levels",
            statement="Refactor to use composition via constructor injection",
        ))

        redundant = await checker.detect_redundant()
        assert len(redundant) == 0


class TestRunAllChecks:
    """Orchestrator behavior."""

    @pytest.mark.asyncio
    async def test_clean_graph_returns_zero(self, db: Neo4jConnection, checker: IntegrityChecker) -> None:
        await db.create_rule(_make_rule("RULE-A-001"))
        await db.create_rule(_make_rule("RULE-B-001"))
        await db.create_edge("RELATED_TO", "RULE-A-001", "RULE-B-001")

        findings = await checker.run_all_checks(skip_redundancy=True)
        assert findings["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_any_finding_returns_nonzero(self, db: Neo4jConnection, checker: IntegrityChecker) -> None:
        await db.create_rule(_make_rule("RULE-A-001"))
        await db.create_rule(_make_rule("RULE-B-001"))
        await db.create_edge("CONFLICTS_WITH", "RULE-A-001", "RULE-B-001")

        findings = await checker.run_all_checks(skip_redundancy=True)
        assert findings["exit_code"] == 1
        assert len(findings["conflicts"]) == 1
