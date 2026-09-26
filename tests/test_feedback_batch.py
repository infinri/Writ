"""Plan f7fc2b37-9a53-4011-a69f-e6b97f5e45fe, batch 4 (3b): one batched write for
SessionEnd rule feedback.

`RuleStoreMixin.apply_feedback_batch` (writ/graph/db/rule_store.py) does not exist at
HEAD: every test below either raises `AttributeError` calling it directly, or gets a 404
through the ASGI app for `POST /feedback/batch`. RED by construction (ENF-PROC-TDD-001).

Per ENF-SYS-005: the counting/atomicity/concurrency claims (one session, one
execute_write, one-flip-under-a-race, all-or-nothing rollback) are proven against the
ISOLATED TEST NEO4J instance (bolt://localhost:7688, `make test-graph-up
PYTHON=$(command -v python3)`), never mocked -- a mocked driver would only replay
whatever the test wrote to it. `tests/_graph.connection()` is the one path to that
instance (isolation forced by tests/conftest.py at import time); every test here skips
(never fails, never falls back to production) when it is unreachable.

The route contract (validation, empty-list no-db-call, no-db-connection) needs no
graph and is tested through the real ASGI app with a stubbed `server._db`.
"""
from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from tests._graph import connection

LOW_T = 4  # a low graduation threshold keeps the batch tests fast (real default is 50)


def _rule(rid: str) -> dict:
    return {
        "rule_id": rid, "domain": "Testing", "severity": "high", "scope": "slice",
        "trigger": "t", "statement": "s", "violation": "v", "pass_example": "p",
        "enforcement": "e", "rationale": "r", "last_validated": "2026-03-15",
        "authority": "ai-provisional",
    }


@pytest_asyncio.fixture()
async def db():
    conn = connection()
    try:
        async with conn._driver.session(database=conn._database) as s:
            await (await s.run("RETURN 1 AS ok")).consume()
    except Exception:
        await conn.close()
        pytest.skip("isolated test Neo4j (bolt://localhost:7688) unreachable; run "
                     "`make test-graph-up PYTHON=$(command -v python3)`")
    await conn.clear_all()
    yield conn
    await conn.clear_all()
    await conn.close()


async def _seed_rule(db, rid: str, *, source_origin: str = "graph-authored",
                     pos: int = 0, neg: int = 0) -> None:
    await db.create_rule(_rule(rid), source_origin=source_origin)
    async with db._driver.session(database=db._database) as s:
        await s.run(
            "MATCH (r:Rule {rule_id: $id}) SET r.times_seen_positive=$p, "
            "r.times_seen_negative=$n", id=rid, p=pos, n=neg,
        )


async def _rule_row(db, rid: str) -> dict | None:
    async with db._driver.session(database=db._database) as s:
        r = await s.run(
            "MATCH (r:Rule {rule_id: $id}) RETURN r.times_seen_positive AS pos, "
            "r.times_seen_negative AS neg, r.provenance AS provenance, "
            "r.last_seen AS last_seen", id=rid,
        )
        rec = await r.single()
        return dict(rec) if rec else None


def _require(db, name: str) -> None:
    if not hasattr(db, name):
        pytest.fail(f"skeleton: RuleStoreMixin has no {name} yet (writ/graph/db/rule_store.py)")


class _SessionCountingDriverProxy:
    """Wraps `db._driver.session` to count sessions opened, auto-commit `session.run`
    calls, `execute_write` calls, and `tx.run` calls made INSIDE an execute_write
    transaction function. Real driver underneath -- only the counting is added."""

    def __init__(self, real_session_method) -> None:
        self._real = real_session_method
        self.sessions_opened = 0
        self.session_run = 0
        self.execute_write_calls = 0
        self.tx_run = 0

    def __call__(self, *args, **kwargs):
        self.sessions_opened += 1
        return _CountingSession(self._real(*args, **kwargs), self)


