# WRIT -- RAG Architecture Handbook

**Hybrid Knowledge Retrieval System**
**v1.0 -- March 2026**

| | |
|---|---|
| **Author** | Lucio Saldivar (Infinri) |
| **Project** | Writ / AI Workflow Coding Bible |
| **Status** | Architecture Spec -- Pre-Implementation |
| **Classification** | Internal -- Confidential |

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

**Status: hnswlib for Phases 1-5. Qdrant is the defined migration target when the corpus outgrows in-process search.**

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

> **LATENCY BUDGET STATUS:** Pre-implementation benchmarks (`benchmarks/run_benchmarks.py`) confirm all measurable stages pass their budgets by 1-2 orders of magnitude on synthetic 1,000-rule data. The infrastructure layer is not the bottleneck. However, these benchmarks measure latency only -- not retrieval quality. Stage 4 was benchmarked on NetworkX (in-memory, zero serialization), which validates the adjacency list mitigation path but does not represent Neo4j performance. The Phase 2 Neo4j performance benchmark remains required. Budgets should not be relaxed until Phase 5 provides both latency and retrieval quality data on real rules.

**Stage 4 failure mitigation:** Pre-compute adjacency lists into memory at startup so traversal becomes a hash lookup rather than a live graph query. Trades memory for latency. Pre-implementation benchmark confirms in-memory 2-hop traversal at 0.05ms (p95) on 1,000 nodes / ~4,000 edges -- this path is validated.

**Stage 3 failure mitigation:** Reduce HNSW `ef_search` parameter to trade recall for speed. This directly conflicts with the MRR@5 > 0.85 target -- lowering `ef_search` means the vector stage returns less relevant candidates. Pre-implementation benchmarks show latency is not the constraint (0.04-0.12ms across all ef_search values), so the Phase 5 decision is purely about recall quality, not a latency/recall tradeoff. If recall is insufficient at any ef_search, the fallback is to upgrade to `all-mpnet-base-v2` (higher quality embeddings). The Phase 5 evaluation must test at least three `ef_search` values and report both latency and MRR@5 for each, so the tradeoff curve is visible before a value is locked.

### 4.2 Ranking Formula

The final score for each candidate rule is a weighted combination of retrieval signal and rule metadata:

```
score = (w1 * bm25_rank) + (w2 * vector_rank) + (w3 * severity_weight) + (w4 * confidence_weight)
```

| Weight Component | Starting Point | Values | Notes |
|---|---|---|---|
| BM25 keyword rank (`w1`) | 0.4 | Normalized rank from BM25 sparse retrieval | Tuning parameter |
| Vector semantic rank (`w2`) | 0.4 | Normalized rank from ANN embedding search | Tuning parameter |
| Severity weight (`w3`) | 0.1 | Critical=1.0, High=0.75, Medium=0.5, Low=0.25 | Tuning parameter |
| Confidence weight (`w4`) | 0.1 | battle-tested=1.0, production-validated=0.8, peer-reviewed=0.6, speculative=0.3 | Tuning parameter |

> **TUNING NOTE:** The weights above are starting points, not settled architecture. Final weights are determined during Phase 5 evaluation against the ground-truth query set. The constraint is `w1 + w2 + w3 + w4 = 1.0`. Weights are stored in a config file (`writ.toml`), not hardcoded, so they can be adjusted without code changes.

### 4.3 Ground-Truth Query Set

MRR@5 > 0.85 is the Phase 5 gate metric. MRR requires a set of queries with known correct answers. The quality of the evaluation is bounded by the quality of this set.

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
| `sentence-transformers` | ^3.3 | Local embedding model -- no API cost, runs fully offline |
| `hnswlib` | ^0.8 | In-process HNSW vector search for Phases 1-5. Migrates to Qdrant at scale (Section 3.5). |
| `httpx` | ^0.27 | Async HTTP client used by the skill to call the service |
| `pydantic` | ^2.9 | Schema validation for rule ingestion and API request/response typing |
| `typer` | ^0.13 | CLI entrypoint for writ serve, ingest, validate, export commands |
| `rich` | ^13 | Terminal output formatting for CLI commands |
| `pytest` | ^8 | Test runner for schema validation, retrieval quality, and pipeline tests |

