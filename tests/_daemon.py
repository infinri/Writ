"""INC-1b: daemon cache-dir alignment helpers for the test suite.

`bin/lib/common.sh::_writ_session` mutates session state in the daemon first (curl), so a
hook writes to the daemon's cache_dir while a file-helper test reads from
`WRIT_CACHE_DIR`/`gettempdir`. When those diverge, live-hook/session tests fail en masse.
These helpers let the suite align the shared daemon to the tests' cache dir at startup.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _port() -> str:
    """The daemon port, resolved LIVE from WRIT_PORT (default 8765).

    A function, not a module constant, so the suite's dedicated test port (set in
    conftest pytest_sessionstart) is honored regardless of when this module is
    imported -- a module-level constant would freeze 8765 at import time.
    """
    return os.environ.get("WRIT_PORT", "8765")


def _health_url() -> str:
    return f"http://localhost:{_port()}/health"


def expected_cache_dir() -> str:
    """The cache dir writ-session.py uses file-side: WRIT_CACHE_DIR or gettempdir()."""
    return os.environ.get("WRIT_CACHE_DIR") or tempfile.gettempdir()


def _daemon_health() -> dict | None:
    """The running daemon's /health JSON, or None if unreachable."""
    try:
        with urllib.request.urlopen(_health_url(), timeout=2) as r:
            return json.load(r)
    except Exception:  # noqa: BLE001
        return None


def daemon_cache_dir() -> str | None:
    """The running daemon's cache_dir from /health, or None if no daemon is reachable."""
    h = _daemon_health()
    return h.get("cache_dir") if h else None


def daemon_friction_log() -> str | None:
    """The running daemon's friction-log path from /health (None if unreachable or an
    older daemon that does not report it)."""
    h = _daemon_health()
    return h.get("friction_log") if h else None


def expected_friction_log() -> str | None:
    """The friction-log path the suite wants the daemon to use (a throwaway set by
    conftest via WRIT_FRICTION_LOG). None means friction alignment is not enforced."""
    return os.environ.get("WRIT_FRICTION_LOG")


def classify_daemon_alignment(daemon_dir: str | None, expected: str) -> str:
    """'down' (no daemon) | 'aligned' (dirs match) | 'diverged' (dirs differ). Pure.

    The anti-masking contract: a diverged daemon must never read as 'aligned', so the suite
    cannot silently tolerate the desync that fails live-hook tests en masse.
    """
    if daemon_dir is None:
        return "down"
    return "aligned" if daemon_dir == expected else "diverged"


def ensure_daemon_aligned() -> str:
    """Realign a daemon whose cache_dir OR friction-log path diverges from expected.

    Restart (stop-server.sh + ensure-server.sh) with WRIT_CACHE_DIR pinned to
    expected_cache_dir() when the daemon's cache_dir diverges, and -- when the suite
    sets WRIT_FRICTION_LOG -- when its friction_log diverges too. Pinning the friction
    log keeps daemon-emitted events (post_compaction, gate decisions for test sessions,
    via daemon-first _writ_session calls) out of the repo's workflow-friction.log; tests
    assert on per-test in-process logs, never the daemon's. Idempotent; a no-op when
    already aligned or when no daemon is running. Returns the cache-alignment state.
    """
    expected = expected_cache_dir()
    expected_friction = expected_friction_log()
    health = _daemon_health()
    cache_state = classify_daemon_alignment(health.get("cache_dir") if health else None, expected)
    friction_aligned = (
        expected_friction is None
        or (health is not None and health.get("friction_log") == expected_friction)
    )
    if cache_state == "down":
        return "down"
    if cache_state == "aligned" and friction_aligned:
        return "aligned"

    env = {**os.environ, "WRIT_CACHE_DIR": expected}
    if expected_friction:
        env["WRIT_FRICTION_LOG"] = expected_friction
    for script in ("stop-server.sh", "ensure-server.sh"):
        path = _REPO_ROOT / "scripts" / script
        if not path.exists():
            continue
        try:
            subprocess.run(
                ["bash", str(path)],
                cwd=str(_REPO_ROOT), env=env,
                capture_output=True, timeout=60, check=False,
            )
        except (subprocess.SubprocessError, OSError):
            return "diverged"

    # ensure-server may take a moment to answer /health after restart.
    for _ in range(10):
        st = classify_daemon_alignment(daemon_cache_dir(), expected)
        if st != "down":
            return st
        time.sleep(0.5)
    return classify_daemon_alignment(daemon_cache_dir(), expected)


