"""Part 3 (isolation cycle v2): server-side project resolution for /query,
/methodology-companion and /prompt-bundle.

plan.md's Part 3 file list: "writ/server/models.py -- CompanionRequest and
PromptBundleRequest gain a project-root field and QueryRequest gains the root
alongside its existing name field; only QueryRequest has any project field
today" and "writ/server/routes/query.py -- resolve the request's project root
to a project name once per request, and pass it into the internal channel-1
QueryRequest at line 216 which currently drops it, so the hot per-prompt path
is scoped and not just /query".

Field-naming choice pinned by these tests: `project_root`, matching every other
request model that already carries a caller-supplied repo path
(CommitCaptureRequest, GitHooksAutoInstallRequest, RecallRequest,
SessionAdvancePhaseRequest all use this exact name). `QueryRequest.project`
already exists and is the RESOLVED project NAME; `project_root` is the new,
unresolved field the hooks actually hold (from detect_project_root) and send.

These tests call the route coroutines directly (no TestClient, no app
lifespan), the same style as tests/test_pre_write_rag_logging.py and
tests/test_retrieval_quality_telemetry.py, so nothing here opens a real Neo4j
connection or a real ONNX model.

RED today: QueryRequest/CompanionRequest/PromptBundleRequest have no
project_root field, and neither route resolves one.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import writ.server as server
import writ.server.routes.query as qroute
from writ.server.models import CompanionRequest, PromptBundleRequest, QueryRequest


# ---------------------------------------------------------------------------
# Model-level: the three request bodies accept project_root
# ---------------------------------------------------------------------------


class TestModelsAcceptProjectRoot:
    def test_query_request_accepts_project_root(self) -> None:
        req = QueryRequest(query="x", project_root="/repo/proj-a")
        assert req.project_root == "/repo/proj-a"

    def test_query_request_project_root_defaults_empty(self) -> None:
        """Every existing caller today sends no project_root at all; the field
        must default to something falsy, not require every caller to be
        touched in the same change that adds it."""
        req = QueryRequest(query="x")
        assert req.project_root == ""

    def test_companion_request_accepts_project_root(self) -> None:
        req = CompanionRequest(mode="work", project_root="/repo/proj-a")
        assert req.project_root == "/repo/proj-a"

    def test_prompt_bundle_request_accepts_project_root(self) -> None:
        req = PromptBundleRequest(session_id="s1", project_root="/repo/proj-a")
        assert req.project_root == "/repo/proj-a"


# ---------------------------------------------------------------------------
# /query resolves project_root server-side
# ---------------------------------------------------------------------------


class _FakeResolverDB:
    """Exposes only the one method /query's project resolution needs."""

    def __init__(self, resolved: str = "") -> None:
        self.resolved = resolved
        self.calls: list[str] = []

    async def resolve_project_for_cwd(self, cwd: str) -> str:
        self.calls.append(cwd)
        return self.resolved


def _fake_pipeline(rules=None):
    mock = MagicMock()
    mock.query.return_value = {
        "rules": rules or [], "mode": "standard", "total_candidates": 0,
        "latency_ms": 1.0, "abstain_signal": 0.0,
    }
    return mock


@pytest.fixture()
def isolated_friction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log_path = tmp_path / "workflow-friction.log"
    monkeypatch.setenv("WRIT_FRICTION_LOG", str(log_path))
    return log_path


def _read_events(log_path: Path, event: str) -> list[dict]:
    if not log_path.exists():
        return []
    out = []
    for line in log_path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("event") == event:
            out.append(row)
    return out


