"""Tests for orchestrator mode suppression (Cycle B, Item 5).

Per TEST-TDD-001: skeletons approved before implementation.
Covers: is_orchestrator field in _read_cache defaults, --orchestrator flag on
_mode_set, HTTP route passthrough, sub-agent cache isolation, and the session
flag reads that guard writ-rag-inject.sh / writ-pretool-rag.sh /
writ-posttool-rag.sh.
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
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
    from writ.server import SessionModeSetRequest  # type: ignore[import]
except ImportError:
    SessionModeSetRequest = None  # type: ignore[assignment,misc]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SESSION_ID = "test-orchestrator-session"
SKILL_DIR = "/home/lucio.saldivar/.claude/skills/writ"
WRIT_SESSION_PY = f"{SKILL_DIR}/bin/lib/writ-session.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_writ_session():
    """Load writ-session.py as a module without installing it."""
    spec = importlib.util.spec_from_file_location("writ_session_orch", WRIT_SESSION_PY)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_base_cache(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "session_id": SESSION_ID,
        "mode": "work",
        "current_phase": "planning",
        "remaining_budget": 8000,
        "context_percent": 30,
        "loaded_rule_ids": [],
        "loaded_rules": [],
        "loaded_rule_ids_by_phase": {},
        "queries": 0,
        "pending_violations": [],
        "escalation": {"needed": False},
        "invalidation_history": {},
        "failed_writes": [],
    }
    base.update(overrides)
    return base


@pytest.fixture()
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture()
def mock_writ_session_orch():
    """Mock writ_session for orchestrator HTTP route tests."""
    session_data = _make_base_cache()
    mock = MagicMock()
    mock._read_cache = MagicMock(return_value=dict(session_data))
    mock._write_cache = MagicMock(return_value=None)
    mock.DEFAULT_SESSION_BUDGET = 8000
    return mock


@pytest_asyncio.fixture()
async def client(mock_writ_session_orch):
    transport = ASGITransport(app=app)
    with patch("writ.server.writ_session", mock_writ_session_orch):
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


# ---------------------------------------------------------------------------
# TestReadCacheDefaults -- _read_cache schema
# ---------------------------------------------------------------------------


class TestReadCacheDefaults:
    """_read_cache must include is_orchestrator: False in its default schema."""

    def setup_method(self):
        self.mod = _load_writ_session()
        self._orig_cache_dir = self.mod.CACHE_DIR
        self._tmpdir = tempfile.mkdtemp()
        self.mod.CACHE_DIR = self._tmpdir

    def teardown_method(self):
        self.mod.CACHE_DIR = self._orig_cache_dir
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_read_cache_default_includes_is_orchestrator_false(self) -> None:
        """_read_cache returns is_orchestrator: False when no cache file exists."""
        result = self.mod._read_cache(SESSION_ID)
        assert "is_orchestrator" in result, (
            "_read_cache default schema must include is_orchestrator field"
        )
        assert result["is_orchestrator"] is False, (
            "is_orchestrator must default to False"
        )

    def test_read_cache_from_file_without_field_returns_false(self) -> None:
        """Cache file that predates the field gets is_orchestrator: False via setdefault."""
        old_cache = _make_base_cache()
        old_cache.pop("is_orchestrator", None)
        path = self.mod._cache_path(SESSION_ID)
        with open(path, "w") as f:
            json.dump(old_cache, f)
        result = self.mod._read_cache(SESSION_ID)
        assert result.get("is_orchestrator") is False, (
            "Reading a cache file without is_orchestrator must setdefault to False"
        )

    def test_read_cache_from_file_with_true_returns_true(self) -> None:
        """Cache file with is_orchestrator: true is read back correctly."""
        cache = _make_base_cache(is_orchestrator=True)
        path = self.mod._cache_path(SESSION_ID)
        with open(path, "w") as f:
            json.dump(cache, f)
        result = self.mod._read_cache(SESSION_ID)
        assert result.get("is_orchestrator") is True


# ---------------------------------------------------------------------------
# TestModeSetOrchestratorFlag -- _mode_set + cmd_mode with --orchestrator
# ---------------------------------------------------------------------------


class TestModeSetOrchestratorFlag:
    """--orchestrator flag on mode set writes is_orchestrator: True to cache."""

    def setup_method(self):
        self.mod = _load_writ_session()
        self._orig_cache_dir = self.mod.CACHE_DIR
        self._tmpdir = tempfile.mkdtemp()
        self.mod.CACHE_DIR = self._tmpdir

    def teardown_method(self):
        self.mod.CACHE_DIR = self._orig_cache_dir
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_mode_set_with_orchestrator_flag_sets_true(self) -> None:
        """_mode_set called with is_orchestrator=True writes is_orchestrator: True."""
        self.mod._mode_set(SESSION_ID, "work", is_orchestrator=True)
        result = self.mod._read_cache(SESSION_ID)
        assert result["is_orchestrator"] is True

    def test_mode_set_without_orchestrator_flag_leaves_false(self) -> None:
        """_mode_set called without is_orchestrator flag leaves is_orchestrator: False."""
        self.mod._mode_set(SESSION_ID, "work")
        result = self.mod._read_cache(SESSION_ID)
        assert result["is_orchestrator"] is False

    def test_mode_set_orchestrator_persists_after_write(self) -> None:
        """is_orchestrator: True round-trips through _write_cache and _read_cache."""
        self.mod._mode_set(SESSION_ID, "work", is_orchestrator=True)
        result = self.mod._read_cache(SESSION_ID)
        assert result["is_orchestrator"] is True
        # Write again and re-read
        self.mod._write_cache(SESSION_ID, result)
        result2 = self.mod._read_cache(SESSION_ID)
        assert result2["is_orchestrator"] is True

    def test_mode_switch_does_not_clear_orchestrator_flag(self) -> None:
        """Switching mode preserves is_orchestrator; it is not reset on mode-switch."""
        self.mod._mode_set(SESSION_ID, "work", is_orchestrator=True)
        self.mod._mode_switch(SESSION_ID, "conversation")
        result = self.mod._read_cache(SESSION_ID)
        assert result["is_orchestrator"] is True


# ---------------------------------------------------------------------------
# TestSubAgentCacheIsolation -- writ-subagent-start.sh creates fresh caches
# ---------------------------------------------------------------------------


class TestSubAgentCacheIsolation:
    """Sub-agent caches must NOT inherit is_orchestrator from the parent session."""

    def test_subagent_start_creates_cache_without_is_orchestrator_true(self) -> None:
        """writ-subagent-start.sh creates a fresh cache where is_orchestrator is absent or False."""
        subagent_start = f"{SKILL_DIR}/.claude/hooks/writ-subagent-start.sh"
        with open(subagent_start) as f:
            source = f.read()
        # The sub-agent cache creation block must not copy is_orchestrator from parent.
        assert "is_orchestrator" not in source or "false" in source.lower(), (
            "writ-subagent-start.sh must not propagate is_orchestrator=true to sub-agents"
        )

    def test_subagent_start_cache_init_block_exists(self) -> None:
        """writ-subagent-start.sh contains a cache initialization block."""
        subagent_start = f"{SKILL_DIR}/.claude/hooks/writ-subagent-start.sh"
        with open(subagent_start) as f:
            source = f.read()
        assert "writ-session" in source, (
            "writ-subagent-start.sh must initialize a session cache for sub-agents"
        )

    def test_fresh_cache_is_orchestrator_defaults_false_in_new_session(self) -> None:
        """A new session (fresh cache) always starts with is_orchestrator: False."""
        mod = _load_writ_session()
        with tempfile.TemporaryDirectory() as tmpdir:
            mod.CACHE_DIR = tmpdir
            result = mod._read_cache("brand-new-session-id")
            assert result.get("is_orchestrator") is False


# ---------------------------------------------------------------------------
# TestOrchestratorSessionFlagRead -- session flag read for hook suppression
# ---------------------------------------------------------------------------


class TestOrchestratorSessionFlagRead:
    """The is_orchestrator flag in the cache is read correctly for hook decision logic."""

    def setup_method(self):
        self.mod = _load_writ_session()
        self._orig_cache_dir = self.mod.CACHE_DIR
        self._tmpdir = tempfile.mkdtemp()
        self.mod.CACHE_DIR = self._tmpdir

    def teardown_method(self):
        self.mod.CACHE_DIR = self._orig_cache_dir
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_is_orchestrator_true_read_from_cache_returns_true(self) -> None:
        """When cache has is_orchestrator: True, _read_cache['is_orchestrator'] is True."""
        cache = _make_base_cache(is_orchestrator=True)
        path = self.mod._cache_path(SESSION_ID)
        with open(path, "w") as f:
            json.dump(cache, f)
        result = self.mod._read_cache(SESSION_ID)
        assert result["is_orchestrator"] is True

    def test_is_orchestrator_false_read_from_cache_returns_false(self) -> None:
        """When cache has is_orchestrator: False, _read_cache['is_orchestrator'] is False."""
        cache = _make_base_cache(is_orchestrator=False)
        path = self.mod._cache_path(SESSION_ID)
        with open(path, "w") as f:
            json.dump(cache, f)
        result = self.mod._read_cache(SESSION_ID)
        assert result["is_orchestrator"] is False

    def test_writ_pretool_rag_references_is_orchestrator(self) -> None:
        """writ-pretool-rag.sh contains an is_orchestrator check for early exit."""
        hook = f"{SKILL_DIR}/.claude/hooks/writ-pretool-rag.sh"
        with open(hook) as f:
            source = f.read()
        assert "is_orchestrator" in source, (
            "writ-pretool-rag.sh must read is_orchestrator to decide early exit"
        )

    def test_writ_posttool_rag_references_is_orchestrator(self) -> None:
        """writ-posttool-rag.sh contains an is_orchestrator check for early exit."""
        hook = f"{SKILL_DIR}/.claude/hooks/writ-posttool-rag.sh"
        with open(hook) as f:
            source = f.read()
        assert "is_orchestrator" in source, (
            "writ-posttool-rag.sh must read is_orchestrator to decide early exit"
        )

    def test_writ_rag_inject_references_is_orchestrator(self) -> None:
        """writ-rag-inject.sh contains an is_orchestrator check to skip /query."""
        hook = f"{SKILL_DIR}/.claude/hooks/writ-rag-inject.sh"
        with open(hook) as f:
            source = f.read()
        assert "is_orchestrator" in source, (
            "writ-rag-inject.sh must check is_orchestrator before calling /query"
        )


# ---------------------------------------------------------------------------
# TestOrchestratorModeRoute -- HTTP route with orchestrator field
# ---------------------------------------------------------------------------


class TestOrchestratorModeRoute:
    """POST /session/{id}/mode with orchestrator: true in body sets the flag."""

    @pytest.mark.asyncio
    async def test_mode_set_with_orchestrator_true_returns_200(
        self, client: AsyncClient
    ) -> None:
        """POST /session/{id}/mode with orchestrator: true in body returns HTTP 200."""
        resp = await client.post(
            f"/session/{SESSION_ID}/mode",
            json={"mode": "work", "orchestrator": True},
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_mode_set_with_orchestrator_true_passes_flag_to_mode_set(
        self, client: AsyncClient, mock_writ_session_orch
    ) -> None:
        """Route handler passes orchestrator=True through to the cache write logic."""
        resp = await client.post(
            f"/session/{SESSION_ID}/mode",
            json={"mode": "work", "orchestrator": True},
        )
        assert resp.status_code == 200
        # Verify _write_cache was called with is_orchestrator=True
        call_args = mock_writ_session_orch._write_cache.call_args
        assert call_args is not None
        written_data = call_args[0][1]
        assert written_data.get("is_orchestrator") is True

    @pytest.mark.asyncio
    async def test_mode_set_without_orchestrator_field_defaults_false(
        self, client: AsyncClient, mock_writ_session_orch
    ) -> None:
        """POST /session/{id}/mode without orchestrator field uses False as default."""
        resp = await client.post(
            f"/session/{SESSION_ID}/mode",
            json={"mode": "work"},
        )
        assert resp.status_code == 200
        # _write_cache should be called but is_orchestrator should not be set to True
        call_args = mock_writ_session_orch._write_cache.call_args
        assert call_args is not None
        written_data = call_args[0][1]
        assert written_data.get("is_orchestrator", False) is False

    @pytest.mark.asyncio
    async def test_session_mode_set_request_accepts_orchestrator_field(self) -> None:
        """SessionModeSetRequest Pydantic model has an orchestrator: bool field."""
        assert SessionModeSetRequest is not None
        model = SessionModeSetRequest(mode="work", orchestrator=True)
        assert model.orchestrator is True
        model_default = SessionModeSetRequest(mode="work")
        assert model_default.orchestrator is False
