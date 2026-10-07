"""Phase 2 recall: rank captured Decision records (and mirrored memories) against the prompt
and compile them into a budgeted briefing of cards.

Recall is NOT retrieval. Decision and Memory records are deliberately absent from
RETRIEVABLE_NODE_TYPES (schema.py), so they never enter the 5-stage RAG pipeline. Ranking
(program item 4, docs/adr/ADR-decision-recall.md) runs in three tiers:

  * path: decisions behind the files the prompt names (FileChange -[MOTIVATED_BY]-> Decision,
    one batched read, db.get_decisions_for_paths);
  * term: decisions and memories whose text matches the prompt, through an in-memory BM25
    index over the project's corpus (KeywordIndex with no index_dir), at or above
    term_score_floor(N) for a corpus of N decisions and memories;
  * recent: the remaining decisions newest first, on the first brief of an epoch only.

Each kept item renders as one card and is costed by its rendered text, so the budget
(PROMPT_SECTION_TOKENS["recall"] by default) governs exactly what is injected. Eviction
policy: the PROTECTED fields are the id, title, rule ids and memory name; the EVICTABLE
fields, dropped in order, are the rationale (a memory's description), then each matched
file's reason. An item that will not fit even after dropping every evictable field is
dropped WHOLE and nothing ranked below it is kept.

This module is a pure query apart from its in-process corpus cache and one metrics row per
term tier (recall_term_floor): it performs no graph writes.
It contains NO model call; cards are built mechanically by string formatting.
"""
from __future__ import annotations

import asyncio
import math
import os
import re
import time
import weakref
from pathlib import Path
from typing import Any

from writ.session.remote_parse import normalize_path
from writ.shared.injection_text import (
    TITLE_CHARS,
    clip,
    fence,
    first_sentence,
    sanitize_retrieved,
)
from writ.shared.logging import emit
from writ.shared.tokens import PROMPT_SECTION_TOKENS, estimate_tokens

# `writ recall --full` prints every kept decision with its full rationale and statements, so
# its budget is large enough that the listing is never evicted.
RECALL_FULL_BUDGET = 20000
# How long a project's decision and memory corpus (and its built index) is reused.
RECALL_CORPUS_TTL_S = 60
# The term-tier floor is this fraction of the IDF a term found in exactly one document gets, so
# it follows the corpus size N. 0.5 / ln(14), about 0.1895, keeps floor(20) at 0.5, the value the
# 20-decision fixture in tests/test_decision_recall.py set; it is a tuning point.
RECALL_TERM_FLOOR_FRACTION = 0.5 / math.log(14)

_RECALL_LABEL = "RECALL"
_RECALL_DETAIL = "recent decisions on this project, rule-grounded, read back from decision memory"
_BRIEFING_DECISIONS = 5
_BRIEFING_MEMORIES = 2
_CARD_FILES = 2
_RATIONALE_CHARS = 160
_REASON_CHARS = 120
_CORPUS_DECISIONS = 200
_TERM_LIMIT = 20
_QUERY_CHARS = 2000
_PATH_TOKENS = 10
_PATH_ANCESTORS = 4
_PATH_CANDIDATES = 40
_PATH_PER_PATH = 3
_MEMORY_PREFIX = "memory:"
_WRITE_CONTEXT_CHARS = 1000
_WRITE_RATIONALE_CHARS = 400
_WRITE_REASON_CHARS = 200
_WRITE_SUBJECT_CHARS = 100

_PATH_SPLIT = re.compile(r"[\s`'\"()\[\]<>]+")
_LINE_SUFFIX = re.compile(r"(?::\d+){1,2}$")
_DOTTED_EXT = re.compile(r"\.[A-Za-z0-9]{1,8}$")

_corpus_cache: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def display_title(decision: dict[str, Any]) -> str:
    """The first sentence of the rationale, else the stored title, else "(untitled)"."""
    return (
        first_sentence(sanitize_retrieved(decision.get("rationale")), TITLE_CHARS)
        or sanitize_retrieved(decision.get("title"))
        or "(untitled)"
    )


def term_score_floor(corpus_size: int) -> float:
    """The lowest BM25 score a term-tier hit needs in a corpus of `corpus_size` documents:
    RECALL_TERM_FLOOR_FRACTION of the IDF KeywordIndex gives a term present in exactly one
    document, ln(1 + (N - n + 0.5) / (n + 0.5)) at n = 1."""
    return RECALL_TERM_FLOOR_FRACTION * math.log(1 + (corpus_size - 0.5) / 1.5)


