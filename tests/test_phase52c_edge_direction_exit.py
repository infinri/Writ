"""Phase 5.2c: edge-direction violations drive run_all_checks exit_code==1.

Item 4b: promote the dispatch/invokes detector tests from detector-level to the
full seed->run_all_checks->exit_code==1 standard required by the Phase 5 exit
criterion (blueprint 1004-1006).

tests/test_phase13b_invokes.py already verifies that detect_dispatch_invokes_invariant
reports the violations; these tests verify that run_all_checks routes those findings
into exit_code==1 (integrity.py:1339-1340 wiring lock).

GREEN-now: run_all_checks already routes dispatch_invokes into has_issues. These
tests lock that wiring and close the gap between detector-level and run_all_checks
coverage.

Seed IDs: PBK-PROC-SDD-001, TEC-PROC-WORKTREE-001, PBK-PROC-BRAIN-001, ROL-EXPLORER-001
-- verified to exist in bible/methodology/ at plan time (same seeds as test_phase13b_invokes.py).

Teardown: explicit sweep of the seeded edges before db.close() so the corpus is
restored for any subsequent test that shares the Neo4j instance.
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
    # Sweep seeded edges before close so corpus is clean for other sessions.
    async with db._driver.session(database=db._database) as s:
        await s.run(
            "MATCH (a)-[r:DISPATCHES|INVOKES]->(b) "
            "WHERE (a.playbook_id = 'PBK-PROC-SDD-001' AND b.technique_id = 'TEC-PROC-WORKTREE-001') "
            "   OR (a.playbook_id = 'PBK-PROC-BRAIN-001' AND b.role_id = 'ROL-EXPLORER-001') "
            "DELETE r"
        )
    await db.close()


class TestEdgeDirectionExitCode:
    """5.2c: each edge-direction violation drives exit_code==1 via run_all_checks."""

    @pytest.mark.asyncio
    async def test_dispatch_to_non_role_fails_run_all_checks(
        self, db_corpus: Neo4jConnection
    ) -> None:
        """A DISPATCHES edge targeting a non-SubagentRole node must drive exit_code==1.

        Seed: PBK-PROC-SDD-001 -> TEC-PROC-WORKTREE-001 via DISPATCHES.
        TEC-PROC-WORKTREE-001 is a Technique (not a SubagentRole) -- the invariant
        violation class test_phase13b_invokes tests at detector level.
        """
        await db_corpus.create_edge("DISPATCHES", "PBK-PROC-SDD-001", "TEC-PROC-WORKTREE-001")
        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
        findings = await checker.run_all_checks(bible_dir=BIBLE)
        assert findings["exit_code"] == 1, (
            f"exit_code should be 1 when dispatch_invokes is truthy; "
            f"got {findings.get('exit_code')}"
        )
        assert findings["dispatch_invokes"] is not None, (
            "findings['dispatch_invokes'] should be non-None after seeding "
            "DISPATCHES to a non-role node"
        )

    @pytest.mark.asyncio
    async def test_invokes_to_role_fails_run_all_checks(
        self, db_corpus: Neo4jConnection
    ) -> None:
        """An INVOKES edge targeting a SubagentRole node must drive exit_code==1.

        Seed: PBK-PROC-BRAIN-001 -> ROL-EXPLORER-001 via INVOKES.
        ROL-EXPLORER-001 is a SubagentRole -- an INVOKES to a role inverts the
        meaning and is the second violation class test_phase13b_invokes tests at
        detector level.
        """
        await db_corpus.create_edge("INVOKES", "PBK-PROC-BRAIN-001", "ROL-EXPLORER-001")
        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
        findings = await checker.run_all_checks(bible_dir=BIBLE)
        assert findings["exit_code"] == 1, (
            f"exit_code should be 1 when dispatch_invokes is truthy; "
            f"got {findings.get('exit_code')}"
        )
        assert findings["dispatch_invokes"] is not None, (
            "findings['dispatch_invokes'] should be non-None after seeding "
            "INVOKES to a SubagentRole node"
        )
