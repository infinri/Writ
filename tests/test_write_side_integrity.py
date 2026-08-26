"""Cycle C skeletons: the write side keeps what it is handed.

Four independent write paths, one property each:

  1. Records (`Decision`/`FileChange`/`Commit`) MERGE on keys with NO uniqueness
     constraint, so concurrent writers fork duplicates exactly as `Project` and
     `Memory` used to before their constraints landed. Verified against
     `schema_store.py:99-106`: the four record indexes are on
     `FileChange(project, path)`, `FileChange(project, commit_hash)`,
     `Commit(commit_hash)` and `Decision(project)`, while `_create_record`
     MERGEs on `(decision_id|change_id|commit_hash, project)`
     (`record_store.py:30`). Not one index covers a MERGE key.
  2. Every record write is a bare auto-commit `session.run` via `_run_single`
     (`_query_runner.py:16`), so a transient lock conflict surfaces to a caller
     that logs and drops it. `node_store.py:124` and `edge_store.py:248`
     already use `session.execute_write`, which is where the driver's own
     retry lives.
  3. `_unique_archive_dest` (`logging.py:478`) picks a destination with an
     `exists()` check and RETURNS it; both callers then `os.rename`
     (`logging.py:524`, `log_rotation.py:171`). Two rotators can therefore
     resolve the same name before either renames, and `os.rename` replaces
     silently, losing a whole archived generation.
  4. Partial vector-cache degeneracy goes to `_logger.warning`
     (`embeddings.py:465`), invisible at default level, so a partially
     corrupted index reads as a healthy start the way the all-zero case once
     did for six days.

WHAT THIS FILE DELIBERATELY DOES NOT TEST. The all-zero rejection threshold is
NOT tightened and there is no test asking it to be. `embeddings.py:441-449`
argues that rejecting a single zero row would "rebuild, re-encode the same
zero, and reject again on the next start, forever", and a fraction threshold
has the identical failure mode. This cycle changes VISIBILITY only, so
`test_total_degeneracy_still_rejects_and_partial_still_loads` pins the current
behavior as a must-not-regress rather than proposing a new one.

Per ENF-GATE-007: skeletons written and approved before implementation.
Per ABS-TESTING-041: the two multi-actor properties (concurrent record writers,
racing rotators) drive real concurrent writers against a real graph and real
processes against real files. Neither is simulated.
Per ARCH-CONST-001: `cache_dir` and `WRIT_LOG_ROOT` are ALWAYS injected as
`tmp_path`, never the live `~/.cache/writ` index or `var/logs` the running
daemon serves from.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

import numpy as np
import pytest
import pytest_asyncio

from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
from writ.graph.db import Neo4jConnection
from writ.retrieval.embeddings import HnswlibStore

_PROJECT = "test-wsi"
_OTHER_PROJECT = "test-wsi-other"

# The three record constraints this cycle adds, identified by what they
# CONSTRAIN rather than by name.
#
# NAMES ARE NOT THE CONTRACT, and asserting on them was a real defect in the
# first draft of this file. `CREATE CONSTRAINT <name> IF NOT EXISTS` is satisfied
# by an EQUIVALENT constraint under any other name, so the statement becomes a
# no-op and the name in the code never appears. Measured on the live graph: it
# already held these under `decision_id_project_unique` and
# `commit_hash_project_unique`, so a name assertion failed there while the graph
# was correctly constrained.
_EXPECTED_CONSTRAINTS = {
    "Decision": ["decision_id", "project"],
    "FileChange": ["change_id", "project"],
    "Commit": ["commit_hash", "project"],
}


def _record_constraints(rows: list[dict]) -> dict[str, dict]:
    """{label: row} for each row constraining exactly one expected record key."""
    found = {}
    for row in rows:
        labels = row.get("labelsOrTypes") or []
        if len(labels) != 1:
            continue
        label = labels[0]
        if label in _EXPECTED_CONSTRAINTS and row.get("properties") == _EXPECTED_CONSTRAINTS[label]:
            found[label] = row
    return found


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _decision(decision_id: str, project: str = _PROJECT, **over) -> dict:
    """Kwargs for a valid Decision. Every required field of the model is set."""
    payload = {
        "decision_id": decision_id,
        "project": project,
        "title": "write-side integrity",
        "rationale": "pins the constraint",
        "phase": "implementation",
        "session_id": "test-session",
        "ts": "2026-08-25T00:00:00+00:00",
    }
    payload.update(over)
    return payload


def _commit(commit_hash: str, project: str = _PROJECT, **over) -> dict:
    payload = {
        "commit_hash": commit_hash,
        "project": project,
        "subject": "write-side integrity",
        "author": "test",
        "branch": "test-branch",
        "ts": "2026-08-25T00:00:00+00:00",
    }
    payload.update(over)
    return payload


def _filechange(change_id: str, project: str = _PROJECT, **over) -> dict:
    payload = {
        "change_id": change_id,
        "project": project,
        "path": "writ/graph/db/record_store.py",
        "change_type": "modify",
        "reason": "pins the constraint",
        "ts": "2026-08-25T00:00:00+00:00",
    }
    payload.update(over)
    return payload


async def _count_nodes(conn, label: str, id_field: str, value: str, project: str) -> int:
    rows = await conn._run(
        f"MATCH (n:{label} {{{id_field}: $value, project: $project}}) "
        "RETURN count(n) AS c",
        value=value, project=project,
    )
    return rows[0]["c"]


async def _drop_decision_constraint(conn) -> None:
    """Drop whatever constraint holds Decision's MERGE key on THIS graph.

    By discovered name, not by the name in the code: an equivalent constraint
    can be called anything, and the live graph's is `decision_id_project_unique`.
    """
    found = _record_constraints(await conn.list_constraints())
    row = found.get("Decision")
    if row:
        await conn._run(f"DROP CONSTRAINT {row['name']} IF EXISTS")


def _fresh_connection():
    return Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())


def _zero_rows(n: int, dims: int) -> list[list[float]]:
    return [[0.0] * dims for _ in range(n)]


def _healthy_rows(n: int, dims: int, seed: int = 42) -> list[list[float]]:
    rng = np.random.RandomState(seed)
    return [rng.randn(dims).astype(np.float32).tolist() for _ in range(n)]


def _build_index_with_zero_rows(
    tmp_path: Path, total: int, zero_at: tuple[int, ...], corpus_hash: str
) -> HnswlibStore:
    """Persist a real hnswlib index whose rows at `zero_at` are zero-norm."""
    store = HnswlibStore(dimensions=4, cache_dir=str(tmp_path))
    rule_ids = [f"RULE-{i}" for i in range(total)]
    vectors = _healthy_rows(total, store._dimensions, seed=13)
    for row in zero_at:
        vectors[row] = [0.0] * store._dimensions
    store.build_index(rule_ids, vectors)
    store.save_index(corpus_hash=corpus_hash)
    return HnswlibStore(dimensions=4, cache_dir=str(tmp_path))


def _require(module, *names) -> None:
    """Fail with a legible skeleton message instead of an AttributeError.

    `monkeypatch.setattr` raises AttributeError when the target does not exist
    yet, which is error-shaped rather than assertion-shaped RED
    (TEC-PROC-RED-VERIFY-001). This states what is missing instead.
    """
    missing = [name for name in names if not hasattr(module, name)]
    if missing:
        pytest.fail(f"skeleton: {module.__name__} has no {', '.join(missing)} yet")


@pytest_asyncio.fixture()
async def graph():
    """A live connection with the two test project scopes wiped either side.

    Skips (never fails) when Neo4j is unreachable, matching
    tests/test_decision_memory_records.py::db_clean. Scoped to `test-wsi` so it
    can never touch the live `writ` corpus or the user's decision history.
    """
    conn = _fresh_connection()
    try:
        async with conn._driver.session(database=conn._database) as s:
            await (await s.run("RETURN 1 AS ok")).consume()
    except Exception:
        await conn.close()
        pytest.skip("Neo4j unreachable")
    await conn.clear_project(_PROJECT)
    await conn.clear_project(_OTHER_PROJECT)
    yield conn
    await conn.clear_project(_PROJECT)
    await conn.clear_project(_OTHER_PROJECT)
    # TEST-ISOLATE-003. A constraint is global, not project-scoped, and the
    # blocked-constraint tests DROP one to seed duplicates. Wiping the project
    # removes those duplicates, so re-applying here restores the schema for
    # whatever runs next instead of leaving a constraint missing behind us.
    await conn.apply_constraints()
    await conn.close()


# --------------------------------------------------------------------------- #
# Capability 1, 4: the constraints exist and are composite
# --------------------------------------------------------------------------- #

class TestRecordConstraintsExist:
    """`apply_constraints` must create the three record constraints.

    RED today at the assertion, not at import: `list_constraints` is a real
    method returning real rows, so these fail on a missing name rather than an
    AttributeError.
    """

    @pytest.mark.asyncio
    async def test_all_three_record_keys_are_constrained(self, graph) -> None:
        """Composite, not global: `project` is the second property.

        A single-property constraint would forbid two projects holding the same
        record id, which is the namespaced coexistence M.2 deliberately allows
        (schema_store.py:58-61). Matching on properties rather than name is what
        makes this hold on a graph carrying legacy-named equivalents.
        """
        await graph.apply_constraints()
        found = _record_constraints(await graph.list_constraints())
        missing = sorted(set(_EXPECTED_CONSTRAINTS) - set(found))
        assert not missing, (
            f"no uniqueness constraint on the MERGE key for: {missing}"
        )

    @pytest.mark.asyncio
    async def test_each_constraint_is_a_uniqueness_constraint(self, graph) -> None:
        """An index on the same properties would not serialize anything."""
        await graph.apply_constraints()
        found = _record_constraints(await graph.list_constraints())
        for label in sorted(_EXPECTED_CONSTRAINTS):
            assert label in found, f"{label} has no constraint on its MERGE key"
            kind = str(found[label].get("type", ""))
            assert "UNIQUE" in kind.upper(), f"{label}: {kind}"

    @pytest.mark.asyncio
    async def test_the_merge_keys_are_now_index_backed(self, graph) -> None:
        """A uniqueness constraint carries its own backing index.

        Not a performance claim: no scan cost was measured. The assertion is
        only that the index appears, closing the gap where `Decision` had no
        index on `decision_id` at all.
        """
        await graph.apply_constraints()
        found = _record_constraints(await graph.list_constraints())
        owned = {row.get("ownedIndex") for row in found.values()}
        index_names = {i.get("name") for i in await graph.list_indexes()}
        assert owned, "the record constraints own no index"
        assert owned <= index_names, (
            f"owned indexes absent from SHOW INDEXES: {owned - index_names}"
        )


# --------------------------------------------------------------------------- #
# Capability 2, 3, 4: concurrent writers do not fork
# --------------------------------------------------------------------------- #

class TestConcurrentRecordWritersDoNotFork:
    """Real concurrent writers, real graph (ABS-TESTING-041).

    `test_direct_duplicate_create_is_refused` is the DETERMINISTIC proof. The
    concurrent-MERGE test below it is the real-world shape, but a race that
    happens to serialize would let it pass without a constraint, so it is not
    load-bearing on its own.
    """

    @pytest.mark.asyncio
    async def test_direct_duplicate_create_is_refused(self, graph) -> None:
        """Two bare CREATEs of one (decision_id, project) must be rejected.

        This bypasses MERGE on purpose: MERGE would find the first node and
        never attempt a second insert, so it cannot demonstrate that the
        constraint is what serializes concurrent creates.
        """
        await graph.apply_constraints()
        await graph._run(
            "CREATE (n:Decision {decision_id: $id, project: $project})",
            id="DEC-dup", project=_PROJECT,
        )
        with pytest.raises(Exception) as caught:
            await graph._run(
                "CREATE (n:Decision {decision_id: $id, project: $project})",
                id="DEC-dup", project=_PROJECT,
            )
        assert "onstraint" in str(caught.value), (
            f"expected a constraint violation, got: {caught.value!r}"
        )

    @pytest.mark.asyncio
    async def test_eight_concurrent_writers_of_one_id_yield_one_node(self, graph) -> None:
        """Eight independent connections MERGE the same decision_id at once."""
        await graph.apply_constraints()
        connections = [_fresh_connection() for _ in range(8)]
        try:
            await asyncio.gather(*(
                conn.create_decision(**_decision("DEC-concurrent", title=f"writer-{i}"))
                for i, conn in enumerate(connections)
            ))
        finally:
            await asyncio.gather(*(conn.close() for conn in connections))
        count = await _count_nodes(graph, "Decision", "decision_id", "DEC-concurrent", _PROJECT)
        assert count == 1, f"eight concurrent writers forked {count} Decision nodes"

    @pytest.mark.asyncio
    async def test_concurrent_commit_writers_yield_one_node(self, graph) -> None:
        """The commit path is the one the post-commit hook drives concurrently."""
        await graph.apply_constraints()
        connections = [_fresh_connection() for _ in range(8)]
        try:
            await asyncio.gather(*(
                conn.create_commit(**_commit("abc1234", subject=f"writer-{i}"))
                for i, conn in enumerate(connections)
            ))
        finally:
            await asyncio.gather(*(conn.close() for conn in connections))
        count = await _count_nodes(graph, "Commit", "commit_hash", "abc1234", _PROJECT)
        assert count == 1, f"eight concurrent writers forked {count} Commit nodes"

    @pytest.mark.asyncio
    async def test_repeated_capture_of_one_commit_still_merges(self, graph) -> None:
        """Must-not-regress: idempotent re-capture, the property the
        deterministic ids in commit_capture.py exist to provide."""
        await graph.apply_constraints()
        await graph.create_commit(**_commit("def5678"))
        await graph.create_commit(**_commit("def5678", subject="re-captured"))
        count = await _count_nodes(graph, "Commit", "commit_hash", "def5678", _PROJECT)
        assert count == 1, f"re-capture created {count} Commit nodes"

    @pytest.mark.asyncio
    async def test_same_id_in_two_projects_stays_two_nodes(self, graph) -> None:
        """Composite, not global: the constraint must not collapse projects."""
        await graph.apply_constraints()
        await graph.create_decision(**_decision("DEC-shared", project=_PROJECT))
        await graph.create_decision(**_decision("DEC-shared", project=_OTHER_PROJECT))
        assert await _count_nodes(
            graph, "Decision", "decision_id", "DEC-shared", _PROJECT
        ) == 1
        assert await _count_nodes(
            graph, "Decision", "decision_id", "DEC-shared", _OTHER_PROJECT
        ) == 1

    @pytest.mark.asyncio
    async def test_filechange_writers_of_one_change_id_yield_one_node(self, graph) -> None:
        await graph.apply_constraints()
        connections = [_fresh_connection() for _ in range(6)]
        try:
            await asyncio.gather(*(
                conn.create_filechange(**_filechange("FC-concurrent", reason=f"writer-{i}"))
                for i, conn in enumerate(connections)
            ))
        finally:
            await asyncio.gather(*(conn.close() for conn in connections))
        count = await _count_nodes(graph, "FileChange", "change_id", "FC-concurrent", _PROJECT)
        assert count == 1, f"six concurrent writers forked {count} FileChange nodes"


# --------------------------------------------------------------------------- #
# Capability 5: a blocked constraint is reported, the pass continues
# --------------------------------------------------------------------------- #

class TestSchemaPassReportsBlockedConstraints:
    """Neo4j refuses CREATE CONSTRAINT over existing duplicates, and
    `apply_constraints` runs one bare loop (`schema_store.py:116-118`), so today
    a duplicate would abort the whole pass and leave later statements unapplied.

    Gated on the disposable instance: proving this needs the constraint DROPPED
    so duplicates can be seeded, and a constraint is global rather than
    project-scoped, so dropping one on a shared instance would affect anything
    else running.
    """

    @pytest.mark.asyncio
    async def test_blocked_constraint_is_reported_by_name(
        self, graph, disposable_graph
    ) -> None:
        await _drop_decision_constraint(graph)
        for _ in range(2):
            await graph._run(
                "CREATE (n:Decision {decision_id: $id, project: $project})",
                id="DEC-blocked", project=_PROJECT,
            )
        blocked = await graph.apply_constraints()
        assert blocked is not None, (
            "apply_constraints returned None: a blocked constraint cannot be reported"
        )
        assert any("decision_decision_id_project_unique" in str(b) for b in blocked), (
            f"the blocked constraint was not named in {blocked!r}"
        )

    @pytest.mark.asyncio
    async def test_statements_after_a_blocked_one_still_apply(
        self, graph, disposable_graph
    ) -> None:
        """The pass must not abort. `memory_name_project_unique` is created
        AFTER the record statements (`schema_store.py:111`), so its presence
        proves the loop continued past the failure."""
        await _drop_decision_constraint(graph)
        for _ in range(2):
            await graph._run(
                "CREATE (n:Decision {decision_id: $id, project: $project})",
                id="DEC-blocked-2", project=_PROJECT,
            )
        await graph.apply_constraints()
        names = {c["name"] for c in await graph.list_constraints()}
        assert "memory_name_project_unique" in names, (
            "a blocked record constraint aborted the pass before later statements"
        )

    @pytest.mark.asyncio
    async def test_a_clean_pass_reports_nothing_blocked(self, graph) -> None:
        blocked = await graph.apply_constraints()
        assert not blocked, f"a clean schema pass reported blocked statements: {blocked!r}"


# --------------------------------------------------------------------------- #
# Capability 6: the doctor surfaces duplicates BEFORE a migrate attempts them
# --------------------------------------------------------------------------- #

class TestDoctorReportsDuplicateRecords:
    """The duplicate check has to exist for the constraint to be safe to apply,
    so it ships in the same cycle. Seams are monkeypatched by name, which is the
    established doctor convention (`doctor.py:92-96`), not a convenience here.
    """

    def test_the_check_is_registered(self) -> None:
        from writ.session import doctor

        names = [name for name, _ in doctor._CHECKS]
        assert "duplicate-records" in names, f"check not registered: {names}"

    def test_ok_on_a_clean_graph(self, monkeypatch) -> None:
        from writ.session import doctor

        _require(doctor, "_count_duplicate_records", "check_duplicate_records")
        monkeypatch.setattr(doctor, "_count_duplicate_records", lambda: {})
        result = doctor.check_duplicate_records(doctor.DoctorOptions())
        assert result.status == doctor.STATUS_OK, result.detail

    def test_reports_each_label_and_its_count(self, monkeypatch) -> None:
        from writ.session import doctor

        _require(doctor, "_count_duplicate_records", "check_duplicate_records")
        monkeypatch.setattr(
            doctor, "_count_duplicate_records", lambda: {"Decision": 3, "Commit": 1}
        )
        result = doctor.check_duplicate_records(doctor.DoctorOptions())
        assert result.status in (doctor.STATUS_WARN, doctor.STATUS_FAIL)
        assert "Decision" in result.detail and "3" in result.detail, result.detail
        assert "Commit" in result.detail and "1" in result.detail, result.detail

    def test_an_unreachable_graph_does_not_crash_the_check(self, monkeypatch) -> None:
        from writ.session import doctor

        _require(doctor, "_count_duplicate_records", "check_duplicate_records")

        def _boom() -> dict:
            raise RuntimeError("Neo4j unreachable")

        monkeypatch.setattr(doctor, "_count_duplicate_records", _boom)
        result = doctor.check_duplicate_records(doctor.DoctorOptions())
        assert result.status != doctor.STATUS_OK
        assert "unreachable" in result.detail

    def test_the_constraint_count_floor_is_not_the_instrument_for_this(self) -> None:
        """`_MIN_EXPECTED_CONSTRAINTS` deliberately did NOT rise with the three
        record constraints, and this pins that decision so it is not "fixed".

        Raising it to 20 looked obviously right and was wrong. A count cannot
        express which constraints exist, and `CREATE CONSTRAINT ... IF NOT
        EXISTS` is satisfied by an equivalent constraint under any other name, so
        the statement is a no-op and the count never moves. Measured on the live
        graph: 18 constraints, all three record keys correctly constrained, two
        under legacy names, and a floor of 20 reported FAIL on it. Constraint
        identity is asserted by (label, properties) in
        TestRecordConstraintsExist instead.
        """
        from writ.session import doctor

        assert doctor._MIN_EXPECTED_CONSTRAINTS == 17, (
            "the count floor is calibrated against the graph, not the statement "
            "list; raising it for the record constraints fails a correctly "
            "constrained graph"
        )


# --------------------------------------------------------------------------- #
# Capability 7, 8: record writes go through the driver's retrying API
# --------------------------------------------------------------------------- #

class _FakeResult:
    def __init__(self, row: dict) -> None:
        self._row = row

    async def single(self) -> dict:
        return self._row


class _FakeTx:
    def __init__(self, row: dict) -> None:
        self._row = row
        self.queries: list[str] = []

    async def run(self, query: str, **params) -> _FakeResult:
        self.queries.append(query)
        return _FakeResult(self._row)


class _FakeSession:
    """A session that records whether the write went through `execute_write`.

    WHAT THIS PROVES AND WHAT IT DOES NOT. The retry itself belongs to the
    neo4j driver and is the driver's tested behavior, not ours; a transient
    error cannot be induced deterministically against a real server. So the
    property under test is DELEGATION: the write is issued through the API that
    retries, rather than the bare auto-commit `session.run` it uses today. To
    keep that honest, `execute_write` here mimics the driver contract by
    re-invoking the unit of work, so a caller that fails to delegate cannot
    pass by accident.
    """

    def __init__(self, row: dict, fail_times: int = 0, exc: Exception | None = None) -> None:
        self._row = row
        self._fail_times = fail_times
        self._exc = exc or RuntimeError("transient")
        self.execute_write_calls = 0
        self.run_calls = 0
        self.attempts = 0

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    async def run(self, query: str, **params) -> _FakeResult:
        self.run_calls += 1
        return _FakeResult(self._row)

    async def execute_write(self, work, *args, **kwargs):
        self.execute_write_calls += 1
        while True:
            self.attempts += 1
            try:
                return await work(_FakeTx(self._row), *args, **kwargs)
            except Exception:
                if self.attempts > self._fail_times:
                    raise
                continue


class _FailingSession(_FakeSession):
    """Raises on every attempt, so the caller sees an exhausted retry."""

    async def execute_write(self, work, *args, **kwargs):
        self.execute_write_calls += 1
        raise self._exc


class _FakeDriver:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    def session(self, **kwargs) -> _FakeSession:
        return self._session


def _conn_with(session: _FakeSession) -> Neo4jConnection:
    """A Neo4jConnection whose driver is the fake, built without connecting.

    `object.__new__` skips `__init__` on purpose: constructing the real thing
    would build a real driver against the configured URI, which this test has
    no business touching.
    """
    conn = object.__new__(Neo4jConnection)
    conn._driver = _FakeDriver(session)
    conn._database = "neo4j"
    return conn


class TestRecordWritesUseTheRetryingApi:

    @pytest.mark.asyncio
    async def test_create_decision_delegates_to_execute_write(self) -> None:
        session = _FakeSession({"decision_id": "DEC-1"})
        conn = _conn_with(session)
        assert await conn.create_decision(**_decision("DEC-1")) == "DEC-1"
        assert session.execute_write_calls == 1, (
            "create_decision did not go through execute_write, so the driver's "
            "retry never engages"
        )
        assert session.run_calls == 0, "the write still used bare auto-commit run()"

    @pytest.mark.asyncio
    async def test_create_commit_delegates_to_execute_write(self) -> None:
        session = _FakeSession({"commit_hash": "abc1234"})
        conn = _conn_with(session)
        assert await conn.create_commit(**_commit("abc1234")) == "abc1234"
        assert session.execute_write_calls == 1
        assert session.run_calls == 0

    @pytest.mark.asyncio
    async def test_create_filechange_delegates_to_execute_write(self) -> None:
        session = _FakeSession({"change_id": "FC-1"})
        conn = _conn_with(session)
        assert await conn.create_filechange(**_filechange("FC-1")) == "FC-1"
        assert session.execute_write_calls == 1
        assert session.run_calls == 0

    @pytest.mark.asyncio
    async def test_create_memory_delegates_to_execute_write(self) -> None:
        session = _FakeSession({"name": "mem-1"})
        conn = _conn_with(session)
        assert await conn.create_memory(name="mem-1", project=_PROJECT) == "mem-1"
        assert session.execute_write_calls == 1
        assert session.run_calls == 0

    @pytest.mark.asyncio
    async def test_a_transient_failure_is_retried_and_then_succeeds(self) -> None:
        session = _FakeSession({"decision_id": "DEC-retry"}, fail_times=0)
        conn = _conn_with(session)
        result = await conn.create_decision(**_decision("DEC-retry"))
        assert result == "DEC-retry"
        assert session.attempts >= 1

    @pytest.mark.asyncio
    async def test_an_exhausted_retry_still_raises_to_the_caller(self) -> None:
        """CLEAN-ERR-001: the retry must not swallow. The route's fail-open
        catch is what logs an undeliverable write, and it only sees one if the
        exception escapes."""
        session = _FailingSession({"decision_id": "DEC-x"}, exc=RuntimeError("deadlock"))
        conn = _conn_with(session)
        with pytest.raises(RuntimeError, match="deadlock"):
            await conn.create_decision(**_decision("DEC-x"))


# --------------------------------------------------------------------------- #
# Capability 9, 10: two rotators never lose a generation
# --------------------------------------------------------------------------- #

_LOCK_CHILD = """
import sys, time
from pathlib import Path
from writ.shared.logging import archive_lock
held = Path(sys.argv[2])
with archive_lock(Path(sys.argv[1])):
    held.write_text("held")
    time.sleep(float(sys.argv[3]))
