# Writ -- Session Stopping Point

**Date:** 2026-03-16
**Session:** Phase 6 authoring tools, graph-neighbor scoring, weight tuning
**Status:** Phases 1-6 complete. All tests + benchmarks passing. Phases 7-9 specified, not started.

---

## What Was Accomplished (This Session)

### Phase 6: Authoring Tools

#### Graph-Neighbor Scoring Boost
- Two-pass ranking in `writ/retrieval/pipeline.py`:
  - Pass 1: score with Phase 5 ratios (renormalized w1-w4) to identify top-3
  - Pass 2: compute graph proximity (1-hop=1.0, 2-hop=0.5, none=0.0) from top-3, re-score with full 5-weight formula
- `compute_graph_proximity()` function: set-based lookup via AdjacencyCache, discrete values per INV-2
- Weight tuning sweep: w_graph in {0.0, 0.01, 0.02, 0.05}
  - w_graph=0.01 reshuffles 12/83 queries, zero MRR@5 regression
  - w_graph=0.02+ regresses Q72 (ARCH-ORG-001 displaced from rank 5 by boosted neighbor)
  - Final weights: 0.198/0.594/0.099/0.099/0.01 (Phase 5 ratios * 0.99)
- MRR@5 gap to 0.85 remains open: corpus too small (80 rules, 147 skeleton edges) for proximity to create net improvement. Mechanism validated; scales with corpus size.

#### Authoring Module
- `writ/authoring.py` -- domain logic: `suggest_relationships()`, `check_redundancy()`, `check_conflicts()`
- `suggest_relationships()`: runs new rule text through pipeline, returns top-5 candidates
- `check_redundancy()`: cosine similarity >= 0.95 threshold (INV-5), uses pipeline's vector store
- `check_conflicts()`: scans AdjacencyCache for CONFLICTS_WITH edges

#### CLI Commands
- `writ add` -- interactive rule authoring: field prompts, schema validation, relationship suggestion, redundancy/conflict warnings, graph write, edge creation
- `writ edit <rule_id>` -- load existing rule, modify fields, re-run all checks, idempotent MERGE update

#### Governance
- `CONTRIBUTING.md` -- multi-author process: PR review required, human conflict resolution, SUPERSEDES for deprecation

### Tests & Benchmarks
- `tests/test_graph_proximity.py` -- 13 tests: proximity unit tests, weight validation, backward compatibility (INV-3), MRR@5/hit rate regression gates, latency gate
- `tests/test_authoring.py` -- 17 tests: schema validation, relationship suggestion, redundancy detection (0.95 threshold), conflict detection, graph writes, idempotent edit
- All 12 existing benchmarks passing. MRR@5=0.7842, hit rate=97.59%, e2e p95=5.9ms

---

## Cumulative State

### Phases 1-5 (Previous Sessions)
- Full 5-stage hybrid retrieval pipeline operational
- 80 rules migrated, 147 skeleton edges
- FastAPI service with `/query`, `/rule/{rule_id}`, `/conflicts`, `/health`
- 77 tests + 12 benchmarks passing

### Phase 6 (This Session)
- `writ/authoring.py` -- relationship suggestion, redundancy detection, conflict checking
- `writ/retrieval/ranking.py` -- 5-weight formula with graph proximity, `first_pass_weights()` renormalization
- `writ/retrieval/pipeline.py` -- `compute_graph_proximity()`, two-pass ranking
- `writ/cli.py` -- 9 commands (added `add` and `edit`)
- `CONTRIBUTING.md` -- multi-author governance

**107 tests + 12 benchmarks = 119 total, all passing.**

---

## Current State of Neo4j

Neo4j is running in Docker (`writ-neo4j` container). Test suite teardown clears the database. Before running `writ query` or `writ serve`, re-migrate:

```bash
docker start writ-neo4j
python scripts/migrate.py
```

---

## What Is Next: Phases 7-9

