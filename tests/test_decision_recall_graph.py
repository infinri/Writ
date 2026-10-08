"""Program item 4 (decision recall), workstream A: what only a real graph can prove.

ENF-SYS-005. Every claim here is about what the graph returns or how many statements it was
asked, so none of it runs against a mock. It runs on the isolated instance (scripts/test-graph.sh,
port 7688; tests/_graph.py is the only door), on real session caches and on real processes:

  * get_decisions_for_paths: the FileChange -[MOTIVATED_BY]-> Decision traversal, newest-first
    ordering across several changes on one path, the per_path slice, the dedupe by decision, the
    project scoping of BOTH ends, the "most recent MOTIVATED_BY-linked change" reading, the
    empty-paths short circuit and the index seek on filechange_project_path (6, 7)
  * the statement counts of the Query Budget Plan: a cold compile is 3 statements with a path in
    the prompt and 2 without, a warm one 1 and 0, --full adds 1, an allowed write with history
    costs 1 and one outside the project 0 (35)
  * the end to end chain: harvest_one_commit with a plan, then a /prompt-bundle recall naming the
    harvested path, then /pre-write-check for that file, once (36)
  * tombstoned memories never reach a card (14)
  * the recall and always_on shown records lose no write when four sections run concurrently on
    one session or when `writ-session update --mark-shown recall` runs in parallel processes (26)

Every project name carries a per-run suffix, and teardown removes exactly the records a test
created, so nothing collides with another module or a re-run.

RED today: Neo4jConnection has no get_decisions_for_paths; compile_recall has no prompt ranking;
/pre-write-check has no decision_context; injection_state does not track the recall section.

Run: flock <lock> .venv/bin/python3 -m pytest tests/test_decision_recall_graph.py
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import writ.server as server
import writ.server.routes.query as qroute
from writ.server import PreWriteCheckRequest, pre_write_check
from writ.server.models import PromptBundleRequest

REPO = Path(__file__).resolve().parent.parent
SESSION_CLI = REPO / "bin" / "lib" / "writ-session.py"

TS_OLD = "2026-05-01T10:00:00+00:00"
TS_MID = "2026-05-10T10:00:00+00:00"
TS_NEW = "2026-05-20T10:00:00+00:00"


# ---------------------------------------------------------------------------
# Helpers: the isolated graph, seeding, counting
# ---------------------------------------------------------------------------


def _graph(work):
    """Run `async work(db)` against the isolated graph on its own loop and close it."""
    from tests._graph import connection

    async def _go():
        db = connection()
        try:
            return await work(db)
        finally:
            await db.close()

    return asyncio.run(_go())


def _unique(label: str) -> str:
    return f"dr-graph-{label}-{uuid.uuid4().hex[:8]}"


@pytest.fixture()
def made_projects():
    """`register(name)` records a project for teardown; every record it holds is removed."""
    names: list[str] = []
    yield names

    async def _drop(db):
        for name in names:
            await db.clear_project(name)
            await db._run("MATCH (p:Project {name: $n}) DETACH DELETE p", n=name)

    if names:
        _graph(_drop)


async def _seed_decision(db, project, decision_id, *, rationale, ts, rules=("PERF-BATCH-001",),
                         planned=(), title=None):
    await db.create_decision(
        decision_id=decision_id, project=project, title=title or rationale[:80],
        rationale=rationale,
        planned_files=[{"path": p, "reason": r, "resolved": False} for p, r in planned],
        governing_rule_ids=list(rules), phase="harvested", session_id="", ts=ts,
    )


async def _seed_change(db, project, change_id, path, *, reason, ts, decision_id=None,
                       commit_hash=None, subject="feat: seeded change"):
    commit_hash = commit_hash or f"c{uuid.uuid4().hex[:12]}"
    await db.create_commit(
        commit_hash=commit_hash, project=project, subject=subject, author="tester", branch="main",
    )
    await db.create_filechange(
        change_id=change_id, project=project, path=path, change_type="modify",
        commit_hash=commit_hash, reason=reason, ts=ts,
    )
    if decision_id:
        await db.wire_motivated_by(change_id, decision_id, project)
    return commit_hash


@contextlib.contextmanager
def _count_statements():
    """Count (and keep) every statement issued through the connection's three query helpers."""
    from writ.graph.db._query_runner import _QueryRunnerMixin

    state = {"n": 0, "queries": [], "params": []}
    names = ("_run", "_run_single", "_write_single")
    originals = {name: getattr(_QueryRunnerMixin, name) for name in names}

    def _wrap(original):
        async def counted(self, query, **params):
            state["n"] += 1
            state["queries"].append(str(query))
            state["params"].append(dict(params))
            return await original(self, query, **params)

        return counted

    # get_rule_statements runs its one batched read on a driver session directly, not
    # through the three helpers, so it is counted as the one statement it issues.
    from writ.graph.db.rule_store import RuleStoreMixin

    original_statements = RuleStoreMixin.get_rule_statements

    async def counted_statements(self, rule_ids):
        if rule_ids:
            state["n"] += 1
        return await original_statements(self, rule_ids)

    for name in names:
        setattr(_QueryRunnerMixin, name, _wrap(originals[name]))
    RuleStoreMixin.get_rule_statements = counted_statements
    try:
        yield state
    finally:
        for name in names:
            setattr(_QueryRunnerMixin, name, originals[name])
        RuleStoreMixin.get_rule_statements = original_statements


