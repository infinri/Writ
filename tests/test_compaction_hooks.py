"""Tests for PreCompact and PostCompact lifecycle hooks (Cycle B, Item 6).

Per TEST-TDD-001: skeletons approved before implementation.
Covers: cmd_clear_rules_for_compaction, cmd_reset_after_compaction,
POST /session/{id}/clear-rules-for-compaction,
POST /session/{id}/reset-after-compaction,
Cycle A heuristic coexistence, and settings.json registrations.
"""

from __future__ import annotations

import importlib.util
import io
import json
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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SESSION_ID = "test-compaction-hooks-session"
SKILL_DIR = "/home/lucio.saldivar/.claude/skills/writ"
WRIT_SESSION_PY = f"{SKILL_DIR}/bin/lib/writ-session.py"
SETTINGS_JSON = f"{SKILL_DIR}/../../settings.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_writ_session():
    """Load writ-session.py as a module without installing it."""
    spec = importlib.util.spec_from_file_location("writ_session_compact", WRIT_SESSION_PY)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_cache_with_rules(
    current_phase: str = "implementation",
    loaded_rules: list[dict[str, Any]] | None = None,
    loaded_rule_ids: list[str] | None = None,
    loaded_rule_ids_by_phase: dict[str, list[str]] | None = None,
    remaining_budget: int = 2000,
) -> dict[str, Any]:
    if loaded_rules is None:
        loaded_rules = [
            {"rule_id": "ARCH-ORG-001", "trigger": "...", "statement": "...",
             "violation": "...", "pass_example": "...", "enforcement": "...",
             "domain": "architecture", "severity": "critical"},
            {"rule_id": "PY-IMPORT-001", "trigger": "...", "statement": "...",
             "violation": "...", "pass_example": "...", "enforcement": "...",
             "domain": "python", "severity": "high"},
        ]
    if loaded_rule_ids is None:
        loaded_rule_ids = ["ARCH-ORG-001", "PY-IMPORT-001"]
    if loaded_rule_ids_by_phase is None:
        loaded_rule_ids_by_phase = {
            current_phase: ["ARCH-ORG-001", "PY-IMPORT-001"],
            "planning": ["ENF-GATE-001"],
        }
    return {
        "session_id": SESSION_ID,
        "mode": "work",
        "current_phase": current_phase,
        "remaining_budget": remaining_budget,
        "context_percent": 85,
        "loaded_rule_ids": loaded_rule_ids,
        "loaded_rules": loaded_rules,
        "loaded_rule_ids_by_phase": loaded_rule_ids_by_phase,
        "queries": 10,
        "pending_violations": [],
        "escalation": {"needed": False},
        "invalidation_history": {},
        "failed_writes": [],
    }


def _run_cmd(mod, cmd_fn, session_id: str, cache_data: dict[str, Any]) -> dict[str, Any]:
    """Write cache, call cmd_fn, return parsed JSON output."""
    path = mod._cache_path(session_id)
    with open(path, "w") as f:
        json.dump(cache_data, f)
    buf = io.StringIO()
    with redirect_stdout(buf):
        cmd_fn(session_id)
    return json.loads(buf.getvalue().strip())


@pytest.fixture()
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture()
def mock_writ_session_compact():
    """Mock writ_session for compaction route tests."""
    mock = MagicMock()
    mock._read_cache = MagicMock(return_value=_make_cache_with_rules())
    mock._write_cache = MagicMock(return_value=None)
    mock.DEFAULT_SESSION_BUDGET = 8000
    mock._log_friction_event = MagicMock(return_value=None)

    def _fake_clear(sid: str) -> None:
        import sys as _sys
        cache = mock._read_cache(sid)
        n = len(cache.get("loaded_rules", []))
        _sys.stdout.write(json.dumps({"rules_cleared": n, "bytes_freed": n * 200}) + "\n")

    def _fake_reset(sid: str) -> None:
        import sys as _sys
        cache = mock._read_cache(sid)
        phase = cache.get("current_phase", "unknown")
        by_phase = cache.get("loaded_rule_ids_by_phase", {})
        cleared = list(by_phase.get(phase, []))
        _sys.stdout.write(json.dumps({"rules_cleared": cleared, "budget_reset": True}) + "\n")

    mock.cmd_clear_rules_for_compaction = MagicMock(side_effect=_fake_clear)
    mock.cmd_reset_after_compaction = MagicMock(side_effect=_fake_reset)
    return mock


