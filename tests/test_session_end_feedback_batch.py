"""Plan f7fc2b37-9a53-4011-a69f-e6b97f5e45fe, batch 4 (3b): the CLIENT side of batched
SessionEnd feedback -- `writ-session.py auto-feedback` against a stub daemon.

RED at HEAD: `writ/session/feedback.py::_send_feedback` loops `POST /feedback` once per
queued rule (feedback.py:123-152); it does not know about `/feedback/batch`, does not
de-duplicate the queue, and has no `feedback_unconfirmed` outcome. Every capability below
either observes the WRONG request pattern against the stub (a real, currently-passing-for-
the-wrong-reason baseline) or is asserted directly RED.

Isolation (TEST-ISOLATE-002): the stub binds an OS-assigned 127.0.0.1 port; every
subprocess gets WRIT_CACHE_DIR under tmp_path, WRIT_SOCKET pointed at a path that
cannot exist (forcing the TCP transport onto the stub, never the real unix socket at
~/.cache/writ/run/writ.sock), and WRIT_SESSION_BASE pointed at the stub -- never at
localhost:8765, which is a live, real daemon on the machine this file was authored on.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from tests.fixtures.net import free_port

REPO = Path(__file__).resolve().parent.parent
HELPER = REPO / "bin" / "lib" / "writ-session.py"
SID = "fb-batch-client-session"

RULE_IDS = ["SEC-BATCH-1", "SEC-BATCH-2", "SEC-BATCH-3", "SEC-BATCH-4", "SEC-BATCH-5"]


def _seed_queue(cache_dir: Path, rule_ids: list[str], *, feedback_sent: list[str] | None = None) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "session_id": SID,
        "loaded_rule_ids": rule_ids,
        # "SEC-*" is a UNIVERSAL_DOMAINS prefix (security), so every rule is relevant
        # to any written file regardless of its own domain; one passing python file
        # is enough to make every queued rule a POSITIVE signal.
        "analysis_results": {"a.py": "pass"},
        "feedback_sent": list(feedback_sent or []),
    }
    (cache_dir / f"writ-session-{SID}.json").write_text(json.dumps(state))


def _read_cache(cache_dir: Path) -> dict:
    return json.loads((cache_dir / f"writ-session-{SID}.json").read_text())


def _run_auto_feedback(cache_dir: Path, *, socket_path: str, base_url: str) -> dict:
    env = os.environ.copy()
    env["WRIT_CACHE_DIR"] = str(cache_dir)
    env["WRIT_SOCKET"] = socket_path
    env["WRIT_SESSION_BASE"] = base_url
    result = subprocess.run(
        [sys.executable, str(HELPER), "auto-feedback", SID],
        env=env, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def _missing_socket(tmp_path: Path) -> str:
    return str(tmp_path / "no-such-writ.sock")


class _RecordingBatchHandler(BaseHTTPRequestHandler):
    """Records every POST path + body; answers according to class-level knobs so each
    test configures ONE handler class per scenario without a metaclass per test."""

    requests: list[tuple[str, dict]] = []
    batch_status = 200
    batch_body = None  # callable(payload) -> dict, or a fixed dict

    def log_message(self, *a) -> None:  # noqa: N802
        pass

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode())
        except Exception:
            payload = {}
        type(self).requests.append((self.path, payload))
        if self.path == "/feedback/batch":
            status = type(self).batch_status
            body_spec = type(self).batch_body
            body = body_spec(payload) if callable(body_spec) else (body_spec or {})
            self._answer(status, body)
        elif self.path == "/feedback":
            self._answer(200, {"rule_id": payload.get("rule_id"), "recorded": True,
                                "graduation_pending": False})
        else:
            self.send_error(404)

    def _answer(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class _NotFoundBatchHandler(_RecordingBatchHandler):
    """404s /feedback/batch (an old daemon that predates the route)."""

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode())
        except Exception:
            payload = {}
        type(self).requests.append((self.path, payload))
        if self.path == "/feedback/batch":
            self.send_error(404)
        elif self.path == "/feedback":
            self._answer(200, {"rule_id": payload.get("rule_id"), "recorded": True,
                                "graduation_pending": False})
        else:
            self.send_error(404)


def _start(handler_cls) -> HTTPServer:
    handler_cls.requests = []
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


class TestOneBatchRequestForFiveQueuedRules:
    def test_sends_one_feedback_batch_and_zero_feedback(self, tmp_path) -> None:
        _RecordingBatchHandler.batch_status = 200
        _RecordingBatchHandler.batch_body = {
            "recorded": RULE_IDS, "not_found": [], "graduation_pending": [], "applied": 5,
        }
        server = _start(_RecordingBatchHandler)
        try:
            cache_dir = tmp_path / "cache"
            _seed_queue(cache_dir, RULE_IDS)
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            report = _run_auto_feedback(cache_dir, socket_path=_missing_socket(tmp_path),
                                        base_url=base_url)
            batch_reqs = [r for r in _RecordingBatchHandler.requests if r[0] == "/feedback/batch"]
            single_reqs = [r for r in _RecordingBatchHandler.requests if r[0] == "/feedback"]
            assert len(batch_reqs) == 1, _RecordingBatchHandler.requests
            assert len(single_reqs) == 0, _RecordingBatchHandler.requests
            cache = _read_cache(cache_dir)
            assert sorted(cache.get("feedback_sent", [])) == sorted(RULE_IDS)
            assert report.get("feedback_sent") == len(RULE_IDS), report
        finally:
            server.shutdown()


class TestOldDaemonFallsBackToPerRuleLoop:
    def test_404_on_batch_falls_back_to_one_post_per_rule(self, tmp_path) -> None:
        server = _start(_NotFoundBatchHandler)
        try:
            cache_dir = tmp_path / "cache"
            _seed_queue(cache_dir, RULE_IDS)
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            _run_auto_feedback(cache_dir, socket_path=_missing_socket(tmp_path), base_url=base_url)
            batch_reqs = [r for r in _NotFoundBatchHandler.requests if r[0] == "/feedback/batch"]
            single_reqs = [r for r in _NotFoundBatchHandler.requests if r[0] == "/feedback"]
            assert len(batch_reqs) == 1
            assert len(single_reqs) == len(RULE_IDS), _NotFoundBatchHandler.requests
            cache = _read_cache(cache_dir)
            assert sorted(cache.get("feedback_sent", [])) == sorted(RULE_IDS)
        finally:
            server.shutdown()


class TestServerErrorMarksNothingSentAndResends:
    @pytest.mark.parametrize("status,body", [
        (500, {}), (422, {"detail": "bad"}), (200, {"error": "Database not connected."}),
    ])
    def test_error_response_marks_no_rule_sent(self, tmp_path, status, body) -> None:
        _RecordingBatchHandler.batch_status = status
        _RecordingBatchHandler.batch_body = body
        server = _start(_RecordingBatchHandler)
        try:
            cache_dir = tmp_path / "cache"
            _seed_queue(cache_dir, RULE_IDS)
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            _run_auto_feedback(cache_dir, socket_path=_missing_socket(tmp_path), base_url=base_url)
            cache = _read_cache(cache_dir)
            assert cache.get("feedback_sent", []) == []
            first_round = len(_RecordingBatchHandler.requests)
            _run_auto_feedback(cache_dir, socket_path=_missing_socket(tmp_path), base_url=base_url)
            second_round = len(_RecordingBatchHandler.requests) - first_round
            assert second_round == 1, "the second run must resend the identical batch"
        finally:
            server.shutdown()


class TestNothingListeningMarksNothingSentOrUnconfirmed:
    def test_refused_connection_marks_nothing(self, tmp_path) -> None:
        cache_dir = tmp_path / "cache"
        _seed_queue(cache_dir, RULE_IDS)
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
        closed.close()
        report = _run_auto_feedback(
            cache_dir, socket_path=_missing_socket(tmp_path),
            base_url=f"http://127.0.0.1:{port}",
        )
        cache = _read_cache(cache_dir)
        assert cache.get("feedback_sent", []) == []
        assert cache.get("feedback_unconfirmed", []) == []
        assert report.get("unconfirmed", 0) == 0


class _SilentServer:
    """Accepts, reads the request, never answers -- the delivered/no-response window."""

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(4)
        self.port = self._sock.getsockname()[1]
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop:
            self._sock.settimeout(0.2)
            try:
                conn, _ = self._sock.accept()
            except OSError:
                continue
            try:
                conn.settimeout(3)
                time.sleep(0.05)
                try:
                    conn.recv(65536)
                except OSError:
                    pass
            finally:
                conn.close()

    def close(self) -> None:
        self._stop = True
        self._sock.close()


class TestDeliveredButUnansweredMarksEveryRuleUnconfirmed:
    def test_all_queued_rules_join_unconfirmed_never_feedback_sent(self, tmp_path) -> None:
        silent = _SilentServer()
        try:
            cache_dir = tmp_path / "cache"
            _seed_queue(cache_dir, RULE_IDS)
            base_url = f"http://127.0.0.1:{silent.port}"
            report = _run_auto_feedback(cache_dir, socket_path=_missing_socket(tmp_path),
                                        base_url=base_url)
            cache = _read_cache(cache_dir)
            assert cache.get("feedback_sent", []) == []
            assert sorted(cache.get("feedback_unconfirmed", [])) == sorted(RULE_IDS)
            assert report.get("unconfirmed") == len(RULE_IDS)

            # A second run against the same silent peer must not re-send NEW work: the
            # unconfirmed ids are excluded from the next queue. It DOES resend the
            # pending batch itself (item B), which is still delivered-and-unanswered
            # here, so the full set stays unconfirmed and the report must say so --
            # not 0, which is what a version that silently dropped them would report.
            _RecordingBatchHandler.requests = []
            report2 = _run_auto_feedback(cache_dir, socket_path=_missing_socket(tmp_path),
                                         base_url=base_url)
            assert report2.get("feedback_sent", 0) == 0
            assert report2.get("unconfirmed") == len(RULE_IDS), (
                f"a second run after a lost response must still report the full "
                f"unconfirmed set, not 0: {report2}"
            )
        finally:
            silent.close()


def _expected_batch_id(session_id: str, queue: list[tuple[str, str]]) -> str:
    """Mirrors plan.md's own spec for `_batch_id`: sha256 of the session id and the
    sorted `rule_id:signal` pairs, hex. Computed independently of production code so
    comparing against it is a real property check, not production checking itself."""
    parts = [session_id] + sorted(f"{rid}:{sig}" for rid, sig in queue)
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def _seed_pending_batch(cache_dir: Path, *, batch_id: str, signals: list[tuple[str, str]],
                        queued_at: float, feedback_sent: list[str] | None = None) -> None:
    """A cache carrying ONE pending batch and nothing else queueable: `loaded_rule_ids`
    is exactly the pending batch's own rule ids, all already in `feedback_unconfirmed`
    (unless `feedback_sent`), so this run's only possible /feedback/batch POST is the
    resend under test -- `analysis_results` stays non-empty so cmd_auto_feedback's
    early return (`if not rules or not results: return`) does not suppress the report
    entirely."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    rule_ids = [rid for rid, _ in signals]
    sent = list(feedback_sent or [])
    state = {
        "session_id": SID,
        "loaded_rule_ids": rule_ids,
        "analysis_results": {"a.py": "pass"},
        "feedback_sent": sent,
        "feedback_unconfirmed": [rid for rid in rule_ids if rid not in sent],
        "feedback_pending_batches": [
            {"batch_id": batch_id, "signals": [list(pair) for pair in signals],
             "queued_at": queued_at},
        ],
    }
    (cache_dir / f"writ-session-{SID}.json").write_text(json.dumps(state))