def _plan_ops(plan) -> list[tuple[str, str]]:
    ops: list[tuple[str, str]] = []

    def walk(node):
        if not node:
            return
        args = node.get("args") or {}
        ops.append((str(node.get("operatorType") or ""), str(args.get("Details") or "")))
        for child in node.get("children") or []:
            walk(child)

    walk(plan)
    return ops


# ===========================================================================
# 6, 7. get_decisions_for_paths on the isolated graph
# ===========================================================================


class TestGetDecisionsForPaths:
    @pytest.fixture()
    def project(self, made_projects):
        name = _unique("read")
        made_projects.append(name)
        return name

    def _read(self, project, paths, **kw):
        return _graph(lambda db: db.get_decisions_for_paths(project, paths, **kw))

    def test_traverses_motivated_by_and_returns_the_decision_with_its_change(self, project):
        async def seed(db):
            await _seed_decision(
                db, project, "DEC-1", rationale="Cache the widget lookups. Slow today.", ts=TS_OLD,
                rules=("PERF-BATCH-001", "ERR-FALLBACK-001"),
                planned=[("src/a.py", "keep lookups in a dict")], title="Cache the widget lookups",
            )
            await _seed_change(db, project, "chg-1", "src/a.py", reason="dict-backed lookups",
                               ts=TS_MID, decision_id="DEC-1", commit_hash="abc1234567",
                               subject="feat: cache widget lookups")

        _graph(seed)
        result = self._read(project, ["src/a.py"])

        assert list(result) == ["src/a.py"]
        (hit,) = result["src/a.py"]
        assert hit["decision_id"] == "DEC-1"
        assert hit["title"] == "Cache the widget lookups"
        assert hit["rationale"] == "Cache the widget lookups. Slow today."
        assert hit["governing_rule_ids"] == ["PERF-BATCH-001", "ERR-FALLBACK-001"]
        assert hit["planned_files"][0]["path"] == "src/a.py"
        assert hit["phase"] == "harvested"
        assert hit["decision_ts"] == TS_OLD
        assert hit["reason"] == "dict-backed lookups"
        assert hit["change_ts"] == TS_MID
        assert hit["commit_hash"] == "abc1234567"
        assert hit["commit_subject"] == "feat: cache widget lookups"

    def test_orders_newest_change_first_and_slices_per_path(self, project):
        async def seed(db):
            for n, (ts, hash_) in enumerate([(TS_OLD, "h-old"), (TS_MID, "h-mid"), (TS_NEW, "h-new")], 1):
                await _seed_decision(db, project, f"DEC-{n}", rationale=f"Decision {n}.", ts=ts)
                await _seed_change(db, project, f"chg-{n}", "src/a.py", reason=f"reason {n}",
                                   ts=ts, decision_id=f"DEC-{n}", commit_hash=hash_)

        _graph(seed)
        three = self._read(project, ["src/a.py"], per_path=3)["src/a.py"]
        assert [h["decision_id"] for h in three] == ["DEC-3", "DEC-2", "DEC-1"]
        assert [h["change_ts"] for h in three] == [TS_NEW, TS_MID, TS_OLD]
        two = self._read(project, ["src/a.py"], per_path=2)["src/a.py"]
        assert [h["decision_id"] for h in two] == ["DEC-3", "DEC-2"]
        default = self._read(project, ["src/a.py"])["src/a.py"]
        assert [h["decision_id"] for h in default] == ["DEC-3"], "per_path defaults to one"

    def test_changes_at_the_same_time_order_by_change_id_descending(self, project):
        async def seed(db):
            for n in (2, 3, 1):
                await _seed_decision(db, project, f"DEC-{n}", rationale=f"Decision {n}.", ts=TS_MID)
                await _seed_change(db, project, f"chg-{n}", "src/a.py", reason=f"reason {n}",
                                   ts=TS_MID, decision_id=f"DEC-{n}")

        _graph(seed)
        three = self._read(project, ["src/a.py"], per_path=3)["src/a.py"]
        assert [h["decision_id"] for h in three] == ["DEC-3", "DEC-2", "DEC-1"]
        assert [h["decision_id"] for h in self._read(project, ["src/a.py"])["src/a.py"]] == ["DEC-3"]

    def test_one_decision_reached_through_two_changes_is_returned_once_with_the_latest(self, project):
        async def seed(db):
            await _seed_decision(db, project, "DEC-1", rationale="One decision.", ts=TS_OLD)
            await _seed_change(db, project, "chg-1", "src/a.py", reason="first touch",
                               ts=TS_OLD, decision_id="DEC-1")
            await _seed_change(db, project, "chg-2", "src/a.py", reason="second touch",
                               ts=TS_NEW, decision_id="DEC-1")

        _graph(seed)
        hits = self._read(project, ["src/a.py"], per_path=3)["src/a.py"]
        assert [h["decision_id"] for h in hits] == ["DEC-1"]
        assert hits[0]["reason"] == "second touch"

    def test_returns_each_matched_path_and_omits_the_unmatched(self, project):
        async def seed(db):
            await _seed_decision(db, project, "DEC-1", rationale="A.", ts=TS_OLD)
            await _seed_decision(db, project, "DEC-2", rationale="B.", ts=TS_NEW)
            await _seed_change(db, project, "chg-a", "src/a.py", reason="a", ts=TS_OLD, decision_id="DEC-1")
            await _seed_change(db, project, "chg-b", "src/b.py", reason="b", ts=TS_NEW, decision_id="DEC-2")

        _graph(seed)
        result = self._read(project, ["src/a.py", "src/b.py", "src/never_changed.py"])
        assert set(result) == {"src/a.py", "src/b.py"}
        assert result["src/a.py"][0]["decision_id"] == "DEC-1"
        assert result["src/b.py"][0]["decision_id"] == "DEC-2"

    def test_never_returns_another_projects_records(self, project, made_projects):
        other = _unique("other")
        made_projects.append(other)

        async def seed(db):
            for proj, title in ((project, "Mine"), (other, "Theirs")):
                await _seed_decision(db, proj, "DEC-SAME", rationale=f"{title}.", ts=TS_OLD, title=title)
                await _seed_change(db, proj, "chg-same", "src/a.py", reason=f"{title} reason",
                                   ts=TS_OLD, decision_id="DEC-SAME")

        _graph(seed)
        mine = self._read(project, ["src/a.py"], per_path=5)["src/a.py"]
        assert [(h["title"], h["reason"]) for h in mine] == [("Mine", "Mine reason")]
        theirs = self._read(other, ["src/a.py"], per_path=5)["src/a.py"]
        assert [h["title"] for h in theirs] == ["Theirs"]

    def test_a_change_with_no_motivated_by_edge_never_returns(self, project):
        async def seed(db):
            await _seed_change(db, project, "chg-unlinked", "src/a.py", reason="unplanned fix",
                               ts=TS_NEW, decision_id=None)

        _graph(seed)
        assert self._read(project, ["src/a.py"]) == {}

    def test_the_decision_behind_the_most_recent_linked_change_wins_over_a_newer_unlinked_one(self, project):
        async def seed(db):
            await _seed_decision(db, project, "DEC-1", rationale="Planned change.", ts=TS_OLD)
            await _seed_change(db, project, "chg-planned", "src/a.py", reason="planned",
                               ts=TS_MID, decision_id="DEC-1")
            await _seed_change(db, project, "chg-fixup", "src/a.py", reason="fix-up commit subject",
                               ts=TS_NEW, decision_id=None)

        _graph(seed)
        hits = self._read(project, ["src/a.py"])["src/a.py"]
        assert [h["decision_id"] for h in hits] == ["DEC-1"]
        assert hits[0]["reason"] == "planned"

    def test_empty_paths_return_an_empty_dict_without_a_statement(self, project):
        async def work(db):
            with _count_statements() as state:
                result = await db.get_decisions_for_paths(project, [])
            return result, state["n"]

        result, issued = _graph(work)
        assert result == {} and issued == 0

    def test_the_read_plans_an_index_seek_on_filechange_project_path(self, project):
        async def seed(db):
            await _seed_decision(db, project, "DEC-1", rationale="A.", ts=TS_OLD)
            await _seed_change(db, project, "chg-a", "src/a.py", reason="a", ts=TS_OLD, decision_id="DEC-1")

        _graph(seed)

        async def explain(db):
            with _count_statements() as state:
                await db.get_decisions_for_paths(project, ["src/a.py"], per_path=3)
            assert state["n"] == 1, "one batched statement"
            query, params = state["queries"][0], state["params"][0]
            async with db._driver.session(database=db._database) as session:
                result = await session.run("EXPLAIN " + query, **params)
                summary = await result.consume()
            return _plan_ops(summary.plan)

        ops = _graph(explain)
        seeks = [d for op, d in ops if "IndexSeek" in op and "FileChange" in d]
        assert seeks, f"expected an index seek on FileChange; plan was {ops!r}"
        assert any("path" in d for d in seeks), "the seek must use the (project, path) index"
        label_scans = [d for op, d in ops if "NodeByLabelScan" in op and "FileChange" in d]
        assert label_scans == [], f"FileChange must not be label-scanned; plan was {ops!r}"


