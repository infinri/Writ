"""Tests for sticky rules / prompt-cache stability (Cycle C, Item 9).

Per TEST-TDD-001: skeletons approved before implementation.
Covers: last_injected_rule_ids session cache field, prefer_rule_ids parameter
on RetrievalPipeline.query() and QueryRequest, tie-breaking reorder logic,
compaction/reset clearing, and writ-rag-inject.sh round-trip.
"""

from __future__ import annotations

import importlib.util
import inspect
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
    from writ.server import QueryRequest  # type: ignore[import]
except ImportError:
    QueryRequest = None  # type: ignore[assignment,misc]

try:
    from writ.retrieval.pipeline import (  # type: ignore[import]
        RetrievalPipeline,
        _apply_sticky_tiebreak,
        STICKY_TIEBREAK_THRESHOLD,
    )
except ImportError:
    RetrievalPipeline = None  # type: ignore[assignment,misc]
    _apply_sticky_tiebreak = None  # type: ignore[assignment,misc]
    STICKY_TIEBREAK_THRESHOLD = 0.02

# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

SESSION_ID = "test-sticky-session"
SKILL_DIR = "/home/lucio.saldivar/.claude/skills/writ"
WRIT_SESSION_PY = f"{SKILL_DIR}/bin/lib/writ-session.py"

TIEBREAK_THRESHOLD = 0.02


def _load_writ_session():
    """Load writ-session.py as a module without installing it."""
    spec = importlib.util.spec_from_file_location("writ_session_sticky", WRIT_SESSION_PY)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_cache(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "session_id": SESSION_ID,
        "mode": "Work",
        "current_phase": "implementation",
        "remaining_budget": 5000,
        "context_percent": 40,
        "loaded_rule_ids": [],
        "loaded_rules": [],
        "loaded_rule_ids_by_phase": {"implementation": ["ARCH-ORG-001"]},
        "queries": 2,
        "pending_violations": [],
        "escalation": {"needed": False},
        "invalidation_history": {},
        "failed_writes": [],
        "is_orchestrator": False,
    }
    base.update(overrides)
    return base


def _make_scored_rules(ids_and_scores: list[tuple[str, float]]) -> list[dict[str, Any]]:
    """Return a list of minimal rule dicts with rule_id and score keys."""
    return [{"rule_id": rid, "score": score} for rid, score in ids_and_scores]


@pytest.fixture()
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture()
def mock_writ_session_sticky():
    """Mock writ_session for sticky-rules route tests."""
    mock = MagicMock()
    mock._read_cache = MagicMock(return_value=_make_cache())
    mock._write_cache = MagicMock(return_value=None)
    mock.DEFAULT_SESSION_BUDGET = 8000
    mock._can_write_check = MagicMock(return_value={"can_write": True, "reason": None})
    return mock


@pytest_asyncio.fixture()
async def client(mock_writ_session_sticky):
    transport = ASGITransport(app=app)
    with patch("writ.server.writ_session", mock_writ_session_sticky):
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


# ---------------------------------------------------------------------------
# TestLastInjectedRuleIdsDefault -- session cache schema
# ---------------------------------------------------------------------------


class TestLastInjectedRuleIdsDefault:
    """last_injected_rule_ids must be present in _read_cache defaults."""

    def setup_method(self) -> None:
        self.mod = _load_writ_session()
        self._orig_cache_dir = self.mod.CACHE_DIR
        self._tmpdir = tempfile.mkdtemp()
        self.mod.CACHE_DIR = self._tmpdir

    def teardown_method(self) -> None:
        self.mod.CACHE_DIR = self._orig_cache_dir
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_fresh_cache_has_last_injected_rule_ids_field(self) -> None:
        """_read_cache for a new session includes last_injected_rule_ids key."""
        cache = self.mod._read_cache("fresh-session")
        assert "last_injected_rule_ids" in cache

    def test_last_injected_rule_ids_default_is_empty_list(self) -> None:
        """last_injected_rule_ids defaults to [] (not None, not missing)."""
        cache = self.mod._read_cache("fresh-session")
        assert cache["last_injected_rule_ids"] == []

    def test_existing_cache_without_field_gets_setdefault_applied(self) -> None:
        """_read_cache on a cache file that predates Cycle C still returns last_injected_rule_ids: []."""
        # Write a cache file without the new field
        legacy_cache = {"loaded_rule_ids": ["ARCH-ORG-001"], "remaining_budget": 5000}
        path = self.mod._cache_path("legacy-session")
        with open(path, "w") as f:
            json.dump(legacy_cache, f)
        cache = self.mod._read_cache("legacy-session")
        assert cache["last_injected_rule_ids"] == []


# ---------------------------------------------------------------------------
# TestStickyRulesTieBreaking -- RetrievalPipeline.query() tie-break logic
# ---------------------------------------------------------------------------


