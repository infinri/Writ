"""Regression guards for defects the Phase 6 adversarial loop review confirmed.

- #2 (HIGH): export_node_to_source must query a node's outgoing edges under the node's
  OWN project (it stripped `project` before reading it -> always 'writ', losing edges for
  non-writ projects on the export round-trip).
- #3 (MEDIUM): reconcile must EXEMPT a graduation_pending node from deletion via the
  provenance axis (a candidate has no markdown home by design; reconcile must not prune it).

(#1 -- the concurrent false-positive in evaluate_and_flip_graduation -- is fixed by checking
the write counter; a true interleaving is not deterministically reproducible single-threaded,
so it is covered by the counter-check + the sequential idempotency test in
test_phase6_graduation.py rather than re-raced here.)
"""
from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
from writ.graph.db import Neo4jConnection
from writ.graph.methodology_ingest import ingest_path, reconcile

from tests._bible_guard import requires_bible

pytestmark = requires_bible


BIBLE = Path(__file__).resolve().parent.parent / "bible"


def _rule(rid: str, project: str = "writ") -> dict:
    return {
        "rule_id": rid, "domain": "Testing", "severity": "high", "scope": "slice",
        "trigger": "t", "statement": "s", "violation": "v", "pass_example": "p",
        "enforcement": "e", "rationale": "r", "last_validated": "2026-03-15",
        "project": project,
    }


class TestExportProjectScope:
    SRC = "RFX-PROJ-SRC-001"
    TGT = "RFX-PROJ-TGT-001"
    PROJECT = "proj2"

    @pytest_asyncio.fixture()
    async def db(self):
        conn = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
        try:
            async with conn._driver.session(database=conn._database) as s:
                await (await s.run("RETURN 1 AS ok")).consume()
        except Exception:
            await conn.close()
            pytest.skip("Neo4j unreachable")
        ids = [self.SRC, self.TGT]
        async with conn._driver.session(database=conn._database) as s:
            await s.run("MATCH (n:Rule) WHERE n.rule_id IN $ids DETACH DELETE n", ids=ids)
        yield conn
        async with conn._driver.session(database=conn._database) as s:
            await s.run("MATCH (n:Rule) WHERE n.rule_id IN $ids DETACH DELETE n", ids=ids)
        await conn.close()

    @pytest.mark.asyncio
    async def test_export_uses_nodes_own_project_for_edges(self, db: Neo4jConnection, tmp_path) -> None:
        from writ.promotion import export_node_to_source
        # A node + edge in a NON-writ project. If export defaults the project to 'writ',
        # the edge query finds nothing and the edge is lost from the exported source.
        await db.create_rule(_rule(self.SRC, self.PROJECT))
        await db.create_rule(_rule(self.TGT, self.PROJECT))
        await db.create_edge("RELATED_TO", self.SRC, self.TGT, project=self.PROJECT)
        path = await export_node_to_source(db, self.SRC, tmp_path)
        md = path.read_text(encoding="utf-8")
        assert self.TGT in md, "edge in the node's own project must survive the export"


class TestReconcileExemptsGraduationPending:
    PENDING = "RFX-PENDING-RECON-001"

    @pytest_asyncio.fixture()
    async def db_corpus(self):
        db = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
        try:
            async with db._driver.session(database=db._database) as s:
                await (await s.run("RETURN 1 AS ok")).consume()
        except Exception:
            await db.close()
            pytest.skip("Neo4j unreachable")
        await db.clear_all()
        await ingest_path(BIBLE, db)
        yield db
        async with db._driver.session(database=db._database) as s:
            await s.run("MATCH (n:Rule {rule_id: $id}) DETACH DELETE n", id=self.PENDING)
        await db.close()

    @pytest.mark.asyncio
    async def test_graduation_pending_survives_reconcile(self, db_corpus: Neo4jConnection) -> None:
        # A graduation_pending candidate is absent from bible/ by design; reconcile (which
        # prunes graph nodes absent from the oracle) must EXEMPT it via the provenance axis.
        await db_corpus.create_rule(_rule(self.PENDING), source_origin="graph-authored")
        async with db_corpus._driver.session(database=db_corpus._database) as s:
            await s.run(
                "MATCH (r:Rule {rule_id:$id}) SET r.provenance='graduation_pending'",
                id=self.PENDING,
            )
        result = await reconcile(BIBLE, db_corpus)
        assert self.PENDING not in result["deleted_nodes"]
        async with db_corpus._driver.session(database=db_corpus._database) as s:
            r = await s.run("MATCH (r:Rule {rule_id:$id}) RETURN r.provenance AS p", id=self.PENDING)
            rec = await r.single()
        assert rec is not None and rec["p"] == "graduation_pending"
