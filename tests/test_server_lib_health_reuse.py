"""Plan f7fc2b37-9a53-4011-a69f-e6b97f5e45fe, batch 5a(iii): writ_ensure_server's
non-realign already-running branch (scripts/lib/writ-server-lib.sh:92-98) re-fetches
/health with writ_http_get and parses cache_dir with a second `python3 -c`, even though
writ_server_health (32-40), called immediately before it, just fetched the exact same
body and threw it away (`>/dev/null`).

The fix: writ_server_health assigns the body it already fetched to `_WRIT_LAST_HEALTH`
(cleared on entry; the function's own return status is unchanged -- still
writ_http_get's / the injected probe's). The non-realign branch reads `cache_dir` out of
that variable through json_transform instead of a second writ_http_get plus python
parse. Both run inside the same flock subshell, so the variable set by the first call is
visible to the second read.

Per TEST-REGRESSION-001: the "exactly 1 GET /health" capability is RED at HEAD (today's
branch always re-fetches, for 2 total); the no-false-warning and writ_server_health
exit-status capabilities are regression guards, already GREEN at HEAD, and must stay
green once 5a(iii) lands. The realign branch (WRIT_REALIGN_CACHE=1, off by default) is
untouched by this plan and is not exercised here.

Per TEST-ISOLATE-002: every stub in this file is a throwaway http.server on an ephemeral
127.0.0.1 port, never the real Writ daemon and never the real port 8765; HOME and
WRIT_CACHE_DIR are always redirected under tmp_path; WRIT_LOG is pinned under tmp_path so
a "server already running" run never creates or appends to a real log file; and the
already-running stub always answers /health successfully, so `_writ_start_locked` never
reaches its `nohup ... writ serve` launch branch at all.
"""

from __future__ import annotations

import http.server
import json
import os
import shlex
import subprocess
import threading
from pathlib import Path

import pytest

from tests.fixtures.net import free_port as _free_port

REPO = Path(__file__).resolve().parent.parent
SERVER_LIB = REPO / "scripts" / "lib" / "writ-server-lib.sh"

_UNSET = object()  # sentinel: omit the `cache_dir` key from /health's JSON body entirely


class _HealthStubHandler(http.server.BaseHTTPRequestHandler):
    cache_dir_field = _UNSET
    raw_body: bytes | None = None
    hits: list[str] = []

    def do_GET(self):  # noqa: N802 (http.server API)
        if self.path == "/health":
            type(self).hits.append(self.path)
            if self.raw_body is not None:
                body = self.raw_body
            else:
                payload = {"status": "ok"}
                if self.cache_dir_field is not _UNSET:
                    payload["cache_dir"] = self.cache_dir_field
                body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, *args):  # silence stderr noise
        pass


class _health_stub:
    """A daemon stub answering /health only. `cache_dir_field` is `_UNSET` (the key is
    absent), a string (the key is present), or `raw_body` overrides the body entirely
    (a non-JSON response). Yields (port, hits) where `hits` records every GET /health
    the stub actually received."""

    def __init__(self, *, cache_dir_field=_UNSET, raw_body: bytes | None = None):
        self._cache_dir_field = cache_dir_field
        self._raw_body = raw_body
        self._server: http.server.HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.hits: list[str] = []

    def __enter__(self):
        handler = type("Handler", (_HealthStubHandler,), {
            "cache_dir_field": self._cache_dir_field, "raw_body": self._raw_body, "hits": self.hits,
        })
        self._server = http.server.HTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self._server.server_address[1], self.hits

    def __exit__(self, *exc):
        assert self._server is not None
        self._server.shutdown()


