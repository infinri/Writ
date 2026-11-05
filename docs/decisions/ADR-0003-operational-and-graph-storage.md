# ADR-0003: Separate transaction state from graph intelligence

**Status:** ACCEPTED BOUNDARY; storage implementation provisional · 2026-11-05

## Decision

Neo4j is the intelligence/provenance relationship store. A transactional operational store owns approvals, policy pins, state revisions, effects, idempotency and authoritative audit linkage. Local profile evaluates SQLite WAL; enterprise profile evaluates PostgreSQL with explicit HA and tenant privileges. Read-only retrieval index generations are derived and invalidatable.

## Why

Graph traversals and semantic scoring are not appropriate synchronous dependencies for each file-write permission. Approval consumption and effect reservation need transactional semantics and reliable replay/recovery. Keeping authority out of graph content also prevents retrieved evidence from accidentally becoming permissions.

## Rejected

Neo4j as single storage authority (unnecessary hot-path dependency and muddled trust); local JSON file token as enterprise approval (difficult atomicity and credentials separation); Redis as canonical authorization storage (persistence/transaction semantics do not match requirements).

## Validation

Two concurrent grant consumes yield at most one valid use; duplicate effect keys never dispatch twice; graph outage leaves local policy evaluation safe; tenant-scoped graph hop never leaks memory. Storage choice finalized at M3/M4 with benchmarks and failure tests.
