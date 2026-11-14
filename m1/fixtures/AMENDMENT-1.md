# M1-02 amendment 1: owner semantic decisions (D-017)

**Date:** 2026-11-07. **Basis:** owner technical review of the M1-02 review baseline `4a66364` and decision [D-017](../../docs/implementation/DECISION-LOG.md). Draft 1 sources remain in Git at `4a66364`. Every case is still **PENDING INDEPENDENT REVIEW**; no expectation was changed to match any implementation.

## Changed expected results

| Case | Draft 1 expected | Draft 2 expected | Reason |
|---|---|---|---|
| acceptance `pos-02-single-mandatory` | ok | `ACCEPTANCE_CRITERIA_MISSING` | Completeness against the trusted requirement set; the record omits two required criteria |
| acceptance `crit-09-no-mandatory` | `ACCEPTANCE_INCOMPLETE` | `ACCEPTANCE_CRITERIA_MISMATCH` | Required criteria downgraded to advisory are a mismatch against the requirement set |
| acceptance `mal-10-revision-fraction` | `ACCEPTANCE_RECORD_MALFORMED` | `INPUT_JSON_INVALID` / `NUMBER_NOT_INTEGER` | Strict parsing rejects non-integer numbers before any record check; case now sent as raw text |
| transitions `tx-07-composite-publish` | deny `COMPOSITE_EFFECT_FORBIDDEN` | invalid `COMPOSITE_NOT_SUPPORTED` | Composite prohibited structurally, not by an editable statement |
| transitions `tx-08-composite-process` | deny `COMPOSITE_EFFECT_FORBIDDEN` | invalid `COMPOSITE_NOT_SUPPORTED` | As above |
| transitions `tx-21` (injected attributes) | require_approval (extras ignored) | invalid `TRANSITION_MALFORMED` | Closed transition attribute set |
| transitions `tx-22` | allow (attribute and resource disagree, kernel ignored attribute) | invalid `WORKFLOW_RESOURCE_INVALID` (state encoded in resource) | Resource names the workflow only; any second representation of the target state is refused |
| scenarios `sc-09` | `DECISION_NOT_ALLOW` | `PERMIT_MISSING` | Promotion now carries the kernel permit instead of a decision-status input |
| scenarios `sc-13` | `DECISION_NOT_ALLOW` then committed | `PERMIT_MISSING` then committed | Same change of code; behavior unchanged |

## Structurally changed inputs (outcomes unchanged)

- All acceptance cases: records are `m1-draft-2` (criteria carry `evaluator_id`), `current` carries `revoked_evaluator_ids`, and every case passes a trusted `requirements` set. The 47 unchanged draft-1 outcomes keep their ids.
- All transition decisions: resource is `workflow:<tenant>/<project>/<workflow_id>`; states are typed attributes; bundle `m1-transition` 0.2.0 uses `to_states` and `from_states` and no longer contains the composite forbid statement. Outcomes of tx-01..06, tx-09..20 and tx-23 are unchanged.
- All scenarios: `promote` carries the full ActionRequest and a permit `{request_fingerprint, record_digest}`; state carries the trusted requirement set and revocation list. Outcomes other than sc-09 and sc-13 are unchanged.
- `m1-transition.bundle.json` 0.1.0 (draft 1) is replaced by 0.2.0.

## Added cases

| Set | Added | Ids |
|---|---|---|
| strict-json (new set, `canonical`) | 22 | cj-01..cj-22 |
| acceptance-binding | 44 | pos-02b, pos-05, mal-13, mal-14, id-01..04, req-01..05, cmp-01..07, ma-01..06, int-05..07, crit-13..16, ord-05, js-01..10 |
| transition-decisions | 12 | tx-24..tx-35 |
| transition-scenarios | 13 | sc-18..sc-30 |
| **Total** | **91** | |

Totals now: canonical-vector 1, strict-json 22, golden-decisions 26 (unchanged), acceptance-binding 94, transition-decisions 35, transition-scenarios 30; 208 cases.

## Contract changes

- `docs/contracts/policy-bundle.schema.json`: optional `from_states`, `to_states` on statements (lowercase state pattern).
- `docs/contracts/action-request.schema.json` `expected_state_revision` and `docs/contracts/decision.schema.json` `state_revision`: `maximum` 2^53-1.

## M0 reference harness

Unchanged (non-normative, D-001). It does not implement D-017: run through the driver after these expectations were frozen it passed strict-json 5/22 and transition-decisions 13/35 (it ignores `to_states`; in `tx-04` it matched the deploy approval rule for a release request). Golden 26/26 and the D-006 vector still pass. The M0 adapter is therefore a participant only for `canonical-vector` and `golden-decisions`.