class _CountingSession:
    def __init__(self, real_session, counters: _SessionCountingDriverProxy) -> None:
        self._real = real_session
        self._counters = counters

    async def __aenter__(self):
        self._session = await self._real.__aenter__()
        return self

    async def __aexit__(self, *exc):
        return await self._real.__aexit__(*exc)

    async def run(self, *a, **k):
        self._counters.session_run += 1
        return await self._session.run(*a, **k)

    async def execute_write(self, fn, *a, **k):
        self._counters.execute_write_calls += 1
        counters = self._counters

        async def _wrapped(tx):
            return await fn(_CountingTx(tx, counters), *a, **k)

        return await self._session.execute_write(_wrapped)

    def __getattr__(self, name):
        return getattr(self._session, name)


class _CountingTx:
    def __init__(self, tx, counters: _SessionCountingDriverProxy) -> None:
        self._tx = tx
        self._counters = counters

    async def run(self, *a, **k):
        self._counters.tx_run += 1
        return await self._tx.run(*a, **k)

    def __getattr__(self, name):
        return getattr(self._tx, name)


def _install_counters(db) -> _SessionCountingDriverProxy:
    proxy = _SessionCountingDriverProxy(db._driver.session)
    db._driver.session = proxy
    return proxy


# --------------------------------------------------------------------------------- #
# Capability: increments, last_seen, not_found -- never touching other rules.
# --------------------------------------------------------------------------------- #


class TestApplyFeedbackBatchIncrements:
    @pytest.mark.asyncio
    async def test_increments_positive_and_negative_by_exact_signal_counts(self, db) -> None:
        _require(db, "apply_feedback_batch")
        await _seed_rule(db, "FB-BATCH-001", pos=0, neg=0)
        await _seed_rule(db, "FB-BATCH-002", pos=5, neg=1)
        result = await db.apply_feedback_batch([
            ("FB-BATCH-001", "positive"), ("FB-BATCH-001", "positive"),
            ("FB-BATCH-002", "negative"),
        ])
        row1 = await _rule_row(db, "FB-BATCH-001")
        row2 = await _rule_row(db, "FB-BATCH-002")
        assert row1["pos"] == 2 and row1["neg"] == 0
        assert row2["pos"] == 5 and row2["neg"] == 2
        assert row1["last_seen"] is not None
        assert "FB-BATCH-001" in result["recorded"]
        assert "FB-BATCH-002" in result["recorded"]

    @pytest.mark.asyncio
    async def test_absent_ids_report_not_found_and_touch_nothing(self, db) -> None:
        _require(db, "apply_feedback_batch")
        await _seed_rule(db, "FB-BATCH-EXIST", pos=1, neg=0)
        result = await db.apply_feedback_batch([
            ("FB-BATCH-EXIST", "positive"), ("FB-BATCH-MISSING", "positive"),
        ])
        assert result["not_found"] == ["FB-BATCH-MISSING"]
        row = await _rule_row(db, "FB-BATCH-EXIST")
        assert row["pos"] == 2


# --------------------------------------------------------------------------------- #
# Capability: exact session/transaction/query counts.
# --------------------------------------------------------------------------------- #


class TestBatchCounts:
    @pytest.mark.asyncio
    async def test_five_rules_none_crossing_is_one_session_one_transaction_one_query(
        self, db
    ) -> None:
        _require(db, "apply_feedback_batch")
        rule_ids = [f"FB-BATCH-CNT-{i}" for i in range(5)]
        for rid in rule_ids:
            await _seed_rule(db, rid, pos=0, neg=0)  # far below LOW_T
        counters = _install_counters(db)
        await db.apply_feedback_batch(
            [(rid, "positive") for rid in rule_ids], threshold=LOW_T,
        )
        assert counters.sessions_opened == 1
        assert counters.execute_write_calls == 1
        assert counters.session_run == 0, (
            "no auto-commit session.run call: the batch must run entirely inside "
            "the one execute_write transaction"
        )
        assert counters.tx_run == 1

    @pytest.mark.asyncio
    async def test_when_one_rule_crosses_the_transaction_runs_exactly_two_queries(
        self, db
    ) -> None:
        _require(db, "apply_feedback_batch")
        await _seed_rule(db, "FB-BATCH-CROSS", pos=LOW_T - 1, neg=0)
        await _seed_rule(db, "FB-BATCH-STAY", pos=0, neg=0)
        counters = _install_counters(db)
        await db.apply_feedback_batch(
            [("FB-BATCH-CROSS", "positive"), ("FB-BATCH-STAY", "positive")],
            threshold=LOW_T,
        )
        assert counters.sessions_opened == 1
        assert counters.execute_write_calls == 1
        assert counters.tx_run == 2


