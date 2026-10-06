"""Program item 1f: the CLEAN-DEAD-001 and CLEAN-RETURN-001 sibling collision.

Q193 (code after a return that can never run, imports nobody references) expects
CLEAN-DEAD-001 and lost it to CLEAN-RETURN-001 on the word "return"
(benchmarks/MISS-TRIAGE-2026-08-05.md). The fix is rule text, in both places it lives: the
tracked dump writ-corpus.cypher (what install, CI and the isolated graph load) and the
phase 2A seed script (which upserts the same rules and would revert the wording on a
re-run). Whether the wording helps is measured, not asserted here:
benchmarks/CLEAN-REWORD-2026-10-06.md.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from writ.graph.dump import cypher_literal

REPO = Path(__file__).resolve().parent.parent
DUMP = REPO / "writ-corpus.cypher"
SEED = REPO / "scripts" / "seed_phase_2a_clean_dry.py"
RULE_IDS = ("CLEAN-DEAD-001", "CLEAN-RETURN-001")
RULE_ID_MENTION = re.compile(r"\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-\d{3}\b")


def _dump_line(rule_id: str) -> str:
    lines = [
        line for line in DUMP.read_text().splitlines()
        if line.startswith("CREATE (:Rule ") and f"rule_id: '{rule_id}'," in line
    ]
    assert len(lines) == 1, f"{rule_id}: expected one Rule line in {DUMP.name}, found {len(lines)}"
    return lines[0]


def _dump_text(rule_id: str, key: str) -> str:
    match = re.search(rf"\b{key}: '((?:[^'\\]|\\.)*)'", _dump_line(rule_id))
    assert match, f"{rule_id}: no {key} in {DUMP.name}"
    return match.group(1)


@pytest.fixture(scope="module")
def seed_rules() -> dict[str, dict]:
    spec = importlib.util.spec_from_file_location("seed_phase_2a_under_test", SEED)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return {r["rule_id"]: r for r in mod.RULES}


class TestReturnRuleTrigger:
    def test_trigger_does_not_claim_the_word_return(self, seed_rules):
        for text in (_dump_text("CLEAN-RETURN-001", "trigger"),
                     seed_rules["CLEAN-RETURN-001"]["trigger"]):
            assert not re.search(r"\breturn", text, re.IGNORECASE), text

    def test_trigger_names_the_sentinel_case(self, seed_rules):
        for text in (_dump_text("CLEAN-RETURN-001", "trigger"),
                     seed_rules["CLEAN-RETURN-001"]["trigger"]):
            assert "sentinel" in text.lower(), text


class TestDeadCodeRuleVocabulary:
    @pytest.mark.parametrize(
        "phrase", ["unreachable", "after a return", "unused imports", "unused variables"],
    )
    def test_trigger_and_statement_carry_the_vocabulary(self, seed_rules, phrase):
        dump = f"{_dump_text('CLEAN-DEAD-001', 'trigger')} {_dump_text('CLEAN-DEAD-001', 'statement')}"
        seed = f"{seed_rules['CLEAN-DEAD-001']['trigger']} {seed_rules['CLEAN-DEAD-001']['statement']}"
        assert phrase in dump.lower()
        assert phrase in seed.lower()


class TestOneWording:
    @pytest.mark.parametrize("rule_id", RULE_IDS)
    @pytest.mark.parametrize("key", ["trigger", "statement"])
    def test_seed_and_dump_agree(self, seed_rules, rule_id, key):
        assert f"{key}: {cypher_literal(seed_rules[rule_id][key])}," in _dump_line(rule_id)

    @pytest.mark.parametrize("rule_id", RULE_IDS)
    def test_text_names_no_rule_id(self, seed_rules, rule_id):
        text = f"{seed_rules[rule_id]['trigger']} {seed_rules[rule_id]['statement']}"
        assert not RULE_ID_MENTION.search(text), text
