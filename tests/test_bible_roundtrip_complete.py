"""F1: bible <-> Neo4j round-trip completeness invariant.

bible/ is meant to be the complete, human-readable source of truth: every edge in
the graph must be regenerable from bible (methodology `edges:` frontmatter +
rule-id cross-references in rule prose). The latent gap is the interactive
`writ add`/`edit` flow, which can create DEPENDS_ON/SUPPLEMENTS/CONFLICTS_WITH
edges that are NOT written back to bible -- those would be graph-only and lost on
a wipe + re-migrate.

This test locks the invariant: a MERGE re-import of bible (non-destructive) must
account for every edge in the graph. If the graph carries an edge bible does not
declare, edges_created (bible-derived) < the graph total, and this fails -- the
signal to add that edge to bible (see project_bible_roundtrip_completeness).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
from writ.graph.db import Neo4jConnection
from writ.graph.db._common import RECORD_LABELS
from writ.graph.methodology_ingest import ingest_path

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture()
def _require_neo4j() -> None:
    # Sync fixture: neo4j_reachable() calls asyncio.run internally, so it must run
    # OUTSIDE the async test's running event loop (calling it inside would raise and
    # mask a reachable DB as "unreachable", silently skipping the invariant).
    from tests._corpus import neo4j_reachable

    if not neo4j_reachable():
        pytest.skip("Neo4j unreachable")


@pytest.mark.asyncio
async def test_graph_has_no_edges_bible_cannot_regenerate(_require_neo4j) -> None:
    if not (REPO / "bible").exists():
        pytest.skip("requires the untracked bible/ source tree (regenerate with `writ export`)")
    db = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
    try:
        # Clear then import bible fresh so the comparison is deterministic (not
        # sensitive to edges other tests left on the shared graph). The import
        # restores the full corpus, so subsequent graph-dependent tests are
        # unaffected -- same pattern conftest uses at session start.
        await db.clear_all()
        report = await ingest_path(REPO / "bible", db, only=None, dry_run=False)
        # RUNTIME RECORD EDGES ARE EXCLUDED, because clear_all now preserves the record
        # nodes by default and bible is not their source. The claim under test is that
        # bible regenerates the CORPUS completely; a `(:Commit)-[:INCLUDES]->(:FileChange)`
        # edge surviving the clear is not a corpus edge bible failed to declare, and
        # counting it made this test read "round-trip incomplete: 1064 vs 1063" the moment
        # a single commit record existed. Scoped by the same RECORD_LABELS set clear_all
        # preserves, so the two cannot drift apart.
        async with db._driver.session(database=db._database) as session:
            result = await session.run(
                "MATCH (a)-[e]->(b) "
                "WHERE NOT any(l IN labels(a) WHERE l IN $records) "
                "  AND NOT any(l IN labels(b) WHERE l IN $records) "
                "RETURN count(e) AS c",
                records=sorted(RECORD_LABELS),
            )
            graph_total = (await result.single())["c"]

        assert graph_total == report.edges_created, (
            f"bible round-trip incomplete: graph has {graph_total} corpus edges but bible "
            f"regenerates {report.edges_created}. A mismatch means either a graph-only "
            f"edge (graph > bible; e.g. created via interactive add/edit and never "
            f"written back) or a duplicate edge declaration in bible (bible > graph). "
            f"Either way bible is no longer the complete source of truth -- reconcile it."
        )
    finally:
        await db.close()
