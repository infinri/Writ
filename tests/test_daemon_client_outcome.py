"""Plan f7fc2b37-9a53-4011-a69f-e6b97f5e45fe, batch 4 (3b): `post_json_outcome`, the new
`bin/lib/writ_daemon_client.py` function that tells "could not connect" apart from "sent,
then got no answer".

WHY THIS EXISTS. `_request` (writ_daemon_client.py:71-109) falls from the unix socket to
TCP on ANY `OSError`, and a timeout raised AFTER `conn.request(...)` wrote the POST bytes
is indistinguishable from a timeout raised while connecting. `_send_feedback` in
`writ/session/feedback.py` (analysed in the plan, 3b) reads `status == 0` as "neither
transport answered" and therefore safe to retry, which double-POSTs a feedback batch that
was in fact delivered and merely slow to answer. `post_json_outcome` returns a third
element, `delivered`, computed from whether the request bytes were written before the
failure, so a caller can tell the three real outcomes apart:

  refused (delivered=False)      -- retry is safe, nothing happened server-side
  answered (status>0, delivered=True) -- the normal case
  delivered, no answer (status=0, delivered=True) -- must NOT be replayed over TCP

RED at HEAD: `writ_daemon_client.py` has no `post_json_outcome` yet (AttributeError on
every test below).

Per TEST-ISOLATE-002 / the E2b convention (tests/test_daemon_enforcement.py): AF_UNIX
paths cap near 107 bytes, so every socket lives under a short, fixed /tmp directory --
never pytest's `tmp_path`, which is long enough to blow the cap on some CI hosts -- and
`WRIT_SOCKET`/`WRIT_SESSION_BASE` are always passed EXPLICITLY as function arguments
(never left to their process-wide defaults), so this file can never reach the operator's
real socket at ~/.cache/writ/run/writ.sock or the real daemon at localhost:8765, both of
which are live on the machine this file was authored on.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from tests.fixtures.net import free_port

REPO = Path(__file__).resolve().parent.parent
_SOCK_DIR = "/tmp/writ-t-outcome"


def _client():
    lib = str(REPO / "bin" / "lib")
    if lib not in sys.path:
        sys.path.insert(0, lib)
    import writ_daemon_client  # noqa: PLC0415 -- deliberate late import, see module docstring

    return writ_daemon_client


def _require(mod, *names) -> None:
    missing = [n for n in names if not hasattr(mod, n)]
    if missing:
        pytest.fail(f"skeleton: bin/lib/writ_daemon_client.py has no {', '.join(missing)} yet")


@pytest.fixture()
def sock_dir():
    path = Path(_SOCK_DIR)
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _closed_tcp_port() -> int:
    """A port nothing is listening on: bind then immediately close."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _EchoHandler(BaseHTTPRequestHandler):
    """A normal, well-behaved daemon: answers every POST with a fixed 200 body."""

    def log_message(self, *a) -> None:  # noqa: N802 -- silence stderr noise
        pass

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0) or 0)
        self.rfile.read(length) if length else None
        body = json.dumps({"recorded": ["r1"]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def tcp_echo_stub():
    server = HTTPServer(("127.0.0.1", 0), _EchoHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()


class _SilentUnixServer:
    """Accepts a connection, reads the request fully (so the caller's write DID
    reach the peer), then closes without writing a response -- the "delivered,
    slow/dead peer" scenario `post_json_outcome` must report as delivered=True."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(path)
        self._sock.listen(1)
        self._thread = threading.Thread(target=self._serve_once, daemon=True)
        self._thread.start()

    def _serve_once(self) -> None:
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        try:
            conn.settimeout(2)
            # Read until the client is done writing (a short sleep covers the
            # single small POST these tests send; there is no wall-clock
            # assertion anywhere in this file, this is purely a read buffer drain).
            time.sleep(0.05)
            try:
                conn.recv(65536)
            except OSError:
                pass
        finally:
            conn.close()

    def close(self) -> None:
        self._sock.close()


@pytest.fixture()
def silent_unix_server(sock_dir):
    path = str(sock_dir / "s.sock")
    server = _SilentUnixServer(path)
    try:
        yield path
    finally:
        server.close()


class TestRefusedConnectionIsUndelivered:
    """Capability: `post_json_outcome` reports a refused connection (no socket file,
    TCP port closed) as `delivered=False`, status 0."""

    def test_no_socket_no_tcp_listener_is_undelivered(self, sock_dir) -> None:
        client = _client()
        _require(client, "post_json_outcome")
        missing_socket = str(sock_dir / "absent.sock")
        closed_port = _closed_tcp_port()
        status, text, delivered = client.post_json_outcome(
            "/feedback/batch", {"signals": []},
            socket_path=missing_socket,
            base_url=f"http://127.0.0.1:{closed_port}",
            timeout=1.0,
        )
        assert status == 0
        assert text == ""
        assert delivered is False


class TestNormalAnswerReturnsStatusAndBody:
    """Capability: a request that gets a real HTTP answer returns that status and
    body, and is reported delivered."""

    def test_a_200_from_the_tcp_stub_is_delivered(self, sock_dir, tcp_echo_stub) -> None:
        client = _client()
        _require(client, "post_json_outcome")
        missing_socket = str(sock_dir / "absent.sock")
        status, text, delivered = client.post_json_outcome(
            "/feedback/batch", {"signals": [{"rule_id": "R-1", "signal": "positive"}]},
            socket_path=missing_socket,
            base_url=f"http://127.0.0.1:{tcp_echo_stub}",
            timeout=1.0,
        )
        assert status == 200
        assert json.loads(text) == {"recorded": ["r1"]}
        assert delivered is True


class TestDeliveredWithNoAnswerIsNotReplayedOverTCP:
    """Capability: a request delivered over the unix socket with no answer is
    reported delivered (status 0), and the TCP stub receives ZERO requests --
    the double-count window `_send_feedback` must close (3b)."""

    def test_the_tcp_stub_receives_zero_requests(
        self, sock_dir, silent_unix_server, tcp_echo_stub
    ) -> None:
        client = _client()
        _require(client, "post_json_outcome")
        hits: list[str] = []
        real_do_post = _EchoHandler.do_POST

        def _counting_do_post(self) -> None:  # noqa: ANN001
            hits.append(self.path)
            real_do_post(self)

        _EchoHandler.do_POST = _counting_do_post
        try:
            status, text, delivered = client.post_json_outcome(
                "/feedback/batch", {"signals": [{"rule_id": "R-1", "signal": "positive"}]},
                socket_path=silent_unix_server,
                base_url=f"http://127.0.0.1:{tcp_echo_stub}",
                timeout=1.0,
            )
        finally:
            _EchoHandler.do_POST = real_do_post
        assert status == 0
        assert delivered is True, (
            "a request whose bytes reached the unix-socket peer must be reported "
            "delivered even though nothing answered"
        )
        assert hits == [], (
            f"a delivered-but-unanswered unix-socket request must never be replayed "
            f"over TCP: {hits}"
        )


class TestExistingCallersUnaffected:
    """Regression guard (must stay green): `post_json` / `get_json` / `_request` keep
    today's (status, text) shape and behaviour -- `post_json_outcome` is additive."""

    def test_post_json_signature_unchanged(self, sock_dir) -> None:
        client = _client()
        missing_socket = str(sock_dir / "absent.sock")
        closed_port = _closed_tcp_port()
        status, text = client.post_json(
            "/feedback", {"rule_id": "R-1", "signal": "positive"},
            socket_path=missing_socket, base_url=f"http://127.0.0.1:{closed_port}",
            timeout=1.0,
        )
        assert (status, text) == (0, "")

    def test_get_json_signature_unchanged(self, sock_dir) -> None:
        client = _client()
        missing_socket = str(sock_dir / "absent.sock")
        closed_port = _closed_tcp_port()
        status, text = client.get_json(
            "/health", socket_path=missing_socket,
            base_url=f"http://127.0.0.1:{closed_port}", timeout=1.0,
        )
        assert (status, text) == (0, "")
