"""Cycle E2b skeletons: make the private door reliable, then shut the public one.

E2a gave the daemon a unix socket beside its TCP port plus a census counting
state-touching writes still arriving over TCP. Reading that census and probing the
live daemon found three things that must be fixed BEFORE enforcement.

1. THE BIND ORDER I SHIPPED IN E2a CAN ORPHAN THE LIVE SOCKET, and it did on this
   machine. `serve` binds the socket before TCP, so a second `writ serve` unlinks
   the working socket, binds its own, fails on port 8765 (already in use) and
   exits, leaving a socket file with nothing behind it. Measured: the daemon logged
   `Listening on unix socket` at 15:01, the socket file's mtime was 15:39, and a
   connect returned errno 111 ECONNREFUSED while the same daemon still answered on
   TCP.
2. A STALE SOCKET SILENTLY CHANGES WHICH CODE ANSWERS. `common.sh` selects the
   socket on `[ -S "$path" ]`, which a stale file satisfies, so every daemon call
   returned curl's exit 7 and fell through to the local python subprocess.
   Correctness held (that fallback is the design) but the daemon went unused with
   nothing announcing it.
3. THE CENSUS NAMED ITS OWN DOMINANT SOURCE, AND IT IS NOT A CURL CLIENT.
   `writ-statusline.sh:77-88` POSTs `context-percent` from an embedded python
   `urllib` block, so E2a's `WRIT_CURL_TRANSPORT` never touched it: 65 of the first
   71 rows. No grep over curl call sites could have found a python client inside a
   shell script, which is why the cycle counted instead of estimating.

ENFORCEMENT SHIPS OFF. `WRIT_TCP_READONLY=1` turns it on, and the go signal is the
census reading ZERO state-touching TCP writes over a day of real use, not a belief
that the migration is complete. Two tests below pin the default-off behaviour.

Per ENF-GATE-007: skeletons written and approved before implementation.
Per ABS-TESTING-041: the bind-order and stale-socket tests use real processes and
real sockets, because both defects are about what happens between two processes.
Per TEST-ISOLATE-003: every socket is under a short /tmp path this module owns, and
`WRIT_SOCKET` is always overridden. NOTHING here touches the real daemon socket at
~/.cache/writ/run/writ.sock -- a test that bound it would orphan the running
daemon, which is defect 1 above, reproduced by accident.
"""
from __future__ import annotations

import errno
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# The child needs uvicorn, so it must NOT inherit sys.executable: the Stop hook runs
# this suite under system python3, where uvicorn is absent, and that made four E2a
# tests pass for me and fail for the hook.
_VENV_PY = REPO / ".venv" / "bin" / "python"
CHILD_PY = str(_VENV_PY) if _VENV_PY.is_file() else sys.executable

# Short, because AF_UNIX paths cap at 107 usable bytes and pytest's tmp_path is
# already long enough to exceed it.
_SOCK_DIR = "/tmp/writ-t-e2b"

READ_ONLY_TCP_ROUTES = ("/health", "/dashboard", "/explore", "/graph")


def _require(module, *names) -> None:
    missing = [n for n in names if not hasattr(module, n)]
    if missing:
        pytest.fail(f"skeleton: {module.__name__} has no {', '.join(missing)} yet")


def _require_module(dotted: str):
    import importlib

    try:
        return importlib.import_module(dotted)
    except ImportError:
        pytest.fail(f"skeleton: {dotted} does not exist yet")


