"""Decision Memory Phase 2 RECALL: tests for the /recall server route.

Every test here is RED until the implementer adds the /recall route and
RecallRequest model to writ/server.py. Tests fail on HTTP 404 (route missing)
or AssertionError, never on a collection/import error.

CRITICAL isolation guarantee: NO test in this file touches the live Neo4j
graph. All tests use FastAPI TestClient with monkeypatched _db and a patched
compile_recall -- the same pattern used for /commit/capture in
test_decision_memory_commit.py.

Run: .venv/bin/python -m pytest tests/test_server_recall.py

Capability map:
  [server-recall-1]  /recall returns {"error": ...} when _db is None
  [server-recall-2]  /recall is fail-open: compile_recall raising logs friction
                     and returns {ok, briefing:"", decisions:[]} (never raises)
  [server-recall-3]  /recall returns {ok, briefing, decisions} shape on success
  [server-recall-4]  /recall degrades safely when the project cannot be
                      resolved (isolation cycle v2, Part 3)
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from writ.server import app


# ---------------------------------------------------------------------------
# Fixtures (TEST-FIXTURE-001)
# ---------------------------------------------------------------------------

@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate WRIT_CACHE_DIR and WRIT_FRICTION_LOG so friction-log writes
    from the fail-open path do not pollute the real log."""
    cache_dir = tmp_path / "writ-cache"
    cache_dir.mkdir()
    log_path = tmp_path / "workflow-friction.log"
    monkeypatch.setenv("WRIT_CACHE_DIR", str(cache_dir))
    monkeypatch.setenv("WRIT_FRICTION_LOG", str(log_path))
    return tmp_path


# ---------------------------------------------------------------------------
# Minimal fake db for success-path tests
# ---------------------------------------------------------------------------

class _FakeDB:
    """Fake db that exposes only the methods the /recall route calls."""

    def __init__(self, project: str = "writ") -> None:
        self._project = project

    async def resolve_project_for_cwd(self, cwd: str) -> str:
        return self._project


# ---------------------------------------------------------------------------
# Request payload factory
# ---------------------------------------------------------------------------

def _recall_request(
    project_root: str = "/tmp/fake-recall-repo",
    branch: str = "main",
    budget: int = 20000,
    full: bool = False,
) -> dict:
    """Minimal well-formed /recall request body."""
    return {
        "project_root": project_root,
        "branch": branch,
        "budget": budget,
        "full": full,
    }


# ---------------------------------------------------------------------------
# Tests: [server-recall-1] _db is None -> {"error": ...}
# ---------------------------------------------------------------------------