# ===========================================================================
# 14. Tombstoned memories never reach a card (real list_memories)
# ===========================================================================


class TestTombstonedMemories:
    def test_a_tombstoned_memory_never_appears_but_a_live_one_does(self, made_projects):
        project = _unique("memories")
        made_projects.append(project)

        async def work(db):
            from writ.session.recall import compile_recall

            await db.create_memory(name="live_lemonsqueezer", project=project,
                                   description="Lemonsqueezer notes live in the wiki.", type="reference")
            await db.create_memory(name="gone_lemonsqueezer", project=project,
                                   description="Lemonsqueezer notes that were deleted.", type="reference")
            await db.tombstone_missing_memories(project, ["live_lemonsqueezer"])
            return await compile_recall(db, project, prompt="check lemonsqueezer")

        result = _graph(work)
        assert [m["name"] for m in result["memories"]] == ["live_lemonsqueezer"]
        assert "gone_lemonsqueezer" not in result["briefing"]
        assert "memory live_lemonsqueezer:" in result["briefing"]


# ===========================================================================
# 35. Statement counts (the Query Budget Plan)
# ===========================================================================


@pytest.fixture()
def history(made_projects, tmp_path):
    """A registered project with one decision, its linked change to src/a.py and one memory."""
    name = _unique("budget")
    made_projects.append(name)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    async def seed(db):
        await db.create_project(name, str(repo_root), str(repo_root))
        await _seed_decision(
            db, name, "DEC-1", rationale="Cache the widget lookups. Slow today.", ts=TS_MID,
            planned=[("src/a.py", "keep lookups in a dict")],
        )
        await _seed_change(db, name, "chg-1", "src/a.py", reason="dict-backed lookups",
                           ts=TS_MID, decision_id="DEC-1")
        await db.create_memory(name="reference_notes", project=name, description="Some notes.", type="reference")

    _graph(seed)
    return SimpleNamespace(project=name, root=str(repo_root))


