# Writ -- Development Plan

**Version:** 1.0
**Date:** 2026-03-15
**Source of Truth:** `RAG_arch_handbook.md` v1.0

---

## 1. Project Constraints

### 1.1 Language & Runtime

- **Python 3.11+** (3.12 recommended). All service components, CLI, and tests run on a single Python runtime.
- **OS:** macOS 13+ / Linux. Windows via WSL2 only (native Windows not tested).
- **No external API calls in the retrieval hot path.** Embeddings are computed locally via `sentence-transformers`. The service operates fully offline once indexes are built.

### 1.2 Dependency Policy

All dependencies are pinned to compatible ranges per handbook Section 5.2:

| Package | Version Constraint |
|---|---|
| `fastapi` | `^0.115` |
| `uvicorn` | `^0.32` |
| `neo4j` | `^5.0` |
| `tantivy` | `^0.22` |
| `sentence-transformers` | `^3.3` |
| `hnswlib` | `^0.8` |
| `httpx` | `^0.27` |
| `pydantic` | `^2.9` |
| `typer` | `^0.13` |
| `rich` | `^13` |

No dependency may be added without verifying it does not introduce external network calls into the query path.

### 1.3 Neo4j

- **Version:** 5.x+
- **Setup:** Local installation or Docker container (`neo4j:5-community` image). Both paths documented.
- Neo4j serves all graph operations: hot-path traversal, offline integrity, rule CRUD. No second graph engine. NetworkX is not used.
- APOC plugin required for integrity checks (Phase 4).

### 1.4 Enforcement Hooks

All files in `.claude/hooks/` and `bin/` carry forward unchanged. They are not part of the RAG system. The development plan does not touch, rewrite, or replace them. The skill continues to invoke hooks exactly as it does today.

### 1.5 No MCP Layer

The service is plain HTTP via FastAPI at `localhost:8765`. The skill calls it with `httpx`. No MCP protocol, no SDK wrapper, no additional abstraction.

### 1.6 Mandatory Rule Boundary

The `mandatory` field on Rule nodes is a hard architectural boundary (handbook Section 7.5):

- Rules with `mandatory: true` (all `ENF-*` rules) are **never** indexed by BM25, **never** embedded, **never** ranked, and **never** returned by `/query`.
- They are excluded at Stage 1 (Domain Filter) before any scoring begins.
- They are accessible only via `GET /rule/{rule_id}` for explicit lookup.
- If a mandatory rule appears in `/query` results, that is a bug.

### 1.7 Rule Count

The migration script ingests all rules found in `bible/`. The count is not hardcoded -- whatever rules exist at migration time are ingested. The handbook references 68 as the count at time of writing; the actual number will be whatever the corpus contains.

---

## 2. Environment Setup

### 2.1 Python Virtual Environment

```bash
# From project root
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

The `pyproject.toml` defines dependency groups: `core` (default), `dev` (pytest, mypy, ruff, pytest-benchmark), and `benchmark`.

### 2.2 Neo4j Setup

**Option A: Docker (recommended for development)**

```bash
docker run -d \
  --name writ-neo4j \
  -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/writdevpass \
  -e NEO4J_PLUGINS='["apoc"]' \
  -v writ-neo4j-data:/data \
  neo4j:5-community
```

**Option B: Local installation**

Install Neo4j 5.x+ per platform instructions. Enable APOC plugin. Configure bolt on `localhost:7687`.

Connection defaults in `writ.toml`:
```toml
[neo4j]
uri = "bolt://localhost:7687"
user = "neo4j"
password = "writdevpass"
database = "neo4j"
```

### 2.3 Model Download

The `sentence-transformers` embedding model must be downloaded before first use:

```python
from sentence_transformers import SentenceTransformer
model = SentenceTransformer("all-MiniLM-L6-v2")  # ~80MB, cached at ~/.cache/torch/sentence_transformers/
```

This happens automatically on first `writ ingest` or `writ serve`, but can be pre-downloaded to avoid startup delay.

### 2.4 Tantivy Build

The `tantivy` Python package (`tantivy-py`) includes Rust bindings that compile on install. Requires a working Rust toolchain if no pre-built wheel is available for the platform. `pip install tantivy` handles this for common platforms (Linux x86_64, macOS ARM/x86).

### 2.5 Development vs. Production Configuration

| Setting | Development | Production |
|---|---|---|
| Neo4j auth | `neo4j/writdev` | Configured per deployment |
| Log level | `DEBUG` | `INFO` |
| Port | `8765` | `8765` |
| Embedding model | `all-MiniLM-L6-v2` | Determined by Phase 5 evaluation |
| Service timeout | `50ms` | Tuned per Phase 5 p99 data |

Configuration is controlled via `writ.toml` with environment variable overrides (see Section 7).

### 2.6 Running the Test Suite

```bash
# All tests
pytest

