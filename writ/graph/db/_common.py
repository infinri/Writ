"""Neo4j connection layer -- bolt protocol, connection pool, async session management.

Per PY-ASYNC-001: all Neo4j operations use AsyncSession (neo4j.AsyncGraphDatabase).
Sync drivers must never be used in async call chains.

Per PERF-IO-001: no sync I/O in the hot path (anything reachable from FastAPI endpoints).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from writ.graph.schema import METHODOLOGY_NODE_TYPES, NODE_ID_FIELDS

if TYPE_CHECKING:
    from neo4j import AsyncDriver


# --- Phase 1 additions: methodology node labels + expanded edge allowlist ---
# ALLOWED_EDGE_TYPES below is the single source of truth; it currently has 24 types
# (7 pre-existing + 8 Phase-1 + BELONGS_TO + INVOKES). Count programmatically, not from
# this comment. (Change C: APPLIES_TO + JUSTIFIED_BY retired, 19 -> 17.)

# Node-type maps derive from the canonical registry in writ.graph.schema (POL-3/C6) -- the
# methodology subset is every node type except the coding-rule `Rule`.
METHODOLOGY_NODE_LABELS: frozenset[str] = frozenset(METHODOLOGY_NODE_TYPES)

METHODOLOGY_NODE_ID_FIELDS: dict[str, str] = {
    k: v for k, v in NODE_ID_FIELDS.items() if k != "Rule"
}

# Runtime-record labels: operational state (mirrored memories, decision-memory
# records) that is NOT part of the corpus and has no bible/dump home. A corpus
# replay must never silently destroy these -- import_cypher_dump preserves any of
# them absent from the incoming dump. Deliberately NOT merged into NODE_ID_FIELDS:
# that registry drives bible parity/ingest semantics, which records must not enter.
RECORD_LABELS: frozenset[str] = frozenset({"Memory", "Decision", "FileChange", "Commit"})

ALLOWED_EDGE_TYPES: frozenset[str] = frozenset({
    # Pre-existing (Change C: APPLIES_TO + JUSTIFIED_BY retired)
    "DEPENDS_ON", "PRECEDES", "CONFLICTS_WITH", "SUPPLEMENTS",
    "SUPERSEDES", "RELATED_TO", "ABSTRACTS",
    # Phase 1 additions per plan Section 3.1
    "TEACHES", "COUNTERS", "DEMONSTRATES", "DISPATCHES",
    "GATES", "PRESSURE_TESTS", "CONTAINS", "ATTACHED_TO",
    # Phase 0 Wave B: Category membership edge.
    "BELONGS_TO",
    # 1.3b: INVOKES = orchestrator applies this methodology inline (one level);
    # DISPATCHES is reserved for spawning a SubagentRole. The edge-level
    # expression of the one-level constraint (enforced by detect_dispatch_invokes_invariant).
    "INVOKES",
    # Decision-memory Phase 1a: record edge types (type-acceptance only; record
    # endpoint resolution is a 1c/1d concern).
    "HAS_DECISION", "HAS_CHANGE", "HAS_COMMIT",
    "MOTIVATED_BY", "GOVERNED_BY", "INCLUDES", "REALIZES",
})

# Record-memory edge types (Decision-memory Phase 1a): these attach runtime
# records (Decision/FileChange/Commit) to rules and to each other; they are never
# authored as rule-to-rule corpus relationships. Named as a subset so the corpus
# set below is DERIVED, never a hand-maintained copy.
RECORD_EDGE_TYPES: frozenset[str] = frozenset({
    "HAS_DECISION", "HAS_CHANGE", "HAS_COMMIT",
    "MOTIVATED_BY", "GOVERNED_BY", "INCLUDES", "REALIZES",
})

# Corpus (rule/methodology cross-reference) edge types = every allowed type that
# is not a record edge. The interactive `writ add` authors rule-to-rule edges and
# offers this set; deriving it from ALLOWED_EDGE_TYPES keeps a single source so a
# new corpus edge type propagates to the CLI automatically.
CORPUS_EDGE_TYPES: frozenset[str] = ALLOWED_EDGE_TYPES - RECORD_EDGE_TYPES


# Valid (label -> id-field) endpoints for create_record_edge. Neo4j cannot bind a
# label or property key as a parameter, so create_record_edge interpolates them; it
# validates each endpoint against this allowlist first so only these known pairs can
# reach the query string (SEC-INJ: closes the label/field interpolation surface on
# the public method). NODE_ID_FIELDS covers Rule + methodology types; the four record
# and registry labels (off NODE_ID_FIELDS by design) are added explicitly.
_RECORD_EDGE_ENDPOINTS: dict[str, str] = {
    **NODE_ID_FIELDS,
    "Decision": "decision_id",
    "FileChange": "change_id",
    "Commit": "commit_hash",
    "Project": "name",
}


# Coalesce a node's primary id regardless of label, built from the canonical
# NODE_ID_FIELDS registry (not hardcoded) so it tracks the schema. `{v}` is the
# Cypher variable bound to the node. THE single coalesce source: integrity.py and
# methodology_ingest.py import this (a hardcoded duplicate was retired in DUP-S4).
_GRAPH_ID_COALESCE: str = (
    "coalesce("
    + ", ".join(f"{{v}}.{f}" for f in sorted(set(NODE_ID_FIELDS.values())))
    + ")"
)


def _id_or_match(node_var: str, param: str) -> str:
    """OR-clause that matches `node_var` if ANY of its primary-id properties
    equals `$param`, built from the canonical NODE_ID_FIELDS registry (not
    hardcoded) so adding a node type propagates automatically (Contract C).
    Mirrors _GRAPH_ID_COALESCE; used by create_edge and the batch_create_edges
    label-less fallback."""
    return " OR ".join(
        f"{node_var}.{f} = ${param}" for f in sorted(set(NODE_ID_FIELDS.values()))
    )


def _now_iso() -> str:
    """Current UTC time as an ISO-8601 string. The default ts for a record create
    that omits one (commit-time capture supplies its own; the direct record-edge
    test path does not)."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _coerce_neo4j_value(v):
    """Convert Python objects to Neo4j-compatible property values.

    Dates → ISO strings. Nested dicts → JSON strings (Neo4j doesn't store maps
    as node properties). Lists of primitives pass through.
    """
    import json
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if isinstance(v, dict):
        return json.dumps(v)
    if isinstance(v, list) and v and isinstance(v[0], dict):
        return json.dumps(v)
    return v