def start_test_daemon() -> str:
    """Start the suite's dedicated daemon on the test port (WRIT_PORT) with the test
    cache + throwaway friction, via ensure-server.sh (idempotent: starts if down).

    The interactive 8765 daemon is never touched -- the suite owns this daemon's full
    lifecycle (started here, stopped in stop_test_daemon). Returns the alignment state.
    """
    env = {**os.environ, "WRIT_CACHE_DIR": expected_cache_dir()}
    ensure = _REPO_ROOT / "scripts" / "ensure-server.sh"
    if ensure.exists():
        try:
            subprocess.run(
                ["bash", str(ensure)],
                cwd=str(_REPO_ROOT), env=env,
                capture_output=True, timeout=60, check=False,
            )
        except (subprocess.SubprocessError, OSError):
            return "down"
    return ensure_daemon_aligned()


def stop_test_daemon() -> None:
    """Stop the suite's dedicated test daemon at session finish (F4b/option C).

    The suite runs its own daemon on WRIT_PORT (a dedicated test port, never the
    interactive 8765), so there is nothing to "restore" -- we just stop the test
    daemon so it is not left running. Supersedes F4's restore_daemon_friction:
    isolation removes the hijack, so the restore-the-shared-daemon dance is gone.
    No-op when no daemon answers on the test port.
    """
    if daemon_cache_dir() is None:
        return
    stop = _REPO_ROOT / "scripts" / "stop-server.sh"
    if not stop.exists():
        return
    try:
        subprocess.run(
            ["bash", str(stop)],
            cwd=str(_REPO_ROOT), env={**os.environ},
            capture_output=True, timeout=30, check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return


# --- Isolated per-module daemon (production audit-stream isolation) ---------
#
# start_test_daemon above owns the SUITE's daemon on the shared test port. The
# pair below is for a module that must own its OWN daemon for its own duration:
# a free port nothing else can be holding, and -- the reason this exists -- a log
# root, socket and cache dir handed EXPLICITLY to the subprocess env, so every
# row that daemon writes lands under a throwaway root instead of the operator's
# real `<skill>/var/logs/<project>/audit.jsonl`.


def _isolated_health(port: int, timeout: float = 2.0) -> dict | None:
    """The /health JSON of the daemon on `port`, or None when nothing answers.

    Separate from _daemon_health(), which resolves the port from WRIT_PORT: an
    isolated daemon's port is OS-assigned and no env var names it.
    """
    url = f"http://localhost:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.load(r)
    except Exception:  # noqa: BLE001
        return None


def _kill_writ_serve_on_port(port: int, force: bool = False) -> None:
    """pkill the `writ serve` process whose command line names exactly `port`.

    Matched by PORT, so it can only ever reach the daemon the caller started;
    the interactive singleton on another port is structurally out of reach.
    Mirrors tests/test_phase3b_approval_rewrap.py::_stop_own_daemon, and is
    deliberately not stop-server.sh: that script locates the process with lsof
    (not always installed) and, on the default port, stops the operator's
    systemd unit instead of a process.
    """
    args = ["pkill", *(["-9"] if force else []), "-f", f"writ serve --port {port}"]
    try:
        subprocess.run(args, capture_output=True, check=False, timeout=10)
    except (subprocess.SubprocessError, OSError):
        pass


def _wait_for_isolated_health(port: int, attempts: int = 40) -> dict | None:
    """Poll /health on `port` until it answers. None when it never does.

    ensure-server.sh already waits ~5s for its own health probe; this second
    poll covers a slower cold start (index pre-warm) without turning a failed
    start into a hang.
    """
    for _ in range(attempts):
        health = _isolated_health(port)
        if health is not None:
            return health
        time.sleep(0.25)
    return None


def _wait_for_socket_file(socket_path: str, attempts: int = 20) -> bool:
    """True once `socket_path` exists. `writ serve` binds TCP first and falls
    back to TCP-only on an unusable or already-served socket, so a daemon that
    answers /health is NOT evidence that its socket came up -- and with
    WRIT_TCP_READONLY set, a missing socket surfaces as a confusing 403 on the
    first state-changing POST rather than as a failed start."""
    for _ in range(attempts):
        if Path(socket_path).exists():
            return True
        time.sleep(0.25)
    return False


def _wait_for_isolated_down(port: int, attempts: int = 20) -> bool:
    """True once nothing answers the health route on `port`, so "stopped" can be
    a VERIFIED state rather than a signal that was sent."""
    for _ in range(attempts):
        if _isolated_health(port, timeout=0.5) is None:
            return True
        time.sleep(0.25)
    return False


def start_isolated_daemon(
    log_root: str,
    socket_path: str,
    cache_dir: str,
    tcp_readonly: bool = False,
    port: int | None = None,
    timeout: float = 40.0,
) -> dict | None:
    """Start a daemon this caller OWNS, fully isolated, on an OS-assigned free port.

    Returns None (never raises, never hangs) when the daemon cannot be brought
    up -- ensure-server.sh missing, the socket path over the AF_UNIX byte cap,
    nothing answering /health, or the socket file never appearing -- so the
    caller can skip with a stated reason. Otherwise returns
    {"port", "base_url", "socket_path", "cache_dir", "log_root", "health"},
    where "health" is that daemon's OWN /health payload: the caller verifies
    the log destination the SERVER process resolved rather than assuming the
    env plumbing took effect.

    THE ENV DICT IS BUILT EXPLICITLY, and each part of it closes a measured trap:

    - `WRIT_LOG_ROOT` is set here rather than inherited. conftest's
      `_isolate_friction_log` is FUNCTION-scoped and autouse, and a
      module-scoped daemon fixture is set up BEFORE any function-scoped
      fixture, so at start time the variable may be absent and the daemon
      would resolve the real `<skill>/var/logs`.
    - `WRIT_FRICTION_LOG` is POPPED, not merely left unset in the parent. It is
      `emit_destination`'s collapse branch: if it survives into the child,
      EVERY stream lands in that one file and the stream classification (a
      refusal row in a per-project `audit.jsonl`) disappears, which is both
      less than production's shape and a file a later test may delete.
    - `WRIT_SOCKET` is pinned because `writ serve` unlinks and TAKES OVER the
      default socket when that socket is stale-but-present (writ/cli.py only
      declines a LIVE one), which would be a real change to the operator's
      install. It is also the load-bearing half of the isolation: the returned
      `socket_path` must be passed to writ_daemon_client's post_json/get_json,
      which prefer an EXISTING socket over `base_url` and otherwise fall back
      to `~/.cache/writ/run/writ.sock` -- the interactive daemon's -- no matter
      what port `base_url` names.
    - `WRIT_CACHE_DIR` survives `writ_ensure_server`'s own
      `export WRIT_CACHE_DIR="$(writ_session_cache_dir)"`, because that
      resolver reads WRIT_CACHE_DIR first (bin/lib/common.sh). Same mechanism
      tests/test_phase3b_approval_rewrap.py relies on.
    - `WRIT_LOG` is popped so `writ_default_server_log`'s resolution order
      falls to `$WRIT_LOG_ROOT/server.log`; an inherited WRIT_LOG outranks the
      log root and would append the daemon's stdout to whatever file it names.
    - `WRIT_TCP_READONLY` matches the deployed daemon's posture (state-changing
      routes over the socket only) when the caller asks for it.
    """
    from writ.config import socket_path_usable

    ensure = _REPO_ROOT / "scripts" / "ensure-server.sh"
    if not ensure.exists():
        return None
    if not socket_path_usable(socket_path):
        return None
    if port is None:
        from tests.fixtures.net import free_port

        port = free_port()

    for directory in (log_root, cache_dir, str(Path(socket_path).parent)):
        Path(directory).mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env.pop("WRIT_FRICTION_LOG", None)
    env.pop("WRIT_LOG", None)
    env.update(
        {
            "WRIT_HOST": "localhost",
            "WRIT_PORT": str(port),
            "WRIT_LOG_ROOT": log_root,
            "WRIT_SOCKET": socket_path,
            "WRIT_CACHE_DIR": cache_dir,
        }
    )
    if tcp_readonly:
        env["WRIT_TCP_READONLY"] = "1"
    else:
        env.pop("WRIT_TCP_READONLY", None)

    try:
        subprocess.run(
            ["bash", str(ensure)],
            cwd=str(_REPO_ROOT), env=env,
            capture_output=True, timeout=timeout, check=False,
        )
    except (subprocess.SubprocessError, OSError):
        _kill_writ_serve_on_port(port)
        return None

    health = _wait_for_isolated_health(port)
    if health is None or not _wait_for_socket_file(socket_path):
        _kill_writ_serve_on_port(port)
        return None

    return {
        "port": port,
        "base_url": f"http://localhost:{port}",
        "socket_path": socket_path,
        "cache_dir": cache_dir,
        "log_root": log_root,
        "health": health,
    }


def stop_isolated_daemon(daemon: dict | None) -> None:
    """Stop the daemon start_isolated_daemon returned, by its exact port.

    Escalates to SIGKILL only if the daemon still answers /health after the
    term, so "stopped" is a verified state and not a signal that was sent.
    Removes the throwaway socket file too: a socket outliving its listener is
    exactly the stale-but-present file `writ serve` unlinks and takes over.
    No-op on None (the caller skipped) or on a payload with no port.
    """
    if not daemon:
        return
    port = daemon.get("port")
    if port is None:
        return
    _kill_writ_serve_on_port(port)
    if not _wait_for_isolated_down(port):
        _kill_writ_serve_on_port(port, force=True)
        _wait_for_isolated_down(port)
    socket_path = daemon.get("socket_path")
    if socket_path:
        try:
            os.unlink(socket_path)
        except OSError:
            pass