class TestBatchIdDeterminism:
    """Plan f7fc2b37-9a53-4011-a69f-e6b97f5e45fe, item 4/B: `_batch_id` is a pure,
    content-addressed function of (session_id, sorted rule_id:signal pairs). RED at
    HEAD: `writ.session.feedback` has no `_batch_id` yet."""

    def _fn(self):
        from writ.session import feedback
        if not hasattr(feedback, "_batch_id"):
            pytest.fail("skeleton: writ.session.feedback has no _batch_id yet")
        return feedback._batch_id

    def test_matches_the_documented_sha256_formula(self) -> None:
        queue = [("SEC-BATCH-1", "positive"), ("SEC-BATCH-2", "negative")]
        got = self._fn()(SID, queue)
        assert got == _expected_batch_id(SID, queue)
        assert len(got) == 64
        int(got, 16)  # must be hex

    def test_a_permuted_queue_gives_the_same_id(self) -> None:
        queue = [("A", "positive"), ("B", "negative")]
        permuted = [("B", "negative"), ("A", "positive")]
        fn = self._fn()
        assert fn(SID, queue) == fn(SID, permuted)

    def test_a_different_session_gives_a_different_id(self) -> None:
        queue = [("A", "positive")]
        fn = self._fn()
        assert fn(SID, queue) != fn("a-completely-different-session", queue)

    def test_a_different_signal_gives_a_different_id(self) -> None:
        fn = self._fn()
        assert fn(SID, [("A", "positive")]) != fn(SID, [("A", "negative")])


