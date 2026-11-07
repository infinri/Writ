# M1 draft: workflow transitions (M1-02)

**Version:** `m1-draft-3` (2026-11-07), amending earlier drafts per owner decisions [D-017 and D-018](../../../docs/implementation/DECISION-LOG.md); changes in [AMENDMENT-1](../AMENDMENT-1.md) and [AMENDMENT-2](../AMENDMENT-2.md). Written by Claude before any Rust or Go code. Expected results are hand-reasoned from this page and [acceptance SEMANTICS](../acceptance/SEMANTICS.md); **PENDING INDEPENDENT REVIEW**. Superseded by M2 production contracts ([ADR-0008](../../../docs/decisions/ADR-0008-acceptance-gate-integrity.md), APPROVED IN PRINCIPLE).

## Four boundaries and what these fixtures cover

| Boundary | Covered here | Not covered |
|---|---|---|
| 1. Pure acceptance-record validation | `check-acceptance` set | |
| 2. Authenticated evaluators, record issuance and signatures | | Not covered. Trust labels, evaluator ids and record digests are plain fields; nothing here proves a record came from the acceptance service or an evaluator is genuine (M3, M4, M6) |
| 3. Transactional workflow promotion | `transition-scenario` set: permit binding, protection, compare-and-set, single-use records, idempotency, side-effect-free refusal, serialized races, interrupted effects and reconciliation | Durable storage, crash recovery and real multi-process locking: prototype tests (M1-05) and M3 |
| 4. Governed execution and artifact immutability | | Not covered. Snapshot ids and digests are opaque values; effect "dispatch" is simulated; nothing stores, protects or re-hashes bytes (ADR-0008 section 6a, M3) |
| Kernel authorization of the exact transition | `transition-decisions` set | |
| Disclosure to agents | `external-refusal` set | Real response channels |

Passing these sets shows a prototype implements the specified logic (guarantee level at best: Tested, unit or reference). It shows nothing about a real host.

**Promotion is not the effect.** A committed promotion changes only Writ's workflow state, atomically, inside the ledger transaction. A later publish or deploy is a separate governed effect with its own decision, permit, reservation, receipt and, after an interrupted dispatch, `unknown_reconcile` state (D-012, INV-08). No fixture or prototype may represent an external effect as part of the promotion transaction.

## Strict input (all commands)

Every input, including `transition-scenario` input, is parsed with the strict rules in acceptance SEMANTICS (duplicate keys, number spellings and range, `NaN` and `Infinity`, depth). A `transition-scenario` input that fails returns `{"input_error": "INPUT_JSON_INVALID", "json_error": "<CODE>"}`; a `decide` input that fails returns `invalid` / `REQUEST_JSON_INVALID`.

## Kernel: decisions (extension of D-008, M1 draft)

**Requested capabilities (all kinds, D-018 A4).** `action.kind` is the operation identity. `requested_capabilities` lists only additional, distinct capabilities and never contains `action.kind`; a request that repeats its own kind is `invalid` / `REQUESTED_CAPABILITIES_INVALID` (checked right after schema validation). Composite requests for ordinary kinds remain policy decisions (each capability must be covered; a forbid gives `deny`).

**Transitions.** For `action.kind == "workflow.transition"`:

- **Resource:** exactly `workflow:<tenant>/<project>/<workflow_id>` (three segments, `workflow_id` matching `^[a-z][a-z0-9_-]{0,63}$`); it names the workflow, never the target state.
- **Attributes:** `from_state` and `to_state` required (lowercase pattern, different); the acceptance binding `acceptance_record_id`, `artifact_snapshot_id`, `artifact_digest` is either entirely present (strings) or entirely absent; no other keys. Otherwise `invalid` / `TRANSITION_MALFORMED`.
- **Composite:** `requested_capabilities` must be empty; any other capability is `invalid` / `COMPOSITE_NOT_SUPPORTED` (structural, before statements, before any effect). A structurally valid action prohibited by policy is `deny` (D-018 A8).

Evaluation order: strict parse; schema; requested-capabilities redundancy; then the D-008 order (deadline, policy availability, bundle rejected at load as `unavailable` / `POLICY_BUNDLE_REJECTED`, authorization-bundle pin, issuer, state revision, capability knowledge, canonical form, scope); then for transitions resource shape (`WORKFLOW_RESOURCE_INVALID`), attributes (`TRANSITION_MALFORMED`), composite (`COMPOSITE_NOT_SUPPORTED`); then statements.

**Statements:** optional `from_states`, `to_states`. A statement with either list matches only `workflow.transition` requests whose attribute is listed. A `permit` or `require_approval` statement covering `workflow.transition` without `to_states` makes the bundle invalid at load. Precedence unchanged.

**Protection is not decided by the kernel.** Whether a transition needs an AcceptanceRecord is decided by the trusted acceptance policy the broker holds (below). The kernel allows `verifying` to `accepted` without acceptance attributes (`tx-38`); the broker refuses the same promotion (`sc-32`). This is a single enforcement point for protection (assumption B-1 in the review package).

**Approval binding:** for transitions, the approval request id binds `from_state` and `to_state` in addition to the D-008 fields (M1-05 property test).

## Broker: `transition-scenario`

Input `{"initial": State, "steps": [Op, ...]}`; output `{"steps": [Result, ...], "final": Final}`.