def invalidate_corpus(db, project: str | None = None) -> None:
    """Drop the cached corpus of `project` on `db`, or of every project on `db` when None."""
    if project is None:
        _corpus_cache.pop(db, None)
    else:
        _corpus_cache.get(db, {}).pop(project, None)


def prompt_path_tokens(prompt: str) -> list[str]:
    """Path-looking tokens of a prompt: slash-bearing or ending in a dotted extension."""
    out: list[str] = []
    for raw in _PATH_SPLIT.split(prompt or ""):
        if "://" in raw:
            continue
        token = _LINE_SUFFIX.sub("", raw.rstrip(".,:;!?")).rstrip(".,:;!?")
        if token and ("/" in token or _DOTTED_EXT.search(token)) and token not in out:
            out.append(token)
            if len(out) == _PATH_TOKENS:
                break
    return out


def path_candidates(path: str, project_root: str) -> list[str]:
    """Normalized repo-relative spellings of `path` under project_root or up to 4 ancestors."""
    if not project_root:
        return []
    out: list[str] = []
    if not os.path.isabs(path):
        out.append(normalize_path(path))
        path = os.path.join(project_root, path)
    path = os.path.normpath(path)
    root = Path(os.path.normpath(project_root))
    bases = [root, *[p for p in root.parents if p.parent != p][:_PATH_ANCESTORS]]
    for base in bases:
        if path.startswith(str(base) + os.sep):
            out.append(normalize_path(os.path.relpath(path, base)))
    return list(dict.fromkeys(out))


def _item_id(item: dict[str, Any]) -> str:
    if "decision_id" in item:
        return item["decision_id"]
    return _MEMORY_PREFIX + item["name"]


def _render_card(item: dict[str, Any]) -> str:
    if "decision_id" not in item:
        description = clip(sanitize_retrieved(item.get("description")), _RATIONALE_CHARS)
        name = sanitize_retrieved(item["name"])
        return f"- memory {name}" + (f": {description}" if description else "")
    rules = sanitize_retrieved(", ".join(item.get("governing_rule_ids") or [])) or "no rules cited"
    lines = [f"- {item['title']} [{rules}]"]
    if item.get("rationale"):
        lines.append(f"  why: {clip(sanitize_retrieved(item['rationale']), _RATIONALE_CHARS)}")
    for matched in (item.get("matched_files") or [])[:_CARD_FILES]:
        if matched.get("reason"):
            reason = clip(sanitize_retrieved(matched["reason"]), _REASON_CHARS)
            lines.append(f"  {sanitize_retrieved(matched['path'])}: {reason}")
    return "\n".join(lines)


def _decision_token_cost(item: dict[str, Any]) -> int:
    """Token cost of the item's card as currently rendered (evicted fields render nothing).

    Plus one per card for its joining newline and the per-piece rounding, so the sum of card
    costs never undercounts the estimate of the joined briefing."""
    return estimate_tokens(_render_card(item), None) + 1


def _evict(item: dict[str, Any]) -> bool:
    """Drop ONE evictable field in policy order: the rationale (a memory's description),
    then each matched file's reason. False when nothing evictable remains."""
    for key in ("rationale", "description"):
        if item.get(key):
            item[key] = ""
            return True
    for matched in item.get("matched_files") or []:
        if matched.get("reason"):
            matched["reason"] = ""
            return True
    return False


def _build_briefing(kept: list[dict[str, Any]]) -> tuple[str, list[dict[str, str]]]:
    """Mechanical (no-LLM) briefing: at most _BRIEFING_DECISIONS cards in the RECALL fence,
    and the cards' [{id, head}] (head is the card's first line)."""
    if not kept:
        return "", []
    lines = []
    cards: list[dict[str, str]] = []
    for item in kept[:_BRIEFING_DECISIONS]:
        card = _render_card(item)
        lines.append(card)
        cards.append({"id": _item_id(item), "head": card.splitlines()[0]})
    return fence(_RECALL_LABEL, "\n".join(lines), _RECALL_DETAIL), cards


