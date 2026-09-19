"""Cycle M skeletons: a sub-agent's write scope comes from its ROLE ALONE.

THE INVARIANT. Today every `SubagentStart`-seeded cache gets an unconditional write
allow (`gates.py:322-326`, the `subagent_bypass` arm). This cycle adds a narrower arm
ahead of it that reads exactly four cache fields -- the resolved role (`agent_type`),
where that role came from (`role_source`), how the cache came to exist (`cache_source`),
and the scope stamped from the role's graph node at dispatch (`role_write_scope`) -- and
nothing else. It never reads `mode`, `current_phase`, `gates_approved`,
`parent_session_id` or `project_root`. The parent's approval is a PRECONDITION for the
dispatch existing at all (`SubagentStart` fired because a human approved the work that
spawned this worker); it is never an INPUT to the child's scope.

THE TRAP THIS CYCLE NAMES AND REJECTS. "The parent's gates, narrowed by the role" is the
same words with the OPPOSITE property: under it, a parent who approves more widens the
child and a parent who approves less narrows it, so authority still inherits and the
child's boundary is a function of who dispatched it rather than of what it is. That is
exactly the shape cycle K found accidentally reproduced (a lazily seeded cache inheriting
its parent's mode+gates) and exactly the shape this cycle exists to make structurally
impossible for a role-scoped write, not just absent by convention. The two PROPERTY test
classes below (`TestNoParentStateLeaksIntoScope`, `TestStampingNeverLoosensADeny`) are
the whole proof: a case list would pass while an unlisted parent-state combination or an
unlisted cache shape silently leaked authority through or loosened a decision, which is
the mistake one cycle up (`tests/test_subagent_seed.py:291-305`) already caught once.

WHAT PASSES TODAY, UNMODIFIED, AND MUST KEEP PASSING:
  - TestCycleKPropertyStillHolds: cycle K's own parametrized property (a lazy_seed cache's
    decision equals the no-cache decision, on all 7 representative paths), the lazy
    source-write deny, a subagent_start cache with no role still writing an ordinary
    source path, and a legacy cache with no cache_source key still writing.
  - TestDebugGateBypassLiteralStillPresent: `is_subagent` with no `cache_source` still
    bypasses the debug gate and writes with mode=None/no gates approved, and gates.py
    still contains the literals `cache.get("is_subagent")` and `subagent_bypass`.
None of these read `role_write_scope`, so nothing in this cycle can touch them.

WHICH OTHER TESTS ARE CURRENTLY GREEN FOR THE WRONG REASON, DOCUMENTED PER TEST/CLASS
(ENF-GATE-007 / "no vacuous passes"): gates.py does not read `role_write_scope` at all
yet, so every "the arm abstains" and "stamping never loosens a decision" case is
trivially true today -- there is no arm yet to abstain from or to loosen anything. Each
such class says so in its own docstring rather than counting toward coverage. Tests that
call `_require(...)` / `_require_field(...)` on a symbol that does not exist yet FAIL
(never skip) instead of passing vacuously; that is most of the rest of this file.
"""
from __future__ import annotations

import asyncio
import io
import itertools
import json
import os
import sys
from pathlib import Path

import pytest

from tests._graph import connection

REPO = Path(__file__).resolve().parent.parent

AGENT = "b1123456789abcdef0"
PARENT = "22222222-3333-4444-5555-666666666666"

# Outside the skill dir on purpose (matches tests/test_subagent_seed.py's GATE_PATHS
# convention): a path under REPO hits the skill-dir exemption before any role-scope
# logic runs, which would make every case here look allowed regardless of the arm.
OUT_OF_TREE_DIR = "/home/lucio.saldivar/workspaces/ai-stack"

ROLE_SCOPE_COVERAGE_CHECK = "subagent-role-scope-coverage"

# The real graph project namespace this file's fixtures live under. Never "writ" --
# reconcile() and detect_prop_parity() are project-scoped precisely so a partial source
# tree (one synthetic .md file) is the FULL corpus for THIS project and therefore safe
# to reconcile against, per methodology_ingest.reconcile's own "FULL-CORPUS ONLY" rule.
TEST_ROLE_PROJECT = "writ-test-role-scope-fixture"


# --------------------------------------------------------------------------------- #
# Shared helpers. ENF-GATE-007 / TEST-ISOLATE-003: fail loudly on missing production
# code, never skip; every real-graph test gets its own synthetic project namespace.
# --------------------------------------------------------------------------------- #


def _require(module, *names) -> None:
    missing = [n for n in names if not hasattr(module, n)]
    if missing:
        pytest.fail(f"skeleton: {module.__name__} has no {', '.join(missing)} yet")


def _require_field(model: type, field: str) -> None:
    """Fail (never skip, never silently pass) when a pydantic node model has not yet
    declared `field`. Without this, a real-graph test comparing two omissions (frontmatter
    that cannot carry the field, a query that cannot project it) would read green for the
    wrong reason -- both sides agreeing on absence proves nothing about the feature."""
    fields = getattr(model, "model_fields", {})
    if field not in fields:
        pytest.fail(
            f"skeleton: {model.__name__} has no '{field}' field yet "
            "(writ/graph/schema.py declares SubagentRole)"
        )


def _role_scope_module():
    try:
        from writ.session import role_scope
    except ImportError as exc:  # pragma: no cover - RED path
        pytest.fail(f"skeleton: writ/session/role_scope.py does not exist yet ({exc})")
    return role_scope


def _seed_module():
    try:
        from writ.session import subagent_seed
    except ImportError as exc:  # pragma: no cover - RED path
        pytest.fail(f"skeleton: writ/session/subagent_seed.py does not exist yet ({exc})")
    return subagent_seed


def _envelope(path: str) -> dict:
    return {"tool_input": {"file_path": path}}


def _role_scope_cache(role: str, scope, **overrides) -> dict:
    """A `subagent_start` cache whose role is resolved and whose scope is known (or None
    for 'undeclared'). Only the four fields the arm may read are meaningful here; mode /
    gates_approved / current_phase are filled with a realistic mid-implementation state
    purely so a test that DOES leak parent state would show a visible effect."""
    base = {
        "is_subagent": True,
        "cache_source": "subagent_start",
        "agent_type": role,
        "role_source": "envelope",
        "role_write_scope": scope,
        "role_scope_source": "graph" if scope is not None else "",
        "mode": "work",
        "current_phase": "implementation",
        "gates_approved": ["phase-a", "test-skeletons"],
        "parent_session_id": PARENT,
        "project_root": "",
    }
    base.update(overrides)
    return base