**State** (broker-held, trusted): `tenant_id`, `project_id`, `task_id`, `workflow_id`, `state`, `revision`, `artifact_snapshot_id`, `artifact_digest`, `acceptance_policy_id`, `acceptance_policy_version`, `acceptance_policy_bundle_sha`, `acceptance_requirements`, `protected_to_states`, `authorization_bundle_sha` (the active authorization bundle, distinct from the acceptance policy's bundle, D-018 A3), `now` (injected clock), `consumed_record_ids`, `revoked_evaluator_ids`; plus a promotion receipt store and an effect store, each keyed by idempotency key in its own namespace.

**Binding fingerprint** of an operation: the D-006 digest of the operation object without its `fault` member (faults are test injections).

**`promote`** `{idempotency_key, request, permit, record, fault?}`; `permit` is `null` (kernel did not allow) or `{request_fingerprint, record_digest (null for unprotected), authorization_bundle_sha}`; `record` may be `null`. Checks, in order, inside one transaction:

1. A receipt exists for the key: same binding fingerprint returns it plus `"replayed": true`; otherwise `IDEMPOTENCY_CONFLICT`.
2. `permit` null: `PERMIT_MISSING`.
3. `permit.request_fingerprint` differs from the D-006 digest of `request`: `PERMIT_REQUEST_MISMATCH`.
4. `permit.authorization_bundle_sha` differs from the state's: `PERMIT_POLICY_STALE`.
5. Request shape: kind, tenant, project or resource not this workflow: `REQUEST_SCOPE_MISMATCH`; `requested_capabilities` containing the kind: `REQUESTED_CAPABILITIES_INVALID`; any other capability: `COMPOSITE_NOT_SUPPORTED`.
6. Protection (D-018 A2): the transition is protected when `to_state` is in `protected_to_states` (protection follows the target state, so no unprotected detour reaches a protected state without acceptance).
   - Protected: acceptance attributes absent or `record` null: `ACCEPTANCE_REQUIRED`; `permit.record_digest` differs from the record's, or the request's acceptance attributes differ from the record: `PERMIT_BINDING_MISMATCH`.
   - Unprotected: any acceptance attribute, a non-null `record` or a non-null `permit.record_digest`: `ACCEPTANCE_BINDING_UNEXPECTED`.
7. `expected_state_revision` differs from `revision`, or `from_state` differs from `state`: `REVISION_CONFLICT`.
8. Protected only: `check_acceptance(record, current, acceptance_requirements)`: refused with its code.
9. `fault: "crash_before_commit"`: the transaction aborts; nothing persists; result `{"result": "crashed"}`.
10. Commit: `state = to_state`, `revision += 1`, record id consumed (protected only), receipt stored. Result `{"result": "committed", "revision": <new>}`.

A refusal changes nothing, stores no receipt and leaves the key reusable (D-018 A6: nothing was reserved); it is counted in `final.refusals` because every refusal is audited.

**`effect`** `{idempotency_key, request, permit, fault?}` with `permit` null or `{request_fingerprint, authorization_bundle_sha}`. A simulated governed effect (for example a publish after promotion):

1. Key in the effect store: different binding fingerprint is `IDEMPOTENCY_CONFLICT`; state `completed` returns `{"result": "effect_completed", "replayed": true}`; state `unknown_reconcile` is `RECONCILIATION_REQUIRED` (never re-dispatched).
2. `PERMIT_MISSING`, `PERMIT_REQUEST_MISMATCH`, `PERMIT_POLICY_STALE` as for promotion (pre-reservation; the key stays unbound).
3. `fault: "crash_before_reserve"`: nothing persists; `{"result": "crashed"}`.
4. Reserve: the key is bound to the binding fingerprint; dispatch. `fault: "crash_after_dispatch"`: the key becomes `unknown_reconcile`; `{"result": "crashed"}`.
5. Complete: the key becomes `completed`; `{"result": "effect_completed"}`.

**`reconcile`** `{idempotency_key, observed}`: only for a key in `unknown_reconcile`. `observed: "applied"` makes it `completed`; `"not_applied"` releases the key (a later identical request may dispatch). Result `{"result": "reconciled", "outcome": <observed>}`. Otherwise `NOTHING_TO_RECONCILE`. Reconciliation can never fabricate a completed effect for an unknown key.

**Other operations:** `promote_concurrent` `{count, promote}` (keys `<key>-1` .. `<key>-<count>`, concurrent, exactly one passes step 7; result `{"result": "concurrent", "committed": n, "refused": {"<CODE>": n}}`); `update_artifact` (bumps `revision`; `artifact_updated` with `revision`); `activate_policy` `{acceptance_policy_version, acceptance_policy_bundle_sha, acceptance_requirements}` (`policy_activated`); `activate_authorization_bundle` `{authorization_bundle_sha}` (`authorization_bundle_activated`); `revoke_evaluator` (`evaluator_revoked`); `advance_time` (`time_advanced`, injected clock only).

Refusals are `{"result": "refused", "code": "<CODE>"}` (internal codes; agents receive the `external-refusal` mapping).

**Final:** `{"state", "revision", "consumed_record_ids" (sorted), "receipts" (promotion receipts), "refusals" (count of refused results, including those inside `promote_concurrent`), "effects" (map key to `completed` or `unknown_reconcile`)}`.

Approval grants are not modeled here; grant consumption and "refusals never consume a grant" stay covered by the broker scenarios in `m0/tests/test_broker.py` (parity set "Broker semantics").
