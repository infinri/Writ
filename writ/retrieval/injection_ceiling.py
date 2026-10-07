"""Character ceilings for the per-prompt injection sections, and the degrade ladders that
keep each one under its ceiling. Pure: no IO, no server state.

The host caps each hook command's injected text at 10,000 characters and replaces anything
longer with a short preview, so every section a hook prints is rendered here to fit
PROMPT_CHAR_CEILING together with the framing the hook adds around it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Protocol

from writ.retrieval.prompt_bundle import _renderable_always_on, split_format
from writ.shared.injection_text import always_on_head_line, is_fence_close, pointer_line
from writ.shared.tokens import CHARS_PER_TOKEN, PROMPT_CHAR_CEILING, PROMPT_SECTION_TOKENS

# A section hook prints "\n" + block + "\n"; the ranked hook prints block + "\n".
SECTION_FRAMING_CHARS = 2
TRUNCATION_MARKER = "[Writ: section truncated at the character ceiling]"
ALWAYS_ON_HEADER = "=== ALWAYS-ACTIVE RULES ==="
ALWAYS_ON_FOOTER = "=== END ALWAYS-ACTIVE RULES ==="
ALWAYS_ON_COLLAPSED_NOTE = (
    "(a rule shown as one WHEN line was given in full earlier this session)"
)
METHODOLOGY_HEADER = "[Writ: methodology companion]"
_EXAMPLE_FIELDS = ("violation", "pass_example")

# Moved verbatim from writ-rag-inject.sh step 10 so the ranked ceiling counts them.
NUDGE_TEXT = {
    "NO_RULES": (
        "[Writ: no matching rules found for this task. If you discover a pattern, "
        "constraint, or gotcha during this work that would help future tasks, propose it "
        "via POST /propose. See HANDBOOK.md for the format and trigger conditions. "
        "No WRIT RULES block follows: no rule matched this prompt beyond those already shown.]"
    ),
    "LOW_SCORES": (
        "[Writ: retrieved rules have low relevance scores (< 0.3). The knowledge base may "
        "not cover this area well. If you discover a pattern worth codifying, propose it "
        "via POST /propose.]"
    ),
}


class RuleRenderer(Protocol):
    """The formatter seam: a /query-shaped payload in, cmd_format's raw output out."""

    def __call__(self, payload: dict) -> str: ...


@dataclass(frozen=True)
class AlwaysOnRender:
    text: str
    rule_ids: list[str]   # every rule present in the block, full or pointer
    full_ids: list[str]   # the subset rendered in full (what --mark-shown records)
    tokens: int


def section_char_limit(section: str) -> int:
    """The longest block a section may return: the per-hook character ceiling or the
    section's token budget in characters, whichever is smaller, minus hook framing."""
    budget_chars = PROMPT_SECTION_TOKENS[section] * CHARS_PER_TOKEN
    return min(PROMPT_CHAR_CEILING, budget_chars) - SECTION_FRAMING_CHARS


def ranked_char_limit(reserve_chars: int, nudge_text: str) -> int:
    """Room left for rules_text in the ranked hook after its control text (reserve_chars,
    measured by the hook), the nudge ("\\n" + text + "\\n") and rules_text's own newline."""
    nudge_cost = len(nudge_text) + 2 if nudge_text else 0
    room = PROMPT_CHAR_CEILING - max(0, int(reserve_chars)) - nudge_cost - 1
    return min(section_char_limit("ranked"), room)


def clamp_lines(text: str, limit: int) -> str:
    """Whole lines of `text` that fit `limit`, plus the truncation marker when cut."""
    if len(text) <= limit:
        return text
    if limit <= len(TRUNCATION_MARKER):
        return ""
    room = limit - len(TRUNCATION_MARKER) - 1
    kept: list[str] = []
    used = 0
    for line in text.split("\n"):
        cost = len(line) + (1 if kept else 0)
        if used + cost > room:
            break
        kept.append(line)
        used += cost
    kept.append(TRUNCATION_MARKER)
    return "\n".join(kept)


def clamp_fenced(text: str, limit: int) -> str:
    """clamp_lines for a fenced block: a cut block keeps its close line, with the truncation
    marker inside it. Text whose last line is not a fence close is clamped by clamp_lines."""
    if len(text) <= limit:
        return text
    head, _, close = text.rpartition("\n")
    if not head or not is_fence_close(close):
        return clamp_lines(text, limit)
    cut = clamp_lines(head, limit - len(close) - 1)
    if cut in ("", TRUNCATION_MARKER):
        return ""
    return f"{cut}\n{close}"


def _total_tokens(ao_json: dict) -> int:
    try:
        return int(ao_json.get("total_tokens", 0) or 0)
    except (TypeError, ValueError):
        return 0


