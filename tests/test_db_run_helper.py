"""Wave-3 dedup Cycle B: writ/graph/db/_query_runner.py's `_QueryRunnerMixin`.

Sixteen read methods spread across rule_store.py/node_store.py/abstraction_store.py/
record_store.py/schema_store.py/project_store.py each hand-roll the same
open-session / run-one-query / drain-result shape that the integrity checker
mixins already dedup'd behind `_QueryMixin._run` (writ/graph/integrity/_query.py).
The planned refactor adds a sibling `_QueryRunnerMixin` to writ/graph/db/ with
two helpers:

  - `_run(query, **params) -> list`            (the `_QueryMixin._run` precedent,
                                                  for the inline `async for record
                                                  in result` / list-comprehension sites)
  - `_run_single(query, **params) -> Record | None`  (for the inline
                                                  `await result.single()` sites)

and migrates the 16 read methods to call `self._run` / `self._run_single`
instead of opening the session inline. See the planned helper signature in the
approved plan for Wave-3 Cycle B.

RED-now / GREEN-after-implementation split in THIS file, mirroring
tests/test_db_record_write_helper.py's Cycle A split:

  - TestQueryRunnerHelper is RED now: writ/graph/db/_query_runner.py does not
    exist yet. Guarded like tests/test_phase_scoped_rules.py -- setup_method
    fails each test in the class individually with a clear reason instead of
    one opaque collection error for the whole file.
  - TestSeamRegistration is RED now: the frozen method-surface list
    (tests/test_db_split_seam.py EXPECTED_METHODS) and the frozen mixin list
    (EXPECTED_MIXINS) have not been updated to include `_run`/`_run_single`/
    `_QueryRunnerMixin` yet. Both flip to GREEN only once the helper lands AND
    the seam lists are updated in the same change.
  - Every other class (the 16 per-method differentials) asserts the
    byte-identical query text / params shape / return-value contract of
    today's INLINE code. That contract is already true (queries below are
    copied verbatim -- captured from a live run against the fake driver, not
    hand-transcribed -- from each method at HEAD), so those tests are the
    regression net that must stay green THROUGH the refactor, not a RED-now
    signal: after migration, self._run/self._run_single must produce the exact
    same session.run call and the exact same return value.

ENF-SYS-005 disclosure: every test below drives the 16 methods (and, once it
exists, `_QueryRunnerMixin._run`/`_run_single` directly) against a hermetic
fake async driver (_FakeDriver/_FakeSession/_FakeResult) -- no real Neo4j, the
shared graph is never touched. This proves the Cypher text, the parameter
shape, and the row-to-return-value projection each method must preserve. It
does NOT and CANNOT prove that any query is correct against a real Neo4j
engine, that SHOW CONSTRAINTS/SHOW INDEXES return what Neo4j actually reports,
or anything about concurrent-access safety -- none of these methods make a
concurrency/idempotency claim this file could falsely certify, so there is no
separate live-Neo4j test this file needs to defer to (unlike Cycle A's
create_decision/create_filechange/create_commit, which do carry an idempotency
claim covered by tests/test_decision_memory_records.py).

Run: .venv/bin/python -m pytest tests/test_db_run_helper.py -q
"""
from __future__ import annotations

import asyncio
import inspect
import json

import pytest

from writ.graph.db._common import RECORD_LABELS

from writ.graph.db.rule_store import RuleStoreMixin
from writ.graph.db.node_store import NodeStoreMixin
from writ.graph.db.edge_store import EdgeStoreMixin
from writ.graph.db.abstraction_store import AbstractionStoreMixin
from writ.graph.db.schema_store import SchemaStoreMixin
from writ.graph.db.project_store import ProjectStoreMixin
from writ.graph.db.record_store import RecordStoreMixin
from writ.graph.db.maintenance_store import MaintenanceStoreMixin
from writ.graph.db import Neo4jConnection, ProjectIdentityConflict

try:
    from writ.graph.db._query_runner import _QueryRunnerMixin  # noqa: E402  # RED until module exists
    _IMPORT_ERROR = None
except ImportError as exc:  # RED until module exists
    _QueryRunnerMixin = None
    _IMPORT_ERROR = exc


def _require_query_runner_module() -> None:
    """Fail the calling test with a clear reason if the helper module isn't
    importable yet (mirrors tests/test_phase_scoped_rules.py's _require_module)."""
    if _IMPORT_ERROR is not None:
        pytest.fail(
            f"writ/graph/db/_query_runner.py is not importable yet: {_IMPORT_ERROR!r}"
        )


# ---------------------------------------------------------------------------
# Expected Cypher text, captured byte-for-byte from a live run of each method
# against the fake driver below (not hand-transcribed -- whitespace inside the
# triple-quoted queries in the source is part of the string).
# ---------------------------------------------------------------------------

