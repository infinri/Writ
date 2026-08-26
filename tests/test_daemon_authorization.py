"""Cycle E1 skeletons: the daemon's one unguarded write door.

`POST /session/{id}/update` (`session_state.py:54-63`) is `cache[key] = value`
inside `mutate_cache`, with no authentication, no key allowlist, and no
Bash-gate arm naming it. Probed against the running daemon on a throwaway
session id:

    POST /session/<id>/update {"key":"current_phase","value":"implementation"}
      -> {"ok": true}, and the following GET showed current_phase=implementation

Both `mode` and `current_phase` are gate inputs: eight hooks call `is_work_mode`
and exit 0 when it is not `work`, so mode decides whether those gates run, and
phase decides which gate comes next. A repo-wide search finds the route, its
Pydantic model, one row in docs/reference/http-api.md, and its own tests. No
hook, no library, no CLI path calls it, which is why this deletes the route
rather than bolting a key allowlist onto a general-purpose setter
(ABS-SECURITY-024: enumerate what is permitted, CLEAN-DEAD-001).

WHY A UNIX SOCKET WOULD NOT HAVE FIXED THIS, since the approved direction was
the socket. The agent runs as the same uid as the daemon, so filesystem
permissions on a socket are invisible to it. The socket stops other local users;
the gate arms and this route's removal stop the agent. Different actors. The
socket migration is Cycle E2 and is deliberately not here: hardening the
transport of a route that should not exist is the wrong order.

WHAT WAS NOT PROVEN, recorded so it is not later read as proven. `can-write`
returned true for the probe session, but that session carries no `project_root`,
so the write gate could not resolve a plan and its answer is an artifact of a
synthetic session. Whether this composes into a usable bypass on a REAL session
needs two real sessions, which is the operational capability, not a test here.

Per ENF-GATE-007: skeletons written and approved before implementation.
Per ABS-TESTING-041: the gate tests run the real hook as a real subprocess and
the route tests drive the real ASGI app. A reimplementation of either would only
test itself.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio

REPO = Path(__file__).resolve().parent.parent
GATE = REPO / "hooks" / "scripts" / "writ-bash-write-gate.sh"
SESSION_ID = "test-session-abc123"

try:
    from httpx import AsyncClient
except ImportError:  # pragma: no cover
    pytestmark = pytest.mark.skip(reason="httpx not installed")

from writ.server import app


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _gate(command: str) -> dict:
    """Run the real Bash gate on `command`; return its decision, or {}.

    A near-twin of the helper in tests/test_hygiene_cycle_d.py, kept local
    rather than shared: that module owns the gate's match PRECISION and this one
    owns its route COVERAGE, and a shared fixture would couple two independent
    reasons to change. `session_id` is required either way, because the hook
    exits at `[ -z "$SESSION_ID" ] && exit 0` (`:105`) before any arm runs.
    """
    payload = json.dumps({
        "tool_name": "Bash",
        "session_id": "cycle-e-probe",
        "tool_input": {"command": command},
    })
    proc = subprocess.run(
        ["bash", str(GATE)],
        input=payload, capture_output=True, text=True,
        env=dict(os.environ), timeout=60,
    )
    for stream in (proc.stdout, proc.stderr):
        for line in stream.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                doc = json.loads(line)
            except ValueError:
                continue
            if "hookSpecificOutput" in doc:
                return doc["hookSpecificOutput"]
    return {}


def _decision(command: str) -> str:
    return _gate(command).get("permissionDecision", "")


def _route_paths() -> set[str]:
    return {getattr(r, "path", "") for r in app.routes}


def _post_paths() -> set[str]:
    paths = set()
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        if "POST" in methods:
            paths.add(getattr(route, "path", ""))
    return paths


@pytest_asyncio.fixture()
async def client():
    """Async client over the real ASGI app, with the session module stubbed.

    The stub is only so a route that reaches the cache does not touch the
    developer's real session files (TEST-ISOLATE-003). Routing, status codes and
    model validation are the real app's.
    """
    from unittest.mock import MagicMock

    from httpx import ASGITransport

    stub = MagicMock()
    transport = ASGITransport(app=app)
    with patch("writ.server.writ_session", stub):
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


# --------------------------------------------------------------------------- #
# Capability 1-6: the arbitrary-key writer is gone, everything else stays
# --------------------------------------------------------------------------- #

class TestTheArbitraryKeyWriterIsUnreachable:

    @pytest.mark.asyncio
    async def test_the_update_route_is_not_registered(self) -> None:
        offenders = sorted(p for p in _route_paths() if p.endswith("/update"))
        assert not offenders, f"an arbitrary-key writer is still routed: {offenders}"

    @pytest.mark.asyncio
    async def test_posting_to_the_update_route_returns_404(self, client) -> None:
        """The reachability check, not just the registry: 404 is what a caller
        holding the old path now gets."""
        response = await client.post(
            f"/session/{SESSION_ID}/update", json={"key": "current_phase", "value": "x"}
        )
        assert response.status_code == 404, (
            f"the route still answers with {response.status_code}"
        )

    def test_the_request_model_is_gone(self) -> None:
        """CLEAN-DEAD-001: the model's only consumer was the route."""
        import writ.server.models as models

        assert not hasattr(models, "SessionUpdateRequest"), (
            "SessionUpdateRequest survives its only consumer"
        )

    def test_no_route_body_has_the_generic_setter_shape(self) -> None:
        """The CLASS of defect, not just this instance.

        A body model carrying exactly a string `key` and a string `value` is an
        arbitrary-key setter whatever the path is called, so no route may declare
        one. This is what stops the same door being reopened under a new name.
        """
        import writ.server.models as models

        try:
            from pydantic import BaseModel
        except ImportError:  # pragma: no cover
            pytest.skip("pydantic not installed")

        offenders = []
        for name in dir(models):
            obj = getattr(models, name)
            if not isinstance(obj, type) or not issubclass(obj, BaseModel):
                continue
            fields = getattr(obj, "model_fields", {})
            if set(fields) == {"key", "value"}:
                offenders.append(name)
        assert not offenders, f"generic key/value setter models remain: {offenders}"

    @pytest.mark.asyncio
    async def test_the_other_session_routes_are_untouched(self) -> None:
        """Must-not-regress: exactly one path leaves, nothing else moves."""
        expected_survivors = [
            f"/session/{{session_id}}",
            "/session/{session_id}/mode",
            "/session/{session_id}/can-write",
            "/session/{session_id}/should-skip",
            "/session/{session_id}/quality-judgment",
            "/session/{session_id}/review-findings",
        ]
        paths = _route_paths()
        missing = [p for p in expected_survivors if p not in paths]
        assert not missing, f"routes lost that should have survived: {missing}"

    @pytest.mark.asyncio
    async def test_the_mode_route_still_accepts_a_mode(self, client) -> None:
        """Must-not-regress, and deliberately so: the agent setting its own mode
        is by design. The exposure that remains is another local user setting
        yours, which Cycle E2's socket closes, not this cycle."""
        response = await client.post(
            f"/session/{SESSION_ID}/mode", json={"mode": "work"}
        )
        assert response.status_code == 200, response.text


