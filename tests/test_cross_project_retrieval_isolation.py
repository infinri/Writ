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


class TestEnrichmentIsScopedLikeCandidates:
    """The second channel out of the pipeline: Stage 4 graph-proximity enrichment.

    `_filter_candidates` was the ONLY is_visible call site, so a candidate that
    passed the scope filter still carried `relationships`, a list built from
    AdjacencyCache with no project filter, no node-type filter and no visibility
    check at all (writ/retrieval/traversal.py's build_from_db matches
    `(a)-[r]->(b)` for every non-BELONGS_TO edge in the shared graph). With
    budget_tokens above STANDARD_THRESHOLD that list reaches the caller verbatim
    and `cmd_format` renders it as the `RELATED: <ids>` line, so a scoped query
    was returning scoped candidates decorated with unscoped neighbours.

    WHY PRESSURESCENARIO AND NOT DECISION. The obvious record type cannot express
    this leak: `_GRAPH_ID_COALESCE` is built from NODE_ID_FIELDS, which has no
    decision_id, so build_from_db's `src_id IS NOT NULL` clause drops every
    Decision edge before it reaches the cache -- pinned directly in
    TestRecordEdgeEndpointsStayOutOfTheCache below, and a Decision-based
    regression test here would have passed before the fix while asserting
    nothing. PressureScenario is the live shape: it is absent from
    DOCTRINE_NODE_TYPES (so node_scope.py classifies it as a record), it carries
    a NODE_ID_FIELDS id (so it DOES enter the cache), and PRESSURE_TESTS wires it
    straight to a Rule. Verified on the disposable graph before this test was
    written: the cache came back with PSC-...  as a neighbour of the Rule.
    """

    PSC_ID = "PSC-ENRICH-001"

    async def _seed(self, db, tmp_path) -> None:
        """Rule A -[RELATED_TO]-> Rule B (doctrine to doctrine, must survive) and
        PressureScenario -[PRESSURE_TESTS]-> Rule A (record to doctrine, must be
        scoped). Everything is tagged "writ" exactly like the real corpus, so
        "another project" here means the CALLER is elsewhere -- the same asymmetry
        TestDoctrineReachesEveryProject guards."""
        bible = _make_bible(tmp_path, "writ", ["ENRICH-DOC-001", "ENRICH-DOC-002"])
        await ingest_path(bible, db)
        await db.create_edge("RELATED_TO", "ENRICH-DOC-001", "ENRICH-DOC-002")
        await db.create_methodology_node("PressureScenario", {
            "scenario_id": self.PSC_ID,
            "prompt": "a pressure prompt",
            "expected_compliance": "comply",
            "failure_patterns": ["fold"],
            "rule_under_test": "ENRICH-DOC-001",
            "difficulty": "medium",
        })
        await db.create_edge("PRESSURE_TESTS", self.PSC_ID, "ENRICH-DOC-001")

    @staticmethod
    def _entry(out: dict, rule_id: str) -> dict:
        for r in out.get("rules", []):
            if r.get("rule_id") == rule_id:
                return r
        raise AssertionError(
            f"{rule_id} is not in the result set at all ({_result_ids(out)!r}); the "
            f"enrichment assertions below would pass vacuously"
        )

    @staticmethod
    def _related(entry: dict) -> set:
        return {
            n.get("rule_id") for n in entry.get("relationships", []) if isinstance(n, dict)
        }

    @pytest.mark.asyncio
    async def test_a_record_typed_neighbour_does_not_ride_along_to_another_project(
        self, db, tmp_path,
    ) -> None:
        await self._seed(db, tmp_path)
        pipeline = await build_pipeline(db)

        out = pipeline.query("a thing happens", project="proj-a", budget_tokens=100_000)
        related = self._related(self._entry(out, "ENRICH-DOC-001"))

        assert self.PSC_ID not in related, (
            f"leak: the record-typed neighbour {self.PSC_ID} (tagged 'writ') reached a "
            f"caller from 'proj-a' through the enrichment channel; relationships={related!r}. "
            f"is_visible must be applied where the neighbour list is ATTACHED, not only in "
            f"_filter_candidates"
        )

    @pytest.mark.asyncio
    async def test_a_doctrine_neighbour_still_survives_the_scope_filter(
        self, db, tmp_path,
    ) -> None:
        """Anti-vacuity, and the same catastrophic-fix guard as
        TestDoctrineReachesEveryProject one level down: a fix that dropped the
        whole neighbour list, or that scoped it by project TAG, would pass the
        test above while silently emptying every RELATED line for every project
        except this one."""
        await self._seed(db, tmp_path)
        pipeline = await build_pipeline(db)

        out = pipeline.query("a thing happens", project="proj-a", budget_tokens=100_000)
        related = self._related(self._entry(out, "ENRICH-DOC-001"))

        assert "ENRICH-DOC-002" in related, (
            f"the doctrine neighbour ENRICH-DOC-002 was dropped for a caller from "
            f"'proj-a'; relationships={related!r}. Doctrine is universal by design, so "
            f"scoping the enrichment must not cost it"
        )

    @pytest.mark.asyncio
    async def test_the_same_record_neighbour_does_reach_its_own_project(
        self, db, tmp_path,
    ) -> None:
        """The other half: the filter is a SCOPE, not a blanket exclusion of
        record-typed neighbours. A caller in the record's own project still gets
        it, which is also what keeps this repo's own RELATED lines intact."""
        await self._seed(db, tmp_path)
        pipeline = await build_pipeline(db)

        out = pipeline.query("a thing happens", project="writ", budget_tokens=100_000)
        related = self._related(self._entry(out, "ENRICH-DOC-001"))

        assert self.PSC_ID in related, (
            f"the record-typed neighbour {self.PSC_ID} is tagged 'writ' and the caller IS "
            f"'writ', so it must still be attached; relationships={related!r}"
        )

    @pytest.mark.asyncio
    async def test_the_rendered_related_line_carries_no_foreign_neighbour(
        self, db, tmp_path,
    ) -> None:
        """End of the channel, not the middle of it: budget_tracking.cmd_format is
        what turns `relationships` into the `RELATED: <ids>` text the model reads
        (writ/session/budget_tracking.py:402-406, full mode only). Asserting on
        the dict alone would leave the possibility that the render reads some other
        source."""
        import io
        import json
        import sys

        from writ.session.budget_tracking import cmd_format

        await self._seed(db, tmp_path)
        pipeline = await build_pipeline(db)
        out = pipeline.query("a thing happens", project="proj-a", budget_tokens=100_000)
        assert out.get("mode") == "full", (
            f"mode is {out.get('mode')!r}, but only full mode renders a RELATED line; "
            f"this test would pass vacuously"
        )

        stdin_backup = sys.stdin
        sys.stdin = io.StringIO(json.dumps(out))
        try:
            from contextlib import redirect_stdout

            buf = io.StringIO()
            with redirect_stdout(buf):
                cmd_format()
        finally:
            sys.stdin = stdin_backup
        rendered = buf.getvalue()

        related_lines = [ln for ln in rendered.splitlines() if ln.startswith("RELATED:")]
        assert related_lines, (
            f"no RELATED line was rendered at all, so the assertion below would pass "
            f"vacuously; rendered output was:\n{rendered}"
        )
        assert self.PSC_ID not in rendered, (
            f"leak: {self.PSC_ID} reached the rendered injection text for a caller from "
            f"'proj-a'. RELATED lines: {related_lines!r}"
        )
        assert any("ENRICH-DOC-002" in ln for ln in related_lines), (
            f"the doctrine neighbour vanished from the rendered RELATED line: {related_lines!r}"
        )

    @pytest.mark.asyncio
    async def test_the_cache_entry_carries_enough_to_decide_visibility(
        self, db, tmp_path,
    ) -> None:
        """The seam the filter depends on, pinned so it cannot be quietly removed.

        A record-typed neighbour is NEVER in the pipeline's `_metadata` dict --
        `_load_candidates` loads Rule plus the five retrievable methodology labels
        and nothing else (TestLoadCandidatesForwardGuard) -- so a filter that
        looked the neighbour up there would find {}, default its node_type to
        "Rule" exactly as _filter_candidates does, and admit every foreign record:
        fail-open, and green on a metadata-shaped unit test. The adjacency entry
        therefore has to carry the neighbour's own label and project tag.
        """
        await self._seed(db, tmp_path)
        cache = AdjacencyCache()
        await cache.build_from_db(db)

        entries = [
            n for n in cache.get_neighbors("ENRICH-DOC-001")
            if n.get("rule_id") == self.PSC_ID
        ]
        assert entries, (
            f"{self.PSC_ID} is not a cached neighbour of ENRICH-DOC-001 at all; the "
            f"leak this class covers cannot be reproduced and every assertion above "
            f"would pass for the wrong reason"
        )
        entry = entries[0]
        assert entry.get("node_type") == "PressureScenario", (
            f"the adjacency entry does not carry the neighbour's own label "
            f"({entry!r}), so is_visible cannot be applied to it without a metadata "
            f"lookup that returns {{}} for every record type"
        )
        assert entry.get("project") == "writ", (
            f"the adjacency entry does not carry the neighbour's project tag ({entry!r})"
        )

    @pytest.mark.asyncio
    async def test_a_doctrine_entry_also_carries_its_label(self, db, tmp_path) -> None:
        """Anti-vacuity for the pin above: the fields are populated for every
        entry, not only for the one label this test names."""
        await self._seed(db, tmp_path)
        cache = AdjacencyCache()
        await cache.build_from_db(db)

        entries = [
            n for n in cache.get_neighbors("ENRICH-DOC-001")
            if n.get("rule_id") == "ENRICH-DOC-002"
        ]
        assert entries, "ENRICH-DOC-002 is not a cached neighbour of ENRICH-DOC-001"
        assert entries[0].get("node_type") == "Rule", entries[0]