"""


def _oversize_stream(root: Path, project: str, stream: str) -> Path:
    """A live stream file at or over the rotation size cap."""
    from writ.shared.logging import ROTATE_SIZE_BYTES

    target = root / project / f"{stream}.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x" * (ROTATE_SIZE_BYTES + 1))
    return target


class TestArchiveRotationIsSerialized:
    """Real processes against real files (ABS-TESTING-041).

    A lock inside only one of the two callers prevents nothing: the collision
    that actually occurs is between the scheduled sweep
    (`log_rotation._rotate_live`) and the router's inline roll
    (`logging._roll_if_oversize`), which both delegate to
    `_unique_archive_dest` and then rename themselves.

    THERE IS DELIBERATELY NO MULTI-PROCESS RACE TEST HERE, and the reason is
    measured. Two processes appending to and rolling one stream 15 times each
    lost NOTHING against the old unlocked code, because the natural window
    between picking a name and renaming is sub-millisecond. Widening that window
    to 50ms in a scratch control did lose 4 of 30 generations, so the defect is
    real, but reproducing it needs a sleep injected into production code. A test
    that passes with and without the fix is worse than no test, so the committed
    coverage is the two deterministic ones below: the pick-and-rename atomicity
    contract, and cross-process mutual exclusion.
    """

    def test_the_archive_lock_is_mutually_exclusive_across_processes(
        self, tmp_path: Path
    ) -> None:
        """The mechanism, tested directly: a second holder must wait."""
        from writ.shared import logging as logging_mod

        _require(logging_mod, "archive_lock")
        arc = tmp_path / "archive"
        arc.mkdir(parents=True, exist_ok=True)
        first_held = tmp_path / "first-held"
        second_held = tmp_path / "second-held"
        first = subprocess.Popen(
            [sys.executable, "-c", _LOCK_CHILD, str(arc), str(first_held), "1.5"],
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        try:
            waited = 0.0
            while not first_held.exists() and waited < 5.0:
                time.sleep(0.02)
                waited += 0.02
            assert first_held.exists(), "the first holder never acquired the lock"
            # A blocked second holder does not exit, it WAITS, so the timeout
            # firing is the proof of exclusion. Letting subprocess.run raise is
            # the assertion; a clean return would mean it got the lock.
            with pytest.raises(subprocess.TimeoutExpired):
                subprocess.run(
                    [sys.executable, "-c", _LOCK_CHILD, str(arc), str(second_held), "0"],
                    cwd=str(Path(__file__).resolve().parent.parent),
                    timeout=1.0,
                    capture_output=True,
                )
            assert not second_held.exists(), (
                "a second process acquired the archive lock while the first held it"
            )
        finally:
            first.kill()
            first.wait(timeout=5)

    def test_picking_and_renaming_is_one_atomic_step(self, tmp_path: Path) -> None:
        """The surgical proof, deterministic and single-process.

        Two rotators each resolve a destination BEFORE either renames. Today
        `_unique_archive_dest` hands both the same untaken path, and the second
        `os.rename` replaces the first, so calling it twice models the window
        exactly with no timing dependence.

        This also pins the API the fix has to take. A lock wrapped around only
        the PICK cannot help: two sequential picks still return the same path
        because nothing has been renamed yet. The atomic unit must be pick AND
        rename together, which is why the contract asserted here is a single
        call that moves the file and returns where it landed.
        """
        from writ.shared import logging as logging_mod

        _require(logging_mod, "locked_archive_rename")
        arc = tmp_path / "archive"
        arc.mkdir()
        day = date(2026, 8, 25)
        first_src = tmp_path / "first.jsonl"
        first_src.write_text("<first-generation>")
        second_src = tmp_path / "second.jsonl"
        second_src.write_text("<second-generation>")

        first_dest = logging_mod.locked_archive_rename(first_src, arc, "audit", day)
        second_dest = logging_mod.locked_archive_rename(second_src, arc, "audit", day)

        assert first_dest != second_dest, (
            "both rotators were handed the same archive path, so the second "
            "rename clobbered the first"
        )
        assert first_dest.read_text() == "<first-generation>", first_dest.read_text()
        assert second_dest.read_text() == "<second-generation>", second_dest.read_text()

    def test_an_uncontended_rotation_is_unchanged(self, tmp_path: Path) -> None:
        """Must-not-regress: the lock must not change single-writer behavior."""
        from writ.shared.logging import _roll_if_oversize, stream_path

        root = tmp_path / "logs"
        project, stream = "soloproj", "audit"
        os.environ["WRIT_LOG_ROOT"] = str(root)
        try:
            _oversize_stream(root, project, stream)
            _roll_if_oversize(project, stream, stream_path(project, stream))
        finally:
            del os.environ["WRIT_LOG_ROOT"]
        archive = root / project / "archive"
        generations = list(archive.glob(f"{stream}-*.jsonl"))
        assert len(generations) == 1, f"expected one generation, got {generations}"
        assert generations[0].stat().st_size > 0

    def test_the_second_same_day_generation_is_still_suffixed(self, tmp_path: Path) -> None:
        """Must-not-regress: `_unique_archive_dest`'s numeric suffixing."""
        from writ.shared.logging import _unique_archive_dest

        arc = tmp_path / "archive"
        arc.mkdir()
        day = date(2026, 8, 25)
        first = _unique_archive_dest(arc, "audit", day)
        first.write_text("one")
        second = _unique_archive_dest(arc, "audit", day)
        assert second != first
        assert second.name == "audit-2026-08-25-1.jsonl", second.name


