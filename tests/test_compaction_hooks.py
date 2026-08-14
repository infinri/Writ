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
import os
import tempfile
from contextlib import redirect_stdout
from typing import Any
from unittest import mock
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio

try:
    from httpx import AsyncClient, ASGITransport
except ImportError:
    pytestmark = pytest.mark.skip(reason="httpx not installed")

from writ.server import app  # type: ignore[import]
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SESSION_ID = "test-compaction-hooks-session"
SKILL_DIR = str(Path(__file__).resolve().parent.parent)
WRIT_SESSION_PY = f"{SKILL_DIR}/bin/lib/writ-session.py"
SETTINGS_JSON = f"{SKILL_DIR}/../../settings.json"
HOOKS_JSON = Path(__file__).resolve().parent.parent / "hooks" / "hooks.json"


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
        self._tmpdir = tempfile.mkdtemp()
        self._env_patch = mock.patch.dict(os.environ, {"WRIT_CACHE_DIR": self._tmpdir})
        self._env_patch.start()

    def teardown_method(self):
        self._env_patch.stop()
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
        self._tmpdir = tempfile.mkdtemp()
        self._env_patch = mock.patch.dict(os.environ, {"WRIT_CACHE_DIR": self._tmpdir})
        self._env_patch.start()

    def teardown_method(self):
        self._env_patch.stop()
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

    def test_sets_post_compact_pending_true(self) -> None:
        """Cycle G: the hook that used to emit the directive itself now only
        QUEUES delivery. cmd_reset_after_compaction sets post_compact_pending
        so the next writ-rag-inject.sh UserPromptSubmit knows to deliver it,
        at zero extra process cost (same mutate_cache block that already
        clears the phase rule ids and resets the budget)."""
        cache = _make_cache_with_rules()
        _run_cmd(self.mod, self.mod.cmd_reset_after_compaction, SESSION_ID, cache)
        updated = self.mod._read_cache(SESSION_ID)
        assert updated.get("post_compact_pending") is True

    def test_sets_pending_true_alongside_the_existing_phase_reset(self) -> None:
        """The pending flag is set in the SAME mutate_cache block, not a
        second write: both effects must be visible from one reset call."""
        cache = _make_cache_with_rules()
        _run_cmd(self.mod, self.mod.cmd_reset_after_compaction, SESSION_ID, cache)
        updated = self.mod._read_cache(SESSION_ID)
        current_phase = cache["current_phase"]
        assert updated["loaded_rule_ids_by_phase"][current_phase] == []
        assert updated.get("post_compact_pending") is True


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
# TestDefaultCacheCarriesPostCompactPending -- schema single-source
# ---------------------------------------------------------------------------


class TestDefaultCacheCarriesPostCompactPending:
    """_default_cache() (writ/session/cache.py) is the single schema source
    _read_cache uses both for a fresh session and to backfill a loaded one, so
    a session cache written before this cycle reads back with the key false
    rather than missing -- the correct degradation (the flag simply never
    fires) rather than a KeyError somewhere downstream."""

    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()
        self._env_patch = mock.patch.dict(os.environ, {"WRIT_CACHE_DIR": self._tmpdir})
        self._env_patch.start()

    def teardown_method(self):
        self._env_patch.stop()
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_default_cache_has_post_compact_pending_false(self) -> None:
        from writ.session.cache import _default_cache
        assert _default_cache()["post_compact_pending"] is False

    def test_fresh_session_cache_reads_back_with_the_key_false(self) -> None:
        mod = _load_writ_session()
        fresh = mod._read_cache("test-pcp-fresh-session")
        assert fresh.get("post_compact_pending") is False

    def test_legacy_cache_written_without_the_key_reads_back_false(self) -> None:
        """A cache file written before this cycle (no post_compact_pending key
        at all) must read back with the key present and false, not KeyError
        and not True."""
        mod = _load_writ_session()
        sid = "test-pcp-legacy-session"
        with open(mod._cache_path(sid), "w") as f:
            json.dump({"mode": "work", "current_phase": "implementation"}, f)
        reread = mod._read_cache(sid)
        assert reread.get("post_compact_pending") is False


