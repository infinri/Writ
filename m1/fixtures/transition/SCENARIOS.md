# M1 draft: workflow transitions (M1-02)

**Version:** `m1-draft-2` (2026-11-07), amending draft 1 per owner decisions [D-017](../../../docs/implementation/DECISION-LOG.md); changes in [AMENDMENT-1](../AMENDMENT-1.md). Written by Claude before any Rust or Go code. Expected results are hand-reasoned from this page and [acceptance SEMANTICS](../acceptance/SEMANTICS.md); **PENDING INDEPENDENT REVIEW**. Superseded by M2 production contracts ([ADR-0008](../../../docs/decisions/ADR-0008-acceptance-gate-integrity.md), APPROVED IN PRINCIPLE).

## Four boundaries and what these fixtures cover

| Boundary | Covered here | Not covered |
|---|---|---|
| 1. Pure acceptance-record validation | `check-acceptance` set | |
| 2. Authenticated evaluators, record issuance and signatures | | Not covered. Trust levels, evaluator ids and record digests are plain fields; nothing here proves a record came from the acceptance service or an evaluator is genuine (M3, M4, M6) |
| 3. Transactional workflow promotion | `transition-scenario` set: permit binding, compare-and-set, single-use records, idempotency, side-effect-free refusal, serialized races | Durable storage, crash recovery and real multi-process locking: prototype tests (M1-05) and M3 |
| 4. Governed execution and artifact immutability | | Not covered. Snapshot ids and digests are opaque values; nothing stores, protects or re-hashes bytes (ADR-0008 section 6a, M3) |
| Kernel authorization of the exact transition | `transition-decisions` set | |

Passing these sets shows a prototype implements the specified logic (guarantee level at best: Tested, unit or reference). It shows nothing about a real host.

**Promotion is not the effect.** A committed promotion changes only Writ's workflow state, atomically, inside the ledger transaction. A later publish or deploy is a separate governed effect with its own decision, permit, effect intent, receipt and, after a crash or timeout, `unknown_reconcile` state (D-012, INV-08). No fixture or prototype may represent an external effect as part of the promotion transaction, and a promotion never implies that a publish or deploy happened.

## Kernel: `workflow.transition` decisions (extension of D-008, M1 draft)

The request uses the existing ActionRequest contract. For `action.kind == "workflow.transition"`:

- **Resource:** `canonical_resource_id` is exactly `workflow:<tenant>/<project>/<workflow_id>` (three segments, `workflow_id` matching `^[a-z][a-z0-9_-]{0,63}$`). The resource names the workflow, never the target state.
- **Attributes:** exactly the keys `from_state`, `to_state`, `acceptance_record_id`, `artifact_snapshot_id`, `artifact_digest`, each a string; `from_state` and `to_state` match `^[a-z][a-z0-9_-]{0,63}$` and differ. Any missing, extra, mistyped or duplicated key is refused. There is one representation of the transition.
- **Composite requests:** `requested_capabilities` must be empty or exactly `["workflow.transition"]`. Anything else is refused structurally by the kernel (RM-01), independent of any policy statement.

Evaluation order: strict JSON parse (else `invalid` / `REQUEST_JSON_INVALID`); then the D-008 order unchanged (schema, deadline, policy availability, bundle rejected at load as `unavailable` / `POLICY_BUNDLE_REJECTED`, version pin, issuer, state revision, capability knowledge, canonical form, tenant and project scope); then for transitions: resource shape (`invalid` / `WORKFLOW_RESOURCE_INVALID`), attributes (`invalid` / `TRANSITION_MALFORMED`), composite (`invalid` / `COMPOSITE_NOT_SUPPORTED`); then statements.

**Statements:** optional `from_states` and `to_states` (bundle schema extension). A statement with either list matches only `workflow.transition` requests whose attribute is listed. A bundle in which a `permit` or `require_approval` statement covers `workflow.transition` without `to_states` is rejected at load, so no permission for one target state can authorize another. Precedence is unchanged: forbid > require_approval > permit > default deny.

