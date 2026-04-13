# WRIT -- RAG Architecture Handbook

**Hybrid Knowledge Retrieval System**
**v2.0 -- March 2026**

| | |
|---|---|
| **Author** | Lucio Saldivar (Infinri) |
| **Project** | Writ / AI Workflow Coding Bible |
| **Status** | All 9 phases + evolution Phases 1-4 complete. ONNX inference optimized. |
| **Classification** | Internal -- Study Guide + Architecture Reference |

---

## 1. Vision & Problem Statement

Writ is a global, persistent knowledge system that governs AI-assisted coding behavior across every project, every session, and every agent context. It is not a per-project config file. It is not a prompt template. It is a living rule graph that an AI agent can query in real-time during a coding task and receive enforceable, binary, context-aware guidance in under 10 milliseconds.

### 1.1 The Core Problem

At 68 rules stored as Markdown files, Writ works. A human can read it. An AI can be context-stuffed with it. But three things break as the corpus scales:

| Problem | At 68 Rules | At 1,000+ Rules |
|---|---|---|
| Relationship visibility | Manual cross-references in prose | Invisible to machine traversal -- broken by default |
| Epistemological metadata | Implicit, trusted by convention | No confidence, staleness, or evidence tracking |
| Integrity | Human diligence sufficient | Contradiction, redundancy, orphan rules undetectable |
| Retrieval quality | Context-stuffing the whole Bible works | Context window exhausted -- retrieval precision required |

### 1.2 The Target State

The target architecture replaces the flat Markdown file system with a graph-native knowledge database paired with a hybrid retrieval layer. The result is a local service that:

- Answers agent queries in under 10ms with deterministic latency
- Returns the most relevant rules for a given coding situation -- not a keyword match
- Traverses rule relationships to return dependent, conflicting, and supplementary rules
- Weights rules by confidence, staleness, and evidence -- not just text similarity
- Exposes a single HTTP interface that all agent runtimes connect to
- Installs once at `~/.claude/skills` and serves every project globally

> **KEY INSIGHT:** The secret sauce is not raw speed. It is pre-computation. Nothing is calculated at query time that could have been calculated at ingestion time. The graph, the embeddings, the BM25 index, and the abstraction node summaries are all pre-built. Retrieval serves from memory.

---

## 2. Rule Anatomy & Enforceability Standard

Before designing the retrieval system, the rule schema must be locked. Retrieval quality is bounded by rule quality. Retrieving the wrong thing fast is worse than retrieving the right thing slowly.

### 2.1 The Enforceability Requirement

A rule is enforceable if and only if an AI agent can make a binary pass/fail decision from it without human interpretation. The following words disqualify a rule from being a rule:

| Disqualifying Language (Principle) | Enforceable Replacement (Rule) |
|---|---|
| "Consider using a service layer" | "Controllers must not contain SQL queries. Violation: direct DB call in controller. Pass: delegates to repository." |
| "Be aware of plugin execution order" | "Before plugins on the same method must declare sort order. Trigger: 2+ before plugins on same observable." |
| "Where appropriate, centralize logic" | "Logic referenced by 2+ modules must be extracted to a shared service. Trigger: duplicate method body detected across modules." |

### 2.2 Required Rule Fields

Every rule node in the graph must carry all of the following fields. Rules missing any required field are rejected at ingestion.

| Field | Required | Purpose |
|---|---|---|
| `rule_id` | Yes | Unique identifier. Format: `PREFIX-NAME-NNN` (e.g. `ARCH-ORG-001`) |
| `domain` | Yes | Top-level category: Architecture, Database, Security, Framework |
| `severity` | Yes | Critical \| High \| Medium \| Low -- affects retrieval ranking weight |
| `scope` | Yes | file \| module \| slice \| PR -- defines enforcement granularity |
| `trigger` | Yes | Specific, measurable condition that activates this rule |
| `statement` | Yes | 1-2 sentence concrete requirement. No abstractions. Binary testable. |
| `violation` | Yes | Concrete bad code or AI output. Agent sees this and knows it fails. |
| `pass` | Yes | Concrete good code or AI output. Agent sees this and knows it passes. |
| `enforcement` | Yes | Gate, hook, or verification step that mechanically catches violations |
| `rationale` | Yes | One paragraph max. The why -- used to generate abstraction summaries. |
| `mandatory` | Graph only | Boolean. `true` for enforcement rules (`ENF-*`) -- excluded from retrieval pipeline, always loaded by skill. `false` for domain rules. Migration default: `true` if `rule_id` starts with `ENF-`, else `false`. See Section 7.5. |
| `confidence` | Graph only | battle-tested \| production-validated \| peer-reviewed \| speculative. Migration default: `production-validated`. See Section 2.3. |
| `evidence` | Graph only | Source of the rule: incident, PR, external doc, architectural decision. Migration default: `doc:original-bible`. See Section 2.3. |
| `staleness_window` | Graph only | Days before rule requires re-validation. Migration default: 365. |
| `last_validated` | Graph only | ISO date of last human review. Migration default: migration run date. Stale rules are flagged in retrieval results. |

### 2.3 Migration Defaults & Ranking Impact

"Graph only" fields are not present in the current Markdown rule files. During the Phase 3 migration, these fields receive explicit defaults. The defaults are chosen to avoid penalizing established rules that predate the graph.

| Field | Migration Default | Rationale |
|---|---|---|
| `confidence` | `production-validated` (0.8) | The 68 existing rules have been used in production. They are not `speculative`, but they have not been formally reviewed against the graph schema, so `battle-tested` (1.0) is not yet earned. |
| `evidence` | `doc:original-bible` | All 68 rules originate from the original Bible document. Specific incident/PR links are added during human review. |
| `staleness_window` | 365 days | Standard annual review cycle. |
| `last_validated` | Migration run date | Starts the clock. Rules become stale 365 days after migration unless reviewed sooner. |

**Ranking interaction:** At the default `confidence: production-validated` (0.8), a migrated rule loses 0.02 in final score compared to a `battle-tested` rule (0.1 * (1.0 - 0.8) = 0.02). This is a minor tiebreaker, not a ranking penalty. A migrated rule will never rank below a genuinely new `speculative` rule unless the new rule dominates on both BM25 and vector relevance.

**Upgrade path:** `writ validate --review-confidence` lists all rules still at migration defaults. Human review upgrades them to `battle-tested` or downgrades to `peer-reviewed` as appropriate.

### 2.4 Limits of Schema Validation

Schema validation (Phase 1) catches structural problems: missing fields, wrong types, empty values. It cannot catch:

- **Vague rules in valid format.** A statement like "All API calls must handle timeouts. Violation: API call without try-catch. Pass: API call with try-catch" passes every schema check. It is also a bad rule because it says nothing about what the catch block should do. The required fields enforce structure, not quality.
- **Conditional conflicts between independently correct rules.** Two rules from different teams can both be enforceable and individually correct but produce contradictory guidance for the same file. Example: "all data access must go through repositories" (Architecture team) vs. "use direct SQL for bulk operations exceeding 10,000 rows" (Platform team). `CONFLICTS_WITH` is a binary structural edge -- it cannot represent "conflicts only when applied to files that do both." The Phase 4 integrity layer detects contradictions by embedding similarity, not by semantic reasoning about when two rules produce different outputs for the same input.

**What schema validation is:** A gate that rejects structurally malformed rules at ingestion. It is necessary but not sufficient.

**What schema validation is not:** A substitute for human review of rule quality. Quality enforcement at scale is a governance problem, not a tooling problem. The Phase 6 authoring tools can flag candidate issues (similarity to existing rules, keyword overlap in triggers), but the decision to accept or reject a rule requires human judgment about whether the statement is genuinely enforceable in context.

**Multi-team intake implication:** When multiple teams contribute rules independently, the rate of conditional conflicts and quality-dressed-as-enforceability will increase. The handbook's `CONFLICTS_WITH` edge taxonomy handles the structural case. The conditional case requires a review process defined in CONTRIBUTING.md (Phase 6 deliverable) where cross-team rules are reviewed for contextual compatibility before ingestion. This is not automatable.

---

## 3. Graph Architecture

The source of truth moves from Markdown files to a graph database. Every rule becomes a typed node. Every relationship between rules becomes a traversable, typed edge. All Markdown files become generated views derived from the graph -- not maintained directly.

### 3.1 Node Types

| Node Type | Description | Key Properties |
|---|---|---|
| Rule | An atomic enforceable constraint | rule_id, domain, severity, scope, trigger, statement, violation, pass, confidence, staleness_window, mandatory |
| Abstraction | A cluster summary node. Pre-computed. Compresses groups of rules into a principle the agent can read before drilling down. | summary, rule_ids[], domain, compression_ratio |
| Domain | Top-level taxonomy node | name, rule_count, last_updated |
| Evidence | Source artifact that justifies a rule | type (incident\|PR\|doc\|ADR), reference, date |
| Tag | Freeform classification label | name, rule_count |

### 3.2 Edge Types

Edges are the intelligence of the graph. They are what makes Writ a knowledge system rather than a searchable document store.

| Edge Type | Direction | Meaning |
|---|---|---|
| `DEPENDS_ON` | Rule -> Rule | This rule cannot be applied correctly without first applying the target rule. |
| `PRECEDES` | Rule -> Rule | This rule must be checked before the target rule in the same enforcement pass. |
| `CONFLICTS_WITH` | Rule <-> Rule | Applying both rules simultaneously produces a contradiction. Requires human resolution. |
| `SUPPLEMENTS` | Rule -> Rule | Target rule adds depth or nuance to this rule. Return together when context budget allows. |
| `SUPERSEDES` | Rule -> Rule | This rule replaces the target rule. Target is deprecated but retained for audit history. |
| `RELATED_TO` | Rule <-> Rule | Weak association. Skeleton edge promoted to a stronger type on human review. |
| `APPLIES_TO` | Rule -> Domain/Tag | Scope declaration. Used for domain-filtered queries. |
| `ABSTRACTS` | Abstraction -> Rule[] | Connects a cluster summary to the rules it compresses. |
| `JUSTIFIED_BY` | Rule -> Evidence | Links a rule to its evidence source for confidence scoring. |

### 3.3 Graph Database -- Neo4j (COMMITTED)

**Status: Closed. Neo4j is the graph engine for all graph operations -- hot-path traversal, offline integrity, and rule storage.**

The scale requirement (6 teams, 1TB corpus) eliminates PostgreSQL+AGE. AGE runs Cypher through PostgreSQL's query planner, adding overhead that becomes unacceptable at TB-scale graph traversal. Neo4j is a native graph engine purpose-built for this workload.