def _compile_count(history, steps):
    """Run `steps` (a list of compile_recall kwargs) on ONE connection; return per-step counts."""
    async def work(db):
        from writ.session.recall import compile_recall

        counts = []
        for kwargs in steps:
            with _count_statements() as state:
                await compile_recall(db, history.project, **kwargs)
            counts.append(state["n"])
        return counts

    return _graph(work)


class TestRecallStatementCounts:
    def test_a_cold_compile_with_a_path_in_the_prompt_issues_three_statements(self, history):
        (cold,) = _compile_count(history, [dict(prompt="update src/a.py", project_root=history.root)])
        assert cold == 3

    def test_a_cold_compile_without_a_path_issues_two_statements(self, history):
        (cold,) = _compile_count(history, [dict(prompt="ok, continue with the next step")])
        assert cold == 2

    def test_within_the_ttl_a_compile_without_a_path_issues_none_and_with_a_path_one(self, history):
        cold, warm_plain, warm_path = _compile_count(history, [
            dict(prompt="ok, continue"),
            dict(prompt="ok, continue again"),
            dict(prompt="update src/a.py", project_root=history.root),
        ])
        assert (cold, warm_plain, warm_path) == (2, 0, 1)

    def test_full_adds_exactly_the_one_rule_statement_read(self, history):
        from writ.session.recall import RECALL_FULL_BUDGET

        (plain,) = _compile_count(history, [dict(prompt="ok")])
        (full,) = _compile_count(history, [dict(prompt="ok", full=True, budget=RECALL_FULL_BUDGET)])
        assert (plain, full) == (2, 3)