@pytest.fixture()
def cache_dir(tmp_path, monkeypatch):
    d = tmp_path / "cache"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("WRIT_CACHE_DIR", str(d))
    return d


def _write_parent_cache(cache_dir: Path, session_id: str = PARENT) -> None:
    payload = {
        "mode": "work",
        "current_phase": "implementation",
        "gates_approved": ["phase-a", "test-skeletons"],
        "project_root": "",
    }
    (cache_dir / f"writ-session-{session_id}.json").write_text(json.dumps(payload))


def _child_cache(cache_dir: Path, agent_id: str = AGENT) -> dict | None:
    path = cache_dir / f"writ-session-{agent_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


# --------------------------------------------------------------------------------- #
# Real-graph fixture authoring (capabilities 1, 2, 4). Synthetic SubagentRole nodes
# under TEST_ROLE_PROJECT, never the real corpus. ENF-SYS-005: these three classes are
# the ones a mocked driver cannot stand in for -- it would replay whatever value the
# test constructed on either side of the round trip and prove nothing about how Neo4j
# actually stores an empty list versus an absent property.
# --------------------------------------------------------------------------------- #

_UNSET = object()


def _role_markdown(role_id: str, name: str, *, write_scope=_UNSET) -> str:
    """Minimal valid SubagentRole frontmatter. `write_scope` omitted (default) means the
    key is absent from the file entirely -- distinct from `write_scope=[]`, which is
    present and explicitly empty. Never dispatched; exists only for this test module."""
    lines = [
        "---",
        f"role_id: {role_id}",
        "node_type: SubagentRole",
        "domain: process",
        "scope: task",
        "trigger: fixture trigger text, never a real dispatch target",
        "statement: fixture statement text for writ/session/role_scope.py tests",
        "rationale: fixture rationale text, exists only for this test module",
        "confidence: peer-reviewed",
        "authority: human",
        "last_validated: '2026-08-28'",
        f"name: {name}",
        "description: test fixture role; never dispatched in production",
        "model_preference: sonnet",
        "prompt_template: fixture prompt, not a real role",
    ]
    if write_scope is not _UNSET:
        if write_scope:
            lines.append("write_scope:")
            lines.extend(f"  - '{p}'" for p in write_scope)
        else:
            lines.append("write_scope: []")
    lines += ["---", "", "# fixture", ""]
    return "\n".join(lines)


def _write_role_fixture(tmp_path: Path, filename: str, markdown: str) -> Path:
    fixture_dir = tmp_path / "fixture-bible"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    (fixture_dir / filename).write_text(markdown)
    return fixture_dir


async def _cleanup_role_fixtures(db) -> None:
    async with db._driver.session(database=db._database) as s:
        await s.run(
            "MATCH (n:SubagentRole) WHERE n.project = $p DETACH DELETE n",
            p=TEST_ROLE_PROJECT,
        )


# --------------------------------------------------------------------------------- #
# Capability 1: a declared scope survives ingest and is readable both ways.
# --------------------------------------------------------------------------------- #


class TestScopeSurvivesIngestAndRoundTrips:
    """RED until schema.py declares `write_scope` on SubagentRole, node_store.py's
    get_subagent_role projects it, and query.py's route returns it."""

    def test_a_declared_scope_comes_back_from_get_subagent_role(self, tmp_path) -> None:
        from writ.graph.schema import SubagentRole
        _require_field(SubagentRole, "write_scope")
        from writ.graph.methodology_ingest import ingest_path

        db = connection()

        async def _run():
            await _cleanup_role_fixtures(db)
            fixture_dir = _write_role_fixture(
                tmp_path, "planner.md",
                _role_markdown("ROL-WRITESCOPETEST-001", "writ-scope-fixture-planner",
                               write_scope=["plan.md", "capabilities.md"]))
            try:
                await ingest_path(fixture_dir, db, project=TEST_ROLE_PROJECT)
                return await db.get_subagent_role("writ-scope-fixture-planner")
            finally:
                await _cleanup_role_fixtures(db)
                await db.close()

        result = asyncio.run(_run())
        assert result is not None, "the ingested role was not found by get_subagent_role"
        assert result.get("write_scope") == ["plan.md", "capabilities.md"], result

    def test_the_same_scope_comes_back_from_the_http_route(self, tmp_path, monkeypatch) -> None:
        from writ.graph.schema import SubagentRole
        _require_field(SubagentRole, "write_scope")
        import writ.server as server
        from writ.graph.methodology_ingest import ingest_path
        from writ.server.routes.query import subagent_role_get

        db = connection()

        async def _run():
            await _cleanup_role_fixtures(db)
            fixture_dir = _write_role_fixture(
                tmp_path, "planner.md",
                _role_markdown("ROL-WRITESCOPETEST-001", "writ-scope-fixture-planner",
                               write_scope=["plan.md", "capabilities.md"]))
            try:
                await ingest_path(fixture_dir, db, project=TEST_ROLE_PROJECT)
                monkeypatch.setattr(server, "_db", db)
                return await subagent_role_get("writ-scope-fixture-planner")
            finally:
                await _cleanup_role_fixtures(db)
                await db.close()

        result = asyncio.run(_run())
        assert result.get("write_scope") == ["plan.md", "capabilities.md"], result


# --------------------------------------------------------------------------------- #
# Capability 2: absent and empty are different facts, and Neo4j must keep them so.
# --------------------------------------------------------------------------------- #


