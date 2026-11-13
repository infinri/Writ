# M3-08 closure: harness bindings and transformations

**Status:** implementation of D-037 items 1, 2, 3, 5, 6 and 7 in the closure adapter (`persistence/sqlite/examples/m3_line_adapter/`, a non-shipping Cargo example). Every transformation between a frozen M1 fixture and the production broker's inputs is listed here. Frozen fixtures (`m1/`) are never changed. Test keys, test sources, test authorities and harness bundles are test infrastructure only; they do not establish production authentication, key provisioning, the acceptance service, the runner or the immutable snapshot store (M4 to M6). Seeded transition cases are **production-component tests with synthetic preconditions**.

## 1. Harness authorization bundles (D-037 item 2)

Files in `m3/closure/bundles/`, derived from `m1/fixtures/transition/m1-transition.bundle.json`: scope `fixture-tenant/fixture-project` replaced by `t1/p1`; only the capabilities `workflow.transition` and `repository.publish`; the original `workflow_protection` and trusted issuer `writ-host-adapter:fixture` kept. Each is loaded through the production `load_bundle`; its digest is computed by `load_bundle` at run time and never asserted by the adapter (the digests below are informational, D-006 over the file without `publisher_signature`).

| File | Digest (informational) | Statements | Used by |
|---|---|---|---|
| `t1p1-transition.bundle.json` | `sha256:2f658517...8c285cc` | permit verifying to accepted; draft, planning to verifying; verifying to reviewing; reviewing to accepted; any to deployed; publish `repository:t1/p1/releases` | active bundle of every transition case except sc-42, sc-45 |
| `t1p1-transition-publish-other.bundle.json` | `sha256:25eebd20...ff6e91` | as above plus publish `repository:t1/p1/other` | active bundle of sc-42, sc-45 |
| `t1p1-transition-v2.bundle.json` | `sha256:54ca2581...116169` | as the base, version `0.4.0-m3c.2` (a real distinct bundle, same protection) | `activate_authorization_bundle` in sc-36 |
| `t1p1-transition-v2-accept-e.bundle.json` | `sha256:51a4af8f...5203ea` | as the base, protection compatible with acceptance-policy bundle `sha256:eeee...` | `activate_authorization_bundle` in sc-08b, sc-59 and companion m3c-59 |

Why these statements (the transition fixtures model "the kernel allowed this" as a client permit and model no approval grants, SCENARIOS.md):

- The transitions are exactly those the frozen scenarios present with a fingerprint-valid permit (`permit.request_fingerprint` equals the D-006 digest of the request): verifying to accepted, draft and planning to verifying, verifying to reviewing (sc-33, sc-34), reviewing to accepted (sc-34), verifying and accepted to deployed (sc-03, sc-04, sc-17). The original `require_approval` for `deployed` becomes `permit` because the transition scenarios never model approval; `released`, egress, workspace and the agent-release forbid are dropped (no scenario uses them). The bundle still passes the production bypass check (no unprotected route from `draft` to `verifying`).
- **Selection rule (mechanical, per case):** the `publish-other` bundle is active exactly when the case contains an `effect` on `repository:t1/p1/other` whose fixture permit is fingerprint-valid (sc-42, sc-45). sc-62's permit for that target does not verify, so sc-62 runs under the base bundle and production refuses the publication by policy (`DEFAULT_DENY`): the legacy permit never authorizes the effect (D-037 item 5).
- **Activation rule:** `activate_authorization_bundle {authorization_bundle_sha, protection}` activates, through the trusted `ActivePolicy::activate`, the first replacement bundle whose `workflow_protection` equals the step's `protection` (protected states, protected edges and acceptance-policy bundle digest) and whose computed digest differs from the active one. The step's bare digest (`sha256:9999...`) is never used as policy authority. No match is answered `unsupported` (none occurs).
- No broad allow-all bundle; wrong scope, missing policy, default deny and activation invalidation stay covered by the production policy tests and the broker scenarios (b-19, b-39).

## 2. Field bindings (fixture field to production input)

