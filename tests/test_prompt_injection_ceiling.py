"""Program item 1a: every per-prompt injection section stays under its character ceiling.

The host caps each hook command's injected text at 10,000 characters and, over the cap,
hands the model a file path plus a short preview instead of the text. Writ's one
UserPromptSubmit hook used to emit 10.4 to 17.2 KB every turn. The approved design splits
the injection into four hooks (always-on rules, ranked rules, methodology, recall), each
asking /prompt-bundle for exactly one section, with a 9,500-character ceiling enforced
SERVER-SIDE when the section is rendered.

This module pins the server side and the pure ceiling module:

  * TestBudgetConstants       the single-source budget numbers and the bash twin of the ceiling
  * TestCharLimits            section_char_limit / ranked_char_limit arithmetic
  * TestClampLines            whole-line clamp with a truncation marker
  * TestRenderAlwaysOnSection byte-identical when nothing is shown, pointer lines when shown,
                              the collapse ladder under the limit
  * TestFitRanked             detail is dropped before rules, using the REAL formatter
  * TestFitMethodology        pull rules go before floor rules, floor rules collapse before
                              they are dropped
  * TestCollapseFloor         which floor rules are marked pointer_only
  * TestCmdFormatPointerOnly  the formatter's one-line pointer branch
  * TestRankedPointerText     the ranked pointer no longer claims the block is "above"
  * TestSectionCeilings       route level: len(block) + 2 <= 9,500 for every section
  * TestSectionIsolation      one section per request, and the query-budget counts per section
  * TestLegacyDefault         a request that omits `sections` keeps the legacy trio
  * TestExplicitSectionsSkip  exhausted budget / context pressure short-circuits explicit sections

THE TESTS DO NOT IMPORT THE NEW MODULES AT THE TOP LEVEL. They import them inside each test
through `_imp`, so a missing module fails each test individually with ModuleNotFoundError
instead of collapsing the whole file into one collection error that hides the count.

No daemon and no graph: the route handler is called as a coroutine with `query_rules`,
`always_on_bundle`, `methodology_companion` and the session cache faked at the module seam
(the style tests/test_prompt_bundle.py uses). The formatter is the REAL
`writ.server._run_cmd_format_locked`, because the ceiling is a claim about real rendered text.

Run with `WRIT_TEST_NO_ISOLATION=1 python3 -m pytest tests/test_prompt_injection_ceiling.py`
when no isolated graph is configured; nothing here touches the graph.
"""
from __future__ import annotations

import importlib
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import writ.server as server
import writ.server.routes.query as qroute
from writ.server.models import PromptBundleRequest

REPO = Path(__file__).resolve().parent.parent
BUDGET_JSON = REPO / "writ" / "shared" / "budget.json"
COMMON_SH = REPO / "bin" / "lib" / "common.sh"

# The host's per-hook cap is 10,000; the design ceiling leaves 500 of headroom.
CEILING = 9500
# A section hook prints "\n" + block + "\n".
FRAMING = 2


def _imp(name: str):
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    return importlib.import_module(name)


def _real_format(payload: dict) -> str:
    """The production formatter, exactly as the route calls it."""
    return server._run_cmd_format_locked(payload)


def _rule(i: int, *, statement_len: int = 120, violation_len: int = 0,
          pass_len: int = 0, rationale_len: int = 0, channel: str | None = None,
          trigger: str | None = None, score: float = 0.9) -> dict:
    rule = {
        "rule_id": f"R-{i:03d}",
        "severity": "high",
        "authority": "human",
        "score": score,
        "trigger": trigger if trigger is not None else f"when condition {i} holds",
        "statement": (f"statement {i} " + "s" * statement_len)[:statement_len],
    }
    if violation_len:
        rule["violation"] = "v" * violation_len
    if pass_len:
        rule["pass_example"] = "p" * pass_len
    if rationale_len:
        rule["rationale"] = "r" * rationale_len
    if channel:
        rule["channel"] = channel
    return rule


def _ao_rule(i: int, statement_len: int = 120, trigger_len: int = 30) -> dict:
    return {
        "rule_id": f"AO-{i:03d}",
        "trigger": (f"trigger {i} " + "t" * trigger_len)[:trigger_len],
        "statement": (f"statement {i} " + "s" * statement_len)[:statement_len],
    }


def _header_count(text: str) -> int:
    """How many rule header lines `[ID] (severity...) score=` the formatter printed."""
    return sum(1 for ln in text.split("\n") if re.match(r"^\[[^\]]+\] \(.*\) score=", ln))


# --------------------------------------------------------------------------- #
# Task 1. Budget constants
# --------------------------------------------------------------------------- #
class TestBudgetConstants:
    def test_ceiling_is_9500_in_budget_json(self):
        data = json.loads(BUDGET_JSON.read_text())
        assert data["prompt_char_ceiling"] == 9500

    def test_tokens_module_exports_the_same_ceiling(self):
        tokens = _imp("writ.shared.tokens")
        assert tokens.PROMPT_CHAR_CEILING == 9500
        assert tokens.CHARS_PER_TOKEN == 4

    def test_four_sections_plus_reserved_equal_the_total(self):
        tokens = _imp("writ.shared.tokens")
        assert set(tokens.PROMPT_SECTION_TOKENS) == {"always_on", "ranked", "methodology", "recall"}
        total = sum(tokens.PROMPT_SECTION_TOKENS.values()) + sum(tokens.PROMPT_RESERVED_TOKENS.values())
        assert tokens.PROMPT_TOTAL_TOKENS == 9500
        assert total == tokens.PROMPT_TOTAL_TOKENS

    def test_section_budgets_are_the_documented_split(self):
        tokens = _imp("writ.shared.tokens")
        assert tokens.PROMPT_SECTION_TOKENS == {
            "always_on": 2000, "ranked": 2300, "methodology": 2000, "recall": 500,
        }

    def test_document_chunk_budget_is_reserved_for_a_fifth_hook(self):
        tokens = _imp("writ.shared.tokens")
        assert tokens.PROMPT_RESERVED_TOKENS.get("document_chunks", 0) > 0

    def test_budget_json_matches_the_loaded_constants(self):
        tokens = _imp("writ.shared.tokens")
        data = json.loads(BUDGET_JSON.read_text())
        assert tokens.PROMPT_SECTION_TOKENS == data["prompt_section_tokens"]
        assert tokens.PROMPT_RESERVED_TOKENS == data["prompt_reserved_tokens"]
        assert tokens.PROMPT_TOTAL_TOKENS == data["prompt_total_tokens"]

    def test_bash_ceiling_constant_equals_the_json_ceiling(self):
        data = json.loads(BUDGET_JSON.read_text())
        match = re.search(r"^WRIT_PROMPT_CHAR_CEILING=(\d+)\s*$", COMMON_SH.read_text(), re.M)
        assert match, "bin/lib/common.sh must define WRIT_PROMPT_CHAR_CEILING=<digits>"
        assert int(match.group(1)) == data["prompt_char_ceiling"]