class TestEmptyScopeStaysDistinguishableFromAbsent:
    """RED until schema.py declares `write_scope`. Real graph only (ENF-SYS-005)."""

    def test_an_explicitly_empty_scope_round_trips_as_an_empty_list(self, tmp_path) -> None:
        from writ.graph.schema import SubagentRole
        _require_field(SubagentRole, "write_scope")
        from writ.graph.methodology_ingest import ingest_path

        db = connection()

        async def _run():
            await _cleanup_role_fixtures(db)
            fixture_dir = _write_role_fixture(
                tmp_path, "empty.md",
                _role_markdown("ROL-WRITESCOPETEST-002", "writ-scope-fixture-empty",
                               write_scope=[]))
            try:
                await ingest_path(fixture_dir, db, project=TEST_ROLE_PROJECT)
                return await db.get_subagent_role("writ-scope-fixture-empty")
            finally:
                await _cleanup_role_fixtures(db)
                await db.close()

        result = asyncio.run(_run())
        assert result is not None
        assert result.get("write_scope") == [], result

    def test_an_undeclared_scope_round_trips_as_none_not_as_an_empty_list(
        self, tmp_path
    ) -> None:
        from writ.graph.schema import SubagentRole
        _require_field(SubagentRole, "write_scope")
        from writ.graph.methodology_ingest import ingest_path

        db = connection()

        async def _run():
            await _cleanup_role_fixtures(db)
            fixture_dir = _write_role_fixture(
                tmp_path, "absent.md",
                _role_markdown("ROL-WRITESCOPETEST-003", "writ-scope-fixture-absent"))
            try:
                await ingest_path(fixture_dir, db, project=TEST_ROLE_PROJECT)
                return await db.get_subagent_role("writ-scope-fixture-absent")
            finally:
                await _cleanup_role_fixtures(db)
                await db.close()

        result = asyncio.run(_run())
        assert result is not None
        assert result.get("write_scope") is None, result

    def test_the_two_are_distinguishable_side_by_side(self, tmp_path) -> None:
        """Guards against a false green: an implementation that returns the same truthy
        default (e.g. always []) for both a declared-empty and an undeclared role would
        not be caught by either test above alone -- only comparing them side by side is."""
        from writ.graph.schema import SubagentRole
        _require_field(SubagentRole, "write_scope")
        from writ.graph.methodology_ingest import ingest_path

        db = connection()

        async def _run():
            await _cleanup_role_fixtures(db)
            fixture_dir = tmp_path / "fixture-bible"
            fixture_dir.mkdir(parents=True, exist_ok=True)
            (fixture_dir / "empty.md").write_text(
                _role_markdown("ROL-WRITESCOPETEST-002", "writ-scope-fixture-empty",
                               write_scope=[]))
            (fixture_dir / "absent.md").write_text(
                _role_markdown("ROL-WRITESCOPETEST-003", "writ-scope-fixture-absent"))
            try:
                await ingest_path(fixture_dir, db, project=TEST_ROLE_PROJECT)
                declared = await db.get_subagent_role("writ-scope-fixture-empty")
                absent = await db.get_subagent_role("writ-scope-fixture-absent")
                return declared, absent
            finally:
                await _cleanup_role_fixtures(db)
                await db.close()

        declared, absent = asyncio.run(_run())
        assert declared is not None and absent is not None
        assert declared.get("write_scope") == []
        assert absent.get("write_scope") is None
        assert declared.get("write_scope") != absent.get("write_scope")


# --------------------------------------------------------------------------------- #
# Capability 3: writ-implementer's deliberate gap keeps today's decision.
# --------------------------------------------------------------------------------- #


class TestUndeclaredScopeKeepsTodaysDecision:
    """writ-implementer is the cycle's one intentional gap (plan Analysis: its scope is
    per-dispatch data, not a role property, so it declares none and keeps the blanket
    allow). The first test is PARTIALLY VACUOUS TODAY, DOCUMENTED: gates.py does not
    read role_write_scope at all yet, so an undeclared scope is inert for the same reason
    every other value would be. The second test is the real coverage -- it fails today,
    because both roles currently get the identical blanket allow."""

    def test_an_undeclared_scope_keeps_the_blanket_allow(self) -> None:
        from writ.session import gates
        path = f"{OUT_OF_TREE_DIR}/src/thing.py"
        cache = _role_scope_cache("writ-implementer", None)
        result = gates._can_write_check(AGENT, _envelope(path), str(REPO), cache)
        assert result["can_write"] is True

    def test_undeclared_differs_from_an_explicitly_empty_scope_on_the_same_path(self) -> None:
        from writ.session import gates
        path = f"{OUT_OF_TREE_DIR}/src/thing.py"
        undeclared = gates._can_write_check(
            AGENT, _envelope(path), str(REPO), _role_scope_cache("writ-implementer", None))
        declared_empty = gates._can_write_check(
            AGENT, _envelope(path), str(REPO), _role_scope_cache("writ-explorer", []))
        assert undeclared["can_write"] is True
        assert declared_empty["can_write"] is False


# --------------------------------------------------------------------------------- #
# Capability 4: writ validate / writ reconcile respect a declared scope.
# --------------------------------------------------------------------------------- #


class TestValidateAndReconcileRespectDeclaredScope:
    """RED until schema.py declares `write_scope`. Real graph only (ENF-SYS-005):
    reconcile's stale-prop clearing and prop-parity's drift detection both run live
    Cypher over MANAGED_PROP_NAMES; a mocked driver cannot prove either respects the
    field once it exists there."""

    def test_reconcile_does_not_clear_a_declared_scope(self, tmp_path) -> None:
        from writ.graph.schema import SubagentRole
        _require_field(SubagentRole, "write_scope")
        from writ.graph.methodology_ingest import reconcile

        db = connection()

        async def _run():
            await _cleanup_role_fixtures(db)
            fixture_dir = _write_role_fixture(
                tmp_path, "planner.md",
                _role_markdown("ROL-WRITESCOPETEST-004", "writ-scope-fixture-reconcile",
                               write_scope=["plan.md"]))
            try:
                await reconcile(fixture_dir, db, project=TEST_ROLE_PROJECT)
                return await db.get_subagent_role("writ-scope-fixture-reconcile")
            finally:
                await _cleanup_role_fixtures(db)
                await db.close()

        result = asyncio.run(_run())
        assert result is not None
        assert result.get("write_scope") == ["plan.md"], (
            f"reconcile cleared or altered a declared write_scope: {result}"
        )

    def test_validate_reports_no_prop_parity_drift_once_bible_and_graph_agree(
        self, tmp_path
    ) -> None:
        """Would be vacuously green before this cycle (neither the parsed frontmatter nor
        the live graph carries the property, so there is nothing for the two to disagree
        about) if not for the `_require_field` guard above, which turns that gap into an
        explicit failure instead of a silent pass."""
        from writ.graph.schema import SubagentRole
        _require_field(SubagentRole, "write_scope")
        from writ.graph.integrity import IntegrityChecker
        from writ.graph.methodology_ingest import ingest_path

        db = connection()

        async def _run():
            await _cleanup_role_fixtures(db)
            fixture_dir = _write_role_fixture(
                tmp_path, "planner.md",
                _role_markdown("ROL-WRITESCOPETEST-005", "writ-scope-fixture-parity",
                               write_scope=["plan.md"]))
            try:
                await ingest_path(fixture_dir, db, project=TEST_ROLE_PROJECT)
                checker = IntegrityChecker(db._driver, db._database)
                return await checker.detect_prop_parity(fixture_dir, project=TEST_ROLE_PROJECT)
            finally:
                await _cleanup_role_fixtures(db)
                await db.close()

        result = asyncio.run(_run())
        assert result is None, (
            f"a freshly ingested declared write_scope was flagged as prop-parity drift: {result}"
        )


# --------------------------------------------------------------------------------- #
# Capability 5: the scope is fetched and stamped ONLY on the subagent_start path.
# --------------------------------------------------------------------------------- #


