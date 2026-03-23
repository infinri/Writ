# Writ

Hybrid RAG knowledge retrieval service for AI coding rule enforcement.

Writ is a local service that replaces flat Markdown rule files with a graph-native knowledge database paired with a hybrid retrieval layer. It answers agent queries in under 10ms with deterministic latency, returning the most relevant rules for a given coding situation -- not a keyword match.

## Why

Context-stuffing all rules into the prompt does not scale:

```
Context-stuffing: 15,812 tokens (46 domain rules)
Writ retrieval:   1,397 tokens (5 rules, 5.8ms)
Ratio:            11x reduction
```

At 80 rules the pipeline returns 11x fewer tokens. At 1,000 rules it becomes ~76x. At 10,000 rules context-stuffing would require 1.17 million tokens -- Writ returns ~1,600 tokens for a 726x reduction.

## Install

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Requires Neo4j 5.x+ (Docker or local):

```bash
docker run -d \
  --name writ-neo4j \
  -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/writdevpass \
  -e NEO4J_PLUGINS='["apoc"]' \
  -v writ-neo4j-data:/data \
  neo4j:5-community
```

## Quick Start

```bash
# Migrate existing rules into the graph
python scripts/migrate.py

# Optional: generate abstraction nodes for compressed retrieval
writ compress

# Start the service (pre-warms all indexes)
writ serve

# Query from CLI
writ query "controller contains SQL query"

# Or hit the API directly
curl -X POST http://localhost:8765/query \
  -H "Content-Type: application/json" \
  -d '{"query": "async blocking event loop"}'
```

## CLI Commands

| Command | Description |
|---|---|
| `writ serve` | Start the local service. Pre-warms indexes. Binds to localhost:8765. |
| `writ ingest <path>` | Parse Markdown rules and ingest into the graph. Validates schema. Auto-exports on success. |
| `writ validate` | Run integrity checks: conflicts, orphans, staleness, redundancy. |
| `writ add` | Interactive rule authoring with relationship suggestion and conflict detection. |
| `writ edit <rule_id>` | Edit an existing rule with re-validation and relationship re-analysis. |
| `writ export <path>` | Regenerate Markdown from graph. Round-trip fidelity with ingest. |
| `writ compress` | Cluster rules into abstraction nodes. Evaluates HDBSCAN vs k-means. |
| `writ migrate` | One-time migration of existing rules into graph. |
| `writ query "..."` | CLI rule query for testing retrieval quality. |
| `writ status` | Health check: rule count, index status, export staleness. |

## Architecture

Five-stage hybrid retrieval pipeline:

1. **Domain Filter** -- Pre-filter to relevant domain subgraph
2. **BM25 Keyword** -- Tantivy sparse retrieval on trigger, statement, tags
3. **ANN Vector** -- hnswlib in-process semantic search
4. **Graph Traversal** -- Pre-computed adjacency cache for DEPENDS_ON, CONFLICTS_WITH, SUPPLEMENTS
5. **Ranking** -- Two-pass RRF: first pass scores BM25/vector/severity/confidence, second pass adds graph-neighbor proximity from top-3 results. Context budget applied. Summary mode (< 2K tokens) returns abstraction summaries when available.

Mandatory enforcement rules (`ENF-*`) are never ranked -- they are loaded by the skill directly, outside the retrieval pipeline.

**Compression layer**: Rules are clustered into abstraction nodes (HDBSCAN or k-means, auto-selected by silhouette score). When context budget is tight, the pipeline returns abstraction summaries instead of individual rules. `writ compress` evaluates both algorithms and writes the winner.

**Session tracking**: Client-side session context tracker (`writ/retrieval/session.py`) accumulates loaded rules across sequential queries, preventing duplicates and decrementing the token budget. The server remains stateless -- session state lives in the client (skill).

## API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/query` | POST | Ranked rule retrieval. Accepts `query`, `domain`, `budget_tokens`, `exclude_rule_ids`, `loaded_rule_ids`. |
| `/rule/{rule_id}` | GET | Full rule node. `?include_graph=true` adds 1-hop context. Returns `abstraction_id` and `sibling_rule_ids`. |
| `/conflicts` | POST | CONFLICTS_WITH edges between provided rule_ids. |
| `/abstractions` | GET | All abstraction nodes with member counts. |
| `/abstractions/{id}` | GET | Full abstraction with member rule details. |
| `/health` | GET | Status, rule/mandatory count, index state, startup time, export timestamp, staleness. |

