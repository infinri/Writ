# Writ

**Stop stuffing 120,000 tokens of coding rules into every prompt.**

Writ is a local service that retrieves only the rules an AI coding agent needs for the current task -- in under 1ms, with deterministic latency. Mandatory safety rules are loaded out-of-band so they can't be dropped by ranking.

## The problem

Teaching an AI agent to follow your coding conventions means giving it rules. The naive approach -- paste all of them into the system prompt -- falls over as the rulebook grows. Tokens you spend on rules you can't spend on the task, and the agent's attention dilutes across content most of it doesn't need right now.

## The result

Measured on a 1,000-rule corpus across 10 representative queries (`benchmarks/scale_benchmark.py`, 2026-04-13):

```
Context-stuffing: 121,473 tokens  (all rules, every turn)
Writ retrieval:     1,602 tokens  (~5 rules, 0.4ms p95)
                   ------------
                   76x reduction
```

The ratio compounds with scale:

| Corpus | Stuffed | Retrieved | Reduction | p95 |
|---|---:|---:|---:|---:|
| 80 rules | 13,876 | 3,155 | 4.4x | 0.28ms |
| 500 rules | 63,003 | 1,600 | 39.4x | 0.36ms |
| 1,000 rules | 121,473 | 1,602 | 75.8x | 0.40ms |
| 10,000 rules | 1,174,142 | 1,617 | 726.1x | 0.56ms |

Three things you get that flat-file loading can't do:

- **Relevance, not pattern-match.** Hybrid retrieval (BM25 + ANN vector + graph proximity + RRF ranking) returns rules that fit the situation, not every rule whose filename contains the right keyword.
- **Sub-millisecond retrieval that holds at scale.** p95 stays under 0.6ms from 80 to 10,000 rules. Pre-warmed indexes, ONNX embeddings, LRU-cached inference.
- **Mandatory rules that can't be dropped.** `ENF-*` rules bypass the ranker entirely and load directly from the skill. Ranking can deprioritize a style rule; it can't silently drop a security gate.

## How it integrates

Writ ships as a local HTTP service plus a set of Claude Code hooks that wire into the agent's turn lifecycle. On every user prompt, a hook POSTs the query to Writ, receives the ranked rules, and injects them into Claude's context as a single block. There is no retrieval code in the prompt and no manual rule loading -- the agent just reacts to rules that fit what it's about to do.

```
--- WRIT RULES (3 rules, standard mode) ---

[PY-ASYNC-001] (?, ?, ?) score=0.892
WHEN: Calling a sync I/O function inside an async def function.
RULE: Async call chains must use async I/O end-to-end.
VIOLATION: Using requests.get() in an async handler
CORRECT: Using httpx.AsyncClient within async context

--- END WRIT RULES ---
```

Installation, hook wiring, mode system, CLI, and HTTP reference live in [docs/integration.md](docs/integration.md).

## Architecture

Five-stage hybrid retrieval pipeline:

1. **Domain Filter** -- Pre-filter to relevant domain subgraph
2. **BM25 Keyword** -- Tantivy sparse retrieval on trigger, statement, tags
3. **ANN Vector** -- hnswlib in-process semantic search
4. **Graph Traversal** -- Pre-computed adjacency cache for `DEPENDS_ON`, `CONFLICTS_WITH`, `SUPPLEMENTS`
5. **Ranking** -- Two-pass RRF: first pass scores BM25 + vector + severity + confidence; second pass adds graph-neighbor proximity seeded from top-3 results (AI-provisional rules excluded from proximity seeding). Context budget applied. Summary mode (< 2K tokens) returns abstraction summaries when available.

Mandatory enforcement rules (`ENF-*`) are never ranked -- they are loaded by the skill directly, outside the retrieval pipeline. Ranking can deprioritize a style rule; it cannot silently drop a security gate.

**Authority model.** Rules carry authority levels (human, ai-provisional, ai-promoted). AI-proposed rules enter as ai-provisional with capped confidence and pass through a five-check structural gate (schema, specificity, redundancy, novelty, conflict). Human review via `writ review` promotes, rejects, or downweights them. Frequency tracking enables empirical confidence graduation at n=50 observations with positive ratio >= 0.75.

**Compression layer.** Rules cluster into abstraction nodes (HDBSCAN or k-means, auto-selected by silhouette score). When the context budget is tight, the pipeline returns abstraction summaries instead of individual rules.

## Benchmarks

Raw numbers and methodology in [SCALE_BENCHMARK_RESULTS.md](SCALE_BENCHMARK_RESULTS.md). Run with `pytest benchmarks/bench_targets.py -v -s` for latency and quality, or `python benchmarks/scale_benchmark.py` for the full scale curve.

### Latency (80-rule corpus, ONNX Runtime, warm indexes + LRU cache)

| Stage | Component | p95 | Budget | Headroom |
|---|---|---|---|---|
| 2 | BM25 (Tantivy) | 0.175ms | 2.0ms | 11x |
| 3 | Vector (hnswlib) | 0.047ms | 3.0ms | 64x |
| 4 | Adjacency cache | 0.001ms | 3.0ms | 3000x |
| 5 | Ranking (two-pass) | 0.089ms | 1.0ms | 11x |
| -- | **End-to-end** | **0.19ms** | **10.0ms** | **53x** |

### Retrieval quality

| Metric | Value | Threshold |
|---|---|---|
| MRR@5 (19 ambiguous queries) | 0.7842 | > 0.78 |
| Hit rate (83 total queries) | 97.59% | > 90% |
| ONNX vs PyTorch ranking stability | 0/83 queries diverge | Identical top-5 |

### Ranking weights (tuned via sweep against 83-query ground-truth set)

| Component | Weight |
|---|---:|
| Vector semantic rank | 0.594 |
| BM25 keyword rank | 0.198 |
| Severity | 0.099 |
| Confidence | 0.099 |
| Graph proximity | 0.010 |

## Links

- [Architecture handbook](RAG_arch_handbook.md) -- complete specification
- [Integration guide](docs/integration.md) -- install, hooks, modes, CLI, API reference
- [Scale benchmarks](SCALE_BENCHMARK_RESULTS.md) -- raw 80 / 500 / 1K / 10K numbers
- [Evolution reference](docs/evolution-reference.md) -- design history and rationale
- [Contributing](CONTRIBUTING.md) -- rule governance and authoring