_GET_RULE_QUERY = "MATCH (r:Rule {rule_id: $rule_id}) RETURN r"
_COUNT_RULES_QUERY = "MATCH (r:Rule) RETURN count(r) AS count"
_GET_ALL_RULES_NO_PROJECT_QUERY = "MATCH (r:Rule) RETURN r ORDER BY r.rule_id"
_GET_ALL_RULES_WITH_PROJECT_QUERY = (
    "MATCH (r:Rule) WHERE r.project = $project RETURN r ORDER BY r.rule_id"
)
_GET_RULES_BY_AUTHORITY_QUERY = (
    "\n            MATCH (r:Rule)\n            WHERE r.authority = $authority\n"
    "            RETURN r\n            ORDER BY r.last_validated DESC\n        "
)
_GET_SUBAGENT_ROLE_QUERY = (
    "\n            MATCH (r:SubagentRole)\n            WHERE r.name = $name\n"
    "               OR r.role_id = $name\n"
    "               OR r.role_id = 'ROL-' + toUpper(replace($name, 'writ-', '')) + '-001'\n"
    "            RETURN r.role_id AS role_id, r.name AS name,\n"
    "                   r.prompt_template AS prompt_template,\n"
    "                   r.model_preference AS model_preference,\n"
    "                   r.dispatched_by AS dispatched_by\n"
    "            LIMIT 1\n        "
)
_GET_ALL_EDGES_CROSS_TYPE_QUERY = (
    "\n            MATCH (a)-[rel]->(b)\n"
    "            WHERE NOT any(l IN labels(a) WHERE l IN $record_labels)\n"
    "              AND NOT any(l IN labels(b) WHERE l IN $record_labels)\n"
    "            RETURN\n"
    "                coalesce(a.abstraction_id, a.antipattern_id, a.category_id, "
    "a.example_id, a.forbidden_id, a.phase_id, a.playbook_id, a.rationalization_id, "
    "a.role_id, a.rule_id, a.scenario_id, a.skill_id, a.technique_id) AS source_id,\n"
    "                labels(a)[0] AS source_label,\n"
    "                coalesce(b.abstraction_id, b.antipattern_id, b.category_id, "
    "b.example_id, b.forbidden_id, b.phase_id, b.playbook_id, b.rationalization_id, "
    "b.role_id, b.rule_id, b.scenario_id, b.skill_id, b.technique_id) AS target_id,\n"
    "                labels(b)[0] AS target_label,\n"
    "                type(rel) AS type\n        "
)
_TRAVERSE_NEIGHBORS_HOPS_1_QUERY = (
    "\n            MATCH (start:Rule {rule_id: $rule_id})-[rel*1..1]-(neighbor:Rule)\n"
    "            WITH neighbor, rel\n            UNWIND rel AS r\n"
    "            RETURN DISTINCT\n                neighbor.rule_id AS rule_id,\n"
    "                type(r) AS edge_type,\n                startNode(r).rule_id AS from_id,\n"
    "                endNode(r).rule_id AS to_id\n        "
)
_TRAVERSE_NEIGHBORS_HOPS_2_QUERY = (
    "\n            MATCH (start:Rule {rule_id: $rule_id})-[rel*1..2]-(neighbor:Rule)\n"
    "            WITH neighbor, rel\n            UNWIND rel AS r\n"
    "            RETURN DISTINCT\n                neighbor.rule_id AS rule_id,\n"
    "                type(r) AS edge_type,\n                startNode(r).rule_id AS from_id,\n"
    "                endNode(r).rule_id AS to_id\n        "
)
_GET_ABSTRACTION_QUERY = (
    "\n            MATCH (a:Abstraction {abstraction_id: $abstraction_id})\n"
    "            OPTIONAL MATCH (a)-[:ABSTRACTS]->(r:Rule)\n"
    "            RETURN a, collect(r {.*}) AS members\n        "
)
_GET_RULE_ABSTRACTION_QUERY = (
    "\n            MATCH (a:Abstraction)-[:ABSTRACTS]->(r:Rule {rule_id: $rule_id})\n"
    "            OPTIONAL MATCH (a)-[:ABSTRACTS]->(sibling:Rule)\n"
    "            WHERE sibling.rule_id <> $rule_id\n"
    "            RETURN a.abstraction_id AS abstraction_id,\n"
    "                   collect(sibling.rule_id) AS sibling_rule_ids\n        "
)
_GET_OPEN_DECISIONS_FOR_PATH_QUERY = (
    "MATCH (d:Decision {project: $project}) RETURN d.decision_id AS decision_id, "
    "d.planned_files AS planned_files, d.governing_rule_ids AS governing_rule_ids, "
    "d.ts AS ts"
)
_GET_LATEST_FILECHANGE_PER_PATH_QUERY = (
    "MATCH (n:FileChange) WHERE n.project = $project AND n.path IN $paths "
    "WITH n ORDER BY n.ts DESC WITH n.path AS path, collect(n)[0] AS latest "
    "OPTIONAL MATCH (c:Commit {commit_hash: latest.commit_hash, project: $project}) "
    "RETURN path, latest.reason AS reason, latest.change_type AS change_type, "
    "latest.commit_hash AS commit_hash, latest.ts AS ts, "
    "latest.queried_rule_ids AS queried_rule_ids, "
    "latest.cited_rule_ids AS cited_rule_ids, c.subject AS commit_subject"
)
_GET_RECENT_DECISIONS_QUERY = (
    "MATCH (d:Decision {project: $project}) RETURN d.decision_id AS decision_id, "
    "d.title AS title, d.rationale AS rationale, d.planned_files AS planned_files, "
    "d.governing_rule_ids AS governing_rule_ids, d.phase AS phase, d.ts AS ts "
    "ORDER BY d.ts DESC LIMIT $limit"
)
_LIST_CONSTRAINTS_QUERY = "SHOW CONSTRAINTS"
_LIST_INDEXES_QUERY = "SHOW INDEXES"
_GET_PROJECTS_QUERY = (
    "MATCH (p:Project) RETURN p.name AS name, p.repo_root AS repo_root, "
    "p.bible_root AS bible_root, p.remote_url AS remote_url ORDER BY name"
)


# ---------------------------------------------------------------------------
# Wave-3 Cycle B2 additions: query text for the 12 writes-with-return methods
# migrating from inline `session.run` + `result.single()` to `self._run_single`.
# Captured byte-for-byte the same way as the block above (live run against the
# fake driver, not hand-transcribed).
# ---------------------------------------------------------------------------

_CREATE_RULE_QUERY = (
    "\n            MERGE (r:Rule {rule_id: $rule_id, project: $project})\n"
    "            SET r += $props\n"
    "            RETURN r.rule_id AS rule_id\n"
    "        "
)
_CREATE_METHODOLOGY_NODE_SKILL_QUERY = (
    "\n            MERGE (n:Skill {skill_id: $node_id, project: $project})\n"
    "            SET n += $props\n"
    "            RETURN n.skill_id AS id\n"
    "        "
)
_CREATE_ABSTRACTION_QUERY = (
    "\n            MERGE (a:Abstraction {abstraction_id: $abstraction_id, project: $project})\n"
    "            SET a += $props\n"
    "            RETURN a.abstraction_id AS abstraction_id\n"
    "        "
)
_DELETE_ABSTRACTIONS_NO_PROJECT_QUERY = (
    "MATCH (a:Abstraction) DETACH DELETE a RETURN count(a) AS deleted"
)
_DELETE_ABSTRACTIONS_WITH_PROJECT_QUERY = (
    "MATCH (a:Abstraction) WHERE coalesce(a.project, 'writ') = $project "
    "DETACH DELETE a RETURN count(a) AS deleted"
)
_CREATE_RECORD_DECISION_QUERY = (
    "MERGE (n:Decision {decision_id: $decision_id, project: $project}) "
    "SET n += $props "
    "RETURN n.decision_id AS decision_id"
)
_UPDATE_RULE_AUTHORITY_QUERY = (
    "\n            MATCH (r:Rule {rule_id: $rule_id})\n"
    "            SET r.authority = $authority\n"
    "            RETURN r.rule_id AS rule_id\n"
    "        "
)
_UPDATE_RULE_CONFIDENCE_QUERY = (
    "\n            MATCH (r:Rule {rule_id: $rule_id})\n"
    "            SET r.confidence = $confidence\n"
    "            RETURN r.rule_id AS rule_id\n"
    "        "
)
_INCREMENT_POSITIVE_QUERY = (
    "\n            MATCH (r:Rule {rule_id: $rule_id})\n"
    "            SET r.times_seen_positive = coalesce(r.times_seen_positive, 0) + 1,\n"
    "                r.last_seen = datetime()\n"
    "            RETURN r.rule_id AS rule_id\n"
    "        "
)
_INCREMENT_NEGATIVE_QUERY = (
    "\n            MATCH (r:Rule {rule_id: $rule_id})\n"
    "            SET r.times_seen_negative = coalesce(r.times_seen_negative, 0) + 1,\n"
    "                r.last_seen = datetime()\n"
    "            RETURN r.rule_id AS rule_id\n"
    "        "
)
_CREATE_PROJECT_QUERY = (
    "MERGE (p:Project {name: $name}) "
    "SET p.repo_root = $repo_root, p.bible_root = $bible_root "
    "FOREACH (_ IN CASE WHEN $remote_url IS NOT NULL AND p.remote_url IS NULL "
    "THEN [1] ELSE [] END | SET p.remote_url = $remote_url) "
    "RETURN p.name AS name, p.remote_url AS remote_url"
)
_CLEAR_PROJECT_QUERY = (
    "MATCH (n) WHERE n.project = $project "
    "WITH collect(n) AS ns, count(n) AS c "
    "FOREACH (x IN ns | DETACH DELETE x) "
    "RETURN c AS deleted"
)


