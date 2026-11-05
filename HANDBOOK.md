# Writ V2 engineering handbook

**Normative source:** [docs/README.md](docs/README.md) and [implementation plan](docs/implementation/MASTER-IMPLEMENTATION-PLAN.md). This handbook is an onboarding and operating map, not an alternate specification.

## Current state

This branch is **specification-first**. Source under writ/, hooks/, bin/, agents/, rules/, tests/ and related V1 tooling has not been migrated. Tests that refer to historic V1 documents may not pass until build tooling is replaced; do not change the semantics of the specification just to satisfy an old V1-generated-document check.

## Development sequence

1. Read the threat model and invariant register before working on tool mediation.
2. Create contract fixtures for the five core outcomes: allow, deny, requires-human-approval, unavailable and invalid-input.
3. Implement a pure deterministic decision core that does no filesystem, network or graph I/O.
4. Put authorization at the capability executor, not only in an HTTP route or a Claude hook.
5. Verify state transitions against durable, optimistic-concurrency-controlled state.
6. Add the local adapter and verify every supported action type; unsupported actions are not silently assumed to be covered.
7. Re-introduce retrieval and memory with strict data/authority separation.
8. Measure complete local event-to-verdict and event-to-effect latency, not merely retrieval kernel microbenchmarks.

## Mandatory contribution evidence

For each behavioral PR: requirements and invariant IDs; before/after contract; dependency impact; unit tests; at least one negative control; failure-mode statement; appropriate integration tests; benchmark when changing a hot path; rollback notes. Never mark a policy 'enforced' solely because it was injected into model context.

## What runs where

- **Kernel**: pure state machine and decision evaluation; no LLM network requests.
- **Policy**: versioned authorization and validation bundles with conflict resolution and test vectors.
- **Broker**: the only privileged executor on governed paths. Issues receipts and checks idempotency.
- **Host adapters**: translate a host's events to typed actions and marshal decisions back.
- **Retrieval**: retrieves evidence and ranked doctrine within caller scope, with abstention.
- **Memory**: provenance, consent, retention and tenant-scoped access.
- **Operational store**: execution state, approvals, receipts, policy versions and immutable audit linkage.
- **Graph store**: connected domain knowledge, provenance and retrieval enrichment.
- **Workflow engine**: optional remote durability for long-running work; not on local permission hot path.

See [architecture website](docs/architecture/index.html), [repo layout](docs/architecture/repository-layout.md) and [verification plan](docs/testing/verification-and-benchmarks.md).

## Non-goals

Do not port all V1 hooks literally. Do not expose privileged operations through a second API path. Do not use a semantic similarity score as authorization. Do not turn advisory linting into a false claim of prevention. Do not invoke a remote durable-workflow service for every ordinary local permission check.

## Decision recording

Any stack lock-in or change to security posture requires an ADR with alternatives, evidence, compatibility impact and rollback strategy. Language choice is intentionally provisional until milestone M1 benchmark gates are satisfied.

## Model- and domain-agnostic design

Writ V2 serves both externally managed agents and an optional Writ-managed AgentRunner. Never put model API calls, provider-specific reasoning or coding-only workflow stages inside the kernel. Provider capability negotiation lives in ModelAdapter; domain workflows, evaluation rubrics and artifact types belong to Capability Packs. Use [M0 start](docs/implementation/M0-START-HERE.md), [models](docs/architecture/model-agnostic-runtime.md) and [packs](docs/architecture/capability-packs.md).
