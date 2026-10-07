"""P2 item 7: retrieval quality signals.

Three outcomes were indistinguishable in the logs, with three unrelated fixes:

  "the S4 abstention gate declined to inject"  -> tune the threshold
  "the query matched no rules"                 -> author a rule
  "the graph was unreachable"                  -> start Neo4j

The abstention path returned `{"rules": [], "mode": "abstained"}` and emitted nothing, an
empty result set emitted nothing, and a raising pipeline produced only a 500 whose cause
never reached a stream. Two HNSW handlers logged at `_logger.debug`/`warning`, invisible at
the default level, so a cold start that re-encoded the entire corpus left no trail.

These drive the real /query route with a stubbed pipeline, so each retrieval outcome is
produced deliberately rather than depending on the live graph.
"""
from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient


@pytest.fixture
def rows(tmp_path, monkeypatch):
    log = tmp_path / "events.jsonl"
    monkeypatch.setenv("WRIT_FRICTION_LOG", str(log))

    def _read(event: str | None = None) -> list[dict]:
        if not log.exists():
            return []
        out = []
        for line in log.read_text().splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event is None or r.get("event") == event:
                out.append(r)
        return out

    return _read


class _StubPipeline:
    """Returns a canned result, or raises, in place of a live graph + index."""

    def __init__(self, result=None, exc=None):
        self._result, self._exc = result, exc
        self.calls = 0

    def query(self, **kwargs):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return self._result


@pytest.fixture
def client(monkeypatch):
    """A client whose pipeline is the stub.

    Deliberately NOT used as a context manager: entering one runs the app lifespan, which
    connects Neo4j and installs the real pipeline over the stub, so every assertion below
    would silently be testing live retrieval instead of the outcome it set up.
    """
    import writ.server as server

    def _with(pipeline):
        monkeypatch.setattr(server, "_pipeline", pipeline)
        return TestClient(server.app, raise_server_exceptions=False)

    return _with


ABSTAINED = {"rules": [], "mode": "abstained", "total_candidates": 0,
             "latency_ms": 4.2, "abstain_signal": 0.1874}
EMPTY = {"rules": [], "mode": "semantic", "total_candidates": 0, "latency_ms": 3.1}
HIT = {"rules": [{"rule_id": "SEC-1"}, {"rule_id": "SEC-2"}], "mode": "semantic",
       "total_candidates": 12, "latency_ms": 9.9}


class TestAbstentionIsVisible:
    def test_abstention_emits_a_row_with_its_signal(self, client, rows):
        c = client(_StubPipeline(ABSTAINED))
        c.post("/query", json={"query": "unrelated gibberish"})
        row = rows("retrieval_result")[0]
        assert row["mode"] == "abstained"
        assert row["rule_count"] == 0
        assert row["abstain_signal"] == pytest.approx(0.1874), (
            "the top cosine that failed the threshold is the number needed to tune it"
        )

    def test_abstention_is_distinguishable_from_an_empty_match(self, client, rows):
        """The whole point: same empty rule set, different recorded cause."""
        c = client(_StubPipeline(ABSTAINED))
        c.post("/query", json={"query": "a"})
        c = client(_StubPipeline(EMPTY))
        c.post("/query", json={"query": "b"})
        modes = [r["mode"] for r in rows("retrieval_result")]
        assert modes == ["abstained", "semantic"]
        signals = [r.get("abstain_signal") for r in rows("retrieval_result")]
        assert signals[0] is not None and signals[1] is None


class TestEveryQueryIsCounted:
    def test_a_successful_retrieval_also_emits(self, client, rows):
        """Without the denominator, an abstention rate cannot be computed at all."""
        c = client(_StubPipeline(HIT))
        c.post("/query", json={"query": "add an index"})
        row = rows("retrieval_result")[0]
        assert row["mode"] == "semantic"
        assert row["rule_count"] == 2
        assert row["total_candidates"] == 12

    def test_rate_is_computable_from_the_rows_alone(self, client, rows):
        for result in (ABSTAINED, HIT, HIT, ABSTAINED):
            c = client(_StubPipeline(result))
            c.post("/query", json={"query": "x"})
        recorded = rows("retrieval_result")
        assert len(recorded) == 4
        abstentions = sum(1 for r in recorded if r["mode"] == "abstained")
        assert abstentions / len(recorded) == 0.5

    def test_session_id_is_carried_when_supplied(self, client, rows):
        c = client(_StubPipeline(HIT))
        c.post("/query", json={"query": "x", "session_id": "sid-9"})
        assert rows("retrieval_result")[0]["session"] == "sid-9"

    def test_session_id_is_optional(self, client, rows):
        """Four hooks POST /query today without one; they must keep working."""
        c = client(_StubPipeline(HIT))
        resp = c.post("/query", json={"query": "x"})
        assert resp.status_code == 200
        assert rows("retrieval_result")[0]["session"] == ""


class TestGraphFailureIsVisible:
    def test_a_raising_pipeline_records_an_exception(self, client, rows):
        c = client(_StubPipeline(exc=RuntimeError("Neo4j unreachable")))
        c.post("/query", json={"query": "x"})
        errs = rows("exception")
        assert errs, "a graph failure must reach the errors stream"
        assert errs[0]["component"] == "server.query"
        assert "Neo4j unreachable" in json.dumps(errs[0])

    def test_the_response_shape_is_unchanged_on_failure(self, client, rows):
        """Deliberately still a 500: hooks fail open on it, and changing the hottest
        route's error contract is a separate decision from making the cause visible."""
        c = client(_StubPipeline(exc=RuntimeError("boom")))
        resp = c.post("/query", json={"query": "x"})
        assert resp.status_code == 500

    def test_a_failed_query_emits_no_quality_row(self, client, rows):
        """A failure is not a retrieval result; counting it would poison the rate."""
        c = client(_StubPipeline(exc=RuntimeError("boom")))
        c.post("/query", json={"query": "x"})
        assert rows("retrieval_result") == []


class TestTelemetryCannotBreakRetrieval:
    def test_a_broken_emit_still_returns_rules(self, client, rows, monkeypatch):
        import writ.server.routes.query as qmod

        monkeypatch.setattr(qmod, "emit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        c = client(_StubPipeline(HIT))
        resp = c.post("/query", json={"query": "x"})
        assert resp.status_code == 200
        assert len(resp.json()["rules"]) == 2


class TestStreamRegistration:
    @pytest.mark.parametrize("event", ["retrieval_result", "hnsw_cache"])
    def test_new_events_are_mapped(self, event):
        from writ.shared.logging import STREAM_MAP

        assert STREAM_MAP.get(event) == "metrics"

    def test_hnsw_handlers_emit_rather_than_only_logging(self):
        """P1 deferred these two as 'least silent'; _logger.debug is not visible."""
        from pathlib import Path

        src = Path(__file__).resolve().parent.parent / "writ" / "retrieval" / "pipeline.py"
        text = src.read_text()
        # The shared index builder prefixes the metric per index ("" for rules).
        assert 'metric_prefix}hnsw_cache"' in text or '"hnsw_cache"' in text
        assert '"retrieval.hnsw.save"' in text, (
            "a failed index save must reach the errors stream, not just _logger.warning"
        )