@pytest.fixture()
def sock_dir():
    import shutil

    path = Path(_SOCK_DIR)
    shutil.rmtree(path, ignore_errors=True)
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _connect_errno(path: str) -> int | None:
    """None when the socket accepts a connection, else the errno."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2)
    try:
        s.connect(path)
        return None
    except OSError as exc:
        return exc.errno
    finally:
        s.close()


# --------------------------------------------------------------------------- #
# Capability 1, 2, 3: a duplicate start cannot orphan the live socket
# --------------------------------------------------------------------------- #

class TestBindOrderProtectsTheLiveSocket:
    """Real processes, because the defect only exists between two of them.

    The first `writ serve` holds both. The second must fail WITHOUT leaving the
    first's socket dead, which is exactly what happened on this machine.
    """

    @staticmethod
    def _serve(sock_path: Path, port: int) -> subprocess.Popen:
        env = dict(os.environ)
        env["WRIT_SOCKET"] = str(sock_path)
        return subprocess.Popen(
            [CHILD_PY, "-m", "writ.cli", "serve", "--port", str(port),
             "--host", "127.0.0.1"],
            cwd=str(REPO), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def _wait_for_socket(self, proc, sock_path: Path, limit: float = 60.0) -> None:
        waited = 0.0
        while waited < limit:
            if sock_path.exists() and _connect_errno(str(sock_path)) is None:
                return
            if proc.poll() is not None:
                out, err = proc.communicate()
                pytest.fail(f"daemon died before serving: {err or out}")
            time.sleep(0.25)
            waited += 0.25
        pytest.fail(f"daemon never served {sock_path}")

    def test_a_second_serve_leaves_the_first_socket_alive(self, sock_dir) -> None:
        """THE REGRESSION. Before the fix the second start unlinked the live socket
        and then died on the port, leaving errno 111 behind it."""
        sock_path = sock_dir / "w.sock"
        port = _free_port()
        first = self._serve(sock_path, port)
        try:
            self._wait_for_socket(first, sock_path)
            second = self._serve(sock_path, port)
            second.wait(timeout=60)
            assert second.returncode != 0, (
                "a second serve on a taken port exited 0; it should fail"
            )
            assert _connect_errno(str(sock_path)) is None, (
                "the second start orphaned the first daemon's socket "
                f"(errno {_connect_errno(str(sock_path))})"
            )
        finally:
            first.kill()
            first.wait(timeout=15)

    def test_a_live_socket_is_never_unlinked(self, sock_dir) -> None:
        """The inode, not just the path: an unlink-then-rebind would leave a
        connectable socket while silently dropping the first daemon's clients."""
        sock_path = sock_dir / "w.sock"
        port = _free_port()
        first = self._serve(sock_path, port)
        try:
            self._wait_for_socket(first, sock_path)
            before = os.stat(sock_path).st_ino
            second = self._serve(sock_path, port)
            second.wait(timeout=60)
            assert os.stat(sock_path).st_ino == before, (
                "the socket inode changed, so a live socket was replaced"
            )
        finally:
            first.kill()
            first.wait(timeout=15)

    def test_an_unusable_socket_path_still_leaves_tcp_serving(self, sock_dir) -> None:
        """CLEAN-ERR-001: a transport upgrade must never stop the daemon starting.
        An over-long path is the measured case (AF_UNIX path too long)."""
        port = _free_port()
        env = dict(os.environ)
        env["WRIT_SOCKET"] = "/tmp/" + ("x" * 200) + "/w.sock"
        proc = subprocess.Popen(
            [CHILD_PY, "-m", "writ.cli", "serve", "--port", str(port),
             "--host", "127.0.0.1"],
            cwd=str(REPO), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            waited = 0.0
            answered = False
            while waited < 60.0 and not answered:
                if proc.poll() is not None:
                    out, err = proc.communicate()
                    pytest.fail(f"daemon died on an unusable socket path: {err or out}")
                out = subprocess.run(
                    ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                     f"http://127.0.0.1:{port}/health"],
                    capture_output=True, text=True, timeout=10,
                ).stdout
                answered = out.strip() == "200"
                if not answered:
                    time.sleep(0.5)
                    waited += 0.5
            assert answered, "TCP never answered with the socket path unusable"
        finally:
            proc.kill()
            proc.wait(timeout=15)


# --------------------------------------------------------------------------- #
# Capability 4, 5: a stale socket falls back to TCP, not to the subprocess
# --------------------------------------------------------------------------- #

