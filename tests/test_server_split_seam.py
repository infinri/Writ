"""Regression guard for the writ/server.py -> writ/server/ package split
(Wave 2, Cycle 1, branch refactor/w2-server-split).

Per TEST-TDD-001 / PBK-PROC-TDD-001: this skeleton is authored BEFORE the split
lands, so several assertions here are deliberately RED against the current
single-file `writ/server.py` and are expected to go GREEN once the module
becomes a package (`writ/server/__init__.py` + `routes/*.py` + `models.py`).
Each test's docstring says which state (RED-now / PASS-now) it is in and why.

Do NOT implement the split from this file. This file only encodes the target
contract: a permanent regression guard the split must satisfy, and stays in
the suite afterward to catch future drift (a router quietly re-freezing an
import, a route getting dropped/re-prefixed, etc).
"""
from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from writ.server import app

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_PKG_DIR = REPO_ROOT / "writ" / "server"

# Six per-domain router modules the plan requires under writ/server/routes/.
DOMAIN_ROUTER_MODULES = (
    "query",
    "session_state",
    "gate",
    "decision_memory",
    "git_hooks",
    "explorer",
)

# ---------------------------------------------------------------------------
# Frozen route baseline -- captured from HEAD (pre-split, single-file
# writ/server.py) via:
#
#   .venv/bin/python -c "
#   import writ.server, json
#   routes = sorted(
#       (m, r.path)
#       for r in writ.server.app.routes
#       for m in (getattr(r, 'methods', None) or [''])
#   )
#   print(json.dumps(routes))
#   print(len(routes))
#   "
#
# 54 total (method, path) tuples registered on `app.routes`: 46 are the
# plan's hand-authored `@app.<verb>` routes (query/session/gate/decision-memory/
# git-hooks/explorer domains); the remaining 8 are FastAPI's always-on
# docs/openapi/redoc routes (GET+HEAD x4: /docs, /docs/oauth2-redirect,
# /openapi.json, /redoc) that every `FastAPI(...)` instance registers
# automatically and are unaffected by the split. This is the CONTRACT the
# split must not break: no route added, dropped, or re-prefixed.
#
# The baseline is a frozen SET, not a ceiling: a genuinely new feature route is
# added here deliberately, in the same commit that adds it to the app, so the
# guard keeps catching accidental drops and re-prefixes. Added since the split:
# POST /memory-record (the auto-memory graph mirror; 53 -> 54).
# ---------------------------------------------------------------------------
ROUTE_BASELINE: list[tuple[str, str]] = [
    ("GET", "/always-on"),
    ("GET", "/dashboard"),
    ("GET", "/docs"),
    ("GET", "/docs/oauth2-redirect"),
    ("GET", "/explore"),
    ("GET", "/graph"),
    ("GET", "/health"),
    ("GET", "/node/{node_id}"),
    ("GET", "/openapi.json"),
    ("GET", "/redoc"),
    ("GET", "/rule/{rule_id}"),
    ("GET", "/session/{session_id}"),
    ("GET", "/session/{session_id}/active-playbook"),
    ("GET", "/session/{session_id}/check-escalation"),
    ("GET", "/session/{session_id}/coverage"),
    ("GET", "/session/{session_id}/current-phase"),
    ("GET", "/session/{session_id}/mode"),
    ("GET", "/session/{session_id}/pending-violations"),
    ("GET", "/session/{session_id}/quality-judgment"),
    ("GET", "/session/{session_id}/review-findings"),
    ("GET", "/session/{session_id}/should-skip"),
    ("GET", "/session/{session_id}/verification-evidence"),
    ("GET", "/subagent-role/{name}"),
    ("HEAD", "/docs"),
    ("HEAD", "/docs/oauth2-redirect"),
    ("HEAD", "/openapi.json"),
    ("HEAD", "/redoc"),
    ("POST", "/analyze"),
    ("POST", "/commit/capture"),
    ("POST", "/conflicts"),
    ("POST", "/feedback"),
    ("POST", "/git-hooks/auto-install"),
    ("POST", "/memory-record"),
    ("POST", "/methodology-companion"),
    ("POST", "/pre-write-check"),
    ("POST", "/prompt-bundle"),
    ("POST", "/propose"),
    ("POST", "/query"),
    ("POST", "/recall"),
    ("POST", "/session/format"),
    ("POST", "/session/{session_id}/active-playbook"),
    ("POST", "/session/{session_id}/add-pending-violation"),
    ("POST", "/session/{session_id}/advance-phase"),
    ("POST", "/session/{session_id}/auto-feedback"),
    ("POST", "/session/{session_id}/can-write"),
    ("POST", "/session/{session_id}/clear-pending-violations"),
    ("POST", "/session/{session_id}/clear-rules-for-compaction"),
    ("POST", "/session/{session_id}/context-percent"),
    ("POST", "/session/{session_id}/invalidate-gate"),
    ("POST", "/session/{session_id}/mode"),
    ("POST", "/session/{session_id}/promote-candidate"),
    ("POST", "/session/{session_id}/quality-judgment"),
    ("POST", "/session/{session_id}/review-findings"),
    ("POST", "/session/{session_id}/reset-after-compaction"),
    ("POST", "/session/{session_id}/update"),
    ("POST", "/session/{session_id}/verification-evidence"),
]