# --------------------------------------------------------------------------------- #
# Capability: mixed set flips exactly what the sequential path flips.
# --------------------------------------------------------------------------------- #


class TestMixedSetFlipsMatchSequentialPath:
    @pytest.mark.asyncio
    async def test_flips_match_increment_then_evaluate_and_flip_graduation(self, db) -> None:
        _require(db, "apply_feedback_batch")
        await _seed_rule(db, "FB-MIX-CROSS", pos=LOW_T - 1, neg=0)         # proposed, crosses
        await _seed_rule(db, "FB-MIX-LOW", pos=0, neg=0)                   # proposed, below threshold
        await _seed_rule(db, "FB-MIX-RATIO", pos=0, neg=LOW_T - 1)         # proposed, low ratio
        await _seed_rule(db, "FB-MIX-HAND", pos=LOW_T - 1, neg=0,
                          source_origin="ingest")                          # hand-authored, crosses
        for rid in ("FB-MIX-CROSS", "FB-MIX-LOW", "FB-MIX-RATIO", "FB-MIX-HAND"):
            await db.increment_positive(rid)
            expected = await db.evaluate_and_flip_graduation(rid, threshold=LOW_T)
            if rid == "FB-MIX-CROSS":
                assert expected == "graduation_pending"
            else:
                assert expected is None

        expected_provenance = {
            rid: (await _rule_row(db, rid))["provenance"]
            for rid in ("FB-MIX-CROSS", "FB-MIX-LOW", "FB-MIX-RATIO", "FB-MIX-HAND")
        }
        expected_authority = {
            rid: (await db.get_rule(rid) or {}).get("authority")
            for rid in expected_provenance
        }

        # Reset and drive the SAME inputs through the batch path instead.
        await db.clear_all()
        await _seed_rule(db, "FB-MIX-CROSS", pos=LOW_T - 1, neg=0)
        await _seed_rule(db, "FB-MIX-LOW", pos=0, neg=0)
        await _seed_rule(db, "FB-MIX-RATIO", pos=0, neg=LOW_T - 1)
        await _seed_rule(db, "FB-MIX-HAND", pos=LOW_T - 1, neg=0, source_origin="ingest")
        result = await db.apply_feedback_batch(
            [(rid, "positive") for rid in
             ("FB-MIX-CROSS", "FB-MIX-LOW", "FB-MIX-RATIO", "FB-MIX-HAND")],
            threshold=LOW_T,
        )
        assert result["graduation_pending"] == ["FB-MIX-CROSS"]
        for rid, prov in expected_provenance.items():
            row = await _rule_row(db, rid)
            assert row["provenance"] == prov, f"{rid}: {row['provenance']} != {prov}"
        for rid, authority in expected_authority.items():
            got = (await db.get_rule(rid) or {}).get("authority")
            assert got == authority, f"{rid}: authority changed ({authority} -> {got})"


# --------------------------------------------------------------------------------- #
# Capability: two concurrent crossings, exactly one flip reported.
# --------------------------------------------------------------------------------- #


