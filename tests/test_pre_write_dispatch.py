"""Tests for the /pre-write-check endpoint and hook consolidation (Cycle B, Item 8).

Per TEST-TDD-001: skeletons approved before implementation.
Covers: POST /pre-write-check endpoint decisions (allow/deny/ask), RAG metadata
in the allow response, _can_write_check reusable function, fallback path in
common.sh, and settings.json hook consolidation.
"""

from __future__ import annotations

import importlib.util
import json
import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

try:
    from httpx import AsyncClient, ASGITransport
except ImportError:
    pytestmark = pytest.mark.skip(reason="httpx not installed")

from writ.server import app  # type: ignore[import]

try:
    from writ.server import PreWriteCheckRequest  # type: ignore[import]
except ImportError:
    PreWriteCheckRequest = None  # type: ignore[assignment,misc]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SESSION_ID = "test-pre-write-dispatch"
SKILL_DIR = "/home/lucio.saldivar/.claude/skills/writ"
WRIT_SESSION_PY = f"{SKILL_DIR}/bin/lib/writ-session.py"
COMMON_SH = f"{SKILL_DIR}/bin/lib/common.sh"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_writ_session():
    """Load writ-session.py as a module without installing it."""
    spec = importlib.util.spec_from_file_location("writ_session_dispatch", WRIT_SESSION_PY)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_allow_session_cache(**overrides: Any) -> dict[str, Any]:
    """Cache representing a session where writes are permitted."""
    base: dict[str, Any] = {
        "session_id": SESSION_ID,
        "mode": "work",
        "current_phase": "implementation",
        "remaining_budget": 5000,
        "context_percent": 40,
        "loaded_rule_ids": [],
        "loaded_rules": [],
        "loaded_rule_ids_by_phase": {},
        "queries": 3,
        "pending_violations": [],
        "escalation": {"needed": False},
        "invalidation_history": {},
        "failed_writes": [],
        "gates_approved": ["phase-a", "test-skeletons"],
        "denial_counts": {},
    }
    base.update(overrides)
    return base


def _make_deny_session_cache(**overrides: Any) -> dict[str, Any]:
    """Cache representing a session where writes are blocked (gate not approved)."""
    base = _make_allow_session_cache(
        gates_approved=[],
        current_phase="planning",
    )
    base.update(overrides)
    return base


@pytest.fixture()
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture()
def mock_writ_session_allow():
    """Mock writ_session where the gate check passes."""
    mock = MagicMock()
    mock._read_cache = MagicMock(return_value=_make_allow_session_cache())
    mock._write_cache = MagicMock(return_value=None)
    mock.DEFAULT_SESSION_BUDGET = 8000

    def _fake_can_write_check(session_id: str, envelope: dict, skill_dir: str) -> dict:
        return {"can_write": True, "reason": None}

    mock._can_write_check = MagicMock(side_effect=_fake_can_write_check)
    return mock


@pytest.fixture()
def mock_writ_session_deny():
    """Mock writ_session where the gate check fails."""
    mock = MagicMock()
    mock._read_cache = MagicMock(return_value=_make_deny_session_cache())
    mock._write_cache = MagicMock(return_value=None)
    mock.DEFAULT_SESSION_BUDGET = 8000

    def _fake_can_write_check(session_id: str, envelope: dict, skill_dir: str) -> dict:
        return {"can_write": False, "reason": "[ENF-GATE-PLAN] Gate not approved"}

    mock._can_write_check = MagicMock(side_effect=_fake_can_write_check)
    return mock


@pytest_asyncio.fixture()
async def client_allow(mock_writ_session_allow):
    transport = ASGITransport(app=app)
    with patch("writ.server.writ_session", mock_writ_session_allow):
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


@pytest_asyncio.fixture()
async def client_deny(mock_writ_session_deny):
    transport = ASGITransport(app=app)
    with patch("writ.server.writ_session", mock_writ_session_deny):
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


def _pre_write_payload(file_path: str = "writ/server.py") -> dict[str, Any]:
    return {
        "session_id": SESSION_ID,
        "tool_input": {"file_path": file_path, "content": "# stub"},
        "skill_dir": SKILL_DIR,
        "file_path": file_path,
    }


# ---------------------------------------------------------------------------
# TestPreWriteCheckEndpoint -- response shape
# ---------------------------------------------------------------------------


