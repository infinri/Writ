"""Cycle E2d skeletons: the doctor must report the daemon's state, not its own.

Isolation is ON as of this cycle: a TCP `POST /session/x/context-percent` returns 403,
`GET /health` still returns 200, and the socket serves everything. `writ doctor` says
"TCP still serves every route".

MEASURED TWO WAYS, which is how the cause was located. Run normally the check prints
"TCP still serves every route (set WRIT_TCP_READONLY=1 ...)" against a daemon that is
demonstrably refusing TCP writes. Run as `WRIT_TCP_READONLY=1 writ doctor` against the
SAME daemon it prints "TCP restricted to the read-only allowlist". So the output
tracks the CLI's environment, not the daemon's: `_socket_state` ends with
`os.environ.get("WRIT_TCP_READONLY")`, and the flag lives in a systemd drop-in that
reaches only the service.

A diagnostic that states the opposite of the truth is worse than silence, and this one
does it in both directions: it will report a control off when it is on, and on when it
is off. The two tests that discriminate are therefore a PAIR, and neither alone is
sufficient:

    daemon ON,  flag absent from the doctor's env  -> must say restricted
    daemon OFF, flag SET in the doctor's env       -> must say open

The first fails today. The second passes today for the wrong reason (the env copy
happens to agree), so it only becomes meaningful once the source of truth moves.

SECOND DEFECT, found by being refused. `writ-quality-judge.sh` PRINTS a
`curl -sX POST http://localhost:8765/...` command for the agent to run (lines 56, 81,
100). Enforcement refuses it, which surfaced while posting this cycle's own plan
judgment. It is the only printed instruction of its kind in `hooks/` or `agents/`, and
the rule below stops the next one being written.

Per ENF-GATE-007: skeletons written and approved before implementation.
Per ABS-TESTING-041: the health-payload tests drive the real ASGI app, and the
instruction rule reads the hooks that actually print instructions.
Per TEST-ISOLATE-003: `_socket_state` is exercised through a stubbed fetch, never
against the developer's live daemon, so a test cannot depend on whether isolation
happens to be enabled on this machine.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "hooks" / "scripts"

try:
    from httpx import ASGITransport, AsyncClient
except ImportError:  # pragma: no cover
    pytestmark = pytest.mark.skip(reason="httpx not installed")


def _require(module, *names) -> None:
    missing = [n for n in names if not hasattr(module, n)]
    if missing:
        pytest.fail(f"skeleton: {module.__name__} has no {', '.join(missing)} yet")


# --------------------------------------------------------------------------- #
# Capability 1, 2, 3: the daemon reports its own enforcement state
# --------------------------------------------------------------------------- #

class TestHealthCarriesTheFlag:
    """`/health` already carries two facts of exactly this shape: `cache_dir` so
    ensure-server.sh can detect a cache desync, and `friction_log` so the suite can
    align on the daemon's log. `tcp_readonly` is the third, and it costs one boolean.
    """

    @staticmethod
    async def _health(monkeypatch, enforcement: str | None) -> dict:
        from writ.server import app

        if enforcement is None:
            monkeypatch.delenv("WRIT_TCP_READONLY", raising=False)
        else:
            monkeypatch.setenv("WRIT_TCP_READONLY", enforcement)
        with patch("writ.server.writ_session", MagicMock()):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                response = await ac.get("/health")
        return response.json()

    @pytest.mark.asyncio
    async def test_health_reports_the_flag_as_a_boolean(self, monkeypatch) -> None:
        body = await self._health(monkeypatch, None)
        assert "tcp_readonly" in body, (
            f"/health does not report tcp_readonly; keys={sorted(body)}"
        )
        assert isinstance(body["tcp_readonly"], bool), (
            f"tcp_readonly is {type(body['tcp_readonly']).__name__}, not bool"
        )

    @pytest.mark.asyncio
    async def test_it_is_true_when_the_daemon_has_enforcement_on(self, monkeypatch) -> None:
        body = await self._health(monkeypatch, "1")
        assert body.get("tcp_readonly") is True, body.get("tcp_readonly")

    @pytest.mark.asyncio
    async def test_it_is_false_when_the_daemon_has_enforcement_off(self, monkeypatch) -> None:
        body = await self._health(monkeypatch, None)
        assert body.get("tcp_readonly") is False, body.get("tcp_readonly")


# --------------------------------------------------------------------------- #
# Capability 4, 5, 6: the report follows the daemon
# --------------------------------------------------------------------------- #

class TestTheReportFollowsTheDaemon:
    """THE DISCRIMINATING PAIR. Either test alone can pass while the check reads the
    wrong process, because an env copy agrees with the daemon whenever the two happen
    to match. Only the crossed cases separate them.
    """

    @staticmethod
    def _state(doctor, health: dict | None) -> dict:
        """`_socket_state` with the daemon's health response stubbed."""
        with patch.object(doctor, "_socket_health", return_value=health):
            return doctor._socket_state()

    def test_it_says_restricted_when_the_daemon_says_so(self, monkeypatch) -> None:
        """The failing half today: the daemon enforces, the CLI env is clean, and the
        check currently reports the opposite."""
        from writ.session import doctor

        _require(doctor, "_socket_health")
        monkeypatch.delenv("WRIT_TCP_READONLY", raising=False)
        with patch.object(doctor, "_socket_state", doctor._socket_state):
            state = self._state(doctor, {"status": "healthy", "tcp_readonly": True})
        assert state.get("tcp_readonly") is True, (
            "the doctor did not take the daemon's word for it; "
            f"state={ {k: v for k, v in state.items() if k != 'path'} }"
        )

    def test_it_says_open_when_the_daemon_says_so(self, monkeypatch) -> None:
        """The other half: the CLI env claims enforcement, the daemon denies it, and
        the daemon wins. Passes today only because the env copy happens to agree in
        the uncrossed case; this is the crossed one."""
        from writ.session import doctor

        _require(doctor, "_socket_health")
        monkeypatch.setenv("WRIT_TCP_READONLY", "1")
        state = self._state(doctor, {"status": "healthy", "tcp_readonly": False})
        assert state.get("tcp_readonly") is False, (
            "the doctor believed its own environment over the daemon"
        )

    def test_it_says_unknown_when_neither_transport_answers(self, monkeypatch) -> None:
        """A third state, not a default. Reporting restricted would be the same false
        confidence being fixed; reporting open would be the same falsehood inverted."""
        from writ.session import doctor

        _require(doctor, "_socket_health")
        monkeypatch.setenv("WRIT_TCP_READONLY", "1")
        state = self._state(doctor, None)
        assert state.get("tcp_readonly") is None, (
            f"an unreachable daemon produced a definite answer: {state.get('tcp_readonly')!r}"
        )

    def test_the_detail_distinguishes_all_three_states(self, monkeypatch) -> None:
        """The operator reads the detail string, not the dict."""
        from writ.session import doctor

        _require(doctor, "_socket_health", "check_daemon_socket")
        seen = {}
        for label, flag in (("on", True), ("off", False), ("unknown", None)):
            monkeypatch.setattr(
                doctor, "_socket_state",
                lambda flag=flag: {"path": "/run/w.sock", "exists": True,
                                   "dir_mode": 0o700, "answers": True,
                                   "tcp_readonly": flag},
            )
            seen[label] = doctor.check_daemon_socket(doctor.DoctorOptions()).detail
        assert len({seen["on"], seen["off"], seen["unknown"]}) == 3, (
            f"two states render identically: {seen}"
        )
        assert "restrict" in seen["on"].lower(), seen["on"]
        assert "could not" in seen["unknown"].lower() or "unknown" in seen["unknown"].lower(), (
            seen["unknown"]
        )


