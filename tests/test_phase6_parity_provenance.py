"""Phase 6.4: the parity check is provenance-aware (unblocks 5.2).

The §5.2 / 0.10 parity invariant says "a methodology node in the graph must have a
markdown home." Self-authoring breaks that for legitimately graph-first nodes: a
`proposed` / `graduation_pending` candidate has NO markdown home BY DESIGN (it is
transient, pre-promotion). So the parity check must:

- EXEMPT provenance in {proposed, graduation_pending} (graph-first, not yet canon), and
- REQUIRE provenance in {graduated, hand-authored} to have a source home -- a graduated
  node with no .md means graduation failed to close the export loop (real drift, flag).

detect_parity_violations (node-presence) is the locus: today it flags EVERY graph node
absent from markdown, so it would false-flag every proposed node forever. Adding the
provenance exemption there satisfies both clauses at once. detect_prop_parity /
detect_edge_parity re-point their graph-authored exemption to provenance for one
authoritative axis.

RED until 6.4 lands. Verified safe: all 399 live corpus nodes are in the oracle, so the
source-home requirement raises 0 false positives on the hand-authored corpus.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
from writ.graph.db import Neo4jConnection
from writ.graph.integrity import IntegrityChecker
from writ.graph.methodology_ingest import ingest_path

BIBLE = Path(__file__).resolve().parent.parent / "bible"

PROPOSED = "ZZZ-PROPOSED-PARITY-001"      # graph-first, no .md -> EXEMPT
GRADUATED = "ZZZ-GRADUATED-PARITY-001"    # graduated, no .md -> FLAG (loop failed)
HAND = "ZZZ-HANDAUTH-PARITY-001"          # hand-authored, no .md -> FLAG (drift)
SEEDS = [PROPOSED, GRADUATED, HAND]


def _rule(rid: str) -> dict:
    return {
        "rule_id": rid, "domain": "Testing", "severity": "high", "scope": "slice",
        "trigger": "t", "statement": "s", "violation": "v", "pass_example": "p",
        "enforcement": "e", "rationale": "r", "last_validated": "2026-03-15",
    }


@pytest_asyncio.fixture()
async def db_corpus():
    db = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
    try:
        async with db._driver.session(database=db._database) as s:
            await (await s.run("RETURN 1 AS ok")).consume()
    except Exception:
        await db.close()
        pytest.skip("Neo4j unreachable")
    if not BIBLE.exists():
        await db.close()
        pytest.skip("requires the untracked bible/ source tree (regenerate with `writ export`)")
    await db.clear_all()
    await ingest_path(BIBLE, db)
    yield db
    async with db._driver.session(database=db._database) as s:
        await s.run("MATCH (n) WHERE n.rule_id IN $ids DETACH DELETE n", ids=SEEDS)
    await db.close()


async def _seed_proposed(db: Neo4jConnection) -> None:
    await db.create_rule(_rule(PROPOSED), source_origin="graph-authored")  # -> proposed


async def _seed_graduated(db: Neo4jConnection) -> None:
    # graduated but never exported to a .md (the loop-failure case 6.4 must catch).
    await db.create_rule({**_rule(GRADUATED), "provenance": "graduated"})


async def _seed_hand_authored_orphan(db: Neo4jConnection) -> None:
    await db.create_rule(_rule(HAND))  # ingest default -> hand-authored, but no .md


class TestNodePresenceParityIsProvenanceAware:
    @pytest.mark.asyncio
    async def test_proposed_node_not_flagged(self, db_corpus: Neo4jConnection) -> None:
        await _seed_proposed(db_corpus)
        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
        violations = await checker.detect_parity_violations(BIBLE)
        ids = {v["id"] for v in violations}
        assert PROPOSED not in ids, "a proposed candidate has no markdown home by design"

    @pytest.mark.asyncio
    async def test_graduated_node_without_source_flagged(self, db_corpus: Neo4jConnection) -> None:
        await _seed_graduated(db_corpus)
        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
        violations = await checker.detect_parity_violations(BIBLE)
        ids = {v["id"] for v in violations}
        assert GRADUATED in ids, "a graduated node with no .md = graduation failed to close the loop"

    @pytest.mark.asyncio
    async def test_hand_authored_orphan_flagged(self, db_corpus: Neo4jConnection) -> None:
        await _seed_hand_authored_orphan(db_corpus)
        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
        violations = await checker.detect_parity_violations(BIBLE)
        ids = {v["id"] for v in violations}
        assert HAND in ids, "a hand-authored node absent from every .md is real drift"

    @pytest.mark.asyncio
    async def test_clean_corpus_still_parity_clean(self, db_corpus: Neo4jConnection) -> None:
        # No seeds: the exemption must not mask real corpus drift (regression guard).
        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
        violations = await checker.detect_parity_violations(BIBLE)
        assert violations == [], f"clean corpus should be parity-clean, got {violations}"


class TestFieldAndEdgeParityExemptProposed:
    @pytest.mark.asyncio
    async def test_prop_parity_exempts_proposed(self, db_corpus: Neo4jConnection) -> None:
        # A proposed node carrying a managed prop absent from any source must NOT be
        # flagged as stale-prop drift (it's graph-first, exempt).
        await db_corpus.create_rule(
            {**_rule(PROPOSED), "always_on": True}, source_origin="graph-authored"
        )
        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
        prop_viol = await checker.detect_prop_parity(BIBLE)
        assert PROPOSED not in (prop_viol or {}), "proposed nodes are field-parity exempt"

    @pytest.mark.asyncio
    async def test_edge_parity_exempts_proposed_endpoint(self, db_corpus: Neo4jConnection) -> None:
        # An edge incident to a proposed node is graph-only by design -> exempt.
        await _seed_proposed(db_corpus)
        await db_corpus.create_edge("RELATED_TO", PROPOSED, "SEC-INJ-SQL-001")
        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
        edge_viol = await checker.detect_edge_parity(BIBLE)
        stale = {(t, s, tg) for (t, s, tg) in (edge_viol or {}).get("stale", [])}
        assert not any(s == PROPOSED for (_t, s, _tg) in stale), (
            "edges incident to a proposed node are exempt"
        )
