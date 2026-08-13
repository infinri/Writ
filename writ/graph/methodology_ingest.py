"""Unified bible ingestion library (v1.5.0).

Single source of truth for the parse + validate + write pipeline that backs
both the `writ import-markdown` CLI command and the thin `scripts/migrate.py`
compatibility shim. Collapses the duplicate ingest loops that previously
lived in `writ/cli.py::import_markdown` (Rule-only) and
`scripts/migrate.py::run_methodology_migration` (methodology) into one
registry-dispatched walk per DRY-DUP-002.

Public surface:
- `ingest_path(path, db, only=None, dry_run=False) -> IngestReport`
- `finish_import(report, db, path, *, dry_run, no_export, compress, parsed_only, is_default_root) -> dict`
- `ingest_edges(parsed_nodes, parsed_edges, db) -> tuple[int, int]`
- `INGESTER_REGISTRY: dict[str, Callable]`
- `KNOWN_NODE_TYPES: frozenset[str]`
- `IngestReport`, `IngestError`

Per SOLID-OCP-002 the per-node dispatch is a registry lookup, not an if/elif
chain. Per API-ERROR-002 validation failures surface as typed
`IngestError(file, node_type, node_id, reason)` records, not raw Pydantic
tracebacks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from writ.graph.db import (
    _GRAPH_ID_COALESCE,
    ALLOWED_EDGE_TYPES,
    Neo4jConnection,
    read_live_edges,
    read_live_nodes_with_keys,
)
from writ.graph.ingest import (
    NODE_ID_FIELDS,
    discover_rule_files,
    extract_belongs_to_edges,
    parse_edges_from_file,
    parse_nodes_from_file,
    validate_parsed_node,
)
from writ.graph.schema import MANAGED_PROP_NAMES, PARITY_EXEMPT_PROVENANCE

if TYPE_CHECKING:
    from neo4j import AsyncDriver


# --- Registry ----------------------------------------------------------------

# Per SOLID-OCP-002: per-node-type dispatch is a registry lookup. Adding a
# new node type is a one-line registry edit, not a CLI-rewrite.
Ingester = Callable[[Neo4jConnection, dict], Awaitable[str]]


async def _ingest_rule(db: Neo4jConnection, clean: dict) -> str:
    return await db.create_rule(clean)


def _make_methodology_ingester(node_type: str) -> Ingester:
    async def _ingest(db: Neo4jConnection, clean: dict) -> str:
        return await db.create_methodology_node(node_type, clean)

    return _ingest


# The node labels the markdown ingest CANNOT author. Abstraction nodes are
# materialized from bible/abstractions.json by writ/compression/abstractions.py;
# discover_rule_files only yields *.md, so no markdown file can ever produce one,
# and _persist_abstraction_layer deletes the project's abstractions before every
# write, so a hand-authored one would not survive the next import either. Keeping
# a registry entry for a label nothing can author is what let the markdown parity
# oracle believe it owned 62 nodes and 186 edges it cannot see.
ARTIFACT_AUTHORED_NODE_TYPES: frozenset[str] = frozenset({"Abstraction"})

INGESTER_REGISTRY: dict[str, Ingester] = {
    "Rule": _ingest_rule,
    **{
        nt: _make_methodology_ingester(nt)
        for nt in NODE_ID_FIELDS
        if nt != "Rule" and nt not in ARTIFACT_AUTHORED_NODE_TYPES
    },
}

KNOWN_NODE_TYPES: frozenset[str] = frozenset(INGESTER_REGISTRY.keys())

# Every label the schema knows, minus the labels the markdown ingest can author.
# DERIVED, not hand-listed: parse_source drops any node_type absent from
# INGESTER_REGISTRY, so this set is exactly the labels the markdown oracle cannot
# produce, and judging a blind spot is how validate came to recommend deleting 186
# correct edges. A future artifact-authored type is exempt by construction.
ORACLE_BLIND_LABELS: frozenset[str] = (
    frozenset(NODE_ID_FIELDS) - frozenset(INGESTER_REGISTRY)
)


async def read_oracle_blind_node_ids(
    driver: AsyncDriver, database: str, project: str
) -> set[str]:
    """Coalesced ids of live nodes whose label the markdown oracle cannot author.

    One query, one round trip. Shared by detect_edge_parity (the check that
    REPORTS stale edges) and reconcile's stale-edge prune (the command that
    DELETES them), so the exemption cannot drift between them. Project scoping
    matches read_live_edges exactly (n.project = $project, not coalesce), because
    the ids are compared against rows that read returns.
    """
    if not ORACLE_BLIND_LABELS:
        return set()
    nid = _GRAPH_ID_COALESCE.format(v="n")
    async with driver.session(database=database) as session:
        result = await session.run(
            f"MATCH (n) WHERE n.project = $project "
            f"AND ANY(l IN labels(n) WHERE l IN $labels) "
            f"RETURN {nid} AS id",
            project=project,
            labels=sorted(ORACLE_BLIND_LABELS),
        )
        return {r["id"] async for r in result if r["id"] is not None}


# --- Typed records -----------------------------------------------------------

@dataclass(frozen=True)
class IngestError:
    """Structured validation / write error (API-ERROR-002).

    `__str__` returns a single-line summary suitable for stderr; no Python
    traceback is included.
    """

    file: Path
    node_type: str | None
    node_id: str | None
    field: str | None
    reason: str

    def __str__(self) -> str:
        nt = self.node_type or "?"
        nid = self.node_id or "?"
        field_part = f" [field={self.field}]" if self.field else ""
        return f"{self.file}:{nt} '{nid}'{field_part} -- {self.reason}"


@dataclass
class IngestReport:
    """Per-type counts + edge counts + structured errors for one ingest run."""

    counts_by_type: dict[str, int] = field(default_factory=dict)
    edges_created: int = 0
    edges_dangling: int = 0
    errors: list[IngestError] = field(default_factory=list)
    ingested: list[tuple[Path, str, str]] = field(default_factory=list)
    dry_run: bool = False
    # Post-write read-back: (verified_count, [mismatched ids]). counts_by_type counts
    # PARSED nodes, and Neo4j's properties_set counter reports the SET operation rather
    # than a delta (it is identical on a no-op re-run), so neither can tell an applied
    # edit from a silent stale apply. Reading the nodes back can.
    verification: tuple[int, list[str]] | None = None

    def render(self) -> str:
        """Human-readable multi-line summary for stdout."""
        lines: list[str] = []
        header = "[DRY RUN] " if self.dry_run else ""
        if self.counts_by_type:
            lines.append(f"{header}Imported nodes by type:")
            for nt in sorted(self.counts_by_type):
                lines.append(f"  {nt}: {self.counts_by_type[nt]}")
            total = sum(self.counts_by_type.values())
            lines.append(f"  Total: {total}")
        else:
            lines.append(f"{header}Imported nodes by type: (none)")
        # When errors exist alongside successful ingests, list the successfully
        # ingested files so the user can see which files DID land (partial-
        # success diagnostics). Skipped in clean runs to keep output compact.
        if self.errors and self.ingested:
            lines.append("Ingested files:")
            for filepath, node_type, node_id in self.ingested:
                lines.append(f"  {filepath}:{node_type} '{node_id}'")
        if not self.dry_run and self.verification is not None:
            verified, mismatched = self.verification
            if mismatched:
                lines.append(
                    f"VERIFY FAILED: {len(mismatched)} node(s) did not match source "
                    f"after the write ({verified} verified): {', '.join(mismatched[:10])}"
                )
                lines.append(
                    "  The write reported success but the graph kept older values. "
                    "Re-run the import, then re-read before treating it as applied."
                )
            else:
                lines.append(f"Verified against source after write: {verified} node(s)")
        if not self.dry_run:
            lines.append(
                f"Edges created: {self.edges_created} "
                f"({self.edges_dangling} skipped: dangling)"
            )
        if self.errors:
            lines.append(f"Errors: {len(self.errors)}")
        return "\n".join(lines)


# --- Core pipeline -----------------------------------------------------------

def _extract_pydantic_field(exc: Exception) -> tuple[str | None, str]:
    """Best-effort extract field names + messages from a Pydantic / ValueError.

    When the exception carries multiple field errors (Pydantic's `.errors()`),
    the returned `field` is the first offending field name and the returned
    reason enumerates ALL field-name -- message pairs joined by `; `. This
    keeps the CLI line single-row while still surfacing every offending field
    name (the test contract requires the offending field to appear in output).
    """
    errors_attr = getattr(exc, "errors", None)
    if callable(errors_attr):
        try:
            errs = errors_attr()
        except Exception:
            errs = None
        if errs:
            parts = []
            first_field: str | None = None
            for e in errs:
                loc = e.get("loc") or ()
                fname = str(loc[0]) if loc else "?"
                if first_field is None and loc:
                    first_field = fname
                msg = e.get("msg") or "validation error"
                parts.append(f"{fname}: {msg}")
            return first_field, "; ".join(parts)
    return None, str(exc)


async def ingest_path(
    path: Path,
    db: Neo4jConnection,
    *,
    only: set[str] | None = None,
    dry_run: bool = False,
    project: str = "writ",
) -> IngestReport:
    """Walk `path` recursively for *.md, parse each, validate, write via registry.

    `only` -- when non-empty, files whose parsed `node_type` is not in the set
    are skipped (parsed-and-discarded). `dry_run` -- parse + validate without
    DB writes. Returns an `IngestReport` with per-type counts and structured
    errors.
    """
    report = IngestReport(dry_run=dry_run)
    if not path.exists():
        report.errors.append(
            IngestError(
                file=path,
                node_type=None,
                node_id=None,
                field=None,
                reason=f"path does not exist: {path}",
            )
        )
        return report

    files = discover_rule_files(path)
    parsed_nodes, parsed_edges, node_source = _parse_and_validate_files(
        files, only, dry_run, report
    )
    parsed_nodes = _dedupe_dual_location(parsed_nodes)
    cleaned = _build_write_specs(parsed_nodes, project)
    await _write_nodes(
        db, cleaned, parsed_nodes, parsed_edges, node_source, dry_run, project, report
    )
    return report


def _parse_and_validate_files(
    files: list[Path],
    only: set[str] | None,
    dry_run: bool,
    report: IngestReport,
) -> tuple[list[dict], list[dict], dict[int, Path]]:
    """Parse + validate each discovered file, accumulating structured errors into
    `report`. Returns (parsed_nodes, parsed_edges, node_source). Extracted from
    ingest_path (B6e); node_source maps id(node)->source file for write-time errors."""
    parsed_nodes: list[dict] = []
    parsed_edges: list[dict] = []
    node_source: dict[int, Path] = {}

    for filepath in files:
        try:
            file_nodes = parse_nodes_from_file(filepath)
        except Exception as exc:
            field_name, reason = _extract_pydantic_field(exc)
            report.errors.append(IngestError(
                file=filepath, node_type=None, node_id=None,
                field=field_name, reason=f"parse error: {reason}",
            ))
            continue

        for node in file_nodes:
            node_type = node.get("node_type", "Rule")
            if only and node_type not in only:
                continue
            if node_type not in INGESTER_REGISTRY:
                id_field = NODE_ID_FIELDS.get(node_type, "id")
                report.errors.append(IngestError(
                    file=filepath, node_type=node_type, node_id=node.get(id_field),
                    field=None,
                    reason=(
                        f"unknown node_type '{node_type}' (expected one of "
                        f"{sorted(KNOWN_NODE_TYPES)})"
                    ),
                ))
                continue

            id_field = NODE_ID_FIELDS.get(node_type, "id")

            # Schema/field validation runs FIRST so missing-field errors are always
            # reported; a reachability 'continue' must NOT suppress them.
            node_invalid = False
            try:
                validate_parsed_node(node)
            except Exception as exc:
                # validate_parsed_node wraps Pydantic in a ValueError; unwrap.
                cause = exc.__cause__ if exc.__cause__ is not None else exc
                field_name, reason = _extract_pydantic_field(cause)
                report.errors.append(IngestError(
                    file=filepath, node_type=node_type, node_id=node.get(id_field),
                    field=field_name, reason=reason,
                ))
                node_invalid = True

            # Reachability invariant (live writes only): every ingested node should
            # carry a 'category'. Reported as an ADDITIONAL error but does NOT abort
            # an otherwise-valid node. dry_run previews + Category nodes are exempt.
            if not dry_run and node_type != "Category" and not node.get("category"):
                report.errors.append(IngestError(
                    file=filepath, node_type=node_type, node_id=node.get(id_field),
                    field="category",
                    reason=(
                        f"reachability: {node_type} "
                        f"'{node.get(id_field)}' has no category; "
                        "node would be unreachable from any Category"
                    ),
                ))

            if node_invalid:
                continue

            node_source[id(node)] = filepath
            parsed_nodes.append(node)

        # Collect edges from the file's front-matter (only-filter is applied at
        # edge-creation time so cross-type edges still resolve).
        try:
            parsed_edges.extend(parse_edges_from_file(filepath))
        except Exception as exc:
            report.errors.append(IngestError(
                file=filepath, node_type=None, node_id=None,
                field=None, reason=f"edge parse error: {exc}",
            ))

    return parsed_nodes, parsed_edges, node_source


def _build_write_specs(parsed_nodes: list[dict], project: str) -> list[tuple[str, dict, dict]]:
    """Cleaned write-spec per node (no DB): strip non-persisted keys + stamp the
    project (M.1). Returns (node_type, clean, original_node). Shared by the dry-run
    count and the batch write."""
    cleaned: list[tuple[str, dict, dict]] = []
    for node in parsed_nodes:
        node_type = node["node_type"]
        clean = {
            k: v for k, v in node.items()
            if k != "node_type" and not k.startswith("_") and k != "edges"
        }
        clean["project"] = project  # M.1: stamp the project on every ingested node
        cleaned.append((node_type, clean, node))
    return cleaned


async def _verify_written_nodes(
    db: Neo4jConnection, cleaned: list[tuple[str, dict, dict]]
) -> tuple[int, list[str]]:
    """Read the just-written nodes back and confirm the graph holds what we sent.

    A `SET n +=` that applies nothing is indistinguishable from a successful write in
    both the parse count and Neo4j's properties_set counter (the latter reports the SET
    operation, so it is identical on a no-op re-run). One 2026-08-06 run reported full
    success while the node kept its old values, and the only reliable check was reading
    the node back by hand -- so the importer does it.

    Compares every scalar property the write sent (lists and JSON-encoded values
    included; both sides pass through the same coercion). Returns
    (verified_count, mismatched_ids); a read failure is reported as a mismatch rather
    than silently passing.
    """
    from writ.graph.db._common import _node_write_spec

    expected: dict[tuple[str, str], dict] = {}
    for node_type, clean, _node in cleaned:
        try:
            label, id_field, node_id, project, props = _node_write_spec(node_type, clean)
        except Exception:
            continue
        expected[(label, id_field, node_id, project)] = props

    verified = 0
    mismatched: list[str] = []
    by_group: dict[tuple[str, str], list[tuple[str, str, dict]]] = {}
    for (label, id_field, node_id, project), props in expected.items():
        by_group.setdefault((label, id_field), []).append((node_id, project, props))

    for (label, id_field), rows in by_group.items():
        ids = [node_id for node_id, _p, _props in rows]
        query = (
            f"MATCH (n:{label}) WHERE n.{id_field} IN $ids "
            f"RETURN n.{id_field} AS id, properties(n) AS props"
        )
        try:
            records = await db._run(query, ids=ids)
            live = {r["id"]: r["props"] for r in records}
        except Exception:
            mismatched.extend(ids)
            continue
        for node_id, _project, props in rows:
            actual = live.get(node_id)
            if actual is None:
                mismatched.append(node_id)
                continue
            drifted = [k for k, v in props.items() if actual.get(k) != v]
            if drifted:
                mismatched.append(node_id)
            else:
                verified += 1
    return verified, sorted(mismatched)


async def _write_nodes(
    db: Neo4jConnection,
    cleaned: list[tuple[str, dict, dict]],
    parsed_nodes: list[dict],
    parsed_edges: list[dict],
    node_source: dict[int, Path],
    dry_run: bool,
    project: str,
    report: IngestReport,
) -> None:
    """Record (dry-run) or batch-write nodes, then edges. Mutates `report`."""
    def _record_ingested(node_type: str, node: dict) -> None:
        report.counts_by_type[node_type] = report.counts_by_type.get(node_type, 0) + 1
        id_field = NODE_ID_FIELDS.get(node_type, "id")
        report.ingested.append((
            node_source.get(id(node), Path("<unknown>")),
            node_type,
            node.get(id_field, "?"),
        ))

    if dry_run:
        for node_type, _clean, node in cleaned:
            _record_ingested(node_type, node)
    elif parsed_nodes:
        # B5.2: one UNWIND-grouped transaction instead of a create_* round-trip per
        # node. Atomic -- a write failure rolls the whole batch back and is reported
        # as one error. Nodes are schema-validated before this point, so a failure
        # here is a genuine DB error, not per-node invalidity.
        #
        # C3 (audit): apply_constraints MUST NOT be swallowed. The DDL is idempotent
        # (IF [NOT] EXISTS), so "already exists" is never the failure path; a real
        # failure (pre-existing duplicate violating a UNIQUE constraint, permissions,
        # DB error) means the composite-uniqueness race guard is NOT applied --
        # ingesting without it is the unsafe state. Fail loud so the operator sees it.
        await db.apply_constraints()
        try:
            await db.batch_create_nodes([(nt, clean) for nt, clean, _ in cleaned])
            for node_type, _clean, node in cleaned:
                _record_ingested(node_type, node)
            report.verification = await _verify_written_nodes(db, cleaned)
        except Exception as exc:
            report.errors.append(IngestError(
                file=Path("<batch>"), node_type=None, node_id=None,
                field=None, reason=f"batch db write failed: {exc}",
            ))

    # Edges -- skip in dry-run.
    if not dry_run and parsed_nodes:
        created, dangling = await ingest_edges(parsed_nodes, parsed_edges, db, project=project)
        report.edges_created = created
        report.edges_dangling = dangling


def _dedupe_dual_location(parsed_nodes: list[dict]) -> list[dict]:
    """Collapse dual-location duplicates to ONE record per (node_type, id).

    The same primary id can appear in two files: the canonical
    bible/methodology/<id>.md front-matter copy and a domain rules.md RULE START
    copy. Prefer the front-matter copy (tagged `_source_format == 'front-matter'`
    by the parser) so the survivor keeps front-matter-only fields. Shared by
    ingest_path (the writer) and parse_source (the reconcile oracle) so both
    collapse identically. Input order is otherwise preserved.
    """
    deduped: list[dict] = []
    by_id: dict[tuple[str, str], int] = {}
    for node in parsed_nodes:
        node_type = node.get("node_type", "Rule")
        id_field = NODE_ID_FIELDS.get(node_type)
        nid = node.get(id_field) if id_field else None
        if nid is None:
            deduped.append(node)
            continue
        key = (node_type, nid)
        if key not in by_id:
            by_id[key] = len(deduped)
            deduped.append(node)
            continue
        existing_idx = by_id[key]
        existing = deduped[existing_idx]
        incoming_is_fm = node.get("_source_format") == "front-matter"
        existing_is_fm = existing.get("_source_format") == "front-matter"
        if incoming_is_fm and not existing_is_fm:
            deduped[existing_idx] = node
    return deduped


def parse_source(
    path: Path, only: set[str] | None = None
) -> tuple[list[dict], list[dict]]:
    """Read-only parse of a bible tree into the (parsed_nodes, parsed_edges) a
    clean ingest WOULD write.

    Mirrors ingest_path's INCLUSION logic -- same discover, per-file parse,
    only/registry filter, field-validation DROP (a node that fails
    validate_parsed_node is not written, so it is not parsed here either), and
    dual-location dedup -- but performs NO writes and NO error reporting. The
    reconcile oracle (compute_expected_graph) shares this with the writer; the
    0.10-C test-pin (oracle == Neo4j read-back on the real corpus) guards against
    any inclusion divergence between the two paths.
    """
    parsed_nodes: list[dict] = []
    parsed_edges: list[dict] = []
    for filepath in discover_rule_files(path):
        try:
            file_nodes = parse_nodes_from_file(filepath)
        except Exception:
            continue
        for node in file_nodes:
            node_type = node.get("node_type", "Rule")
            if only and node_type not in only:
                continue
            if node_type not in INGESTER_REGISTRY:
                continue
            try:
                validate_parsed_node(node)
            except Exception:
                # Field-invalid: ingest_path drops it from the write, so the
                # oracle must not expect it either.
                continue
            parsed_nodes.append(node)
        try:
            parsed_edges.extend(parse_edges_from_file(filepath))
        except Exception:
            continue
    return _dedupe_dual_location(parsed_nodes), parsed_edges


def compute_expected_graph(
    path: Path, only: set[str] | None = None
) -> tuple[set[str], set[tuple[str, str, str]]]:
    """In-memory oracle: the node-ids and (type, source, target) edge tuples a
    CLEAN ingest of `path` should produce.

    known_ids = the parsed node ids (the clean-DB case: nothing pre-exists, so
    cross-reference resolution sees only this corpus). Shares parse_source +
    derive_edges with the writer so the reconcile target cannot drift from what
    ingest actually writes. No DB access.
    """
    parsed_nodes, parsed_edges = parse_source(path, only=only)
    node_ids: set[str] = set()
    for node in parsed_nodes:
        nt = node.get("node_type", "Rule")
        id_field = NODE_ID_FIELDS.get(nt)
        if id_field and id_field in node:
            node_ids.add(node[id_field])
    edges, _dangling = derive_edges(parsed_nodes, parsed_edges, node_ids)
    # Model create_edge's MATCH..MATCH..MERGE semantics: an edge persists ONLY
    # when BOTH endpoints exist as nodes. derive_edges only known-id-filters
    # declared edges (to preserve the writer's created/dangling counts); the
    # oracle additionally drops any derived edge to a non-existent endpoint
    # (e.g. a BELONGS_TO to a category id that is not a parsed node), exactly as
    # the write no-ops it. Without this the oracle would over-count edges the
    # graph never holds, and the 0.10-C pin would (correctly) fail.
    edge_tuples = {
        (e["type"], e["source"], e["target"])
        for e in edges
        if e["source"] in node_ids and e["target"] in node_ids
    }
    return node_ids, edge_tuples


def expected_managed_props(
    path: Path, only: set[str] | None = None
) -> dict[str, set[str]]:
    """Per node-id, the MANAGED prop keys the SOURCE declares (0.10 prop completion).

    Shares parse_source with the writer, so a node's expected managed keys are
    exactly the managed-named keys a clean ingest would write. A live node carrying
    a MANAGED prop NOT in its set here has a stale prop (the upsert-only no-prune
    drift at the property level): reconcile clears it, detect_prop_parity flags it.
    A parsed node with no managed props maps to an empty set (still a key) so a
    caller can distinguish "source has zero managed props" from "node unknown to
    the oracle" (absent key -> never prop-reconciled).
    """
    parsed_nodes, _edges = parse_source(path, only=only)
    out: dict[str, set[str]] = {}
    for node in parsed_nodes:
        nt = node.get("node_type", "Rule")
        id_field = NODE_ID_FIELDS.get(nt)
        if not id_field or id_field not in node:
            continue
        out[node[id_field]] = {k for k in node if k in MANAGED_PROP_NAMES}
    return out




async def reconcile(path: Path, db: Neo4jConnection, project: str = "writ") -> dict:
    """Make the graph match the source-of-truth at rest.

    M.1 KEYSTONE: scoped to ONE `project` (default 'writ'). Every live read,
    delete, and prop-clear filters `WHERE n.project=$project`, so reconciling
    one project's bible NEVER deletes or alters another project's nodes/edges.
    Without this, the whole-graph DETACH DELETE below would erase project 2 when
    handed project 1's bible.

    Import `path` (upsert), then DELETE graph nodes and edges ABSENT from the
    oracle (`compute_expected_graph`), EXEMPTING graph-first nodes (6.4:
    `provenance in {proposed, graduation_pending}` -- the same authoritative axis
    the parity checks key on) and any edge incident to one. Closes the upsert-only no-prune gap
    (project_ingest_no_prune): a renamed or deleted source node/edge otherwise
    leaves a stale graph artifact forever (the 1.3a stale-edge class).

    FULL-CORPUS ONLY -- never run on a partial / only-filtered source, or it
    deletes everything the partial omits. Refuses an empty oracle as a guard.
    Returns {"deleted_nodes": [...], "deleted_edges": [(type, src, tgt), ...],
    "cleared_props": {node_id: [prop, ...]}}.
    """
    expected_nodes, expected_edges = compute_expected_graph(path)
    if not expected_nodes:
        raise ValueError(
            "refusing to reconcile against an empty oracle "
            f"(no parseable nodes under {path}); this would delete the graph"
        )

    await ingest_path(path, db, project=project)

    nid = _GRAPH_ID_COALESCE.format(v="n")
    a_id = _GRAPH_ID_COALESCE.format(v="a")
    b_id = _GRAPH_ID_COALESCE.format(v="b")

    # Three sequential prune phases. Each later phase reads in a fresh session, so it
    # sees only the survivors of the prior phase's autocommit deletes -- the ordering
    # (nodes -> edges -> props) is load-bearing and preserved by awaiting in turn.
    stale_node_ids = await _reconcile_delete_stale_nodes(db, expected_nodes, project, nid)
    stale_edges = await _reconcile_delete_stale_edges(db, expected_edges, project, a_id, b_id)
    cleared_props = await _reconcile_clear_stale_props(db, path, project, nid)

    return {
        "deleted_nodes": stale_node_ids,
        "deleted_edges": stale_edges,
        "cleared_props": cleared_props,
    }


async def _reconcile_delete_stale_nodes(
    db: Neo4jConnection, expected_nodes, project: str, nid: str
) -> list[str]:
    """reconcile phase A: DETACH DELETE live nodes absent from the oracle (DETACH removes
    their incident edges too). 6.4: exempt graph-first nodes (proposed | graduation_pending)
    -- the authoritative provenance axis the parity checks (integrity.py) key on. Cycle 7:
    also exempt nodes whose label the oracle cannot author (ORACLE_BLIND_LABELS). M.1:
    scoped to this project, so another project's nodes are never even read. Returns the
    sorted stale node ids."""
    async with db._driver.session(database=db._database) as s:
        res = await s.run(
            f"MATCH (n) WHERE n.project = $project "
            f"RETURN {nid} AS id, n.provenance AS provenance, labels(n) AS labels",
            project=project,
        )
        live_nodes = [
            (r["id"], r["provenance"], r["labels"])
            async for r in res
            if r["id"] is not None
        ]
        stale_node_ids = sorted(
            node_id
            for (node_id, provenance, labels) in live_nodes
            if node_id not in expected_nodes
            and provenance not in PARITY_EXEMPT_PROVENANCE
            # Cycle 7: never delete a node the oracle cannot author. ingest_path
            # (called at the top of reconcile) materializes the abstraction layer,
            # and this phase used to delete it again on the same run.
            and not (set(labels or []) & ORACLE_BLIND_LABELS)
        )
        if stale_node_ids:
            await s.run(
                f"MATCH (n) WHERE {nid} IN $ids AND n.project = $project DETACH DELETE n",
                ids=stale_node_ids,
                project=project,
            )
    return stale_node_ids


async def _reconcile_delete_stale_edges(
    db: Neo4jConnection, expected_edges, project: str, a_id: str, b_id: str
) -> list:
    """reconcile phase B: delete live within-project edges absent from the oracle whose
    neither endpoint is graph-first. Reads in a fresh session so it sees only survivors of
    phase A's DETACH DELETE (autocommit). Shared read with integrity.detect_edge_parity.
    Batched by relationship type (the type must be a Cypher literal, not a parameter).
    Returns the stale (etype, src, tgt) list."""
    live_edges = await read_live_edges(db._driver, db._database, project)
    blind_ids = await read_oracle_blind_node_ids(db._driver, db._database, project)

    # An edge is stale when it is absent from the oracle, neither endpoint is graph-first
    # (a proposed | graduation_pending node's edges are intentionally graph-only), and
    # neither endpoint is a label the oracle cannot author (cycle 7).
    stale_edges = [
        (etype, src, tgt)
        for (etype, src, tgt, sp, tp) in live_edges
        if (etype, src, tgt) not in expected_edges
        and sp not in PARITY_EXEMPT_PROVENANCE
        and tp not in PARITY_EXEMPT_PROVENANCE
        and src not in blind_ids
        and tgt not in blind_ids
    ]
    edges_by_type: dict[str, list[dict]] = {}
    for etype, src, tgt in stale_edges:
        if etype not in ALLOWED_EDGE_TYPES:
            continue  # never interpolate an unvalidated type
        edges_by_type.setdefault(etype, []).append({"src": src, "tgt": tgt})
    if edges_by_type:
        async with db._driver.session(database=db._database) as s:
            for etype, pairs in edges_by_type.items():
                await s.run(
                    f"UNWIND $pairs AS p MATCH (a)-[r:`{etype}`]->(b) "
                    f"WHERE {a_id} = p.src AND {b_id} = p.tgt "
                    f"AND a.project = $project AND b.project = $project DELETE r",
                    pairs=pairs,
                    project=project,
                )
    return stale_edges


async def _reconcile_clear_stale_props(
    db: Neo4jConnection, path: Path, project: str, nid: str
) -> dict:
    """reconcile phase C (0.10 prop completion): clear stale MANAGED props -- present in a
    live node but ABSENT from its source (the upsert cannot remove an omitted prop). Only
    MANAGED_PROP_NAMES are touched; graph-first nodes are exempt; a node unknown to the
    oracle is skipped (never wiped). Stale nodes are already gone (phase A), so this only
    sees survivors. Shared read with integrity.detect_prop_parity. Returns {node_id: [prop]}."""
    expected_props = expected_managed_props(path)
    cleared_props: dict[str, list[str]] = {}
    prop_rows = await read_live_nodes_with_keys(db._driver, db._database, project)
    for node_id, keys, provenance in prop_rows:
        if provenance in PARITY_EXEMPT_PROVENANCE or node_id not in expected_props:
            continue
        stale = sorted(
            {k for k in (keys or []) if k in MANAGED_PROP_NAMES} - expected_props[node_id]
        )
        if stale:
            cleared_props[node_id] = stale
    if cleared_props:
        # One round-trip: UNWIND the (id -> {prop: null}) updates. SET n += map
        # removes each key whose value is null (Neo4j: a null prop is absence),
        # matching the prior per-node `SET n.k = null`.
        updates = [
            {"id": node_id, "nulls": {k: None for k in stale}}
            for node_id, stale in cleared_props.items()
        ]
        async with db._driver.session(database=db._database) as s:
            await s.run(
                f"UNWIND $updates AS u MATCH (n) WHERE {nid} = u.id "
                f"AND n.project = $project SET n += u.nulls",
                updates=updates,
                project=project,
            )
    return cleared_props


def derive_edges(
    parsed_nodes: list[dict],
    parsed_edges: list[dict],
    known_ids: set[str],
) -> tuple[list[dict], int]:
    """Pure: the edge set a clean ingest WOULD create from this source, plus the
    pre-write dangling count.

    Factored out of ingest_edges (0.10) so the reconcile oracle
    (`compute_expected_graph`) and the writer share ONE derivation -- a
    derive-vs-derive divergence is the silent-drift class 0.10 closes. Returns
    (edges, dangling) where `edges` is a list of {type, source, target} dicts in
    the same 4-source order ingest uses, and `dangling` counts declared edges
    that reference an unknown id or omit a field (knowable WITHOUT writing).
    Create-time failures (a target that does not resolve in Neo4j) are counted by
    the caller, not here -- they are a write-boundary concern.
    """
    edges: list[dict] = []
    dangling = 0

    # 1. Front-matter declared edges.
    for edge in parsed_edges:
        src = edge.get("source")
        tgt = edge.get("target")
        etype = edge.get("type")
        if not src or not tgt or not etype:
            dangling += 1
            continue
        if src not in known_ids or tgt not in known_ids:
            dangling += 1
            continue
        edges.append({"type": etype, "source": src, "target": tgt})

    # 2. Legacy RELATED_TO skeleton edges from cross-references on Rule nodes.
    rule_ids = {
        n["rule_id"] for n in parsed_nodes
        if n.get("node_type", "Rule") == "Rule" and "rule_id" in n
    }
    for node in parsed_nodes:
        if node.get("node_type", "Rule") != "Rule":
            continue
        own_id = node.get("rule_id")
        if not own_id:
            continue
        for ref_id in node.get("_cross_references", []):
            if ref_id in rule_ids:
                edges.append({"type": "RELATED_TO", "source": own_id, "target": ref_id})

    # 3. BELONGS_TO category edges, derived from each node's `category` value.
    # extract_belongs_to_edges was unit-tested but never wired into ingest;
    # without this a real re-import produced zero BELONGS_TO edges and left
    # every node unreachable from its Category.
    edges.extend(extract_belongs_to_edges(parsed_nodes))

    # 4. Category tree edges: a child Category BELONGS_TO its parent Category, so
    # non-leaf parent categories (whose members live in child leaf categories)
    # are graph-connected rather than orphaned. `parent` is a category_id or null.
    for node in parsed_nodes:
        if node.get("node_type") != "Category":
            continue
        parent = node.get("parent")
        child_id = node.get("category_id")
        if not parent or not child_id:
            continue
        edges.append({"type": "BELONGS_TO", "source": child_id, "target": parent})

    return edges, dangling


async def ingest_edges(
    parsed_nodes: list[dict],
    parsed_edges: list[dict],
    db: Neo4jConnection,
    project: str = "writ",
) -> tuple[int, int]:
    """Create methodology + cross-reference edges; return (created, dangling).

    M.1: edges are stamped with `project`. M.2 (closed): known_ids below is now
    project-scoped via get_all_rules(project=project), so a cross-project id
    reference is reported DANGLING instead of silently resolving to another
    project's node that happens to share the same id.
    """
    # Build the set of node IDs known to the graph: parsed-this-run union the
    # pre-existing Rule corpus.
    parsed_ids: set[str] = set()
    id_to_label: dict[str, str] = {}  # B5.2: endpoint id -> label for index-using matches
    for node in parsed_nodes:
        nt = node.get("node_type", "Rule")
        id_field = NODE_ID_FIELDS.get(nt)
        if id_field and id_field in node:
            parsed_ids.add(node[id_field])
            id_to_label[node[id_field]] = nt

    # C-series fail-loud (Wave 1): a real get_all_rules failure must propagate,
    # not be silently converted to "no pre-existing rules" (which mislabels every
    # valid cross-reference to an existing Rule as dangling). Mirrors the
    # apply_constraints fail-loud in _write_nodes (C3). ingest_edges is awaited
    # directly by _write_nodes with no swallowing wrapper, so this surfaces to
    # ingest_path and its callers.
    existing_rule_ids = {
        r["rule_id"] for r in await db.get_all_rules(project=project)
    }
    known_ids = parsed_ids | existing_rule_ids
    # Pre-existing ids resolved via get_all_rules() are Rule nodes.
    for rid in existing_rule_ids:
        id_to_label.setdefault(rid, "Rule")

    edges, dangling = derive_edges(parsed_nodes, parsed_edges, known_ids)

    # B5.2: one UNWIND-grouped transaction instead of a create_edge round-trip per
    # edge, with label-scoped (id, project)-index seeks for the endpoints. created
    # counts every valid-type edge (== the per-edge loop); write_dangling counts
    # unknown-type edges (create_edge's old ValueError path).
    created, write_dangling = await db.batch_create_edges(
        edges, project=project, id_to_label=id_to_label
    )
    return created, dangling + write_dangling


async def finish_import(
    report: IngestReport,
    db: Neo4jConnection,
    path: Path,
    *,
    dry_run: bool,
    no_export: bool,
    compress: bool,
    parsed_only: set[str] | None,
    is_default_root: bool,
) -> dict:
    """Post-ingest orchestration for `writ import-markdown`.

    Runs the two independent post-write gates -- (b) auto-export the Rule
    corpus back to markdown, and (c) refresh the Abstraction layer (compress
    or materialize-from-artifact) -- and returns the outcome as structured
    data. Typer-free: never calls `typer.echo`; the CLI command renders the
    returned dict. Keys: `exported` (export_result dict|None), `compressed`
    (comp_result dict|None), `materialized` (int|None), `compress_import_error`
    (Exception|None). `is_default_root` is computed by the caller from the CLI
    default constant and passed in, keeping this library free of the CLI
    default.
    """
    result: dict = {
        "exported": None,
        "compressed": None,
        "materialized": None,
        "compress_import_error": None,
    }

    total_nodes = sum(report.counts_by_type.values())
    # Auto-export only on a full-corpus import against the canonical
    # bible root. On a subdirectory import (e.g. `bible/methodology/`)
    # the exporter would write the WHOLE graph back through a
    # file-location lookup that only sees files inside that subdir;
    # rules whose original files live outside the scope fall through
    # to `<output_dir>/<domain>/rules.md` and create bogus duplicates.
    if (
        not dry_run
        and not no_export
        and not report.errors
        and total_nodes > 0
        and parsed_only is None
        and is_default_root
    ):
        # Rule-only auto-export: regenerates the derived domain rules.md
        # files from the graph. Methodology nodes (Skill/Playbook/.../and
        # the 12 dual-location rules that live as front-matter in
        # bible/methodology/) are hand-authored SOURCE and are NOT
        # rewritten here -- the rule export excludes the 12 canonical ids
        # (group_rules_by_file) so it never clobbers their front-matter.
        from writ.export import export_rules_to_markdown

        result["exported"] = await export_rules_to_markdown(db, path)

    # Abstraction layer refresh after a successful, node-producing,
    # non-dry-run ingest. Precedence (Approach A: durable abstraction
    # persistence):
    #   --compress  -> run_compression: recompute clusters + write graph
    #                  + write the artifact. The heavy path; needs the
    #                  [fallback] sentence-transformers dep, which
    #                  production installs exclude -- so a missing dep
    #                  warns and continues (never fails the ingest).
    #   else artifact exists -> materialize_abstractions_from_artifact:
    #                  dep-free reproduce of the graph layer from the
    #                  committed abstractions.json (no clustering). The
    #                  artifact is resolved relative to the ingest path
    #                  (<path>/abstractions.json), which equals the repo
    #                  default bible/abstractions.json for a default-root
    #                  import.
    #   else -> nothing.
    #
    # Gated on parsed_only is None: a --only filtered import is a
    # surgical partial import (it does not rebuild the full graph), so
    # it must not trigger an abstraction-layer rebuild (which would
    # create Abstraction nodes a `--only Rule` import must not produce).
    if (
        not dry_run
        and not report.errors
        and total_nodes > 0
        and parsed_only is None
    ):
        import writ.compression.abstractions as _ab

        if compress:
            try:
                result["compressed"] = await _ab.run_compression(
                    db, artifact_path=path / "abstractions.json"
                )
            except ImportError as exc:
                result["compress_import_error"] = exc
        else:
            artifact_path = path / "abstractions.json"
            if artifact_path.exists():
                result["materialized"] = await _ab.materialize_abstractions_from_artifact(
                    artifact_path, db
                )

    return result