# ---------------------------------------------------------------------------
# Hermetic fake async driver -- captures every session.run call, never opens
# a socket, never touches the shared Neo4j graph.
# ---------------------------------------------------------------------------

_UNSET = object()


class _FakeRecord(dict):
    """Stands in for neo4j's Record. dict already gives __getitem__/.get();
    .data() mirrors Record.data() (plain-dict projection of the row)."""

    def data(self) -> dict:
        return dict(self)


class _FakeResult:
    """Stands in for neo4j's AsyncResult: supports both access patterns the 16
    target methods use -- async iteration (`async for record in result` / the
    `[... async for record in result]` list comprehensions) and
    `await result.single()`.

    `single` defaults to the first preset row (or None if no rows) so a test
    that only cares about iteration doesn't have to pass it explicitly; pass
    `single=None` or an explicit row to control the single()-path independently
    of the iteration rows.
    """

    def __init__(self, rows: list | None = None, single=_UNSET) -> None:
        self._rows = list(rows) if rows is not None else []
        if single is _UNSET:
            self._single = self._rows[0] if self._rows else None
        else:
            self._single = single

    def __aiter__(self):
        return self._aiter()

    async def _aiter(self):
        for row in self._rows:
            yield row

    async def single(self):
        return self._single


class _FakeSession:
    """Async context manager standing in for neo4j's AsyncSession.

    Appends every run() call to a shared `calls` list, then pops the next
    preset _FakeResult off a shared queue (every one of the 16 target methods
    issues exactly one session.run per `async with` block, so a length-1 queue
    is the common case; the queue supports more calls for methods that open a
    session and run more than one query in it).
    """

    def __init__(self, calls: list[dict], results: list[_FakeResult]) -> None:
        self._calls = calls
        self._results = results

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    async def run(self, query: str, **params) -> _FakeResult:
        self._calls.append({"query": query, "params": params})
        if self._results:
            return self._results.pop(0)
        return _FakeResult()


class _FakeDriver:
    """Stands in for neo4j's AsyncDriver. session() returns a fresh _FakeSession
    bound to the same shared `calls` list and the same shared `results` queue
    every time (mirrors real driver reuse across `async with self._driver.session()`
    blocks)."""

    def __init__(self, calls: list[dict], results: list[_FakeResult]) -> None:
        self._calls = calls
        self._results = results
        # Every `database=` the driver's session() was opened with, in order, so a
        # regression that drops `database=self._database` from _run/_run_single is
        # caught (the driver must route reads to the configured database).
        self.session_databases: list[str | None] = []

    def session(self, database: str | None = None) -> _FakeSession:
        self.session_databases.append(database)
        return _FakeSession(self._calls, self._results)


class _Conn(
    _QueryRunnerMixin,
    RuleStoreMixin,
    NodeStoreMixin,
    EdgeStoreMixin,
    AbstractionStoreMixin,
    SchemaStoreMixin,
    ProjectStoreMixin,
    RecordStoreMixin,
    MaintenanceStoreMixin,
):
    """Mirrors Neo4jConnection's base list (writ/graph/db/__init__.py), INCLUDING
    _QueryRunnerMixin, so the migrated read methods resolve self._run/_run_single
    exactly as they do on the real Neo4jConnection. The fake driver's session()
    still intercepts, so the helper runs against the fake (no real Neo4j) and the
    differential tests exercise the migrated methods end-to-end through the real
    _run/_run_single, asserting byte-identical query/params and return value."""


def _make_conn(*results: _FakeResult) -> tuple[_Conn, list[dict]]:
    """Fresh _Conn + fresh calls list per call (TEST-ISOLATE-001: nothing here
    is shared across tests). `results` presets the fake driver's per-call
    response queue, in order."""
    calls: list[dict] = []
    driver = _FakeDriver(calls, list(results))
    conn = _Conn.__new__(_Conn)
    conn._driver = driver
    conn._database = "neo4j"
    return conn, calls


# ---------------------------------------------------------------------------
# Section 1: the helper itself. RED until writ/graph/db/_query_runner.py exists.
# ---------------------------------------------------------------------------


class TestQueryRunnerHelper:
    def setup_method(self) -> None:
        _require_query_runner_module()

    @staticmethod
    def _mixin_instance(*results: _FakeResult):
        calls: list[dict] = []
        driver = _FakeDriver(calls, list(results))
        obj = _QueryRunnerMixin.__new__(_QueryRunnerMixin)
        obj._driver = driver
        obj._database = "neo4j"
        return obj, calls

    def test_run_returns_preset_rows_as_a_list_in_order(self) -> None:
        rows = [_FakeRecord({"n": i}) for i in range(3)]
        obj, calls = self._mixin_instance(_FakeResult(rows=rows))
        result = asyncio.run(obj._run("MATCH (n) RETURN n", foo="bar"))
        assert result == rows
        assert len(calls) == 1
        assert calls[0] == {"query": "MATCH (n) RETURN n", "params": {"foo": "bar"}}

    def test_run_single_returns_the_preset_single_record(self) -> None:
        row = _FakeRecord({"count": 7})
        obj, _calls = self._mixin_instance(_FakeResult(single=row))
        result = asyncio.run(obj._run_single("MATCH (n) RETURN count(n) AS count"))
        assert result == row

    def test_run_single_returns_none_when_fake_single_yields_none(self) -> None:
        obj, _calls = self._mixin_instance(_FakeResult(single=None))
        result = asyncio.run(obj._run_single("MATCH (n) RETURN n"))
        assert result is None

    def test_run_is_a_coroutine_function(self) -> None:
        assert inspect.iscoroutinefunction(_QueryRunnerMixin._run)

    def test_run_single_is_a_coroutine_function(self) -> None:
        assert inspect.iscoroutinefunction(_QueryRunnerMixin._run_single)

    def test_run_opens_the_session_on_the_configured_database(self) -> None:
        obj, _calls = self._mixin_instance(_FakeResult(rows=[]))
        asyncio.run(obj._run("MATCH (n) RETURN n"))
        assert obj._driver.session_databases == ["neo4j"]

    def test_run_single_opens_the_session_on_the_configured_database(self) -> None:
        obj, _calls = self._mixin_instance(_FakeResult(single=None))
        asyncio.run(obj._run_single("MATCH (n) RETURN n"))
        assert obj._driver.session_databases == ["neo4j"]