def _write_request(path: str) -> PreWriteCheckRequest:
    return PreWriteCheckRequest(
        session_id="dr-write-counts", tool_input={"file_path": path}, file_path=path, skill_dir="/x",
    )


def _install_gate(monkeypatch, db, cache: dict) -> None:
    pipeline = MagicMock()
    pipeline.query.return_value = {"rules": [], "mode": "standard", "total_candidates": 0, "latency_ms": 1}
    monkeypatch.setattr(server, "_db", db)
    monkeypatch.setattr(server, "_pipeline", pipeline)
    monkeypatch.setattr(server.writ_session, "_read_cache", lambda sid: dict(cache))
    monkeypatch.setattr(
        server.writ_session, "_can_write_check",
        lambda sid, env, skill, cache=None: {"can_write": True, "reason": None},
    )


def _gate_cache(root: str) -> dict:
    return {
        "mode": "work", "remaining_budget": 1500, "project_root": root,
        "loaded_rule_ids_by_phase": {}, "current_phase": "", "loaded_rule_ids": [],
        "compaction_epoch": 0, "injection_shown": {},
    }


class TestWriteStatementCounts:
    def test_an_allowed_write_to_a_file_with_history_issues_exactly_one_statement(
        self, history, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))

        async def work(db):
            _install_gate(monkeypatch, db, _gate_cache(history.root))
            await db.resolve_project_for_cwd(history.root)  # a warm registry, as in the plan
            with _count_statements() as state:
                result = await pre_write_check(_write_request(os.path.join(history.root, "src/a.py")))
            return result, state["n"]

        result, issued = _graph(work)
        assert result["decision"] == "allow"
        # Program item 7a/7d: decision card, open questions and co-change, concurrently.
        assert issued == 3
        assert "Cache the widget lookups" in result["decision_context"]

    def test_an_allowed_write_outside_the_project_issues_no_statement(self, history, tmp_path, monkeypatch):
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))

        async def work(db):
            _install_gate(monkeypatch, db, _gate_cache(history.root))
            await db.resolve_project_for_cwd(history.root)
            with _count_statements() as state:
                result = await pre_write_check(_write_request("/x/elsewhere/other.py"))
            return result, state["n"]

        result, issued = _graph(work)
        assert result["decision"] == "allow"
        assert issued == 0
        assert result["decision_context"] == ""