class TestPreWriteCheckEndpoint:
    """POST /pre-write-check returns allow/deny/ask decisions with correct structure."""

    @pytest.mark.asyncio
    async def test_allow_decision_returns_200(self, client_allow: AsyncClient) -> None:
        """POST /pre-write-check returns HTTP 200 when gate passes."""
        resp = await client_allow.post("/pre-write-check", json=_pre_write_payload())
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_allow_decision_field_is_allow(self, client_allow: AsyncClient) -> None:
        """Response decision is 'allow' when gate check passes."""
        resp = await client_allow.post("/pre-write-check", json=_pre_write_payload())
        body = resp.json()
        assert body["decision"] == "allow"

    @pytest.mark.asyncio
    async def test_deny_decision_field_is_deny(self, client_deny: AsyncClient) -> None:
        """Response decision is 'deny' when gate check fails."""
        resp = await client_deny.post("/pre-write-check", json=_pre_write_payload())
        body = resp.json()
        assert body["decision"] in ("deny", "ask")

    @pytest.mark.asyncio
    async def test_deny_response_includes_reason(self, client_deny: AsyncClient) -> None:
        """Deny response includes a non-empty reason string."""
        resp = await client_deny.post("/pre-write-check", json=_pre_write_payload())
        body = resp.json()
        assert body.get("reason") is not None
        assert len(body["reason"]) > 0

    @pytest.mark.asyncio
    async def test_deny_reason_matches_gate_denial_format(
        self, client_deny: AsyncClient
    ) -> None:
        """Deny reason includes the ENF- prefix pattern matching check-gate-approval.sh output."""
        resp = await client_deny.post("/pre-write-check", json=_pre_write_payload())
        body = resp.json()
        assert "ENF-" in body.get("reason", "")

    @pytest.mark.asyncio
    async def test_allow_response_includes_rag_rules_field(
        self, client_allow: AsyncClient
    ) -> None:
        """Allow response includes rag_rules text field (may be empty string if no rules)."""
        resp = await client_allow.post("/pre-write-check", json=_pre_write_payload())
        assert resp.status_code == 200
        body = resp.json()
        assert "rag_rules" in body, "allow response must include rag_rules field"
        assert isinstance(body["rag_rules"], str)

    @pytest.mark.asyncio
    async def test_allow_response_includes_rag_meta_field(
        self, client_allow: AsyncClient
    ) -> None:
        """Allow response includes rag_meta with rule_ids list and tokens integer."""
        resp = await client_allow.post("/pre-write-check", json=_pre_write_payload())
        assert resp.status_code == 200
        body = resp.json()
        assert "rag_meta" in body, "allow response must include rag_meta field"
        rag_meta = body["rag_meta"]
        assert "rule_ids" in rag_meta, "rag_meta must include rule_ids"
        assert "tokens" in rag_meta, "rag_meta must include tokens"
        assert isinstance(rag_meta["rule_ids"], list)
        assert isinstance(rag_meta["tokens"], int)

    @pytest.mark.asyncio
    async def test_repeated_denials_escalate_to_ask(
        self, client_deny: AsyncClient, mock_writ_session_deny
    ) -> None:
        """After repeated deny decisions for the same session, decision escalates to 'ask'."""
        # Set denial_counts >= 2 to trigger escalation
        deny_cache = _make_deny_session_cache(denial_counts={"phase-a": 3})
        mock_writ_session_deny._read_cache.return_value = deny_cache
        resp = await client_deny.post("/pre-write-check", json=_pre_write_payload())
        body = resp.json()
        assert body["decision"] == "ask"

    @pytest.mark.asyncio
    async def test_endpoint_rejects_missing_session_id(
        self, client_allow: AsyncClient
    ) -> None:
        """POST /pre-write-check without session_id returns HTTP 422."""
        payload = {k: v for k, v in _pre_write_payload().items() if k != "session_id"}
        resp = await client_allow.post("/pre-write-check", json=payload)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_endpoint_handler_is_async(self) -> None:
        """POST /pre-write-check route endpoint is declared with async def."""
        import inspect
        routes = [
            r for r in app.routes
            if hasattr(r, "path") and "pre-write-check" in getattr(r, "path", "")
        ]
        assert len(routes) > 0, "/pre-write-check route not registered"
        for route in routes:
            endpoint = getattr(route, "endpoint", None)
            if endpoint is not None:
                assert inspect.iscoroutinefunction(endpoint)


# ---------------------------------------------------------------------------
# TestCanWriteCheckReusable -- _can_write_check extraction
# ---------------------------------------------------------------------------


