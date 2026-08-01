"""Validate: freshness guard on bible/abstractions.json.

IntegrityChecker gains a new detector `detect_artifact_dangling_rule_ids` that
reads bible/abstractions.json and flags any `rule_ids` entry that references a
Rule id absent from the graph (a dangling reference).

The new finding key is `artifact_dangling_rule_ids` and causes exit_code=1.

RED today because:
  - detect_artifact_dangling_rule_ids does not exist on IntegrityChecker.
  - run_all_checks does not include the new finding key.

Model: mirrors test_phase52a_field_drift.py: db_corpus fixture does a clean
ingest, tests seed a minimal artifact file referencing a bogus rule_id, then
call the detector (or run_all_checks) and assert the finding is truthy.

Artifact path assumption
-------------------------
The detector is expected to accept an `artifact_path: Path | None = None`
parameter. When None it defaults to the bible root's abstractions.json.
Tests always supply an explicit tmp artifact path so they never read or
mutate the committed file.
"""

from __future__ import annotations

import json
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

# A rule_id that will never appear in the corpus.
BOGUS_RULE_ID = "ZZZ-DANGLING-001"

# A rule_id that IS present in the corpus (methodology dual-location rule).
REAL_RULE_ID = "ENF-COMMS-001"


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture()
async def db_corpus():
    """Live Neo4j with the full corpus ingested. Clears abstractions on teardown."""
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
    await db.delete_abstractions(project="writ")
    await db.close()


# ---------------------------------------------------------------------------
# Artifact builder helpers (pure)
# ---------------------------------------------------------------------------

