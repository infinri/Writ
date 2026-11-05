# Writ V2 architecture and change governance

## Authority

The [implementation plan](docs/implementation/MASTER-IMPLEMENTATION-PLAN.md) is the roadmap. The [security invariants](docs/security/invariants.md) and versioned wire contracts are normative for behavior. Accepted ADRs describe design choices; they do not supersede security invariants without an explicitly approved revision. The docs/architecture HTML pages are explanatory projections of these normative sources.

## Decision process

A major architecture decision documents: problem, constraints, viable options, scored evidence, selected choice, rejected alternatives, operational cost, rollback, and the milestone responsible. Security-sensitive changes require a human reviewer and reproducible adversarial tests.

## Ownership

The execution kernel owns state-transition semantics. The policy registry owns policy version and rule provenance. The broker owns side effects and receipts. Host adapters own host-specific delivery and authentication handoff. The graph module owns knowledge relationships, not authorization. Operational persistence owns transaction and approval state. No component may take another component's authority by reading its derived cache.

## Breaking changes

Change contracts via explicit versioning, migration fixtures and a bounded deprecation period. Never silently relax authorization or 'fix' an incident by adding a second independent bypass path.

## Historic V1

V1 governance documentation is preserved on main. V2 documentation is normative only for the rebuild branch until implemented and released.

## Domain packs and provider compatibility

The platform's security invariants apply identically regardless of model, host or Capability Pack. A domain pack may require stronger evaluation or human review, but cannot relax organization policy. Provider-specific capability claims must be verified by adapter tests, and domain artifacts/rubrics must be versioned before release. [ADR-0006](docs/decisions/ADR-0006-model-and-domain-independence.md) is accepted as a target architecture, not a declaration of shipped integrations.
