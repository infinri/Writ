"""Tests for the detect-compaction subcommand and its HTTP route.

Per TEST-TDD-001: skeletons approved before implementation.
Covers: cmd_detect_compaction(), POST /session/{session_id}/detect-compaction,
and the _writ_session detect-compaction shell wrapper.
"""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio

try:
    from httpx import AsyncClient, ASGITransport
except ImportError:
    pytestmark = pytest.mark.skip(reason="httpx not installed")

from writ.server import app  # type: ignore[import]

try:
    from writ.server import DetectCompactionRequest  # type: ignore[import]
except ImportError:
    DetectCompactionRequest = None  # type: ignore[assignment,misc]

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

SESSION_ID = "test-compaction-session"
SKILL_DIR = "/home/lucio.saldivar/.claude/skills/writ"
WRIT_SESSION_PY = f"{SKILL_DIR}/bin/lib/writ-session.py"


def _load_writ_session():
    """Load writ-session.py as a module."""
    spec = importlib.util.spec_from_file_location("writ_session_test", WRIT_SESSION_PY)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_cache(
    context_percent: int = 50,
    current_phase: str = "implementation",
    remaining_budget: int = 3000,
    loaded_rule_ids_by_phase: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    return {
        "session_id": SESSION_ID,
        "mode": "Work",
        "current_phase": current_phase,
        "remaining_budget": remaining_budget,
        "context_percent": context_percent,
        "loaded_rule_ids": ["ARCH-ORG-001", "PY-IMPORT-001"],
        "loaded_rule_ids_by_phase": loaded_rule_ids_by_phase
        if loaded_rule_ids_by_phase is not None
        else {current_phase: ["ARCH-ORG-001", "PY-IMPORT-001"], "planning": ["ENF-GATE-001"]},
        "queries": 5,
        "pending_violations": [],
        "escalation": {"needed": False},
        "invalidation_history": {},
        "failed_writes": [],
    }


def _run_detect(mod, session_id, cache_data, current_pct):
    """Write a cache, run cmd_detect_compaction, return parsed output."""
    cache_path = mod._cache_path(session_id)
    tmp_path = cache_path + ".tmp"
    with open(cache_path, "w") as f:
        json.dump(cache_data, f)
    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.cmd_detect_compaction(session_id, current_pct)
    return json.loads(buf.getvalue().strip())


@pytest.fixture()
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture()
def mock_writ_session_compaction():
    """Mock the writ_session module for compaction route tests.

    The route handler captures stdout from cmd_detect_compaction, so the mock
    must actually write JSON to stdout when called.
    """
    mock = MagicMock()
    mock._read_cache = MagicMock(return_value=_make_cache())
    mock._write_cache = MagicMock(return_value=None)
    mock.DEFAULT_SESSION_BUDGET = 8000
    mock._log_friction_event = MagicMock(return_value=None)

    def _fake_detect(session_id: str, context_percent: int) -> dict:
        import sys as _sys
        cache = mock._read_cache(session_id)
        previous_pct = cache.get("context_percent", 0)
        drop = previous_pct - context_percent
        if drop > 20:
            phase = cache.get("current_phase", "unknown")
            by_phase = cache.get("loaded_rule_ids_by_phase", {})
            rules_cleared = list(by_phase.get(phase, []))
            result = {"compacted": True, "context_drop_percent": drop, "rules_cleared": rules_cleared}
        else:
            result = {"compacted": False, "context_drop_percent": drop}
        _sys.stdout.write(json.dumps(result) + "\n")
        return result

    mock.cmd_detect_compaction = MagicMock(side_effect=_fake_detect)
    return mock


@pytest_asyncio.fixture()
async def client(mock_writ_session_compaction):
    transport = ASGITransport(app=app)
    with patch("writ.server.writ_session", mock_writ_session_compaction):
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


# ---------------------------------------------------------------------------
# TestCmdDetectCompaction -- Python subcommand logic
# ---------------------------------------------------------------------------


class TestCmdDetectCompaction:
    """Unit tests for cmd_detect_compaction() in writ-session.py."""

    def setup_method(self):
        self.mod = _load_writ_session()
        self._orig_cache_dir = self.mod.CACHE_DIR
        self._tmpdir = tempfile.mkdtemp()
        self.mod.CACHE_DIR = self._tmpdir

    def teardown_method(self):
        self.mod.CACHE_DIR = self._orig_cache_dir
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_returns_compacted_true_when_drop_exceeds_20_percent(self) -> None:
        """cmd_detect_compaction returns {"compacted": true, ...} when context drops >20%."""
        cache = _make_cache(context_percent=60)
        result = _run_detect(self.mod, SESSION_ID, cache, 30)
        assert result["compacted"] is True

    def test_returns_compacted_false_when_drop_within_20_percent(self) -> None:
        """cmd_detect_compaction returns {"compacted": false} when context drops <=20%."""
        cache = _make_cache(context_percent=50)
        result = _run_detect(self.mod, SESSION_ID, cache, 35)
        assert result["compacted"] is False

    def test_compaction_clears_loaded_rule_ids_for_current_phase_only(self) -> None:
        """On compaction, loaded_rule_ids_by_phase[current_phase] is cleared; other phases are untouched."""
        cache = _make_cache(context_percent=80, loaded_rule_ids_by_phase={
            "implementation": ["ARCH-ORG-001", "PY-IMPORT-001"],
            "planning": ["ENF-GATE-001"],
        })
        _run_detect(self.mod, SESSION_ID, cache, 30)
        updated = self.mod._read_cache(SESSION_ID)
        assert updated["loaded_rule_ids_by_phase"]["implementation"] == []

    def test_compaction_does_not_clear_other_phase_rule_ids(self) -> None:
        """Phase entries in loaded_rule_ids_by_phase other than current_phase are preserved."""
        cache = _make_cache(context_percent=80, loaded_rule_ids_by_phase={
            "implementation": ["ARCH-ORG-001"],
            "planning": ["ENF-GATE-001"],
        })
        _run_detect(self.mod, SESSION_ID, cache, 30)
        updated = self.mod._read_cache(SESSION_ID)
        assert updated["loaded_rule_ids_by_phase"]["planning"] == ["ENF-GATE-001"]

    def test_compaction_resets_remaining_budget_to_default(self) -> None:
        """On compaction, remaining_budget is reset to DEFAULT_SESSION_BUDGET (8000)."""
        cache = _make_cache(context_percent=80, remaining_budget=1000)
        _run_detect(self.mod, SESSION_ID, cache, 30)
        updated = self.mod._read_cache(SESSION_ID)
        assert updated["remaining_budget"] == 8000

    def test_compaction_logs_friction_event_with_correct_event_key(self) -> None:
        """On compaction, _log_friction_event is called with event='compaction_detected'."""
        cache = _make_cache(context_percent=80)
        with patch.object(self.mod, "_log_friction_event") as mock_log:
            _run_detect(self.mod, SESSION_ID, cache, 30)
            mock_log.assert_called_once()
            call_args = mock_log.call_args
            assert call_args[0][2] == "compaction_detected"

    def test_no_previous_context_percent_does_not_trigger_compaction(self) -> None:
        """First turn with no context_percent in cache (defaults to 0) does NOT trigger compaction."""
        cache = _make_cache(context_percent=0)
        result = _run_detect(self.mod, SESSION_ID, cache, 25)
        assert result["compacted"] is False

    def test_context_percent_increase_does_not_trigger_compaction(self) -> None:
        """When context_percent goes up (normal growth), compaction is NOT triggered."""
        cache = _make_cache(context_percent=30)
        result = _run_detect(self.mod, SESSION_ID, cache, 50)
        assert result["compacted"] is False

    def test_boundary_exactly_20_percent_drop_does_not_trigger(self) -> None:
        """A drop of exactly 20% does NOT trigger compaction (threshold is strictly >20%)."""
        cache = _make_cache(context_percent=50)
        result = _run_detect(self.mod, SESSION_ID, cache, 30)
        assert result["compacted"] is False

    def test_boundary_21_percent_drop_triggers_compaction(self) -> None:
        """A drop of 21% triggers compaction."""
        cache = _make_cache(context_percent=50)
        result = _run_detect(self.mod, SESSION_ID, cache, 29)
        assert result["compacted"] is True

    def test_return_value_includes_context_drop_percent_field(self) -> None:
        """Return dict includes context_drop_percent with the computed delta value."""
        cache = _make_cache(context_percent=60)
        result = _run_detect(self.mod, SESSION_ID, cache, 30)
        assert "context_drop_percent" in result
        assert result["context_drop_percent"] == 30

    def test_return_value_includes_rules_cleared_field_on_compaction(self) -> None:
        """On compaction, return dict includes rules_cleared with the list that was cleared."""
        cache = _make_cache(context_percent=80, loaded_rule_ids_by_phase={
            "implementation": ["ARCH-ORG-001", "PY-IMPORT-001"],
            "planning": ["ENF-GATE-001"],
        })
        result = _run_detect(self.mod, SESSION_ID, cache, 30)
        assert "rules_cleared" in result
        assert sorted(result["rules_cleared"]) == ["ARCH-ORG-001", "PY-IMPORT-001"]


# ---------------------------------------------------------------------------
# TestDetectCompactionRoute -- HTTP route
# ---------------------------------------------------------------------------


class TestDetectCompactionRoute:
    """POST /session/{session_id}/detect-compaction route."""

    @pytest.mark.asyncio
    async def test_route_returns_200(self, client: AsyncClient) -> None:
        """POST /session/{session_id}/detect-compaction with valid body returns HTTP 200."""
        resp = await client.post(
            f"/session/{SESSION_ID}/detect-compaction",
            json={"context_percent": 25},
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_route_returns_compacted_bool_field(self, client: AsyncClient) -> None:
        """Response body contains a boolean 'compacted' field."""
        resp = await client.post(
            f"/session/{SESSION_ID}/detect-compaction",
            json={"context_percent": 25},
        )
        body = resp.json()
        assert "compacted" in body
        assert isinstance(body["compacted"], bool)

    @pytest.mark.asyncio
    async def test_route_response_shape_matches_subcommand(self, client: AsyncClient) -> None:
        """Route response contains the same fields as cmd_detect_compaction output."""
        resp = await client.post(
            f"/session/{SESSION_ID}/detect-compaction",
            json={"context_percent": 25},
        )
        body = resp.json()
        assert "compacted" in body
        assert "context_drop_percent" in body

    @pytest.mark.asyncio
    async def test_route_rejects_missing_context_percent(self, client: AsyncClient) -> None:
        """POST with no context_percent field returns HTTP 422."""
        resp = await client.post(
            f"/session/{SESSION_ID}/detect-compaction",
            json={},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_route_handler_is_async(self) -> None:
        """The detect-compaction route handler is declared with async def (PY-ASYNC-001)."""
        import inspect
        from writ.server import app as fastapi_app

        detect_routes = [
            r for r in fastapi_app.routes
            if hasattr(r, "path") and "detect-compaction" in getattr(r, "path", "")
        ]
        assert len(detect_routes) > 0, "detect-compaction route not registered"
        for route in detect_routes:
            endpoint = getattr(route, "endpoint", None)
            if endpoint is not None:
                assert inspect.iscoroutinefunction(endpoint), (
                    f"Route {route.path} endpoint must be async"  # type: ignore[attr-defined]
                )


# ---------------------------------------------------------------------------
# TestDetectCompactionShell -- shell wrapper in common.sh
# ---------------------------------------------------------------------------


class TestDetectCompactionShell:
    """_writ_session detect-compaction shell integration (via subprocess)."""

    def test_shell_wrapper_attempts_http_first(self) -> None:
        """_writ_session detect-compaction posts to /session/{id}/detect-compaction before subprocess fallback."""
        common_sh = f"{SKILL_DIR}/bin/lib/common.sh"
        with open(common_sh) as f:
            source = f.read()
        assert "detect-compaction" in source, (
            "common.sh must have a detect-compaction case in _writ_session()"
        )
        # Verify it uses curl to the detect-compaction endpoint
        assert "/detect-compaction" in source, (
            "common.sh detect-compaction case must POST to /session/{id}/detect-compaction"
        )

    def test_shell_wrapper_has_subprocess_fallback(self) -> None:
        """_writ_session detect-compaction has a python3 fallback when server is unreachable."""
        common_sh = f"{SKILL_DIR}/bin/lib/common.sh"
        with open(common_sh) as f:
            source = f.read()
        # Both the HTTP path and the python fallback must be present
        assert "detect-compaction" in source
        assert "python3" in source or "python" in source, (
            "common.sh must include a python3 fallback for detect-compaction"
        )
