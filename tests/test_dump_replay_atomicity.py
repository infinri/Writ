"""The corpus dump replay must be ONE transaction, not 1714 of them.

THE DEFECT. `import_cypher_dump` (writ/graph/dump.py:104-107) deletes the whole
corpus and then replays the dump one line at a time:

    statements = [line for line in text.splitlines() if line.strip()]
    for statement in statements:
        await db.execute(statement)

`db.execute` opens its own session per call, so a 1714-statement dump is a mass
delete followed by 1714 independent transactions that reuse freed node ids. That
is the shape behind the long-standing full-suite flake:

    neo4j.exceptions.ClientError: {code: Neo.ClientError.Statement.EntityNotFound}
    {message: Unable to load NODE 4:ce424234-...:856.}

An earlier cycle already chased this error and fixed the fire-and-forget half,
adding `consume()` to both `clear_all` and `execute` (see the comment at
maintenance_store.py:49-56, which names EntityNotFound explicitly). That made
each statement FINISH before the next began and left the 1714-session structure
untouched, so it narrowed the window instead of removing it. Asking why there
are 1714 transactions at all is the part that was skipped.

Two independent reasons this is the right change even if the flake proves to have
another cause: a replay that fails halfway currently leaves the graph
half-populated with no way back, and 1714 round trips is 1714 round trips.
"""
from __future__ import annotations

import pytest

from writ.graph.db.maintenance_store import MaintenanceStoreMixin


# --- recording doubles: count SESSIONS, which is the property under test ------

class _FakeResult:
    async def consume(self) -> None:
        return None


class _FakeTx:
    def __init__(self, log: list[str]) -> None:
        self._log = log
        self.committed = False
        self.rolled_back = False

    async def run(self, statement: str, **params):
        if "BOOM" in statement:
            raise RuntimeError("statement failed mid-batch")
        self._log.append(statement)
        return _FakeResult()

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


class _FakeSession:
    def __init__(self, owner: _Recorder) -> None:
        self._owner = owner

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def run(self, statement: str, **params):
        self._owner.statements.append(statement)
        return _FakeResult()

    async def begin_transaction(self) -> _FakeTx:
        """A COROUTINE returning the transaction, matching neo4j's async driver.

        The first version of this double returned a context manager directly and
        synchronously. All four tests passed against it while the production code
        raised `TypeError: 'coroutine' object does not support the asynchronous
        context manager protocol` on the very first real replay. A double that
        misstates the API under test only proves the double agrees with itself,
        which is why this signature is now pinned by
        test_the_double_matches_the_real_driver_signature below.
        """
        tx = _FakeTx(self._owner.statements)
        self._owner.transactions.append(tx)
        return tx


class _FakeDriver:
    def __init__(self, owner: _Recorder) -> None:
        self._owner = owner

    def session(self, database: str | None = None) -> _FakeSession:
        self._owner.sessions_opened += 1
        return _FakeSession(self._owner)


class _Recorder(MaintenanceStoreMixin):
    """The REAL mixin over a driver that counts sessions and transactions.

    Subclassing the production mixin rather than reimplementing it is what makes
    this worth anything: the shipped code path is the one under test.
    """

    def __init__(self) -> None:
        self.sessions_opened = 0
        self.statements: list[str] = []
        self.transactions: list[_FakeTx] = []
        self._driver = _FakeDriver(self)
        self._database = "neo4j"
        self._uri = "bolt://localhost:7688"


# --- the contract -------------------------------------------------------------

class TestExecuteManyIsOneUnit:
    """[cycle 9] execute_many replaces the per-statement session loop."""

    @pytest.mark.asyncio
    async def test_all_statements_run_in_a_single_session(self) -> None:
        rec = _Recorder()
        stmts = [f"CREATE (:Rule {{rule_id: 'R-{i}'}});" for i in range(50)]

        await rec.execute_many(stmts)

        assert rec.statements == stmts, "every statement must run, in order"
        assert rec.sessions_opened == 1, (
            "50 statements must cost ONE session, not 50. The per-statement "
            f"session loop is the defect; got {rec.sessions_opened} sessions"
        )

    @pytest.mark.asyncio
    async def test_a_failure_mid_batch_applies_nothing(self) -> None:
        """Atomicity, and the reason this matters beyond the flake: a replay
        that dies halfway currently leaves a half-populated corpus with no way
        back, which is worse than either succeeding or failing cleanly."""
        rec = _Recorder()
        stmts = [
            "CREATE (:Rule {rule_id: 'R-1'});",
            "CREATE (:Rule {rule_id: 'BOOM'});",
            "CREATE (:Rule {rule_id: 'R-3'});",
        ]

        with pytest.raises(RuntimeError):
            await rec.execute_many(stmts)

        assert rec.transactions, "execute_many must use an explicit transaction"
        tx = rec.transactions[0]
        assert not tx.committed, "a failed batch must not commit"
        assert "CREATE (:Rule {rule_id: 'R-3'});" not in rec.statements, (
            "statements after the failure must not run"
        )

    @pytest.mark.asyncio
    async def test_empty_batch_opens_nothing(self) -> None:
        rec = _Recorder()
        await rec.execute_many([])
        assert rec.sessions_opened == 0
        assert rec.statements == []


class TestTheDoubleMatchesTheRealDriver:
    """The guard that was missing, written after the double got it wrong.

    Every test above runs against `_FakeSession`. If the double's API drifts from
    neo4j's, they all keep passing while production breaks on the first real
    call, which is precisely what happened: `begin_transaction` was written
    synchronously here, four tests went green, and the real driver raised
    TypeError on the first replay.
    """

    def test_the_double_matches_the_real_driver_signature(self) -> None:
        import inspect

        from neo4j import AsyncSession

        real = AsyncSession.begin_transaction
        fake = _FakeSession.begin_transaction

        assert inspect.iscoroutinefunction(real), (
            "neo4j changed begin_transaction to something other than a "
            "coroutine function; the double and the production call site both "
            "need revisiting"
        )
        assert inspect.iscoroutinefunction(fake) == inspect.iscoroutinefunction(real), (
            "the double's begin_transaction must be a coroutine function exactly "
            "when the real one is, or every test in this file proves only that "
            "the double agrees with itself"
        )


class TestImportCypherDumpUsesTheBatch:
    """The structural pin. Without it the helper could exist while
    import_cypher_dump kept its own loop, which is the shape where a fix ships
    and changes nothing."""

    def test_dump_does_not_execute_one_statement_at_a_time(self) -> None:
        import inspect

        from writ.graph import dump

        src = inspect.getsource(dump.import_cypher_dump)
        assert "execute_many" in src, (
            "import_cypher_dump must replay through execute_many"
        )
        assert "await db.execute(statement)" not in src, (
            "the per-statement session loop is still there; that is the defect"
        )