class TestScopeStampedOnlyOnStartPath:
    """RED until writ/session/role_scope.py exists and writ/session/subagent_seed.py
    calls its fetcher on the subagent_start path only.

    SEAM CONTRACT for the implementer: the seeder must call `role_scope.fetch_declared_
    scope(...)` through the MODULE object (e.g. `from writ.session import role_scope`
    then `role_scope.fetch_declared_scope(...)`, resolved fresh at call time), not via a
    bare `from writ.session.role_scope import fetch_declared_scope` bound once at
    subagent_seed.py's own top-level import -- these tests monkeypatch the attribute on
    the role_scope module, which only takes effect if the caller re-resolves it per call.
    This mirrors the module's own existing style (`from writ.shared.logging import
    emit_exception` inside function bodies throughout this codebase)."""

    def test_a_start_seed_fetches_and_stamps_the_declared_scope(
        self, cache_dir, monkeypatch
    ) -> None:
        scope_mod = _role_scope_module()
        _require(scope_mod, "fetch_declared_scope")
        seed_mod = _seed_module()
        _write_parent_cache(cache_dir)
        monkeypatch.setattr(
            "writ.session.role_scope.fetch_declared_scope",
            lambda *a, **k: ["plan.md", "capabilities.md"],
        )
        seed_mod.seed_subagent_cache(
            AGENT, PARENT, cache_source=seed_mod.CACHE_SOURCE_START,
            envelope_agent_type="writ-planner")
        child = _child_cache(cache_dir)
        assert child is not None
        assert child["role_write_scope"] == ["plan.md", "capabilities.md"]
        assert child.get("role_scope_source"), "no source recorded for the stamped scope"

    def test_a_lazy_seed_never_calls_the_fetcher(self, cache_dir, monkeypatch) -> None:
        scope_mod = _role_scope_module()
        seed_mod = _seed_module()
        _write_parent_cache(cache_dir)
        calls = []
        monkeypatch.setattr(
            "writ.session.role_scope.fetch_declared_scope",
            lambda *a, **k: calls.append((a, k)) or ["plan.md"],
        )
        seed_mod.seed_subagent_cache(AGENT, PARENT, envelope_agent_type="writ-planner")
        assert calls == [], "the lazy path fetched a scope it must never fetch"
        child = _child_cache(cache_dir)
        assert child is not None
        assert child["role_write_scope"] is None
        assert child.get("role_scope_source") == ""

    def test_a_start_seed_with_no_declared_scope_stamps_none(
        self, cache_dir, monkeypatch
    ) -> None:
        scope_mod = _role_scope_module()
        seed_mod = _seed_module()
        _write_parent_cache(cache_dir)
        monkeypatch.setattr(
            "writ.session.role_scope.fetch_declared_scope", lambda *a, **k: None)
        seed_mod.seed_subagent_cache(
            AGENT, PARENT, cache_source=seed_mod.CACHE_SOURCE_START,
            envelope_agent_type="writ-implementer")
        child = _child_cache(cache_dir)
        assert child is not None
        assert child["role_write_scope"] is None


# --------------------------------------------------------------------------------- #
# Capability 6: the cache schema tells "no declared scope" from "declares nothing".
# --------------------------------------------------------------------------------- #


class TestDefaultCacheDeclaresNoScope:
    """RED until cache.py's _default_cache() declares the two new keys."""

    def test_default_cache_has_no_declared_scope(self) -> None:
        from writ.session.cache import _default_cache
        cache = _default_cache()
        assert "role_write_scope" in cache, "role_write_scope is missing from the schema"
        assert cache["role_write_scope"] is None

    def test_default_cache_has_an_empty_scope_source(self) -> None:
        from writ.session.cache import _default_cache
        cache = _default_cache()
        assert "role_scope_source" in cache
        assert cache["role_scope_source"] == ""

    def test_a_pre_cycle_cache_on_disk_reads_back_as_no_declared_scope(
        self, cache_dir
    ) -> None:
        """A real on-disk cache written before this cycle carries neither key at all.
        _read_cache fills any missing key from _default_cache() (cache.py:278), so
        re-reading it must produce role_write_scope=None -- keeping today's write
        authority -- never [], which would deny everything a subagent tries to write."""
        from writ.session.cache import _read_cache
        legacy = {"mode": "work", "is_subagent": True, "cache_source": "subagent_start"}
        (cache_dir / f"writ-session-{AGENT}.json").write_text(json.dumps(legacy))
        reread = _read_cache(AGENT)
        assert reread["role_write_scope"] is None


# --------------------------------------------------------------------------------- #
# Capability 7: an out-of-scope write is refused, through every write surface.
# --------------------------------------------------------------------------------- #


class TestOutOfScopeWriteIsRefused:
    """RED: gates.py does not read role_write_scope yet, so every case below currently
    resolves to the blanket allow (can_write=True) instead of the expected deny."""

    def test_write_to_an_out_of_scope_path_is_denied(self) -> None:
        from writ.session import gates
        cache = _role_scope_cache("writ-explorer", [])
        result = gates._can_write_check(
            AGENT, _envelope(f"{OUT_OF_TREE_DIR}/capabilities.md"), str(REPO), cache)
        assert result["can_write"] is False
        assert "ENF-ROLE-SCOPE" in (result["reason"] or "")

    def test_the_refusal_names_the_role_and_that_only_a_human_can_change_it(self) -> None:
        from writ.session import gates
        cache = _role_scope_cache("writ-explorer", [])
        result = gates._can_write_check(
            AGENT, _envelope(f"{OUT_OF_TREE_DIR}/src/thing.py"), str(REPO), cache)
        reason = result["reason"] or ""
        assert "writ-explorer" in reason
        assert "human" in reason.lower()

    def test_edit_is_refused_the_same_way(self) -> None:
        from writ.session import gates
        cache = _role_scope_cache("writ-explorer", [])
        envelope = {"tool_input": {"file_path": f"{OUT_OF_TREE_DIR}/src/thing.py"}}
        result = gates._can_write_check(AGENT, envelope, str(REPO), cache)
        assert result["can_write"] is False

    def test_notebookedit_is_refused_via_its_own_path_key(self) -> None:
        from writ.session import gates
        cache = _role_scope_cache("writ-explorer", [])
        envelope = {"tool_input": {"notebook_path": f"{OUT_OF_TREE_DIR}/notebook.ipynb"}}
        result = gates._can_write_check(AGENT, envelope, str(REPO), cache)
        assert result["can_write"] is False

    def test_the_bash_fallback_cli_reaches_the_same_refusal(self, tmp_path, monkeypatch) -> None:
        """`cmd_can_write` is exactly what bin/lib/writ-session.py's `can-write` command
        runs, which is exactly what writ-bash-write-gate.sh's daemon-unreachable branch
        shells out to. This proves the Bash-mediated write vector inherits the refusal
        with no change to that script."""
        from writ.session import gates
        from writ.session.cache import _write_cache
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
        _write_cache(AGENT, _role_scope_cache("writ-explorer", []))
        monkeypatch.setattr(
            sys, "stdin",
            io.StringIO(json.dumps(
                {"tool_input": {"file_path": f"{OUT_OF_TREE_DIR}/src/thing.py"}})),
        )
        buf = io.StringIO()
        monkeypatch.setattr(sys, "stdout", buf)
        gates.cmd_can_write(AGENT, str(REPO))
        out = json.loads(buf.getvalue())
        assert out["decision"] == "deny"
        assert "ENF-ROLE-SCOPE" in out.get("reason", "")