def _run_ensure_server(
    tmp_path: Path, port: int, *, cache_dir: str | None = None, extra_env: dict | None = None,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home")
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    env.pop("CLAUDE_PLUGIN_DATA", None)
    env["WRIT_HOST"] = "127.0.0.1"
    env["WRIT_PORT"] = str(port)
    env["WRIT_LOG"] = str(tmp_path / "server.log")
    env["WRIT_CACHE_DIR"] = cache_dir if cache_dir is not None else str(tmp_path / "cache")
    if extra_env:
        env.update(extra_env)
    cmd = f'source {shlex.quote(str(SERVER_LIB))}; writ_ensure_server'
    return subprocess.run(["bash", "-c", cmd], env=env, capture_output=True, text=True, timeout=20)


def _run_writ_server_health(
    tmp_path: Path, port: int, *, extra_env: dict | None = None,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home")
    env["WRIT_HOST"] = "127.0.0.1"
    env["WRIT_PORT"] = str(port)
    if extra_env:
        env.update(extra_env)
    cmd = f'source {shlex.quote(str(SERVER_LIB))}; writ_server_health'
    return subprocess.run(["bash", "-c", cmd], env=env, capture_output=True, text=True, timeout=20)


class TestAlreadyRunningReusesTheHealthBody:
    """Capability: writ_ensure_server against an already-running stub makes exactly 1
    GET /health (2 at HEAD: writ_server_health's own probe, thrown away, plus a SECOND
    fetch inside the non-realign already-running branch to read cache_dir), and still
    prints the cache-dir warning when the stub's cache_dir differs from WRIT_CACHE_DIR."""

    def test_exactly_one_health_request_on_a_cache_dir_mismatch(self, tmp_path):
        with _health_stub(cache_dir_field=str(tmp_path / "daemon-cache")) as (port, hits):
            result = _run_ensure_server(tmp_path, port, cache_dir=str(tmp_path / "our-cache"))
        assert result.returncode == 0, result.stderr
        assert len(hits) == 1, f"expected exactly one GET /health: {hits}"

    def test_cache_dir_mismatch_still_warns_and_still_reports_already_running(self, tmp_path):
        daemon_cache = str(tmp_path / "daemon-cache")
        our_cache = str(tmp_path / "our-cache")
        with _health_stub(cache_dir_field=daemon_cache) as (port, _hits):
            result = _run_ensure_server(tmp_path, port, cache_dir=our_cache)
        assert result.returncode == 0, result.stderr
        assert "reads session state from" in result.stderr, result.stderr
        assert daemon_cache in result.stderr, result.stderr
        assert "already running" in result.stderr.lower(), result.stderr


class TestAlreadyRunningNoFalseWarning:
    """Capability: writ_ensure_server against a stub whose cache_dir matches, or whose
    /health body has no cache_dir key or is not JSON, prints no cache-dir warning and
    still prints "Server already running". Regression guard: already GREEN at HEAD (the
    warning's own `[ -n "$running_nr" ]` / mismatch guards already cover these three
    shapes); 5a(iii) reads the SAME body through a different path and must not change
    which of these three shapes triggers the warning."""

    def test_matching_cache_dir_prints_no_warning(self, tmp_path):
        cache_dir = str(tmp_path / "cache")
        with _health_stub(cache_dir_field=cache_dir) as (port, _hits):
            result = _run_ensure_server(tmp_path, port, cache_dir=cache_dir)
        assert result.returncode == 0, result.stderr
        assert "reads session state from" not in result.stderr, result.stderr
        assert "already running" in result.stderr.lower(), result.stderr

    def test_missing_cache_dir_key_prints_no_warning(self, tmp_path):
        with _health_stub() as (port, _hits):  # cache_dir_field defaults to _UNSET
            result = _run_ensure_server(tmp_path, port, cache_dir=str(tmp_path / "cache"))
        assert result.returncode == 0, result.stderr
        assert "reads session state from" not in result.stderr, result.stderr
        assert "already running" in result.stderr.lower(), result.stderr

    def test_non_json_health_body_prints_no_warning(self, tmp_path):
        with _health_stub(raw_body=b"not json") as (port, _hits):
            result = _run_ensure_server(tmp_path, port, cache_dir=str(tmp_path / "cache"))
        assert result.returncode == 0, result.stderr
        assert "reads session state from" not in result.stderr, result.stderr
        assert "already running" in result.stderr.lower(), result.stderr


class TestWritServerHealthExitStatusUnchanged:
    """Capability: writ_server_health's exit status is unchanged: 0 against a healthy
    stub, non-zero against a closed port, with and without curl on PATH (WRIT_NO_CURL=1,
    the seam tests/test_no_tool_prereqs.py::TestWritServerHealthCurlAbsent already
    exercises for this same function). Regression guard: assigning `_WRIT_LAST_HEALTH`
    must not change the function's return value, which today is writ_http_get's own."""

    @pytest.mark.parametrize("no_curl", [False, True], ids=["curl", "no-curl"])
    def test_healthy_stub_exits_zero(self, tmp_path, no_curl):
        extra = {"WRIT_NO_CURL": "1"} if no_curl else None
        with _health_stub() as (port, _hits):
            result = _run_writ_server_health(tmp_path, port, extra_env=extra)
        assert result.returncode == 0, result.stderr

    @pytest.mark.parametrize("no_curl", [False, True], ids=["curl", "no-curl"])
    def test_closed_port_exits_nonzero(self, tmp_path, no_curl):
        closed_port = _free_port()  # bound then released; nothing listens on it
        extra = {"WRIT_NO_CURL": "1"} if no_curl else None
        result = _run_writ_server_health(tmp_path, closed_port, extra_env=extra)
        assert result.returncode != 0
