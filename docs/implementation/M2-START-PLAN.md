# M2 starting plan: contracts and kernel in Rust (bounded; not started)

**Status:** PLANNED (2026-11-12). Prepared after ADR-0002 ACCEPTED (Rust, D-030). **No M2 implementation has begun**; starting it needs the owner's go-ahead on the decisions at the end. ADR-0008 APPROVED IN PRINCIPLE; release and production NO-GO.

## Scope (MASTER-IMPLEMENTATION-PLAN section 10, M2)

**Deliverable:** typed schemas, a pure state-transition reducer, policy resolution and decision provenance, as the production Rust kernel. **Gate:** determinism, deny precedence, invalid and malformed input behaviour, property tests; running code and recorded evidence, never documents alone.

**In scope:** the deterministic kernel only: strict JSON (D-017) and D-006 canonical form; TaskSpec, request, policy bundle and Decision types; decision evaluation with deny precedence and matched-rule provenance; the workflow transition reducer and Acceptance Gate Integrity structure checks (ADR-0008 design); fail-closed behaviour on every invalid input. No I/O, no clock, no keys, no model SDK in the kernel.

**Out of scope for M2:** the broker, SQLite ledger and effects (M3); human approval service (M4); host and model adapters (M5); rule registry (M6); retrieval, Neo4j, PostgreSQL, Control Center (M7 to M10). The Go prototype is not extended.

## Starting tasks (proposed IDs; each maps to TASKS.md once approved)

| ID | Task | Acceptance evidence |
|---|---|---|
| M2-00 | Production crate layout and toolchain pin (for example a `kernel/` Rust workspace, separate from `m1/`), `#![forbid(unsafe_code)]`, lint and format policy, CI job | builds and tests in CI on the exact SHA; no dependency outside an approved list |
| M2-01 | Dependency and supply-chain policy for the kernel (ENT-R06): pinned versions, minimum release age, advisory and licence checks, SBOM generation | policy document plus a CI check that fails on an unapproved or advisory-flagged dependency |
| M2-02 | Strict JSON and canonical form ported from the M1 specification (not copied blindly from `m1/rust`) | M0 and M1 canonical and strict-JSON fixtures pass; property tests for round trip and rejection |
| M2-03 | Typed contracts (request, TaskSpec, bundle, Decision, refusal codes) with tenant and project binding (ENT-R02) | contract fixtures from `docs/contracts/` and M1 decide fixtures pass; cross-tenant negative tests |
| M2-04 | Decision evaluation: deny precedence, matched-rule provenance, `invalid` and `unavailable` outcomes | all M1 decide and golden fixtures; property tests for deny precedence and determinism (same input, same output, any order) |
| M2-05 | Transition reducer and acceptance structure checks (ADR-0008 design, approved in principle) | M1 transition fixtures; malformed-input property tests |
| M2-06 | Differential harness: production kernel versus the M1 Rust prototype and the M0 reference on generated inputs | 0 mismatches over the M1 property profiles; any intended difference recorded as a decision |

**Reuse:** the 447 M1 fixtures, the M0 reference harness and the M1 property generators are the acceptance oracle; the M1 Rust prototype is reference material, rewritten to production standard (error types, documentation, no test-only shortcuts), not promoted unchanged.

## Carried risks (not M2 deliverables; tracked to their gates)

Cross-process reconciliation (M3 broker, M10 PostgreSQL coordination and recovery gate); real-host enforcement, SR-06 (M5); power-loss durability (M9); single-host measurements and missing throttle counters (M10); independent human review of fixtures (REL-01) before any release.

## Owner decisions before M2 begins

1. Approve the M2-00 crate location and layout (proposal: a top-level `kernel/` Rust workspace; `m1/` stays as frozen bake-off evidence).
2. Approve the dependency policy direction for M2-01 (proposal: standard library plus a short reviewed allow-list, starting from `sha2` and `hmac` only for the kernel).
3. Confirm whether M2 starts immediately or waits for other priorities (for example ENT-00 follow-ups).
