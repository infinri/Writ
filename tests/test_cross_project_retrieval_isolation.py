"""Part 3 (isolation cycle v2): the retrieval-scope proof against a REAL graph.

ENF-SYS-005: a claim about a confidentiality boundary between concurrent projects
sharing one database is not provable with a stubbed metadata dict, so every class
here runs against a real Neo4j instance. Point WRIT_NEO4J_URI at the disposable
test instance before running this file -- see tests/conftest.py's
_refuse_production_graph_when_isolated, which raises immediately if a test opens
the production host:port while WRIT_TEST_GRAPH=1 is set. NEVER run this file
against the default (production) instance.

    WRIT_TEST_GRAPH=1 WRIT_NEO4J_URI=bolt://localhost:7688 \\
    WRIT_NEO4J_PASSWORD=writtestpass \\
    .venv/bin/python -m pytest tests/test_cross_project_retrieval_isolation.py -q

THE GUARD THAT MUST BE ASSERTED FIRST. All 287 Rule nodes and every methodology
node in the live corpus carry `project: "writ"`. The naive fix -- scoping a query
to {caller_project, "_shared"} -- would exclude the ENTIRE doctrine corpus from
every project except this one, and every isolation test would still pass while
Writ silently stopped injecting any rule anywhere else. TestDoctrineReachesEveryProject
is that guard, seeded with real Rule nodes tagged "writ" exactly as the corpus is,
queried by a caller that is deliberately NOT "writ".

WHY A REAL "RECORD" NODE IS SIMULATED RATHER THAN LOADED. `_load_candidates`
(writ/retrieval/pipeline.py) only ever loads Rule + the five retrievable
methodology labels -- see TestLoadCandidatesForwardGuard below, which pins that
structurally. So there is no real record-typed node that ever reaches
RetrievalPipeline.query() today, by design, and this file cannot seed one through
the normal ingestion path. TestRecordScoping instead reads back a REAL Decision
record's `project` value from a real Neo4j write (db.create_decision), and builds
the pipeline's in-memory metadata dict with that value under an explicit
`node_types=["Rule", "Decision"]` override -- the caller-chosen-whitelist branch
of `_resolve_stage1_filter`, which is the one legitimate way to get a non-doctrine
type past Stage 1 for this defense-in-depth check. The `project` value is real
graph data; the metadata SHAPE around it is test-only, and the class docstring
says so again at the point it matters.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
from writ.graph.db import Neo4jConnection
from writ.graph.methodology_ingest import ingest_path
from writ.retrieval.pipeline import RetrievalPipeline, _load_candidates, build_pipeline
from writ.retrieval.traversal import AdjacencyCache


# ---------------------------------------------------------------------------
# Shared graph fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def db():
    conn = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
    await conn.clear_all()
    yield conn
    await conn.clear_all()
    await conn.close()


def _rule_md(rule_id: str, domain: str = "security") -> str:
    """A minimal well-formed rule doc, sufficient for ingest_path to create a
    Rule node that RANKED_INCLUDE_WHERE (mandatory IS NULL OR false) admits into
    the candidate pool _load_candidates builds."""
    return f"""<!-- RULE START: {rule_id} -->
## Rule {rule_id}

**Domain**: {domain}
**Severity**: Medium
**Scope**: Component

### Trigger
When a thing happens.

### Statement
A statement for {rule_id}.

### Violation
```python
x = 1
```

### Pass
```python
y = 2
```

### Enforcement
Code review.

### Rationale
A rationale for {rule_id}.

