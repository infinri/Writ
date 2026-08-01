"""1.7a: Invariant A -- node-declared floor_modes must match Appendix B per mode.

floor_modes (on each node's front-matter) is the RUNTIME source; EXPECTED_FLOORS
(the Appendix-B fixture in integrity.py) is the central completeness check a
missing tag would not otherwise trip. Exact set equality per mode: a missing
member (floor node that lost its tag) AND a spurious member (node wrongly tagged)
both fail. The seed tests are the non-vacuity witnesses (both directions).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
from writ.graph.db import Neo4jConnection
from writ.graph.integrity import IntegrityChecker
from writ.graph.methodology_ingest import ingest_path

from tests._bible_guard import requires_bible

pytestmark = requires_bible


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
    await db.clear_all()
    await ingest_path(BIBLE, db)
    yield db
    # Restore a clean corpus: these tests SET floor_modes on real nodes, and
    # upsert (SET n += props) does NOT clear a property absent from source, so a
    # mutation would leak. clear_all + re-ingest fully rebuilds from bible.
    await db.clear_all()
    await ingest_path(BIBLE, db)
    await db.close()


class TestFloorCompleteness:
    @pytest.mark.asyncio
    async def test_floors_match_appendix_b(self, db_corpus: Neo4jConnection) -> None:
        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
        result = await checker.detect_floor_completeness()
        assert result is None, f"floor drift vs Appendix B: {result}"

    @pytest.mark.asyncio
    async def test_detects_missing_floor_member(self, db_corpus: Neo4jConnection) -> None:
        # Non-vacuity (missing): strip a universal floor node's tags -> it drops
        # out of all 5 mode floors and must be flagged missing.
        async with db_corpus._driver.session(database=db_corpus._database) as s:
            await s.run("MATCH (n:Skill {skill_id:'SKL-PROC-BRAIN-001'}) SET n.floor_modes = []")
        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
        result = await checker.detect_floor_completeness()
        assert result is not None
        assert "SKL-PROC-BRAIN-001" in result["work"]["missing"]
        assert "SKL-PROC-BRAIN-001" in result["debug"]["missing"]

    @pytest.mark.asyncio
    async def test_detects_spurious_floor_member(self, db_corpus: Neo4jConnection) -> None:
        # Non-vacuity (spurious): tag a non-floor node into the work floor.
        async with db_corpus._driver.session(database=db_corpus._database) as s:
            await s.run(
                "MATCH (n:Technique {technique_id:'TEC-PROC-WORKTREE-001'}) "
                "SET n.floor_modes = ['work']"
            )
        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
        result = await checker.detect_floor_completeness()
        assert result is not None
        assert "TEC-PROC-WORKTREE-001" in result["work"]["spurious"]
