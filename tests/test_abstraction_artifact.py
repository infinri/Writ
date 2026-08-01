"""Durable abstraction artifact: bible/abstractions.json persistence tests.

These tests cover the two new behaviors introduced by the artifact design:

1. `writ compress` (or `import-markdown --compress`) writes a cached JSON artifact
   at a configurable path (defaulting to bible/abstractions.json).

2. A new dep-free materialization function reads that artifact and writes
   Abstraction nodes + ABSTRACTS edges WITHOUT importing sentence_transformers.

RED today because:
  - run_compression does not accept an `artifact_path` kwarg.
  - The dep-free materialization function (materialize_abstractions_from_artifact)
    does not exist.

Artifact path override assumption
----------------------------------
The implementation is expected to accept an `artifact_path: Path | None = None`
keyword argument on `run_compression` (in writ/compression/abstractions.py) and
a standalone `materialize_abstractions_from_artifact(artifact_path, db, project)`
async function in the same module. When `artifact_path` is None the default
is `bible/abstractions.json` relative to the repo root.

Tests use a `tmp_path`-scoped artifact path so they never mutate the committed
bible/abstractions.json file.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import pytest_asyncio

from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
from writ.graph.db import Neo4jConnection

BIBLE = Path(__file__).resolve().parent.parent / "bible"
REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture()
async def db_corpus():
    """Live Neo4j with full corpus restored from the tracked cypher dump.

    The dump, not a bible/ re-ingest, because these tests only need a complete
    RULE corpus to link edges against, and bible/ is untracked (absent on CI);
    a re-ingest there would wipe the graph and repopulate nothing. Clears
    abstractions on teardown.
    """
    db = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
    try:
        async with db._driver.session(database=db._database) as s:
            await (await s.run("RETURN 1 AS ok")).consume()
    except Exception:
        await db.close()
        pytest.skip("Neo4j unreachable")
    from writ.graph.dump import import_cypher_dump

    await import_cypher_dump(db, (REPO_ROOT / "writ-corpus.cypher").read_text())
    yield db
    # Sweep any abstractions created during the test; rules stay.
    await db.delete_abstractions(project="writ")
    await db.close()


# ---------------------------------------------------------------------------
# Schema validation helper (pure)
# ---------------------------------------------------------------------------

def _assert_valid_artifact(data: dict) -> None:
    """Assert the artifact conforms to the documented schema."""
    assert "project" in data, "artifact must have a 'project' key"
    assert "abstractions" in data, "artifact must have an 'abstractions' list"
    assert isinstance(data["abstractions"], list), "'abstractions' must be a list"
    for i, item in enumerate(data["abstractions"]):
        assert "abstraction_id" in item, f"item[{i}] missing 'abstraction_id'"
        assert "summary" in item, f"item[{i}] missing 'summary'"
        assert "rule_ids" in item, f"item[{i}] missing 'rule_ids'"
        assert "compression_ratio" in item, f"item[{i}] missing 'compression_ratio'"
        assert isinstance(item["rule_ids"], list), (
            f"item[{i}]['rule_ids'] must be a list"
        )


# ---------------------------------------------------------------------------
# Test 1: run_compression writes the JSON artifact
# ---------------------------------------------------------------------------

class TestRunCompressionWritesArtifact:
    """run_compression writes bible/abstractions.json (or a caller-supplied path).

    RED today: run_compression does not accept artifact_path and does not write
    any file.
    """

    @pytest.mark.asyncio
    async def test_run_compression_writes_artifact(
        self, db_corpus: Neo4jConnection, tmp_path: Path
    ) -> None:
        """After run_compression, the artifact file exists, parses correctly,
        and contains >0 abstractions each with a non-empty rule_ids list.

        Uses a tmp_path artifact so the repo's bible/abstractions.json is not
        touched during the test run.

        Skips when sentence-transformers ([fallback] extra, Finding D) is not
        installed: run_compression's clustering path needs it, and CI installs
        [dev] only.
        """
        pytest.importorskip(
            "sentence_transformers",
            reason="sentence-transformers ([fallback] extra) not installed",
        )
        from writ.compression.abstractions import run_compression

        artifact_path = tmp_path / "abstractions.json"

        # RED: run_compression does not yet accept artifact_path.
        result = await run_compression(db_corpus, project="writ", artifact_path=artifact_path)

        # 1. The artifact file must exist on disk.
        assert artifact_path.exists(), (
            f"run_compression did not write artifact to {artifact_path}"
        )

        # 2. The file must be valid JSON conforming to the artifact schema.
        raw = artifact_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        _assert_valid_artifact(data)

        # 3. Must have >0 abstractions (the corpus has hundreds of non-mandatory rules).
        assert len(data["abstractions"]) > 0, (
            "Expected at least one abstraction in artifact; corpus is non-empty"
        )

        # 4. Every abstraction must name at least one rule.
        for item in data["abstractions"]:
            assert len(item["rule_ids"]) > 0, (
                f"abstraction {item['abstraction_id']!r} has an empty rule_ids list"
            )

        # 5. The in-memory result must also be consistent with what landed on disk.
        assert result["abstractions"], (
            "run_compression returned empty abstractions list but corpus is non-empty"
        )
        assert len(result["abstractions"]) == len(data["abstractions"]), (
            "in-memory abstractions count differs from artifact abstractions count"
        )


# ---------------------------------------------------------------------------
# Test 2: dep-free materialization from artifact
# ---------------------------------------------------------------------------

class TestMaterializeFromArtifactIsDepFree:
    """materialize_abstractions_from_artifact reads the artifact and writes
    Abstraction nodes + ABSTRACTS edges WITHOUT importing sentence_transformers.

    RED today: the function does not exist.
    """

    @pytest.mark.asyncio
    async def test_materialize_from_artifact_is_dep_free(
        self, db_corpus: Neo4jConnection, tmp_path: Path
    ) -> None:
        """Given a hand-crafted abstractions.json referencing real rule_ids, calling
        materialize_abstractions_from_artifact:
          - writes Abstraction nodes with matching summaries,
          - creates ABSTRACTS edges pointing to the named rule_ids,
          - does NOT import sentence_transformers (the whole dep-free point).

        We use two well-known rule ids present in the corpus (ENF-COMMS-001 and
        ENF-PROC-BRAIN-001 which are dual-location methodology rules, always
        ingested). The test verifies the nodes land and edges resolve.
        """
        # These rule_ids exist in bible/methodology/ and survive every clean ingest.
        rule_id_a = "ENF-COMMS-001"
        rule_id_b = "ENF-PROC-BRAIN-001"

        artifact_data = {
            "project": "writ",
            "abstractions": [
                {
                    "abstraction_id": "ABS-TESTING-000",
                    "summary": "Artifact materialization test abstraction",
                    "rule_ids": [rule_id_a, rule_id_b],
                    "compression_ratio": 2.5,
                },
                {
                    "abstraction_id": "ABS-TESTING-001",
                    "summary": "Single-member test abstraction",
                    "rule_ids": [rule_id_a],
                    "compression_ratio": 1.0,
                },
            ],
        }

        artifact_path = tmp_path / "abstractions.json"
        artifact_path.write_text(
            json.dumps(artifact_data, indent=2), encoding="utf-8"
        )

        # Ensure sentence_transformers is NOT already imported so the dep-free
        # assertion is meaningful. If it was already imported by the corpus
        # fixture we can't un-import it, so we test the module list AFTER the call.
        modules_before = set(sys.modules.keys())

        # RED: materialize_abstractions_from_artifact does not exist yet.
        from writ.compression.abstractions import materialize_abstractions_from_artifact  # noqa: PLC0415

        await materialize_abstractions_from_artifact(
            artifact_path=artifact_path,
            db=db_corpus,
            project="writ",
        )

        # 1. Abstraction nodes must exist with matching summaries.
        async with db_corpus._driver.session(database=db_corpus._database) as s:
            res = await s.run(
                "MATCH (a:Abstraction) WHERE a.project = 'writ' "
                "RETURN a.abstraction_id AS abs_id, a.summary AS summary "
                "ORDER BY a.abstraction_id"
            )
            nodes = [r.data() async for r in res]

        node_ids = {n["abs_id"] for n in nodes}
        assert "ABS-TESTING-000" in node_ids, (
            f"ABS-TESTING-000 not found in graph; got {sorted(node_ids)}"
        )
        assert "ABS-TESTING-001" in node_ids, (
            f"ABS-TESTING-001 not found in graph; got {sorted(node_ids)}"
        )

        summaries = {n["abs_id"]: n["summary"] for n in nodes}
        assert summaries["ABS-TESTING-000"] == "Artifact materialization test abstraction", (
            f"summary mismatch: {summaries['ABS-TESTING-000']!r}"
        )

        # 2. ABSTRACTS edges must point to the named rule_ids.
        async with db_corpus._driver.session(database=db_corpus._database) as s:
            res = await s.run(
                "MATCH (a:Abstraction {abstraction_id: 'ABS-TESTING-000'})-[:ABSTRACTS]->(r:Rule) "
                "RETURN r.rule_id AS rule_id ORDER BY rule_id"
            )
            linked_rules = [r["rule_id"] async for r in res]

        assert rule_id_a in linked_rules, (
            f"ABSTRACTS edge to {rule_id_a} missing; linked: {linked_rules}"
        )
        assert rule_id_b in linked_rules, (
            f"ABSTRACTS edge to {rule_id_b} missing; linked: {linked_rules}"
        )

        # 3. sentence_transformers must NOT have been imported by the materialization
        # call (dep-free contract). We check the delta: if it was not imported before
        # the call, it must not be imported after. (The subprocess test below is the
        # non-vacuous guard -- this in-process check can pass vacuously if an earlier
        # test in the session already loaded the dep.)
        if "sentence_transformers" not in modules_before:
            assert "sentence_transformers" not in sys.modules, (
                "materialize_abstractions_from_artifact imported sentence_transformers "
                "-- this breaks the dep-free contract"
            )

    def test_materialize_import_chain_is_dep_free(self) -> None:
        """Non-vacuous dep-free guard (fresh interpreter): importing the abstractions
        module and referencing the materialize entrypoint must NOT pull the [fallback]
        clustering deps (sentence_transformers, sklearn). Catches a future top-level
        heavy import in abstractions.py that the in-process sys.modules delta check
        would miss once an earlier test has already loaded the dep."""
        import subprocess

        script = (
            "import sys; import writ.compression.abstractions as ab; "
            "assert hasattr(ab, 'materialize_abstractions_from_artifact'); "
            "heavy = [m for m in ('sentence_transformers', 'sklearn') if m in sys.modules]; "
            "assert not heavy, heavy; print('DEPFREE_OK')"
        )
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True
        )
        assert result.returncode == 0, (
            f"dep-free import check failed:\nstdout={result.stdout}\nstderr={result.stderr}"
        )
        assert "DEPFREE_OK" in result.stdout