# --------------------------------------------------------------------------- #
# Capability 7, 8: where the answer is fetched from
# --------------------------------------------------------------------------- #

class TestTheFetchPrefersTheSocketAndFallsBackToTcp:

    def test_the_socket_is_tried_first(self) -> None:
        """The socket is the authority for whether the socket works, and it answers
        both questions in one request."""
        from writ.session import doctor

        _require(doctor, "_socket_health")
        source = doctor.__file__
        text = Path(source).read_text()
        assert "AF_UNIX" in text, "the health fetch no longer speaks to the socket"

    def test_it_falls_back_to_tcp_health(self) -> None:
        """Enforcement leaves `/health` allowlisted precisely so this works when the
        socket is missing, which is exactly when the answer is most in doubt."""
        from writ.session import doctor

        _require(doctor, "_socket_health")
        text = Path(doctor.__file__).read_text()
        assert "_HEALTH_URL" in text or "8765" in text, (
            "there is no TCP fallback for the health fetch"
        )

    def test_an_unanswering_socket_still_warns(self, monkeypatch) -> None:
        """Must-not-regress: the E2a branch that caught the orphaned socket."""
        from writ.session import doctor

        _require(doctor, "_socket_state", "check_daemon_socket")
        monkeypatch.setattr(
            doctor, "_socket_state",
            lambda: {"path": "/run/w.sock", "exists": True, "dir_mode": 0o700,
                     "answers": False, "tcp_readonly": None},
        )
        result = doctor.check_daemon_socket(doctor.DoctorOptions())
        assert result.status == doctor.STATUS_WARN, result.detail
        assert "did not answer" in result.detail, result.detail

    def test_a_world_readable_socket_directory_still_fails(self, monkeypatch) -> None:
        """Must-not-regress: uvicorn makes the socket 0666, so the directory mode is
        the whole control and reporting it must not be diluted by the new clause."""
        from writ.session import doctor

        _require(doctor, "_socket_state", "check_daemon_socket")
        monkeypatch.setattr(
            doctor, "_socket_state",
            lambda: {"path": "/run/w.sock", "exists": True, "dir_mode": 0o755,
                     "answers": True, "tcp_readonly": True},
        )
        result = doctor.check_daemon_socket(doctor.DoctorOptions())
        assert result.status == doctor.STATUS_FAIL, result.detail
        assert "755" in result.detail or "0o755" in result.detail, result.detail


