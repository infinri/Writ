# M1 draft: `check-acceptance` semantics (M1-02, AG-M1)

**Status:** M1 draft for the bake-off only, written 2026-11-07 by Claude before any prototype exists. Superseded by the M2 production AcceptanceRecord contract ([ADR-0008](../../../docs/decisions/ADR-0008-acceptance-gate-integrity.md), APPROVED IN PRINCIPLE). Expected results in `acceptance-binding.cases.json` are hand-reasoned from this page, not produced by any implementation; they are **PENDING INDEPENDENT REVIEW** like the M0 golden cases.

`check_acceptance(record, current)` is a pure function. It does not verify signatures (key handling is a broker concern, M3), read storage or call evaluators. It answers one question: does this record, as given, authorize promoting this exact workflow transition in this exact current state? It is one precondition of a promotion; the kernel decision for `workflow.transition` is separate and also required.

## Inputs

**record** (all fields required, no others allowed):

| Field | Type |
|---|---|
| `contract_version` | `"m1-draft-1"` |
| `acceptance_record_id`, `tenant_id`, `project_id`, `task_id`, `workflow_id`, `artifact_snapshot_id`, `acceptance_policy_id`, `acceptance_policy_version` | non-empty string |
| `transition` | `{"from_state": string, "to_state": string}`, both non-empty, no other fields |
| `artifact_digest`, `policy_bundle_sha`, `record_digest` | `sha256:` followed by 64 lowercase hex |
| `state_revision` | integer, 0 to 2^53-1 (a JSON number with a fraction or exponent is malformed) |
| `status` | `accepted`, `rejected`, `incomplete`, `needs_human_review` |
| `issued_at`, `expires_at` | UTC timestamp `YYYY-MM-DDTHH:MM:SSZ` exactly |
| `criteria` | non-empty array; `criterion_id` unique |

Each criterion (all fields required, no others): `criterion_id` (non-empty string), `mandatory` (boolean), `required_class` (`mechanical`, `evidence`, `human`, `model_assisted`), `status` (`pass`, `fail`, `unknown`, `stale`, `waived`), `evaluator_trust` (`U`, `MA`, `M`, `E`, `H`), `fresh_until` (timestamp).

**current** (all required, no others): `tenant_id`, `project_id`, `task_id`, `workflow_id`, `transition`, `artifact_snapshot_id`, `artifact_digest`, `acceptance_policy_id`, `acceptance_policy_version`, `policy_bundle_sha`, `state_revision`, `now` (timestamp), `consumed_record_ids` (array of strings).

`record_digest` is the D-006 canonical digest of the record **without** its `record_digest` field.

## Checks, in this order; the first failure is the result

1. `current` malformed: `CURRENT_STATE_MALFORMED`.
2. `record` malformed: `ACCEPTANCE_RECORD_MALFORMED`.
3. `record_digest` differs from the recomputed digest, or `issued_at` is later than `now`: `ACCEPTANCE_RECORD_INVALID`.
4. `acceptance_record_id` is in `consumed_record_ids`: `ACCEPTANCE_REPLAYED`.
5. Tenant, project, task or workflow differ: `ACCEPTANCE_SCOPE_MISMATCH`.
6. `transition` differs (either state): `ACCEPTANCE_TRANSITION_MISMATCH`.
7. `artifact_snapshot_id` or `artifact_digest` differ: `ACCEPTANCE_ARTIFACT_MISMATCH`.
8. `acceptance_policy_id`, `acceptance_policy_version` or `policy_bundle_sha` differ: `ACCEPTANCE_POLICY_MISMATCH`.
9. `state_revision` differs: `ACCEPTANCE_REVISION_MISMATCH`.
10. `now` is at or after `expires_at`: `ACCEPTANCE_EXPIRED`.
11. No criterion is mandatory: `ACCEPTANCE_INCOMPLETE` (fail closed).
12. For each **mandatory** criterion in ascending `criterion_id` order (byte order of UTF-8), the first problem found:
    1. `evaluator_trust` is not the trust required by `required_class` (`mechanical` needs `M`, `evidence` needs `E`, `human` needs `H`, `model_assisted` needs `MA`; `U` never suffices): `ACCEPTANCE_UNTRUSTED_EVALUATOR`.
    2. `status` is `stale`, or `status` is `pass` and `now` is at or after `fresh_until`: `ACCEPTANCE_STALE`.
    3. `status` is `fail`, `unknown` or `waived` (waivers are not part of M1): `ACCEPTANCE_INCOMPLETE`.
13. `status` is not `accepted` although every mandatory criterion passed: `ACCEPTANCE_RECORD_INVALID`.
14. Otherwise `{"ok": true}`.

Non-mandatory criteria are validated for shape but never affect the result.

## Why this order

Integrity and replay come before scope so that a forged or reused record is reported as such; scope and binding checks come before time and criteria so that a record for another artifact or policy is never described as merely stale. The order is part of the contract so that both prototypes must agree on the code, not only on refusal.