### 5.3 Embedding Model

The embedding model runs locally via sentence-transformers. No external API calls in the retrieval path.

| Model | Dimensions | Measured Single-Rule (p95) | Measured Batch-68 | Rationale |
|---|---|---|---|---|
| `all-MiniLM-L6-v2` | 384 | 9.0ms | 0.14s | Fast, small (80MB), strong semantic performance on technical text. Default choice. |
| `all-mpnet-base-v2` | 768 | Not yet measured | Not yet measured | Higher quality, 420MB, 2-3x slower. Upgrade path when retrieval quality needs improvement. Phase 5 evaluation. |

---

## 6. Project Structure

Writ is a single Python package installed globally. The service component and the CLI share the same codebase.

### 6.1 Directory Layout

```
writ/
├── bible/                      # Rule source files (Markdown, parsed at ingestion)
│   ├── architecture/
│   ├── database/
│   ├── security/
│   └── frameworks/
├── writ/                  # Python package
│   ├── __init__.py
│   ├── cli.py                  # typer CLI entrypoint
│   ├── server.py               # FastAPI HTTP server
│   ├── graph/
│   │   ├── schema.py           # Node and edge Pydantic models
│   │   ├── db.py               # Neo4j connection layer (bolt protocol)
│   │   ├── ingest.py           # Markdown -> graph migration and ingestion
│   │   └── integrity.py        # Contradiction, orphan, staleness detection
│   ├── retrieval/
│   │   ├── pipeline.py         # Orchestrates all 5 retrieval stages
│   │   ├── keyword.py           # Tantivy BM25 index build and query
│   │   ├── embeddings.py       # sentence-transformers + vector search abstraction (hnswlib/Qdrant)
│   │   ├── traversal.py        # Neo4j Cypher hop queries
│   │   └── ranking.py          # RRF + metadata weighting
│   └── compression/
│       ├── clusters.py         # Rule clustering algorithm
│       └── abstractions.py     # Abstraction node generation
├── tests/
│   ├── fixtures/               # Sample rules for unit tests
│   ├── test_schema.py
│   ├── test_retrieval.py
│   └── test_integrity.py
├── scripts/
│   └── migrate_68.py           # One-time migration of existing 68 rules
├── pyproject.toml
└── README.md
```

### 6.2 CLI Commands

| Command | Description |
|---|---|
| `writ serve` | Start the local service. Pre-warms all indexes into memory. Binds to localhost:8765. |
| `writ ingest` | Parse Markdown rule files and ingest into the graph database. Validates schema before writing. |
| `writ validate` | Run integrity checks: conflicts, orphans, staleness, missing required fields. |
| `writ export` | Regenerate all Markdown documentation from graph. Overwrites bible/ directory. |
| `writ migrate` | One-time migration of existing 68 Markdown rules into the graph. Runs scripts/migrate_68.py. |
| `writ query "..."` | CLI-level rule query for testing retrieval quality without an agent session. |
| `writ status` | Health check: rule count, index status, last ingestion date, stale rule count. |

---

## 7. HTTP API Interface

Writ runs as a local FastAPI service at localhost:8765. The skill file at `~/.claude/skills` calls this service directly over HTTP. No MCP layer. No additional protocol. Plain async HTTP calls with JSON payloads.

### 7.1 Endpoints

| Method | Endpoint | Parameters | Returns |
|---|---|---|---|
| POST | `/query` | `query: str`, `domain?: str`, `scope?: str`, `budget_tokens?: int`, `exclude_rule_ids?: list[str]` | Ranked list of matching rules with statement, trigger, violation, pass, and relationship context |
| GET | `/rule/{rule_id}` | `include_graph?: bool` | Full rule node with all fields. Optionally includes 1-hop graph context (connected rule IDs and edge types). |
| POST | `/conflicts` | `rule_ids: list[str]` | Any CONFLICTS_WITH edges between the provided rule set. Empty list = no conflicts detected. |
| GET | `/health` | none | Service status, rule count, index state, last ingestion timestamp. Skill calls this at session start. |

