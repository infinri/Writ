# Python Coding Standards

## Purpose

This document defines **Python-specific coding standards** for the Phaselock codebase and any Python project governed by Phaselock rules. These complement the universal architecture rules (ARCH-*) and performance rules (PERF-*) with Python-specific enforcement.

---

<!-- RULE START: PY-ASYNC-001 -->
## Rule PY-ASYNC-001: Async All the Way

**Domain**: Python / Async
**Severity**: Critical
**Scope**: module

### Trigger
Mixing sync and async in the same call chain. Any `await` caller that calls a sync function performing I/O.

### Statement
Async call chains must use async I/O end-to-end. A sync I/O call inside an async function blocks the event loop, defeating the purpose of async and violating PERF-IO-001.

### Violation (bad)
```python
async def handle_query(request: QueryRequest) -> QueryResponse:
    # neo4j.Session.run() is sync -- blocks the event loop
    session = driver.session()
    result = session.run("MATCH (r:Rule) RETURN r")
    ...
```

### Pass (good)
```python
async def handle_query(request: QueryRequest) -> QueryResponse:
    # neo4j.AsyncSession.run() is async -- does not block
    async with driver.async_session() as session:
        result = await session.run("MATCH (r:Rule) RETURN r")
    ...
```

### Enforcement
ruff rule ASYNC100 (blocking-http-call-in-async-function). Code review for sync DB driver usage inside async functions.

### Rationale
A single sync call in an async chain blocks the entire event loop thread. In a FastAPI server handling concurrent requests, one sync Neo4j call blocks all other requests until it completes. The Neo4j Python driver provides both sync and async sessions -- always use AsyncSession in async code.
<!-- RULE END: PY-ASYNC-001 -->

---

<!-- RULE START: PY-IMPORT-001 -->
## Rule PY-IMPORT-001: No Circular Imports

**Domain**: Python / Module Design
**Severity**: High
**Scope**: module

### Trigger
Any import that would fail at module load time, or any `TYPE_CHECKING`-guarded import used at runtime (not just for annotations).

### Statement
Circular imports are forbidden. If module A imports from module B and module B imports from module A, extract shared types into a third module that both import from. `TYPE_CHECKING`-guarded imports may be used for annotation-only imports but must never be used at runtime.

### Violation (bad)
```python
# graph/db.py
from retrieval.pipeline import PipelineConfig  # imports from retrieval

# retrieval/pipeline.py
from graph.db import GraphConnection  # imports from graph -- circular
```

### Pass (good)
```python
# graph/schema.py -- shared types
class PipelineConfig(BaseModel): ...
class GraphConnection: ...

# graph/db.py
from graph.schema import GraphConnection

# retrieval/pipeline.py
from graph.schema import PipelineConfig
```

### Enforcement
Python raises `ImportError` on circular imports at module load time. ruff rule TID252 (banned-module-level-imports). Integration tests catch load-time failures.

### Rationale
Circular imports cause `ImportError` at module load, produce `None` references to partially-loaded modules, and indicate that the module boundary is wrong. Extracting shared types to a common module fixes the dependency direction.
<!-- RULE END: PY-IMPORT-001 -->

---

<!-- RULE START: PY-PROTO-001 -->
## Rule PY-PROTO-001: Protocol Over ABC for Interfaces

**Domain**: Python / Type System
**Severity**: Medium
**Scope**: module

### Trigger
Abstract base class used solely to define an interface (all methods abstract, no concrete implementation).

### Statement
Pure interfaces (no implementation) must use `typing.Protocol`, not `abc.ABC`. Protocol enables structural subtyping -- a class satisfies the interface by having the right methods, without inheriting from the protocol. ABC is appropriate when the base class provides shared implementation (template method pattern).

### Violation (bad)
```python
from abc import ABC, abstractmethod

class VectorStore(ABC):
    @abstractmethod
    def search(self, vector: list[float], k: int) -> list[ScoredResult]:
        ...
```

### Pass (good)
```python
from typing import Protocol

class VectorStore(Protocol):
    def search(self, vector: list[float], k: int) -> list[ScoredResult]:
        ...
```

### Enforcement
Code review. mypy verifies Protocol conformance at type-check time.

### Rationale
Protocol enables structural subtyping. The hnswlib-to-Qdrant swap in `embeddings.py` (Section 3.5 of the RAG handbook) is exactly this pattern: the pipeline depends on the Protocol shape, not a class hierarchy. The hnswlib implementation and the future Qdrant implementation both satisfy `VectorStore` without inheriting from it. This supports ARCH-COMP-001 (composition over inheritance).
<!-- RULE END: PY-PROTO-001 -->

---

<!-- RULE START: PY-PYDANTIC-001 -->
## Rule PY-PYDANTIC-001: Pydantic for All External Data Boundaries

**Domain**: Python / Data Validation
**Severity**: High
**Scope**: file

### Trigger
Data crossing a trust boundary (HTTP request, JSON file, DB result mapping, CLI input) without Pydantic model validation.

### Statement
All data entering the system from external sources must be validated through a Pydantic model before use. Direct dictionary access on unvalidated external data is forbidden.

### Violation (bad)
```python
data = json.loads(request.body)
rule_id = data["rule_id"]       # KeyError if missing, no type validation
domain = data["domain"]         # could be any type
```

### Pass (good)
```python
payload = QueryRequest.model_validate_json(request.body)
rule_id = payload.rule_id       # validated str, guaranteed present
domain = payload.domain         # validated against allowed values
```

### Enforcement
Code review. mypy with pydantic plugin catches unvalidated dict access patterns. FastAPI enforces this automatically for endpoint parameters.

### Rationale
Unvalidated external data is the root cause of injection attacks, type errors at runtime, and missing-key crashes in production. Pydantic validation at the boundary means all downstream code can trust the types and presence of fields. This complements ENF-SEC-001 (input validation at trust boundaries).
<!-- RULE END: PY-PYDANTIC-001 -->
