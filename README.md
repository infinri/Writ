# Writ

Hybrid RAG knowledge retrieval service for AI coding rule enforcement.

Writ is a local service that replaces flat Markdown rule files with a graph-native knowledge database paired with a hybrid retrieval layer. It answers agent queries in under 10ms with deterministic latency, returning the most relevant rules for a given coding situation -- not a keyword match.

## Why

Context-stuffing all rules into the prompt does not scale:

```
Context-stuffing: 15,812 tokens (46 domain rules)
Writ retrieval:   1,411 tokens (5 rules, 6.3ms)
Ratio:            11x reduction
```

At 80 rules the pipeline returns 11x fewer tokens. At 1,000 rules it becomes ~140x. At 10,000 rules context-stuffing is physically impossible.

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
| `writ ingest <path>` | Parse Markdown rules and ingest into the graph. Validates schema. |
| `writ validate` | Run integrity checks: conflicts, orphans, staleness, redundancy. |
| `writ add` | Interactive rule authoring with relationship suggestion and conflict detection. |
| `writ edit <rule_id>` | Edit an existing rule with re-validation and relationship re-analysis. |
| `writ export <path>` | Regenerate Markdown from graph. (Phase 7) |
| `writ migrate` | One-time migration of existing rules into graph. |
| `writ query "..."` | CLI rule query for testing retrieval quality. |
| `writ status` | Health check: rule count, index status, last ingestion. |

## Architecture

Five-stage hybrid retrieval pipeline:

1. **Domain Filter** -- Pre-filter to relevant domain subgraph
2. **BM25 Keyword** -- Tantivy sparse retrieval on trigger, statement, tags
3. **ANN Vector** -- hnswlib in-process semantic search
4. **Graph Traversal** -- Pre-computed adjacency cache for DEPENDS_ON, CONFLICTS_WITH, SUPPLEMENTS
5. **Ranking** -- Two-pass RRF: first pass scores BM25/vector/severity/confidence, second pass adds graph-neighbor proximity from top-3 results. Context budget applied.

Mandatory enforcement rules (`ENF-*`) are never ranked -- they are loaded by the skill directly, outside the retrieval pipeline.

## Benchmarks

All targets from [handbook Section 10](RAG_arch_handbook.md). Full results in [PHASE5_RESULTS.md](PHASE5_RESULTS.md).

Run with: `pytest benchmarks/bench_targets.py -v -s`

### Latency (80-rule corpus, warm indexes)

| Stage | Component | p95 | Budget | Headroom |
|---|---|---|---|---|
| 2 | BM25 (Tantivy) | 0.110ms | 2.0ms | 18x |
| 3 | Vector (hnswlib) | 0.110ms | 3.0ms | 27x |
| 4 | Adjacency cache | 0.002ms | 3.0ms | 1500x |
| 5 | Ranking (two-pass) | 0.060ms | 1.0ms | 17x |
| -- | **End-to-end** | **5.9ms** | **10.0ms** | **1.7x** |

### Retrieval Quality

| Metric | Value | Threshold |
|---|---|---|
| MRR@5 (19 ambiguous queries) | 0.7842 | > 0.78 |
| Hit rate (83 total queries) | 97.59% | > 90% |
| Context reduction | 11x | -- |

### Infrastructure

| Metric | Value | Budget |
|---|---|---|
| Cold start (build_pipeline) | 0.38s | < 3s |
| Memory footprint (RSS) | 1,181 MB | < 2 GB |
| Integrity check (80 rules) | 5.5ms p95 | < 500ms |
| Single rule ingestion | 0.032s p95 | < 2s |

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
# Unit and integration tests (107 tests)
pytest tests/ -q

# Performance benchmarks (12 tests, requires Neo4j with migrated rules)
pytest benchmarks/bench_targets.py -v -s

# Neo4j traversal scale benchmarks (1K/10K nodes)
pytest benchmarks/run_benchmarks.py -v -s
```

## Configuration

All settings in `writ.toml`, overridable via `WRIT_` environment variables.

## Project Status

Phases 1-6 complete. Phases 7-9 (generated artifacts, compression layer, agentic retrieval loop) specified and ready for implementation.

See [EXECUTION_PLAN.md](EXECUTION_PLAN.md) for phase details and decision/deviation logs. See [CONTRIBUTING.md](CONTRIBUTING.md) for the multi-author rule governance process.

## Full Specification

See [RAG_arch_handbook.md](RAG_arch_handbook.md) for the complete architecture specification.