# --------------------------------------------------------------------------- #
# Task 3. The pure ceiling module
# --------------------------------------------------------------------------- #
class TestCharLimits:
    @pytest.mark.parametrize("section,expected", [
        ("always_on", 7998),     # min(9500, 2000*4) - 2
        ("ranked", 9198),        # min(9500, 2300*4) - 2
        ("methodology", 7998),   # min(9500, 2000*4) - 2
        ("recall", 1998),        # min(9500, 500*4) - 2
    ])
    def test_section_char_limit(self, section, expected):
        ic = _imp("writ.retrieval.injection_ceiling")
        assert ic.section_char_limit(section) == expected

    def test_every_section_limit_plus_framing_fits_the_ceiling(self):
        ic = _imp("writ.retrieval.injection_ceiling")
        for section in ("always_on", "ranked", "methodology", "recall"):
            assert ic.section_char_limit(section) + ic.SECTION_FRAMING_CHARS <= CEILING

    def test_ranked_limit_without_reserve_or_nudge_is_the_section_limit(self):
        ic = _imp("writ.retrieval.injection_ceiling")
        assert ic.ranked_char_limit(0, "") == ic.section_char_limit("ranked")

    def test_ranked_limit_subtracts_reserve_nudge_and_its_own_newline(self):
        ic = _imp("writ.retrieval.injection_ceiling")
        nudge = ic.NUDGE_TEXT["LOW_SCORES"]
        assert ic.ranked_char_limit(3000, nudge) == CEILING - 3000 - (len(nudge) + 2) - 1

    def test_negative_reserve_is_treated_as_zero(self):
        ic = _imp("writ.retrieval.injection_ceiling")
        assert ic.ranked_char_limit(-50, "") == ic.ranked_char_limit(0, "")

    def test_reserve_larger_than_the_ceiling_leaves_no_room(self):
        ic = _imp("writ.retrieval.injection_ceiling")
        assert ic.ranked_char_limit(CEILING + 100, "") <= 0

    def test_nudge_texts_are_the_two_moved_sentences(self):
        ic = _imp("writ.retrieval.injection_ceiling")
        assert set(ic.NUDGE_TEXT) == {"NO_RULES", "LOW_SCORES"}
        assert ic.NUDGE_TEXT["NO_RULES"].startswith("[Writ: no matching rules found for this task.")
        assert ic.NUDGE_TEXT["LOW_SCORES"].startswith("[Writ: retrieved rules have low relevance scores (< 0.3).")
        assert all("POST /propose" in t for t in ic.NUDGE_TEXT.values())


class TestClampLines:
    def test_text_within_the_limit_is_returned_unchanged(self):
        ic = _imp("writ.retrieval.injection_ceiling")
        text = "line one\nline two"
        assert ic.clamp_lines(text, 1000) == text

    def test_text_exactly_at_the_limit_is_unchanged(self):
        ic = _imp("writ.retrieval.injection_ceiling")
        text = "a" * 40
        assert ic.clamp_lines(text, 40) == text

    def test_over_limit_text_ends_with_the_truncation_marker_and_fits(self):
        ic = _imp("writ.retrieval.injection_ceiling")
        text = "\n".join(f"line {i:04d} " + "x" * 80 for i in range(200))
        out = ic.clamp_lines(text, 1000)
        assert len(out) <= 1000
        assert out.split("\n")[-1] == ic.TRUNCATION_MARKER

    def test_only_whole_lines_survive(self):
        ic = _imp("writ.retrieval.injection_ceiling")
        original = [f"line {i:04d} " + "x" * 80 for i in range(200)]
        out = ic.clamp_lines("\n".join(original), 1000)
        kept = out.split("\n")[:-1]
        assert kept, "some whole lines should fit in 1000 characters"
        assert kept == original[: len(kept)]

    def test_limit_not_larger_than_the_marker_returns_empty(self):
        ic = _imp("writ.retrieval.injection_ceiling")
        assert ic.clamp_lines("x" * 500, len(ic.TRUNCATION_MARKER)) == ""

    def test_negative_limit_returns_empty(self):
        ic = _imp("writ.retrieval.injection_ceiling")
        assert ic.clamp_lines("anything at all", -5) == ""

    def test_a_single_line_longer_than_the_room_keeps_only_the_marker(self):
        ic = _imp("writ.retrieval.injection_ceiling")
        out = ic.clamp_lines("y" * 5000, 200)
        assert out == ic.TRUNCATION_MARKER