# --------------------------------------------------------------------------------- #
# Capability 8: an in-scope write is allowed, and the decision is countable.
# --------------------------------------------------------------------------------- #


class TestInScopeWriteIsAllowedAndCountable:
    def test_writ_planner_writes_plan_md(self, tmp_path, monkeypatch) -> None:
        friction = tmp_path / "friction.jsonl"
        monkeypatch.setenv("WRIT_FRICTION_LOG", str(friction))
        from writ.session import gates
        cache = _role_scope_cache("writ-planner", ["plan.md", "capabilities.md"])
        result = gates._can_write_check(
            AGENT, _envelope(f"{OUT_OF_TREE_DIR}/plan.md"), str(REPO), cache)
        assert result["can_write"] is True
        rows = [json.loads(l) for l in friction.read_text().splitlines() if l.strip()] \
            if friction.exists() else []
        assert any(str(r.get("gate_status", "")).startswith("role_scope") for r in rows), (
            f"no role-scope gate_status was logged for a role-scoped allow: {rows}"
        )

    def test_writ_planner_writes_capabilities_md(self) -> None:
        from writ.session import gates
        cache = _role_scope_cache("writ-planner", ["plan.md", "capabilities.md"])
        result = gates._can_write_check(
            AGENT, _envelope(f"{OUT_OF_TREE_DIR}/capabilities.md"), str(REPO), cache)
        assert result["can_write"] is True

    def test_writ_explorer_is_refused_capabilities_md_that_blanket_allow_permits(self) -> None:
        """The intended tightening (plan Analysis, 'Ordering consequences'): today's
        blanket allow lets ANY subagent write capabilities.md via _check_special_files
        before mode is ever consulted. Once a role declares an empty scope, this arm
        must run first (it sits ahead of the blanket allow) and refuse it."""
        from writ.session import gates
        cache = _role_scope_cache("writ-explorer", [])
        result = gates._can_write_check(
            AGENT, _envelope(f"{OUT_OF_TREE_DIR}/capabilities.md"), str(REPO), cache)
        assert result["can_write"] is False

    def test_todays_blanket_allow_still_permits_it_for_an_undeclared_role(self) -> None:
        """Contrast case (not new coverage on its own): proves the refusal above is
        attributable to the DECLARED scope, not to some unrelated change to how
        capabilities.md is handled. This one passes both before and after the cycle."""
        from writ.session import gates
        cache = _role_scope_cache("writ-implementer", None)
        result = gates._can_write_check(
            AGENT, _envelope(f"{OUT_OF_TREE_DIR}/capabilities.md"), str(REPO), cache)
        assert result["can_write"] is True


# --------------------------------------------------------------------------------- #
# Capability 9 -- PROPERTY 1. For a fixed (role, path), no parent state may change the
# verdict or its reason. Modeled on tests/test_subagent_seed.py:291-305.
# --------------------------------------------------------------------------------- #

_PARENT_MODES = ["work", "debug", "investigate", "review", "conversation", None]
_PARENT_GATE_SETS = [[], ["phase-a"], ["phase-a", "test-skeletons"]]
_PARENT_PHASES = [None, "planning", "implementation"]
_PARENT_STATE_CROSS_PRODUCT = list(
    itertools.product(_PARENT_MODES, _PARENT_GATE_SETS, _PARENT_PHASES)
)
assert len(_PARENT_STATE_CROSS_PRODUCT) == 54


def _combo_id(combo) -> str:
    mode, gates_approved, phase = combo
    return f"mode={mode}-gates={','.join(gates_approved) or 'none'}-phase={phase}"


_PARENT_STATE_IDS = [_combo_id(c) for c in _PARENT_STATE_CROSS_PRODUCT]

# The representative (role, declared scope, path) set: an in-scope path and an
# out-of-scope path for a role with a non-empty scope, plus an out-of-scope path for a
# role with an explicitly empty one -- covering both shapes plan Analysis distinguishes.
_ROLE_PATH_CASES = [
    pytest.param("writ-planner", ["plan.md", "capabilities.md"],
                 f"{OUT_OF_TREE_DIR}/plan.md", id="planner-in-scope-plan-md"),
    pytest.param("writ-planner", ["plan.md", "capabilities.md"],
                 f"{OUT_OF_TREE_DIR}/src/thing.py", id="planner-out-of-scope-src"),
    pytest.param("writ-explorer", [], f"{OUT_OF_TREE_DIR}/capabilities.md",
                 id="explorer-empty-scope-capabilities-md"),
]


class TestNoParentStateLeaksIntoScope:
    """PROPERTY 1 (plan capability 9).

    VACUOUS TODAY, DOCUMENTED, NOT COUNTED AS COVERAGE: gates.py's existing sub-agent
    bypass (`cache.get("is_subagent") and not is_lazily_seeded`) already grants an
    unconditional allow with no reference to mode/gates_approved/current_phase at all, so
    every one of these 162 parametrized cases is already invariant across the cross
    product today -- for the WRONG reason (nothing reads these fields yet), not because
    the new arm is correctly isolated from them. It becomes a real test of that isolation
    the moment the arm reads role_write_scope and starts denying the out-of-scope cases:
    from then on, a decision that starts varying with `mode` for a fixed (role, path) is
    exactly the parent-authority leak this cycle exists to remove.
    """

    def _cache(self, role, scope, mode, gates_approved, current_phase) -> dict:
        return {
            "is_subagent": True,
            "cache_source": "subagent_start",
            "agent_type": role,
            "role_source": "envelope",
            "role_write_scope": scope,
            "mode": mode,
            "gates_approved": list(gates_approved),
            "current_phase": current_phase,
            "parent_session_id": PARENT,
            "project_root": "",
        }

    @pytest.mark.parametrize("role,scope,path", _ROLE_PATH_CASES)
    @pytest.mark.parametrize(
        "mode,gates_approved,current_phase", _PARENT_STATE_CROSS_PRODUCT, ids=_PARENT_STATE_IDS
    )
    def test_verdict_and_reason_are_identical_across_every_parent_state(
        self, role, scope, path, mode, gates_approved, current_phase
    ) -> None:
        from writ.session import gates
        reference = gates._can_write_check(
            AGENT, _envelope(path), str(REPO), self._cache(role, scope, None, [], None))
        combo = gates._can_write_check(
            AGENT, _envelope(path), str(REPO),
            self._cache(role, scope, mode, gates_approved, current_phase))
        assert (combo["can_write"], combo["reason"]) == (
            reference["can_write"], reference["reason"]
        ), (
            f"parent state leaked through for role={role!r} path={path!r}: "
            f"mode={mode!r} gates_approved={gates_approved!r} phase={current_phase!r} "
            f"produced {combo} instead of the mode=None/no-gates reference {reference}"
        )