class TestStickyRulesTieBreaking:
    """prefer_rule_ids tie-breaking in RetrievalPipeline.query()."""

    def test_prefer_rule_ids_parameter_exists_on_query_method(self) -> None:
        """RetrievalPipeline.query() accepts prefer_rule_ids parameter with default None."""
        assert RetrievalPipeline is not None
        sig = inspect.signature(RetrievalPipeline.query)
        assert "prefer_rule_ids" in sig.parameters, (
            "RetrievalPipeline.query() must have prefer_rule_ids parameter"
        )
        assert sig.parameters["prefer_rule_ids"].default is None, (
            "prefer_rule_ids default must be None"
        )

    def test_prefer_rule_ids_none_does_not_alter_ranking(self) -> None:
        """When prefer_rule_ids is None, the ranked order is returned unchanged."""
        rules = _make_scored_rules([("A", 0.9), ("B", 0.85), ("C", 0.80)])
        result = _apply_sticky_tiebreak(rules, None)
        assert result is rules  # Same object returned when None

    def test_prefer_rule_ids_empty_list_does_not_alter_ranking(self) -> None:
        """When prefer_rule_ids is [], the ranked order is returned unchanged."""
        rules = _make_scored_rules([("A", 0.9), ("B", 0.85), ("C", 0.80)])
        result = _apply_sticky_tiebreak(rules, [])
        assert result is rules  # Same object returned when empty

    def test_preferred_rule_promoted_when_scores_within_threshold(self) -> None:
        """Two adjacent rules within 0.02 of each other are reordered if preferred rule would come later."""
        # Scores: A=0.91, B=0.905 -- gap 0.005 (<= 0.02), B is preferred -> B takes A's position.
        rules = _make_scored_rules([("A", 0.91), ("B", 0.905)])
        result = _apply_sticky_tiebreak(rules, ["B", "A"])
        assert [r["rule_id"] for r in result] == ["B", "A"]

    def test_preferred_rule_not_promoted_when_scores_exceed_threshold(self) -> None:
        """A preferred rule with score 0.03+ below its predecessor is NOT promoted."""
        # Scores: A=0.95, B=0.90 -- gap 0.05 (> 0.02), preference for B does not override A.
        rules = _make_scored_rules([("A", 0.95), ("B", 0.90)])
        result = _apply_sticky_tiebreak(rules, ["B", "A"])
        assert [r["rule_id"] for r in result] == ["A", "B"]

    def test_preference_is_tie_breaker_only_rule_must_be_in_result_set(self) -> None:
        """A rule in prefer_rule_ids that was not returned by retrieval is never added to results."""
        rules = _make_scored_rules([("A", 0.9), ("B", 0.85)])
        result = _apply_sticky_tiebreak(rules, ["C", "A"])
        result_ids = [r["rule_id"] for r in result]
        assert "C" not in result_ids
        assert len(result) == 2

    def test_multiple_preferred_rules_maintain_relative_order_from_preference_list(self) -> None:
        """When multiple preferred rules are in the tie zone, their order matches prefer_rule_ids order."""
        # All within 0.02 of each other
        rules = _make_scored_rules([("A", 0.91), ("B", 0.905), ("C", 0.90)])
        result = _apply_sticky_tiebreak(rules, ["C", "B", "A"])
        result_ids = [r["rule_id"] for r in result]
        assert result_ids == ["C", "B", "A"]

    def test_boundary_exactly_0_02_gap_is_reordered(self) -> None:
        """A score gap of exactly 0.02 falls within the threshold and the preferred rule IS promoted."""
        rules = _make_scored_rules([("A", 0.92), ("B", 0.90)])
        result = _apply_sticky_tiebreak(rules, ["B", "A"])
        assert [r["rule_id"] for r in result] == ["B", "A"]

    def test_boundary_0_021_gap_is_not_reordered(self) -> None:
        """A score gap of 0.021 exceeds the threshold and the preferred rule is NOT promoted."""
        rules = _make_scored_rules([("A", 0.921), ("B", 0.90)])
        result = _apply_sticky_tiebreak(rules, ["B", "A"])
        assert [r["rule_id"] for r in result] == ["A", "B"]

    def test_reorder_is_stable_for_non_preferred_rules(self) -> None:
        """Rules not in prefer_rule_ids keep their original relative positions after tie-breaking."""
        rules = _make_scored_rules([("A", 0.91), ("B", 0.905), ("C", 0.90)])
        # Only B is preferred; A and C are not
        result = _apply_sticky_tiebreak(rules, ["B"])
        result_ids = [r["rule_id"] for r in result]
        # B should come first (preferred), then A and C in original order
        assert result_ids == ["B", "A", "C"]


# ---------------------------------------------------------------------------
# TestQueryRequestPreferRuleIds -- Pydantic model field
# ---------------------------------------------------------------------------