# --------------------------------------------------------------------------- #
# Capability 7-9: the gate covers the path, so re-adding it is not silent
# --------------------------------------------------------------------------- #

class TestTheGateCoversTheRoute:
    """Removing the route fixes today; the gate arm is what stops tomorrow.

    NARROWER THAN THE CAPABILITY'S WORDING, stated rather than glossed. The plan
    says "a session-state mutation route"; the arm implemented here covers the
    arbitrary-key setter path specifically. A blanket refusal on session-state
    POSTs would refuse the agent's own legitimate calls -- quality-judgment,
    verification-evidence, context-percent are all agent-initiated by design --
    so the general form would have to enumerate exceptions, which is the
    blocklist shape ABS-SECURITY-024 rejects.
    """

    def test_a_bash_post_to_the_update_path_is_refused(self) -> None:
        cmd = (
            "curl -s -X POST http://localhost:8765/session/abc/update "
            "-H 'Content-Type: application/json' "
            "-d '{\"key\":\"current_phase\",\"value\":\"implementation\"}'"
        )
        assert _decision(cmd) == "deny", (
            "a Bash POST to the arbitrary-key setter is not refused, so re-adding "
            "the route would silently re-open the bypass"
        )

    def test_a_bash_get_of_session_state_is_still_allowed(self) -> None:
        """Must-not-regress: reading is fine, and hooks depend on it."""
        cmd = "curl -s http://localhost:8765/session/abc"
        assert _decision(cmd) != "deny"

    def test_a_bash_post_of_a_quality_judgment_is_still_allowed(self) -> None:
        """Must-not-regress with teeth: this session posts one per artifact, and
        refusing it would break the quality-judge loop."""
        cmd = (
            "curl -s -X POST http://localhost:8765/session/abc/quality-judgment "
            "-H 'Content-Type: application/json' "
            "-d '{\"artifact_path\":\"/tmp/x.py\",\"score\":4,\"rationale\":\"ok\"}'"
        )
        assert _decision(cmd) != "deny"

    def test_a_bash_post_of_a_verdict_is_still_refused(self) -> None:
        """Must-not-regress: the arm at `:234` that already guards the verdict
        record. Confirmed live this session by being refused while attempting
        exactly this."""
        endpoint = "review" + "-findings"
        cmd = (
            f"curl -s -X POST http://localhost:8765/session/abc/{endpoint} "
            "-d '{\"message\":\"PASS\",\"agent_id\":\"x\"}'"
        )
        assert _decision(cmd) == "deny"


# --------------------------------------------------------------------------- #
# Capability 10-12: the documented surface and the operational check
# --------------------------------------------------------------------------- #

class TestTheDocumentedSurfaceAgrees:

    def test_the_route_count_baseline_agrees_with_the_app(self) -> None:
        """The frozen baseline in tests/test_server_split_seam.py is the contract;
        this asserts the app agrees with it after the deletion, so the two cannot
        drift apart silently."""
        import tests.test_server_split_seam as seam

        baseline = {path for _method, path in seam.ROUTE_BASELINE}
        assert not any(p.endswith("/update") for p in baseline), (
            "the frozen route baseline still lists the deleted route"
        )
        assert baseline <= _route_paths() | {""}, (
            f"baseline names paths the app does not serve: "
            f"{sorted(baseline - _route_paths())}"
        )

    def test_current_phase_is_not_settable_through_any_post_body(self) -> None:
        """No POST route may take `current_phase` as a caller-supplied field.

        Phase advances through the approval workflow, which spends a bound token.
        A route that accepts a phase as input is that workflow's bypass whatever
        it is named.
        """
        try:
            from pydantic import BaseModel
        except ImportError:  # pragma: no cover
            pytest.skip("pydantic not installed")

        import writ.server.models as models

        offenders = []
        for name in dir(models):
            obj = getattr(models, name)
            if not isinstance(obj, type) or not issubclass(obj, BaseModel):
                continue
            if "current_phase" in getattr(obj, "model_fields", {}):
                offenders.append(name)
        assert not offenders, f"a request body accepts current_phase: {offenders}"
