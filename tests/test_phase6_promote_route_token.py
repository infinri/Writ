"""Phase 6.3c (security): the /promote-candidate route must require the gate token.

The canon write -- graduating a self-proposed rule into the trusted bible/ source -- is the
one seam where Writ could write its own memory unsupervised. Like /advance-phase, the route
must REFUSE without the agent-unforgeable token that auto-approve-gate.sh writes only on a
genuine user approval prompt. A tokenless promotion is the self-approval hole.

Integration test against the live daemon (skips if unreachable). It targets a NONEXISTENT
candidate on the valid-token path so the gate clears but no real bible/ file is written.

A VALID token is now a BOUND one (cycle 1: line 1 the secret, line 2 the gate it
authorizes, line 3 the plan fingerprint), minted through the shared fixture so the
binding is derived from the session cache exactly as the production mint derives it. The
one-line file this file used to write as its "valid token" is the pre-binding format, and
the route refuses it: an unbound token records nothing about what it authorized, so it
cannot be checked at all, and accepting it left the canon write reachable by anything able
to put a single line into /tmp/writ-gate-token-<sid>. That refusal is asserted below
rather than dropped -- it is the same fail-open branch class the advance route lost.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid

import pytest

from tests._daemon import _port
from tests.fixtures.session_state import write_bound_gate_token

SERVER = f"http://localhost:{_port()}"


def _server_up() -> bool:
    try:
        with urllib.request.urlopen(f"{SERVER}/health", timeout=2):
            return True
    except (urllib.error.URLError, OSError):
        return False


def _post_promote(session_id: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{SERVER}/session/{session_id}/promote-candidate",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _token_path(session_id: str) -> str:
    """The production resolver, not a local reconstruction of it.

    gate_token_path hardcodes /tmp on purpose (its own docstring: the bash writer and the
    python reader must never be able to disagree on where the file lives), so a
    gettempdir() copy here would point somewhere else the moment $TMPDIR is set -- the
    mint below would write one path while the cleanup removed another, leaking a token
    file into the real /tmp.
    """
    from writ.session.gate_token import gate_token_path

    return gate_token_path(session_id)


class TestPromoteCandidateTokenGate:
    def test_tokenless_promote_is_refused(self) -> None:
        if not _server_up():
            pytest.skip("Writ server unreachable")
        sid = f"promo-noauth-{uuid.uuid4().hex[:8]}"
        try:
            os.remove(_token_path(sid))
        except OSError:
            pass
        result = _post_promote(sid, {"candidate_id": "ZZZ-NOEXIST-001"})
        assert result.get("promoted") is False
        assert "token" in json.dumps(result).lower(), (
            f"a tokenless promotion must be refused with a token error; got {result}"
        )

    def test_valid_token_clears_gate(self) -> None:
        if not _server_up():
            pytest.skip("Writ server unreachable")
        sid = f"promo-ok-{uuid.uuid4().hex[:8]}"
        # A fresh session id has no cache, so the derived binding is gate="" (no phase
        # gate pending) with an empty plan fingerprint -- the state a promotion approval
        # is actually given in, and the one this route requires.
        token = write_bound_gate_token(sid)
        path = _token_path(sid)
        try:
            # Nonexistent candidate: the request clears the token gate, then promote_candidate
            # returns a not-found error -- proving the refusal was NOT a token refusal and no
            # canon was written.
            result = _post_promote(sid, {"candidate_id": "ZZZ-NOEXIST-001", "token": token})
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        refused_for_token = "token" in json.dumps(result).lower()
        assert not refused_for_token, f"a valid token must clear the gate; got {result}"
        assert result.get("promoted") is False  # candidate does not exist

    def test_unbound_one_line_token_is_refused(self) -> None:
        """The pre-binding format, over HTTP: refused, and told apart from "no token".

        The in-process mirror (tests/test_gate_token_binding.py::
        TestPromoteCandidateBinding) proves promote_candidate is never reached; this one
        proves the daemon serving the route agrees, since the same file used to send a
        one-line token through here and call it valid.
        """
        if not _server_up():
            pytest.skip("Writ server unreachable")
        sid = f"promo-unbound-{uuid.uuid4().hex[:8]}"
        token = uuid.uuid4().hex
        path = _token_path(sid)
        with open(path, "w") as f:
            f.write(token)  # one line, no gate, no plan fingerprint
        try:
            result = _post_promote(sid, {"candidate_id": "ZZZ-NOEXIST-001", "token": token})
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        assert result.get("promoted") is False
        error = (result.get("error") or "").lower()
        assert "approve again" in error, (
            f"an unbound token must be refused with the shared unbound reason; got {result}"
        )
        assert "invalid or missing gate token" not in error, (
            "a token that is PRESENT and matching but unbound must not be reported as "
            f"missing -- that is the message the user cannot act on; got {result}"
        )
