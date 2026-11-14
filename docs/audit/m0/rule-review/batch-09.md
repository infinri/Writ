# Rule review batch-09: performance (19 rules)

Source: `writ-corpus.cypher` at `ebdafe5450b201339f81e717ed104e6bcfd3fe3e`. Proposed dispositions are the automated first pass (D-004), not verdicts. Record decisions in [batch-09.csv](batch-09.csv).

## PERF-ASYNC-001

- **Domain / severity / mandatory:** performance / high / false
- **Statement:** I/O-bound operations use async / non-blocking calls, not synchronous blocking. The event loop is not stalled waiting for HTTP, DB, or file I/O.
- **Trigger:** When implementing I/O-bound operations in a runtime that supports async (Python asyncio, Node, Tokio, Kotlin coroutines).
- **V1 enforcement text:** Code review. Linter rules (asyncio-blocker check, eslint async-no-floating-promises).
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** ARCH-ASYNC-002, PERF-BATCH-001, PERF-IO-001, PY-ASYNC-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## PERF-BATCH-001

- **Domain / severity / mandatory:** performance / medium / false
- **Statement:** Independent I/O calls run in parallel. `Promise.all`, `asyncio.gather`, `tokio::join!`, goroutine + WaitGroup. Serial awaiting of independent calls is a violation.
- **Trigger:** When making multiple independent I/O calls within a request handler.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 1 file(s): `writ/graph/db/rule_store.py`
- **Rule id referenced in V1 tests:** 9 file(s)
- **Related rules (conflict candidates):** ARCH-ASYNC-001, PERF-ASYNC-001, PERF-BIGO-001, PERF-QUERY-001, PERF-QUERY-003
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## PERF-BIGO-001

- **Domain / severity / mandatory:** performance / high / false
- **Statement:** Nested loops over the same or related datasets must be justified. O(n^2) or worse is a violation unless the dataset size is bounded by a documented constant (e.g., "max 20 items per order" per business rule).
- **Trigger:** When writing a loop that iterates over a collection, or when nesting loops, or when calling a method inside a loop that itself iterates over a collection.
- **V1 enforcement text:** Code review. Related: PERF-BATCH-001.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 1 file(s): `writ/compression/clusters.py`
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** PERF-BATCH-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## PERF-BUNDLE-001

- **Domain / severity / mandatory:** performance / medium / false
- **Statement:** Frontend assets are bundled and minified for production. Unminified, un-bundled, or unhashed source files are not served in production. Build tooling (webpack, vite, rollup, esbuild) produces the bundle; CI gates the asset checks.
- **Trigger:** When serving JavaScript or CSS to web clients.
- **V1 enforcement text:** CI gate (asset-size budget; lighthouse score).
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** PERF-IMAGE-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## PERF-CACHE-001

- **Domain / severity / mandatory:** performance / high / false
- **Statement:** Cache keys include every parameter that affects the cached value. A cache key that omits user_id is a violation: it lets one user's response be returned to another user.
- **Trigger:** When caching the result of a function or query that varies by user, tenant, locale, or other parameter.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** SEC-AUTHZ-TENANT-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## PERF-CACHE-002

- **Domain / severity / mandatory:** performance / high / false
- **Statement:** Cache entries have an explicit TTL. Indefinite caching without an invalidation strategy is a violation. The TTL is short enough that stale data is bounded; the invalidation strategy is short enough that fresh data is bounded.
- **Trigger:** When setting a cache entry.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** PERF-CACHE-003, PERF-CACHE-004, PERF-MEM-002, RESEARCH-STALENESS-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## PERF-CACHE-003

- **Domain / severity / mandatory:** performance / medium / false
- **Statement:** Cache invalidation on write is explicit. The write path explicitly invalidates or refreshes the affected cache entries. 'It will expire eventually' is not a strategy.
- **Trigger:** When writing data that has a cached representation.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** PERF-CACHE-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## PERF-CACHE-004

- **Domain / severity / mandatory:** performance / medium / false
- **Statement:** Cache stampede is mitigated: when a popular cache entry expires, many concurrent callers must not all hit the underlying store at once. Mechanisms: short locks on regenerate, probabilistic early expiration, background refresh.
- **Trigger:** When implementing caches that may be hit by many concurrent requests after an entry expires.
- **V1 enforcement text:** Code review. Cache libraries (cachetools, django-cacheops) implement stampede protection.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** ARCH-STATE-001, PERF-CACHE-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## PERF-IMAGE-001

- **Domain / severity / mandatory:** performance / low / false
- **Statement:** Images are served in modern formats (WebP, AVIF) when supported, sized appropriately for the rendered display, and lazy-loaded for off-screen content. Original-resolution PNGs in image elements rendered at 200x200 are violations.
- **Trigger:** When serving images to web clients.
- **V1 enforcement text:** Image-optimization pipeline (next/image, Cloudinary, ImgIX).
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** PERF-BUNDLE-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## PERF-IO-001

- **Domain / severity / mandatory:** performance / critical / false
- **Statement:** Functions in the hot path must not perform synchronous I/O. Data must be loaded at startup and served from memory, or accessed via async I/O. **Hot path definition (per project):** - Phaselock: anything reachable from a FastAPI endpoint - Magento: anything in the request/response cycle
- **Trigger:** Any blocking I/O call (file read, network request, DB query) in a function reachable from a request handler or time-critical code path.
- **V1 enforcement text:** Code review. Grep for `open(`, `requests.get`, sync DB calls inside `async def` functions. See also PY-ASYNC-001 for Python-specific async enforcement.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 9 file(s): `bin/lib/analyzers-regex.sh`, `writ/graph/db/_common.py`, `writ/retrieval/pipeline.py`, `writ/retrieval/traversal.py`, `writ/server/__init__.py` ...
- **Rule id referenced in V1 tests:** 10 file(s)
- **Related rules (conflict candidates):** PERF-ASYNC-001, PERF-LAZY-001, PY-ASYNC-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## PERF-LAZY-001

