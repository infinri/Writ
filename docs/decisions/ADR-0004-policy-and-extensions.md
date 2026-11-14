# ADR-0004: One policy authority, defer extra runtimes

**Status:** ACCEPTED DIRECTION; engine API to validate · 2026-11-05

## Decision

Use a versioned, independently published declarative authorization language (native Cedar in Rust candidate, Cedar-Go in Go candidate) for access decisions. The kernel owns decision precedence and the broker enforces effects. Keep validation and advisory instructions distinct. Defer CEL/OPA unless the need for another expression or control plane is demonstrated. Defer WASM and Temporal until stable extension contracts and workload evidence exist.

## Rationale

A product built to prevent policy drift should not begin V2 with several overlapping languages and three possible effect paths. Declarative policy with schema and golden tests can be audited. Isolated executable extensions are useful, but their host imports/permissions are security-critical and must be designed after the broker.

## Rejected

Injecting policy as system prompts; shell-only permission checks; graph-based permission claims; automatic 'ask' after hard deny; direct WASM host filesystem access; Temporal as the synchronous local authorization service.

## Required experiment

Implement both candidates over identical authorization rules; verify deny/approval semantics, unsupported input behavior, policy change/pinning and signed manifests. Record chosen engine and compatibility guarantees in a follow-on ADR.