class TestQueryRouteResolvesProjectRoot:
    @pytest.mark.asyncio
    async def test_project_root_is_resolved_and_forwarded_to_the_pipeline(
        self, isolated_friction, monkeypatch,
    ) -> None:
        db = _FakeResolverDB(resolved="proj-a")
        pipeline = _fake_pipeline()
        monkeypatch.setattr(server, "_db", db)
        monkeypatch.setattr(server, "_pipeline", pipeline)

        await qroute.query_rules(QueryRequest(query="secret", project_root="/repo/proj-a"))

        assert db.calls == ["/repo/proj-a"], (
            f"the route must resolve project_root via resolve_project_for_cwd; "
            f"got calls={db.calls!r}"
        )
        _, kwargs = pipeline.query.call_args
        assert kwargs.get("project") == "proj-a", (
            f"the resolved project name must reach pipeline.query(project=...); "
            f"got kwargs={kwargs!r}"
        )

    @pytest.mark.asyncio
    async def test_no_project_root_sent_resolves_nothing_and_queries_unscoped(
        self, isolated_friction, monkeypatch,
    ) -> None:
        """Backward compat: a caller that sends neither project nor project_root
        (every hook today) must not trip a resolution call at all."""
        db = _FakeResolverDB(resolved="proj-a")
        pipeline = _fake_pipeline()
        monkeypatch.setattr(server, "_db", db)
        monkeypatch.setattr(server, "_pipeline", pipeline)

        await qroute.query_rules(QueryRequest(query="secret"))

        assert db.calls == [], "no project_root was sent; the resolver must not be called"
        _, kwargs = pipeline.query.call_args
        assert kwargs.get("project") is None

    @pytest.mark.asyncio
    async def test_unresolved_project_root_degrades_to_doctrine_only_not_an_error(
        self, isolated_friction, monkeypatch,
    ) -> None:
        db = _FakeResolverDB(resolved="")  # unregistered cwd
        pipeline = _fake_pipeline()
        monkeypatch.setattr(server, "_db", db)
        monkeypatch.setattr(server, "_pipeline", pipeline)

        result = await qroute.query_rules(
            QueryRequest(query="secret", project_root="/tmp/unregistered")
        )

        assert "error" not in result, "an unresolved project must fail open, never raise/error"
        _, kwargs = pipeline.query.call_args
        assert not kwargs.get("project"), (
            f"an unresolved project_root must reach pipeline.query as no project "
            f"(doctrine-only degrade), got {kwargs.get('project')!r}"
        )

    @pytest.mark.asyncio
    async def test_unresolved_project_root_is_visible_on_the_retrieval_result_row(
        self, isolated_friction, monkeypatch,
    ) -> None:
        """Capability 30. A silent scope degradation is exactly the failure mode
        query.py's own _emit_retrieval_result docstring calls out for the S4
        abstention gate: three different outcomes must not collapse into one
        indistinguishable log line. The RAW project_root the caller sent must
        appear on the row even though it failed to resolve, so an operator can
        tell WHICH root is unregistered rather than only that resolution failed
        for someone, somewhere."""
        db = _FakeResolverDB(resolved="")
        pipeline = _fake_pipeline()
        monkeypatch.setattr(server, "_db", db)
        monkeypatch.setattr(server, "_pipeline", pipeline)

        await qroute.query_rules(
            QueryRequest(query="secret", session_id="sess-1", project_root="/tmp/unregistered")
        )

        rows = _read_events(isolated_friction, "retrieval_result")
        assert len(rows) == 1, f"expected exactly one retrieval_result row, got {rows!r}"
        row = rows[0]
        assert row.get("project_root") == "/tmp/unregistered", (
            f"the unresolved project_root must be recorded on the row; got {row!r}"
        )
        assert not row.get("project"), (
            f"the resolved project must be empty/absent for an unresolved root; got {row!r}"
        )


# ---------------------------------------------------------------------------
# /prompt-bundle's internal channel-1 QueryRequest carries the resolved project
# ---------------------------------------------------------------------------


class TestPromptBundleForwardsResolvedProject:
    @pytest.mark.asyncio
    async def test_channel_one_query_is_scoped_to_the_resolved_project(
        self, isolated_friction, monkeypatch,
    ) -> None:
        db = _FakeResolverDB(resolved="proj-a")
        pipeline = _fake_pipeline()
        monkeypatch.setattr(server, "_db", db)
        monkeypatch.setattr(server, "_pipeline", pipeline)
        monkeypatch.setattr(
            server.writ_session, "_read_cache",
            lambda sid: {"remaining_budget": 1500, "loaded_rule_ids": [],
                         "last_injected_rule_ids": [], "detected_domain": ""},
        )
        monkeypatch.setattr(server.writ_session, "cmd_update", lambda sid, args: None)
        monkeypatch.setattr(server, "_run_cmd_format_locked", lambda payload: "")

        async def _fake_always_on(**kwargs):
            return {"rules": [], "total_tokens": 0}

        monkeypatch.setattr(qroute, "always_on_bundle", _fake_always_on)

        await qroute.prompt_bundle(PromptBundleRequest(
            session_id="s1", prompt="secret", project_root="/repo/proj-a",
        ))

        _, kwargs = pipeline.query.call_args
        assert kwargs.get("project") == "proj-a", (
            f"prompt_bundle's channel-1 /query call must carry the resolved "
            f"project, not drop it as query.py:216 does today; got {kwargs!r}"
        )

    @pytest.mark.asyncio
    async def test_no_project_root_on_the_bundle_request_queries_unscoped(
        self, isolated_friction, monkeypatch,
    ) -> None:
        db = _FakeResolverDB(resolved="proj-a")
        pipeline = _fake_pipeline()
        monkeypatch.setattr(server, "_db", db)
        monkeypatch.setattr(server, "_pipeline", pipeline)
        monkeypatch.setattr(
            server.writ_session, "_read_cache",
            lambda sid: {"remaining_budget": 1500, "loaded_rule_ids": [],
                         "last_injected_rule_ids": [], "detected_domain": ""},
        )
        monkeypatch.setattr(server.writ_session, "cmd_update", lambda sid, args: None)
        monkeypatch.setattr(server, "_run_cmd_format_locked", lambda payload: "")

        async def _fake_always_on(**kwargs):
            return {"rules": [], "total_tokens": 0}

        monkeypatch.setattr(qroute, "always_on_bundle", _fake_always_on)

        await qroute.prompt_bundle(PromptBundleRequest(session_id="s1", prompt="secret"))

        assert db.calls == [], "no project_root sent; the resolver must not be called"
        _, kwargs = pipeline.query.call_args
        assert kwargs.get("project") is None
