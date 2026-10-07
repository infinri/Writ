# ADR: live reload of the daemon's retrieval state

Status: accepted
Date: 2026-10-06
Plan: `.claude/plans/1c1f801f-c493-4e74-aa60-76c1e69ea30e/plan.md` (program item 2, wave 1
workstream A, `docs/programs/knowledge-engine-program.md`)
Touches: `writ/retrieval/pipeline.py`, `writ/retrieval/traversal.py`,
`writ/retrieval/trigger_index.py`, `writ/graph/db/rule_store.py`, `writ/neo4j_password.py`,
`writ/server/reload.py`, `writ/server/__init__.py`, `writ/server/routes/query.py`,
`writ/server/transport.py`, `bin/lib/writ_daemon_client.py`, `writ/cli.py`,
`tests/conftest.py`

## Context

The daemon built its retrieval pipeline and its methodology trigger index once, in the
server lifespan. Everything after that was frozen until a restart:

- An edited rule, a new edge, a promoted node or a new Category route reached retrieval
  only after the daemon restarted.
- Feedback (`POST /feedback`, `POST /feedback/batch`) wrote counts to the graph only, so
  learned confidence kept ranking on the counts loaded at startup.
- The BM25 cache helper rebuilt `<cache_root>/bm25` in place on a hash miss: it deleted the
  directory and wrote a new index into the same path. Any process that had the old index
  open (the running daemon, a concurrent CLI build) lost it from under its readers.

## Decisions

### 1. One generation-stamped handle, swapped whole

`writ/server/reload.py` defines `RetrievalHandle(generation, pipeline, trigger_index,
fingerprint, built_at)`. The pipeline and the trigger index are built from the same graph
read and installed together by `writ.server._install_retrieval`, which sets
`server._retrieval`, `server._pipeline` and `server._trigger_index` with no `await` in
between. On the single-threaded event loop no handler sees two generations between two
reads that do not await. `_pipeline` and `_trigger_index` stay module names, because every
route reads them live and many tests monkeypatch them; they are now aliases of the handle's
two halves. `/query` binds `server._pipeline.query` when it hands the call to a worker
thread, so an in-flight query finishes on the generation it started with.

### 2. A coalescing reloader that keeps the last good generation

`RetrievalReloader` serializes rebuilds behind one `asyncio.Lock`. Each request takes a
ticket; a rebuild that starts inside the lock covers every ticket issued before it started,
because it reads the graph after all of them. A waiter whose ticket is already covered
returns that rebuild's outcome. N requests during one rebuild cost at most one more
rebuild, and every requester gets the outcome of a rebuild that began after its request.

A builder exception is caught: the outcome is `failed` with an `error`, the previous handle
keeps serving, and an exception row is written. The first build (`start()`) still raises,
so a daemon that cannot build once does not start, as before. Every run writes one
`retrieval_reload` row on the metrics stream. `request_soon()` queues one coalesced
follow-up only while a rebuild is running; the feedback routes call it so a rebuild whose
graph read predates a feedback write cannot swap in older counts.

`POST /retrieval/reload` returns the outcome: `swapped`, `unchanged` or `failed`. `/health`
reports `retrieval.generation`, `built_at`, `reloading` and `last_reload` on both the ready
and the not-ready branch.

### 3. Rebuild only what changed: a per-source fingerprint

`build_pipeline` is split into `load_pipeline_inputs` (graph reads, on the event loop that
owns the async driver) and the synchronous `assemble_pipeline` (the existing BM25, HNSW and
encoder code, moved). `build_pipeline` composes the two, so every existing caller keeps its
signature.

`pipeline_fingerprint` returns one hash per source: `index_version`, `bm25`, `hnsw`,
`metadata`, `edges`, `routes` and `abstractions`; the reload adds `triggers`. `bm25` and
`hnsw` are the existing persisted cache keys, used as they are. The rest are
`fingerprint_digest`, the same SHA-256 over JSON idiom as the BM25 key with sorted keys and
string conversion for the graph's temporal values, applied once to each source's canonical
state (`AdjacencyCache.snapshot()`, `MethodologyTriggerIndex.snapshot()`, the metadata, the
routes, the abstractions). A reload whose fingerprint equals the served one skips the
rebuild and the swap. Because the whole metadata dict is hashed, an edit that touches
neither index (severity, confidence, authority) still swaps, while BM25 and HNSW load as
cache hits. The rebuild reuses the running encoder, so the embedding model loads once per
process and its query cache survives the swap.

`RETRIEVAL_INDEX_VERSION` is its own `index_version` key and is never folded into the
persisted BM25 or HNSW keys.

### 4. BM25 generation directories

`<cache_root>/bm25/CURRENT` names the live generation; each `gen-<hash12>-<random>/` holds
one complete index plus its sidecar. On a hit the generation `CURRENT` names is opened. On a
miss a fresh directory is created, built, given its sidecar, and only then is `CURRENT`
switched. The switch reuses the stage-then-commit write in `writ/neo4j_password.py`
(`stage_config` writes a private temp file beside the target and fsyncs it, `commit_config`
renames it and fsyncs the directory); `stage_config` gained an optional `prefix` so the
pointer stages under its own name. After the switch two prunes run: superseded `gen-*`
directories that are neither the new one nor the one `CURRENT` named before, and that are
older than `BM25_PRUNE_GRACE_SECONDS` (one hour, so another process's not yet switched
build survives); and the flat-layout files the old code left directly in `bm25/`, matched
by the names and extensions that layout wrote. A pointer that is malformed or escapes the
cache root reads as a miss. The in-memory `nocache` fallback is unchanged. The fix lives in
the helper, so every CLI `build_pipeline` caller gets it.

