# M1 draft: broker, ingress, approval grants and effects (`broker-scenario`, M1-02 N-2)

**Version:** `m1-draft-3` (2026-11-07), per owner decision [D-019](../../../docs/implementation/DECISION-LOG.md) (N-2). Written by Claude from the normative decisions D-008 to D-012, SR-01..SR-11 and D-018 A6 **before** any Rust or Go code and before comparing with the M0 broker; expected results are **PENDING INDEPENDENT REVIEW**. Disagreements with the M0 harness, found only after freezing, are recorded in [AMENDMENT-3](../AMENDMENT-3.md).

## Simulated, not verified

- **Signatures:** ingress seals and permit MACs are HMAC-SHA256 with fixture keys held in scenario state. This simulates authenticated ingress (D-010) and permit integrity (SR-07); it is not key management, rotation or registration (M3/M5).
- **Storage and crashes:** the ledger is a model; `fault` injects a crash at a named point. Atomicity and durability of a real store, process isolation and crash recovery are tested by each prototype's own tests (M1-05) and in M3, not by these fixtures.
- **Humans:** an "authenticated human" is a credential match on a fixture channel; it simulates the approval service (M4).
- **Effects:** "dispatch" increments a counter; no real side effect occurs.

## State

`now` (injected clock); `state_revision` (kernel state); `bundle` (active authorization bundle; its sha is the D-008 bundle sha); `ingress_keys` (registered adapter id to hex key); `permit_key` (hex, broker only); `channels` (trusted channel id to `{principal: credential}`); `dependencies` (`policy`, `ledger`: `up` or `down`). The broker keeps grants (by fixture label), receipts (by tenant and idempotency key), issued permits (by step index) and consumed permit ids.

**Seal:** `{"adapter_id", "mac"}` where `mac` is the hex HMAC-SHA256 under `ingress_keys[adapter_id]` of the D-006 canonical bytes of the request. Valid only if the adapter is registered, the MAC matches and `adapter_id` equals `request.principal.verified_by`.

## Operations

**`execute`** `{request, seal, grant_ref?, fault?}`. In order:

1. Seal invalid or absent: `INGRESS_UNAUTHENTICATED`.
2. `dependencies.ledger` down: `LEDGER_UNAVAILABLE` (fail closed before any decision side effect).
3. Use-time kernel decision with the active bundle (`null` if `dependencies.policy` is down, giving `unavailable` / `POLICY_UNAVAILABLE`). Status not `allow` or `require_approval`: refused with the decision's `reason_code`.
4. `require_approval` and no `grant_ref`: `APPROVAL_REQUIRED`.
5. Transaction:
   1. Receipt exists for (tenant, idempotency key): different request fingerprint (D-012) or different effective grant (the grant label for `require_approval`, none for `allow`): `IDEMPOTENCY_CONFLICT`. Same: by receipt status, `observed_success` returns `{"result": "effect_completed", "replayed": true}`, `observed_failure` returns `{"result": "effect_failed", "replayed": true}`, `reserved` resumes at step 6 without consuming the grant again, `dispatched` or `unknown_reconcile` is `RECONCILIATION_REQUIRED`.
   2. `fault: "crash_before_reserve"`: nothing persists; `{"result": "crashed"}`.
   3. `require_approval`: grant label unknown: `GRANT_UNKNOWN`; grant bindings differ from the request and decision (verdict `approved`, approval request id, tenant, requester principal, kind, resource, action digest, sorted capability set, bundle sha): `GRANT_MISMATCH`; `now` at or after grant expiry: `GRANT_EXPIRED`; uses exhausted: `GRANT_EXHAUSTED`. Otherwise consume one use.
   4. Reserve the receipt (`reserved`).
6. `fault: "crash_after_reserve"`: the reservation (and any grant use) persists; nothing dispatched; `{"result": "crashed"}`.
7. Issue a permit (bound to the request fingerprint, bundle sha, expiry `min(now + 30 s, deadline)`, MAC under `permit_key`), mark the receipt `dispatched`, call the adapter: the adapter checks the permit's fingerprint against the request, then MAC, then expiry, then records the permit id as consumed, then applies the effect.
8. `fault: "crash_after_dispatch"`: the effect was applied; the receipt stays `dispatched`; `{"result": "crashed"}`. `fault: "effect_fails"`: nothing applied; receipt `observed_failure`; `{"result": "effect_failed"}`.
9. Receipt `observed_success`; `{"result": "effect_completed"}`.

**`approve`** `{for_request, channel, human, credential, verdict, ttl_seconds?, grant_ref}`: channel unknown or credential wrong: `HUMAN_UNAUTHENTICATED`; the use-time decision for `for_request` is not `require_approval`: `NOT_APPROVABLE`; `human` equals the request's principal id or agent id: `SELF_APPROVAL_FORBIDDEN`. Otherwise a grant (labelled `grant_ref`, `max_uses` 1, expiry `now + ttl_seconds`, default 600) binds the fields listed in execute step 5.3 with the given verdict: `{"result": "grant_issued"}`. Approval is a separate trusted channel; nothing in a request can create a grant.

**`present_permit`** `{of_step, request, tamper?}`: presents the permit issued at step `of_step` (optionally tampered: `mac` flips the MAC, `expiry` extends it without re-signing) to the adapter with `request`. Adapter order: fingerprint differs: `PERMIT_REQUEST_MISMATCH` (permit not consumed); MAC invalid: `PERMIT_INVALID`; expired: `PERMIT_EXPIRED`; already consumed: `PERMIT_REPLAYED`; else applied: `{"result": "effect_applied"}`. A step that issued no permit: `PERMIT_MISSING`.

**`apply_without_permit`** `{request}`: `PERMIT_MISSING`; nothing applied.

**`concurrent`** `{operations: [...]}`: submitted concurrently; outcomes equal some serial order of atomic operations. Result `{"result": "concurrent", "outcomes": {"<result or refusal code>": n}}` (replays count as `replayed`).

**`activate_bundle`** `{bundle}`; **`advance_time`** `{now}`; **`set_dependency`** `{name, status}`; **`reconcile`** `{}`: every `dispatched` receipt becomes `unknown_reconcile`; `{"result": "reconciled", "count": n}`. Reconciliation never creates a receipt or marks an effect completed.

Results of refusals: `{"result": "refused", "code": "<CODE>"}`. Every refusal is audited.

**Final:** `{"effects_applied": n, "grant_uses": {label: uses}, "receipts": {key: status}, "consumed_permits": n, "refusals": n}`.