def _node_write_spec(
    node_type: str, data: dict, source_origin: str = "ingest",
    provenance: str | None = None,
) -> tuple[str, str, str, str, dict]:
    """Single source of truth for a node's MERGE identity + props, shared by
    create_rule / create_methodology_node / batch_create_nodes so the per-node and
    batch ingest paths produce byte-identical graph state (B5.2).

    Returns (label, id_field, node_id, project, props).
    INVARIANT (0.10 prop-parity): persist EXACTLY the parsed-dict keys -- never
    model_dump() (which would write defaulted fields the parser omits, so reconcile
    would then clear them as "stale"; test_clean_corpus_in_parity pins this).
    M.2: project is the composite MERGE key, excluded from props (a non-model prop,
    so prop-parity/reconcile never clear it). source_origin (0.10) records markdown
    provenance: 'ingest' has a markdown home; 'graph-authored' (propose/add/edit) does not.
    """
    if node_type == "Rule":
        id_field = "rule_id"
    elif node_type in METHODOLOGY_NODE_LABELS:
        id_field = METHODOLOGY_NODE_ID_FIELDS[node_type]
    else:
        raise ValueError(f"Unknown node_type: {node_type}")
    if id_field not in data:
        raise ValueError(f"{node_type} data missing required {id_field}")
    node_id = data[id_field]
    project = data.get("project", "writ")
    props = {
        k: _coerce_neo4j_value(v)
        for k, v in data.items() if k not in (id_field, "project")
    }
    props["source_origin"] = source_origin
    # 6.1: provenance is the 4-state refinement of source_origin. Resolution order:
    # explicit caller arg > frontmatter value carried in `data` (a re-ingested
    # graduated node keeps its lineage) > derive from source_origin (ingest ->
    # hand-authored, graph-authored -> proposed). It layers on source_origin, it
    # does not replace it (the reconcile exemption keys on provenance from 6.4).
    resolved = provenance if provenance is not None else props.get("provenance")
    if resolved is None:
        resolved = "hand-authored" if source_origin == "ingest" else "proposed"
    props["provenance"] = resolved
    return node_type, id_field, node_id, project, props


