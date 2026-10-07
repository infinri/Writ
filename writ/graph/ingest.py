"""Markdown parsing -> schema validation -> graph write.

bible/*.md is the exported view of the canonical Neo4j graph, not the source
of truth. Use `writ import-markdown` only for initial bootstrap or when
re-importing after manual Markdown edits.

Three marker / format families are supported:

- Legacy `<!-- RULE START: id --> ... <!-- RULE END: id -->` — existing bible/
  rules. Routed through parse_rules_from_file or parse_nodes_from_file.
- `<!-- NODE START type=X id=Y --> ... <!-- NODE END: Y -->` — Phase 1 methodology
  markers that extend the bible convention to new node types.
- YAML front-matter (one node per file, delimited by `---` blocks) — Phase 0
  synthetic corpus format and preferred form for one-node-per-file content.

Per ARCH-ORG-001: parsing lives here, validation lives in schema.py.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel

from writ.graph.schema import (
    EVIDENCE_DEFAULT,
    NODE_ID_FIELDS,
    NODE_TYPE_MODELS,
    SECTION_HEADERS,
    STALENESS_WINDOW_DEFAULT,
    TRUST_GRAPH_ONLY_PROPS,
    Rule,
)

# Per ARCH-CONST-001: named patterns for parsing.
RULE_START_PATTERN = re.compile(r"<!--\s*RULE START:\s*(\S+)\s*-->")
NODE_START_PATTERN = re.compile(r"<!--\s*NODE START\s+type=(\S+)\s+id=(\S+)\s*-->")
METADATA_PATTERN = re.compile(r"\*\*(\w+)\*\*:\s*(.+)")
CROSS_REF_PATTERN = re.compile(r"\b([A-Z][A-Z0-9]*(?:-[A-Z][A-Z0-9]*)+(?:-\d{3}|-[A-Z][A-Z0-9]*))\b")
FRONT_MATTER_PATTERN = re.compile(r"^---\n(.*?)\n---\n(.*)", re.DOTALL)
# Lines in a RULE-START `### Edges` section: `- TYPE: TARGET-ID`. Lines that
# do not match are silently skipped (prose, blanks).
EDGE_DECL_PATTERN = re.compile(r"^-\s*([A-Z_]+):\s*(\S+)")

# SECTION_HEADERS (the export/ingest round-trip header contract) is the canonical
# registry in writ.graph.schema; imported above so it cannot drift across modules.

# Node-type -> Pydantic model dispatch and node-type -> id-field map are the canonical registry
# in writ.graph.schema (POL-3/C6); re-exported here for the existing `from writ.graph.ingest
# import NODE_ID_FIELDS` call sites.

# Reverse of NODE_ID_FIELDS: primary-id field name -> node_type. Lets the
# front-matter ingest path infer node_type when the YAML omits it (export via
# node_to_yaml_frontmatter does not always carry node_type, since it is a graph
# label rather than a node property).
_ID_FIELD_TO_NODE_TYPE: dict[str, str] = {field: nt for nt, field in NODE_ID_FIELDS.items()}


def _apply_rederived_defaults(result: dict, node_type: str) -> None:
    """Set the graph-only / re-derived fields that export strips and re-ingest
    must restore. Mirrors the RULE-START path (_parse_rule_block) and the
    NODE-START path (_parse_node_block) so a Rule or methodology node parsed
    from YAML front-matter validates without the excluded fields present.

    Only fills absent keys; values already in the front-matter win.
    """
    if node_type == "Rule" and "mandatory" not in result:
        result["mandatory"] = False
    if node_type == "Rule" and not isinstance(result.get("rationalization_counters"), list):
        # Export may serialise this as a JSON string; coerce/default to a list
        # so the Pydantic list field validates on re-ingest.
        raw = result.get("rationalization_counters")
        coerced: list | None = None
        if isinstance(raw, str) and raw.strip():
            import json

            try:
                loaded = json.loads(raw)
                if isinstance(loaded, list):
                    coerced = loaded
            except (ValueError, TypeError):
                coerced = None
        result["rationalization_counters"] = coerced if coerced is not None else []
    if node_type == "Rule":
        # Routing lists default to empty so a rule parsed from YAML front-matter
        # (or a pre-migration rule) validates without the fields present.
        result.setdefault("applicability_scope", [])
        result.setdefault("trigger_keywords", [])
    result.setdefault("confidence", "production-validated")
    result.setdefault("authority", "human")
    result.setdefault("times_seen_positive", 0)
    result.setdefault("times_seen_negative", 0)
    result.setdefault("last_seen", None)
    result.setdefault(
        "evidence", EVIDENCE_DEFAULT if node_type == "Rule" else "peer-reviewed"
    )
    result.setdefault("staleness_window", STALENESS_WINDOW_DEFAULT)
    result.setdefault("last_validated", date.today().isoformat())


def _parse_trust_metadata(key: str, value: str, result: dict) -> bool:
    """Store an authored trust prop (program item 6) when `key` names one.

    Shared by the RULE-START and NODE-START whitelists so both parse the four props
    alike. Returns whether the key was consumed. A non-integer interval is kept raw so
    Pydantic reports the field by name.
    """
    if key == "layer":
        result["layer"] = value.lower()
    elif key == "basis":
        result["basis"] = value.lower()
    elif key == "deliberate":
        result["deliberate"] = value.lower() == "true"
    elif key == "verify_interval_days" or key == "verifyintervaldays":
        try:
            result["verify_interval_days"] = int(value)
        except ValueError:
            result["verify_interval_days"] = value
    else:
        return False
    return True


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
        elif key == "category":
            result["category"] = value
        elif key == "mechanical_enforcement_path" or key == "mechanicalenforcementpath":
            result["mechanical_enforcement_path"] = value
        elif key == "applicability_scope" or key == "applicabilityscope":
            result["applicability_scope"] = [s.strip() for s in value.split(",") if s.strip()]
        elif key == "trigger_keywords" or key == "triggerkeywords":
            result["trigger_keywords"] = [s.strip() for s in value.split(",") if s.strip()]
        else:
            _parse_trust_metadata(key, value, result)

    # Extract sections by heading.
    for field_name, heading_prefix in SECTION_HEADERS.items():
        content = _extract_section(block, heading_prefix)
        if content:
            result[field_name] = content

    # Mandatory must be declared explicitly via the **Mandatory** field
    # (writ-evolution.md Section 2.2). The earlier rule_id.startswith("ENF-")
    # convention was removed 2026-05-09: ENF-prefixed rules can be advisory
    # too, and non-ENF rules can be mandatory if explicitly declared.
    if "mandatory" not in result:
        result["mandatory"] = False
    result["confidence"] = "production-validated"
    result["authority"] = "human"
    result["times_seen_positive"] = 0
    result["times_seen_negative"] = 0
    result["last_seen"] = None
    result["evidence"] = EVIDENCE_DEFAULT
    result["staleness_window"] = STALENESS_WINDOW_DEFAULT
    result["last_validated"] = date.today().isoformat()

    # Detect cross-references to other rules. The `### Edges` section is a
    # STRUCTURED edge-declaration surface (parsed separately below); its target
    # ids must NOT also be captured as prose cross-references, or derive_edges
    # would emit a spurious RELATED_TO alongside the intended typed edge. Strip
    # the section before the prose scan; all other prose is scanned unchanged.
    result["_cross_references"] = _extract_cross_refs(_strip_edges_section(block), rule_id)

    # Declared edges from an optional `### Edges` section (Change A). These flow
    # into the graph exactly like front-matter `edges:` declarations; the ingest
    # path and the reconcile oracle both collect them via parse_edges_from_file.
    result["_declared_edges"] = _parse_declared_edges(rule_id, block)

    return result


def _parse_declared_edges(source_id: str, block: str) -> list[dict]:
    """Parse a RULE-START `### Edges` section into declared-edge dicts.

    Each body line of the form `- TYPE: TARGET-ID` becomes
    {source: source_id, target: TARGET-ID, type: TYPE}. Lines that do not match
    EDGE_DECL_PATTERN (prose, blanks, missing dash) are silently skipped. The
    TYPE is validated against ALLOWED_EDGE_TYPES; an unknown type is skipped +
    continued (mirrors db.py create_edge / batch_create_edges) so a typo never
    crashes the parse. The ALLOWED_EDGE_TYPES import is lazy so the parser does
    not pull in the neo4j-backed db module unless an `### Edges` section is
    actually present.
    """
    section = _extract_section(block, "### Edges")
    if not section:
        return []
    try:
        from writ.graph.db import ALLOWED_EDGE_TYPES
    except Exception:
        ALLOWED_EDGE_TYPES = None
    edges: list[dict] = []
    for line in section.split("\n"):
        m = EDGE_DECL_PATTERN.match(line.strip())
        if not m:
            continue
        etype, target = m.group(1), m.group(2)
        if ALLOWED_EDGE_TYPES is not None and etype not in ALLOWED_EDGE_TYPES:
            continue
        edges.append({"source": source_id, "target": target, "type": etype})
    return edges


def _strip_edges_section(block: str) -> str:
    """Return `block` with its `### Edges` section (heading + body) removed.

    The `### Edges` section is a structured edge-declaration surface consumed by
    _parse_declared_edges. Its `- TYPE: TARGET-ID` lines must never leak into the
    prose cross-reference scan (CROSS_REF_PATTERN): a declared edge target would
    otherwise be captured as a `_cross_reference` and derive_edges would emit a
    spurious RELATED_TO edge to the same target. Lines under `### Edges` are
    dropped until the next `### ` heading or end of block; all other prose is
    preserved verbatim.
    """
    lines = block.split("\n")
    out: list[str] = []
    skipping = False
    for line in lines:
        if line.startswith("### Edges"):
            skipping = True
            continue
        if skipping:
            if line.startswith("### "):
                skipping = False
                out.append(line)
            # else: still inside the Edges section -- drop the line.
            continue
        out.append(line)
    return "\n".join(out)


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


# --- Phase 1: multi-node-type ingest (plan Section 6.1 deliverable 2) ---------


def parse_nodes_from_file(filepath: Path) -> list[dict]:
    """Extract node definitions from a Markdown file supporting all three formats.

    Precedence:
    1. YAML front-matter (single node, one-per-file) takes highest precedence.
    2. <!-- NODE START type=X id=Y --> markers (multi-node).
    3. <!-- RULE START: id --> markers (legacy, routed as Rule node_type).

    Returns list of dicts; each carries a `node_type` key. Empty list if no
    markers / front-matter found.
    """
    text = filepath.read_text(encoding="utf-8")

    # 1. YAML front-matter — one node per file.
    fm_match = FRONT_MATTER_PATTERN.match(text)
    if fm_match:
        fm_yaml = fm_match.group(1)
        body = fm_match.group(2).strip()
        try:
            fm = yaml.safe_load(fm_yaml) or {}
        except yaml.YAMLError as e:
            raise ValueError(f"Front-matter YAML parse error in {filepath}: {e}") from e
        node_type = fm.get("node_type")
        if node_type is None:
            # Infer node_type from whichever primary id field is present. Export
            # via node_to_yaml_frontmatter writes the id field (e.g. skill_id,
            # antipattern_id) but not node_type (that is a graph label, not a
            # node property), so inference keeps the round trip lossless.
            for id_field, candidate_type in _ID_FIELD_TO_NODE_TYPE.items():
                if id_field in fm:
                    node_type = candidate_type
                    break
        if node_type is None:
            return []
        data = dict(fm)
        data["node_type"] = node_type
        # Graph-only trust props are written by the graph alone: markdown never authors one.
        for prop in TRUST_GRAPH_ONLY_PROPS:
            data.pop(prop, None)
        # Restore the graph-only / re-derived fields export strips (last_validated,
        # confidence, evidence, ...) for every node type, mirroring the RULE-START
        # and NODE-START paths so a front-matter Rule or methodology node validates.
        _apply_rederived_defaults(data, node_type)
        # Mark front-matter origin so dual-location dedup can prefer the
        # bible/methodology/<id>.md front-matter copy over a domain rules.md
        # RULE START copy of the same primary id.
        data["_source_format"] = "front-matter"
        # body field is populated from post-frontmatter content unless explicitly set
        if "body" not in data or not data.get("body"):
            data["body"] = body
        # cross-refs from the body text
        data["_cross_references"] = _extract_cross_refs(body, data.get(NODE_ID_FIELDS.get(node_type, ""), ""))
        return [data]

    # 2. NODE START markers — possibly multiple per file.
    node_starts = list(NODE_START_PATTERN.finditer(text))
    if node_starts:
        nodes: list[dict] = []
        for start_match in node_starts:
            node_type = start_match.group(1)
            node_id = start_match.group(2)
            end_pattern = re.compile(rf"<!--\s*NODE END:\s*{re.escape(node_id)}\s*-->")
            end_match = end_pattern.search(text, start_match.end())
            if end_match is None:
                continue
            block = text[start_match.end():end_match.start()]
            parsed = _parse_node_block(node_type, node_id, block)
            if parsed is not None:
                nodes.append(parsed)
        return nodes

    # 3. Legacy RULE START markers — route as Rule node_type.
    legacy = parse_rules_from_file(filepath)
    for r in legacy:
        r.setdefault("node_type", "Rule")
    return legacy


def _parse_node_block(node_type: str, node_id: str, block: str) -> dict | None:
    """Parse a NODE START marker block (bible-style sections) into a field dict."""
    id_field = NODE_ID_FIELDS.get(node_type)
    if id_field is None:
        return None
    result: dict = {id_field: node_id, "node_type": node_type}

    for match in METADATA_PATTERN.finditer(block):
        key = match.group(1).lower()
        value = match.group(2).strip()
        if key in ("domain", "severity", "scope"):
            result[key] = value.lower() if key in ("severity", "scope") else value
        elif key == "category":
            result["category"] = value
        elif key == "mandatory" and node_type == "Rule":
            result["mandatory"] = value.lower() == "true"
        elif node_type == "Rule":
            _parse_trust_metadata(key, value, result)

    for field_name, heading_prefix in SECTION_HEADERS.items():
        content = _extract_section(block, heading_prefix)
        if content:
            result[field_name] = content

    # mandatory is explicit-only (the rule_id.startswith("ENF-") convention was
    # removed 2026-05-09): ENF rules can be advisory and non-ENF rules mandatory
    # if declared. Defaults for fields not typically present inline are applied by
    # the shared helper so a Rule parsed via NODE START agrees with one parsed via
    # RULE START or YAML front-matter (audit #7).
    _apply_rederived_defaults(result, node_type)

    # Strip the structured `### Edges` section before the prose cross-ref scan so
    # declared edge targets do not leak into RELATED_TO derivation (see
    # _strip_edges_section).
    result["_cross_references"] = _extract_cross_refs(_strip_edges_section(block), node_id)
    return result


def _extract_cross_refs(text: str, own_id: str) -> list[str]:
    refs = set()
    for match in CROSS_REF_PATTERN.finditer(text):
        ref_id = match.group(1)
        if ref_id != own_id:
            refs.add(ref_id)
    return sorted(refs)


def parse_edges_from_file(filepath: Path) -> list[dict]:
    """Extract edge declarations from a file.

    Two declared-edge sources are collected here -- this is the single per-file
    declared-edge surface that both the ingest writer (ingest_path) and the
    reconcile oracle (parse_source -> compute_expected_graph) consume:

    1. YAML front-matter `edges:` list (one-node-per-file form).
    2. RULE-START `### Edges` sections (Change A): each `- TYPE: TARGET-ID`
       line under a block's `### Edges` heading, sourced from that block's
       rule id.

    Returns list of dicts with source/target/type. Inline
    `<!-- EDGE: src --TYPE--> tgt -->` markers are a reserved format for future
    use; not currently consumed.
    """
    text = filepath.read_text(encoding="utf-8")
    fm_match = FRONT_MATTER_PATTERN.match(text)
    if fm_match:
        try:
            fm = yaml.safe_load(fm_match.group(1)) or {}
        except yaml.YAMLError:
            return []
        node_type = fm.get("node_type") or ("Rule" if "rule_id" in fm else None)
        if node_type is None:
            return []
        id_field = NODE_ID_FIELDS.get(node_type)
        source = fm.get(id_field) if id_field else None
        if source is None:
            return []
        out = []
        for edge in fm.get("edges") or []:
            if not isinstance(edge, dict):
                continue
            target = edge.get("target")
            etype = edge.get("type")
            if not target or not etype:
                continue
            out.append({"source": source, "target": target, "type": etype})
        return out

    # No front-matter: collect declared edges from RULE-START `### Edges`
    # sections. parse_rules_from_file already populates `_declared_edges` per
    # block via _parse_declared_edges (type-validated, malformed-tolerant).
    out = []
    for rule in parse_rules_from_file(filepath):
        out.extend(rule.get("_declared_edges", []))
    return out


def extract_belongs_to_edges(nodes: list[dict]) -> list[dict]:
    """Derive BELONGS_TO edge records from parsed node dicts carrying a category.

    For each node with a non-empty 'category' value, emit
    {'source': <node primary id>, 'target': <category value>, 'type': 'BELONGS_TO'}.
    Nodes without a 'category' key are skipped. Input order is preserved.
    """
    edges: list[dict] = []
    for node in nodes:
        category = node.get("category")
        if not category:
            continue
        node_type = node.get("node_type", "Rule")
        id_field = NODE_ID_FIELDS.get(node_type)
        source = node.get(id_field) if id_field else None
        if source is None:
            # Fall back to scanning any known id field present on the node.
            for candidate in NODE_ID_FIELDS.values():
                if candidate in node:
                    source = node[candidate]
                    break
        if source is None:
            continue
        edges.append({"source": source, "target": category, "type": "BELONGS_TO"})
    return edges


def validate_parsed_node(node_data: dict) -> BaseModel:
    """Validate a parsed node dict against the Pydantic model for its node_type.

    Per PY-PYDANTIC-001: all external data validated. Dispatches to the correct
    model via NODE_TYPE_MODELS. Validation errors cite the node_type and id.
    """
    node_type = node_data.get("node_type", "Rule")
    model = NODE_TYPE_MODELS.get(node_type)
    if model is None:
        raise ValueError(f"Unknown node_type '{node_type}' (expected one of {sorted(NODE_TYPE_MODELS)})")
    # Drop harness-only keys before model construction.
    clean = {
        k: v for k, v in node_data.items()
        if k != "node_type" and not k.startswith("_") and k != "edges"
    }
    try:
        return model(**clean)
    except Exception as e:
        id_field = NODE_ID_FIELDS.get(node_type, "id")
        nid = node_data.get(id_field, "unknown")
        raise ValueError(f"Validation failed for {node_type} '{nid}': {e}") from e
