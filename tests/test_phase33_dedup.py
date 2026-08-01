"""Phase 3.3: dedup + contradictions -- forbidden-phrase overlap, shared code
examples, and cluster cross-links.

RED-FIRST. Three things, all verified on live disk:
  - detect_forbidden_phrase_overlap: a phrase in >1 ForbiddenResponse node.
    Live: 4 success-claim phrases shared by FRB-COMMS-001 (performative agreement)
    and FRB-COMMS-002 (unverified success claims). Fix = remove the 4 from 001.
  - detect_shared_code_example: a verbatim (normalized) fenced block in >1 rule.
    Live: PERF-QUERY-004 and SEC-RATE-QUERY-001 share the same /api/orders
    violation. Fix = differentiate SEC-RATE-QUERY-001 (its concern is user-input
    queries). This is the dedup-gate-gap closer (cosine-0.95 misses these).
  - Cluster cross-links (RELATED_TO via "Related rules:" mentions; the merge-or-keep
    human calls resolved KEEP+cross-link): the pagination triad API-PAGINATION-001 /
    PERF-QUERY-004 / SEC-RATE-QUERY-001, CLEAN-COUPLING-001 <-> SOLID-DIP-001,
    SEC-DATA-PII-002 <-> SEC-UNI-003, ENF-PROC-TDD-001 <-> ENF-GATE-007.

Each test isolated (TEST-ISO-001).
"""

from __future__ import annotations

from collections import defaultdict
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
from writ.graph.integrity import IntegrityChecker, _normalized_code_blocks

from tests._bible_guard import requires_bible

pytestmark = requires_bible


NEO4J_URI = get_neo4j_uri()
NEO4J_USER = get_neo4j_user()
NEO4J_PASSWORD = get_neo4j_password()

BIBLE_DIR = Path(__file__).resolve().parent.parent / "bible"

CROSS_LINK_PAIRS = [
    ("API-PAGINATION-001", "PERF-QUERY-004"),
    ("API-PAGINATION-001", "SEC-RATE-QUERY-001"),
    ("PERF-QUERY-004", "SEC-RATE-QUERY-001"),
    ("CLEAN-COUPLING-001", "SOLID-DIP-001"),
    ("SEC-DATA-PII-002", "SEC-UNI-003"),
    ("ENF-PROC-TDD-001", "ENF-GATE-007"),
]


def _make_rule(rule_id: str, violation: str = "Bad.", pass_example: str = "Good.") -> dict:
    return {
        "rule_id": rule_id, "domain": "security", "severity": "medium",
        "scope": "file", "trigger": "t.", "statement": "s.", "violation": violation,
        "pass_example": pass_example, "enforcement": "e.", "rationale": "r.",
        "mandatory": False, "confidence": "production-validated",
        "evidence": "doc:original-bible", "staleness_window": 365,
        "last_validated": date.today().isoformat(),
    }


def _fence(code: str, lang: str = "python") -> str:
    return f"```{lang}\n{code}\n```"


def _xref_map() -> dict[str, set]:
    """{rule_id: set(cross-referenced ids)} across BOTH rule formats."""
    out: dict[str, set] = {}
    for f in discover_rule_files(BIBLE_DIR):
        if "methodology" in f.parts:
            for n in parse_nodes_from_file(f):
                if "rule_id" in n:
                    out[n["rule_id"]] = set(n.get("_cross_references") or [])
        else:
            for r in parse_rules_from_file(f):
                out[r["rule_id"]] = set(r.get("_cross_references") or [])
    return out


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


class TestForbiddenPhraseOverlap:
    @pytest.mark.asyncio
    async def test_shared_phrase_flagged(self, db, checker) -> None:
        await db.create_methodology_node("ForbiddenResponse",
            {"forbidden_id": "FRB-T-001", "forbidden_phrases": ["looks good to me", "great point"]})
        await db.create_methodology_node("ForbiddenResponse",
            {"forbidden_id": "FRB-T-002", "forbidden_phrases": ["looks good to me", "done!"]})
        result = await checker.detect_forbidden_phrase_overlap()
        assert result is not None
        assert any(d["phrase"] == "looks good to me" for d in result)

    @pytest.mark.asyncio
    async def test_distinct_phrases_clean(self, db, checker) -> None:
        await db.create_methodology_node("ForbiddenResponse",
            {"forbidden_id": "FRB-T-003", "forbidden_phrases": ["great point"]})
        await db.create_methodology_node("ForbiddenResponse",
            {"forbidden_id": "FRB-T-004", "forbidden_phrases": ["done!"]})
        assert await checker.detect_forbidden_phrase_overlap() is None