@pytest_asyncio.fixture()
async def client(mock_writ_session_compact):
    transport = ASGITransport(app=app)
    with patch("writ.server.writ_session", mock_writ_session_compact):
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


# ---------------------------------------------------------------------------
# TestCmdClearRulesForCompaction -- Python subcommand
# ---------------------------------------------------------------------------


class TestCmdClearRulesForCompaction:
    """Unit tests for cmd_clear_rules_for_compaction() in writ-session.py."""

    def setup_method(self):
        self.mod = _load_writ_session()
        self._orig_cache_dir = self.mod.CACHE_DIR
        self._tmpdir = tempfile.mkdtemp()
        self.mod.CACHE_DIR = self._tmpdir

    def teardown_method(self):
        self.mod.CACHE_DIR = self._orig_cache_dir
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_clears_loaded_rules_from_cache(self) -> None:
        """cmd_clear_rules_for_compaction sets loaded_rules to [] in the cache."""
        cache = _make_cache_with_rules()
        _run_cmd(self.mod, self.mod.cmd_clear_rules_for_compaction, SESSION_ID, cache)
        updated = self.mod._read_cache(SESSION_ID)
        assert updated["loaded_rules"] == []

    def test_preserves_loaded_rule_ids(self) -> None:
        """cmd_clear_rules_for_compaction does NOT clear loaded_rule_ids (flat ID list)."""
        cache = _make_cache_with_rules()
        original_ids = list(cache["loaded_rule_ids"])
        _run_cmd(self.mod, self.mod.cmd_clear_rules_for_compaction, SESSION_ID, cache)
        updated = self.mod._read_cache(SESSION_ID)
        assert updated["loaded_rule_ids"] == original_ids

    def test_preserves_loaded_rule_ids_by_phase(self) -> None:
        """cmd_clear_rules_for_compaction does NOT clear loaded_rule_ids_by_phase."""
        cache = _make_cache_with_rules()
        original_by_phase = dict(cache["loaded_rule_ids_by_phase"])
        _run_cmd(self.mod, self.mod.cmd_clear_rules_for_compaction, SESSION_ID, cache)
        updated = self.mod._read_cache(SESSION_ID)
        assert updated["loaded_rule_ids_by_phase"] == original_by_phase

    def test_returns_rules_cleared_count(self) -> None:
        """Return value includes rules_cleared: N where N is the count cleared."""
        cache = _make_cache_with_rules()
        result = _run_cmd(self.mod, self.mod.cmd_clear_rules_for_compaction, SESSION_ID, cache)
        assert result["rules_cleared"] == 2

    def test_returns_bytes_freed_estimate(self) -> None:
        """Return value includes bytes_freed: N as an integer estimate."""
        cache = _make_cache_with_rules()
        result = _run_cmd(self.mod, self.mod.cmd_clear_rules_for_compaction, SESSION_ID, cache)
        assert isinstance(result["bytes_freed"], int)
        assert result["bytes_freed"] > 0

    def test_empty_loaded_rules_returns_zero_counts(self) -> None:
        """When loaded_rules is already empty, rules_cleared is 0 and bytes_freed is 0."""
        cache = _make_cache_with_rules(loaded_rules=[])
        result = _run_cmd(self.mod, self.mod.cmd_clear_rules_for_compaction, SESSION_ID, cache)
        assert result["rules_cleared"] == 0
        assert result["bytes_freed"] == 0

    def test_output_is_valid_json(self) -> None:
        """cmd_clear_rules_for_compaction writes valid JSON to stdout."""
        cache = _make_cache_with_rules()
        result = _run_cmd(self.mod, self.mod.cmd_clear_rules_for_compaction, SESSION_ID, cache)
        assert isinstance(result, dict)
        assert "rules_cleared" in result
        assert "bytes_freed" in result


