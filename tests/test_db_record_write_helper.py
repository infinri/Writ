"""Wave-3 dedup Cycle A: RecordStoreMixin._create_record helper.

record_store.py's create_decision / create_filechange / create_commit each
inline the same three-line shape (build props -> MERGE-by-id-and-project ->
SET n += $props -> RETURN the id). The planned refactor extracts a single
`_create_record(self, model, id_field)` helper and has each create_* method
build its Pydantic model, then delegate. See writ/graph/db/record_store.py:11-62.

RED-now / GREEN-after-implementation split in THIS file:
  - TestCreateRecordHelperSeam is the actual RED signal for this cycle: the
    helper does not exist yet, and the frozen method-surface list in
    tests/test_db_split_seam.py (EXPECTED_METHODS) has not been updated to
    include it. Both assertions flip to GREEN only once the helper lands AND
    the seam list is updated in the same change.
  - Every other class asserts the byte-identical query/params/return contract
    of the three create_* methods. That contract is ALREADY true of today's
    inline code (the planned query strings are copied verbatim from
    record_store.py), so those tests are the regression net that must stay
    green THROUGH the refactor, not a RED-now signal.

ENF-SYS-005 disclosure: every test below drives create_decision/create_filechange/
create_commit against a hermetic fake async driver (_FakeDriver/_FakeSession/
_FakeResult) -- no real Neo4j, the shared graph is never touched. This proves the
Cypher text, parameter shape, and stamped-property contract the helper must
preserve. It does NOT and CANNOT prove MERGE idempotency, concurrent-write safety,
or that the query executes correctly against a real Neo4j engine -- the fake
session.run never touches a database. That coverage is a separate concern already
owned by the live-Neo4j-gated tests in tests/test_decision_memory_records.py
(e.g. test_create_decision_is_idempotent, test_create_commit_is_idempotent),
which this file does not duplicate or replace.

Run: .venv/bin/python -m pytest tests/test_db_record_write_helper.py -q
"""
from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from writ.graph.db._query_runner import _QueryRunnerMixin
from writ.graph.db.record_store import RecordStoreMixin
from writ.graph.schema import Commit, Decision, FileChange

# ---------------------------------------------------------------------------
# Expected Cypher text, copied verbatim from the planned helper's callers
# (record_store.py:11-62 today). Byte-for-byte, including the single-space
# joins between the three concatenated string fragments in the source.
# ---------------------------------------------------------------------------

_DECISION_QUERY = (
    "MERGE (n:Decision {decision_id: $decision_id, project: $project}) "
    "SET n += $props "
    "RETURN n.decision_id AS decision_id"
)
_FILECHANGE_QUERY = (
    "MERGE (n:FileChange {change_id: $change_id, project: $project}) "
    "SET n += $props "
    "RETURN n.change_id AS change_id"
)
_COMMIT_QUERY = (
    "MERGE (n:Commit {commit_hash: $commit_hash, project: $project}) "
    "SET n += $props "
    "RETURN n.commit_hash AS commit_hash"
)


# ---------------------------------------------------------------------------
# Hermetic fake async driver -- captures every session.run call, never opens
# a socket, never touches the shared Neo4j graph.
# ---------------------------------------------------------------------------


class _FakeResult:
    """Stands in for neo4j's AsyncResult. Real Result.single() is awaitable."""

    def __init__(self, row: dict) -> None:
        self._row = row

    async def single(self) -> dict:
        return self._row


class _FakeSession:
    """Async context manager standing in for neo4j's AsyncSession.

    Appends every run() call to a shared `calls` list so tests can inspect
    exactly what create_* sent, then echoes back whichever id parameter
    (decision_id/change_id/commit_hash) was passed so single() returns a
    plausible row without any real query execution.
    """

    def __init__(self, calls: list[dict]) -> None:
        self._calls = calls

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    async def run(self, query: str, **params) -> _FakeResult:
        self._calls.append({"query": query, "params": params})
        for id_field in ("decision_id", "change_id", "commit_hash"):
            if id_field in params:
                return _FakeResult({id_field: params[id_field]})
        return _FakeResult({})


