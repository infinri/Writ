# ADR-0001: Clean rebuild and single execution authority

**Status:** ACCEPTED AS TARGET ARCHITECTURE · 2026-11-05

## Context

V1 is coupled across 52 host hook command registrations, multiple shell/Python fallback paths, file-based approval credentials, graph-derived and prompt-only rules, mutable session state and host-specific semantics. The corpus may claim blocking validation where the host lifecycle is advisory. Tests are valuable but do not prove an end-to-end effect boundary.

## Decision

Rebuild into a typed local-first, host-agnostic execution kernel, separate policy registry, trusted capability broker, versioned state/approval ledger, graph intelligence and host adapters. Enforced privileges flow through exactly one broker. Advisory retrieval never grants rights. Preserve V1 data and golden tests via explicit importers, not direct code porting.

## Alternatives

- **Literal Rust translation of V1:** rejected; carries confusing authority and duplicated host-specific control points forward.
- **Keep V1 plus more hooks:** rejected for enterprise execution; creates more heterogeneous enforcement surfaces.
- **Central remote orchestration for every tool call:** rejected; adds network/failure dependencies on a low-latency local boundary.
- **Clean separation with compatibility importer:** selected; higher up-front redesign cost, much stronger contract ownership.

## Consequences

This branch's V1 code remains legacy-only until explicitly migrated. A docs-first branch is not deployable as a new engine. Operator must not equate a host's 'deny' message with an OS sandbox. Every component has a contract, tests, performance budget and explicit failure posture. No blanket enterprise security claim until integration and adversarial tests pass.

## Revisit conditions

Evidence of a simpler design with equivalent authorization correctness, local latency and tenant isolation. Any change must preserve [invariants](../security/invariants.md).
