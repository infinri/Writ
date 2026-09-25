"""Which transport a request arrived on, and a census of writes on the public one.

WHY THIS EXISTS. The daemon binds `127.0.0.1:8765`, which every local account can
reach; `docs/reference/http-api.md` says so ("no auth (binds localhost only)").
Cycle E1 removed the one route that let a caller write arbitrary session state. What
stays reachable over TCP is reading all of it, plus the state-writing routes that
legitimately exist. The end state is a unix socket for those routes and TCP for the
read-only surface, because `/dashboard` and `/explore` serve HTML to a browser and a
browser cannot open a unix socket.

IT COUNTED BEFORE IT ENFORCED, and it still counts. 16 files POST to the daemon and 27
hold a daemon URL; flipping enforcement before they moved would have broken them,
silently and on somebody else's session. So every state-touching request arriving over
TCP emits one audit row naming the route and whether the policy refused it, and that
observed list drove the migration. A grep for the same list was already wrong once (13
files claimed, 16 that POST), which is the whole argument for measuring instead of
estimating. Enforcement itself is opt-in via `WRIT_TCP_READONLY` and ships off.

HOW THE TRANSPORT IS KNOWN. From the ASGI scope, never from a header: a header is
written by the caller, while `scope["client"]` is set by the server. Measured against
uvicorn 0.51.0 with both transports bound in one process:

    over the socket:  client=None                      server=["<path>", None]
    over TCP:         client=("127.0.0.1", 57966)      server=["127.0.0.1", 8765]
"""
from __future__ import annotations

from typing import Any

# Verbs that can change state. GET and HEAD are reads; OPTIONS is a preflight. Anything
# else is counted, so a future PATCH or DELETE route is in the census on the day it
# lands rather than the day someone remembers to add it here (ABS-SECURITY-024: the
# read set is the enumerable one, so that is the set named).
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

TRANSPORT_SOCKET = "socket"
TRANSPORT_TCP = "tcp"


def request_transport(scope: dict[str, Any]) -> str:
    """`"socket"` or `"tcp"` for one ASGI scope.

    An ABSENT or None `client` reads as socket-origin, which is what uvicorn sends
    over a UDS. That default is also the safe one for the eventual enforcement: a
    scope shape this function does not recognise is treated as the trusted transport
    only because it cannot then be used to smuggle a write past a policy that has not
    been written yet. When enforcement lands in E2b the default flips with it, and
    the test that pins this behaviour is the place to change.
    """
    client = scope.get("client")
    if not client:
        return TRANSPORT_SOCKET
    return TRANSPORT_TCP


def is_state_touching(method: str) -> bool:
    """True when the verb can change state."""
    return (method or "").upper() not in _READ_METHODS


# State-touching VERBS that TCP may still use, named by path: routes that change
# nothing but must carry a body, so the verb alone misjudges them. Exact match, no
# prefixes, and an ALLOWLIST rather than a blocklist, so a route that really does write
# is private the day it lands (ABS-SECURITY-024).
#
# /query is the retrieval read. `writ/static/explore.html:505` posts it from the page
# and a browser cannot open a unix socket, so refusing it broke the /explore query
# panel on the day enforcement went on. Its handler runs the pipeline and emits log
# rows: no graph write, no counter, nothing another local account could change.
#
# /subagent/start-context is the SubagentStart composite. Its handler runs query_rules,
# the session formatter and get_subagent_role, and writes nothing; refusing it would
# push writ-subagent-start.sh onto its multi-call fallback without saying so.
#
# THIS REPLACED A PATH-KEYED ALLOWLIST naming /health, /dashboard, /explore, /graph and
# the /node/ prefix. Keyed on the path alone it exempted every verb on those paths (a
# measured `POST /health` over TCP returned 405, not 403), while the exemption those
# five actually needed is for reads, which `tcp_refusal` now grants on every path. A
# constant that grants nothing while its name claims a boundary is worse than no
# constant, so it is deleted rather than amended. The reason those paths mattered
# survives as the read exemption below and in README.md:120, which tells every new user
# to verify an install with `curl http://localhost:8765/health`.
TCP_READONLY_POST_ALLOWLIST = ("/query", "/subagent/start-context")
_ENFORCE_ENV = "WRIT_TCP_READONLY"


