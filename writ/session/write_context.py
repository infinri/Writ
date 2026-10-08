"""The context one allowed write receives: the decision card, the open questions that bear on
it and the files it usually changes with, fetched concurrently in one coroutine. Each block runs
under its own timeout and fails alone (program items 7a and 7d,
docs/adr/ADR-open-questions-and-co-change.md).
"""
from __future__ import annotations

import asyncio
import fnmatch
from typing import NamedTuple

from writ.session.recall import _sentence, write_decision_context
from writ.shared.injection_text import clip, sanitize_retrieved

COCHANGE_MAX_COMMIT_FILES = 20
COCHANGE_RECENT_COMMITS = 200
COCHANGE_MIN_SUPPORT = 2
COCHANGE_MIN_CONFIDENCE = 0.4
COCHANGE_SCAN_LIMIT = 20
# fnmatch patterns, matched against the path and its basename. "*.lock" covers composer,
# Gemfile, Cargo, poetry and uv lockfiles.
COCHANGE_EXCLUDED = (
    "*.lock", "package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock",
    "go.sum", "*.min.js", "*.min.css", "*.map", "*_pb2.py", "*_pb2_grpc.py", "*.pb.go",
    "*.generated.*", "dist/*", "build/*", "*/dist/*", "*/build/*", "__generated__/*",
    "*/__generated__/*", "vendor/*", "*/vendor/*", "node_modules/*", "*/node_modules/*",
)

WRITE_BLOCKS = {
    "pre_write_decision": "pre_write_decision_failed",
    "pre_write_questions": "pre_write_questions_failed",
    "pre_write_cochange": "pre_write_cochange_failed",
}

_QUESTION_LINES = 3
_QUESTION_LINE_CHARS = 300
_QUESTION_CHARS = 160
_WHO_CHARS = 60
_SETTLED_CHARS = 100
_ABOUT_CHARS = 80
_COCHANGE_LINES = 3
_COCHANGE_LINE_CHARS = 200
_COCHANGE_PATH_CHARS = 60


class WriteContext(NamedTuple):
    text: str
    marks: dict[str, list[str]]
    errors: dict[str, BaseException]


def is_cochange_noise(path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatchcase(path, p) or fnmatch.fnmatchcase(base, p) for p in COCHANGE_EXCLUDED)


def _question_line(row: dict) -> str:
    about = clip(sanitize_retrieved(", ".join(row.get("about") or [])), _ABOUT_CHARS)
    question = clip(sanitize_retrieved(row.get("question")), _QUESTION_CHARS)
    parts = [(f"[Writ open question {sanitize_retrieved(row['question_id'])} about {about}] "
              f"{_sentence(question)}")]
    who = clip(sanitize_retrieved(row.get("who_can_answer")), _WHO_CHARS)
    if who:
        parts.append(_sentence(f"Who can answer: {who}"))
    settled = clip(sanitize_retrieved(row.get("settled_by")), _SETTLED_CHARS)
    if settled:
        parts.append(_sentence(f"Settled by: {settled}"))
    return clip(" ".join(parts), _QUESTION_LINE_CHARS)


def _cochange_line(path: str, other: str, support: int, base: int) -> str:
    path = clip(sanitize_retrieved(path), _COCHANGE_PATH_CHARS)
    other = clip(sanitize_retrieved(other), _COCHANGE_PATH_CHARS)
    return clip(f"[Writ co-change] {path} usually changes with {other} "
                f"({support} of {base} recent commits).", _COCHANGE_LINE_CHARS)


async def _decision_block(db, project: str, candidates: list[str], shown) -> tuple[str, list[str]]:
    text, key = await write_decision_context(db, project, candidates, shown)
    return text, [key] if key else []


async def _questions_block(db, project: str, candidates: list[str], rule_ids: list[str],
                           shown) -> tuple[str, list[str]]:
    rows = (await db.get_open_questions_for_write(
        project, candidates, list(rule_ids), sorted(shown), limit=_QUESTION_LINES))[:_QUESTION_LINES]
    return "\n".join(_question_line(r) for r in rows), [r["question_id"] for r in rows]


async def _cochange_block(db, project: str, candidates: list[str], shown) -> tuple[str, list[str]]:
    if any(c in shown or is_cochange_noise(c) for c in candidates):
        return "", []
    result = await db.get_cochanged_paths(
        project, candidates, max_files=COCHANGE_MAX_COMMIT_FILES,
        recent_commits=COCHANGE_RECENT_COMMITS, min_support=COCHANGE_MIN_SUPPORT,
        min_confidence=COCHANGE_MIN_CONFIDENCE, limit=COCHANGE_SCAN_LIMIT,
    ) or {}
    hits = [h for h in result.get("hits") or [] if not is_cochange_noise(h["path"])][:_COCHANGE_LINES]
    if not hits:
        return "", []
    lines = [_cochange_line(result["path"], h["path"], h["support"], result["base"]) for h in hits]
    return "\n".join(lines), [result["path"]]


async def write_context(db, project: str, candidates: list[str], rule_ids: list[str], shown: dict,
                        *, timeout_s: float) -> WriteContext:
    """The three blocks for one write, concurrently, each under `timeout_s`. `shown` maps each
    WRITE_BLOCKS section to its ids shown this epoch. The text is the non-empty blocks joined
    by newlines in WRITE_BLOCKS order; marks are the ids each block showed; errors are the
    blocks that raised or timed out, by section."""
    blocks = {
        "pre_write_decision": _decision_block(db, project, candidates, shown["pre_write_decision"]),
        "pre_write_questions": _questions_block(
            db, project, candidates, rule_ids, shown["pre_write_questions"]),
        "pre_write_cochange": _cochange_block(db, project, candidates, shown["pre_write_cochange"]),
    }
    results = await asyncio.gather(
        *(asyncio.wait_for(block, timeout_s) for block in blocks.values()), return_exceptions=True)
    texts: list[str] = []
    marks: dict[str, list[str]] = {}
    errors: dict[str, BaseException] = {}
    for section, result in zip(blocks, results):
        if isinstance(result, BaseException):
            errors[section] = result
            continue
        text, ids = result
        if text:
            texts.append(text)
        if ids:
            marks[section] = ids
    return WriteContext("\n".join(texts), marks, errors)
