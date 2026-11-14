# ADR-0008: Acceptance Gate Integrity

**Status:** APPROVED IN PRINCIPLE (owner, 2026-11-07); not ACCEPTED until the conditions below are documented and reviewed · requirement ACCEPTED by owner direction 2026-11-06 ([D-016](../implementation/DECISION-LOG.md))

**Owner conditions for ACCEPTED (2026-11-07):** (a) immutable artifact snapshots or equivalent use-time mediation sufficient to prevent artifact substitution and external TOCTOU races, documented in section 6a of the specification; (b) a clear distinction between designed defenses, tested defenses and verified real-host guarantees, documented in section 12a; (c) no claim of adversarial bypass resistance until the relevant integration and threat tests pass. (a) and (b) are drafted; (a) corrected 2026-11-07 per owner review (a descriptor, lock or single digest comparison is not an immutability guarantee). Review of the corrected text and tests pending; status remains APPROVED IN PRINCIPLE.

## Context

Writ's differentiator is that authorization and acceptance requirements are enforced outside the model's reasoning. Existing V2 design covers action authorization (kernel, broker, approvals) and evaluation contracts, but not how a workflow is promoted to an accepted or deployable state, how evaluator trust is established, or how acceptance binds to an exact artifact and policy version. Without that, an agent could fabricate test results, swap an artifact after validation or write workflow state directly.

## Decision (proposed)

1. Acceptance Gate Integrity is a core requirement in Governed Execution mode ([specification](../architecture/acceptance-gate-integrity.md)).
2. Three concepts with separate owners: the kernel authorizes; registered evaluators and the acceptance service establish whether requirements are satisfied; the transition service, a broker operation, enforces promotion. No second authorization authority.
3. Promotion is a `workflow.transition` ActionRequest decided by the kernel; the broker checks a signed AcceptanceRecord at use time inside one transaction with compare-and-set on the workflow revision and single-use consumption.
4. Evaluator trust comes only from a human-published registry; mechanical results count only from the Writ-controlled runner; model-assisted results never satisfy mechanical, evidence or human criteria.
5. Invalidation is enforced at use time by recomputing bindings.
6. Two labelled modes: Advisory Integration and Governed Execution; advisory transitions are never shown as governed.
7. Evaluation, approval, promotion and downstream effects use a content-addressed immutable snapshot stored where the agent cannot modify it, with the digest verified at creation and at every consumption, and all records bound to the snapshot id and digest. An open descriptor, lock or single digest comparison is not a guarantee. Any alternative needs an ADR showing equivalent protection against concurrent mutation; otherwise protected transitions fail closed.
8. Guarantee levels Designed, Tested (unit or reference), Tested (integration and threat) and Verified (real host); claims never exceed the recorded level.
9. Proposed invariants INV-32 to INV-40 and the sequencing changes in section 13 of the specification.

## Alternatives rejected

- **Acceptance inside the kernel:** pulls evidence I/O and evaluator calls into the pure decision path.
- **Acceptance as an independent authority that can promote by itself:** creates a competing authorization path.
- **Trusting host-hook or agent-reported results:** fails threats 2, 4 and 12.
- **Event-driven invalidation only:** a lost event would leave a stale record usable.

## Consequences

New contracts in M2; transition service and mechanical runner in M3; signoff provenance in M4; signed acceptance policies and evaluator registry in M6; M1 parity suite gains transition and binding fixtures. Status becomes ACCEPTED only by owner decision after review.
