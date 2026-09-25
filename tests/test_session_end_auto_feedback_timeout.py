"""Plan f7fc2b37-9a53-4011-a69f-e6b97f5e45fe, batch 3a: SessionEnd must stop running
auto-feedback twice when the daemon is reachable but slow.

Today `_writ_session auto-feedback` (bin/lib/common.sh, the "auto-feedback" case arm
plus the shared curl/fallback tail at 2280-2298) treats EVERY curl failure the same way:
an empty result falls back to `python3 writ-session.py auto-feedback`, which re-runs the
whole per-rule /feedback POST loop while the daemon's own `asyncio.to_thread` correlation
is still running. Plan capabilities pin: curl exit 28 (operation timed out) must skip the
local fallback and print a one-line JSON skip marker instead; curl exit 7 (connection
refused) and curl exit 22 (-f on an HTTP error) must still fall back exactly as today;
and the `coverage` subcommand -- a read-only computation with no side effect to
duplicate -- must never gain the new timeout-skip behaviour.

Per TEST-ISOLATE-002 / the plan's own isolation warning (## Analysis, 3a, last
paragraph): `_send_feedback` (writ/session/feedback.py) reaches the operator's real
daemon through `bin/lib/writ_daemon_client.py`, which reads WRIT_SOCKET and
WRIT_SESSION_BASE straight from the environment (no common.sh export). Every test here
pins WRIT_SOCKET to a path that cannot exist under tmp_path and WRIT_SESSION_BASE (in
addition to WRIT_HOST/WRIT_PORT, which is what common.sh's `_writ_session` itself reads)
at the stub's own host:port, so a fallback that DOES run can only ever reach the stub.

Per TEST-REGRESSION-001: the daemon_timeout capability is RED at HEAD (today's shared
tail has no rc-28 special case, so the fallback always runs and always double-POSTs);
the rc-7, rc-22 and `coverage` capabilities are regression guards, already GREEN at HEAD
and must stay green once the new arm lands.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from tests.fixtures.net import free_port as _free_port
from tests.fixtures.session_state import sandbox_cwd  # noqa: F401 -- autouse, protects real gate state

REPO = Path(__file__).resolve().parent.parent
COMMON_SH = REPO / "bin" / "lib" / "common.sh"
SESSION_END_HOOK = REPO / "hooks" / "scripts" / "writ-session-end.sh"
SESSION_HELPER = REPO / "bin" / "lib" / "writ-session.py"

# How long the stub sleeps before answering auto-feedback, when it is answering slow.
# Comfortably above curl's `--max-time 0.5` (common.sh:2283-2288) so the client always
# times out (rc 28), and short enough that the suite is not slow.
_SLOW_SECONDS = 1.0


class _AutoFeedbackStub(BaseHTTPRequestHandler):
    """Answers the three routes this batch's fallback path can reach.

    `/session/<sid>/auto-feedback` -- POST, sleeps `slow_seconds` (0 for an immediate
    500, used by the rc-22 case) then answers with `status_code`.
    `/session/<sid>/coverage` -- GET, ALSO sleeps `slow_seconds` before answering: the
    coverage capability is that a daemon slow enough to make curl time out (rc 28,
    the exact condition auto-feedback now special-cases) still falls back for
    coverage, so its stub must reproduce that same condition, not just answer fast.
    `/feedback` -- POST, recorded so a test can assert the local fallback's per-rule
    loop actually reached (or never reached) the daemon.
    """

    slow_seconds: float = 0.0
    status_code: int = 200
    feedback_posts: list[dict] = []

    def log_message(self, *args):  # silence stderr noise
        pass

    def do_GET(self):  # noqa: N802 (http.server API)
        if self.path.endswith("/coverage"):
            if _AutoFeedbackStub.slow_seconds:
                time.sleep(_AutoFeedbackStub.slow_seconds)
            self._ok(b'{"coverage": 1.0}')
        else:
            self.send_error(404)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        if self.path.endswith("/auto-feedback"):
            if _AutoFeedbackStub.slow_seconds:
                time.sleep(_AutoFeedbackStub.slow_seconds)
            if _AutoFeedbackStub.status_code >= 400:
                self.send_error(_AutoFeedbackStub.status_code)
                return
            self._ok(b'{"feedback_sent": 0, "positive": 0, "negative": 0, "skipped_already_sent": 0}')
        elif self.path == "/feedback":
            try:
                _AutoFeedbackStub.feedback_posts.append(json.loads(raw))
            except Exception:
                _AutoFeedbackStub.feedback_posts.append({"_unparseable": raw.decode("utf-8", "replace")})
            self._ok(b'{"ok": true}')
        else:
            self.send_error(404)

    def _ok(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def stub(request):
    """A daemon stub on a free port. `request.param` (slow_seconds, status_code)
    defaults to an immediately-answering 200 (the rc-7/rc-22 tests override it)."""
    slow_seconds, status_code = getattr(request, "param", (0.0, 200))
    _AutoFeedbackStub.slow_seconds = slow_seconds
    _AutoFeedbackStub.status_code = status_code
    _AutoFeedbackStub.feedback_posts = []
    port = _free_port()
    srv = HTTPServer(("localhost", port), _AutoFeedbackStub)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield port
    finally:
        srv.shutdown()


def _isolated_env(tmp_path, port) -> dict:
    """WRIT_SOCKET pointed at a path that cannot exist and WRIT_SESSION_BASE (plus
    WRIT_HOST/WRIT_PORT, which common.sh's own `_writ_session` reads) at the stub, so
    neither the bash curl call nor the python fallback's `writ_daemon_client` can ever
    reach the operator's real daemon at localhost:8765.

    WRIT_DIR is set because the bare `_run_writ_session` harness below sources
    common.sh directly, without going through a hook that sets it first (unlike
    writ-session-end.sh, which sets WRIT_DIR/SESSION_HELPER before its own `source`).
    Without it, common.sh's local-helper fallback (common.sh:1972,
    `${SKILL_DIR:-${WRIT_DIR:-}}/bin/lib/writ-session.py`) resolves to the bare
    "/bin/lib/writ-session.py" and every fallback exec fails outright."""
    env = os.environ.copy()
    env["WRIT_DIR"] = str(REPO)
    env["WRIT_HOST"] = "localhost"
    env["WRIT_PORT"] = str(port)
    env["WRIT_SESSION_BASE"] = f"http://localhost:{port}"
    env["WRIT_SOCKET"] = str(tmp_path / "no-such-writ.sock")
    # A local fallback that DOES run (rc 7, rc 22, coverage) reads/writes a session
    # cache file; without this it falls back to the real state root under the
    # operator's own HOME.
    env["WRIT_CACHE_DIR"] = str(tmp_path / "cache")
    return env


def _shim_dir(tmp_path):
    """A python3 PATH shim that appends its full argv to a counter file, then execs
    the real interpreter -- the pattern of tests/test_read_records_examined.py
    (TestPython3ExecCountPerRead), used here to prove whether the LOCAL
    `writ-session.py auto-feedback` fallback actually ran."""
    real_python3 = shutil.which("python3")
    assert real_python3, "no python3 on PATH to build the exec-count shim from"
    shim_dir = tmp_path / "path-shim"
    shim_dir.mkdir()
    wrapper = shim_dir / "python3"
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "${WRIT_EXEC_COUNTER:?}"\n'
        f'exec "{real_python3}" "$@"\n'
    )
    wrapper.chmod(0o755)
    return shim_dir


def _run_writ_session(subcmd: str, session_id: str, *, env: dict) -> subprocess.CompletedProcess:
    """`bash -c 'source common.sh; _writ_session <subcmd> <session_id>'`, the
    pattern tests/test_pre_write_dispatch.py::TestCanWriteFallbackForwardsEnvelope
    already uses to exercise one `_writ_session` arm without the rest of a hook."""
    cmd = f"source {shlex.quote(str(COMMON_SH))}; _writ_session {shlex.quote(subcmd)} {shlex.quote(session_id)}"
    return subprocess.run(["bash", "-c", cmd], env=env, capture_output=True, text=True, timeout=10)


class TestDaemonTimeoutSkipsTheLocalFallback:
    """Capability: curl exit 28 (the daemon accepted the POST but answered after
    --max-time) runs no local `writ-session.py auto-feedback`, prints the JSON skip
    marker, and returns 0. RED at HEAD: today's shared tail cannot tell rc 28 apart
    from any other curl failure and always falls back."""

    def test_prints_the_skip_marker_and_returns_zero(self, tmp_path, stub):
        _AutoFeedbackStub.slow_seconds = _SLOW_SECONDS
        env = _isolated_env(tmp_path, stub)
        result = _run_writ_session("auto-feedback", "afb-timeout-1", env=env)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout.strip()) == {
            "auto_feedback": "daemon_timeout",
            "local_fallback": "skipped",
        }

    def test_runs_no_local_auto_feedback_process(self, tmp_path, stub):
        _AutoFeedbackStub.slow_seconds = _SLOW_SECONDS
        env = _isolated_env(tmp_path, stub)
        shim = _shim_dir(tmp_path)
        counter = tmp_path / "exec-count.txt"
        counter.write_text("")
        env["PATH"] = f"{shim}{os.pathsep}{env['PATH']}"
        env["WRIT_EXEC_COUNTER"] = str(counter)
        result = _run_writ_session("auto-feedback", "afb-timeout-2", env=env)
        assert result.returncode == 0, result.stderr
        lines = [ln for ln in counter.read_text().splitlines() if "auto-feedback" in ln]
        assert lines == [], f"local auto-feedback fallback ran when the daemon only timed out: {lines}"


class TestFullHookNoLongerDoubleFeedsBackOnTimeout:
    """Capability: writ-session-end.sh against a slow-but-live daemon, with a cache
    holding loaded rules and a passing analysis result, execs the local
    `writ-session.py auto-feedback` 0 times (1 at HEAD) and its process tree sends 0
    /feedback POSTs to the daemon (>= 1 at HEAD, one per loaded rule).

    RED at HEAD on both assertions: the shared tail falls back unconditionally, and
    `cmd_auto_feedback`'s local run reads `feedback_sent` before the server's own
    thread has written it back, so every loaded rule is POSTed twice.
    """

    def _seed_cache(self, cache_dir: Path, session_id: str) -> None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache = {
            "mode": "work",
            "loaded_rule_ids": ["ARCH-ORG-001"],
            "analysis_results": {"writ/server.py": "pass"},
            "feedback_sent": [],
        }
        (cache_dir / f"writ-session-{session_id}.json").write_text(json.dumps(cache))

    def _run_hook(self, tmp_path, cache_dir, session_id, port, extra_env=None):
        env = _isolated_env(tmp_path, port)
        env["WRIT_CACHE_DIR"] = str(cache_dir)
        env["WRIT_SESSION_ID"] = session_id
        shim = _shim_dir(tmp_path)
        counter = tmp_path / "exec-count.txt"
        counter.write_text("")
        env["PATH"] = f"{shim}{os.pathsep}{env['PATH']}"
        env["WRIT_EXEC_COUNTER"] = str(counter)
        if extra_env:
            env.update(extra_env)
        result = subprocess.run(
            ["bash", str(SESSION_END_HOOK)], input="{}", capture_output=True, text=True,
            env=env, timeout=15,
        )
        return result, counter

    def test_no_local_auto_feedback_exec_against_a_slow_daemon(self, tmp_path, stub):
        _AutoFeedbackStub.slow_seconds = _SLOW_SECONDS
        cache_dir = tmp_path / "cache"
        self._seed_cache(cache_dir, "afb-hook-1")
        result, counter = self._run_hook(tmp_path, cache_dir, "afb-hook-1", stub)
        assert result.returncode == 0, result.stderr
        lines = [ln for ln in counter.read_text().splitlines() if "auto-feedback" in ln]
        assert lines == [], f"expected 0 local auto-feedback execs against a slow daemon: {lines}"

    def test_sends_no_feedback_posts_against_a_slow_daemon(self, tmp_path, stub):
        _AutoFeedbackStub.slow_seconds = _SLOW_SECONDS
        cache_dir = tmp_path / "cache"
        self._seed_cache(cache_dir, "afb-hook-2")
        result, _counter = self._run_hook(tmp_path, cache_dir, "afb-hook-2", stub)
        assert result.returncode == 0, result.stderr
        assert _AutoFeedbackStub.feedback_posts == [], (
            f"expected 0 /feedback POSTs against a slow daemon: {_AutoFeedbackStub.feedback_posts}"
        )


class TestConnectionRefusedStillFallsBack:
    """Capability: curl exit 7 (nothing listening) still runs the local fallback
    exactly once. Regression guard: already GREEN at HEAD, must stay green -- rc 7 is
    not rc 28."""

    def test_local_fallback_runs_once(self, tmp_path):
        closed_port = _free_port()  # bound then released; nothing listens on it
        env = _isolated_env(tmp_path, closed_port)
        shim = _shim_dir(tmp_path)
        counter = tmp_path / "exec-count.txt"
        counter.write_text("")
        env["PATH"] = f"{shim}{os.pathsep}{env['PATH']}"
        env["WRIT_EXEC_COUNTER"] = str(counter)
        result = _run_writ_session("auto-feedback", "afb-refused-1", env=env)
        assert result.returncode == 0, result.stderr
        lines = [ln for ln in counter.read_text().splitlines() if "auto-feedback" in ln]
        assert len(lines) == 1, f"expected exactly one local auto-feedback fallback: {lines}"


class TestHttpErrorStillFallsBack:
    """Capability: curl exit 22 (-f on an HTTP error, here a 500) still runs the
    local fallback exactly once. Regression guard: already GREEN at HEAD."""

    @pytest.mark.parametrize("stub", [(0.0, 500)], indirect=True)
    def test_local_fallback_runs_once(self, tmp_path, stub):
        env = _isolated_env(tmp_path, stub)
        shim = _shim_dir(tmp_path)
        counter = tmp_path / "exec-count.txt"
        counter.write_text("")
        env["PATH"] = f"{shim}{os.pathsep}{env['PATH']}"
        env["WRIT_EXEC_COUNTER"] = str(counter)
        result = _run_writ_session("auto-feedback", "afb-http-error-1", env=env)
        assert result.returncode == 0, result.stderr
        lines = [ln for ln in counter.read_text().splitlines() if "auto-feedback" in ln]
        assert len(lines) == 1, f"expected exactly one local auto-feedback fallback: {lines}"


class TestCoverageIsNeverGivenTheTimeoutSkip:
    """Capability: `_writ_session coverage` against a slow daemon still falls back to
    the local helper -- the rc-28 skip is scoped to `auto-feedback` only. Regression
    guard: coverage's route is read-only (session_state.py:288-301), so a duplicate
    local run costs time but never duplicates a side effect, and nothing about this
    batch may change that."""

    def test_coverage_still_falls_back_when_the_daemon_is_slow(self, tmp_path, stub):
        _AutoFeedbackStub.slow_seconds = _SLOW_SECONDS
        env = _isolated_env(tmp_path, stub)
        shim = _shim_dir(tmp_path)
        counter = tmp_path / "exec-count.txt"
        counter.write_text("")
        env["PATH"] = f"{shim}{os.pathsep}{env['PATH']}"
        env["WRIT_EXEC_COUNTER"] = str(counter)
        result = _run_writ_session("coverage", "afb-coverage-1", env=env)
        assert result.returncode == 0, result.stderr
        lines = [ln for ln in counter.read_text().splitlines() if "coverage" in ln]
        assert len(lines) == 1, f"expected coverage to still fall back locally: {lines}"
