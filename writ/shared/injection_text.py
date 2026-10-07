"""The one-line rule pointer every collapsed render uses (always-on block, methodology floor),
and the one word-boundary clip and display-title helper (recall cards, the pre-write card, the
harvester title).

Lowest layer on purpose: writ.retrieval and writ.session both render it and neither may
import the other.
"""
import re

# A decision's title (harvested and displayed): the first sentence of its rationale, clipped.
TITLE_CHARS = 100

_SENTENCE_END = re.compile(r"\.(?:\s|$)|(?<=[?!])\s")


def pointer_line(rule_id: str, trigger: str) -> str:
    return f"[{rule_id}] WHEN: {(trigger or '').strip()}"


def always_on_head_line(rule_id: str, trigger: str, tags) -> str:
    tag_slot = f" ({', '.join(tags)})" if tags else ""
    return f"[{rule_id}]{tag_slot} WHEN: {(trigger or '').strip()}"


def clip(text, limit: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit - 2)
    if cut <= 0:
        cut = limit - 3
    return text[:cut] + "..."


def first_sentence(text, limit: int) -> str:
    for line in (text or "").splitlines():
        line = line.strip().lstrip("#-* ").strip()
        if line:
            return clip(_SENTENCE_END.split(line, 1)[0], limit)
    return ""