class TestRenderAlwaysOnSection:
    def test_nothing_shown_and_under_the_limit_is_byte_identical_to_render_always_on(self):
        ic = _imp("writ.retrieval.injection_ceiling")
        pb = _imp("writ.retrieval.prompt_bundle")
        ao = {"total_tokens": 42, "rules": [_ao_rule(1), _ao_rule(2), _ao_rule(3)]}
        legacy_block, legacy_tokens, _count = pb.render_always_on(ao)
        got = ic.render_always_on_section(ao, set(), 7998)
        assert got.text == legacy_block
        assert got.tokens == legacy_tokens
        assert got.rule_ids == ["AO-001", "AO-002", "AO-003"]
        assert got.full_ids == got.rule_ids

    def test_no_renderable_rules_yields_an_empty_render(self):
        ic = _imp("writ.retrieval.injection_ceiling")
        got = ic.render_always_on_section({"rules": [], "total_tokens": 0}, set(), 7998)
        assert (got.text, got.rule_ids, got.full_ids, got.tokens) == ("", [], [], 0)

    def test_a_shown_rule_renders_as_one_when_line_with_no_statement(self):
        ic = _imp("writ.retrieval.injection_ceiling")
        ao = {"total_tokens": 10, "rules": [_ao_rule(1), _ao_rule(2)]}
        got = ic.render_always_on_section(ao, {"AO-001"}, 7998)
        lines = got.text.split("\n")
        assert f"[AO-001] WHEN: {ao['rules'][0]['trigger']}" in lines
        assert ao["rules"][0]["statement"] not in got.text
        assert ao["rules"][1]["statement"] in got.text
        assert ic.ALWAYS_ON_COLLAPSED_NOTE in lines

    def test_every_id_is_still_recorded_but_only_unshown_ones_are_full(self):
        ic = _imp("writ.retrieval.injection_ceiling")
        ao = {"total_tokens": 10, "rules": [_ao_rule(1), _ao_rule(2), _ao_rule(3)]}
        got = ic.render_always_on_section(ao, {"AO-001", "AO-003"}, 7998)
        assert got.rule_ids == ["AO-001", "AO-002", "AO-003"]
        assert got.full_ids == ["AO-002"]

    def test_no_note_line_when_nothing_is_collapsed(self):
        ic = _imp("writ.retrieval.injection_ceiling")
        ao = {"total_tokens": 10, "rules": [_ao_rule(1)]}
        got = ic.render_always_on_section(ao, set(), 7998)
        assert ic.ALWAYS_ON_COLLAPSED_NOTE not in got.text

    def test_block_keeps_header_and_footer(self):
        ic = _imp("writ.retrieval.injection_ceiling")
        ao = {"total_tokens": 10, "rules": [_ao_rule(1), _ao_rule(2)]}
        got = ic.render_always_on_section(ao, {"AO-001", "AO-002"}, 7998)
        assert got.text.startswith(ic.ALWAYS_ON_HEADER)
        assert got.text.endswith(ic.ALWAYS_ON_FOOTER)

    def test_sixty_long_rules_collapse_to_fit_with_every_id_present(self):
        ic = _imp("writ.retrieval.injection_ceiling")
        limit = ic.section_char_limit("always_on")
        ao = {"total_tokens": 9999, "rules": [_ao_rule(i, statement_len=400) for i in range(60)]}
        got = ic.render_always_on_section(ao, set(), limit)
        assert len(got.text) <= limit
        assert got.rule_ids == [f"AO-{i:03d}" for i in range(60)]
        for rid in got.rule_ids:
            assert f"[{rid}] WHEN:" in got.text
        assert 0 < len(got.full_ids) < 60, "head rules stay full, tail rules collapse"
        assert got.full_ids == got.rule_ids[: len(got.full_ids)], "collapse runs from the tail first"

    def test_collapsed_render_reports_estimated_tokens_not_the_bundle_total(self):
        ic = _imp("writ.retrieval.injection_ceiling")
        limit = ic.section_char_limit("always_on")
        ao = {"total_tokens": 9999, "rules": [_ao_rule(i, statement_len=400) for i in range(60)]}
        got = ic.render_always_on_section(ao, set(), limit)
        assert got.tokens == len(got.text) // ic.CHARS_PER_TOKEN

    def test_extreme_case_drops_rules_from_the_tail_behind_an_omitted_line(self):
        ic = _imp("writ.retrieval.injection_ceiling")
        limit = ic.section_char_limit("always_on")
        ao = {"total_tokens": 1, "rules": [_ao_rule(i, statement_len=50, trigger_len=110) for i in range(400)]}
        got = ic.render_always_on_section(ao, set(), limit)
        assert len(got.text) <= limit
        assert 0 < len(got.rule_ids) < 400
        assert got.rule_ids == [f"AO-{i:03d}" for i in range(len(got.rule_ids))]
        omitted = 400 - len(got.rule_ids)
        assert f"[Writ: {omitted} always-active rule(s) omitted at the character ceiling]" in got.text
        assert got.full_ids == [], "nothing stays full once the block had to drop rules"

    def test_a_limit_too_small_for_even_one_pointer_renders_nothing(self):
        ic = _imp("writ.retrieval.injection_ceiling")
        ao = {"total_tokens": 1, "rules": [_ao_rule(1)]}
        got = ic.render_always_on_section(ao, set(), 10)
        assert (got.text, got.rule_ids, got.full_ids, got.tokens) == ("", [], [], 0)


class TestFitRanked:
    def test_a_payload_that_fits_is_returned_as_retrieved(self):
        ic = _imp("writ.retrieval.injection_ceiling")
        payload = {"mode": "standard", "rules": [_rule(i, violation_len=40, pass_len=40) for i in range(3)]}
        text, meta, used = ic.fit_ranked(payload, _real_format, 9000)
        assert used == payload
        assert "VIOLATION:" in text and "CORRECT:" in text
        assert meta["rule_ids"] == ["R-000", "R-001", "R-002"]

    def test_ten_full_rules_with_huge_examples_fit_by_stripping_the_examples(self):
        ic = _imp("writ.retrieval.injection_ceiling")
        payload = {"mode": "full", "rules": [_rule(i, violation_len=2000, pass_len=2000) for i in range(10)]}
        text, meta, used = ic.fit_ranked(payload, _real_format, 9000)
        assert len(text) <= 9000
        assert meta["rule_ids"] == [f"R-{i:03d}" for i in range(10)], "no rule dropped"
        assert "VIOLATION:" not in text and "CORRECT:" not in text
        assert all("violation" not in r and "pass_example" not in r for r in used["rules"])

    def test_full_becomes_standard_before_any_rule_is_dropped(self):
        ic = _imp("writ.retrieval.injection_ceiling")
        # No example fields, so the strip step changes nothing; the rationale is what is big.
        payload = {"mode": "full", "rules": [_rule(i, rationale_len=1500) for i in range(10)]}
        text, meta, used = ic.fit_ranked(payload, _real_format, 9000)
        assert used["mode"] == "standard"
        assert "RATIONALE:" not in text
        assert len(meta["rule_ids"]) == 10

    def test_summary_is_reached_before_any_rule_is_dropped(self):
        ic = _imp("writ.retrieval.injection_ceiling")
        # Standard renders RELATED lines; summary does not. 10 rules x 40 related ids.
        rules = []
        for i in range(10):
            r = _rule(i, statement_len=100)
            r["relationships"] = [{"rule_id": f"REL-{i:03d}-{j:03d}", "direction": "out"} for j in range(40)]
            rules.append(r)
        payload = {"mode": "standard", "rules": rules}
        text, meta, used = ic.fit_ranked(payload, _real_format, 4000)
        assert used["mode"] == "summary"
        assert "RELATED:" not in text
        assert len(meta["rule_ids"]) == 10

    def test_long_statements_drop_rules_from_the_tail_last(self):
        ic = _imp("writ.retrieval.injection_ceiling")
        payload = {"mode": "standard", "rules": [_rule(i, statement_len=1500) for i in range(10)]}
        text, meta, used = ic.fit_ranked(payload, _real_format, 9000)
        kept = meta["rule_ids"]
        assert 0 < len(kept) < 10
        assert kept == [f"R-{i:03d}" for i in range(len(kept))], "a prefix of the input order"
        assert len(text) <= 9000

    def test_rendered_header_count_equals_the_recorded_rule_ids(self):
        ic = _imp("writ.retrieval.injection_ceiling")
        payload = {"mode": "standard", "rules": [_rule(i, statement_len=1500) for i in range(10)]}
        text, meta, _used = ic.fit_ranked(payload, _real_format, 9000)
        assert _header_count(text) == len(meta["rule_ids"])
        assert f"--- WRIT RULES ({len(meta['rule_ids'])} rules," in text

    def test_negative_limit_returns_nothing(self):
        ic = _imp("writ.retrieval.injection_ceiling")
        payload = {"mode": "standard", "rules": [_rule(0)]}
        text, meta, used = ic.fit_ranked(payload, _real_format, -1)
        assert text == ""
        assert meta == {"rule_ids": [], "cost": 0}
        assert used["rules"] == []

    def test_ladder_order_detail_before_rules(self):
        ic = _imp("writ.retrieval.injection_ceiling")
        seen: list[tuple] = []

        def render(candidate: dict) -> str:
            rules = candidate["rules"]
            seen.append((candidate.get("mode"), len(rules), any("violation" in r for r in rules)))
            return "x" * 20000 + '\nWRIT_META:{"rule_ids": [], "cost": 0}'

        payload = {"mode": "full", "rules": [_rule(i, violation_len=10, pass_len=10) for i in range(3)]}
        text, meta, used = ic.fit_ranked(payload, render, 100)
        assert text == "" and used["rules"] == []
        assert seen == [
            ("full", 3, True),       # as retrieved
            ("full", 3, False),      # examples stripped
            ("standard", 3, False),  # full -> standard
            ("summary", 3, False),   # standard -> summary
            ("summary", 2, False),   # then rules from the tail
            ("summary", 1, False),
            ("summary", 0, False),
        ]