class _FakeDriver:
    """Stands in for neo4j's AsyncDriver. session() returns a fresh _FakeSession
    bound to the same shared `calls` list every time (mirrors real driver reuse
    across `async with self._driver.session(...)` blocks)."""

    def __init__(self, calls: list[dict]) -> None:
        self._calls = calls

    def session(self, database: str | None = None) -> _FakeSession:
        return _FakeSession(self._calls)


class _RecordStoreUnderTest(_QueryRunnerMixin, RecordStoreMixin):
    """RecordStoreMixin composed with the query runner, mirroring production.

    RecordStoreMixin._create_record calls self._run_single, which is defined on
    _QueryRunnerMixin -- Neo4jConnection resolves it through the MRO
    (writ/graph/db/__init__.py: `class Neo4jConnection(_QueryRunnerMixin,
    ..., RecordStoreMixin, ...)`). Instantiating RecordStoreMixin ALONE, as this
    fixture previously did via __new__, builds an object that cannot exist in
    production and fails with AttributeError: no attribute '_run_single'.
    Composing the same two mixins in the same order tests the real resolution.
    """


@pytest.fixture()
def hermetic_conn():
    """Fresh record-store instance wired to a fresh fake driver.

    No __init__ call (no real connection): _driver/_database are set the same
    way Neo4jConnection.__init__ sets them, so create_decision/create_filechange/
    create_commit run unmodified against the fake. Fresh calls list per test
    (TEST-ISOLATE-001): nothing here is shared across tests.

    Returns (conn, calls).
    """
    calls: list[dict] = []
    conn = _RecordStoreUnderTest.__new__(_RecordStoreUnderTest)
    conn._driver = _FakeDriver(calls)
    conn._database = "neo4j"
    return conn, calls


# ---------------------------------------------------------------------------
# Minimal valid model-input factories (schema.py required fields only).
# ---------------------------------------------------------------------------


def _decision_data(**overrides) -> dict:
    defaults = {
        "decision_id": "DEC-helper-0001",
        "project": "test-helper",
        "title": "Add _create_record helper",
        "rationale": "Dedup the three create_* MERGE blocks.",
        "phase": "1a",
        "session_id": "sess-helper-0001",
        "ts": "2026-07-13T00:00:00Z",
    }
    return {**defaults, **overrides}


def _filechange_data(**overrides) -> dict:
    defaults = {
        "change_id": "FC-helper-0001",
        "project": "test-helper",
        "path": "writ/graph/db/record_store.py",
        "change_type": "modify",
        "reason": "extract _create_record helper",
    }
    return {**defaults, **overrides}


def _commit_data(**overrides) -> dict:
    defaults = {
        "commit_hash": "abc123def456abc123def456abc123def456ab",
        "project": "test-helper",
        "subject": "refactor(db): add _create_record helper",
        "author": "Test Author",
        "branch": "refactor/w3-record-store-dedup",
    }
    return {**defaults, **overrides}


# ---------------------------------------------------------------------------
# RED signal for this cycle: the helper's existence + its registration in the
# frozen method-surface seam list.
# ---------------------------------------------------------------------------


class TestCreateRecordHelperSeam:
    def test_record_store_mixin_has_create_record_helper(self) -> None:
        """RED now, GREEN after implementation: _create_record does not exist
        on RecordStoreMixin until create_decision/create_filechange/create_commit
        are refactored to delegate to it."""
        assert hasattr(RecordStoreMixin, "_create_record"), (
            "RecordStoreMixin._create_record is missing -- the planned dedup "
            "helper has not been added yet"
        )

    def test_create_record_is_registered_in_split_seam_expected_methods(self) -> None:
        """RED until the seam list is updated: once _create_record is added to
        RecordStoreMixin, it becomes part of Neo4jConnection's resolved method
        surface (test_db_split_seam.py's test_no_extra_or_missing_methods
        filters single-underscore helpers IN, e.g. _parse_planned_files is
        already listed). EXPECTED_METHODS must be updated in the same change or
        that seam test starts failing on an unlisted extra method."""
        from tests.test_db_split_seam import EXPECTED_METHODS

        assert "_create_record" in EXPECTED_METHODS, (
            "tests/test_db_split_seam.py:EXPECTED_METHODS has not been updated "
            "to include '_create_record'; update it in the same change that "
            "adds the helper to RecordStoreMixin"
        )