class TestConcurrentCrossingFlipsExactlyOnce:
    @pytest.mark.asyncio
    async def test_both_batches_increment_only_one_reports_the_flip(self, db) -> None:
        _require(db, "apply_feedback_batch")
        await _seed_rule(db, "FB-RACE-001", pos=LOW_T - 1, neg=0)

        results = await asyncio.gather(
            db.apply_feedback_batch([("FB-RACE-001", "positive")], threshold=LOW_T),
            db.apply_feedback_batch([("FB-RACE-001", "positive")], threshold=LOW_T),
        )
        flips = [r for r in results if "FB-RACE-001" in r.get("graduation_pending", [])]
        assert len(flips) == 1, f"expected exactly one flip, got {results}"
        row = await _rule_row(db, "FB-RACE-001")
        assert row["pos"] == LOW_T + 1, "both increments must be recorded"
        assert row["provenance"] == "graduation_pending"


# --------------------------------------------------------------------------------- #
# Capability: a failure inside the transaction rolls back everything.
# --------------------------------------------------------------------------------- #


class TestFailureInsideTransactionRollsBackEverything:
    @pytest.mark.asyncio
    async def test_a_raising_evaluate_graduation_leaves_every_rule_unchanged(
        self, db, monkeypatch
    ) -> None:
        _require(db, "apply_feedback_batch")
        await _seed_rule(db, "FB-FAIL-001", pos=LOW_T - 1, neg=0)
        await _seed_rule(db, "FB-FAIL-002", pos=0, neg=0)

        import writ.frequency as frequency

        def _boom(*_a, **_k):
            raise RuntimeError("evaluate_graduation exploded mid-transaction")

        monkeypatch.setattr(frequency, "evaluate_graduation", _boom)
        # writ/graph/db/rule_store.py imports evaluate_graduation by name; patch both
        # the origin and (best-effort) the imported binding so the skeleton failure
        # is legible either way once the import exists.
        try:
            import writ.graph.db.rule_store as rule_store
            monkeypatch.setattr(rule_store, "evaluate_graduation", _boom)
        except (ImportError, AttributeError):
            pass

        with pytest.raises(Exception):
            await db.apply_feedback_batch(
                [("FB-FAIL-001", "positive"), ("FB-FAIL-002", "positive")], threshold=LOW_T,
            )
        row1 = await _rule_row(db, "FB-FAIL-001")
        row2 = await _rule_row(db, "FB-FAIL-002")
        assert row1["pos"] == LOW_T - 1 and row1["provenance"] == "proposed"
        assert row2["pos"] == 0


# --------------------------------------------------------------------------------- #
# Plan f7fc2b37-9a53-4011-a69f-e6b97f5e45fe, item B: a content-addressed batch_id,
# recorded in the SAME transaction as its effects, makes a resent or concurrently
# duplicated batch apply once. RED at HEAD: `apply_feedback_batch` has no `batch_id`
# parameter at all, so every call below raises TypeError until it is added.
# --------------------------------------------------------------------------------- #


class TestBatchIdReplayAppliesOnce:
    @pytest.mark.asyncio
    async def test_same_batch_id_twice_moves_counts_once_and_the_second_answer_replays(
        self, db
    ) -> None:
        _require(db, "apply_feedback_batch")
        await _seed_rule(db, "FB-REPLAY-001", pos=0, neg=0)
        batch_id = "1" * 64
        first = await db.apply_feedback_batch(
            [("FB-REPLAY-001", "positive")], batch_id=batch_id)
        second = await db.apply_feedback_batch(
            [("FB-REPLAY-001", "positive")], batch_id=batch_id)
        row = await _rule_row(db, "FB-REPLAY-001")
        assert row["pos"] == 1, "the second (replayed) call must not apply a second increment"
        assert second.get("replayed") is True, second
        expected = dict(first)
        expected["replayed"] = True
        assert second == expected, (second, first)


class TestSameContentUnderADifferentBatchIdAppliesAgain:
    @pytest.mark.asyncio
    async def test_a_different_batch_id_applies_a_second_time(self, db) -> None:
        _require(db, "apply_feedback_batch")
        await _seed_rule(db, "FB-DIFFID-001", pos=0, neg=0)
        await db.apply_feedback_batch([("FB-DIFFID-001", "positive")], batch_id="2" * 64)
        await db.apply_feedback_batch([("FB-DIFFID-001", "positive")], batch_id="3" * 64)
        row = await _rule_row(db, "FB-DIFFID-001")
        assert row["pos"] == 2, "identical content under a different batch_id is not a replay"


