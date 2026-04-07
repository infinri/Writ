# Writ Codebase -- Architecture and Development Guide

This describes Writ's own codebase. Read this before modifying any Writ source.

## Architecture overview

Writ is a hybrid RAG knowledge retrieval service. Python 3.12, FastAPI, Neo4j graph
database, Tantivy BM25 index, hnswlib vector index, ONNX Runtime inference.

### Five-stage retrieval pipeline (`writ/retrieval/pipeline.py`)

```
Query text
  |
  v
Stage 1: Domain filter (post-filter on BM25/vector results)
Stage 2: BM25 keyword search (Tantivy, top-50 candidates)
Stage 3: ANN vector search (hnswlib, ONNX embeddings, top-10 candidates)
Stage 4: Graph traversal (pre-computed adjacency cache, DEPENDS_ON/CONFLICTS_WITH/SUPPLEMENTS)
Stage 5: Two-pass RRF ranking
  Pass 1: score = 0.198*bm25 + 0.594*vector + 0.099*severity + 0.099*confidence
  Pass 2: add graph proximity from top-3, apply authority preference, apply context budget
  |
  v
Ranked rules (respecting token budget)
```

All indexes are pre-warmed at startup. No I/O in the hot path (PERF-IO-001).

### Module map

| Module | Lines | Role | Load-bearing? |
|--------|-------|------|---------------|
| `writ/retrieval/pipeline.py` | 346 | Pipeline orchestrator. All 5 stages. | YES -- any change here affects every query. Run benchmarks after changes. |
| `writ/retrieval/ranking.py` | 292 | RRF scoring, authority preference, context budget. | YES -- ranking changes affect MRR@5 and hit rate. Run benchmarks. |
| `writ/retrieval/embeddings.py` | 226 | ONNX embedding model, hnswlib vector store, LRU cache. | YES -- model or index changes affect vector search quality. |
| `writ/retrieval/keyword.py` | 96 | Tantivy BM25 index wrapper. | Moderate -- field changes affect BM25 recall. |
| `writ/retrieval/traversal.py` | 109 | Pre-computed adjacency cache (1-hop, 2-hop neighbors). | Moderate -- cache structure affects graph proximity scoring. |
| `writ/retrieval/session.py` | 88 | Client-side session tracker (budget, dedup). | Low -- stateless helper. Changes here also need mirroring in `bin/lib/writ-session.py`. |
| `writ/graph/db.py` | 348 | Neo4j connection pool, CRUD operations. | YES -- all data access goes through here. |
| `writ/graph/schema.py` | 216 | Pydantic models (Rule, Abstraction, Edge types). | YES -- schema changes cascade to ingest, export, API responses. |
| `writ/graph/ingest.py` | 152 | Markdown parser, field validation. | Moderate -- changes affect rule ingestion from Bible files. |
| `writ/graph/integrity.py` | 273 | Conflict detection, orphan detection, staleness checks. | Moderate -- integrity checks run on ingest and via CLI. |
| `writ/server.py` | 212 | FastAPI HTTP endpoints (/query, /propose, /feedback, /health). | YES -- API contract. Hook integration depends on response schema. |
| `writ/gate.py` | 249 | Structural pre-filter for AI rule proposals (5 checks). | Moderate -- gate logic determines what AI rules get accepted. |
| `writ/cli.py` | 685 | Typer CLI (serve, ingest, query, propose, review, etc.). | Low -- UI layer. Safe to modify without affecting pipeline. |
| `writ/frequency.py` | 53 | Graduation logic (n>=50, ratio>=0.75). | Low -- small, well-tested. |
| `writ/authoring.py` | 95 | Relationship suggestion, redundancy detection. | Low -- authoring helper. |
| `writ/export.py` | 188 | Round-trip Markdown generation from graph. | Low -- export utility. |
| `writ/origin_context.py` | 86 | SQLite store for AI rule proposal context. | Low -- write-once store. |
| `writ/compression/clusters.py` | 198 | HDBSCAN/k-means clustering. | Low -- used only by `writ compress`. |
| `writ/compression/abstractions.py` | 107 | Abstraction node generation. | Low -- used only by compression. |

### Key invariants

These must hold after any change. Violating them breaks the system.

1. **No sync I/O in hot path.** All pipeline stages use pre-warmed, in-process indexes. No network calls, no disk reads during `pipeline.query()`. (PERF-IO-001)
2. **Authority hard preference.** At equal relevance, human rules outrank AI rules. This is enforced via a hard reranking step, not by weight tuning. The preference threshold is 0.0749 (empirically derived from 83-query gap analysis). (writ.toml `[authority]`)
3. **Session is stateless server-side.** `writ/retrieval/session.py` is a client-side helper. The server (`/query` endpoint) has no session state. Deduplication is done via `exclude_rule_ids` passed by the caller.
4. **AI-provisional rules excluded from graph proximity seeding.** Only human and ai-promoted rules seed the adjacency cache. This prevents untested rules from boosting each other.
5. **Mandatory rules (ENF-*) bypass the pipeline.** They are always loaded by the skill directly, never returned by `/query`. The pipeline only handles non-mandatory domain rules.
6. **Embedding dimensions are 384.** The ONNX model produces 384-dim vectors. Changing the model requires re-indexing all vectors.
7. **Ranking weights must sum to 1.0.** The RRF formula in `ranking.py` assumes normalized weights from writ.toml `[ranking]`.
8. **Graduation requires n>=50 with ratio>=0.75.** The frequency graduation thresholds are derived from Wilson CI analysis. Do not lower them without statistical justification.

