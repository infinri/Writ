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

            # A second run against the same silent peer must not re-send: the
            # unconfirmed ids are excluded from the next queue.
            _RecordingBatchHandler.requests = []
            report2 = _run_auto_feedback(cache_dir, socket_path=_missing_socket(tmp_path),
                                         base_url=base_url)
            assert report2.get("feedback_sent", 0) == 0
        finally:
            silent.close()


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
