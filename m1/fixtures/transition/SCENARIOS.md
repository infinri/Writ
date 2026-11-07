# M1 draft: workflow transitions (M1-02 part 2)

**Status:** M1 draft for the bake-off only, written 2026-11-07 by Claude before any Rust or Go code. Expected results are hand-reasoned from this page and [acceptance SEMANTICS](../acceptance/SEMANTICS.md); **PENDING INDEPENDENT REVIEW**. Superseded by M2 production contracts ([ADR-0008](../../../docs/decisions/ADR-0008-acceptance-gate-integrity.md), APPROVED IN PRINCIPLE).

## What these fixtures do and do not cover

| Boundary | Covered here | Not covered |
|---|---|---|
| 1. Pure acceptance-record validation | `check-acceptance` set (50 cases) | |
| 2. Authenticated evaluators and record signatures | | Not covered. Trust levels are fields in the record; nothing here proves an evaluator or a signature is genuine (M2/M3/M6) |
| 3. Transactional workflow promotion | `transition-scenario` set: compare-and-set, single-use records, idempotency, refusal without side effects, serialized races | Real storage durability, crash recovery and multi-process locking are prototype tests (M1-05) and M3 |
| 4. Governed execution and artifact immutability | | Not covered. Snapshot ids and digests are opaque values; nothing here stores, protects or re-hashes bytes (ADR-0008 section 6a, M3) |
| Kernel authorization of `workflow.transition` | `transition-decisions` set (23 cases) under `m1-transition.bundle.json` | |

Passing these sets shows a prototype implements the specified decision and transition logic. It does not show that any boundary is enforced in a real host (guarantee level: Tested, unit or reference, at best).

## Transition decisions

`workflow.transition` requests use the existing M0 decision semantics unchanged. Assumption **A-1**: the target state is encoded in the canonical resource id, `workflow:<tenant>/<project>/transition/<to_state>/<workflow_id>`, so the existing prefix-matching statement language can select it; M2 may replace this with attribute conditions. Assumption **A-2**: composite requests (a transition carrying effect capabilities) are refused by an explicit forbid statement in the bundle rather than by a kernel rule (RM-01 prohibit). Assumption **A-3**: the kernel decides on the resource id only; any `to_state` attribute that disagrees with the resource must be refused by the broker, not the kernel (case `tx-22` documents this).

## Transition scenario semantics (`transition-scenario`)

Input `{"initial": State, "steps": [Op, ...]}`; output `{"steps": [Result, ...], "final": Final}`.

**State:** `tenant_id`, `project_id`, `task_id`, `workflow_id`, `state`, `revision`, `artifact_snapshot_id`, `artifact_digest`, `acceptance_policy_id`, `acceptance_policy_version`, `policy_bundle_sha`, `now`, `consumed_record_ids`. The broker also keeps a receipt store keyed by idempotency key.

**Operations**, each applied atomically in order:

- `promote` `{idempotency_key, decision_status, expected_revision, from_state, to_state, record}`. Its fingerprint is the D-006 digest of the whole operation object. Checks in this order:
  1. Key already has a receipt: same fingerprint returns the stored result plus `"replayed": true` and changes nothing; different fingerprint is refused `IDEMPOTENCY_CONFLICT`.
  2. `decision_status` is not `allow`: refused `DECISION_NOT_ALLOW` (the kernel decision is an input here; the scenario tests the broker).
  3. `expected_revision` differs from `revision`, or `from_state` differs from `state`: refused `REVISION_CONFLICT`.
  4. `check_acceptance(record, current)` where `current` is the state's scope, artifact, policy, revision, `now` and `consumed_record_ids` plus the operation's `from_state` and `to_state`: refused with its code.
  5. Commit: `state = to_state`, `revision += 1`, append the record id to `consumed_record_ids`, store a receipt `{"result": "committed", "revision": <new>}` under the key. Return it.
  A refusal changes nothing and stores no receipt (the key stays unused).
- `promote_concurrent` `{count, promote}`: `count` copies of `promote` with idempotency keys `<key>-1` .. `<key>-<count>`, submitted concurrently against the same state. Serializable execution means exactly one can pass step 3; the rest are refused `REVISION_CONFLICT`. Result `{"result": "concurrent", "committed": <n>, "refused": {"<CODE>": <n>, ...}}`.
- `update_artifact` `{artifact_snapshot_id, artifact_digest}`: a new artifact version; `revision += 1`. Result `{"result": "artifact_updated", "revision": <new>}`.
- `activate_policy` `{acceptance_policy_version, policy_bundle_sha}`: changes the active acceptance policy; revision unchanged. Result `{"result": "policy_activated"}`.
- `advance_time` `{now}`. Result `{"result": "time_advanced"}`.

Refusals are `{"result": "refused", "code": "<CODE>"}`.

**Final:** `{"state", "revision", "consumed_record_ids" (sorted), "receipts" (count)}`.