# --------------------------------------------------------------------------- #
# Capability 12, 13: no hook may print an instruction the daemon refuses
# --------------------------------------------------------------------------- #

class TestNoHookPrintsARefusedInstruction:
    """Found by being refused: the quality-judge hook prints a TCP POST for the agent
    to run, and enforcement returns 403 on it. This rule is what stops the next one.
    """

    # A printed instruction is a POST to the daemon's TCP port. GETs are allowlisted,
    # so only writes are refused, and only writes are matched here.
    _TCP_WRITE_INSTRUCTION = re.compile(
        r"curl[^\n]*-X\s*POST[^\n]*http://(?:localhost|127\.0\.0\.1):8765"
        r"|curl[^\n]*-sX\s*POST[^\n]*http://(?:localhost|127\.0\.0\.1):8765"
    )

    def test_the_quality_judge_hook_prints_a_usable_command(self) -> None:
        source = (HOOKS / "writ-quality-judge.sh").read_text()
        offenders = [
            f"line {i}" for i, line in enumerate(source.splitlines(), start=1)
            if self._TCP_WRITE_INSTRUCTION.search(line)
        ]
        assert not offenders, (
            f"the hook prints a POST the daemon refuses under enforcement: {offenders}. "
            "Print the --unix-socket form."
        )

    def test_the_printed_command_is_runnable_as_printed(self) -> None:
        """Not merely absent-TCP, and not merely containing the flag.

        The first fix printed a bare `$WRIT_SOCKET`, which is UNSET in an ordinary
        shell, so the command expanded to `--unix-socket ` and failed. My test passed
        it because it only looked for the flag. An instruction has to run as printed,
        so the socket argument must carry its own default.
        """
        source = (HOOKS / "writ-quality-judge.sh").read_text()
        printed = [ln for ln in source.splitlines() if "--unix-socket" in ln]
        assert printed, (
            "the hook prints no socket-based command, so the agent has nothing to run"
        )
        for line in printed:
            assert "WRIT_SOCKET:-" in line or "/writ.sock" in line, (
                f"the printed socket argument has no default and would expand to "
                f"nothing in a shell that has not set it: {line.strip()}"
            )

    def test_no_other_hook_prints_one_either(self) -> None:
        """The rule, applied to every hook rather than to the one that broke."""
        offenders = []
        for path in sorted(HOOKS.glob("*.sh")):
            for i, line in enumerate(path.read_text().splitlines(), start=1):
                if self._TCP_WRITE_INSTRUCTION.search(line):
                    offenders.append(f"{path.name}:{i}")
        assert not offenders, (
            f"hooks print daemon writes over a refused transport: {offenders}"
        )

    def test_the_rule_would_catch_a_regression(self) -> None:
        """A detector that matches nothing proves nothing: the E2c lesson, applied
        here before the fix rather than after."""
        assert self._TCP_WRITE_INSTRUCTION.search(
            "  curl -sX POST http://localhost:8765/session/$SESSION_ID/quality-judgment \\\\"
        ), "the rule does not match the exact line that broke"
        assert not self._TCP_WRITE_INSTRUCTION.search(
            "  curl -s --unix-socket $WRIT_SOCKET -X POST http://localhost/session/x/y"
        ), "the rule flags the socket form it is meant to allow"
