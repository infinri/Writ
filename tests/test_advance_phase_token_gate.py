"""Audit P0: the /advance-phase server route must require the gate token.

The CLI cmd_advance_phase validates a token written by auto-approve-gate.sh (which fires
only on a user approval-pattern prompt the agent cannot forge), logging
`agent_self_approval_blocked` on a missing/wrong token. But the SERVER route -- the
daemon-first path that _writ_session and /writ-approve actually use -- advanced on
`confirmation_source` alone with NO token check, so any caller could POST
{"confirmation_source":"tool"} and self-advance its own gate. That is the self-approval hole:
oversight replaced by an honor-system instruction ("Never fabricate approval").

This is an integration test against the live daemon (skips if unreachable). RED before the
route validates the token (a tokenless advance succeeds); GREEN after (tokenless is refused;
a valid token gets past the gate). The token is consumed on a successful advance.
"""

from __future__ import annotations

import json
import os
import tempfile
import urllib.error
import urllib.request
import uuid

from pathlib import Path

import pytest

# This is a security-integration test of the DEPLOYED daemon's advance-phase route, so it
# targets the interactive daemon on :8765 directly (skips if unreachable). It only writes a
# throwaway token file (gettempdir) and advances a throwaway session id, so it does not
# mutate shared graph/cache state.
SERVER = "http://localhost:8765"


def _server_up() -> bool:
    try:
        with urllib.request.urlopen(f"{SERVER}/health", timeout=2):
            return True
    except (urllib.error.URLError, OSError):
        return False


def _post_advance(session_id: str, body: dict) -> dict:
    """POST through the shared client, which prefers the daemon's unix socket.

    THIS TEST STILL TARGETS THE DEPLOYED DAEMON, which is its whole point. What
    changed is the transport: with WRIT_TCP_READONLY=1 the daemon serves
    state-changing routes only over its socket, so a direct urllib POST to :8765
    returns 403 and this test failed on a daemon that was working correctly. The
    client falls back to TCP when no socket exists, so an install that has not
    enabled isolation behaves exactly as before.
    """
    import sys as _sys

    lib = str(Path(__file__).resolve().parent.parent / "bin" / "lib")
    if lib not in _sys.path:
        _sys.path.insert(0, lib)
    import writ_daemon_client

    status, text = writ_daemon_client.post_json(
        f"/session/{session_id}/advance-phase", body, base_url=SERVER, timeout=5.0
    )
    if status != 200:
        raise urllib.error.HTTPError(
            f"{SERVER}/session/{session_id}/advance-phase", status,
            f"daemon returned {status}: {text[:200]}", {}, None,
        )
    return json.loads(text)


def _token_path(session_id: str) -> str:
    return os.path.join(tempfile.gettempdir(), f"writ-gate-token-{session_id}")


class TestAdvancePhaseTokenGate:
    def test_tokenless_advance_is_refused(self):
        """The core security property: no token => no advance, regardless of source."""
        if not _server_up():
            pytest.skip("Writ server unreachable")
        sid = f"selfapproval-{uuid.uuid4().hex[:8]}"
        # Ensure no stray token for this fresh session.
        try:
            os.remove(_token_path(sid))
        except OSError:
            pass
        result = _post_advance(sid, {"confirmation_source": "tool"})
        # Must NOT advance, and must say so via an error mentioning the token.
        assert result.get("advanced") is False or "error" in result, result
        assert "token" in json.dumps(result).lower(), (
            f"a tokenless advance must be refused with a token-related error; got {result}"
        )
        assert "phase" not in result or result.get("advanced") is False, result

    def test_valid_token_passes_the_gate(self):
        """With the gate token present and passed, the request clears the token check
        (it no longer returns the token-refusal error)."""
        if not _server_up():
            pytest.skip("Writ server unreachable")
        sid = f"selfapproval-ok-{uuid.uuid4().hex[:8]}"
        token = uuid.uuid4().hex
        path = _token_path(sid)
        with open(path, "w") as f:
            f.write(token)
        try:
            result = _post_advance(sid, {"confirmation_source": "tool", "token": token})
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        # Got past the token gate: the response is a normal advance result, not the
        # token-refusal error.
        refused = result.get("advanced") is False and "token" in json.dumps(result).lower()
        assert not refused, f"a valid token must clear the gate; got {result}"
