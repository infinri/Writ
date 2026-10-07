"""Program item 6, workstream G: supersede as a derived, graph-only flag (capabilities 12-17).

An authored `SUPERSEDES` edge runs from the REPLACEMENT to the deprecated rule, so the
TARGET is superseded. `superseded` is derived from the incoming edges by one statement
(refresh_superseded_flags) after the edge state it reads is final, and every reader then
drops the flagged rule: the ranked pool, the BM25 build, /always-on, the corpus-footprint
replica, the lockstep integrity checks and detect_redundant.

Capability map
  Cap 12 -- ingest sets the flag on the target; removing the declaration and running
            reconcile clears it (real graph)
  Cap 13 -- writ add / writ edit creating a SUPERSEDES edge refreshes the flag before the
            command exits; any other edge type does not run the refresh (real graph, with
            the pipeline and export collaborators stubbed)
  Cap 14 -- the pipeline pool, the BM25 build and /always-on exclude a superseded rule,
            including a superseded mandatory one (real graph for pool and route)
  Cap 15 -- the lockstep integrity checks (real graph)
  Cap 16 -- the BM25 key moves with the flag: pinned in tests/test_bm25_persistence.py
  Cap 17 -- always_on_bundle_cost and detect_redundant skip superseded rules

Real-graph claims use project "test-trust-sup" and never wipe the corpus. The CLI tests
write two throwaway rules into the default project and delete them again.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from typer.testing import CliRunner

from tests._graph import connection
from tests.fixtures.server_routes import always_on, route_db  # noqa: F401  (fixtures)

PROJECT = "test-trust-sup"

_BODY = """
### Trigger
When {rid} applies.

