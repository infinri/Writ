# ADR: long documents, chunk retrieval and the documents hook

Status: accepted
Date: 2026-10-07
Plan: `.claude/plans/1c1f801f-c493-4e74-aa60-76c1e69ea30e/plan.md` (program item 5, workstream D,
phases 2 to 4, `docs/programs/knowledge-engine-program.md`)
Touches: `writ/documents/splitter.py`, `writ/documents/ingest.py`, `writ/graph/schema.py`,
`writ/graph/db/_common.py`, `writ/graph/db/record_store.py`, `writ/graph/db/schema_store.py`,
`writ/retrieval/pipeline.py`, `writ/retrieval/documents.py`, `writ/server/reload.py`,
`writ/server/__init__.py`, `writ/server/models.py`, `writ/server/routes/query.py`,
`writ/session/injection_state.py`, `writ/shared/budget.json`, `writ/cli.py`,
`bin/lib/writ-prompt-section.sh`, `hooks/scripts/writ-inject-documents.sh`, `hooks/hooks.json`,
`templates/settings.json`

## Context

Writ retrieved only short rule-shaped nodes. A project's own prose (its docs/ tree, its ADRs,
its READMEs) carried the reasons behind most decisions, and none of it reached the model unless
someone pasted it. The per-prompt injection already had a 2,700-token reservation for document
chunks (`docs/adr/ADR-prompt-injection-split.md`), with no source, no index and no hook behind
it. Workstream P (program item 5, phases 0 and 1) supplied the fence and sanitizer for retrieved
text (`docs/adr/ADR-retrieved-text-fence.md`) and the collector registry
(`docs/adr/ADR-collector-registry.md`); this ADR covers what is built on them.

## Decisions

### 1. Document and Chunk are record labels, never node types

`Document` (doc_id, project, path, kind, title, source_hash, chunk_count, ingested_at) and
`Chunk` (chunk_id, project, doc_id, ordinal, breadcrumb, text, est_tokens, source_hash) are
pydantic models beside TrustEvent, stamped `provenance="record"` and
`source_origin="graph-authored"`. They join `RECORD_LABELS` and `RECORD_ID_FIELDS`
(`doc_id`, `chunk_id`) and stay out of `NodeType`, `NODE_TYPE_MODELS`, `NODE_ID_FIELDS` and
`DOCTRINE_NODE_TYPES`.

- Preserve: a wipe (`clear_all`) or a corpus replay must not destroy what only
  `writ docs ingest` can rebuild.
- Exclude: chunk text is project content, possibly private, and has no place in
  `writ-corpus.cypher`. The dump never carries either label or any edge touching them.
- Isolation: no property name is a `NODE_ID_FIELDS` value, so the adjacency read, the dump edge
  read, parity and reconcile pruning (which all coalesce ids from `NODE_ID_FIELDS`) never see a
  chunk.
- Scoping: `is_visible` already treats every label outside `DOCTRINE_NODE_TYPES` as a
  project-scoped record, which is exactly what chunks need.
- `chunk_id` is `<project>:<doc_id>#<ordinal>`: the in-memory indexes key every candidate by one
  string, and two projects can both have a README.md.

Edges reuse `CONTAINS` (Document to Chunk) and `PRECEDES` (Chunk to the next Chunk). Two
composite uniqueness constraints (`document_doc_id_project_unique`,
`chunk_chunk_id_project_unique`) and the `document_project` index sit beside the TrustEvent
pair.

`replace_document` is one statement: MERGE the Document, DETACH DELETE its old chunks, create the
new chunks and their CONTAINS edges, then the PRECEDES chain by ordinal. A crash mid-run leaves
each document fully old or fully new, and two concurrent ingests of one file serialize on the
Document's constraint lock. `delete_documents` is one statement; an empty list touches nothing.

### 2. A new markdown splitter

`writ/documents/splitter.py` is pure (no graph, no filesystem). Front matter is stripped (its
`title:` is a title candidate). ATX H1 to H3 open sections outside fenced code; H4 and deeper stay
in the body. Every chunk carries the breadcrumb of its enclosing headings, led by the document
title when there is no H1, clipped with the shared `clip()` to `CHUNK_HEADER_CHARS` (120). Whole
paragraphs are packed up to `CHUNK_MAX_TOKENS` (300 tokens, 1,200 characters) without crossing a
section boundary; a fenced block is one paragraph; an oversize paragraph splits at line
boundaries and an oversize line at a word boundary. A heading with no body yields no chunk. The
title is the first H1, else the front-matter title, else the file stem.

### 3. Ingest: sources, hashing and deletion scope

