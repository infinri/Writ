# M1-02 fixture review package

**Status: PENDING INDEPENDENT REVIEW.** Revision 2 (2026-11-07), after owner decisions [D-017](DECISION-LOG.md). Prepared by Claude, who also wrote every expectation. No Rust or Go code exists. Expectations must not be changed to make a prototype pass; a disagreement is resolved by a recorded review decision. Revision 1 is at commit `4a66364`; changes are listed in [amendment 1](../../m1/fixtures/AMENDMENT-1.md).

## Files

| Path | Content |
|---|---|
| [`m1/fixtures/acceptance/SEMANTICS.md`](../../m1/fixtures/acceptance/SEMANTICS.md) | `check-acceptance` m1-draft-2: input trust, strict JSON, identifier rules, check order, internal versus external codes |
| [`m1/fixtures/acceptance/acceptance-binding.cases.json`](../../m1/fixtures/acceptance/acceptance-binding.cases.json) | 94 cases (source) |
| [`m1/fixtures/transition/SCENARIOS.md`](../../m1/fixtures/transition/SCENARIOS.md) | Boundary table, promotion versus effect, kernel transition extension, broker promotion semantics |
| [`m1/fixtures/transition/m1-transition.bundle.json`](../../m1/fixtures/transition/m1-transition.bundle.json), [`m1-transition-unscoped.bundle.json`](../../m1/fixtures/transition/m1-transition-unscoped.bundle.json) | Transition policy fixtures (both validate against `policy-bundle.schema.json`; the second must be rejected at load) |
| [`m1/fixtures/transition/transition-decisions.cases.json`](../../m1/fixtures/transition/transition-decisions.cases.json) | 35 decisions (source) |
| [`m1/fixtures/transition/transition-scenarios.cases.json`](../../m1/fixtures/transition/transition-scenarios.cases.json) | 30 scenarios (source) |
| [`m1/fixtures/canonical/strict-json.cases.json`](../../m1/fixtures/canonical/strict-json.cases.json) | 22 strict parsing and canonical cases (source) |
| [`m1/fixtures/AMENDMENT-1.md`](../../m1/fixtures/AMENDMENT-1.md) | Changed expected results with reasons; added cases; contract changes |
| [`m1/fixtures/export_m0_fixtures.py`](../../m1/fixtures/export_m0_fixtures.py), [`manifest.json`](../../m1/fixtures/manifest.json), `m1/fixtures/sets/*.jsonl` | Builder (assembles inputs only), manifest, 6 sets, 208 cases |
| [`m1/driver/tests/test_fixtures.py`](../../m1/driver/tests/test_fixtures.py) | Fixture integrity tests (schema validity, raw-text defects present as claimed, review status, drift) |

## Counts by category

| Set | Cases | Categories |
|---|---|---|
| canonical-vector | 1 | D-006 known vector |
| strict-json | 22 | duplicate keys 4 (plus 1 control), numbers 8, invalid JSON 5, surrogate 1, depth 2, ordering and escapes 1 |
| golden-decisions | 26 | M0 golden, unchanged |
| acceptance-binding | 94 | pos-prefixed 6 (one now a refusal), malformed 14, identifiers 4, requirement set 5, completeness 7, model-assisted 6, integrity and time 7, replay 1, scope 4, transition 2, artifact and snapshot 2, policy 3, revision 1, expiry 1, per-criterion trust, revocation, freshness and status 16, order 5, strict JSON 10 |
| transition-decisions | 35 | precedence and roles 6, from-state 1, composite 3, isolation 2, stale revision and policy 4, identity 2, non-canonical path 2, deadline 1, unknown kind 1, attribute and resource shape 7, bundle rejection 1, strict JSON 4, negative revision 1 |
| transition-scenarios | 30 | commit 1, idempotency 2, record replay 1, artifact and snapshot 2, stale state 2, policy change 1, permit missing 2, permit bound to request 7, permit bound to record 2, other workflow 1, composite at broker 1, race 1, expiry 1, side-effect freedom 1, malformed 1, cross-project 1, transition not evaluated 1, incomplete record 1, revoked evaluator 1 |

## Category to invariant map (INV-32..INV-42 PROPOSED)