def _current_route_tuples() -> list[tuple[str, str]]:
    """Recompute the live (method, path) set from `writ.server.app.routes`,
    the same way the frozen baseline above was captured."""
    return sorted(
        (m, r.path)
        for r in app.routes
        for m in (getattr(r, "methods", None) or [""])
    )


# ---------------------------------------------------------------------------
# Structural contract: writ.server becomes a package with domain routers.
# ---------------------------------------------------------------------------


class TestServerIsPackage:
    def test_route_baseline_captured_count(self) -> None:
        """Sanity check on the frozen constant itself: exactly 56 tuples are
        declared (53 captured from HEAD, plus /memory-record, plus the GET and POST
        halves of /session/{sid}/review-findings added 2026-08-06). Guards against
        a copy/paste mistake in ROUTE_BASELINE, independent of the split."""
        assert len(ROUTE_BASELINE) == 56
        assert len(set(ROUTE_BASELINE)) == 56, "ROUTE_BASELINE must have no duplicate tuples"

    def test_writ_server_is_package(self) -> None:
        """RED now: `writ.server` is still the single-file writ/server.py module
        (no `__path__`). GREEN once writ/server.py is `git mv`'d to
        writ/server/__init__.py, turning `writ.server` into a package."""
        import writ.server as server_module

        assert hasattr(server_module, "__path__"), (
            "writ.server must be a package (writ/server/__init__.py) after the "
            "W2 split; it currently resolves to the single writ/server.py module"
        )

    def test_routes_directory_exists(self) -> None:
        """RED now: writ/server/routes/ does not exist yet (server.py is a file,
        not a directory)."""
        routes_dir = SERVER_PKG_DIR / "routes"
        assert routes_dir.is_dir(), f"expected {routes_dir} to exist post-split"

    @pytest.mark.parametrize("domain_module", DOMAIN_ROUTER_MODULES)
    def test_domain_router_module_importable(self, domain_module: str) -> None:
        """RED now: writ/server/routes/<domain>.py does not exist for any of the
        six domains yet. GREEN once each router module is created and
        importable as writ.server.routes.<domain>."""
        module_path = SERVER_PKG_DIR / "routes" / f"{domain_module}.py"
        assert module_path.exists(), f"missing writ/server/routes/{domain_module}.py"

        try:
            mod = importlib.import_module(f"writ.server.routes.{domain_module}")
        except ModuleNotFoundError as exc:
            pytest.fail(f"writ.server.routes.{domain_module} not importable: {exc}")
        assert mod is not None


# ---------------------------------------------------------------------------
# Permanent contract guard: facade re-exports resolve as attributes of
# writ.server. This is a PASS-now test (every name already lives in the
# single-file module) that must keep passing after the split -- it is the
# regression net for the facade's re-export list in __init__.py.
# ---------------------------------------------------------------------------


class TestMustSurviveNamesImportable:
    def test_handler_and_app_names_importable(self) -> None:
        """PASS now (names already resolve in the single-file module); must
        remain importable post-split via __init__.py's facade re-exports."""
        from writ.server import (  # noqa: F401
            app,
            session_advance_phase,
            methodology_companion,
            pre_write_check,
            _health_status,
            writ_session,
            INJECTION_RULE_WHERE,
        )

    def test_pydantic_model_names_importable(self) -> None:
        """PASS now; must remain importable post-split via models.py + the
        __init__.py re-export of writ.server.models."""
        from writ.server import (  # noqa: F401
            QueryRequest,
            CompanionRequest,
            PreWriteCheckRequest,
            SessionAdvancePhaseRequest,
            SessionModeSetRequest,
            SessionCanWriteRequest,
            SessionFormatRequest,
            SessionAutoFeedbackRequest,
            SessionAddViolationRequest,
        )

    def test_app_is_fastapi_instance(self) -> None:
        """PASS now and post-split: writ.server.app is the live FastAPI app
        used by cli.py:572 (`from writ.server import app`) to boot the daemon."""
        from fastapi import FastAPI

        assert isinstance(app, FastAPI)