`writ docs ingest [--repo PATH] [--include-claude-md] [--memory-dir PATH]` resolves the project
from the repo, applies the constraints, ingests, prints one summary line and asks the running
daemon to reload only when something was written or deleted (`_notify_daemon_reload`, which
honours `WRIT_DAEMON_RELOAD=0`). Ingest runs only on that command: not on startup, not on commit,
not from a hook.

- Kinds: `docs` (markdown under docs/), `adr` (under an adr or adrs directory, or named `ADR-*`
  under docs/), `readme` (README.md anywhere) by default; `claude_md` only with
  `--include-claude-md`; `memory` only with `--memory-dir`, where MEMORY.md is skipped through
  `memory_capture.is_memory_index_file`.
- Containment: hidden and dependency directories are pruned; each file is resolved and must pass
  `project_boundary.is_contained` (a symlink escape is skipped and counted, never read); files
  over 1 MiB are skipped and counted; credential-named files and anything under a secrets
  directory are never read.
- Hashing: `source_hash` is the sha256 of the file bytes. An unchanged hash is neither split nor
  written. Chunk text is stored raw; sanitizing happens once, at render, so a sanitizer change
  never requires a re-ingest.
- Root: `--repo` must be the project's registered root (from the project registry); a
  subdirectory is refused with a plain message, because doc ids are relative to `--repo` and
  deletion is scoped by project and kind.
- Memory ids: a memory document's id is `memory/<short hash of its directory>/<file>`, so two
  memory directories never share an id, and its stored path is home-relative (`~/...`).
- Deletion: a stored document is deleted only when its kind is one of this run's kinds and its
  file was not seen. A file that is still present but skipped (over the size limit, or a
  symlink escape) counts as seen, so its stored document is kept. A run without `--include-claude-md` never deletes earlier `claude_md`
  documents, and a run with a different `--memory-dir` deletes only the memory documents it did
  not see.

### 4. One shared, parametrized index builder

