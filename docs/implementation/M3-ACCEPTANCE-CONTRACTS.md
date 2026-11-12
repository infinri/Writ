# Acceptance-gate contract carry-over (M3-06) and INV-32..INV-40 map

**Status:** PROPOSED FOR OWNER REVIEW (2026-11-12). Nothing in this document is implemented except where a row says so. ADR-0008 APPROVED IN PRINCIPLE; release and production NO-GO.

## Scope amendment (recorded as D-032)

The owner accepted M2 at `eecab4d` for its **documented kernel scope** ([M2-START-PLAN](M2-START-PLAN.md), E-056, E-057). The acceptance-gate plan ([acceptance-gate-integrity](../architecture/acceptance-gate-integrity.md) section 13, M2 row) also assigned to M2: the evaluator registry entry, runner attestation and EvaluationRecord scope contracts, the workflow state machine with protected states, and INV-32..INV-40 as contract tests. **Those obligations were omitted from the M2 plan and were not delivered.** They are not described as completed anywhere; they are carried as **M3-06**, and every M3 consumer that depends on them (M3-05 transition-service use-time recomputation and evaluator checks, M3-07 mechanical runner) waits for the owner's review of the contract shapes below. What M2 did deliver for the acceptance gate: the AcceptanceRecord binding check (`check-acceptance`, 97 fixtures, D-031) and the kernel's transition structure checks.

## Proposed contracts (shapes for review; not yet schemas)

All identifiers follow the existing id patterns; digests are `sha256:` plus 64 lowercase hex; times are strict UTC whole seconds (D-022); every contract is a closed object (unknown members invalid) with a `contract_version`.

### Evaluator registry entry

| Field | Type | Rule |
|---|---|---|
| `evaluator_id`, `version` | name, version string | identity; a change of version is a different evaluator for INV-36 |
| `class` | `mechanical` / `evidence` / `model_assisted` / `human` | `model_assisted` never satisfies a mandatory criterion (INV-34) |
| `trust` | `M` / `E` / `MA` / `H` | fixed per class; packs cannot raise it |
| `implementation_digest`, `environment_digest` | digest | bound into attestations |
| `attestation_key_id` | id | signer identity; key material lives outside the kernel (broker or runner) |
| `criteria` | array of criterion ids | the only criteria this evaluator may satisfy |
| `scope` | `{tenant_id, project_id}` or a published global marker | scope check at use time |
| `owner`, `publisher`, `published_at`, `registry_revision` | ids, time, integer | published only by an authorized human through the policy path (INV-12) |
| `status` | `active` / `revoked` (with `revoked_at`) | revocation is a new registry revision, checked at use time |

### Runner attestation

| Field | Type | Rule |
|---|---|---|
| `attestation_id` | id | single use per acceptance record criterion (replay protection) |
| `evaluator_id`, `evaluator_version`, `registry_revision` | | must match an active registry entry at use time |
| `tenant_id`, `project_id`, `task_id`, `workflow_id` | ids | scope binding |
| `artifact_snapshot_id`, `artifact_digest`, `input_digests` | id, digests | the snapshot the runner actually read (INV-40) |
| `command_digest`, `environment_digest` | digests | must equal the registry entry |
| `exit_status`, `output_digest` | integer, digest | result |
| `started_at`, `finished_at`, `fresh_until` | times | freshness against the trusted clock |
| `signer_key_id`, `signature` | id, bytes | verified by the broker or acceptance service against the registry key (never by the kernel; the kernel stays key-free) |

### EvaluationRecord scope fields (additions to `docs/contracts/evaluation-record.schema.json`)

`tenant_id`, `project_id`, `workflow_id`, `transition` (`from_state`, `to_state`), `artifact_snapshot_id`, `artifact_digest`, `acceptance_policy_id`, `acceptance_policy_version`, `acceptance_policy_bundle_sha`, `state_revision`, `evaluator_id`, `evaluator_version`, `evaluator_trust`, `attestation_ref`, `issued_at`, `fresh_until`, `mode` (`governed` / `advisory`, INV-39). The current schema identifies the task and artifact but carries none of the policy, revision, snapshot-digest, evaluator-version or mode bindings.