### 5. Socket-only route, and a CLI notice on the existing client

`writ/server/transport.py` gains `SOCKET_ONLY_PATHS = ("/retrieval/reload",)`.
`tcp_refusal` refuses those paths over TCP for every verb, before and independent of the
`WRIT_TCP_READONLY` check, so the transport census row records `refused: true`.

The nine graph-writing CLI commands (`add`, `edit`, `import-markdown`, `review`,
`compress`, `migrate`, `reconcile`, `prune`, `feedback`) call `_notify_daemon_reload()`
after their graph write, from a `finally` so a failure after a partial write still
notifies while a precondition failure does not. It uses the existing daemon client
(`bin/lib/writ_daemon_client.py`, loaded through `writ.session.feedback._daemon_client`)
and the existing socket resolver (`writ.config.get_daemon_socket_path`). The only client
change is `post_json_outcome(tcp_fallback=False)`, which makes a call socket-only. The
notice is silent when no daemon socket exists and when the reload is confirmed, prints one
stderr line otherwise, is bounded by `RELOAD_TIMEOUT_SECONDS` (5 s), never raises and never
changes the exit code. `WRIT_DAEMON_RELOAD=0` skips it; `tests/conftest.py` sets that for
the whole suite so a test-run write never reaches the operator's real daemon.

### 6. Feedback state from the rows the writes already return

`evaluate_and_flip_graduation` already reads the post-increment counts and provenance, and
`apply_feedback_batch`'s increment already RETURNs them. Each gains a keyword-only
`state_out` that it fills from those rows (adding `r.project` to the existing RETURN,
because the graph's key is `rule_id + project` while the pipeline's metadata is keyed by
`rule_id`), with provenance set to `graduation_pending` for rules the call flipped. The
batch clears `state_out` at the start of each transaction attempt, so a retry leaves only
committed rows and a replay leaves it empty. Return values and the stored replay record are
unchanged. `RetrievalPipeline.apply_feedback(rows)` sets absolute counts on entries the
pipeline already holds, skips unknown ids and same-id nodes from another project, and
replaces each entry rather than mutating it. A failed apply leaves the route's response
unchanged and writes a `server.feedback.refresh` exception row. A crossing `/feedback`
still issues exactly three statements.

### 7. The documents half (program item 5)

`RetrievalHandle` gains `documents` (last, default None), the chunk retrieval of
`docs/adr/ADR-document-retrieval.md`. `build_retrieval_handle` loads the inputs with documents
(one more read, the `DOCUMENT_COLLECTOR` chunk read) and the fingerprint gains
`documents_bm25`, `documents_hnsw` and `documents_meta`. The swap is still one handle and one
`_install_retrieval`, which now also sets `server._documents` with no `await` between the
assignments.

Partial rebuild: when only document keys moved, the previous rule pipeline object is reused and
`assemble_pipeline` is not called; when only rule keys moved, the previous document pipeline is
reused. The document build receives the rule pipeline's encoder.

Document failure: if the document build raises, the handle is still built with the new rule half,
the previous document half and the previous handle's three document keys (None and `"unbuilt"`
on first start), and a `retrieval.documents.build` exception row is written. A bad chunk never
blocks a rule reload or the daemon's first start, and the next reload retries because the stored
keys differ from the graph's. A rule build failure still fails the whole reload and keeps the
last good generation.

## Alternatives considered

- **A file watcher or polling.** Rejected: the writers are known (the CLI commands and the
  daemon's own feedback routes), so they notify. Polling would rebuild on a timer whether or
  not anything changed and would still lag the write.
- **Mutating the live pipeline's indexes in place.** Rejected: the keyword and vector
  indexes are not built for concurrent writes while query threads read them. A whole new
  generation, swapped by reference, needs no locking on the query path.
- **A token-guarded TCP reload route.** Rejected: the socket directory's 0700 mode already
  gives per-user isolation, and a token adds a secret to provision for no gain.
- **A separate post-write graph read for feedback.** Rejected: the writes already read or
  RETURN the post-write state, and a second read would break the three-statement budget.
- **Folding the index version into the persisted cache keys.** Rejected: every install would
  re-encode its corpus once for a shape change that did not happen. A shape change that must
  invalidate those caches changes their hash inputs instead.
- **A second unix-socket HTTP client in the CLI.** Rejected: one already exists and is
  shared by the hooks and the session code; the CLI gained one keyword argument on it.

## Consequences

- During a swap two generations are in memory at once until the old one's last reader
  finishes.
- A cache-miss rebuild runs in a worker thread and competes for CPU with queries; the
  previous generation keeps serving throughout.
- A handler that awaits between reading `_pipeline` and `_trigger_index`
  (`/prompt-bundle`) may span a swap. The two channels are independent, so this is benign.
- Superseded BM25 generations linger for up to an hour before a later rebuild prunes them.
- Until every process runs this code there is a mixed-version window: an old CLI's in-place
  rebuild can still remove the generation layout. The next rebuild by new code reads that as
  a miss and recreates it.
- The authority-preference threshold is still read once at startup: a reload changes the
  graph's content, not the process's configuration.
- The CLI wait is bounded at five seconds. A rebuild that takes longer completes in the
  daemon after the CLI has moved on, and `GET /health` shows the generation it reached.
