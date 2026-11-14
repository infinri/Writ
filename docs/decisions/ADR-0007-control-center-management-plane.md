# ADR-0007: Writ Control Center as a separate management plane

**Status:** ACCEPTED (requirement and boundaries, owner direction 2026-11-06, [D-015](../implementation/DECISION-LOG.md)) · implementation language and UI technology OPEN

## Context

Operating V1 requires editing JSON, YAML, scripts and Neo4j directly. V2 adds versioned policies, approvals, receipts, packs, adapters, knowledge and memory; without a management surface, ordinary users cannot understand or safely administer it, and advanced users fall back to direct store edits that bypass versioning and audit. A management UI also creates new risks: a privileged channel an agent might reach, a dependency that could slow or block decisions, and a place where toggles could weaken enforcement.

## Decision

1. Writ Control Center is a first-class product requirement, specified in [docs/control-center](../control-center/README.md).
2. It is a **separate management plane**: a management service exposing versioned, authenticated APIs, with an optional web UI and full CLI coverage for essential operations.
3. The management plane is **outside the kernel decision path**. The kernel and broker never call it, wait on it or receive pushes from it; they pull signed policy bundles from the registry (INV-27).
4. The management service is **not an authority**. It holds no ingress, permit or approval-signing keys. Approvals are created by the approval service from an authenticated human step-up; policies are published by the registry, which enforces mandatory invariants regardless of caller (INV-29).
5. Agent-realm principals never authenticate to the management API (INV-28).
6. Every administrative mutation is authorized, audited before it takes effect, and versioned. Security audit records, operational logs, metrics and traces, and knowledge and memory content are separate data classes with separate retention; content deletion never removes security audit records (INV-30).
7. No management API returns secret values (INV-31).
8. Work is distributed across M2 to M10+; a minimum usable Control Center is part of the first local beta scope; enterprise features are separately justified. It is not a prerequisite for M1.

## Alternatives rejected

- **UI embedded in the kernel process:** couples availability and attack surface of the decision path to a web stack.
- **Direct database and graph editing tools:** bypass versioning, audit and invariant checks.
- **CLI only:** meets headless needs but fails the usability requirement for ordinary users.
- **Management service as policy authority:** concentrates authority in the most exposed component.

## Consequences

New contracts under `docs/contracts/management/` from M2; new invariants INV-27 to INV-31; new acceptance tests CC-SEC and CC-UX; each later milestone carries Control Center deliverables ([implementation plan](../control-center/implementation-plan.md)). The management-service language is decided after ADR-0002; the UI technology in an ADR at the start of M9.