# ---------------------------------------------------------------------------
# Behavioral regression net: query text / params shape / stamped props /
# return value. True today (inline code); must stay true after the refactor.
# ---------------------------------------------------------------------------


class TestCreateDecision:
    def test_sends_exactly_one_session_run_call(self, hermetic_conn) -> None:
        conn, calls = hermetic_conn
        asyncio.run(conn.create_decision(**_decision_data()))
        assert len(calls) == 1

    def test_query_matches_expected_cypher_text(self, hermetic_conn) -> None:
        conn, calls = hermetic_conn
        asyncio.run(conn.create_decision(**_decision_data()))
        assert calls[0]["query"] == _DECISION_QUERY

    def test_params_have_exactly_decision_id_project_props_keys(self, hermetic_conn) -> None:
        conn, calls = hermetic_conn
        asyncio.run(conn.create_decision(**_decision_data()))
        assert set(calls[0]["params"].keys()) == {"decision_id", "project", "props"}

    def test_params_carry_the_model_decision_id_and_project(self, hermetic_conn) -> None:
        conn, calls = hermetic_conn
        data = _decision_data(decision_id="DEC-helper-0002", project="another-project")
        asyncio.run(conn.create_decision(**data))
        params = calls[0]["params"]
        assert params["decision_id"] == "DEC-helper-0002"
        assert params["project"] == "another-project"

    def test_props_stamp_provenance_record_and_source_origin_graph_authored(
        self, hermetic_conn
    ) -> None:
        conn, calls = hermetic_conn
        asyncio.run(conn.create_decision(**_decision_data()))
        props = calls[0]["params"]["props"]
        assert props["provenance"] == "record"
        assert props["source_origin"] == "graph-authored"

    def test_returns_the_decision_id(self, hermetic_conn) -> None:
        conn, calls = hermetic_conn
        result = asyncio.run(conn.create_decision(**_decision_data(decision_id="DEC-helper-ret")))
        assert result == "DEC-helper-ret"
        assert result == calls[0]["params"]["decision_id"]

    def test_omitting_ts_raises_validation_error_not_auto_set(self, hermetic_conn) -> None:
        """Decision.ts is a required model field (no default) and create_decision
        does not setdefault it (unlike create_filechange/create_commit) -- so
        omitting ts must fail model construction, not silently get a generated
        value."""
        conn, _calls = hermetic_conn
        data = _decision_data()
        del data["ts"]
        with pytest.raises(ValidationError):
            asyncio.run(conn.create_decision(**data))


class TestCreateFilechange:
    def test_sends_exactly_one_session_run_call(self, hermetic_conn) -> None:
        conn, calls = hermetic_conn
        asyncio.run(conn.create_filechange(**_filechange_data()))
        assert len(calls) == 1

    def test_query_matches_expected_cypher_text(self, hermetic_conn) -> None:
        conn, calls = hermetic_conn
        asyncio.run(conn.create_filechange(**_filechange_data()))
        assert calls[0]["query"] == _FILECHANGE_QUERY

    def test_params_have_exactly_change_id_project_props_keys(self, hermetic_conn) -> None:
        conn, calls = hermetic_conn
        asyncio.run(conn.create_filechange(**_filechange_data()))
        assert set(calls[0]["params"].keys()) == {"change_id", "project", "props"}

    def test_params_carry_the_model_change_id_and_project(self, hermetic_conn) -> None:
        conn, calls = hermetic_conn
        data = _filechange_data(change_id="FC-helper-0002", project="another-project")
        asyncio.run(conn.create_filechange(**data))
        params = calls[0]["params"]
        assert params["change_id"] == "FC-helper-0002"
        assert params["project"] == "another-project"

    def test_props_stamp_provenance_record_and_source_origin_graph_authored(
        self, hermetic_conn
    ) -> None:
        conn, calls = hermetic_conn
        asyncio.run(conn.create_filechange(**_filechange_data()))
        props = calls[0]["params"]["props"]
        assert props["provenance"] == "record"
        assert props["source_origin"] == "graph-authored"

    def test_auto_sets_ts_when_omitted(self, hermetic_conn) -> None:
        conn, calls = hermetic_conn
        data = _filechange_data()
        assert "ts" not in data
        asyncio.run(conn.create_filechange(**data))
        props = calls[0]["params"]["props"]
        assert isinstance(props["ts"], str) and props["ts"] != ""

    def test_does_not_override_an_explicitly_supplied_ts(self, hermetic_conn) -> None:
        conn, calls = hermetic_conn
        data = _filechange_data(ts="2020-01-01T00:00:00Z")
        asyncio.run(conn.create_filechange(**data))
        props = calls[0]["params"]["props"]
        assert props["ts"] == "2020-01-01T00:00:00Z"

    def test_returns_the_change_id(self, hermetic_conn) -> None:
        conn, calls = hermetic_conn
        result = asyncio.run(
            conn.create_filechange(**_filechange_data(change_id="FC-helper-ret"))
        )
        assert result == "FC-helper-ret"
        assert result == calls[0]["params"]["change_id"]