# Schema tests only (Phase 1)
pytest tests/test_schema.py

# Integration tests requiring Neo4j (Phase 2+)
pytest tests/test_integrity.py

# Retrieval quality tests (Phase 5)
pytest tests/test_retrieval.py

# Benchmarks
pytest benchmarks/run_benchmarks.py --benchmark-only
```

### 2.7 Starting the Service Locally

```bash
writ serve
# Starts FastAPI on localhost:8765
# Pre-warms BM25 index, vector index, and adjacency cache into memory
# Health check: curl http://localhost:8765/health
```

---

## 3. Directory Structure

```
writ/                              # Project root
├── bible/                         # Rule source files (Markdown, parsed at ingestion)
│   ├── architecture/              # ARCH-* rules
│   ├── database/                  # DB-SQL-* rules
│   ├── security/                  # SEC-UNI-* rules
│   └── frameworks/                # FW-M2-*, FW-M2-RT-* rules
├── writ/                          # Python package
│   ├── __init__.py                # Version string only
│   ├── cli.py                     # Typer CLI: serve, ingest, validate, export, migrate, query, status
│   ├── server.py                  # FastAPI app: /query, /rule/{rule_id}, /conflicts, /health
│   ├── graph/
│   │   ├── __init__.py
│   │   ├── schema.py              # Pydantic models: Rule, Abstraction, Domain, Evidence, Tag nodes; all edge types
│   │   ├── db.py                  # Neo4j connection layer (bolt protocol), connection pool, session management
│   │   ├── ingest.py              # Markdown parsing -> schema validation -> graph write. Triggers export on completion.
│   │   └── integrity.py           # Contradiction, orphan, staleness, redundancy detection via Cypher + APOC
│   ├── retrieval/
│   │   ├── __init__.py
│   │   ├── pipeline.py            # Orchestrates 5 retrieval stages in sequence
│   │   ├── keyword.py             # Tantivy BM25 index build + query on trigger, statement, tag fields
│   │   ├── embeddings.py          # Abstraction layer: search(vector, k, filters) -> list[ScoredResult]. hnswlib impl.
│   │   ├── traversal.py           # Neo4j Cypher 1-2 hop queries from candidate rule_ids
│   │   └── ranking.py             # RRF of BM25 + vector scores, weighted by severity + confidence. Context budget.
│   └── compression/
│       ├── __init__.py
│       ├── clusters.py            # Rule clustering algorithm (Phase 8)
│       └── abstractions.py        # Abstraction node generation from clusters (Phase 8)
├── tests/
│   ├── __init__.py
│   ├── fixtures/                  # Sample rules, malformed rules, ground-truth queries
│   │   └── README.md              # Documents what each fixture file contains and which phase uses it
│   ├── test_schema.py             # Phase 1: Pydantic model validation, required field checks, rejection of malformed rules
│   ├── test_retrieval.py          # Phase 5: MRR@5 against ground-truth query set, latency benchmarks
│   └── test_integrity.py          # Phase 4: Conflict detection, orphan detection, staleness flagging, redundancy
├── scripts/
│   └── migrate.py              # One-time migration of all 80 current rules into the graph
├── benchmarks/
│   └── run_benchmarks.py          # pytest-benchmark suite: per-stage latency, end-to-end pipeline
├── pyproject.toml                 # Package definition, dependencies, entry points
├── writ.toml                      # Default configuration
├── RAG_arch_handbook.md           # Architecture specification (source of truth)
├── DEVELOPMENT_PLAN.md            # This file
├── EXECUTION_PLAN.md              # Phase-by-phase tracker
└── README.md                      # Project overview, install, usage, architecture summary
```

---

## 4. Dependency Manifest

```toml
[project]
name = "writ"
version = "0.1.0"
description = "Hybrid RAG knowledge retrieval service for AI coding rule enforcement"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.115,<1",
    "uvicorn>=0.32,<1",
    "neo4j>=5.0,<6",
    "tantivy>=0.22,<1",
    "sentence-transformers>=3.3,<4",
    "hnswlib>=0.8,<1",
    "httpx>=0.27,<1",
    "pydantic>=2.9,<3",
    "typer>=0.13,<1",
    "rich>=13,<14",
]

