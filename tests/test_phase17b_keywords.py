"""1.7b: Invariant B -- trigger_keyword content-parity + pull-reachability.

(1) every trigger_keyword appears verbatim (whole word) in the node's own
    trigger/statement/body/tags; (2) a pull-routed methodology node with no
    floor_modes, no action_triggers, and no trigger_keywords is unreachable.
The seed tests are the non-vacuity witnesses for both clauses.
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
    # These tests mutate trigger_keywords on real nodes; upsert does not clear a
    # property absent from source, so fully rebuild from bible on teardown.
    await db.clear_all()
    await ingest_path(BIBLE, db)
    await db.close()


class TestTriggerKeywordInvariant:
    @pytest.mark.asyncio
    async def test_corpus_clean(self, db_corpus: Neo4jConnection) -> None:
        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
        result = await checker.detect_trigger_keyword_invariant()
        assert result is None, f"Invariant B violations: {result}"

    @pytest.mark.asyncio
    async def test_detects_content_parity_violation(self, db_corpus: Neo4jConnection) -> None:
        # Non-vacuity (parity): a keyword not present in the node's text is flagged.
        async with db_corpus._driver.session(database=db_corpus._database) as s:
            await s.run(
                "MATCH (n:Skill {skill_id:'SKL-PROC-WORKTREE-001'}) "
                "SET n.trigger_keywords = ['worktree', 'zzz-not-in-text']"
            )
        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
        result = await checker.detect_trigger_keyword_invariant()
        assert result is not None
        assert {"id": "SKL-PROC-WORKTREE-001", "keyword": "zzz-not-in-text"} in result["parity_violations"]

    @pytest.mark.asyncio
    async def test_detects_pull_orphan(self, db_corpus: Neo4jConnection) -> None:
        # Non-vacuity (reachability): a pull-routed node with no floor/action/keywords
        # is unreachable. SKL-PROC-WORKTREE-001 is pull-routed; strip its keywords AND
        # the action_triggers 1.8a authored (["worktree"]) so it has no surface at all.
        async with db_corpus._driver.session(database=db_corpus._database) as s:
            await s.run(
                "MATCH (n:Skill {skill_id:'SKL-PROC-WORKTREE-001'}) "
                "SET n.trigger_keywords = [], n.action_triggers = []"
            )
        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
        result = await checker.detect_trigger_keyword_invariant()
        assert result is not None
        assert "SKL-PROC-WORKTREE-001" in result["pull_orphans"]