# ---------------------------------------------------------------------------
# TestCmdResetAfterCompaction -- Python subcommand
# ---------------------------------------------------------------------------


class TestCmdResetAfterCompaction:
    """Unit tests for cmd_reset_after_compaction() in writ-session.py."""

    def setup_method(self):
        self.mod = _load_writ_session()
        self._orig_cache_dir = self.mod.CACHE_DIR
        self._tmpdir = tempfile.mkdtemp()
        self.mod.CACHE_DIR = self._tmpdir

    def teardown_method(self):
        self.mod.CACHE_DIR = self._orig_cache_dir
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_clears_loaded_rule_ids_by_phase_for_current_phase(self) -> None:
        """cmd_reset_after_compaction clears loaded_rule_ids_by_phase[current_phase] to []."""
        cache = _make_cache_with_rules()
        _run_cmd(self.mod, self.mod.cmd_reset_after_compaction, SESSION_ID, cache)
        updated = self.mod._read_cache(SESSION_ID)
        current_phase = cache["current_phase"]
        assert updated["loaded_rule_ids_by_phase"][current_phase] == []

    def test_does_not_clear_other_phases_rule_ids(self) -> None:
        """cmd_reset_after_compaction does NOT clear loaded_rule_ids_by_phase for other phases."""
        cache = _make_cache_with_rules()
        _run_cmd(self.mod, self.mod.cmd_reset_after_compaction, SESSION_ID, cache)
        updated = self.mod._read_cache(SESSION_ID)
        assert updated["loaded_rule_ids_by_phase"]["planning"] == ["ENF-GATE-001"]

    def test_resets_remaining_budget_to_default(self) -> None:
        """cmd_reset_after_compaction resets remaining_budget to DEFAULT_SESSION_BUDGET (8000)."""
        cache = _make_cache_with_rules(remaining_budget=500)
        _run_cmd(self.mod, self.mod.cmd_reset_after_compaction, SESSION_ID, cache)
        updated = self.mod._read_cache(SESSION_ID)
        assert updated["remaining_budget"] == 8000

    def test_returns_rules_cleared_list(self) -> None:
        """Return value includes rules_cleared: [...] listing the IDs that were cleared."""
        cache = _make_cache_with_rules()
        result = _run_cmd(self.mod, self.mod.cmd_reset_after_compaction, SESSION_ID, cache)
        assert isinstance(result["rules_cleared"], list)
        assert set(result["rules_cleared"]) == {"ARCH-ORG-001", "PY-IMPORT-001"}

    def test_returns_budget_reset_true(self) -> None:
        """Return value includes budget_reset: true."""
        cache = _make_cache_with_rules()
        result = _run_cmd(self.mod, self.mod.cmd_reset_after_compaction, SESSION_ID, cache)
        assert result["budget_reset"] is True

    def test_budget_already_at_default_still_returns_budget_reset_true(self) -> None:
        """budget_reset is True even when remaining_budget was already at 8000 (idempotent)."""
        cache = _make_cache_with_rules(remaining_budget=8000)
        result = _run_cmd(self.mod, self.mod.cmd_reset_after_compaction, SESSION_ID, cache)
        assert result["budget_reset"] is True

    def test_output_is_valid_json(self) -> None:
        """cmd_reset_after_compaction writes valid JSON to stdout."""
        cache = _make_cache_with_rules()
        result = _run_cmd(self.mod, self.mod.cmd_reset_after_compaction, SESSION_ID, cache)
        assert isinstance(result, dict)
        assert "rules_cleared" in result
        assert "budget_reset" in result


# ---------------------------------------------------------------------------
# TestClearRulesForCompactionRoute -- HTTP route
# ---------------------------------------------------------------------------


