# Writ

Hybrid RAG knowledge retrieval service for AI coding rule enforcement.

Writ is a local service that replaces flat Markdown rule files with a graph-native knowledge database paired with a hybrid retrieval layer. It answers agent queries in under 10ms with deterministic latency, returning the most relevant rules for a given coding situation -- not a keyword match.

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

## Run the Service

```bash
writ serve
# Starts FastAPI on localhost:8765
# Pre-warms BM25 index, vector index, and adjacency cache into memory
```

## CLI Commands

| Command | Description |
|---|---|
| `writ serve` | Start the local service. Pre-warms indexes. Binds to localhost:8765. |
| `writ ingest <path>` | Parse Markdown rules and ingest into the graph. Validates schema. |
| `writ validate` | Run integrity checks: conflicts, orphans, staleness, redundancy. |
| `writ export <path>` | Regenerate Markdown from graph. |
| `writ migrate` | One-time migration of existing rules into graph. |
| `writ query "..."` | CLI rule query for testing retrieval quality. |
| `writ status` | Health check: rule count, index status, last ingestion, stale rules. |

## Architecture

Five-stage hybrid retrieval pipeline:

1. **Domain Filter** -- Pre-filter to relevant domain subgraph (< 1ms)
2. **BM25 Keyword** -- Tantivy sparse retrieval on trigger, statement, tags (< 2ms)
3. **ANN Vector** -- hnswlib in-process semantic search (< 3ms)
4. **Graph Traversal** -- Neo4j 1-2 hop for DEPENDS_ON, CONFLICTS_WITH, SUPPLEMENTS (< 3ms)
5. **Ranking** -- RRF + severity/confidence weighting, context budget applied (< 1ms)

Total target: p95 < 10ms end-to-end.

Mandatory enforcement rules (`ENF-*`) are never ranked -- they are loaded by the skill directly, outside the retrieval pipeline.

## Configuration

All settings in `writ.toml`, overridable via `WRIT_` environment variables.

## Full Specification

See [RAG_arch_handbook.md](RAG_arch_handbook.md) for the complete architecture specification.