| Capability | Neo4j Feature | Used By |
|---|---|---|
| Hot-path traversal (Stage 4) | Native Cypher queries, zero-copy traversal | Retrieval pipeline |
| Full-text keyword search (Stage 2) | Built-in Lucene-based full-text indexes | Available as fallback -- but Stage 2 uses Tantivy (see Section 3.4) |
| Offline integrity | Cypher aggregation + APOC graph algorithms | `writ validate` |
| Rule storage | Node/edge CRUD with ACID transactions | Ingestion, migration, authoring |

**Phase 2 benchmark protocol (validates performance, not engine choice):**

1. Generate a synthetic 1,000-node graph with realistic edge density (avg 4 edges/node).
2. Run 3-hop traversal queries on Neo4j. Measure p95 latency over 1,000 iterations.
3. If Neo4j exceeds the Stage 4 latency budget (< 3ms), the first mitigation is pre-computed adjacency lists cached in memory at service startup. Pre-implementation benchmarks show in-memory traversal at 0.05ms (p95) for 2-hop on 1,000 nodes (see `benchmark.md`).
4. Benchmark at 10K, 100K, and 1M nodes to validate the scale path. The 1,000-node benchmark validates the pipeline; the larger benchmarks validate the architecture.

### 3.4 Keyword Search -- Tantivy (COMMITTED)

**Status: Closed. Tantivy is the keyword search engine for Stage 2 of the retrieval pipeline.**

Tantivy is Apache Lucene rewritten in Rust. It provides BM25 scoring on structured text without the JVM overhead, slow reindexing, and operational complexity of Elasticsearch/OpenSearch. Python bindings via `tantivy-py`.

| Property | Tantivy | Elasticsearch |
|---|---|---|
| Language | Rust (no GC pauses) | Java (JVM, GC overhead) |
| Deployment | Library -- embeds in process | Separate service + cluster |
| Index rebuild (1K docs) | Milliseconds | Seconds to minutes |
| Index rebuild (1M docs) | Seconds | Minutes to hours |
| BM25 scoring | Native | Native |
| Memory footprint | ~50-100 MB for 1M docs | 2-4 GB JVM heap minimum |
| Scale ceiling | Millions of documents, single process | Billions, distributed |

**Why not Elasticsearch:** Real-world experience with ES/OS in Magento shows slow reindexing, heavy resource consumption, and operational complexity. Tantivy delivers the same BM25 scoring without those costs. If the corpus exceeds Tantivy's single-process ceiling (unlikely for rule text -- even 1M rules is ~2 GB of text), the migration path is to shard the index by domain.

**Integration:** Tantivy indexes are built at ingestion time (`writ ingest`) and pre-warmed into memory at service startup (`writ serve`). The index is read-only during query time. Rebuilding the index is a background operation that does not block queries -- the service continues serving from the old index until the new one is ready, then swaps atomically.

### 3.5 Vector Search -- hnswlib (now) / Qdrant (at scale)

**Status: hnswlib for all phases. Embedding inference via ONNX Runtime (preferred) or sentence-transformers (fallback). Qdrant is the defined migration target at 10K+ rules.**

Stage 3 vector search is behind an abstraction layer (`retrieval/embeddings.py`). The interface accepts a query vector and returns ranked `rule_id` results. The implementation behind that interface swaps without changes to the pipeline.

**Phase 1-5 implementation: hnswlib (in-process)**

hnswlib is a C++ HNSW library with Python bindings. It runs in-process with zero network overhead. Pre-implementation benchmarks show 0.04ms p95 at 1,000 vectors. At the corpus sizes relevant to pipeline validation (70-10,000 rules), it is faster than any networked alternative and adds no operational complexity.

| Property | hnswlib | Qdrant |
|---|---|---|
| Deployment | In-process library | Separate service (Rust binary) |
| Latency (1K vectors) | 0.04ms p95 | ~0.5-1ms (network overhead) |
| Operational cost | Zero -- no service to manage | Service lifecycle, health monitoring |
| Scale ceiling | Single-process, no quantization | Billions of vectors, quantization, distributed |
| Filtered search | Manual post-filter | Native filterable HNSW |

**Migration trigger to Qdrant:** Any of the following signals:

- Corpus exceeds 100K rules (hnswlib memory grows linearly, no quantization)
- Filtered vector search becomes a requirement (domain-scoped ANN without a separate domain filter stage)
- p99 vector search latency exceeds 5ms on the production corpus
- Memory for the HNSW index competes with other service memory needs

**Qdrant migration target (validated):**

Qdrant is the only vector DB candidate with published, independently verified benchmarks at TB scale:

- 50M vectors, 768-dim: p50 4.7ms, p95 5.5ms, p99 5.8ms (90% recall)
- Quantization: scalar (4x), binary (32x), product (64x) memory reduction
- Filterable HNSW with query-time cardinality evaluation
- Rust (no GC, predictable tail latency)
- Production-validated with Neo4j: Lettria GraphRAG case study (20-25% accuracy uplift over pure vector search)

**Why not Neo4j Vector Index:** Neo4j's vector search (Lucene HNSW, added 5.11) has no published benchmarks, no quantization, and immature filtering (2026.01). The single-query advantage (vector + graph in one Cypher call) does not justify betting on an unproven component at the system's target scale.

**The swap is a single-file change.** `retrieval/embeddings.py` exposes `search(vector, k, filters) -> list[ScoredResult]`. The hnswlib implementation builds and queries an in-process index. The Qdrant implementation calls the Qdrant gRPC/REST API. The pipeline, ranking, and all upstream/downstream stages are unchanged.

### 3.6 Graph Engine Roles

Neo4j serves both hot-path and offline operations. There is no second graph engine.

| Role | Method | Used For | Latency Requirement |
|---|---|---|---|
| Hot-path traversal | Cypher queries via Neo4j Python driver (bolt protocol) | Stage 4 of retrieval pipeline: 1-2 hop traversal from query results | < 3ms |
| Offline integrity | Cypher aggregation + APOC graph algorithms | `writ validate`: orphan detection, contradiction analysis, staleness, redundancy | < 5s for full corpus at scale |

NetworkX is removed from the architecture. At 1TB, loading the graph into Python memory for analysis is not viable. Neo4j's native Cypher queries and APOC library handle the same integrity checks (shortest path for orphan detection, pattern matching for contradiction analysis) without loading the full graph into a separate process.

---

## 4. Hybrid Retrieval Pipeline

The retrieval pipeline is the core engineering challenge. It must be deterministically fast, context-budget-aware, and return the most relevant rules for a given coding situation -- not the most similar text.

### 4.1 Pipeline Architecture

Five stages execute in sequence. Each stage eliminates candidates so the next stage works on a smaller, higher-quality set. **The pipeline operates on domain rules only.** Mandatory enforcement rules (`ENF-*`, `mandatory: true` in the graph) are excluded before Stage 1. They are loaded by the skill directly, not ranked. See Section 7.5.

| # | Stage | Method | Latency Budget | Pre-Impl Benchmark (p95) | Output |
|---|---|---|---|---|---|
| 1 | Domain Filter | Agent declares domain context at session start. Pre-filters corpus to relevant domain subgraph. | < 1ms | Not benchmarked (trivial filter) | Domain-scoped rule set (10-30% of corpus) |
| 2 | BM25 Keyword Filter | Tantivy BM25 sparse retrieval on trigger, statement, and tag fields. Eliminates 80-90% of candidates. | < 2ms | 0.59ms (rank-bm25; Tantivy expected faster) | Top-N keyword candidates (N = 50) |
| 3 | ANN Vector Search | hnswlib in-process ANN on pre-computed embeddings. Qdrant at scale (Section 3.5). | < 3ms | 0.04ms (hnswlib, 1K vectors) | Top-K semantic candidates (K = 10) |
| 4 | Graph Traversal | Neo4j Cypher 1-2 hop traversal from top-K results. Fetches DEPENDS_ON, CONFLICTS_WITH, SUPPLEMENTS edges. | < 3ms | 0.05ms (in-memory baseline; Neo4j TBD Phase 2) | Enriched candidate set with relationship context |
| 5 | Ranking & Return | Reciprocal Rank Fusion of BM25 + vector scores, weighted by severity and confidence. Context budget applied. | < 1ms | Not benchmarked (arithmetic) | Final ranked rule list for agent context |

**Total pipeline target: p95 < 10ms end-to-end, deterministic ceiling**

> **LATENCY BUDGET STATUS (VALIDATED):** All stages pass their budgets on the production 80-rule corpus. At 10K synthetic rules, end-to-end p95 = 8.0ms (under 10ms budget). Stage 4 uses pre-computed adjacency cache (0.002ms) after Neo4j live queries exceeded the 3ms budget in Phase 2 benchmarks. Full results in `PHASE5_RESULTS.md` and `SCALE_BENCHMARK_RESULTS.md`.

**Stage 4 failure mitigation:** Pre-compute adjacency lists into memory at startup so traversal becomes a hash lookup rather than a live graph query. Trades memory for latency. Pre-implementation benchmark confirms in-memory 2-hop traversal at 0.05ms (p95) on 1,000 nodes / ~4,000 edges -- this path is validated.

**Stage 3 result:** Vector search latency is not a constraint. At 10K rules, hnswlib p95 = 0.205ms (budget: 3ms). `ef_search = 50` is the production value (set in `writ.toml`). MiniLM embeddings achieved MRR@5 = 0.7842, passing the 0.78 threshold. mpnet reserved as upgrade path if quality degrades at scale.

### 4.2 Ranking Formula

The final score for each candidate rule is a weighted combination of retrieval signal and rule metadata:

```
score = (w1 * bm25_rank) + (w2 * vector_rank) + (w3 * severity_weight) + (w4 * confidence_weight) + (w5 * graph_proximity)
```

| Weight Component | Final Value | Values | Notes |
|---|---|---|---|
| BM25 keyword rank (`w1`) | 0.198 | Normalized rank from BM25 sparse retrieval | Tuned Phase 5-6 |
| Vector semantic rank (`w2`) | 0.594 | Normalized rank from ANN embedding search | Tuned Phase 5-6 |
| Severity weight (`w3`) | 0.099 | Critical=1.0, High=0.75, Medium=0.5, Low=0.25 | Tuned Phase 5-6 |
| Confidence weight (`w4`) | 0.099 | battle-tested=1.0, production-validated=0.8, peer-reviewed=0.6, speculative=0.3 | Tuned Phase 5-6 |
| Graph proximity (`w5`) | 0.01 | 1-hop=1.0, 2-hop=0.5, none=0.0. Discrete values from AdjacencyCache. | Added Phase 6 |

