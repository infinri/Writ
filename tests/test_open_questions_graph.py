"""Program item 7a (open questions): what only a real graph, real token files and real
processes can prove (ENF-SYS-005).

Runs on the isolated instance (scripts/test-graph.sh, port 7688, reached only through
tests/_graph.py). Mocks cannot prove a uniqueness constraint, a conditional write under
concurrency, preserve-on-wipe, a reconcile that must not prune, an index seek, or one approval
raced by two OS processes.

Capability map (plan 1c1f801f)
  Cap 2  -- wire_about on the real graph: Rule and Decision targets, the endpoint allowlist
  Cap 3  -- the unique constraint and the (project, status) index; eight concurrent creates
  Cap 4  -- clear_all preserves OpenQuestion and its edges; the dump holds neither; reconcile
            keeps an ABOUT edge into a Rule
  Cap 5  -- `writ question open` writes the node, one ABOUT edge per target, one audit row
  Cap 6  -- every `writ question open` refusal writes nothing
  Cap 7  -- `writ question list`
  Cap 14 -- two real `writ question answer` processes racing one token; the conditional
            resolve under eight concurrent callers
  Cap 16-18 (7a) -- get_open_questions_for_write: scoping, status, exclude, limit, ordering,
            Decision reachability; the index seek; one /pre-write-check end to end

Co-change reads, the three-statement count and the combined end to end belong to item 7d's
files; every /pre-write-check here has a single commit per file, so co-change contributes
nothing.

Identity values are explicit fixed strings minted into the token.

RED today: no OpenQuestion model, store methods, ABOUT edge, constraint, index or CLI group.
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from typer.testing import CliRunner

import writ.server as server
from tests._graph import connection
from tests._writ_cmd import WRIT_CMD_PREFIX
from tests.test_decision_recall_graph import (  # noqa: F401  (fixture + helpers)
    _count_statements,
    _gate_cache,
    _graph,
    _plan_ops,
    _seed_change,
    _seed_decision,
    _unique,
    _write_request,
    made_projects,
)
from tests.test_review_promote_authority import (  # noqa: F401  (autouse leak guard + helpers)
    _mint_cleanup,
    _no_leaked_gate_tokens,
    _read_stream_rows,
    _sid,
)
from tests.test_trust_records import _make_bible, _reachable_or_skip, _rule_dict
from tests.test_trust_review import (
    GIT_NAME,
    LOGIN,
    _cli,
    _env,
    _mint,
    _pending_binding,
)
from writ.server import pre_write_check

pytestmark = pytest.mark.no_friction_isolation

PROJECT = "test-oq-graph"
OTHER_PROJECT = "test-oq-graph-b"
TS_OLD = "2026-05-01T10:00:00+00:00"
TS_MID = "2026-05-10T10:00:00+00:00"
TS_NEW = "2026-05-20T10:00:00+00:00"
runner = CliRunner()


def _oq() -> str:
    return "OQ-" + uuid.uuid4().hex[:10]


def _q(db, qid=None, project=PROJECT, **overrides):
    data = {
        "question_id": qid or _oq(), "project": project, "question": "Does the cache expire",
        "who_can_answer": "the owner", "settled_by": "a load test", "status": "open",
        "opened_at": TS_MID, "opened_session_id": "sess-open",
    }
    data.update(overrides)
    return db.create_open_question(**data)


async def _node(db, qid: str) -> dict | None:
    rows = list(await db._run("MATCH (q:OpenQuestion {question_id: $q}) RETURN properties(q) AS p", q=qid))
    return dict(rows[0]["p"]) if rows else None


async def _count(db, qid: str | None = None) -> int:
    where = "WHERE q.question_id = $q" if qid else ""
    rows = list(await db._run(f"MATCH (q:OpenQuestion) {where} RETURN count(q) AS c", q=qid))
    return rows[0]["c"]


async def _about(db, qid: str) -> set[str]:
    rows = await db._run(
        "MATCH (q:OpenQuestion {question_id: $q})-[:ABOUT]->(t) "
        "RETURN coalesce(t.rule_id, t.decision_id) AS id", q=qid)
    return {r["id"] for r in rows}


def _new_rule_id(label: str) -> str:
    return f"OQG-{label.upper()}-{100 + uuid.uuid4().int % 900}"


@pytest_asyncio.fixture()
async def db():
    conn = connection()
    await _reachable_or_skip(conn)
    await conn.clear_project(PROJECT)
    await conn.clear_project(OTHER_PROJECT)
    await conn.apply_constraints()
    yield conn
    await conn.clear_project(PROJECT)
    await conn.clear_project(OTHER_PROJECT)
    await conn.close()


@pytest_asyncio.fixture()
async def rule(db):
    """`make(label) -> rule_id`: a real Rule in the shared corpus (project 'writ' style)."""
    made: list[str] = []

    async def make(label: str) -> str:
        rid = _new_rule_id(label)
        await db._run("MATCH (r:Rule {rule_id: $r}) DETACH DELETE r", r=rid)
        await db.create_rule(_rule_dict(rid, project="writ"), source_origin="graph-authored")
        made.append(rid)
        return rid

    yield make
    for rid in made:
        await db._run("MATCH (r:Rule {rule_id: $r}) DETACH DELETE r", r=rid)


@pytest_asyncio.fixture()
async def wipe_db(disposable_graph):
    conn = connection()
    await _reachable_or_skip(conn)
    await conn.clear_all()
    yield conn
    await conn.clear_all()
    for project in ("writ", OTHER_PROJECT, PROJECT):
        await conn.clear_project(project)
    await conn.close()


@pytest.fixture(scope="module", autouse=True)
def _restore_corpus_after_module():
    yield
    from tests._corpus import ensure_corpus

    ensure_corpus()


# ---------------------------------------------------------------------------
# Cap 3: the constraint, the index, concurrent MERGE
# ---------------------------------------------------------------------------


class TestOpenQuestionSchema:
    @pytest.mark.asyncio
    async def test_apply_constraints_creates_the_unique_constraint_and_the_status_index(self, db) -> None:
        constraints = {c.get("name"): c for c in await db.list_constraints()}
        assert "openquestion_question_id_project_unique" in constraints, sorted(constraints)
        text = str(constraints["openquestion_question_id_project_unique"])
        assert "question_id" in text and "project" in text
        indexes = {i.get("name"): i for i in await db.list_indexes()}
        assert "openquestion_project_status" in indexes, sorted(indexes)
        text = str(indexes["openquestion_project_status"])
        assert "project" in text and "status" in text

    @pytest.mark.asyncio
    async def test_a_second_apply_constraints_reports_no_failure(self, db) -> None:
        assert not await db.apply_constraints()


class TestCreateOpenQuestion:
    @pytest.mark.asyncio
    async def test_returns_the_id_and_stores_every_field_with_record_stamps(self, db) -> None:
        qid = _oq()
        assert await _q(db, qid, question="Who owns the retry budget", who_can_answer="sre",
                        settled_by="the runbook") == qid
        props = await _node(db, qid)
        assert props["project"] == PROJECT
        assert props["question"] == "Who owns the retry budget"
        assert props["who_can_answer"] == "sre" and props["settled_by"] == "the runbook"
        assert props["status"] == "open"
        assert props["opened_at"] == TS_MID and props["opened_session_id"] == "sess-open"
        assert props["provenance"] == "record" and props["source_origin"] == "graph-authored"

    @pytest.mark.asyncio
    async def test_eight_concurrent_creates_of_one_id_leave_one_node(self, db) -> None:
        qid = _oq()
        results = await asyncio.gather(*[_q(db, qid) for _ in range(8)], return_exceptions=True)
        assert not [r for r in results if isinstance(r, BaseException)], results
        assert await _count(db, qid) == 1

    @pytest.mark.asyncio
    async def test_the_same_id_in_two_projects_is_two_nodes(self, db) -> None:
        qid = _oq()
        await _q(db, qid)
        await _q(db, qid, project=OTHER_PROJECT)
        assert await _count(db, qid) == 2

    @pytest.mark.asyncio
    async def test_an_invalid_status_or_id_writes_nothing(self, db) -> None:
        from pydantic import ValidationError

        qid = _oq()
        with pytest.raises(ValidationError):
            await _q(db, qid, status="resolved")
        with pytest.raises(ValidationError):
            await _q(db, "answer:" + qid)
        assert await _count(db) == 0


# ---------------------------------------------------------------------------
# Cap 2: ABOUT edges
# ---------------------------------------------------------------------------


class TestAboutEdges:
    @pytest.mark.asyncio
    async def test_wire_about_links_a_question_to_a_rule_and_a_decision(self, db, rule) -> None:
        rid = await rule("about")
        await _seed_decision(db, PROJECT, "DEC-ABOUT", rationale="Cache it.", ts=TS_OLD)
        qid = _oq()
        await _q(db, qid)
        await db.wire_about(qid, "Rule", rid, PROJECT)
        await db.wire_about(qid, "Decision", "DEC-ABOUT", PROJECT)
        assert await _about(db, qid) == {rid, "DEC-ABOUT"}
        rows = list(await db._run(
            "MATCH (:OpenQuestion {question_id: $q})-[e:ABOUT]->() RETURN e.project AS p", q=qid))
        assert [r["p"] for r in rows] == [PROJECT, PROJECT]

    @pytest.mark.asyncio
    async def test_wire_about_is_idempotent(self, db, rule) -> None:
        rid = await rule("idem")
        qid = _oq()
        await _q(db, qid)
        await db.wire_about(qid, "Rule", rid, PROJECT)
        await db.wire_about(qid, "Rule", rid, PROJECT)
        rows = list(await db._run(
            "MATCH (:OpenQuestion {question_id: $q})-[e:ABOUT]->() RETURN count(e) AS c", q=qid))
        assert rows[0]["c"] == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("label", ["Memory", "FileChange", "Commit", "Project", "OpenQuestion", "Skill"])
    async def test_wire_about_refuses_any_target_label_but_rule_and_decision(self, db, label) -> None:
        qid = _oq()
        await _q(db, qid)
        with pytest.raises(ValueError):
            await db.wire_about(qid, label, "anything", PROJECT)
        assert await _about(db, qid) == set()

    @pytest.mark.asyncio
    async def test_the_record_edge_writer_accepts_an_open_question_endpoint(self, db) -> None:
        await _seed_decision(db, PROJECT, "DEC-RAW", rationale="Raw.", ts=TS_OLD)
        qid = _oq()
        await _q(db, qid)
        await db.create_record_edge(
            "ABOUT", src_label="OpenQuestion", src_id_field="question_id", src_id=qid,
            tgt_label="Decision", tgt_id_field="decision_id", tgt_id="DEC-RAW", project=PROJECT)
        assert await _about(db, qid) == {"DEC-RAW"}


class TestGetQuestionTargets:
    @pytest.mark.asyncio
    async def test_returns_only_the_named_targets_that_exist_in_scope(self, db, rule) -> None:
        rid = await rule("targets")
        await _seed_decision(db, PROJECT, "DEC-MINE", rationale="Mine.", ts=TS_OLD)
        await _seed_decision(db, OTHER_PROJECT, "DEC-THEIRS", rationale="Theirs.", ts=TS_OLD)

        found = await db.get_question_targets(
            PROJECT, [rid, "ENF-NOPE-999"], ["DEC-MINE", "DEC-THEIRS", "DEC-GONE"])

        flat = set().union(*found.values())
        assert flat == {rid, "DEC-MINE"}, found

    @pytest.mark.asyncio
    async def test_empty_inputs_return_empty_sets_without_a_statement(self, db) -> None:
        with _count_statements() as state:
            found = await db.get_question_targets(PROJECT, [], [])
        assert state["n"] == 0
        assert all(not v for v in found.values())

    @pytest.mark.asyncio
    async def test_it_is_one_statement_for_any_mix(self, db, rule) -> None:
        rid = await rule("one")
        await _seed_decision(db, PROJECT, "DEC-ONE", rationale="One.", ts=TS_OLD)
        with _count_statements() as state:
            await db.get_question_targets(PROJECT, [rid], ["DEC-ONE"])
        assert state["n"] == 1


# ---------------------------------------------------------------------------
# Cap 4: wipe, dump, reconcile
# ---------------------------------------------------------------------------


class TestRecordSurvivesWipeAndNeverShips:
    @pytest.mark.asyncio
    async def test_a_default_clear_all_leaves_the_question_and_its_edge_to_a_decision(self, wipe_db) -> None:
        await _seed_decision(wipe_db, PROJECT, "DEC-WIPE", rationale="Wipe.", ts=TS_OLD)
        qid = _oq()
        await _q(wipe_db, qid)
        await wipe_db.wire_about(qid, "Decision", "DEC-WIPE", PROJECT)

        await wipe_db.clear_all()

        assert await _count(wipe_db, qid) == 1, "clear_all deleted an OpenQuestion"
        assert await _about(wipe_db, qid) == {"DEC-WIPE"}, "clear_all dropped the ABOUT edge"

    @pytest.mark.asyncio
    async def test_the_node_and_edge_dump_hold_no_question_and_no_about_edge(self, db, rule) -> None:
        from writ.graph.dump import render_cypher_dump

        rid = await rule("dump")
        qid = _oq()
        await _q(db, qid, question="zz-question-sentinel", resolved_os_login=LOGIN,
                 resolved_git_name=GIT_NAME)
        await db.wire_about(qid, "Rule", rid, PROJECT)

        nodes = await db.get_all_nodes_for_dump()
        edges = await db.get_all_edges_cross_type()
        assert "OpenQuestion" not in {n["label"] for n in nodes}
        assert rid in {n["id"] for n in nodes}, "the Rule itself must still dump"
        script = render_cypher_dump(nodes, edges)
        for forbidden in ("OpenQuestion", "ABOUT", "zz-question-sentinel", qid, LOGIN, GIT_NAME):
            assert forbidden not in script, forbidden

    @pytest.mark.asyncio
    async def test_a_corpus_only_dump_replay_leaves_the_question(self, wipe_db) -> None:
        from writ.graph.dump import import_cypher_dump, render_cypher_dump

        qid = _oq()
        await _q(wipe_db, qid)
        script = render_cypher_dump(
            await wipe_db.get_all_nodes_for_dump(), await wipe_db.get_all_edges_cross_type())
        await import_cypher_dump(wipe_db, script)
        assert await _count(wipe_db, qid) == 1


class TestReconcileKeepsAbout:
    @pytest.mark.asyncio
    async def test_a_full_reconcile_keeps_an_about_edge_into_a_rule(self, db, tmp_path) -> None:
        from writ.graph.methodology_ingest import ingest_path, reconcile

        rid = "OQG-RECON-001"
        bible = _make_bible(tmp_path, [rid, "OQG-RECON-002"])
        await ingest_path(bible, db, project=PROJECT)
        qid = _oq()
        await _q(db, qid)
        await db.wire_about(qid, "Rule", rid, PROJECT)
        before = await _node(db, qid)

        result = await reconcile(bible, db, project=PROJECT)

        assert qid not in result["deleted_nodes"]
        assert await _about(db, qid) == {rid}, "reconcile pruned an ABOUT edge"
        assert await _node(db, qid) == before


# ---------------------------------------------------------------------------
# Reads and the conditional write (cap 14, store half)
# ---------------------------------------------------------------------------


class TestReadsAndResolve:
    @pytest.mark.asyncio
    async def test_get_open_question_returns_the_fields_and_the_about_ids(self, db, rule) -> None:
        rid = await rule("get")
        qid = _oq()
        await _q(db, qid)
        await db.wire_about(qid, "Rule", rid, PROJECT)
        got = await db.get_open_question(qid)
        assert got["question_id"] == qid and got["status"] == "open"
        assert got["question"] == "Does the cache expire"
        assert set(got["about"]) == {rid}

    @pytest.mark.asyncio
    async def test_get_open_question_of_an_unknown_id_is_none(self, db) -> None:
        assert await db.get_open_question("OQ-0000000000") is None

    @pytest.mark.asyncio
    async def test_list_returns_open_questions_newest_first_with_about(self, db, rule) -> None:
        rid = await rule("list")
        old, new, done, other = _oq(), _oq(), _oq(), _oq()
        await _q(db, old, opened_at=TS_OLD)
        await _q(db, new, opened_at=TS_NEW)
        await _q(db, done, opened_at=TS_MID, status="answered")
        await _q(db, other, project=OTHER_PROJECT, opened_at=TS_NEW)
        await db.wire_about(new, "Rule", rid, PROJECT)

        rows = await db.list_open_questions(PROJECT)

        assert [r["question_id"] for r in rows] == [new, old]
        assert set(rows[0]["about"]) == {rid} and not rows[1]["about"]
        everything = await db.list_open_questions(PROJECT, include_resolved=True)
        assert [r["question_id"] for r in everything] == [new, done, old]

    @pytest.mark.asyncio
    async def test_resolve_sets_the_props_once_and_refuses_a_second_resolve(self, db) -> None:
        qid = _oq()
        await _q(db, qid)
        first = {"status": "answered", "answer": "first", "resolved_via": "question_answer",
                 "resolved_at": TS_NEW, "resolved_session_id": "s1",
                 "resolved_os_login": LOGIN, "resolved_git_name": GIT_NAME}
        assert await db.resolve_open_question(qid, first) is True
        assert await db.resolve_open_question(qid, {**first, "answer": "second"}) is False
        props = await _node(db, qid)
        assert props["answer"] == "first" and props["status"] == "answered"
        assert props["resolved_os_login"] == LOGIN and props["resolved_git_name"] == GIT_NAME
        assert props["question"] == "Does the cache expire", "a resolve must not clear other fields"

    @pytest.mark.asyncio
    async def test_a_closed_question_is_never_overwritten(self, db) -> None:
        qid = _oq()
        await _q(db, qid, status="closed", answer="closed earlier")
        assert await db.resolve_open_question(qid, {"status": "answered", "answer": "late"}) is False
        assert (await _node(db, qid))["answer"] == "closed earlier"

    @pytest.mark.asyncio
    async def test_resolving_an_unknown_id_returns_false(self, db) -> None:
        assert await db.resolve_open_question("OQ-0000000000", {"status": "closed"}) is False

    @pytest.mark.asyncio
    async def test_of_eight_concurrent_resolves_exactly_one_wins_and_its_answer_stays(self, db) -> None:
        qid = _oq()
        await _q(db, qid)
        results = await asyncio.gather(*[
            db.resolve_open_question(qid, {"status": "answered", "answer": f"answer-{i}"})
            for i in range(8)
        ])
        assert results.count(True) == 1, results
        winner = results.index(True)
        assert (await _node(db, qid))["answer"] == f"answer-{winner}"


# ---------------------------------------------------------------------------
# Caps 16-18, 24: the pre-write question read
# ---------------------------------------------------------------------------


async def _decision_with_change(db, project, decision_id, path, *, ts=TS_MID, change_id=None):
    await _seed_decision(db, project, decision_id, rationale=f"{decision_id} rationale.", ts=ts)
    await _seed_change(db, project, change_id or f"chg-{decision_id}", path, reason="r", ts=ts,
                       decision_id=decision_id)


def _read_for_write(db, paths=("src/a.py",), rule_ids=(), exclude=(), limit=3, project=PROJECT):
    return db.get_open_questions_for_write(project, list(paths), list(rule_ids), list(exclude), limit=limit)


class TestOpenQuestionsForWrite:
    @pytest.mark.asyncio
    async def test_a_question_about_the_decision_behind_the_file_is_returned_with_its_fields(self, db) -> None:
        await _decision_with_change(db, PROJECT, "DEC-A", "src/a.py")
        qid = _oq()
        await _q(db, qid, question="Is it safe", who_can_answer="sre", settled_by="a drill")
        await db.wire_about(qid, "Decision", "DEC-A", PROJECT)

        (row,) = await _read_for_write(db)

        assert row["question_id"] == qid
        assert row["question"] == "Is it safe"
        assert row["who_can_answer"] == "sre" and row["settled_by"] == "a drill"
        assert row["opened_at"] == TS_MID
        assert set(row["about"]) == {"DEC-A"}

    @pytest.mark.asyncio
    async def test_a_question_about_a_rule_in_the_write_rag_ids_is_returned_and_one_outside_is_not(
        self, db, rule,
    ) -> None:
        hit_rule, other_rule = await rule("hit"), await rule("other")
        hit, miss = _oq(), _oq()
        await _q(db, hit)
        await _q(db, miss)
        await db.wire_about(hit, "Rule", hit_rule, PROJECT)
        await db.wire_about(miss, "Rule", other_rule, PROJECT)

        rows = await _read_for_write(db, rule_ids=[hit_rule])

        assert [r["question_id"] for r in rows] == [hit]

    @pytest.mark.asyncio
    async def test_a_question_about_nothing_behind_this_write_never_surfaces(self, db) -> None:
        await _decision_with_change(db, PROJECT, "DEC-ELSE", "src/else.py")
        qid = _oq()
        await _q(db, qid)
        await db.wire_about(qid, "Decision", "DEC-ELSE", PROJECT)
        assert await _read_for_write(db, paths=["src/a.py"], rule_ids=["ENF-NONE-001"]) == []

    @pytest.mark.asyncio
    async def test_any_decision_behind_any_motivated_change_of_the_file_counts(self, db) -> None:
        await _decision_with_change(db, PROJECT, "DEC-OLD", "src/a.py", ts=TS_OLD, change_id="chg-old")
        await _decision_with_change(db, PROJECT, "DEC-NEW", "src/a.py", ts=TS_NEW, change_id="chg-new")
        qid = _oq()
        await _q(db, qid)
        await db.wire_about(qid, "Decision", "DEC-OLD", PROJECT)
        assert [r["question_id"] for r in await _read_for_write(db)] == [qid]

    @pytest.mark.asyncio
    async def test_a_question_matching_both_a_rule_and_a_decision_is_one_row_naming_both(self, db, rule) -> None:
        rid = await rule("both")
        await _decision_with_change(db, PROJECT, "DEC-BOTH", "src/a.py")
        qid = _oq()
        await _q(db, qid)
        await db.wire_about(qid, "Rule", rid, PROJECT)
        await db.wire_about(qid, "Decision", "DEC-BOTH", PROJECT)
        (row,) = await _read_for_write(db, rule_ids=[rid])
        assert set(row["about"]) == {rid, "DEC-BOTH"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["answered", "closed"])
    async def test_a_resolved_question_never_surfaces(self, db, status) -> None:
        await _decision_with_change(db, PROJECT, "DEC-RES", "src/a.py")
        qid = _oq()
        await _q(db, qid, status=status)
        await db.wire_about(qid, "Decision", "DEC-RES", PROJECT)
        assert await _read_for_write(db) == []

    @pytest.mark.asyncio
    async def test_another_projects_question_never_surfaces_even_about_a_shared_rule(self, db, rule) -> None:
        rid = await rule("scope")
        mine, theirs = _oq(), _oq()
        await _q(db, mine)
        await _q(db, theirs, project=OTHER_PROJECT)
        await db.wire_about(mine, "Rule", rid, PROJECT)
        await db.wire_about(theirs, "Rule", rid, OTHER_PROJECT)
        assert [r["question_id"] for r in await _read_for_write(db, rule_ids=[rid])] == [mine]
        assert [r["question_id"] for r in
                await _read_for_write(db, rule_ids=[rid], project=OTHER_PROJECT)] == [theirs]

    @pytest.mark.asyncio
    async def test_exclude_ids_are_applied_inside_the_query_so_a_fourth_question_surfaces_next(
        self, db, rule,
    ) -> None:
        rid = await rule("limit")
        ids = [_oq() for _ in range(4)]
        for n, qid in enumerate(ids):
            await _q(db, qid, opened_at=f"2026-05-0{n + 1}T10:00:00+00:00")
            await db.wire_about(qid, "Rule", rid, PROJECT)

        first = await _read_for_write(db, rule_ids=[rid], limit=3)
        assert [r["question_id"] for r in first] == [ids[3], ids[2], ids[1]], "newest first, three"
        second = await _read_for_write(db, rule_ids=[rid], exclude=[r["question_id"] for r in first])
        assert [r["question_id"] for r in second] == [ids[0]]

    @pytest.mark.asyncio
    async def test_equal_opened_at_orders_by_question_id(self, db, rule) -> None:
        rid = await rule("tie")
        ids = sorted(_oq() for _ in range(3))
        for qid in reversed(ids):
            await _q(db, qid, opened_at=TS_MID)
            await db.wire_about(qid, "Rule", rid, PROJECT)
        rows = await _read_for_write(db, rule_ids=[rid])
        assert [r["question_id"] for r in rows] == ids

    @pytest.mark.asyncio
    async def test_empty_paths_return_nothing_without_a_statement(self, db) -> None:
        with _count_statements() as state:
            assert await _read_for_write(db, paths=[], rule_ids=["ENF-NONE-001"]) == []
        assert state["n"] == 0

    @pytest.mark.asyncio
    async def test_one_statement_and_a_seek_on_the_project_status_index(self, db) -> None:
        await _decision_with_change(db, PROJECT, "DEC-IDX", "src/a.py")
        qid = _oq()
        await _q(db, qid)
        await db.wire_about(qid, "Decision", "DEC-IDX", PROJECT)

        with _count_statements() as state:
            await _read_for_write(db, rule_ids=["ENF-NONE-001"])
        assert state["n"] == 1
        query, params = state["queries"][0], state["params"][0]
        async with db._driver.session(database=db._database) as session:
            result = await session.run("EXPLAIN " + query, **params)
            summary = await result.consume()
        ops = _plan_ops(summary.plan)
        seeks = [d for op, d in ops if "IndexSeek" in op and "OpenQuestion" in d]
        assert seeks, f"expected an index seek on OpenQuestion; plan was {ops!r}"
        assert any("status" in d for d in seeks), "the seek must use the (project, status) index"
        scans = [d for op, d in ops if "NodeByLabelScan" in op and "OpenQuestion" in d]
        assert scans == [], f"OpenQuestion must not be label-scanned; plan was {ops!r}"


# ---------------------------------------------------------------------------
# Caps 5, 6, 7: `writ question open` and `list` on the real graph
# ---------------------------------------------------------------------------


@pytest.fixture()
def registered(made_projects, tmp_path, monkeypatch):
    """A registered repo with one rule, one decision, one open-able environment."""
    name = _unique("oqcli")
    made_projects.append(name)
    root = tmp_path / "repo"
    root.mkdir()
    rid = _new_rule_id("cli")
    sid = f"oq-cli-{uuid.uuid4().hex[:8]}"
    log_project = _env(monkeypatch, tmp_path, "oqcli")
    monkeypatch.setenv("CLAUDE_SESSION_ID", sid)

    async def seed(db):
        await db.create_project(name, str(root), str(root))
        await db._run("MATCH (r:Rule {rule_id: $r}) DETACH DELETE r", r=rid)
        await db.create_rule(_rule_dict(rid, project="writ"), source_origin="graph-authored")
        await _seed_decision(db, name, "DEC-CLI", rationale="Cli decision.", ts=TS_OLD)
        await _seed_decision(db, name + "-x", "DEC-FOREIGN", rationale="Foreign.", ts=TS_OLD)

    _graph(seed)
    made_projects.append(name + "-x")
    yield SimpleNamespace(project=name, root=str(root), rule=rid, sid=sid, log=log_project, tmp=tmp_path)

    async def drop(db):
        await db._run("MATCH (r:Rule {rule_id: $r}) DETACH DELETE r", r=rid)

    _graph(drop)


def _open_args(reg, *extra, text="Does the cache expire between deploys", repo=None):
    args = ["question", "open", "--repo", repo or reg.root]
    if text is not None:
        args += ["--text", text]
    return args + list(extra)


def _total_questions() -> int:
    return _graph(lambda db: _count(db))


class TestQuestionOpenCli:
    def test_open_writes_the_node_one_edge_per_target_one_audit_row_and_prints_the_id(self, registered) -> None:
        reg = registered
        result, notify = _cli(_open_args(
            reg, "--who", "the platform owner", "--settled-by", "a load test",
            "--rule", reg.rule, "--decision", "DEC-CLI"))

        assert result.exit_code == 0, result.output
        match = re.search(r"OQ-[0-9a-f]{10}", result.output)
        assert match, result.output
        qid = match.group(0)
        props = _graph(lambda db: _node(db, qid))
        assert props["project"] == reg.project
        assert props["question"] == "Does the cache expire between deploys"
        assert props["who_can_answer"] == "the platform owner" and props["settled_by"] == "a load test"
        assert props["status"] == "open"
        assert re.match(r"\d{4}-\d{2}-\d{2}T", props["opened_at"])
        assert props["opened_session_id"] == reg.sid
        assert _graph(lambda db: _about(db, qid)) == {reg.rule, "DEC-CLI"}
        audit = [r for r in _read_stream_rows(reg.log, "audit") if r.get("event") == "question_opened"]
        assert len(audit) == 1
        assert audit[0].get("question_id") == qid
        dumped = str(audit[0])
        assert "Does the cache expire" not in dumped, "the audit row carries no question text"
        assert reg.rule in dumped and "DEC-CLI" in dumped
        notify.assert_not_called()

    def test_open_needs_no_token_and_creates_the_constraint_and_index(self, registered) -> None:
        reg = registered
        result, _n = _cli(_open_args(reg, "--rule", reg.rule))
        assert result.exit_code == 0, result.output
        names = _graph(lambda db: db.list_constraints())
        assert "openquestion_question_id_project_unique" in {c.get("name") for c in names}

    @pytest.mark.parametrize("case", [
        "empty_text", "no_target", "eleven_targets", "unknown_rule", "foreign_decision", "unregistered_repo",
    ])
    def test_every_refusal_exits_non_zero_names_the_problem_and_writes_nothing(self, registered, case) -> None:
        reg = registered
        before = _total_questions()
        text, repo, extra, needle = "A real question", None, [], ""
        if case == "empty_text":
            text, extra, needle = "", ["--rule", reg.rule], "text"
        elif case == "no_target":
            extra, needle = [], "rule"
        elif case == "eleven_targets":
            extra = [x for _ in range(10) for x in ("--rule", reg.rule)] + ["--decision", "DEC-CLI"]
            needle = "10"
        elif case == "unknown_rule":
            extra, needle = ["--rule", "ENF-NOPE-999"], "ENF-NOPE-999"
        elif case == "foreign_decision":
            extra, needle = ["--decision", "DEC-FOREIGN"], "DEC-FOREIGN"
        elif case == "unregistered_repo":
            stray = reg.tmp / "stray"
            stray.mkdir()
            repo, extra, needle = str(stray), ["--rule", reg.rule], str(stray)

        result, _n = _cli(_open_args(reg, *extra, text=text, repo=repo))

        assert result.exit_code != 0, result.output
        assert needle.lower() in result.output.lower(), result.output
        assert _total_questions() == before
        assert not [r for r in _read_stream_rows(reg.log, "audit") if r.get("event") == "question_opened"]


class TestQuestionListCli:
    def _seed(self, reg):
        async def seed(db):
            ids = {"old": _oq(), "new": _oq(), "done": _oq(), "closed": _oq(), "foreign": _oq()}
            await _q(db, ids["old"], project=reg.project, opened_at=TS_OLD, question="Older one",
                     who_can_answer="alice", settled_by="a test")
            await _q(db, ids["new"], project=reg.project, opened_at=TS_NEW, question="Newer one")
            await _q(db, ids["done"], project=reg.project, opened_at=TS_MID, status="answered",
                     question="Answered one")
            await _q(db, ids["closed"], project=reg.project, opened_at=TS_MID, status="closed",
                     question="Closed one")
            await _q(db, ids["foreign"], project=reg.project + "-x", question="Foreign one")
            await db.wire_about(ids["new"], "Rule", reg.rule, reg.project)
            return ids

        return _graph(seed)

    def test_list_prints_open_questions_newest_first_with_who_settled_by_and_about(self, registered) -> None:
        reg = registered
        ids = self._seed(reg)

        result, _n = _cli(["question", "list", "--repo", reg.root])

        assert result.exit_code == 0, result.output
        out = result.output
        assert ids["new"] in out and ids["old"] in out
        assert out.index(ids["new"]) < out.index(ids["old"]), "newest first"
        assert "open" in out
        assert "alice" in out and "a test" in out
        assert reg.rule in out
        for hidden in ("done", "closed", "foreign"):
            assert ids[hidden] not in out, hidden

    def test_list_all_adds_answered_and_closed_but_never_another_project(self, registered) -> None:
        reg = registered
        ids = self._seed(reg)

        result, _n = _cli(["question", "list", "--repo", reg.root, "--all"])

        assert result.exit_code == 0, result.output
        for shown in ("old", "new", "done", "closed"):
            assert ids[shown] in result.output, shown
        assert "answered" in result.output and "closed" in result.output
        assert ids["foreign"] not in result.output


# ---------------------------------------------------------------------------
# Cap 14: one approval raced by real processes
# ---------------------------------------------------------------------------


class TestConcurrentAnswersRealProcesses:
    PROCESSES = 4

    def test_exactly_one_of_several_real_answer_processes_writes(self, registered, monkeypatch) -> None:
        reg = registered
        qid = _oq()
        _graph(lambda db: _q(db, qid, project=reg.project))
        sid = _sid("qrace")
        with _mint_cleanup(sid):
            result, _n = _cli(["question", "answer", qid, "--session-id", sid])
            assert result.exit_code == 0, result.output
            assert _pending_binding(sid) == f"answer:{qid}"
            token = _mint(sid, _pending_binding(sid))
            env = {**os.environ, "WRIT_DAEMON_RELOAD": "0"}
            procs = [
                subprocess.Popen(
                    [*WRIT_CMD_PREFIX, "question", "answer", qid, "--text", f"answer-{i}",
                     "--session-id", sid, "--token", token],
                    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for i in range(self.PROCESSES)
            ]
            codes = [(p.communicate(timeout=180), p.returncode)[1] for p in procs]

        assert codes.count(0) == 1, f"exactly one process may win; exit codes {codes}"
        props = _graph(lambda db: _node(db, qid))
        assert props["status"] == "answered"
        assert props["answer"] in {f"answer-{i}" for i in range(self.PROCESSES)}
        assert props["resolved_os_login"] == LOGIN and props["resolved_git_name"] == GIT_NAME
        audit = _read_stream_rows(reg.log, "audit")
        assert len([r for r in audit if r.get("event") == "question_answered"]) == 1
        # A loser is refused by one of three arms, and timing decides which: the single
        # snapshot read finding the token gone, or the claim rename (both audited), or,
        # when it starts after the winner answered, the "question is not open" check that
        # capability 13 places before any token check (input validation, not audited).
        refusal_events = {"rule_promotion_claim_lost", "agent_self_approval_blocked"}
        others = [r.get("event") for r in audit
                  if r.get("event") not in refusal_events | {"question_answered"}]
        refusals = [r for r in audit if r.get("event") in refusal_events]
        assert len(refusals) <= self.PROCESSES - 1, [r.get("event") for r in audit]
        assert not [e for e in others if "question" in str(e)], others
        assert all(r.get("question_id") == qid for r in refusals)


# ---------------------------------------------------------------------------
# Caps 16, 18 end to end through /pre-write-check (decision card plus question line)
# ---------------------------------------------------------------------------


class TestPreWriteCheckEndToEnd:
    @pytest.fixture()
    def chain(self, made_projects, tmp_path, monkeypatch):
        name = _unique("oqe2e")
        made_projects.append(name)
        root = tmp_path / "repo"
        root.mkdir()
        cache = tmp_path / "cache"
        cache.mkdir()
        monkeypatch.setenv("WRIT_CACHE_DIR", str(cache))
        monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path / "logs"))
        monkeypatch.setenv("WRIT_LOG_PROJECT", name)
        return SimpleNamespace(project=name, root=str(root), sid=f"oq-e2e-{uuid.uuid4().hex[:6]}")

    @pytest.fixture()
    def made_rules(self):
        # Created inside _graph's own event loop (the async `rule` fixture's connection
        # belongs to another loop), and removed the same way.
        made: list[str] = []
        yield made
        if made:
            _graph(lambda db: db._run("MATCH (r:Rule) WHERE r.rule_id IN $ids DETACH DELETE r",
                                      ids=list(made)))

    def _install(self, monkeypatch, db, chain, *, rag_rule_ids=()):
        from writ.session.cache import mutate_cache

        with mutate_cache(chain.sid) as data:
            data.update(mode="work", project_root=chain.root, remaining_budget=1500,
                        loaded_rule_ids_by_phase={}, current_phase="", loaded_rule_ids=[])
        rules = [{"rule_id": r} for r in rag_rule_ids]
        monkeypatch.setattr(server, "_db", db)
        monkeypatch.setattr(server, "_pipeline", MagicMock(query=MagicMock(return_value={
            "rules": rules, "mode": "standard", "total_candidates": len(rules), "latency_ms": 1})))
        meta = '{"rule_ids": %s, "cost": 10}' % str(list(rag_rule_ids)).replace("'", '"')
        monkeypatch.setattr(server, "_run_cmd_format_locked", lambda payload: f"RAG TEXT\nWRIT_META:{meta}")
        monkeypatch.setattr(server.writ_session, "_can_write_check",
                            lambda sid, env, skill, cache=None: {"can_write": True, "reason": None})

    def _check(self, chain, rel):
        from writ.server import PreWriteCheckRequest

        path = os.path.join(chain.root, rel)
        return PreWriteCheckRequest(session_id=chain.sid, tool_input={"file_path": path},
                                    file_path=path, skill_dir="/x")

    def test_the_card_then_the_question_line_then_only_what_was_not_yet_shown(self, chain, monkeypatch) -> None:
        from writ.session.cache import _read_cache

        first_q, second_q = _oq(), _oq()

        async def work(db):
            await db.create_project(chain.project, chain.root, chain.root)
            await _decision_with_change(db, chain.project, "DEC-E2E", "src/a.py")
            await _q(db, first_q, project=chain.project, question="Is the lookup thread safe",
                     who_can_answer="sre", settled_by="a race test")
            await db.wire_about(first_q, "Decision", "DEC-E2E", chain.project)
            self._install(monkeypatch, db, chain)
            request = self._check(chain, "src/a.py")
            one = await pre_write_check(request)
            two = await pre_write_check(request)
            await _q(db, second_q, project=chain.project, question="Who rotates the keys")
            await db.wire_about(second_q, "Decision", "DEC-E2E", chain.project)
            three = await pre_write_check(request)
            return one, two, three

        one, two, three = _graph(work)

        lines = one["decision_context"].split("\n")
        assert lines[0].startswith("[Writ decision memory:")
        assert len(lines) == 2
        assert lines[1].startswith(f"[Writ open question {first_q} about DEC-E2E]")
        assert "Is the lookup thread safe" in lines[1]
        assert "Who can answer: sre." in lines[1] and "Settled by: a race test." in lines[1]
        assert two["decision_context"] == ""
        assert three["decision_context"].startswith(f"[Writ open question {second_q} about DEC-E2E]")
        assert "\n" not in three["decision_context"]
        shown = _read_cache(chain.sid)["injection_shown"]
        assert shown["pre_write_questions"] == sorted([first_q, second_q])
        assert shown["pre_write_decision"] == ["src/a.py#DEC-E2E"]
        assert set(one) == {"decision", "reason", "rag_rules", "rag_meta", "mode",
                            "max_denial_count", "decision_context"}

    def test_a_question_about_a_rule_the_write_rag_block_returned_surfaces_by_that_rule(
        self, chain, made_rules, monkeypatch,
    ) -> None:
        qid = _oq()

        async def work(db):
            rid = _new_rule_id("rag")
            await db.create_rule(_rule_dict(rid, project="writ"), source_origin="graph-authored")
            made_rules.append(rid)
            await db.create_project(chain.project, chain.root, chain.root)
            await _q(db, qid, project=chain.project, question="Is the rag rule current")
            await db.wire_about(qid, "Rule", rid, chain.project)
            self._install(monkeypatch, db, chain, rag_rule_ids=[rid])
            return rid, await pre_write_check(self._check(chain, "user_login_handler.py"))

        rid, out = _graph(work)

        assert out["decision_context"].startswith(f"[Writ open question {qid} about {rid}]")
        assert "Is the rag rule current" in out["decision_context"]

    def test_a_question_about_a_rule_the_rag_block_did_not_return_stays_silent(
        self, chain, made_rules, monkeypatch,
    ) -> None:
        async def work(db):
            rid = _new_rule_id("quiet")
            await db.create_rule(_rule_dict(rid, project="writ"), source_origin="graph-authored")
            made_rules.append(rid)
            await db.create_project(chain.project, chain.root, chain.root)
            qid = _oq()
            await _q(db, qid, project=chain.project)
            await db.wire_about(qid, "Rule", rid, chain.project)
            self._install(monkeypatch, db, chain, rag_rule_ids=[])
            return await pre_write_check(self._check(chain, "user_login_handler.py"))

        assert _graph(work)["decision_context"] == ""

    def test_a_compaction_shows_the_question_again(self, chain, monkeypatch) -> None:
        from writ.session.cache import mutate_cache

        qid = _oq()

        async def work(db):
            await db.create_project(chain.project, chain.root, chain.root)
            await _decision_with_change(db, chain.project, "DEC-COMP", "src/a.py")
            await _q(db, qid, project=chain.project)
            await db.wire_about(qid, "Decision", "DEC-COMP", chain.project)
            self._install(monkeypatch, db, chain)
            request = self._check(chain, "src/a.py")
            first = await pre_write_check(request)
            second = await pre_write_check(request)
            with mutate_cache(chain.sid) as data:
                data["compaction_epoch"] = 1
            third = await pre_write_check(request)
            return first, second, third

        first, second, third = _graph(work)
        assert qid in first["decision_context"]
        assert second["decision_context"] == ""
        assert qid in third["decision_context"]