<!-- RULE END: {rule_id} -->
"""


def _make_bible(tmp: Path, name: str, rule_ids: list[str]) -> Path:
    d = tmp / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "rules.md").write_text("\n".join(_rule_md(r) for r in rule_ids), encoding="utf-8")
    return d


def _result_ids(out: dict) -> set:
    return {r["rule_id"] for r in out.get("rules", [])}


# ---------------------------------------------------------------------------
# THE GUARD, asserted first: doctrine reaches every project regardless of tag
# ---------------------------------------------------------------------------


class TestDoctrineReachesEveryProject:
    """Capability 25. Seeds real Rule nodes the way the actual corpus is tagged
    (ingest_path's own default project is "writ"), builds the REAL pipeline over
    the real graph (no stubbed BM25/vector), and queries as a caller from a
    project that is NOT "writ". If the fix scoped by project tag instead of node
    type, this class fails: it would exclude every Rule from "proj-a"."""

    @pytest.mark.asyncio
    async def test_project_a_query_retrieves_the_writ_tagged_doctrine_corpus(
        self, db, tmp_path,
    ) -> None:
        bible = _make_bible(tmp_path, "writ", ["CROSS-DOC-001", "CROSS-DOC-002"])
        await ingest_path(bible, db)  # default project -- "writ", exactly like the real corpus

        pipeline = await build_pipeline(db)
        result = pipeline.query("a thing happens", project="proj-a", budget_tokens=100_000)
        ids = _result_ids(result)

        assert {"CROSS-DOC-001", "CROSS-DOC-002"} <= ids, (
            "a query carrying a foreign project must still retrieve the full "
            f"writ-tagged doctrine corpus; got {ids!r}. If this fails, the fix "
            "scoped by project tag instead of node type -- the catastrophic outcome "
            "this guard exists to catch."
        )

    @pytest.mark.asyncio
    async def test_a_second_foreign_project_also_receives_the_same_corpus(
        self, db, tmp_path,
    ) -> None:
        """Anti-vacuity: doctrine reaching ONE foreign project could still be an
        accidental allowlist entry for "proj-a" specifically. A second, unrelated
        caller project must see the identical corpus."""
        bible = _make_bible(tmp_path, "writ", ["CROSS-DOC-003"])
        await ingest_path(bible, db)

        pipeline = await build_pipeline(db)
        ids_a = _result_ids(pipeline.query("a thing happens", project="proj-a", budget_tokens=100_000))
        ids_b = _result_ids(pipeline.query("a thing happens", project="totally-unrelated-project", budget_tokens=100_000))

        assert "CROSS-DOC-003" in ids_a
        assert "CROSS-DOC-003" in ids_b


class TestNoProjectStillReceivesFullDoctrine:
    """Capability 27, doctrine half. An unscoped caller (project=None) must still
    receive the complete doctrine corpus -- this is a completeness contract, not
    only a leak-prevention one; a design that made project=None return LESS
    doctrine than a scoped caller would break every existing unscoped caller
    (authoring.py's dedup suggestions, `writ query`)."""

    @pytest.mark.asyncio
    async def test_unscoped_query_still_retrieves_the_full_doctrine_corpus(
        self, db, tmp_path,
    ) -> None:
        bible = _make_bible(tmp_path, "writ", ["CROSS-NOPROJ-001"])
        await ingest_path(bible, db)

        pipeline = await build_pipeline(db)
        ids = _result_ids(pipeline.query("a thing happens", budget_tokens=100_000))

        assert "CROSS-NOPROJ-001" in ids


# ---------------------------------------------------------------------------
# Record-typed scoping: the two-sided proof
# ---------------------------------------------------------------------------


class TestRecordScoping:
    """Capability 26 and capability 27's record half.

    _load_candidates never loads a record type (see TestLoadCandidatesForwardGuard),
    so a record-typed candidate is injected into the pipeline's metadata directly,
    with its `project` value read back from a REAL Decision write. The explicit
    `node_types=["Rule", "Decision"]` argument is what lets a non-doctrine type
    past Stage 1 at all -- see `_resolve_stage1_filter`'s caller-chosen-whitelist
    branch -- so the project-scope predicate at the end of `_filter_candidates`
    is what this class is actually exercising.
    """

    async def _real_decision_project(self, db, decision_id: str, project: str) -> str:
        await db.create_decision(
            decision_id=decision_id, project=project, title="t", rationale="r",
            phase="planning", session_id="s", ts="2026-08-11T00:00:00+00:00",
        )
        try:
            rows = await db._run(
                "MATCH (d:Decision {decision_id: $id}) RETURN d.project AS project",
                id=decision_id,
            )
            return rows[0]["project"]
        finally:
            # Decision is in clear_all()'s default PRESERVE set, so the fixture's
            # teardown will not remove it -- delete it explicitly here.
            await db._run(
                "MATCH (d:Decision {decision_id: $id}) DETACH DELETE d", id=decision_id,
            )

    def _stub_pipeline_with(self, metadata: dict) -> RetrievalPipeline:
        from unittest.mock import MagicMock

        import numpy as np

        from writ.retrieval.embeddings import ScoredResult

        ids = list(metadata.keys())
        keyword_stub = MagicMock()
        keyword_stub.search.return_value = [{"rule_id": r, "score": 0.9} for r in ids]
        vector_stub = MagicMock()
        vector_stub.search.return_value = [ScoredResult(rule_id=r, score=0.9) for r in ids]
        encoder_stub = MagicMock()
        encoder_stub.encode.return_value = np.zeros(384, dtype=np.float32)
        return RetrievalPipeline(
            keyword_index=keyword_stub, vector_store=vector_stub,
            adjacency_cache=AdjacencyCache(), embedding_model=encoder_stub,
            rule_metadata=metadata,
        )

    @pytest.mark.asyncio
    async def test_project_a_retrieves_its_own_record_and_not_project_bs(self, db) -> None:
        project_a = await self._real_decision_project(db, "CROSS-REC-A", "proj-a")
        project_b = await self._real_decision_project(db, "CROSS-REC-B", "proj-b")

        metadata = {
            "CROSS-REC-A": {
                "node_type": "Decision", "routes": ["semantic"], "domain": "process",
                "severity": "low", "confidence": "n/a", "statement": "decision a",
                "trigger": "t", "project": project_a,
            },
            "CROSS-REC-B": {
                "node_type": "Decision", "routes": ["semantic"], "domain": "process",
                "severity": "low", "confidence": "n/a", "statement": "decision b",
                "trigger": "t", "project": project_b,
            },
        }
        pipeline = self._stub_pipeline_with(metadata)
        ids = _result_ids(pipeline.query(
            "decision", project="proj-a", node_types=["Rule", "Decision"],
        ))

        assert "CROSS-REC-A" in ids
        assert "CROSS-REC-B" not in ids, "leak: proj-b's record reached proj-a's query"

    @pytest.mark.asyncio
    async def test_no_project_retrieves_zero_project_specific_records(self, db) -> None:
        project_a = await self._real_decision_project(db, "CROSS-REC-NOPROJ", "proj-a")

        metadata = {
            "CROSS-REC-NOPROJ": {
                "node_type": "Decision", "routes": ["semantic"], "domain": "process",
                "severity": "low", "confidence": "n/a", "statement": "decision",
                "trigger": "t", "project": project_a,
            },
        }
        pipeline = self._stub_pipeline_with(metadata)
        ids = _result_ids(pipeline.query("decision", node_types=["Rule", "Decision"]))

        assert "CROSS-REC-NOPROJ" not in ids, (
            "an unscoped caller (project=None) must not receive a project-specific "
            "record; only doctrine gets the no-project pass"
        )


# ---------------------------------------------------------------------------
# Forward guard: _load_candidates never admits a record type
# ---------------------------------------------------------------------------


class TestLoadCandidatesForwardGuard:
    """Capability 36. Pins an EXISTING invariant so a future widening of
    _load_candidates cannot quietly add a record type to the pool that every
    downstream project-scope filter then has to defend against. This is NOT a
    claim about the observed cross-project content leak, which was Claude Code's
    delivery mechanic, not Writ's retrieval path -- see plan.md's Analysis
    section, "What changed since the approved plan"."""

    @pytest.mark.asyncio
    async def test_a_real_decision_node_never_appears_in_loaded_candidates(self, db) -> None:
        await db.create_decision(
            decision_id="CROSS-GUARD-001", project="writ", title="t", rationale="r",
            phase="planning", session_id="s", ts="2026-08-11T00:00:00+00:00",
        )
        try:
            all_candidates, rule_metadata = await _load_candidates(db)
            ids = {c.get("rule_id") for c in all_candidates}
            node_types = {c.get("node_type") for c in all_candidates}

            assert "CROSS-GUARD-001" not in ids, (
                "_load_candidates admitted a Decision node into the candidate pool"
            )
            assert node_types <= {"Rule", "Skill", "Playbook", "Technique", "AntiPattern", "ForbiddenResponse"}, (
                f"_load_candidates admitted an unexpected node_type: {node_types!r}"
            )
        finally:
            await db._run(
                "MATCH (d:Decision {decision_id: $id}) DETACH DELETE d",
                id="CROSS-GUARD-001",
            )

    @pytest.mark.asyncio
    async def test_candidate_pool_is_not_trivially_empty(self, db, tmp_path) -> None:
        """Anti-vacuity: the forward guard above would pass on an empty pool for
        the wrong reason. Seed one real Rule and show it IS loaded."""
        bible = _make_bible(tmp_path, "writ", ["CROSS-GUARD-RULE-001"])
        await ingest_path(bible, db)

        all_candidates, _ = await _load_candidates(db)
        ids = {c.get("rule_id") for c in all_candidates}
        assert "CROSS-GUARD-RULE-001" in ids