class TestCanWriteCheckReusable:
    """_can_write_check must be extractable from cmd_can_write as a standalone function."""

    def test_can_write_check_function_exists_in_writ_session(self) -> None:
        """writ-session.py defines _can_write_check as a callable function."""
        mod = _load_writ_session()
        assert hasattr(mod, "_can_write_check"), (
            "_can_write_check must be defined as a standalone function in writ-session.py"
        )
        assert callable(mod._can_write_check), (
            "_can_write_check must be callable"
        )

    def test_can_write_check_returns_dict(self) -> None:
        """_can_write_check returns a dict (not None or bool)."""
        mod = _load_writ_session()
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            mod.CACHE_DIR = tmpdir
            # Create a cache with work mode + gates approved
            cache = _make_allow_session_cache()
            path = mod._cache_path(SESSION_ID)
            import json
            with open(path, "w") as f:
                json.dump(cache, f)
            result = mod._can_write_check(SESSION_ID, {"tool_input": {"file_path": "test.py"}})
            assert isinstance(result, dict)

    def test_can_write_check_result_has_can_write_field(self) -> None:
        """_can_write_check result contains can_write bool field."""
        mod = _load_writ_session()
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            mod.CACHE_DIR = tmpdir
            cache = _make_allow_session_cache()
            path = mod._cache_path(SESSION_ID)
            import json
            with open(path, "w") as f:
                json.dump(cache, f)
            result = mod._can_write_check(SESSION_ID, {"tool_input": {"file_path": "test.py"}})
            assert "can_write" in result
            assert isinstance(result["can_write"], bool)

    def test_can_write_check_returns_reason_on_deny(self) -> None:
        """_can_write_check result contains a non-empty reason string when can_write is False."""
        mod = _load_writ_session()
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            mod.CACHE_DIR = tmpdir
            # Cache with no mode = deny
            cache = _make_deny_session_cache(mode=None, gates_approved=[])
            path = mod._cache_path(SESSION_ID)
            import json
            with open(path, "w") as f:
                json.dump(cache, f)
            result = mod._can_write_check(SESSION_ID, {"tool_input": {"file_path": "src/main.py"}})
            assert result["can_write"] is False
            assert result["reason"] is not None
            assert len(result["reason"]) > 0

    def test_cmd_can_write_calls_can_write_check(self) -> None:
        """cmd_can_write delegates to _can_write_check internally (source inspection)."""
        mod = _load_writ_session()
        import inspect
        src = inspect.getsource(mod.cmd_can_write)
        assert "_can_write_check" in src, (
            "cmd_can_write must delegate to _can_write_check"
        )


# ---------------------------------------------------------------------------
# TestPreWriteCheckRequestModel -- Pydantic model
# ---------------------------------------------------------------------------


class TestPreWriteCheckRequestModel:
    """PreWriteCheckRequest Pydantic model has required fields."""

    def test_pre_write_check_request_importable(self) -> None:
        """PreWriteCheckRequest can be imported from writ.server."""
        assert PreWriteCheckRequest is not None

    def test_pre_write_check_request_has_session_id_field(self) -> None:
        """PreWriteCheckRequest has session_id: str field."""
        model = PreWriteCheckRequest(session_id="test", tool_input={}, skill_dir="", file_path="")
        assert model.session_id == "test"

    def test_pre_write_check_request_has_file_path_field(self) -> None:
        """PreWriteCheckRequest has file_path: str field."""
        model = PreWriteCheckRequest(session_id="test", file_path="/tmp/foo.py")
        assert model.file_path == "/tmp/foo.py"

    def test_pre_write_check_request_has_skill_dir_field(self) -> None:
        """PreWriteCheckRequest has skill_dir: str field."""
        model = PreWriteCheckRequest(session_id="test", skill_dir="/skill")
        assert model.skill_dir == "/skill"

    def test_pre_write_check_request_has_tool_input_field(self) -> None:
        """PreWriteCheckRequest has tool_input: dict field for the full envelope."""
        model = PreWriteCheckRequest(session_id="test", tool_input={"file_path": "foo.py"})
        assert model.tool_input == {"file_path": "foo.py"}


# ---------------------------------------------------------------------------
# TestFallbackPath -- dispatcher falls back when server unreachable
# ---------------------------------------------------------------------------


