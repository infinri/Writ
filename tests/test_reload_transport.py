"""Program item 2 (D1): the reload route is served over the unix socket only.

The middleware tests drive the REAL TransportCensusMiddleware with scopes shaped exactly
as the ASGI server sends them (client None over a unix socket, a host/port tuple over
TCP, tests/test_daemon_transport.py measured both). The real-daemon class at the bottom
proves the same against real sockets, because transport identity comes from the accepted
connection (ENF-SYS-005).
"""
from __future__ import annotations

import http.client
import json
import shutil
import tempfile
from pathlib import Path

import pytest

from writ.server.transport import SOCKET_ONLY_PATHS, TransportCensusMiddleware, tcp_refusal

RELOAD = "/retrieval/reload"


def _scope(client, method: str = "POST", path: str = RELOAD) -> dict:
    return {"type": "http", "client": client, "method": method, "path": path, "headers": []}


async def _drive(scope: dict) -> tuple[list[str], list[dict]]:
    reached: list[str] = []
    sent: list[dict] = []

    async def inner(scope, receive, send) -> None:
        reached.append(scope["path"])

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    await TransportCensusMiddleware(inner)(scope, receive, send)
    return reached, sent


class TestTheReloadRouteIsSocketOnly:
    def test_the_route_is_declared_socket_only(self) -> None:
        assert RELOAD in SOCKET_ONLY_PATHS

    @pytest.mark.asyncio
    @pytest.mark.parametrize("flag", [None, "1"])
    async def test_tcp_is_refused_whatever_the_readonly_flag_says(self, monkeypatch, flag) -> None:
        if flag is None:
            monkeypatch.delenv("WRIT_TCP_READONLY", raising=False)
        else:
            monkeypatch.setenv("WRIT_TCP_READONLY", flag)
        reached, sent = await _drive(_scope(("127.0.0.1", 5555)))
        assert reached == [], "the reload route was served over TCP"
        assert sent[0]["status"] == 403
        assert b"socket" in sent[1]["body"]

    @pytest.mark.asyncio
    async def test_the_socket_reaches_the_route(self, monkeypatch) -> None:
        monkeypatch.setenv("WRIT_TCP_READONLY", "1")
        reached, _sent = await _drive(_scope(None))
        assert reached == [RELOAD]

    def test_every_verb_is_refused_over_tcp(self, monkeypatch) -> None:
        monkeypatch.delenv("WRIT_TCP_READONLY", raising=False)
        for verb in ("GET", "HEAD", "POST", "PUT"):
            assert tcp_refusal(_scope(("127.0.0.1", 5555), method=verb)) is not None, verb

    def test_other_routes_keep_the_flag_off_behaviour(self, monkeypatch) -> None:
        monkeypatch.delenv("WRIT_TCP_READONLY", raising=False)
        assert tcp_refusal(_scope(("127.0.0.1", 5555), path="/session/abc/mode")) is None

    @pytest.mark.asyncio
    async def test_the_census_row_records_the_refusal(self, tmp_path, monkeypatch) -> None:
        log = tmp_path / "events.jsonl"
        monkeypatch.setenv("WRIT_FRICTION_LOG", str(log))
        monkeypatch.delenv("WRIT_TCP_READONLY", raising=False)
        await _drive(_scope(("127.0.0.1", 5555)))
        rows = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
        row = next(r for r in rows if r.get("event") == "daemon_tcp_write")
        assert row.get("path") == RELOAD and row.get("refused") is True, row


class TestAgainstARealDaemon:
    """Real sockets on a real daemon this test owns (tests/_daemon.py), isolated graph.

    Transport identity is decided by the server from the accepted connection, so only a
    real process can prove TCP is refused and the socket is served (ENF-SYS-005).
    """

    @pytest.fixture(scope="class")
    def daemon(self):
        from tests._daemon import start_isolated_daemon, stop_isolated_daemon

        root = Path(tempfile.mkdtemp(prefix="wrd-", dir="/tmp"))
        started = start_isolated_daemon(
            log_root=str(root / "logs"), socket_path=str(root / "w.sock"),
            cache_dir=str(root / "cache"), tcp_readonly=False,
        )
        if not started.get("started"):
            shutil.rmtree(root, ignore_errors=True)
            pytest.skip(started["reason"])
        yield started
        verdict = stop_isolated_daemon(started)
        shutil.rmtree(root, ignore_errors=True)
        assert verdict.get("stopped"), verdict

    def test_tcp_is_refused(self, daemon) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", daemon["port"], timeout=30)
        try:
            conn.request("POST", RELOAD, body=b"{}",
                         headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            body = response.read().decode()
        finally:
            conn.close()
        assert response.status == 403 and "socket" in body

    def test_the_socket_reloads(self, daemon) -> None:
        from writ.session.feedback import _daemon_client

        status, text, delivered = _daemon_client().post_json_outcome(
            RELOAD, {}, socket_path=daemon["socket_path"], timeout=120, tcp_fallback=False,
        )
        assert (status, delivered) == (200, True), text
        body = json.loads(text)
        assert body["status"] in ("swapped", "unchanged")
        assert isinstance(body["generation"], int) and body["generation"] >= 1

    def test_health_reports_the_generation(self, daemon) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", daemon["port"], timeout=30)
        try:
            conn.request("GET", "/health")
            health = json.loads(conn.getresponse().read())
        finally:
            conn.close()
        assert isinstance(health["retrieval"]["generation"], int)
        assert set(health["retrieval"]) == {"generation", "built_at", "reloading", "last_reload"}