# --------------------------------------------------------------------------------- #
# Capability 10 -- PROPERTY 2. Stamping a scope may only turn an allow into a deny.
# --------------------------------------------------------------------------------- #

_MONOTONICITY_CACHE_SHAPES = {
    "subagent_start_resolved": {
        "is_subagent": True, "cache_source": "subagent_start",
        "agent_type": "writ-planner", "role_source": "envelope",
    },
    "subagent_start_unresolved": {
        "is_subagent": True, "cache_source": "subagent_start",
        "agent_type": "unknown", "role_source": "unresolved",
    },
    "lazy_seed": {
        "is_subagent": True, "cache_source": "lazy_seed",
        "agent_type": "writ-planner", "role_source": "envelope",
    },
    "legacy_no_cache_source": {
        "is_subagent": True,
        "agent_type": "writ-planner", "role_source": "envelope",
    },
    "main_session": {
        "is_subagent": False, "mode": "work", "current_phase": "implementation",
        "gates_approved": ["phase-a", "test-skeletons"],
    },
}

_MONOTONICITY_PATHS = [
    str(REPO / "writ" / "session" / "gates.py"),      # skill-dir exempt
    f"{OUT_OF_TREE_DIR}/src/thing.py",                 # ordinary source
    f"{OUT_OF_TREE_DIR}/.env",                          # credential deny (precedes everything)
    str(Path.home() / ".claude" / "settings.json"),    # settings exempt
    f"{OUT_OF_TREE_DIR}/plan.md",
    f"{OUT_OF_TREE_DIR}/capabilities.md",
]

_CANDIDATE_SCOPES = [[], ["plan.md", "capabilities.md"], ["*/tests/*"]]


class TestStampingNeverLoosensADeny:
    """PROPERTY 2 (plan capability 10).

    VACUOUS TODAY, DOCUMENTED, NOT COUNTED AS COVERAGE: gates.py does not read
    role_write_scope at all yet, so `before` and `after` below are computed by the exact
    same code path and are trivially identical for every shape/path/scope triple in this
    set -- there is no transition to observe until the new arm exists. It becomes a real
    regression fence, not a placeholder, the moment the arm is added: it starts failing
    the instant a future edit lets a declared scope loosen a decision that was already a
    deny for an unrelated reason (e.g. the credential guard, or no mode at all).
    """

    def _base_cache(self, shape_name: str) -> dict:
        return dict(_MONOTONICITY_CACHE_SHAPES[shape_name])

    @pytest.mark.parametrize("shape_name", sorted(_MONOTONICITY_CACHE_SHAPES))
    @pytest.mark.parametrize("path", _MONOTONICITY_PATHS)
    def test_stamping_a_scope_never_turns_a_deny_into_an_allow(self, shape_name, path) -> None:
        from writ.session import gates
        before = gates._can_write_check(
            AGENT, _envelope(path), str(REPO), self._base_cache(shape_name))
        for scope in _CANDIDATE_SCOPES:
            after_cache = {**self._base_cache(shape_name), "role_write_scope": scope}
            after = gates._can_write_check(AGENT, _envelope(path), str(REPO), after_cache)
            if before["can_write"] is False:
                assert after["can_write"] is False, (
                    f"stamping role_write_scope={scope!r} turned a DENY into an ALLOW "
                    f"for shape={shape_name!r} path={path!r}: before={before} after={after}"
                )


# --------------------------------------------------------------------------------- #
# Capability 11: the arm abstains -- exactly, not just "not worse" -- without all four
# conditions (resolved role, subagent_start, a stamped list).
# --------------------------------------------------------------------------------- #


class TestEnforcementAbstainsWithoutAllFourConditions:
    """PARTIALLY VACUOUS TODAY, DOCUMENTED: gates.py does not read role_write_scope yet,
    so every abstain case here already matches the no-scope baseline for the same
    underlying reason as TestStampingNeverLoosensADeny -- there is no arm yet to abstain
    from. It becomes a real test of the four-condition guard once the arm exists: these
    are the cases proving it checks role resolution and cache_source itself rather than
    trusting that a stamped scope implies both.
    """

    @pytest.mark.parametrize("cache_overrides", [
        pytest.param({"agent_type": ""}, id="empty-agent-type"),
        pytest.param({"agent_type": "unknown", "role_source": "unresolved"},
                     id="unresolved-role"),
        pytest.param({"cache_source": "lazy_seed"}, id="lazy-seed-cache-source"),
        pytest.param({"cache_source": ""}, id="empty-cache-source-string"),
        pytest.param({"role_write_scope": "not-a-list"}, id="role-write-scope-not-a-list"),
    ])
    def test_the_arm_abstains_and_todays_decision_stands(self, cache_overrides) -> None:
        """The baseline is the SAME cache shape with no scope declared, not a fixed
        external reference: some of these overrides (e.g. cache_source="lazy_seed") carry
        their OWN pre-existing decision under cycle K's rules -- independent of this cycle
        -- and comparing against a resolved-subagent_start reference would conflate that
        decision with a role-scope leak."""
        from writ.session import gates
        path = f"{OUT_OF_TREE_DIR}/src/thing.py"
        base = {
            "is_subagent": True, "cache_source": "subagent_start",
            "agent_type": "writ-planner", "role_source": "envelope",
        }
        without_scope = {**base, "role_write_scope": None, **cache_overrides}
        with_scope = {**base, "role_write_scope": ["plan.md", "capabilities.md"],
                     **cache_overrides}
        before = gates._can_write_check(AGENT, _envelope(path), str(REPO), without_scope)
        after = gates._can_write_check(AGENT, _envelope(path), str(REPO), with_scope)
        assert (after["can_write"], after["reason"]) == (
            before["can_write"], before["reason"]
        ), f"the arm fired when it should have abstained ({cache_overrides!r}): {after}"

    def test_a_legacy_cache_with_no_cache_source_key_abstains_too(self) -> None:
        from writ.session import gates
        path = f"{OUT_OF_TREE_DIR}/src/thing.py"
        base = {"is_subagent": True, "agent_type": "writ-planner", "role_source": "envelope"}
        before = gates._can_write_check(
            AGENT, _envelope(path), str(REPO), {**base, "role_write_scope": None})
        after = gates._can_write_check(
            AGENT, _envelope(path), str(REPO), {**base, "role_write_scope": ["plan.md"]})
        assert (after["can_write"], after["reason"]) == (
            before["can_write"], before["reason"]
        )