# ---------------------------------------------------------------------------
# Route parity vs the frozen HEAD baseline.
# ---------------------------------------------------------------------------


class TestRouteParityVsBaseline:
    def test_route_set_matches_baseline_exactly(self) -> None:
        """PASS now (current tree IS the baseline); locks parity through the
        split. If the implementer drops a route, adds one, or re-prefixes a
        path (e.g. moving /health under a router without preserving the bare
        path), this goes RED."""
        current = _current_route_tuples()
        baseline = sorted(ROUTE_BASELINE)

        missing = set(baseline) - set(current)
        added = set(current) - set(baseline)
        assert not missing, f"routes present in the HEAD baseline but missing now: {sorted(missing)}"
        assert not added, f"routes present now but absent from the HEAD baseline: {sorted(added)}"
        assert current == baseline, "route (method, path) set diverges from the frozen HEAD baseline"

    def test_route_count_matches_baseline(self) -> None:
        """PASS now; a duplicate or dropped route changes the count even if
        set membership alone were checked loosely elsewhere."""
        assert len(_current_route_tuples()) == len(ROUTE_BASELINE) == 56


# ---------------------------------------------------------------------------
# THE MONKEYPATCH SEAM RULE (plan's #1 hazard): router handlers must read
# mutable/monkeypatched module state via LIVE attribute access on
# `writ.server` (`server._db`, `server.writ_session`, ...), never via a
# frozen `from writ.server import _db` at router module top. A frozen import
# would make the lifespan reassignment AND every test monkeypatch silently
# invisible to the handler.
#
# These two tests PASS today (the handler code lives directly inside
# writ/server.py, so `patch("writ.server._db", ...)` trivially reaches it).
# Their real job starts post-split: if a router does
# `from writ.server import _db` at module top instead of `import writ.server
# as server` + `server._db` at call time, these tests go RED because the
# patched sentinel is invisible to the router's frozen snapshot.
# ---------------------------------------------------------------------------


class TestMonkeypatchSeamDb:
    def test_patched_db_is_observed_by_get_rule_route(self) -> None:
        """GET /rule/{rule_id} calls `_db.get_rule(rule_id)`. Patching
        writ.server._db with a mock whose get_rule returns a sentinel body
        must flow through to the HTTP response -- proof the handler reads
        `server._db` live rather than a module-load-time snapshot."""
        sentinel_rule = {"rule_id": "SEAM-SENTINEL-001", "statement": "seam-sentinel-value"}
        mock_db = MagicMock()
        mock_db.get_rule = AsyncMock(return_value=sentinel_rule)

        with patch("writ.server._db", mock_db):
            client = TestClient(app)
            response = client.get("/rule/SEAM-SENTINEL-001")

        assert response.status_code == 200
        body = response.json()
        assert body.get("rule") == sentinel_rule, (
            f"expected the patched _db sentinel to be reflected in the response; got {body!r}"
        )
        mock_db.get_rule.assert_awaited_once_with("SEAM-SENTINEL-001")

    def test_db_none_short_circuits_without_touching_a_stale_db(self) -> None:
        """Reverse check: patching _db to None must also be observed live (the
        route's `if _db is None` guard fires), not a stale non-None object
        captured at import time."""
        with patch("writ.server._db", None):
            client = TestClient(app)
            response = client.get("/rule/anything")

        assert response.status_code == 200
        assert response.json() == {"error": "Database not connected."}


class TestMonkeypatchSeamWritSession:
    def test_patched_writ_session_is_observed_by_session_read_route(self) -> None:
        """GET /session/{session_id} calls
        `asyncio.to_thread(writ_session._read_cache, session_id)`. Patching
        writ.server.writ_session with a mock whose _read_cache returns a
        sentinel dict must flow through to the HTTP response -- proof the
        handler reads `server.writ_session` live."""
        mock_writ_session = MagicMock()
        mock_writ_session._read_cache = MagicMock(
            return_value={"marker": "seam-sentinel-value"}
        )

        with patch("writ.server.writ_session", mock_writ_session):
            client = TestClient(app)
            response = client.get("/session/SEAM-SESSION-001")

        assert response.status_code == 200
        body = response.json()
        assert body.get("marker") == "seam-sentinel-value", (
            f"expected the patched writ_session sentinel to be reflected in the response; got {body!r}"
        )
        mock_writ_session._read_cache.assert_called_once_with("SEAM-SESSION-001")