def _methodology_payload(floor: int = 0, pull: int = 0, statement_len: int = 1500) -> dict:
    rules = [_rule(i, statement_len=statement_len, channel="floor", trigger=f"floor trigger {i}")
             for i in range(floor)]
    rules += [_rule(100 + i, statement_len=statement_len, channel="pull", trigger=f"pull trigger {i}")
              for i in range(pull)]
    for r in rules:
        r["rule_id"] = ("FLOOR-" if r["channel"] == "floor" else "PULL-") + r["rule_id"][2:]
    return {"mode": "summary", "rules": rules}


class TestFitMethodology:
    def test_a_payload_that_fits_is_prefixed_with_the_companion_header(self):
        ic = _imp("writ.retrieval.injection_ceiling")
        payload = _methodology_payload(floor=2, statement_len=100)
        block, meta, used = ic.fit_methodology(payload, _real_format, 7998)
        assert block.startswith(ic.METHODOLOGY_HEADER + "\n")
        assert len(block) <= 7998
        assert meta["rule_ids"] == ["FLOOR-000", "FLOOR-001"]
        assert used["rules"] == payload["rules"]

    def test_pull_rules_are_dropped_before_any_floor_rule(self):
        ic = _imp("writ.retrieval.injection_ceiling")
        payload = _methodology_payload(floor=3, pull=2, statement_len=1500)  # ~7.5k of text
        block, meta, used = ic.fit_methodology(payload, _real_format, 5500)
        assert len(block) <= 5500
        assert meta["rule_ids"] == ["FLOOR-000", "FLOOR-001", "FLOOR-002"]
        assert not any(r["channel"] == "pull" for r in used["rules"])
        assert not any(r.get("pointer_only") for r in used["rules"]), "floor stays full when dropping pull is enough"

    def test_floor_rules_collapse_to_pointers_before_any_is_dropped(self):
        ic = _imp("writ.retrieval.injection_ceiling")
        payload = _methodology_payload(floor=8, statement_len=1500)  # ~12k of text
        block, meta, used = ic.fit_methodology(payload, _real_format, 5000)
        assert len(block) <= 5000
        assert meta["rule_ids"] == [f"FLOOR-{i:03d}" for i in range(8)], "no floor rule dropped"
        pointers = [r for r in used["rules"] if r.get("pointer_only")]
        assert 0 < len(pointers) < 8
        # the collapse runs from the tail: the head stays full
        assert not used["rules"][0].get("pointer_only")
        assert used["rules"][-1].get("pointer_only")
        assert "[FLOOR-007] WHEN: floor trigger 7" in block.split("\n")
        assert payload["rules"][7]["statement"] not in block

    def test_floor_rules_are_dropped_from_the_tail_only_after_all_are_pointers(self):
        ic = _imp("writ.retrieval.injection_ceiling")
        payload = _methodology_payload(floor=8, statement_len=1500)
        block, meta, used = ic.fit_methodology(payload, _real_format, 230)
        assert len(block) <= 230
        kept = meta["rule_ids"]
        assert 0 < len(kept) < 8
        assert kept == [f"FLOOR-{i:03d}" for i in range(len(kept))]
        assert all(r.get("pointer_only") for r in used["rules"])
        assert "RULE:" not in block

    def test_a_limit_too_small_for_anything_returns_an_empty_block(self):
        ic = _imp("writ.retrieval.injection_ceiling")
        payload = _methodology_payload(floor=2)
        block, meta, used = ic.fit_methodology(payload, _real_format, 10)
        assert block == ""
        assert used["rules"] == []

    def test_an_empty_companion_renders_no_block(self):
        ic = _imp("writ.retrieval.injection_ceiling")
        block, meta, _used = ic.fit_methodology({"mode": "summary", "rules": []}, _real_format, 7998)
        assert block == ""
        assert meta["rule_ids"] == []

    def test_the_input_payload_is_not_mutated(self):
        ic = _imp("writ.retrieval.injection_ceiling")
        payload = _methodology_payload(floor=8, pull=2, statement_len=1500)
        snapshot = json.loads(json.dumps(payload))
        ic.fit_methodology(payload, _real_format, 5000)
        assert payload == snapshot


class TestCollapseFloor:
    def test_a_shown_floor_rule_is_marked_pointer_only(self):
        ic = _imp("writ.retrieval.injection_ceiling")
        rules = [{"rule_id": "F-1", "channel": "floor"}, {"rule_id": "F-2", "channel": "floor"}]
        out = ic.collapse_floor(rules, {"F-1"})
        assert out[0]["pointer_only"] is True
        assert not out[1].get("pointer_only")

    def test_pull_and_push_rules_are_never_collapsed_even_when_their_id_was_shown(self):
        ic = _imp("writ.retrieval.injection_ceiling")
        rules = [{"rule_id": "P-1", "channel": "pull"}, {"rule_id": "U-1", "channel": "push"}]
        out = ic.collapse_floor(rules, {"P-1", "U-1"})
        assert not any(r.get("pointer_only") for r in out)

    def test_inputs_are_copied_not_mutated(self):
        ic = _imp("writ.retrieval.injection_ceiling")
        rules = [{"rule_id": "F-1", "channel": "floor"}]
        out = ic.collapse_floor(rules, {"F-1"})
        assert "pointer_only" not in rules[0]
        assert out[0] is not rules[0]