### Statement
Statement for {rid}.

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
Rationale for {rid}.
"""


def _rule_md(rid: str, *, mandatory: bool = False, edges: tuple[str, ...] = ()) -> str:
    edge_block = ("\n### Edges\n" + "\n".join(f"- {e}" for e in edges) + "\n") if edges else ""
    mand = "**Mandatory**: true\n" if mandatory else ""
    return (
        f"<!-- RULE START: {rid} -->\n## Rule {rid}\n\n"
        f"**Domain**: security\n**Severity**: Medium\n**Scope**: Component\n{mand}"
        + _BODY.format(rid=rid) + edge_block + f"\n<!-- RULE END: {rid} -->\n"
    )


def _bible(tmp_path: Path, *blocks: str) -> Path:
    d = tmp_path / "bible"
    d.mkdir(parents=True, exist_ok=True)
    (d / "rules.md").write_text("\n".join(blocks), encoding="utf-8")
    return d


@pytest_asyncio.fixture()
async def db():
    conn = connection()
    try:
        async with conn._driver.session(database=conn._database) as s:
            await (await s.run("RETURN 1 AS ok")).consume()
    except Exception:
        await conn.close()
        pytest.skip("isolated test Neo4j (bolt://localhost:7688) unreachable; run "
                    "`bash scripts/test-graph.sh up`")
    await conn.clear_project(PROJECT)
    yield conn
    await conn.clear_project(PROJECT)
    await conn.close()


async def _flag(db, rule_id: str, project: str = PROJECT) -> bool:
    """The superseded flag as every reader sees it (a missing prop reads false)."""
    rows = list(await db._run(
        "MATCH (r:Rule {rule_id: $id, project: $p}) "
        "RETURN coalesce(r.superseded, false) AS s", id=rule_id, p=project))
    assert rows, f"rule {rule_id} not in the graph"
    return rows[0]["s"]


async def _raw_flag(db, rule_id: str, project: str = PROJECT):
    rows = list(await db._run(
        "MATCH (r:Rule {rule_id: $id, project: $p}) RETURN r.superseded AS s",
        id=rule_id, p=project))
    return rows[0]["s"]


# ---------------------------------------------------------------------------
# Capability 12: derivation on ingest and reconcile
# ---------------------------------------------------------------------------


class TestSupersededDerivedOnIngestAndReconcile:
    @pytest.mark.asyncio
    async def test_the_target_of_a_supersedes_edge_is_flagged_and_the_source_is_not(
        self, db, tmp_path: Path
    ) -> None:
        from writ.graph.methodology_ingest import ingest_path

        bible = _bible(tmp_path, _rule_md("TS-A-001", edges=("SUPERSEDES: TS-B-001",)),
                       _rule_md("TS-B-001"))
        await ingest_path(bible, db, project=PROJECT)
        assert await _flag(db, "TS-B-001") is True
        assert await _flag(db, "TS-A-001") is False

    @pytest.mark.asyncio
    async def test_the_edge_runs_from_the_replacement_to_the_deprecated_rule(
        self, db, tmp_path: Path
    ) -> None:
        from writ.graph.methodology_ingest import ingest_path

        bible = _bible(tmp_path, _rule_md("TS-D-001", edges=("SUPERSEDES: TS-D-002",)),
                       _rule_md("TS-D-002"))
        await ingest_path(bible, db, project=PROJECT)
        rows = list(await db._run(
            "MATCH (a:Rule {rule_id: 'TS-D-001'})-[:SUPERSEDES]->(b:Rule {rule_id: 'TS-D-002'}) "
            "RETURN count(*) AS c"))
        assert rows[0]["c"] == 1

    @pytest.mark.asyncio
    async def test_a_rule_nobody_supersedes_carries_no_flag_value_of_true(
        self, db, tmp_path: Path
    ) -> None:
        from writ.graph.methodology_ingest import ingest_path

        bible = _bible(tmp_path, _rule_md("TS-N-001"), _rule_md("TS-N-002"))
        await ingest_path(bible, db, project=PROJECT)
        assert await _flag(db, "TS-N-001") is False
        assert await _raw_flag(db, "TS-N-001") in (None, False)

    @pytest.mark.asyncio
    async def test_removing_the_declaration_and_running_reconcile_clears_the_flag(
        self, db, tmp_path: Path
    ) -> None:
        from writ.graph.methodology_ingest import ingest_path, reconcile

        bible = _bible(tmp_path, _rule_md("TS-R-001", edges=("SUPERSEDES: TS-R-002",)),
                       _rule_md("TS-R-002"))
        await ingest_path(bible, db, project=PROJECT)
        assert await _flag(db, "TS-R-002") is True

        _bible(tmp_path, _rule_md("TS-R-001"), _rule_md("TS-R-002"))
        result = await reconcile(bible, db, project=PROJECT)

        assert ("SUPERSEDES", "TS-R-001", "TS-R-002") in {
            tuple(e) for e in result["deleted_edges"]}, result["deleted_edges"]
        assert await _flag(db, "TS-R-002") is False, "the flag must follow the pruned edge"

    @pytest.mark.asyncio
    async def test_reconcile_keeps_the_flag_while_the_declaration_stands(
        self, db, tmp_path: Path
    ) -> None:
        from writ.graph.methodology_ingest import ingest_path, reconcile

        bible = _bible(tmp_path, _rule_md("TS-K-001", edges=("SUPERSEDES: TS-K-002",)),
                       _rule_md("TS-K-002"))
        await ingest_path(bible, db, project=PROJECT)
        await reconcile(bible, db, project=PROJECT)
        assert await _flag(db, "TS-K-002") is True

    @pytest.mark.asyncio
    async def test_a_rule_superseded_twice_stays_flagged_when_one_edge_goes(
        self, db, tmp_path: Path
    ) -> None:
        from writ.graph.methodology_ingest import ingest_path, reconcile

        bible = _bible(
            tmp_path,
            _rule_md("TS-M-001", edges=("SUPERSEDES: TS-M-003",)),
            _rule_md("TS-M-002", edges=("SUPERSEDES: TS-M-003",)),
            _rule_md("TS-M-003"))
        await ingest_path(bible, db, project=PROJECT)
        _bible(tmp_path, _rule_md("TS-M-001", edges=("SUPERSEDES: TS-M-003",)),
               _rule_md("TS-M-002"), _rule_md("TS-M-003"))
        await reconcile(bible, db, project=PROJECT)
        assert await _flag(db, "TS-M-003") is True

    @pytest.mark.asyncio
    async def test_refresh_superseded_flags_is_idempotent_and_project_scoped(self, db) -> None:
        other = PROJECT + "-other"
        for rid, proj in (("TS-S-001", PROJECT), ("TS-S-002", PROJECT), ("TS-S-003", other)):
            await db.create_rule(_rule_dict(rid, proj))
        await db.create_edge("SUPERSEDES", "TS-S-001", "TS-S-002", project=PROJECT)
        try:
            await db.refresh_superseded_flags(PROJECT)
            await db.refresh_superseded_flags(PROJECT)
            assert await _flag(db, "TS-S-002") is True
            assert await _flag(db, "TS-S-001") is False
            assert await _flag(db, "TS-S-003", other) is False
        finally:
            await db.clear_project(other)


class TestRefreshIsCalledByTheIngestPaths:
    @pytest.mark.asyncio
    async def test_one_live_ingest_refreshes_exactly_once_and_a_dry_run_never(
        self, db, tmp_path: Path, monkeypatch
    ) -> None:
        from writ.graph.db import Neo4jConnection
        from writ.graph.methodology_ingest import ingest_path

        calls: list[tuple] = []
        original = Neo4jConnection.refresh_superseded_flags

        async def spy(self, *args, **kwargs):
            calls.append(args)
            return await original(self, *args, **kwargs)

        monkeypatch.setattr(Neo4jConnection, "refresh_superseded_flags", spy)
        bible = _bible(tmp_path, _rule_md("TS-C-001"))

        await ingest_path(bible, db, project=PROJECT, dry_run=True)
        assert calls == [], "a dry run must not refresh"
        await ingest_path(bible, db, project=PROJECT)
        assert len(calls) == 1, calls

    @pytest.mark.asyncio
    async def test_reconcile_refreshes_again_after_the_edge_prune(
        self, db, tmp_path: Path, monkeypatch
    ) -> None:
        from writ.graph.db import Neo4jConnection
        from writ.graph.methodology_ingest import reconcile

        calls: list[tuple] = []
        original = Neo4jConnection.refresh_superseded_flags

        async def spy(self, *args, **kwargs):
            calls.append(args)
            return await original(self, *args, **kwargs)

        monkeypatch.setattr(Neo4jConnection, "refresh_superseded_flags", spy)
        bible = _bible(tmp_path, _rule_md("TS-C-010"))
        await reconcile(bible, db, project=PROJECT)
        assert len(calls) == 2, "one inside ingest_path and one after the edge prune"


# ---------------------------------------------------------------------------
# Capability 13: writ add / writ edit
# ---------------------------------------------------------------------------

runner = CliRunner()
_ADD_PROMPTS = ["Testing", "high", "slice", "t", "s", "v", "p", "e", "r"]


def _rule_dict(rule_id: str, project: str = "writ") -> dict:
    return {
        "rule_id": rule_id, "domain": "Testing", "severity": "high", "scope": "slice",
        "trigger": "t", "statement": "s", "violation": "v", "pass_example": "p",
        "enforcement": "e", "rationale": "r", "last_validated": "2026-03-15",
        "project": project,
    }


@pytest_asyncio.fixture()
async def cli_rules(monkeypatch, tmp_path: Path):
    """Two default-project rules, the pipeline and export collaborators stubbed so the CLI
    runs against the real graph without building embeddings or writing the bible."""
    import writ.authoring as authoring
    import writ.retrieval.pipeline as pipeline_mod

    conn = connection()
    try:
        async with conn._driver.session(database=conn._database) as s:
            await (await s.run("RETURN 1 AS ok")).consume()
    except Exception:
        await conn.close()
        pytest.skip("isolated test Neo4j (bolt://localhost:7688) unreachable")
    ids = ("TS-CLI-OLD-001", "TS-CLI-NEW-001", "TS-CLI-EDIT-001")
    for rid in ids:
        await conn.delete_rule(rid)
    await conn.create_rule(_rule_dict("TS-CLI-OLD-001"))
    await conn.create_rule(_rule_dict("TS-CLI-EDIT-001"))

    monkeypatch.setattr(pipeline_mod, "build_pipeline", AsyncMock(return_value=object()))
    monkeypatch.setattr(authoring, "check_redundancy", lambda *a, **k: [])
    monkeypatch.setattr(
        authoring, "suggest_relationships",
        lambda *a, **k: [{"rule_id": "TS-CLI-OLD-001", "score": 0.9, "statement": "old"}])
    monkeypatch.setattr(
        authoring, "finalize_conflict_and_export",
        AsyncMock(return_value={"conflicts": [], "rules_exported": 0,
                                "export_dir": str(tmp_path)}))
    yield conn
    for rid in ids:
        await conn.delete_rule(rid)
    await conn.close()


def _spy_refresh(monkeypatch) -> list[tuple]:
    from writ.graph.db import Neo4jConnection

    calls: list[tuple] = []
    original = Neo4jConnection.refresh_superseded_flags

    async def spy(self, *args, **kwargs):
        calls.append(args)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(Neo4jConnection, "refresh_superseded_flags", spy)
    return calls


class TestWritAddAndEditRefreshOnSupersedes:
    @pytest.mark.asyncio
    async def test_add_with_a_supersedes_edge_flags_the_target_before_exit(
        self, cli_rules, monkeypatch
    ) -> None:
        from writ import cli

        calls = _spy_refresh(monkeypatch)
        feed = "\n".join(["TS-CLI-NEW-001", *_ADD_PROMPTS, "y", "SUPERSEDES"]) + "\n"
        result = await _invoke(cli.app, ["add"], feed)
        assert result.exit_code == 0, result.output
        assert len(calls) == 1, "a SUPERSEDES edge must run the refresh exactly once"
        assert await _flag(cli_rules, "TS-CLI-OLD-001", "writ") is True
        assert await _flag(cli_rules, "TS-CLI-NEW-001", "writ") is False

    @pytest.mark.asyncio
    async def test_add_with_any_other_edge_type_does_not_run_the_refresh(
        self, cli_rules, monkeypatch
    ) -> None:
        from writ import cli

        calls = _spy_refresh(monkeypatch)
        feed = "\n".join(["TS-CLI-NEW-001", *_ADD_PROMPTS, "y", "RELATED_TO"]) + "\n"
        result = await _invoke(cli.app, ["add"], feed)
        assert result.exit_code == 0, result.output
        assert calls == []
        assert await _flag(cli_rules, "TS-CLI-OLD-001", "writ") is False

    @pytest.mark.asyncio
    async def test_add_with_no_edge_accepted_does_not_run_the_refresh(
        self, cli_rules, monkeypatch
    ) -> None:
        from writ import cli

        calls = _spy_refresh(monkeypatch)
        feed = "\n".join(["TS-CLI-NEW-001", *_ADD_PROMPTS, "n"]) + "\n"
        result = await _invoke(cli.app, ["add"], feed)
        assert result.exit_code == 0, result.output
        assert calls == []

    @pytest.mark.asyncio
    async def test_edit_with_a_supersedes_edge_flags_the_target_before_exit(
        self, cli_rules, monkeypatch
    ) -> None:
        from writ import cli

        calls = _spy_refresh(monkeypatch)
        feed = "\n" * 9 + "y\nSUPERSEDES\n"
        result = await _invoke(cli.app, ["edit", "TS-CLI-EDIT-001"], feed)
        assert result.exit_code == 0, result.output
        assert len(calls) == 1
        assert await _flag(cli_rules, "TS-CLI-OLD-001", "writ") is True
        assert await _flag(cli_rules, "TS-CLI-EDIT-001", "writ") is False

    @pytest.mark.asyncio
    async def test_edit_with_any_other_edge_type_does_not_run_the_refresh(
        self, cli_rules, monkeypatch
    ) -> None:
        from writ import cli

        calls = _spy_refresh(monkeypatch)
        feed = "\n" * 9 + "y\nDEPENDS_ON\n"
        result = await _invoke(cli.app, ["edit", "TS-CLI-EDIT-001"], feed)
        assert result.exit_code == 0, result.output
        assert calls == []


async def _invoke(app, args: list[str], feed: str):
    """CliRunner.invoke runs asyncio.run inside the command, so it must leave this loop."""
    import asyncio

    return await asyncio.get_running_loop().run_in_executor(
        None, lambda: runner.invoke(app, args, input=feed))


# ---------------------------------------------------------------------------
# Capability 14: exclusion from the pool, the BM25 build and /always-on
# ---------------------------------------------------------------------------


class TestSupersededRuleLeavesTheRankedPool:
    @pytest.mark.asyncio
    async def test_the_candidate_pool_drops_a_superseded_rule_and_keeps_the_rest(
        self, db, tmp_path: Path
    ) -> None:
        from writ.graph.methodology_ingest import ingest_path
        from writ.retrieval.pipeline import _load_candidates

        bible = _bible(tmp_path, _rule_md("TS-P-001", edges=("SUPERSEDES: TS-P-002",)),
                       _rule_md("TS-P-002"), _rule_md("TS-P-003"))
        await ingest_path(bible, db, project=PROJECT)
        candidates, metadata = await _load_candidates(db)
        ids = {c["rule_id"] for c in candidates}
        assert "TS-P-002" not in ids, "a superseded rule must not be a ranking candidate"
        assert {"TS-P-001", "TS-P-003"} <= ids
        assert "TS-P-002" not in metadata

    @pytest.mark.asyncio
    async def test_a_rule_returns_to_the_pool_when_it_is_no_longer_superseded(
        self, db, tmp_path: Path
    ) -> None:
        from writ.graph.methodology_ingest import ingest_path, reconcile
        from writ.retrieval.pipeline import _load_candidates

        bible = _bible(tmp_path, _rule_md("TS-P-010", edges=("SUPERSEDES: TS-P-011",)),
                       _rule_md("TS-P-011"))
        await ingest_path(bible, db, project=PROJECT)
        _bible(tmp_path, _rule_md("TS-P-010"), _rule_md("TS-P-011"))
        await reconcile(bible, db, project=PROJECT)
        candidates, _ = await _load_candidates(db)
        assert "TS-P-011" in {c["rule_id"] for c in candidates}


class TestSupersededRuleLeavesTheBm25Build:
    def _rules(self) -> list[dict]:
        return [
            {"rule_id": "TS-BM-LIVE", "trigger": "parameterized queries", "statement": "live",
             "tags": "", "mandatory": False},
            {"rule_id": "TS-BM-OLD", "trigger": "parameterized queries", "statement": "old",
             "tags": "", "mandatory": False, "superseded": True},
            {"rule_id": "TS-BM-OLDMAND", "trigger": "parameterized queries", "statement": "m",
             "tags": "", "mandatory": True, "superseded": True},
        ]

    def test_build_skips_superseded_and_mandatory_rules(self) -> None:
        from writ.retrieval.keyword import KeywordIndex

        index = KeywordIndex()
        assert index.build(self._rules()) == 1
        hits = {r["rule_id"] for r in index.search("parameterized queries", limit=10)}
        assert hits == {"TS-BM-LIVE"}

    def test_an_explicit_false_flag_is_indexed(self) -> None:
        from writ.retrieval.keyword import KeywordIndex

        rules = self._rules()
        rules[1]["superseded"] = False
        index = KeywordIndex()
        assert index.build(rules) == 2


class TestSupersededRuleLeavesAlwaysOn:
    @pytest.mark.asyncio
    async def test_a_superseded_mandatory_rule_is_absent_from_the_route(
        self, always_on, db, tmp_path: Path
    ) -> None:
        from writ.graph.methodology_ingest import ingest_path

        bible = _bible(
            tmp_path,
            _rule_md("TS-AO-NEW-001", mandatory=True, edges=("SUPERSEDES: TS-AO-OLD-001",)),
            _rule_md("TS-AO-OLD-001", mandatory=True),
            _rule_md("TS-AO-KEEP-001", mandatory=True))
        await ingest_path(bible, db, project=PROJECT)
        data = await always_on()
        ids = {r["rule_id"] for r in data["rules"]}
        assert "TS-AO-OLD-001" not in ids, "a superseded mandatory rule must leave the floor"
        assert {"TS-AO-NEW-001", "TS-AO-KEEP-001"} <= ids

    def test_both_predicates_carry_the_not_superseded_clause_fully_parenthesized(self) -> None:
        from writ.graph.predicates import INJECTION_RULE_WHERE, RANKED_INCLUDE_WHERE

        for predicate in (INJECTION_RULE_WHERE, RANKED_INCLUDE_WHERE):
            assert "coalesce(r.superseded, false) = false" in predicate
            assert predicate.startswith("(") and predicate.endswith(")"), (
                "an unparenthesized OR would let a superseded rule through when the predicate "
                "is embedded in a larger WHERE")
        assert "r.mandatory = true" in INJECTION_RULE_WHERE
        assert "r.always_on = true" in INJECTION_RULE_WHERE
        assert "r.mandatory IS NULL" in RANKED_INCLUDE_WHERE


# ---------------------------------------------------------------------------
# Capability 15: the lockstep integrity checks
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def supersede_graph(db, tmp_path: Path):
    """A superseded mandatory rule, a superseded plain rule, and their replacements."""
    from writ.graph.methodology_ingest import ingest_path

    bible = _bible(
        tmp_path,
        _rule_md("TS-IC-NEWM-001", mandatory=True, edges=("SUPERSEDES: TS-IC-OLDM-001",)),
        _rule_md("TS-IC-OLDM-001", mandatory=True),
        _rule_md("TS-IC-NEW-001", edges=("SUPERSEDES: TS-IC-OLD-001",)),
        _rule_md("TS-IC-OLD-001"))
    await ingest_path(bible, db, project=PROJECT)
    yield db


class TestIntegrityChecksStayInLockstep:
    @pytest.mark.asyncio
    async def test_mandatory_rule_ids_omits_a_superseded_mandatory_rule(
        self, supersede_graph
    ) -> None:
        from writ.graph.integrity.frequency_checks import mandatory_rule_ids

        db = supersede_graph
        async with db._driver.session(database=db._database) as session:
            ids = await mandatory_rule_ids(session)
        assert "TS-IC-NEWM-001" in ids
        assert "TS-IC-OLDM-001" not in ids

    @pytest.mark.asyncio
    async def test_superseded_rule_ids_lists_every_flagged_rule_and_only_those(
        self, supersede_graph
    ) -> None:
        from writ.graph.integrity.frequency_checks import superseded_rule_ids

        db = supersede_graph
        async with db._driver.session(database=db._database) as session:
            ids = await superseded_rule_ids(session)
        assert {"TS-IC-OLDM-001", "TS-IC-OLD-001"} <= ids
        assert not ({"TS-IC-NEWM-001", "TS-IC-NEW-001"} & ids)

    @pytest.mark.asyncio
    async def test_the_exclusion_mismatch_check_holds_on_a_corpus_with_superseded_rules(
        self, supersede_graph
    ) -> None:
        from writ.graph.integrity import IntegrityChecker

        db = supersede_graph
        checker = IntegrityChecker(db._driver, db._database)
        assert await checker.detect_ranked_exclusion_mismatch() is None

    @pytest.mark.asyncio
    async def test_the_exclusion_mismatch_check_still_reports_an_unexplained_exclusion(
        self, supersede_graph
    ) -> None:
        from writ.graph.integrity import IntegrityChecker
        from writ.graph.predicates import RANKED_INCLUDE_WHERE

        db = supersede_graph
        checker = IntegrityChecker(db._driver, db._database)
        narrowed = f"({RANKED_INCLUDE_WHERE} AND r.rule_id <> 'TS-IC-NEW-001')"
        report = await checker.detect_ranked_exclusion_mismatch(ranked_include_where=narrowed)
        assert report is not None
        assert set(report) == {"excluded_not_mandatory", "mandatory_not_excluded"}
        assert "TS-IC-NEW-001" in report["excluded_not_mandatory"]
        assert "TS-IC-OLD-001" not in report["excluded_not_mandatory"], (
            "a superseded rule is an explained exclusion")

    @pytest.mark.asyncio
    async def test_the_check_reports_a_superseded_rule_the_pool_still_ranks(
        self, supersede_graph
    ) -> None:
        # A pool predicate that forgot the clause leaves a superseded rule ranked: that is
        # the drift the widened check exists to catch.
        from writ.graph.integrity import IntegrityChecker

        db = supersede_graph
        checker = IntegrityChecker(db._driver, db._database)
        stale_predicate = "(r.mandatory IS NULL OR r.mandatory = false)"
        report = await checker.detect_ranked_exclusion_mismatch(
            ranked_include_where=stale_predicate)
        assert report is not None
        drift = set(report["mandatory_not_excluded"]) | set(report["excluded_not_mandatory"])
        assert "TS-IC-OLD-001" in drift, report

    @pytest.mark.asyncio
    async def test_detect_stranded_mandatory_does_not_report_a_superseded_mandatory_rule(
        self, supersede_graph
    ) -> None:
        from writ.graph.integrity import IntegrityChecker

        db = supersede_graph
        checker = IntegrityChecker(db._driver, db._database)
        stranded = await checker.detect_stranded_mandatory()
        assert "TS-IC-OLDM-001" not in stranded
        assert "TS-IC-NEWM-001" not in stranded, "the live mandatory rule is on the floor"


# ---------------------------------------------------------------------------
# Capability 17: the corpus-footprint replica and detect_redundant
# ---------------------------------------------------------------------------


class TestAlwaysOnBundleCostSkipsSuperseded:
    def test_a_superseded_rule_is_not_counted_or_costed(self) -> None:
        from writ.analysis.corpus_footprint import always_on_bundle_cost

        live = {"rule_id": "A", "mandatory": True, "trigger": "when a", "statement": "do a"}
        old = {"rule_id": "B", "mandatory": True, "trigger": "when b" * 40,
               "statement": "do b" * 40, "superseded": True}
        old_always = {"rule_id": "C", "always_on": True, "trigger": "when c",
                      "statement": "do c", "superseded": True}
        with_old = always_on_bundle_cost([live, old, old_always])
        without = always_on_bundle_cost([live])
        assert with_old["rule_count"] == 1
        assert with_old["tokens_floor_est"] == without["tokens_floor_est"]

    def test_an_explicit_false_flag_still_counts(self) -> None:
        from writ.analysis.corpus_footprint import always_on_bundle_cost

        rules = [{"rule_id": "A", "mandatory": True, "trigger": "t", "statement": "s",
                  "superseded": False},
                 {"rule_id": "B", "always_on": True, "trigger": "t", "statement": "s"}]
        assert always_on_bundle_cost(rules)["rule_count"] == 2


class TestDetectRedundantSkipsSuperseded:
    @pytest.fixture()
    def fake_embedder(self, monkeypatch):
        """sentence-transformers is an optional extra that CI does not install. The stand-in
        embeds every text carrying the sentinel identically and every other text uniquely,
        so only the sentinel rules can ever be reported as near-identical."""
        import numpy as np

        class FakeModel:
            def __init__(self, *_a, **_k) -> None:
                pass

            def encode(self, texts, normalize_embeddings=True):
                n = len(texts)
                out = np.zeros((n, n + 1))
                for i, text in enumerate(texts):
                    if "ZZ-REDUNDANT-SENTINEL" in text:
                        out[i, 0] = 1.0
                    else:
                        out[i, i + 1] = 1.0
                return out

        module = types.ModuleType("sentence_transformers")
        module.SentenceTransformer = FakeModel
        monkeypatch.setitem(sys.modules, "sentence_transformers", module)

    def _sentinel_rule(self, rid: str) -> dict:
        data = _rule_dict(rid, PROJECT)
        data["trigger"] = "ZZ-REDUNDANT-SENTINEL trigger"
        data["statement"] = "ZZ-REDUNDANT-SENTINEL statement"
        return data

    @pytest.mark.asyncio
    async def test_two_live_near_identical_rules_are_flagged(self, db, fake_embedder) -> None:
        from writ.graph.integrity import IntegrityChecker

        await db.create_rule(self._sentinel_rule("TS-RED-001"))
        await db.create_rule(self._sentinel_rule("TS-RED-002"))
        pairs = await IntegrityChecker(db._driver, db._database).detect_redundant()
        flagged = {frozenset((p["rule_a"], p["rule_b"])) for p in pairs}
        assert frozenset(("TS-RED-001", "TS-RED-002")) in flagged

    @pytest.mark.asyncio
    async def test_a_superseded_rule_and_its_replacement_are_not_flagged(
        self, db, fake_embedder
    ) -> None:
        from writ.graph.integrity import IntegrityChecker

        await db.create_rule(self._sentinel_rule("TS-RED-010"))
        await db.create_rule(self._sentinel_rule("TS-RED-011"))
        await db.create_edge("SUPERSEDES", "TS-RED-010", "TS-RED-011", project=PROJECT)
        await db.refresh_superseded_flags(PROJECT)
        pairs = await IntegrityChecker(db._driver, db._database).detect_redundant()
        for pair in pairs:
            assert "TS-RED-011" not in (pair["rule_a"], pair["rule_b"]), pair

    def test_detect_redundant_reads_its_pool_from_the_shared_predicate(self) -> None:
        # Lockstep by construction: the query is built from RANKED_INCLUDE_WHERE, not from an
        # inline copy of the mandatory exclusion.
        import inspect

        from writ.graph.integrity import structural_checks

        source = inspect.getsource(_find_detect_redundant(structural_checks))
        assert "RANKED_INCLUDE_WHERE" in source
        assert "r.mandatory IS NULL OR r.mandatory = false" not in source


def _find_detect_redundant(module):
    for value in vars(module).values():
        if isinstance(value, type) and "detect_redundant" in vars(value):
            return vars(value)["detect_redundant"]
    raise AssertionError("detect_redundant not found in structural_checks")
