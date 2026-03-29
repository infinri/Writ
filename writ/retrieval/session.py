"""Client-side session context tracker for multi-query retrieval.

Per handbook Section 7.4: "Writ is stateless per request. Session state is managed
by the skill (client side), not the server."

This module is a helper for skill integration. It accumulates loaded_rule_ids
and tracks remaining budget across sequential queries. It does not make HTTP
calls -- it builds request payloads and processes response dicts.
"""

from __future__ import annotations

# Per ARCH-CONST-001
DEFAULT_SESSION_BUDGET = 8000
APPROX_TOKENS_PER_RULE_FULL = 200
APPROX_TOKENS_PER_RULE_STANDARD = 120
APPROX_TOKENS_PER_RULE_SUMMARY = 40


class SessionTracker:
    """Tracks session state across sequential queries.

    Per ARCH-ORG-001: client-side helper, not server state.
    Per ARCH-TYPE-001: all public methods typed.
    """

    def __init__(self, initial_budget: int = DEFAULT_SESSION_BUDGET) -> None:
        self._loaded_rule_ids: set[str] = set()
        self._initial_budget = initial_budget
        self._remaining_budget = initial_budget

    @property
    def loaded_rule_ids(self) -> list[str]:
        """Currently loaded rule IDs (read-only, sorted for determinism)."""
        return sorted(self._loaded_rule_ids)

    @property
    def remaining_budget(self) -> int:
        """Remaining token budget for this session."""
        return self._remaining_budget

    def next_query(self, query_text: str, domain: str | None = None) -> dict:
        """Build a query request payload with accumulated session state.

        Returns a dict suitable for POST /query.
        """
        payload: dict = {
            "query": query_text,
            "budget_tokens": self._remaining_budget,
            "loaded_rule_ids": self.loaded_rule_ids,
        }
        if domain is not None:
            payload["domain"] = domain
        return payload

    def load_results(self, response: dict) -> None:
        """Update session state from a /query response.

        Extracts rule_ids from returned rules and decrements budget.
        """
        rules = response.get("rules", [])
        mode = response.get("mode", "standard")

        for rule in rules:
            rid = rule.get("rule_id")
            if rid:
                self._loaded_rule_ids.add(rid)
            # Abstraction summaries contain member rule_ids.
            for member_id in rule.get("rule_ids", []):
                self._loaded_rule_ids.add(member_id)

        cost = _estimate_token_cost(rules, mode)
        self._remaining_budget = max(0, self._remaining_budget - cost)

    def reset(self) -> None:
        """Reset session to initial state."""
        self._loaded_rule_ids.clear()
        self._remaining_budget = self._initial_budget


def _estimate_token_cost(rules: list[dict], mode: str) -> int:
    """Estimate token cost of returned rules based on mode."""
    if mode == "full":
        return len(rules) * APPROX_TOKENS_PER_RULE_FULL
    elif mode == "standard":
        return len(rules) * APPROX_TOKENS_PER_RULE_STANDARD
    else:
        return len(rules) * APPROX_TOKENS_PER_RULE_SUMMARY