`assemble_pipeline`'s lower layer moved into `build_search_indexes(candidates, location,
text_of, *, embedding_model=None, ...)`, parametrized by an `IndexLocation` (BM25 root, HNSW
dir, metric prefix) and a vector-text builder. The rule pipeline calls it with the rule
parameters, so its cache keys, build order and metric names are byte-identical; the document
pipeline calls it with its own. `merge_ranked_hits` (the ordered union with the raw cosine as
`similarity`, then `normalize_ranks`) is shared the same way, and
`RetrievalPipeline._merge_and_normalize` delegates to it.

`KeywordIndex` is unchanged: a chunk maps onto its fields (rule_id = chunk_id, trigger =
breadcrumb, boosted, statement = the full chunk text at full weight, tags = the path). The
vector text is the breadcrumb plus the first `CHUNK_EMBED_HEAD_CHARS` (512) characters, the
encoder's 128-token window, so an edit to a chunk's tail re-indexes BM25 but not the vectors.

### 5. The encoder is injected, and the caches are separate

The document build always receives the rule pipeline's `encoder`, the same `CachedEncoder`
object; no second embedding model is ever constructed, and a documents query whose prompt the
ranked section already encoded is a cache hit. The document indexes live under
`<cache>/documents/bm25` and `<cache>/documents/hnsw`, beside the rule caches, so the fixed HNSW
file name cannot collide and a document build never writes a rule cache file. The document
metrics carry the `documents_` prefix. An empty chunk corpus builds nothing and encodes nothing.

### 6. One reload generation, with document failure isolation

`RetrievalHandle` gains `documents` (last, default None), and `pipeline_fingerprint` gains
`documents_bm25`, `documents_hnsw` (the persisted cache keys) and `documents_meta` (the
`DOCUMENT_COLLECTOR` stamp). `build_retrieval_handle` loads the inputs with documents and
rebuilds only the half whose keys moved: a documents-only change reuses the previous rule
pipeline object, a rules-only change reuses the previous document pipeline. One handle, one
`_install_retrieval`, one swap. If the document build raises, the handle still carries the new
rule half with the previous document half and the previous document keys (None and `"unbuilt"`
on first start), an exception row is written, and the next reload retries because the stored
keys differ from the graph's. A rule build failure still fails the whole reload. See
`docs/adr/ADR-live-reload.md`.

### 7. A documents section with its own budget, and a fifth hook

`budget.json` moves the 2,700-token reservation into `prompt_section_tokens.documents`;
`prompt_reserved_tokens` is empty and the total stays 9,500. `section_char_limit("documents")`
is 9,498. `/prompt-bundle` builds the section only when a request names it (the legacy trio
never does), skips it for an orchestrator master, and runs the query in a worker thread.
`writ-inject-documents.sh` is the fifth UserPromptSubmit hook, the recall hook's exact shape over
the shared `writ_prompt_section_main`, registered after it. Unlike recall it runs for sub-agents,
and it keeps the minimum-prompt-length gate.

The block is built by P's helpers: every retrieved field goes through `sanitize_retrieved`,
primary hits carry `similarity_slot`'s ` sim=0.612`, the block is `fence("DOCUMENTS", body,
"<n> chunks")` measured as fenced against the limit, and `clamp_fenced` is the outer guard, so a
cut block keeps its close marker. An abstained or empty result returns no block, which means
nothing matched. Shown chunks are recorded with `--mark-shown documents` per epoch
(`COLLAPSIBLE_SECTIONS`), and only ids whose head line is in the block are recorded. Every
request writes one `documents_query` metrics row (mode, top cosine, hits, shown, characters).

### 8. Ranking, abstention and its calibration

`DocumentPipeline.query` runs BM25 and vector search (50 candidates each), filtered by the same
predicate (not shown this epoch, `is_visible` for the caller's project). The abstention gate
reads the best raw cosine that survives the filter. Survivors are ranked by the default
weighted mean of the normalized BM25 and vector ranks (`RankingWeights()`, so no new tuned
weight exists), ties in discovery order. Primary hits are the top `DOCUMENT_TOP_K` (3) whose own
raw cosine reaches the threshold; a keyword-only hit is never primary.

`DOCUMENT_ABSTENTION_THRESHOLD` is its own constant, not the rules' 0.30: a false positive here
costs up to 2,700 tokens where a rule costs tens, the chunk corpus is the project's own prose (so
near-domain prompts sit at a higher baseline cosine than against the rulebook), and the 0.30
knee was measured on rules. The chosen value is 0.40. The calibration test runs on the isolated
graph with the real encoder over this repo's docs/: the negative prompts in
`tests/fixtures/ground_truth_negatives.json` must abstain at 90 percent or more, and at least 10
of the 12 prompts in `tests/fixtures/document_queries.json` must place their named document in
the top 3. Measured on 2026-10-07: at 0.40 all 20 negatives abstain (100 percent; the
highest negative cosine was 0.379) and 11 of 12 document queries place their document in the
top 3 (their top cosines ran from 0.571 to 0.724). At 0.35 only 80 percent of the negatives
abstain, and at 0.30 only 65 percent, so the rules' value would fail the gate here. 0.40 sits
in the gap between the two populations. The calibration rests on a small sample (20 negatives,
12 queries) drawn from this repo's own docs, so it is a starting point, not a general result.
The candidate pool is filled per caller: the search widens, bounded by the corpus size, until
enough of the caller's chunks survive the scope filter, so another project's chunks cannot
crowd them out. The `documents_query` metrics row gives passive
real-session data for any later move.

### 9. Expansion from the loaded snapshot

The chunk read (`_collect_chunks`, one statement through `DOCUMENT_COLLECTOR`) returns each
chunk with its document's title, path and kind and its neighbours' ids, so expansion costs no
graph read per prompt. After the primary hits, each hit's successor and then its predecessor
(one hop, same document only) is added while it fits, skipping chunks already placed or shown
this epoch; a neighbour is marked `(context)` and has no similarity slot. A context chunk that
reaches the block is recorded as shown exactly like a primary hit, so it is not repeated in the
same epoch either. Chunks are grouped
under one `## <title> (<path>)` line per document, in ordinal order. Expansion never runs on an
abstained query.

## Alternatives considered

- **Document and Chunk as node types.** Rejected: parity, reconcile pruning and ingest dispatch
  would all start reasoning about project prose, and the dump would ship it.
- **Extending the rule ingester (`writ/graph/ingest.py`).** Rejected: it is RULE-START-shaped; a
  generic markdown splitter is a different job.
- **A copied `assemble_pipeline` for documents.** Rejected: two index stacks would drift. The
  lower layer was extracted and parametrized instead.
- **A second reloader or a second handle for documents.** Rejected: two swaps are not atomic,
  and coalescing, serialization and keep-last-good already exist.
- **Documents inside the ranked rule channel.** Rejected: a chunk costs hundreds of tokens and
  would crowd rules out of their own budget; a separate section has its own ceiling.
- **Embedding the full chunk.** Rejected: the encoder truncates at 128 tokens, so the hash would
  move on edits the vectors never see.

## Consequences

- Documents are only as fresh as the last `writ docs ingest`.
- A documents-only reload still pays the generation's graph reads, plus one for the chunks, but
  none of the rule CPU or disk work.
- A failed document build leaves the previous document half serving until a later reload
  succeeds.
- Chunk text sits in the graph unsanitized; every renderer must go through `sanitize_retrieved`.
