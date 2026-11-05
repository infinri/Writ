# Writ V2 — authoritative documentation

**Branch:** v2/architecture-rebuild  
**Status:** target architecture accepted in principle; implementation pending  
**Baseline:** main commit ebdafe5450b201339f81e717ed104e6bcfd3fe3e  
**Revision:** 2026-11-05

This documentation replaces the prior V1 docs (including old ADRs, reference pages, pressure-run records and HTML architecture assets) **on this branch**. Nothing in the old docs tree is normative for V2. Existing runtime code remains V1 reference, not proof that any described V2 behavior exists.

## Canonical reading order

1. [Master implementation specification](implementation/MASTER-IMPLEMENTATION-PLAN.md) — complete build and release program.
2. [System architecture](architecture/architecture.md) and [repository layout](architecture/repository-layout.md).
3. [Execution & state](architecture/execution-and-state.md) and [authorization & approvals](architecture/authorization-and-approvals.md).
4. [Retrieval & memory](architecture/retrieval-and-memory.md), [data contracts](architecture/data-model.md), and [host adapters](architecture/host-adapters.md).
5. [Security invariants](security/invariants.md) and [threat model](security/threat-model.md).
6. [Testing, benchmarks & acceptance](testing/verification-and-benchmarks.md).
7. [V1 disposition and migration](audit/V1-ARCHITECTURE-BASELINE.md), [migration strategy](migration/v1-to-v2.md).
8. [Operations](operations/deployment.md), [extensions](architecture/extensibility.md), [observability](architecture/observability.md).
9. [ADRs](decisions/) for decisions and open questions.
10. [HTML architecture portal](architecture/index.html) — a visual guide, not an alternate source of truth.

## Decision language

- **DECIDED:** architectural invariant or boundary fixed for the V2 design.
- **PROVISIONAL:** a component pattern chosen for prototype, subject to acceptance measurements.
- **DEFERRED:** consciously not part of the initial runtime.
- **OPEN:** requires experiment or human architecture decision.
- **LEGACY:** current V1 behavior only; must not be described as V2 implementation.

## Key choices

**DECIDED:** one enforceable authority for privileged side effects; host-agnostic normalized actions; typed immutable events; identity-bound human approvals; independently versioned policies; tenant-scoped retrieval and memory; bounded graph working sets; local-first permission path; operational state separate from knowledge graph; granular ports/adapters.

**PROVISIONAL:** Rust versus Go kernel. Preserve the ability to use either during M0/M1; choose with benchmarks, not aesthetics.

**DEFERRED:** WASM plugins, remote workflow engine, message broker and Kubernetes until an acceptance test demonstrates a need.

## Contracts

JSON Schema documents under contracts/ are **initial design contracts**, not production wire compatibility promises. Version them and publish golden examples before integrating clients. A field named 'principal' does not authenticate the caller; the host adapter must provide independently verified identity.

## Staleness control

Any code PR altering a contract must update its schema, corresponding markdown page, HTML projection, tests and the relevant ADR. Documentation-build CI must check links, JSON schema validity and contract examples. The HTML site is static and intentionally has no database-generated V1 graph payload.

## No hidden change to main

All V1 design artifacts remain reachable in the original main branch and Git history. This branch has no implicit migration, no automatic cleanup of production state and no claim that V1 hooks or CI are V2-ready.
