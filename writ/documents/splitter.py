"""split_markdown: one markdown file to a title and an ordered list of chunk drafts.

Pure: no graph, no filesystem. ATX H1 to H3 open sections outside fenced code, every chunk
carries the breadcrumb of its enclosing headings, whole paragraphs are packed up to
CHUNK_MAX_TOKENS without crossing a section boundary (docs/adr/ADR-document-retrieval.md).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from writ.shared.injection_text import clip
from writ.shared.tokens import CHARS_PER_TOKEN

CHUNK_MAX_TOKENS = 300
CHUNK_HEADER_CHARS = 120
_MAX_CHARS = CHUNK_MAX_TOKENS * CHARS_PER_TOKEN

_HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")
_CLOSING_HASHES = re.compile(r"(?:^|[ \t]+)#+$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_FRONT_MATTER_TITLE = re.compile(r"^title:\s*(.*?)\s*$")


@dataclass(frozen=True)
class ChunkDraft:
    ordinal: int
    breadcrumb: str
    text: str
    est_tokens: int


@dataclass(frozen=True)
class SplitResult:
    title: str
    chunks: list[ChunkDraft]


def _strip_front_matter(lines: list[str]) -> tuple[list[str], str]:
    if not lines or lines[0].strip() != "---":
        return lines, ""
    for end in range(1, len(lines)):
        if lines[end].strip() in ("---", "..."):
            title = ""
            for line in lines[1:end]:
                match = _FRONT_MATTER_TITLE.match(line)
                if match:
                    title = match.group(1).strip("'\"").strip()
                    break
            return lines[end + 1:], title
    return lines, ""


def _sections(lines: list[str]) -> list[tuple[list[str | None], list[str]]]:
    """(heading stack [h1, h2, h3], paragraphs) per section, in order."""
    stack: list[str | None] = [None, None, None]
    sections: list[tuple[list[str | None], list[str]]] = [(list(stack), [])]
    paragraph: list[str] = []
    fence = ""

    def close_paragraph() -> None:
        if paragraph:
            sections[-1][1].append("\n".join(paragraph))
            paragraph.clear()

    for raw in lines:
        line = raw.rstrip()
        if fence:
            paragraph.append(line)
            stripped = line.strip()
            if stripped.startswith(fence) and set(stripped) == {fence[0]}:
                fence = ""
            continue
        opened = _FENCE.match(line)
        if opened:
            fence = opened.group(1)
            paragraph.append(line)
            continue
        heading = _HEADING.match(line)
        if heading and len(heading.group(1)) <= 3:
            close_paragraph()
            level = len(heading.group(1))
            stack[level - 1] = _CLOSING_HASHES.sub("", heading.group(2) or "").strip()
            for deeper in range(level, 3):
                stack[deeper] = None
            sections.append((list(stack), []))
            continue
        if not line.strip():
            close_paragraph()
            continue
        paragraph.append(line)
    close_paragraph()
    return sections


def _split_line(line: str) -> list[str]:
    pieces: list[str] = []
    while len(line) > _MAX_CHARS:
        cut = line.rfind(" ", 0, _MAX_CHARS + 1)
        if cut <= 0:
            pieces.append(line[:_MAX_CHARS])
            line = line[_MAX_CHARS:]
        else:
            pieces.append(line[:cut])
            line = line[cut + 1:]
    if line:
        pieces.append(line)
    return pieces


def _pack(units: list[str], joiner: str) -> list[str]:
    packed: list[str] = []
    current = ""
    for unit in units:
        if current and len(current) + len(joiner) + len(unit) <= _MAX_CHARS:
            current += joiner + unit
        else:
            if current:
                packed.append(current)
            current = unit
    if current:
        packed.append(current)
    return packed


def _fit(paragraph: str) -> list[str]:
    if len(paragraph) <= _MAX_CHARS:
        return [paragraph]
    lines = [piece for line in paragraph.split("\n") for piece in _split_line(line)]
    return _pack(lines, "\n")


def split_markdown(text: str, *, fallback_title: str) -> SplitResult:
    lines, front_title = _strip_front_matter((text or "").splitlines())
    sections = _sections(lines)
    first_h1 = next((stack[0] for stack, _ in sections if stack[0]), "")
    title = first_h1 or front_title or fallback_title
    chunks: list[ChunkDraft] = []
    for stack, paragraphs in sections:
        names = [name for name in stack if name]
        if not stack[0]:
            names.insert(0, title)
        breadcrumb = clip(" > ".join(names), CHUNK_HEADER_CHARS)
        units = [piece for paragraph in paragraphs for piece in _fit(paragraph)]
        for body in _pack(units, "\n\n"):
            chunks.append(ChunkDraft(len(chunks), breadcrumb, body, len(body) // CHARS_PER_TOKEN))
    return SplitResult(title, chunks)