class TestRequestCarriesTheComputedBatchId:
    def test_the_batch_request_carries_the_documented_batch_id(self, tmp_path) -> None:
        _RecordingBatchHandler.batch_status = 200
        _RecordingBatchHandler.batch_body = {
            "recorded": RULE_IDS, "not_found": [], "graduation_pending": [], "applied": 5,
        }
        server = _start(_RecordingBatchHandler)
        try:
            cache_dir = tmp_path / "cache"
            _seed_queue(cache_dir, RULE_IDS)
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            _run_auto_feedback(cache_dir, socket_path=_missing_socket(tmp_path), base_url=base_url)
            batch_reqs = [p for path, p in _RecordingBatchHandler.requests if path == "/feedback/batch"]
            assert len(batch_reqs) == 1, _RecordingBatchHandler.requests
            body = batch_reqs[0]
            batch_id = body.get("batch_id")
            assert isinstance(batch_id, str) and len(batch_id) == 64, body
            int(batch_id, 16)
            queue = [(s["rule_id"], s["signal"]) for s in body.get("signals", [])]
            assert batch_id == _expected_batch_id(SID, queue)
        finally:
            server.shutdown()


class TestLostBatchIsResentWithTheSameIdAndSignals:
    """Plan item B: a batch delivered-but-unanswered is resent on the NEXT run as
    exactly one POST carrying the same batch_id and signals; once answered, its ids
    move to feedback_sent, feedback_unconfirmed and feedback_pending_batches empty
    out, and the report shows resolved == 5. RED at HEAD: `_send_feedback` has no
    pending-batch resend at all."""

    def test_second_run_resends_exactly_one_post_matching_run_ones_batch(
        self, tmp_path
    ) -> None:
        silent = _SilentServer()
        cache_dir = tmp_path / "cache"
        try:
            _seed_queue(cache_dir, RULE_IDS)
            base_url = f"http://127.0.0.1:{silent.port}"
            _run_auto_feedback(cache_dir, socket_path=_missing_socket(tmp_path), base_url=base_url)
            cache_after_1 = _read_cache(cache_dir)
            assert sorted(cache_after_1.get("feedback_unconfirmed", [])) == sorted(RULE_IDS)
            pending = cache_after_1.get("feedback_pending_batches", [])
            assert len(pending) == 1, (
                f"run 1's lost batch must be stored for a later resend: {cache_after_1}"
            )
            batch_id = pending[0]["batch_id"]
            signals = pending[0]["signals"]
        finally:
            silent.close()

        _RecordingBatchHandler.batch_status = 200
        _RecordingBatchHandler.batch_body = {
            "recorded": RULE_IDS, "not_found": [], "graduation_pending": [], "applied": 5,
        }
        server = _start(_RecordingBatchHandler)
        try:
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            report2 = _run_auto_feedback(cache_dir, socket_path=_missing_socket(tmp_path),
                                         base_url=base_url)
            batch_reqs = [(path, body) for path, body in _RecordingBatchHandler.requests
                         if path == "/feedback/batch"]
            assert len(batch_reqs) == 1, _RecordingBatchHandler.requests
            resent_body = batch_reqs[0][1]
            assert resent_body.get("batch_id") == batch_id, (
                "the resend must carry the SAME batch_id run 1 computed"
            )
            resent_signals = sorted(
                (s["rule_id"], s["signal"]) for s in resent_body.get("signals", []))
            assert resent_signals == sorted(tuple(pair) for pair in signals), (
                "the resend must carry the SAME signals run 1 queued"
            )

            cache_after_2 = _read_cache(cache_dir)
            assert cache_after_2.get("feedback_unconfirmed", []) == []
            assert cache_after_2.get("feedback_pending_batches", []) == []
            assert sorted(cache_after_2.get("feedback_sent", [])) == sorted(RULE_IDS)
            assert report2.get("resolved") == len(RULE_IDS), report2
        finally:
            server.shutdown()


