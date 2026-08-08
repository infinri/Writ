"""GET /session/{id}/prompt-state must answer exactly what the three routes it replaces do.

The RAG hook asked three separate questions about one session (should-skip, the full cache
read, check-escalation). Each was its own HTTP round trip AND its own read of the same
cache file. This route answers all of them from a single read.

WHY PARITY IS THE WHOLE TEST. The individual routes stay in place for other callers, so
any drift between them and this aggregate is a silent behaviour change: the hook would
make a different skip decision, or see a different cache shape, with nothing failing. The
field names are deliberately the existing ones so the two can be compared directly.

The consistency gain is worth stating too: three separate reads can straddle a concurrent
write and hand the hook a skip decision from one moment and an escalation flag from
another. One read cannot.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

try:
    from httpx import ASGITransport, AsyncClient
except ImportError:  # pragma: no cover
    pytestmark = pytest.mark.skip(reason="httpx not installed")

from writ.server import app  # type: ignore[import]

SKILL_DIR = str(Path(__file__).resolve().parent.parent)

CACHE = {
    "mode": "work",
    "loaded_rule_ids": ["A-1", "B-2"],
    "remaining_budget": 4200,
    "escalation": {"needed": True},
}


@pytest.fixture()
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture()
def session_mod():
    mock = MagicMock()
    mock._read_cache = MagicMock(return_value=dict(CACHE))
    mock._cache_path = MagicMock(return_value=f"{SKILL_DIR}/pyproject.toml")  # a real path
    mock.cmd_should_skip = MagicMock(return_value=False)
    with patch("writ.server.writ_session", mock):
        yield mock


async def _get(path: str):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


class TestPromptStateMatchesTheRoutesItReplaces:
    @pytest.mark.asyncio
    async def test_it_returns_all_three_answers(self, session_mod) -> None:
        resp = await _get("/session/s1/prompt-state")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"should_skip", "known", "escalation", "cache"}

    @pytest.mark.asyncio
    async def test_should_skip_and_known_match_the_dedicated_route(self, session_mod) -> None:
        bundle = (await _get("/session/s1/prompt-state")).json()
        single = (await _get("/session/s1/should-skip")).json()
        assert bundle["should_skip"] == single["should_skip"]
        assert bundle["known"] == single["known"]

    @pytest.mark.asyncio
    async def test_escalation_matches_the_dedicated_route(self, session_mod) -> None:
        bundle = (await _get("/session/s1/prompt-state")).json()
        single = (await _get("/session/s1/check-escalation")).json()
        assert bundle["escalation"] == single["escalation"] is True

    @pytest.mark.asyncio
    async def test_cache_matches_the_dedicated_route(self, session_mod) -> None:
        bundle = (await _get("/session/s1/prompt-state")).json()
        single = (await _get("/session/s1")).json()
        assert bundle["cache"] == single

    @pytest.mark.asyncio
    async def test_it_delegates_the_skip_policy_rather_than_reimplementing_it(
        self, session_mod
    ) -> None:
        """cmd_should_skip carries the is_subagent never-skip exemption. Re-deriving the
        rule here from cache fields would drop that exemption silently, which is the bug
        the dedicated route's docstring says it was written to fix."""
        session_mod.cmd_should_skip.return_value = True
        assert (await _get("/session/s1/prompt-state")).json()["should_skip"] is True
        session_mod.cmd_should_skip.assert_called()

    @pytest.mark.asyncio
    async def test_one_cache_read_serves_the_whole_response(self, session_mod) -> None:
        """The point of the route. Three reads could straddle a concurrent write; the
        aggregate must not reintroduce that by reading per field."""
        session_mod._read_cache.reset_mock()
        await _get("/session/s1/prompt-state")
        assert session_mod._read_cache.call_count == 1, (
            f"read the cache {session_mod._read_cache.call_count} times; the route exists "
            f"to read it once"
        )


class TestUnknownSessions:
    @pytest.mark.asyncio
    async def test_known_is_false_when_no_cache_file_exists(self) -> None:
        """_read_cache returns a defaults scaffold for unknown sessions, so file existence
        is the recognition signal. `known: false` tells the hook the boolean is a default
        rather than an answer, which is what makes its local fallback correct."""
        mock = MagicMock()
        mock._read_cache = MagicMock(return_value={})
        mock._cache_path = MagicMock(return_value="/nonexistent/writ/session.json")
        mock.cmd_should_skip = MagicMock(return_value=False)
        with patch("writ.server.writ_session", mock):
            body = (await _get("/session/ghost/prompt-state")).json()
        assert body["known"] is False
        assert body["escalation"] is False

    @pytest.mark.asyncio
    async def test_a_non_dict_escalation_does_not_raise(self) -> None:
        """The cache is a JSON file on disk; a corrupted or hand-edited `escalation` that
        is not an object must not turn a telemetry read into a 500."""
        mock = MagicMock()
        mock._read_cache = MagicMock(return_value={"escalation": "broken"})
        mock._cache_path = MagicMock(return_value="/nonexistent/writ/session.json")
        mock.cmd_should_skip = MagicMock(return_value=False)
        with patch("writ.server.writ_session", mock):
            resp = await _get("/session/weird/prompt-state")
        assert resp.status_code == 200
        assert resp.json()["escalation"] is False
