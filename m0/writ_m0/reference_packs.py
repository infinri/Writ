"""Evidence-kind checks supplied by the three reference Capability Packs.

This is the only module in the harness with domain knowledge. The core
evaluator (evaluation.py) receives these checks as data; it does not import
this module.
"""
from __future__ import annotations

import json
import re

from .evaluation import KindCheck, Source

_FILE_LINE = re.compile(r"^[^:\s]+:\d+(-\d+)?$")
_CHAR_RANGE = re.compile(r"^chars:(\d+)-(\d+)$")


def _file_line(item: dict, source: Source) -> str | None:
    if not _FILE_LINE.match(item.get("citation_locator") or ""):
        return "locator is not path:line"
    return None


def _test_outcome(item: dict, source: Source) -> str | None:
    excerpt = item.get("quoted_excerpt") or ""
    if not excerpt.endswith((" PASSED", " FAILED")):
        return "excerpt is not a test outcome line"
    return None


def _dated_source(item: dict, source: Source) -> str | None:
    claimed = item.get("source_published_at")
    if not claimed:
        return "no publication date"
    if claimed != source.published_at:
        return "publication date differs from source"
    return None


def _counterevidence(item: dict, source: Source) -> str | None:
    return None if item.get("stance") == "contradicts" else "stance is not contradicts"


def _citation_locator(item: dict, source: Source) -> str | None:
    return None if item.get("citation_locator") else "no locator"


def _document_range(item: dict, source: Source) -> str | None:
    match = _CHAR_RANGE.match(item.get("citation_locator") or "")
    if not match:
        return "locator is not chars:start-end"
    start, end = int(match.group(1)), int(match.group(2))
    if source.content[start:end] != item.get("quoted_excerpt"):
        return "range does not contain the excerpt"
    return None


def _rubric_criterion(rubric_source: Source | None):
    criteria = set(json.loads(rubric_source.content)["criteria"]) if rubric_source else set()

    def check(item: dict, source: Source) -> str | None:
        claims = set(item.get("claim_ids", []))
        if not claims or not claims <= criteria:
            return "claims are not criteria of the task rubric"
        return None
    return check


SOFTWARE_ENGINEERING: dict[str, KindCheck] = {"file_line": _file_line, "test_outcome": _test_outcome}
RESEARCH: dict[str, KindCheck] = {
    "dated_source": _dated_source,
    "counterevidence": _counterevidence,
    "citation_locator": _citation_locator,
}


def review_audit(rubric_source: Source | None) -> dict[str, KindCheck]:
    return {"document_range": _document_range, "rubric_criterion": _rubric_criterion(rubric_source)}
