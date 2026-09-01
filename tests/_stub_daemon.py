"""A loopback HTTP stub for the two daemon routes the write-path dispatch hook calls.

WHY THIS EXISTS
---------------
`tests/test_write_path_process_budget.py` measured exactly one branch of
`hooks/scripts/writ-pre-write-dispatch.sh`: the daemon-down deny. That is the
branch the TEST produces, because `tests/conftest.py` pins `WRIT_PORT` at a
port with nothing listening, and it is not a branch real usage hits. The branch
real source-code writes DO hit is allow-with-rules-injected, and it was never
counted at all.

Measuring the live branches needs a server answering on `WRIT_PORT`. It does
NOT need the real Writ daemon: the hook reaches exactly two routes, both plain
`curl` calls, and both take a JSON body the test can write out as a literal.

  * `POST /pre-write-check` -- `_writ_session pre-write-check`
    (bin/lib/common.sh:1900-1911)
  * `GET  /always-on`       -- the applicability-scoped write-time injection
    (hooks/scripts/writ-pre-write-dispatch.sh)

So this module starts a stdlib `ThreadingHTTPServer` on port 0, serves canned
responses for those two routes, and 404s everything else. No Neo4j, no live
daemon, no network beyond loopback. `bin/lib/common.sh:1672` already resolves
the transport to TCP rather than the unix socket whenever `WRIT_PORT` is set,
which conftest does unconditionally, so a loopback listener reaches both routes
with no socket plumbing.

WHAT IT CANNOT PROVE, stated here rather than discovered at review
------------------------------------------------------------------
The bodies are literals, so a measurement against this stub proves the hook's
BRANCH STRUCTURE and its process cost per branch. It does not prove that the
real server returns these shapes, it does not prove live wall time, and it
cannot validate gate POLICY, because the allow/ask/deny decision is made
server-side and this stub just asserts one. The existing gate tests keep that
job.

WHY IT RECORDS REQUESTS
-----------------------
Absence of a failure is not evidence that a branch ran. A stub that 404s, or a
canned body the hook cannot parse, sends the hook down its daemon-down fallback
and produces a plausible process count that reads green. The recorded request
log is the POSITIVE signal: a caller can assert that the hook actually reached
`/pre-write-check`, that it went on to `/always-on` (which only the allow
branch does), and that the query string carries the `mode` the canned response
supplied, which proves the response was parsed rather than merely delivered.

USAGE
-----
    from tests._stub_daemon import StubDaemon

    with StubDaemon(pre_write_check={"decision": "allow", ...},
                    always_on={"rules": []}) as stub:
        env = {**os.environ, "WRIT_HOST": "127.0.0.1", "WRIT_PORT": str(stub.port)}
        ...run the hook...
        assert stub.saw("POST", "/pre-write-check")

Pass an `int` instead of a dict to serve a bare status with no body, which is
how the negative-control test makes `/pre-write-check` 404.

DUPLICATE, KNOWINGLY: `tests/firedrill/test_out_capture.py:139`'s
`_stub_pre_write_daemon` already answers these same two routes with the same
loopback pattern. Deduping them is DEFERRED to its own cycle rather than done
here, because reshaping the firedrill mid-cycle is how a green suite goes stale.
Named so the next reader finds both instead of a third.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

# WRIT_HOST must be set to this in the child env, not left at the `localhost`
# default. `localhost` can resolve to ::1 first, and this server binds a single
# IPv4 address; curl would then pay a failed connection attempt before falling
# back, which is latency and an ordering difference inside a measurement whose
# whole point is determinism.
STUB_HOST = "127.0.0.1"

# Routes this stub knows about. Anything else is a 404 on purpose: the hook is
# supposed to reach only these two, and a silent 200 for a third route would
# hide a new daemon dependency instead of surfacing it.
PRE_WRITE_CHECK = ("POST", "/pre-write-check")
ALWAYS_ON = ("GET", "/always-on")


class RecordedRequest:
    """One request the stub answered, with everything a proof might assert on."""

    __slots__ = ("method", "path", "query", "body", "status")

    def __init__(self, method: str, path: str, query: dict[str, list[str]],
                 body: bytes, status: int) -> None:
        self.method = method
        self.path = path
        self.query = query
        self.body = body
        self.status = status

    @property
    def json_body(self) -> Any:
        """The request body parsed as JSON, or None when it is not JSON.

        Returns None rather than raising because a caller asserting on
        `skill_dir` wants a clear "the body was not JSON" failure from its own
        assertion, not a JSONDecodeError from inside the recorder.
        """
        if not self.body:
            return None
        try:
            return json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (
            f"RecordedRequest({self.method} {self.path} status={self.status} "
            f"query={self.query!r} body={self.body[:120]!r})"
        )


class StubDaemon:
    """A loopback stub serving canned `/pre-write-check` and `/always-on`.

    `pre_write_check` and `always_on` each take either a dict (served as a 200
    with that JSON body) or an int (served as that bare status with an empty
    body, which is how a caller makes the route 404).
    """

    def __init__(self, *, pre_write_check: dict | int | None = None,
                 always_on: dict | int | None = None) -> None:
        self._canned: dict[tuple[str, str], dict | int] = {}
        if pre_write_check is not None:
            self._canned[PRE_WRITE_CHECK] = pre_write_check
        if always_on is not None:
            self._canned[ALWAYS_ON] = always_on
        self.requests: list[RecordedRequest] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> StubDaemon:
        stub = self

        class Handler(BaseHTTPRequestHandler):
            # HTTP/1.0 so every response closes the connection. curl in the
            # hook uses --max-time 1; a keep-alive the client does not expect
            # is a way to spend that budget doing nothing.
            protocol_version = "HTTP/1.0"

            def log_message(self, fmt: str, *args: Any) -> None:
                """Silence. The default writes every request to stderr, which
                inside a traced measurement is noise in the failure output."""

            def _handle(self, method: str) -> None:
                parsed = urlparse(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                canned = stub._canned.get((method, parsed.path))
                if canned is None:
                    status, payload = 404, {"detail": "not found"}
                elif isinstance(canned, int):
                    status, payload = canned, None
                else:
                    status, payload = 200, canned
                with stub._lock:
                    stub.requests.append(RecordedRequest(
                        method, parsed.path, parse_qs(parsed.query), body, status,
                    ))
                raw = b"" if payload is None else json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                if raw:
                    self.wfile.write(raw)

            def do_GET(self) -> None:  # noqa: N802 - stdlib naming
                self._handle("GET")

            def do_POST(self) -> None:  # noqa: N802 - stdlib naming
                self._handle("POST")

        self._server = ThreadingHTTPServer((STUB_HOST, 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> StubDaemon:
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # -- inspection --------------------------------------------------------

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("StubDaemon.port read before start()")
        return self._server.server_address[1]

    def reset(self) -> None:
        """Drop the recorded requests, keeping the canned responses.

        Used by the determinism check, which runs the same branch twice and
        wants each run's request log on its own rather than concatenated.
        """
        with self._lock:
            self.requests.clear()

    def matching(self, method: str, path: str) -> list[RecordedRequest]:
        with self._lock:
            return [r for r in self.requests if r.method == method and r.path == path]

    def saw(self, method: str, path: str) -> bool:
        return bool(self.matching(method, path))

    def describe(self) -> str:
        """Every recorded request, for a failure message.

        A branch proof that fails needs to say what the hook actually did, or
        the reader is left guessing between "never reached the stub", "reached
        it and got a 404" and "reached it and ignored the answer".
        """
        with self._lock:
            if not self.requests:
                return "the stub answered NO requests at all"
            return "; ".join(
                f"{r.method} {r.path} -> {r.status}" for r in self.requests
            )
