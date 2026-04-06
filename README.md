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

## Claude Code Integration

Writ integrates with Claude Code via hooks and a plugin manifest that automatically
inject relevant rules into Claude's context on every turn. No manual rule loading
required.

### Setup

```bash
# 1. Start Neo4j (if not already running)
docker start writ-neo4j

# 2. Ingest rules and start the server
python scripts/migrate.py       # first time only
writ serve                      # runs on localhost:8765

# 3. Install hooks into Claude Code
bash scripts/install-skill.sh   # patches ~/.claude/settings.json

# 4. Restart Claude Code
```

After setup, every Claude Code session automatically queries Writ for relevant rules.
The hooks handle injection, deduplication, budget tracking, and context pressure.

### Plugin mode

Writ also ships a `.claude-plugin/plugin.json` manifest. When Claude Code discovers
the plugin, it registers hooks and lifecycle scripts automatically -- no manual
`install-skill.sh` step required.

The plugin lifecycle handles server startup:
- **Init** (`scripts/ensure-server.sh`) -- starts Neo4j (Docker) and `writ serve`
  if they are not already running. Non-fatal: if Docker is missing or startup
  times out, hooks fall back gracefully.
- **Shutdown** (`scripts/stop-server.sh`) -- stops the Writ server. Neo4j is left
  running since it may be shared with other tools.

### Tier routing

Writ classifies every task into a complexity tier (0-3) before work begins. The tier
controls ceremony (how many approval gates), not knowledge (which rules are injected).

| Tier | Label | Ceremony |
|------|-------|----------|
| 0 | Research | No gates. Read-only tasks. |
| 1 | Patch | No gates. 1-3 file changes, static analysis only. |
| 2 | Standard | Phases A-C combined (one gate) + test skeletons. |
| 3 | Complex | Full sequential gates (A, B, C, [D], test skeletons, final). |

The RAG inject hook prompts Claude to classify on the first turn. Gate hooks read the
declared tier from the session cache and adjust enforcement accordingly. If no tier is
declared, hooks default to Tier 3 (maximum ceremony). Tiers escalate up only, never down.

### How it works

1. **UserPromptSubmit hook** fires at the start of every user turn
2. Hook POSTs the user's prompt to `localhost:8765/query`
3. Writ's hybrid pipeline ranks rules by relevance (BM25 + vector + graph proximity)
4. Rules are injected into Claude's context as a `--- WRIT RULES ---` block
5. If no tier is declared, a classification directive is also injected
6. A session cache deduplicates rules across turns and tracks a token budget (8000 tokens)
7. When context exceeds 75% or budget is exhausted, injection silently skips

### What Claude sees

```
--- WRIT RULES (3 rules, standard mode) ---

[PY-ASYNC-001] (high, human, python) score=0.892
WHEN: Writing async Python code
RULE: All I/O operations must be async. Never use sync I/O in hot paths.
VIOLATION: Using requests.get() in an async handler
CORRECT: Using httpx.AsyncClient within async context

--- END WRIT RULES ---
```

### Feedback loop

The integration includes a complete feedback cycle:

- **Automatic feedback** -- the Stop hook correlates which rules were in context
  with static analysis outcomes (pass/fail) and auto-POSTs feedback to Writ.
  This feeds the frequency tracking system so rules can graduate from
  `ai-provisional` to `ai-promoted`.
- **Rule proposals** -- when no matching rules are found (or scores are low),
  the hook appends a proposal nudge to Claude's context. Claude can propose
  new rules via `POST /propose` when it discovers patterns not in the knowledge base.
- **Coverage tracking** -- the session cache records which files were written
  and which rule domains covered them, logged to `/tmp/writ-coverage-*.log`.

### Ensuring Writ starts with Claude Code

**Plugin mode (recommended):** If Claude Code discovers the `.claude-plugin/plugin.json`
manifest, the `ensure-server.sh` Init lifecycle script starts Neo4j and the Writ
server automatically. No manual setup needed.

**Manual mode:** If not using the plugin, run `scripts/ensure-server.sh` before
opening Claude Code, or start the services yourself:

```bash
docker start writ-neo4j           # start Neo4j
source .venv/bin/activate && writ serve   # start Writ
```

If Writ is not running, the hooks fall back gracefully -- Claude sees
`[Writ: server unavailable, proceeding without rules]` and proceeds normally.

### Hooks and agents

**Hooks (8):**

All hooks parse Claude Code's stdin JSON envelope via a shared parser
(`bin/lib/parse-hook-stdin.py`) with `$CLAUDE_TOOL_INPUT` env var fallback.
PreToolUse hooks that deny a write exit with code 2 (hard block per Claude Code's
internal hook contract). PostToolUse hooks skip validation when the write itself
failed (`tool_result_is_error`), preventing confusing cascading errors.

| Hook | Event | Matcher | Exit | Purpose |
|------|-------|---------|------|---------|
| `writ-rag-inject.sh` | UserPromptSubmit | all | 0 | Query Writ, inject rules into context |
| `writ-context-tracker.sh` | Stop | all | 0 | Track context pressure, auto-feedback, coverage |
| `check-gate-approval.sh` | PreToolUse | Write\|Edit | 0/2 | Gate sequencing (Phases A-D) |
| `enforce-final-gate.sh` | PreToolUse | Write\|Edit | 0/2 | Block completion until ENF-GATE-FINAL |
| `pre-validate-file.sh` | PreToolUse | Write\|Edit | 0/2 | Static analysis before write |
| `validate-file.sh` | PostToolUse | Write\|Edit | 0/1 | Static analysis after write |
| `validate-handoff.sh` | PostToolUse | Write\|Edit | 0/1 | Handoff JSON validation |
| `log-session-metrics.sh` | Stop | all | 0 | Gate metrics logging |

**Agents (3):**

| Agent | Purpose |
|-------|---------|
| `plan-guardian` | Verify code slices against approved plan (read-only) |
| `static-analysis` | Run static analysis in isolated context |
| `slice-builder` | Generate one implementation slice in isolation |

All hooks and agents are project-agnostic -- they work with any language or framework.

## CLI Commands

| Command | Description |
|---|---|
| `writ serve` | Start the local service. Pre-warms indexes. Binds to localhost:8765. |
| `writ ingest <path>` | Parse Markdown rules and ingest into the graph. Validates schema. Auto-exports on success. |
| `writ validate` | Run integrity checks: conflicts, orphans, staleness, redundancy, unreviewed count, frequency staleness, graduation flags. |
| `writ add` | Interactive rule authoring with relationship suggestion and conflict detection. |
| `writ edit <rule_id>` | Edit an existing rule with re-validation and relationship re-analysis. |
| `writ export <path>` | Regenerate Markdown from graph. Round-trip fidelity with ingest. |
| `writ compress` | Cluster rules into abstraction nodes. Evaluates HDBSCAN vs k-means. |
| `writ propose` | Propose an AI-generated rule. Runs structural gate before ingestion. |
| `writ review` | Review AI-proposed rules. List, inspect, promote, reject, downweight, stats. |
| `writ feedback <rule_id> <signal>` | Record positive/negative feedback for a rule (hook integration). |
| `writ migrate` | One-time migration of existing rules into graph. |
| `writ query "..."` | CLI rule query for testing retrieval quality. |
| `writ status` | Health check: rule count, index status, export staleness. |

## Architecture

Five-stage hybrid retrieval pipeline:

1. **Domain Filter** -- Pre-filter to relevant domain subgraph
2. **BM25 Keyword** -- Tantivy sparse retrieval on trigger, statement, tags
3. **ANN Vector** -- hnswlib in-process semantic search
4. **Graph Traversal** -- Pre-computed adjacency cache for DEPENDS_ON, CONFLICTS_WITH, SUPPLEMENTS
5. **Ranking** -- Two-pass RRF: first pass scores BM25/vector/severity/confidence, second pass adds graph-neighbor proximity from top-3 results (AI-provisional rules excluded from proximity seeding). Hard authority preference: human-authored rules outrank AI-provisional within the empirically derived threshold (0.0749). Context budget applied. Summary mode (< 2K tokens) returns abstraction summaries when available.

