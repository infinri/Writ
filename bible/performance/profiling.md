# Performance & Profiling

## Purpose

This document defines **performance principles and optimization guidelines** for maintaining efficient, scalable code.

---

<!-- RULE START: PERF-BIGO-001 -->
## Rule PERF-BIGO-001: Algorithm Complexity Awareness

**Domain**: Performance
**Severity**: High
**Scope**: file

### Trigger
When writing a loop that iterates over a collection, or when nesting loops, or when calling a method inside a loop that itself iterates over a collection.

### Statement
Nested loops over the same or related datasets must be justified. O(n^2) or worse is a violation unless the dataset size is bounded by a documented constant (e.g., "max 20 items per order" per business rule).

### Violation (bad)
```php
// O(n*m) -- scans all items for every order
foreach ($orders as $order) {
    foreach ($allItems as $item) {
        if ($item->getOrderId() === $order->getId()) {
            $order->addItem($item);
        }
    }
}
```

### Pass (good)
```php
// O(n+m) -- index first, then lookup
$itemsByOrder = [];
foreach ($allItems as $item) {
    $itemsByOrder[$item->getOrderId()][] = $item;
}
foreach ($orders as $order) {
    $order->setItems($itemsByOrder[$order->getId()] ?? []);
}
```

### Enforcement
Per-slice findings table (ENF-POST-006) must justify any nested iteration and declare the expected dataset size. Code review.

### Rationale
A seemingly simple nested loop can bring production systems to a halt. O(n^2) on 10 items is fine; O(n^2) on 10,000 items takes 100 million iterations.
<!-- RULE END: PERF-BIGO-001 -->

---

<!-- RULE START: PERF-OPT-001 -->
## Rule PERF-OPT-001: Optimization Requires Measurement

**Domain**: Performance
**Severity**: Medium
**Scope**: module

### Trigger
When the AI proposes a performance optimization (caching, query rewrite, algorithm change, raw SQL instead of repository) without evidence that the current code is a measured bottleneck.

### Statement
Do not optimize without measurement. Optimizations must cite the profiling evidence or performance requirement that justifies them. "For performance" is not a justification.

### Violation (bad)
```
// AI rewrites repository call to raw SQL:
"Replacing ProductRepository::getById() with direct SQL query for performance."
// No profiling data. No evidence the repository call is slow.
```

### Pass (good)
```
// AI cites evidence:
"ProductRepository::getList() in the loop generates 3 queries per call (entity + EAV + extension).
With 500 products, this is 1500 queries. Replacing with a single collection query reduces to 3.
Evidence: MySQL slow query log shows this endpoint averaging 4.2s."
```

### Enforcement
ENF-OPS-001 (operational claim validation) -- any performance claim requires evidence. Code review.

### Rationale
Premature optimization leads to complex, hard-to-maintain code that bypasses service contracts. Optimization without measurement often targets the wrong bottleneck while introducing coupling to the persistence layer.
<!-- RULE END: PERF-OPT-001 -->

---

<!-- RULE START: PERF-LAZY-001 -->
## Rule PERF-LAZY-001: Defer Expensive Loading Until First Access

**Domain**: Performance
**Severity**: Medium
**Scope**: file

### Trigger
When a constructor or early method eagerly loads data (DB query, API call, file read) that may not be used by every code path through the class.

### Statement
Defer expensive resource loading until first access. Use instance-level caching (`$this->cache ??= $this->load()`) to avoid repeated loads within the same request.

### Violation (bad)
```php
public function __construct(
    private readonly OrderRepositoryInterface $orderRepository,
    private readonly int $orderId
) {
    // Loads on construction -- even if getOrder() is never called
    $this->order = $this->orderRepository->getById($orderId);
}
```

### Pass (good)
```php
private ?OrderInterface $order = null;

public function getOrder(): OrderInterface
{
    if ($this->order === null) {
        $this->order = $this->orderRepository->getById($this->orderId);
    }
    return $this->order;
}
```

