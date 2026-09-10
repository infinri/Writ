"""FIX-2: daemon cache-dir alignment (server-cache-desync hardening, audit finding #1).

The daemon's session-cache dir is `WRIT_CACHE_DIR` or `tempfile.gettempdir()`; no start
path pins it, so a daemon started under a different TMPDIR serves a divergent cache dir
and live-hook tests false-red. FIX-2: expose cache_dir on /health, pin WRIT_CACHE_DIR at
every start path, and make ensure-server self-heal a misaligned daemon.

Post-systemd (f5f21e6): the systemd user service owns the daemon lifecycle and
auto-restarts it, so the realign-on-mismatch restart is now OPT-IN (set
WRIT_REALIGN_CACHE=1) and OFF by default -- a default-on realign would fight
systemd. The realign capability stays in the lib; the self-heal tests opt in to
exercise it, and test_default_does_not_realign pins the systemd-safe default.

Integration tests spawn an ISOLATED daemon on an alt port and clean it up by exact
command match -- they never touch the interactive session daemon. They skip gracefully.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tests.conftest import writ_server_source
from tests._hook_runner import isolated_daemon  # noqa: F401  (module fixture)

SKILL = Path(__file__).resolve().parent.parent
ENSURE = SKILL / "scripts" / "ensure-server.sh"
# The cache-pin / port / realign start logic moved into the shared flock-guarded lib
# (SERVER-SINGLETON); ensure-server.sh sources it. The start-path source-shape checks read it.
LIB = SKILL / "scripts" / "lib" / "writ-server-lib.sh"
RAG_INJECT = SKILL / "hooks" / "scripts" / "writ-rag-inject.sh"
ALT_PORT = 8799


def _health(port: int, timeout: float = 2.0):
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/health", timeout=timeout) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _port_busy(port: int) -> bool:
    return _health(port) is not None


def _kill_alt_daemon(port: int) -> None:
    # Precise: only the `writ serve --port <port>` process; never the bare-`writ serve` session daemon.
    subprocess.run(["pkill", "-f", f"writ serve --port {port}"], capture_output=True)
    subprocess.run(["pkill", "-f", f"uvicorn.*--port {port}"], capture_output=True)
    time.sleep(1)


def _ensure(port: int, cache_dir: str, timeout: int = 20, friction_log: str | None = None,
            realign: bool = False):
    env = {**os.environ, "WRIT_PORT": str(port), "WRIT_HOST": "localhost", "WRIT_CACHE_DIR": cache_dir}
    if friction_log is not None:
        env["WRIT_FRICTION_LOG"] = friction_log
    # Post-systemd, realign-on-mismatch is opt-in (off by default so ensure-server
    # never fights the systemd-managed daemon). The self-heal tests opt in.
    if realign:
        env["WRIT_REALIGN_CACHE"] = "1"
    subprocess.run(["bash", str(ENSURE)], capture_output=True, text=True, env=env, timeout=40)
    for _ in range(timeout * 2):
        h = _health(port)
        if h is not None:
            return h
        time.sleep(0.5)
    return None


class TestHealthCacheDir:
    """OWNED DAEMON over HTTP (Decision 4/5, plan.md
    2412ba38-51e1-4b73-895b-7b240a3c21d3, module 6). Converts the module's
    key-presence test: a daemon reporting the WRONG dir satisfied `"cache_dir"
    in h`, which is the FIX-2 defect this module exists for.
    """

    def test_health_reports_the_cache_dir_the_start_was_handed(self, isolated_daemon) -> None:
        expected = isolated_daemon["cache_dir"]
        reported = isolated_daemon["health"].get("cache_dir")
        assert reported == expected, (
            f"/health must report the cache_dir this daemon's start was handed "
            f"({expected!r}), not merely carry a cache_dir key; got {reported!r}"
        )
    # MUTATION: having /health report tempfile.gettempdir() instead of the
    # resolved dir keeps the key present and reddens this equality.


class TestStartPathsPinCacheDir:
    def test_ensure_server_pins_cache_dir(self) -> None:
        assert "writ-server-lib.sh" in ENSURE.read_text(), "ensure-server.sh must source the start lib"
        body = LIB.read_text()
        assert "WRIT_CACHE_DIR" in body, "the start lib must pin WRIT_CACHE_DIR before starting the daemon"

    def test_ensure_server_passes_port_to_serve(self) -> None:
        body = LIB.read_text()
        assert re.search(r"writ serve.*--port|--port.*\$\{?WRIT_PORT", body), \
            "the start lib must start 'writ serve' on the port it health-checks (--port)"

    def test_ensure_server_has_alignment_restart(self) -> None:
        body = LIB.read_text()
        assert "cache_dir" in body, \
            "the start lib must compare the running daemon's /health cache_dir to realign on mismatch"

    def test_ensure_server_realigns_on_friction_mismatch(self) -> None:
        """Audit #4: realign must also fire on a friction-log divergence, not just
        cache_dir -- a daemon with a stale WRIT_FRICTION_LOG blackholes telemetry."""
        body = LIB.read_text()
        assert "friction_log" in body, \
            "the start lib must read the running daemon's /health friction_log"
        assert "WRIT_FRICTION_LOG" in body, \
            "the start lib must compare friction_log against WRIT_FRICTION_LOG to realign"

    def test_rag_inject_autostart_pins_cache_dir(self) -> None:
        body = RAG_INJECT.read_text()
        # Must EXPORT it (not just read it via os.environ): a hook-started daemon must be born aligned.
        assert "export WRIT_CACHE_DIR" in body, \
            "writ-rag-inject.sh auto-start must `export WRIT_CACHE_DIR` so a hook-started daemon is aligned"

    def test_health_source_includes_cache_dir(self) -> None:
        assert "cache_dir" in writ_server_source(), "server.py /health must include a cache_dir field"


@pytest.mark.skipif(shutil.which("pkill") is None, reason="pkill required for isolated-daemon cleanup")
class TestEnsureServerSelfHeal:
    def test_realign_restarts_misaligned_daemon(self, tmp_path) -> None:
        if _port_busy(ALT_PORT):
            pytest.skip(f"alt port {ALT_PORT} already in use")
        dir_a = str(tmp_path / "A"); dir_b = str(tmp_path / "B")
        os.makedirs(dir_a, exist_ok=True); os.makedirs(dir_b, exist_ok=True)
        try:
            h_a = _ensure(ALT_PORT, dir_a, realign=True)
            if h_a is None:
                pytest.skip("could not start isolated daemon (env/Neo4j unavailable)")
            assert h_a.get("cache_dir") == dir_a, f"daemon should serve cache_dir A; got {h_a.get('cache_dir')}"
            # Re-ensure with a DIFFERENT cache dir -> opt-in self-heal must restart + realign.
            h_b = _ensure(ALT_PORT, dir_b, realign=True)
            assert h_b is not None and h_b.get("cache_dir") == dir_b, \
                f"ensure-server must realign a misaligned daemon to B; got {h_b and h_b.get('cache_dir')}"
        finally:
            _kill_alt_daemon(ALT_PORT)

    def test_default_does_not_realign_misaligned_daemon(self, tmp_path) -> None:
        """Systemd-safety default (f5f21e6): with WRIT_REALIGN_CACHE unset, ensure-server
        is start-only -- it must NOT kill+restart a running-but-misaligned daemon (that
        would fight the systemd-managed daemon). Pins the default that the systemd
        migration introduced; the inverse of the opt-in realign test above."""
        if _port_busy(ALT_PORT):
            pytest.skip(f"alt port {ALT_PORT} already in use")
        dir_a = str(tmp_path / "A"); dir_b = str(tmp_path / "B")
        os.makedirs(dir_a, exist_ok=True); os.makedirs(dir_b, exist_ok=True)
        try:
            h_a = _ensure(ALT_PORT, dir_a)  # default: realign OFF
            if h_a is None:
                pytest.skip("could not start isolated daemon (env/Neo4j unavailable)")
            assert h_a.get("cache_dir") == dir_a
            # Re-ensure with a DIFFERENT cache dir, realign OFF -> daemon stays on A.
            h_b = _ensure(ALT_PORT, dir_b)
            assert h_b is not None and h_b.get("cache_dir") == dir_a, \
                f"default ensure-server must NOT realign (systemd-safety); expected A, got {h_b and h_b.get('cache_dir')}"
        finally:
            _kill_alt_daemon(ALT_PORT)

    def test_idempotent_when_aligned(self, tmp_path) -> None:
        if _port_busy(ALT_PORT):
            pytest.skip(f"alt port {ALT_PORT} already in use")
        dir_a = str(tmp_path / "A"); os.makedirs(dir_a, exist_ok=True)
        try:
            h1 = _ensure(ALT_PORT, dir_a, realign=True)
            if h1 is None:
                pytest.skip("could not start isolated daemon")
            t1 = h1.get("startup_time")
            h2 = _ensure(ALT_PORT, dir_a, realign=True)  # realign on but already aligned -> must NOT restart
            assert h2 is not None and h2.get("startup_time") == t1, \
                "ensure-server must not restart an already-aligned daemon (startup_time unchanged)"
        finally:
            _kill_alt_daemon(ALT_PORT)

    def test_realign_restarts_on_friction_mismatch(self, tmp_path) -> None:
        """Same cache_dir, divergent friction-log -> ensure-server must restart and
        realign the daemon onto the caller's WRIT_FRICTION_LOG (audit #4)."""
        if _port_busy(ALT_PORT):
            pytest.skip(f"alt port {ALT_PORT} already in use")
        cache = str(tmp_path / "C"); os.makedirs(cache, exist_ok=True)
        fa = str(tmp_path / "fa.log"); fb = str(tmp_path / "fb.log")
        try:
            h_a = _ensure(ALT_PORT, cache, friction_log=fa, realign=True)
            if h_a is None:
                pytest.skip("could not start isolated daemon (env/Neo4j unavailable)")
            assert h_a.get("friction_log") == fa, \
                f"daemon should serve friction_log A; got {h_a.get('friction_log')}"
            # Re-ensure: same cache, DIFFERENT friction-log -> opt-in self-heal must realign.
            h_b = _ensure(ALT_PORT, cache, friction_log=fb, realign=True)
            assert h_b is not None and h_b.get("friction_log") == fb, \
                f"ensure-server must realign friction-log to B; got {h_b and h_b.get('friction_log')}"
        finally:
            _kill_alt_daemon(ALT_PORT)