## Benchmarks

All targets from [handbook Section 10](RAG_arch_handbook.md). Baseline results in [PHASE5_RESULTS.md](PHASE5_RESULTS.md). Scale results in [SCALE_BENCHMARK_RESULTS.md](SCALE_BENCHMARK_RESULTS.md).

Run with: `pytest benchmarks/bench_targets.py -v -s`

### Latency (80-rule corpus, warm indexes)

| Stage | Component | p95 | Budget | Headroom |
|---|---|---|---|---|
| 2 | BM25 (Tantivy) | 0.136ms | 2.0ms | 15x |
| 3 | Vector (hnswlib) | 0.101ms | 3.0ms | 30x |
| 4 | Adjacency cache | 0.002ms | 3.0ms | 1500x |
| 5 | Ranking (two-pass) | 0.053ms | 1.0ms | 19x |
| -- | **End-to-end** | **6.7ms** | **10.0ms** | **1.5x** |

### Retrieval Quality

| Metric | Value | Threshold |
|---|---|---|
| MRR@5 (19 ambiguous queries) | 0.7842 | > 0.78 |
| Hit rate (83 total queries) | 97.59% | > 90% |
| Context reduction | 11x | -- |

### Scale Performance

| Metric | 80 rules | 500 rules | 1K rules | 10K rules |
|---|---|---|---|---|
| E2E p95 | 5.97ms | 6.83ms | 6.51ms | 8.0ms |
| Context reduction | 4.4x | 39.3x | 75.7x | 725.7x |
| Memory (RSS) | 1,075 MB | 1,211 MB | 1,281 MB | 1,469 MB |
| Cold start | 0.29s | 1.33s | 2.60s | 22.0s |
| Clusters | 13 | 70 | 419 | 528 |
| Compression ratio | 5.6x | 7.6x | 2.8x | 24.8x |
| Session duplicates | 0 | 0 | 0 | 0 |

### Infrastructure

| Metric | Value | Budget |
|---|---|---|
| Cold start (build_pipeline) | 0.31-0.40s | < 3s |
| Memory footprint (RSS) | 1,181 MB | < 2 GB |
| Integrity check (80 rules) | 3.5ms median, 38.8ms p95 | < 500ms |
| Single rule ingestion | 0.008s median, 0.012s p95 | < 2s |

### Ranking Configuration

Weights tuned via sweep against 83-query ground-truth set. Phase 5 ratios preserved, scaled by 0.99 for graph proximity:

| Component | Weight |
|---|---|
| BM25 keyword rank | 0.198 |
| Vector semantic rank | 0.594 |
| Severity | 0.099 |
| Confidence | 0.099 |
| Graph proximity | 0.01 |

## Testing

```bash
# Unit and integration tests (183 tests across 10 test files)
pytest tests/ -q

# Performance benchmarks (12 tests, requires Neo4j with migrated rules)
pytest benchmarks/bench_targets.py -v -s

# Neo4j traversal scale benchmarks (1K/10K nodes)
pytest benchmarks/run_benchmarks.py -v -s

# Comprehensive scale benchmark (80/500/1K/10K rules)
python benchmarks/scale_benchmark.py
```

## Configuration

All settings in `writ.toml`, overridable via `WRIT_` environment variables.

## Project Status

All 9 phases complete. Core architecture operational.

Future work:
- Complement mode -- semantic gap detection for multi-query sessions. Viable at 500+ rules where domain clusters are separable enough to detect gaps between loaded rules and available rules. At 80 rules, embeddings cluster too tightly for meaningful signal.
- Graph-level versioning -- immutable snapshots pinned at session start. Becomes relevant when corpus scales and graph mutations during a session could cause inconsistency.
- Qdrant migration -- swap hnswlib at > 10K rules. VectorStore Protocol abstraction already in place.
- Pre-computed embeddings -- eliminate cold start scaling (22s at 10K rules)

See [EXECUTION_PLAN.md](EXECUTION_PLAN.md) for phase details and decision/deviation logs. See [CONTRIBUTING.md](CONTRIBUTING.md) for the multi-author rule governance process. See [SCALE_BENCHMARK_RESULTS.md](SCALE_BENCHMARK_RESULTS.md) for detailed scale benchmarks at 80/500/1K/10K rules.

## Full Specification

See [RAG_arch_handbook.md](RAG_arch_handbook.md) for the complete architecture specification.