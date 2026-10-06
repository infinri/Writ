"""Program item 2 (D1): CLI graph writes ask the daemon to reload, and never fail on it.

The notice is SILENT when no daemon socket exists (the common scripted case) and on a
successful reload; it prints one stderr line only when a socket exists and the reload did
not confirm. tests/conftest.py sets WRIT_DAEMON_RELOAD=0 for the whole suite so no CLI
write reaches the operator's real daemon socket; these tests delete it and point
WRIT_SOCKET at a short /tmp path. The end-to-end class needs the isolated graph:
    PYTHON=$(command -v python3) bash scripts/test-graph.sh up
    python3 -m pytest tests/test_cli_reload_notice.py -q
(never with WRIT_TEST_NO_ISOLATION=1).
"""
from __future__ import annotations

import asyncio
import contextlib
import http.server
import inspect
import json
import shutil
import socket
import socketserver
import tempfile
import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from writ import cli

GRAPH_WRITING_COMMANDS = (
    "add", "edit", "import_markdown", "review", "compress",
    "migrate", "reconcile", "prune", "feedback",
)
RID = "TEST-RELOAD-CLI-001"


@pytest.fixture()
def sock_dir(monkeypatch):
    """A short directory (AF_UNIX paths cap near 107 bytes) and the opt-out removed."""
    path = Path(tempfile.mkdtemp(prefix="wrl-", dir="/tmp"))
    monkeypatch.delenv("WRIT_DAEMON_RELOAD", raising=False)
    monkeypatch.setenv("WRIT_SOCKET", str(path / "w.sock"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


class _QuietUnixServer(socketserver.UnixStreamServer):
    def handle_error(self, request, client_address) -> None:
        pass


@contextlib.contextmanager
def _fake_daemon(sock_path: Path, body, delay: float = 0.0):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if delay:
                time.sleep(delay)
            payload = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args) -> None:
            pass

    server = _QuietUnixServer(str(sock_path), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()


class TestEveryGraphWritingCommandNotifies:
    @pytest.mark.parametrize("name", GRAPH_WRITING_COMMANDS)
    def test_the_command_calls_the_notifier(self, name: str) -> None:
        source = inspect.getsource(getattr(cli, name))
        assert "_notify_daemon_reload()" in source, f"writ {name} never asks the daemon to reload"


class TestTheNotice:
    def test_no_socket_is_silent(self, sock_dir, capsys) -> None:
        cli._notify_daemon_reload()
        assert capsys.readouterr().err == ""

    def test_a_confirmed_reload_is_silent(self, sock_dir, capsys) -> None:
        with _fake_daemon(sock_dir / "w.sock", {"status": "swapped", "generation": 2}):
            cli._notify_daemon_reload()
        assert capsys.readouterr().err == ""

    def test_a_failed_rebuild_prints_its_error(self, sock_dir, capsys) -> None:
        with _fake_daemon(sock_dir / "w.sock", {"status": "failed", "error": "boom"}):
            cli._notify_daemon_reload()
        err = capsys.readouterr().err
        assert "did not reload" in err and "boom" in err

    def test_a_socket_with_no_listener_prints_a_notice(self, sock_dir, capsys) -> None:
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(sock_dir / "w.sock"))
        stale.close()
        cli._notify_daemon_reload()
        assert "did not reload" in capsys.readouterr().err

    def test_the_wait_is_bounded(self, sock_dir, capsys, monkeypatch) -> None:
        assert cli.RELOAD_TIMEOUT_SECONDS <= 10, "the CLI must not wait long on the daemon"
        monkeypatch.setattr(cli, "RELOAD_TIMEOUT_SECONDS", 0.2)
        with _fake_daemon(sock_dir / "w.sock", {"status": "swapped"}, delay=1.0):
            started = time.monotonic()
            cli._notify_daemon_reload()
            elapsed = time.monotonic() - started
        assert elapsed < 1.0
        assert "did not answer" in capsys.readouterr().err

    def test_the_opt_out_skips_the_request(self, sock_dir, capsys, monkeypatch) -> None:
        monkeypatch.setenv("WRIT_DAEMON_RELOAD", "0")
        with _fake_daemon(sock_dir / "w.sock", {"status": "failed", "error": "boom"}):
            cli._notify_daemon_reload()
        assert capsys.readouterr().err == ""

    def test_a_config_error_never_raises(self, sock_dir, capsys, monkeypatch) -> None:
        def broken(*args, **kwargs):
            raise ValueError("unreadable config (simulated)")

        monkeypatch.setattr("writ.config.get_daemon_socket_path", broken)
        cli._notify_daemon_reload()
        assert "did not reload" in capsys.readouterr().err


@pytest.fixture()
def seeded():
    from tests._corpus import neo4j_reachable
    from tests._graph import connection

    if not neo4j_reachable():
        pytest.skip("isolated graph unreachable: PYTHON=$(command -v python3) bash scripts/test-graph.sh up")

    async def _put() -> None:
        db = connection()
        try:
            await db.create_rule({
                "rule_id": RID, "domain": "testing", "severity": "low", "scope": "file",
                "trigger": "When testing the CLI reload notice.", "statement": "s",
            }, source_origin="graph-authored")
        finally:
            await db.close()

    async def _drop() -> None:
        db = connection()
        try:
            await db.delete_rule(RID)
        finally:
            await db.close()

    asyncio.run(_put())
    yield RID
    asyncio.run(_drop())


class TestAgainstTheIsolatedGraph:
    def test_a_recorded_signal_with_no_daemon_exits_zero_and_prints_nothing_extra(
        self, seeded, sock_dir,
    ) -> None:
        result = CliRunner().invoke(cli.app, ["feedback", seeded, "positive"])
        assert result.exit_code == 0, result.output
        assert result.output.strip() == f"Recorded positive feedback for {seeded}"

    def test_an_unknown_rule_does_not_notify(self, seeded, monkeypatch) -> None:
        calls: list[int] = []
        monkeypatch.setattr(cli, "_notify_daemon_reload", lambda: calls.append(1))
        result = CliRunner().invoke(cli.app, ["feedback", "TEST-RELOAD-NO-SUCH-RULE", "positive"])
        assert result.exit_code == 1 and calls == []

    def test_a_dry_run_import_does_not_notify(self, seeded, monkeypatch, tmp_path) -> None:
        bible = tmp_path / "bible"
        bible.mkdir()
        calls: list[int] = []
        monkeypatch.setattr(cli, "_notify_daemon_reload", lambda: calls.append(1))
        result = CliRunner().invoke(
            cli.app, ["import-markdown", str(bible), "--dry-run", "--no-export"])
        assert calls == [], result.output
