"""1.3b: INVOKES edge type + the dispatch/invokes one-level graph invariant.

DISPATCHES means "spawn a SubagentRole"; INVOKES means "the orchestrator applies
this methodology inline (one level)". 10 DISPATCHES edges currently mis-target
non-SubagentRole methodology (Playbooks/Skills/Techniques) -- they should be
INVOKES. detect_dispatch_invokes_invariant makes the one-level constraint a graph
invariant: a DISPATCHES to a non-role, or an INVOKES to a role, fails validate.

The clean-corpus test is RED until the 10 source edges are remapped
DISPATCHES->INVOKES; the seed tests prove the detector is non-vacuous either way.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
from writ.graph.db import ALLOWED_EDGE_TYPES, Neo4jConnection
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
    await db.close()


def test_invokes_is_allowed_edge_type() -> None:
    assert "INVOKES" in ALLOWED_EDGE_TYPES


class TestDispatchInvokesInvariant:
    @pytest.mark.asyncio
    async def test_clean_corpus_has_no_violations(self, db_corpus: Neo4jConnection) -> None:
        # RED until the 10 mis-targeted DISPATCHES edges are remapped to INVOKES
        # in the bible source; GREEN after.
        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
        result = await checker.detect_dispatch_invokes_invariant()
        assert result is None, (
            f"dispatch/invokes invariant violated: "
            f"{result['dispatch_to_non_role'] if result else []}"
        )

    @pytest.mark.asyncio
    async def test_detects_dispatch_to_non_role(self, db_corpus: Neo4jConnection) -> None:
        # Non-vacuity: a DISPATCHES to a non-SubagentRole is flagged.
        await db_corpus.create_edge("DISPATCHES", "PBK-PROC-SDD-001", "TEC-PROC-WORKTREE-001")
        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
        result = await checker.detect_dispatch_invokes_invariant()
        assert result is not None
        assert any(
            src == "PBK-PROC-SDD-001" and tgt == "TEC-PROC-WORKTREE-001"
            for (src, tgt, _label) in result["dispatch_to_non_role"]
        )

    @pytest.mark.asyncio
    async def test_detects_invokes_to_role(self, db_corpus: Neo4jConnection) -> None:
        # Non-vacuity: an INVOKES to a SubagentRole inverts the meaning -> flagged.
        await db_corpus.create_edge("INVOKES", "PBK-PROC-BRAIN-001", "ROL-EXPLORER-001")
        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
        result = await checker.detect_dispatch_invokes_invariant()
        assert result is not None
        assert any(
            src == "PBK-PROC-BRAIN-001" and tgt == "ROL-EXPLORER-001"
            for (src, tgt, _label) in result["invokes_to_role"]
        )

    @pytest.mark.asyncio
    async def test_real_role_dispatches_are_not_flagged(self, db_corpus: Neo4jConnection) -> None:
        # The 8 DISPATCHES -> ROL-* edges must NOT be reported.
        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
        result = await checker.detect_dispatch_invokes_invariant()
        flagged = {(s, t) for (s, t, _l) in (result["dispatch_to_non_role"] if result else [])}
        assert ("PBK-PROC-ORCHESTRATOR-001", "ROL-PLANNER-001") not in flagged