[project.optional-dependencies]
dev = [
    "pytest>=8,<9",
    "pytest-benchmark>=4,<5",
    "pytest-asyncio>=0.23,<1",
    "mypy>=1.11,<2",
    "ruff>=0.6,<1",
]
benchmark = [
    "pytest>=8,<9",
    "pytest-benchmark>=4,<5",
    "memory-profiler>=0.61,<1",
]

[project.scripts]
writ = "writ.cli:app"

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"
```

### Entry Points

- `writ` CLI command maps to `writ.cli:app` (Typer application)
- Commands: `serve`, `ingest`, `validate`, `export`, `migrate`, `query`, `status`

---

## 5. Interface Contracts

### 5.1 Node Models (Pydantic)

**Rule Node:**

```python
class Rule(BaseModel):
    rule_id: str                    # Format: PREFIX-NAME-NNN (e.g., ARCH-ORG-001)
    domain: str                     # Architecture | Database | Security | Framework
    severity: Severity              # Critical | High | Medium | Low
    scope: Scope                    # file | module | slice | PR
    trigger: str                    # Measurable activation condition
    statement: str                  # 1-2 sentence binary-testable requirement
    violation: str                  # Concrete bad example
    pass_example: str               # Concrete good example (field name: "pass" in schema)
    enforcement: str                # Gate, hook, or verification step
    rationale: str                  # One paragraph max -- the "why"
    mandatory: bool = False         # True for ENF-* rules. Excluded from retrieval pipeline.
    confidence: Confidence = "production-validated"
    evidence: str = "doc:original-bible"
    staleness_window: int = 365     # Days before re-validation required
    last_validated: date            # ISO date of last human review
```

**Abstraction Node:**

```python
class Abstraction(BaseModel):
    abstraction_id: str
    summary: str                    # Pre-computed cluster summary
    rule_ids: list[str]             # Rules this abstraction compresses
    domain: str
    compression_ratio: float        # Rules compressed / summary token count
```

**Domain Node:**

```python
class Domain(BaseModel):
    name: str                       # Architecture | Database | Security | Framework
    rule_count: int
    last_updated: datetime
```

**Evidence Node:**

```python
class Evidence(BaseModel):
    evidence_id: str
    type: EvidenceType              # incident | PR | doc | ADR
    reference: str                  # URL, file path, or identifier
    date: date
```

**Tag Node:**

```python
class Tag(BaseModel):
    name: str
    rule_count: int
```

### 5.2 Edge Models

```python
class DependsOn(BaseModel):       # Rule -> Rule
    source_id: str
    target_id: str

class Precedes(BaseModel):        # Rule -> Rule
    source_id: str
    target_id: str

class ConflictsWith(BaseModel):   # Rule <-> Rule (bidirectional)
    source_id: str
    target_id: str

class Supplements(BaseModel):     # Rule -> Rule
    source_id: str
    target_id: str

class Supersedes(BaseModel):      # Rule -> Rule
    source_id: str
    target_id: str

class RelatedTo(BaseModel):       # Rule <-> Rule (skeleton edge)
    source_id: str
    target_id: str