Mandatory enforcement rules (`ENF-*`) are never ranked -- they are loaded by the skill directly, outside the retrieval pipeline.

**Authority model**: Rules have authority levels (human / ai-provisional / ai-promoted). AI-proposed rules enter as ai-provisional with capped confidence (speculative). Human review via `writ review` promotes, rejects, or downweights them. Frequency tracking (times_seen_positive/negative) enables empirical confidence graduation at n=50 with ratio >= 0.75.

**Structural gate**: AI rule proposals pass through five checks before ingestion: schema validation, specificity (vague language disqualifiers), redundancy (cosine > 0.95), novelty (cosine > 0.85), and conflict detection. The gate catches structural failures -- human judgment is the gate for correctness.

**Compression layer**: Rules are clustered into abstraction nodes (HDBSCAN or k-means, auto-selected by silhouette score). When context budget is tight, the pipeline returns abstraction summaries instead of individual rules. `writ compress` evaluates both algorithms and writes the winner.

**Session tracking**: Client-side session context tracker (`writ/retrieval/session.py`) accumulates loaded rules across sequential queries, preventing duplicates and decrementing the token budget. The server remains stateless -- session state lives in the client (skill).

## API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/query` | POST | Ranked rule retrieval. Accepts `query`, `domain`, `budget_tokens`, `exclude_rule_ids`, `loaded_rule_ids`. |
| `/propose` | POST | AI rule proposal. Runs structural gate, ingests if accepted with authority=ai-provisional. |
| `/feedback` | POST | Record positive/negative feedback for a rule. Increments frequency counters. |
| `/rule/{rule_id}` | GET | Full rule node. `?include_graph=true` adds 1-hop context. Returns `abstraction_id` and `sibling_rule_ids`. |
| `/conflicts` | POST | CONFLICTS_WITH edges between provided rule_ids. |
| `/abstractions` | GET | All abstraction nodes with member counts. |
| `/abstractions/{id}` | GET | Full abstraction with member rule details. |
| `/health` | GET | Status, rule/mandatory count, index state, startup time. |

## Benchmarks

All targets from [handbook Section 10](RAG_arch_handbook.md). Baseline results in [PHASE5_RESULTS.md](PHASE5_RESULTS.md). Scale results in [SCALE_BENCHMARK_RESULTS.md](SCALE_BENCHMARK_RESULTS.md).

Run with: `pytest benchmarks/bench_targets.py -v -s`

### Latency (80-rule corpus, ONNX Runtime, warm indexes + LRU cache)

| Stage | Component | p95 | Budget | Headroom |
|---|---|---|---|---|
| 2 | BM25 (Tantivy) | 0.175ms | 2.0ms | 11x |
| 3 | Vector (hnswlib) | 0.047ms | 3.0ms | 64x |
| 4 | Adjacency cache | 0.001ms | 3.0ms | 3000x |
| 5 | Ranking (two-pass) | 0.089ms | 1.0ms | 11x |
| -- | **End-to-end** | **0.19ms** | **10.0ms** | **53x** |

Embedding inference via ONNX Runtime with LRU cache (maxsize=1024). PyTorch/sentence-transformers eliminated from runtime. Cache hits are sub-microsecond; cold misses ~2ms via ONNX (vs ~7ms PyTorch).

### Retrieval Quality

| Metric | Value | Threshold |
|---|---|---|
| MRR@5 (19 ambiguous queries) | 0.7842 | > 0.78 |
| Hit rate (83 total queries) | 97.59% | > 90% |
| Context reduction | 4.4x (80 rules) | -- |
| ONNX vs PyTorch ranking stability | 0/83 queries diverge | Identical top-5 |

### Scale Performance

