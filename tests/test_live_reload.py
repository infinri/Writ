"""Program item 2 against the isolated graph: edits and feedback reach retrieval live.

ENF-SYS-005, declared: every test here needs the REAL graph and the real index build,
because each claim is about what the graph holds after a write. The ordering-only claims
(lock, coalescing) are proven with an injected builder in tests/test_retrieval_reloader.py.

Isolated graph only:
    PYTHON=$(command -v python3) bash scripts/test-graph.sh up
    python3 -m pytest tests/test_live_reload.py -q
Never run this with WRIT_TEST_NO_ISOLATION=1: it creates and deletes a rule. The on-disk
BM25 and HNSW caches are redirected into tmp_path so the operator's caches are untouched.
"""
from __future__ import annotations

import asyncio
import threading
import uuid

import pytest
import pytest_asyncio

import writ.retrieval.pipeline as pipeline_mod
import writ.server as server
import writ.server.reload as reload_mod
from writ.frequency import (
    DEFAULT_GRADUATION_RATIO_MIN,
    DEFAULT_GRADUATION_THRESHOLD,
    evaluate_graduation,
)
from writ.server.models import (
    FeedbackBatchRequest,
    FeedbackRequest,
    FeedbackSignal,
    QueryRequest,
)
from writ.server.routes import query as query_routes

RID = "TEST-RELOAD-LIVE-001"
_SERVER_STATE = ("_db", "_pipeline", "_trigger_index", "_retrieval", "_reloader")


def _rule(**overrides) -> dict:
    data = {
        "rule_id": RID, "domain": "testing", "severity": "medium", "scope": "file",
        "trigger": "When verifying the live reload path.",
        "statement": "Reload fixtures use the marmot keyword.",
        "violation": "x = 1", "pass_example": "y = 2", "enforcement": "test",
        "rationale": "test", "last_validated": "2026-10-06",
        "confidence": "speculative", "authority": "ai-provisional",
        "provenance": "proposed", "times_seen_positive": 49, "times_seen_negative": 0,
    }
    data.update(overrides)
    return data


def _bm25_ids(text: str) -> list[str]:
    return [r["rule_id"] for r in server._pipeline._keyword.search(text, limit=20)]


@pytest.fixture()
def graph_up():
    pytest.importorskip("onnxruntime")
    from tests._corpus import neo4j_reachable

    if not neo4j_reachable():
        pytest.skip("isolated graph unreachable: PYTHON=$(command -v python3) bash scripts/test-graph.sh up")


@pytest_asyncio.fixture()
async def live(graph_up, monkeypatch, tmp_path):
    from tests._graph import connection

    monkeypatch.setattr(pipeline_mod, "get_hnsw_cache_dir",
                        lambda *a, **k: str(tmp_path / "hnsw"))
    # Re-setting each name to its own value registers it for restoration at teardown.
    for name in _SERVER_STATE:
        monkeypatch.setattr(server, name, getattr(server, name))
    db = connection()
    await db.create_rule(_rule(), source_origin="graph-authored")
    monkeypatch.setattr(server, "_db", db)
    try:
        await server._start_retrieval(db)
        yield db
    finally:
        if server._reloader is not None:
            server._reloader.cancel_pending()
        await db.delete_rule(RID)
        await db.close()