def _write_artifact(path: Path, abstractions: list[dict]) -> None:
    """Write a minimal artifact file to `path`."""
    data = {"project": "writ", "abstractions": abstractions}
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestArtifactDanglingRuleIdsDetector:
    """detect_artifact_dangling_rule_ids -- RED until the method is added."""

    @pytest.mark.asyncio
    async def test_flags_dangling_rule_id(
        self, db_corpus: Neo4jConnection, tmp_path: Path
    ) -> None:
        """An artifact whose rule_ids list contains a rule id absent from the
        graph is flagged as a dangling reference.

        The fixture artifact has one abstraction with two rule_ids: one that
        resolves (REAL_RULE_ID) and one that does not (BOGUS_RULE_ID). The
        detector must flag the bogus id.
        """
        artifact_path = tmp_path / "abstractions.json"
        _write_artifact(artifact_path, [
            {
                "abstraction_id": "ABS-VALIDATE-TEST-000",
                "summary": "Test abstraction with a dangling rule ref",
                "rule_ids": [REAL_RULE_ID, BOGUS_RULE_ID],
                "compression_ratio": 2.0,
            },
        ])

        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)

        # RED: detect_artifact_dangling_rule_ids does not exist yet -> AttributeError.
        result = await checker.detect_artifact_dangling_rule_ids(
            artifact_path=artifact_path
        )

        assert result is not None, (
            "Expected a truthy result when the artifact contains a dangling rule_id; "
            "got None"
        )
        assert isinstance(result, list), (
            f"Expected a list of dangling entries; got {type(result).__name__}"
        )
        assert len(result) > 0, (
            f"Expected at least one dangling finding; got empty list"
        )

        # The bogus id must appear in the findings.
        dangling_ids = {
            entry.get("rule_id") or entry if isinstance(entry, str) else entry.get("rule_id")
            for entry in result
        }
        assert BOGUS_RULE_ID in dangling_ids, (
            f"{BOGUS_RULE_ID!r} not in dangling findings; got {dangling_ids}"
        )

    @pytest.mark.asyncio
    async def test_clean_artifact_returns_empty_or_none(
        self, db_corpus: Neo4jConnection, tmp_path: Path
    ) -> None:
        """An artifact whose rule_ids all resolve against the graph yields no
        findings (returns None or empty list).
        """
        artifact_path = tmp_path / "abstractions.json"
        _write_artifact(artifact_path, [
            {
                "abstraction_id": "ABS-VALIDATE-CLEAN-000",
                "summary": "Clean artifact with only known rule ids",
                "rule_ids": [REAL_RULE_ID],
                "compression_ratio": 1.0,
            },
        ])

        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)

        # RED: method does not exist yet.
        result = await checker.detect_artifact_dangling_rule_ids(
            artifact_path=artifact_path
        )

        assert not result, (
            f"Expected no dangling findings for a clean artifact; got {result!r}"
        )

    @pytest.mark.asyncio
    async def test_missing_artifact_file_is_skipped(
        self, db_corpus: Neo4jConnection, tmp_path: Path
    ) -> None:
        """When no artifact file exists at the given path, the detector returns
        None (skip) rather than raising. This is the "artifact not yet written"
        state -- normal before any `writ compress` has ever run.
        """
        artifact_path = tmp_path / "nonexistent_abstractions.json"
        assert not artifact_path.exists(), "precondition: file must not exist"

        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)

        # RED: method does not exist yet.
        result = await checker.detect_artifact_dangling_rule_ids(
            artifact_path=artifact_path
        )

        assert result is None, (
            f"Expected None when artifact file is absent; got {result!r}"
        )

    @pytest.mark.asyncio
    async def test_all_abstractions_with_dangling_flagged(
        self, db_corpus: Neo4jConnection, tmp_path: Path
    ) -> None:
        """When multiple abstractions each carry at least one dangling rule_id,
        all of them appear in the findings.
        """
        artifact_path = tmp_path / "abstractions.json"
        _write_artifact(artifact_path, [
            {
                "abstraction_id": "ABS-VALIDATE-TEST-001",
                "summary": "First abstraction with a dangling rule ref",
                "rule_ids": [REAL_RULE_ID, "ZZZ-DANGLING-002"],
                "compression_ratio": 1.5,
            },
            {
                "abstraction_id": "ABS-VALIDATE-TEST-002",
                "summary": "Second abstraction with a dangling rule ref",
                "rule_ids": ["ZZZ-DANGLING-003"],
                "compression_ratio": 1.0,
            },
        ])

        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)

        # RED: method does not exist yet.
        result = await checker.detect_artifact_dangling_rule_ids(
            artifact_path=artifact_path
        )

        assert result, (
            f"Expected dangling findings for two abstractions with bogus ids; got {result!r}"
        )
        dangling_ids = {
            entry.get("rule_id") if isinstance(entry, dict) else entry
            for entry in result
        }
        assert "ZZZ-DANGLING-002" in dangling_ids, (
            f"ZZZ-DANGLING-002 missing from findings; got {dangling_ids}"
        )
        assert "ZZZ-DANGLING-003" in dangling_ids, (
            f"ZZZ-DANGLING-003 missing from findings; got {dangling_ids}"
        )

    @pytest.mark.asyncio
    async def test_run_all_checks_flags_dangling_artifact_refs(
        self, db_corpus: Neo4jConnection, tmp_path: Path
    ) -> None:
        """run_all_checks with an artifact_path containing a dangling rule_id
        must set exit_code=1 and findings['artifact_dangling_rule_ids'] truthy.

        This is the integration gate: the new detector must be wired into
        run_all_checks AND must flip the exit code.
        """
        artifact_path = tmp_path / "abstractions.json"
        _write_artifact(artifact_path, [
            {
                "abstraction_id": "ABS-VALIDATE-RUNALL-000",
                "summary": "run_all_checks dangling ref test",
                "rule_ids": [REAL_RULE_ID, BOGUS_RULE_ID],
                "compression_ratio": 2.0,
            },
        ])

        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)

        # RED: run_all_checks does not include artifact_dangling_rule_ids yet.
        # The method also needs to accept artifact_path so it can forward it
        # to the new detector.
        findings = await checker.run_all_checks(
            skip_redundancy=True,
            bible_dir=BIBLE,
            artifact_path=artifact_path,
        )

        assert findings.get("exit_code") == 1, (
            f"exit_code should be 1 when artifact_dangling_rule_ids is truthy; "
            f"got exit_code={findings.get('exit_code')}"
        )
        assert findings.get("artifact_dangling_rule_ids"), (
            "findings['artifact_dangling_rule_ids'] must be truthy after seeding "
            f"a dangling rule_id; findings={findings.get('artifact_dangling_rule_ids')!r}"
        )