class AppliesTo(BaseModel):       # Rule -> Domain/Tag
    rule_id: str
    target_name: str
    target_type: str               # "domain" | "tag"

class Abstracts(BaseModel):       # Abstraction -> Rule[]
    abstraction_id: str
    rule_ids: list[str]

class JustifiedBy(BaseModel):     # Rule -> Evidence
    rule_id: str
    evidence_id: str
```

### 5.3 API Endpoints (FastAPI)

```python
@app.post("/query")
async def query_rules(
    query: str,
    domain: str | None = None,
    scope: str | None = None,
    budget_tokens: int | None = None,
    exclude_rule_ids: list[str] | None = None,
) -> QueryResponse:
    """Ranked list of matching domain rules. Mandatory rules excluded."""

@app.get("/rule/{rule_id}")
async def get_rule(
    rule_id: str,
    include_graph: bool = False,
) -> RuleResponse:
    """Full rule node. Optionally includes 1-hop graph context."""

@app.post("/conflicts")
async def check_conflicts(
    rule_ids: list[str],
) -> ConflictsResponse:
    """CONFLICTS_WITH edges between provided rules. Empty = no conflicts."""

@app.get("/health")
async def health() -> HealthResponse:
    """Service status, rule count, index state, last ingestion timestamp."""
```

### 5.4 CLI Commands (Typer)

```python
@app.command()
def serve(port: int = 8765, host: str = "localhost"):
    """Start Writ service. Pre-warms indexes into memory."""

@app.command()
def ingest(path: Path = Path("bible/")):
    """Parse Markdown rules and ingest into graph. Validates schema. Triggers export."""

@app.command()
def validate(review_confidence: bool = False, benchmark: bool = False):
    """Run integrity checks: conflicts, orphans, staleness, redundancy."""

@app.command()
def export(output: Path = Path("bible/")):
    """Regenerate Markdown from graph. Overwrites bible/ directory."""

@app.command()
def migrate():
    """One-time migration of existing rules into graph. Runs scripts/migrate.py."""

@app.command()
def query(query_text: str, domain: str | None = None, budget: int | None = None):
    """CLI rule query for testing retrieval quality."""

@app.command()
def status():
    """Health check: rule count, index status, last ingestion, stale rules."""
```

### 5.5 Response Models

```python
class ScoredRule(BaseModel):
    rule_id: str
    score: float
    statement: str
    trigger: str
    violation: str
    pass_example: str
    rationale: str | None           # Omitted in standard mode
    relationships: list[RelationshipContext] | None  # Omitted in standard mode

class QueryResponse(BaseModel):
    rules: list[ScoredRule]
    mode: str                       # "summary" | "standard" | "full"
    total_candidates: int
    latency_ms: float

class RuleResponse(BaseModel):
    rule: Rule
    graph_context: list[RelationshipContext] | None

class ConflictsResponse(BaseModel):
    conflicts: list[ConflictPair]

class HealthResponse(BaseModel):
    status: str
    rule_count: int
    mandatory_count: int
    index_state: str                # "warm" | "cold" | "rebuilding"
    last_ingestion: datetime | None
    stale_rule_count: int
