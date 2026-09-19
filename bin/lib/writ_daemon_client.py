"""One daemon client for python callers: unix socket when it answers, TCP otherwise.

WHY THIS EXISTS. E2a moved the daemon behind a unix socket and taught the bash side
about it via `WRIT_CURL_TRANSPORT`. The census it added then named its own dominant
source, and it was not a curl client at all: `hooks/scripts/writ-statusline.sh` POSTs
`context-percent` from an EMBEDDED python block using `urllib.request`, which is 65 of
the first 71 recorded TCP writes. Two more callers hardcode the URL outright
(`writ_send_escalation_feedback.py`, `writ/session/feedback.py`). No grep over curl
call sites could have found any of them, which is the argument for counting rather
than estimating.

STDLIB ONLY. Callers include a hook that runs under BARE system python3, so requests,
httpx and the unix-socket adapters built on them are unavailable. `http.client`
already speaks HTTP/1.1 over any socket; only `connect` changes.

FAIL-OPEN, ALWAYS. Every caller here is best-effort telemetry or a context refresh: a
status bar must not crash because the daemon is down, and a feedback POST must not
fail a hook. Errors resolve to a status of 0 rather than raising, which is what the
callers already did with their own bare `except`.
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import stat
import urllib.parse

DEFAULT_BASE_URL = "http://localhost:8765"
DEFAULT_SOCKET = os.path.join(
    os.path.expanduser("~"), ".cache", "writ", "run", "writ.sock"
)


class UnixSocketHTTPConnection(http.client.HTTPConnection):
    """HTTP/1.1 over an AF_UNIX socket.

    The Host header stays "localhost": the daemon does not route on it, and a socket
    path is not a valid header value.
    """

    def __init__(self, socket_path: str, timeout: float | None = None) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if self.timeout is not None:
            sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


def socket_available(socket_path: str) -> bool:
    """True when the path is a socket file. NOT whether anything is listening.

    Deliberately cheap: a connect probe on top of the request that follows would
    double the syscalls on the hot path. A socket file whose listener has gone is
    handled by falling back to TCP after the attempt fails, the same way the bash
    side retries on curl's exit 7, rather than by predicting it here.
    """
    if not socket_path:
        return False
    try:
        return stat.S_ISSOCK(os.stat(socket_path).st_mode)
    except OSError:
        return False


def _request(
    method: str,
    path: str,
    body: bytes | None,
    socket_path: str,
    base_url: str,
    timeout: float,
) -> tuple[int, str]:
    """Try the socket, then TCP. Returns (status, text); status 0 means neither answered."""
    headers = {"Host": "localhost"}
    if body is not None:
        headers["Content-Type"] = "application/json"

    if socket_available(socket_path):
        conn = UnixSocketHTTPConnection(socket_path, timeout=timeout)
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            return response.status, response.read().decode("utf-8", "replace")
        except OSError:
            # A stale socket file is the ordinary aftermath of a replaced daemon.
            # Fall through to TCP rather than reporting the daemon as down.
            pass
        finally:
            conn.close()

    parsed = urllib.parse.urlsplit(base_url)
    conn = http.client.HTTPConnection(
        parsed.hostname or "localhost", parsed.port or 8765, timeout=timeout
    )
    try:
        conn.request(method, path, body=body, headers={k: v for k, v in headers.items()
                                                       if k != "Host"})
        response = conn.getresponse()
        return response.status, response.read().decode("utf-8", "replace")
    except OSError:
        return 0, ""
    finally:
        conn.close()


def post_json(
    path: str,
    payload: dict,
    socket_path: str | None = None,
    base_url: str | None = None,
    timeout: float = 0.5,
) -> tuple[int, str]:
    """POST `payload` as JSON to `path` (e.g. "/session/abc/context-percent")."""
    return _request(
        "POST", path, json.dumps(payload).encode(),
        socket_path if socket_path is not None else os.environ.get("WRIT_SOCKET", DEFAULT_SOCKET),
        base_url if base_url is not None else os.environ.get("WRIT_SESSION_BASE", DEFAULT_BASE_URL),
        timeout,
    )


def get_json(
    path: str,
    socket_path: str | None = None,
    base_url: str | None = None,
    timeout: float = 0.5,
) -> tuple[int, str]:
    """GET `path` and return (status, body text)."""
    return _request(
        "GET", path, None,
        socket_path if socket_path is not None else os.environ.get("WRIT_SOCKET", DEFAULT_SOCKET),
        base_url if base_url is not None else os.environ.get("WRIT_SESSION_BASE", DEFAULT_BASE_URL),
        timeout,
    )
