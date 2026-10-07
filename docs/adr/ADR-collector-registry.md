# ADR: a collector registry for retrieval sources, and the allowlist collapse

Status: accepted
Date: 2026-10-07
Plan: `.claude/plans/1c1f801f-c493-4e74-aa60-76c1e69ea30e/plan.md` (knowledge-engine program item 5, workstream P, phase 1)
Touches: `writ/retrieval/collectors.py`, `writ/retrieval/pipeline.py`, `writ/graph/schema.py`,
`writ/graph/db/_common.py`, `writ/graph/db/record_store.py`

## Context

Item 5 adds a second retrieval source (document chunks) beside the ranked pool. The ranked
pool was one function, `_load_candidates`, that knew both of its halves by heart, and the
graph's label lists were kept in several parallel places: `NodeType`,
`RETRIEVABLE_NODE_TYPES`, `NODE_TYPE_MODELS`, `NODE_ID_FIELDS`, the id coalesce strings and
the record id names typed out at each record write. A new source written the same way would
add one more list to keep in agreement, and a label added to the wrong list decides who may
read it: a doctrine label reaches every project, a record label only its own.

## Decision

### The registry (`writ/retrieval/collectors.py`)

A `Collector` is one retrieval source: a `name`, the graph `labels` it reads, a `scope`
(`"doctrine"` or `"record"`), an async `collect(db)` returning its units, and a `stamp_fn`
(default `fingerprint_digest`, moved here from the pipeline). `run(db)` returns a
`Collected` whose `stamp` is computed on first read, so the ranked build, which never reads
it, pays nothing.

`validate_collectors` raises `CollectorRegistryError` (a `ValueError`) when a name is empty
or repeated, a collector claims no label, a label is claimed twice, a label is in neither
`NODE_ID_FIELDS` nor `RECORD_LABELS`, a doctrine collector names a label outside
`DOCTRINE_NODE_TYPES`, or a record collector names a label outside `RECORD_LABELS`, inside
`DOCTRINE_NODE_TYPES` or inside `NODE_ID_FIELDS`. It fails closed: an unknown label is
refused, never admitted.

`writ.retrieval.pipeline` defines `RANKED_COLLECTORS`: `rules` (the Rule query on
`RANKED_INCLUDE_WHERE`, ordered by `rule_id`) and `methodology` (one ordered query per
`RANKED_METHODOLOGY_LABELS` label), both moved verbatim out of `_load_candidates`, and calls
`validate_collectors(RANKED_COLLECTORS)` at module level, a raise and not an assert so it
holds under `python -O`. Importing the pipeline is daemon startup, so a bad registry stops
the daemon before it serves a prompt. `_load_candidates` concatenates the collectors' units
in order and builds the metadata as before: same six statements, same order, same
candidates (measured: `benchmarks/ITEM5-PHASE01-RANKING-2026-10-07.md`).
`fingerprint_digest` stays importable from the pipeline, and the reload fingerprint's keys
and values are unchanged.

The trigger index (Channel 2) is not on the registry: it is a deterministic matcher with its
own load and fingerprint key, and documents do not need it moved.

### Collapsed (every derived value is identical)

- `NODE_TYPE_MODELS` and `NODE_ID_FIELDS` derive from one `_NODE_TYPE_TABLE` of
  (NodeType, model, id field) rows in today's order; import raises `ValueError` when the
  table and `NodeType` disagree.
- `RETRIEVABLE_NODE_TYPES` derives from Rule, Abstraction and `RANKED_METHODOLOGY_LABELS`,
  and keeps `NodeType` members.
- `_GRAPH_ID_FIELDS` is the one sorted id-field tuple behind `_GRAPH_ID_COALESCE` and
  `_id_or_match` (strings byte-identical); `_id_field_for_label` and `_node_write_spec`
  resolve through `NODE_ID_FIELDS` with the same results and errors.
- `RECORD_ID_FIELDS` maps each record label to its id property, written out beside
  `RECORD_LABELS`; import raises when its keys differ from `RECORD_LABELS` or when a record
  label appears in `NODE_ID_FIELDS` or `NODE_TYPE_MODELS`, so "records stay out of the node
  registry" is enforced, not remembered. `_RECORD_EDGE_ENDPOINTS` takes the record ids from
  it for the written-out `_RECORD_EDGE_ENDPOINT_LABELS` (Decision, FileChange, Commit,
  Project), and the four `_create_record` callers pass `RECORD_ID_FIELDS[label]`.

### Kept explicit (unsafe to derive)

- `DOCTRINE_NODE_TYPES`: deriving it would make a new label readable by every project. The
  registry is checked against it, never the reverse.
- `RECORD_LABELS`: membership means "runtime state with no markdown home", preserved across
  wipes and excluded from the dump, a durability and confidentiality decision per label.
- `RANKED_METHODOLOGY_LABELS`: its order is the index build order, which can break ties; it
  is the source, not a derivation.

## Extension point: documents

Workstream D adds `Collector("documents", ("Document", "Chunk"), "record", ...)` and
validates it together with `RANKED_COLLECTORS` in the same one module-level call. That
requires both labels in `RECORD_LABELS` and `RECORD_ID_FIELDS` (the import guard makes the
two move together) and never in `NODE_ID_FIELDS` or `NODE_TYPE_MODELS`. A copy claiming
`Chunk` as doctrine, or a record label missing from `RECORD_LABELS`, is refused at import.

## Alternatives rejected

- **Derive the doctrine set from the registry.** Fail-open: a new collector would widen
  cross-project visibility by existing.
- **One registry for every label, records included.** Parity, reconcile and the ingest
  dispatch iterate `NODE_ID_FIELDS`; records there would be reconciled against markdown they
  do not have.
- **Validate lazily on first query.** A bad registry would serve stale or wrong results
  until the first prompt; failing at import stops it before anything is served.

## Consequences

- Adding a node type is one table row plus the enum member, and the guard names any miss.
- Adding a retrieval source is one `Collector` and an allowlist decision, reviewed where the
  allowlists live.
- No new graph statement on any path; validation is pure and runs once at import.