> **TUNING RESULT:** Weights finalized after two tuning rounds: initial 0.4/0.4/0.1/0.1 -> 0.2/0.6/0.1/0.1 (Phase 5 sweep) -> 0.198/0.594/0.099/0.099/0.01 (Phase 6: Phase 5 ratios * 0.99, graph proximity added). Constraint: `w1 + w2 + w3 + w4 + w5 = 1.0`. Weights stored in `writ.toml`.

### 4.3 Ground-Truth Query Set

MRR@5 > 0.78 is the Phase 5 gate metric (recalibrated from 0.85 after switching from manual holistic scoring to automated strict 1/rank methodology -- same retrieval quality, different measurement scale). MRR requires a set of queries with known correct answers. The quality of the evaluation is bounded by the quality of this set.

**Specification:**

| Parameter | Requirement |
|---|---|
| Minimum query count | 50 |
| Author | Human (Lucio). Not AI-generated. Queries must reflect real coding situations, not keyword-stuffed test strings. |
| Domain coverage | At least one query per domain (Architecture, Database, Security, Framework). No domain may have fewer than 5 queries. |
| Difficulty spread | At least 10 queries where the correct rule is not the most obvious keyword match (tests semantic retrieval over BM25). |
| Answer format | Each query maps to 1-3 correct rule_ids, ordered by relevance. Partial credit: correct rule in top-5 but wrong rank scores 0.5. |
| Authoring deadline | Must be complete before Phase 5 begins. Started during Phase 3 (migration), refined during Phase 4 (integrity checks reveal which rules cluster together). |
| Scaling policy | The query set must grow proportionally with the corpus. Target ratio: 1 query per 10 rules (minimum). At 68 rules, 50 queries covers 74% of rules. At 1,000 rules, 50 queries covers 5% -- insufficient. When the corpus doubles, the query set must be expanded to maintain coverage. MRR@5 measured against a query set that covers 5% of the corpus is not a valid quality signal. **Enforcement:** `writ validate` checks the query-to-rule ratio and warns when it drops below 1:10. This is a non-blocking warning, not a hard failure -- but it signals that the evaluation set is going stale. |
| Storage | `tests/fixtures/ground_truth_queries.json` -- versioned in repo. |

**Example entry:**

```json
{
  "query": "controller contains SQL query",
  "domain": "Architecture",
  "expected": ["ARCH-ORG-001"],
  "notes": "Tests basic keyword + semantic match on trigger field"
}
```

### 4.4 Context Budget Management

The agent operates under a context window budget. The retrieval system must respect this budget and never return more rules than fit. Three modes govern this:

| Mode | Trigger | Behaviour |
|---|---|---|
| Summary mode | Budget < 2,000 tokens | Return abstraction node summaries only. Agent receives compressed principles, can request drill-down. |
| Standard mode | Budget 2,000-8,000 tokens | Return top-5 full rules with statement, trigger, violation, and pass fields. Omit rationale. |
| Full mode | Budget > 8,000 tokens | Return top-10 full rules including rationale and graph relationship context. |

---

## 5. Environment & Technology Stack

All components are chosen for TB-scale performance with sub-10ms query latency. The service connects to its backing stores (Neo4j, vector DB, Tantivy index) via standard connection credentials -- deployment topology (local, remote, containerized) is a configuration decision, not an architecture one.

### 5.1 Runtime Requirements

| Requirement | Minimum | Notes |
|---|---|---|
| Python | 3.11+ | 3.12 recommended. Required for all service components. |
| RAM | 8 GB available | Writ service (~1 GB) + Neo4j (2-4 GB) + vector DB + Tantivy index. 16 GB recommended for concurrent ingest + query at scale. |
| Storage | Scales with corpus | Neo4j data dir + vector index + Tantivy index + embedding cache. At 70 rules: < 1 GB. At 1TB corpus: plan storage accordingly. |
| Neo4j | 5.x+ | Graph database for traversal, integrity, and rule storage (Section 3.3). |
| Qdrant | 1.17+ (at scale) | Vector database -- migration target when corpus outgrows hnswlib (Section 3.5). Not required for Phases 1-5. |
| OS | macOS 13+ / Linux | Windows supported via WSL2. Native Windows not tested. |
| Claude Code | Pro plan or higher | Required for Claude Code skill system. |

### 5.2 Python Dependencies

| Package | Version | Purpose |
|---|---|---|
| `fastapi` | ^0.115 | Async HTTP server for the Writ service |
| `uvicorn` | ^0.32 | ASGI server -- starts in <500ms, serves Writ |
| `neo4j` | ^5.0 | Neo4j Python driver. Graph traversal, integrity, rule storage (Section 3.3). |
| `tantivy` | ^0.22 | Rust-based BM25 keyword search. Stage 2 of retrieval pipeline (Section 3.4). Python bindings via `tantivy-py`. |
| `onnxruntime` | ^1.24 | ONNX Runtime for embedding inference -- replaces PyTorch at runtime. 15-29x faster query latency. |
| `tokenizers` | ^0.21 | Rust-backed tokenizer (HuggingFace). No PyTorch dependency. |
| `hnswlib` | ^0.8 | In-process HNSW vector search. Migrates to Qdrant at scale (Section 3.5). |
| `sentence-transformers` | ^3.3 | Dev/build dependency only -- used by `scripts/export_onnx.py` to export ONNX model. Not loaded at runtime when ONNX model is available. |
| `httpx` | ^0.27 | Async HTTP client used by the skill to call the service |
| `pydantic` | ^2.9 | Schema validation for rule ingestion and API request/response typing |
| `typer` | ^0.13 | CLI entrypoint for writ serve, ingest, validate, export commands |
| `rich` | ^13 | Terminal output formatting for CLI commands |
| `pytest` | ^8 | Test runner for schema validation, retrieval quality, and pipeline tests |

### 5.3 Embedding Model

The embedding model runs locally via ONNX Runtime (preferred) or sentence-transformers (fallback). No external API calls in the retrieval path. PyTorch is not loaded at runtime when the ONNX model is available.

| Backend | Model | Single-text p95 | Batch-80 | Notes |
|---|---|---|---|---|
| ONNX Runtime | `all-MiniLM-L6-v2` | ~2ms (cold), sub-us (cached) | 0.51s | Production default. Exported via `scripts/export_onnx.py`. |
| sentence-transformers | `all-MiniLM-L6-v2` | ~7ms | 0.14s | Fallback when ONNX model not exported. Faster bulk encoding. |

Query-time encoding uses an LRU cache (maxsize=1024, ~1.5MB). Cache hits return in sub-microseconds. The cache returns independent copies to prevent mutation corruption.

ONNX export: `python scripts/export_onnx.py` converts the model via HuggingFace optimum with fused-attention optimization. Output at `~/.cache/writ/models/onnx/`. One-time operation.

Ranking stability: 0/83 ground-truth queries produce different top-5 results between ONNX and PyTorch backends.

---

## 6. Project Structure

Writ is a single Python package installed globally. The service component and the CLI share the same codebase.

### 6.1 Directory Layout

```
writ/
├── bible/                      # Generated rule files (Markdown, produced by writ export)
│   ├── architecture/
│   ├── database/
│   ├── enforcement/
│   ├── frameworks/magento/
│   ├── languages/php/
│   ├── languages/python/
│   ├── performance/
│   ├── playbooks/
│   ├── rules/
│   ├── security/
│   └── testing/
├── writ/                       # Python package
│   ├── __init__.py
│   ├── cli.py                  # typer CLI (serve, ingest, validate, add, edit, export, compress, propose, review, feedback, migrate, query, status)
│   ├── server.py               # FastAPI HTTP server (/query, /propose, /feedback, /rule, /conflicts, /abstractions, /health)
│   ├── authoring.py            # Rule authoring domain logic (relationship suggestion, redundancy, conflict checks)
│   ├── gate.py                 # Structural pre-filter (5 checks) + AI rule proposal workflow
│   ├── origin_context.py       # SQLite store for AI rule proposal origin context
│   ├── frequency.py            # Frequency-based confidence graduation (Wilson CI)
│   ├── export.py               # Graph-to-Markdown export with round-trip fidelity, staleness detection
│   ├── graph/
│   │   ├── schema.py           # Node and edge Pydantic models (Rule with authority + frequency fields, Abstraction, 9 edge types)
│   │   ├── db.py               # Neo4j async connection layer (CRUD, authority ops, frequency increments, constraints)
│   │   ├── ingest.py           # Markdown -> graph ingestion with schema validation, extensible scope, mandatory decoupling
│   │   └── integrity.py        # Conflict, orphan, staleness, redundancy, unreviewed count, frequency staleness, graduation flags
│   ├── retrieval/
│   │   ├── pipeline.py         # Orchestrates all 5 stages. ONNX auto-detect, CachedEncoder, authority preference.
│   │   ├── keyword.py          # Tantivy BM25 index build and query (mandatory rules excluded)
│   │   ├── embeddings.py       # OnnxEmbeddingModel + CachedEncoder + VectorStore Protocol (hnswlib)
│   │   ├── traversal.py        # Pre-computed adjacency cache (replaced live Neo4j queries for hot path)
│   │   ├── ranking.py          # Two-pass RRF + authority preference + confidence graduation + proximity seeding filter
│   │   └── session.py          # Client-side session tracker (loaded_rule_ids, budget management)
│   └── compression/
│       ├── clusters.py         # HDBSCAN + k-means evaluation, silhouette-based selection
│       └── abstractions.py     # Abstraction node generation (centroid-nearest summary, no LLM)
├── scripts/
│   ├── migrate.py              # One-time rule migration with Neo4j constraint application
│   ├── export_onnx.py          # Export embedding model to ONNX via HuggingFace optimum
│   └── profile_hotpath.py      # pyinstrument profiling for pipeline hot path
├── tests/                      # ~320 tests across 30 test files
│   ├── conftest.py             # Shared fixtures (mock rules, mock DB, mock pipeline)
│   ├── fixtures/               # Sample rules, ground-truth queries (83 queries)
│   ├── test_schema.py          # 42 tests -- Pydantic model validation, scope extensibility, mandatory, authority
│   ├── test_infrastructure.py  # 11 tests -- Neo4j CRUD, Tantivy, hnswlib
│   ├── test_ingest.py          # 28 tests -- Markdown parsing, mandatory parsing, scope extensibility, Neo4j constraints
│   ├── test_integrity.py       # 10 tests -- Conflict, orphan, staleness, redundancy detection
│   ├── test_retrieval.py       # 14 tests -- Pipeline stages, MRR@5, latency
│   ├── test_graph_proximity.py # 13 tests -- Two-pass ranking, graph-neighbor boost
│   ├── test_authoring.py       # 17 tests -- writ add/edit, relationship suggestion
│   ├── test_gate.py            # 21 tests -- Structural gate checks (schema, specificity, novelty, redundancy, conflict)
│   ├── test_origin_context.py  # 7 tests -- SQLite origin context store
│   ├── test_authority.py       # 20 tests -- Authority model, ranking preference, proximity seeding, proposal
│   ├── test_frequency.py       # 16 tests -- Graduation logic, ranking integration, frequency properties
│   ├── test_embeddings.py      # 11 tests -- ONNX model, cached encoder with mutation safety, ranking stability
│   ├── test_export.py          # 33 tests -- Round-trip fidelity, staleness, auto-export
│   ├── test_compression.py     # 31 tests -- Clustering, abstractions, summary mode
│   └── test_session.py         # 22 tests -- SessionTracker, multi-query simulation
├── benchmarks/
│   ├── bench_targets.py        # 12 performance benchmarks (latency, MRR@5, hit rate)
│   ├── run_benchmarks.py       # Neo4j traversal scale benchmarks (1K/10K nodes)
│   └── scale_benchmark.py      # Comprehensive scale benchmark (80/500/1K/10K rules)
├── scripts/
│   └── migrate.py              # One-time migration of existing rules into graph
├── pyproject.toml
├── writ.toml                   # All configuration (overridable via WRIT_ env vars)
└── README.md
```