def _decision_item(src: dict[str, Any], match: str) -> dict[str, Any]:
    return {
        "decision_id": src.get("decision_id"),
        "title": display_title(src),
        "rationale": src.get("rationale") or "",
        "planned_files": [dict(pf) for pf in (src.get("planned_files") or [])],
        "governing_rule_ids": list(src.get("governing_rule_ids") or []),
        "phase": src.get("phase"),
        "ts": src.get("ts"),
        "match": match,
        "matched_files": [],
    }


def _memory_item(src: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": src.get("name"),
        "description": src.get("description") or "",
        "type": src.get("type"),
        "updated_at": src.get("updated_at"),
    }


async def _load_corpus(db, project: str):
    """The project's recent decisions, live memories and their in-memory BM25 index, reused
    per (db, project) for RECALL_CORPUS_TTL_S."""
    per_db = _corpus_cache.setdefault(db, {})
    cached = per_db.get(project)
    now = time.monotonic()
    if cached and now - cached[0] < RECALL_CORPUS_TTL_S:
        return cached[1:]
    decisions = await db.get_recent_decisions(project, limit=_CORPUS_DECISIONS)
    memories = await db.list_memories(project)
    from writ.retrieval.keyword import KeywordIndex

    index = KeywordIndex()
    index.build(
        [{
            "rule_id": d["decision_id"],
            "trigger": display_title(d),
            "statement": d.get("rationale") or "",
            "tags": " ".join(pf.get("path") or "" for pf in d.get("planned_files") or []),
            "body": " ".join(pf.get("reason") or "" for pf in d.get("planned_files") or []),
        } for d in decisions]
        + [{
            "rule_id": _MEMORY_PREFIX + m["name"],
            "trigger": m["name"],
            "statement": m.get("description") or "",
        } for m in memories]
    )
    per_db[project] = (now, decisions, memories, index)
    return decisions, memories, index


async def _path_tier(db, project: str, prompt: str, project_root: str) -> list[dict[str, Any]]:
    tokens = prompt_path_tokens(prompt)
    if not tokens or not project_root:
        return []
    by_token = {token: path_candidates(token, project_root) for token in tokens}
    candidates = list(dict.fromkeys(c for cands in by_token.values() for c in cands))
    candidates = candidates[:_PATH_CANDIDATES]
    if not candidates:
        return []
    hits = await db.get_decisions_for_paths(project, candidates, per_path=_PATH_PER_PATH)
    items: dict[str, dict[str, Any]] = {}
    latest: dict[str, str] = {}
    for cands in by_token.values():
        matched = [c for c in cands if c in hits]
        if not matched:
            continue
        path = max(matched, key=len)
        for row in hits[path]:
            item = items.get(row["decision_id"])
            if item is None:
                item = _decision_item({**row, "ts": row.get("decision_ts")}, "path")
                items[row["decision_id"]] = item
            if any(m["path"] == path for m in item["matched_files"]):
                continue
            planned = {pf.get("path"): pf.get("reason") for pf in item["planned_files"]}
            item["matched_files"].append(
                {"path": path, "reason": row.get("reason") or planned.get(path) or ""}
            )
            latest[row["decision_id"]] = max(latest.get(row["decision_id"], ""), row.get("change_ts") or "")
    return sorted(
        items.values(),
        key=lambda i: (latest[i["decision_id"]], i["ts"] or ""),
        reverse=True,
    )


