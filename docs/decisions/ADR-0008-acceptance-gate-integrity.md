# ADR-0008: Acceptance Gate Integrity

**Status:** PROPOSED (design) · requirement ACCEPTED by owner direction 2026-11-06 ([D-016](../implementation/DECISION-LOG.md)) · 2026-11-06

## Context

Writ's differentiator is that authorization and acceptance requirements are enforced outside the model's reasoning. Existing V2 design covers action authorization (kernel, broker, approvals) and evaluation contracts, but not how a workflow is promoted to an accepted or deployable state, how evaluator trust is established, or how acceptance binds to an exact artifact and policy version. Without that, an agent could fabricate test results, swap an artifact after validation or write workflow state directly.

## Decision (proposed)

1. Acceptance Gate Integrity is a core requirement in Governed Execution mode ([specification](../architecture/acceptance-gate-integrity.md)).
2. Three concepts with separate owners: the kernel authorizes; registered evaluators and the acceptance service establish whether requirements are satisfied; the transition service, a broker operation, enforces promotion. No second authorization authority.
3. Promotion is a `workflow.transition` ActionRequest decided by the kernel; the broker checks a signed AcceptanceRecord at use time inside one transaction with compare-and-set on the workflow revision and single-use consumption.
4. Evaluator trust comes only from a human-published registry; mechanical results count only from the Writ-controlled runner; model-assisted results never satisfy mechanical, evidence or human criteria.
5. Invalidation is enforced at use time by recomputing bindings.
6. Two labelled modes: Advisory Integration and Governed Execution; advisory transitions are never shown as governed.
7. Proposed invariants INV-32 to INV-39 and the sequencing changes in section 13 of the specification.

## Alternatives rejected

- **Acceptance inside the kernel:** pulls evidence I/O and evaluator calls into the pure decision path.
- **Acceptance as an independent authority that can promote by itself:** creates a competing authorization path.
- **Trusting host-hook or agent-reported results:** fails threats 2, 4 and 12.
- **Event-driven invalidation only:** a lost event would leave a stale record usable.

## Consequences

New contracts in M2; transition service and mechanical runner in M3; signoff provenance in M4; signed acceptance policies and evaluator registry in M6; M1 parity suite gains transition and binding fixtures. Status becomes ACCEPTED only by owner decision after review.