### 6.2 CLI Commands

| Command | Description |
|---|---|
| `writ serve` | Start the local service. Pre-warms all indexes into memory. Binds to localhost:8765. |
| `writ ingest <path>` | Parse Markdown rules and ingest into the graph. Validates schema. Auto-exports on success. |
| `writ validate` | Run integrity checks: conflicts, orphans, staleness, redundancy. `--review-confidence` lists rules at migration defaults. |
| `writ add` | Interactive rule authoring with relationship suggestion, redundancy detection, and conflict path checking. Auto-exports. |
| `writ edit <rule_id>` | Edit an existing rule with re-validation and relationship re-analysis. Auto-exports. |
| `writ export <path>` | Regenerate Markdown from graph with round-trip fidelity. Writes `.export_timestamp` for staleness tracking. |
| `writ compress` | Cluster rules into abstraction nodes. Evaluates HDBSCAN vs k-means, selects by silhouette score. |
| `writ migrate` | One-time migration of existing rules into the graph. Discovers rules dynamically from `bible/`. |
| `writ query "..."` | CLI rule query for testing retrieval quality. Supports `--domain` and `--budget` flags. |
| `writ status` | Health check: rule count, mandatory count, index status, export staleness. |

---

## 7. HTTP API Interface

Writ runs as a local FastAPI service at localhost:8765. The skill file at `~/.claude/skills` calls this service directly over HTTP. No MCP layer. No additional protocol. Plain async HTTP calls with JSON payloads.

### 7.1 Endpoints

| Method | Endpoint | Parameters | Returns |
|---|---|---|---|
| POST | `/query` | `query: str`, `domain?: str`, `scope?: str`, `budget_tokens?: int`, `exclude_rule_ids?: list[str]`, `loaded_rule_ids?: list[str]` | Ranked list of matching rules. `loaded_rule_ids` and `exclude_rule_ids` union is excluded from results. Response includes mode (summary/standard/full), total_candidates, latency_ms. |
| GET | `/rule/{rule_id}` | `include_graph?: bool` | Full rule node with all fields. Optionally includes 1-hop graph context. Always includes `abstraction_id` and `sibling_rule_ids` (null/empty if not a member). |
| POST | `/conflicts` | `rule_ids: list[str]` | Any CONFLICTS_WITH edges between the provided rule set. Empty list = no conflicts detected. |
| GET | `/abstractions` | none | All abstraction nodes with member counts. |
| GET | `/abstractions/{id}` | none | Full abstraction with member rule details. |
| GET | `/health` | none | Service status, rule count, mandatory count, index state, startup time, export timestamp, export staleness. |

### 7.2 Skill Integration Pattern

The skill file instructs Claude to call Writ at the start of any coding task. The skill uses httpx for async HTTP. The loading sequence has two phases: mandatory rules first (enforcement skeleton), then retrieved rules (domain knowledge). See Section 7.5 for the full mandatory vs. retrieved distinction.

```
# Phase 1: Service check + mode classification
# At session start -- the UserPromptSubmit hook (writ-rag-inject.sh) fires automatically.
GET http://localhost:8765/health
-> Confirms service is running. If down, hooks fall back gracefully.

# Hook prompts AI to set a mode (conversation/debug/review/work).
# Mode determines ceremony: Work mode requires plan + test-skeleton gates.
# Gate enforcement is handled by check-gate-approval.sh (PreToolUse on Write/Edit).
# ENF-* rules are loaded by the skill, not by the retrieval pipeline (Section 7.5).

# Phase 2: Domain rule retrieval (budget = total - mandatory tokens consumed)
POST http://localhost:8765/query
{ "query": "controller contains SQL query", "domain": "Architecture", "budget_tokens": 3200 }
-> Returns ranked domain rules (ARCH-*, DB-SQL-*, etc.). Fills remaining context budget.

# On explicit rule lookup -- triggered by rule ID reference
GET http://localhost:8765/rule/ARCH-ORG-001?include_graph=true
-> Returns full rule with connected rule IDs and edge types.
```

### 7.3 Skill File Location & Fallback

| Condition | Skill Behaviour |
|---|---|
| Service running | Skill calls `/query` with task description. Injects returned rules into context. Fast path. |
| Service not running (pre-Phase 7) | Skill falls back to loading relevant bible/ Markdown files directly into context. Slower, higher token cost, no graph traversal. Skill logs a warning to run: `writ serve` |
| Service not running (post-Phase 7) | Skill falls back to loading bible/ files generated by the most recent `writ export`. These are generated views from the graph, not hand-authored. The fallback is degraded (no graph traversal, no ranking, higher token cost) but uses current rule content. Skill logs a warning to run: `writ serve` |
| Service timeout (> 50ms) | Skill treats as not running and falls back. Timeout value is a tuning parameter (see below). |

> **TIMEOUT RATIONALE:** The 50ms fallback timeout is 5x the p95 latency target (10ms). This is a starting point, not a validated threshold. If the service is under load (e.g., embedding computation during a concurrent ingest), a transient spike above 50ms would trigger unnecessary fallback. The timeout is configurable in `writ.toml` and should be tuned during Phase 5 based on observed p99 latency under concurrent ingest+query load. The tradeoff: too low and you get false fallbacks on transient spikes; too high and a genuinely stuck service blocks the agent.

> **FALLBACK IS DEGRADED, NOT EQUIVALENT:** The fallback path exists so the agent is never blocked, not because context-stuffing is an acceptable long-term mode. At 68 rules, context-stuffing works. At 200+ rules, it will exhaust the context budget. The fallback buys time to start the service -- it does not replace it.

**Export/ingest synchronization:** `writ ingest` automatically runs `writ export` as its final step so fallback files are always current with the graph. There is no manual export step to forget. `writ serve` checks at startup whether the export files are older than the last ingest timestamp and logs a warning if they are stale (safety net for direct DB writes that bypass the CLI).

### 7.4 Sequential Queries & Session State

The production usage pattern is not single-query. An agent mid-slice will query multiple times as it encounters situations the initial rules don't cover. Each subsequent query operates in a context where:

- The context budget has shrunk (earlier rules are already loaded).
- Returning duplicate rules wastes budget.
- The agent needs complementary rules, not the same top results.

**Writ is stateless per request.** The service has no session concept. Session state is managed by the skill (client side), not the server. The `SessionTracker` helper (`writ/retrieval/session.py`) accumulates `loaded_rule_ids` and tracks remaining `budget_tokens` across queries. `loaded_rule_ids` are passed on subsequent `/query` calls and unioned with `exclude_rule_ids` for exclusion. The skill also passes the remaining `budget_tokens` (total budget minus tokens already consumed).

This design keeps the server simple and cacheable. The tradeoff is that the skill must be disciplined about tracking state. If the skill fails to pass `exclude_rule_ids`, the agent gets duplicate rules and wastes context.

**What this does not solve:** The skill can exclude rules it already has, but it cannot tell the server "I need rules that complement what I already have." That requires the server to understand the semantic gap between loaded rules and the current situation. This is the Phase 9 (Agentic Retrieval Loop) problem. Phase 9 is a sketch because it depends on Phase 5 proving that single-query retrieval works first, but it is the actual production usage pattern, not a nice-to-have.

> **INSTALL ONCE:** The skill lives at `~/.claude/skills` -- installed once, active for every project. Run `writ serve` once per machine session. The HTTP service binds to localhost:8765 and all projects share the same live knowledge graph with zero per-project configuration.

### 7.5 Mandatory vs. Retrieved Rule Sets

**Status: Closed. Writ is an enforcement framework with intelligent retrieval, not a retrieval system that serves an enforcement framework.**

The retrieval pipeline (Section 4) is not authoritative for which rules the AI sees. It is authoritative for domain-specific technical rules only. A separate mandatory rule set always loads into agent context regardless of retrieval ranking, query content, or context budget.

**Why this distinction is load-bearing:**

The hooks (`check-gate-approval.sh`, `enforce-final-gate.sh`) enforce mechanically -- `check-gate-approval.sh` delegates to `writ-session.py can-write`, which checks session state (mode, approved gates, file classification). They return JSON `permissionDecision` (deny with `additionalContext`, or ask to force user dialog on repeated violations). An AI that hits a hook denial sees the reason and workflow instructions in the `additionalContext` field. The deny-to-ask escalation ensures repeated violations force human intervention -- the AI physically cannot proceed until the user responds.

If `ENF-GATE-001` is subject to retrieval ranking and the ranking algorithm does not surface it for a given query, the AI proceeds as if no gate exists, writes a file, and gets blocked by the hook. The hook saved the output, but the session is now in a confused state -- the AI will retry, guess, or hallucinate an explanation. That is not enforcement. That is a trap.

**The two rule tiers:**