### Enforcement
Per-slice findings table (ENF-POST-006) must verify constructors do not perform DB queries or API calls. Code review.

### Rationale
Eager loading of unused resources wastes memory and CPU. A class injected into 10 different contexts should not trigger a DB query on construction when only 2 of those contexts call the method that needs the data.
<!-- RULE END: PERF-LAZY-001 -->

---

<!-- RULE START: PERF-QBUDGET-001 -->
## Rule PERF-QBUDGET-001: Query Budget Declaration Gate

**Domain**: Performance
**Severity**: Critical
**Scope**: module

### Trigger
When requirements specify a DB query budget (e.g., "must not add more than N queries per request"), or when a feature involves repository or collection calls inside a loop.

### Statement
When a query budget exists, produce a Query Budget Plan before writing implementation code. The plan must list each expected DB query, worst-case count, caching strategy, fallback approach, and measurement plan.

### Violation (bad)
```
// No query budget analysis -- assumes 1 repo call = 1 query
"getById() is called once per item. With 10 items, that's 10 queries."
// WRONG -- getById() may trigger 3-5 queries (entity + EAV + extension attributes + plugins)
```

### Pass (good)
```
Query Budget Plan:
1. Expected: getList() with filter -- 1 collection query + 1 count query = 2 queries
2. Worst-case: EAV attributes loaded = +1 query. Extension attributes = +1 per extension.
   Total worst-case: 4 queries for up to 100 items.
3. Caching: Results cached in instance variable per request. Invalidated on save.
4. Fallback: If budget exceeded, use resource model query selecting only needed columns.
5. Measurement: Integration test with DB query counter asserting <= 5 queries.
```

### Required plan sections
1. **Expected query count**: Each DB query with the repository method or resource model call that triggers it.
2. **Worst-case analysis**: Repository `getList()` calls may trigger multiple underlying queries. Do not assume 1 call = 1 query.
3. **Caching strategy**: Per-request caching? Invalidation conditions?
4. **Fallback if budget exceeded**: Specialized resource model query as a justified trade-off.
5. **Measurement plan**: How query count will be verified (MySQL log, profiler, integration test with counter).

### Enforcement
Phase B domain invariant must include query budget when requirements specify one. Per-slice findings table (ENF-POST-006). ENF-OPS-001 (operational claim validation).

### Rationale
Repository and service contract abstractions hide query complexity. A single `getList()` call can generate 2-5+ queries depending on entity type, collection implementation, and extension attributes. Assuming query behavior without verification is the primary source of performance budget violations in Magento custom modules.
<!-- RULE END: PERF-QBUDGET-001 -->

---

<!-- RULE START: PERF-IO-001 -->
## Rule PERF-IO-001: No Synchronous I/O in Hot Path

**Domain**: Performance
**Severity**: Critical
**Scope**: module

### Trigger
Any blocking I/O call (file read, network request, DB query) in a function reachable from a request handler or time-critical code path.

### Statement
Functions in the hot path must not perform synchronous I/O. Data must be loaded at startup and served from memory, or accessed via async I/O.

**Hot path definition (per project):**
- Phaselock: anything reachable from a FastAPI endpoint
- Magento: anything in the request/response cycle

### Violation (bad)
```python
@app.post("/query")
async def handle_query(request: QueryRequest):
    config = open("config.json").read()  # sync file I/O in hot path
    ...
```

### Pass (good)
```python
# Config loaded at startup
config = load_config()

@app.post("/query")
async def handle_query(request: QueryRequest):
    # config already in memory -- no I/O
    ...
```

### Enforcement
Code review. Grep for `open(`, `requests.get`, sync DB calls inside `async def` functions. See also PY-ASYNC-001 for Python-specific async enforcement.

### Rationale
Synchronous I/O in a hot path blocks the event loop (async servers) or the request thread (sync servers). At the 10ms pipeline target, a single 50ms file read blows the entire latency budget. See also PERF-LAZY-001 for deferred loading patterns.
<!-- RULE END: PERF-IO-001 -->
