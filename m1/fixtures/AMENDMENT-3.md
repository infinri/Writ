# M1-02 amendment 3: final security amendment (D-019, N-1 to N-4)

**Date:** 2026-11-07. **Basis:** owner review of revision 3 (`226e4d2af7637f2bbb2cc06ca6faf8175819cd63`) and decision [D-019](../../docs/implementation/DECISION-LOG.md). Earlier revisions remain in Git (`4a66364`, `a97e8f1`, `226e4d2`). All cases remain **PENDING INDEPENDENT REVIEW**; no expectation was changed to match an implementation.

## Changed expected results

| Case | Before | After | Reason |
|---|---|---|---|
| transition-decisions `tx-06-unlisted-target-state` | deny `DEFAULT_DENY` | invalid `ACCEPTANCE_BINDING_UNEXPECTED` | N-1: the kernel classifies protection; verifying to planning is unprotected, so the request's acceptance binding is structurally invalid. The default-deny intent moves to new `tx-06b` |
| transition-decisions `tx-38-protected-without-acceptance-kernel` | allow `ACCEPT_TRANSITION_PERMITTED` | invalid `ACCEPTANCE_BINDING_REQUIRED` | N-1: a protected transition without the binding never receives ALLOW (closes finding N-1) |
| transition-scenarios `sc-08-policy-changed` | `ACCEPTANCE_POLICY_MISMATCH` | `POLICY_DEPENDENCY_MISMATCH` | N-1: activating an acceptance policy that the active authorization bundle does not declare compatible fails closed first. The original intent moves to new `sc-08b` |

## Structurally changed inputs (outcomes unchanged)

- Bundle `m1-transition` 0.4.0 gains `workflow_protection` (protected states accepted, deployed, released; protected edge draft to verifying; compatible acceptance-policy bundle digest) and `permit-start-verification` now also permits `draft`. Schema `policy-bundle.schema.json` gains `workflow_protection`. `tx-14`'s pinned sha follows the bundle.
- Scenario state: `protected_to_states` replaced by `protection` (classification plus its source authorization bundle digest); new `effect_prerequisites` and `last_promotion`. `activate_authorization_bundle` carries `protection`. Effect requests carry `workflow_id`, `artifact_snapshot_id`, `artifact_digest`, `promotion_receipt_id`.
- Effect cases sc-40..sc-46 now start from a promoted workflow (state accepted, revision 8, `acc-1` consumed, `last_promotion` set), because publication requires one (N-3). Their step results are unchanged; their `final` state, revision and consumed ids reflect the new starting point.
- Broker promotion order gains step 5 (policy view consistency); later steps renumbered. Every earlier scenario was re-checked by hand against the new order.

## Added cases (90)

| Set | Added | Content |
|---|---|---|
| broker-scenarios (new, `broker-scenario`) | 43 | b-01..b-43: sealed ingress (unsealed, forged, unregistered, other issuer, altered), permits (missing, forged, replayed, other operation, expired, extended expiry, bounded by deadline), approvals (required, single use, bound to resource, capabilities, principal, bundle; expired, denied, unknown, forbid not approvable, wrong credential, untrusted channel, self approval), 16-way grant race, refusal before reservation keeps the grant, crash before reserve, after reserve (resume), after dispatch (reconciliation), idempotent retry, conflicting key, other grant, concurrent duplicates, effect failure, policy and ledger outage, no fabricated receipts, unknown capability |
| transition-decisions | 11 | tx-06b; tx-46..tx-55: protected edge with and without binding, destination protection from any source, binding on unprotected, classification missing, duplicate edge, self edge, bypassable edge, alternate spelling, intermediate route not permitted |
| transition-scenarios | 17 | sc-08b; sc-50..sc-65: classification missing or from another bundle, protected edge, edge record reused for destination, publish before acceptance, accept A publish B, accepted artifact modified, policy changed after promotion, authorization changed after promotion, missing and forged promotion receipts, publication permit reused for another target, valid publish, concurrent invalidation and publication (`one_of`), uncertain publication |
| external-refusal | 20 | mappings for every new internal code; one generic code `PROMOTION_REFUSED` for every non-validation security refusal; `APPROVAL_REQUIRED` passes through |

Totals: canonical-vector 1, strict-json 37, golden 26, acceptance-binding 97, transition-decisions 56, transition-scenarios 66, external-refusal 56, broker-scenarios 43; **382 cases**.

Driver: new compare mode `one_of` (any listed output passes) for concurrent steps with several valid serializations; self-test added.

## Comparison with the M0 broker (after freezing; by test correspondence, not execution)

The broker cases were written from BROKER.md, then compared with the behavior asserted by `m0/tests/test_broker.py` and `m0/writ_m0/broker.py`. No M0 adapter executes `broker-scenario` yet (it would need the fixture's simulated keys injected into M0's ingress verifier and permit signer); executing the comparison is listed for M1-05. Disagreements recorded, M0 unchanged:

| Topic | M0 | M1 draft |
|---|---|---|
| Grant mismatch code | `GRANT_MISMATCH:<fields>` | `GRANT_MISMATCH` (field detail is audit-only) |
| Retry after crash after dispatch | returns the stored `dispatched` receipt | `RECONCILIATION_REQUIRED` (A6: never treated as harmless) |
| Retry after crash after reservation | returns the stored `reserved` receipt; nothing dispatches | resumes dispatch without consuming the grant again |
| Ledger unavailable | no explicit path (store exceptions) | `LEDGER_UNAVAILABLE`, before any decision side effect |
| Approval refusals | `PermissionError` without codes | `HUMAN_UNAUTHENTICATED`, `NOT_APPROVABLE`, `SELF_APPROVAL_FORBIDDEN` |
| Adapter refusals | one `UnauthorizedEffect` for invalid, expired or used permits | distinct `PERMIT_INVALID`, `PERMIT_EXPIRED`, `PERMIT_REPLAYED`, in a fixed order |
| Malformed stored grants | `GRANT_MALFORMED` | not reachable through `approve`; not covered (gap) |

Consistent with M0: sealed ingress rules (SR-01), permit TTL 30 s bounded by the deadline (SR-03), permit consumed once (SR-07), whole-request fingerprint (SR-10), receipt replay bound to fingerprint and grant (SR-11), single-use grants bound to the capability set (SR-02), no subagent inheritance (D-009, D-011), policy change invalidates grants, grant TTL 10 minutes, forbid never approvable.