class TestConcurrentCallsWithOneBatchIdApplyOnce:
    @pytest.mark.asyncio
    async def test_two_concurrent_calls_move_counts_once_and_exactly_one_replays(
        self, db
    ) -> None:
        _require(db, "apply_feedback_batch")
        await _seed_rule(db, "FB-CONC-BID-001", pos=0, neg=0)
        batch_id = "4" * 64
        results = await asyncio.gather(
            db.apply_feedback_batch([("FB-CONC-BID-001", "positive")], batch_id=batch_id),
            db.apply_feedback_batch([("FB-CONC-BID-001", "positive")], batch_id=batch_id),
        )
        row = await _rule_row(db, "FB-CONC-BID-001")
        assert row["pos"] == 1, "the increment must move exactly once across both calls"
        replayed_flags = [r.get("replayed", False) for r in results]
        assert replayed_flags.count(True) == 1, f"expected exactly one replay: {results}"


class TestFailureInsideTheBatchTransactionLeavesNoFeedbackBatchRecord:
    @pytest.mark.asyncio
    async def test_a_raising_transaction_leaves_no_record_and_a_retry_applies(
        self, db, monkeypatch
    ) -> None:
        _require(db, "apply_feedback_batch")
        await _seed_rule(db, "FB-BATCHFAIL-001", pos=LOW_T - 1, neg=0)

        import writ.frequency as frequency

        def _boom(*_a, **_k):
            raise RuntimeError("evaluate_graduation exploded mid-transaction")

        monkeypatch.setattr(frequency, "evaluate_graduation", _boom)
        try:
            import writ.graph.db.rule_store as rule_store
            monkeypatch.setattr(rule_store, "evaluate_graduation", _boom)
        except (ImportError, AttributeError):
            pass

        batch_id = "5" * 64
        with pytest.raises(Exception):
            await db.apply_feedback_batch(
                [("FB-BATCHFAIL-001", "positive")], threshold=LOW_T, batch_id=batch_id)

        row = await _rule_row(db, "FB-BATCHFAIL-001")
        assert row["pos"] == LOW_T - 1, "the failed attempt must apply nothing"

        async with db._driver.session(database=db._database) as s:
            result = await s.run(
                "MATCH (b:FeedbackBatch {batch_id: $id}) RETURN count(b) AS n", id=batch_id)
            rec = await result.single()
            assert rec["n"] == 0, (
                "a rolled-back attempt must leave no FeedbackBatch record: 'record exists' "
                "means 'applied' and vice versa"
            )

        monkeypatch.undo()  # restore the real evaluate_graduation for the retry
        second = await db.apply_feedback_batch(
            [("FB-BATCHFAIL-001", "positive")], threshold=LOW_T, batch_id=batch_id)
        row = await _rule_row(db, "FB-BATCHFAIL-001")
        assert row["pos"] == LOW_T, (
            "a retry with the same id must apply, since the failed attempt left no record"
        )
        assert second.get("replayed") is not True