## INV-32..INV-40 map

Dimensions: **I** identity, **S** scope, **A** artifact or snapshot binding, **P** policy or revision binding, **F** freshness, **R** replay protection, **T** signer trust, **V** revocation.

| Invariant | Dimensions | Implemented now | M3 implementation | Negative tests (planned unless stated) | Unresolved obligation and gate |
|---|---|---|---|---|---|
| INV-32 no protected transition without kernel ALLOW and a current AcceptanceRecord; agents cannot write workflow state | I S P | kernel `workflow.transition` decisions and binding-required checks (M2, transition-decisions 62) | M3-05 transition service is the only writer of workflow state (fenced store operation) | promotion without record; direct state write through any public store API; record for another workflow | agent lacking store credentials on a real host (M5, SR-06) |
| INV-33 record binds tenant, project, task, workflow, transition, snapshot and digest, policy version, evaluator identities, evidence digests, revision | I S A P | `check-acceptance` binding check (M2, 97 fixtures; D-031) | M3-05 use-time recomputation against current state | each single bound-field mutation refused (M2 property test, 3,000 cases, done) | evidence-digest resolution against a catalog (M7) |
| INV-34 only registered evaluators of the exact required trust; model output advisory | I T V | trust-class matching in `check-acceptance` (M2) | M3-06 registry contract; M3-05 registry lookup at use time | unregistered evaluator; `MA` for a mandatory criterion; trust label without registration; revoked evaluator | registry publication by authorized humans (M6) |
| INV-35 mechanical results only from the Writ runner, digest inputs, attested | I A T R | none | M3-06 attestation contract; M3-07 runner signs, broker verifies | agent-supplied result file; tampered command or input digest; reused attestation; unknown signer | runner key management (M5, M9); sandbox strength on a real host (M5) |
| INV-36 changes to artifact, evidence, policy, evaluator version or key, scope or revision invalidate the record at use time | A P F V | revision and binding comparison in `check-acceptance` (M2) | M3-05 recomputation at use; evaluator version and key from the registry | artifact updated between acceptance and promotion; evaluator key rotated; policy revision bumped | evidence-change detection (M7) |
| INV-37 acceptance requirements are versioned policy; agents and packs cannot create, weaken or waive them | P I | requirements are an input contract in `check-acceptance` | M3-05 loads requirements from the trusted policy path only | pack-supplied requirements; waiver of an invariant-derived criterion | signed acceptance policies (M6); waivers as approvals (M4) |
| INV-38 missing, failed, stale, unverifiable or malformed mandatory evidence, or an unavailable evaluator or service, refuses | F T | stale, failed and malformed refusals in `check-acceptance` (M2) | M3-05 fail closed on store, registry or verifier unavailability | evaluator timeout; malformed output; acceptance service down | none beyond M3 |
| INV-39 advisory mode never reported as governed or verified; mode on every record | I | none | M3-06 `mode` field; M3-05 refuses advisory records for protected transitions | advisory record presented for promotion | Control Center display (M9, out of this authorization) |
| INV-40 one content-addressed immutable snapshot in agent-unwritable storage, digest verified at creation and every consumption | A R | snapshot id and digest compared as bound fields (M2) | M3-07 runner and M3-05 recompute the digest of the bytes they read | snapshot bytes changed after acceptance; digest mismatch at consumption | agent-unwritable snapshot storage on a real host (M5) |

## Review questions for the owner

1. Approve or amend the three contract shapes (fields, closed objects, signer location outside the kernel).
2. Confirm that M3-05 implements CAS, single-use records and audit events now, but defers evaluator-registry and attestation checks until these contracts are approved.
3. Confirm attestation replay protection as single use per acceptance-record criterion (alternative: single use per attestation id across the ledger).