class TestResendErrorKeepsPendingAndALaterSuccessResolves:
    @pytest.mark.parametrize("status,body", [
        (500, {}), (422, {"detail": "bad"}), (200, {"error": "Database not connected."}),
    ])
    def test_an_error_response_to_the_resend_keeps_it_pending(
        self, tmp_path, status, body
    ) -> None:
        cache_dir = tmp_path / "cache"
        batch_id = "a" * 64
        signals = [(rid, "positive") for rid in RULE_IDS]
        _seed_pending_batch(cache_dir, batch_id=batch_id, signals=signals,
                            queued_at=time.time())

        _RecordingBatchHandler.batch_status = status
        _RecordingBatchHandler.batch_body = body
        server = _start(_RecordingBatchHandler)
        try:
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            _run_auto_feedback(cache_dir, socket_path=_missing_socket(tmp_path), base_url=base_url)
            cache = _read_cache(cache_dir)
            pending = cache.get("feedback_pending_batches", [])
            assert len(pending) == 1 and pending[0].get("batch_id") == batch_id, (
                f"an error response must keep the batch pending: {cache}"
            )
            assert sorted(cache.get("feedback_unconfirmed", [])) == sorted(RULE_IDS)
        finally:
            server.shutdown()

        _RecordingBatchHandler.batch_status = 200
        _RecordingBatchHandler.batch_body = {
            "recorded": RULE_IDS, "not_found": [], "graduation_pending": [], "applied": 5,
        }
        server2 = _start(_RecordingBatchHandler)
        try:
            base_url = f"http://127.0.0.1:{server2.server_address[1]}"
            _run_auto_feedback(cache_dir, socket_path=_missing_socket(tmp_path), base_url=base_url)
            cache = _read_cache(cache_dir)
            assert cache.get("feedback_pending_batches", []) == []
            assert cache.get("feedback_unconfirmed", []) == []
            assert sorted(cache.get("feedback_sent", [])) == sorted(RULE_IDS)
        finally:
            server2.shutdown()


