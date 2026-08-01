"""0.10 (prop completion): reconcile clears stale MANAGED props + detect_prop_parity.

The upsert-only no-prune gap, PROPERTY edition. `SET n += props` aligns values but
never clears a prop ABSENT from the new source, so removing a frontmatter field
leaves the old value in the graph forever -- and `writ validate` never sees it (it
checks node/edge existence, not per-node prop presence). This forced the manual
`SET action_triggers = null` in 1.8b.

Fix (two halves, mirroring 0.10's reconcile + detect_edge_parity):
- reconcile() now clears stale MANAGED props -- present-in-live, absent-from-source,
  on non-graph-authored nodes. MANAGED = (all node models' fields) - RUNTIME_EXEMPT.
- detect_prop_parity() surfaces the same drift in `writ validate`.

Boundary (the decision): RUNTIME props the daemon writes (times_seen_positive/
negative, last_seen, source_origin) are NEVER touched -- the allowlist protects
them (and any non-model prop, e.g. an embedding). authority/confidence are MANAGED:
source-of-truth-at-rest is absolute (no per-field blind spot); both are universally
source-declared, and Phase 5 graduation exports promotions to source.

The corpus fixture is MODULE-scoped (ingest ONCE) and mutation tests snapshot ->
mutate -> restore, so the shared Neo4j is not churned by 9x clear_all+ingest cycles
(which trigger transient EntityNotFound under contention).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
from writ.graph.db import Neo4jConnection
from writ.graph.integrity import IntegrityChecker
from writ.graph.methodology_ingest import ingest_path, reconcile
from writ.graph.schema import MANAGED_PROP_NAMES

from tests._bible_guard import requires_bible

pytestmark = requires_bible

BIBLE = Path(__file__).resolve().parent.parent / "bible"

# A real Rule node. Rules never declare `action_triggers` in source (it is a
# _MethodologyNodeBase field), so a graph action_triggers on a Rule is a stale
# MANAGED prop -- the exact dead-tag class 1.8b hit with META-AUTH.
REAL_RULE = "SEC-INJ-SQL-001"


async def _get_prop(db: Neo4jConnection, node_id: str, prop: str):
    async with db._driver.session(database=db._database) as s:
        r = await s.run(f"MATCH (n) WHERE n.rule_id = $id RETURN n.{prop} AS v", id=node_id)
        rec = await r.single()
        return rec["v"] if rec else None


async def _set_prop(db: Neo4jConnection, node_id: str, prop: str, value) -> None:
    async with db._driver.session(database=db._database) as s:
        await s.run(f"MATCH (n) WHERE n.rule_id = $id SET n.{prop} = $v", id=node_id, v=value)


async def _snapshot(db: Neo4jConnection, node_id: str, *keys: str) -> dict:
    cols = ", ".join(f"n.{k} AS {k}" for k in keys)
    async with db._driver.session(database=db._database) as s:
        r = await s.run(f"MATCH (n) WHERE n.rule_id = $id RETURN {cols}", id=node_id)
        rec = await r.single()
        return {k: (rec[k] if rec else None) for k in keys}


async def _restore(db: Neo4jConnection, node_id: str, snap: dict) -> None:
    # MATCH-guarded SET (no-op if the node is absent -> never raises EntityNotFound);
    # SET prop = null removes it, so a None snapshot restores exactly to absent.
    sets = ", ".join(f"n.{k} = ${k}" for k in snap)
    async with db._driver.session(database=db._database) as s:
        await s.run(f"MATCH (n) WHERE n.rule_id = $id SET {sets}", id=node_id, **snap)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
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
    # Leave the shared graph canonical for the rest of the suite.
    await db.clear_all()
    await ingest_path(BIBLE, db)
    await db.close()


class TestManagedPropBoundary:
    """The allowlist split is the whole safety story -- lock it explicitly (no DB)."""

    def test_managed_includes_content_props(self) -> None:
        for p in ("authority", "confidence", "action_triggers", "floor_modes",
                  "trigger_keywords", "tags", "severity"):
            assert p in MANAGED_PROP_NAMES, f"{p} should be a managed (source) prop"

    def test_managed_excludes_runtime_props(self) -> None:
        # Daemon-written observation/provenance props must never be reconcile-cleared.
        for p in ("times_seen_positive", "times_seen_negative", "last_seen", "source_origin"):
            assert p not in MANAGED_PROP_NAMES, f"{p} must be RUNTIME-exempt, not managed"


class TestReconcileClearsProps:
    pytestmark = pytest.mark.asyncio(loop_scope="module")

    async def test_clears_stale_managed_prop(self, db_corpus: Neo4jConnection) -> None:
        # A MANAGED prop present in the graph but absent from the node's source
        # must be cleared by reconcile (the upsert alone cannot remove it).
        snap = await _snapshot(db_corpus, REAL_RULE, "action_triggers")
        try:
            await _set_prop(db_corpus, REAL_RULE, "action_triggers", ["zzz-stale"])
            assert await _get_prop(db_corpus, REAL_RULE, "action_triggers") == ["zzz-stale"]
            result = await reconcile(BIBLE, db_corpus)
            assert await _get_prop(db_corpus, REAL_RULE, "action_triggers") is None
            assert REAL_RULE in result.get("cleared_props", {})
            assert "action_triggers" in result["cleared_props"][REAL_RULE]
        finally:
            await _restore(db_corpus, REAL_RULE, snap)

    async def test_clearing_step_exempts_runtime_props(self, db_corpus: Neo4jConnection) -> None:
        # SAFETY: the prop-CLEARING step must never target a runtime/observation prop
        # -- they are exempt from MANAGED_PROP_NAMES, so reconcile never reports them
        # in cleared_props. (The upsert separately re-initializes frequency counters
        # to 0 on every ingest -- a pre-existing, intentional parser setdefault
        # (ingest.py:94); that is Phase-5 frequency-PERSISTENCE territory, not the
        # stale-prop pruning this work adds. All frequency readers coalesce absent->0.)
        snap = await _snapshot(db_corpus, REAL_RULE, "times_seen_positive")
        try:
            await _set_prop(db_corpus, REAL_RULE, "times_seen_positive", 42)
            result = await reconcile(BIBLE, db_corpus)
            cleared = result.get("cleared_props", {}).get(REAL_RULE, [])
            assert "times_seen_positive" not in cleared
            assert "times_seen_negative" not in cleared
            assert "last_seen" not in cleared
        finally:
            await _restore(db_corpus, REAL_RULE, snap)

    async def test_idempotent_on_clean_corpus(self, db_corpus: Neo4jConnection) -> None:
        # A freshly-ingested corpus has no stale managed props -> reconcile clears none.
        result = await reconcile(BIBLE, db_corpus)
        assert result.get("cleared_props", {}) == {}


class TestPropParity:
    pytestmark = pytest.mark.asyncio(loop_scope="module")

    async def test_clean_corpus_in_parity(self, db_corpus: Neo4jConnection) -> None:
        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
        assert await checker.detect_prop_parity(BIBLE) is None

    async def test_flags_stale_managed_prop(self, db_corpus: Neo4jConnection) -> None:
        snap = await _snapshot(db_corpus, REAL_RULE, "action_triggers")
        try:
            await _set_prop(db_corpus, REAL_RULE, "action_triggers", ["zzz-stale"])
            checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
            result = await checker.detect_prop_parity(BIBLE)
            assert result is not None
            assert "action_triggers" in result.get(REAL_RULE, [])
        finally:
            await _restore(db_corpus, REAL_RULE, snap)

    async def test_stale_prop_fails_run_all_checks(self, db_corpus: Neo4jConnection) -> None:
        # 5.2c coverage-map row 4: promote the detector-level seed to the full
        # seed -> run_all_checks() -> exit_code==1 standard (prop_parity routes into
        # has_issues at integrity.py:has_issues block).
        snap = await _snapshot(db_corpus, REAL_RULE, "action_triggers")
        try:
            await _set_prop(db_corpus, REAL_RULE, "action_triggers", ["zzz-stale"])
            checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
            findings = await checker.run_all_checks(bible_dir=BIBLE, skip_redundancy=True)
            assert findings["exit_code"] == 1
            assert findings["prop_parity"] is not None
        finally:
            await _restore(db_corpus, REAL_RULE, snap)

    async def test_ignores_runtime_prop(self, db_corpus: Neo4jConnection) -> None:
        # A runtime counter present-in-live-absent-from-source must NOT be flagged.
        snap = await _snapshot(db_corpus, REAL_RULE, "times_seen_positive")
        try:
            await _set_prop(db_corpus, REAL_RULE, "times_seen_positive", 7)
            checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
            result = await checker.detect_prop_parity(BIBLE) or {}
            assert "times_seen_positive" not in result.get(REAL_RULE, [])
        finally:
            await _restore(db_corpus, REAL_RULE, snap)

    async def test_reconcile_restores_prop_parity(self, db_corpus: Neo4jConnection) -> None:
        snap = await _snapshot(db_corpus, REAL_RULE, "action_triggers")
        try:
            await _set_prop(db_corpus, REAL_RULE, "action_triggers", ["zzz-stale"])
            checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
            assert await checker.detect_prop_parity(BIBLE) is not None  # drift present
            await reconcile(BIBLE, db_corpus)
            assert await checker.detect_prop_parity(BIBLE) is None       # restored
        finally:
            await _restore(db_corpus, REAL_RULE, snap)
