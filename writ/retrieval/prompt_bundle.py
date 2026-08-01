"""Pure render/parse helpers for the /prompt-bundle endpoint (#8).

The per-prompt rag-inject hook used to retrieve + parse + render the three injection
channels (broad /query, /always-on, /methodology-companion) in bash, spawning ~28
cold python3 processes per turn (~420ms of a measured ~646ms). /prompt-bundle moves
that work into the warm daemon: it awaits the existing handlers in-process and uses
these helpers to render, so the hook drops to one curl + emit.

These functions are pure (no IO, no globals) so they unit-test without a server. The
output must stay byte-identical to the prior bash rendering -- the golden-diff in
tests/test_prompt_bundle.py is the regression oracle.
"""
from __future__ import annotations

import json


def _renderable_always_on(ao_json: dict) -> list[dict]:
    """The always-on rules that actually reach the injected block.

    A rule missing its id, trigger, or statement has nothing to render, so it is
    dropped. Both render_always_on and always_on_rule_ids derive from this one filter
    (DRY-DUP-002), so the IDs the session records can never name a rule the agent was
    not shown -- which would reintroduce the citation bug inverted, telling the agent
    it may cite something absent from its context.
    """
    out = []
    for r in ao_json.get("rules") or []:
        rid = r.get("rule_id", "")
        trig = (r.get("trigger") or "").strip()
        stmt = (r.get("statement") or "").strip()
        if not rid or not trig or not stmt:
            continue
        out.append({"rule_id": rid, "trigger": trig, "statement": stmt})
    return out


def render_always_on(ao_json: dict) -> tuple[str, int, int]:
    """Render the always-on bundle. Mirror of the ALWAYS_ON_PARSED heredoc that
    writ-rag-inject.sh used. Returns (block_text, total_tokens, rule_count); block is
    "" when there are no renderable rules.

    rule_count stays len(rules), NOT the renderable count, as in the bash version.
    """
    rules = ao_json.get("rules") or []
    try:
        tokens = int(ao_json.get("total_tokens", 0) or 0)
    except (TypeError, ValueError):
        tokens = 0
    count = len(rules)
    if not rules:
        return "", tokens, count
    lines = ["=== ALWAYS-ACTIVE RULES ==="]
    for r in _renderable_always_on(ao_json):
        lines.append(f"[{r['rule_id']}] WHEN: {r['trigger']}")
        lines.append(f"  {r['statement']}")
    lines.append("=== END ALWAYS-ACTIVE RULES ===")
    return "\n".join(lines), tokens, count


def always_on_rule_ids(ao_json: dict) -> list[str]:
    """The rule IDs injected by the always-on channel, in bundle order.

    The session must record these. Always-on was the only one of the three prompt-bundle
    channels that injected rules without recording them, so the plan gate validated
    citations against a record missing every always-on rule and reported the agent's
    correct citations as hallucinated.
    """
    return [r["rule_id"] for r in _renderable_always_on(ao_json)]


def compute_nudge(query_resp: dict, threshold: float = 0.3) -> str:
    """Low-relevance proposal nudge. Mirror of the PROPOSAL_NUDGE heredoc:
    'NO_RULES' when nothing matched, 'LOW_SCORES' when every match is below the
    threshold, else ''."""
    rules = query_resp.get("rules", [])
    if not rules:
        return "NO_RULES"
    if all((r.get("score", 0) or 0) < threshold for r in rules):
        return "LOW_SCORES"
    return ""


def extract_rule_objects(query_resp: dict) -> list[dict]:
    """The compliance-matching rule fields cached via --add-rule-objects. Mirror of
    common.sh extract_rule_objects (C1)."""
    objects = []
    for r in query_resp.get("rules", []) or []:
        objects.append({
            "rule_id": r.get("rule_id", ""),
            "trigger": r.get("trigger", ""),
            "statement": r.get("statement", ""),
            "violation": r.get("violation", ""),
            "pass_example": r.get("pass_example", ""),
            "enforcement": r.get("enforcement", ""),
            "domain": r.get("domain", ""),
            "severity": r.get("severity", ""),
        })
    return objects


def split_format(raw: str) -> tuple[str, dict]:
    """Split raw cmd_format output ("<text>\\nWRIT_META:{...}") into (text, meta).

    text is stripped (matching the /session/format endpoint the hook used via
    `_writ_session format`); meta is {'rule_ids': [...], 'cost': N} parsed from the
    WRIT_META: line ({} on absence/parse error)."""
    text_lines, meta = [], {"rule_ids": [], "cost": 0}
    for line in (raw or "").splitlines():
        if line.startswith("WRIT_META:"):
            try:
                parsed = json.loads(line[len("WRIT_META:"):])
                meta = {
                    "rule_ids": parsed.get("rule_ids", []),
                    "cost": parsed.get("cost", 0),
                }
            except (ValueError, json.JSONDecodeError):
                pass
        else:
            text_lines.append(line)
    return "\n".join(text_lines).strip(), meta