async def compile_recall(
    db,
    project: str,
    *,
    budget: int = PROMPT_SECTION_TOKENS["recall"],
    full: bool = False,
    prompt: str = "",
    project_root: str = "",
    exclude_ids=(),
    matched_only: bool = False,
) -> dict[str, Any]:
    """Rank the project's decisions and memories against `prompt` into a budgeted payload.

    Returns {"briefing", "decisions", "memories", "cards"}. Each decision carries
    decision_id, title (display), rationale (full, or "" when evicted), planned_files,
    governing_rule_ids, rule_statements (read only when `full`), phase, ts, match ("path",
    "term" or "recent") and matched_files. Items whose id is in exclude_ids never appear;
    matched_only skips the recency fill. Without `full` at most _BRIEFING_DECISIONS items
    are kept, of which at most _BRIEFING_MEMORIES are memories.
    """
    decisions, memories, index = await _load_corpus(db, project)
    excluded = set(exclude_ids)

    ranked = [i for i in await _path_tier(db, project, prompt, project_root)
              if i["decision_id"] not in excluded]
    taken = {i["decision_id"] for i in ranked} | excluded

    if prompt:
        by_id = {d["decision_id"]: d for d in decisions}
        by_id.update({_MEMORY_PREFIX + m["name"]: m for m in memories})
        corpus_size = len(decisions) + len(memories)
        floor = term_score_floor(corpus_size)
        hits = index.search(prompt[:_QUERY_CHARS], limit=_TERM_LIMIT)
        try:
            await asyncio.to_thread(
                emit, None, "recall_term_floor", "", None,
                project=project, corpus_size=corpus_size, floor=floor,
                top_score=max((h["score"] for h in hits), default=0),
                cleared=sum(h["score"] >= floor for h in hits),
            )
        except Exception:
            pass
        term: list[tuple[float, str, dict[str, Any]]] = []
        for hit in hits:
            rid = hit["rule_id"]
            if hit["score"] < floor or rid in taken or rid not in by_id:
                continue
            taken.add(rid)
            src = by_id[rid]
            if rid.startswith(_MEMORY_PREFIX):
                term.append((hit["score"], src.get("updated_at") or "", _memory_item(src)))
            else:
                term.append((hit["score"], src.get("ts") or "", _decision_item(src, "term")))
        term.sort(key=lambda t: (t[0], t[1]), reverse=True)
        ranked += [item for _, _, item in term]

    if not matched_only:
        ranked += [_decision_item(d, "recent") for d in decisions if d["decision_id"] not in taken]

    kept: list[dict[str, Any]] = []
    kept_memories = 0
    used = estimate_tokens(fence(_RECALL_LABEL, "", _RECALL_DETAIL), None)
    for item in ranked:
        if not full and len(kept) >= _BRIEFING_DECISIONS:
            break
        is_memory = "decision_id" not in item
        if is_memory and kept_memories >= _BRIEFING_MEMORIES:
            continue
        cost = _decision_token_cost(item)
        while used + cost > budget and _evict(item):
            cost = _decision_token_cost(item)
        if used + cost > budget:
            break
        kept.append(item)
        kept_memories += is_memory
        used += cost

    kept_decisions = [i for i in kept if "decision_id" in i]
    statements: dict[str, str] = {}
    if full:
        rule_ids = list(dict.fromkeys(
            rid for d in kept_decisions for rid in d["governing_rule_ids"]
        ))
        statements = await db.get_rule_statements(rule_ids)
    for d in kept_decisions:
        d["rule_statements"] = (
            {rid: statements.get(rid, "") for rid in d["governing_rule_ids"]} if full else {}
        )

    briefing, cards = _build_briefing(kept)
    return {
        "briefing": briefing,
        "decisions": kept_decisions,
        "memories": [i for i in kept if "decision_id" not in i],
        "cards": cards,
    }


def _sentence(text: str) -> str:
    return text if text.endswith((".", "!", "?")) else text + "."


async def write_decision_context(db, project: str, candidates: list[str], shown) -> tuple[str, str]:
    """The decision behind the most recent motivated change to the written file, as one line
    of at most _WRITE_CONTEXT_CHARS, and its shown key "<path>#<decision_id>". ("", "") when
    the file has no motivated change or the key is already in `shown`."""
    hits = await db.get_decisions_for_paths(project, candidates, per_path=1)
    if not hits:
        return "", ""
    path = max(hits, key=len)
    row = hits[path][0]
    key = f"{path}#{row['decision_id']}"
    if key in shown:
        return "", ""
    rules = ", ".join(row.get("governing_rule_ids") or []) or "no rules cited"
    parts = [
        f"[Writ decision memory: {path} last changed under this decision] "
        f"{display_title(row)} [{rules}]."
    ]
    rationale = clip(sanitize_retrieved(row.get("rationale")), _WRITE_RATIONALE_CHARS)
    if rationale:
        parts.append(_sentence(f"Why: {rationale}"))
    reason = clip(sanitize_retrieved(row.get("reason")), _WRITE_REASON_CHARS)
    if reason:
        parts.append(_sentence(f"This file: {reason}"))
    commit = " ".join(filter(None, [
        clip(sanitize_retrieved(row.get("commit_subject")), _WRITE_SUBJECT_CHARS),
        f"({row['commit_hash'][:8]})" if row.get("commit_hash") else "",
    ]))
    if commit:
        parts.append(_sentence(f"Commit: {commit}"))
    return clip(" ".join(parts), _WRITE_CONTEXT_CHARS), key
