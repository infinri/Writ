"""Program item 1a: full versus pointer rendering of the repeating injection text.

The always-on rules and a mode's floor methodology rules repeat on every turn. They render
IN FULL once per epoch and as ONE pointer line per rule (`[ID] WHEN: trigger`) after that.
An epoch is the pair (compaction_epoch, current_phase): compaction bumps the counter and any
phase write changes the phase, so both boundaries reset the record. Outside work mode
current_phase stays None and only the counter moves, which is the case item 1c's by_phase
reset gets wrong, so the compaction tests below run in BOTH a work session and an
investigate session.

Classes:

  * TestInjectionState        the pure epoch / shown-ids / mark-shown helpers
  * TestMarkShownFlag         `cmd_update --mark-shown` through a REAL cache
  * TestCompactionEpoch       cmd_reset_after_compaction bumps the counter in every mode
  * TestShouldSkipCache       the pure skip rule extracted from cmd_should_skip
  * TestCollapseAcrossTurns   the route, several turns, a REAL cache (only retrieval is faked)
  * TestConcurrentSections    no cache write is lost when sections run concurrently

WHY REAL CACHES, NOT A MOCKED cmd_update. The claim "no write is lost when four parallel
section requests write one session cache" cannot be proven with a mock that returns whatever
it was told to (a mock proves the call happened, not that the file survived it). The
concurrency tests run the real `cmd_update` -> `mutate_cache` (flock) against a real cache
directory, from concurrent threads (four `prompt_bundle` calls gathered on one loop, each
writing through `asyncio.to_thread`) and from separate OS processes.

New modules are imported inside each test (`_imp`), so a missing module fails each test on
its own rather than turning the file into one collection error.

Run with `WRIT_TEST_NO_ISOLATION=1 python3 -m pytest tests/test_injection_collapse.py` when
no isolated graph is configured; nothing here touches the graph.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import writ.server as server
import writ.server.routes.query as qroute
from writ.server.models import PromptBundleRequest

REPO = Path(__file__).resolve().parent.parent
SESSION_CLI = REPO / "bin" / "lib" / "writ-session.py"


def _imp(name: str):
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    return importlib.import_module(name)


@pytest.fixture()
def cache_dir(tmp_path, monkeypatch) -> Path:
    """A throwaway session-cache directory; the cache module resolves it at call time."""
    path = tmp_path / "cache"
    path.mkdir()
    monkeypatch.setenv("WRIT_CACHE_DIR", str(path))
    return path


def _read(sid: str) -> dict:
    return _imp("writ.session.cache")._read_cache(sid)


def _seed(sid: str, **fields) -> None:
    cache = _imp("writ.session.cache")
    with cache.mutate_cache(sid) as data:
        data.update(fields)


def _update(sid: str, *args: str) -> None:
    _imp("writ.session.budget_tracking").cmd_update(sid, list(args))


# --------------------------------------------------------------------------- #
# Task 2. Collapse state
# --------------------------------------------------------------------------- #
class TestInjectionState:
    def test_epoch_is_the_counter_and_the_phase_joined_by_a_pipe(self):
        st = _imp("writ.session.injection_state")
        assert st.injection_epoch({"compaction_epoch": 2, "current_phase": "planning"}) == "2|planning"

    def test_epoch_of_an_empty_cache_is_zero_and_no_phase(self):
        st = _imp("writ.session.injection_state")
        assert st.injection_epoch({}) == "0|"

    def test_a_none_phase_contributes_an_empty_string(self):
        st = _imp("writ.session.injection_state")
        assert st.injection_epoch({"compaction_epoch": 3, "current_phase": None}) == "3|"

    def test_an_unparseable_counter_is_treated_as_zero(self):
        st = _imp("writ.session.injection_state")
        assert st.injection_epoch({"compaction_epoch": "banana", "current_phase": "x"}) == "0|x"

    def test_a_phase_change_changes_the_epoch(self):
        st = _imp("writ.session.injection_state")
        assert st.injection_epoch({"current_phase": "planning"}) != st.injection_epoch({"current_phase": "implementation"})

    def test_a_counter_bump_changes_the_epoch_even_with_no_phase(self):
        st = _imp("writ.session.injection_state")
        assert st.injection_epoch({"compaction_epoch": 0}) != st.injection_epoch({"compaction_epoch": 1})

    def test_shown_ids_is_empty_without_a_record(self):
        st = _imp("writ.session.injection_state")
        assert st.shown_ids({}, "always_on") == set()
        assert st.shown_ids({"injection_shown": {}}, "always_on") == set()

    def test_shown_ids_returns_the_ids_of_the_current_epoch(self):
        st = _imp("writ.session.injection_state")
        cache = {"compaction_epoch": 1, "current_phase": "p",
                 "injection_shown": {"epoch": "1|p", "always_on": ["A", "B"], "floor": ["F"]}}
        assert st.shown_ids(cache, "always_on") == {"A", "B"}
        assert st.shown_ids(cache, "floor") == {"F"}

    def test_shown_ids_is_empty_for_a_record_of_another_epoch(self):
        st = _imp("writ.session.injection_state")
        cache = {"compaction_epoch": 2, "current_phase": "p",
                 "injection_shown": {"epoch": "1|p", "always_on": ["A"]}}
        assert st.shown_ids(cache, "always_on") == set()

    def test_shown_ids_tolerates_a_malformed_record(self):
        st = _imp("writ.session.injection_state")
        assert st.shown_ids({"injection_shown": "garbage"}, "always_on") == set()
        assert st.shown_ids({"injection_shown": None}, "floor") == set()

    def test_apply_mark_shown_records_ids_under_the_current_epoch(self):
        st = _imp("writ.session.injection_state")
        cache: dict = {}
        st.apply_mark_shown(cache, "always_on", "0|", ["B", "A"])
        assert cache["injection_shown"] == {"epoch": "0|", "always_on": ["A", "B"]}

    def test_apply_mark_shown_unions_across_calls_and_sections(self):
        st = _imp("writ.session.injection_state")
        cache: dict = {}
        st.apply_mark_shown(cache, "always_on", "0|", ["A"])
        st.apply_mark_shown(cache, "always_on", "0|", ["B", "A"])
        st.apply_mark_shown(cache, "floor", "0|", ["F"])
        assert cache["injection_shown"] == {"epoch": "0|", "always_on": ["A", "B"], "floor": ["F"]}

    def test_apply_mark_shown_drops_a_write_computed_under_a_stale_epoch(self):
        st = _imp("writ.session.injection_state")
        cache = {"compaction_epoch": 1, "current_phase": "", "injection_shown": {}}
        st.apply_mark_shown(cache, "always_on", "0|", ["A"])
        assert cache["injection_shown"] == {}
        assert st.shown_ids(cache, "always_on") == set()

    def test_apply_mark_shown_replaces_a_record_left_by_an_older_epoch(self):
        st = _imp("writ.session.injection_state")
        cache = {"compaction_epoch": 1, "injection_shown": {"epoch": "0|", "always_on": ["OLD"]}}
        st.apply_mark_shown(cache, "always_on", "1|", ["NEW"])
        assert cache["injection_shown"] == {"epoch": "1|", "always_on": ["NEW"]}

    def test_apply_mark_shown_ignores_an_unknown_section(self):
        st = _imp("writ.session.injection_state")
        cache: dict = {}
        st.apply_mark_shown(cache, "ranked", "0|", ["A"])
        assert "injection_shown" not in cache or cache["injection_shown"] == {}

    def test_apply_mark_shown_skips_empty_ids(self):
        st = _imp("writ.session.injection_state")
        cache: dict = {}
        st.apply_mark_shown(cache, "always_on", "0|", ["", None, "A"])
        assert cache["injection_shown"]["always_on"] == ["A"]

    def test_only_always_on_and_floor_are_collapsible(self):
        st = _imp("writ.session.injection_state")
        assert tuple(st.COLLAPSIBLE_SECTIONS) == ("always_on", "floor")

    def test_the_default_cache_carries_the_collapse_record(self):
        cache = _imp("writ.session.cache")._default_cache()
        assert cache["compaction_epoch"] == 0
        assert cache["injection_shown"] == {}


class TestMarkShownFlag:
    def test_mark_shown_writes_the_record_through_a_real_cache(self, cache_dir):
        _update("ms-1", "--mark-shown", "always_on", "0|", '["A","B"]')
        assert _read("ms-1")["injection_shown"] == {"epoch": "0|", "always_on": ["A", "B"]}

    def test_a_second_write_unions_with_the_first(self, cache_dir):
        _update("ms-2", "--mark-shown", "always_on", "0|", '["A"]')
        _update("ms-2", "--mark-shown", "always_on", "0|", '["B"]')
        _update("ms-2", "--mark-shown", "floor", "0|", '["F"]')
        record = _read("ms-2")["injection_shown"]
        assert record["always_on"] == ["A", "B"] and record["floor"] == ["F"]

    def test_malformed_json_is_ignored_and_later_flags_still_apply(self, cache_dir):
        _update("ms-3", "--mark-shown", "always_on", "0|", "{not json", "--inc-queries")
        cache = _read("ms-3")
        assert cache["injection_shown"] == {}
        assert cache["queries"] == 1

    def test_a_non_list_payload_is_ignored(self, cache_dir):
        _update("ms-4", "--mark-shown", "always_on", "0|", '{"A": 1}')
        assert _read("ms-4")["injection_shown"] == {}

    def test_a_write_under_a_stale_epoch_is_dropped(self, cache_dir):
        _seed("ms-5", compaction_epoch=2)
        _update("ms-5", "--mark-shown", "always_on", "1|", '["A"]')
        assert _read("ms-5")["injection_shown"] == {}

    def test_a_write_under_the_current_phase_epoch_lands(self, cache_dir):
        _seed("ms-6", compaction_epoch=1, current_phase="planning")
        _update("ms-6", "--mark-shown", "floor", "1|planning", '["F"]')
        assert _read("ms-6")["injection_shown"] == {"epoch": "1|planning", "floor": ["F"]}

    def test_the_flag_is_registered_with_three_arguments(self):
        bt = _imp("writ.session.budget_tracking")
        handler, arity = bt._UPDATE_HANDLERS["--mark-shown"]
        assert arity == 3


class TestCompactionEpoch:
    def test_compaction_bumps_the_counter_in_a_work_session(self, cache_dir, capsys):
        sl = _imp("writ.session.session_lifecycle")
        _seed("ce-work", mode="work", current_phase="planning")
        sl.cmd_reset_after_compaction("ce-work")
        assert _read("ce-work")["compaction_epoch"] == 1
        sl.cmd_reset_after_compaction("ce-work")
        assert _read("ce-work")["compaction_epoch"] == 2

    def test_compaction_bumps_the_counter_in_an_investigate_session_with_no_phase(self, cache_dir, capsys):
        sl = _imp("writ.session.session_lifecycle")
        _seed("ce-inv", mode="investigate", current_phase=None)
        sl.cmd_reset_after_compaction("ce-inv")
        cache = _read("ce-inv")
        assert cache["compaction_epoch"] == 1
        assert cache["current_phase"] is None

    def test_compaction_moves_the_epoch_so_the_shown_record_no_longer_matches(self, cache_dir, capsys):
        st = _imp("writ.session.injection_state")
        sl = _imp("writ.session.session_lifecycle")
        _seed("ce-rec", mode="investigate", current_phase=None)
        _update("ce-rec", "--mark-shown", "always_on", "0|", '["A"]')
        assert st.shown_ids(_read("ce-rec"), "always_on") == {"A"}
        sl.cmd_reset_after_compaction("ce-rec")
        assert st.shown_ids(_read("ce-rec"), "always_on") == set()

    def test_a_missing_counter_is_read_as_zero_before_the_bump(self, cache_dir, capsys):
        sl = _imp("writ.session.session_lifecycle")
        cache_mod = _imp("writ.session.cache")
        with cache_mod.mutate_cache("ce-old") as data:
            data.pop("compaction_epoch", None)
        sl.cmd_reset_after_compaction("ce-old")
        assert _read("ce-old")["compaction_epoch"] == 1

    def test_the_existing_compaction_effects_are_unchanged(self, cache_dir, capsys):
        sl = _imp("writ.session.session_lifecycle")
        _seed("ce-keep", mode="work", current_phase="planning",
              loaded_rule_ids_by_phase={"planning": ["X"]}, remaining_budget=10)
        sl.cmd_reset_after_compaction("ce-keep")
        cache = _read("ce-keep")
        assert cache["loaded_rule_ids_by_phase"]["planning"] == []
        assert cache["post_compact_pending"] is True
        assert cache["remaining_budget"] > 10


class TestShouldSkipCache:
    def test_a_master_with_budget_and_low_pressure_proceeds(self):
        bt = _imp("writ.session.budget_tracking")
        assert bt.should_skip_cache({"remaining_budget": 100, "context_percent": 10}) is False

    def test_an_exhausted_budget_skips(self):
        bt = _imp("writ.session.budget_tracking")
        assert bt.should_skip_cache({"remaining_budget": 0}) is True
        assert bt.should_skip_cache({"remaining_budget": -5}) is True

    def test_context_pressure_at_the_threshold_skips_and_below_does_not(self):
        bt = _imp("writ.session.budget_tracking")
        assert bt.should_skip_cache({"remaining_budget": 100, "context_percent": 74}) is False
        assert bt.should_skip_cache({"remaining_budget": 100, "context_percent": 75}) is True

    def test_the_threshold_is_a_parameter(self):
        bt = _imp("writ.session.budget_tracking")
        cache = {"remaining_budget": 100, "context_percent": 50}
        assert bt.should_skip_cache(cache, 50) is True
        assert bt.should_skip_cache(cache, 51) is False

    def test_a_sub_agent_is_never_skipped(self):
        bt = _imp("writ.session.budget_tracking")
        assert bt.should_skip_cache({"is_subagent": True, "remaining_budget": 0, "context_percent": 100}) is False

    def test_an_absent_budget_defaults_to_the_session_budget_and_proceeds(self):
        bt = _imp("writ.session.budget_tracking")
        assert bt.should_skip_cache({}) is False

    def test_cmd_should_skip_gives_the_same_answer_on_a_real_cache(self, cache_dir):
        bt = _imp("writ.session.budget_tracking")
        _seed("sk-1", remaining_budget=0)
        _seed("sk-2", remaining_budget=500, context_percent=10)
        assert bt.cmd_should_skip("sk-1") is True
        assert bt.cmd_should_skip("sk-2") is False
        assert bt.cmd_should_skip("sk-1") == bt.should_skip_cache(_read("sk-1"))
        assert bt.cmd_should_skip("sk-2") == bt.should_skip_cache(_read("sk-2"))


# --------------------------------------------------------------------------- #
# Task 4. The route across turns, against a real cache
# --------------------------------------------------------------------------- #
def _ao_rules(n: int = 2, statement_len: int = 60) -> list[dict]:
    return [{
        "rule_id": f"AO-{i:03d}",
        "trigger": f"always-on trigger {i}",
        "statement": f"ALWAYS-ON STATEMENT {i} " + "a" * statement_len,
    } for i in range(1, n + 1)]


def _floor_rule(i: int) -> dict:
    return {
        "rule_id": f"FLOOR-{i:03d}", "node_type": "Playbook", "channel": "floor",
        "trigger": f"floor trigger {i}", "statement": f"FLOOR STATEMENT {i} " + "f" * 60,
        "severity": "high", "authority": "human", "domain": "process", "score": 1.0,
        "relationships": [],
    }


class _Bundle:
    """Drive /prompt-bundle turn by turn against the REAL session cache."""

    def __init__(self, monkeypatch, *, always_on_rules=None, floor_count: int = 2):
        self.always_on_rules = always_on_rules if always_on_rules is not None else _ao_rules()
        self.floor_rules = [_floor_rule(i) for i in range(1, floor_count + 1)]
        monkeypatch.setattr(server, "_pipeline", object())
        monkeypatch.setattr(server, "_trigger_index", SimpleNamespace(
            floor_ids=lambda mode: {r["rule_id"] for r in self.floor_rules}))
        self.query = AsyncMock(return_value={"mode": "standard", "rules": []})
        self.always_on = AsyncMock(side_effect=lambda **kw: {
            "total_tokens": 100, "rules": list(self.always_on_rules)})
        self.companion = AsyncMock(side_effect=lambda req: {
            "mode": "summary", "total_tokens": 100, "rules": [dict(r) for r in self.floor_rules]})
        monkeypatch.setattr(qroute, "query_rules", self.query)
        monkeypatch.setattr(qroute, "always_on_bundle", self.always_on)
        monkeypatch.setattr(qroute, "methodology_companion", self.companion)
        decision_memory = importlib.import_module("writ.server.routes.decision_memory")
        monkeypatch.setattr(decision_memory, "recall", AsyncMock(return_value={"briefing": ""}))

    async def turn(self, sid: str, section: str, mode: str = "work") -> dict:
        return await qroute.prompt_bundle(PromptBundleRequest(
            session_id=sid, prompt="please implement the thing", mode=mode, sections=[section]))


def _ids(rules: list[dict]) -> list[str]:
    return [r["rule_id"] for r in rules]


class TestCollapseAcrossTurns:
    @pytest.mark.asyncio
    async def test_first_turn_renders_always_on_rules_in_full(self, cache_dir, monkeypatch):
        bundle = _Bundle(monkeypatch)
        out = await bundle.turn("ct-first", "always_on")
        for rule in bundle.always_on_rules:
            assert f"[{rule['rule_id']}] WHEN: {rule['trigger']}" in out["always_on_block"]
            assert rule["statement"] in out["always_on_block"]

    @pytest.mark.asyncio
    async def test_first_turn_renders_floor_methodology_in_full(self, cache_dir, monkeypatch):
        bundle = _Bundle(monkeypatch)
        out = await bundle.turn("ct-first-floor", "methodology")
        for rule in bundle.floor_rules:
            assert rule["statement"] in out["methodology_block"]
            assert f"[{rule['rule_id']}] (" in out["methodology_block"]

    @pytest.mark.asyncio
    async def test_later_turn_collapses_always_on_rules_to_one_line_each(self, cache_dir, monkeypatch):
        ic = _imp("writ.retrieval.injection_ceiling")
        bundle = _Bundle(monkeypatch)
        await bundle.turn("ct-later", "always_on")
        out = await bundle.turn("ct-later", "always_on")
        lines = out["always_on_block"].split("\n")
        for rule in bundle.always_on_rules:
            assert f"[{rule['rule_id']}] WHEN: {rule['trigger']}" in lines
            assert rule["statement"] not in out["always_on_block"]
        assert ic.ALWAYS_ON_COLLAPSED_NOTE in lines

    @pytest.mark.asyncio
    async def test_collapsed_always_on_ids_are_still_recorded_for_citation(self, cache_dir, monkeypatch):
        bundle = _Bundle(monkeypatch)
        await bundle.turn("ct-cite", "always_on")
        out = await bundle.turn("ct-cite", "always_on")
        assert out["ao_meta"]["rule_ids"] == _ids(bundle.always_on_rules)
        assert _read("ct-cite")["always_on_rule_ids"] == sorted(_ids(bundle.always_on_rules))

    @pytest.mark.asyncio
    async def test_later_turn_collapses_floor_rules_and_still_reports_their_ids(self, cache_dir, monkeypatch):
        bundle = _Bundle(monkeypatch)
        await bundle.turn("ct-floor", "methodology")
        out = await bundle.turn("ct-floor", "methodology")
        lines = out["methodology_block"].split("\n")
        for rule in bundle.floor_rules:
            assert f"[{rule['rule_id']}] WHEN: {rule['trigger']}" in lines
            assert rule["statement"] not in out["methodology_block"]
        assert out["method_meta"]["rule_ids"] == _ids(bundle.floor_rules)

    @pytest.mark.asyncio
    async def test_the_shown_record_holds_only_rules_rendered_in_full(self, cache_dir, monkeypatch):
        bundle = _Bundle(monkeypatch)
        await bundle.turn("ct-record", "always_on")
        await bundle.turn("ct-record", "methodology")
        record = _read("ct-record")["injection_shown"]
        assert record["always_on"] == sorted(_ids(bundle.always_on_rules))
        assert record["floor"] == sorted(_ids(bundle.floor_rules))

    @pytest.mark.asyncio
    async def test_a_rule_collapsed_by_the_ceiling_on_its_first_turn_is_not_marked_shown(self, cache_dir, monkeypatch):
        bundle = _Bundle(monkeypatch, always_on_rules=_ao_rules(60, statement_len=400))
        out = await bundle.turn("ct-ceiling", "always_on")
        block = out["always_on_block"]
        full_ids = [f"AO-{i:03d}" for i in range(1, 61) if f"ALWAYS-ON STATEMENT {i} " in block]
        assert out["ao_meta"]["rule_ids"] == [f"AO-{i:03d}" for i in range(1, 61)], "every id present, full or pointer"
        assert 0 < len(full_ids) < 60, "the ceiling collapsed some rules and kept some in full"
        shown = _read("ct-ceiling")["injection_shown"]["always_on"]
        assert shown == sorted(full_ids), "only what was rendered in full is marked shown"

    @pytest.mark.asyncio
    async def test_after_compaction_in_work_mode_the_next_turn_renders_in_full_again(self, cache_dir, monkeypatch, capsys):
        sl = _imp("writ.session.session_lifecycle")
        bundle = _Bundle(monkeypatch)
        _seed("ct-comp-work", mode="work", current_phase="planning")
        await bundle.turn("ct-comp-work", "always_on")
        await bundle.turn("ct-comp-work", "methodology")
        collapsed = await bundle.turn("ct-comp-work", "always_on")
        assert bundle.always_on_rules[0]["statement"] not in collapsed["always_on_block"]
        sl.cmd_reset_after_compaction("ct-comp-work")
        ao = await bundle.turn("ct-comp-work", "always_on")
        floor = await bundle.turn("ct-comp-work", "methodology")
        for rule in bundle.always_on_rules:
            assert rule["statement"] in ao["always_on_block"]
        for rule in bundle.floor_rules:
            assert rule["statement"] in floor["methodology_block"]

    @pytest.mark.asyncio
    async def test_after_compaction_in_a_mode_with_no_phase_the_next_turn_renders_in_full_again(self, cache_dir, monkeypatch, capsys):
        sl = _imp("writ.session.session_lifecycle")
        bundle = _Bundle(monkeypatch)
        _seed("ct-comp-inv", mode="investigate", current_phase=None)
        await bundle.turn("ct-comp-inv", "always_on", mode="investigate")
        await bundle.turn("ct-comp-inv", "methodology", mode="investigate")
        collapsed = await bundle.turn("ct-comp-inv", "always_on", mode="investigate")
        assert bundle.always_on_rules[0]["statement"] not in collapsed["always_on_block"]
        sl.cmd_reset_after_compaction("ct-comp-inv")
        ao = await bundle.turn("ct-comp-inv", "always_on", mode="investigate")
        floor = await bundle.turn("ct-comp-inv", "methodology", mode="investigate")
        for rule in bundle.always_on_rules:
            assert rule["statement"] in ao["always_on_block"]
        for rule in bundle.floor_rules:
            assert rule["statement"] in floor["methodology_block"]

    @pytest.mark.asyncio
    async def test_after_a_phase_change_the_next_turn_renders_in_full_again(self, cache_dir, monkeypatch):
        bundle = _Bundle(monkeypatch)
        _seed("ct-phase", mode="work", current_phase="planning")
        await bundle.turn("ct-phase", "always_on")
        await bundle.turn("ct-phase", "methodology")
        collapsed = await bundle.turn("ct-phase", "methodology")
        assert bundle.floor_rules[0]["statement"] not in collapsed["methodology_block"]
        _seed("ct-phase", current_phase="test-skeletons")
        ao = await bundle.turn("ct-phase", "always_on")
        floor = await bundle.turn("ct-phase", "methodology")
        for rule in bundle.always_on_rules:
            assert rule["statement"] in ao["always_on_block"]
        for rule in bundle.floor_rules:
            assert rule["statement"] in floor["methodology_block"]

    @pytest.mark.asyncio
    async def test_a_new_rule_renders_in_full_beside_pointers_for_the_ones_already_shown(self, cache_dir, monkeypatch):
        bundle = _Bundle(monkeypatch, always_on_rules=_ao_rules(2))
        await bundle.turn("ct-new", "always_on")
        bundle.always_on_rules = _ao_rules(3)
        out = await bundle.turn("ct-new", "always_on")
        block = out["always_on_block"]
        lines = block.split("\n")
        assert "[AO-001] WHEN: always-on trigger 1" in lines
        assert "[AO-002] WHEN: always-on trigger 2" in lines
        assert "ALWAYS-ON STATEMENT 1" not in block and "ALWAYS-ON STATEMENT 2" not in block
        assert "[AO-003] WHEN: always-on trigger 3" in lines
        assert any(ln.startswith("  ALWAYS-ON STATEMENT 3") for ln in lines)

    @pytest.mark.asyncio
    async def test_a_session_never_sees_another_sessions_shown_record(self, cache_dir, monkeypatch):
        bundle = _Bundle(monkeypatch)
        await bundle.turn("ct-iso-a", "always_on")
        out = await bundle.turn("ct-iso-b", "always_on")
        assert bundle.always_on_rules[0]["statement"] in out["always_on_block"]

    @pytest.mark.asyncio
    async def test_pull_rules_are_never_collapsed_by_the_shown_record(self, cache_dir, monkeypatch):
        bundle = _Bundle(monkeypatch)
        pull = {**_floor_rule(9), "rule_id": "PULL-009", "channel": "pull"}
        bundle.companion.side_effect = lambda req: {
            "mode": "summary", "total_tokens": 1,
            "rules": [dict(r) for r in bundle.floor_rules] + [dict(pull)]}
        await bundle.turn("ct-pull", "methodology")
        out = await bundle.turn("ct-pull", "methodology")
        assert pull["statement"] in out["methodology_block"]
        assert "floor trigger 1" in out["methodology_block"]
        assert bundle.floor_rules[0]["statement"] not in out["methodology_block"]


# --------------------------------------------------------------------------- #
# Concurrency: real cmd_update, real cache, real flock
# --------------------------------------------------------------------------- #
class TestConcurrentSections:
    @pytest.mark.asyncio
    async def test_four_sections_gathered_on_one_session_lose_no_cache_write(self, cache_dir, monkeypatch):
        bundle = _Bundle(monkeypatch)
        ranked_rules = [{
            "rule_id": f"RK-{i:03d}", "severity": "high", "authority": "human", "score": 0.9,
            "trigger": f"ranked trigger {i}", "statement": f"ranked statement {i}",
        } for i in range(3)]
        bundle.query.return_value = {"mode": "standard", "rules": ranked_rules}
        decision_memory = importlib.import_module("writ.server.routes.decision_memory")
        decision_memory.recall.return_value = {"briefing": "a prior decision"}

        for round_no in range(10):
            sid = f"cc-gather-{round_no}"
            _seed(sid, mode="work", current_phase="planning")
            results = await asyncio.gather(*(bundle.turn(sid, s) for s in
                                             ("ranked", "always_on", "methodology", "recall")))
            ranked, always_on, methodology, recall = results
            assert all(r["error"] is False and r["skipped"] is False for r in results)
            assert recall["recall_block"] == "a prior decision"

            cache = _read(sid)
            assert cache["recall_briefed"] is True, f"round {round_no}: the recall write was lost"
            assert cache["queries"] == 2, f"round {round_no}: ranked and methodology each count one query"
            assert cache["always_on_rule_ids"] == sorted(_ids(bundle.always_on_rules))
            assert cache["always_on_tokens_used"] > 0
            assert set(ranked["broad_meta"]["rule_ids"]) <= set(cache["loaded_rule_ids"])
            assert set(methodology["method_meta"]["rule_ids"]) <= set(cache["loaded_rule_ids"])
            assert cache["injection_shown"]["always_on"] == sorted(_ids(bundle.always_on_rules))
            assert cache["injection_shown"]["floor"] == sorted(_ids(bundle.floor_rules))
            assert cache["last_injected_rule_ids"] == ranked["broad_meta"]["rule_ids"]

    def test_parallel_processes_marking_distinct_ids_lose_no_write(self, cache_dir):
        ids = [f"AO-P{i:02d}" for i in range(12)]
        procs = [
            subprocess.Popen(
                [sys.executable, str(SESSION_CLI), "update", "cc-procs",
                 "--mark-shown", "always_on", "0|", json.dumps([rid])],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env={**os.environ, "WRIT_CACHE_DIR": str(cache_dir)},
            )
            for rid in ids
        ]
        for proc in procs:
            _out, err = proc.communicate(timeout=60)
            assert proc.returncode == 0, err
        record = _read("cc-procs")["injection_shown"]
        assert record == {"epoch": "0|", "always_on": sorted(ids)}

    def test_parallel_processes_mixing_collapse_and_budget_writes_lose_nothing(self, cache_dir):
        env = {**os.environ, "WRIT_CACHE_DIR": str(cache_dir)}
        argsets = [
            ["--mark-shown", "always_on", "0|", '["A1"]'],
            ["--mark-shown", "floor", "0|", '["F1"]'],
            ["--add-rules", '["M1"]', "--cost", "10", "--inc-queries"],
            ["--add-always-on-rules", '["A1"]', "--add-always-on-tokens", "7"],
            ["--set-recall-briefed"],
        ] * 3
        procs = [
            subprocess.Popen([sys.executable, str(SESSION_CLI), "update", "cc-mixed", *args],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
            for args in argsets
        ]
        for proc in procs:
            _out, err = proc.communicate(timeout=60)
            assert proc.returncode == 0, err
        cache = _read("cc-mixed")
        assert cache["injection_shown"] == {"epoch": "0|", "always_on": ["A1"], "floor": ["F1"]}
        assert cache["queries"] == 3
        assert cache["always_on_tokens_used"] == 21
        assert cache["recall_briefed"] is True
        assert cache["loaded_rule_ids"] == ["M1"]