class TestCmdFormatPointerOnly:
    def test_a_pointer_only_rule_renders_one_when_line(self):
        payload = {"mode": "summary", "rules": [
            {**_rule(1, channel="floor", trigger="floor trigger one"), "pointer_only": True},
        ]}
        raw = _real_format(payload)
        lines = raw.split("\n")
        assert "[R-001] WHEN: floor trigger one" in lines
        assert "RULE:" not in raw
        assert "score=" not in raw

    def test_a_pointer_only_rule_still_lands_in_the_meta_rule_ids(self):
        pb = _imp("writ.retrieval.prompt_bundle")
        payload = {"mode": "summary", "rules": [
            {**_rule(1, channel="floor"), "pointer_only": True}, _rule(2, channel="floor"),
        ]}
        _text, meta = pb.split_format(_real_format(payload))
        assert meta["rule_ids"] == ["R-001", "R-002"]

    def test_pointer_lines_have_no_blank_separator_but_full_rules_do(self):
        payload = {"mode": "summary", "rules": [
            {**_rule(1, channel="floor", trigger="t one"), "pointer_only": True},
            {**_rule(2, channel="floor", trigger="t two"), "pointer_only": True},
            _rule(3, channel="floor", trigger="t three"),
        ]}
        lines = _real_format(payload).split("\n")
        first = lines.index("[R-001] WHEN: t one")
        assert lines[first + 1] == "[R-002] WHEN: t two"
        assert lines[first + 2].startswith("[R-003] (")

    def test_a_rule_without_the_flag_renders_exactly_as_before(self):
        payload = {"mode": "standard", "rules": [_rule(1, violation_len=20, pass_len=20)]}
        raw = _real_format(payload)
        assert "[R-001] (high) score=0.900" in raw
        assert "WHEN: when condition 1 holds" in raw
        assert "VIOLATION:" in raw and "CORRECT:" in raw


class TestRankedPointerText:
    def test_the_pointer_constant_no_longer_says_above(self):
        bt = _imp("writ.session.budget_tracking")
        assert bt._ALWAYS_ACTIVE_POINTER == "SEE: [{rule_id}] in the ALWAYS-ACTIVE RULES block"

    def test_a_ranked_rule_already_in_the_always_on_block_points_without_claiming_above(self):
        payload = {"mode": "standard", "rules": [
            {**_rule(1, violation_len=20, pass_len=20), "already_injected": True},
        ]}
        raw = _real_format(payload)
        assert "SEE: [R-001] in the ALWAYS-ACTIVE RULES block" in raw
        assert "above" not in raw.lower()

    def test_summary_mode_keeps_its_content_even_for_an_overlapping_rule(self):
        payload = {"mode": "summary", "rules": [{**_rule(1), "already_injected": True}]}
        raw = _real_format(payload)
        assert "SEE:" not in raw
        assert "WHEN: when condition 1 holds" in raw


# --------------------------------------------------------------------------- #
# Task 4. The /prompt-bundle route
# --------------------------------------------------------------------------- #
def _cache(**overrides) -> dict:
    cache = {
        "loaded_rule_ids_by_phase": {}, "current_phase": "", "loaded_rule_ids": [],
        "remaining_budget": 8000, "last_injected_rule_ids": [], "detected_domain": "",
        "context_percent": 0, "is_subagent": False, "recall_briefed": False,
        "compaction_epoch": 0, "injection_shown": {},
    }
    cache.update(overrides)
    return cache


def _install(monkeypatch, *, cache=None, query=None, always_on=None, companion=None,
             briefing="", floor_ids=()):
    """Fake the retrieval seams and return spies. The formatter stays REAL."""
    monkeypatch.setattr(server, "_pipeline", object())
    monkeypatch.setattr(server, "_trigger_index", SimpleNamespace(floor_ids=lambda mode: set(floor_ids)))
    snapshot = cache if cache is not None else _cache()
    monkeypatch.setattr(server.writ_session, "_read_cache", lambda sid: dict(snapshot))
    spies = SimpleNamespace(
        query=AsyncMock(return_value=query if query is not None else {"mode": "standard", "rules": []}),
        always_on=AsyncMock(return_value=always_on if always_on is not None else {"rules": [], "total_tokens": 0}),
        companion=AsyncMock(return_value=companion if companion is not None else {"mode": "summary", "rules": []}),
        recall=AsyncMock(return_value={"briefing": briefing}),
        update=MagicMock(),
    )
    monkeypatch.setattr(qroute, "query_rules", spies.query)
    monkeypatch.setattr(qroute, "always_on_bundle", spies.always_on)
    monkeypatch.setattr(qroute, "methodology_companion", spies.companion)
    monkeypatch.setattr(server.writ_session, "cmd_update", spies.update)
    decision_memory = importlib.import_module("writ.server.routes.decision_memory")
    monkeypatch.setattr(decision_memory, "recall", spies.recall)
    return spies


def _update_flags(spies) -> set[str]:
    flags: set[str] = set()
    for call in spies.update.call_args_list:
        flags.update(a for a in call.args[-1] if isinstance(a, str) and a.startswith("--"))
    return flags


def _oversize_ranked(n: int = 10, statement_len: int = 3000, score: float = 0.9) -> dict:
    return {"mode": "standard", "rules": [_rule(i, statement_len=statement_len, score=score) for i in range(n)]}


def _oversize_always_on() -> dict:
    return {"total_tokens": 20000, "rules": [_ao_rule(i, statement_len=400) for i in range(100)]}


def _oversize_companion() -> dict:
    rules = [_rule(i, statement_len=3000, channel="floor") for i in range(8)]
    rules += [_rule(50 + i, statement_len=3000, channel="pull") for i in range(4)]
    for r in rules:
        r["rule_id"] = ("FLOOR-" if r["channel"] == "floor" else "PULL-") + r["rule_id"][2:]
    return {"mode": "summary", "rules": rules, "total_tokens": 30000}


def _oversize_briefing() -> str:
    return "\n".join(f"decision {i:03d} " + "d" * 900 for i in range(40))