| Metric | 80 rules | 500 rules | 1K rules | 10K rules |
|---|---|---|---|---|
| E2E p95 | 0.19ms | 0.48ms | 0.57ms | 0.55ms |
| Context reduction | 4.4x | 39.3x | 75.8x | 727x |
| Memory (RSS)* | 1,564 MB | 2,337 MB | 2,695 MB | 2,951 MB |
| Cold start | 0.58s | 3.64s | 7.74s | 70.2s |
| Clusters | 13 | 70 | 419 | 519 |
| Compression ratio | 5.6x | 7.6x | 2.8x | 25.2x |
| Session duplicates | 0 | 0 | 0 | 0 |

*Scale benchmark RSS is `ru_maxrss` (high-water mark) in a sequential process that also loads sklearn/hdbscan for compression benchmarks. A clean `writ serve` process at 80 rules uses ~700MB.

**10K cold start note:** ONNX bulk encoding is 3-5x slower than sentence-transformers' C++ batching. The 70s cold start at 10K is a known tradeoff for eliminating PyTorch from the runtime. At that scale, pre-computed embeddings (Qdrant with persistent vectors) is the correct architecture.

### Infrastructure

| Metric | Value | Budget |
|---|---|---|
| Cold start (build_pipeline, 80 rules) | 0.58s | < 3s |
| Memory footprint (clean serve) | ~700 MB | < 2 GB |
| Integrity check (80 rules) | 23ms median, 50ms p95 | < 500ms |
| Single rule ingestion | < 0.01s median | < 2s |

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
# Unit and integration tests (314 tests across 16 test files)
pytest tests/ -q

# Performance benchmarks (12 tests, requires Neo4j with migrated rules)
pytest benchmarks/bench_targets.py -v -s

# Neo4j traversal scale benchmarks (1K/10K nodes)
pytest benchmarks/run_benchmarks.py -v -s

# Comprehensive scale benchmark (80/500/1K/10K rules)
python benchmarks/scale_benchmark.py

# Export ONNX model (one-time, required for ONNX inference)
python scripts/export_onnx.py
```

## Configuration

All settings in `writ.toml`, overridable via `WRIT_` environment variables.

## Project Status

All 9 original phases complete. Evolution plan Phases 1-4 complete. ONNX inference optimization applied. Claude Code integration complete (RAG bridge, feedback loop, proposal triggers).

**Evolution plan (Dwarf in the Glass):**

| Phase | Status | What it added |
|---|---|---|
| Phase 1: Schema Hardening | Complete | Extensible scope, decoupled mandatory, Neo4j constraints |
| Phase 2: Structural Pre-Filter | Complete | 5-check gate, human review queue, origin context store |
| Phase 3: AI Rule Ingestion | Complete | Authority model, hard preference rule, proposal workflow |
| Phase 4: Frequency Tracking | Complete | Graduation at n=50, increment triggers, staleness detection |
| Phase 5: Domain Generalization | Conditional | Requires concrete non-coding domain passing generalizability test |

**ONNX optimization:** PyTorch/sentence-transformers eliminated from runtime. Embedding inference via ONNX Runtime + Rust tokenizers. 15-29x query latency improvement. Ranking-identical to PyTorch (0/83 queries diverge).

Future work:
- Complement mode -- semantic gap detection for multi-query sessions. Viable at 500+ rules.
- Graph-level versioning -- immutable snapshots. Relevant when corpus scales and graph mutations during sessions could cause inconsistency.
- Qdrant migration -- swap hnswlib at 10K+ rules. VectorStore Protocol already in place. Would also eliminate the 10K cold start problem by persisting embeddings.
- Phase 5 -- domain generalization. Conditional on generalizability test with a concrete non-coding domain.
- PreToolUse hook -- narrow, file-context-specific rule injection before writes. Deferred pending measurement of whether the broad UserPromptSubmit query covers file-specific needs.

See [EVOLUTION_PLAN.md](EVOLUTION_PLAN.md) for the evolution plan specification. See [CONTRIBUTING.md](CONTRIBUTING.md) for the multi-author rule governance process. See [SCALE_BENCHMARK_RESULTS.md](SCALE_BENCHMARK_RESULTS.md) for detailed scale benchmarks.

## Full Specification

See [RAG_arch_handbook.md](RAG_arch_handbook.md) for the complete architecture specification.