```

---

## 6. Testing Strategy

### 6.1 Phase 1: Schema Validation (Unit Tests)

**Scope:** Pydantic models accept valid rules, reject malformed rules. No database required.

**Fixtures needed:**
- `tests/fixtures/valid_rule.json` -- A well-formed rule with all required fields
- `tests/fixtures/valid_enf_rule.json` -- An `ENF-*` rule with `mandatory: true`
- `tests/fixtures/missing_field_rules/` -- One JSON per required field, each missing that field
- `tests/fixtures/invalid_type_rules/` -- Wrong types (severity as int, scope as list, etc.)
- `tests/fixtures/edge_cases.json` -- Empty strings, very long strings, special characters in trigger

**Tests:**
- Valid rule parses without error
- Each missing required field raises `ValidationError` with actionable message
- `mandatory` defaults to `false`
- `confidence` defaults to `production-validated`
- `staleness_window` defaults to 365
- Invalid `severity` values rejected
- Invalid `scope` values rejected
- `rule_id` format validation (PREFIX-NAME-NNN pattern)

### 6.2 Phase 2: Infrastructure Integration Tests

**Scope:** Neo4j CRUD, Tantivy indexing, hnswlib search. Requires running Neo4j.

**Tests:**
- Create a Rule node, read it back, verify all fields
- Create two Rule nodes with a `DEPENDS_ON` edge, verify traversal returns the neighbor
- 1-hop and 2-hop traversal returns correct neighbors
- Tantivy BM25 index builds from rule text, query returns expected rules
- Mandatory rules (`mandatory: true`) are excluded from BM25 index and vector index at build time
- hnswlib cosine search returns nearest neighbors on synthetic embeddings
- Neo4j benchmark: 1K/10K/100K/1M node traversal latency (pytest-benchmark)

### 6.3 Phase 4: Integrity Tests

**Scope:** `writ validate` detects known problems in test fixtures.

**Fixtures needed:**
- Two rules with `CONFLICTS_WITH` edge -- conflict must be detected
- An orphan rule (no edges, unreachable by query path) -- must be flagged
- A rule with `last_validated` older than `staleness_window` -- must be reported stale
- Two rules with near-identical embeddings -- redundancy must be flagged

**Tests:**
- `writ validate` exits non-zero when conflicts exist
- Orphan detection finds the isolated rule
- Staleness check flags overdue rules
- Redundancy detection identifies high-similarity duplicates

### 6.4 Phase 5: Retrieval Quality Tests

**Scope:** End-to-end pipeline accuracy and latency on real rules.

**Fixtures needed:**
- `tests/fixtures/ground_truth_queries.json` -- 50+ human-authored queries with expected rule_ids
- All rules ingested into graph with indexes built

**Tests:**
- MRR@5 > 0.85 on ground-truth query set
- p95 latency < 10ms on 100 warm-index queries (pytest-benchmark)
- Context budget modes work: summary (< 2K tokens), standard (2K-8K), full (> 8K)
- `exclude_rule_ids` correctly removes rules from results
- Domain filter restricts results to specified domain

### 6.5 Benchmark Tests

```python
# benchmarks/run_benchmarks.py
# Per-stage benchmarks:
# - Stage 2: Tantivy BM25 query latency (target: < 2ms)
# - Stage 3: hnswlib ANN search latency (target: < 3ms)
# - Stage 4: Neo4j traversal latency (target: < 3ms)
# - Stage 5: Ranking computation latency (target: < 1ms)
# - End-to-end: Full pipeline latency (target: p95 < 10ms)
# - Cold start: writ serve startup time (target: < 3s)
# - Ingestion: Single rule ingest time (target: < 2s)
# - Integrity: writ validate on full corpus (target: < 500ms at 80 rules)
```

---

## 7. Configuration

### 7.1 `writ.toml` Schema

```toml
[service]
host = "localhost"
port = 8765
timeout_ms = 50           # Fallback timeout for skill. Tuned in Phase 5.
log_level = "INFO"        # DEBUG | INFO | WARNING | ERROR

[neo4j]
uri = "bolt://localhost:7687"
user = "neo4j"
password = "writdevpass"
database = "neo4j"

[embedding]
model = "all-MiniLM-L6-v2"         # Phase 5 may switch to all-mpnet-base-v2
dimensions = 384                     # Must match model output
cache_dir = "~/.cache/writ/models"

[vector]
backend = "hnswlib"                  # "hnswlib" or "qdrant" (post-migration)
ef_construction = 200                # HNSW build parameter
ef_search = 50                       # HNSW query parameter. Tuned in Phase 5.
M = 16                               # HNSW connectivity parameter

[vector.qdrant]                      # Used only when backend = "qdrant"
url = "http://localhost:6333"
collection = "writ_rules"

[tantivy]
index_dir = "~/.cache/writ/tantivy"
fields = ["trigger", "statement", "tags"]   # BM25 indexed fields