class TestStaleSocketFallsBackToTcp:
    """A stale socket file satisfies `[ -S ]`, which is how the daemon went unused
    with nothing announcing it. The fallback order matters: TCP keeps the daemon
    answering, while the local subprocess is a different code path.

    TWO THINGS THIS CLASS HAD TO WORK AROUND, both found by running it.

    First, `tests/conftest.py:17` sets `WRIT_PORT` globally, on purpose, to keep the
    suite off the interactive 8765 daemon. E2a's override guard reads any explicit
    `WRIT_PORT` as "the caller named an endpoint" and disables the socket, so NO test
    can reach the socket through `common.sh` while that is set. The first draft of
    this test therefore passed without touching a socket at all: the call went
    straight to TCP and returned 200.

    Second, the guard has to be refined for that reason: `WRIT_PORT` alone still
    means TCP, but `WRIT_SOCKET` set TOGETHER with it means "use this socket, with
    that port as the fallback". That is what makes the retry testable without a
    daemon on 8765, and it is a deliberate design change, not a test workaround.

    The fix under test is a per-call RETRY, not a startup probe. Probing the socket
    when `common.sh` is sourced would add an HTTP round trip or a python start to
    every hook; retrying only on curl's exit 7 costs nothing when the socket is
    healthy.
    """

    @staticmethod
    def _tcp_server(port: int) -> subprocess.Popen:
        return subprocess.Popen(
            [CHILD_PY, "-c", _ONE_SHOT_TCP_SERVER, str(port)],
            cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def _stale_socket(self, sock_dir: Path) -> Path:
        sock_dir.mkdir(parents=True, exist_ok=True)
        stale = sock_dir / "w.sock"
        if stale.exists():
            stale.unlink()
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(stale))
        s.close()
        assert _connect_errno(str(stale)) == errno.ECONNREFUSED, (
            "the fixture did not reproduce a stale socket"
        )
        return stale

    def test_an_explicit_socket_is_selected_even_with_a_port_set(self, sock_dir) -> None:
        """The guard refinement, pinned: both set means use the socket.

        Without this, the suite's global `WRIT_PORT` makes every socket path
        unreachable from a test, which is how the first draft of this class proved
        nothing.
        """
        stale = self._stale_socket(sock_dir)
        result = subprocess.run(
            ["bash", "-c", 'source bin/lib/common.sh; printf "%s" "$WRIT_CURL_TRANSPORT"'],
            cwd=str(REPO), capture_output=True, text=True, timeout=60,
            env={**os.environ, "WRIT_SOCKET": str(stale), "WRIT_PORT": "8765"},
        )
        assert str(stale) in result.stdout, (
            f"an explicit WRIT_SOCKET was ignored because WRIT_PORT was set: "
            f"{result.stdout!r}"
        )

    def test_a_port_alone_still_means_tcp(self, sock_dir) -> None:
        """Must-not-regress from E2a: a caller naming only a port gets TCP, or a
        test pointing at a fake daemon is silently redirected to the real one."""
        result = subprocess.run(
            ["bash", "-c", 'source bin/lib/common.sh; printf "%s" "$WRIT_CURL_TRANSPORT"'],
            cwd=str(REPO), capture_output=True, text=True, timeout=60,
            env={k: v for k, v in os.environ.items() if k != "WRIT_SOCKET"}
                | {"WRIT_PORT": "19999"},
        )
        assert result.stdout.strip() == "", (
            f"a bare WRIT_PORT still selected a socket: {result.stdout!r}"
        )

    def test_a_stale_socket_retries_over_tcp(self, sock_dir) -> None:
        """THE REGRESSION, end to end against a real local server.

        Before the fix every call over a stale socket returned curl's exit 7 and
        fell through to the local python subprocess, so the daemon went unused.
        """
        stale = self._stale_socket(sock_dir)
        port = _free_port()
        server = self._tcp_server(port)
        try:
            waited = 0.0
            while waited < 20.0:
                probe = subprocess.run(
                    ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                     f"http://127.0.0.1:{port}/health"],
                    capture_output=True, text=True, timeout=10,
                ).stdout.strip()
                if probe == "200":
                    break
                time.sleep(0.25)
                waited += 0.25
            else:
                pytest.fail("the probe TCP server never answered")

            result = subprocess.run(
                ["bash", "-c",
                 f'source bin/lib/common.sh; writ_http_get "http://localhost:{port}/health"'],
                cwd=str(REPO), capture_output=True, text=True, timeout=60,
                env={**os.environ, "WRIT_SOCKET": str(stale), "WRIT_PORT": str(port)},
            )
            assert result.returncode == 0, (
                f"a stale socket broke a daemon read instead of retrying over TCP: "
                f"rc={result.returncode} stderr={result.stderr[:200]}"
            )
            assert "ok" in result.stdout, result.stdout[:200]
        finally:
            server.kill()
            server.wait(timeout=10)

    def test_the_subprocess_fallback_still_exists(self) -> None:
        """Must-not-regress: TCP is inserted BEFORE the subprocess, not instead of
        it. With neither transport answering, the local read is still the answer."""
        source = (REPO / "bin" / "lib" / "common.sh").read_text()
        assert 'python3 "$helper" mode get' in source
        assert 'python3 "$helper" should-skip' in source


_ONE_SHOT_TCP_SERVER = '''
import http.server, sys

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass

http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
'''


# --------------------------------------------------------------------------- #
# Capability 6, 7, 8, 9: the python clients move
# --------------------------------------------------------------------------- #

