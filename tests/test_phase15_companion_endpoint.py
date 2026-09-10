"""1.5: the /methodology-companion endpoint (CHANNEL 2).

Wraps the 1.6 trigger index and reshapes its match into /query's response shape
so the shared cmd_format renders it (summary form). BUILT-NOT-WIRED per D5: the
hook keeps the legacy /query path until the 1.7 cutover, so on the current corpus
(no floors/keywords authored yet) the endpoint returns an empty bundle -- which is
itself the assertion that nothing injects before authoring.

The shaping/wiring tests call the endpoint coroutine directly with an in-memory
index (the index is built at startup, so seeding the live graph wouldn't reach it
without a restart); one live smoke test confirms the endpoint is reachable and
empty pre-authoring.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from writ import server
from writ.server import CompanionRequest, methodology_companion
from writ.retrieval.trigger_index import MethodologyTriggerIndex


def _node(node_id, *, floor_modes=None, trigger_keywords=None,
          trigger="when x", statement="do y", domain="process") -> dict:
    return {
        "id": node_id, "node_type": "Skill",
        "floor_modes": floor_modes or [], "action_triggers": [],
        "trigger_keywords": trigger_keywords or [],
        "trigger": trigger, "statement": statement, "severity": "high", "domain": domain,
    }


class TestCompanionShaping:
    @pytest.mark.asyncio
    async def test_returns_query_shape_for_cmd_format(self, monkeypatch) -> None:
        idx = MethodologyTriggerIndex([_node("SKL-FLOOR-001", floor_modes=["work"])])
        monkeypatch.setattr(server, "_trigger_index", idx)
        resp = await methodology_companion(CompanionRequest(mode="work", prompt=""))
        assert resp["mode"] == "summary"  # cmd_format renders trigger+statement only
        assert resp["total_candidates"] == 1
        assert {r["rule_id"] for r in resp["rules"]} == {"SKL-FLOOR-001"}
        r0 = resp["rules"][0]
        # the keys cmd_format .get()s
        for k in ("rule_id", "trigger", "statement", "severity", "authority", "domain", "score"):
            assert k in r0
        assert r0["channel"] == "floor"

    @pytest.mark.asyncio
    async def test_pull_then_exclude(self, monkeypatch) -> None:
        idx = MethodologyTriggerIndex([_node("SKL-PULL-001", trigger_keywords=["worktree"])])
        monkeypatch.setattr(server, "_trigger_index", idx)
        hit = await methodology_companion(
            CompanionRequest(mode="work", prompt="please fix the worktree")
        )
        assert {r["rule_id"] for r in hit["rules"]} == {"SKL-PULL-001"}
        assert hit["rules"][0]["channel"] == "pull"
        # exclude_rule_ids drops an already-injected node (no re-inject, no budget spend)
        excluded = await methodology_companion(
            CompanionRequest(
                mode="work", prompt="please fix the worktree",
                exclude_rule_ids=["SKL-PULL-001"],
            )
        )
        assert excluded["rules"] == []

    @pytest.mark.asyncio
    async def test_uninitialized_index_guarded(self, monkeypatch) -> None:
        monkeypatch.setattr(server, "_trigger_index", None)
        resp = await methodology_companion(CompanionRequest(mode="work"))
        assert "error" in resp


class TestCompanionLive:
    """ROUTE (Decision 4, plan.md 2412ba38-51e1-4b73-895b-7b240a3c21d3): the
    `except (URLError, OSError) -> pytest.skip` this class used to carry is
    gone. /methodology-companion is driven over its own URL in process, with a
    real MethodologyTriggerIndex installed exactly the way
    TestCompanionShaping installs one -- no daemon involved, because the
    endpoint reads only `server._trigger_index`.
    """

    async def _post(self, monkeypatch, idx: MethodologyTriggerIndex, mode: str, prompt: str) -> dict:
        monkeypatch.setattr(server, "_trigger_index", idx)
        async with AsyncClient(
            transport=ASGITransport(app=server.app), base_url="http://test"
        ) as ac:
            resp = await ac.post("/methodology-companion", json={"mode": mode, "prompt": prompt})
        # Asserted FIRST, for the reason server_routes.py:200-214 records: an
        # error payload has no `rules` key and would satisfy every shape
        # assertion below just as well.
        assert resp.status_code == 200, (
            f"POST /methodology-companion returned {resp.status_code}, not 200: "
            f"{resp.text}. MUTATION: dropping the router registration for this "
            f"path, or renaming it, produces exactly this 404 -- the regression "
            f"class the module's four in-process TestCompanionShaping tests are "
            f"structurally blind to, since they call the coroutine directly."
        )
        data = resp.json()
        assert "error" not in data, (
            f"POST /methodology-companion returned the error sentinel instead of "
            f"a bundle: {data}"
        )
        return data

    @pytest.mark.asyncio
    async def test_reachable_with_a_matching_floor_node(self, monkeypatch) -> None:
        idx = MethodologyTriggerIndex([_node("SKL-FLOOR-LIVE-001", floor_modes=["work"])])
        data = await self._post(monkeypatch, idx, "work", "")
        assert {r["rule_id"] for r in data["rules"]} == {"SKL-FLOOR-LIVE-001"}
        assert isinstance(data.get("total_tokens"), int) and "over_budget" in data
        # `mode` is a POLARITY property in principle (decision 5, module 1), but
        # methodology_companion hardcodes `"mode": "summary"` regardless of
        # branch (writ/server/routes/query.py:191-198): there is no second value
        # this handler can ever return, so the fallback the plan itself states
        # applies -- mode is asserted as one of the endpoint's declared values,
        # named against the input (a floor-sized index of 1 node) that produced it.
        assert data["mode"] in {"summary"}, (
            f"mode must be one of the endpoint's declared values for a "
            f"floor-matching index; got {data['mode']!r}"
        )

    @pytest.mark.asyncio
    async def test_reachable_with_no_matching_node(self, monkeypatch) -> None:
        idx = MethodologyTriggerIndex([_node("SKL-FLOOR-LIVE-002", floor_modes=["debug"])])
        data = await self._post(monkeypatch, idx, "work", "")
        assert data["rules"] == []
        assert data["mode"] in {"summary"}, (
            f"mode must be one of the endpoint's declared values for a "
            f"non-matching index; got {data['mode']!r}"
        )