class TestCreateCommit:
    def test_sends_exactly_one_session_run_call(self, hermetic_conn) -> None:
        conn, calls = hermetic_conn
        asyncio.run(conn.create_commit(**_commit_data()))
        assert len(calls) == 1

    def test_query_matches_expected_cypher_text(self, hermetic_conn) -> None:
        conn, calls = hermetic_conn
        asyncio.run(conn.create_commit(**_commit_data()))
        assert calls[0]["query"] == _COMMIT_QUERY

    def test_params_have_exactly_commit_hash_project_props_keys(self, hermetic_conn) -> None:
        conn, calls = hermetic_conn
        asyncio.run(conn.create_commit(**_commit_data()))
        assert set(calls[0]["params"].keys()) == {"commit_hash", "project", "props"}

    def test_params_carry_the_model_commit_hash_and_project(self, hermetic_conn) -> None:
        conn, calls = hermetic_conn
        data = _commit_data(
            commit_hash="def456abc123def456abc123def456abc123de",
            project="another-project",
        )
        asyncio.run(conn.create_commit(**data))
        params = calls[0]["params"]
        assert params["commit_hash"] == "def456abc123def456abc123def456abc123de"
        assert params["project"] == "another-project"

    def test_props_stamp_provenance_record_and_source_origin_graph_authored(
        self, hermetic_conn
    ) -> None:
        conn, calls = hermetic_conn
        asyncio.run(conn.create_commit(**_commit_data()))
        props = calls[0]["params"]["props"]
        assert props["provenance"] == "record"
        assert props["source_origin"] == "graph-authored"

    def test_auto_sets_ts_when_omitted(self, hermetic_conn) -> None:
        conn, calls = hermetic_conn
        data = _commit_data()
        assert "ts" not in data
        asyncio.run(conn.create_commit(**data))
        props = calls[0]["params"]["props"]
        assert isinstance(props["ts"], str) and props["ts"] != ""

    def test_does_not_override_an_explicitly_supplied_ts(self, hermetic_conn) -> None:
        conn, calls = hermetic_conn
        data = _commit_data(ts="2020-01-01T00:00:00Z")
        asyncio.run(conn.create_commit(**data))
        props = calls[0]["params"]["props"]
        assert props["ts"] == "2020-01-01T00:00:00Z"

    def test_returns_the_commit_hash(self, hermetic_conn) -> None:
        conn, calls = hermetic_conn
        result = asyncio.run(
            conn.create_commit(**_commit_data(commit_hash="ff00ff00ff00ff00ff00ff00ff00ff00ff00f"))
        )
        assert result == "ff00ff00ff00ff00ff00ff00ff00ff00ff00f"
        assert result == calls[0]["params"]["commit_hash"]


# ---------------------------------------------------------------------------
# Sanity check on the fake driver itself: a model-validation constructor
# import smoke test, so a schema.py signature drift is caught here rather
# than surfacing only as a confusing failure deep in the create_* tests above.
# ---------------------------------------------------------------------------


class TestModelConstructorsAcceptTheFactoryData:
    def test_decision_model_accepts_the_minimal_factory_dict(self) -> None:
        Decision(**_decision_data())

    def test_filechange_model_accepts_the_minimal_factory_dict_plus_ts(self) -> None:
        FileChange(**_filechange_data(ts="2026-07-13T00:00:00Z"))

    def test_commit_model_accepts_the_minimal_factory_dict_plus_ts(self) -> None:
        Commit(**_commit_data(ts="2026-07-13T00:00:00Z"))
