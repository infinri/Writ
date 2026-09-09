"""Audit P0: the /advance-phase server route must require the gate token.

The CLI cmd_advance_phase validates a token written by auto-approve-gate.sh (which fires
only on a user approval-pattern prompt the agent cannot forge), logging
`agent_self_approval_blocked` on a missing/wrong token. But the SERVER route -- the
daemon-first path that _writ_session and /writ-approve actually use -- advanced on
`confirmation_source` alone with NO token check, so any caller could POST
{"confirmation_source":"tool"} and self-advance its own gate. That is the self-approval hole:
oversight replaced by an honor-system instruction ("Never fabricate approval").

RED before the route validates the token (a tokenless advance succeeds); GREEN after
(tokenless is refused; a valid token gets past the gate). The token is consumed on a
successful advance.

ISOLATION, CORRECTED (2026-09-09). Earlier revisions of this module claimed it "does not
mutate shared graph/cache state" and deliberately targeted the DEPLOYED interactive daemon
on :8765. Both claims were wrong, in the same direction, and the second is what caused the
first to be false: a full run measurably appended one `agent_self_approval_blocked` row per
run into the operator's REAL `var/logs/github.com/infinri/Writ/audit.jsonl` (1518 rows
before, 1519 after) -- a security-shaped row an auditor reading the trail would mistake for
a genuine attempt by an agent to approve its own gate. The accumulated total is
deliberately not quoted: it moved while this cycle was being verified, because proving the
defect meant running it, so the guard below asserts a DELTA of zero around a run rather
than any absolute count.

The port literal was never even the mechanism that chose the destination:
bin/lib/writ_daemon_client.py's `post_json` prefers an EXISTING unix socket over `base_url`,
defaulting `socket_path` to `WRIT_SOCKET` or `~/.cache/writ/run/writ.sock` -- the interactive
daemon's default socket. The old call passed `base_url=SERVER` and no `socket_path`, so the
request landed on whichever daemon owned the DEFAULT socket regardless of the port named in
`SERVER`. Changing the port alone would have changed nothing about where the row landed.

This module now starts and owns ONE daemon for its own duration (`own_daemon`, via
tests/_daemon.py::start_isolated_daemon / stop_isolated_daemon), on an OS-assigned free port
(tests/fixtures/net.py::free_port()), handing `WRIT_LOG_ROOT`, `WRIT_SOCKET`,
`WRIT_CACHE_DIR` and `WRIT_TCP_READONLY` EXPLICITLY to the subprocess env that starts it, and
passes that daemon's own socket_path explicitly to every `post_json` call so the client can
never fall back onto the interactive daemon's default socket. Every row this module causes
now lands under a throwaway root that daemon's own `/health` reports -- verified by
`_production_audit_guard`, a module-scoped fixture that reads the REAL production
`audit.jsonl` (via tests/_audit_stream.py, which deliberately does not read the same
`WRIT_LOG_ROOT`/`WRIT_FRICTION_LOG` this module's own isolation sets) before and after the
whole module runs, and fails loud with the appended rows named if the count attributable to
this module's `selfapproval` session prefix (or the `agent_self_approval_blocked` event) is
ever nonzero.

Coverage given up, stated rather than silently dropped: nothing here now detects a running
interactive daemon whose code is older than this checkout. That is a deployment-freshness
question (`writ doctor`, `/health`), not a property this test can prove without
reintroducing the leak that motivated this cycle.

This module now runs in CI (it starts its own daemon from this checkout rather than
depending on the operator's interactive singleton), where the old form skipped
unconditionally on an unreachable :8765 and covered nothing.

CORRECTLY RED as of the testing phase: tests/_daemon.py does not yet define
start_isolated_daemon / stop_isolated_daemon (assigned to the implementation phase), so this
module currently fails to COLLECT (ImportError), not merely to pass. Every test below is
written against the contract those two functions and tests/_audit_stream.py's helpers must
satisfy.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import pytest

from tests._audit_stream import (
    attributable_rows,
    count_attributable,
    production_audit_path,
    read_rows,
)
from tests._daemon import start_isolated_daemon, stop_isolated_daemon
from tests.fixtures.net import free_port
from writ.session.gate_token import gate_token_path

_LIB = str(Path(__file__).resolve().parent.parent / "bin" / "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
import writ_daemon_client  # noqa: E402

# The session-id prefix every test in this module mints, and the same prefix
# tests/_audit_stream.py's attribution predicate matches. A single literal so
# the module and the predicate's docstring cannot drift.
SESSION_PREFIX = "selfapproval"


def _token_path(session_id: str) -> str:
    """Delegates to the production resolver so the writer (this test) and the
    daemon's reader (server.read_gate_token -> gate_token_path) always agree,
    including when TMPDIR is set. The former local join (the stdlib tempfile
    module's directory helper, joined with a "writ-gate-token-<session_id>"
    name) would diverge from gate_token_path's hardcoded /tmp the instant
    TMPDIR is set.
    """
    return gate_token_path(session_id)


@pytest.fixture(scope="module")
def _production_audit_guard():
    """Module-scoped before/after measurement of the REAL production audit
    stream at production_audit_path(). own_daemon DEPENDS on this fixture so
    the ordering (count, start daemon, run tests, stop daemon, assert) is
    explicit rather than a consequence of pytest's autouse ordering.

    Reddened by restoring a module-level `SERVER` constant bound to the
    interactive daemon's default port (retargeting the deployed daemon), or
    by dropping the explicit `socket_path` from a `post_json` call so the
    client falls back to the default socket -- either mutation routes a
    request back onto the operator's real daemon, whose refusal row lands in
    the exact file this fixture is watching.
    """
    path = production_audit_path()
    before_rows = read_rows(path)
    before_attributable = count_attributable(before_rows)
    yield
    after_rows = read_rows(path)
    after_attributable = count_attributable(after_rows)
    appended = after_attributable - before_attributable
    assert appended == 0, (
        f"{path} gained {appended} row(s) attributable to this test module "
        f"(session prefix '{SESSION_PREFIX}-' or event "
        f"'agent_self_approval_blocked') across this run: "
        f"{attributable_rows(path)}"
    )


@pytest.fixture(scope="module")
def own_daemon(_production_audit_guard, tmp_path_factory):
    """This module's own daemon: an OS-assigned free port, a throwaway log
    root, socket and cache dir, all handed EXPLICITLY to the subprocess env
    that starts it (tests/_daemon.py::start_isolated_daemon). Torn down at
    module end (stop_isolated_daemon) so the rest of the suite sees the same
    world it would without this file.

    Skips, with a stated reason, when the daemon cannot be brought up: nothing
    answers /health on the free port, the throwaway socket path is over the
    AF_UNIX byte cap, or the socket file never appears. Reddened (as a hang or
    an unguarded failure instead of a skip) by pointing the fixture at a port
    nothing serves.
    """
    tmp_dir = tmp_path_factory.mktemp("advance-phase-token-gate")
    log_root = tmp_dir / "logs"
    daemon = start_isolated_daemon(
        log_root=str(log_root),
        socket_path=str(tmp_dir / "writ.sock"),
        cache_dir=str(tmp_dir / "cache"),
        tcp_readonly=True,
    )
    if daemon is None:
        pytest.skip(
            "could not bring up this module's isolated daemon (free port "
            "unanswered, socket path over the AF_UNIX byte cap, or the "
            "socket file never appeared)"
        )
    daemon["log_root"] = str(log_root)
    yield daemon
    stop_isolated_daemon(daemon)


def _post_advance(daemon: dict, session_id: str, body: dict) -> dict:
    """POST through the shared client, pinned to THIS module's own daemon and
    socket -- never the default socket writ_daemon_client would otherwise
    fall back to.

    Reddened by omitting socket_path: the client then prefers WRIT_SOCKET or
    ~/.cache/writ/run/writ.sock (the interactive daemon's default) over this
    module's own throwaway socket, regardless of what base_url names.
    """
    status, text = writ_daemon_client.post_json(
        f"/session/{session_id}/advance-phase",
        body,
        socket_path=daemon["socket_path"],
        base_url=daemon["base_url"],
        timeout=5.0,
    )
    assert status == 200, f"daemon returned {status}: {text[:200]}"
    return json.loads(text)


class TestOwnDaemonReportsThrowawayLogDestination:
    """The module's daemon must report a log destination inside its own
    throwaway root, not the production path -- the fact the zero-delta guard
    depends on to mean anything."""

    def test_audit_log_is_under_the_fixtures_writ_log_root(self, own_daemon) -> None:
        """Reddened by removing WRIT_LOG_ROOT from the env dict handed to
        ensure-server.sh, or by setting it only in the pytest process
        instead of the subprocess env: the daemon's own /health would then
        report the production audit path instead."""
        audit_log = Path(own_daemon["health"]["audit_log"])
        assert str(audit_log).startswith(own_daemon["log_root"]), (
            f"expected {audit_log} under {own_daemon['log_root']}"
        )

    def test_friction_log_is_under_the_fixtures_writ_log_root(self, own_daemon) -> None:
        friction_log = Path(own_daemon["health"]["friction_log"])
        assert str(friction_log).startswith(own_daemon["log_root"]), (
            f"expected {friction_log} under {own_daemon['log_root']}"
        )

    def test_audit_log_is_not_the_production_path(self, own_daemon) -> None:
        """The positive half of the isolation claim: not merely 'some path
        was set', but 'it is not THE path this whole cycle exists to
        protect'. Reddened by the same env-plumbing mutation as the tests
        above."""
        assert Path(own_daemon["health"]["audit_log"]) != production_audit_path()


class TestAdvancePhaseTokenGate:
    def test_tokenless_advance_is_refused(self, own_daemon) -> None:
        """The core security property: no token => no advance, regardless of
        source. Reddened by returning before the gate_token_valid check in
        writ/server/routes/gate.py."""
        sid = f"{SESSION_PREFIX}-{uuid.uuid4().hex[:8]}"
        try:
            os.remove(_token_path(sid))
        except OSError:
            pass
        result = _post_advance(own_daemon, sid, {"confirmation_source": "tool"})
        assert result.get("advanced") is False or "error" in result, result
        assert "token" in json.dumps(result).lower(), (
            f"a tokenless advance must be refused with a token-related error; got {result}"
        )
        assert "phase" not in result or result.get("advanced") is False, result

    def test_refusal_row_lands_in_the_isolated_stream(self, own_daemon) -> None:
        """Reddened by the same mutation as the destination capability above
        -- and this is what stops the module-scoped zero-delta guard from
        passing vacuously (a correctly-reported destination that nothing
        ever writes to proves nothing)."""
        sid = f"{SESSION_PREFIX}-{uuid.uuid4().hex[:8]}"
        try:
            os.remove(_token_path(sid))
        except OSError:
            pass
        _post_advance(own_daemon, sid, {"confirmation_source": "tool"})
        rows = read_rows(Path(own_daemon["health"]["audit_log"]))
        matches = [
            r for r in rows
            if r.get("session") == sid and r.get("event") == "agent_self_approval_blocked"
        ]
        assert matches, (
            f"expected a refusal row for session {sid} in "
            f"{own_daemon['health']['audit_log']}; got {rows}"
        )

    def test_valid_token_passes_the_gate(self, own_daemon) -> None:
        """With the gate token present and passed, the response is not a
        token-related refusal. Reddened by posting a token different from
        the one written to the token file."""
        sid = f"{SESSION_PREFIX}-ok-{uuid.uuid4().hex[:8]}"
        token = uuid.uuid4().hex
        path = _token_path(sid)
        with open(path, "w") as f:
            f.write(token)
        try:
            result = _post_advance(
                own_daemon, sid, {"confirmation_source": "tool", "token": token}
            )
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        refused = result.get("advanced") is False and "token" in json.dumps(result).lower()
        assert not refused, f"a valid token must clear the gate; got {result}"


class TestTokenPathResolvesThroughProductionResolver:
    def test_token_path_matches_gate_token_path_under_tmpdir(
        self, tmp_path, monkeypatch
    ) -> None:
        """The behavioral half of
        tests/test_production_audit_stream_isolation.py::TestTokenGateModuleResolvesThroughProductionTokenPath's
        source-scan guard. Reddened by restoring the former local join (the
        stdlib tempfile module's directory helper, joined with a
        "writ-gate-token-<session_id>" name): gate_token_path hardcodes
        /tmp (it must match the bash writer auto-approve-gate.sh
        byte-for-byte), so a TMPDIR-based local join diverges from it the
        instant TMPDIR is set, and the writer (this test) and the daemon's
        reader would disagree."""
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        sid = f"{SESSION_PREFIX}-tmpdir-check"
        assert _token_path(sid) == gate_token_path(sid)