# ---------------------------------------------------------------------------
# Section 2: seam registration. RED until the seam lists are updated.
# ---------------------------------------------------------------------------


class TestSeamRegistration:
    def test_run_is_registered_in_expected_methods(self) -> None:
        from tests.test_db_split_seam import EXPECTED_METHODS

        assert "_run" in EXPECTED_METHODS, (
            "tests/test_db_split_seam.py:EXPECTED_METHODS has not been updated "
            "to include '_run'; update it in the same change that adds "
            "_QueryRunnerMixin"
        )

    def test_run_single_is_registered_in_expected_methods(self) -> None:
        from tests.test_db_split_seam import EXPECTED_METHODS

        assert "_run_single" in EXPECTED_METHODS, (
            "tests/test_db_split_seam.py:EXPECTED_METHODS has not been updated "
            "to include '_run_single'; update it in the same change that adds "
            "_QueryRunnerMixin"
        )

    def test_query_runner_mixin_is_registered_in_expected_mixins(self) -> None:
        from tests.test_db_split_seam import EXPECTED_MIXINS

        assert (
            "writ.graph.db._query_runner",
            "_QueryRunnerMixin",
        ) in EXPECTED_MIXINS, (
            "tests/test_db_split_seam.py:EXPECTED_MIXINS has not been updated "
            "to include ('writ.graph.db._query_runner', '_QueryRunnerMixin')"
        )


# ---------------------------------------------------------------------------
# Section 3: per-method differentials. GREEN now (today's inline code); must
# stay green after the _run/_run_single migration.
# ---------------------------------------------------------------------------


class TestGetRule:
    def test_sends_expected_query_and_params(self) -> None:
        row = _FakeRecord({"r": {"rule_id": "RULE-1", "statement": "x"}})
        conn, calls = _make_conn(_FakeResult(single=row))
        asyncio.run(conn.get_rule("RULE-1"))
        assert calls == [{"query": _GET_RULE_QUERY, "params": {"rule_id": "RULE-1"}}]

    def test_returns_dict_of_the_rule_node_props(self) -> None:
        row = _FakeRecord({"r": {"rule_id": "RULE-1", "statement": "x"}})
        conn, _calls = _make_conn(_FakeResult(single=row))
        result = asyncio.run(conn.get_rule("RULE-1"))
        assert result == {"rule_id": "RULE-1", "statement": "x"}

    def test_returns_none_when_no_matching_rule(self) -> None:
        conn, _calls = _make_conn(_FakeResult(single=None))
        result = asyncio.run(conn.get_rule("RULE-missing"))
        assert result is None


class TestCountRules:
    def test_sends_expected_query_with_no_params(self) -> None:
        conn, calls = _make_conn(_FakeResult(single=_FakeRecord({"count": 7})))
        asyncio.run(conn.count_rules())
        assert calls == [{"query": _COUNT_RULES_QUERY, "params": {}}]

    def test_returns_the_preset_count(self) -> None:
        conn, _calls = _make_conn(_FakeResult(single=_FakeRecord({"count": 7})))
        result = asyncio.run(conn.count_rules())
        assert result == 7


class TestGetAllRules:
    def test_no_project_filter_sends_expected_query_and_empty_params(self) -> None:
        conn, calls = _make_conn(_FakeResult(rows=[]))
        asyncio.run(conn.get_all_rules())
        assert calls == [{"query": _GET_ALL_RULES_NO_PROJECT_QUERY, "params": {}}]

    def test_with_project_filter_sends_expected_query_and_params(self) -> None:
        conn, calls = _make_conn(_FakeResult(rows=[]))
        asyncio.run(conn.get_all_rules(project="writ"))
        assert calls == [
            {"query": _GET_ALL_RULES_WITH_PROJECT_QUERY, "params": {"project": "writ"}}
        ]

    def test_returns_list_of_dicts_in_order(self) -> None:
        rows = [
            _FakeRecord({"r": {"rule_id": "RULE-1"}}),
            _FakeRecord({"r": {"rule_id": "RULE-2"}}),
        ]
        conn, _calls = _make_conn(_FakeResult(rows=rows))
        result = asyncio.run(conn.get_all_rules())
        assert result == [{"rule_id": "RULE-1"}, {"rule_id": "RULE-2"}]


class TestGetRulesByAuthority:
    def test_sends_expected_query_and_params(self) -> None:
        conn, calls = _make_conn(_FakeResult(rows=[]))
        asyncio.run(conn.get_rules_by_authority("ai-provisional"))
        assert calls == [
            {"query": _GET_RULES_BY_AUTHORITY_QUERY, "params": {"authority": "ai-provisional"}}
        ]

    def test_returns_list_of_dicts_in_order(self) -> None:
        rows = [
            _FakeRecord({"r": {"rule_id": "RULE-1"}}),
            _FakeRecord({"r": {"rule_id": "RULE-2"}}),
        ]
        conn, _calls = _make_conn(_FakeResult(rows=rows))
        result = asyncio.run(conn.get_rules_by_authority("ai-provisional"))
        assert result == [{"rule_id": "RULE-1"}, {"rule_id": "RULE-2"}]


class TestGetSubagentRole:
    @staticmethod
    def _row(**overrides) -> _FakeRecord:
        defaults = {
            "role_id": "ROL-EXPLORER-001",
            "name": "writ-explorer",
            "prompt_template": "explore the codebase",
            "model_preference": "sonnet",
            "dispatched_by": "orchestrator",
        }
        return _FakeRecord({**defaults, **overrides})

    def test_sends_expected_query_and_params(self) -> None:
        conn, calls = _make_conn(_FakeResult(single=self._row()))
        asyncio.run(conn.get_subagent_role("writ-explorer"))
        assert calls == [
            {"query": _GET_SUBAGENT_ROLE_QUERY, "params": {"name": "writ-explorer"}}
        ]

    def test_returns_the_projection_dict(self) -> None:
        conn, _calls = _make_conn(_FakeResult(single=self._row()))
        result = asyncio.run(conn.get_subagent_role("writ-explorer"))
        assert result == {
            "role_id": "ROL-EXPLORER-001",
            "name": "writ-explorer",
            "prompt_template": "explore the codebase",
            "model_preference": "sonnet",
            "dispatched_by": "orchestrator",
        }

    def test_returns_none_when_no_role_matches(self) -> None:
        conn, _calls = _make_conn(_FakeResult(single=None))
        result = asyncio.run(conn.get_subagent_role("nonexistent-role"))
        assert result is None


