"""Shared token-estimate heuristics (RW3 + DUP-S4 consolidation).

The char-based estimate for a rule's trigger+statement (~4 chars/token) was
inlined identically at three sites: the /always-on summary render (server),
the always-on budget-breach check (integrity), and the trigger-index summary
estimate. They are coupled by design (the budget accounting only balances if
all three agree), so the heuristic must have one definition. stdlib only;
lowest layer so retrieval, graph, and server can all import it.

cost_for() is the by-mode per-rule budget cost: it was duplicated byte-for-byte
as retrieval.session._estimate_token_cost and session.budget_tracking._estimate_cost.
Both encode the same policy (cost = rule_count x per-mode rate), so a rate change
must hit one place. The rates load from the canonical shared/budget.json sibling.
"""

import json
from pathlib import Path

_BUDGET_JSON = Path(__file__).resolve().parent / "budget.json"
_budget_data = json.loads(_BUDGET_JSON.read_text())
_RULE_COST_BY_MODE = {
    "full": _budget_data["rule_cost_full"],
    "standard": _budget_data["rule_cost_standard"],
}
_RULE_COST_DEFAULT = _budget_data["rule_cost_summary"]

# The per-prompt injection ceilings (program item 1a, docs/adr/ADR-prompt-injection-split.md).
# The host caps each hook command's injected text at 10,000 characters, so every
# UserPromptSubmit injection hook stays under PROMPT_CHAR_CEILING; the section token budgets
# plus the reserved budgets sum to PROMPT_TOTAL_TOKENS.
CHARS_PER_TOKEN = 4
PROMPT_CHAR_CEILING: int = int(_budget_data["prompt_char_ceiling"])
PROMPT_TOTAL_TOKENS: int = int(_budget_data["prompt_total_tokens"])
PROMPT_SECTION_TOKENS: dict[str, int] = {
    k: int(v) for k, v in _budget_data["prompt_section_tokens"].items()
}
PROMPT_RESERVED_TOKENS: dict[str, int] = {
    k: int(v) for k, v in _budget_data["prompt_reserved_tokens"].items()
}


def estimate_tokens(trigger: str | None, statement: str | None) -> int:
    """Approximate token count of a rule's trigger + statement (4 chars/token)."""
    return (len((trigger or "").strip()) + len((statement or "").strip())) // 4


def cost_for(rules: list[dict], mode: str) -> int:
    """Estimated token cost of `rules` at retrieval `mode`.

    full/standard use their per-rule rate; any other mode falls back to the
    summary rate (matches the prior _estimate_cost / _estimate_token_cost else-branch).
    """
    return len(rules) * _RULE_COST_BY_MODE.get(mode, _RULE_COST_DEFAULT)