class TestReloadPicksUpTheGraph:
    @pytest.mark.asyncio
    async def test_an_edited_rule_is_served_after_a_reload(self, live) -> None:
        gen = server._retrieval.generation
        assert "marmot" in server._pipeline._metadata[RID]["statement"]
        await live.create_rule(_rule(statement="Reload fixtures use the quokka keyword."),
                               source_origin="graph-authored")

        outcome = await server._reloader.request()

        assert (outcome.status, outcome.generation) == ("swapped", gen + 1)
        assert {"bm25", "hnsw", "metadata"} <= set(outcome.changed)
        assert server._retrieval.generation == gen + 1
        assert server._pipeline is server._retrieval.pipeline
        assert server._trigger_index is server._retrieval.trigger_index
        assert "quokka" in server._pipeline._metadata[RID]["statement"]
        assert RID in _bm25_ids("quokka")
        assert RID not in _bm25_ids("marmot")

    @pytest.mark.asyncio
    async def test_an_unchanged_graph_does_not_swap(self, live) -> None:
        before = server._retrieval
        body = await query_routes.retrieval_reload()
        assert body["status"] == "unchanged" and "error" not in body
        assert server._retrieval is before and server._pipeline is before.pipeline

    @pytest.mark.asyncio
    async def test_the_reload_reuses_the_running_encoder(self, live) -> None:
        old = server._pipeline
        await live.create_rule(_rule(statement="Reload fixtures use the wombat keyword."),
                               source_origin="graph-authored")
        assert (await server._reloader.request()).status == "swapped"
        assert server._pipeline is not old
        assert server._pipeline.encoder is old.encoder

    @pytest.mark.asyncio
    async def test_concurrent_requests_build_once_more_at_most(self, live) -> None:
        gen = server._retrieval.generation
        await live.create_rule(_rule(statement="Reload fixtures use the ibex keyword."),
                               source_origin="graph-authored")
        outcomes = await asyncio.gather(*(server._reloader.request() for _ in range(4)))
        assert [o.status for o in outcomes].count("swapped") == 1
        assert server._retrieval.generation == gen + 1


class TestAFailedRebuildKeepsServing:
    @pytest.mark.asyncio
    async def test_a_graph_read_failure_keeps_the_previous_generation(self, live, monkeypatch) -> None:
        before = server._retrieval

        async def _unreachable(db):
            raise RuntimeError("graph unreachable (simulated)")

        monkeypatch.setattr(reload_mod, "load_pipeline_inputs", _unreachable)
        body = await query_routes.retrieval_reload()

        assert body["status"] == "failed" and "graph unreachable" in body["error"]
        assert server._retrieval is before and server._pipeline is before.pipeline
        answer = await query_routes.query_rules(
            QueryRequest(query="When verifying the live reload path", budget_tokens=4000))
        assert "error" not in answer
        health = await query_routes.health()
        assert health["retrieval"]["generation"] == before.generation
        assert health["retrieval"]["last_reload"]["status"] == "failed"

    @pytest.mark.asyncio
    async def test_a_build_failure_after_an_edit_keeps_the_old_content(self, live, monkeypatch) -> None:
        await live.create_rule(_rule(statement="Reload fixtures use the quokka keyword."),
                               source_origin="graph-authored")

        def _broken(*args, **kwargs):
            raise RuntimeError("index build failed (simulated)")

        monkeypatch.setattr(reload_mod, "assemble_pipeline", _broken)
        outcome = await server._reloader.request()
        assert outcome.status == "failed"
        assert "marmot" in server._pipeline._metadata[RID]["statement"]
        assert RID in _bm25_ids("marmot")


class TestQueriesDuringAReload:
    @pytest.mark.asyncio
    async def test_every_query_during_a_swap_succeeds(self, live) -> None:
        await live.create_rule(_rule(statement="Reload fixtures use the quokka keyword."),
                               source_origin="graph-authored")
        old = server._pipeline
        errors: list[BaseException] = []
        calls = {"n": 0}
        stop = threading.Event()

        def hammer() -> None:
            while not stop.is_set():
                try:
                    server._pipeline.query(query_text="live reload path", budget_tokens=4000)
                    calls["n"] += 1
                except BaseException as exc:  # noqa: BLE001 - the assertion is "none"
                    errors.append(exc)

        worker = threading.Thread(target=hammer)
        worker.start()
        try:
            queries = [
                query_routes.query_rules(QueryRequest(query=q, budget_tokens=4000))
                for q in ("live reload path", "verifying the reload", "marmot", "quokka") * 5
            ]
            results = await asyncio.gather(server._reloader.request(), *queries)
        finally:
            stop.set()
            worker.join(timeout=30)

        outcome, answers = results[0], results[1:]
        assert outcome.status == "swapped"
        assert errors == []
        assert calls["n"] > 0
        assert all("error" not in a and isinstance(a.get("rules"), list) for a in answers)
        stale = await asyncio.to_thread(old.query, query_text="live reload path", budget_tokens=4000)
        assert isinstance(stale.get("rules"), list), "an in-flight query on the old generation broke"