class TestGetAllEdgesCrossType:
    def test_sends_expected_query_with_no_params(self) -> None:
        conn, calls = _make_conn(_FakeResult(rows=[]))
        asyncio.run(conn.get_all_edges_cross_type())
        assert calls == [{"query": _GET_ALL_EDGES_CROSS_TYPE_QUERY,
                          "params": {"record_labels": sorted(RECORD_LABELS)}}]

    def test_returns_row_data_dicts_in_order(self) -> None:
        rows = [
            _FakeRecord(
                {
                    "source_id": "RULE-1",
                    "source_label": "Rule",
                    "target_id": "RULE-2",
                    "target_label": "Rule",
                    "type": "RELATED_TO",
                }
            ),
            _FakeRecord(
                {
                    "source_id": "RULE-2",
                    "source_label": "Rule",
                    "target_id": "SKL-1",
                    "target_label": "Skill",
                    "type": "BELONGS_TO",
                }
            ),
        ]
        conn, _calls = _make_conn(_FakeResult(rows=rows))
        result = asyncio.run(conn.get_all_edges_cross_type())
        assert result == [dict(r) for r in rows]


class TestTraverseNeighbors:
    def test_default_hops_sends_expected_query_and_params(self) -> None:
        conn, calls = _make_conn(_FakeResult(rows=[]))
        asyncio.run(conn.traverse_neighbors("RULE-1"))
        assert calls == [
            {"query": _TRAVERSE_NEIGHBORS_HOPS_1_QUERY, "params": {"rule_id": "RULE-1"}}
        ]

    def test_hops_2_interpolates_the_hop_count_into_the_query(self) -> None:
        conn, calls = _make_conn(_FakeResult(rows=[]))
        asyncio.run(conn.traverse_neighbors("RULE-1", hops=2))
        assert calls == [
            {"query": _TRAVERSE_NEIGHBORS_HOPS_2_QUERY, "params": {"rule_id": "RULE-1"}}
        ]

    def test_returns_row_data_dicts_in_order(self) -> None:
        rows = [
            _FakeRecord(
                {
                    "rule_id": "RULE-2",
                    "edge_type": "RELATED_TO",
                    "from_id": "RULE-1",
                    "to_id": "RULE-2",
                }
            ),
        ]
        conn, _calls = _make_conn(_FakeResult(rows=rows))
        result = asyncio.run(conn.traverse_neighbors("RULE-1"))
        assert result == [dict(r) for r in rows]

    def test_hops_zero_raises_value_error_without_touching_the_driver(self) -> None:
        conn, calls = _make_conn()
        with pytest.raises(ValueError):
            asyncio.run(conn.traverse_neighbors("RULE-1", hops=0))
        assert calls == []

    def test_hops_four_raises_value_error_without_touching_the_driver(self) -> None:
        conn, calls = _make_conn()
        with pytest.raises(ValueError):
            asyncio.run(conn.traverse_neighbors("RULE-1", hops=4))
        assert calls == []


class TestGetAbstraction:
    @staticmethod
    def _row() -> _FakeRecord:
        return _FakeRecord(
            {
                "a": {"abstraction_id": "ABS-1", "domain": "testing"},
                "members": [{"rule_id": "RULE-1"}],
            }
        )

    def test_sends_expected_query_and_params(self) -> None:
        conn, calls = _make_conn(_FakeResult(single=self._row()))
        asyncio.run(conn.get_abstraction("ABS-1"))
        assert calls == [
            {"query": _GET_ABSTRACTION_QUERY, "params": {"abstraction_id": "ABS-1"}}
        ]

    def test_returns_data_dict_with_members(self) -> None:
        conn, _calls = _make_conn(_FakeResult(single=self._row()))
        result = asyncio.run(conn.get_abstraction("ABS-1"))
        assert result == {
            "abstraction_id": "ABS-1",
            "domain": "testing",
            "members": [{"rule_id": "RULE-1"}],
        }

    def test_returns_none_when_no_matching_abstraction(self) -> None:
        conn, _calls = _make_conn(_FakeResult(single=None))
        result = asyncio.run(conn.get_abstraction("ABS-missing"))
        assert result is None


class TestGetRuleAbstraction:
    def test_sends_expected_query_and_params(self) -> None:
        row = _FakeRecord({"abstraction_id": "ABS-1", "sibling_rule_ids": ["RULE-2", "RULE-3"]})
        conn, calls = _make_conn(_FakeResult(single=row))
        asyncio.run(conn.get_rule_abstraction("RULE-1"))
        assert calls == [
            {"query": _GET_RULE_ABSTRACTION_QUERY, "params": {"rule_id": "RULE-1"}}
        ]

    def test_returns_abstraction_id_and_sorted_siblings(self) -> None:
        row = _FakeRecord({"abstraction_id": "ABS-1", "sibling_rule_ids": ["RULE-3", "RULE-2"]})
        conn, _calls = _make_conn(_FakeResult(single=row))
        result = asyncio.run(conn.get_rule_abstraction("RULE-1"))
        assert result == {"abstraction_id": "ABS-1", "sibling_rule_ids": ["RULE-2", "RULE-3"]}

    def test_returns_none_when_row_has_no_abstraction_id(self) -> None:
        row = _FakeRecord({"abstraction_id": None, "sibling_rule_ids": []})
        conn, _calls = _make_conn(_FakeResult(single=row))
        result = asyncio.run(conn.get_rule_abstraction("RULE-orphan"))
        assert result is None

    def test_returns_none_when_single_yields_none(self) -> None:
        conn, _calls = _make_conn(_FakeResult(single=None))
        result = asyncio.run(conn.get_rule_abstraction("RULE-none-match"))
        assert result is None


class TestListConstraints:
    def test_sends_exactly_show_constraints_with_no_params(self) -> None:
        conn, calls = _make_conn(_FakeResult(rows=[]))
        asyncio.run(conn.list_constraints())
        assert calls == [{"query": _LIST_CONSTRAINTS_QUERY, "params": {}}]

    def test_returns_row_data_dicts_in_order(self) -> None:
        rows = [
            _FakeRecord({"name": "rule_id_project_unique", "type": "UNIQUENESS"}),
            _FakeRecord({"name": "project_name_unique", "type": "UNIQUENESS"}),
        ]
        conn, _calls = _make_conn(_FakeResult(rows=rows))
        result = asyncio.run(conn.list_constraints())
        assert result == [dict(r) for r in rows]


class TestListIndexes:
    def test_sends_exactly_show_indexes_with_no_params(self) -> None:
        conn, calls = _make_conn(_FakeResult(rows=[]))
        asyncio.run(conn.list_indexes())
        assert calls == [{"query": _LIST_INDEXES_QUERY, "params": {}}]

    def test_returns_row_data_dicts_in_order(self) -> None:
        rows = [_FakeRecord({"name": "rule_domain", "type": "RANGE"})]
        conn, _calls = _make_conn(_FakeResult(rows=rows))
        result = asyncio.run(conn.list_indexes())
        assert result == [dict(r) for r in rows]


