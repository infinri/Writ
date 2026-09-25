"""The SubagentStart hook's one composite daemon call, as a function.

writ-subagent-start.sh used to make four daemon round trips per dispatch (/health,
/query, /session/format and GET /subagent-role/{role}). POST /subagent/start-context
answers all of them at once; this is its client, kept out of the hook's heredoc so the
heredoc stays thin and the client is unit-testable.

Transport is bin/lib/writ_daemon_client.py, reading WRIT_SOCKET and WRIT_SESSION_BASE,
which the hook sets to exactly the transport its curl calls would have used. The route
only reads, so a plain post_json (which may retry a stale socket over TCP) is safe here.

NEVER RAISES. None means "use the multi-call fallback": a daemon that predates the
route (404), one that is down (status 0), or any answer that is not the documented shape.
"""
from __future__ import annotations

import json
import os
import sys

_PATH = "/subagent/start-context"
_KEYS = ("retrieval", "text", "rule_ids", "query_rule_ids", "rule_count",
         "role_lookup", "write_scope")


def _daemon_client():
    lib = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "bin", "lib",
    )
    if lib not in sys.path:
        sys.path.insert(0, lib)
    import writ_daemon_client

    return writ_daemon_client


def fetch_start_context(query: str, project_root: str, role: str,
                        budget_tokens: int = 2000, timeout: float = 2.0) -> dict | None:
    """POST /subagent/start-context; the parsed answer on a well-formed 200, else None."""
    try:
        status, text = _daemon_client().post_json(
            _PATH,
            {"query": query, "budget_tokens": budget_tokens,
             "project_root": project_root, "role": role},
            timeout=timeout,
        )
        if status != 200:
            return None
        body = json.loads(text)
    except Exception:  # noqa: BLE001 - every failure means "take the fallback"
        return None
    if not isinstance(body, dict) or any(key not in body for key in _KEYS):
        return None
    if not isinstance(body["text"], str) or not isinstance(body["rule_ids"], list):
        return None
    return body