class _CountingSession:
    """Delegates to a real session and records every statement it runs."""

    def __init__(self, inner, statements: list[str]) -> None:
        self._inner = inner
        self._statements = statements

    async def __aenter__(self):
        self._session = await self._inner.__aenter__()
        return self

    async def __aexit__(self, *exc):
        return await self._inner.__aexit__(*exc)

    async def run(self, query, *args, **kwargs):
        self._statements.append(" ".join(str(query).split())[:60])
        return await self._session.run(query, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._session, name)


class _CountingDriver:
    def __init__(self, inner, statements: list[str]) -> None:
        self._inner = inner
        self._statements = statements

    def session(self, *args, **kwargs):
        return _CountingSession(self._inner.session(*args, **kwargs), self._statements)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class TestFeedbackWithoutAReload:
    @pytest.mark.asyncio
    async def test_a_signal_updates_the_served_ranking_inputs(self, live, monkeypatch) -> None:
        assert evaluate_graduation(
            50, 0, DEFAULT_GRADUATION_THRESHOLD, DEFAULT_GRADUATION_RATIO_MIN,
        ).graduated, "precondition: 50/0 must cross graduation"
        gen, pipeline = server._retrieval.generation, server._pipeline

        def score() -> float:
            ranked = pipeline._first_pass_rank(
                {RID: {"bm25_norm": 1.0, "vector_norm": 1.0}}, pipeline._weights)
            return ranked[0][1]

        before = score()
        statements: list[str] = []
        monkeypatch.setattr(live, "_driver", _CountingDriver(live._driver, statements))
        resp = await query_routes.record_feedback(FeedbackRequest(rule_id=RID, signal="positive"))

        assert resp["recorded"] is True and resp["graduation_pending"] is True
        meta = server._pipeline._metadata[RID]
        assert meta["times_seen_positive"] == 50
        assert meta["provenance"] == "graduation_pending"
        assert server._retrieval.generation == gen and server._pipeline is pipeline
        assert score() > before, "the learned confidence did not reach the ranking"
        assert len(statements) == 3, (
            f"PERF-QBUDGET-001: increment, graduation read, flip and nothing more: {statements}"
        )

    @pytest.mark.asyncio
    async def test_a_batch_applies_its_returned_rows_and_a_replay_applies_nothing(self, live) -> None:
        batch_id = uuid.uuid4().hex + uuid.uuid4().hex
        request = FeedbackBatchRequest(signals=[
            FeedbackSignal(rule_id=RID, signal="negative"),
            FeedbackSignal(rule_id=RID, signal="negative"),
            FeedbackSignal(rule_id="TEST-RELOAD-NO-SUCH-RULE", signal="positive"),
        ], batch_id=batch_id)
        try:
            first = await query_routes.record_feedback_batch(request)
            assert first["recorded"] == [RID]
            assert "state" not in first and "rows" not in first, "the answer shape changed"
            assert server._pipeline._metadata[RID]["times_seen_negative"] == 2

            server._pipeline._metadata[RID] = {
                **server._pipeline._metadata[RID], "times_seen_negative": 0}
            replay = await query_routes.record_feedback_batch(request)
            assert replay.get("replayed") is True
            assert server._pipeline._metadata[RID]["times_seen_negative"] == 0, (
                "a replay committed nothing and must apply nothing"
            )
        finally:
            await live._run("MATCH (b:FeedbackBatch {batch_id: $b}) DELETE b", b=batch_id)