def render_always_on_section(ao_json: dict, shown: set[str], limit: int) -> AlwaysOnRender:
    """The ALWAYS-ACTIVE block with shown rules collapsed to one pointer line each.

    With nothing shown and nothing over the limit the text is byte-identical to
    prompt_bundle.render_always_on. Over the limit: full rules collapse to pointers from
    the tail first, then rules drop from the tail behind an "omitted" line.
    """
    rules = _renderable_always_on(ao_json)
    if not rules:
        return AlwaysOnRender("", [], [], 0)
    full = {r["rule_id"] for r in rules if r["rule_id"] not in shown}
    kept = list(rules)

    def build() -> str:
        lines = [ALWAYS_ON_HEADER]
        if any(r["rule_id"] not in full for r in kept):
            lines.append(ALWAYS_ON_COLLAPSED_NOTE)
        for r in kept:
            if r["rule_id"] in full:
                lines.append(always_on_head_line(r["rule_id"], r["trigger"], r["tags"]))
                lines.append(f"  {r['statement']}")
            else:
                lines.append(pointer_line(r["rule_id"], r["trigger"]))
        omitted = len(rules) - len(kept)
        if omitted:
            lines.append(
                f"[Writ: {omitted} always-active rule(s) omitted at the character ceiling]"
            )
        lines.append(ALWAYS_ON_FOOTER)
        return "\n".join(lines)

    text = build()
    for r in reversed(rules):
        if len(text) <= limit:
            break
        if r["rule_id"] in full:
            full.discard(r["rule_id"])
            text = build()
    while len(text) > limit and kept:
        kept.pop()
        text = build()
    if len(text) > limit:
        return AlwaysOnRender("", [], [], 0)
    rule_ids = [r["rule_id"] for r in kept]
    full_ids = [i for i in rule_ids if i in full]
    tokens = (
        _total_tokens(ao_json) if len(full_ids) == len(rules)
        else len(text) // CHARS_PER_TOKEN
    )
    return AlwaysOnRender(text, rule_ids, full_ids, tokens)


def _ranked_ladder(payload: dict) -> Iterator[dict]:
    """Detail before rules: as retrieved; examples stripped; full -> standard; summary;
    then summary with rules dropped from the tail (lowest rank first)."""
    rules = [dict(r) for r in payload.get("rules") or []]
    mode = payload.get("mode", "standard")
    yield payload
    stripped = [{k: v for k, v in r.items() if k not in _EXAMPLE_FIELDS} for r in rules]
    if stripped != rules:
        yield {**payload, "rules": stripped}
    if mode == "full":
        yield {**payload, "rules": stripped, "mode": "standard"}
    if mode != "summary":
        yield {**payload, "rules": stripped, "mode": "summary"}
    for n in range(len(stripped) - 1, -1, -1):
        yield {**payload, "rules": stripped[:n], "mode": "summary"}


def fit_ranked(payload: dict, render: RuleRenderer, limit: int) -> tuple[str, dict, dict]:
    """(text, meta, payload actually rendered) for the first ladder step that fits."""
    for candidate in _ranked_ladder(payload):
        text, meta = split_format(render(candidate))
        if len(text) <= limit:
            return text, meta, candidate
    return "", {"rule_ids": [], "cost": 0}, {**payload, "rules": []}


def collapse_floor(rules: list[dict], shown: set[str]) -> list[dict]:
    """Copies of the companion rules with every already-shown FLOOR rule marked
    pointer_only. Push and pull are untouched (pull keeps its own session dedup)."""
    return [
        {**r, "pointer_only": True}
        if r.get("channel") == "floor" and r.get("rule_id") in shown else dict(r)
        for r in rules
    ]


def _methodology_ladder(payload: dict) -> Iterator[dict]:
    """Pull before obligations: as given; pull dropped from the tail; remaining rules
    collapsed to pointers from the tail; then rules dropped from the tail."""
    current = [dict(r) for r in payload.get("rules") or []]
    yield {**payload, "rules": list(current)}
    while any(r.get("channel") == "pull" for r in current):
        last = max(i for i, r in enumerate(current) if r.get("channel") == "pull")
        current = current[:last] + current[last + 1:]
        yield {**payload, "rules": list(current)}
    for i in range(len(current) - 1, -1, -1):
        if not current[i].get("pointer_only"):
            current[i] = {**current[i], "pointer_only": True}
            yield {**payload, "rules": list(current)}
    while current:
        current = current[:-1]
        yield {**payload, "rules": list(current)}


def fit_methodology(payload: dict, render: RuleRenderer, limit: int) -> tuple[str, dict, dict]:
    """(block with METHODOLOGY_HEADER, meta, payload rendered) under `limit`."""
    body_limit = limit - len(METHODOLOGY_HEADER) - 1
    for candidate in _methodology_ladder(payload):
        text, meta = split_format(render(candidate))
        if not text:
            return "", meta, candidate
        if len(text) <= body_limit:
            return f"{METHODOLOGY_HEADER}\n{text}", meta, candidate
    return "", {"rule_ids": [], "cost": 0}, {**payload, "rules": []}
