"""1.3c: the TEACHES-direction convention, enforced.

Convention (decided from 22 live edges): "A TEACHES B" means the instructional
node A (Skill/Playbook/Technique) imparts the lesson/subject B. A Rule is a terse
enforced mandate -- it is what gets taught, never the teacher (0 of 22 edges
originate from a Rule). The 3 PBK-AUTHOR-001 TEACHES edges CONFORM (the playbook's
own steps direct the reader to apply the two meta-techniques, and AUTHOR -> TDD ->
rules mirrors the SKL-WORKTREE -> TEC-WORKTREE -> ENF-WORKTREE chain), so the
audit's "inverted" verdict is refuted and no edge is flipped.

detect_teaches_source_invariant pins the convention so a future mis-authored
Rule-sourced TEACHES fails validate (and a future audit cannot re-raise the
guess). The seed test is the detector's non-vacuity witness.
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
    # Sweep any seeded Rule-sourced TEACHES edge so it does not leak into the
    # shared graph. Ingest is upsert-only, so a non-clearing consumer (a later
    # `writ validate`, or sessionfinish's upsert re-ingest) would otherwise flag
    # the stale edge. A Rule-sourced TEACHES is exactly the forbidden shape these
    # tests assert, so deleting all of them on teardown is always safe.
    async with db._driver.session(database=db._database) as s:
        await (
            await s.run(
                "MATCH (a)-[r:TEACHES]->() WHERE a.rule_id = 'ENF-PROC-TDD-001' DELETE r"
            )
        ).consume()
    await db.close()


class TestTeachesConvention:
    @pytest.mark.asyncio
    async def test_clean_corpus_no_rule_sourced_teaches(self, db_corpus: Neo4jConnection) -> None:
        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
        violations = await checker.detect_teaches_source_invariant()
        assert violations == [], (
            f"a Rule is sourcing a TEACHES edge (rules are taught, not teachers): {violations}"
        )

    @pytest.mark.asyncio
    async def test_detects_rule_sourced_teaches(self, db_corpus: Neo4jConnection) -> None:
        # Non-vacuity: a Rule sourcing a TEACHES edge is flagged.
        await db_corpus.create_edge("TEACHES", "ENF-PROC-TDD-001", "SKL-PROC-PLAN-001")
        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
        violations = await checker.detect_teaches_source_invariant()
        assert any(
            v["src"] == "ENF-PROC-TDD-001" and v["tgt"] == "SKL-PROC-PLAN-001"
            for v in violations
        )

    @pytest.mark.asyncio
    async def test_rule_sourced_teaches_fails_run_all_checks(self, db_corpus: Neo4jConnection) -> None:
        # 5.2c coverage-map row 7: promote the detector-level seed to the full
        # seed -> run_all_checks() -> exit_code==1 standard (teaches_source routes into
        # has_issues at integrity.py). The next fixture's clear_all+re-ingest sweeps the seed.
        await db_corpus.create_edge("TEACHES", "ENF-PROC-TDD-001", "SKL-PROC-PLAN-001")
        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
        findings = await checker.run_all_checks(bible_dir=BIBLE, skip_redundancy=True)
        assert findings["exit_code"] == 1
        assert findings["teaches_source"]

    @pytest.mark.asyncio
    async def test_pbk_author_edges_conform(self, db_corpus: Neo4jConnection) -> None:
        # The audit-refutation as a regression: the 3 PBK-AUTHOR-001 TEACHES edges
        # are Playbook-sourced (instructional), so none are convention violations.
        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
        violations = await checker.detect_teaches_source_invariant()
        srcs = {v["src"] for v in violations}
        assert "PBK-AUTHOR-001" not in srcs