class TestRecallRouteDbNone:
    """[server-recall-1]: route returns error shape when database is not connected."""

    def test_db_none_returns_error_key(
        self,
        client: TestClient,
        isolated_cache: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # [server-recall-1]: POST /recall with _db=None must return HTTP 200 and
        # a body containing an "error" key (same pattern as /commit/capture).
        # RED: route does not exist yet (404).
        import writ.server as _srv
        monkeypatch.setattr(_srv, "_db", None)

        resp = client.post("/recall", json=_recall_request())

        assert resp.status_code == 200, (
            f"_db is None guard must return 200; got {resp.status_code}: {resp.text}"
        )
        data = resp.json()
        assert "error" in data, (
            f"_db is None must return a body with 'error' key; got {list(data.keys())!r}"
        )

    def test_db_none_error_message_mentions_database(
        self,
        client: TestClient,
        isolated_cache: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # [server-recall-1]: the error message must be informative (not empty).
        # RED: route does not exist yet (404).
        import writ.server as _srv
        monkeypatch.setattr(_srv, "_db", None)

        resp = client.post("/recall", json=_recall_request())
        data = resp.json()

        error_msg = data.get("error", "")
        assert len(error_msg) > 0, (
            "error message must be non-empty when _db is None"
        )

    def test_db_none_does_not_return_ok_true(
        self,
        client: TestClient,
        isolated_cache: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # [server-recall-1]: the error shape must NOT carry ok=True (it is an
        # error, not a degraded success).
        # RED: 404.
        import writ.server as _srv
        monkeypatch.setattr(_srv, "_db", None)

        resp = client.post("/recall", json=_recall_request())
        data = resp.json()

        assert data.get("ok") is not True, (
            f"_db is None response must not have ok=True; got {data!r}"
        )


# ---------------------------------------------------------------------------
# Tests: [server-recall-2] fail-open on compile_recall exception
# ---------------------------------------------------------------------------

class TestRecallRouteFailOpen:
    """[server-recall-2]: compile_recall raising must log friction and return
    {ok, briefing:"", decisions:[]} -- never raise to the caller."""

    def test_compile_recall_exception_returns_empty_but_valid_shape(
        self,
        client: TestClient,
        isolated_cache: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # [server-recall-2]: when compile_recall raises RuntimeError the route
        # must return HTTP 200 with {ok: True, briefing: "", decisions: []}.
        # RED: route does not exist yet (404).
        import writ.server as _srv

        monkeypatch.setattr(_srv, "_db", _FakeDB())

        async def _raise(*args, **kwargs):
            raise RuntimeError("simulated compile_recall failure")

        with patch("writ.session.recall.compile_recall", new=_raise):
            resp = client.post("/recall", json=_recall_request())

        assert resp.status_code == 200, (
            f"compile_recall failure must not propagate as a 500; "
            f"got {resp.status_code}: {resp.text}"
        )
        data = resp.json()
        assert data.get("ok") is True, (
            f"fail-open must return ok=True; got {data!r}"
        )
        assert data.get("briefing") == "", (
            f"fail-open must return briefing=''; got {data.get('briefing')!r}"
        )
        assert data.get("decisions") == [], (
            f"fail-open must return decisions=[]; got {data.get('decisions')!r}"
        )

    def test_compile_recall_exception_does_not_return_error_key(
        self,
        client: TestClient,
        isolated_cache: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # [server-recall-2]: the fail-open degraded shape must NOT carry "error"
        # (it is a safe empty payload, not the _db-None guard shape).
        # RED: 404.
        import writ.server as _srv

        monkeypatch.setattr(_srv, "_db", _FakeDB())

        async def _raise(*args, **kwargs):
            raise ValueError("another failure")

        with patch("writ.session.recall.compile_recall", new=_raise):
            resp = client.post("/recall", json=_recall_request())

        data = resp.json()
        assert "error" not in data, (
            f"fail-open shape must NOT have 'error' key; got {list(data.keys())!r}"
        )

    def test_compile_recall_exception_friction_log_does_not_block(
        self,
        client: TestClient,
        isolated_cache: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # [server-recall-2]: even if the friction log write itself fails (e.g.
        # log path unwritable), the route must still return 200. We verify this
        # by patching log_friction_event to also raise.
        # RED: 404.
        import writ.server as _srv

        monkeypatch.setattr(_srv, "_db", _FakeDB())

        async def _raise_compile(*args, **kwargs):
            raise RuntimeError("compile failure")

        def _raise_log(*args, **kwargs):
            raise OSError("log write failed")

        with patch("writ.session.recall.compile_recall", new=_raise_compile):
            with patch("writ.server.log_friction_event", new=_raise_log):
                resp = client.post("/recall", json=_recall_request())

        assert resp.status_code == 200, (
            f"route must return 200 even when both compile_recall AND "
            f"log_friction_event raise; got {resp.status_code}"
        )


# ---------------------------------------------------------------------------
# Tests: [server-recall-3] success shape
# ---------------------------------------------------------------------------

class TestRecallRouteSuccess:
    """[server-recall-3]: success returns {ok, briefing, decisions} shape."""

    def test_success_returns_ok_true_briefing_decisions(
        self,
        client: TestClient,
        isolated_cache: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # [server-recall-3]: when compile_recall returns a payload the route must
        # return {ok: True, briefing: str, decisions: list}.
        # RED: route does not exist yet (404).
        import writ.server as _srv

        monkeypatch.setattr(_srv, "_db", _FakeDB())

        canned_payload = {
            "briefing": "[Writ recall: recent decisions]\n- Add recall [PERF-BATCH-001]",
            "decisions": [
                {
                    "decision_id": "DEC-2026-001",
                    "title": "Add recall module",
                    "rationale": "Users need read-back.",
                    "planned_files": [],
                    "governing_rule_ids": ["PERF-BATCH-001"],
                    "rule_statements": {"PERF-BATCH-001": "Batch DB reads."},
                    "phase": "planning",
                    "ts": "2026-06-27T10:00:00+00:00",
                }
            ],
        }

        async def _canned_compile(*args, **kwargs):
            return canned_payload

        with patch("writ.session.recall.compile_recall", new=_canned_compile):
            resp = client.post("/recall", json=_recall_request())

        assert resp.status_code == 200, (
            f"success path must return 200; got {resp.status_code}: {resp.text}"
        )
        data = resp.json()

        assert data.get("ok") is True, f"success must have ok=True; got {data!r}"
        assert "briefing" in data, f"success must have 'briefing' key; got {list(data.keys())!r}"
        assert "decisions" in data, f"success must have 'decisions' key; got {list(data.keys())!r}"

    def test_success_briefing_content_passes_through(
        self,
        client: TestClient,
        isolated_cache: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # [server-recall-3]: the briefing string from compile_recall must appear
        # in the response unchanged.
        # RED: 404.
        import writ.server as _srv

        monkeypatch.setattr(_srv, "_db", _FakeDB())

        expected_briefing = "[Writ recall: recent decisions on this project]\n- Test decision [RULE-001]"

        async def _canned(*args, **kwargs):
            return {"briefing": expected_briefing, "decisions": []}

        with patch("writ.session.recall.compile_recall", new=_canned):
            resp = client.post("/recall", json=_recall_request())

        data = resp.json()
        assert data.get("briefing") == expected_briefing, (
            f"briefing must pass through unchanged; "
            f"expected {expected_briefing!r}, got {data.get('briefing')!r}"
        )

    def test_success_decisions_list_passes_through(
        self,
        client: TestClient,
        isolated_cache: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # [server-recall-3]: the decisions list from compile_recall must appear
        # in the response unchanged.
        # RED: 404.
        import writ.server as _srv

        monkeypatch.setattr(_srv, "_db", _FakeDB())

        expected_decisions = [
            {"decision_id": "DEC-PASS-001", "title": "Pass-through test", "rationale": "",
             "planned_files": [], "governing_rule_ids": [], "rule_statements": {},
             "phase": "planning", "ts": "2026-06-27T10:00:00+00:00"},
        ]

        async def _canned(*args, **kwargs):
            return {"briefing": "header", "decisions": expected_decisions}

        with patch("writ.session.recall.compile_recall", new=_canned):
            resp = client.post("/recall", json=_recall_request())

        data = resp.json()
        decisions = data.get("decisions", [])
        assert len(decisions) == 1, f"expected 1 decision; got {len(decisions)}"
        assert decisions[0]["decision_id"] == "DEC-PASS-001", (
            f"decision_id must pass through; got {decisions[0].get('decision_id')!r}"
        )

    def test_success_with_empty_decisions_returns_valid_shape(
        self,
        client: TestClient,
        isolated_cache: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # [server-recall-3]: a project with no decisions yet must return a valid
        # {ok, briefing:"", decisions:[]} shape (not 404 or error).
        # RED: 404.
        import writ.server as _srv

        monkeypatch.setattr(_srv, "_db", _FakeDB())

        async def _empty(*args, **kwargs):
            return {"briefing": "", "decisions": []}

        with patch("writ.session.recall.compile_recall", new=_empty):
            resp = client.post("/recall", json=_recall_request())

        assert resp.status_code == 200
        data = resp.json()
        assert data.get("ok") is True
        assert data.get("briefing") == ""
        assert data.get("decisions") == []

    def test_route_passes_budget_and_full_to_compile_recall(
        self,
        client: TestClient,
        isolated_cache: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # [server-recall-3]: the route must forward the budget and full parameters
        # from the request body to compile_recall (not hardcode them).
        # RED: 404.
        import writ.server as _srv

        monkeypatch.setattr(_srv, "_db", _FakeDB())

        captured_kwargs: dict = {}

        async def _capture(db, project, **kwargs):
            captured_kwargs.update(kwargs)
            return {"briefing": "", "decisions": []}

        with patch("writ.session.recall.compile_recall", new=_capture):
            client.post("/recall", json=_recall_request(budget=5000, full=True))

        assert captured_kwargs.get("budget") == 5000, (
            f"budget must be forwarded to compile_recall; got {captured_kwargs!r}"
        )
        assert captured_kwargs.get("full") is True, (
            f"full must be forwarded to compile_recall; got {captured_kwargs!r}"
        )


# ---------------------------------------------------------------------------
# Tests: [server-recall-4] unresolved project (isolation cycle v2, Part 3)
# ---------------------------------------------------------------------------

class TestRecallRouteUnresolvedProject:
    """[server-recall-4]: resolve_project_for_cwd's default no longer returns
    "writ" for an unregistered cwd -- it returns an empty string (see
    tests/test_phaseM3_project_query_scope.py::TestProjectRegistry). /recall
    must handle that empty answer explicitly, per plan.md's Part 3 Analysis:
    "the route must return its empty-but-valid payload with a logged reason
    rather than querying for the empty string."
    """

    def test_unresolved_project_returns_empty_but_valid_payload(
        self,
        client: TestClient,
        isolated_cache: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import writ.server as _srv

        monkeypatch.setattr(_srv, "_db", _FakeDB(project=""))

        compile_recall_mock = AsyncMock()

        with patch("writ.session.recall.compile_recall", new=compile_recall_mock):
            resp = client.post("/recall", json=_recall_request())

        assert resp.status_code == 200
        data = resp.json()
        assert data.get("ok") is True
        assert data.get("briefing") == ""
        assert data.get("decisions") == []
        compile_recall_mock.assert_not_called()

    def test_unresolved_project_logs_a_reason_not_silently(
        self,
        client: TestClient,
        isolated_cache: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import writ.server as _srv

        monkeypatch.setattr(_srv, "_db", _FakeDB(project=""))
        logged: list[dict] = []
        monkeypatch.setattr(_srv, "log_friction_event", lambda **kw: logged.append(kw))

        with patch("writ.session.recall.compile_recall", new=AsyncMock()):
            client.post("/recall", json=_recall_request(project_root="/tmp/unregistered-repo"))

        assert logged, "an unresolved project must log a friction event with a reason"
        assert any("project" in str(e).lower() for e in logged), (
            f"the logged event should mention the project-resolution reason; got {logged!r}"
        )

    def test_resolved_project_is_unaffected_by_this_guard(
        self,
        client: TestClient,
        isolated_cache: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Anti-vacuity: the guard must not swallow the normal success path too."""
        import writ.server as _srv

        monkeypatch.setattr(_srv, "_db", _FakeDB(project="proj-a"))

        async def _canned(*args, **kwargs):
            return {"briefing": "hello", "decisions": []}

        with patch("writ.session.recall.compile_recall", new=_canned):
            resp = client.post("/recall", json=_recall_request())

        assert resp.json().get("briefing") == "hello"