class TestFallbackPath:
    """Fallback: dispatcher uses individual _writ_session calls when server is unreachable."""

    def test_common_sh_has_pre_write_check_case(self) -> None:
        """common.sh contains a pre-write-check case in _writ_session()."""
        with open(COMMON_SH) as f:
            source = f.read()
        assert "pre-write-check" in source, (
            "common.sh must have a pre-write-check case in _writ_session()"
        )

    def test_common_sh_pre_write_check_posts_to_endpoint(self) -> None:
        """common.sh pre-write-check case POSTs to /pre-write-check via curl."""
        with open(COMMON_SH) as f:
            source = f.read()
        assert "/pre-write-check" in source, (
            "common.sh pre-write-check case must POST to /pre-write-check"
        )

    def test_common_sh_pre_write_check_has_fallback(self) -> None:
        """common.sh pre-write-check has a fallback to individual subcommand calls."""
        with open(COMMON_SH) as f:
            source = f.read()
        # The fallback path must invoke can-write at minimum when server is down
        assert "can-write" in source, (
            "common.sh fallback for pre-write-check must include can-write call"
        )


# ---------------------------------------------------------------------------
# TestSettingsJsonConsolidation -- settings.json hook changes
# ---------------------------------------------------------------------------


class TestSettingsJsonConsolidation:
    """Hook consolidation must be reflected in settings.json."""

    def _load_settings(self) -> dict[str, Any]:
        home = os.path.expanduser("~")
        settings_path = os.path.join(home, ".claude", "settings.json")
        with open(settings_path) as f:
            return json.load(f)

    def _get_pretooluse_write_commands(self) -> list[str]:
        settings = self._load_settings()
        hooks = settings.get("hooks", {})
        pretooluse = hooks.get("PreToolUse", [])
        commands: list[str] = []
        for entry in pretooluse:
            if isinstance(entry, dict):
                matcher = entry.get("matcher", "")
                if "Write" in matcher or "Edit" in matcher:
                    for hook in entry.get("hooks", []):
                        if isinstance(hook, dict):
                            commands.append(hook.get("command", ""))
                        elif isinstance(hook, str):
                            commands.append(hook)
            elif isinstance(entry, str):
                commands.append(entry)
        return commands

    def test_check_gate_approval_removed_from_pretooluse(self) -> None:
        """settings.json PreToolUse Write|Edit must not include check-gate-approval.sh."""
        commands = self._get_pretooluse_write_commands()
        assert not any("check-gate-approval" in cmd for cmd in commands), (
            "check-gate-approval.sh must be removed from PreToolUse Write|Edit"
        )

    def test_enforce_final_gate_removed_from_pretooluse(self) -> None:
        """settings.json PreToolUse Write|Edit must not include enforce-final-gate.sh."""
        commands = self._get_pretooluse_write_commands()
        assert not any("enforce-final-gate" in cmd for cmd in commands), (
            "enforce-final-gate.sh must be removed from PreToolUse Write|Edit"
        )

    def test_writ_pretool_rag_removed_from_pretooluse(self) -> None:
        """settings.json PreToolUse Write|Edit must not include writ-pretool-rag.sh."""
        commands = self._get_pretooluse_write_commands()
        assert not any("writ-pretool-rag.sh" in cmd for cmd in commands), (
            "writ-pretool-rag.sh must be removed from PreToolUse Write|Edit"
        )

    def test_writ_pre_write_dispatch_added_to_pretooluse(self) -> None:
        """settings.json PreToolUse Write|Edit must include writ-pre-write-dispatch.sh."""
        commands = self._get_pretooluse_write_commands()
        assert any("writ-pre-write-dispatch" in cmd for cmd in commands), (
            "writ-pre-write-dispatch.sh must be added to PreToolUse Write|Edit"
        )

    def test_pre_validate_file_still_in_pretooluse(self) -> None:
        """settings.json PreToolUse Write|Edit still includes pre-validate-file.sh."""
        commands = self._get_pretooluse_write_commands()
        assert any("pre-validate-file" in cmd for cmd in commands), (
            "pre-validate-file.sh must remain in PreToolUse Write|Edit (not consolidated)"
        )

    def test_writ_pre_write_dispatch_bash_permission_added(self) -> None:
        """settings.json Bash permission for writ-pre-write-dispatch.sh is added."""
        settings = self._load_settings()
        permissions = settings.get("permissions", {})
        allowed_tools = permissions.get("allow", [])
        bash_allows = [
            t for t in allowed_tools
            if isinstance(t, str) and "writ-pre-write-dispatch" in t
        ]
        assert len(bash_allows) > 0, (
            "Bash permission for writ-pre-write-dispatch.sh must be added to settings.json"
        )
