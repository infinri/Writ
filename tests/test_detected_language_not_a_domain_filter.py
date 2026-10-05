"""Program item 1b: a detected project language never filters ranked retrieval.

The CwdChanged hook tagged the session with a language (php, python, javascript, rust,
go) and /prompt-bundle passed it to /query as domain=. The pipeline's Stage 1 filter
matches domain exactly and no rule domain is a language (writ/graph/schema.py
VALID_DOMAINS), so every ranked candidate was dropped. These tests pin that the ranked
request carries no domain whatever the cache holds, that an explicit /query domain still
filters, and that the dead cache field and its update flag are gone.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import writ.server as server
import writ.server.routes.query as qroute
from writ.graph.schema import VALID_DOMAINS
from writ.server.models import PromptBundleRequest, QueryRequest

from tests.conftest import writ_server_source
from tests.fixtures.server_routes import isolated_cache  # noqa: F401
from tests.fixtures.session_state import sandbox_cwd  # noqa: F401

LANGUAGES = ("php", "python", "javascript", "rust", "go")


def _cache(**overrides):
    cache = {
        "loaded_rule_ids_by_phase": {}, "current_phase": None, "loaded_rule_ids": [],
        "remaining_budget": 8000, "last_injected_rule_ids": [],
    }
    cache.update(overrides)
    return cache


async def _ranked_request(monkeypatch, cache) -> QueryRequest:
    monkeypatch.setattr(server, "_pipeline", object())
    monkeypatch.setattr(server.writ_session, "_read_cache", lambda sid: cache)
    fake_query_rules = AsyncMock(return_value={"rules": [], "mode": "standard"})
    monkeypatch.setattr(qroute, "query_rules", fake_query_rules)
    monkeypatch.setattr(qroute, "always_on_bundle",
                        AsyncMock(return_value={"rules": [], "total_tokens": 0}))
    monkeypatch.setattr(server.writ_session, "cmd_update", MagicMock())
    monkeypatch.setattr(server, "_run_cmd_format_locked", lambda payload: "")
    await qroute.prompt_bundle(PromptBundleRequest(session_id="s-1b", prompt="add a repository method"))
    fake_query_rules.assert_called_once()
    return fake_query_rules.call_args.args[0]


class TestRankedRequestCarriesNoLanguageDomain:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("language", LANGUAGES)
    async def test_a_cached_language_is_not_passed_as_domain(self, isolated_cache, monkeypatch, language):
        request = await _ranked_request(monkeypatch, _cache(detected_domain=language))
        assert request.domain is None

    @pytest.mark.asyncio
    async def test_no_language_recorded_means_no_domain(self, isolated_cache, monkeypatch):
        request = await _ranked_request(monkeypatch, _cache())
        assert request.domain is None

    def test_no_detected_language_is_a_rule_domain(self):
        assert VALID_DOMAINS.isdisjoint(LANGUAGES)

    def test_the_server_never_reads_the_language_marker(self):
        assert "detected_domain" not in writ_server_source()


class TestExplicitQueryDomainStillFilters:
    @pytest.mark.asyncio
    async def test_query_route_passes_a_caller_domain_through(self, isolated_cache, monkeypatch):
        calls: list[dict] = []

        class _Pipe:
            def query(self, **kwargs):
                calls.append(kwargs)
                return {"rules": [], "mode": "standard", "total_candidates": 0, "latency_ms": 0.0}

        monkeypatch.setattr(server, "_pipeline", _Pipe())
        await qroute.query_rules(QueryRequest(query="sql injection in a raw query", domain="security"))
        assert calls[0]["domain"] == "security"


class TestLanguageMarkerIsNoLongerCached:
    def test_update_cli_no_longer_registers_the_flag(self):
        from writ.session.budget_tracking import _UPDATE_HANDLERS

        assert "--set-detected-domain" not in _UPDATE_HANDLERS

    def test_cache_schema_no_longer_carries_the_field(self):
        from writ.session.cache import _default_cache

        assert "detected_domain" not in _default_cache()
