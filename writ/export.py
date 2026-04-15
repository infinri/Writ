"""Markdown export from graph -- generates bible/ as a derived view.

bible/ is a derived exported view of the canonical Neo4j graph, not a source
of truth. The graph is canonical. Use `writ import-markdown` only for initial
bootstrap or when re-importing after manual Markdown edits.

Per ARCH-SSOT-001: the graph is the canonical source; exported Markdown is derived.
Per ARCH-ORG-001: export is a separate concern from ingest and retrieval.

The exported format must round-trip through ingest.py without field loss (INV-RT).
Graph-only fields (mandatory, confidence, evidence, staleness_window, last_validated)
are excluded from output -- they are re-derived on re-ingest.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from writ.graph.db import Neo4jConnection

# Per ARCH-CONST-001: named constants.
EXPORT_TIMESTAMP_FILE = ".export_timestamp"
SECTION_ORDER = ("trigger", "statement", "violation", "pass_example", "enforcement", "rationale")
SECTION_HEADERS = {
    "trigger": "### Trigger",
    "statement": "### Statement",
    "violation": "### Violation",
    "pass_example": "### Pass",
    "enforcement": "### Enforcement",
    "rationale": "### Rationale",
}
# Fields that ingest re-derives; must not appear in exported Markdown.
GRAPH_ONLY_FIELDS = {"mandatory", "confidence", "evidence", "staleness_window", "last_validated"}
METADATA_FIELDS = ("domain", "severity", "scope")


def rule_to_markdown(rule: dict) -> str:
    """Convert a single rule dict to a Markdown block with RULE START/END markers.

    Output format matches what ingest.py parses:
    - <!-- RULE START/END --> markers
    - **Bold**: metadata lines
    - ### Section headers
    """
    rule_id = rule["rule_id"]
    lines: list[str] = []
    lines.append(f"<!-- RULE START: {rule_id} -->")
    lines.append(f"## Rule {rule_id}")
    lines.append("")

    # Metadata: Domain, Severity, Scope (title-cased values for readability).
    lines.append(f"**Domain**: {rule.get('domain', '')}")
    lines.append(f"**Severity**: {str(rule.get('severity', '')).title()}")
    lines.append(f"**Scope**: {str(rule.get('scope', '')).title()}")
    lines.append("")

    # Content sections in canonical order.
    for field in SECTION_ORDER:
        header = SECTION_HEADERS[field]
        content = rule.get(field, "")
        lines.append(header)
        lines.append(content)
        lines.append("")

    lines.append(f"<!-- RULE END: {rule_id} -->")
    return "\n".join(lines)


def group_rules_by_file(rules: list[dict], bible_dir: Path) -> dict[Path, list[dict]]:
    """Group rules into output files based on existing bible/ structure.

    Scans bible_dir for existing .md files and builds a domain-to-file map
    by reading which rule IDs each file contains. Rules whose domain doesn't
    match any existing file are grouped into a new file derived from the domain name.
    """
    # Build map: rule_id -> relative file path (from existing bible structure).
    rule_id_to_file: dict[str, Path] = {}
    if bible_dir.exists():
        for md_file in sorted(bible_dir.rglob("*.md")):
            text = md_file.read_text(encoding="utf-8")
            from writ.graph.ingest import RULE_START_PATTERN

            for match in RULE_START_PATTERN.finditer(text):
                found_id = match.group(1)
                rule_id_to_file[found_id] = md_file.relative_to(bible_dir)

    # Group rules by their target file.
    file_groups: dict[Path, list[dict]] = {}
    for rule in rules:
        rid = rule["rule_id"]
        if rid in rule_id_to_file:
            rel_path = rule_id_to_file[rid]
        else:
            # Derive path from domain: "AI Enforcement" -> "ai-enforcement/rules.md"
            domain = rule.get("domain", "uncategorized")
            dir_name = domain.lower().replace(" ", "-").replace("/", "-")
            rel_path = Path(dir_name) / "rules.md"
        file_groups.setdefault(rel_path, []).append(rule)

    # Sort rules within each file by rule_id for deterministic output.
    for group in file_groups.values():
        group.sort(key=lambda r: r["rule_id"])

    return file_groups


def _build_file_content(rules: list[dict]) -> str:
    """Build complete Markdown file content from a list of rules."""
    blocks = [rule_to_markdown(r) for r in rules]
    return "\n---\n\n".join(blocks) + "\n"


async def export_rules_to_markdown(
    db: Neo4jConnection,
    output_dir: Path,
    bible_dir: Path | None = None,
) -> dict[str, int]:
    """Export all rules from graph to Markdown files.

    Args:
        db: Neo4j connection.
        output_dir: Directory to write exported files.
        bible_dir: Existing bible directory for structure mapping.
                   Defaults to output_dir (in-place export).

    Returns:
        {"files_written": N, "rules_exported": M}
    """
    if bible_dir is None:
        bible_dir = output_dir

    rules = await db.get_all_rules()
    if not rules:
        return {"files_written": 0, "rules_exported": 0}

    file_groups = group_rules_by_file(rules, bible_dir)

    files_written = 0
    rules_exported = 0
    for rel_path, grouped_rules in sorted(file_groups.items()):
        target = output_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        content = _build_file_content(grouped_rules)
        target.write_text(content, encoding="utf-8")
        files_written += 1
        rules_exported += len(grouped_rules)

    # Write timestamp for staleness detection.
    write_export_timestamp(output_dir)

    return {"files_written": files_written, "rules_exported": rules_exported}


def write_export_timestamp(output_dir: Path) -> None:
    """Record the export time for staleness comparison."""
    ts_file = output_dir / EXPORT_TIMESTAMP_FILE
    ts_data = {"exported_at": datetime.now(timezone.utc).isoformat()}
    ts_file.write_text(json.dumps(ts_data), encoding="utf-8")


def read_export_timestamp(output_dir: Path) -> datetime | None:
    """Read the last export timestamp. Returns None if no export has been done."""
    ts_file = output_dir / EXPORT_TIMESTAMP_FILE
    if not ts_file.exists():
        return None
    try:
        data = json.loads(ts_file.read_text(encoding="utf-8"))
        return datetime.fromisoformat(data["exported_at"])
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


def check_export_staleness(output_dir: Path, last_graph_write: datetime | None) -> bool:
    """Return True if the export is stale (older than last graph write).

    Returns False if no graph write time is available or no export exists.
    """
    if last_graph_write is None:
        return False
    export_ts = read_export_timestamp(output_dir)
    if export_ts is None:
        return True
    # Ensure both are offset-aware for comparison.
    if export_ts.tzinfo is None:
        export_ts = export_ts.replace(tzinfo=timezone.utc)
    if last_graph_write.tzinfo is None:
        last_graph_write = last_graph_write.replace(tzinfo=timezone.utc)
    return export_ts < last_graph_write
