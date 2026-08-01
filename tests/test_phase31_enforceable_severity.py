"""Phase 3.1: detect_enforceable_severity_coupling -- enforceable critical/high
rules must be mandatory.

Severity = how bad the violation; mandatory = whether a machine can catch it
out-of-band. A rule that carries a mechanical_enforcement_path (MEP) yet is left
advisory is the flag. This LOCKS a measured-0 state: all 27 MEP-bearing rules in
the corpus are already mandatory, so the corpus is clean. RED is fixture-driven
(seed the bug -> validator flags it); the corpus-clean test is the regression
lock. Severity alone is NOT the trigger -- the bulk of advisory critical/high
rules have no MEP and are correctly left advisory. Each test isolated (TEST-ISO-001).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import pytest_asyncio

from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
from writ.graph.db import Neo4jConnection
from writ.graph.ingest import (
    discover_rule_files,
    parse_nodes_from_file,
    parse_rules_from_file,
)
from writ.graph.integrity import IntegrityChecker

from tests._bible_guard import requires_bible

pytestmark = requires_bible


NEO4J_URI = get_neo4j_uri()
NEO4J_USER = get_neo4j_user()
NEO4J_PASSWORD = get_neo4j_password()

BIBLE_DIR = Path(__file__).resolve().parent.parent / "bible"


def _make_rule(rule_id: str, severity: str, mandatory: bool, mep: str | None) -> dict:
    d = {
        "rule_id": rule_id,
        "domain": "security",
        "severity": severity,
        "scope": "file",
        "trigger": "t.",
        "statement": "s.",
        "violation": "v.",
        "pass_example": "p.",
        "enforcement": "e.",
        "rationale": "r.",
        "mandatory": mandatory,
        "confidence": "production-validated",
        "evidence": "doc:original-bible",
        "staleness_window": 365,
        "last_validated": date.today().isoformat(),
    }
    if mep is not None:
        d["mechanical_enforcement_path"] = mep
    return d


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


class TestDetectEnforceableSeverityCoupling:
    @pytest.mark.asyncio
    async def test_enforceable_critical_advisory_flagged(self, db, checker) -> None:
        await db.create_rule(_make_rule("SEC-T-001", "critical", False, "writ-auth-scan"))
        result = await checker.detect_enforceable_severity_coupling()
        assert result is not None
        assert any(r["rule_id"] == "SEC-T-001" for r in result)

    @pytest.mark.asyncio
    async def test_enforceable_high_advisory_flagged(self, db, checker) -> None:
        await db.create_rule(_make_rule("SEC-T-002", "high", False, "writ-lint"))
        result = await checker.detect_enforceable_severity_coupling()
        assert result is not None
        assert any(r["rule_id"] == "SEC-T-002" for r in result)

    @pytest.mark.asyncio
    async def test_enforceable_mandatory_not_flagged(self, db, checker) -> None:
        await db.create_rule(_make_rule("SEC-T-003", "critical", True, "writ-auth-scan"))
        assert await checker.detect_enforceable_severity_coupling() is None

    @pytest.mark.asyncio
    async def test_advisory_without_mep_not_flagged(self, db, checker) -> None:
        # Severity alone is NOT the trigger -- no MEP means nothing to enforce.
        await db.create_rule(_make_rule("SEC-T-004", "critical", False, None))
        assert await checker.detect_enforceable_severity_coupling() is None

    @pytest.mark.asyncio
    async def test_medium_with_mep_not_flagged(self, db, checker) -> None:
        await db.create_rule(_make_rule("SEC-T-005", "medium", False, "writ-lint"))
        assert await checker.detect_enforceable_severity_coupling() is None


class TestWiring:
    @pytest.mark.asyncio
    async def test_in_findings_and_exit_code(self, db, checker) -> None:
        await db.create_rule(_make_rule("SEC-T-006", "critical", False, "writ-auth-scan"))
        findings = await checker.run_all_checks(skip_redundancy=True)
        assert "enforceable_severity" in findings
        assert findings["enforceable_severity"]
        assert findings["exit_code"] == 1


def _all_rules() -> list[dict]:
    """Every rule, BOTH storage formats: RULE-START blocks under bible/<domain>/
    AND YAML-frontmatter Rules under bible/methodology/ (e.g. META-AUTH-*).

    The methodology-dir Rules are the blind spot that initially hid
    META-AUTH-001/002 from this witness -- parse_rules_from_file only sees
    RULE-START markers, so the YAML-frontmatter Rules must be picked up via
    parse_nodes_from_file.
    """
    out: list[dict] = []
    for f in discover_rule_files(BIBLE_DIR):
        if "methodology" in f.parts:
            out.extend(n for n in parse_nodes_from_file(f) if "rule_id" in n)
        else:
            out.extend(parse_rules_from_file(f))
    return out


class TestLiveCorpusClean:
    """The shipped corpus must have ZERO enforceable-but-advisory rules.

    Covers BOTH rule formats (the methodology-dir YAML Rules included), so it
    actually witnesses META-AUTH-001/002 -- the rules an earlier methodology-skip
    let slip past. Green after the META-AUTH MEP de-claim (Option A).
    """

    def test_methodology_rules_are_covered(self) -> None:
        # Guard against the blind spot regressing: META-AUTH must be in scope.
        ids = {r["rule_id"] for r in _all_rules()}
        assert {"META-AUTH-001", "META-AUTH-002"} <= ids

    def test_no_enforceable_advisory_rules(self) -> None:
        offenders: list[tuple[str, str]] = []
        for r in _all_rules():
            sev = (r.get("severity") or "").lower()
            mep = r.get("mechanical_enforcement_path")
            if sev in ("critical", "high") and mep and not r.get("mandatory"):
                offenders.append((r["rule_id"], sev))
        assert offenders == [], (
            f"{len(offenders)} enforceable critical/high rule(s) left advisory: {offenders}"
        )
