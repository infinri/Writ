"""The one-line rule pointer every collapsed render uses (always-on block, methodology floor),
and the one word-boundary clip and display-title helper (recall cards, the pre-write card, the
harvester title), plus the one sanitizer, block fence and similarity spelling for retrieved
text (docs/adr/ADR-retrieved-text-fence.md).

Lowest layer on purpose: writ.retrieval and writ.session both render it and neither may
import the other.
"""
import re

# A decision's title (harvested and displayed): the first sentence of its rationale, clipped.
TITLE_CHARS = 100

_SENTENCE_END = re.compile(r"\.(?:\s|$)|(?<=[?!])\s")

_LINE_BREAK = re.compile("\r\n?|[\x85\u2028\u2029]")
# Unicode Cc and Cf minus "\n" and "\t", written out (unicodedata 15.0.0).
_INVISIBLE = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f\xad\u0600-\u0605\u061c\u06dd\u070f\u0890-\u0891\u08e2"
    "\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f\ufeff\ufff9-\ufffb"
    "\U000110bd\U000110cd\U00013430-\U0001343f\U0001bca0-\U0001bca3\U0001d173-\U0001d17a"
    "\U000e0001\U000e0020-\U000e007f]"
)
_MARKER_LINE = re.compile(
    r"^([^\S\n]*)((?:---|===)[^\S\n]*(?:END[^\S\n]+)?(?:WRIT|ALWAYS-ACTIVE|APPLICABLE RULES)|WRIT_META:)",
    re.IGNORECASE | re.MULTILINE,
)
_LABEL = "[A-Z][A-Z -]*"
_FENCE_CLOSE = re.compile(f"--- END WRIT {_LABEL} ---")


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


def sanitize_retrieved(text) -> str:
    if not text:
        return ""
    text = _INVISIBLE.sub("", _LINE_BREAK.sub("\n", text))
    return _MARKER_LINE.sub(r"\1\\\2", text)


def fence(label: str, body: str, detail: str = "") -> str:
    if not re.fullmatch(_LABEL, label):
        raise ValueError(f"fence label {label!r} must be upper-case letters, spaces or hyphens")
    detail = " ".join(sanitize_retrieved(detail).split())
    head = f"--- WRIT {label} ({detail}) ---" if detail else f"--- WRIT {label} ---"
    return f"{head}\n{sanitize_retrieved(body)}\n--- END WRIT {label} ---"


def is_fence_close(line: str) -> bool:
    return _FENCE_CLOSE.fullmatch(line) is not None


def similarity_slot(entry) -> str:
    if "similarity" not in entry:
        return ""
    value = entry["similarity"]
    return " sim=n/a" if value is None else f" sim={value:.3f}"