class TestSectionCeilings:
    @pytest.mark.asyncio
    async def test_always_on_block_plus_framing_fits_the_ceiling(self, monkeypatch):
        _install(monkeypatch, always_on=_oversize_always_on())
        out = await qroute.prompt_bundle(PromptBundleRequest(
            session_id="s1", prompt="x", mode="work", sections=["always_on"]))
        assert out["always_on_block"] != ""
        assert len(out["always_on_block"]) + FRAMING <= CEILING

    @pytest.mark.asyncio
    async def test_ranked_rules_text_plus_framing_fits_the_ceiling(self, monkeypatch):
        _install(monkeypatch, query=_oversize_ranked())
        out = await qroute.prompt_bundle(PromptBundleRequest(
            session_id="s1", prompt="x", mode="work", sections=["ranked"]))
        assert out["rules_text"] != ""
        assert len(out["rules_text"]) + FRAMING <= CEILING

    @pytest.mark.asyncio
    async def test_methodology_block_plus_framing_fits_the_ceiling(self, monkeypatch):
        _install(monkeypatch, companion=_oversize_companion(),
                 floor_ids={f"FLOOR-{i:03d}" for i in range(8)})
        out = await qroute.prompt_bundle(PromptBundleRequest(
            session_id="s1", prompt="x", mode="work", sections=["methodology"]))
        assert out["methodology_block"] != ""
        assert len(out["methodology_block"]) + FRAMING <= CEILING

    @pytest.mark.asyncio
    async def test_recall_block_plus_framing_fits_the_ceiling_and_its_own_limit(self, monkeypatch):
        ic = _imp("writ.retrieval.injection_ceiling")
        _install(monkeypatch, briefing=_oversize_briefing())
        out = await qroute.prompt_bundle(PromptBundleRequest(
            session_id="s1", prompt="x", mode="work", sections=["recall"]))
        assert out["recall_block"] != ""
        assert len(out["recall_block"]) + FRAMING <= CEILING
        assert len(out["recall_block"]) <= ic.section_char_limit("recall")

    @pytest.mark.asyncio
    async def test_recall_is_clamped_at_whole_lines_and_ends_with_the_marker(self, monkeypatch):
        ic = _imp("writ.retrieval.injection_ceiling")
        briefing = _oversize_briefing()
        _install(monkeypatch, briefing=briefing)
        out = await qroute.prompt_bundle(PromptBundleRequest(
            session_id="s1", prompt="x", mode="work", sections=["recall"]))
        lines = out["recall_block"].split("\n")
        assert lines[-1] == ic.TRUNCATION_MARKER
        original = briefing.split("\n")
        assert lines[:-1] == original[: len(lines) - 1]

    @pytest.mark.asyncio
    async def test_a_short_recall_briefing_is_returned_whole(self, monkeypatch):
        _install(monkeypatch, briefing="one decision\nanother decision")
        out = await qroute.prompt_bundle(PromptBundleRequest(
            session_id="s1", prompt="x", mode="work", sections=["recall"]))
        assert out["recall_block"] == "one decision\nanother decision"

    @pytest.mark.asyncio
    async def test_ranked_renders_into_the_room_left_by_reserve_chars_and_the_nudge(self, monkeypatch):
        ic = _imp("writ.retrieval.injection_ceiling")
        _install(monkeypatch, query=_oversize_ranked(score=0.1))
        out = await qroute.prompt_bundle(PromptBundleRequest(
            session_id="s1", prompt="x", mode="work", sections=["ranked"], reserve_chars=3000))
        assert out["nudge"] == "LOW_SCORES"
        assert out["nudge_text"] == ic.NUDGE_TEXT["LOW_SCORES"]
        assert out["rules_text"] != ""
        total = 3000 + len(out["rules_text"]) + 1 + len(out["nudge_text"]) + 2
        assert total <= CEILING

    @pytest.mark.asyncio
    async def test_a_larger_reserve_yields_a_smaller_ranked_block(self, monkeypatch):
        _install(monkeypatch, query=_oversize_ranked())
        small = await qroute.prompt_bundle(PromptBundleRequest(
            session_id="s1", prompt="x", mode="work", sections=["ranked"], reserve_chars=0))
        big = await qroute.prompt_bundle(PromptBundleRequest(
            session_id="s1", prompt="x", mode="work", sections=["ranked"], reserve_chars=6000))
        assert len(big["rules_text"]) <= CEILING - 6000 - 1
        assert len(big["rules_text"]) < len(small["rules_text"])

    @pytest.mark.asyncio
    async def test_recorded_rule_ids_are_exactly_the_rules_that_were_rendered(self, monkeypatch):
        spies = _install(monkeypatch, query=_oversize_ranked())
        out = await qroute.prompt_bundle(PromptBundleRequest(
            session_id="s1", prompt="x", mode="work", sections=["ranked"]))
        assert _header_count(out["rules_text"]) == len(out["broad_meta"]["rule_ids"])
        assert 0 < len(out["broad_meta"]["rule_ids"]) < 10
        add_rules = [c.args[-1] for c in spies.update.call_args_list if "--add-rules" in c.args[-1]]
        assert add_rules, "the ranked section records what it injected"
        args = add_rules[0]
        recorded = json.loads(args[args.index("--add-rules") + 1])
        assert recorded == out["broad_meta"]["rule_ids"]