class TestClearRulesForCompactionRoute:
    """POST /session/{id}/clear-rules-for-compaction route."""

    @pytest.mark.asyncio
    async def test_route_returns_200(self, client: AsyncClient) -> None:
        """POST /session/{id}/clear-rules-for-compaction returns HTTP 200."""
        resp = await client.post(f"/session/{SESSION_ID}/clear-rules-for-compaction")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_route_returns_rules_cleared_field(self, client: AsyncClient) -> None:
        """Response body contains rules_cleared integer field."""
        resp = await client.post(f"/session/{SESSION_ID}/clear-rules-for-compaction")
        body = resp.json()
        assert "rules_cleared" in body
        assert isinstance(body["rules_cleared"], int)

    @pytest.mark.asyncio
    async def test_route_returns_bytes_freed_field(self, client: AsyncClient) -> None:
        """Response body contains bytes_freed integer field."""
        resp = await client.post(f"/session/{SESSION_ID}/clear-rules-for-compaction")
        body = resp.json()
        assert "bytes_freed" in body
        assert isinstance(body["bytes_freed"], int)

    @pytest.mark.asyncio
    async def test_route_handler_is_async(self) -> None:
        """clear-rules-for-compaction route endpoint is declared with async def."""
        import inspect
        routes = [
            r for r in app.routes
            if hasattr(r, "path") and "clear-rules-for-compaction" in getattr(r, "path", "")
        ]
        assert len(routes) > 0, "clear-rules-for-compaction route not registered"
        for route in routes:
            endpoint = getattr(route, "endpoint", None)
            if endpoint is not None:
                assert inspect.iscoroutinefunction(endpoint)


# ---------------------------------------------------------------------------
# TestResetAfterCompactionRoute -- HTTP route
# ---------------------------------------------------------------------------


class TestResetAfterCompactionRoute:
    """POST /session/{id}/reset-after-compaction route."""

    @pytest.mark.asyncio
    async def test_route_returns_200(self, client: AsyncClient) -> None:
        """POST /session/{id}/reset-after-compaction returns HTTP 200."""
        resp = await client.post(f"/session/{SESSION_ID}/reset-after-compaction")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_route_returns_rules_cleared_list(self, client: AsyncClient) -> None:
        """Response body contains rules_cleared list field."""
        resp = await client.post(f"/session/{SESSION_ID}/reset-after-compaction")
        body = resp.json()
        assert "rules_cleared" in body
        assert isinstance(body["rules_cleared"], list)

    @pytest.mark.asyncio
    async def test_route_returns_budget_reset_true(self, client: AsyncClient) -> None:
        """Response body contains budget_reset: true."""
        resp = await client.post(f"/session/{SESSION_ID}/reset-after-compaction")
        body = resp.json()
        assert body.get("budget_reset") is True

    @pytest.mark.asyncio
    async def test_route_handler_is_async(self) -> None:
        """reset-after-compaction route endpoint is declared with async def."""
        import inspect
        routes = [
            r for r in app.routes
            if hasattr(r, "path") and "reset-after-compaction" in getattr(r, "path", "")
        ]
        assert len(routes) > 0, "reset-after-compaction route not registered"
        for route in routes:
            endpoint = getattr(route, "endpoint", None)
            if endpoint is not None:
                assert inspect.iscoroutinefunction(endpoint)


# ---------------------------------------------------------------------------
# TestCycleAHeuristicCoexistence -- fallback safety
# ---------------------------------------------------------------------------


