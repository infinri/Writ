"""Program item 7d (co-change hints): what only a real graph can prove.

ENF-SYS-005. Plan: .claude/plans/1c1f801f-c493-4e74-aa60-76c1e69ea30e/plan.md, section 5. This
module covers ONLY 7d (get_cochanged_paths and the co-change block on real rows); 7a's question
read is tested in tests/test_open_questions_graph.py. Every claim is about what the graph
returns, how many statements it was asked, or which index its plan seeks, so none of it runs
against a mock. It runs on the isolated instance (scripts/test-graph.sh, port 7688;
tests/_graph.py is the only door) through the helpers of tests/test_decision_recall_graph.py.

  * support and confidence thresholds, each at the threshold, just above, well above and just
    below (ENF-POST-005)
  * the commit-size cap (about 20 files) counted at query time from FileChange rows: at, just
    over and well over the cap; distinct paths; a lockfile still counting toward its commit's
    size; a file added to a commit AFTER a first read flips it (no stored count); the same
    commit_hash in another project never counting
  * the 200-commit window, ordering and limit, the matched path, project scoping
  * the write-context block and the route over real rows, including the once-per-epoch dedupe
    and the exclusion of lockfiles from the hints
  * EXPLAIN: filechange_project_path and filechange_project_commit_hash are sought, FileChange
    is never label-scanned
  * statement counts: one statement per co-change read, none for empty paths, none for a
    deduped, noise or out-of-project write

Interface (plan section 5): db.get_cochanged_paths(project, paths, *, max_files, recent_commits,
min_support, min_confidence, limit) -> {"path", "base", "hits": [{"path", "support"}]}.

RED today: Neo4jConnection has no get_cochanged_paths and writ.session.write_context does not
exist.

Run: flock <lock> .venv/bin/python3 -m pytest tests/test_cochange_hints_graph.py
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import uuid
from unittest.mock import MagicMock

import pytest

import writ.server as server
from tests.test_decision_recall_graph import (  # noqa: F401  (made_projects is a fixture)
    _count_statements,
    _graph,
    _plan_ops,
    _unique,
    made_projects,
)
from writ.server import PreWriteCheckRequest, pre_write_check

A = "src/a.py"
T0 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)

DEFAULTS = dict(max_files=20, recent_commits=200, min_support=2, min_confidence=0.4, limit=20)


def _ts(i: int) -> str:
    return (T0 + dt.timedelta(minutes=i)).isoformat()


async def _commit(db, project: str, commit_hash: str, paths: list[str], ts: str) -> None:
    """One commit with one FileChange row per entry in `paths` (a repeated path is a second row)."""
    await db.create_commit(commit_hash=commit_hash, project=project, subject="seed", author="t",
                           branch="main", ts=ts)
    await asyncio.gather(*(
        db.create_filechange(change_id=f"chg-{uuid.uuid4().hex[:12]}", project=project, path=p,
                             change_type="modify", commit_hash=commit_hash, reason="seed", ts=ts)
        for p in paths
    ))


async def _commits(db, project: str, specs: list[tuple[str, list[str], str]], chunk: int = 20) -> None:
    for i in range(0, len(specs), chunk):
        await asyncio.gather(*(_commit(db, project, h, p, t) for h, p, t in specs[i:i + chunk]))


def _h() -> str:
    return f"c{uuid.uuid4().hex[:12]}"


@pytest.fixture()
def project(made_projects):
    name = _unique("cochange")
    made_projects.append(name)
    return name


def _read(project, paths, **over):
    kw = {**DEFAULTS, **over}
    return _graph(lambda db: db.get_cochanged_paths(project, paths, **kw))


def _hits(result) -> dict[str, int]:
    return {h["path"]: h["support"] for h in ((result or {}).get("hits") or [])}


def _seed_pairs(project: str, base: int, with_x: int, x: str = "src/x.py") -> None:
    """`base` normal commits touching A; the first `with_x` of them also touch X."""
    specs = [(_h(), [A] + ([x] if i < with_x else []), _ts(i)) for i in range(base)]
    _graph(lambda db: _commits(db, project, specs))


# ===========================================================================
# Support and confidence: at, just above, well above, just below (ENF-POST-005)
# ===========================================================================


class TestSupportThreshold:
    """Minimum support is 2. Confidence is 1.0 in every case, so only support decides."""

    @pytest.mark.parametrize("base,with_x,returned", [
        pytest.param(2, 2, True, id="at-threshold-2"),
        pytest.param(3, 3, True, id="just-above-3"),
        pytest.param(10, 10, True, id="well-above-10"),
        pytest.param(2, 1, False, id="just-below-1-of-2"),
        pytest.param(1, 1, False, id="just-below-1-of-1"),
    ])
    def test_a_pair_is_returned_only_at_or_above_the_minimum_support(self, project, base, with_x, returned):
        _seed_pairs(project, base, with_x)
        result = _read(project, [A])
        if returned:
            assert _hits(result) == {"src/x.py": with_x}
            assert result["base"] == base
            assert result["path"] == A
        else:
            assert _hits(result) == {}


class TestConfidenceThreshold:
    """Minimum confidence is 0.4; base is 15, so support 6 is exactly 0.4 (6/15), 7 just above,
    14 well above, 5 just below."""

    @pytest.mark.parametrize("with_x,returned", [
        pytest.param(6, True, id="at-threshold-6-of-15"),
        pytest.param(7, True, id="just-above-7-of-15"),
        pytest.param(14, True, id="well-above-14-of-15"),
        pytest.param(5, False, id="just-below-5-of-15"),
        pytest.param(2, False, id="support-at-minimum-but-confidence-low-2-of-15"),
    ])
    def test_a_pair_is_returned_only_at_or_above_the_minimum_confidence(self, project, with_x, returned):
        _seed_pairs(project, 15, with_x)
        result = _read(project, [A])
        if returned:
            assert _hits(result) == {"src/x.py": with_x}
            assert result["base"] == 15
        else:
            assert _hits(result) == {}

    def test_the_thresholds_are_the_parameters_not_constants_in_the_statement(self, project):
        _seed_pairs(project, 10, 3)
        assert _hits(_read(project, [A])) == {}
        assert _hits(_read(project, [A], min_confidence=0.3)) == {"src/x.py": 3}
        assert _hits(_read(project, [A], min_support=4, min_confidence=0.0)) == {}


class TestOrderingAndLimit:
    def test_hits_order_by_support_descending_then_path_ascending(self, project):
        specs = []
        for i in range(6):
            partners = ["src/z.py"]
            if i < 5:
                partners.append("src/m.py")
            if i < 5:
                partners.append("src/b.py")
            if i < 4:
                partners.append("src/c.py")
            specs.append((_h(), [A] + partners, _ts(i)))
        _graph(lambda db: _commits(db, project, specs))
        result = _read(project, [A])
        assert [h["path"] for h in result["hits"]] == ["src/z.py", "src/b.py", "src/m.py", "src/c.py"]
        assert [h["support"] for h in result["hits"]] == [6, 5, 5, 4]

    def test_the_limit_parameter_bounds_the_rows_returned(self, project):
        specs = [(_h(), [A, "src/p.py", "src/q.py", "src/r.py"], _ts(i)) for i in range(3)]
        _graph(lambda db: _commits(db, project, specs))
        assert [h["path"] for h in _read(project, [A], limit=2)["hits"]] == ["src/p.py", "src/q.py"]

    def test_the_result_names_the_candidate_that_matched(self, project):
        _seed_pairs(project, 3, 3)
        result = _read(project, ["zzz/none.py", A, "other/none.py"])
        assert result["path"] == A
        assert _hits(result) == {"src/x.py": 3}

    def test_a_file_with_no_history_returns_no_hits(self, project):
        _seed_pairs(project, 3, 3)
        assert _hits(_read(project, ["src/never-changed.py"])) == {}


# ===========================================================================
# The commit-size cap, counted at query time
# ===========================================================================


def _filler(n: int, tag: str) -> list[str]:
    return [f"fill/{tag}-{i}.py" for i in range(n)]


class TestCommitSizeCap:
    """Each scenario has A and X in every commit, plus filler to reach a total file count. Commits
    over 20 files leave BOTH support and base."""

    @pytest.mark.parametrize("size,kept", [
        pytest.param(19, True, id="just-below-19"),
        pytest.param(20, True, id="at-cap-20"),
        pytest.param(21, False, id="just-over-21"),
        pytest.param(60, False, id="well-over-60"),
    ])
    def test_a_commit_over_the_cap_counts_toward_neither_support_nor_base(self, project, size, kept):
        specs = [(_h(), [A, "src/x.py"], _ts(0)), (_h(), [A, "src/x.py"], _ts(1)),
                 (_h(), [A, "src/x.py"] + _filler(size - 2, "big"), _ts(2))]
        _graph(lambda db: _commits(db, project, specs))
        result = _read(project, [A])
        expected = 3 if kept else 2
        assert result["base"] == expected
        assert result["hits"][0] == {"path": "src/x.py", "support": expected}

    def test_a_commit_that_gains_a_file_after_a_read_is_excluded_by_the_next_read(self, project):
        """No stored count: the size is whatever FileChange rows exist when the read runs."""
        big = _h()
        specs = [(_h(), [A, "src/x.py"], _ts(0)), (big, [A, "src/x.py"] + _filler(18, "g"), _ts(1))]
        _graph(lambda db: _commits(db, project, specs))
        assert _read(project, [A])["base"] == 2

        async def grow(db):
            await db.create_filechange(change_id=f"chg-{uuid.uuid4().hex[:12]}", project=project,
                                       path="fill/one-more.py", change_type="modify",
                                       commit_hash=big, reason="late", ts=_ts(1))

        _graph(grow)
        after = _read(project, [A])
        assert after["base"] == 1
        assert _hits(after) == {}, "support 1 is below the minimum"

    def test_a_commit_stores_no_file_count(self, project):
        _graph(lambda db: _commits(db, project, [(_h(), [A, "src/x.py"], _ts(0))]))

        async def props(db):
            rows = await db._run("MATCH (c:Commit {project: $p}) RETURN keys(c) AS k", p=project)
            return {k for row in rows for k in row["k"]}

        assert not [k for k in _graph(props) if "count" in k.lower() or "size" in k.lower() or "files" in k.lower()]

    def test_distinct_paths_are_counted_so_a_repeated_path_does_not_inflate_the_commit(self, project):
        twenty = [A, "src/x.py"] + _filler(18, "d")
        specs = [(_h(), twenty + [A, "fill/d-0.py"], _ts(0)), (_h(), twenty, _ts(1))]
        _graph(lambda db: _commits(db, project, specs))
        result = _read(project, [A])
        assert result["base"] == 2
        assert _hits(result)["src/x.py"] == 2

    def test_a_lockfile_in_a_commit_still_counts_toward_its_size(self, project):
        """19 files plus a lockfile is 20 (kept); 20 files plus a lockfile is 21 (dropped)."""
        specs = [
            (_h(), [A, "src/x.py", "composer.lock"] + _filler(17, "k"), _ts(0)),
            (_h(), [A, "src/x.py", "composer.lock"] + _filler(18, "l"), _ts(1)),
            (_h(), [A, "src/x.py"], _ts(2)),
        ]
        _graph(lambda db: _commits(db, project, specs))
        result = _read(project, [A])
        assert result["base"] == 2
        assert _hits(result)["src/x.py"] == 2

    def test_the_cap_is_the_parameter(self, project):
        specs = [(_h(), [A, "src/x.py"] + _filler(8, f"p{i}"), _ts(i)) for i in range(3)]
        _graph(lambda db: _commits(db, project, specs))
        assert _read(project, [A], max_files=10)["base"] == 3
        assert _hits(_read(project, [A], max_files=9)) == {}


# ===========================================================================
# The recent-commit window
# ===========================================================================


class TestRecentCommitWindow:
    def test_only_the_two_hundred_most_recent_commits_of_the_file_are_considered(self, project):
        """205 commits touch A. The 200 newest also touch recent.py; the 5 oldest touch old.py.
        Windowed: base 200, recent.py support 200, old.py absent. Unwindowed: base 205."""
        specs = [(_h(), [A, "src/old.py" if i < 5 else "src/recent.py"], _ts(i)) for i in range(205)]
        _graph(lambda db: _commits(db, project, specs))
        result = _read(project, [A])
        assert result["base"] == 200
        assert _hits(result) == {"src/recent.py": 200}

    def test_the_window_is_the_parameter_and_keeps_the_newest(self, project):
        specs = [(_h(), [A, "src/old.py" if i < 3 else "src/new.py"], _ts(i)) for i in range(8)]
        _graph(lambda db: _commits(db, project, specs))
        windowed = _read(project, [A], recent_commits=5)
        assert windowed["base"] == 5
        assert _hits(windowed) == {"src/new.py": 5}

    def test_a_commit_over_the_cap_does_not_use_a_window_slot_for_support(self, project):
        """The window takes the newest commits of the file; the cap then filters them, so base is
        counted over the survivors only."""
        specs = [(_h(), [A, "src/x.py"], _ts(0)), (_h(), [A, "src/x.py"], _ts(1)),
                 (_h(), [A, "src/x.py"] + _filler(30, "huge"), _ts(2))]
        _graph(lambda db: _commits(db, project, specs))
        result = _read(project, [A], recent_commits=3)
        assert result["base"] == 2


# ===========================================================================
# Project scoping
# ===========================================================================


class TestProjectScoping:
    def test_another_projects_commits_never_count(self, project, made_projects):
        other = _unique("cochange-other")
        made_projects.append(other)
        mine = [(_h(), [A, "src/x.py"], _ts(i)) for i in range(2)]
        theirs = [(_h(), [A, "src/x.py", "src/y.py"], _ts(i)) for i in range(5)]

        async def seed(db):
            await _commits(db, project, mine)
            await _commits(db, other, theirs)

        _graph(seed)
        result = _read(project, [A])
        assert result["base"] == 2
        assert _hits(result) == {"src/x.py": 2}
        assert _hits(_read(other, [A])) == {"src/x.py": 5, "src/y.py": 5}

    def test_the_same_commit_hash_in_another_project_does_not_change_this_commits_size(
            self, project, made_projects):
        other = _unique("cochange-other")
        made_projects.append(other)
        shared = _h()

        async def seed(db):
            await _commits(db, project, [(shared, [A, "src/x.py"], _ts(0)), (_h(), [A, "src/x.py"], _ts(1))])
            await _commit(db, other, shared, _filler(40, "o"), _ts(0))

        _graph(seed)
        result = _read(project, [A])
        assert result["base"] == 2
        assert _hits(result) == {"src/x.py": 2}

    def test_a_project_with_no_commits_returns_no_hits(self, project):
        assert _hits(_read(project, [A])) == {}


# ===========================================================================
# Exclusion, on real rows, through the write-context block
# ===========================================================================


def _context(project, candidates, shown=None, timeout_s=0.9):
    from writ.session.write_context import write_context

    sections = ("pre_write_decision", "pre_write_questions", "pre_write_cochange")
    shown = shown or {s: set() for s in sections}
    return _graph(lambda db: write_context(db, project, candidates, [], shown, timeout_s=timeout_s))


class TestExclusionOnRealRows:
    def test_a_lockfile_that_always_changes_with_the_file_is_never_suggested(self, project):
        specs = [(_h(), [A, "composer.lock", "src/real.py"], _ts(i)) for i in range(3)]
        _graph(lambda db: _commits(db, project, specs))
        assert "composer.lock" in _hits(_read(project, [A])), "the store returns it; Python drops it"
        ctx = _context(project, [A])
        assert "composer.lock" not in ctx.text
        assert ctx.text == "[Writ co-change] src/a.py usually changes with src/real.py (3 of 3 recent commits)."

    def test_noise_among_the_top_rows_does_not_crowd_out_real_partners(self, project):
        noise = ["composer.lock", "yarn.lock", "dist/a.js"]
        specs = [(_h(), [A] + noise + ["src/p.py", "src/q.py", "src/r.py"], _ts(i)) for i in range(3)]
        _graph(lambda db: _commits(db, project, specs))
        lines = _context(project, [A]).text.split("\n")
        assert [ln.split(" changes with ")[1].split(" ")[0] for ln in lines] == [
            "src/p.py", "src/q.py", "src/r.py"]

    def test_at_most_three_lines_over_five_real_partners(self, project):
        partners = [f"src/p{i}.py" for i in range(5)]
        specs = [(_h(), [A] + partners, _ts(i)) for i in range(3)]
        _graph(lambda db: _commits(db, project, specs))
        assert len(_context(project, [A]).text.split("\n")) == 3

    def test_a_write_to_a_lockfile_issues_no_statement(self, project):
        _graph(lambda db: _commits(db, project, [(_h(), ["composer.lock", "src/x.py"], _ts(i)) for i in range(3)]))

        async def work(db):
            from writ.session.write_context import write_context

            with _count_statements() as state:
                ctx = await write_context(db, project, ["composer.lock"], [],
                                          {s: set() for s in ("pre_write_decision", "pre_write_questions",
                                                              "pre_write_cochange")}, timeout_s=0.9)
            return ctx, [q for q, p in zip(state["queries"], state["params"]) if "recent_commits" in p]

        ctx, cochange_statements = _graph(work)
        assert cochange_statements == []
        assert "[Writ co-change]" not in ctx.text


# ===========================================================================
# EXPLAIN: seeks, not scans
# ===========================================================================


class TestIndexSeeks:
    def test_the_read_seeks_both_filechange_indexes_and_never_label_scans_filechange(self, project):
        _seed_pairs(project, 3, 3)

        async def explain(db):
            with _count_statements() as state:
                await db.get_cochanged_paths(project, [A], **DEFAULTS)
            assert state["n"] == 1, "one batched statement"
            query, params = state["queries"][0], state["params"][0]
            async with db._driver.session(database=db._database) as session:
                result = await session.run("EXPLAIN " + query, **params)
                summary = await result.consume()
            return _plan_ops(summary.plan)

        ops = _graph(explain)
        seeks = [d for op, d in ops if "IndexSeek" in op and "FileChange" in d]
        assert any("path" in d for d in seeks), f"filechange_project_path must be sought; plan was {ops!r}"
        assert any("FileChange(project, path)" in d and "path = $paths" in d for d in seeks), (
            f"the written file's FileChanges must be sought on (project, path) by the hint; plan was {ops!r}")
        assert any("commit_hash" in d for d in seeks), (
            f"filechange_project_commit_hash must be sought; plan was {ops!r}")
        scans = [d for op, d in ops if "NodeByLabelScan" in op and "FileChange" in d]
        assert scans == [], f"FileChange must not be label-scanned; plan was {ops!r}"


# ===========================================================================
# Statement counts
# ===========================================================================


class TestStatementCounts:
    def test_one_read_is_exactly_one_statement(self, project):
        _seed_pairs(project, 3, 3)

        async def work(db):
            with _count_statements() as state:
                await db.get_cochanged_paths(project, [A], **DEFAULTS)
            return state["n"]

        assert _graph(work) == 1

    def test_empty_paths_return_nothing_without_a_statement(self, project):
        async def work(db):
            with _count_statements() as state:
                result = await db.get_cochanged_paths(project, [], **DEFAULTS)
            return result, state["n"]

        result, issued = _graph(work)
        assert _hits(result) == {} and issued == 0


@pytest.fixture()
def registered(project, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    specs = [(_h(), [A, "src/b.py"], _ts(i)) for i in range(2)]

    async def seed(db):
        await db.create_project(project, str(root), str(root))
        await _commits(db, project, specs)

    _graph(seed)
    return str(root)


def _request(path: str) -> PreWriteCheckRequest:
    return PreWriteCheckRequest(session_id="cochange-graph", tool_input={"file_path": path},
                                file_path=path, skill_dir="/x")


def _wire(monkeypatch, db, tmp_path, root: str) -> None:
    from writ.session.cache import mutate_cache

    monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "cache").mkdir(exist_ok=True)
    with mutate_cache("cochange-graph") as data:
        data.update(mode="work", project_root=root, remaining_budget=1500, current_phase="",
                    compaction_epoch=0)
    pipeline = MagicMock()
    pipeline.query.return_value = {"rules": [], "mode": "standard", "total_candidates": 0, "latency_ms": 1}
    monkeypatch.setattr(server, "_db", db)
    monkeypatch.setattr(server, "_pipeline", pipeline)
    monkeypatch.setattr(
        server.writ_session, "_can_write_check",
        lambda sid, env, skill, cache=None: {"can_write": True, "reason": None},
    )


def _is_cochange(params: dict) -> bool:
    return "recent_commits" in params


class TestRouteOnRealRows:
    def test_the_first_write_issues_one_cochange_statement_and_the_second_in_the_epoch_none(
            self, registered, monkeypatch, tmp_path):
        async def work(db):
            _wire(monkeypatch, db, tmp_path, registered)
            await db.resolve_project_for_cwd(registered)  # a warm registry, as in the plan
            with _count_statements() as first:
                out1 = await pre_write_check(_request(os.path.join(registered, A)))
            with _count_statements() as second:
                out2 = await pre_write_check(_request(os.path.join(registered, A)))
            return out1, out2, first, second

        out1, out2, first, second = _graph(work)
        assert out1["decision"] == "allow"
        assert out1["decision_context"] == (
            "[Writ co-change] src/a.py usually changes with src/b.py (2 of 2 recent commits).")
        assert sum(_is_cochange(p) for p in first["params"]) == 1
        assert out2["decision_context"] == ""
        assert sum(_is_cochange(p) for p in second["params"]) == 0

    def test_a_write_outside_the_project_and_a_lockfile_write_issue_no_cochange_statement(
            self, registered, monkeypatch, tmp_path):
        async def work(db):
            _wire(monkeypatch, db, tmp_path, registered)
            await db.resolve_project_for_cwd(registered)
            with _count_statements() as state:
                outside = await pre_write_check(_request("/x/elsewhere/a.py"))
                lock = await pre_write_check(_request(os.path.join(registered, "composer.lock")))
            return outside, lock, state

        outside, lock, state = _graph(work)
        assert outside["decision_context"] == "" and "[Writ co-change]" not in lock["decision_context"]
        assert not any(_is_cochange(p) for p in state["params"])

    def test_a_compaction_makes_the_file_hint_again_on_the_real_graph(self, registered, monkeypatch, tmp_path):
        from writ.session.cache import mutate_cache

        async def work(db):
            _wire(monkeypatch, db, tmp_path, registered)
            first = await pre_write_check(_request(os.path.join(registered, A)))
            with mutate_cache("cochange-graph") as data:
                data["compaction_epoch"] = 1
            again = await pre_write_check(_request(os.path.join(registered, A)))
            return first, again

        first, again = _graph(work)
        assert "[Writ co-change]" in first["decision_context"]
        assert again["decision_context"] == first["decision_context"]