class TestStalePendingBatchIsNotResent:
    """A pending batch whose queued_at is older than the client's resend age is
    dropped from the pending list without sending -- its own server record may
    already be pruned -- and its ids stay in feedback_unconfirmed rather than
    silently vanishing."""

    def test_a_batch_older_than_the_resend_age_sends_zero_requests(self, tmp_path) -> None:
        cache_dir = tmp_path / "cache"
        batch_id = "b" * 64
        signals = [(rid, "positive") for rid in RULE_IDS]
        eight_days_ago = time.time() - 8 * 86400
        _seed_pending_batch(cache_dir, batch_id=batch_id, signals=signals,
                            queued_at=eight_days_ago)

        _RecordingBatchHandler.batch_status = 200
        _RecordingBatchHandler.batch_body = {
            "recorded": RULE_IDS, "not_found": [], "graduation_pending": [], "applied": 5,
        }
        server = _start(_RecordingBatchHandler)
        try:
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            _run_auto_feedback(cache_dir, socket_path=_missing_socket(tmp_path), base_url=base_url)
            batch_reqs = [p for path, p in _RecordingBatchHandler.requests if path == "/feedback/batch"]
            assert len(batch_reqs) == 0, (
                f"a stale pending batch must not be resent: {_RecordingBatchHandler.requests}"
            )
            cache = _read_cache(cache_dir)
            assert sorted(cache.get("feedback_unconfirmed", [])) == sorted(RULE_IDS)
        finally:
            server.shutdown()