def _id_field_for_label(label: str) -> str:
    """The primary-id property name for a node label (Rule->rule_id, else the
    methodology id field). Used to build label-scoped, index-using edge matches."""
    return "rule_id" if label == "Rule" else METHODOLOGY_NODE_ID_FIELDS[label]


async def read_live_edges(
    driver: AsyncDriver, database: str, project: str
) -> list[tuple]:
    """Within-project edges as (type, src_id, tgt_id, src_prov, tgt_prov), with
    coalesced endpoint ids and null-endpoint rows dropped.

    Single source for the live-edge read that detect_edge_parity (integrity) and
    reconcile's stale-edge prune (methodology_ingest) both depend on: both must
    see the SAME edge shape and project scoping or the no-prune parity comparison
    diverges. Uses the canonical _GRAPH_ID_COALESCE (also used by the callers'
    DELETE clauses, so the ids match per node)."""
    a_id = _GRAPH_ID_COALESCE.format(v="a")
    b_id = _GRAPH_ID_COALESCE.format(v="b")
    async with driver.session(database=database) as session:
        result = await session.run(
            f"MATCH (a)-[r]->(b) WHERE a.project = $project AND b.project = $project "
            f"RETURN type(r) AS type, {a_id} AS src, {b_id} AS tgt, "
            "a.provenance AS src_prov, b.provenance AS tgt_prov",
            project=project,
        )
        return [
            (r["type"], r["src"], r["tgt"], r["src_prov"], r["tgt_prov"])
            async for r in result
            if r["src"] is not None and r["tgt"] is not None
        ]


async def read_live_nodes_with_keys(
    driver: AsyncDriver, database: str, project: str
) -> list[tuple]:
    """Within-project nodes as (id, keys, provenance), with coalesced id and
    null-id rows dropped.

    Single source for the live-node read that detect_prop_parity (integrity) and
    reconcile's stale-managed-prop clear (methodology_ingest) both depend on."""
    nid = _GRAPH_ID_COALESCE.format(v="n")
    async with driver.session(database=database) as session:
        result = await session.run(
            f"MATCH (n) WHERE n.project = $project "
            f"RETURN {nid} AS id, keys(n) AS keys, n.provenance AS provenance",
            project=project,
        )
        return [
            (r["id"], r["keys"], r["provenance"])
            async for r in result
            if r["id"] is not None
        ]


class GraphConnection(Protocol):
    """Connection interface for graph database operations.

    Per PY-PROTO-001: Protocol over ABC for pure interfaces.
    """

    async def get_rule(self, rule_id: str) -> dict | None: ...
    async def create_rule(self, rule_data: dict) -> str: ...
    async def create_edge(self, edge_type: str, source_id: str, target_id: str) -> None: ...
    async def traverse_neighbors(self, rule_id: str, hops: int) -> list[dict]: ...
    async def close(self) -> None: ...


class ProjectIdentityConflict(Exception):
    """A project name is already bound to a different non-None remote_url.

    Raised by create_project (decision-memory Phase 1b) rather than silently
    overwriting: the name<->remote_url 1:1 FAIL-LOUD guard.
    """