class TestPruningIsBoundedAndAgeBased:
    @pytest.mark.asyncio
    async def test_applying_a_new_batch_prunes_expired_records_bounded_by_the_limit(
        self, db
    ) -> None:
        _require(db, "apply_feedback_batch")
        from writ.graph.db import rule_store
        if not hasattr(rule_store, "FEEDBACK_BATCH_PRUNE_LIMIT"):
            pytest.fail(
                "skeleton: writ.graph.db.rule_store has no FEEDBACK_BATCH_PRUNE_LIMIT yet"
            )
        limit = rule_store.FEEDBACK_BATCH_PRUNE_LIMIT
        await _seed_rule(db, "FB-PRUNE-TARGET", pos=0, neg=0)

        expired_ids = [f"expired-{i:03d}" for i in range(6)]
        async with db._driver.session(database=db._database) as s:
            for eid in expired_ids:
                await s.run(
                    "MERGE (b:FeedbackBatch {batch_id: $id}) "
                    "SET b.created_at = datetime() - duration({days: 31}), b.result = '{}'",
                    id=eid,
                )
            await s.run(
                "MERGE (b:FeedbackBatch {batch_id: 'young-record'}) "
                "SET b.created_at = datetime() - duration({days: 1}), b.result = '{}'",
            )

        await db.apply_feedback_batch([("FB-PRUNE-TARGET", "positive")], batch_id="6" * 64)

        async with db._driver.session(database=db._database) as s:
            expired_left = await (await s.run(
                "MATCH (b:FeedbackBatch) WHERE b.batch_id IN $ids RETURN count(b) AS n",
                ids=expired_ids,
            )).single()
            young_left = await (await s.run(
                "MATCH (b:FeedbackBatch {batch_id: 'young-record'}) RETURN count(b) AS n",
            )).single()

        assert young_left["n"] == 1, "a record younger than the TTL must not be pruned"
        assert expired_left["n"] < len(expired_ids), (
            "at least one expired record must have been pruned"
        )
        assert expired_left["n"] >= max(0, len(expired_ids) - limit), (
            f"at most {limit} expired record(s) may be pruned per applied batch: "
            f"{expired_left['n']} of {len(expired_ids)} remain"
        )

    @pytest.mark.asyncio
    async def test_a_replay_prunes_nothing(self, db) -> None:
        _require(db, "apply_feedback_batch")
        await _seed_rule(db, "FB-PRUNE-REPLAY", pos=0, neg=0)
        batch_id = "7" * 64
        # The first call is a new batch, which prunes expired records by design, so the
        # expired record is seeded only after it: what is under test is the replay alone.
        await db.apply_feedback_batch([("FB-PRUNE-REPLAY", "positive")], batch_id=batch_id)
        async with db._driver.session(database=db._database) as s:
            await s.run(
                "MERGE (b:FeedbackBatch {batch_id: 'stale-for-replay-test'}) "
                "SET b.created_at = datetime() - duration({days: 31}), b.result = '{}'",
            )
        # A pure replay: must not run the prune step at all.
        await db.apply_feedback_batch([("FB-PRUNE-REPLAY", "positive")], batch_id=batch_id)
        async with db._driver.session(database=db._database) as s:
            rec = await (await s.run(
                "MATCH (b:FeedbackBatch {batch_id: 'stale-for-replay-test'}) "
                "RETURN count(b) AS n",
            )).single()
            assert rec["n"] == 1, "a replay must not prune an expired record"


class TestBatchIdQueryCounts:
    @pytest.mark.asyncio
    async def test_new_batch_with_id_none_crossing_is_four_queries(self, db) -> None:
        _require(db, "apply_feedback_batch")
        rule_ids = [f"FB-BID-CNT-{i}" for i in range(3)]
        for rid in rule_ids:
            await _seed_rule(db, rid, pos=0, neg=0)  # far below LOW_T
        counters = _install_counters(db)
        await db.apply_feedback_batch(
            [(rid, "positive") for rid in rule_ids], threshold=LOW_T, batch_id="8" * 64,
        )
        assert counters.sessions_opened == 1
        assert counters.execute_write_calls == 1
        assert counters.tx_run == 4, (
            "merge + increment + store + prune, no rule crossing: 4 queries"
        )

    @pytest.mark.asyncio
    async def test_new_batch_with_id_and_a_crossing_rule_is_five_queries(self, db) -> None:
        _require(db, "apply_feedback_batch")
        await _seed_rule(db, "FB-BID-CROSS", pos=LOW_T - 1, neg=0)
        counters = _install_counters(db)
        await db.apply_feedback_batch(
            [("FB-BID-CROSS", "positive")], threshold=LOW_T, batch_id="9" * 64,
        )
        assert counters.tx_run == 5, "merge + increment + flip + store + prune = 5 queries"

    @pytest.mark.asyncio
    async def test_a_replay_is_exactly_one_query(self, db) -> None:
        _require(db, "apply_feedback_batch")
        await _seed_rule(db, "FB-BID-REPLAY-CNT", pos=0, neg=0)
        batch_id = "a1" * 32
        await db.apply_feedback_batch([("FB-BID-REPLAY-CNT", "positive")], batch_id=batch_id)
        counters = _install_counters(db)
        await db.apply_feedback_batch([("FB-BID-REPLAY-CNT", "positive")], batch_id=batch_id)
        assert counters.sessions_opened == 1
        assert counters.execute_write_calls == 1
        assert counters.tx_run == 1, "a replay must run only the merge lookup"

    @pytest.mark.asyncio
    async def test_without_a_batch_id_the_counts_are_todays(self, db) -> None:
        _require(db, "apply_feedback_batch")
        rule_ids = [f"FB-NOID-CNT-{i}" for i in range(3)]
        for rid in rule_ids:
            await _seed_rule(db, rid, pos=0, neg=0)
        counters = _install_counters(db)
        await db.apply_feedback_batch([(rid, "positive") for rid in rule_ids], threshold=LOW_T)
        assert counters.tx_run == 1, "no batch_id: today's single-query path is unchanged"


