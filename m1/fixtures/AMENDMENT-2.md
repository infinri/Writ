# M1-02 amendment 2: owner semantic decisions A1 to A8 (D-018)

**Date:** 2026-11-07. **Basis:** owner review of the published amendment-1 baseline `a97e8f14beef94b0d24aef092405cffab044ec5f` and decision [D-018](../../docs/implementation/DECISION-LOG.md). Earlier sources remain in Git at `a97e8f1` (amendment 1) and `4a66364` (draft 1). All cases remain **PENDING INDEPENDENT REVIEW**; no expectation was changed to match an implementation.

## Changed expected results

| Case | Before | After | Reason |
|---|---|---|---|
| transition-decisions `tx-09-own-kind-only` | allow `ACCEPT_TRANSITION_PERMITTED` | invalid `REQUESTED_CAPABILITIES_INVALID` | A4: `requested_capabilities` never repeats `action.kind` |
| transition-scenarios, all 30 earlier cases | `final` = state, revision, consumed ids, receipts | `final` also carries `refusals` (count of refused results) and `effects` (`{}` for these cases) | A6: refusals are auditable; effect store added. Values of the earlier fields unchanged; `refusals` filled from each case's own expected step results |

No other earlier expected value changed. In particular `cj-19` (65 nested empty arrays accepted) and `cj-20` (66 rejected) are unchanged: they were correct under D-006 traversal depth but could not distinguish it from container counting; their rationales now say so, and cj-23..cj-34 make the distinction (A1).

## Structurally changed inputs (outcomes unchanged)

- Acceptance records, current state and requirement sets: `policy_bundle_sha` renamed `acceptance_policy_bundle_sha` (A3, distinct from the authorization bundle digest); record `contract_version` `m1-draft-3`.
- Scenario state gains `authorization_bundle_sha` and `protected_to_states`; promotion permits gain `authorization_bundle_sha`; the broker order adds the authorization-bundle check (step 4) and protection (step 6). Every earlier scenario was re-checked by hand against the new order; none changes outcome.
- Bundle `m1-transition` 0.3.0 adds `forbid-egress`, `permit-start-verification` (planning to verifying) and `permit-workspace-write`; existing transition outcomes unchanged; `tx-14`'s pinned sha follows the new bundle.
- Kernel order: requested-capabilities redundancy is checked right after schema validation.

## Added cases (83)

| Set | Added | Ids | Decision |
|---|---|---|---|
| strict-json | 15 | cj-23..cj-33 depth boundaries (scalar leaves, empty containers, object member names, mixed arrays and objects), cj-34 width, cj-35..cj-37 `Infinity`, `-Infinity`, `NaN` | A1, strict parsing |
| acceptance-binding | 3 | js-11..js-13 `Infinity`, `NaN`, `-Infinity` | strict parsing |
| transition-decisions | 10 | tx-36..tx-38 protection (kernel side), tx-39..tx-42 DENY versus INVALID and capability redundancy, tx-43..tx-45 strict parsing | A2, A4, A8 |
| transition-scenarios | 19 | sc-31..sc-35 protected and unprotected transitions, alternate routes, unexpected bindings; sc-36 authorization bundle change; sc-37 redundancy; sc-38..sc-47 refusals, crash before commit, effect retry, interrupted dispatch, reconciliation, key binding; sc-48, sc-49 strict parsing | A2, A3, A4, A6 |
| external-refusal (new set) | 36 | every internal refusal code plus one unknown code | A7 |

Totals: canonical-vector 1, strict-json 37, golden-decisions 26 (unchanged), acceptance-binding 97, transition-decisions 45, transition-scenarios 49, external-refusal 36; **291 cases**.

## Depth limit (A1)

D-006 exactly: root at depth 0; each array element, object member name and object member value at its parent's depth plus 1; anything deeper than 64 rejected. This counts traversal depth, not containers. The earlier "65 pass, 66 fail" observation concerned nested **empty** arrays, whose deepest value is the innermost container (depth 64 at 65 levels). With a scalar leaf, 65 nested arrays fail (cj-24) because the scalar is at depth 65. After the expectations were frozen, all 14 depth and width vectors were run through the D-006 reference module (`m0/writ_m0/canonical.py`): 14 of 14 agree, including canonical bytes and digests. D-006 was not changed.

## Deliberate divergence from the M0 harness (recorded, M0 unchanged)

| Topic | M0 behavior | M1 draft-3 |
|---|---|---|
| Duplicate keys | Python parser keeps the last | rejected |
| `-0`, `NaN`, `Infinity` at parse | `-0` canonicalizes to `0`; `NaN` and `Infinity` parse as floats and are then rejected by canonicalization | rejected at parse as `NUMBER_NEGATIVE_ZERO` or `INVALID_JSON` |
| `requested_capabilities` repeating the kind | allowed | `REQUESTED_CAPABILITIES_INVALID` |
| `to_states`, `from_states` statement conditions | ignored (any target state matches) | enforced; unscoped transition permits rejected at load |
| Composite promotion | decided by statements | structural `COMPOSITE_NOT_SUPPORTED` |

Driver run of the M0 adapter after freezing: canonical-vector 1/1, golden 26/26, strict-json 12/37, transition-decisions 13/45 with 16 protocol errors (the adapter does not accept `input_text` and stops answering at the first such case, so the transition count is not a clean divergence measure).