[ranking]
w_bm25 = 0.4                        # BM25 keyword rank weight
w_vector = 0.4                       # Vector semantic rank weight
w_severity = 0.1                     # Severity weight
w_confidence = 0.1                   # Confidence weight
# Constraint: w_bm25 + w_vector + w_severity + w_confidence = 1.0

[ranking.severity_values]
critical = 1.0
high = 0.75
medium = 0.5
low = 0.25

[ranking.confidence_values]
battle-tested = 1.0
production-validated = 0.8
peer-reviewed = 0.6
speculative = 0.3

[context_budget]
summary_threshold = 2000             # Below this: return abstraction summaries only
standard_threshold = 8000            # Below this: top-5, omit rationale
# Above standard_threshold: top-10 with full context

[ingestion]
bible_dir = "bible/"
auto_export = true                   # Run export after every ingest

[validation]
query_rule_ratio_warning = 10        # Warn when ratio drops below 1:N
staleness_default_days = 365
```

### 7.2 Environment Variable Overrides

Every config key can be overridden via environment variable with the prefix `WRIT_` and double-underscore section separators:

```bash
WRIT_SERVICE__PORT=9000
WRIT_NEO4J__URI=bolt://production-host:7687
WRIT_NEO4J__PASSWORD=prodpass
WRIT_RANKING__W_BM25=0.35
WRIT_EMBEDDING__MODEL=all-mpnet-base-v2
```

---

## 8. Migration Plan

### 8.1 Scope

The migration script (`scripts/migrate.py`) discovers and ingests all Markdown rules found in `bible/`. The count is not hardcoded -- whatever rules exist at migration time are ingested.

### 8.2 Field Mapping

| Current Markdown Field | Graph Field | Mapping |
|---|---|---|
| Rule ID (header or filename) | `rule_id` | Direct. Format: `PREFIX-NAME-NNN` |
| Domain (directory or metadata) | `domain` | Map directory to domain: `architecture/` -> Architecture, etc. |
| Severity (if present) | `severity` | Direct. Default to `Medium` if absent. |
| Scope (if present) | `scope` | Direct. Default to `file` if absent. |
| Trigger section | `trigger` | Extract from rule body |
| Statement / requirement | `statement` | Extract from rule body |
| Violation example | `violation` | Extract from rule body |
| Pass example | `pass_example` | Extract from rule body |
| Enforcement section | `enforcement` | Extract from rule body |
| Rationale / why section | `rationale` | Extract from rule body |

### 8.3 Graph-Only Field Defaults (Section 2.3)

| Field | Default Value | Rationale |
|---|---|---|
| `mandatory` | `true` if `rule_id` starts with `ENF-`, else `false` | Enforcement rules bypass retrieval |
| `confidence` | `production-validated` (0.8) | Rules are production-used but not formally graph-reviewed |
| `evidence` | `doc:original-bible` | All rules originate from the original Bible |
| `staleness_window` | 365 days | Standard annual review cycle |
| `last_validated` | Migration run date | Starts the clock |

### 8.4 Cross-Reference to Edge Mapping

Existing Markdown rules contain cross-references (e.g., "See ARCH-ORG-001", "Related to DB-SQL-003"). During migration:

1. Parse all cross-reference patterns from rule bodies
2. Create `RELATED_TO` skeleton edges for each cross-reference
3. `RELATED_TO` edges are promoted to stronger types (`DEPENDS_ON`, `CONFLICTS_WITH`, etc.) during human review
4. Log all created skeleton edges for review

### 8.5 Idempotency

The migration script is idempotent:

- Uses `MERGE` (not `CREATE`) in Cypher operations
- Running the script twice produces the same graph state
- Existing nodes are updated, not duplicated
- Edges are matched by source + target + type before creation
- Script logs "created" vs. "updated" counts for verification

### 8.6 Validation Post-Migration

After migration completes:
1. Assert Rule node count in graph matches rule file count in `bible/`
2. Assert all required fields are populated on every node
3. Assert `mandatory: true` on all `ENF-*` rules, `false` on all others
4. Assert graph-only field defaults match Section 2.3
5. Assert all parsed cross-references have corresponding `RELATED_TO` edges
6. Run `writ validate` to check for integrity issues