- **Domain / severity / mandatory:** performance / medium / false
- **Statement:** Defer expensive resource loading until first access. Use instance-level caching (`$this->cache ??= $this->load()`) to avoid repeated loads within the same request.
- **Trigger:** When a constructor or early method eagerly loads data (DB query, API call, file read) that may not be used by every code path through the class.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 1 file(s): `writ/retrieval/pipeline.py`
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** PERF-IO-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## PERF-MEM-001

- **Domain / severity / mandatory:** performance / high / false
- **Statement:** Large collections are processed via streaming/iteration, not loaded entirely into memory. Generator expressions, iterator protocols, server-side cursors, chunked reads. Loading the full result set when the consumer needs one row at a time is a violation.
- **Trigger:** When processing collections that may grow with input size: rows from a query, lines from a file, items from an API.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** PERF-MEM-002, PERF-QUERY-004
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## PERF-MEM-002

- **Domain / severity / mandatory:** performance / medium / false
- **Statement:** Object references are released after use. Long-running processes do not retain unbounded structures (caches without eviction, growing dicts, accumulating lists). Memory-leak patterns are reviewed and bounded.
- **Trigger:** When writing long-running processes (workers, daemons, web servers, ML training loops).
- **V1 enforcement text:** Memory profiling. Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** PERF-CACHE-002, PERF-MEM-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## PERF-OPT-001

- **Domain / severity / mandatory:** performance / medium / false
- **Statement:** Do not optimize without measurement. Optimizations must cite the profiling evidence or performance requirement that justifies them. "For performance" is not a justification.
- **Trigger:** When the AI proposes a performance optimization (caching, query rewrite, algorithm change, raw SQL instead of repository) without evidence that the current code is a measured bottleneck.
- **V1 enforcement text:** ENF-OPS-001 (operational claim validation) -- any performance claim requires evidence. Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** ENF-OPS-001, TEST-PERF-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## PERF-QBUDGET-001

- **Domain / severity / mandatory:** performance / critical / false
- **Statement:** When a query budget exists, produce a Query Budget Plan before writing implementation code. The plan must list each expected DB query, worst-case count, caching strategy, fallback approach, and measurement plan.
- **Trigger:** When requirements specify a DB query budget (e.g., "must not add more than N queries per request"), or when a feature involves repository or collection calls inside a loop.
- **V1 enforcement text:** ENF-OPS-001 (operational claim validation).
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 10 file(s)
- **Related rules (conflict candidates):** ENF-OPS-001, PERF-QUERY-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## PERF-QUERY-001

- **Domain / severity / mandatory:** performance / critical / true
- **Statement:** N+1 database query patterns are forbidden. When loading a list of N parents and then accessing each parent's children inside the loop, use joins, eager loading (Django select_related/prefetch_related, SQLAlchemy joinedload, Eloquent with()), or batch fetching. One query per item in a loop is a violation.
- **Trigger:** When iterating over a collection and accessing a related entity inside the loop body.
- **V1 enforcement text:** Mechanically enforced by bin/run-analysis.sh::analyze_performance_n_plus_one: regex flags loop bodies that contain ORM access (`.filter`, `.get`, `.query`, `.find`, `.where`, `.first`, `.fetch`, `.where(...).first`) where the loop variable is referenced in the query. Heuristic; reviewer judgment closes false positives.
- **V1 mechanical path:** bin/run-analysis.sh::analyze_performance_n_plus_one (resolves at baseline: yes)
- **Rule id referenced in V1 code:** 1 file(s): `bin/lib/analyzers-regex.sh`
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** PERF-BATCH-001, PERF-QBUDGET-001, PERF-QUERY-002
- **Proposed disposition:** candidate_enforced (V1 binding resolves at baseline; must be rebound to a V2 validator); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## PERF-QUERY-002

- **Domain / severity / mandatory:** performance / high / false
- **Statement:** Columns used in WHERE clauses or JOIN conditions for non-trivial table sizes must have indexes. Full-table scans on production-sized tables are violations. The migration that introduces the query is accompanied by the migration that creates the index.
- **Trigger:** When writing database queries that filter (WHERE) or join (JOIN) on a column.
- **V1 enforcement text:** Schema review during migration PRs. Query plans (EXPLAIN) for new queries.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** PERF-QUERY-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## PERF-QUERY-003

- **Domain / severity / mandatory:** performance / medium / false
- **Statement:** Queries specify the columns needed, not `SELECT *`. Wide rows transferred over the network when the consumer needs three fields wastes bandwidth, memory, and ORM hydration time.
- **Trigger:** When writing SELECT queries against any table.
- **V1 enforcement text:** Code review. Related: PERF-BATCH-001.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** PERF-BATCH-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## PERF-QUERY-004

- **Domain / severity / mandatory:** performance / medium / false
- **Statement:** List endpoints require pagination with a maximum page size. Unbounded result sets are violations. Pagination is parameterized (limit/offset, cursor, or page/per_page); the default page size is small (10-25); the maximum is bounded (100-200).
- **Trigger:** When implementing list endpoints that return collections.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** API-PAGINATION-001, API-PAGINATION-002, PERF-MEM-001, SEC-RATE-QUERY-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_