class TestQueryRequestPreferRuleIds:
    """prefer_rule_ids field on the QueryRequest Pydantic model."""

    def test_query_request_model_accepts_prefer_rule_ids_list(self) -> None:
        """QueryRequest can be constructed with prefer_rule_ids as a list of strings."""
        assert QueryRequest is not None
        req = QueryRequest(query="test", prefer_rule_ids=["ARCH-ORG-001"])
        assert req.prefer_rule_ids == ["ARCH-ORG-001"]

    def test_query_request_prefer_rule_ids_defaults_to_none(self) -> None:
        """QueryRequest constructed without prefer_rule_ids has prefer_rule_ids=None."""
        assert QueryRequest is not None
        req = QueryRequest(query="test")
        assert req.prefer_rule_ids is None

    def test_query_request_prefer_rule_ids_accepts_empty_list(self) -> None:
        """QueryRequest with prefer_rule_ids=[] is valid."""
        assert QueryRequest is not None
        req = QueryRequest(query="test", prefer_rule_ids=[])
        assert req.prefer_rule_ids == []


# ---------------------------------------------------------------------------
# TestStickyRulesCompactionClearing -- cmd_detect_compaction + cmd_reset
# ---------------------------------------------------------------------------


class TestStickyRulesCompactionClearing:
    """last_injected_rule_ids is cleared on compaction and on reset."""

    def setup_method(self) -> None:
        self.mod = _load_writ_session()
        self._orig_cache_dir = self.mod.CACHE_DIR
        self._tmpdir = tempfile.mkdtemp()
        self.mod.CACHE_DIR = self._tmpdir

    def teardown_method(self) -> None:
        self.mod.CACHE_DIR = self._orig_cache_dir
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_detect_compaction_clears_last_injected_rule_ids_on_compaction(self) -> None:
        """cmd_detect_compaction with >20% context drop sets last_injected_rule_ids to []."""
        cache = _make_cache(
            context_percent=80,
            last_injected_rule_ids=["ARCH-ORG-001", "PY-IMPORT-001"],
        )
        cache_path = self.mod._cache_path(SESSION_ID)
        with open(cache_path, "w") as f:
            json.dump(cache, f)
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.mod.cmd_detect_compaction(SESSION_ID, 30)
        updated = self.mod._read_cache(SESSION_ID)
        assert updated.get("last_injected_rule_ids") == []

    def test_detect_compaction_does_not_clear_last_injected_when_no_compaction(self) -> None:
        """cmd_detect_compaction with <=20% drop does NOT clear last_injected_rule_ids."""
        cache = _make_cache(
            context_percent=50,
            last_injected_rule_ids=["ARCH-ORG-001"],
        )
        cache_path = self.mod._cache_path(SESSION_ID)
        with open(cache_path, "w") as f:
            json.dump(cache, f)
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.mod.cmd_detect_compaction(SESSION_ID, 40)
        updated = self.mod._read_cache(SESSION_ID)
        assert updated.get("last_injected_rule_ids") == ["ARCH-ORG-001"]

    def test_cmd_reset_after_compaction_clears_last_injected_rule_ids(self) -> None:
        """cmd_reset_after_compaction sets last_injected_rule_ids to []."""
        cache = _make_cache(last_injected_rule_ids=["SEC-UNI-001"])
        cache_path = self.mod._cache_path(SESSION_ID)
        with open(cache_path, "w") as f:
            json.dump(cache, f)
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.mod.cmd_reset_after_compaction(SESSION_ID)
        updated = self.mod._read_cache(SESSION_ID)
        assert updated.get("last_injected_rule_ids") == []


# ---------------------------------------------------------------------------
# TestPreWriteCheckPassthrough -- /pre-write-check prefer_rule_ids
# ---------------------------------------------------------------------------


class TestPreWriteCheckPassthrough:
    """POST /pre-write-check passes prefer_rule_ids through to the pipeline query."""

    @pytest.mark.asyncio
    async def test_pre_write_check_accepts_prefer_rule_ids_in_request(
        self, client: AsyncClient
    ) -> None:
        """POST /pre-write-check with prefer_rule_ids in body does not return 422."""
        payload = {
            "session_id": SESSION_ID,
            "file_path": "/tmp/test.py",
            "prefer_rule_ids": ["ARCH-ORG-001"],
        }
        resp = await client.post("/pre-write-check", json=payload)
        assert resp.status_code != 422

    @pytest.mark.asyncio
    async def test_pre_write_check_without_prefer_rule_ids_is_valid(
        self, client: AsyncClient
    ) -> None:
        """POST /pre-write-check without prefer_rule_ids is still valid (field is optional)."""
        payload = {"session_id": SESSION_ID, "file_path": "/tmp/test.py"}
        resp = await client.post("/pre-write-check", json=payload)
        assert resp.status_code != 422