class TestFeedbackBatchSchemaAndDumpExclusion:
    @pytest.mark.asyncio
    async def test_apply_constraints_creates_the_uniqueness_constraint_and_the_index(
        self, db
    ) -> None:
        await db.apply_constraints()
        constraints = await db.list_constraints()
        names = {c.get("name") for c in constraints}
        assert "feedbackbatch_batch_id_unique" in names, names
        indexes = await db.list_indexes()
        index_names = {i.get("name") for i in indexes}
        assert "feedbackbatch_created_at" in index_names, index_names

    def test_feedbackbatch_is_in_record_labels(self) -> None:
        from writ.graph.db._common import RECORD_LABELS
        assert "FeedbackBatch" in RECORD_LABELS

    @pytest.mark.asyncio
    async def test_a_stored_feedbackbatch_record_is_absent_from_the_dump(self, db) -> None:
        _require(db, "apply_feedback_batch")
        await _seed_rule(db, "FB-DUMP-001", pos=0, neg=0)
        await db.apply_feedback_batch([("FB-DUMP-001", "positive")], batch_id="b2" * 32)
        dump_nodes = await db.get_all_nodes_for_dump()
        labels_in_dump = {node.get("label") for node in dump_nodes}
        assert "FeedbackBatch" not in labels_in_dump, (
            "a FeedbackBatch record must never ship in the public corpus dump"
        )


# --------------------------------------------------------------------------------- #
# Capability: the route contract, no graph needed.
# --------------------------------------------------------------------------------- #


@pytest_asyncio.fixture()
async def route_client():
    from unittest.mock import AsyncMock

    from httpx import ASGITransport, AsyncClient

    import writ.server as server

    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, server