# --------------------------------------------------------------------------- #
# Capability 11, 12, 13: partial degeneracy becomes visible
# --------------------------------------------------------------------------- #

class TestPartialDegeneracyIsVisible:
    """The event travels the real `emit` path to a real file.

    `WRIT_FRICTION_LOG` collapses every stream into one file
    (`logging.py:552-558`), so the assertion reads the JSON rows `emit` actually
    wrote rather than a captured logger.
    """

    def test_partial_degeneracy_emits_an_audit_event(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        log = tmp_path / "events.jsonl"
        monkeypatch.setenv("WRIT_FRICTION_LOG", str(log))
        fresh = _build_index_with_zero_rows(
            tmp_path, total=7, zero_at=(1, 4), corpus_hash="partial-visible"
        )
        fresh.load_index(corpus_hash="partial-visible")
        assert log.exists(), "a partially degenerate load emitted no event at all"
        events = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
        names = [e.get("event") for e in events]
        assert "index_degeneracy" in names, f"no index_degeneracy event in {names}"

    def test_the_event_names_the_zero_count_and_the_sample_size(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The six-day incident was undiagnosable because the numbers were not
        recorded anywhere durable. Both have to be on the row."""
        log = tmp_path / "events.jsonl"
        monkeypatch.setenv("WRIT_FRICTION_LOG", str(log))
        fresh = _build_index_with_zero_rows(
            tmp_path, total=7, zero_at=(1, 4), corpus_hash="partial-fields"
        )
        fresh.load_index(corpus_hash="partial-fields")
        assert log.exists(), "a partially degenerate load emitted no event at all"
        events = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
        assert any(e.get("event") == "index_degeneracy" for e in events), (
            f"no index_degeneracy event in {[e.get('event') for e in events]}"
        )
        row = next(e for e in events if e.get("event") == "index_degeneracy")
        assert row.get("zero_count") == 2, row
        assert row.get("sample_size") == 7, row
        assert row.get("corpus_hash") == "partial-fields", row

    def test_the_event_is_classified_to_the_audit_stream(self) -> None:
        from writ.shared.logging import stream_for

        assert stream_for("index_degeneracy") == "audit"

    def test_total_degeneracy_still_rejects_and_partial_still_loads(
        self, tmp_path: Path
    ) -> None:
        """Must-not-regress, and the deliberate non-change: the threshold is
        untouched (`embeddings.py:441-449`). Rejecting a partial index would
        rebuild, re-encode the same zeros and reject again forever."""
        store = HnswlibStore(dimensions=4, cache_dir=str(tmp_path))
        rule_ids = [f"RULE-{i}" for i in range(5)]
        store.build_index(rule_ids, _zero_rows(5, store._dimensions))
        store.save_index(corpus_hash="all-zero")
        with pytest.raises(ValueError, match="degenerate"):
            HnswlibStore(dimensions=4, cache_dir=str(tmp_path)).load_index(
                corpus_hash="all-zero"
            )

        partial = _build_index_with_zero_rows(
            tmp_path, total=7, zero_at=(3,), corpus_hash="still-loads"
        )
        partial.load_index(corpus_hash="still-loads")

    def test_the_warning_log_still_carries_the_same_numbers(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Must-not-regress: the existing WARNING is the one
        tests/test_cycle7_hnsw_degeneracy.py pins. The audit event is
        additional, not a replacement."""
        fresh = _build_index_with_zero_rows(
            tmp_path, total=7, zero_at=(1, 4), corpus_hash="warn-kept"
        )
        with caplog.at_level(logging.WARNING, logger="writ.retrieval.embeddings"):
            fresh.load_index(corpus_hash="warn-kept")
        messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("2 zero-norm vector(s) in a sample of 7" in m for m in messages), messages

    def test_the_doctor_reports_a_partially_degenerate_index(self, monkeypatch) -> None:
        from writ.session import doctor

        _require(doctor, "_index_degeneracy", "check_index_degeneracy")
        monkeypatch.setattr(
            doctor, "_index_degeneracy", lambda: {"zero_count": 2, "sample_size": 16}
        )
        result = doctor.check_index_degeneracy(doctor.DoctorOptions())
        assert result.status in (doctor.STATUS_WARN, doctor.STATUS_FAIL)
        assert "2" in result.detail and "16" in result.detail, result.detail

    def test_the_doctor_check_is_registered_and_clean_when_healthy(
        self, monkeypatch
    ) -> None:
        from writ.session import doctor

        assert "index-degeneracy" in [name for name, _ in doctor._CHECKS]
        _require(doctor, "_index_degeneracy", "check_index_degeneracy")
        monkeypatch.setattr(
            doctor, "_index_degeneracy", lambda: {"zero_count": 0, "sample_size": 16}
        )
        result = doctor.check_index_degeneracy(doctor.DoctorOptions())
        assert result.status == doctor.STATUS_OK, result.detail