# ---------------------------------------------------------------------------
# TestClearPostCompactPendingUpdateHandler -- cmd_update flag
# ---------------------------------------------------------------------------


class TestClearPostCompactPendingUpdateHandler:
    """`update --clear-post-compact-pending` (writ/session/budget_tracking.py),
    mirroring `--set-recall-briefed`: writ-rag-inject.sh calls this once, after
    emitting, to clear the one-shot flag."""

    def setup_method(self):
        self.mod = _load_writ_session()
        self._tmpdir = tempfile.mkdtemp()
        self._env_patch = mock.patch.dict(os.environ, {"WRIT_CACHE_DIR": self._tmpdir})
        self._env_patch.start()

    def teardown_method(self):
        self._env_patch.stop()
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_clears_a_true_flag_to_false(self) -> None:
        sid = "test-pcp-clear"
        cache = self.mod._read_cache(sid)
        cache["post_compact_pending"] = True
        self.mod._write_cache(sid, cache)
        self.mod.cmd_update(sid, ["--clear-post-compact-pending"])
        updated = self.mod._read_cache(sid)
        assert updated.get("post_compact_pending") is False

    def test_idempotent_when_already_false(self) -> None:
        sid = "test-pcp-clear-noop"
        cache = self.mod._read_cache(sid)
        cache["post_compact_pending"] = False
        self.mod._write_cache(sid, cache)
        self.mod.cmd_update(sid, ["--clear-post-compact-pending"])
        updated = self.mod._read_cache(sid)
        assert updated.get("post_compact_pending") is False

    def test_idempotent_when_key_absent(self) -> None:
        """A legacy cache with no post_compact_pending key at all must not
        raise; clearing an absent flag is a safe no-op."""
        sid = "test-pcp-clear-absent"
        cache = {"mode": "work"}
        with open(self.mod._cache_path(sid), "w") as f:
            json.dump(cache, f)
        self.mod.cmd_update(sid, ["--clear-post-compact-pending"])
        updated = self.mod._read_cache(sid)
        assert updated.get("post_compact_pending") is False


# ---------------------------------------------------------------------------
# TestCycleAHeuristicCoexistence -- fallback safety
# ---------------------------------------------------------------------------


class TestPostCompactIsSoleRecovery:
    """PostCompact is the sole compaction-recovery TRIGGER; the Cycle A
    detect-compaction heuristic was removed entirely (POL-5a-cleanup).

    Cycle G splits trigger from delivery into two hooks: PostCompact fires
    once, authoritatively, on the real compaction event and QUEUES recovery
    (cmd_reset_after_compaction resets the phase/budget state AND sets
    post_compact_pending=True) -- it does not deliver anything itself, because
    CC's hook-output validator rejects a PostCompact hookSpecificOutput reply
    outright. The next writ-rag-inject.sh UserPromptSubmit invocation is the
    one hook confirmed to reach the model on this build, so it is what
    actually emits the state line + directive and clears the flag. There is
    still exactly one authoritative trigger (no detect-compaction heuristic
    resurrected to duplicate it) -- delivery just no longer rides the same
    hook invocation as the trigger.
    """

    def test_detect_compaction_removed_from_rag_inject_hook(self) -> None:
        """writ-rag-inject.sh no longer calls detect-compaction.

        The env-var-based heuristic was removed because the env var it read
        doesn't exist in Claude Code, and POL-5a-cleanup removed the dead
        cmd_detect_compaction chain entirely. PostCompact hook is the sole
        authoritative TRIGGER; writ-rag-inject.sh only ever DELIVERS what
        PostCompact already queued via post_compact_pending, never detects
        the compaction itself.
        """
        hook = f"{SKILL_DIR}/hooks/scripts/writ-rag-inject.sh"
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
            with mock.patch.dict(os.environ, {"WRIT_CACHE_DIR": tmpdir}):
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
            with mock.patch.dict(os.environ, {"WRIT_CACHE_DIR": tmpdir}):
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
        with open(HOOKS_JSON) as f:
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