class TestFeedbackBatchRoute:
    @pytest.mark.asyncio
    async def test_recorded_not_found_and_graduation_pending_lists(self, route_client) -> None:
        from unittest.mock import AsyncMock

        ac, server = route_client
        fake_db = AsyncMock()
        fake_db.apply_feedback_batch.return_value = {
            "recorded": ["R-1"], "not_found": ["R-2"], "graduation_pending": ["R-1"],
            "applied": 1,
        }
        orig = server._db
        server._db = fake_db
        try:
            resp = await ac.post("/feedback/batch", json={"signals": [
                {"rule_id": "R-1", "signal": "positive"},
                {"rule_id": "R-2", "signal": "negative"},
            ]})
        finally:
            server._db = orig
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["recorded"] == ["R-1"]
        assert body["not_found"] == ["R-2"]
        assert body["graduation_pending"] == ["R-1"]

    @pytest.mark.asyncio
    async def test_empty_list_makes_no_db_call(self, route_client) -> None:
        from unittest.mock import AsyncMock

        ac, server = route_client
        fake_db = AsyncMock()
        orig = server._db
        server._db = fake_db
        try:
            resp = await ac.post("/feedback/batch", json={"signals": []})
        finally:
            server._db = orig
        assert resp.status_code == 200, resp.text
        fake_db.apply_feedback_batch.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_invalid_signal_is_422_with_nothing_applied(self, route_client) -> None:
        from unittest.mock import AsyncMock

        ac, server = route_client
        fake_db = AsyncMock()
        orig = server._db
        server._db = fake_db
        try:
            resp = await ac.post("/feedback/batch", json={"signals": [
                {"rule_id": "R-1", "signal": "sideways"},
            ]})
        finally:
            server._db = orig
        assert resp.status_code == 422, resp.text
        fake_db.apply_feedback_batch.assert_not_called()

    @pytest.mark.asyncio
    async def test_more_than_1000_items_is_422_with_nothing_applied(self, route_client) -> None:
        from unittest.mock import AsyncMock

        ac, server = route_client
        fake_db = AsyncMock()
        orig = server._db
        server._db = fake_db
        try:
            resp = await ac.post("/feedback/batch", json={"signals": [
                {"rule_id": f"R-{i}", "signal": "positive"} for i in range(1001)
            ]})
        finally:
            server._db = orig
        assert resp.status_code == 422, resp.text
        fake_db.apply_feedback_batch.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_db_connection_returns_the_error_key(self, route_client) -> None:
        ac, server = route_client
        orig = server._db
        server._db = None
        try:
            resp = await ac.post("/feedback/batch", json={"signals": [
                {"rule_id": "R-1", "signal": "positive"},
            ]})
        finally:
            server._db = orig
        assert resp.status_code == 200, resp.text
        assert "error" in resp.json()


class TestFeedbackBatchRouteBatchIdValidation:
    @pytest.mark.asyncio
    async def test_a_malformed_batch_id_is_422_with_nothing_applied(self, route_client) -> None:
        from unittest.mock import AsyncMock

        ac, server = route_client
        fake_db = AsyncMock()
        orig = server._db
        server._db = fake_db
        try:
            resp = await ac.post("/feedback/batch", json={
                "signals": [{"rule_id": "R-1", "signal": "positive"}],
                "batch_id": "not-64-hex-chars",
            })
        finally:
            server._db = orig
        assert resp.status_code == 422, resp.text
        fake_db.apply_feedback_batch.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_valid_batch_id_is_passed_through_as_batch_id(self, route_client) -> None:
        from unittest.mock import AsyncMock

        ac, server = route_client
        fake_db = AsyncMock()
        fake_db.apply_feedback_batch.return_value = {
            "recorded": ["R-1"], "not_found": [], "graduation_pending": [], "applied": 1,
        }
        orig = server._db
        server._db = fake_db
        batch_id = "c" * 64
        try:
            resp = await ac.post("/feedback/batch", json={
                "signals": [{"rule_id": "R-1", "signal": "positive"}],
                "batch_id": batch_id,
            })
        finally:
            server._db = orig
        assert resp.status_code == 200, resp.text
        _args, kwargs = fake_db.apply_feedback_batch.call_args
        assert kwargs.get("batch_id") == batch_id, (
            "record_feedback_batch must pass batch_id=request.batch_id through to "
            f"apply_feedback_batch: got kwargs {kwargs}"
        )


class TestSingleFeedbackRouteUnchanged:
    """Regression guard: POST /feedback keeps recording a single signal and flipping
    graduation exactly as at HEAD. Must stay green before and after 3b."""

    @pytest.mark.asyncio
    async def test_records_a_single_signal_and_flips_graduation(self, route_client) -> None:
        from unittest.mock import AsyncMock

        ac, server = route_client
        fake_db = AsyncMock()
        fake_db.increment_positive.return_value = True
        fake_db.evaluate_and_flip_graduation.return_value = "graduation_pending"
        orig = server._db
        server._db = fake_db
        try:
            resp = await ac.post("/feedback", json={"rule_id": "R-1", "signal": "positive"})
        finally:
            server._db = orig
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["recorded"] is True
        assert body["graduation_pending"] is True
        fake_db.increment_positive.assert_awaited_once_with("R-1")