class TestPythonClientsUseTheSocket:
    """Three call sites, one shared stdlib client (DRY-DUP-001).

    `writ_install.py` proved the AF_UNIX connection class in E2a; this is that plus
    the TCP fallback, in one importable place, because the callers are a shell
    script's inline python and two modules with hardcoded URLs.
    """

    def test_the_shared_client_exists_and_is_stdlib_only(self) -> None:
        sys.path.insert(0, str(REPO / "bin" / "lib"))
        try:
            client = _require_module("writ_daemon_client")
        finally:
            sys.path.pop(0)
        _require(client, "post_json", "get_json")
        import re

        source = (REPO / "bin" / "lib" / "writ_daemon_client.py").read_text()
        for banned in ("requests", "httpx", "aiohttp"):
            pattern = re.compile(rf"^\s*(?:import {banned}\b|from {banned}[. ])", re.M)
            assert not pattern.search(source), (
                f"importing {banned} breaks the bare-python3 contract"
            )

    def test_the_client_prefers_a_live_socket(self, sock_dir) -> None:
        """A one-request server on a real socket: the client must reach it rather
        than the TCP port, which is not listening at all here."""
        sock_dir.mkdir(parents=True, exist_ok=True)
        sock_path = sock_dir / "w.sock"
        server = subprocess.Popen(
            [CHILD_PY, "-c", _ONE_SHOT_SOCKET_SERVER, str(sock_path)],
            cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            waited = 0.0
            while waited < 20.0 and _connect_errno(str(sock_path)) is not None:
                time.sleep(0.25)
                waited += 0.25
            assert _connect_errno(str(sock_path)) is None, "the probe server never bound"

            sys.path.insert(0, str(REPO / "bin" / "lib"))
            try:
                client = _require_module("writ_daemon_client")
                status, body = client.post_json(
                    "/session/probe/context-percent", {"context_percent": 7},
                    socket_path=str(sock_path), base_url="http://127.0.0.1:1/",
                )
            finally:
                sys.path.pop(0)
            assert status == 200, (status, body)
            assert "socket" in body, body
        finally:
            server.kill()
            server.wait(timeout=10)

    def test_the_client_falls_back_to_tcp_when_the_socket_is_absent(self) -> None:
        """No socket at all is the ordinary state on a machine that has not
        restarted the daemon yet, so it must not be an error."""
        sys.path.insert(0, str(REPO / "bin" / "lib"))
        try:
            client = _require_module("writ_daemon_client")
            status, _body = client.get_json(
                "/health", socket_path="/tmp/writ-t-e2b/absent.sock",
                base_url="http://localhost:8765",
            )
        finally:
            sys.path.pop(0)
        assert status == 200, "the client did not fall back to the live TCP daemon"

    def test_the_statusline_no_longer_builds_its_own_request(self) -> None:
        """The census's dominant source: 65 of the first 71 rows came from this
        inline urllib POST, which E2a's curl-side transport could not reach."""
        source = (REPO / "hooks" / "scripts" / "writ-statusline.sh").read_text()
        assert "writ_daemon_client" in source, (
            "the statusline still posts context-percent through its own request"
        )
        assert "urllib.request.Request(" not in source, (
            "an inline urllib request survives in the statusline"
        )

    def test_the_statusline_still_renders_a_percentage(self) -> None:
        """RUNS the hook, because reading its source could not catch what broke it.

        Two defects in one edit, both invisible to `bash -n` and to a grep:
        WRIT_DIR is a shell variable rather than an exported one, so the embedded
        python read it as unset; and a comment placed BETWEEN two line-continuation
        lines detached the whole env chain from `python3`, so STATUSLINE_STDIN never
        arrived either. The bar rendered "Writ ctx --" and posted nothing. Valid
        syntax, wrong program.

        Asserting on the rendered percentage is the cheapest signal that the env
        chain, the import and the extraction all still work together.
        """
        result = subprocess.run(
            ["bash", str(REPO / "hooks" / "scripts" / "writ-statusline.sh")],
            input='{"session_id":"e2b-render","context_window":{"used_percentage":33}}',
            cwd=str(REPO), capture_output=True, text=True, timeout=60,
            env={**os.environ, "WRIT_DIR": str(REPO)},
        )
        assert "33%" in result.stdout, (
            f"the status bar did not render the percentage: {result.stdout!r} "
            f"stderr={result.stderr[:200]}"
        )
        assert "--" not in result.stdout, (
            f"the bar fell back to its no-data render: {result.stdout!r}"
        )

    def test_the_two_hardcoded_feedback_urls_are_gone(self) -> None:
        for rel in ("bin/lib/writ_send_escalation_feedback.py",
                    "writ/session/feedback.py"):
            source = (REPO / rel).read_text()
            assert "writ_daemon_client" in source or "daemon_client" in source, (
                f"{rel} still builds its own request"
            )


_ONE_SHOT_SOCKET_SERVER = '''
import http.server, socketserver, sys, os

class H(http.server.BaseHTTPRequestHandler):
    def _reply(self):
        body = b'{"via": "socket"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        self._reply()
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self._reply()
    def log_message(self, *a):
        pass

class S(socketserver.ThreadingUnixStreamServer):
    allow_reuse_address = True

path = sys.argv[1]
if os.path.exists(path):
    os.unlink(path)
with S(path, H) as srv:
    srv.serve_forever()
'''


# --------------------------------------------------------------------------- #
# Capability 10 to 14: enforcement, shipped OFF
# --------------------------------------------------------------------------- #

class TestEnforcementIsOptIn:
    """The flag exists, the default is off, and the user flips it when the census
    reads zero. Turning it on before the clients moved would break them.
    """

    @staticmethod
    def _scope(transport: str, method: str, path: str) -> dict:
        client = None if transport == "socket" else ("127.0.0.1", 5555)
        return {"type": "http", "client": client, "method": method, "path": path}

    def test_the_default_allows_a_state_touching_tcp_request(self, monkeypatch) -> None:
        transport = _require_module("writ.server.transport")

        _require(transport, "tcp_refusal")
        monkeypatch.delenv("WRIT_TCP_READONLY", raising=False)
        assert transport.tcp_refusal(
            self._scope("tcp", "POST", "/session/abc/mode")
        ) is None, "enforcement is on by default; it must ship off"

    def test_enabled_it_refuses_a_state_touching_tcp_request(self, monkeypatch) -> None:
        transport = _require_module("writ.server.transport")

        _require(transport, "tcp_refusal")
        monkeypatch.setenv("WRIT_TCP_READONLY", "1")
        refusal = transport.tcp_refusal(self._scope("tcp", "POST", "/session/abc/mode"))
        assert refusal is not None, "a TCP write was allowed with enforcement on"
        assert "socket" in str(refusal).lower(), refusal

    @pytest.mark.parametrize("path", READ_ONLY_TCP_ROUTES)
    def test_enabled_it_still_serves_the_browser_surface(self, monkeypatch, path) -> None:
        """README.md:120 tells new users to curl /health, and a browser cannot open
        a unix socket, so these five stay on TCP."""
        transport = _require_module("writ.server.transport")

        _require(transport, "tcp_refusal")
        monkeypatch.setenv("WRIT_TCP_READONLY", "1")
        assert transport.tcp_refusal(self._scope("tcp", "GET", path)) is None, (
            f"{path} was refused over TCP; it is on the read-only allowlist"
        )

    def test_enabled_it_still_serves_a_node_detail_page(self, monkeypatch) -> None:
        transport = _require_module("writ.server.transport")

        _require(transport, "tcp_refusal")
        monkeypatch.setenv("WRIT_TCP_READONLY", "1")
        assert transport.tcp_refusal(
            self._scope("tcp", "GET", "/node/ENF-GATE-007")
        ) is None

    def test_enabled_the_socket_still_serves_everything(self, monkeypatch) -> None:
        """The point of the flag: state routes move to the socket, they do not
        disappear."""
        transport = _require_module("writ.server.transport")

        _require(transport, "tcp_refusal")
        monkeypatch.setenv("WRIT_TCP_READONLY", "1")
        for method, path in (
            ("POST", "/session/abc/mode"),
            ("POST", "/session/abc/context-percent"),
            ("POST", "/query"),
            ("GET", "/health"),
        ):
            assert transport.tcp_refusal(self._scope("socket", method, path)) is None, (
                f"{method} {path} was refused over the socket"
            )

    def test_a_read_only_tcp_request_outside_the_allowlist_is_still_read(
        self, monkeypatch
    ) -> None:
        """A GET is not state-touching, so it is not the thing being closed. The
        allowlist bounds what TCP may reach; the verb bounds what it may change."""
        transport = _require_module("writ.server.transport")

        _require(transport, "tcp_refusal")
        monkeypatch.setenv("WRIT_TCP_READONLY", "1")
        refusal = transport.tcp_refusal(self._scope("tcp", "GET", "/session/abc/mode"))
        assert refusal is None or "socket" in str(refusal).lower()

    def test_the_doctor_reports_whether_enforcement_is_on(self, monkeypatch) -> None:
        from writ.session import doctor

        _require(doctor, "_socket_state", "check_daemon_socket")
        monkeypatch.setattr(
            doctor, "_socket_state",
            lambda: {"path": "/run/w.sock", "exists": True, "dir_mode": 0o700,
                     "answers": True, "tcp_readonly": True},
        )
        result = doctor.check_daemon_socket(doctor.DoctorOptions())
        assert "read-only" in result.detail.lower() or "readonly" in result.detail.lower(), (
            f"the socket check does not say whether TCP is restricted: {result.detail}"
        )