| Fixture | Production | Rule |
|---|---|---|
| `initial.authorization_bundle_sha` (placeholder `sha256:aaaa...`) | computed digest of the active harness bundle | used only where the fixture repeats it: a seeded `last_promotion.authorization_bundle_sha` equal to the placeholder is stored as the computed digest; any other value is kept verbatim |
| `initial.protection` | not read | protection comes only from the active bundle (agent- or scenario-supplied classification is never reintroduced, D-037 item 1); sc-50 and sc-51 are exact production differences |
| `initial.now`, `advance_time.now` | injected `EffectiveClock` | |
| `initial.acceptance_requirements`, `initial.effect_prerequisites` | production `administer(ActivateAcceptancePolicy)` | prerequisites map each effect kind to its `required_states` |
| `activate_policy.acceptance_requirements` | production `administer(ActivateAcceptancePolicy)` | the case's initial effect prerequisites are kept |
| `initial.revoked_evaluator_ids`, `revoke_evaluator` | production `administer(RevokeEvaluator)` | |
| `update_artifact {artifact_snapshot_id, artifact_digest}` | production `administer(UpdateArtifact)` | the snapshot is added to the test snapshot store first; answer `{"result": "artifact_updated", "revision": <stored revision>}` |
| operation `idempotency_key` | the sealed request's `idempotency_key` | the M1 receipt namespace is the operation key (SCENARIOS.md), the fixture requests all carry `idem-p1` or `idem-e1`; `promote_concurrent` uses `<key>-1` .. `<key>-<count>` |
| `request` | `Seal::create` with the fixed test ingress key of `principal.verified_by`, then production `verify_ingress` | an unregistered issuer is refused and audited (C1) |
| `permit` (promote and effect) | never read | D-036 item 2 (promotion), D-037 item 5 (effect) |
| inline `record` | issued by the test acceptance source (section 3); the broker reads it by the request's `acceptance_record_id` only | |
| `effect` | sealed `Broker::execute(verified, stored workflow revision, no grant)` | prerequisites in T-reserve, controlled effect store; faults `crash_before_reserve` and `crash_after_dispatch` fail T-reserve and T-observe at begin (C5) |
| `reconcile {idempotency_key, observed}` | production evidence-based `Broker::reconcile()` | the client assertion is never read; answer `{"result": "production_difference", "reason": "CLIENT_ASSERTED_RECONCILIATION_NOT_PROVIDED", "evidence_reconciled": <count>}` (D-036 item 3, D-037 items 5 and 6) |
| `final.state`, `revision`, `consumed_record_ids` | ledger rows after every store is closed | |
| `final.receipts` | promotion receipts written after setup | the seeded receipt behind `last_promotion` is a precondition; the fixture's receipt store starts empty |
| `final.effects` | effect receipts by key | `observed_success` is reported as `completed`; every other status is reported unchanged (`dispatched` is never renamed `unknown_reconcile`, D-035 item 3) |
| `final.refusals` | `audit_refusals` rows | |

Broker scenarios: `apply_without_permit` (b-08) and presenting the permit of a step that issued none (p-11) have no production operation; the step answers `{"result": "production_difference", "reason": "NO_PRODUCTION_OPERATION" | "NO_PERMIT_TO_PRESENT"}`, nothing is audited, the case continues and the final state shows zero effects.

## 3. Authenticated test evidence (D-037 item 3)

