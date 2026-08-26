"""Which transport a request arrived on, and a census of writes on the public one.

WHY THIS EXISTS. The daemon binds `127.0.0.1:8765`, which every local account can
reach; `docs/reference/http-api.md` says so ("no auth (binds localhost only)").
Cycle E1 removed the one route that let a caller write arbitrary session state. What
stays reachable over TCP is reading all of it, plus the state-writing routes that
legitimately exist. The end state is a unix socket for those routes and TCP for the
read-only surface, because `/dashboard` and `/explore` serve HTML to a browser and a
browser cannot open a unix socket.

THIS MODULE ENFORCES NOTHING YET, deliberately. 16 files POST to the daemon and 27
hold a daemon URL; flipping enforcement before they move would break them, silently
and on somebody else's session. So it COUNTS first: every state-touching request that
arrives over TCP emits one audit row naming the route, and that observed list drives
the migration. A grep for the same list was already wrong once (13 files claimed,
16 that POST), which is the whole argument for measuring instead of estimating.

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


def note_request(transport: str, method: str, path: str) -> None:
    """Record one state-touching request that arrived over TCP. Never raises.

    Reads and socket traffic are NOT recorded: the census exists to produce the list
    of call sites still writing over the public transport, and rows for `/health` or
    `/dashboard` would bury it. `emit` is already fire-and-forget, and this wraps it
    anyway so a logging fault can never fail a request the daemon would otherwise
    have served.
    """
    if transport != TRANSPORT_TCP or not is_state_touching(method):
        return
    try:
        from writ.shared.logging import emit

        emit(None, "daemon_tcp_write", "", None, method=method.upper(), path=path)
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
        note_request(transport, scope.get("method", ""), scope.get("path", ""))
        await self.app(scope, receive, send)