### 7.2 Skill Integration Pattern

The skill file instructs Claude to call Writ at the start of any coding task. The skill uses httpx for async HTTP. The loading sequence has two phases: mandatory rules first (enforcement skeleton), then retrieved rules (domain knowledge). See Section 7.5 for the full mandatory vs. retrieved distinction.

```
# Phase 1: Service check + mandatory rules
# At session start -- called by the skill automatically
GET http://localhost:8765/health
-> Confirms service is running. If down, skill falls back to loading enforcement/ and bible/ files directly.

# Skill loads ENF-ROUTE-001 and ENF-CTX-004 unconditionally (from file or via /rule endpoint).
# AI classifies task into tier. Skill loads tier-appropriate ENF-* rules.
# These are mandatory -- they bypass retrieval entirely (Section 7.5).
GET http://localhost:8765/rule/ENF-GATE-001
-> Returns full enforcement rule. Loaded before Phase A, not subject to ranking.

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

**Writ is stateless per request.** The service has no session concept. Session state is managed by the skill (client side), not the server. The skill tracks which rule_ids are already in context and passes them via `exclude_rule_ids` on subsequent `/query` calls. The skill also passes the remaining `budget_tokens` (total budget minus tokens already consumed).

This design keeps the server simple and cacheable. The tradeoff is that the skill must be disciplined about tracking state. If the skill fails to pass `exclude_rule_ids`, the agent gets duplicate rules and wastes context.

**What this does not solve:** The skill can exclude rules it already has, but it cannot tell the server "I need rules that complement what I already have." That requires the server to understand the semantic gap between loaded rules and the current situation. This is the Phase 9 (Agentic Retrieval Loop) problem. Phase 9 is a sketch because it depends on Phase 5 proving that single-query retrieval works first, but it is the actual production usage pattern, not a nice-to-have.

> **INSTALL ONCE:** The skill lives at `~/.claude/skills` -- installed once, active for every project. Run `writ serve` once per machine session. The HTTP service binds to localhost:8765 and all projects share the same live knowledge graph with zero per-project configuration.

### 7.5 Mandatory vs. Retrieved Rule Sets

**Status: Closed. Writ is an enforcement framework with intelligent retrieval, not a retrieval system that serves an enforcement framework.**

The retrieval pipeline (Section 4) is not authoritative for which rules the AI sees. It is authoritative for domain-specific technical rules only. A separate mandatory rule set always loads into agent context regardless of retrieval ranking, query content, or context budget.

**Why this distinction is load-bearing:**

The hooks (`check-gate-approval.sh`, `enforce-final-gate.sh`) enforce mechanically by checking filesystem state. They block writes whether or not the AI understands why. But an AI that hits a hook block without ever seeing the rule that explains the gate cannot reason about what to do next. It sees "write blocked" with no context for resolution. The enforcement model requires the AI to know the gates exist before it encounters them.

If `ENF-GATE-001` is subject to retrieval ranking and the ranking algorithm does not surface it for a given query, the AI proceeds as if no gate exists, writes a file, and gets blocked by the hook. The hook saved the output, but the session is now in a confused state -- the AI will retry, guess, or hallucinate an explanation. That is not enforcement. That is a trap.

**The two rule tiers:**

| Tier | Rules | Loading Mechanism | Subject to Retrieval? | Subject to Context Budget? |
|---|---|---|---|---|
| **Mandatory (enforcement skeleton)** | All `ENF-*` rules: routing, gates, pre-checks, post-checks, context discipline, system dynamics, security boundaries, operational claims | Skill injects at session start. Tier-appropriate subset per `ENF-ROUTE-001`. | No. Never ranked. Never filtered. Never omitted. | Partially. Summary mode (Section 4.4) may compress mandatory rules to statement + trigger only, omitting rationale. But the rule ID, statement, and trigger are always present. |
| **Retrieved (domain knowledge)** | All `ARCH-*`, `DB-SQL-*`, `FW-M2-*`, `FW-M2-RT-*`, `SEC-UNI-*`, `PERF-*`, `TEST-*`, `PY-*`, `PHP-*` rules | Retrieval pipeline via `/query` endpoint | Yes. Ranked by BM25 + vector + severity + confidence. | Yes. Context budget governs how many domain rules are returned. |

**Mandatory rule loading sequence (performed by the skill, not the service):**

1. Skill loads `ENF-ROUTE-001` and `ENF-CTX-004` unconditionally at session start.
2. AI classifies the task into a tier (0-3).
3. Skill loads the tier-appropriate enforcement subset:
   - **Tier 0-1**: No additional enforcement rules. Domain rules loaded via `/query` or fallback.
   - **Tier 2**: `ENF-PRE-001` through `ENF-PRE-004` (combined phases A-C). `ENF-POST-006`, `ENF-POST-007` (findings + static analysis).
   - **Tier 3**: Full enforcement set loaded incrementally per phase. `ENF-GATE-001` before Phase A. `ENF-GATE-002` before Phase B. And so on through `ENF-GATE-FINAL`.
4. Skill queries `/query` for domain-specific rules relevant to the task description. These are additive -- they fill the remaining context budget after mandatory rules are loaded.

**The service knows about mandatory rules but does not serve them on `/query`:**

Mandatory rules are stored in the graph like all other rules. They have the same schema, the same node type, the same edges. But they carry a graph property `mandatory: true` that excludes them from the retrieval pipeline's candidate set. They are never BM25-indexed, never embedded, never ranked. The service exposes them via `GET /rule/{rule_id}` for explicit lookup (e.g., the skill fetching `ENF-GATE-003` before Phase C), but `/query` never returns them.

This is not a soft convention. It is a hard filter at Stage 1 (Domain Filter) of the pipeline. Mandatory rules are excluded before BM25 scoring begins. If a mandatory rule appears in `/query` results, that is a bug.

**Why mandatory rules are not just "high severity":**

Setting `severity: Critical` on enforcement rules and trusting the ranking formula to surface them is insufficient. A Critical rule with low BM25 and low vector similarity to the query still ranks below a Medium rule with high textual relevance. Severity is a tiebreaker (weight 0.1), not a guarantee. The only guarantee is exclusion from ranking entirely.

**Context budget interaction:**

Mandatory rules consume context budget before the retrieval pipeline runs. The skill calculates `remaining_budget = total_budget - mandatory_tokens` and passes `remaining_budget` as `budget_tokens` to `/query`. This means domain rule retrieval operates within whatever budget remains after enforcement rules are loaded. At Tier 3, mandatory rules consume more budget (full gate sequence), leaving less room for domain rules. This is correct -- a Complex task needs more enforcement scaffolding, and the retrieval pipeline adapts by returning fewer but higher-ranked domain rules.

**Fallback behavior:**

When the service is not running, the skill falls back to loading Markdown files (Section 7.3). Mandatory rules fall back to the same mechanism they use today: the CLAUDE.md navigation table loads enforcement files directly. The fallback path does not change the mandatory/retrieved distinction -- it changes the retrieval mechanism for domain rules (from HTTP query to file loading). The enforcement skeleton is always loaded from files, whether the service is running or not.

> **ARCHITECTURAL INVARIANT:** No change to the retrieval pipeline -- ranking weights, embedding model, BM25 tuning, graph traversal depth -- can cause an enforcement rule to disappear from agent context. The mandatory set is outside the blast radius of retrieval tuning. If you are debugging a session where the AI did not follow a gate, check whether the mandatory rule was loaded, not whether the retrieval pipeline surfaced it.

---

## 8. Implementation Roadmap

Nine phases in strict sequence. Each phase produces a testable artifact before the next begins. No phase is started until the previous phase passes its acceptance criteria.

**Phases 1-5 are fully specified.** They constitute the core architecture and must be validated before any further work. **Phases 6-9 are directional sketches.** They will be fully specified only after Phase 5 passes its thesis gate. Specifying them in detail now would create sunk-cost pressure to proceed past a failed Phase 5.

### Phases 1-5 -- Core Architecture (Fully Specified)

| # | Phase | Deliverable | Acceptance Criteria |
|---|---|---|---|
| 1 | Schema & Validation Engine | Pydantic models for all node and edge types. Standalone CLI validator that accepts/rejects rule files against schema. Testable with fixtures, no database required. | All 68 existing rules pass validation. Malformed fixture rules are correctly rejected with actionable error messages. |
| 2 | Infrastructure Setup & Performance Validation | Neo4j running with schema. Tantivy index buildable from rule text. hnswlib vector search operational (Section 3.5). Data access layer with CRUD on Rule nodes and graph edges. | CRUD on Rule nodes passes. Neo4j traversal returns correct 1-hop neighbours on test fixtures. Tantivy BM25 returns expected results on test fixtures. hnswlib cosine search returns expected results on synthetic embeddings. **Neo4j benchmarked at 1K/10K/100K/1M nodes -- latency validated or mitigation identified.** |
| 3 | Migration Script -- 68 Rules | One-time script ingests all 68 existing Markdown rules into the graph. Existing metadata maps directly. New graph-only fields receive the defaults specified in Section 2.3 and are flagged for human review. Cross-references become skeleton RELATED_TO edges. | All 68 rules present in graph with no data loss. Graph-only fields populated per Section 2.3 defaults. Skeleton edges created for all existing cross-references. Script is idempotent. |
| 4 | Post-Commit Validation & Integrity Reporting | Automated integrity checks run after every ingest: contradiction detection (CONFLICTS_WITH), orphan detection (rules unreachable by any query path), staleness flagging (rules past staleness_window), redundancy detection (high-similarity duplicates). Uses Neo4j Cypher + APOC for offline analysis (see Section 3.6). | Known conflict in test fixtures detected. Orphan rule flagged. Stale rule reported. Redundant pair identified. `writ validate` exits non-zero on any failure. |
| 5 | Task-Time Retrieval -- Thesis Gate | Full 5-stage hybrid pipeline operational. FastAPI service at localhost:8765. All indexes pre-warmed at startup. Ranking weights tuned against ground-truth query set (see Section 4.2). Manual query testing via `writ query` CLI command. | `writ query "controller contains SQL"` returns ARCH-ORG-001 as top result. p95 latency < 10ms on warm index. MRR@5 > 0.85 on ground-truth query set. **If this gate fails, Phases 6-9 are invalid and the pipeline must be re-architected.** |

### Phases 6-9 -- Post-Validation Extensions (Sketches Only)

These phases are designed only after Phase 5 passes. Full deliverables and acceptance criteria will be written at that time.

| # | Phase | Intent | Key Question to Resolve |
|---|---|---|---|
| 6 | Authoring Tools | `writ add` command with relationship suggestion, conflict detection, and redundancy warnings at authoring time. | Multi-author conflict resolution governance (see Section 9). |
| 7 | Generated Artifacts | `writ export` regenerates all Markdown docs from the graph. bible/ becomes a generated view, not the source of truth. Fallback path (Section 7.3) must use exported files. | Round-trip fidelity: can generated Markdown reproduce the original 68 rules? |
| 8 | Compression Layer | Rule clustering into abstraction nodes. Summary mode retrieval returns abstractions when context budget is low. | Which clustering algorithm (k-means vs HDBSCAN) produces coherent groups? |
| 9 | Agentic Retrieval Loop | Agent-driven uncertainty resolution. Mid-session drill-down without re-running the full pipeline. **This is the actual production usage pattern** (see Section 7.4), not a nice-to-have. Phase 5 validates single-query retrieval; Phase 9 validates the sequential multi-query pattern that real coding sessions produce. | Server-side session state vs. client-side tracking. Semantic gap detection: how does the service know what rules would complement the ones already loaded? |

> **CRITICAL PATH NOTE:** Phase 5 (Retrieval Pipeline) is the thesis validation gate. If `/query` does not return correct results faster than context-stuffing the whole Bible, the architecture assumption is wrong and Phases 6-9 are invalid. Do not proceed past Phase 5 without validated retrieval quality metrics.

---

## 9. Open Questions & Decision Gates

These questions must be answered before the relevant phase begins. They are not deferred indefinitely -- each has a phase gate that forces resolution.

| Question | Decision Gate | Resolution Path | Status |
|---|---|---|---|
| Neo4j traversal performance: does it meet < 3ms at 1K/10K/100K/1M nodes? | Phase 2 | Benchmark protocol defined in Section 3.3. Neo4j committed; Phase 2 validates performance and identifies mitigation if needed. | Open (engine closed, perf validation open) |
| Vector search engine. | -- | hnswlib for Phases 1-5. Qdrant is the defined migration target at scale (Section 3.5). Migration triggers documented. | **Closed** |
| Which embedding model (MiniLM vs mpnet) produces better retrieval precision on rule text? | Phase 5 | Evaluate both on ground-truth query set derived from the 68 existing rules. MRR@5 metric determines winner. | Open |
| Ranking formula weights (w1-w4): what values maximize MRR@5? | Phase 5 | Grid search or manual tuning against ground-truth query set. Starting points in Section 4.2. | Open |
| Graph-level versioning: how do agents reference a stable graph version during long-running sessions? | Phase 5 | Design immutable graph snapshots tagged by ingest timestamp. Agent pins to snapshot at session start via /health response. | Open |
| Rule-level versioning: when a rule is edited (not replaced), what happens to the old version? | Phase 5 | Snapshot model handles intra-session consistency (pin at start). Cross-session drift (code reviewed under old rule, new session applies new rule) is a governance problem -- not solvable by the retrieval layer. Phase 6 authoring tools should surface rule edit history. | Open |
| What clustering algorithm produces the most coherent abstraction nodes for rule compression? | Post-Phase 5 | Evaluate k-means vs HDBSCAN on rule embeddings. Human review of cluster coherence scores the winner. | Deferred |
| Multi-author conflict resolution: when two contributors add conflicting rules, what is the governance process? | Post-Phase 5 | Define conflict review process in CONTRIBUTING.md before authoring tools ship. Human resolution required -- no automatic merge. | Deferred |
| Agent integration protocol: REST API (localhost:8765). | -- | Skill calls FastAPI service directly over HTTP. No MCP layer. No SDK. Plain async HTTP with httpx. | **Closed** |
| Is the retrieval service authoritative or advisory for enforcement rules? | -- | Advisory. Enforcement rules (`ENF-*`) are mandatory -- always loaded by the skill, never subject to retrieval ranking. Retrieval is authoritative for domain rules only. See Section 7.5. | **Closed** |

---

## 10. Performance Targets & Measurement

These are contractual targets, not aspirational ones. If any target is missed in Phase 5 testing, the pipeline must be re-architected before proceeding to Phases 6-9.

| Metric | Target | Pre-Impl Benchmark | Measurement Method | Failure Action |
|---|---|---|---|---|
| End-to-end query latency (warm index) | p95 < 10ms | Stages 2-4 sum: ~0.7ms (synthetic, no DB) | pytest-benchmark, 100 queries | Profile each stage. Optimize bottleneck. |
| Service cold start (`writ serve`) | < 3 seconds | Index rebuild: 0.03s (excludes model load + DB) | `time writ serve --ready` | Lazy-load non-critical indexes. |
| Retrieval precision (MRR@5) | > 0.85 | Not measured (requires real rules + ground-truth set) | Ground-truth query set, 68-rule corpus | Tune BM25 field weights. Upgrade embedding model. |
| Memory footprint (warm service) | < 2 GB RAM | 913 MB RSS (includes embedding model) | htop / memory_profiler during load test | Compress embeddings. Reduce HNSW ef_construction. |
| Integrity check duration (68 rules) | < 500ms | Not measured (requires Phase 4) | `writ validate --benchmark` | Cache graph traversal results. |
| Rule ingestion (single rule) | < 2 seconds | 9ms embedding (excludes DB write) | `writ ingest --benchmark` | Batch embedding computation. |

> **BENCHMARK REFERENCE:** Pre-implementation benchmark data from `benchmarks/run_benchmarks.py`, run 2026-03-15 on synthetic 1,000-rule corpus. Full results in `benchmark.md`. These numbers validate infrastructure latency only -- retrieval quality is unmeasured until Phase 5.