### Configuration

All tunable parameters are in `writ.toml`. Key sections:

- `[ranking]` -- RRF weights (bm25, vector, severity, confidence, graph)
- `[authority]` -- preference threshold, AI confidence ceilings
- `[context_budget]` -- summary/standard thresholds (2000/8000 tokens)
- `[gate]` -- novelty (0.85) and redundancy (0.95) cosine thresholds
- `[frequency]` -- graduation threshold (50) and ratio minimum (0.75)

Environment variable override: any setting can be overridden with `WRIT_` prefix
(e.g., `WRIT_SERVICE_PORT=9999`).

## Test structure

282 test functions across 15 test files + 12 benchmark tests.

| Test file | Count | Tests for |
|-----------|-------|-----------|
| `tests/test_schema.py` | 32 | Pydantic model validation, field constraints, edge types |
| `tests/test_export.py` | 33 | Round-trip Markdown fidelity, export correctness |
| `tests/test_compression.py` | 31 | HDBSCAN/k-means clustering, abstraction generation |
| `tests/test_ingest.py` | 24 | Markdown parsing, field defaults, validation errors |
| `tests/test_session.py` | 22 | Session tracker budget, dedup, reset |
| `tests/test_gate.py` | 21 | Structural gate (schema, specificity, redundancy, novelty, conflict) |
| `tests/test_authority.py` | 20 | Authority model, hard preference, ranking with AI rules |
| `tests/test_authoring.py` | 17 | Relationship suggestion, redundancy detection |
| `tests/test_frequency.py` | 16 | Graduation logic, edge cases |
| `tests/test_retrieval.py` | 14 | Full pipeline queries, context budget, domain filtering |
| `tests/test_graph_proximity.py` | 13 | Adjacency cache, proximity scoring |
| `tests/test_embeddings.py` | 11 | ONNX inference, tokenization, vector search |
| `tests/test_infrastructure.py` | 11 | Startup, index warming, latency |
| `tests/test_integrity.py` | 10 | Conflict detection, orphan detection, staleness |
| `tests/test_origin_context.py` | 7 | SQLite store CRUD |

Tests use in-memory mocks for Neo4j and indexes. No running server required.
The `tests/conftest.py` provides shared fixtures.

### Benchmark contracts

12 benchmark tests in `benchmarks/bench_targets.py`. These require Neo4j running
with the 80-rule corpus migrated (`python scripts/migrate.py`).

| Benchmark | Target | What breaks it |
|-----------|--------|----------------|
| End-to-end p95 | < 10ms | Adding sync I/O, slow ranking, index degradation |
| BM25 (stage 2) p95 | < 2ms | Tantivy index corruption, field changes |
| Vector (stage 3) p95 | < 3ms | ONNX model change, ef_search tuning |
| Cache (stage 4) p95 | < 3ms | Adjacency cache structure change |
| Ranking (stage 5) p95 | < 1ms | Weight computation complexity |
| Cold start | < 3s | Model loading, index building |
| Memory RSS | < 2 GB | Model size, index size, cache bloat |
| MRR@5 (ambiguous) | >= 0.78 | Ranking weight changes, embedding model change |
| Hit rate (all queries) | >= 90% | BM25 field changes, vector dimension change |
| Integrity check | < 500ms | Query complexity, graph size |
| Single ingestion | < 2s | Embedding model, validation complexity |
| Context reduction | > 1x | (informational -- always passes) |

## Testing and benchmarking directives

### After modifying any file in `writ/`

Run the full test suite:
```bash
pytest tests/ -x -q
```
Verify all 282 tests pass. The `-x` flag stops on first failure. Do not commit
if any test fails.

### After modifying files in `writ/retrieval/` or `writ/graph/schema.py`

Also run benchmarks (requires Neo4j + migrated corpus):
```bash
pytest benchmarks/bench_targets.py -v -s
```
Verify all 12 targets pass. If any target regresses, the change must be reworked.
Do not trade latency for features.

### After modifying `writ/retrieval/ranking.py` or `writ.toml` `[ranking]`

Run benchmarks AND pay special attention to MRR@5 and hit rate. These are the
quality gate metrics. A ranking change that improves latency but drops MRR@5
below 0.78 is a regression.

### After modifying `writ/retrieval/session.py`

Also verify that `bin/lib/writ-session.py` mirrors the same constants:
- `DEFAULT_SESSION_BUDGET = 8000`
- Token costs: full=200, standard=120, summary=40

These two files must stay in sync. The session helper is the hook-side mirror
of the server-side session tracker.

### After modifying `writ/server.py` response schema

The hook `claude/hooks/writ-rag-inject.sh` parses the `/query` response via
`bin/lib/writ-session.py format`. If you change the response fields (rule_id,
score, statement, trigger, violation, pass_example, rationale, relationships,
mode, total_candidates), update the format command to match.

### After modifying `writ/graph/schema.py`

Schema changes cascade. Check:
1. `writ/graph/ingest.py` -- parser produces matching fields
2. `writ/graph/db.py` -- CRUD queries match new schema
3. `writ/export.py` -- Markdown export handles new/changed fields
4. `writ/server.py` -- API response includes changed fields
5. `bin/lib/writ-session.py` format command -- displays changed fields