- **Keys:** acceptance-record key `m3c-acceptance-key-1` (type `AcceptanceRecordKey`) and runner key `runner-key-1` (type `RunnerAttestationKey`), distinct purposes and values; HMAC-SHA256 computed in the example from the kernel's SHA-256 (no new dependency). HMAC is a MAC, not a signature.
- **Registry:** four `evaluator-registry-entry/1` entries scoped to the case's tenant and project, registered through production `administer(RegisterEvaluator)`: `ci-runner` (mechanical, M, command `cargo-test`, criterion `build`), `evidence-engine` (evidence, E, `citations`), `approval-service` (human, H, `signoff`), `reviewer-model` (model_assisted, MA, `style`); version `1.4.0`, implementation and environment digests `sha256:1111...`, `sha256:2222...`, registry revision 12, attestation key `runner-key-1`. They are the registry's standing contents, not derived from any record or expectation.
- **Record translation:** before each `promote` (and once before a concurrent group) whose inline `record` is an object with a string `acceptance_record_id`, the test source issues `authenticated-acceptance-record/1` with `record` = the fixture record **unchanged** (no repair, its own `record_digest`), `issuer` `m3c-test-acceptance-service`, `mode` governed, and one evaluation per record criterion whose evaluator is registered: `evaluation_id` `eval-<record>-<criterion>`, the registered evaluator binding, `attestation_id` `att-<record>-<criterion>` exactly for evaluator trust `M` (else null), no evidence digests. The source keeps the latest issue per record id and does not filter by tenant or project, so production's own scope check runs (sc-16). A null record issues nothing. A semantically defective record (sc-12, sc-14, sc-16, sc-25, sc-26, sc-29) still gets a valid test MAC because semantic validation is under test; no fixture case tests an invalid record MAC, so no tamper-after-MAC case arises (the broker permit tamper cases b-09, b-13 tamper after the MAC).
- **Synthetic attestations** (one per trust-`M` criterion): bound to the record's own fields (tenant, project, task, workflow, transition, snapshot and digest, `workflow:<t>/<p>/<w>` target, acceptance policy id, version and digest, state revision, record id), the criterion, the registered evaluator binding and command; `exit_status` 0 exactly when the criterion's status is `pass`; `fresh_until` the criterion's; `started_at` = `finished_at` = the record's `issued_at`; MACed with `runner-key-1`. They test verification only; end-to-end runner-produced attestation coverage stays with the runner tests (M3-07).
- **Snapshot store:** confirms exactly the (snapshot id, digest) pairs the scenario declares: the initial artifact, the artifact of `last_promotion`, and each `update_artifact`. A record naming any other pair is `ARTIFACT_INTEGRITY_VIOLATION` (INV-40; sc-26).

## 4. Preconditions (D-037 item 1, I-2)

Order: production `SqliteLedger::create` and `SqliteEffectStore::create`; production administration (registry, revocations, acceptance policy and, when the initial state is neither protected nor the target of a protected edge and there is no `last_promotion`, `CreateWorkflow` plus `revision` artifact updates); then every store is closed and the test seeder writes, in one rusqlite transaction, only what production cannot express:

- the workflow row in a protected state (or with history) at the fixture revision, with the `last_promotion` columns;
- for `last_promotion`: a promotion receipt (`idempotency_key` `m3c-seed-<receipt id>`, fingerprint the D-006 digest of `{"m3c_seeded_promotion": <receipt id>}`, revision parsed from the receipt id) and a `promote` workflow event (`from_state` null, the fixture does not say);
- every `consumed_record_ids` entry (the last promotion's record under the seed promotion key, others under `m3c-seed-consumed-<id>`).

Then validation on the closed ledger, failing closed with `{"refused_setup": {"case", "reason"}}` without running the case: the state is a state named by the active bundle; revision at least 1 and at least the number of revision-changing workflow events (`artifact`, `promote`; the production `create` event is at revision 0 and is not counted); the last-promotion columns all or none, its receipt belongs to this workflow at a revision between 1 and the current one, matches `promotion:<workflow>:<revision>`, has its `promote` event with the record, and its record is consumed; consumed records equal `consumed_record_ids`; revoked evaluators equal `revoked_evaluator_ids`; no receipt exists for any step's idempotency key and no consumed record is presented by any step. Then production `open` for both stores and a new broker. Negative tests: `m3/closure/companions/seed-negatives.jsonl` (6 cases, all refused). Production itself still cannot create protected initial states (`WORKFLOW_INITIAL_STATE_PROTECTED`, `transition_tests`).

## 5. Inspection (D-037 item 7)

Final state is read through read-only connections only after the case closes every store (no owner, quiescent); seeding opens a read-write connection only while every store is closed. The adapter refuses either while a store is open.

## 6. Companions (D-037 item 4, I-5)

`m3/closure/companions/transition-companions.jsonl`: 18 mechanical-only companion cases (`meta.label` "mechanical-only companion; not human-signoff coverage", `meta.companion_of` the frozen case). Inputs are the frozen inputs with the requirement sets (initial and `activate_policy`) reduced to the mechanical criterion `build` and inline records reduced to `build` plus the non-mandatory `style` (record digest recomputed); m3c-13 and m3c-38 replace the null-permit steps by the untrusted-evaluator record of sc-12 (a null permit is not a refusal in production). Expectations are written by hand from the invariant. sc-10 has no exact companion: 16 concurrent mechanical promotions always commit exactly once, but the 15 refusal codes split nondeterministically between the use-time kernel decision (`STATE_REVISION_CONFLICT`) and the ledger compare-and-set (`REVISION_CONFLICT`).
