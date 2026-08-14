"""Markdown export from graph: generates bible/ as a derived view.

bible/ is a derived exported view of the canonical Neo4j graph, not a source
of truth. The graph is canonical. Use `writ import-markdown` only for initial
bootstrap or when re-importing after manual Markdown edits.

Per ARCH-SSOT-001: the graph is the canonical source; exported Markdown is derived.
Per ARCH-ORG-001: export is a separate concern from ingest and retrieval.

The exported format must round-trip through ingest.py without field loss (INV-RT).
mandatory is written as a metadata line so it survives the round trip; the
remaining graph-only fields (confidence, evidence, staleness_window,
last_validated) are excluded from output and re-derived on re-ingest.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from writ.graph.db import Neo4jConnection

# Per ARCH-CONST-001: named constants.
EXPORT_TIMESTAMP_FILE = ".export_timestamp"
SECTION_ORDER = ("trigger", "statement", "violation", "pass_example", "enforcement", "rationale")
# SECTION_HEADERS (the export/ingest round-trip header contract) is imported from
# writ.graph.schema inside rule_to_markdown (deferred, matching this module's
# schema-import pattern) so the map cannot drift between export and ingest.
# Fields that ingest re-derives; must not appear in exported Markdown.
# Note: mandatory used to be in this set, but ingest's re-derivation logic
# (the ENF- prefix convention) was removed on 2026-05-09. Until then, mandatory
# was silently lost on export/import cycles. We now write it as a metadata
# line so the round trip is lossless.
GRAPH_ONLY_FIELDS = {
    "confidence",
    "evidence",
    "staleness_window",
    "last_validated",
    "authority",
    "times_seen_positive",
    "times_seen_negative",
    "last_seen",
    # 0.10: set at write time by the creation path (ingest | graph-authored), never
    # authored in markdown. Excluded from export and from 5.2's field-level parity diff.
    "source_origin",
}

# Edge types DERIVED at ingest from prose/fields, never authored in a RULE-START
# `### Edges` section: RELATED_TO is derived from prose cross-references and
# BELONGS_TO from the `**Category**:` field. The RULE-START export path must NOT
# render these as `### Edges` lines: doing so would pollute every rule with
# hundreds of derived RELATED_TO lines and break idempotency (a re-ingest would
# then treat them as declared edges). NOTE: this exclusion applies ONLY to the
# RULE-START path. The frontmatter path (node_to_yaml_frontmatter) deliberately
# does NOT apply it, because methodology nodes carry hand-authored RELATED_TO
# edges in their `edges:` block that are source-of-truth and must round-trip.
DERIVED_EDGE_TYPES = frozenset({"RELATED_TO", "BELONGS_TO"})


def _declared_edge_types() -> frozenset[str]:
    """The edge types that may be RENDERED in an `### Edges` section: every
    allowed type EXCEPT the derived ones. Lazy import keeps export.py importable
    without pulling the neo4j-backed db module at definition time.
    """
    from writ.graph.db import ALLOWED_EDGE_TYPES

    return frozenset(ALLOWED_EDGE_TYPES) - DERIVED_EDGE_TYPES


# Module-level snapshot for callers/tests that want the declared-edge-type set
# directly. Built once at import; db.py is a hard project dependency.
DECLARED_EDGE_TYPES: frozenset[str] = _declared_edge_types()


def _renderable_edges(edges: list | None) -> list[dict]:
    """Filter + sort outgoing edges for rendering in an `### Edges` section.

    Keeps only edges whose type is a DECLARED edge type (derived RELATED_TO /
    BELONGS_TO are dropped). Sorts deterministically by (type, target) so export
    is stable. Each kept edge must carry a non-empty type and target.
    """
    out: list[dict] = []
    for e in edges or []:
        etype = e.get("type")
        target = e.get("target")
        if not etype or not target:
            continue
        if etype not in DECLARED_EDGE_TYPES:
            continue
        out.append({"type": etype, "target": target})
    out.sort(key=lambda e: (e["type"], e["target"]))
    return out

# Rule IDs that live both as standalone files under bible/methodology/ and as
# duplicate blocks inside a bible/<domain>/rules.md. bible/methodology/<ID>.md is
# the canonical home; group_rules_by_file routes these there and excludes them
# from domain rules.md output.
_METHODOLOGY_CANONICAL_RULE_IDS = frozenset({
    "ENF-COMMS-001",
    "ENF-COMMS-OUTPUT-001",
    "ENF-META-CONCISE-001",
    "ENF-PROC-BRAIN-001",
    "ENF-PROC-DEBUG-001",
    "ENF-PROC-FIXLOOP-001",
    "ENF-PROC-PLAN-001",
    "ENF-PROC-PRIORITY-001",
    "ENF-PROC-SDD-001",
    "ENF-PROC-TDD-001",
    "ENF-PROC-VERIFY-001",
    "ENF-PROC-WORKTREE-001",
    "META-AUTH-001",
    "META-AUTH-002",
})


def _is_methodology_canonical(rule_id: str | None, bible_dir: Path) -> bool:
    """A Rule is methodology-canonical when bible/methodology/<id>.md exists.

    The frozenset above is the historical hand-maintained list, and being
    hand-maintained is exactly how it failed: cycle F's ENF-PROC-FIXLOOP-001
    was authored as a methodology Rule, was not added here, and the auto-export
    dutifully wrote a duplicate RULE-START copy into bible/process/rules.md --
    one node declared in two files, one edge declared twice, and the round-trip
    completeness test red. The filesystem IS the fact the set tries to cache,
    so consult it too; the set stays because three test modules import it and
    because it documents the original Phase 0 migration cohort.
    """
    if not rule_id:
        return False
    if rule_id in _METHODOLOGY_CANONICAL_RULE_IDS:
        return True
    try:
        return (bible_dir / "methodology" / f"{rule_id}.md").exists()
    except OSError:
        return False


def rule_to_markdown(rule: dict, edges: list | None = None) -> str:
    """Convert a single rule dict to a Markdown block with RULE START/END markers.

    Output format matches what ingest.py parses:
    - <!-- RULE START/END --> markers
    - **Bold**: metadata lines
    - ### Section headers
    - an `### Edges` section (rendered BEFORE the RULE END marker) when `edges`
      contains DECLARED edges (derived RELATED_TO / BELONGS_TO are excluded; see
      _renderable_edges). Omitted entirely when there are no declarable edges so
      the ~350-file corpus does not churn.
    """
    from writ.graph.schema import SECTION_HEADERS

    rule_id = rule["rule_id"]
    lines: list[str] = []
    lines.append(f"<!-- RULE START: {rule_id} -->")
    lines.append(f"## Rule {rule_id}")
    lines.append("")

    # Metadata: Domain, Category, Severity, Scope, Mandatory, and (when present)
    # the mechanical enforcement path. Title-cased values for readability.
    lines.append(f"**Domain**: {rule.get('domain', '')}")
    # Category (a CAT-* node id) is stored as a graph node property and routes
    # the rule into a Category via a BELONGS_TO edge. It must survive export so
    # the auto-export does not strip the category the migration injected.
    category = rule.get("category")
    if category:
        lines.append(f"**Category**: {category}")
    lines.append(f"**Severity**: {str(rule.get('severity', '')).title()}")
    lines.append(f"**Scope**: {str(rule.get('scope', '')).title()}")
    lines.append(f"**Mandatory**: {'true' if rule.get('mandatory', False) else 'false'}")
    mech_path = rule.get('mechanical_enforcement_path')
    if mech_path:
        lines.append(f"**Mechanical_Enforcement_Path**: {mech_path}")
    # Always-on applicability routing (rendered only when present so the corpus does
    # not churn). Comma-space joined; the parser splits on comma and strips.
    scope = rule.get('applicability_scope')
    if scope:
        lines.append(f"**Applicability_Scope**: {', '.join(scope)}")
    keywords = rule.get('trigger_keywords')
    if keywords:
        lines.append(f"**Trigger_Keywords**: {', '.join(keywords)}")
    lines.append("")

    # Content sections in canonical order.
    for field in SECTION_ORDER:
        header = SECTION_HEADERS[field]
        content = rule.get(field, "")
        lines.append(header)
        lines.append(content)
        lines.append("")

    # Declared edges, rendered as `- TYPE: TARGET` lines under an `### Edges`
    # heading, ONLY when there are declarable edges to emit (derived RELATED_TO /
    # BELONGS_TO are filtered out by _renderable_edges). Placed before RULE END so
    # _parse_declared_edges picks it up on re-ingest.
    renderable = _renderable_edges(edges)
    if renderable:
        lines.append("### Edges")
        for e in renderable:
            lines.append(f"- {e['type']}: {e['target']}")
        lines.append("")

    lines.append(f"<!-- RULE END: {rule_id} -->")
    return "\n".join(lines)


def node_to_yaml_frontmatter(
    node: dict, edges: list | None = None, node_type: str | None = None
) -> str:
    """Serialise a methodology node dict to a YAML front-matter Markdown block.

    Emits '---\\n', the node's fields as YAML (excluding GRAPH_ONLY_FIELDS),
    an injected 'edges:' key when edges are present (each edge carries its
    target id and edge type), a closing '---', then the node body.

    Edges may be supplied via the `edges` argument or an 'edges' key on the
    node dict. The body is taken from a 'body' key on the node if present.

    `node_type` is written into the front-matter so re-ingest is self-describing
    (the graph stores type as a label, not a node property, so without this the
    round trip relies on id-field inference). A 'node_type' key already on the
    node dict takes precedence.
    """
    fields = {
        k: v
        for k, v in node.items()
        if k not in GRAPH_ONLY_FIELDS and k not in ("edges", "body")
    }
    # 6.1 (D2): provenance is graph-side lineage. Omit the default hand-authored so
    # the ~350-file corpus never churns; emit only a non-default value (graduated),
    # which round-trips to preserve the self-authoring lineage.
    if fields.get("provenance") == "hand-authored":
        fields.pop("provenance", None)
    if node_type is not None:
        fields.setdefault("node_type", node_type)

    # Lead the front-matter with the primary id field then node_type. The graph
    # stores properties in insertion order, which buries the id field deep in the
    # block; the corpus convention (and downstream first-N-lines id heuristics)
    # expects the id near the top, so emit it first for a stable, scannable head.
    from writ.graph.schema import NODE_ID_FIELDS

    effective_type = fields.get("node_type")
    id_field = NODE_ID_FIELDS.get(effective_type) if effective_type else None
    ordered: dict = {}
    if id_field and id_field in fields:
        ordered[id_field] = fields[id_field]
    if "node_type" in fields:
        ordered["node_type"] = fields["node_type"]
    for k, v in fields.items():
        if k not in ordered:
            ordered[k] = v
    fields = ordered

    edge_source = edges if edges is not None else node.get("edges")
    # NOTE: the frontmatter path intentionally does NOT apply the RULE-START
    # DERIVED_EDGE_TYPES exclusion. Methodology nodes carry HAND-AUTHORED
    # RELATED_TO edges in their `edges:` block (e.g. bible/methodology/*.md) that
    # are source-of-truth and must round-trip; stripping them would corrupt the
    # committed corpus. The exclusion is correct only for the RULE-START path,
    # where RELATED_TO/BELONGS_TO are DERIVED from prose/category at ingest and
    # so must never be re-emitted. See DERIVED_EDGE_TYPES / _renderable_edges.
    if edge_source:
        fields["edges"] = [
            {"target": e.get("target"), "type": e.get("type")} for e in edge_source
        ]

    front = yaml.dump(fields, sort_keys=False, default_flow_style=False, allow_unicode=True)

    body = node.get("body", "")
    parts = ["---\n", front, "---\n"]
    if body:
        parts.append(body)
    return "".join(parts)


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
        if _is_methodology_canonical(rid, bible_dir):
            # Dual-location rule: hand-authored front-matter SOURCE lives at
            # bible/methodology/<ID>.md (carrying its edges + all fields). The
            # rule export does NOT regenerate it -- skip entirely so the export
            # never overwrites the hand-authored methodology file. Its domain
            # rules.md duplicate was removed by the Phase 0 migration.
            continue
        if rule.get("provenance") == "graduated":
            # 6.3c: a graduated rule is methodology-homed at bible/methodology/<ID>.md
            # (rich front-matter, written by the promotion gate). Skip it from the
            # domain rules.md export so the full-corpus export never creates a lossy
            # duplicate that drops provenance/graduated_via.
            continue
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


def _build_file_content(
    rules: list[dict], edges_by_source: dict[str, list[dict]] | None = None
) -> str:
    """Build complete Markdown file content from a list of rules.

    `edges_by_source` maps a rule_id to its outgoing edges; when supplied, each
    rule's declared edges are rendered into an `### Edges` section by
    rule_to_markdown. Derived RELATED_TO / BELONGS_TO edges are filtered out
    downstream (_renderable_edges).
    """
    edges_by_source = edges_by_source or {}
    blocks = [
        rule_to_markdown(r, edges=edges_by_source.get(r["rule_id"])) for r in rules
    ]
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

    # Collect every outgoing edge keyed by source id so declared edges survive
    # export (the RULE-START path previously dropped them -- asymmetric round
    # trip). Derived RELATED_TO / BELONGS_TO are filtered at render time by
    # _renderable_edges, so the map can carry the full edge set.
    edges_by_source: dict[str, list[dict]] = {}
    try:
        all_edges = await db.get_all_edges_cross_type()
    except Exception:
        all_edges = []
    for edge in all_edges:
        src = edge.get("source_id")
        if src is None:
            continue
        edges_by_source.setdefault(src, []).append(
            {"target": edge.get("target_id"), "type": edge.get("type")}
        )

    file_groups = group_rules_by_file(rules, bible_dir)

    files_written = 0
    rules_exported = 0
    for rel_path, grouped_rules in sorted(file_groups.items()):
        target = output_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        content = _build_file_content(grouped_rules, edges_by_source)
        target.write_text(content, encoding="utf-8")
        files_written += 1
        rules_exported += len(grouped_rules)

    # Write timestamp for staleness detection.
    write_export_timestamp(output_dir)

    return {"files_written": files_written, "rules_exported": rules_exported}


async def export_graph_to_markdown(db: Neo4jConnection, output_dir: Path) -> dict[str, int]:
    """Export every node type and every edge from the graph to Markdown.

    Methodology nodes and the 12 dual-location canonical rules are written to
    <output_dir>/methodology/<id>.md via node_to_yaml_frontmatter, with their
    outgoing edges injected into the front-matter. Plain Rules are written via
    the existing domain rules.md path (group_rules_by_file + rule_to_markdown).

    Returns:
        {"nodes_exported": int, "edges_exported": int}
    """
    from writ.graph.schema import METHODOLOGY_NODE_TYPES, NODE_ID_FIELDS

    output_dir.mkdir(parents=True, exist_ok=True)

    # Collect every edge once, keyed by source id for front-matter injection.
    all_edges = await db.get_all_edges_cross_type()
    edges_exported = len(all_edges)
    edges_by_source: dict[str, list[dict]] = {}
    for edge in all_edges:
        src = edge.get("source_id")
        if src is None:
            continue
        edges_by_source.setdefault(src, []).append(
            {"target": edge.get("target_id"), "type": edge.get("type")}
        )

    nodes_exported = 0
    plain_rules: list[dict] = []

    for label, id_field in NODE_ID_FIELDS.items():
        nodes = await db.get_all_nodes_by_type(label)
        for node in nodes:
            # Drop the internal label key surfaced by get_all_nodes.
            node = {k: v for k, v in node.items() if k != "label"}
            node_id = node.get(id_field)
            is_methodology = label in METHODOLOGY_NODE_TYPES
            is_dual_location = label == "Rule" and _is_methodology_canonical(
                node_id, output_dir
            )

            if is_methodology or is_dual_location:
                target = output_dir / "methodology" / f"{node_id}.md"
                target.parent.mkdir(parents=True, exist_ok=True)
                edges = edges_by_source.get(node_id)
                target.write_text(
                    node_to_yaml_frontmatter(node, edges=edges, node_type=label),
                    encoding="utf-8",
                )
                nodes_exported += 1
            elif label == "Rule":
                plain_rules.append(node)
            else:
                nodes_exported += 1

    if plain_rules:
        file_groups = group_rules_by_file(plain_rules, output_dir)
        for rel_path, grouped_rules in sorted(file_groups.items()):
            target = output_dir / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                _build_file_content(grouped_rules, edges_by_source), encoding="utf-8"
            )
            nodes_exported += len(grouped_rules)

    write_export_timestamp(output_dir)

    return {"nodes_exported": nodes_exported, "edges_exported": edges_exported}


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
