"""Part 3 (isolation cycle v2): the write-gate retrieval in
writ/server/routes/gate.py's /pre-write-check is project-scoped.

plan.md's Part 3 file list: "writ/server/routes/gate.py -- the write-gate rule
retrieval at line 550 calls pipeline.query with no project and is scoped like
every other caller." The session cache already carries `project_root` (gate.py
reads it at line 204 to resolve plan_hash), so the fix resolves THAT value
through resolve_project_for_cwd rather than adding a new request field --
pre_write_check's caller (the Write/Edit pre-tool-use hook) has no project_root
field on PreWriteCheckRequest today and this cycle does not add one.

Modeled directly on tests/test_pre_write_rag_logging.py's mocking style
(server.writ_session._read_cache, server.writ_session._can_write_check,
server._pipeline as a MagicMock) so a failure here says "the RAG branch does
not scope by project" without dragging in gate-approval or friction-logging
concerns that file already owns.

RED today: pre_write_check's RAG branch calls
server._pipeline.query(query_text=..., budget_tokens=..., exclude_rule_ids=...,
prefer_rule_ids=...) with no project kwarg, and never calls
resolve_project_for_cwd at all.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import writ.server as server
from writ.server import PreWriteCheckRequest, pre_write_check


class _FakeResolverDB:
    def __init__(self, resolved: str = "") -> None:
        self.resolved = resolved
        self.calls: list[str] = []

    async def resolve_project_for_cwd(self, cwd: str) -> str:
        self.calls.append(cwd)
        return self.resolved


def _base_cache(**overrides) -> dict:
    cache = {
        "mode": "work", "remaining_budget": 1500, "project_root": "/repo/proj-a",
        "loaded_rule_ids_by_phase": {}, "current_phase": "", "loaded_rule_ids": [],
    }
    cache.update(overrides)
    return cache


def _fake_pipeline():
    mock = MagicMock()
    mock.query.return_value = {"rules": [], "mode": "standard", "total_candidates": 0, "latency_ms": 1}
    return mock


class TestPreWriteCheckResolvesProjectFromCache:
    def test_rag_query_is_scoped_to_the_cached_project_root(self, monkeypatch) -> None:
        db = _FakeResolverDB(resolved="proj-a")
        pipeline = _fake_pipeline()
        monkeypatch.setattr(server, "_db", db)
        monkeypatch.setattr(server, "_pipeline", pipeline)
        monkeypatch.setattr(server.writ_session, "_read_cache", lambda sid: _base_cache())
        monkeypatch.setattr(
            server.writ_session, "_can_write_check",
            lambda sid, env, skill, cache=None: {"can_write": True, "reason": None},
        )

        req = PreWriteCheckRequest(
            session_id="s-scope-1",
            tool_input={"file_path": "/x/user_login_handler.py"},
            file_path="/x/user_login_handler.py",
            skill_dir="/x",
        )
        asyncio.run(pre_write_check(req))

        assert db.calls == ["/repo/proj-a"], (
            f"pre_write_check must resolve the cached project_root; got calls={db.calls!r}"
        )
        _, kwargs = pipeline.query.call_args
        assert kwargs.get("project") == "proj-a", (
            f"the write-gate RAG query must carry the resolved project; got {kwargs!r}"
        )

    def test_no_project_root_in_cache_queries_unscoped_not_erroring(self, monkeypatch) -> None:
        db = _FakeResolverDB(resolved="proj-a")
        pipeline = _fake_pipeline()
        monkeypatch.setattr(server, "_db", db)
        monkeypatch.setattr(server, "_pipeline", pipeline)
        monkeypatch.setattr(
            server.writ_session, "_read_cache", lambda sid: _base_cache(project_root=""),
        )
        monkeypatch.setattr(
            server.writ_session, "_can_write_check",
            lambda sid, env, skill, cache=None: {"can_write": True, "reason": None},
        )

        req = PreWriteCheckRequest(
            session_id="s-scope-2",
            tool_input={"file_path": "/x/user_login_handler.py"},
            file_path="/x/user_login_handler.py",
            skill_dir="/x",
        )
        result = asyncio.run(pre_write_check(req))

        assert result["decision"] == "allow", "an unresolved project must fail open"
        _, kwargs = pipeline.query.call_args
        assert not kwargs.get("project")

    def test_unregistered_project_root_degrades_to_doctrine_only(self, monkeypatch) -> None:
        db = _FakeResolverDB(resolved="")  # registered lookup found nothing
        pipeline = _fake_pipeline()
        monkeypatch.setattr(server, "_db", db)
        monkeypatch.setattr(server, "_pipeline", pipeline)
        monkeypatch.setattr(server.writ_session, "_read_cache", lambda sid: _base_cache())
        monkeypatch.setattr(
            server.writ_session, "_can_write_check",
            lambda sid, env, skill, cache=None: {"can_write": True, "reason": None},
        )

        req = PreWriteCheckRequest(
            session_id="s-scope-3",
            tool_input={"file_path": "/x/user_login_handler.py"},
            file_path="/x/user_login_handler.py",
            skill_dir="/x",
        )
        result = asyncio.run(pre_write_check(req))

        assert result["decision"] == "allow"
        _, kwargs = pipeline.query.call_args
        assert not kwargs.get("project"), (
            f"an unregistered project_root must degrade to doctrine-only "
            f"(project falsy), not raise or block the write; got {kwargs!r}"
        )

    def test_gate_denial_short_circuits_before_any_resolution(self, monkeypatch) -> None:
        """Guard: a gate denial must not even attempt project resolution -- the
        RAG branch (and now the resolution inside it) never runs on that path."""
        db = _FakeResolverDB(resolved="proj-a")
        pipeline = _fake_pipeline()
        monkeypatch.setattr(server, "_db", db)
        monkeypatch.setattr(server, "_pipeline", pipeline)
        monkeypatch.setattr(
            server.writ_session, "_read_cache",
            lambda sid: _base_cache(denial_counts={}),
        )
        monkeypatch.setattr(
            server.writ_session, "_can_write_check",
            lambda sid, env, skill, cache=None: {"can_write": False, "reason": "gate closed"},
        )

        req = PreWriteCheckRequest(
            session_id="s-scope-4",
            tool_input={"file_path": "/x/user_login_handler.py"},
            file_path="/x/user_login_handler.py",
            skill_dir="/x",
        )
        result = asyncio.run(pre_write_check(req))

        assert result["decision"] == "deny"
        assert db.calls == [], "a denied write must not trigger project resolution at all"
        pipeline.query.assert_not_called()