# ===========================================================================
# 36. End to end: harvest, recall, pre-write
# ===========================================================================

_PLAN = (
    "## Analysis\n\n"
    "Cache the widget lookups in memory. The daemon re-reads them on every prompt, which is slow.\n\n"
    "## Files\n\n"
    "- `src/widget_cache.py` (modify) -- keep the lookups in an in-memory dict\n\n"
    "## Rules Applied\n\n"
    "- **ERR-FALLBACK-001**: fail open\n"
)


class TestEndToEnd:
    @pytest.fixture()
    def chain(self, made_projects, tmp_path, monkeypatch):
        name = _unique("e2e")
        made_projects.append(name)
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        monkeypatch.setenv("WRIT_CACHE_DIR", str(cache_dir))
        monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path / "logs"))
        monkeypatch.setenv("WRIT_LOG_PROJECT", name)
        return SimpleNamespace(project=name, root=str(repo_root), sid=f"dr-e2e-{uuid.uuid4().hex[:6]}")

    async def _harvest(self, db, chain):
        from writ.session.harvester import harvest_one_commit

        await db.create_project(chain.project, chain.root, chain.root)
        return await harvest_one_commit(
            db, chain.project, commit_hash="e2e0001", subject="feat: cache widget lookups",
            author="tester", branch="main", commit_ts=TS_MID,
            files=[{"path": "src/widget_cache.py", "change_type": "modify"}],
            plan_text=_PLAN, plan_ts=TS_OLD,
        )

    def _seed_session_cache(self, chain):
        from writ.session.cache import mutate_cache

        with mutate_cache(chain.sid) as data:
            data.update(
                mode="work", project_root=chain.root, remaining_budget=1500,
                loaded_rule_ids_by_phase={}, current_phase="", loaded_rule_ids=[],
            )

    def test_the_harvested_decision_has_a_real_title_and_is_read_back_by_path(self, chain):
        async def work(db):
            stats = await self._harvest(db, chain)
            recent = await db.get_recent_decisions(chain.project)
            by_path = await db.get_decisions_for_paths(chain.project, ["src/widget_cache.py"])
            return stats, recent, by_path

        stats, recent, by_path = _graph(work)
        assert stats["decision_id"]
        assert recent[0]["title"].rstrip(".") == "Cache the widget lookups in memory"
        (hit,) = by_path["src/widget_cache.py"]
        assert hit["decision_id"] == stats["decision_id"]
        assert hit["reason"] == "keep the lookups in an in-memory dict"
        assert hit["commit_subject"] == "feat: cache widget lookups"

    def test_recall_returns_the_card_with_the_files_reason_then_the_write_shows_its_context_once(
        self, chain, monkeypatch
    ):
        from writ.session.cache import _read_cache

        async def work(db):
            stats = await self._harvest(db, chain)
            self._seed_session_cache(chain)
            monkeypatch.setattr(server, "_db", db)
            monkeypatch.setattr(server, "_pipeline", MagicMock(
                query=MagicMock(return_value={"rules": [], "mode": "standard",
                                              "total_candidates": 0, "latency_ms": 1})))
            monkeypatch.setattr(server, "_trigger_index", SimpleNamespace(floor_ids=lambda mode: set()))
            monkeypatch.setattr(
                server.writ_session, "_can_write_check",
                lambda sid, env, skill, cache=None: {"can_write": True, "reason": None},
            )

            first = await qroute.prompt_bundle(PromptBundleRequest(
                session_id=chain.sid, prompt="please tweak src/widget_cache.py", mode="work",
                sections=["recall"], project_root=chain.root,
            ))
            repeat = await qroute.prompt_bundle(PromptBundleRequest(
                session_id=chain.sid, prompt="and src/widget_cache.py again", mode="work",
                sections=["recall"], project_root=chain.root,
            ))
            path = os.path.join(chain.root, "src/widget_cache.py")
            request = PreWriteCheckRequest(
                session_id=chain.sid, tool_input={"file_path": path}, file_path=path, skill_dir="/x")
            write_one = await pre_write_check(request)
            write_two = await pre_write_check(request)
            return stats, first, repeat, write_one, write_two

        stats, first, repeat, write_one, write_two = _graph(work)

        block = first["recall_block"]
        assert "Cache the widget lookups in memory" in block
        assert "src/widget_cache.py: keep the lookups in an in-memory dict" in block
        assert len(block) <= 1998
        assert repeat["recall_block"] == "", "the decision was shown earlier in this epoch"
        assert _read_cache(chain.sid)["injection_shown"]["recall"] == [stats["decision_id"]]

        context = write_one["decision_context"]
        assert context.startswith("[Writ decision memory:")
        assert "src/widget_cache.py" in context
        assert "Cache the widget lookups in memory" in context
        assert "keep the lookups in an in-memory dict" in context
        assert "feat: cache widget lookups" in context
        assert "\n" not in context and len(context) <= 1000
        assert write_two["decision_context"] == "", "once per (path, decision) per epoch"
        assert f"src/widget_cache.py#{stats['decision_id']}" in (
            _read_cache(chain.sid)["injection_shown"]["pre_write_decision"]
        )