# --------------------------------------------------------------------------------- #
# Capability 12: matching judges the RESOLVED path, not the raw string.
# --------------------------------------------------------------------------------- #


class TestScopeMatchingJudgesTheResolvedPath:
    """RED until writ/session/role_scope.py exists. Real symlinks via os.symlink, not
    string fixtures -- a lexical `..` or a symlink target is not decidable from the raw
    path (gates.py:291-301 explains the identical reasoning for realpath over normpath
    in the settings exemption)."""

    def test_dot_dot_traversal_out_of_scope_is_refused(self, tmp_path) -> None:
        mod = _role_scope_module()
        _require(mod, "path_in_scope")
        (tmp_path / "proj" / "tests").mkdir(parents=True)
        escape = tmp_path / "proj" / "tests" / ".." / ".." / "elsewhere.py"
        assert mod.path_in_scope(str(escape), ["*/tests/*"]) is False

    def test_a_path_inside_the_declared_scope_is_allowed(self, tmp_path) -> None:
        mod = _role_scope_module()
        target = tmp_path / "proj" / "tests" / "test_thing.py"
        target.parent.mkdir(parents=True)
        target.write_text("")
        assert mod.path_in_scope(str(target), ["*/tests/*"]) is True

    def test_a_symlink_pointing_outside_the_scope_is_refused(self, tmp_path) -> None:
        mod = _role_scope_module()
        (tmp_path / "proj" / "tests").mkdir(parents=True)
        (tmp_path / "outside").mkdir()
        real_target = tmp_path / "outside" / "escape.py"
        real_target.write_text("")
        link = tmp_path / "proj" / "tests" / "link.py"
        os.symlink(real_target, link)
        assert mod.path_in_scope(str(link), ["*/tests/*"]) is False, (
            "a symlink placed under the declared dir but pointing outside it must be refused"
        )

    def test_a_symlink_whose_target_is_inside_the_scope_is_allowed(self, tmp_path) -> None:
        mod = _role_scope_module()
        (tmp_path / "proj" / "tests").mkdir(parents=True)
        (tmp_path / "outside").mkdir()
        real_target = tmp_path / "proj" / "tests" / "real_test.py"
        real_target.write_text("")
        link = tmp_path / "outside" / "link.py"
        os.symlink(real_target, link)
        assert mod.path_in_scope(str(link), ["*/tests/*"]) is True, (
            "a symlink whose TARGET is inside scope must be allowed even though the raw "
            "link path is not"
        )

    def test_the_gate_refuses_a_symlink_escape_for_a_scoped_role(self, tmp_path) -> None:
        """Full-stack version of the escape above, through _can_write_check."""
        from writ.session import gates
        (tmp_path / "proj" / "tests").mkdir(parents=True)
        (tmp_path / "outside").mkdir()
        real_target = tmp_path / "outside" / "escape.py"
        real_target.write_text("")
        link = tmp_path / "proj" / "tests" / "link.py"
        os.symlink(real_target, link)
        cache = _role_scope_cache("writ-test-writer", ["*/tests/*"])
        result = gates._can_write_check(AGENT, _envelope(str(link)), str(REPO), cache)
        assert result["can_write"] is False


# --------------------------------------------------------------------------------- #
# Capability 13: the write decision costs 0 graph queries and 0 HTTP calls.
# --------------------------------------------------------------------------------- #


class TestWriteDecisionNeedsNoGraphQuery:
    """RED until writ/session/role_scope.py exists. Mirrors the plan's own stated
    verification (Query Budget point 6): patch the dispatch-time fetcher to raise, then
    prove both an in-scope allow and an out-of-scope deny still compute from the cache
    alone. If gates.py ever called the fetcher on the write path, this raise would
    propagate and the test would error instead of asserting cleanly."""

    def test_both_verdicts_compute_with_the_fetcher_raising(self, monkeypatch) -> None:
        scope_mod = _role_scope_module()
        _require(scope_mod, "fetch_declared_scope")

        def _boom(*_a, **_k):
            raise RuntimeError("the write path must never call the dispatch-time fetcher")

        monkeypatch.setattr("writ.session.role_scope.fetch_declared_scope", _boom)
        from writ.session import gates
        cache = _role_scope_cache("writ-planner", ["plan.md", "capabilities.md"])
        allowed = gates._can_write_check(
            AGENT, _envelope(f"{OUT_OF_TREE_DIR}/plan.md"), str(REPO), cache)
        denied = gates._can_write_check(
            AGENT, _envelope(f"{OUT_OF_TREE_DIR}/src/thing.py"), str(REPO), cache)
        assert allowed["can_write"] is True
        assert denied["can_write"] is False


# --------------------------------------------------------------------------------- #
# Capability 14 -- MUST-NOT-REGRESS. Cycle K's property and named cases, unmodified by
# this cycle. These must pass right now and keep passing throughout implementation.
# --------------------------------------------------------------------------------- #

_CYCLE_K_PARENT_STATE = {
    "mode": "work",
    "current_phase": "implementation",
    "gates_approved": ["phase-a", "test-skeletons"],
    "project_root": str(REPO),
}

# Same representative-path convention as tests/test_subagent_seed.py::GATE_PATHS: the
# skill-dir exemption, an ordinary out-of-tree source file, a credential path, the
# settings exemption, a test file, and the two special-cased plan artifacts.
_CYCLE_K_GATE_PATHS = [
    str(REPO / "writ" / "session" / "gates.py"),
    f"{OUT_OF_TREE_DIR}/src/thing.py",
    f"{OUT_OF_TREE_DIR}/.env",
    str(Path.home() / ".claude" / "settings.json"),
    f"{OUT_OF_TREE_DIR}/tests/test_thing.py",
    f"{OUT_OF_TREE_DIR}/plan.md",
    f"{OUT_OF_TREE_DIR}/capabilities.md",
]


