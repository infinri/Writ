"""Program item 2: feedback reaches the served ranking without a reload (unit level).

No graph: this file pins RetrievalPipeline.apply_feedback's contract and the feedback
routes' state, replay and fail-open branches. The graph-backed claims (the state the real
writes return, a replay answered from a real FeedbackBatch record, the unchanged statement
count) are in tests/test_live_reload.py, because a stub returning what the test wrote
proves nothing about them (ENF-SYS-005).
"""
from __future__ import annotations

import pytest

import writ.server as server
from writ.retrieval.pipeline import RetrievalPipeline
from writ.retrieval.traversal import AdjacencyCache
from writ.server.models import FeedbackBatchRequest, FeedbackRequest, FeedbackSignal
from writ.server.routes import query as query_routes


def _pipeline(metadata: dict[str, dict], encoder: object = None) -> RetrievalPipeline:
    return RetrievalPipeline(
        keyword_index=None, vector_store=None, adjacency_cache=AdjacencyCache(),
        embedding_model=encoder, rule_metadata=metadata,
    )


def _meta(rid: str = "R-1", project: str = "writ") -> dict:
    return {"rule_id": rid, "project": project, "times_seen_positive": 1,
            "times_seen_negative": 0, "provenance": "proposed"}


def _row(rid="R-1", project="writ", pos=0, neg=0, provenance=None) -> dict:
    return {"rule_id": rid, "project": project, "times_seen_positive": pos,
            "times_seen_negative": neg, "provenance": provenance}


class TestApplyFeedback:
    def test_sets_absolute_counts_and_provenance(self) -> None:
        p = _pipeline({"R-1": _meta()})
        assert p.apply_feedback([_row(pos=50, neg=2, provenance="graduation_pending")]) == ["R-1"]
        m = p._metadata["R-1"]
        assert (m["times_seen_positive"], m["times_seen_negative"], m["provenance"]) == (
            50, 2, "graduation_pending")

    def test_an_unknown_rule_is_never_added(self) -> None:
        p = _pipeline({"R-1": _meta()})
        assert p.apply_feedback([_row(rid="R-NEW", pos=3)]) == []
        assert set(p._metadata) == {"R-1"}

    def test_a_same_id_node_from_another_project_is_skipped(self) -> None:
        p = _pipeline({"R-1": _meta(project="writ")})
        assert p.apply_feedback([_row(project="other", pos=9, neg=9)]) == []
        assert p._metadata["R-1"]["times_seen_positive"] == 1

    def test_the_entry_is_replaced_not_mutated(self) -> None:
        original = _meta()
        p = _pipeline({"R-1": original})
        p.apply_feedback([_row(pos=7)])
        assert original["times_seen_positive"] == 1, "a reader of the old entry saw the write"
        assert p._metadata["R-1"] is not original

    def test_a_null_provenance_keeps_the_existing_value(self) -> None:
        p = _pipeline({"R-1": _meta()})
        p.apply_feedback([_row(pos=2)])
        assert p._metadata["R-1"]["provenance"] == "proposed"

    def test_the_encoder_property_is_the_query_encoder(self) -> None:
        sentinel = object()
        assert _pipeline({}, encoder=sentinel).encoder is sentinel


class _Db:
    """Graph stub for the route branches only: fills state_out as the real writes do."""

    def __init__(self, replayed: bool = False) -> None:
        self.replayed = replayed

    async def increment_positive(self, rule_id: str) -> bool:
        return True

    async def evaluate_and_flip_graduation(self, rule_id: str, state_out=None):
        if state_out is not None:
            state_out.update(_row(pos=2, provenance="proposed"))
        return None

    async def apply_feedback_batch(self, signals, batch_id=None, state_out=None) -> dict:
        answer = {"recorded": ["R-1"], "not_found": [], "graduation_pending": [], "applied": 1}
        if self.replayed:
            return {**answer, "replayed": True}
        if state_out is not None:
            state_out.append(_row(neg=4, provenance="proposed"))
        return answer


@pytest.fixture()
def exceptions(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(query_routes, "emit_exception",
                        lambda component, exc, *a, **k: seen.append(component))
    monkeypatch.setattr(server, "_reloader", None)
    return seen


class TestTheFeedbackRoutesApplyTheReturnedState:
    @pytest.mark.asyncio
    async def test_a_single_signal_applies_the_state_the_write_read(self, monkeypatch, exceptions) -> None:
        monkeypatch.setattr(server, "_db", _Db())
        monkeypatch.setattr(server, "_pipeline", _pipeline({"R-1": _meta()}))
        resp = await query_routes.record_feedback(FeedbackRequest(rule_id="R-1", signal="positive"))
        assert resp == {"rule_id": "R-1", "signal": "positive", "recorded": True,
                        "graduation_pending": False}
        assert server._pipeline._metadata["R-1"]["times_seen_positive"] == 2
        assert exceptions == []

    @pytest.mark.asyncio
    async def test_a_batch_applies_the_returned_rows_and_answers_unchanged(self, monkeypatch, exceptions) -> None:
        monkeypatch.setattr(server, "_db", _Db())
        monkeypatch.setattr(server, "_pipeline", _pipeline({"R-1": _meta()}))
        resp = await query_routes.record_feedback_batch(FeedbackBatchRequest(
            signals=[FeedbackSignal(rule_id="R-1", signal="negative")]))
        assert resp == {"recorded": ["R-1"], "not_found": [], "graduation_pending": [], "applied": 1}
        assert server._pipeline._metadata["R-1"]["times_seen_negative"] == 4

    @pytest.mark.asyncio
    async def test_a_replayed_batch_applies_nothing(self, monkeypatch, exceptions) -> None:
        monkeypatch.setattr(server, "_db", _Db(replayed=True))
        monkeypatch.setattr(server, "_pipeline", _pipeline({"R-1": _meta()}))
        resp = await query_routes.record_feedback_batch(FeedbackBatchRequest(
            signals=[FeedbackSignal(rule_id="R-1", signal="positive")], batch_id="a" * 64))
        assert resp["replayed"] is True
        assert server._pipeline._metadata["R-1"] == _meta()


class TestTheFeedbackRoutesFailOpen:
    @pytest.mark.asyncio
    async def test_a_failed_apply_leaves_the_single_answer_unchanged(self, monkeypatch, exceptions) -> None:
        monkeypatch.setattr(server, "_db", _Db())
        monkeypatch.setattr(server, "_pipeline", object())
        resp = await query_routes.record_feedback(FeedbackRequest(rule_id="R-1", signal="positive"))
        assert resp == {"rule_id": "R-1", "signal": "positive", "recorded": True,
                        "graduation_pending": False}
        assert exceptions == ["server.feedback.refresh"]

    @pytest.mark.asyncio
    async def test_a_failed_apply_leaves_the_batch_answer_unchanged(self, monkeypatch, exceptions) -> None:
        monkeypatch.setattr(server, "_db", _Db())
        monkeypatch.setattr(server, "_pipeline", object())
        resp = await query_routes.record_feedback_batch(FeedbackBatchRequest(
            signals=[FeedbackSignal(rule_id="R-1", signal="positive")]))
        assert resp == {"recorded": ["R-1"], "not_found": [], "graduation_pending": [], "applied": 1}
        assert exceptions == ["server.feedback.refresh"]