# ===========================================================================
# 26. Concurrent shown-record writes on a real cache
# ===========================================================================


@pytest.fixture()
def cache_dir(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "cache"
    path.mkdir()
    monkeypatch.setenv("WRIT_CACHE_DIR", str(path))
    return path


def _read(sid: str) -> dict:
    return importlib.import_module("writ.session.cache")._read_cache(sid)


class _Sections:
    """Four /prompt-bundle sections against the REAL session cache; only retrieval is faked."""

    def __init__(self, monkeypatch):
        self.ranked = [{
            "rule_id": f"RK-{i:03d}", "severity": "high", "authority": "human", "score": 0.9,
            "trigger": f"ranked trigger {i}", "statement": f"ranked statement {i}",
        } for i in range(3)]
        self.always_on = [{"rule_id": "AO-001", "trigger": "always-on trigger",
                           "statement": "ALWAYS-ON STATEMENT " + "a" * 60}]
        self.floor = [{
            "rule_id": "FLOOR-001", "node_type": "Playbook", "channel": "floor",
            "trigger": "floor trigger", "statement": "FLOOR STATEMENT " + "f" * 60,
            "severity": "high", "authority": "human", "domain": "process", "score": 1.0,
            "relationships": [],
        }]
        monkeypatch.setattr(server, "_pipeline", object())
        monkeypatch.setattr(server, "_trigger_index", SimpleNamespace(floor_ids=lambda mode: {"FLOOR-001"}))
        monkeypatch.setattr(qroute, "query_rules", AsyncMock(
            return_value={"mode": "standard", "rules": self.ranked}))
        monkeypatch.setattr(qroute, "always_on_bundle", AsyncMock(
            side_effect=lambda **kw: {"total_tokens": 100, "rules": list(self.always_on)}))
        monkeypatch.setattr(qroute, "methodology_companion", AsyncMock(
            side_effect=lambda req: {"mode": "summary", "total_tokens": 100,
                                     "rules": [dict(r) for r in self.floor]}))
        card = {"id": "DEC-A", "head": "- Card A title [RULE-A]"}
        decision_memory = importlib.import_module("writ.server.routes.decision_memory")
        monkeypatch.setattr(decision_memory, "recall", AsyncMock(return_value={
            "ok": True, "briefing": "[Writ recall]\n" + card["head"], "decisions": [],
            "memories": [], "cards": [card]}))

    async def turn(self, sid: str, section: str) -> dict:
        return await qroute.prompt_bundle(PromptBundleRequest(
            session_id=sid, prompt="please implement the thing", mode="work", sections=[section]))


class TestConcurrentShownRecords:
    @pytest.mark.asyncio
    async def test_four_sections_gathered_on_one_session_lose_no_recall_or_always_on_write(
        self, cache_dir, monkeypatch
    ):
        from writ.session.cache import mutate_cache

        sections = _Sections(monkeypatch)
        for round_no in range(10):
            sid = f"dr-gather-{round_no}"
            with mutate_cache(sid) as data:
                data.update(mode="work", current_phase="planning")
            results = await asyncio.gather(*(sections.turn(sid, s) for s in
                                             ("ranked", "always_on", "methodology", "recall")))
            assert all(r["error"] is False and r["skipped"] is False for r in results)
            assert "Card A title" in results[3]["recall_block"]

            cache = _read(sid)
            shown = cache["injection_shown"]
            assert shown["recall"] == ["DEC-A"], f"round {round_no}: the recall mark was lost"
            assert shown["always_on"] == ["AO-001"], f"round {round_no}: the always-on mark was lost"
            assert shown["floor"] == ["FLOOR-001"], f"round {round_no}: the floor mark was lost"
            assert cache["queries"] == 2, f"round {round_no}: ranked and methodology each count one query"
            assert cache["always_on_rule_ids"] == ["AO-001"]

    def test_parallel_mark_shown_recall_processes_lose_no_id(self, cache_dir):
        ids = [f"DEC-P{i:02d}" for i in range(12)]
        procs = [
            subprocess.Popen(
                [sys.executable, str(SESSION_CLI), "update", "dr-procs",
                 "--mark-shown", "recall", "0|", json.dumps([rid])],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env={**os.environ, "WRIT_CACHE_DIR": str(cache_dir)},
            )
            for rid in ids
        ]
        for proc in procs:
            _out, err = proc.communicate(timeout=60)
            assert proc.returncode == 0, err
        assert _read("dr-procs")["injection_shown"] == {"epoch": "0|", "recall": sorted(ids)}

    def test_parallel_recall_and_pre_write_marks_mixed_with_always_on_lose_nothing(self, cache_dir):
        env = {**os.environ, "WRIT_CACHE_DIR": str(cache_dir)}
        argsets = [
            ["--mark-shown", "recall", "0|", '["DEC-1"]'],
            ["--mark-shown", "pre_write_decision", "0|", '["a.py#DEC-1"]'],
            ["--mark-shown", "always_on", "0|", '["AO-1"]'],
            ["--add-rules", '["M1"]', "--cost", "10", "--inc-queries"],
        ] * 3
        procs = [
            subprocess.Popen([sys.executable, str(SESSION_CLI), "update", "dr-mixed", *args],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
            for args in argsets
        ]
        for proc in procs:
            _out, err = proc.communicate(timeout=60)
            assert proc.returncode == 0, err
        cache = _read("dr-mixed")
        assert cache["injection_shown"] == {
            "epoch": "0|", "recall": ["DEC-1"], "pre_write_decision": ["a.py#DEC-1"], "always_on": ["AO-1"],
        }
        assert cache["queries"] == 3