class TestGetOpenDecisionsForPath:
    def test_sends_expected_query_and_params(self) -> None:
        conn, calls = _make_conn(_FakeResult(rows=[]))
        asyncio.run(conn.get_open_decisions_for_path("writ", "foo.py"))
        assert calls == [
            {"query": _GET_OPEN_DECISIONS_FOR_PATH_QUERY, "params": {"project": "writ"}}
        ]

    def test_filters_to_open_claims_on_path_most_recent_first(self) -> None:
        rows = [
            _FakeRecord(
                {
                    "decision_id": "DEC-1",
                    "planned_files": json.dumps([{"path": "foo.py", "resolved": False}]),
                    "governing_rule_ids": [],
                    "ts": "2026-01-01T00:00:00Z",
                }
            ),
            _FakeRecord(
                {
                    "decision_id": "DEC-2",
                    "planned_files": json.dumps([{"path": "foo.py", "resolved": True}]),
                    "governing_rule_ids": [],
                    "ts": "2026-02-01T00:00:00Z",
                }
            ),
            _FakeRecord(
                {
                    "decision_id": "DEC-3",
                    "planned_files": json.dumps([{"path": "foo.py", "resolved": False}]),
                    "governing_rule_ids": [],
                    "ts": "2026-03-01T00:00:00Z",
                }
            ),
            _FakeRecord(
                {
                    "decision_id": "DEC-4",
                    "planned_files": json.dumps([{"path": "bar.py", "resolved": False}]),
                    "governing_rule_ids": [],
                    "ts": "2026-04-01T00:00:00Z",
                }
            ),
        ]
        conn, _calls = _make_conn(_FakeResult(rows=rows))
        result = asyncio.run(conn.get_open_decisions_for_path("writ", "foo.py"))
        assert [r["decision_id"] for r in result] == ["DEC-3", "DEC-1"]

    def test_decision_with_empty_planned_files_never_matches(self) -> None:
        rows = [
            _FakeRecord(
                {
                    "decision_id": "DEC-empty",
                    "planned_files": json.dumps([]),
                    "governing_rule_ids": [],
                    "ts": "2026-01-01T00:00:00Z",
                }
            ),
        ]
        conn, _calls = _make_conn(_FakeResult(rows=rows))
        result = asyncio.run(conn.get_open_decisions_for_path("writ", "foo.py"))
        assert result == []


class TestGetLatestFilechangePerPath:
    def test_sends_expected_query_and_params(self) -> None:
        conn, calls = _make_conn(_FakeResult(rows=[]))
        asyncio.run(conn.get_latest_filechange_per_path("writ", ["foo.py"]))
        assert calls == [
            {
                "query": _GET_LATEST_FILECHANGE_PER_PATH_QUERY,
                "params": {"project": "writ", "paths": ["foo.py"]},
            }
        ]

    def test_returns_dict_keyed_by_path_with_defaulted_missing_lists(self) -> None:
        rows = [
            _FakeRecord(
                {
                    "path": "foo.py",
                    "reason": "refactor",
                    "change_type": "modify",
                    "commit_hash": "abc123",
                    "ts": "2026-07-01T00:00:00Z",
                    "queried_rule_ids": None,
                    "cited_rule_ids": None,
                    "commit_subject": "refactor(db): dedup",
                }
            ),
        ]
        conn, _calls = _make_conn(_FakeResult(rows=rows))
        result = asyncio.run(conn.get_latest_filechange_per_path("writ", ["foo.py"]))
        assert result == {
            "foo.py": {
                "reason": "refactor",
                "change_type": "modify",
                "commit_hash": "abc123",
                "ts": "2026-07-01T00:00:00Z",
                "queried_rule_ids": [],
                "cited_rule_ids": [],
                "commit_subject": "refactor(db): dedup",
            }
        }

    def test_empty_paths_returns_empty_dict_without_touching_the_driver(self) -> None:
        conn, calls = _make_conn()
        result = asyncio.run(conn.get_latest_filechange_per_path("writ", []))
        assert result == {}
        assert calls == []


class TestGetRecentDecisions:
    def test_default_limit_sends_expected_query_and_params(self) -> None:
        conn, calls = _make_conn(_FakeResult(rows=[]))
        asyncio.run(conn.get_recent_decisions("writ"))
        assert calls == [
            {"query": _GET_RECENT_DECISIONS_QUERY, "params": {"project": "writ", "limit": 20}}
        ]

    def test_explicit_limit_sends_expected_params(self) -> None:
        conn, calls = _make_conn(_FakeResult(rows=[]))
        asyncio.run(conn.get_recent_decisions("writ", limit=5))
        assert calls == [
            {"query": _GET_RECENT_DECISIONS_QUERY, "params": {"project": "writ", "limit": 5}}
        ]

    def test_parses_planned_files_and_defaults_governing_rule_ids(self) -> None:
        rows = [
            _FakeRecord(
                {
                    "decision_id": "DEC-1",
                    "title": "Add helper",
                    "rationale": "dedup",
                    "planned_files": json.dumps([{"path": "foo.py", "resolved": False}]),
                    "governing_rule_ids": None,
                    "phase": "1a",
                    "ts": "2026-07-01T00:00:00Z",
                }
            ),
        ]
        conn, _calls = _make_conn(_FakeResult(rows=rows))
        result = asyncio.run(conn.get_recent_decisions("writ"))
        assert result == [
            {
                "decision_id": "DEC-1",
                "title": "Add helper",
                "rationale": "dedup",
                "planned_files": [{"path": "foo.py", "resolved": False}],
                "governing_rule_ids": [],
                "phase": "1a",
                "ts": "2026-07-01T00:00:00Z",
            }
        ]


class TestGetProjects:
    def test_sends_expected_query_with_no_params(self) -> None:
        conn, calls = _make_conn(_FakeResult(rows=[]))
        asyncio.run(conn.get_projects())
        assert calls == [{"query": _GET_PROJECTS_QUERY, "params": {}}]

    def test_returns_list_of_dicts_in_order(self) -> None:
        rows = [
            _FakeRecord(
                {
                    "name": "writ",
                    "repo_root": "/repo/writ",
                    "bible_root": "/repo/writ/bible",
                    "remote_url": None,
                }
            ),
            _FakeRecord(
                {
                    "name": "otherproj",
                    "repo_root": "/repo/other",
                    "bible_root": "/repo/other/bible",
                    "remote_url": "git@example.com:x/other.git",
                }
            ),
        ]
        conn, _calls = _make_conn(_FakeResult(rows=rows))
        result = asyncio.run(conn.get_projects())
        assert result == [dict(r) for r in rows]
# ---------------------------------------------------------------------------
# Section 4: Wave-3 Cycle B2 -- the 12 writes-with-return methods migrating
# from inline `async with self._driver.session(...): ... await result.single()`
# to `self._run_single(...)`.
#
# TestWritesAdoptRunSingle is the SOURCE GUARD (RED now, GREEN after the
# migration lands): it asserts each method's source calls self._run_single(...)
# and no longer hand-rolls the inline session/run/single boilerplate.
#
# The 12 classes after it are behavioral differentials, GREEN now against
# today's inline code: they lock the exact Cypher text, the exact params, and
# the exact row-to-return-value projection each method must preserve. They run
# unchanged after the migration (the fake driver intercepts self._run_single
# exactly like it intercepts the inline session.run today), so they are the
# regression net that proves the refactor changed nothing observable.
#
# ENF-SYS-005 disclosure: same as the module docstring above -- these drive the
# 12 methods against the hermetic fake driver only. They prove Cypher text,
# param shape, and return-value projection; they do NOT and cannot prove the
# project_name_unique / rule_id-project constraints actually serialize
# concurrent create_project / create_rule calls against a real Neo4j engine.
# ---------------------------------------------------------------------------