class TestSharedCodeExample:
    @pytest.mark.asyncio
    async def test_shared_block_flagged(self, db, checker) -> None:
        code = _fence("def handler():\n    return Order.query.all()  # unbounded result set here")
        await db.create_rule(_make_rule("SH-T-001", violation=code))
        await db.create_rule(_make_rule("SH-T-002", violation=code))
        result = await checker.detect_shared_code_example()
        assert result is not None
        assert any(set(d["rules"]) == {"SH-T-001", "SH-T-002"} for d in result)

    @pytest.mark.asyncio
    async def test_distinct_blocks_clean(self, db, checker) -> None:
        await db.create_rule(_make_rule("SH-T-003",
            violation=_fence("def a():\n    return X.query.all()  # one distinct example body")))
        await db.create_rule(_make_rule("SH-T-004",
            violation=_fence("def b():\n    return Y.objects.filter(z=1)  # a different example body")))
        assert await checker.detect_shared_code_example() is None

    @pytest.mark.asyncio
    async def test_short_shared_snippet_not_flagged(self, db, checker) -> None:
        # Below min_len -> trivial shared snippet, ignored.
        code = _fence("x = 1")
        await db.create_rule(_make_rule("SH-T-005", violation=code))
        await db.create_rule(_make_rule("SH-T-006", violation=code))
        assert await checker.detect_shared_code_example() is None


class TestWiring:
    @pytest.mark.asyncio
    async def test_both_in_findings_and_exit_code(self, db, checker) -> None:
        code = _fence("def handler():\n    return Order.query.all()  # unbounded result set here")
        await db.create_rule(_make_rule("SH-T-007", violation=code))
        await db.create_rule(_make_rule("SH-T-008", violation=code))
        findings = await checker.run_all_checks(skip_redundancy=True)
        assert "forbidden_phrase_overlap" in findings
        assert "shared_code_example" in findings
        assert findings["shared_code_example"]
        assert findings["exit_code"] == 1


class TestLiveCorpusClean:
    """Source-based RED witnesses; GREEN after the corpus fixes."""

    def test_no_forbidden_phrase_overlap(self) -> None:
        by_phrase: dict[str, set] = defaultdict(set)
        for f in (BIBLE_DIR / "methodology").glob("*.md"):
            for n in parse_nodes_from_file(f):
                for p in (n.get("forbidden_phrases") or []):
                    by_phrase[p.strip().lower()].add(n.get("forbidden_id"))
        overlaps = {p: sorted(ids) for p, ids in by_phrase.items() if len(ids) > 1}
        assert overlaps == {}, f"forbidden-phrase overlaps: {overlaps}"

    def test_no_shared_code_example(self) -> None:
        by_block: dict[str, set] = defaultdict(set)
        for f in discover_rule_files(BIBLE_DIR):
            if "methodology" in f.parts:
                continue
            for r in parse_rules_from_file(f):
                for fld in ("violation", "pass_example"):
                    for b in _normalized_code_blocks(r.get(fld)):
                        by_block[b].add(r["rule_id"])
        dups = {tuple(sorted(ids)) for ids in by_block.values() if len(ids) > 1}
        assert dups == set(), f"shared code blocks across rules: {dups}"

    def test_clusters_cross_linked(self) -> None:
        xref = _xref_map()
        missing = []
        for a, b in CROSS_LINK_PAIRS:
            linked = b in xref.get(a, set()) or a in xref.get(b, set())
            if not linked:
                missing.append((a, b))
        assert missing == [], f"missing cross-links: {missing}"