| Category | Cases | Invariants |
|---|---|---|
| Strict parsing | cj-*, js-*, tx-31..34 | INV-41, INV-18 |
| Exact transition authorization | tx-01..06, tx-24..30 | INV-42, INV-03, INV-04 |
| Composite sub-effects | tx-07..09, sc-28 | INV-42, RM-01 |
| Permit binding | sc-09, sc-13, sc-18..26 | INV-42, INV-01, D-012 |
| Requirement completeness | pos-02, pos-02b, req-*, cmp-*, ma-04, ma-05, crit-09, sc-29 | INV-33, INV-37, INV-38 |
| Model-assisted is advisory | ma-01..03, crit-01, crit-12 | INV-34, INV-25 |
| Revoked and stale evaluators | crit-06, crit-07, crit-13..16, sc-30 | INV-36, INV-38 |
| Isolation | scope-*, tx-10, tx-11, sc-16, sc-21, sc-27 | INV-10, INV-33 |
| Stale state and revisions | rev-01, tx-12, sc-06, sc-15, sc-20 | INV-17, INV-36 |
| Policy version and downgrade | pol-*, ord-05, tx-13..15, tx-29, sc-08 | INV-05, INV-12, INV-36, INV-37 |
| Replay, idempotency, races | rep-01, sc-02..04, sc-10, sc-13 | INV-06, INV-07, INV-09 |
| Artifact and snapshot | art-*, int-01, sc-05, sc-07, sc-22, sc-23, sc-26 | INV-33, INV-40 (identity binding only) |
| Time and integrity | int-02..07, exp-01, ord-04, sc-11 | INV-36, INV-38 |
| Injection and alternate routes | mal-02, tx-20..22 | INV-02, INV-11, INV-13, INV-14 |

## Can the revised requirements be enforced independently of the AI?

By design, yes, and only at the **Designed** level today. Every input that decides the outcome comes from a trusted side: the requirement set from the policy registry, `current` (including `now` and revocations) from the broker's own state and injected clock, the permit from the kernel and broker. The agent contributes only the request and the record, both fully checked. Enforcement independent of the AI additionally requires boundaries the fixtures do not cover: authentic record issuance and evaluator attestation (boundary 2), the agent lacking credentials to write workflow state or perform effects directly, and immutable snapshots (boundary 4). Until those are built and tested (M3 to M6) and SR-06 shows what the host mediates (M5), no enforcement or bypass-resistance claim is made.

## Contradictions found and how they were resolved

| # | Finding | Resolution |
|---|---|---|
| C-1 | Draft 1 encoded the target state in the resource id, contradicting ADR-0008 section 2 (`workflow:<tenant>/<project>/<workflow_id>` with attributes) | D-017: resource names the workflow; states are typed attributes |
| C-2 | ADR-0008 trust table let model-assisted evaluators satisfy mandatory `model_assisted` criteria, in tension with AGI-3 | D-017: model-assisted is advisory only; table and INV-34 amended |
| C-3 | The D-006 reference implementation (M0 Python) keeps the last duplicate key and accepts `-0` | D-017 tightens parsing; M0 harness unchanged and non-normative; M0 is no longer a driver participant for strict-json |
| C-4 | D-008 approval request ids do not include transition attributes; with the new resource form, an approval for one target state would bind identically to another | SCENARIOS.md requires `from_state` and `to_state` in the transition approval binding; M1-05 property test. The M0 oracle demonstrates the underlying flaw: in `tx-04` it matched the deploy approval rule for a release request |
| C-5 | Draft 1 could be read as representing promotion and external effects in one transaction | SCENARIOS.md and ADR-0008: promotion is atomic for workflow state only; effects keep receipts and reconciliation (INV-08) |

No contradiction found with D-010 (scenario requests are unsealed by design: ingress sealing is outside the pure fixtures) or D-012 (the permit fingerprint is the D-012 full-request fingerprint).

## Remaining assumptions needing owner decisions

1. **Depth limit counting:** a top-level value is depth 0 and depth above 64 is rejected (65 nested arrays accepted, 66 rejected), matching the D-006 reference implementation (cj-19, cj-20).
2. **Transition attributes always include acceptance bindings:** every `workflow.transition` must carry `acceptance_record_id`, `artifact_snapshot_id` and `artifact_digest`, including transitions that may not need acceptance (for example `planning` to `verifying`). Alternative: per-transition required attributes in policy.
3. **Two bundle digests:** the authorization bundle (`policy_bundle_sha` on requests) and the acceptance policy's bundle digest (on records and requirements) are treated as separate values; M2 should decide whether acceptance policies live in the same signed bundle.
4. **Composite allowance for the request's own kind:** `requested_capabilities: ["workflow.transition"]` is accepted (tx-09); the stricter rule "must be empty" is equally defensible.
5. **Exact trust match** (no hierarchy), **exclusive expiry and freshness with no clock-skew tolerance**, **`issued_at` equal to `now` accepted** (A-5, A-6).
6. **Refused idempotency keys are reusable** (sc-13) and **compare-and-set precedes the acceptance check** (race losers report `REVISION_CONFLICT`).
7. **Which refusal codes may be returned externally in detail:** draft says only malformed-input codes; all acceptance refusals become `PROMOTION_REFUSED`.
8. **Decide-command composite refusal status:** `invalid` (structural) rather than `deny`.

## Evidence

Recorded in [TEST-EVIDENCE](TEST-EVIDENCE.md) E-019 (amendment checks). No implementation of `check-acceptance`, `transition-scenario`, strict parsing or the transition extension exists yet.