| Set | Rules | Loading Mechanism | Subject to Retrieval? | Subject to Context Budget? |
|---|---|---|---|---|
| **Mandatory (enforcement skeleton)** | All `ENF-*` rules: routing, gates, pre-checks, post-checks, context discipline, system dynamics, security boundaries, operational claims | Loaded by the skill/hooks at session start. Mode-appropriate subset. | No. Never ranked. Never filtered. Never omitted. | Partially. Summary mode (Section 4.4) may compress mandatory rules to statement + trigger only, omitting rationale. But the rule ID, statement, and trigger are always present. |
| **Retrieved (domain knowledge)** | All `ARCH-*`, `DB-SQL-*`, `FW-M2-*`, `FW-M2-RT-*`, `SEC-UNI-*`, `PERF-*`, `TEST-*`, `PY-*`, `PHP-*` rules | Retrieval pipeline via `/query` endpoint | Yes. Ranked by BM25 + vector + severity + confidence. | Yes. Context budget governs how many domain rules are returned. |

**Mandatory rule loading sequence (performed by hooks, not the service):**

1. `writ-rag-inject.sh` (UserPromptSubmit) fires on every turn, prompting mode classification if not set.
2. AI sets the mode (conversation/debug/review/work). Modes replace the old tier 0-3 system.
3. Mode-appropriate enforcement applies:
   - **Conversation/Debug/Review**: No write gates. Domain rules loaded via `/query`. RAG injection per turn.
   - **Work**: Two-gate enforcement (plan + test-skeletons). `check-gate-approval.sh` blocks writes via JSON `permissionDecision`. Per-file RAG injection via `writ-pretool-rag.sh` and `writ-posttool-rag.sh`.
4. Hooks query `/query` for domain-specific rules relevant to the current file context. These are additive -- they fill the remaining context budget. Each sub-agent worker gets a fresh 8000-token RAG budget.

**The service knows about mandatory rules but does not serve them on `/query`:**

Mandatory rules are stored in the graph like all other rules. They have the same schema, the same node type, the same edges. But they carry a graph property `mandatory: true` that excludes them from the retrieval pipeline's candidate set. They are never BM25-indexed, never embedded, never ranked. The service exposes them via `GET /rule/{rule_id}` for explicit lookup (e.g., the skill fetching `ENF-GATE-003` before Phase C), but `/query` never returns them.

This is not a soft convention. It is a hard filter at Stage 1 (Domain Filter) of the pipeline. Mandatory rules are excluded before BM25 scoring begins. If a mandatory rule appears in `/query` results, that is a bug.

**Why mandatory rules are not just "high severity":**

Setting `severity: Critical` on enforcement rules and trusting the ranking formula to surface them is insufficient. A Critical rule with low BM25 and low vector similarity to the query still ranks below a Medium rule with high textual relevance. Severity is a tiebreaker (weight 0.1), not a guarantee. The only guarantee is exclusion from ranking entirely.

**Context budget interaction:**

Mandatory rules consume context budget before the retrieval pipeline runs. The hooks calculate `remaining_budget = total_budget - mandatory_tokens` and pass `remaining_budget` as `budget_tokens` to `/query`. In the sub-agent architecture, each worker gets a fresh 8000-token budget, so mandatory rules do not compete with domain rules across phase boundaries. Within a single worker, domain rule retrieval operates within whatever budget remains after enforcement rules are loaded.

**Fallback behavior:**

When the service is not running, the skill falls back to loading Markdown files (Section 7.3). Mandatory rules fall back to the same mechanism they use today: the CLAUDE.md navigation table loads enforcement files directly. The fallback path does not change the mandatory/retrieved distinction -- it changes the retrieval mechanism for domain rules (from HTTP query to file loading). The enforcement skeleton is always loaded from files, whether the service is running or not.

> **ARCHITECTURAL INVARIANT:** No change to the retrieval pipeline -- ranking weights, embedding model, BM25 tuning, graph traversal depth -- can cause an enforcement rule to disappear from agent context. The mandatory set is outside the blast radius of retrieval tuning. If you are debugging a session where the AI did not follow a gate, check whether the mandatory rule was loaded, not whether the retrieval pipeline surfaced it.

---

## 8. Implementation Roadmap

Nine original phases executed in strict sequence, followed by the Dwarf in the Glass evolution plan (Phases 1-4) extending Writ from coding rule retrieval to experiential memory with AI rule ingestion. ONNX inference optimization applied post-evolution.

**Phases 1-5** constitute the core architecture. Phase 5 was the thesis gate -- if retrieval quality or latency failed, Phases 6-9 would not have proceeded. **Phases 6-9** were specified in detail only after Phase 5 passed. Full deliverables and acceptance criteria for all phases are in [EXECUTION_PLAN.md](EXECUTION_PLAN.md).

### Phases 1-5 -- Core Architecture (Fully Specified)

| # | Phase | Deliverable | Acceptance Criteria |
|---|---|---|---|
| 1 | Schema & Validation Engine | Pydantic models for all node and edge types. Standalone CLI validator that accepts/rejects rule files against schema. Testable with fixtures, no database required. | All 68 existing rules pass validation. Malformed fixture rules are correctly rejected with actionable error messages. |
| 2 | Infrastructure Setup & Performance Validation | Neo4j running with schema. Tantivy index buildable from rule text. hnswlib vector search operational (Section 3.5). Data access layer with CRUD on Rule nodes and graph edges. | CRUD on Rule nodes passes. Neo4j traversal returns correct 1-hop neighbours on test fixtures. Tantivy BM25 returns expected results on test fixtures. hnswlib cosine search returns expected results on synthetic embeddings. **Neo4j benchmarked at 1K/10K/100K/1M nodes -- latency validated or mitigation identified.** |
| 3 | Migration Script -- 68 Rules | One-time script ingests all 68 existing Markdown rules into the graph. Existing metadata maps directly. New graph-only fields receive the defaults specified in Section 2.3 and are flagged for human review. Cross-references become skeleton RELATED_TO edges. | All 68 rules present in graph with no data loss. Graph-only fields populated per Section 2.3 defaults. Skeleton edges created for all existing cross-references. Script is idempotent. |
| 4 | Post-Commit Validation & Integrity Reporting | Automated integrity checks run after every ingest: contradiction detection (CONFLICTS_WITH), orphan detection (rules unreachable by any query path), staleness flagging (rules past staleness_window), redundancy detection (high-similarity duplicates). Uses Neo4j Cypher + APOC for offline analysis (see Section 3.6). | Known conflict in test fixtures detected. Orphan rule flagged. Stale rule reported. Redundant pair identified. `writ validate` exits non-zero on any failure. |
| 5 | Task-Time Retrieval -- Thesis Gate | Full 5-stage hybrid pipeline operational. FastAPI service at localhost:8765. All indexes pre-warmed at startup. Ranking weights tuned against ground-truth query set (see Section 4.2). Manual query testing via `writ query` CLI command. | `writ query "controller contains SQL"` returns ARCH-ORG-001 as top result. p95 latency < 10ms on warm index. MRR@5 > 0.78 on ground-truth query set (83 queries, automated strict scoring). **Gate passed.** |

### Phases 6-9 -- Post-Validation Extensions (Complete)

All phases completed 2026-03-20. Full deliverables and acceptance criteria in [EXECUTION_PLAN.md](EXECUTION_PLAN.md).

| # | Phase | Deliverable | Status |
|---|---|---|---|
| 6 | Authoring Tools | `writ add`/`edit` with relationship suggestion, conflict detection, redundancy warnings. Graph-neighbor scoring boost (w_graph=0.01). CONTRIBUTING.md governance. 30 tests. | **Complete** |
| 7 | Generated Artifacts | `writ export` with round-trip fidelity (export -> ingest -> export = equivalent). Auto-export after ingest/add/edit. Staleness detection. `/health` extended. 31 tests. | **Complete** |
| 8 | Compression Layer | HDBSCAN + k-means evaluation via `writ compress`. Abstraction nodes with centroid-nearest summaries (no LLM). Summary mode returns abstractions. `/abstractions` endpoints. 31 tests. | **Complete** |
| 9 | Agentic Retrieval Loop | Client-side `SessionTracker` (next_query, load_results). `loaded_rule_ids` exclusion on `/query`. Abstraction membership on `/rule/{rule_id}`. 3-query session simulation. 22 tests. | **Complete** |

> **THESIS VALIDATED:** Phase 5 gate passed. MRR@5 = 0.7842 (> 0.78 threshold), hit rate = 97.59%, p95 latency = 6.7ms (< 10ms budget). Phases 6-9 proceeded and completed successfully. Current: ~320 tests across 30 test files + 12 benchmarks, all passing. V2 hardening and v3 sub-agent isolation added ~140 tests for enforcement layer and session management.

---

## 9. Open Questions & Decision Gates

These questions must be answered before the relevant phase begins. They are not deferred indefinitely -- each has a phase gate that forces resolution.

| Question | Decision Gate | Resolution Path | Status |
|---|---|---|---|
| Neo4j traversal performance: does it meet < 3ms at 1K/10K/100K/1M nodes? | Phase 2 | Live queries exceed budget (1K 1-hop p95=3.7ms, 10K p95=7.8ms). Mitigation: pre-computed adjacency cache in memory. Cache lookup = 0.002ms. | **Resolved** |
| Vector search engine. | -- | hnswlib for all phases. Qdrant is the defined migration target at 10K+ rules (Section 3.5). VectorStore Protocol in place. | **Closed** |
| Which embedding model (MiniLM vs mpnet) produces better retrieval precision on rule text? | Phase 5 | MiniLM selected. MRR@5 = 0.7842, hit rate = 97.59% on 83-query set. mpnet reserved as upgrade path. | **Resolved** |
| Ranking formula weights (w1-w5): what values maximize MRR@5? | Phase 5-6 | Final: 0.198/0.594/0.099/0.099/0.01 after two tuning rounds + Phase 6 graph proximity addition. | **Resolved** |
| Graph-level versioning: how do agents reference a stable graph version during long-running sessions? | -- | Deferred. Not needed at 80-rule corpus. Session tracker + loaded_rule_ids exclusion sufficient. Revisit at 500+ rules. | **Deferred** |
| Rule-level versioning: when a rule is edited (not replaced), what happens to the old version? | -- | Deferred. Export regenerates from current graph state. Governance concern, not retrieval. | **Deferred** |
| What clustering algorithm produces the most coherent abstraction nodes for rule compression? | Phase 8 | Both HDBSCAN and k-means implemented and evaluated via `evaluate_both()`. HDBSCAN preferred (auto-discovers cluster count). Selection at runtime by silhouette score. | **Resolved** |
| Multi-author conflict resolution: when two contributors add conflicting rules, what is the governance process? | Phase 6 | CONTRIBUTING.md: PR review required, CONFLICTS_WITH requires human resolution by domain owner, deprecation via SUPERSEDES. | **Resolved** |
| Agent integration protocol: REST API (localhost:8765). | -- | Skill calls FastAPI service directly over HTTP. No MCP layer. No SDK. Plain async HTTP with httpx. | **Closed** |
| Is the retrieval service authoritative or advisory for enforcement rules? | -- | Advisory. Enforcement rules (`ENF-*`) are mandatory -- always loaded by the skill, never subject to retrieval ranking. Retrieval is authoritative for domain rules only. See Section 7.5. | **Closed** |