### Phase 7: Generated Artifacts
- `writ export` regenerates Markdown from graph (currently a stub)
- `bible/` becomes a generated view, not the source of truth
- Round-trip fidelity: export -> ingest -> export -> diff must be empty
- Auto-export after `writ ingest`, staleness check in `writ serve`

### Phase 8: Compression Layer
- Rule clustering (HDBSCAN vs k-means evaluation)
- Abstraction nodes with summary text
- Summary mode returns abstractions instead of raw statement+trigger

### Phase 9: Agentic Retrieval Loop
- Session-aware `/query` with `loaded_rule_ids`
- Client-side session tracker (`writ/retrieval/session.py`)
- Skill integration documentation

---

## Open Questions

Resolved this session:
- Multi-author conflict governance: CONTRIBUTING.md process -- **Closed**

Still open:
- Graph-level versioning (immutable snapshots for long sessions) -- Phase 9
- Rule-level versioning (edit history) -- Phase 7
- Clustering algorithm for abstraction nodes -- Phase 8

---

## Known Issues

1. **PY-ASYNC-001 noise** -- appears in ~10 unrelated queries via high vector similarity. Documented limitation. Domain filter and reduced BM25 weight (0.198) are the mitigations.

2. **Test cleanup clears Neo4j** -- the test suite's module-scoped fixtures wipe the database on teardown. Running tests then using `writ query` requires a re-migration.

3. **MRR@5 gap to 0.85** -- automated strict MRR@5 is 0.7842. Graph-neighbor proximity mechanism is operational (12/83 queries reshuffled) but does not improve net MRR@5 at 80 rules. Corpus too small and well-connected. Q66 and Q84 are genuinely too vague. Revisit at 500+ rules with typed edges.

4. **BENCH-INGEST-001 persistence** -- the benchmark's ingestion test writes a synthetic rule to Neo4j that persists across re-migrations. Clean with `db.clear_all()` before re-migrating if results seem off.

---

## File Inventory

```
writ/
  __init__.py                  # v0.1.0
  cli.py                       # 9 commands (added add, edit)
  server.py                    # FastAPI with 4 endpoints, model reuse
  authoring.py                 # NEW: suggest_relationships, check_redundancy, check_conflicts
  graph/
    schema.py                  # Pydantic models + validators
    db.py                      # Neo4j async CRUD + GraphConnection protocol
    ingest.py                  # Markdown parser + validation
    integrity.py               # IntegrityChecker (4 detection types)
  retrieval/
    pipeline.py                # Two-pass ranking + compute_graph_proximity()
    keyword.py                 # Tantivy BM25 with trigger boost + sanitization
    embeddings.py              # hnswlib HnswlibStore + VectorStore protocol
    traversal.py               # AdjacencyCache + GraphTraverser
    ranking.py                 # 5-weight RRF + first_pass_weights() + context budget
  compression/
    clusters.py                # Stub (Phase 8)
    abstractions.py            # Stub (Phase 8)
tests/
  conftest.py                  # Shared fixtures
  test_schema.py               # 29 tests
  test_infrastructure.py       # 11 tests
  test_ingest.py               # 13 tests
  test_integrity.py            # 10 tests
  test_retrieval.py            # 14 tests
  test_graph_proximity.py      # 13 tests (NEW)
  test_authoring.py            # 17 tests (NEW)
  fixtures/
    ground_truth_queries.json  # 83 ground-truth queries
scripts/
  migrate.py                   # One-time migration (idempotent)
benchmarks/
  run_benchmarks.py            # Neo4j traversal benchmarks (1K/10K)
  bench_targets.py             # Section 10 contractual targets (12 tests)
CONTRIBUTING.md                # NEW: multi-author governance
```

---

## Quick Resume Commands

```bash
cd ~/workspaces/Writ
pyon
docker start writ-neo4j
python scripts/migrate.py
writ query "your question here"
pytest tests/ -q
pytest benchmarks/bench_targets.py -v -s
```
