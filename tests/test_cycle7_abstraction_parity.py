"""Cycle 7, Defects 2a + 2b: stop judging nodes the markdown oracle cannot
author, and give the artifact check the coverage the exemption gives up.

BACKGROUND (plan.md "### Defect 2a" / "### Defect 2b"). `writ validate
--bible-dir bible` exits 1 today on "Edge parity drift (stale=186, missing=0)"
and prints the remedy `writ reconcile`, which would DETACH DELETE all 186
ABSTRACTS edges. The graph is correct: a read-only probe measured 62
Abstraction nodes and 186 ABSTRACTS edges matching bible/abstractions.json
exactly, zero drift in both directions. The markdown parity oracle compares
against `bible/**/*.md` via `rglob("*.md")`, so a .json artifact is
structurally invisible to it -- Abstraction nodes are materialized by
`writ/compression/abstractions.py`, never parsed from a .md file.

This cycle removes "Abstraction" from `INGESTER_REGISTRY` (methodology_ingest.py),
derives `ORACLE_BLIND_LABELS = frozenset(NODE_ID_FIELDS) - frozenset(INGESTER_REGISTRY)`
from that registry, exempts edges incident to an oracle-blind node in
`detect_edge_parity` and in both reconcile prune phases, and adds
`detect_artifact_abstracts_parity` (artifact_checks.py) as the replacement
coverage: a both-directions comparison of (abstraction_id, rule_id) pairs
between bible/abstractions.json and the live graph.

Every symbol under test is UNIMPLEMENTED as of this writing:
`ARTIFACT_AUTHORED_NODE_TYPES`, `ORACLE_BLIND_LABELS`, and
`detect_artifact_abstracts_parity` do not exist yet, and `INGESTER_REGISTRY`
still contains "Abstraction". Every test below is RED today. Imports of the
not-yet-existing names are guarded inside test bodies so the module still
collects.

Capability map:
  [cap-7]  INGESTER_REGISTRY has no Abstraction key, KNOWN_NODE_TYPES still
           equals its key set, and a markdown file declaring an Abstraction
           produces an explicit unknown-node-type IngestError instead of a
           silent write.
  [cap-8]  ORACLE_BLIND_LABELS == frozenset(NODE_ID_FIELDS) -
           frozenset(INGESTER_REGISTRY), and evaluates to {"Abstraction"}.
  [cap-9]  (real graph) detect_edge_parity against the live corpus returns
           None, with no stale ABSTRACTS edge in particular.
  [cap-10] (real graph) LOAD-BEARING. detect_edge_parity STILL reports a
           genuinely stale edge between two ordinary, markdown-authorable
           nodes seeded in a private project scope -- proving the exemption
           narrowed the check rather than blinding it. Without this test, an
           exemption that blinded detect_edge_parity entirely would still
           pass every other test in this file.
  [cap-11] (real graph) reconcile against a tmp markdown corpus in a private
           project scope deletes a genuinely stale seeded node but leaves a
           seeded Abstraction node and its ABSTRACTS edge in place.
  [cap-12] detect_artifact_abstracts_parity reports graph-only pairs as
           `stale` and artifact-only pairs as `missing`, in one result.
  [cap-13] detect_artifact_abstracts_parity returns None when the artifact
           file is absent, when the driver is None, and when the graph holds
           zero Category nodes.
  [cap-14] (real graph) detect_artifact_abstracts_parity returns None against
           the live graph and the committed bible/abstractions.json -- the
           measured zero-drift state.

GRAPH SAFETY. The suite shares ONE live Neo4j at bolt://localhost:7687 with a
running interactive daemon. No `clear_all()` anywhere in this file. Capabilities
9 and 14 are strictly read-only (no fixture-owned writes, nothing to scope or
tear down). Capabilities 10 and 11 write under a private project scope,
`_TEST_SCOPE`, deliberately not starting with "-" (every real Claude-Code-
encoded project segment is derived from an absolute path and therefore always
starts with "-", so this string can never equal a real project value), cleaned
up via `clear_project(_TEST_SCOPE)` before and after -- mirrors
tests/test_memory_project_integrity.py:150,157-169 and
tests/test_decision_memory_capture.py:76,307-328. Capabilities 7, 8, 12 and 13
are pure or guard-shaped and open no Neo4j connection at all; 12 and 13 use a
small deterministic fake driver (not a mock of a real session -- neither
capability is marked real-graph) that answers the two queries
detect_artifact_abstracts_parity issues and raises loudly on any other query.

Run interpreter: .venv/bin/python -m pytest (has onnxruntime for embedding
imports elsewhere in the suite; not needed by this file, but kept consistent).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio

from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
from writ.graph.db import Neo4jConnection
from writ.graph.integrity import IntegrityChecker

# Deliberately does NOT start with "-": every real Claude-Code-encoded project
# segment is derived from an absolute path and therefore always starts with
# "-", so this string can never equal a real project value.
_TEST_SCOPE = "zzz-writ-test-only-cycle7-abstraction-parity-never-a-real-project"

BIBLE = Path(__file__).resolve().parent.parent / "bible"


def _seed_rule(rule_id: str) -> dict:
    """Minimal Rule payload for db.create_rule (no markdown, no Pydantic
    validation on this path -- see writ/graph/db/_common.py:_node_write_spec).
    Default source_origin='ingest' -> provenance='hand-authored', which is
    NOT in PARITY_EXEMPT_PROVENANCE, so a node built from this is ordinary and
    markdown-authorable in shape."""
    return {
        "rule_id": rule_id,
        "project": _TEST_SCOPE,
        "domain": "testing",
        "severity": "high",
        "scope": "slice",
        "trigger": "t",
        "statement": "s",
        "violation": "v",
        "pass_example": "p",
        "enforcement": "e",
        "rationale": "r",
        "last_validated": "2026-03-15",
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture()
async def live_db():
    """Read-only connection to the live graph: ping, yield, close.

    Capabilities 9 and 14 only READ the live corpus / committed artifact and
    write nothing, so there is no project scope to clear before or after --
    scoping and teardown apply to the fixtures that seed data (db_scoped).
    """
    db = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
    try:
        async with db._driver.session(database=db._database) as s:
            await (await s.run("RETURN 1 AS ok")).consume()
    except Exception:
        await db.close()
        pytest.skip("Neo4j unreachable")
    yield db
    await db.close()


async def _require_intact_corpus(db) -> None:
    """Skip unless the live corpus is actually intact.

    These three tests assert properties OF the real corpus, so an incomplete
    graph makes them meaningless rather than failing. That is not hypothetical:
    in a full-suite run this class failed with a `missing` list full of
    BELONGS_TO and ATTACHED_TO edges, because another test had called
    clear_all() and the graph was mid-rebuild when we read it. 48 test files
    wipe the shared production graph unscoped; that is a known open defect
    scheduled for its own cycle.

    Skipping with a named reason is the honest answer to an unmet
    precondition. It deliberately does NOT re-ingest: repairing the graph here
    would hide the contamination and make this file a silent accomplice to the
    defect. Run this file on its own to assert the real thing.
    """
    async with db._driver.session(database=db._database) as s:
        rec = await (await s.run(
            "MATCH (r:Rule) WITH count(r) AS rules "
            "MATCH ()-[e:ABSTRACTS]->() RETURN rules, count(e) AS edges"
        )).single()
    if not rec or rec["rules"] == 0 or rec["edges"] == 0:
        pytest.skip(
            f"live corpus not intact (rules={rec['rules'] if rec else 0}, "
            f"ABSTRACTS={rec['edges'] if rec else 0}); another test wiped the "
            "shared graph. Run this file alone to assert against the real corpus."
        )


@pytest_asyncio.fixture()
async def db_scoped():
    """Connect to Neo4j, wipe the private _TEST_SCOPE project, yield, clean up.

    Mirrors tests/test_memory_project_integrity.py:157-169 exactly: skip on
    unreachable Neo4j, clear_project(_TEST_SCOPE) before AND after, never
    clear_all(). Used by capabilities 10 and 11, both of which seed live nodes
    scoped to _TEST_SCOPE.
    """
    db = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
    try:
        async with db._driver.session(database=db._database) as s:
            await (await s.run("RETURN 1 AS ok")).consume()
    except Exception:
        await db.close()
        pytest.skip("Neo4j unreachable")
    await db.clear_project(_TEST_SCOPE)
    yield db
    await db.clear_project(_TEST_SCOPE)
    await db.close()


# ---------------------------------------------------------------------------
# [cap-7] INGESTER_REGISTRY excludes Abstraction; unknown-type IngestError
# ---------------------------------------------------------------------------

class TestIngesterRegistryExcludesAbstraction:
    """[cap-7] The registry correction: INGESTER_REGISTRY drops Abstraction,
    KNOWN_NODE_TYPES still derives from it, and a markdown file declaring an
    Abstraction gets an explicit IngestError instead of a silent write."""

    def test_ingester_registry_has_no_abstraction_key(self) -> None:
        from writ.graph.methodology_ingest import INGESTER_REGISTRY

        assert "Abstraction" not in INGESTER_REGISTRY, (
            "Abstraction nodes are materialized from bible/abstractions.json, "
            "never authored by the markdown ingest (discover_rule_files only "
            "yields *.md); a registry entry for a label nothing can author is "
            "what let 186 correct ABSTRACTS edges read as drift. "
            f"INGESTER_REGISTRY keys: {sorted(INGESTER_REGISTRY.keys())!r}"
        )

    def test_known_node_types_still_equals_registry_keys(self) -> None:
        from writ.graph.methodology_ingest import INGESTER_REGISTRY, KNOWN_NODE_TYPES

        assert KNOWN_NODE_TYPES == frozenset(INGESTER_REGISTRY.keys()), (
            "KNOWN_NODE_TYPES must stay derived from INGESTER_REGISTRY, not "
            "hand-listed, so removing an entry propagates automatically"
        )

    @pytest.mark.asyncio
    async def test_markdown_abstraction_produces_explicit_ingest_error(
        self, tmp_path: Path
    ) -> None:
        from writ.graph.methodology_ingest import ingest_path

        corpus = tmp_path / "corpus"
        corpus.mkdir()
        (corpus / "abs.md").write_text(
            "---\n"
            "node_type: Abstraction\n"
            "abstraction_id: ZZZ-CYCLE7-ABS-MD-001\n"
            "summary: s\n"
            "rule_ids:\n"
            "  - RULE-A\n"
            "domain: testing\n"
            "compression_ratio: 0.5\n"
            "---\n\nBody.\n",
            encoding="utf-8",
        )

        # dry_run=True + db=None is safe here: a node rejected at the
        # INGESTER_REGISTRY check never reaches `cleaned`/`parsed_nodes`, and
        # dry_run's write branch only iterates `cleaned` -- db is never
        # touched (writ/graph/methodology_ingest.py:_write_nodes).
        report = await ingest_path(corpus, None, dry_run=True)

        assert len(report.errors) == 1, (
            f"expected exactly one unknown-node-type error, got {report.errors!r}"
        )
        (error,) = report.errors
        assert error.node_type == "Abstraction"
        assert "unknown node_type" in error.reason, (
            f"expected an explicit unknown-node-type reason, got {error.reason!r}"
        )
        assert "Abstraction" not in report.counts_by_type, (
            "an unknown-type node must not be silently written; "
            f"counts_by_type={report.counts_by_type!r}"
        )
        assert report.ingested == [], (
            f"an unknown-type node must not appear as ingested; got {report.ingested!r}"
        )


# ---------------------------------------------------------------------------
# [cap-8] ORACLE_BLIND_LABELS is a derived set, and today it is {"Abstraction"}
# ---------------------------------------------------------------------------

class TestOracleBlindLabelsDerivedSet:
    """[cap-8] ORACLE_BLIND_LABELS is DERIVED (frozenset(NODE_ID_FIELDS) -
    frozenset(INGESTER_REGISTRY)), not hand-listed, and evaluates to exactly
    {"Abstraction"} once cap-7's registry fix lands. Before that fix it would
    evaluate to the EMPTY set -- which is why a naive version of this fix
    would have been a no-op."""

    def test_oracle_blind_labels_matches_its_derivation(self) -> None:
        from writ.graph.ingest import NODE_ID_FIELDS

        try:
            from writ.graph.methodology_ingest import (
                INGESTER_REGISTRY,
                ORACLE_BLIND_LABELS,
            )
        except ImportError:
            pytest.fail(
                "writ.graph.methodology_ingest.ORACLE_BLIND_LABELS does not "
                "exist yet"
            )

        assert ORACLE_BLIND_LABELS == (
            frozenset(NODE_ID_FIELDS) - frozenset(INGESTER_REGISTRY)
        ), "ORACLE_BLIND_LABELS must be derived from the registry, not hand-listed"

    def test_oracle_blind_labels_is_exactly_abstraction(self) -> None:
        try:
            from writ.graph.methodology_ingest import ORACLE_BLIND_LABELS
        except ImportError:
            pytest.fail(
                "writ.graph.methodology_ingest.ORACLE_BLIND_LABELS does not "
                "exist yet"
            )

        assert ORACLE_BLIND_LABELS == frozenset({"Abstraction"}), (
            "with the cap-7 registry fix landed, ORACLE_BLIND_LABELS must be "
            f"exactly {{'Abstraction'}}; got {ORACLE_BLIND_LABELS!r}"
        )


# ---------------------------------------------------------------------------
# [cap-9] (real graph) detect_edge_parity is clean against the live corpus
# ---------------------------------------------------------------------------

class TestEdgeParityLiveCorpusIsClean:
    """[cap-9] detect_edge_parity against the live corpus (project='writ',
    the real bible/ tree) reports zero drift, including zero stale ABSTRACTS
    edges -- the measured post-fix state (62 Abstraction nodes, 186 ABSTRACTS
    edges, matching bible/abstractions.json exactly in both directions)."""

    @pytest.mark.asyncio
    async def test_detect_edge_parity_returns_none_against_live_corpus(
        self, live_db: Neo4jConnection
    ) -> None:
        if not BIBLE.exists():
            pytest.skip(
                "requires the untracked bible/ source tree (regenerate with "
                "`writ export`; clean checkouts and CI run without it)"
            )

        await _require_intact_corpus(live_db)

        checker = IntegrityChecker(live_db._driver, live_db._database)
        result = await checker.detect_edge_parity(BIBLE, project="writ")

        assert result is None, (
            "detect_edge_parity must report zero drift against the live "
            f"corpus once Abstraction is exempt from the markdown oracle; "
            f"got {result}"
        )

    @pytest.mark.asyncio
    async def test_detect_edge_parity_reports_no_stale_abstracts_edge(
        self, live_db: Neo4jConnection
    ) -> None:
        if not BIBLE.exists():
            pytest.skip(
                "requires the untracked bible/ source tree (regenerate with "
                "`writ export`; clean checkouts and CI run without it)"
            )

        await _require_intact_corpus(live_db)

        checker = IntegrityChecker(live_db._driver, live_db._database)
        result = await checker.detect_edge_parity(BIBLE, project="writ")

        stale = (result or {}).get("stale", [])
        abstracts_stale = [e for e in stale if e[0] == "ABSTRACTS"]
        assert abstracts_stale == [], (
            f"no ABSTRACTS edge should read as stale; got {abstracts_stale!r}"
        )


# ---------------------------------------------------------------------------
# [cap-10] LOAD-BEARING: the exemption narrows the check, it does not blind it
# ---------------------------------------------------------------------------

class TestEdgeParityStillCatchesGenuineDrift:
    """[cap-10] LOAD-BEARING. Without this test, an exemption that blinded
    detect_edge_parity entirely (e.g. a bug that exempted every edge, not just
    edges incident to an oracle-blind label) would still pass every other test
    in this file. Seeds a genuinely stale edge between two ORDINARY,
    markdown-authorable Rule nodes (not Abstraction) in a private project
    scope, against an intentionally empty oracle, and asserts it is STILL
    reported stale."""

    @pytest.mark.asyncio
    async def test_stale_edge_between_markdown_authorable_nodes_still_reported(
        self, db_scoped: Neo4jConnection, tmp_path: Path
    ) -> None:
        # An empty oracle: no *.md files at all, so compute_expected_graph
        # returns (set(), set()). This isolates the claim under test -- the
        # edge below is stale ONLY because of what it is (an ordinary Rule
        # edge, no oracle-blind label, no exempt provenance), not because of
        # what the real bible/ tree happens to contain.
        empty_oracle = tmp_path / "empty-oracle"
        empty_oracle.mkdir()

        src = "ZZZ-CYCLE7-SRC-001"
        tgt = "ZZZ-CYCLE7-TGT-001"
        await db_scoped.create_rule(_seed_rule(src))
        await db_scoped.create_rule(_seed_rule(tgt))
        await db_scoped.create_edge("RELATED_TO", src, tgt, project=_TEST_SCOPE)

        checker = IntegrityChecker(db_scoped._driver, db_scoped._database)
        result = await checker.detect_edge_parity(empty_oracle, project=_TEST_SCOPE)

        assert result is not None, (
            "a stale edge between two plain Rule nodes must still be "
            "reported; an exemption that blinded the whole check would pass "
            "every other test in this file while missing this one"
        )
        assert ("RELATED_TO", src, tgt) in result["stale"], (
            f"expected ('RELATED_TO', {src!r}, {tgt!r}) in stale, "
            f"got {result['stale']!r}"
        )


# ---------------------------------------------------------------------------
# [cap-11] (real graph) reconcile prunes the stale node, keeps the Abstraction
# ---------------------------------------------------------------------------

class TestReconcileKeepsOracleBlindAbstraction:
    """[cap-11] reconcile against a tmp markdown corpus, scoped to
    _TEST_SCOPE, deletes a genuinely stale seeded Rule node but leaves a
    seeded Abstraction node and its ABSTRACTS edge in place -- neither has a
    markdown home in ANY corpus (Abstraction nodes are materialized from
    bible/abstractions.json, never parsed from *.md), so their absence from
    this tmp corpus is not drift."""

    @pytest.mark.asyncio
    async def test_reconcile_deletes_stale_node_but_keeps_abstraction_and_edge(
        self, db_scoped: Neo4jConnection, tmp_path: Path
    ) -> None:
        from writ.graph.methodology_ingest import reconcile

        kept_rule_id = "ZZZ-CYCLE7-KEPT-001"
        stale_rule_id = "ZZZ-CYCLE7-STALE-001"
        abstraction_id = "ZZZ-CYCLE7-ABS-001"

        # Non-empty oracle (reconcile refuses an empty one): one Rule that
        # matches a seeded node, so reconcile's node-presence check has a real
        # "kept" case to contrast the stale/blind cases against.
        corpus = tmp_path / "corpus"
        corpus.mkdir()
        (corpus / "kept.md").write_text(
            "---\n"
            "node_type: Rule\n"
            f"rule_id: {kept_rule_id}\n"
            "domain: testing\n"
            "severity: high\n"
            "scope: slice\n"
            "trigger: t\n"
            "statement: s\n"
            "violation: v\n"
            "pass_example: p\n"
            "enforcement: e\n"
            "rationale: r\n"
            "last_validated: 2026-03-15\n"
            "---\n\nBody.\n",
            encoding="utf-8",
        )

        # The kept Rule must exist before create_abstracts_edge below can
        # resolve it; reconcile's internal ingest_path will MERGE the same
        # rule_id from the tmp corpus and leave it in place.
        await db_scoped.create_rule(_seed_rule(kept_rule_id))

        # A genuinely stale Rule: seeded under this project, absent from the
        # tmp corpus, ordinary provenance -> an ordinary reconcile prune target.
        await db_scoped.create_rule(_seed_rule(stale_rule_id))

        # An Abstraction + its ABSTRACTS edge to the kept Rule. Neither has a
        # markdown home in any corpus.
        await db_scoped.create_abstraction({
            "abstraction_id": abstraction_id,
            "summary": "cycle7 test abstraction",
            "domain": "testing",
            "compression_ratio": 0.5,
            "rule_count": 1,
            "project": _TEST_SCOPE,
        })
        await db_scoped.create_abstracts_edge(
            abstraction_id, kept_rule_id, project=_TEST_SCOPE
        )

        result = await reconcile(corpus, db_scoped, project=_TEST_SCOPE)

        assert stale_rule_id in result["deleted_nodes"], (
            f"stale rule {stale_rule_id!r} should be pruned; "
            f"deleted_nodes={result['deleted_nodes']!r}"
        )
        assert await db_scoped.get_rule(stale_rule_id) is None, (
            f"stale rule {stale_rule_id!r} must no longer exist after reconcile"
        )

        assert abstraction_id not in result["deleted_nodes"], (
            f"Abstraction {abstraction_id!r} must survive reconcile; "
            f"deleted_nodes={result['deleted_nodes']!r}"
        )
        assert ("ABSTRACTS", abstraction_id, kept_rule_id) not in result["deleted_edges"], (
            "the ABSTRACTS edge must survive reconcile; "
            f"deleted_edges={result['deleted_edges']!r}"
        )

        async with db_scoped._driver.session(database=db_scoped._database) as s:
            abs_rec = await (await s.run(
                "MATCH (a:Abstraction {abstraction_id: $id, project: $project}) "
                "RETURN count(a) AS c",
                id=abstraction_id, project=_TEST_SCOPE,
            )).single()
            edge_rec = await (await s.run(
                "MATCH (a:Abstraction {abstraction_id: $aid, project: $project})"
                "-[e:ABSTRACTS]->(r:Rule {rule_id: $rid, project: $project}) "
                "RETURN count(e) AS c",
                aid=abstraction_id, rid=kept_rule_id, project=_TEST_SCOPE,
            )).single()

        assert abs_rec["c"] == 1, (
            "the seeded Abstraction node must still exist in the graph after "
            "reconcile"
        )
        assert edge_rec["c"] == 1, (
            "the seeded ABSTRACTS edge must still exist in the graph after "
            "reconcile"
        )


# ---------------------------------------------------------------------------
# Fake driver for [cap-12] / [cap-13] -- deterministic, not a real-DB mock.
# Neither capability is marked real-graph in plan.md; detect_artifact_abstracts_parity
# is guard-shaped / pure over its inputs, so a fake that answers the two
# queries the method issues (Category count, ABSTRACTS pairs) is sufficient.
# ---------------------------------------------------------------------------

class _FakeCategoryCountResult:
    def __init__(self, count: int) -> None:
        self._count = count

    async def single(self) -> dict:
        return {"count": self._count}


class _FakeAbstractsPairsResult:
    def __init__(self, pairs: list[tuple[str, str]]) -> None:
        self._pairs = pairs

    def __aiter__(self) -> "_FakeAbstractsPairsResult":
        self._iter = iter(self._pairs)
        return self

    async def __anext__(self) -> dict:
        try:
            abstraction_id, rule_id = next(self._iter)
        except StopIteration:
            raise StopAsyncIteration
        return {"abstraction_id": abstraction_id, "rule_id": rule_id}


class _FakeSession:
    def __init__(self, category_count: int, abstracts_pairs: list[tuple[str, str]]) -> None:
        self._category_count = category_count
        self._abstracts_pairs = abstracts_pairs

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    async def run(self, query: str, **params):
        if "Category" in query:
            return _FakeCategoryCountResult(self._category_count)
        if "ABSTRACTS" in query:
            return _FakeAbstractsPairsResult(self._abstracts_pairs)
        raise AssertionError(f"unexpected query issued to the fake driver: {query!r}")


class _FakeDriver:
    """Deterministic stand-in for an AsyncDriver. Answers exactly the two
    queries detect_artifact_abstracts_parity issues (Category count, then
    ABSTRACTS pairs) and raises loudly on anything else, so a call that
    reaches a query branch it should have short-circuited past fails loudly
    rather than silently returning a wrong-but-plausible answer."""

    def __init__(self, category_count: int, abstracts_pairs: list[tuple[str, str]]) -> None:
        self._category_count = category_count
        self._abstracts_pairs = abstracts_pairs

    def session(self, database: str | None = None) -> _FakeSession:
        return _FakeSession(self._category_count, self._abstracts_pairs)


def _write_artifact(path: Path, abstractions: list[dict], project: str = "writ") -> Path:
    path.write_text(
        json.dumps({"project": project, "abstractions": abstractions}),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# [cap-12] both-directions parity: stale + missing in one result
# ---------------------------------------------------------------------------

class TestArtifactAbstractsParityBothDirections:
    """[cap-12] detect_artifact_abstracts_parity reports graph-only pairs as
    `stale` and artifact-only pairs as `missing`, in a single result dict."""

    @pytest.mark.asyncio
    async def test_reports_stale_and_missing_together(self, tmp_path: Path) -> None:
        artifact = _write_artifact(
            tmp_path / "abstractions.json",
            [{"abstraction_id": "ABS-A", "rule_ids": ["RULE-SHARED", "RULE-MISSING-1"]}],
        )
        # Live graph: RULE-SHARED matches the artifact; RULE-STALE-1 does not
        # appear in the artifact at all (graph-only -> stale). RULE-MISSING-1
        # is declared by the artifact but never appears live (artifact-only
        # -> missing).
        driver = _FakeDriver(
            category_count=1,
            abstracts_pairs=[("ABS-A", "RULE-SHARED"), ("ABS-A", "RULE-STALE-1")],
        )
        checker = IntegrityChecker(driver, "neo4j")

        result = await checker.detect_artifact_abstracts_parity(
            artifact_path=artifact, project="writ"
        )

        assert result is not None, "expected a non-None result: drift exists both ways"
        assert result["stale"] == [("ABS-A", "RULE-STALE-1")], (
            f"stale should be exactly the graph-only pair; got {result!r}"
        )
        assert result["missing"] == [("ABS-A", "RULE-MISSING-1")], (
            f"missing should be exactly the artifact-only pair; got {result!r}"
        )


# ---------------------------------------------------------------------------
# [cap-13] the three guards: artifact absent, driver None, zero Category nodes
# ---------------------------------------------------------------------------

class TestArtifactAbstractsParityGuards:
    """[cap-13] Same three guards as its sibling detect_artifact_dangling_rule_ids:
    artifact absent, driver absent, and zero Category nodes (the corpus-presence
    skip, so a crafted unit-test graph of a few rules does not report the
    entire artifact as missing) -- each returns None."""

    @pytest.mark.asyncio
    async def test_none_when_artifact_absent(self, tmp_path: Path) -> None:
        missing_artifact = tmp_path / "does-not-exist.json"
        driver = _FakeDriver(category_count=1, abstracts_pairs=[])
        checker = IntegrityChecker(driver, "neo4j")

        result = await checker.detect_artifact_abstracts_parity(
            artifact_path=missing_artifact, project="writ"
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_none_when_driver_is_none(self, tmp_path: Path) -> None:
        artifact = _write_artifact(
            tmp_path / "abstractions.json",
            [{"abstraction_id": "ABS-A", "rule_ids": ["RULE-A"]}],
        )
        checker = IntegrityChecker(None, "neo4j")

        result = await checker.detect_artifact_abstracts_parity(
            artifact_path=artifact, project="writ"
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_none_when_zero_category_nodes(self, tmp_path: Path) -> None:
        artifact = _write_artifact(
            tmp_path / "abstractions.json",
            [{"abstraction_id": "ABS-A", "rule_ids": ["RULE-A"]}],
        )
        # category_count=0 must short-circuit BEFORE the ABSTRACTS query; the
        # fake's run() raises AssertionError on any query besides Category
        # count / ABSTRACTS pairs are answered, and abstracts_pairs is left
        # empty here so even a code path that skipped the guard would report
        # every artifact pair as "missing" rather than None -- a clear signal
        # the guard did not fire, not a coincidental pass.
        driver = _FakeDriver(category_count=0, abstracts_pairs=[])
        checker = IntegrityChecker(driver, "neo4j")

        result = await checker.detect_artifact_abstracts_parity(
            artifact_path=artifact, project="writ"
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_none_when_the_layer_was_never_materialized(
        self, tmp_path: Path
    ) -> None:
        """The fourth guard, found by the full suite after the targeted runs
        were all green.

        The abstraction layer is materialized by finish_import, NOT by
        ingest_path, so the very common `clear_all()` + `ingest_path(bible)`
        fixture leaves 22 Category nodes (so the corpus-presence guard passes)
        and ZERO ABSTRACTS edges, while the artifact still declares its pairs.
        Without this guard the check reported the whole artifact as missing and
        GATED, which broke two pre-existing tests, one of them asserting exactly
        that a graph-only abstraction must not cause exit_code=1.

        An unmaterialized layer is absence, not drift.
        """
        artifact = _write_artifact(
            tmp_path / "abstractions.json",
            [{"abstraction_id": "ABS-A", "rule_ids": ["RULE-A", "RULE-B"]}],
        )
        # Categories present (corpus-presence guard passes), zero ABSTRACTS
        # edges. Before the fix this returned missing=[(ABS-A, RULE-A),
        # (ABS-A, RULE-B)] and gated.
        driver = _FakeDriver(category_count=22, abstracts_pairs=[])
        checker = IntegrityChecker(driver, "neo4j")

        result = await checker.detect_artifact_abstracts_parity(
            artifact_path=artifact, project="writ"
        )
        assert result is None, (
            "a never-materialized abstraction layer is absence, not drift; "
            f"got {result!r}"
        )

    @pytest.mark.asyncio
    async def test_partial_loss_still_reports(self, tmp_path: Path) -> None:
        """The guard's price, bounded. It can only hide a WHOLESALE loss,
        because any surviving edge puts the check back on the comparison path.
        Without this test the guard above could be widened to 'skip whenever
        anything is missing' and nothing would notice."""
        artifact = _write_artifact(
            tmp_path / "abstractions.json",
            [{"abstraction_id": "ABS-A", "rule_ids": ["RULE-A", "RULE-B"]}],
        )
        driver = _FakeDriver(
            category_count=22, abstracts_pairs=[("ABS-A", "RULE-A")]
        )
        checker = IntegrityChecker(driver, "neo4j")

        result = await checker.detect_artifact_abstracts_parity(
            artifact_path=artifact, project="writ"
        )
        assert result is not None, "one surviving edge must re-enable the check"
        assert result["missing"] == [("ABS-A", "RULE-B")]
        assert result["stale"] == []


# ---------------------------------------------------------------------------
# [cap-14] (real graph) zero drift against the live graph + committed artifact
# ---------------------------------------------------------------------------

class TestArtifactAbstractsParityLiveGraphZeroDrift:
    """[cap-14] Against the live graph and the committed bible/abstractions.json,
    detect_artifact_abstracts_parity returns None -- the measured post-fix
    state (62 Abstraction nodes, 186 ABSTRACTS edges, zero drift in both
    directions)."""

    @pytest.mark.asyncio
    async def test_returns_none_against_live_graph_and_committed_artifact(
        self, live_db: Neo4jConnection
    ) -> None:
        from writ.compression.abstractions import DEFAULT_ABSTRACTIONS_ARTIFACT

        if not DEFAULT_ABSTRACTIONS_ARTIFACT.exists():
            pytest.skip(
                "requires the untracked bible/abstractions.json artifact "
                "(regenerate with `writ compress`)"
            )

        await _require_intact_corpus(live_db)

        checker = IntegrityChecker(live_db._driver, live_db._database)
        result = await checker.detect_artifact_abstracts_parity(project="writ")

        assert result is None, (
            "the read-only probe measured zero drift in both directions "
            f"between the live graph and bible/abstractions.json; got {result}"
        )
