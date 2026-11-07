# M1 draft: `check-acceptance` semantics (M1-02, AG-M1)

**Version:** `m1-draft-3` (2026-11-07), amending earlier drafts per owner decisions [D-017 and D-018](../../../docs/implementation/DECISION-LOG.md). Changes are listed in [AMENDMENT-1](../AMENDMENT-1.md) and [AMENDMENT-2](../AMENDMENT-2.md). Written by Claude before any prototype exists; expected results in `acceptance-binding.cases.json` are hand-reasoned from this page and are **PENDING INDEPENDENT REVIEW**. Superseded by the M2 production contract ([ADR-0008](../../../docs/decisions/ADR-0008-acceptance-gate-integrity.md), APPROVED IN PRINCIPLE).

## What this function is, and is not

`check_acceptance(record, current, requirements)` is a pure function. It answers: does this record, as given, satisfy the trusted acceptance requirements for this exact workflow transition in this exact current state?

It does **not** verify that the record was issued by the acceptance service, that any evaluator is genuine, or that an attestation is authentic (boundary 2, trusted services, M3/M4/M6). `record_digest` is an unkeyed consistency check; it is **not** proof of trusted issuance. It does not store or protect artifact bytes (boundary 4). It is one precondition of a promotion; the kernel decision for `workflow.transition` and the broker's permit and compare-and-set checks are separate and also required.

## Trust of the three inputs

| Input | Source | Trusted? |
|---|---|---|
| `requirements` | Policy registry: the active acceptance policy's requirement set, bound to its policy id, version and acceptance-policy bundle digest | Yes (trusted-side input; never from the agent or the record) |
| `current` | Broker: its own stored workflow state, injected clock (`now`), consumed-record store, revoked-evaluator list | Yes (the agent never supplies `now` or any other field) |
| `record` | Presented with the promotion request | **No**: everything in it is checked |

## Strict JSON (all M1 commands)

Every command parses its input with a strict parser before building any map:

- Duplicate object member names at any depth are rejected, including names that are equal only after escape decoding (`"a"` and `"a"`). No first-wins or last-wins.
- Numbers: only `-?(0|[1-9][0-9]*)`, magnitude at most 2^53-1. A fraction, an exponent, `-0`, leading zeros or a value beyond the range is rejected; no lossy conversion.
- `NaN`, `Infinity`, `-Infinity` and any other non-JSON literal are invalid JSON (a permissive parser such as Python's default `json.loads` is not acceptable as the normative parser).
- Invalid JSON and lone surrogates are rejected.
- Depth (D-006, D-018): the root value is at depth 0; each array element, object member name and object member value is at its parent's depth plus 1; any value or member name at depth greater than 64 is rejected. This counts traversal depth, not containers: 65 nested empty arrays are accepted (deepest value at depth 64), 65 nested arrays around a scalar are rejected (scalar at depth 65).

A `check-acceptance` input that fails strict parsing returns `{"refusal": "INPUT_JSON_INVALID", "json_error": "<CODE>"}` with codes `DUPLICATE_KEY`, `NUMBER_NOT_INTEGER`, `NUMBER_NEGATIVE_ZERO`, `NUMBER_OUT_OF_RANGE`, `INVALID_JSON`, `LONE_SURROGATE`, `DEPTH_EXCEEDED`. The `canonical` command returns `{"error": "<CODE>"}` with the same codes.

## Identifier rules

Identifiers that Writ defines are restricted lowercase ASCII, compared byte for byte, never case-folded or Unicode-normalized:

| Field | Pattern |
|---|---|
| `criterion_id`, `workflow_id`, state names (`from_state`, `to_state`), `evaluator_id` | `^[a-z][a-z0-9_-]{0,63}$` |
| `tenant_id`, `project_id`, `task_id`, `acceptance_record_id`, `artifact_snapshot_id`, `acceptance_policy_id` | `^[a-z0-9][a-z0-9_-]{0,63}$` |
| `acceptance_policy_version` | `^[0-9]{1,9}$` |

Trust labels (`evaluator_trust`) are claims inside an untrusted record. Strict class matching is checked here, but a label is never proof of evaluator identity or registration; that is a trusted-service responsibility (boundary 2).
| digests | `^sha256:[0-9a-f]{64}$` |
| timestamps | `^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$` and a valid calendar time |
| `state_revision` | integer 0 to 2^53-1 |

Display names are not part of any of these inputs.

## Inputs

**requirements** (all fields required, no others): `acceptance_policy_id`, `acceptance_policy_version`, `acceptance_policy_bundle_sha` (the acceptance policy's bundle digest, distinct from the authorization bundle digest that a request may pin, D-018 A3), `criteria`: non-empty array of `{"criterion_id", "required_class"}` with unique ids, where `required_class` is `mechanical`, `evidence` or `human`. Every listed criterion is mandatory. `model_assisted` is **not** an allowed required class (D-017: a model-produced finding never establishes a mandatory pass).

**record** (all required, no others): `contract_version` (`"m1-draft-3"`), `acceptance_record_id`, `tenant_id`, `project_id`, `task_id`, `workflow_id`, `transition` (`{"from_state", "to_state"}`), `artifact_snapshot_id`, `artifact_digest`, `acceptance_policy_id`, `acceptance_policy_version`, `acceptance_policy_bundle_sha`, `state_revision`, `status` (`accepted`, `rejected`, `incomplete`, `needs_human_review`), `issued_at`, `expires_at`, `criteria` (non-empty, unique ids), `record_digest`.

Each record criterion (all required, no others): `criterion_id`, `mandatory` (boolean), `required_class` (`mechanical`, `evidence`, `human`, `model_assisted`), `status` (`pass`, `fail`, `unknown`, `stale`, `waived`), `evaluator_id`, `evaluator_trust` (`U`, `MA`, `M`, `E`, `H`), `fresh_until`. A criterion of class `model_assisted` must have `mandatory: false` (otherwise malformed); it is advisory and never affects the result.

**current** (all required, no others): `tenant_id`, `project_id`, `task_id`, `workflow_id`, `transition`, `artifact_snapshot_id`, `artifact_digest`, `acceptance_policy_id`, `acceptance_policy_version`, `acceptance_policy_bundle_sha`, `state_revision`, `now`, `consumed_record_ids` (array of ids), `revoked_evaluator_ids` (array of ids).

`record_digest` is the D-006 canonical digest of the record without its `record_digest` member.

## Checks, in this order; the first failure is the result

1. `current` malformed: `CURRENT_STATE_MALFORMED`.
2. `requirements` malformed (including a `model_assisted` required class or an empty list): `REQUIREMENTS_MALFORMED`.
3. `requirements` policy id, version or acceptance-policy bundle digest differ from `current`'s active policy: `REQUIREMENTS_POLICY_MISMATCH`.
4. `record` malformed: `ACCEPTANCE_RECORD_MALFORMED`.
5. `record_digest` differs from the recomputed digest; or `expires_at` is not later than `issued_at`; or `issued_at` is later than `now`: `ACCEPTANCE_RECORD_INVALID`.
6. `acceptance_record_id` in `consumed_record_ids`: `ACCEPTANCE_REPLAYED`.
7. Tenant, project, task or workflow differ: `ACCEPTANCE_SCOPE_MISMATCH`.
8. `transition` differs: `ACCEPTANCE_TRANSITION_MISMATCH`.
9. `artifact_snapshot_id` or `artifact_digest` differ: `ACCEPTANCE_ARTIFACT_MISMATCH`.
10. Record policy id, version or acceptance-policy bundle digest differ from `current`: `ACCEPTANCE_POLICY_MISMATCH`.
11. `state_revision` differs: `ACCEPTANCE_REVISION_MISMATCH`.
12. `now` at or after `expires_at`: `ACCEPTANCE_EXPIRED`.
13. Completeness against `requirements`, in ascending `criterion_id` byte order over the requirement set:
    1. Required criterion absent from the record: `ACCEPTANCE_CRITERIA_MISSING`.
    2. Present but `mandatory` is false or `required_class` differs: `ACCEPTANCE_CRITERIA_MISMATCH`.
    Then any record criterion with `mandatory: true` that is not in the requirement set: `ACCEPTANCE_CRITERIA_MISMATCH`.
14. For each required criterion in ascending `criterion_id` order, the first problem:
    1. `evaluator_trust` is not exactly the trust for its class (`mechanical` M, `evidence` E, `human` H): `ACCEPTANCE_UNTRUSTED_EVALUATOR`.
    2. `evaluator_id` in `revoked_evaluator_ids`: `ACCEPTANCE_EVALUATOR_REVOKED`.
    3. `status` is `stale`, or `status` is `pass` and `now` is at or after `fresh_until`: `ACCEPTANCE_STALE`.
    4. `status` is `fail`, `unknown` or `waived`: `ACCEPTANCE_INCOMPLETE`.
15. Record `status` is not `accepted` although every required criterion passed: `ACCEPTANCE_RECORD_INVALID`.
16. Otherwise `{"ok": true}`.

Non-required, non-mandatory criteria (including every `model_assisted` criterion) are checked for shape only.

## Internal codes versus external responses

The codes above are internal: they go to the access-controlled audit record and to these parity fixtures. The response returned to an agent or host is computed by the `external-refusal` mapping (D-018 A7, set `external-refusal`): request-validation codes that describe only the caller's own input, key or expected revision pass through; every other acceptance, permit, scope or trusted-state code becomes `PROMOTION_REFUSED` with an audit reference; unknown codes also become `PROMOTION_REFUSED`. External behavior must not reveal cross-tenant existence, policy predicates (including whether a transition is protected), evaluator identities or resource secrets.

## Why this order

Trusted inputs are validated first, so a broken registry or broker state fails closed before any record is examined. Integrity and replay come before scope; scope and binding before time and criteria, so that a record for another artifact or policy is never described as merely stale; completeness before per-criterion status, so that an incomplete record is never described by the status of the criteria it happens to contain. The order is part of the contract so both prototypes must agree on the code, not only on refusal.
