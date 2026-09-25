"""Methodology-trigger index (WRIT-BLUEPRINT 1.6).

The matching substrate for CHANNEL 2 (methodology by workflow-state), separate
from rule retrieval. Methodology is NOT ranked by embedding similarity -- it is
matched deterministically by node-declared routing data:

    match(mode, prompt, action?) =
          floor(mode)        # mode in node.floor_modes        (push-by-mode)
        u push(action)       # action in node.action_triggers  (push-by-action, 1.8)
        u pull(prompt)       # any node.trigger_keyword whole-word in prompt  (pull)

The set is deduped by id (the index is FLAT -- there is no depth-2 BFS, so a node
reached "by multiple paths" cannot duplicate; this is graph add-on #3,
path-redundancy pruning, for free), ordered floor-first, and budget-capped:
floor+push are obligations (never dropped; over-budget is flagged loud), pull is
the flexible tail (lowest keyword-match-count dropped first).

Reaching for embeddings here would collapse the rule/methodology distinction the
hybrid model rests on -- so matching is purely structural.
"""

from __future__ import annotations

import re

from writ.shared.tokens import estimate_tokens

# Retrievable methodology node types eligible for push/pull injection. Rules and
# ForbiddenResponse stay in CHANNEL 1 (/always-on + /query), per D1.
RETRIEVABLE_METHODOLOGY_LABELS = ("Skill", "Playbook", "Technique", "AntiPattern")

# Summary-render token estimate (the shared /always-on heuristic).
def _est_tokens(node: dict) -> int:
    return estimate_tokens(node.get("trigger"), node.get("statement"))


def _keyword_matches(keyword: str, prompt: str) -> bool:
    """A curated keyword matches if it appears as a case-insensitive WHOLE word
    (or phrase) in the prompt -- deterministic, not similarity (D2)."""
    kw = (keyword or "").strip()
    if not kw:
        return False
    return re.search(rf"\b{re.escape(kw)}\b", prompt, re.IGNORECASE) is not None


class MethodologyTriggerIndex:
    """In-memory index over node-declared routing data, built once at startup.

    Each node record is a dict carrying at least: id, node_type, floor_modes,
    action_triggers, trigger_keywords, trigger, statement, severity.
    """

    def __init__(self, nodes: list[dict]) -> None:
        self._nodes = nodes

    @classmethod
    async def build_from_db(cls, db) -> "MethodologyTriggerIndex":
        """Load retrievable methodology nodes + their routing data from Neo4j."""
        nodes: list[dict] = []
        # Reuse the canonical label->id-field registry (NODE_ID_FIELDS) rather than a
        # local copy, so a new retrievable label resolves automatically.
        from writ.graph.schema import NODE_ID_FIELDS
        id_fields = {label: NODE_ID_FIELDS[label] for label in RETRIEVABLE_METHODOLOGY_LABELS}
        async with db._driver.session(database=db._database) as session:
            for label in RETRIEVABLE_METHODOLOGY_LABELS:
                id_field = id_fields[label]
                result = await session.run(
                    f"MATCH (n:{label}) RETURN n.{id_field} AS id, "
                    "n.floor_modes AS floor_modes, n.action_triggers AS action_triggers, "
                    "n.trigger_keywords AS trigger_keywords, n.trigger AS trigger, "
                    "n.statement AS statement, n.severity AS severity, n.domain AS domain"
                )
                async for r in result:
                    if r["id"] is None:
                        continue
                    nodes.append({
                        "id": r["id"],
                        "node_type": label,
                        "floor_modes": r["floor_modes"] or [],
                        "action_triggers": r["action_triggers"] or [],
                        "trigger_keywords": r["trigger_keywords"] or [],
                        "trigger": r["trigger"] or "",
                        "statement": r["statement"] or "",
                        "severity": r["severity"],
                        "domain": r["domain"],
                    })
        return cls(nodes)

    def match(
        self,
        mode: str | None,
        prompt: str = "",
        action: str | None = None,
        budget_tokens: int = 5000,
        exclude_ids: set[str] | list[str] | None = None,
    ) -> dict:
        """Return the methodology bundle for this workflow state.

        Response: {"nodes": [...each tagged with "channel"...], "total_tokens": N,
        "over_budget": bool}. floor+push are always present (over_budget=True if
        they alone exceed the cap -- a loud signal, never a silent drop); pull
        fills the remaining budget, lowest match-count dropped first.
        `exclude_ids` drops nodes already injected this turn (so they neither
        re-inject nor consume budget).
        """
        exclude = set(exclude_ids or ())
        nodes = [n for n in self._nodes if n["id"] not in exclude]
        floor = self._select_floor(nodes, mode)
        push = self._select_push(action)
        pull_scored = self._score_pull(nodes, prompt)
        return self._assemble_bundle(floor, push, pull_scored, budget_tokens)

    def floor_ids(self, mode: str | None) -> set[str]:
        """Ids of every node floored in `mode`, ignoring any exclude list."""
        return {n["id"] for n in self._select_floor(self._nodes, mode)}

    @staticmethod
    def _select_floor(nodes: list[dict], mode: str | None) -> list[dict]:
        """floor: nodes whose floor_modes contain the current mode (push-by-mode)."""
        return [n for n in nodes if mode and mode in (n.get("floor_modes") or [])]

    def _select_push(self, action: str | None) -> list[dict]:
        """push (D-A): nodes whose action_triggers contain `action`, selected from the
        UNFILTERED node set so the action's node lands AT the action moment even if it
        was already injected this turn (and so sits in exclude_ids). Floor/pull stay
        exclude-filtered -- only push bypasses, because timing IS the value of
        push-by-action. floor-first dedup still wins when a node is both."""
        return [
            n for n in self._nodes
            if action and action in (n.get("action_triggers") or [])
        ]

    @staticmethod
    def _score_pull(nodes: list[dict], prompt: str) -> list[tuple[int, dict]]:
        """pull: keyword match-count per node, ordered desc (id tiebreak for determinism)."""
        pull_scored: list[tuple[int, dict]] = []
        for n in nodes:
            count = sum(
                1 for kw in (n.get("trigger_keywords") or []) if _keyword_matches(kw, prompt)
            )
            if count:
                pull_scored.append((count, n))
        pull_scored.sort(key=lambda t: (-t[0], t[1]["id"]))
        return pull_scored

    @staticmethod
    def _assemble_bundle(floor: list[dict], push: list[dict],
                         pull_scored: list[tuple[int, dict]], budget_tokens: int) -> dict:
        """Dedup floor-first, then push (obligations, never dropped), then fill the
        remaining budget with pull (lowest match-count dropped first). over_budget is
        True only if the obligations alone exceed the cap (a loud signal, never silent)."""
        seen: set[str] = set()
        out: list[dict] = []

        def _add(node: dict, channel: str) -> None:
            if node["id"] in seen:
                return
            seen.add(node["id"])
            out.append({**node, "channel": channel})

        # Obligations first: floor (floor-first), then push. Never dropped.
        for n in floor:
            _add(n, "floor")
        for n in push:
            _add(n, "push")
        mandatory_tokens = sum(_est_tokens(n) for n in out)

        # Pull fills the remaining budget; lowest match-count dropped first.
        total = mandatory_tokens
        for _count, n in pull_scored:
            if n["id"] in seen:
                continue
            cost = _est_tokens(n)
            if total + cost > budget_tokens:
                continue
            _add(n, "pull")
            total += cost

        return {
            "nodes": out,
            "total_tokens": total,
            "over_budget": mandatory_tokens > budget_tokens,
        }
