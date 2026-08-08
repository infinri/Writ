"""Portable Cypher-script graph dump: render the whole graph as a replay
script, and import that script back into a Neo4j instance.

This is a separate serialization from `writ/export.py`'s markdown round-trip
-- a single-file, human-readable, git-diffable script (the `.sql`-dump
equivalent for Neo4j), not the bible/ markdown tree.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from writ.graph.db import Neo4jConnection

_STAGING_PROPERTY = "_dump_id"


def cypher_literal(value: object) -> str:
    """Render a Python scalar as a Cypher literal.

    Strings are single-quoted; backslash is escaped first so a value ending
    in a literal backslash cannot swallow the escape sequence that follows.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        escaped = (
            value.replace("\\", "\\\\")
            .replace("'", "\\'")
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
        )
        return f"'{escaped}'"
    if isinstance(value, list):
        return "[" + ", ".join(cypher_literal(v) for v in value) + "]"
    raise TypeError(f"cypher_literal: unsupported type {type(value).__name__}")


def _render_props(props: dict, extra: dict | None = None) -> str:
    merged = dict(props)
    if extra:
        merged.update(extra)
    parts = [f"{k}: {cypher_literal(v)}" for k, v in merged.items() if v is not None]
    return "{" + ", ".join(parts) + "}"


def render_cypher_dump(nodes: list[dict], edges: list[dict]) -> str:
    """Render a full-graph Cypher replay script from `get_all_nodes_for_dump`/
    `get_all_edges_cross_type` output.

    Nodes and edges are keyed by the same portable business id both methods
    already coalesce (rule_id, skill_id, ... -- never Neo4j's internal element
    id), staged as a temporary property so edges can re-`MATCH` their
    endpoints regardless of node type, then removed. Node/edge order is
    sorted by id so the same graph always renders to the same bytes. Edges in
    this schema carry no properties of their own (`create_edge` takes no
    props param), so none are rendered here.
    """
    lines: list[str] = []

    for node in sorted(nodes, key=lambda n: n["id"]):
        props_str = _render_props(node["props"], {_STAGING_PROPERTY: node["id"]})
        lines.append(f"CREATE (:{node['label']} {props_str});")

    for edge in sorted(edges, key=lambda e: (e["source_id"], e["target_id"])):
        lines.append(
            f"MATCH (a {{{_STAGING_PROPERTY}: {cypher_literal(edge['source_id'])}}}), "
            f"(b {{{_STAGING_PROPERTY}: {cypher_literal(edge['target_id'])}}}) "
            f"CREATE (a)-[:{edge['type']}]->(b);"
        )

    lines.append(f"MATCH (n) WHERE n.{_STAGING_PROPERTY} IS NOT NULL REMOVE n.{_STAGING_PROPERTY};")
    return "\n".join(lines) + "\n"


async def import_cypher_dump(db: "Neo4jConnection", text: str) -> dict:
    """Replace the graph's contents with a rendered Cypher dump.

    Wipes `db` first: the dump's CREATE statements are not idempotent (unlike
    `writ import-markdown`'s MERGE-based writes), so replaying into an
    already-populated graph raises a uniqueness constraint violation on any
    node whose business key (e.g. rule_id) already exists. Every real call
    site (bootstrap, CI, the test suite's self-heal hook) wants "make the
    graph exactly match this dump," not "merge this dump into whatever is
    already there" -- so restore semantics belong here, not on every caller.

    Runtime-record labels (RECORD_LABELS: Memory, Decision, FileChange, Commit)
    are preserved through the wipe UNLESS the incoming dump itself creates that
    label: a corpus dump is not the whole graph, so replaying one must not
    destroy operational records. A dump that does carry
    a record label (a full snapshot) still gets exact-replace semantics for it.

    Returns {"statements_run": N}.
    """
    from writ.graph.db._common import RECORD_LABELS

    labels_in_dump = set(re.findall(r"CREATE \(:([A-Za-z]+)", text))
    await db.clear_all(preserve_labels=RECORD_LABELS - labels_in_dump)
    statements = [line for line in text.splitlines() if line.strip()]
    for statement in statements:
        await db.execute(statement)
    return {"statements_run": len(statements)}
