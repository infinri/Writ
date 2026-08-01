"""Phase 5.2a: bidirectional field-level methodology parity (KEYSTONE).

IntegrityChecker.detect_methodology_field_drift compares every managed field value
of each hand-authored/graduated methodology node against its bible/methodology/<id>.md
frontmatter. Catches VALUE-drift (graph says severity='low', source says 'high') and
MISSING-prop drift (source declares a field the graph node lacks). Provenance-aware:
proposed/graduation_pending nodes are exempt (graph-first, no markdown home yet).

RED until detect_methodology_field_drift is added to IntegrityChecker (Item 2). The
method is called INSIDE each test body (not at import time) so collection succeeds and
the test fails at call time with AttributeError -- the cleaner RED form per plan §1c.

Seed strategy: mutate an existing hand-authored methodology node's graph prop via raw
Cypher after a clean ingest, assert the detector reports the drift, then the fixture
re-ingests on teardown to restore corpus state.

Real node used: SKL-PROC-BRAIN-001 (skill_id, severity=high, exists on disk as
bible/methodology/SKL-PROC-BRAIN-001.md -- verified at plan time).
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

# Real hand-authored methodology node verified to exist in bible/methodology/ at plan time.
REAL_METH_ID = "SKL-PROC-BRAIN-001"
# The real severity value in bible/methodology/SKL-PROC-BRAIN-001.md at plan time.
REAL_SEVERITY_SOURCE = "high"

# Seed ids for nodes created fresh in individual tests.
SEED_PROPOSED = "ZZZ-PROPOSED-FIELDDRIFT-001"
SEEDS = [SEED_PROPOSED]


def _meth_node(nid: str) -> dict:
    """Minimal methodology node dict for seeding proposed nodes."""
    return {
        "skill_id": nid,
        "node_type": "Skill",
        "domain": "testing",
        "severity": "high",
        "scope": "session",
        "trigger": "seed trigger",
        "statement": "seed statement",
        "rationale": "seed rationale",
        "confidence": "speculative",
        "authority": "human",
        "last_validated": "2026-06-16",
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
    await db.clear_all()
    await ingest_path(BIBLE, db)
    yield db
    # Sweep fresh seeds; the next fixture call re-ingests so any mutation is wiped.
    async with db._driver.session(database=db._database) as s:
        await s.run(
            "MATCH (n) WHERE n.skill_id IN $ids DETACH DELETE n", ids=SEEDS
        )
    await db.close()


class TestMethodologyFieldDrift:
    """5.2a: detect_methodology_field_drift -- RED until the method is added."""

    @pytest.mark.asyncio
    async def test_flags_value_drift(self, db_corpus: Neo4jConnection) -> None:
        """After directly mutating a managed prop on a real graph node, the detector
        reports that node_id with a VALUE drift entry for the mutated field."""
        # Verify the real node survived ingest (non-vacuity precondition).
        async with db_corpus._driver.session(database=db_corpus._database) as s:
            res = await s.run(
                "MATCH (n) WHERE n.skill_id = $sid RETURN n.severity AS sev",
                sid=REAL_METH_ID,
            )
            row = await res.single()
        assert row is not None, (
            f"{REAL_METH_ID} not found in graph after ingest -- corpus may have drifted"
        )

        # Mutate severity in the graph only (source still says 'high').
        async with db_corpus._driver.session(database=db_corpus._database) as s:
            await s.run(
                "MATCH (n) WHERE n.skill_id = $sid SET n.severity = 'low'",
                sid=REAL_METH_ID,
            )

        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
        # RED: detect_methodology_field_drift does not exist yet -> AttributeError.
        result = await checker.detect_methodology_field_drift(BIBLE)

        assert result is not None, (
            f"Expected drift detected for {REAL_METH_ID} but got None"
        )
        assert REAL_METH_ID in result, (
            f"{REAL_METH_ID} not in drift result; got keys: {list(result)}"
        )
        drift_for_node = result[REAL_METH_ID]
        assert "severity" in drift_for_node, (
            f"Expected 'severity' drift entry; got fields: {list(drift_for_node)}"
        )
        severity_drift = drift_for_node["severity"]
        assert severity_drift["graph"] == "low", (
            f"graph value should be 'low'; got {severity_drift['graph']!r}"
        )
        assert severity_drift["source"] == REAL_SEVERITY_SOURCE, (
            f"source value should be {REAL_SEVERITY_SOURCE!r}; "
            f"got {severity_drift['source']!r}"
        )

    @pytest.mark.asyncio
    async def test_flags_missing_prop(self, db_corpus: Neo4jConnection) -> None:
        """REMOVE a managed prop from a real node; detect_methodology_field_drift
        must report it as a MISSING drift (source has it, graph lacks it)."""
        # Confirm node present.
        async with db_corpus._driver.session(database=db_corpus._database) as s:
            res = await s.run(
                "MATCH (n) WHERE n.skill_id = $sid RETURN n.severity AS sev",
                sid=REAL_METH_ID,
            )
            row = await res.single()
        assert row is not None, f"{REAL_METH_ID} not found after ingest"

        # Remove severity from the graph node.
        async with db_corpus._driver.session(database=db_corpus._database) as s:
            await s.run(
                "MATCH (n) WHERE n.skill_id = $sid REMOVE n.severity",
                sid=REAL_METH_ID,
            )

        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
        # RED: method does not exist yet.
        result = await checker.detect_methodology_field_drift(BIBLE)

        assert result is not None
        assert REAL_METH_ID in result, (
            f"{REAL_METH_ID} not in drift result; got keys: {list(result)}"
        )
        drift_for_node = result[REAL_METH_ID]
        assert "severity" in drift_for_node, (
            f"Expected missing 'severity' entry; got fields: {list(drift_for_node)}"
        )
        # A missing-prop drift: graph side should signal absence (e.g. None / sentinel).
        severity_drift = drift_for_node["severity"]
        # The graph value must reflect absence; the source value must be the real value.
        assert severity_drift.get("source") == REAL_SEVERITY_SOURCE, (
            f"source value should be {REAL_SEVERITY_SOURCE!r}; got {severity_drift!r}"
        )

    @pytest.mark.asyncio
    async def test_flags_source_null_graph_nonnull(self, db_corpus: Neo4jConnection) -> None:
        """Source declares a managed field as null (the writer drops null props, so a
        clean ingest leaves it ABSENT in the graph). A manual graph edit that SETS a
        non-null value on that field IS value drift -- the source-null/graph-non-null
        quadrant the None-skip must not mask (blueprint: field-by-field equality)."""
        # Precondition: SKL-PROC-BRAIN-001 declares `source_commit: null`, so after a
        # clean ingest the graph node has no source_commit property.
        async with db_corpus._driver.session(database=db_corpus._database) as s:
            res = await s.run(
                "MATCH (n) WHERE n.skill_id = $sid RETURN n.source_commit AS sc",
                sid=REAL_METH_ID,
            )
            row = await res.single()
        assert row is not None, f"{REAL_METH_ID} not found after ingest"
        assert row["sc"] is None, (
            "precondition: a null-declared field must be absent in the graph after "
            f"clean ingest; got {row['sc']!r}"
        )

        # The attack vector: a graph edit not reflected in source.
        async with db_corpus._driver.session(database=db_corpus._database) as s:
            await s.run(
                "MATCH (n) WHERE n.skill_id = $sid SET n.source_commit = 'fake-sha'",
                sid=REAL_METH_ID,
            )

        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
        result = await checker.detect_methodology_field_drift(BIBLE)

        assert result is not None and REAL_METH_ID in result, (
            "source-null/graph-non-null must be reported as value drift, not skipped; "
            f"got {result!r}"
        )
        assert "source_commit" in result[REAL_METH_ID], (
            f"expected source_commit drift; got {list(result[REAL_METH_ID])}"
        )
        sc_drift = result[REAL_METH_ID]["source_commit"]
        assert sc_drift["graph"] == "fake-sha", (
            f"graph value should be 'fake-sha'; got {sc_drift['graph']!r}"
        )
        assert sc_drift["source"] is None, (
            f"source value should be None; got {sc_drift['source']!r}"
        )

    @pytest.mark.asyncio
    async def test_exempts_proposed(self, db_corpus: Neo4jConnection) -> None:
        """A proposed (graph-first) node carrying a managed field with an arbitrary
        value must NOT appear in the drift result (GRAPH_FIRST exemption)."""
        # Seed a proposed methodology node with a divergent severity value.
        await db_corpus.create_methodology_node(
            "Skill",
            {**_meth_node(SEED_PROPOSED), "severity": "critical"},
            source_origin="graph-authored",
        )

        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
        # RED: method does not exist yet.
        result = await checker.detect_methodology_field_drift(BIBLE)

        # The proposed node must not be flagged.
        if result is not None:
            assert SEED_PROPOSED not in result, (
                f"proposed node {SEED_PROPOSED} must be exempt from field-drift check"
            )

    @pytest.mark.asyncio
    async def test_clean_corpus_has_no_field_drift(self, db_corpus: Neo4jConnection) -> None:
        """No seeds: the live corpus (after clean ingest) must be value-clean.

        This is a regression guard -- the exemption must not mask real corpus drift,
        and the hand-authored corpus must already be value-clean post-ingest."""
        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
        # RED: method does not exist yet.
        result = await checker.detect_methodology_field_drift(BIBLE)
        assert result is None, (
            f"Clean corpus must have no field drift; got: {result}"
        )

    @pytest.mark.asyncio
    async def test_run_all_checks_flags_field_drift(self, db_corpus: Neo4jConnection) -> None:
        """Seed value drift, call run_all_checks, assert exit_code==1 and
        findings['methodology_field_drift'] is truthy.

        This is both the item-2 exit-code coverage and the item-4 coverage-map
        entry (#3) for the new finding class."""
        # Mutate a managed prop.
        async with db_corpus._driver.session(database=db_corpus._database) as s:
            await s.run(
                "MATCH (n) WHERE n.skill_id = $sid SET n.severity = 'low'",
                sid=REAL_METH_ID,
            )

        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
        # RED: run_all_checks does not include methodology_field_drift yet.
        findings = await checker.run_all_checks(bible_dir=BIBLE)

        assert findings["exit_code"] == 1, (
            f"exit_code should be 1 when methodology_field_drift is truthy; "
            f"got {findings.get('exit_code')}"
        )
        assert findings.get("methodology_field_drift"), (
            "findings['methodology_field_drift'] should be truthy after seeding value drift"
        )
