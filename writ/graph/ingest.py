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
    STALENESS_WINDOW_DEFAULT,
    AntiPattern,
    ForbiddenResponse,
    Phase,
    Playbook,
    PressureScenario,
    Rationalization,
    Rule,
    Skill,
    SubagentRole,
    Technique,
    WorkedExample,
)

# Per ARCH-CONST-001: named patterns for parsing.
RULE_START_PATTERN = re.compile(r"<!--\s*RULE START:\s*(\S+)\s*-->")
RULE_END_PATTERN = re.compile(r"<!--\s*RULE END:\s*(\S+)\s*-->")
NODE_START_PATTERN = re.compile(r"<!--\s*NODE START\s+type=(\S+)\s+id=(\S+)\s*-->")
NODE_END_PATTERN = re.compile(r"<!--\s*NODE END:\s*(\S+)\s*-->")
METADATA_PATTERN = re.compile(r"\*\*(\w+)\*\*:\s*(.+)")
CROSS_REF_PATTERN = re.compile(r"\b([A-Z][A-Z0-9]*(?:-[A-Z][A-Z0-9]*)+(?:-\d{3}|-[A-Z][A-Z0-9]*))\b")
FRONT_MATTER_PATTERN = re.compile(r"^---\n(.*?)\n---\n(.*)", re.DOTALL)

# Section headers to extract. Keys are normalized names, values are heading prefixes to match.
SECTION_HEADERS = {
    "trigger": "### Trigger",
    "statement": "### Statement",
    "violation": "### Violation",
    "pass_example": "### Pass",
    "enforcement": "### Enforcement",
    "rationale": "### Rationale",
}

# Node-type → Pydantic model dispatch.
NODE_TYPE_MODELS: dict[str, type[BaseModel]] = {
    "Rule": Rule,
    "Skill": Skill,
    "Playbook": Playbook,
    "Technique": Technique,
    "AntiPattern": AntiPattern,
    "ForbiddenResponse": ForbiddenResponse,
    "Phase": Phase,
    "Rationalization": Rationalization,
    "PressureScenario": PressureScenario,
    "WorkedExample": WorkedExample,
    "SubagentRole": SubagentRole,
}

# Node-type → primary-key field name.
NODE_ID_FIELDS: dict[str, str] = {
    "Rule": "rule_id",
    "Skill": "skill_id",
    "Playbook": "playbook_id",
    "Technique": "technique_id",
    "AntiPattern": "antipattern_id",
    "ForbiddenResponse": "forbidden_id",
    "Phase": "phase_id",
    "Rationalization": "rationalization_id",
    "PressureScenario": "scenario_id",
    "WorkedExample": "example_id",
    "SubagentRole": "role_id",
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
    result["times_seen_positive"] = 0
    result["times_seen_negative"] = 0
    result["last_seen"] = None
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
        if node_type is None and "rule_id" in fm:
            node_type = "Rule"
        if node_type is None:
            return []
        data = dict(fm)
        data["node_type"] = node_type
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
        elif key == "mandatory" and node_type == "Rule":
            result["mandatory"] = value.lower() == "true"

    for field_name, heading_prefix in SECTION_HEADERS.items():
        content = _extract_section(block, heading_prefix)
        if content:
            result[field_name] = content

    if node_type == "Rule" and "mandatory" not in result:
        result["mandatory"] = node_id.startswith("ENF-")

    # Defaults for fields not typically present inline.
    result.setdefault("confidence", "production-validated")
    result.setdefault("authority", "human")
    result.setdefault("times_seen_positive", 0)
    result.setdefault("times_seen_negative", 0)
    result.setdefault("last_seen", None)
    result.setdefault("evidence", EVIDENCE_DEFAULT if node_type == "Rule" else "doc:superpowers")
    result.setdefault("staleness_window", STALENESS_WINDOW_DEFAULT)
    result.setdefault("last_validated", date.today().isoformat())

    result["_cross_references"] = _extract_cross_refs(block, node_id)
    return result


def _extract_cross_refs(text: str, own_id: str) -> list[str]:
    refs = set()
    for match in CROSS_REF_PATTERN.finditer(text):
        ref_id = match.group(1)
        if ref_id != own_id:
            refs.add(ref_id)
    return sorted(refs)


def parse_edges_from_file(filepath: Path) -> list[dict]:
    """Extract edge declarations from a file's front-matter `edges:` list.

    Returns list of dicts with source/target/type. Source is inferred from the
    node's primary id field. Inline `<!-- EDGE: src --TYPE--> tgt -->` markers
    are a reserved format for future use; not currently consumed.
    """
    text = filepath.read_text(encoding="utf-8")
    fm_match = FRONT_MATTER_PATTERN.match(text)
    if not fm_match:
        return []
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
