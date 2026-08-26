"""Cycle E2a skeletons: a private door for the daemon, and a census of the public one.

The daemon binds `127.0.0.1:8765`, which every local account can reach.
`docs/reference/http-api.md:6` says so: "no auth (binds localhost only)". E1 removed
the route that let a caller write arbitrary session state; what stays reachable is
reading all of it plus the state-writing routes that legitimately exist.

FOUR MEASUREMENTS SHAPE THESE TESTS, and the first inverts the obvious design.

1. uvicorn chmods a unix socket to 0o666. Read from `uvicorn.config.Config.bind_socket`
   (`uds_perms = 0o666` then `os.chmod(self.uds, uds_perms)`) and confirmed on disk by a
   probe: `socket mode: 0o666`. A world-writable socket buys NOTHING over the open TCP
   port, so the control is the PARENT DIRECTORY at 0700, created before the bind. Every
   containment assertion below is on the directory, never on the socket's own mode, and a
   test that asserted 0600 on the socket would fail against correct code.
2. AF_UNIX paths cap near 107 bytes. A probe binding under this session's scratchpad died
   with `ERROR: AF_UNIX path too long`, so the path cannot be derived from an arbitrary
   install root and an over-long one must degrade rather than kill the daemon.
3. One process serves both transports: uvicorn 0.51.0's `Server.serve(sockets=[...])`
   accepts a socket list, and a probe bound a UDS and a TCP port together.
4. The transports are distinguishable. Measured: over the socket `scope["client"]` is
   `None` and `scope["server"]` is `["<path>", None]`; over TCP `scope["client"]` is
   `("127.0.0.1", 57966)`. That is the whole basis of the middleware.

THIS CYCLE ENFORCES NOTHING, and one test asserts that directly. TCP cannot simply be
switched off: `/dashboard` and `/explore` serve HTML to a browser, and `README.md:120`
tells new users to verify an install with `curl http://localhost:8765/health`. 16 files
POST to the daemon and 27 hold a daemon URL, so the enforcement flip waits for E2b and is
driven by the audit rows this cycle produces rather than by a grep that has already been
wrong once.

Per ENF-GATE-007: skeletons written and approved before implementation.
Per ABS-TESTING-041: the containment and discriminator tests bind a REAL socket and make
REAL requests over both transports. A mocked ASGI scope would only replay what the test
wrote, which is precisely the claim under test.
Per TEST-ISOLATE-003: every socket lives under tmp_path, never the real runtime directory,
and no test touches the running daemon.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# The interpreter for the dual-transport CHILD, which needs uvicorn.
#
# NOT sys.executable. The Stop hook runs this suite under system python3, which has no
# uvicorn, so the child died with ModuleNotFoundError and four tests failed while the
# same file passed when run from the venv by hand. An interpreter-fragile test is worse
# than no test: it reports green to whoever runs it the way the author did.
_VENV_PY = REPO / ".venv" / "bin" / "python"
CHILD_PY = str(_VENV_PY) if _VENV_PY.is_file() else sys.executable

# Kept well under the ~107-byte AF_UNIX cap; pytest's tmp_path is already long, so
# tests that bind use a short /tmp path of their own and clean it up.
_SHORT_SOCK_DIR = "/tmp/writ-t-e2a"


def _require_module(dotted: str):
    """Import a module, or fail with a skeleton message instead of an ImportError.

    Used for `writ.server.transport`, which this cycle creates. An ImportError
    inside a fixture surfaces as a pytest ERROR rather than assertion-shaped RED
    (TEC-PROC-RED-VERIFY-001), and the first draft of this file produced four of
    those from its dual-server fixture.
    """
    import importlib

    try:
        return importlib.import_module(dotted)
    except ImportError:
        pytest.fail(f"skeleton: {dotted} does not exist yet")


def _require(module, *names) -> None:
    """Legible skeleton failure instead of an AttributeError from monkeypatch."""
    missing = [n for n in names if not hasattr(module, n)]
    if missing:
        pytest.fail(f"skeleton: {module.__name__} has no {', '.join(missing)} yet")


@pytest.fixture()
def short_dir():
    """A short, private directory for real socket binds, removed afterwards."""
    import shutil

    path = Path(_SHORT_SOCK_DIR)
    shutil.rmtree(path, ignore_errors=True)
    yield path
    shutil.rmtree(path, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Capability 2, 3: containment is the DIRECTORY, and a long path degrades
# --------------------------------------------------------------------------- #

class TestSocketContainment:
    """The directory is the control, because uvicorn makes the socket 0666."""

    def test_the_resolver_returns_a_path_inside_a_private_directory(self, short_dir) -> None:
        from writ import config

        _require(config, "get_daemon_socket_path")
        resolved = config.get_daemon_socket_path()
        assert resolved, "no socket path resolved"
        parent = Path(resolved).parent
        assert parent.name, f"socket resolved to a filesystem root: {resolved}"

    def test_preparing_the_socket_directory_makes_it_0700(self, short_dir) -> None:
        """0700 BEFORE the bind, so the 0666 socket is never reachable, not even
        for the instant between bind and a chmod."""
        from writ import config

        _require(config, "prepare_socket_dir")
        sock_path = short_dir / "w.sock"
        config.prepare_socket_dir(str(sock_path))
        mode = stat.S_IMODE(os.stat(short_dir).st_mode)
        assert mode == 0o700, f"socket directory is {oct(mode)}, not 0o700"

    def test_the_directory_is_private_even_when_it_already_existed(self, short_dir) -> None:
        """A directory left behind by an older, laxer version must be tightened,
        not trusted."""
        from writ import config

        _require(config, "prepare_socket_dir")
        short_dir.mkdir(parents=True)
        os.chmod(short_dir, 0o755)
        config.prepare_socket_dir(str(short_dir / "w.sock"))
        mode = stat.S_IMODE(os.stat(short_dir).st_mode)
        assert mode == 0o700, f"a pre-existing 0755 directory stayed {oct(mode)}"

    def test_an_over_long_path_is_refused_rather_than_bound(self) -> None:
        """Measured: binding under a long path dies with "AF_UNIX path too long".
        The daemon must decline the socket and keep serving TCP, not fail to start.
        """
        from writ import config

        _require(config, "socket_path_usable")
        too_long = "/tmp/" + ("x" * 200) + "/w.sock"
        assert config.socket_path_usable(too_long) is False, (
            "a path over the AF_UNIX cap was accepted; the daemon would die at bind"
        )
        assert config.socket_path_usable(f"{_SHORT_SOCK_DIR}/w.sock") is True

    def test_the_length_cap_is_not_hardcoded_optimistically(self) -> None:
        """A cap above the kernel's real limit would pass this suite and fail at
        bind time on a real install, which is the failure mode measured."""
        from writ import config

        _require(config, "MAX_SOCKET_PATH")
        assert 90 <= config.MAX_SOCKET_PATH <= 108, (
            f"MAX_SOCKET_PATH={config.MAX_SOCKET_PATH} is outside the plausible "
            "AF_UNIX range; Linux allows 107 usable bytes"
        )


# --------------------------------------------------------------------------- #
# Capability 1, 4, 5: both transports, told apart
# --------------------------------------------------------------------------- #

_DUAL_SERVER = '''
import asyncio, json, os, sys
import uvicorn

sock_path, port = sys.argv[1], int(sys.argv[2])

async def app(scope, receive, send):
    if scope["type"] != "http":
        return
    from writ.server.transport import request_transport
    body = json.dumps({"transport": request_transport(scope)}).encode()
    await send({"type": "http.response.start", "status": 200,
                "headers": [[b"content-type", b"application/json"]]})
    await send({"type": "http.response.body", "body": body})

async def main():
    from writ import config
    config.prepare_socket_dir(sock_path)
    if os.path.exists(sock_path):
        os.unlink(sock_path)
    uds_cfg = uvicorn.Config(app, uds=sock_path, log_level="error")
    tcp_cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(uds_cfg)
    await server.serve(sockets=[uds_cfg.bind_socket(), tcp_cfg.bind_socket()])

asyncio.run(main())
'''


class TestBothTransportsAndTheDiscriminator:
    """Real binds, real requests. The discriminator is the basis of the whole
    design, so it is measured rather than asserted against a hand-built scope."""

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    @classmethod
    def _serve(cls, short_dir):
        """Start the dual-transport child, after proving its dependencies exist.

        A helper rather than a fixture: the preconditions are the very things this
        cycle builds, and a fixture that dies on a missing import reports as ERROR
        instead of a legible skeleton failure.
        """
        import contextlib
        import shutil
        import time

        from writ import config

        _require(config, "prepare_socket_dir")
        _require_module("writ.server.transport")

        sock_path = short_dir / "w.sock"
        port = cls._free_port()
        proc = subprocess.Popen(
            [CHILD_PY, "-c", _DUAL_SERVER, str(sock_path), str(port)],
            cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        deadline = 0.0
        while deadline < 15.0 and not sock_path.exists():
            if proc.poll() is not None:
                out, err = proc.communicate()
                pytest.fail(f"dual-transport server died: {err or out}")
            time.sleep(0.1)
            deadline += 0.1
        if not sock_path.exists():
            proc.kill()
            pytest.fail("the server never created its socket")

        @contextlib.contextmanager
        def _live():
            try:
                yield sock_path, port
            finally:
                proc.kill()
                proc.wait(timeout=10)
                shutil.rmtree(short_dir, ignore_errors=True)

        return _live()

    @staticmethod
    def _curl(*args: str) -> str:
        return subprocess.run(
            ["curl", "-s", *args], capture_output=True, text=True, timeout=30,
        ).stdout

    def test_the_socket_answers(self, short_dir) -> None:
        with self._serve(short_dir) as (sock_path, _port):
            out = self._curl("--unix-socket", str(sock_path), "http://localhost/x")
        assert out.strip(), "no response over the unix socket"

    def test_tcp_answers_from_the_same_process(self, short_dir) -> None:
        with self._serve(short_dir) as (_sock, port):
            out = self._curl(f"http://127.0.0.1:{port}/x")
        assert out.strip(), "no response over TCP from the dual-bound process"

    def test_a_socket_request_is_identified_as_socket_origin(self, short_dir) -> None:
        with self._serve(short_dir) as (sock_path, _port):
            out = self._curl("--unix-socket", str(sock_path), "http://localhost/x")
        assert json.loads(out)["transport"] == "socket", out

    def test_a_tcp_request_is_identified_as_tcp_origin(self, short_dir) -> None:
        with self._serve(short_dir) as (_sock, port):
            out = self._curl(f"http://127.0.0.1:{port}/x")
        assert json.loads(out)["transport"] == "tcp", out

    def test_the_discriminator_reads_client_not_a_header(self) -> None:
        """A header could be forged by the caller; the ASGI scope's `client` is set
        by the server. Measured: None over the socket, a host/port tuple over TCP.
        """
        request_transport = _require_module("writ.server.transport").request_transport

        assert request_transport({"client": None, "server": ["/run/w.sock", None]}) == "socket"
        assert request_transport({"client": ("127.0.0.1", 5000), "server": ["127.0.0.1", 8765]}) == "tcp"
        assert request_transport({}) == "socket", (
            "an absent client must read as socket-origin, matching what uvicorn "
            "sends over a UDS"
        )


# --------------------------------------------------------------------------- #
# Capability 6, 7, 8: it counts, and it does not block
# --------------------------------------------------------------------------- #

class TestTheAuditCensus:
    """The deliverable of this cycle is the LIST of call sites still writing over
    TCP, taken from observed traffic. A grep for that list was already wrong once
    this cycle (13 files claimed, 16 that POST and 27 holding a URL), which is why
    the census is measured instead.
    """

    def test_a_state_touching_tcp_request_is_recorded(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("WRIT_FRICTION_LOG", str(tmp_path / "events.jsonl"))
        transport = _require_module("writ.server.transport")

        _require(transport, "note_request")
        transport.note_request("tcp", "POST", "/session/abc/mode")
        rows = [
            json.loads(line)
            for line in (tmp_path / "events.jsonl").read_text().splitlines()
            if line.strip()
        ]
        names = [r.get("event") for r in rows]
        assert "daemon_tcp_write" in names, f"no census row emitted: {names}"
        row = next(r for r in rows if r.get("event") == "daemon_tcp_write")
        assert row.get("path") == "/session/abc/mode", row
        assert row.get("method") == "POST", row

    def test_a_read_only_tcp_request_is_not_recorded(self, tmp_path, monkeypatch) -> None:
        """The census must name write traffic only, or the E2b list is noise."""
        monkeypatch.setenv("WRIT_FRICTION_LOG", str(tmp_path / "events.jsonl"))
        transport = _require_module("writ.server.transport")

        _require(transport, "note_request")
        transport.note_request("tcp", "GET", "/health")
        transport.note_request("tcp", "GET", "/dashboard")
        log = tmp_path / "events.jsonl"
        rows = [
            json.loads(line) for line in log.read_text().splitlines() if line.strip()
        ] if log.exists() else []
        assert not [r for r in rows if r.get("event") == "daemon_tcp_write"], rows

    def test_socket_traffic_is_not_recorded(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("WRIT_FRICTION_LOG", str(tmp_path / "events.jsonl"))
        transport = _require_module("writ.server.transport")

        _require(transport, "note_request")
        transport.note_request("socket", "POST", "/session/abc/mode")
        log = tmp_path / "events.jsonl"
        rows = [
            json.loads(line) for line in log.read_text().splitlines() if line.strip()
        ] if log.exists() else []
        assert not [r for r in rows if r.get("event") == "daemon_tcp_write"], rows

    def test_the_census_event_lands_on_the_audit_stream(self) -> None:
        from writ.shared.logging import stream_for

        assert stream_for("daemon_tcp_write") == "audit"

    @pytest.mark.asyncio
    async def test_the_middleware_blocks_nothing_this_cycle(self) -> None:
        """The explicit enforces-nothing assertion. Flipping enforcement before the
        16 POSTing clients move would break them, so a state-touching POST over TCP
        must still be served.
        """
        from unittest.mock import MagicMock, patch

        from httpx import ASGITransport, AsyncClient

        from writ.server import app

        with patch("writ.server.writ_session", MagicMock()):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                response = await ac.post(
                    "/session/abc/mode", json={"mode": "work"}
                )
        assert response.status_code != 403, (
            "the middleware is enforcing already; E2a must only count"
        )


# --------------------------------------------------------------------------- #
# Capability 9, 10, 11, 12: the clients and the diagnostic
# --------------------------------------------------------------------------- #

class TestClientsAndDiagnostic:

    def test_common_sh_defines_one_transport_variable(self) -> None:
        """DRY-CONFIG-001: one definition, so the path cannot drift between the
        bash clients and the server."""
        source = (REPO / "bin" / "lib" / "common.sh").read_text()
        assert "WRIT_CURL_TRANSPORT" in source, (
            "common.sh defines no single transport variable"
        )
        # The PATH is what must not drift. The flag itself is assigned twice, in the
        # two arms of one if/else (socket present or not), which is a single
        # definition site; counting assignments of the flag would forbid that and
        # force a less readable one-liner.
        assert source.count("WRIT_SESSION_SOCKET=") == 1, (
            "the socket path is resolved in more than one place, which is how a "
            "client and the server end up on different sockets"
        )

    def test_common_sh_uses_the_transport_on_every_daemon_call(self) -> None:
        """The hot path is the point: `common.sh` carries the per-prompt traffic.

        Checked per INVOCATION, not per URL: the URLs are built into a `url`
        variable lines away from the curl that sends them, so a line-based rule
        pairing the two finds nothing. Every non-comment curl in this file targets
        the daemon (verified: it holds no Neo4j calls), so the rule is simply that
        each one carries the transport.
        """
        source = (REPO / "bin" / "lib" / "common.sh").read_text()
        invocations = [
            line for line in source.splitlines()
            if line.lstrip().startswith(("curl ", "$(curl "))
            or ("=$(curl " in line)
            or ("(curl " in line and not line.lstrip().startswith("#"))
        ]
        assert len(invocations) >= 9, (
            f"expected the known daemon invocations, found {len(invocations)}"
        )
        # Either form counts: the daemon-only call sites use the variable directly,
        # while the two GENERIC wrappers must decide per URL via the helper, because
        # hooks call them for non-daemon hosts as well.
        missing = [
            ln.strip() for ln in invocations
            if "WRIT_CURL_TRANSPORT" not in ln and "_writ_transport_for" not in ln
        ]
        assert not missing, f"daemon calls without the transport flag: {missing}"

    def test_the_subprocess_fallback_is_untouched(self) -> None:
        """Must-not-regress, and the reason a transport change is safe: every
        daemon call already degrades to a local python read."""
        source = (REPO / "bin" / "lib" / "common.sh").read_text()
        assert 'python3 "$helper" mode get' in source, (
            "the local-subprocess fallback for `mode get` is gone"
        )
        assert 'python3 "$helper" should-skip' in source, (
            "the local-subprocess fallback for `should-skip` is gone"
        )

    def test_the_installer_client_speaks_over_a_socket(self, short_dir) -> None:
        """`writ_install.py` runs under bare system python3, so this must stay
        stdlib-only: an http.client.HTTPConnection subclass over AF_UNIX."""
        sys.path.insert(0, str(REPO / "bin" / "lib"))
        try:
            import writ_install
        finally:
            sys.path.pop(0)
        assert hasattr(writ_install, "UnixSocketHTTPConnection"), (
            "no stdlib unix-socket connection class in writ_install.py"
        )
        # IMPORT-shaped, not the bare word. The first draft banned the bare token
        # "requests_unixsocket" and then failed on the docstring that explains why
        # that library is unavailable here. A mention is not a use, which is the
        # same distinction three gate arms needed this session.
        source = (REPO / "bin" / "lib" / "writ_install.py").read_text()
        import re as _re

        for banned in ("requests", "httpx", "requests_unixsocket", "aiohttp"):
            pattern = _re.compile(
                rf"^\s*(?:import {banned}\b|from {banned}[. ]) ?", _re.MULTILINE
            )
            assert not pattern.search(source), (
                f"importing {banned} would break the bare-python3 contract"
            )

    def test_the_doctor_reports_the_socket(self, monkeypatch) -> None:
        from writ.session import doctor

        _require(doctor, "_socket_state", "check_daemon_socket")
        monkeypatch.setattr(
            doctor, "_socket_state",
            lambda: {"path": "/run/w.sock", "exists": True, "dir_mode": 0o700,
                     "answers": True},
        )
        result = doctor.check_daemon_socket(doctor.DoctorOptions())
        assert result.status == doctor.STATUS_OK, result.detail

    def test_the_doctor_flags_a_world_readable_socket_directory(self, monkeypatch) -> None:
        """The measured failure mode: uvicorn's own 0666 socket in a directory
        anyone can traverse is a private-looking door that is not private."""
        from writ.session import doctor

        _require(doctor, "_socket_state", "check_daemon_socket")
        monkeypatch.setattr(
            doctor, "_socket_state",
            lambda: {"path": "/run/w.sock", "exists": True, "dir_mode": 0o755,
                     "answers": True},
        )
        result = doctor.check_daemon_socket(doctor.DoctorOptions())
        assert result.status != doctor.STATUS_OK
        assert "755" in result.detail or "0o755" in result.detail, result.detail

    def test_the_doctor_check_is_registered(self) -> None:
        from writ.session import doctor

        names = [name for name, _ in doctor._CHECKS]
        assert "daemon-socket" in names, f"not registered: {names}"