class TestCycleKPropertyStillHolds:
    """MUST PASS NOW, unmodified by this cycle. None of these caches carry
    role_write_scope, so nothing added by cycle M can touch them."""

    @pytest.mark.parametrize("path", _CYCLE_K_GATE_PATHS)
    def test_a_lazy_seeded_cache_matches_the_no_cache_decision(self, path) -> None:
        from writ.session import gates
        from writ.session.cache import _read_cache
        from writ.session.subagent_seed import CACHE_SOURCE_LAZY
        ungoverned = _read_cache("no-such-agent-role-scope-cycle-m")
        lazy_cache = {
            **_CYCLE_K_PARENT_STATE, "is_subagent": True,
            "parent_session_id": PARENT, "cache_source": CACHE_SOURCE_LAZY,
        }
        before = gates._can_write_check(AGENT, _envelope(path), str(REPO), ungoverned)
        after = gates._can_write_check(AGENT, _envelope(path), str(REPO), lazy_cache)
        assert (after["can_write"], after["reason"]) == (
            before["can_write"], before["reason"]
        ), f"seeding changed the decision for {path}: {before} -> {after}"

    def test_the_lazy_source_write_deny_stands(self) -> None:
        from writ.session import gates
        from writ.session.subagent_seed import CACHE_SOURCE_LAZY
        cache = {
            "mode": None, "current_phase": None, "is_subagent": True,
            "cache_source": CACHE_SOURCE_LAZY,
        }
        result = gates._can_write_check(
            AGENT, _envelope(f"{OUT_OF_TREE_DIR}/src/thing.py"), str(REPO), cache)
        assert result["can_write"] is False

    def test_a_start_cache_with_no_role_still_writes_an_ordinary_source_path(self) -> None:
        from writ.session import gates
        from writ.session.subagent_seed import CACHE_SOURCE_START
        cache = {
            **_CYCLE_K_PARENT_STATE, "is_subagent": True,
            "cache_source": CACHE_SOURCE_START,
        }  # no agent_type / role_source at all
        result = gates._can_write_check(
            AGENT, _envelope(f"{OUT_OF_TREE_DIR}/src/thing.py"), str(REPO), cache)
        assert result["can_write"] is True

    def test_a_legacy_cache_with_no_cache_source_key_still_writes(self) -> None:
        from writ.session import gates
        cache = {**_CYCLE_K_PARENT_STATE, "is_subagent": True}  # no cache_source key
        result = gates._can_write_check(
            AGENT, _envelope(f"{OUT_OF_TREE_DIR}/src/thing.py"), str(REPO), cache)
        assert result["can_write"] is True


# --------------------------------------------------------------------------------- #
# Capability 15 -- MUST-NOT-REGRESS. The debug-gate bypass and its literals.
# --------------------------------------------------------------------------------- #


class TestDebugGateBypassLiteralStillPresent:
    """MUST PASS NOW. Restated here (also pinned by
    tests/test_subagent_role_census.py::TestNoGateDecisionChanges) because this cycle is
    the one that could plausibly have replaced the blanket allow rather than adding a
    narrower arm ahead of it."""

    def test_gates_py_still_contains_the_two_literals(self) -> None:
        from writ.session import gates
        text = Path(gates.__file__).read_text()
        assert 'cache.get("is_subagent")' in text
        assert "subagent_bypass" in text

    def test_is_subagent_with_no_cache_source_still_bypasses_the_debug_gate(self) -> None:
        from writ.session import gates
        cache = {
            "mode": None, "current_phase": None, "gates_approved": [],
            "is_subagent": True,
        }  # no cache_source -> not lazily seeded -> the blanket bypass fires
        result = gates._can_write_check(
            AGENT, _envelope(f"{OUT_OF_TREE_DIR}/src/thing.py"), str(REPO), cache)
        assert result["can_write"] is True


# --------------------------------------------------------------------------------- #
# Capability 16: the doctor reports how many roles declare a scope.
# --------------------------------------------------------------------------------- #


class TestDoctorReportsRoleScopeCoverage:
    """RED until doctor.py adds check_subagent_role_scope_coverage and its `_CHECKS`
    entry, plus a mockable `_subagent_role_scope_census()` seam (mirroring
    `_count_neo4j_rules` / `_list_neo4j_constraint_names`'s existing pattern: a private,
    synchronous, asyncio-bridged module-level helper)."""

    def test_the_check_is_registered(self) -> None:
        from tests._inventory import doctor_check_names
        assert ROLE_SCOPE_COVERAGE_CHECK in doctor_check_names()

    def test_it_names_roles_with_no_declared_scope(self, monkeypatch) -> None:
        from writ.session import doctor
        _require(doctor, "check_subagent_role_scope_coverage", "_subagent_role_scope_census")
        monkeypatch.setattr(doctor, "_subagent_role_scope_census", lambda: [
            {"name": "writ-explorer", "write_scope": []},
            {"name": "writ-planner", "write_scope": ["plan.md", "capabilities.md"]},
            {"name": "writ-implementer", "write_scope": None},
        ])
        result = doctor.check_subagent_role_scope_coverage(doctor.DoctorOptions())
        assert "2" in result.detail, f"the declared-scope count is not reported: {result.detail}"
        assert "writ-implementer" in result.detail

    def test_an_unreachable_graph_is_unmeasured_not_a_failure(self, monkeypatch) -> None:
        from writ.session import doctor
        _require(doctor, "check_subagent_role_scope_coverage", "_subagent_role_scope_census")

        def _raise():
            raise RuntimeError("no bolt connection")

        monkeypatch.setattr(doctor, "_subagent_role_scope_census", _raise)
        result = doctor.check_subagent_role_scope_coverage(doctor.DoctorOptions())
        assert result.status == "ok", result.detail

    def test_a_corpus_where_no_role_declares_one_is_a_single_warn_not_an_accusation(
        self, monkeypatch
    ) -> None:
        from writ.session import doctor
        _require(doctor, "check_subagent_role_scope_coverage", "_subagent_role_scope_census")
        monkeypatch.setattr(doctor, "_subagent_role_scope_census", lambda: [
            {"name": "writ-explorer", "write_scope": None},
            {"name": "writ-reviewer", "write_scope": None},
        ])
        result = doctor.check_subagent_role_scope_coverage(doctor.DoctorOptions())
        # One CheckResult per registered check (run_all_checks' own contract) -- the
        # "not an accusation" half of this capability is that status is a warn, never a
        # fail, when the corpus simply has not declared anything yet.
        assert result.status == "warn", result.detail
        assert isinstance(result.detail, str) and result.detail
