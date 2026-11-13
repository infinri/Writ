# M3-05 transition service: design and owner questions

**Status:** QUESTIONS ANSWERED by the owner in D-036 (2026-11-13; all five proposals approved, item 5 with boundaries); **implemented** (E-068): `broker::acceptance`, `broker::workflow`, `writ-ledger-sqlite` module `workflow`, schema `writ-ledger-5`. The 66 transition-scenarios and 6 protocol-transition cases are closed in M3-08 with the exact exceptions below. M3-05 is authorized (M3-START-PLAN; D-034: M3-05/M3-07 integration may proceed after semantic validation, use-time MAC, registry, revocation, replay, immutable-snapshot and atomic-consumption tests remain mandatory). The questions in section 3 are trust-boundary or semantic choices that the approved documents do not settle; implementation starts after the owner answers them. ADR-0008 APPROVED IN PRINCIPLE; release and production NO-GO.

## 1. What M3-05 delivers (from approved documents)

- **One writer of workflow state (INV-32):** a fenced ledger transaction `promote` is the only public operation that changes a workflow's state and revision; no public store API writes workflow state otherwise.
- **Promotion as an ordinary request:** a sealed `workflow.transition` ActionRequest, re-decided at use time through `writ-kernel` under the shared policy hold (kernel structure checks, protection classification, D-019, D-020).
- **Use-time recomputation (INV-33, INV-36, INV-38):** inside the promotion transaction, `writ_kernel::acceptance::check` runs on the stored record against the workflow's current state, revision, artifact snapshot and digest, and the active acceptance policy's trusted requirement set (INV-37: requirements from the trusted policy path only, never from the request or a pack).
- **Single use (INV-33, D-034):** the acceptance record id is consumed atomically with the state change; an identical retry is idempotent (promotion receipt replay); any other use is refused.
- **Compare-and-set:** `expected_state_revision` and `from_state` must equal the stored workflow (`REVISION_CONFLICT`), checked in the same transaction as the write.
- **Audit:** every promotion and every refusal recorded (workflow events, refusal audit), sanitized.
- **Schema `writ-ledger-5`** (additions only, like `writ-ledger-4`): `workflows` (scope, state, revision, artifact snapshot id and digest, acceptance policy id, version and bundle digest, last promotion), `promotion_receipts`, `consumed_records`, `workflow_events`, and, for registry use at use time, the evaluator registry and revocations (from the trusted policy path).
- **Effect prerequisites (D-019 N-3):** a governed effect whose kind trusted policy lists (for example `repository.publish` requires `accepted`) is checked against the workflow store inside `Broker::execute` at use time (`EFFECT_PREREQUISITE_UNMET`, `PROMOTION_RECEIPT_INVALID`, `EFFECT_ARTIFACT_MISMATCH`, `PROMOTION_STALE`), never atomic with the promotion.

## 2. M1 model versus production

The M1 `transition-scenario` model (m1/fixtures/transition/SCENARIOS.md) passes the acceptance record, a permit and reconciliation outcomes inline from the client. Production cannot trust any of them from the agent side; section 3 asks how each is replaced.

| M1 element | Cases | Production issue |
|---|---|---|
| `record` supplied inline in `promote` | 64 promote steps | A record from the request is agent-supplied (AGI-3). ADR-0008 section 4: issued only by the acceptance service with a MAC or signature under a key no agent-realm component holds. The approved M3-06 contracts define HMAC authentication for runner attestations, not for AcceptanceRecords. |
| client-held promotion `permit` | `PERMIT_MISSING`, `PERMIT_REQUEST_MISMATCH`, `PERMIT_POLICY_STALE` cases | D-035 item 3 removed independent permit-presentation paths for effects; a promotion is decided and committed in one broker operation, so a client-held promotion permit adds a second path. |
| `reconcile {observed}` | sc-43, sc-44, sc-47 | D-035 item 3: client assertions are not evidence; sc-44 (`not_applied` releases the key for redispatch) is a redispatch after uncertainty. |
| `revoke_evaluator`, `activate_policy`, `activate_authorization_bundle` | 7 steps | Need trusted operator or policy paths (not agent requests). |

## 3. Owner questions

1. **AcceptanceRecord authentication.** Proposal: records reach the broker only through an `AcceptanceRecordSource` interface (no production implementation, like `ApprovalAuthority`; the acceptance service is M4/M6) **and** carry an HMAC-SHA256 under an acceptance-service test key with domain separation (`purpose` `writ.acceptance-record.v1`, authenticated bytes as for runner attestations), verified at use time; the request carries only `acceptance_record_id`; an inline or unauthenticated record is refused. This adds a `purpose`/`auth` member to the AcceptanceRecord contract (a contract amendment). Approve, or choose the interface without a MAC for M3 (trust then rests on the store being writable only by the acceptance service).
2. **Promotion permits.** Proposal: no client-held promotion permit; `promote` decides at use time and commits in one operation (as `execute` does). M1 permit refusals for promotion become production differences (exact M3-08 exceptions). Approve, or require a two-step permit flow.
3. **Transition-scenario reconciliation.** Proposal: post-promotion effects run through `Broker::execute` with D-035 evidence-based reconciliation; client-asserted `reconcile {observed}` is not provided; sc-43 and sc-44 become exact M3-08 exceptions (sc-47, nothing to reconcile, maps directly). Approve.
4. **Fixture b-36 (from D-035).** Reconciliation now records `observed_success` from adapter evidence where M1 expects `unknown_reconcile`. Confirm it as an exact M3-08 exception.
5. **Trusted inputs for workflow state.** Proposal: workflow creation, artifact updates (new snapshot id and digest), acceptance-policy activation and evaluator revocation are operator or service operations behind authenticated interfaces with no production implementation in M3 (tests provide them), never agent requests. Approve.

## 4. Test plan (after answers)

transition-scenarios (66) and protocol-transition (6) through a broker command layer with the exceptions above listed exactly; INV-32..INV-40 negative tests from M3-ACCEPTANCE-CONTRACTS; 64-way concurrent promotion of one transition (exactly one commits); record replay across keys refused; artifact update, policy activation and evaluator revocation between acceptance and promotion refused; store faults at begin, body and commit with `outcome_unknown` evidence; SIGKILL during promotion (state and record consumption atomic); restart persistence.