class TestWritesAdoptRunSingle:
    """RED now: all 12 methods still hand-roll `async with self._driver.session():`
    + `await session.run(...)` + `await result.single()` instead of calling
    `self._run_single(...)`. GREEN once each method is migrated (Cycle B2)."""

    _TARGET_METHOD_NAMES = [
        "create_rule",
        "create_methodology_node",
        "create_abstraction",
        "delete_abstractions",
        "_create_record",
        "update_rule_authority",
        "update_rule_confidence",
        "increment_positive",
        "increment_negative",
        "create_project",
        "clear_project",
    ]

    @pytest.mark.parametrize("name", _TARGET_METHOD_NAMES)
    def test_method_calls_self_run_single_not_inline_session(self, name: str) -> None:
        method = getattr(Neo4jConnection, name)
        source = inspect.getsource(method)
        assert "self._run_single(" in source, (
            f"{name} has not been migrated to self._run_single(...) yet "
            "(Wave-3 Cycle B2)"
        )
        assert "await session.run(" not in source, (
            f"{name} still hand-rolls `await session.run(...)` inline; "
            "migrate to self._run_single(...)"
        )
        assert "await result.single()" not in source, (
            f"{name} still hand-rolls `await result.single()` inline; "
            "migrate to self._run_single(...)"
        )


class TestCreateRule:
    def test_sends_expected_query_and_params(self) -> None:
        conn, calls = _make_conn(_FakeResult(single=_FakeRecord({"rule_id": "RULE-TEST-001"})))
        rule_data = {"rule_id": "RULE-TEST-001", "statement": "test statement", "domain": "testing"}
        asyncio.run(conn.create_rule(rule_data))
        assert calls == [
            {
                "query": _CREATE_RULE_QUERY,
                "params": {
                    "rule_id": "RULE-TEST-001",
                    "project": "writ",
                    "props": {
                        "statement": "test statement",
                        "domain": "testing",
                        "source_origin": "ingest",
                        "provenance": "hand-authored",
                    },
                },
            }
        ]

    def test_returns_the_rule_id_from_the_row(self) -> None:
        conn, _calls = _make_conn(_FakeResult(single=_FakeRecord({"rule_id": "RULE-TEST-001"})))
        result = asyncio.run(conn.create_rule({"rule_id": "RULE-TEST-001", "statement": "x"}))
        assert result == "RULE-TEST-001"


class TestCreateMethodologyNode:
    def test_sends_expected_query_and_params(self) -> None:
        conn, calls = _make_conn(_FakeResult(single=_FakeRecord({"id": "SKL-TEST-001"})))
        data = {"skill_id": "SKL-TEST-001", "statement": "test skill"}
        asyncio.run(conn.create_methodology_node("Skill", data))
        assert calls == [
            {
                "query": _CREATE_METHODOLOGY_NODE_SKILL_QUERY,
                "params": {
                    "node_id": "SKL-TEST-001",
                    "project": "writ",
                    "props": {
                        "statement": "test skill",
                        "source_origin": "ingest",
                        "provenance": "hand-authored",
                    },
                },
            }
        ]

    def test_returns_the_id_from_the_row(self) -> None:
        conn, _calls = _make_conn(_FakeResult(single=_FakeRecord({"id": "SKL-TEST-001"})))
        data = {"skill_id": "SKL-TEST-001", "statement": "x"}
        result = asyncio.run(conn.create_methodology_node("Skill", data))
        assert result == "SKL-TEST-001"


class TestCreateAbstraction:
    def test_sends_expected_query_and_params(self) -> None:
        conn, calls = _make_conn(_FakeResult(single=_FakeRecord({"abstraction_id": "ABS-TEST-001"})))
        data = {"abstraction_id": "ABS-TEST-001", "domain": "testing"}
        asyncio.run(conn.create_abstraction(data))
        assert calls == [
            {
                "query": _CREATE_ABSTRACTION_QUERY,
                "params": {
                    "abstraction_id": "ABS-TEST-001",
                    "project": "writ",
                    "props": {"domain": "testing"},
                },
            }
        ]

    def test_returns_the_abstraction_id_from_the_row(self) -> None:
        conn, _calls = _make_conn(_FakeResult(single=_FakeRecord({"abstraction_id": "ABS-TEST-001"})))
        data = {"abstraction_id": "ABS-TEST-001", "domain": "testing"}
        result = asyncio.run(conn.create_abstraction(data))
        assert result == "ABS-TEST-001"


class TestDeleteAbstractions:
    def test_no_project_sends_expected_query_and_empty_params(self) -> None:
        conn, calls = _make_conn(_FakeResult(single=_FakeRecord({"deleted": 3})))
        asyncio.run(conn.delete_abstractions())
        assert calls == [{"query": _DELETE_ABSTRACTIONS_NO_PROJECT_QUERY, "params": {}}]

    def test_with_project_sends_expected_query_and_params(self) -> None:
        conn, calls = _make_conn(_FakeResult(single=_FakeRecord({"deleted": 2})))
        asyncio.run(conn.delete_abstractions(project="writ"))
        assert calls == [
            {"query": _DELETE_ABSTRACTIONS_WITH_PROJECT_QUERY, "params": {"project": "writ"}}
        ]

    def test_returns_the_deleted_count_from_the_row(self) -> None:
        conn, _calls = _make_conn(_FakeResult(single=_FakeRecord({"deleted": 5})))
        result = asyncio.run(conn.delete_abstractions())
        assert result == 5


class TestCreateRecord:
    """Differential for RecordStoreMixin._create_record, driven via create_decision
    (label='Decision', id_field='decision_id'). create_filechange/create_commit
    share the same helper -- this is the shared-code contract, not a claim about
    every caller's specific query text."""

    _DECISION_KWARGS = {
        "decision_id": "DEC-TEST-001",
        "project": "writ",
        "title": "Test decision",
        "rationale": "because tests",
        "phase": "1a",
        "session_id": "SES-1",
        "ts": "2026-01-01T00:00:00Z",
    }

    def test_sends_expected_query_and_params(self) -> None:
        conn, calls = _make_conn(_FakeResult(single=_FakeRecord({"decision_id": "DEC-TEST-001"})))
        asyncio.run(conn.create_decision(**self._DECISION_KWARGS))
        assert calls == [
            {
                "query": _CREATE_RECORD_DECISION_QUERY,
                "params": {
                    "decision_id": "DEC-TEST-001",
                    "project": "writ",
                    "props": {
                        "decision_id": "DEC-TEST-001",
                        "project": "writ",
                        "title": "Test decision",
                        "rationale": "because tests",
                        "planned_files": [],
                        "governing_rule_ids": [],
                        "phase": "1a",
                        "session_id": "SES-1",
                        "ts": "2026-01-01T00:00:00Z",
                        "provenance": "record",
                        "source_origin": "graph-authored",
                    },
                },
            }
        ]

    def test_returns_the_decision_id_from_the_row(self) -> None:
        conn, _calls = _make_conn(_FakeResult(single=_FakeRecord({"decision_id": "DEC-TEST-001"})))
        result = asyncio.run(conn.create_decision(**self._DECISION_KWARGS))
        assert result == "DEC-TEST-001"


