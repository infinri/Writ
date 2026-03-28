"""Markdown parsing -> schema validation -> graph write.

Parses rule blocks delimited by <!-- RULE START/END --> markers.
Files without markers are skipped (playbooks, checklists, etc.).

Per ARCH-ORG-001: parsing lives here, validation lives in schema.py.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from writ.graph.schema import EVIDENCE_DEFAULT, STALENESS_WINDOW_DEFAULT, Rule

# Per ARCH-CONST-001: named patterns for parsing.
RULE_START_PATTERN = re.compile(r"<!--\s*RULE START:\s*(\S+)\s*-->")
RULE_END_PATTERN = re.compile(r"<!--\s*RULE END:\s*(\S+)\s*-->")
METADATA_PATTERN = re.compile(r"\*\*(\w+)\*\*:\s*(.+)")
CROSS_REF_PATTERN = re.compile(r"\b([A-Z][A-Z0-9]*(?:-[A-Z][A-Z0-9]*)+(?:-\d{3}|-[A-Z][A-Z0-9]*))\b")

# Section headers to extract. Keys are normalized names, values are heading prefixes to match.
SECTION_HEADERS = {
    "trigger": "### Trigger",
    "statement": "### Statement",
    "violation": "### Violation",
    "pass_example": "### Pass",
    "enforcement": "### Enforcement",
    "rationale": "### Rationale",
}


def parse_rules_from_file(filepath: Path) -> list[dict]:
    """Extract rule blocks from a Markdown file.

    Returns list of raw dicts (one per rule) with parsed fields.
    Files without RULE START markers return an empty list.
    """
    text = filepath.read_text(encoding="utf-8")
    starts = list(RULE_START_PATTERN.finditer(text))
    if not starts:
        return []

    rules: list[dict] = []
    for start_match in starts:
        rule_id = start_match.group(1)
        end_pattern = re.compile(rf"<!--\s*RULE END:\s*{re.escape(rule_id)}\s*-->")
        end_match = end_pattern.search(text, start_match.end())
        if end_match is None:
            continue
        block = text[start_match.end():end_match.start()]
        parsed = _parse_rule_block(rule_id, block)
        if parsed is not None:
            rules.append(parsed)
    return rules


def _parse_rule_block(rule_id: str, block: str) -> dict | None:
    """Parse a single rule block into a field dict.

    Per ARCH-ERR-001: errors propagate context about which rule failed.
    """
    result: dict = {"rule_id": rule_id}

    # Extract metadata (Domain, Severity, Scope) from bold patterns.
    for match in METADATA_PATTERN.finditer(block):
        key = match.group(1).lower()
        value = match.group(2).strip()
        if key == "domain":
            result["domain"] = value
        elif key == "severity":
            result["severity"] = value.lower()
        elif key == "scope":
            result["scope"] = value.lower()
        elif key == "mandatory":
            result["mandatory"] = value.lower() == "true"

    # Extract sections by heading.
    for field_name, heading_prefix in SECTION_HEADERS.items():
        content = _extract_section(block, heading_prefix)
        if content:
            result[field_name] = content

    # Phase 1b: explicit Mandatory field overrides convention.
    # Convention fallback: ENF-* rules default to mandatory, others do not.
    if "mandatory" not in result:
        result["mandatory"] = rule_id.startswith("ENF-")
    result["confidence"] = "production-validated"
    result["authority"] = "human"
    result["evidence"] = EVIDENCE_DEFAULT
    result["staleness_window"] = STALENESS_WINDOW_DEFAULT
    result["last_validated"] = date.today().isoformat()

    # Detect cross-references to other rules.
    own_id = rule_id
    refs = set()
    for match in CROSS_REF_PATTERN.finditer(block):
        ref_id = match.group(1)
        if ref_id != own_id:
            refs.add(ref_id)
    result["_cross_references"] = sorted(refs)

    return result


def _extract_section(block: str, heading_prefix: str) -> str:
    """Extract text content under a section heading.

    Collects all lines after the heading until the next ### heading or end of block.
    Code blocks (``` fenced) are included as-is.
    """
    lines = block.split("\n")
    capturing = False
    content_lines: list[str] = []

    for line in lines:
        if line.startswith(heading_prefix):
            capturing = True
            continue
        if capturing:
            # Stop at next section heading.
            if line.startswith("### "):
                break
            content_lines.append(line)

    text = "\n".join(content_lines).strip()
    return text if text else ""


def validate_parsed_rule(rule_data: dict) -> Rule:
    """Validate a parsed rule dict against the Pydantic schema.

    Per PY-PYDANTIC-001: all external data validated through Pydantic.
    Per ARCH-ERR-001: validation errors include the rule_id for context.
    """
    # Remove internal fields before validation.
    clean = {k: v for k, v in rule_data.items() if not k.startswith("_")}
    try:
        return Rule(**clean)
    except Exception as e:
        raise ValueError(
            f"Validation failed for rule '{rule_data.get('rule_id', 'unknown')}': {e}"
        ) from e


def discover_rule_files(bible_dir: Path) -> list[Path]:
    """Find all .md files in the bible directory tree."""
    return sorted(bible_dir.rglob("*.md"))
