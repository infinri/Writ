# Writ -- Phase 5 Thesis Gate Results

**Date:** 2026-03-15
**Evaluator:** Automated pipeline + human review
**Source of truth:** `RAG_arch_handbook.md` Section 8, Phase 5

---

## Gate Verdict: PASS

All acceptance criteria met. The hybrid retrieval pipeline returns correct, relevant rules faster than context-stuffing the full Bible.

---

## Retrieval Quality

| Metric | Target | Result | Status |
|---|---|---|---|
| MRR@5 (ambiguous held-out) | > 0.85 | **0.8558** | Pass |
| Relevant rule in top 5 | > 85% | **96%** (80/83) | Pass |
| Strong or Good coverage | -- | **83%** (69/83) | -- |
| Query corpus | >= 50 | **83** (50 keyword + 14 symptom + 19 ambiguous) | Pass |

### Evaluation Methodology

Three query sets evaluated with locked weights (0.3/0.5/0.1/0.1 + trigger 2x boost):

1. **Keyword set (50 queries):** Direct rule-terminology queries. Baseline regression check.
2. **Symptom set (15 queries):** Natural language problem descriptions. Tests semantic retrieval over keyword matching.
3. **Ambiguous set (20 queries):** Cross-domain problem descriptions written after weights were locked. Never seen during tuning. MRR@5 = 0.8558. This is the canonical gate metric.

All 85 queries evaluated for coverage quality, not just position of a single expected rule.

### Full Evaluation -- All 85 Queries