class TestRecordEdgeEndpointsStayOutOfTheCache:
    """Pins the SECOND, independent reason no Decision record can ride along
    today, and the reason this file's enrichment tests use PressureScenario.

    `wire_governed_by` creates real Decision -[GOVERNED_BY]-> Rule edges in the
    live graph, and build_from_db matches every non-BELONGS_TO edge. The Decision
    is dropped only because `_GRAPH_ID_COALESCE` is derived from NODE_ID_FIELDS,
    which deliberately excludes decision_id, so the coalesced src_id is NULL and
    the `src_id IS NOT NULL` clause discards the row.

    THIS TEST PASSED BEFORE THE ENRICHMENT FIX. It is a forward guard, not the
    regression proof: adding decision_id to NODE_ID_FIELDS (or widening the
    coalesce) would make record nodes cache-reachable, and that edit should have
    to turn this red rather than silently arming the channel.
    """

    @pytest.mark.asyncio
    async def test_a_real_governed_by_edge_puts_no_decision_in_the_cache(
        self, db, tmp_path,
    ) -> None:
        bible = _make_bible(tmp_path, "writ", ["ENRICH-GOV-001"])
        await ingest_path(bible, db)
        await db.create_decision(
            decision_id="ENRICH-DEC-001", project="proj-b", title="t", rationale="r",
            phase="planning", session_id="s", ts="2026-08-11T00:00:00+00:00",
        )
        try:
            await db.wire_governed_by("ENRICH-DEC-001", "ENRICH-GOV-001", "proj-b")
            edges = await db._run(
                "MATCH (d:Decision {decision_id: $id})-[r:GOVERNED_BY]->(:Rule) "
                "RETURN count(r) AS c", id="ENRICH-DEC-001",
            )
            assert edges[0]["c"] == 1, (
                "the GOVERNED_BY edge was not created, so the assertion below would "
                "pass vacuously"
            )

            cache = AdjacencyCache()
            await cache.build_from_db(db)
            neighbours = {
                n.get("rule_id") for n in cache.get_neighbors("ENRICH-GOV-001")
            }
            assert "ENRICH-DEC-001" not in neighbours, (
                f"a Decision record became cache-reachable: {neighbours!r}. The "
                f"enrichment scope filter is now the only thing standing between a "
                f"record edge and another project's RELATED line"
            )
        finally:
            await db._run(
                "MATCH (d:Decision {decision_id: $id}) DETACH DELETE d",
                id="ENRICH-DEC-001",
            )


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