---

## 10. Performance Targets & Measurement

These are contractual targets, not aspirational ones. If any target is missed in Phase 5 testing, the pipeline must be re-architected before proceeding to Phases 6-9.

| Metric | Target | Actual (80 rules) | Actual (10K rules) | Status |
|---|---|---|---|---|
| End-to-end query latency (warm index) | p95 < 10ms | 6.7ms | 8.0ms | **Pass** |
| Service cold start (`writ serve`) | < 3 seconds | 0.31-0.40s | 22.0s | **Pass at 80; exceeds at 10K** |
| Retrieval precision (MRR@5) | > 0.78 | 0.7842 (17/19 hits) | -- | **Pass** |
| Hit rate (all queries) | > 90% | 97.59% (81/83) | -- | **Pass** |
| Memory footprint (warm service) | < 2 GB RAM | 1,075 MB | 1,469 MB | **Pass** |
| Integrity check duration (80 rules) | < 500ms | 3.5ms median, 38.8ms p95 | -- | **Pass** |
| Rule ingestion (single rule) | < 2 seconds | 0.008s median, 0.012s p95 | -- | **Pass** |
| Context reduction | > 1x | 4.4x (80 rules) | 726x (10K rules) | **Pass** |

> **BENCHMARK REFERENCE:** Phase 5 baseline in `PHASE5_RESULTS.md`. Scale benchmark in `SCALE_BENCHMARK_RESULTS.md`. Evolution plan in `EVOLUTION_PLAN.md`. ONNX optimization reduces E2E p95 from 6.6ms to 0.19ms at 80 rules. Cold start at 10K (70s) is a known tradeoff of ONNX bulk encoding -- Qdrant with persistent vectors is the defined mitigation.

---

## 11. Technology Decision Rationale

This section explains why each technology was chosen. Read this if you want to understand the tradeoffs, not just the choices. Each decision was driven by the same principle: minimize operational complexity and latency at the corpus sizes Writ actually operates at (80-10K rules), while preserving a migration path for larger scales.

### 11.1 Why Neo4j over PostgreSQL

**The question:** Rules have typed relationships (DEPENDS_ON, CONFLICTS_WITH, SUPPLEMENTS). Should those relationships live in a relational database with foreign keys, or a graph database with native edges?

**Why not PostgreSQL:**

PostgreSQL can model relationships with join tables. For simple 1-hop lookups ("what rules does ARCH-ORG-001 depend on?"), a SQL JOIN is fine. But Writ's retrieval pipeline needs multi-hop traversal -- "starting from the top-3 ranked rules, find all rules within 2 hops connected by DEPENDS_ON, CONFLICTS_WITH, or SUPPLEMENTS edges." In PostgreSQL, that is a recursive CTE or multiple self-joins. The query planner treats each hop as a new table scan.

PostgreSQL also has the AGE extension, which adds Cypher query support. But AGE runs Cypher through PostgreSQL's query planner, adding translation overhead. At TB-scale graph traversal (the long-term target for 6 teams, 1TB corpus), that overhead becomes unacceptable.

**Why Neo4j:**

Neo4j is a native graph engine. "Native" means the storage engine is optimized for pointer-chasing along edges -- it does not translate graph queries into relational operations. A 2-hop traversal in Neo4j follows physical pointers between nodes in memory; the same operation in PostgreSQL requires hash joins across tables.

| Operation | PostgreSQL | Neo4j |
|---|---|---|
| 1-hop neighbor lookup | JOIN on FK, index scan | Pointer chase, O(1) per edge |
| 2-hop traversal | Recursive CTE or self-join x2 | `MATCH (a)-[*1..2]-(b)` -- native |
| Variable-depth path | Recursive CTE (hard to optimize) | `MATCH path = shortestPath(...)` -- built-in |
| Schema flexibility | ALTER TABLE for new edge types | New relationship type = new label, no migration |
| ACID transactions | Yes | Yes |
| Full-text search | Yes (tsvector) | Yes (Lucene-based, used as fallback) |
| Operational complexity | Standard -- most teams run Postgres already | Separate service, JVM-based, needs its own memory allocation |

**The tradeoff:** Neo4j adds operational complexity (a separate JVM service). PostgreSQL would be simpler to deploy for teams that already run Postgres. The decision was driven by the hot-path requirement: Stage 4 of the retrieval pipeline must complete in < 3ms including graph traversal. At 1K nodes, even Neo4j's live Cypher queries exceeded this budget (p95 = 6.4ms) due to network round-trip. The mitigation was pre-computed adjacency lists cached in memory (0.002ms). But the offline integrity checks (orphan detection, contradiction analysis, shortest-path calculations) still benefit from Neo4j's native Cypher and APOC graph algorithms, which would require significantly more code to replicate in SQL.

**When PostgreSQL would be the right choice:** If Writ only needed 1-hop lookups and the corpus stayed under 1K rules, PostgreSQL with a simple join table would be simpler and sufficient. The graph database becomes load-bearing when you need variable-depth traversal, path algorithms, and a query language designed for graph patterns.

### 11.2 Why Tantivy over Elasticsearch

**The question:** Stage 2 of the pipeline needs BM25 keyword search over rule text (trigger, statement, tags fields). Should that be a full search cluster or an embedded library?

**What is BM25:** BM25 (Best Matching 25) is a ranking function used by search engines. Given a query like "controller SQL query", BM25 scores each document by how well the query terms match, accounting for term frequency (how often the term appears in the document), inverse document frequency (how rare the term is across all documents), and document length normalization. It is the standard algorithm behind Elasticsearch, Solr, and Lucene.

**Why not Elasticsearch:**

Elasticsearch is the industry standard for full-text search. It runs on the JVM, operates as a separate cluster, and is designed for billions of documents across distributed nodes. It is also operationally heavy:

- **JVM overhead:** 2-4 GB minimum heap. Garbage collection pauses cause unpredictable tail latency.
- **Cluster management:** Even a single-node Elasticsearch deployment requires health monitoring, shard management, and index lifecycle policies.
- **Reindexing speed:** On a Magento production system, Elasticsearch reindexing 50K product documents takes minutes. At 1M documents, it can take hours. Writ needs to rebuild indexes at ingestion time without blocking queries.
- **Cold start:** Starting Elasticsearch takes 10-30 seconds. Writ's target is < 3 seconds for the entire service.

**Why Tantivy:**

Tantivy is Apache Lucene rewritten in Rust. It provides the same BM25 scoring algorithm without the JVM, without a separate service, and without cluster management. It embeds directly into the Python process via `tantivy-py` bindings.

| Property | Tantivy | Elasticsearch |
|---|---|---|
| Runtime | Rust (no GC pauses) | Java (JVM, garbage collection) |
| Deployment | Library -- embeds in process | Separate service + optional cluster |
| Index rebuild (1K docs) | Milliseconds | Seconds |
| Index rebuild (1M docs) | Seconds | Minutes to hours |
| BM25 scoring | Native (same algorithm) | Native (same algorithm) |
| Memory footprint | ~50-100 MB for 1M docs | 2-4 GB JVM heap minimum |
| Scale ceiling | Millions of documents, single process | Billions, distributed sharding |
| Operational cost | Zero -- no service to manage | Monitoring, shard management, JVM tuning |

**Measured performance:** At 80 rules, Tantivy BM25 search completes in 0.136ms (p95). At 10K rules, 0.518ms (p95). Both are well under the 2ms Stage 2 budget.

**When Elasticsearch would be the right choice:** If the corpus exceeds millions of documents, or if you need features like fuzzy matching, custom analyzers, or distributed search across multiple machines. Writ's rule corpus is text-light (even 10K rules is ~2 GB of text) -- well within Tantivy's single-process ceiling.

### 11.3 Why hnswlib before Qdrant

**The question:** Stage 3 needs approximate nearest neighbor (ANN) vector search over rule embeddings. Should that be a dedicated vector database or an in-process library?

**What is ANN vector search:** Each rule is converted into a numerical vector (an embedding) that captures its semantic meaning. "Controller contains SQL" and "direct database access in request handler" produce similar vectors even though they share few keywords. ANN search finds the vectors closest to the query vector -- "approximate" because it trades a small amount of accuracy for dramatically faster search (exact nearest neighbor search is O(n); HNSW is O(log n)).

**What is HNSW:** Hierarchical Navigable Small World is the algorithm both hnswlib and Qdrant use internally. It builds a multi-layered graph of vectors where each layer has fewer nodes. Search starts at the top (sparse) layer and navigates down to the bottom (dense) layer, narrowing the search space at each level. Think of it as a skip list but for high-dimensional space.

**Why hnswlib first:**

hnswlib is a C++ HNSW implementation with Python bindings. It runs entirely in-process -- no network calls, no service to manage, no connection pool. At the corpus sizes Writ validated against (80-10K rules), it is faster than any networked alternative:

| Property | hnswlib | Qdrant |
|---|---|---|
| Deployment | In-process C++ library | Separate Rust service |
| Latency (1K vectors) | 0.04ms (p95) | ~0.5-1ms (network round-trip) |
| Latency (10K vectors) | 0.205ms (p95) | ~0.5-1ms (network round-trip) |
| Operational cost | Zero | Service lifecycle, health checks |
| Memory | Grows linearly with vectors, no quantization | Scalar/binary/product quantization (4x-64x reduction) |
| Filtered search | Manual post-filter in Python | Native filterable HNSW (filter during search) |
| Scale ceiling | Single process, ~1M vectors practical | Billions, distributed, sharded |
| Persistence | In-memory only (rebuild from embeddings) | Persistent storage with WAL |

**Why Qdrant later:**

Qdrant becomes necessary when any of these triggers fire:
- Corpus exceeds 100K rules (hnswlib memory grows linearly, no quantization)
- Filtered vector search becomes a requirement (domain-scoped ANN without a separate domain filter stage)
- p99 vector search latency exceeds 5ms
- Memory for the HNSW index competes with other service needs

**The swap is a single-file change.** `writ/retrieval/embeddings.py` implements a VectorStore Protocol. The hnswlib backend builds and queries an in-process index. A Qdrant backend would call the Qdrant REST/gRPC API. The pipeline, ranking, and all other stages are unchanged. This is the Protocol over ABC pattern (see `PY-PROTO-001` in the Coding Bible).