class TestSectionIsolation:
    _FIELDS = ("always_on_block", "rules_text", "methodology_block", "recall_block")
    _FIELD_OF = {"always_on": "always_on_block", "ranked": "rules_text",
                 "methodology": "methodology_block", "recall": "recall_block"}
    _META_OF = {"always_on": "ao_meta", "ranked": "broad_meta", "methodology": "method_meta"}

    def _fakes(self, monkeypatch):
        return _install(
            monkeypatch,
            query=_oversize_ranked(n=3, statement_len=100),
            always_on={"total_tokens": 50, "rules": [_ao_rule(1), _ao_rule(2)]},
            companion={"mode": "summary", "total_tokens": 100, "rules": [
                {**_rule(1, channel="floor"), "rule_id": "FLOOR-001"}]},
            briefing="a prior decision",
            floor_ids={"FLOOR-001"},
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("section", ["always_on", "ranked", "methodology", "recall"])
    async def test_a_single_section_fills_only_its_own_field(self, monkeypatch, section):
        self._fakes(monkeypatch)
        out = await qroute.prompt_bundle(PromptBundleRequest(
            session_id="s1", prompt="x", mode="work", sections=[section]))
        assert out["error"] is False
        assert out[self._FIELD_OF[section]] != ""
        for other, field in self._FIELD_OF.items():
            if other != section:
                assert out[field] == "", f"{field} must stay empty for a {section} request"
        for other, meta in self._META_OF.items():
            if other == section:
                assert out[meta] is not None
            else:
                assert out[meta] is None, f"{meta} must stay null for a {section} request"

    @pytest.mark.asyncio
    async def test_always_on_request_reads_always_on_only(self, monkeypatch):
        spies = self._fakes(monkeypatch)
        await qroute.prompt_bundle(PromptBundleRequest(
            session_id="s1", prompt="x", mode="work", sections=["always_on"]))
        assert spies.always_on.await_count == 1
        assert spies.query.await_count == 0
        assert spies.companion.await_count == 0
        assert spies.recall.await_count == 0

    @pytest.mark.asyncio
    async def test_ranked_request_runs_retrieval_and_the_read_only_always_on_overlap_read(self, monkeypatch):
        spies = self._fakes(monkeypatch)
        out = await qroute.prompt_bundle(PromptBundleRequest(
            session_id="s1", prompt="x", mode="work", sections=["ranked"]))
        assert spies.query.await_count == 1
        assert spies.always_on.await_count == 1
        assert spies.companion.await_count == 0
        assert spies.recall.await_count == 0
        assert out["always_on_block"] == "" and out["ao_meta"] is None
        assert "--add-always-on-rules" not in _update_flags(spies), "the overlap read must not record citations"

    @pytest.mark.asyncio
    async def test_methodology_request_runs_the_companion_only(self, monkeypatch):
        spies = self._fakes(monkeypatch)
        await qroute.prompt_bundle(PromptBundleRequest(
            session_id="s1", prompt="x", mode="work", sections=["methodology"]))
        assert spies.companion.await_count == 1
        assert spies.query.await_count == 0
        assert spies.always_on.await_count == 0
        assert spies.recall.await_count == 0

    @pytest.mark.asyncio
    async def test_recall_request_runs_recall_only(self, monkeypatch):
        spies = self._fakes(monkeypatch)
        await qroute.prompt_bundle(PromptBundleRequest(
            session_id="s1", prompt="x", mode="work", sections=["recall"]))
        assert spies.recall.await_count == 1
        assert spies.query.await_count == 0
        assert spies.always_on.await_count == 0
        assert spies.companion.await_count == 0

    @pytest.mark.asyncio
    async def test_ranked_retrieval_budget_is_capped_at_the_section_budget(self, monkeypatch):
        spies = self._fakes(monkeypatch)
        await qroute.prompt_bundle(PromptBundleRequest(
            session_id="s1", prompt="x", mode="work", sections=["ranked"]))
        assert spies.query.await_args.args[0].budget_tokens == 2300

    @pytest.mark.asyncio
    async def test_ranked_budget_follows_a_smaller_remaining_budget(self, monkeypatch):
        spies = _install(monkeypatch, cache=_cache(remaining_budget=900),
                         query=_oversize_ranked(n=1, statement_len=50))
        await qroute.prompt_bundle(PromptBundleRequest(
            session_id="s1", prompt="x", mode="work", sections=["ranked"]))
        assert spies.query.await_args.args[0].budget_tokens == 900

    @pytest.mark.asyncio
    async def test_methodology_budget_is_the_named_two_thousand_when_budget_remains(self, monkeypatch):
        spies = self._fakes(monkeypatch)
        await qroute.prompt_bundle(PromptBundleRequest(
            session_id="s1", prompt="x", mode="work", sections=["methodology"]))
        assert spies.companion.await_args.args[0].budget_tokens == 2000

    @pytest.mark.asyncio
    async def test_methodology_budget_is_zero_when_the_rule_budget_is_nearly_spent(self, monkeypatch):
        spies = _install(monkeypatch, cache=_cache(remaining_budget=500),
                         companion={"mode": "summary", "rules": []})
        await qroute.prompt_bundle(PromptBundleRequest(
            session_id="s1", prompt="x", mode="work", sections=["methodology"]))
        assert spies.companion.await_args.args[0].budget_tokens == 0

    @pytest.mark.asyncio
    async def test_a_mode_with_no_methodology_source_returns_no_methodology_block(self, monkeypatch):
        spies = self._fakes(monkeypatch)
        out = await qroute.prompt_bundle(PromptBundleRequest(
            session_id="s1", prompt="x", mode="", sections=["methodology"]))
        assert out["methodology_block"] == ""
        assert spies.companion.await_count == 0

    @pytest.mark.asyncio
    async def test_a_suppressed_ranked_channel_keeps_its_sentinel_and_runs_no_retrieval(self, monkeypatch):
        spies = self._fakes(monkeypatch)
        out = await qroute.prompt_bundle(PromptBundleRequest(
            session_id="s1", prompt="x", mode="work", sections=["ranked"], include_ranked=False))
        assert out["broad_meta"] == {"suppressed": True}
        assert out["rules_text"] == ""
        assert spies.query.await_count == 0

    @pytest.mark.asyncio
    async def test_recall_briefs_once_per_session_and_sets_the_flag(self, monkeypatch):
        spies = self._fakes(monkeypatch)
        out = await qroute.prompt_bundle(PromptBundleRequest(
            session_id="s1", prompt="x", mode="work", sections=["recall"]))
        assert out["recall_block"] == "a prior decision"
        assert "--set-recall-briefed" in _update_flags(spies)

    @pytest.mark.asyncio
    async def test_recall_is_silent_once_the_session_was_briefed(self, monkeypatch):
        spies = _install(monkeypatch, cache=_cache(recall_briefed=True), briefing="a prior decision")
        out = await qroute.prompt_bundle(PromptBundleRequest(
            session_id="s1", prompt="x", mode="work", sections=["recall"]))
        assert out["recall_block"] == ""
        assert spies.recall.await_count == 0
        assert "--set-recall-briefed" not in _update_flags(spies)

    @pytest.mark.asyncio
    async def test_recall_sets_the_flag_even_when_there_is_nothing_to_brief(self, monkeypatch):
        spies = _install(monkeypatch, briefing="")
        out = await qroute.prompt_bundle(PromptBundleRequest(
            session_id="s1", prompt="x", mode="work", sections=["recall"]))
        assert out["recall_block"] == ""
        assert "--set-recall-briefed" in _update_flags(spies)

    @pytest.mark.asyncio
    async def test_a_recall_failure_still_returns_a_clean_response(self, monkeypatch):
        spies = _install(monkeypatch)
        spies.recall.side_effect = RuntimeError("decision store unavailable")
        out = await qroute.prompt_bundle(PromptBundleRequest(
            session_id="s1", prompt="x", mode="work", sections=["recall"]))
        assert out["error"] is False
        assert out["recall_block"] == ""

    @pytest.mark.asyncio
    async def test_a_query_error_on_the_ranked_section_is_reported_and_skips_always_on(self, monkeypatch):
        spies = self._fakes(monkeypatch)
        spies.query.return_value = {"error": "channel blew up"}
        out = await qroute.prompt_bundle(PromptBundleRequest(
            session_id="s1", prompt="x", mode="work", sections=["ranked"]))
        assert out["error"] is True
        assert out["rules_text"] == ""
        assert spies.always_on.await_count == 0
        spies.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_always_on_request_records_its_tokens_and_citations_in_one_cache_write(self, monkeypatch):
        spies = self._fakes(monkeypatch)
        await qroute.prompt_bundle(PromptBundleRequest(
            session_id="s1", prompt="x", mode="work", sections=["always_on"]))
        assert spies.update.call_count == 1
        flags = _update_flags(spies)
        assert {"--add-always-on-tokens", "--add-always-on-rules", "--mark-shown"} <= flags


class TestLegacyDefault:
    @pytest.mark.asyncio
    async def test_omitting_sections_returns_the_ranked_always_on_and_methodology_channels(self, monkeypatch):
        spies = _install(
            monkeypatch,
            query=_oversize_ranked(n=3, statement_len=100),
            always_on={"total_tokens": 50, "rules": [_ao_rule(1)]},
            companion={"mode": "summary", "total_tokens": 100,
                       "rules": [{**_rule(1, channel="floor"), "rule_id": "FLOOR-001"}]},
            floor_ids={"FLOOR-001"},
        )
        out = await qroute.prompt_bundle(PromptBundleRequest(session_id="s1", prompt="x", mode="work"))
        assert out["rules_text"] != ""
        assert out["always_on_block"] != ""
        assert out["methodology_block"] != ""
        assert out["recall_block"] == ""
        assert spies.recall.await_count == 0, "recall is opt-in so a legacy caller never spends the briefing"
        assert "--set-recall-briefed" not in _update_flags(spies)

    @pytest.mark.asyncio
    async def test_first_turn_always_on_block_is_byte_identical_to_render_always_on(self, monkeypatch):
        pb = _imp("writ.retrieval.prompt_bundle")
        ao = {"total_tokens": 50, "rules": [_ao_rule(1), _ao_rule(2), _ao_rule(3)]}
        _install(monkeypatch, always_on=ao)
        out = await qroute.prompt_bundle(PromptBundleRequest(session_id="s1", prompt="x", mode="work"))
        expected_block, expected_tokens, _count = pb.render_always_on(ao)
        assert out["always_on_block"] == expected_block
        assert out["ao_meta"]["tokens"] == expected_tokens

    @pytest.mark.asyncio
    async def test_response_carries_the_eight_legacy_keys_plus_the_three_new_ones(self, monkeypatch):
        _install(monkeypatch)
        out = await qroute.prompt_bundle(PromptBundleRequest(session_id="s1", prompt="x"))
        assert set(out) == {
            "always_on_block", "rules_text", "methodology_block", "nudge", "error",
            "broad_meta", "ao_meta", "method_meta",
            "recall_block", "nudge_text", "skipped",
        }
        assert out["skipped"] is False

    @pytest.mark.asyncio
    async def test_the_error_response_shape_gains_only_the_new_keys(self, monkeypatch):
        spies = _install(monkeypatch)
        spies.query.return_value = {"error": "boom"}
        out = await qroute.prompt_bundle(PromptBundleRequest(session_id="s1", prompt="x"))
        assert out == {
            "always_on_block": "", "rules_text": "", "methodology_block": "", "recall_block": "",
            "nudge": "", "nudge_text": "", "error": True, "skipped": False,
            "broad_meta": None, "ao_meta": None, "method_meta": None,
        }

    @pytest.mark.asyncio
    async def test_a_legacy_request_is_never_skipped_even_with_the_budget_exhausted(self, monkeypatch):
        spies = _install(monkeypatch, cache=_cache(remaining_budget=0, context_percent=99),
                         query=_oversize_ranked(n=1, statement_len=50))
        out = await qroute.prompt_bundle(PromptBundleRequest(session_id="s1", prompt="x"))
        assert out["skipped"] is False
        assert spies.query.await_count == 1

    def test_the_request_model_defaults(self):
        req = PromptBundleRequest(session_id="s1")
        assert req.sections is None
        assert req.reserve_chars == 0

    def test_the_request_model_rejects_an_unknown_section_and_a_negative_reserve(self):
        with pytest.raises(Exception):
            PromptBundleRequest(session_id="s1", sections=["documents"])
        with pytest.raises(Exception):
            PromptBundleRequest(session_id="s1", reserve_chars=-1)


class TestExplicitSectionsSkip:
    async def _call(self, monkeypatch, cache, sections=("ranked",)):
        spies = _install(monkeypatch, cache=cache, query=_oversize_ranked(n=1, statement_len=50),
                         always_on={"total_tokens": 5, "rules": [_ao_rule(1)]}, briefing="b")
        out = await qroute.prompt_bundle(PromptBundleRequest(
            session_id="s1", prompt="x", mode="work", sections=list(sections)))
        return out, spies

    @pytest.mark.asyncio
    @pytest.mark.parametrize("cache_kwargs", [
        {"remaining_budget": 0},
        {"context_percent": 75},
        {"context_percent": 99},
    ])
    async def test_a_master_with_no_budget_or_high_context_pressure_is_skipped(self, monkeypatch, cache_kwargs):
        out, spies = await self._call(monkeypatch, _cache(**cache_kwargs))
        assert out["skipped"] is True
        assert out["error"] is False
        assert out["always_on_block"] == out["rules_text"] == out["methodology_block"] == out["recall_block"] == ""
        assert out["broad_meta"] is None and out["ao_meta"] is None and out["method_meta"] is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("section", ["always_on", "ranked", "methodology", "recall"])
    async def test_a_skipped_request_runs_no_retrieval_and_makes_no_cache_write(self, monkeypatch, section):
        out, spies = await self._call(monkeypatch, _cache(remaining_budget=0), sections=(section,))
        assert out["skipped"] is True
        assert spies.query.await_count == 0
        assert spies.always_on.await_count == 0
        assert spies.companion.await_count == 0
        assert spies.recall.await_count == 0
        spies.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_context_pressure_just_below_the_threshold_is_not_skipped(self, monkeypatch):
        out, spies = await self._call(monkeypatch, _cache(context_percent=74))
        assert out["skipped"] is False
        assert spies.query.await_count == 1

    @pytest.mark.asyncio
    async def test_a_sub_agent_is_never_skipped(self, monkeypatch):
        out, spies = await self._call(monkeypatch, _cache(is_subagent=True, remaining_budget=0, context_percent=99))
        assert out["skipped"] is False
        assert spies.query.await_count == 1

    @pytest.mark.asyncio
    async def test_a_skipped_recall_request_does_not_set_the_briefed_flag(self, monkeypatch):
        out, spies = await self._call(monkeypatch, _cache(context_percent=90), sections=("recall",))
        assert out["skipped"] is True
        assert "--set-recall-briefed" not in _update_flags(spies)