**Approval binding:** for transitions, the approval request id must bind `from_state` and `to_state` in addition to the D-008 fields, so a grant for one target state can never be consumed for another. This is a property test in M1-05, not a fixture value.

## Broker: transition scenario semantics (`transition-scenario`)

Input `{"initial": State, "steps": [Op, ...]}`; output `{"steps": [Result, ...], "final": Final}`.

**State** (broker-held, trusted): `tenant_id`, `project_id`, `task_id`, `workflow_id`, `state`, `revision`, `artifact_snapshot_id`, `artifact_digest`, `acceptance_policy_id`, `acceptance_policy_version`, `policy_bundle_sha`, `acceptance_requirements` (the trusted requirement set of the active policy), `now` (injected clock), `consumed_record_ids`, `revoked_evaluator_ids`, plus a receipt store keyed by idempotency key.

**`promote`** `{idempotency_key, request, permit, record}` where `request` is the complete `workflow.transition` ActionRequest and `permit` is `null` (the kernel did not allow) or `{"request_fingerprint", "record_digest"}`. The operation fingerprint is the D-006 digest of the whole operation object. Checks, in order, all inside one transaction:

1. Receipt exists for the key: same operation fingerprint returns the stored result plus `"replayed": true`; otherwise `IDEMPOTENCY_CONFLICT`.
2. `permit` is null: `PERMIT_MISSING`.
3. `permit.request_fingerprint` differs from the D-006 digest of `request` (D-012 full-request fingerprint): `PERMIT_REQUEST_MISMATCH`. Changing actor, scope, workflow, target state, expected revision or any attribute after the permit was issued lands here.
4. `permit.record_digest` differs from the presented record's `record_digest`, or the request attributes `acceptance_record_id`, `artifact_snapshot_id`, `artifact_digest` differ from the record's: `PERMIT_BINDING_MISMATCH`.
5. Request does not target this workflow (`kind` not `workflow.transition`, or tenant, project or resource not `workflow:<tenant>/<project>/<workflow_id>` of the state): `REQUEST_SCOPE_MISMATCH`. `requested_capabilities` other than empty or `["workflow.transition"]`: `COMPOSITE_NOT_SUPPORTED` (checked by the broker independently of the kernel).
6. `request.expected_state_revision` differs from `revision`, or `from_state` attribute differs from `state`: `REVISION_CONFLICT`.
7. `check_acceptance(record, current, acceptance_requirements)` with `current` built from the state and the request's transition: refused with its code.
8. Commit: `state = to_state`, `revision += 1`, append the record id to `consumed_record_ids`, store the receipt `{"result": "committed", "revision": <new>}`. Return it.

A refusal changes nothing and stores no receipt. Results are `{"result": "refused", "code": "<CODE>"}`. These are internal codes; the external response is `PROMOTION_REFUSED` with an audit reference (see acceptance SEMANTICS).

**Other operations:** `promote_concurrent` `{count, promote}` submits `count` copies with keys `<key>-1` .. `<key>-<count>` concurrently against the same state; serializable execution means exactly one passes step 6; result `{"result": "concurrent", "committed": n, "refused": {"<CODE>": n}}`. `update_artifact` `{artifact_snapshot_id, artifact_digest}` bumps `revision`. `activate_policy` `{acceptance_policy_version, policy_bundle_sha, acceptance_requirements}` leaves `revision` unchanged. `revoke_evaluator` `{evaluator_id}` and `advance_time` `{now}` (injected clock only; never from a request) leave `revision` unchanged. Results: `artifact_updated` with `revision`, `policy_activated`, `evaluator_revoked`, `time_advanced`.

**Final:** `{"state", "revision", "consumed_record_ids" (sorted), "receipts" (count)}`.