**Why not Qdrant from the start:** Adding a separate service before you need it adds operational complexity, deployment steps, and failure modes -- all for zero benefit at 80 rules. The principle: use the simplest tool that meets current requirements, with a clean abstraction boundary for when requirements change.

### 11.4 Why FastAPI over Flask/Django

**The question:** The HTTP service needs to serve JSON responses with sub-10ms latency. Which web framework?

**Why FastAPI:**

- **Async native:** FastAPI is built on Starlette (ASGI). All endpoints are `async def` by default. The Neo4j driver, httpx, and the pipeline are all async. Flask would require threading or gevent for concurrent requests; Django would require ASGI adapter configuration.
- **Pydantic integration:** Request and response bodies are validated through Pydantic models automatically. Writ already uses Pydantic for rule schema validation (Phase 1), so the models are shared between ingestion and API layers.
- **Startup speed:** uvicorn (the ASGI server) starts in < 500ms. Django's startup involves ORM initialization, middleware chain setup, and URL resolution -- overkill for a single-purpose service.
- **No ORM needed:** Writ talks to Neo4j via the bolt protocol driver, not through an ORM. Django's main advantage (the ORM + admin) provides no value here.

| Property | FastAPI | Flask | Django |
|---|---|---|---|
| Async support | Native (ASGI) | Bolt-on (threading/gevent) | Bolt-on (ASGI adapter) |
| Request validation | Pydantic (built-in) | Manual or marshmallow | Django REST Framework |
| Startup time | < 500ms | < 500ms | 1-3 seconds |
| ORM | None (not needed) | None (not needed) | Built-in (not needed) |
| OpenAPI docs | Auto-generated | Manual | django-rest-framework |

### 11.5 Why Local Inference (ONNX Runtime) over Cloud Embeddings

**The question:** Rule text needs to be converted to vectors for semantic search. Should that use a local model or a cloud API?

**Why local:**

- **Zero latency variance:** ONNX Runtime on CPU takes ~2ms per text (cold), sub-microsecond (cached). An API call takes 50-200ms depending on network.
- **Zero cost:** No per-token API charges.
- **Fully offline:** Writ runs on localhost with no cloud dependencies.
- **Privacy:** Rule text may contain proprietary coding standards.

**Why ONNX Runtime over PyTorch (sentence-transformers):**

- **15-29x faster query latency:** ONNX Runtime + LRU cache reduces E2E p95 from 6.6ms (PyTorch) to 0.19ms at 80 rules. The embedding encode step was 92% of the hot path under PyTorch; it's now invisible (cache hits).
- **No PyTorch at runtime:** Eliminates torch, transformers, sentence-transformers from the running process. Runtime memory drops from ~1.1GB to ~700MB.
- **Ranking-identical:** 0/83 ground-truth queries produce different top-5 results between ONNX and PyTorch backends.
- **Tradeoff at scale:** ONNX bulk encoding (startup, 10K rules) is 3-5x slower than sentence-transformers' C++ batching. Cold start at 10K rules is 70s (ONNX) vs 20s (PyTorch). At that scale, pre-computed embeddings via Qdrant is the correct architecture.

**Model choice:** `all-MiniLM-L6-v2` (384 dimensions, 86MB ONNX). Exported via HuggingFace optimum with fused-attention optimization. MRR@5 = 0.7842 on the 83-query ground-truth set. sentence-transformers remains a dev/build dependency for the export script (`scripts/export_onnx.py`).

### 11.6 Why Pre-Computed Adjacency Cache over Live Graph Queries

**The question:** Stage 4 of the pipeline needs 1-2 hop graph traversal from the top-K ranked rules. Should that query Neo4j at request time or read from a pre-computed cache?

**What happened:** Phase 2 benchmarks showed Neo4j live Cypher queries exceeded the 3ms Stage 4 budget:

| Scale | 1-hop p95 | 2-hop p95 |
|---|---|---|
| 1K nodes | 6.4ms | 9.3ms |
| 10K nodes | 9.7ms | 11.6ms |

The bottleneck is network round-trip over the bolt protocol, not query execution inside Neo4j. Even with connection pooling, each query pays ~3-5ms of network overhead.

**The solution:** At service startup (`writ serve`), Writ reads all edges from Neo4j once and builds an in-memory adjacency dictionary. Stage 4 traversal becomes a Python dict lookup:

| Scale | Cache build time | 1-hop lookup | 2-hop lookup |
|---|---|---|---|
| 80 rules | < 50ms | 0.002ms | 0.002ms |
| 10K rules | < 500ms | 0.003ms | 0.004ms |

**The tradeoff:** The cache is stale if rules are added while the service is running. Since Writ is a local development tool (not a multi-tenant production service), the cache is rebuilt on `writ ingest` or by restarting the service. This is acceptable because rule ingestion is an infrequent operation, not a hot path.

**Neo4j is still used for:** Offline integrity checks (`writ validate`), rule CRUD, and any operation that does not have a sub-millisecond latency requirement.

### 11.7 Technology Decision Summary

| Component | Chosen | Alternative | Why Not Alternative | Migration Trigger |
|---|---|---|---|---|
| Graph database | Neo4j | PostgreSQL + AGE | Multi-hop traversal, native Cypher, APOC algorithms | -- (committed) |
| Keyword search | Tantivy (Rust) | Elasticsearch | JVM overhead, cluster management, slow reindex | Corpus > millions of docs |
| Vector search | hnswlib (C++) | Qdrant (Rust) | Zero operational cost at current scale | Corpus > 10K rules or filtered ANN needed |
| Web framework | FastAPI (Python) | Flask, Django | Async native, Pydantic built-in, no ORM needed | -- (committed) |
| Embeddings (runtime) | ONNX Runtime + Rust tokenizers | sentence-transformers (PyTorch) | 15-29x faster queries, no PyTorch at runtime, ~700MB vs ~1.1GB | -- (committed) |
| Embeddings (export) | optimum (HuggingFace) | torch.onnx.export | Fused attention, 30%+ faster than naive export | -- (committed) |
| Hot-path traversal | Pre-computed adjacency cache | Live Neo4j queries | Network round-trip exceeds 3ms budget | -- (committed) |
| Clustering | HDBSCAN + k-means (evaluated) | LLM-generated summaries | Deterministic, no API cost, reproducible | -- (committed) |

---

## 12. Glossary

Technical terms used throughout this handbook, ordered from foundational concepts to Writ-specific terminology.

### Search & Retrieval

| Term | Definition |
|---|---|
| **RAG** | Retrieval-Augmented Generation. A pattern where an AI model retrieves relevant documents from a knowledge base before generating a response, rather than relying solely on its training data. Writ is the retrieval half of this pattern -- it finds the right rules so the AI can apply them. |
| **BM25** | Best Matching 25. A ranking function that scores documents by how well query terms match, accounting for term frequency (how often a word appears in the document), inverse document frequency (how rare the word is across all documents), and document length. It is the algorithm behind most search engines. Tantivy implements BM25 for Writ's Stage 2 keyword search. |
| **TF-IDF** | Term Frequency-Inverse Document Frequency. The predecessor to BM25. Scores documents by term frequency divided by how common the term is globally. BM25 improves on TF-IDF by adding document length normalization and a saturation function (diminishing returns for repeated terms). |
| **Full-text search** | Searching document content by words and phrases, as opposed to searching structured fields (like a database WHERE clause). Tantivy and Elasticsearch are full-text search engines. |
| **Inverted index** | A data structure that maps each word to the list of documents containing it. When you search "controller SQL", the engine looks up "controller" and "SQL" in the index and intersects the document lists. This is how BM25 search achieves sub-millisecond latency -- it never scans every document. |

### Vector Search & Embeddings

