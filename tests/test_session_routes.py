"""Integration tests for /session/* FastAPI routes.

Per TEST-TDD-001: skeletons approved before implementation.
Per PY-ASYNC-001: route handlers use async def with asyncio.to_thread().
Per PY-PYDANTIC-001: request/response bodies use Pydantic models.
Per PERF-IO-001: no event-loop blocking in route handlers.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from tests.conftest import writ_server_source

# httpx.AsyncClient is the standard async test client for FastAPI / Starlette.
try:
    from httpx import AsyncClient
except ImportError:
    pytestmark = pytest.mark.skip(reason="httpx not installed")

# Import the FastAPI app.
from writ.server import app  # type: ignore[import]

# Import future Pydantic request/response models.
# ImportError is expected until implementation lands.
try:
    from writ.server import (
        SessionUpdateRequest,
        SessionModeSetRequest,
        SessionCanWriteRequest,
        SessionFormatRequest,
        SessionAutoFeedbackRequest,
        SessionAddViolationRequest,
    )
except ImportError:
    SessionUpdateRequest = None  # type: ignore[assignment,misc]
    SessionModeSetRequest = None  # type: ignore[assignment,misc]
    SessionCanWriteRequest = None  # type: ignore[assignment,misc]
    SessionFormatRequest = None  # type: ignore[assignment,misc]
    SessionAutoFeedbackRequest = None  # type: ignore[assignment,misc]
    SessionAddViolationRequest = None  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


SESSION_ID = "test-session-abc123"


@pytest.fixture()
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture()
def mock_writ_session():
    """Mock the underlying writ_session module so routes never touch the filesystem.

    The server routes call _read_cache / _write_cache directly (not cmd_*),
    so the mock must expose those callables with correct return types.
    """
    session_data: dict[str, Any] = {
        "session_id": SESSION_ID,
        "mode": "Work",
        "current_phase": "planning",
        "remaining_budget": 8000,
        "context_percent": 0,
        "loaded_rule_ids": [],
        "queries": 0,
        "pending_violations": [],
        "escalation": {"needed": False},
        "invalidation_history": {},
    }
    mock = MagicMock()
    mock._read_cache = MagicMock(return_value=dict(session_data))
    mock._write_cache = MagicMock(return_value=None)
    mock.DEFAULT_SESSION_BUDGET = 8000
    mock.cmd_format = MagicMock(return_value=None)
    # /can-write now runs the real gate (_can_write_check); /auto-feedback now runs
    # the real correlation (cmd_auto_feedback). The routes are no longer stubs, so the
    # mock must expose those callables with realistic shapes.
    mock._can_write_check = MagicMock(return_value={"can_write": True, "reason": None})
    mock.cmd_auto_feedback = MagicMock(return_value=None)
    # /should-skip delegates to cmd_should_skip (single policy source) and derives
    # `known` from the cache FILE's existence via _cache_path.
    mock.cmd_should_skip = MagicMock(return_value=False)
    mock._cache_path = MagicMock(return_value="/nonexistent/mock-session-cache.json")
    return mock


@pytest_asyncio.fixture()
async def client(mock_writ_session):
    """Async HTTP client wired to the FastAPI app with patched session module."""
    from httpx import ASGITransport

    transport = ASGITransport(app=app)
    with patch("writ.server.writ_session", mock_writ_session):
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


# ---------------------------------------------------------------------------
# TestSessionRead
# ---------------------------------------------------------------------------


class TestSessionRead:
    """GET /session/{session_id}"""

    @pytest.mark.asyncio
    async def test_read_returns_200(self, client: AsyncClient) -> None:
        """GET /session/{session_id} returns HTTP 200 for a known session."""
        response = await client.get(f"/session/{SESSION_ID}")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_read_response_contains_session_id(self, client: AsyncClient) -> None:
        """Response body includes the session_id field."""
        response = await client.get(f"/session/{SESSION_ID}")
        assert response.json().get("session_id") == SESSION_ID


# ---------------------------------------------------------------------------
# TestSessionUpdate
# ---------------------------------------------------------------------------


class TestSessionUpdate:
    """POST /session/{session_id}/update"""

    @pytest.mark.asyncio
    async def test_update_returns_200(self, client: AsyncClient) -> None:
        """POST /session/{session_id}/update with valid body returns HTTP 200."""
        payload = {"key": "mode", "value": "Work"}
        response = await client.post(f"/session/{SESSION_ID}/update", json=payload)
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_update_rejects_missing_body(self, client: AsyncClient) -> None:
        """POST /session/{session_id}/update with no body returns HTTP 422."""
        response = await client.post(f"/session/{SESSION_ID}/update")
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# TestSessionShouldSkip
# ---------------------------------------------------------------------------


class TestSessionShouldSkip:
    """GET /session/{session_id}/should-skip"""

    @pytest.mark.asyncio
    async def test_should_skip_returns_bool(self, client: AsyncClient) -> None:
        """GET /session/{session_id}/should-skip returns a boolean value."""
        response = await client.get(f"/session/{SESSION_ID}/should-skip")
        assert response.status_code == 200
        assert isinstance(response.json().get("should_skip"), bool)


# ---------------------------------------------------------------------------
# TestSessionModeGet
# ---------------------------------------------------------------------------


class TestSessionModeGet:
    """GET /session/{session_id}/mode"""

    @pytest.mark.asyncio
    async def test_mode_get_returns_string(self, client: AsyncClient) -> None:
        """GET /session/{session_id}/mode returns mode as a string."""
        response = await client.get(f"/session/{SESSION_ID}/mode")
        assert response.status_code == 200
        assert isinstance(response.json().get("mode"), str)


# ---------------------------------------------------------------------------
# TestSessionModeSet
# ---------------------------------------------------------------------------


class TestSessionModeSet:
    """POST /session/{session_id}/mode"""

    @pytest.mark.asyncio
    async def test_mode_set_returns_200(self, client: AsyncClient) -> None:
        """POST /session/{session_id}/mode with valid mode returns HTTP 200."""
        response = await client.post(f"/session/{SESSION_ID}/mode", json={"mode": "Work"})
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_mode_set_rejects_empty_body(self, client: AsyncClient) -> None:
        """POST /session/{session_id}/mode with empty body returns HTTP 422."""
        response = await client.post(f"/session/{SESSION_ID}/mode", json={})
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# TestSessionCanWrite
# ---------------------------------------------------------------------------


class TestSessionCanWrite:
    """POST /session/{session_id}/can-write"""

    @pytest.mark.asyncio
    async def test_can_write_returns_bool(self, client: AsyncClient) -> None:
        """POST /session/{session_id}/can-write returns a boolean."""
        response = await client.post(f"/session/{SESSION_ID}/can-write", json={})
        assert response.status_code == 200
        assert isinstance(response.json().get("can_write"), bool)

    @pytest.mark.asyncio
    async def test_can_write_delegates_to_real_gate(
        self, client: AsyncClient, mock_writ_session
    ) -> None:
        """The route must run the REAL gate (_can_write_check), not a permissive stub.

        Audit #2: /can-write previously returned True unconditionally in work mode,
        ignoring the test-skeleton / phase-a gates -- a silent write bypass.
        """
        response = await client.post(
            f"/session/{SESSION_ID}/can-write",
            json={"tool_input": {"file_path": "src/main.py"}, "skill_dir": "/x"},
        )
        assert response.status_code == 200
        mock_writ_session._can_write_check.assert_called_once()
        call = mock_writ_session._can_write_check.call_args
        assert call.args[0] == SESSION_ID
        assert call.args[1]["tool_input"] == {"file_path": "src/main.py"}
        assert call.args[2] == "/x"

    @pytest.mark.asyncio
    async def test_can_write_propagates_deny(
        self, client: AsyncClient, mock_writ_session
    ) -> None:
        """A gate denial from _can_write_check must surface as can_write=False + reason."""
        mock_writ_session._can_write_check.return_value = {
            "can_write": False,
            "reason": "[ENF-GATE-PLAN] Gate not approved",
        }
        response = await client.post(
            f"/session/{SESSION_ID}/can-write",
            json={"tool_input": {"file_path": "src/main.py"}},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["can_write"] is False
        assert "ENF-GATE-PLAN" in (body.get("reason") or "")


# ---------------------------------------------------------------------------
# TestSessionAdvancePhase
# ---------------------------------------------------------------------------


class TestSessionAdvancePhase:
    """POST /session/{session_id}/advance-phase"""

    @pytest.mark.asyncio
    async def test_advance_phase_returns_200(self, client: AsyncClient) -> None:
        """POST /session/{session_id}/advance-phase returns HTTP 200."""
        response = await client.post(f"/session/{SESSION_ID}/advance-phase", json={})
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_advance_phase_returns_new_phase(self, client: AsyncClient) -> None:
        """Response body contains the new phase value. The advance route now requires the
        gate token (audit P0 self-approval fix), so write it and pass it."""
        import os
        import tempfile
        tok = "test-gate-token-xyz"
        tok_path = os.path.join(tempfile.gettempdir(), f"writ-gate-token-{SESSION_ID}")
        with open(tok_path, "w") as f:
            f.write(tok)
        try:
            response = await client.post(
                f"/session/{SESSION_ID}/advance-phase",
                json={"confirmation_source": "tool", "token": tok},
            )
        finally:
            try:
                os.remove(tok_path)
            except OSError:
                pass
        assert "phase" in response.json()

    @pytest.mark.asyncio
    async def test_advance_phase_without_token_is_refused(self, client: AsyncClient) -> None:
        """Audit P0: a tokenless advance must be refused (self-approval hole closed)."""
        response = await client.post(
            f"/session/{SESSION_ID}/advance-phase", json={"confirmation_source": "tool"}
        )
        body = response.json()
        assert body.get("advanced") is False and "token" in str(body).lower(), body


# ---------------------------------------------------------------------------
# TestSessionCurrentPhase
# ---------------------------------------------------------------------------


class TestSessionCurrentPhase:
    """GET /session/{session_id}/current-phase"""

    @pytest.mark.asyncio
    async def test_current_phase_returns_string(self, client: AsyncClient) -> None:
        """GET /session/{session_id}/current-phase returns phase as a string."""
        response = await client.get(f"/session/{SESSION_ID}/current-phase")
        assert response.status_code == 200
        assert isinstance(response.json().get("phase"), str)


# ---------------------------------------------------------------------------
# TestSessionFormat
# ---------------------------------------------------------------------------


class TestSessionFormat:
    """POST /session/format  (stateless)"""

    @pytest.mark.asyncio
    async def test_format_returns_200(self, client: AsyncClient) -> None:
        """POST /session/format with valid query response JSON returns HTTP 200."""
        payload = {"query_response": {"rules": [], "mode": "standard"}}
        response = await client.post("/session/format", json=payload)
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_format_rejects_empty_body(self, client: AsyncClient) -> None:
        """POST /session/format with no body returns HTTP 422."""
        response = await client.post("/session/format")
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# TestSessionCoverage
# ---------------------------------------------------------------------------


class TestSessionCoverage:
    """GET /session/{session_id}/coverage"""

    @pytest.mark.asyncio
    async def test_coverage_returns_float(self, client: AsyncClient) -> None:
        """GET /session/{session_id}/coverage returns a numeric coverage value."""
        response = await client.get(f"/session/{SESSION_ID}/coverage")
        assert response.status_code == 200
        value = response.json().get("coverage")
        assert isinstance(value, (int, float))


# ---------------------------------------------------------------------------
# TestSessionCheckEscalation
# ---------------------------------------------------------------------------


class TestSessionCheckEscalation:
    """GET /session/{session_id}/check-escalation"""

    @pytest.mark.asyncio
    async def test_check_escalation_returns_bool(self, client: AsyncClient) -> None:
        """GET /session/{session_id}/check-escalation returns a boolean."""
        response = await client.get(f"/session/{SESSION_ID}/check-escalation")
        assert response.status_code == 200
        assert isinstance(response.json().get("escalation"), bool)


# ---------------------------------------------------------------------------
# TestSessionAutoFeedback
# ---------------------------------------------------------------------------


class TestSessionAutoFeedback:
    """POST /session/{session_id}/auto-feedback"""

    @pytest.mark.asyncio
    async def test_auto_feedback_returns_200(self, client: AsyncClient) -> None:
        """POST /session/{session_id}/auto-feedback with valid body returns HTTP 200."""
        payload = {"feedback": "rule matched correctly"}
        response = await client.post(f"/session/{SESSION_ID}/auto-feedback", json=payload)
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_auto_feedback_runs_correlation(
        self, client: AsyncClient, mock_writ_session
    ) -> None:
        """The route must run the REAL correlation (cmd_auto_feedback), not a no-op.

        Audit #2: /auto-feedback returned {"ok": True} without correlating loaded
        rules to outcomes, so the daemon-up session-end feedback loop was dead.
        """
        response = await client.post(
            f"/session/{SESSION_ID}/auto-feedback", json={"feedback": ""}
        )
        assert response.status_code == 200
        mock_writ_session.cmd_auto_feedback.assert_called_once_with(SESSION_ID)


# ---------------------------------------------------------------------------
# TestSessionClearPendingViolations
# ---------------------------------------------------------------------------


class TestSessionClearPendingViolations:
    """POST /session/{session_id}/clear-pending-violations"""

    @pytest.mark.asyncio
    async def test_clear_pending_violations_returns_200(self, client: AsyncClient) -> None:
        """POST /session/{session_id}/clear-pending-violations returns HTTP 200."""
        response = await client.post(f"/session/{SESSION_ID}/clear-pending-violations", json={})
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# TestSessionAddPendingViolation
# ---------------------------------------------------------------------------


class TestSessionAddPendingViolation:
    """POST /session/{session_id}/add-pending-violation"""

    @pytest.mark.asyncio
    async def test_add_pending_violation_returns_200(self, client: AsyncClient) -> None:
        """POST /session/{session_id}/add-pending-violation with valid body returns HTTP 200."""
        payload = {"rule_id": "ARCH-ORG-001", "detail": "mixed layers"}
        response = await client.post(f"/session/{SESSION_ID}/add-pending-violation", json=payload)
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_add_pending_violation_rejects_missing_rule_id(self, client: AsyncClient) -> None:
        """POST with no rule_id returns HTTP 422."""
        response = await client.post(
            f"/session/{SESSION_ID}/add-pending-violation", json={"detail": "missing rule_id"}
        )
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# TestSessionInvalidateGate
# ---------------------------------------------------------------------------


class TestSessionInvalidateGate:
    """POST /session/{session_id}/invalidate-gate

    2.4: the route must call the real cmd_invalidate_gate (record the
    invalidation cycle, delete the .approved file, check escalation), not the
    old `setdefault("invalidation_history", {})` no-op.
    """

    @pytest.mark.asyncio
    async def test_invalidate_gate_calls_real_command(self, client: AsyncClient, mock_writ_session) -> None:
        body = {
            "gate": "phase-a",
            "rule_id": "ENF-GATE-PLAN",
            "file": "src/x.py",
            "evidence": "plan/code mismatch",
            "project_root": "/tmp/proj",
        }
        response = await client.post(f"/session/{SESSION_ID}/invalidate-gate", json=body)
        assert response.status_code == 200
        assert response.json().get("ok") is True
        mock_writ_session.cmd_invalidate_gate.assert_called_once()
        called_sid, called_args = mock_writ_session.cmd_invalidate_gate.call_args.args
        assert called_sid == SESSION_ID
        assert called_args[0] == "phase-a"
        assert "--rule" in called_args and "ENF-GATE-PLAN" in called_args
        assert "--file" in called_args and "src/x.py" in called_args
        assert "--project-root" in called_args and "/tmp/proj" in called_args

    @pytest.mark.asyncio
    async def test_invalidate_gate_missing_fields_does_not_call(self, client: AsyncClient, mock_writ_session) -> None:
        """Empty/under-specified body returns ok=False and never invalidates."""
        response = await client.post(f"/session/{SESSION_ID}/invalidate-gate", json={})
        assert response.status_code == 200
        assert response.json().get("ok") is False
        mock_writ_session.cmd_invalidate_gate.assert_not_called()


# ---------------------------------------------------------------------------
# TestSessionPendingViolations
# ---------------------------------------------------------------------------


class TestSessionPendingViolations:
    """GET /session/{session_id}/pending-violations"""

    @pytest.mark.asyncio
    async def test_pending_violations_returns_list(self, client: AsyncClient) -> None:
        """GET /session/{session_id}/pending-violations returns a list."""
        response = await client.get(f"/session/{SESSION_ID}/pending-violations")
        assert response.status_code == 200
        assert isinstance(response.json().get("violations"), list)


# ---------------------------------------------------------------------------
# TestAdvancePhaseTypedBody / TestVerificationEvidenceTypedBody
#
# Wave1 Cycle6 Target 3: 5 session routes still take `body: dict` /
# `body: dict | None`, so a present-but-wrong-typed field is silently
# accepted (200) instead of failing Pydantic validation (422). API-BREAKING-001
# requires that an OMITTED key still returns today's 200 business response
# (the new models keep the same `.get(key, default)` defaults) -- those are
# stability guards, not RED, and must pass both before and after the fix.
# ---------------------------------------------------------------------------


class TestAdvancePhaseTypedBody:
    """POST /session/{session_id}/advance-phase body validation (PY-PYDANTIC-001)."""

    @pytest.mark.asyncio
    async def test_wrong_typed_confirmation_source_returns_422(
        self, client: AsyncClient
    ) -> None:
        """A present-but-wrong-typed confirmation_source (a list, not a str)
        must be rejected with 422 once the field is Pydantic-typed.

        RED today: `body: dict | None = None` accepts any JSON shape, so this
        currently returns 200 with `{"error": "Invalid confirmation_source: ..."}`
        (confirmed live on HEAD), not a 422.
        """
        response = await client.post(
            f"/session/{SESSION_ID}/advance-phase",
            json={"confirmation_source": [1, 2, 3]},
        )
        assert response.status_code == 422, (
            f"expected 422, got {response.status_code}: {response.text}"
        )

    @pytest.mark.asyncio
    async def test_wrong_typed_token_returns_422(self, client: AsyncClient) -> None:
        """A present-but-wrong-typed token (an int, not a str) must be
        rejected with 422.

        RED today: returns 200 with a token-mismatch business error (the
        untyped dict happily accepts an int).
        """
        response = await client.post(
            f"/session/{SESSION_ID}/advance-phase", json={"token": 12345}
        )
        assert response.status_code == 422, (
            f"expected 422, got {response.status_code}: {response.text}"
        )

    @pytest.mark.asyncio
    async def test_omitted_keys_unchanged_200(self, client: AsyncClient) -> None:
        """API-BREAKING-001: a body that omits every key must keep TODAY'S
        200 business response (invalid/missing token), never a 422.

        Stability guard -- passes both before and after Target 3 lands.
        """
        response = await client.post(f"/session/{SESSION_ID}/advance-phase", json={})
        assert response.status_code == 200
        body = response.json()
        assert body.get("advanced") is False
        assert "token" in str(body.get("error", "")).lower()


class TestVerificationEvidenceTypedBody:
    """POST /session/{session_id}/verification-evidence body validation."""

    @pytest.mark.asyncio
    async def test_wrong_typed_exit_code_returns_422(self, client: AsyncClient) -> None:
        """A present-but-wrong-typed exit_code (a dict, not an int) must be
        rejected with 422.

        RED today: `body: dict` stores the dict verbatim with no type check,
        returning 200 `{"ok": true, "todo_id": "t1"}` (confirmed live on HEAD).
        """
        response = await client.post(
            f"/session/{SESSION_ID}/verification-evidence",
            json={"todo_id": "t1", "exit_code": {"a": 1}},
        )
        assert response.status_code == 422, (
            f"expected 422, got {response.status_code}: {response.text}"
        )

    @pytest.mark.asyncio
    async def test_bool_exit_code_returns_422(self, client: AsyncClient) -> None:
        """A JSON bool exit_code must be rejected with 422, not silently coerced.

        Pydantic v2 lax mode coerces JSON true->1 because bool subclasses int;
        the field_validator(mode="before") blocks the bool so a wrong-typed
        value fails validation instead of being accepted as 1.
        """
        response = await client.post(
            f"/session/{SESSION_ID}/verification-evidence",
            json={"todo_id": "t1", "exit_code": True},
        )
        assert response.status_code == 422, (
            f"expected 422, got {response.status_code}: {response.text}"
        )

    @pytest.mark.asyncio
    async def test_omitted_keys_unchanged_200(self, client: AsyncClient) -> None:
        """An empty body must keep today's 200
        {"ok": False, "error": "todo_id required"} response.

        Stability guard -- passes both before and after Target 3 lands.
        """
        response = await client.post(
            f"/session/{SESSION_ID}/verification-evidence", json={}
        )
        assert response.status_code == 200
        assert response.json() == {"ok": False, "error": "todo_id required"}


# ---------------------------------------------------------------------------
# TestPydanticValidation
# ---------------------------------------------------------------------------


class TestPydanticValidation:
    """Malformed request bodies are rejected with HTTP 422 before handler runs."""

    @pytest.mark.asyncio
    async def test_update_wrong_type_rejected(self, client: AsyncClient) -> None:
        """PUT body with wrong field type returns 422, not 500."""
        response = await client.post(
            f"/session/{SESSION_ID}/update", json={"key": 42, "value": None}
        )
        # key must be a string; 42 should fail Pydantic validation.
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_mode_set_non_string_rejected(self, client: AsyncClient) -> None:
        """mode field that is not a string returns 422."""
        response = await client.post(f"/session/{SESSION_ID}/mode", json={"mode": 99})
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# TestFallbackSemantics
# ---------------------------------------------------------------------------


class TestFallbackSemantics:
    """Routes exist and respond even when the session file is empty/missing."""

    @pytest.mark.asyncio
    async def test_read_empty_session_returns_defaults(self, mock_writ_session, client: AsyncClient) -> None:
        """GET /session/{session_id} with empty session file returns defaults, not 500."""
        mock_writ_session.cmd_read.return_value = {}
        response = await client.get(f"/session/{SESSION_ID}")
        # Must not crash; 200 or a handled 404 are both acceptable.
        assert response.status_code in (200, 404)


# ---------------------------------------------------------------------------
# TestAsyncBoundary
# ---------------------------------------------------------------------------


class TestAsyncBoundary:
    """Route handlers must not block the event loop (PY-ASYNC-001, PERF-IO-001)."""

    def test_route_handlers_are_async_functions(self) -> None:
        """All /session/* route handler functions are declared with async def."""
        from writ.server import app as fastapi_app

        session_routes = [
            route for route in fastapi_app.routes
            if hasattr(route, "path") and "/session" in getattr(route, "path", "")
        ]
        if not session_routes:
            pytest.fail("skeleton -- no /session/* routes registered yet")

        for route in session_routes:
            endpoint = getattr(route, "endpoint", None)
            if endpoint is not None:
                assert inspect.iscoroutinefunction(endpoint), (
                    f"Route {route.path} endpoint is not async"  # type: ignore[attr-defined]
                )

    def test_asyncio_to_thread_used_in_file_io(self) -> None:
        """asyncio.to_thread is present in writ.server (no blocking file I/O).

        W2 (server package split, branch refactor/w2-server-split): reads via
        writ_server_source() (tests/conftest.py), which is layout-agnostic --
        post-split the asyncio.to_thread calls live in writ/server/routes/*.py
        (not the __init__.py facade), so scanning the whole package is required;
        pre-split it falls back to the single writ/server.py file.
        """
        source = writ_server_source()

        assert "asyncio.to_thread" in source, (
            "writ.server must use asyncio.to_thread() for session file I/O (PERF-IO-001)"
        )
