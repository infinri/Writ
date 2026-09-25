"""A mode's methodology floor reaches the prompt on every turn, not once per session.

/prompt-bundle used to hand the companion the SESSION's loaded_rule_ids as its
exclude list. The index reads exclude_ids as "already injected this turn", so a
floor node delivered on turn one was filtered out of every later turn: an
investigate session (no phases, so no per-phase reset) saw PBK-PROC-AUDIT-FANOUT-001
once and never again. Floors now bypass the session exclude, cost the rule budget
nothing (like the always-on channel), and survive a drained budget; pull keeps
its session dedup and its budget.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import writ.server as server
import writ.server.routes.query as qroute
from writ.retrieval.trigger_index import MethodologyTriggerIndex
from writ.server.models import PromptBundleRequest

from tests.fixtures.session_state import sandbox_cwd  # noqa: F401


def _node(node_id, *, floor_modes=None, trigger_keywords=None) -> dict:
    return {
        "id": node_id, "node_type": "Playbook",
        "floor_modes": floor_modes or [], "action_triggers": [],
        "trigger_keywords": trigger_keywords or [],
        "trigger": f"when {node_id}", "statement": f"do {node_id}",
        "severity": "high", "domain": "process",
    }


def _run(monkeypatch, *, loaded, remaining_budget=8000):
    idx = MethodologyTriggerIndex([
        _node("PBK-FLOOR-001", floor_modes=["investigate"]),
        _node("SKL-PULL-001", trigger_keywords=["audit"]),
    ])
    cache = {
        "loaded_rule_ids_by_phase": {}, "current_phase": "",
        "loaded_rule_ids": loaded, "remaining_budget": remaining_budget,
        "last_injected_rule_ids": [], "detected_domain": "",
    }
    cmd_update = MagicMock()
    monkeypatch.setattr(server, "_pipeline", object())
    monkeypatch.setattr(server, "_trigger_index", idx)
    monkeypatch.setattr(server.writ_session, "_read_cache", lambda sid: cache)
    monkeypatch.setattr(server.writ_session, "cmd_update", cmd_update)
    monkeypatch.setattr(qroute, "always_on_bundle",
                        AsyncMock(return_value={"rules": [], "total_tokens": 0}))
    return cmd_update


async def _bundle():
    return await qroute.prompt_bundle(PromptBundleRequest(
        session_id="s1", prompt="audit the checkout", mode="investigate",
        include_ranked=False,
    ))


def _companion_cost(cmd_update) -> int:
    for call in cmd_update.call_args_list:
        args = call.args[1]
        if "--inc-queries" in args and "--cost" in args:
            return int(args[args.index("--cost") + 1])
    raise AssertionError("companion never updated the cache")


class TestFloorEveryTurn:
    @pytest.mark.asyncio
    async def test_floor_already_loaded_this_session_is_injected_again(self, monkeypatch):
        _run(monkeypatch, loaded=["PBK-FLOOR-001", "SKL-PULL-001"])
        out = await _bundle()
        assert "PBK-FLOOR-001" in out["methodology_block"]
        assert out["method_meta"]["rule_ids"] == ["PBK-FLOOR-001"]

    @pytest.mark.asyncio
    async def test_pull_already_loaded_this_session_is_not_repeated(self, monkeypatch):
        _run(monkeypatch, loaded=["SKL-PULL-001"])
        out = await _bundle()
        assert "SKL-PULL-001" not in out["methodology_block"]

    @pytest.mark.asyncio
    async def test_floor_costs_no_rule_budget(self, monkeypatch):
        cmd_update = _run(monkeypatch, loaded=["SKL-PULL-001"])
        await _bundle()
        assert _companion_cost(cmd_update) == 0

    @pytest.mark.asyncio
    async def test_pull_still_costs_rule_budget(self, monkeypatch):
        cmd_update = _run(monkeypatch, loaded=[])
        out = await _bundle()
        assert set(out["method_meta"]["rule_ids"]) == {"PBK-FLOOR-001", "SKL-PULL-001"}
        assert _companion_cost(cmd_update) > 0

    @pytest.mark.asyncio
    async def test_drained_budget_still_delivers_floor_but_not_pull(self, monkeypatch):
        _run(monkeypatch, loaded=[], remaining_budget=100)
        out = await _bundle()
        assert out["method_meta"]["rule_ids"] == ["PBK-FLOOR-001"]