| Term | Definition |
|---|---|
| **Embedding** | A numerical vector (array of floating-point numbers) that represents the semantic meaning of text. "Controller contains SQL" and "direct database call in handler" produce similar embeddings even though they share few words. Generated by a neural network (sentence-transformers in Writ's case). |
| **Embedding model** | The neural network that converts text to embeddings. Writ uses `all-MiniLM-L6-v2` (384-dimensional vectors, 80MB model). Larger models like `all-mpnet-base-v2` (768 dimensions) produce higher-quality embeddings but are slower and use more memory. |
| **Dimensions** | The number of floating-point values in an embedding vector. MiniLM produces 384-dimensional vectors. Each dimension captures some aspect of the text's meaning. More dimensions = more expressive but more memory and slower comparison. |
| **Cosine similarity** | A measure of how similar two vectors are, computed as the cosine of the angle between them. Value ranges from -1 (opposite) to 1 (identical). Writ uses cosine similarity for vector search (finding similar rules) and redundancy detection (flagging near-duplicate rules at 0.95 threshold). |
| **ANN (Approximate Nearest Neighbor)** | Finding the vectors closest to a query vector without comparing against every vector in the dataset. Exact nearest neighbor is O(n); ANN algorithms like HNSW achieve O(log n) by trading a small amount of accuracy for dramatically faster search. |
| **HNSW** | Hierarchical Navigable Small World. The algorithm used by hnswlib and Qdrant. Builds a multi-layered graph where each layer has progressively fewer nodes. Search starts at the top (sparse) layer and navigates down, narrowing candidates at each level. Parameters: `M` (connections per node), `ef_construction` (build quality), `ef_search` (query accuracy vs speed). |
| **VectorStore Protocol** | Writ's abstraction layer for vector search (`writ/retrieval/embeddings.py`). Defines a `search(vector, k, filters)` interface that hnswlib implements today and Qdrant would implement in the future. Uses Python's Protocol type (structural subtyping) rather than abstract base classes. |
| **Quantization** | Reducing the memory footprint of vectors by lowering numerical precision. Scalar quantization (float32 to int8) saves 4x memory. Binary quantization saves 32x. Product quantization saves 64x. hnswlib does not support quantization; Qdrant does. This is one trigger for the migration. |

### Graph Database

| Term | Definition |
|---|---|
| **Graph database** | A database where data is stored as nodes (entities) and edges (relationships between entities). Unlike relational databases where relationships are expressed through foreign keys and JOINs, graph databases store relationships as first-class citizens with their own properties. |
| **Node** | An entity in the graph. In Writ: Rule, Abstraction, Domain, Evidence, Tag. Each node has typed properties (like a row in a relational table, but schema-flexible). |
| **Edge (Relationship)** | A typed, directed connection between two nodes. In Writ: DEPENDS_ON, CONFLICTS_WITH, SUPPLEMENTS, SUPERSEDES, RELATED_TO, etc. Edges can have properties (like weight or metadata). |
| **Traversal** | Following edges from one node to its neighbors. "1-hop" means direct neighbors. "2-hop" means neighbors of neighbors. Writ's Stage 4 traverses 1-2 hops from top-ranked rules to find related rules that keyword and vector search might miss. |
| **Cypher** | Neo4j's query language for graph patterns. Example: `MATCH (a:Rule)-[:DEPENDS_ON]->(b:Rule) WHERE a.rule_id = 'ARCH-ORG-001' RETURN b` finds all rules that ARCH-ORG-001 depends on. Analogous to SQL for relational databases. |
| **Bolt protocol** | Neo4j's binary communication protocol. The Python driver (`neo4j` package) connects to Neo4j over bolt (default port 7687). Faster than HTTP for query execution but still incurs network round-trip latency. |
| **APOC** | A Neo4j plugin library ("Awesome Procedures on Cypher") that adds graph algorithms (shortest path, community detection, PageRank) and utility functions. Writ uses APOC for offline integrity analysis in `writ validate`. |
| **Adjacency list** | A data structure where each node maps to its list of neighbors. Writ pre-computes adjacency lists from Neo4j at startup and stores them in a Python dictionary, so Stage 4 traversal is a dict lookup (0.002ms) instead of a network query (6ms+). |

### Retrieval Pipeline

| Term | Definition |
|---|---|
| **Hybrid retrieval** | Combining multiple retrieval methods (keyword search + vector search + graph traversal) and fusing their results. No single method is sufficient: BM25 misses semantic matches ("SQL" vs "database query"), vectors miss exact keyword matches, and neither understands rule relationships. |
| **Pipeline stage** | One step in the retrieval process. Writ has 5 stages that execute in sequence, each narrowing the candidate set: Domain Filter -> BM25 -> Vector -> Graph Traversal -> Ranking. |
| **RRF (Reciprocal Rank Fusion)** | A method for combining ranked lists from different retrieval systems. Each candidate's score is `1 / (k + rank)` where k is a constant (typically 60). RRF is rank-based, not score-based, so it works even when BM25 scores and vector distances are on different scales. Writ uses weighted RRF in its ranking formula. |
| **MRR@5 (Mean Reciprocal Rank at 5)** | A retrieval quality metric. For each query, if the correct answer appears at rank 1, score = 1.0; rank 2 = 0.5; rank 3 = 0.33; rank 4 = 0.25; rank 5 = 0.2; not in top 5 = 0.0. MRR@5 is the average across all queries. Writ's threshold is 0.78. |
| **Hit rate** | The percentage of queries where the correct answer appears anywhere in the top-K results. Writ's hit rate is 97.59% (81 of 83 queries return the expected rule in top 5). |
| **Context budget** | The maximum number of tokens the retrieval system can return. The AI agent has a limited context window; rules must fit within it alongside the code being reviewed. Writ has three modes: summary (< 2K tokens), standard (2K-8K), and full (> 8K). |
| **Ground-truth query set** | A set of human-authored queries with known correct answers, used to measure retrieval quality. Writ's set has 83 queries (19 ambiguous, 64 keyword/symptom) stored in `tests/fixtures/ground_truth_queries.json`. |
| **Pre-warming** | Loading indexes into memory before serving queries. At `writ serve` startup, Tantivy indexes, hnswlib vectors, and the adjacency cache are all built and loaded so the first query pays no cold-start penalty. |

### Clustering & Compression

| Term | Definition |
|---|---|
| **Clustering** | Grouping similar items together based on their vector representations. Writ clusters rules into groups so each group can be summarized by a single abstraction node. |
| **HDBSCAN** | Hierarchical Density-Based Spatial Clustering of Applications with Noise. A clustering algorithm that automatically discovers the number of clusters based on density. Unlike k-means, it does not require you to specify k (the number of clusters) in advance. It also identifies noise points (rules that do not fit any cluster). Writ dissolves singleton clusters to an "ungrouped" set. |
| **k-means** | A clustering algorithm that partitions data into exactly k groups by iteratively assigning each point to the nearest centroid and recomputing centroids. Requires specifying k in advance. Simpler and faster than HDBSCAN but less flexible. |
| **Silhouette score** | A measure of clustering quality from -1 to 1. For each point, it compares how close the point is to its own cluster versus the nearest other cluster. Higher = better separation between clusters. Writ evaluates both HDBSCAN and k-means and selects the algorithm with the higher silhouette score. |
| **Abstraction node** | A graph node that summarizes a cluster of rules. The summary is the statement of the rule nearest to the cluster centroid (no LLM generation). When context budget is tight, the pipeline returns abstraction summaries instead of individual rules. The agent can then drill down into specific rules if needed. |
| **Centroid** | The geometric center of a cluster in vector space (the average of all member vectors). The rule whose embedding is closest to the centroid is the most representative rule in the cluster; its statement becomes the abstraction summary. |
| **Compression ratio** | The ratio of total member text tokens to summary tokens. A compression ratio of 5.6x means the abstraction summary is 5.6 times shorter than reading all member rules individually. At 10K rules, Writ achieves 24.8x compression. |

### Infrastructure & Operations

| Term | Definition |
|---|---|
| **ASGI** | Asynchronous Server Gateway Interface. The Python standard for async web servers. FastAPI runs on ASGI via uvicorn. The synchronous predecessor is WSGI (used by Flask in sync mode). |
| **uvicorn** | A fast ASGI server written in Python with optional Cython/uvloop acceleration. Starts in < 500ms. Serves Writ's FastAPI application. |
| **Pydantic** | A Python library for data validation using type annotations. Writ uses Pydantic models for rule schema validation (rejecting malformed rules at ingestion) and for API request/response typing (FastAPI integration). |
| **Protocol (Python)** | A Python typing construct for structural subtyping ("duck typing with type checking"). If a class has the right methods, it satisfies the Protocol -- no inheritance required. Writ uses Protocol for the VectorStore abstraction: hnswlib and a future Qdrant backend both satisfy the same Protocol without sharing a base class. |
| **p95 / p99 latency** | The 95th or 99th percentile of response times. "p95 = 6.7ms" means 95% of queries complete in 6.7ms or less. Percentile metrics are more useful than averages because they capture tail latency -- the slow outliers that users actually experience. |
| **Cold start** | The time from `writ serve` to the first query being servable. Includes loading the embedding model, building Tantivy indexes, building hnswlib indexes, and populating the adjacency cache. At 80 rules: 0.3s. At 10K rules: 22s (dominated by embedding computation). |
| **Round-trip fidelity** | The property that exporting rules from the graph to Markdown and re-ingesting that Markdown produces the same graph state. Writ validates this: export -> ingest -> export produces structurally equivalent output. This guarantees the fallback path (loading Markdown files when the service is down) does not lose information. |
| **Staleness** | A rule whose `last_validated` date is older than its `staleness_window` (default 365 days). Stale rules are flagged by `writ validate` and reported in `/health`. Staleness is a governance signal, not a retrieval penalty -- stale rules still rank normally but are surfaced for human review. |

### Writ-Specific Concepts

| Term | Definition |
|---|---|
| **Mandatory rule** | A rule with `mandatory: true` in the graph (all `ENF-*` rules). Mandatory rules are loaded by the skill unconditionally at session start. They are never indexed, never embedded, never ranked, and never returned by `/query`. They exist outside the retrieval pipeline's blast radius. |
| **Domain rule** | A rule with `mandatory: false` (all non-ENF rules). Domain rules are indexed, embedded, ranked, and returned by the retrieval pipeline based on query relevance and context budget. |
| **Skill** | A Claude Code extension that provides specialized capabilities. The Phaselock skill uses Writ to retrieve coding rules relevant to the current task. The skill calls `writ serve` at localhost:8765 and injects retrieved rules into the AI's context. |
| **SessionTracker** | A client-side helper (`writ/retrieval/session.py`) that accumulates loaded rule IDs across sequential queries within a single coding session. Prevents duplicate rules from being returned and tracks remaining context budget. The server is stateless; all session state lives in the client. |
| **Thesis gate** | Phase 5 of the implementation. If retrieval quality (MRR@5) or latency (p95) failed their targets, Phases 6-9 would not proceed. The gate passed: MRR@5 = 0.7842 (> 0.78), p95 = 6.7ms (< 10ms). |
| **Context-stuffing** | Loading all rules into the AI's context window at once, without retrieval. Works at 50 rules (~15K tokens). Fails at 500+ rules (context window exhausted). Writ replaces context-stuffing with intelligent retrieval. |
| **Complement mode** | A future feature for multi-query sessions. Given rules already loaded, find rules that fill semantic gaps -- "what rules would complement what I already have?" Requires corpus of 500+ rules where domain clusters are separable. Not viable at 80 rules where embeddings cluster too tightly. |
| **Abstraction membership** | A rule's association with an abstraction node. The `/rule/{rule_id}` endpoint returns `abstraction_id` (which cluster the rule belongs to) and `sibling_rule_ids` (other rules in the same cluster). Null if the rule is ungrouped. |
| **Export staleness** | The condition where the Markdown files in `bible/` are older than the last graph modification. Detected by comparing `.export_timestamp` against the service startup time. Reported in `/health` as `export_stale: true`. |

---

## 13. Future Work

All 9 original phases and evolution Phases 1-4 complete. ONNX inference optimization applied.

| Item | Trigger | Description |
|---|---|---|
| Phase 5: Domain Generalization | Concrete non-coding domain | Conditional on generalizability test. Requires automated non-absence triggers, sufficient frequency, low noise, short latency, no expert dependency. Five defined failure modes. See EVOLUTION_PLAN.md. |
| Qdrant migration | 10K+ rules | Swap hnswlib for Qdrant. VectorStore Protocol already in place. Eliminates the 10K cold start problem by persisting embeddings -- no bulk encode at startup. Also needed for filtered ANN and memory pressure relief. |
| Complement mode | 500+ rules | Semantic gap detection for multi-query sessions. At 80 rules, embeddings cluster too tightly for meaningful gap detection. |
| Graph-level versioning | 500+ rules, long sessions | Immutable graph snapshots pinned at session start. Not needed at current scale. |
| Authority preference re-measurement | 10+ AI-provisional rules in query results | Re-derive preference threshold with mixed-authority candidate set. Current threshold (0.0749) derived from homogeneous human corpus. |
| mpnet upgrade | MRR@5 regression | Switch embedding model from MiniLM (384-dim) to mpnet (768-dim). Would require re-exporting ONNX model. |