class TestUpdateRuleAuthority:
    def test_sends_expected_query_and_params(self) -> None:
        conn, calls = _make_conn(_FakeResult(single=_FakeRecord({"rule_id": "RULE-1"})))
        asyncio.run(conn.update_rule_authority("RULE-1", "ai-provisional"))
        assert calls == [
            {
                "query": _UPDATE_RULE_AUTHORITY_QUERY,
                "params": {"rule_id": "RULE-1", "authority": "ai-provisional"},
            }
        ]

    def test_returns_true_when_the_rule_is_found(self) -> None:
        conn, _calls = _make_conn(_FakeResult(single=_FakeRecord({"rule_id": "RULE-1"})))
        result = asyncio.run(conn.update_rule_authority("RULE-1", "ai-provisional"))
        assert result is True

    def test_returns_false_when_no_matching_rule(self) -> None:
        conn, _calls = _make_conn(_FakeResult(single=None))
        result = asyncio.run(conn.update_rule_authority("RULE-missing", "ai-provisional"))
        assert result is False


class TestUpdateRuleConfidence:
    def test_sends_expected_query_and_params(self) -> None:
        conn, calls = _make_conn(_FakeResult(single=_FakeRecord({"rule_id": "RULE-1"})))
        asyncio.run(conn.update_rule_confidence("RULE-1", "high"))
        assert calls == [
            {
                "query": _UPDATE_RULE_CONFIDENCE_QUERY,
                "params": {"rule_id": "RULE-1", "confidence": "high"},
            }
        ]

    def test_returns_true_when_the_rule_is_found(self) -> None:
        conn, _calls = _make_conn(_FakeResult(single=_FakeRecord({"rule_id": "RULE-1"})))
        result = asyncio.run(conn.update_rule_confidence("RULE-1", "high"))
        assert result is True

    def test_returns_false_when_no_matching_rule(self) -> None:
        conn, _calls = _make_conn(_FakeResult(single=None))
        result = asyncio.run(conn.update_rule_confidence("RULE-missing", "high"))
        assert result is False


class TestIncrementPositive:
    def test_sends_expected_query_and_params(self) -> None:
        conn, calls = _make_conn(_FakeResult(single=_FakeRecord({"rule_id": "RULE-1"})))
        asyncio.run(conn.increment_positive("RULE-1"))
        assert calls == [{"query": _INCREMENT_POSITIVE_QUERY, "params": {"rule_id": "RULE-1"}}]

    def test_returns_true_when_the_rule_is_found(self) -> None:
        conn, _calls = _make_conn(_FakeResult(single=_FakeRecord({"rule_id": "RULE-1"})))
        result = asyncio.run(conn.increment_positive("RULE-1"))
        assert result is True

    def test_returns_false_when_no_matching_rule(self) -> None:
        conn, _calls = _make_conn(_FakeResult(single=None))
        result = asyncio.run(conn.increment_positive("RULE-missing"))
        assert result is False


class TestIncrementNegative:
    def test_sends_expected_query_and_params(self) -> None:
        conn, calls = _make_conn(_FakeResult(single=_FakeRecord({"rule_id": "RULE-1"})))
        asyncio.run(conn.increment_negative("RULE-1"))
        assert calls == [{"query": _INCREMENT_NEGATIVE_QUERY, "params": {"rule_id": "RULE-1"}}]

    def test_returns_true_when_the_rule_is_found(self) -> None:
        conn, _calls = _make_conn(_FakeResult(single=_FakeRecord({"rule_id": "RULE-1"})))
        result = asyncio.run(conn.increment_negative("RULE-1"))
        assert result is True

    def test_returns_false_when_no_matching_rule(self) -> None:
        conn, _calls = _make_conn(_FakeResult(single=None))
        result = asyncio.run(conn.increment_negative("RULE-missing"))
        assert result is False


class TestCreateProject:
    def test_sends_expected_query_and_params(self) -> None:
        conn, calls = _make_conn(
            _FakeResult(single=_FakeRecord({"name": "writ", "remote_url": None}))
        )
        asyncio.run(conn.create_project("writ", "/repo/writ", "/repo/writ/bible"))
        assert calls == [
            {
                "query": _CREATE_PROJECT_QUERY,
                "params": {
                    "name": "writ",
                    "repo_root": "/repo/writ",
                    "bible_root": "/repo/writ/bible",
                    "remote_url": None,
                },
            }
        ]

    def test_returns_the_name_when_remote_url_not_supplied(self) -> None:
        conn, _calls = _make_conn(
            _FakeResult(single=_FakeRecord({"name": "writ", "remote_url": None}))
        )
        result = asyncio.run(conn.create_project("writ", "/repo/writ", "/repo/writ/bible"))
        assert result == "writ"

    def test_raises_project_identity_conflict_on_stored_remote_mismatch(self) -> None:
        conn, _calls = _make_conn(
            _FakeResult(single=_FakeRecord({"name": "otherproj", "remote_url": "https://a"}))
        )
        with pytest.raises(ProjectIdentityConflict):
            asyncio.run(
                conn.create_project(
                    "otherproj", "/repo/other", "/repo/other/bible", remote_url="https://b",
                )
            )

    def test_returns_the_name_when_remote_url_matches_stored_value(self) -> None:
        conn, _calls = _make_conn(
            _FakeResult(single=_FakeRecord({"name": "otherproj", "remote_url": "https://a"}))
        )
        result = asyncio.run(
            conn.create_project(
                "otherproj", "/repo/other", "/repo/other/bible", remote_url="https://a",
            )
        )
        assert result == "otherproj"


class TestClearProject:
    def test_sends_expected_query_and_params(self) -> None:
        conn, calls = _make_conn(_FakeResult(single=_FakeRecord({"deleted": 5})))
        asyncio.run(conn.clear_project("writ"))
        assert calls == [{"query": _CLEAR_PROJECT_QUERY, "params": {"project": "writ"}}]

    def test_returns_the_deleted_count_from_the_row(self) -> None:
        conn, _calls = _make_conn(_FakeResult(single=_FakeRecord({"deleted": 5})))
        result = asyncio.run(conn.clear_project("writ"))
        assert result == 5

    def test_returns_zero_when_single_yields_none(self) -> None:
        conn, _calls = _make_conn(_FakeResult(single=None))
        result = asyncio.run(conn.clear_project("writ"))
        assert result == 0