class TestLegacyUnconfirmedIdsWithNoStoredBatchAreNeverResent:
    """Ids that were unconfirmed BEFORE this change have no stored pending batch and
    must never be resent -- only reported -- because their exact signal composition
    cannot be recovered from a bare id list."""

    def test_legacy_unconfirmed_ids_send_no_request_and_stay_counted(self, tmp_path) -> None:
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        state = {
            "session_id": SID,
            "loaded_rule_ids": RULE_IDS,
            "analysis_results": {"a.py": "pass"},
            "feedback_sent": [],
            "feedback_unconfirmed": list(RULE_IDS),
            # deliberately no feedback_pending_batches key at all (pre-upgrade cache)
        }
        (cache_dir / f"writ-session-{SID}.json").write_text(json.dumps(state))

        _RecordingBatchHandler.batch_status = 200
        _RecordingBatchHandler.batch_body = {
            "recorded": [], "not_found": [], "graduation_pending": [], "applied": 0,
        }
        server = _start(_RecordingBatchHandler)
        try:
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            report = _run_auto_feedback(cache_dir, socket_path=_missing_socket(tmp_path),
                                        base_url=base_url)
            assert _RecordingBatchHandler.requests == [], (
                f"legacy unconfirmed ids with no stored batch must never be resent: "
                f"{_RecordingBatchHandler.requests}"
            )
            assert report.get("unconfirmed") == len(RULE_IDS), report
        finally:
            server.shutdown()


class TestClientResendMaxAgeIsBelowServerTtl:
    def test_seven_days_is_below_thirty(self) -> None:
        from writ.session import feedback
        if not hasattr(feedback, "FEEDBACK_RESEND_MAX_AGE_DAYS"):
            pytest.fail("skeleton: writ.session.feedback has no FEEDBACK_RESEND_MAX_AGE_DAYS yet")
        from writ.graph.db import rule_store
        if not hasattr(rule_store, "FEEDBACK_BATCH_TTL_DAYS"):
            pytest.fail("skeleton: writ.graph.db.rule_store has no FEEDBACK_BATCH_TTL_DAYS yet")
        assert feedback.FEEDBACK_RESEND_MAX_AGE_DAYS < rule_store.FEEDBACK_BATCH_TTL_DAYS


class TestDuplicateIdsAndAlreadySentAreHandled:
    def test_duplicates_collapse_to_one_signal_and_sent_rules_are_excluded(self, tmp_path) -> None:
        _RecordingBatchHandler.batch_status = 200
        _RecordingBatchHandler.batch_body = lambda payload: {
            "recorded": [s["rule_id"] for s in payload.get("signals", [])],
            "not_found": [], "graduation_pending": [], "applied": len(payload.get("signals", [])),
        }
        server = _start(_RecordingBatchHandler)
        try:
            cache_dir = tmp_path / "cache"
            rule_ids = ["SEC-DUP-1", "SEC-DUP-1", "SEC-DUP-2", "SEC-DUP-3"]
            _seed_queue(cache_dir, rule_ids, feedback_sent=["SEC-DUP-3"])
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            _run_auto_feedback(cache_dir, socket_path=_missing_socket(tmp_path), base_url=base_url)
            batch_reqs = [p for path, p in _RecordingBatchHandler.requests if path == "/feedback/batch"]
            assert len(batch_reqs) == 1
            sent_rule_ids = [s["rule_id"] for s in batch_reqs[0].get("signals", [])]
            assert sorted(sent_rule_ids) == ["SEC-DUP-1", "SEC-DUP-2"], sent_rule_ids
            assert len(sent_rule_ids) == len(set(sent_rule_ids)), "each rule id must appear once"
        finally:
            server.shutdown()