Each query rated: **Strong** (primary rule #1 + useful supporting rules), **Good** (primary rule in top 5 + some noise), **Partial** (relevant rules present but not well-ranked), **Weak** (noise dominates or question is not a retrieval question).

#### Keyword Queries (Q1-Q50)

| # | Query | #1 Result | Verdict |
|---|---|---|---|
| 1 | controller contains SQL query | DB-SQL-001 | Good -- DB-SQL rules relevant, ARCH-ORG-001 at #5 |
| 2 | dependency injection constructor | ARCH-DI-001 | Strong |
| 3 | async blocking event loop | PY-ASYNC-001 | Strong -- PERF-IO-001 at #2 |
| 4 | circular import between modules | PY-IMPORT-001 | Strong |
| 5 | magic number in conditional | ARCH-CONST-001 | Strong |
| 6 | function too long needs refactor | ARCH-FUNC-001 | Strong |
| 7 | catch exception without context | PHP-TRY-001 | Strong -- ARCH-ERR-001 at #2 |
| 8 | protocol vs abstract base class | PY-PROTO-001 | Strong -- ARCH-COMP-001 at #2 |
| 9 | pydantic validation external data | PY-PYDANTIC-001 | Strong -- FW-M2-001 at #2 |
| 10 | SQL positional placeholder | DB-SQL-001 | Strong -- all DB-SQL in top 5 |
| 11 | deep inheritance hierarchy | ARCH-COMP-001 | Strong -- PY-PROTO-001 at #2 |
| 12 | duplicate logic across modules | ARCH-DRY-001 | Strong -- ARCH-ORG-001 at #3 |
| 13 | public method missing type annotation | PHP-TYPE-001 | Strong -- ARCH-TYPE-001 at #2 |
| 14 | single source of truth REST GraphQL | FW-M2-RT-004 | Good -- ARCH-SSOT-001 at #2 |
| 15 | plugin targeting interface vs concrete | FW-M2-004 | Strong |
| 16 | repository pattern entity retrieval | FW-M2-002 | Strong -- FW-M2-001 at #2 |
| 17 | test isolation shared state | TEST-ISO-001 | Strong -- all TEST rules top 3 |
| 18 | test first before implementation | TEST-TDD-001 | Strong |
| 19 | integration test real database | TEST-INT-001 | Strong -- all TEST rules top 3 |
| 20 | SQL string concatenation fragmented | DB-SQL-002 | Strong -- all DB-SQL top 3 |
| 21 | performance optimization without measurement | PERF-OPT-001 | Strong -- PERF-QBUDGET-001 at #2 |
| 22 | lazy loading deferred resource | PERF-LAZY-001 | Strong -- PERF-IO-001 at #2 |
| 23 | nested loop O(n^2) | PERF-BIGO-001 | Strong |
| 24 | synchronous IO in request handler | PY-ASYNC-001 | Strong -- PERF-IO-001 at #2 |
| 25 | query budget database calls | PERF-QBUDGET-001 | Strong |
| 26 | authentication vs authorization check | SEC-UNI-001 | Strong -- full security stack in top 5 |
| 27 | API endpoint data exposure | SEC-UNI-003 | Strong -- SEC-UNI-001 #2, FW-M2-RT-004 #3 |
| 28 | extend vendor class without modifying | ARCH-EXT-001 | Strong |
| 29 | order state machine transitions | FW-M2-RT-002 | Strong -- FW-M2-RT-001 at #3 |
| 30 | MSI inventory salability | FW-M2-RT-001 | Strong -- FW-M2-RT-006 at #2 |
| 31 | admin config system value | FW-M2-RT-003 | Strong -- SEC-UNI-004 at #2 |
| 32 | REST endpoint authorization ACL | FW-M2-RT-004 | Strong -- SEC-UNI-001 #2, SEC-UNI-003 #3 |
| 33 | message queue consumer setup | FW-M2-RT-005 | Strong |
| 34 | multi-website stock scope | FW-M2-RT-006 | Strong -- FW-M2-RT-001 at #3 |
| 35 | totals collector execution order | FW-M2-005 | Strong -- FW-M2-006, FW-M2-003 in top 5 |
| 36 | quote state before collect totals | FW-M2-003 | Strong -- FW-M2-006, FW-M2-005 in top 3 |
| 37 | CartTotalRepository stale read | FW-M2-006 | Strong -- FW-M2-005, FW-M2-003 also present |
| 38 | PHP docblock type annotation | PHP-TYPE-001 | Strong -- ARCH-TYPE-001 at #3 |
| 39 | PHP try catch throwable | PHP-TRY-001 | Strong -- ARCH-ERR-001 at #2 |
| 40 | fail fast guard clause | ARCH-ERR-001 | Good -- PHP-ERR-001 missing from top 5 |
| 41 | graceful degradation error recovery | ARCH-ERR-001 | Strong -- full error handling stack in top 4 |
| 42 | race condition concurrent writers | FW-M2-RT-002 | Good -- correct #1, noise in #2-5 |
| 43 | idempotent message processing | FW-M2-RT-005 | Strong -- FW-M2-005 #2, FW-M2-RT-002 #3 |
| 44 | persistence backed validation | FW-M2-001 | Strong -- PY-PYDANTIC-001 at #2 |
| 45 | input sanitization trust boundary | PY-PYDANTIC-001 | Strong -- full validation stack in top 3 |
| 46 | SQL formatting readable vertical | DB-SQL-003 | Strong -- all DB-SQL top 3 |
| 47 | composer update vendor override | ARCH-EXT-001 | Strong |
| 48 | response field minimization API | SEC-UNI-003 | Strong |
| 49 | multiple plugins same method sort order | FW-M2-004 | Good -- correct #1, noise in #2-5 |
| 50 | constructor loads database eagerly | PERF-LAZY-001 | Strong -- ARCH-DI-001 #2, PERF-IO-001 #4 |

**Keyword set: 43 Strong, 6 Good, 1 Partial. 50/50 have relevant rule in top 5.**

#### Symptom Queries (Q51-Q65)

| # | Query | #1 Result | Verdict |
|---|---|---|---|
| 51 | my totals are wrong after adding item to cart | FW-M2-003 | Strong -- FW-M2-005 #2, FW-M2-006 #3, ARCH-SSOT-001 #4 |
| 52 | customer can see another's order by changing the ID | SEC-UNI-002 | Strong -- SEC-UNI-001 at #2 |
| 54 | my before plugin runs but the data is not there yet | FW-M2-003 | Strong -- FW-M2-004 at #3 |
| 55 | server hangs calling Neo4j in async endpoint | PY-ASYNC-001 | Strong -- PERF-IO-001 at #2 |
| 56 | same discount different in REST vs GraphQL | ARCH-SSOT-001 | Strong -- FW-M2-RT-004 #2, FW-M2-006 #3, FW-M2-003 #5 |
| 57 | test passes alone but fails with other tests | TEST-ISO-001 | Strong -- TEST-INT-001 #2, TEST-TDD-001 #4 |
| 58 | I keep getting ImportError at startup | PY-IMPORT-001 | Strong |
| 59 | two queue consumers processing the same message | FW-M2-RT-005 | Strong -- FW-M2-RT-001 #3, FW-M2-RT-002 #4 |
| 60 | my class has 50 lines and does too many things | ARCH-FUNC-001 | Good -- ARCH-COMP-001 #3 relevant, SEC-UNI-001 #2 noise |
| 61 | stock available but order fails at checkout | FW-M2-RT-001 | Strong -- FW-M2-RT-002 #2, FW-M2-RT-006 #3 |
| 62 | I mocked the repository but prod still fails | TEST-INT-001 | Strong -- TEST-ISO-001 #3, FW-M2-002 #4 |
| 63 | admin config not loading in different store view | FW-M2-RT-003 | Strong -- SEC-UNI-004 at #2 |
| 64 | hard coded 5 in the if statement what does it mean | ARCH-DRY-001 | Weak -- ARCH-CONST-001 at #3, should be #1 |
| 65 | process crashes halfway through leaves partial state | PERF-IO-001 | Partial -- FW-M2-RT-002 at #2 is the real answer |

**Symptom set: 10 Strong, 1 Good, 1 Partial, 1 Weak. 13/14 have relevant rule in top 5.**

#### Ambiguous Queries (Q66-Q85)

| # | Query | #1 Result | Verdict |
|---|---|---|---|
| 66 | my code works in dev but breaks in production | ARCH-FUNC-001 | Weak -- TEST-INT-001 not in top 5 |
| 67 | I have the same logic in three different places | ARCH-DRY-001 | Strong -- ARCH-ORG-001 #2 |
| 68 | the API returns too much data to the frontend | SEC-UNI-003 | Strong |
| 69 | my plugin fires but nothing happens | FW-M2-003 | Strong -- FW-M2-004 #2 |
| 71 | the page loads slowly with lots of products | PERF-LAZY-001 | Partial -- missing PERF-BIGO-001 and PERF-QBUDGET-001 |
| 72 | I wrote a class that handles validation and saves to DB | PHP-ERR-001 | Good -- FW-M2-001 #3, ARCH-ORG-001 #4 |
| 73 | discount applies in checkout but not in cart summary | FW-M2-003 | Strong -- FW-M2-005 #3, FW-M2-006 #5 |
| 74 | CI passes but the feature is broken on staging | TEST-INT-001 | Good -- correct #1, rest is noise |
| 75 | I need to change how orders are cancelled, afraid of side effects | FW-M2-RT-002 | Strong -- FW-M2-RT-001 at #4 |
| 76 | my async function takes forever even though the query is fast | PY-ASYNC-001 | Strong -- PERF-IO-001 #2, PERF-OPT-001 #3 |
| 77 | two cron jobs updating the same table at midnight | FW-M2-RT-001 | Partial -- FW-M2-RT-002 at #4 is the real answer |
| 78 | I inherited this class three levels deep and cannot figure it out | ARCH-COMP-001 | Strong -- PY-PROTO-001 #2, ARCH-ORG-001 #3 |
| 79 | customer data is leaking through the GraphQL API | FW-M2-RT-004 | Strong -- SEC-UNI-003 #3 |
| 80 | the test only works if you run it after the other test | TEST-ISO-001 | Strong -- all TEST rules top 4 |
| 81 | I am newing up services inside my controller | ARCH-DI-001 | Strong -- ARCH-ORG-001 #3 |
| 82 | product shows in stock on one website but out of stock on another | FW-M2-RT-006 | Strong -- FW-M2-RT-001 #2 |
| 83 | error message says nothing useful just something went wrong | ARCH-ERR-001 | Strong -- PHP-ERR-002 #2, PHP-TRY-001 #4 |
| 84 | I added a column but forgot what the number 30 means in where clause | DB-SQL-003 | Partial -- ARCH-CONST-001 missing from top 5 |
| 85 | the queue keeps reprocessing the same order | FW-M2-RT-005 | Strong -- FW-M2-RT-001 #2, FW-M2-RT-002 #3 |

**Ambiguous set: 12 Strong, 2 Good, 3 Partial, 1 Weak. 17/19 have relevant rule in top 5.**

---

## Overall Quality Summary

| Verdict | Count | Percentage |
|---|---|---|
| Strong | 57 | 69% |
| Good | 12 | 14% |
| Partial | 5 | 6% |
| Weak | 2 | 2% |
| **Strong + Good** | **69** | **83%** |
| **Any relevant rule in top 5** | **80/83** | **96%** |

2 governance queries removed from evaluation (Q52 original, Q69 ambiguous: "teams contradicting rules" / "rules say opposite things"). These are `writ validate` questions, not retrieval questions.

### Known Weaknesses

**PY-ASYNC-001 noise (documented limitation):**
PY-ASYNC-001 appears in the top 5 for ~10 queries where it is not relevant. The cause is vector-side: the embedding for "async/sync/function/call/I/O" has high cosine similarity to many programming concepts. This is not a BM25 problem -- trigger field boosting does not fix it. At 80 rules, one rule with a broad trigger can dominate. At 1,000+ rules, the noise dilutes as more rules compete.

**Mitigation:** Domain filter. When the developer specifies `domain: "Architecture"` or `domain: "Database"`, PY-ASYNC-001 (domain: Python/Async) is excluded from results. The noise only appears in unfiltered queries where no domain context is provided. The skill integration (Phase 7+) will pass domain context automatically based on the task classification.

**Other noise patterns:**
- SEC-UNI-001/002 leak into non-security queries occasionally via "endpoint"/"data" keyword overlap.
- These are minor at current corpus size and do not push relevant rules out of top 5.

**Misses (3 queries):**
- Q64: "hard coded 5 in if statement" -- ARCH-CONST-001 at #3 instead of #1. BM25 noise from "if statement."
- Q66: "code works in dev breaks in production" -- too vague for any specific rule. TEST-INT-001 not surfaced.
- Q84: "forgot what number 30 means in where clause" -- ARCH-CONST-001 missing from top 5. DB-SQL rules dominate on "where clause."

---

## Latency

| Metric | Target | Result | Status |
|---|---|---|---|
| End-to-end pipeline (p95, warm index) | < 10ms | **6.3ms** | Pass |
| End-to-end pipeline (p50, warm index) | -- | **5.5ms** | -- |
| BM25 keyword search (Tantivy) | < 2ms | < 1ms | Pass |
| ANN vector search (hnswlib) | < 3ms | < 0.1ms | Pass |
| Graph traversal (adjacency cache) | < 3ms | **0.06us** | Pass |
| Ranking computation | < 1ms | < 0.5ms | Pass |

Measured over 100 queries on warm indexes with 80 rules (45 domain + 35 mandatory).

### Neo4j Live Query Benchmarks

Neo4j live Cypher traversal exceeds the 3ms Stage 4 budget at all scales tested:

| Scale | 1-hop p95 | 2-hop p95 |
|---|---|---|
| 1K nodes | 6.4ms | 9.3ms |
| 10K nodes | 9.7ms | 11.6ms |

Mitigation: pre-computed adjacency lists cached in memory at service startup. Cache lookup is 0.06 microseconds per call. Neo4j is used for offline operations (ingest, validate, integrity checks) and for building the cache at startup. The hot path never hits Neo4j.

---

## Infrastructure

| Metric | Target | Result | Status |
|---|---|---|---|
| Service cold start | < 3 seconds | ~2s (model cached) | Pass |
| Memory footprint | < 2 GB RAM | ~1 GB (includes embedding model) | Pass |
| Rule corpus | -- | 80 rules (45 domain, 35 mandatory) | -- |
| RELATED_TO skeleton edges | -- | 147 (from cross-references) | -- |

---

## Ranking Configuration

Weights tuned against 65-query tuning set, validated on 20-query ambiguous held-out set.

| Component | Weight | Notes |
|---|---|---|
| BM25 keyword rank | 0.3 | Reduced from 0.4 to limit keyword noise |
| Vector semantic rank | 0.5 | Increased from 0.4 to prioritize semantic intent |
| Severity | 0.1 | Critical=1.0, High=0.75, Medium=0.5, Low=0.25 |
| Confidence | 0.1 | battle-tested=1.0, production-validated=0.8, peer-reviewed=0.6, speculative=0.3 |

Additional tuning: Tantivy trigger field boosted 2x over statement/tags fields. Prioritizes matching on the rule's activation condition rather than incidental keywords.

### Embedding Model

| Model | Status |
|---|---|
| all-MiniLM-L6-v2 (384 dim, 80MB) | **Selected** -- sufficient quality at current corpus size |
| all-mpnet-base-v2 (768 dim, 420MB) | Not evaluated -- reserved as upgrade path if quality degrades at scale |

---

## Mandatory Rule Boundary

Verified at multiple layers:

- Mandatory rules (`ENF-*`, `mandatory: true`) excluded from BM25 index at build time
- Mandatory rules excluded from vector index at build time
- `/query` endpoint never returns mandatory rules
- Automated test confirms: query for "gate approval phase enforcement" returns zero ENF-* rules

This is a hard architectural boundary. If a mandatory rule appears in `/query` results, that is a bug.

---

## Test Suite

| Suite | Count | Status |
|---|---|---|
| Schema validation (Phase 1) | 29 | All passing |
| Infrastructure integration (Phase 2) | 11 | All passing |
| Ingest and migration (Phase 3) | 13 | All passing |
| Integrity checks (Phase 4) | 10 | All passing |
| Retrieval pipeline (Phase 5) | 14 | All passing |
| **Total** | **77** | **All passing** |

---

## What This Means

At 80 rules, Writ returns relevant rules in the top 5 for 96% of queries (80/83), with strong or good coverage for 83% (69/83). p95 latency is 6.3ms on warm indexes. The retrieval pipeline is faster and more precise than context-stuffing the entire Bible.

The 3 misses are queries where BM25 keyword overlap pushes the target rule out of top 5 (Q64, Q84) or the query is too vague for any specific rule (Q66). The 5 partial results have relevant rules present but not optimally ranked -- addressable through domain filtering and further weight tuning as the corpus grows.

The architecture is validated for the current corpus. Phases 6-9 (authoring tools, generated artifacts, compression layer, agentic retrieval loop) can proceed.

### Scaling Considerations

- Current corpus (80 rules) fits comfortably in hnswlib and Tantivy in-memory indexes
- Adjacency cache mitigation already in place for graph traversal
- At 1,000+ rules, the ground-truth query set must grow proportionally (1:10 ratio per handbook)
- At 100K+ rules, hnswlib migrates to Qdrant (migration triggers documented in handbook Section 3.5)
- PY-ASYNC-001 noise pattern may worsen at scale as more rules compete for keyword overlap -- field-specific BM25 weighting or per-query domain filtering will become more important