class TestCycleAHeuristicCoexistence:
    """Cycle A detect-compaction heuristic stays as fallback alongside PostCompact hook."""

    def test_detect_compaction_still_exists_in_writ_session(self) -> None:
        """cmd_detect_compaction is still present in writ-session.py (Cycle A heuristic intact)."""
        mod = _load_writ_session()
        assert hasattr(mod, "cmd_detect_compaction"), (
            "cmd_detect_compaction must not be removed; Cycle A heuristic is a fallback"
        )

    def test_detect_compaction_removed_from_rag_inject_hook(self) -> None:
        """writ-rag-inject.sh no longer calls detect-compaction.

        The env-var-based heuristic was removed because the env var it read
        doesn't exist in Claude Code. PostCompact hook is the authoritative
        recovery mechanism. The subcommand/route/helper are preserved
        (see test_detect_compaction_still_exists_in_writ_session above).
        """
        hook = f"{SKILL_DIR}/.claude/hooks/writ-rag-inject.sh"
        with open(hook) as f:
            source = f.read()
        assert "detect-compaction" not in source, (
            "writ-rag-inject.sh must NOT call detect-compaction; "
            "PostCompact hook handles recovery now"
        )

    def test_reset_after_compaction_is_idempotent_with_already_empty_phase(self) -> None:
        """Running reset-after-compaction when phase IDs already empty is a no-op (safe)."""
        mod = _load_writ_session()
        with tempfile.TemporaryDirectory() as tmpdir:
            mod.CACHE_DIR = tmpdir
            cache = _make_cache_with_rules(
                loaded_rule_ids_by_phase={"implementation": [], "planning": ["ENF-GATE-001"]},
            )
            result = _run_cmd(mod, mod.cmd_reset_after_compaction, SESSION_ID, cache)
            assert result["rules_cleared"] == []
            assert result["budget_reset"] is True

    def test_clear_rules_for_compaction_is_idempotent_when_loaded_rules_already_empty(
        self,
    ) -> None:
        """Running clear-rules-for-compaction when loaded_rules already empty returns zeros."""
        mod = _load_writ_session()
        with tempfile.TemporaryDirectory() as tmpdir:
            mod.CACHE_DIR = tmpdir
            cache = _make_cache_with_rules(loaded_rules=[])
            result = _run_cmd(mod, mod.cmd_clear_rules_for_compaction, SESSION_ID, cache)
            assert result["rules_cleared"] == 0
            assert result["bytes_freed"] == 0


# ---------------------------------------------------------------------------
# TestSettingsJsonCompactionHooks -- settings.json registration
# ---------------------------------------------------------------------------


class TestSettingsJsonCompactionHooks:
    """PreCompact and PostCompact hooks must be registered in settings.json."""

    def _load_settings(self) -> dict[str, Any]:
        import os
        home = os.path.expanduser("~")
        settings_path = os.path.join(home, ".claude", "settings.json")
        with open(settings_path) as f:
            return json.load(f)

    def _extract_commands(self, entries: list) -> list[str]:
        """Extract command strings from settings.json hook entries (nested structure)."""
        commands = []
        for entry in entries:
            if isinstance(entry, dict):
                # Direct command at top level
                if "command" in entry:
                    commands.append(entry["command"])
                # Nested hooks array: {"matcher": "", "hooks": [{"command": "..."}]}
                for hook in entry.get("hooks", []):
                    if isinstance(hook, dict):
                        commands.append(hook.get("command", ""))
                    elif isinstance(hook, str):
                        commands.append(hook)
            elif isinstance(entry, str):
                commands.append(entry)
        return commands

    def test_precompact_hook_registered_in_settings(self) -> None:
        """settings.json PreCompact event includes writ-precompact.sh."""
        settings = self._load_settings()
        hooks = settings.get("hooks", {})
        precompact_hooks = hooks.get("PreCompact", [])
        hook_commands = self._extract_commands(precompact_hooks)
        assert any("writ-precompact.sh" in cmd for cmd in hook_commands), (
            "settings.json PreCompact must include writ-precompact.sh"
        )

    def test_postcompact_hook_registered_in_settings(self) -> None:
        """settings.json PostCompact event includes writ-postcompact.sh."""
        settings = self._load_settings()
        hooks = settings.get("hooks", {})
        postcompact_hooks = hooks.get("PostCompact", [])
        hook_commands = self._extract_commands(postcompact_hooks)
        assert any("writ-postcompact.sh" in cmd for cmd in hook_commands), (
            "settings.json PostCompact must include writ-postcompact.sh"
        )