def tcp_readonly_enabled() -> bool:
    """True when TCP may read but not change, bar the named body-carrying reads.

    SHIPS OFF. The go signal is the census reading zero state-touching TCP writes over
    a day of real use, not a belief that the client migration is complete: turning it
    on early breaks whichever caller was missed, silently, on somebody else's session.
    """
    import os

    return os.environ.get(_ENFORCE_ENV, "").strip() == "1"


def tcp_refusal(scope: dict[str, Any]) -> str | None:
    """A refusal reason for this request, or None to serve it.

    THE VERB BOUNDS WHAT TCP MAY CHANGE, not the path. None whenever enforcement is
    off, whenever the request came over the socket, whenever the verb is a read (so the
    browser surface and any preflight keep working on every path), and whenever the
    path is a named body-carrying read. Everything else is socket-only.

    Reads stay served on every path rather than on a browser allowlist: closing them is
    a larger change than this sweep made, and hooks plus the CLI still read over TCP.
    """
    if not tcp_readonly_enabled():
        return None
    if request_transport(scope) != TRANSPORT_TCP:
        return None
    if not is_state_touching(scope.get("method", "")):
        return None
    path = scope.get("path", "") or ""
    if path in TCP_READONLY_POST_ALLOWLIST:
        return None
    return (
        f"{scope.get('method', '')} {path} is served over the Writ daemon's unix "
        "socket, not its TCP port. State-changing routes are private to the user "
        "running the daemon; TCP serves the read-only surface only."
    )


def note_request(transport: str, method: str, path: str,
                 refused: bool | None = None) -> None:
    """Record one state-touching request that arrived over TCP. Never raises.

    Reads and socket traffic are NOT recorded: the census exists to produce the list
    of call sites still writing over the public transport, and rows for `/health` or
    `/dashboard` would bury it. `emit` is already fire-and-forget, and this wraps it
    anyway so a logging fault can never fail a request the daemon would otherwise
    have served.

    `refused` puts the policy decision on the row. Without it an exempted write and a
    blocked straggler are indistinguishable, which is how three rows in the live log
    read as three refusals when one of them had been served. None means the caller did
    not decide, and the field is then omitted rather than guessed at.
    """
    if transport != TRANSPORT_TCP or not is_state_touching(method):
        return
    try:
        from writ.shared.logging import emit

        decision = {} if refused is None else {"refused": bool(refused)}
        emit(None, "daemon_tcp_write", "", None,
             method=method.upper(), path=path, **decision)
    except Exception:  # noqa: BLE001 - a census row must never break a request
        pass


class TransportCensusMiddleware:
    """Pure-ASGI middleware: tags the transport, counts TCP writes, blocks nothing.

    Pure ASGI rather than Starlette's BaseHTTPMiddleware because that wrapper buffers
    the response body, and this app streams HTML dashboards. It also puts the resolved
    transport on the scope under `writ_transport`, so E2b's policy reads one value
    instead of re-deriving it per route.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        transport = request_transport(scope)
        scope["writ_transport"] = transport
        refusal = tcp_refusal(scope)
        # Counted whatever the decision, and counted before it is served: the census
        # must record what enforcement blocked, so turning the flag on does not blind
        # the audit. The decision rides on the row, or an exempted write reads exactly
        # like a client that never migrated.
        note_request(transport, scope.get("method", ""), scope.get("path", ""),
                     refused=refusal is not None)
        if refusal is not None:
            import json as _json

            body = _json.dumps({"error": refusal}).encode()
            await send({
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    [b"content-type", b"application/json"],
                    [b"content-length", str(len(body)).encode()],
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)
