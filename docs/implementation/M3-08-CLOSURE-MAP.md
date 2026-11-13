# M3-08 closure map: frozen M1 broker and transition cases against the production broker

**Status (2026-11-13): COMPLETE. Scoped M3 development acceptance (D-039)** on code `b60bf33`, required CI run [43518287362](https://github.com/infinri/Writ/actions/runs/43518287362), 6/6, closure gate step GATE PASS with report verification (E-074). The gate has zero unexplained and zero unclassified cases: every one of the 127 frozen cases runs through production code and is classified individually. This is **not 127/127 exact parity** and never will be: 25 cases are deferred obligations carried to M4 (human signoff; not passes) and 21 are exact approved production differences. The gate is a required CI step (E-073); its `--verify-report` revalidates current fixture bytes (D-039 correction, E-074). Frozen fixtures are unchanged (pinned by SHA-256). Evidence: E-072, E-073 in [TEST-EVIDENCE](TEST-EVIDENCE.md). Decisions: [D-036, D-037, D-038](DECISION-LOG.md). Accepted limitations (D-039): declared provenance of `sc-12`'s trusted record and manually reviewed cascade reasons; any change to either needs an independent review (CO-HR-01). Carried obligations: TASKS "M3 carried obligations".

## What runs

- **Adapter** (`persistence/sqlite/examples/m3_line_adapter/`, Cargo example of `writ-ledger-sqlite`, never shipped): drives the production `Broker` (`execute`, `grants().approve`, `present_permit`, `reconcile`, `promote`, `administer`, `ActivePolicy::activate`) on a fresh production ledger, effect store and stores per case, and appends one adaptation-log line per case (`WRIT_M3_ADAPTATIONS`). Test keys, authorities and sources only: they do not establish production authentication, key isolation or host security.
- **Test preconditions (D-037 item 1):** production administration first; whatever production cannot express (protected state, revision, last promotion, receipts, consumption) is written by the adapter's seeder in one transaction while no store is open, validated for consistency (never the outcome under test), then the production `open` path and broker run. Labelled `seeded-preconditions`. Separate production tests prove production cannot create protected initial states or bypass promotion (`d037_tests`). Inconsistent seeds are refused (`companions/seed-negatives.jsonl`, 6 cases).
- **Harness policy (item 2):** per-case scoped bundles declared in `m3/closure/bundles/cases.json` (14 bundles; digests computed, never asserted; policy changes activate a real distinct bundle; never chosen from client permits). Bindings and every transformation: [`m3/closure/bindings.md`](../../m3/closure/bindings.md).
- **Evidence (item 3; D-038 item 6):** test-MAC (HMAC, a MAC, not a signature) acceptance records, purpose-separated keys, registry entries through administration and synthetic attestations with a declared runner outcome; trusted records are issued once at setup; later inline agent records are never authority. `sc-12`'s trusted record is declared by hand in `m3/closure/bundles/trusted-records.json` (what the acceptance service would issue); no inline record of that case is issued (its content equals the step-1 record; independence rests on the declared provenance, which no tool can check). `sc-25`/`m3c-25` use the first presentation of another case's record (D-037 item 3). Fixture defects are kept. End-to-end runner-produced attestations stay covered by `runner_tests` (E-069).
- **Gate** (`m3/closure/check.py`, entries `m3/closure/exceptions.json`, self-tests `test_check.py`, 31): kinds `adapted` (exact frozen output through the listed adaptations), `difference` (approved production difference: exact actual output and exactly the differing steps and final members), `deferred` (D-037 item 4 only: carried to M4; first differing step must be the `ACCEPTANCE_HUMAN_UNAVAILABLE` refusal; D-038 item 6 fields `blocking_dependency`, `observed_m3_refusal` (checked equal to that step's answer), `companions` (checked equal to the companions naming the case), and `cascade` (a reason for every later differing step; free text, its truth is reviewed, not machine-checked)). It fails on any unexplained, unclassified, mislabelled, missing, stale, not-driven or nondeterministic case, blanket or imprecise entries, duplicate ids, unpinned companion lists, or a deferral without companion. `--verify-report` rejects a report whose inputs changed; `--dump-outputs` writes every output and adaptation log (used to prove adapter changes leave frozen outputs byte-identical).

```
source m1/tools/env.sh
cargo build --release --locked -p writ-ledger-sqlite --example m3_line_adapter
python3 m3/closure/check.py --proto target/release/examples/m3_line_adapter --repeat 2 --report /tmp/m3-08.json
python3 -m unittest discover -s m3/closure
```

## Result (2026-11-13, `6c79819`, `--repeat 2`, deterministic)

| Classification | Cases |
|---|---|
| Exact pass (no adaptation) | 17 |
| Adapted coverage (exact frozen output through approved adaptations) | 64 |
| Approved production differences (exact) | 21 |
| Deferred to M4 (human signoff, D-037 item 4, D-038 item 6; shown separately, **not passes**) | 25 |
| Unexplained / unclassified | 0 / 0 |
| Companions: mechanical-only (not human-signoff coverage) 27; seed negatives 6 | 33/33 exact |
| **Gate** | **PASS (scoped M3; not 127/127 exact parity)** |

**M4 carry-over (confirmed, D-038 item 6), 25 cases:** the 19 successful-promotion expectations `sc-01`..`sc-04`, `sc-10`, `sc-12`, `sc-13`, `sc-15`, `sc-38`, `sc-39`, `sc-53`, `sc-54`, `sc-56`..`sc-59`, `sc-63`, `sc-65`, `p-16`, and 6 refusal cases whose targeted check is masked by the human-signoff refusal: `sc-09`, `sc-19`, `sc-25`, `sc-36`, `sc-50`, `sc-51`. Each entry in `exceptions.json` carries its intended assertion, blocking dependency, exact observed M3 refusal, companions, cascade reasons and the M4 acceptance criterion; every one has a mechanical-only companion (`sc-10`: `m3c-10-race-16`, replacing the former waiver). These are deferred obligations, not passes; human signoff is not bypassed, removed or reclassified.

## Owner decisions applied (D-038; formerly OQ-13..OQ-17)

- **OQ-13 (item 1):** option A. Authenticated embedded evaluations are authoritative; `evaluation-record-scope/1` is superseded for the authorization path (never ingested; history kept). Enforcement map and gaps: [M3-ACCEPTANCE-CONTRACTS](M3-ACCEPTANCE-CONTRACTS.md).
- **OQ-14 (item 2):** promotions answer `REVISION_CONFLICT` for a stale expected revision whether detected at the use-time decision or in the transaction (`Refusal::RevisionConflict(ConflictStage)`, stage kept as operator diagnostic; kernel, execute and approvals unchanged). `sc-06` is now adapted coverage; `sc-20` re-recorded (`REVISION_CONFLICT` versus frozen `PERMIT_REQUEST_MISMATCH`); `sc-04`, `sc-15`, `sc-54` re-recorded; `sc-10` deterministic (20 repeats).
- **OQ-15 (item 3):** `sc-27` exact difference, step 0 `WORKFLOW_UNKNOWN` versus frozen `REQUEST_SCOPE_MISMATCH`; real cross-scope and malformed bindings stay scope errors (`d038_tests`, `m3c-27`); `sc-21` cites it.
- **OQ-16 (item 4):** `sc-26` exact difference, step 0 `ARTIFACT_INTEGRITY_VIOLATION` versus frozen `PERMIT_BINDING_MISMATCH` (snap-2 not in the trusted store, not manufactured); the binding check is reached with two declared trusted snapshots with distinct digests (`m3c-26`, `d038_tests`).
- **OQ-17 (item 5):** stage-aware precedence under the C4 partial outage, documented in `broker/src/execute.rs` and `broker/src/workflow.rs`: ingress, trusted time, every use-time decision refusal and missing approval come before any lifecycle transaction; anything at or after T-reserve is `LEDGER_UNAVAILABLE` with no progress; a failed refusal audit never changes the answer; promotions read their durable receipt first (storage failure there is `LEDGER_UNAVAILABLE`). The property oracle has one expected code per generated step (no either-code allowance).

## Property, storage and chaos coverage

- **Broker property gate** (`m3/props/`, oracle-only against the adapter, M1 generators read-only): CI profile seed 20261113, 950 sequences, 1008 restart, refusal and inspection twins, 80 concurrent groups (16/32/64) x 3, 20 storage-fault concurrent groups x 3, 18,430 checks, 0 failed (local, 122 s); self-tests 31 including mutants for wrong C4 precedence both ways, reserve-stage codes, hidden consumption, dispatch, effects or uncertain commits, restart healing an outage, per-key status swaps and wrong recovery answers.
  - Dependency status persists across `restart_broker` until `set_dependency ... up` (restart-under-outage cases re-enabled; restart never heals the fault).
  - C4 steps: one expected code from the reference model's deciding stage; read-only inspection twins (`ledger_detail`, stores closed first, quiescent) prove nothing was reserved, consumed, dispatched or applied and no possible progress.
  - Fault groups (`concurrent_faults`): faults at reserve, dispatch and observe (cannot begin, commit lost, commit applied with outcome unknown); per-key serialization (fingerprint and grant per receipt), no grant overuse, at most one dispatch per key, no duplicate or unauthorized effect, then restart, retries (one exact expected answer each) and evidence-only reconciliation.
  - Limitations: no adapter (effect-store) transaction faults or `effect_fails` inside groups; same-type operations on distinct single-use keys are interchangeable in the model; final state after recovery is checked against the recovery model, not a serialization; retries capped at 8 keys per group; `apply_without_permit` and absent permits excluded (D-037 item 5).
- **Production-native transition properties** (`persistence/sqlite/src/transition_props.rs`, D-038 item 7): 6 fixed seeds, mechanical-only trusted test policy, compare-and-set, replay, same key different request, consumed-record and attestation reuse, binding mutations and concurrent invalidation (artifact update, policy activation, evaluator revocation from another thread) checked against an independent model. Limitation: in the concurrent phase, policy and evaluator invalidations are checked against allowed outcomes only (admin and workflow event orders are separate sequences); the sequential phase checks them exactly. The M1 transition generators themselves are not applicable without re-specification (client promotion permits, inline records, client reconcile) and are not run.
- **Production regressions** (`persistence/sqlite/src/d038_tests.rs`, 14): revision conflict at both stages (a real competing commit between decision and transaction), replay before re-evaluation, 16-way mechanical-only promotion race (5 rounds: one commit, 15 `REVISION_CONFLICT`, evidence consumed once), missing workflow versus cross-scope mismatch without probing, integrity versus binding precedence without state change or consumption, C4 stage order for execute and promotion, time-floor failure first.
- **Storage failures** (`persistence/sqlite/tests/storage_failures.rs`, 6) and **chaos** (`kill_tests`, run alone): unchanged from E-072.

## Per-case classification (127)

| Set | Case | Classification | Decision | Adaptations | Steps / final members that differ |
|---|---|---|---|---|---|
| broker-scenarios | `b-01-allow-path` | exact pass |  | none |  |
| broker-scenarios | `b-02-deny-never-reaches-adapter` | exact pass |  | none |  |
| broker-scenarios | `b-03-unsealed` | adapted coverage | D-037 item 7 | C1-ingress-audit |  |
| broker-scenarios | `b-04-forged-seal` | adapted coverage | D-037 item 7 | C1-ingress-audit |  |
| broker-scenarios | `b-05-unregistered-adapter` | adapted coverage | D-037 item 7 | C1-ingress-audit |  |
| broker-scenarios | `b-06-adapter-vouches-for-other-issuer` | adapted coverage | D-037 item 7 | C1-ingress-audit |  |
| broker-scenarios | `b-07-altered-after-sealing` | adapted coverage | D-037 item 7 | C1-ingress-audit |  |
| broker-scenarios | `b-08-apply-without-permit` | approved difference | D-037 item 5 | production-difference | steps [0]; final ['refusals'] |
| broker-scenarios | `b-09-forged-permit` | exact pass |  | none |  |
| broker-scenarios | `b-10-permit-replay` | exact pass |  | none |  |
| broker-scenarios | `b-11-permit-for-other-operation` | exact pass |  | none |  |
| broker-scenarios | `b-12-expired-permit` | exact pass |  | none |  |
| broker-scenarios | `b-13-extended-expiry-without-resign` | exact pass |  | none |  |
| broker-scenarios | `b-14-approval-required-without-grant` | exact pass |  | none |  |
| broker-scenarios | `b-15-grant-single-use` | adapted coverage | D-037 item 7 | C2-approval-sealing, C3-synthetic-human-authority, adapter-sealed |  |
| broker-scenarios | `b-16-grant-bound-to-resource` | adapted coverage | D-037 item 7 | C2-approval-sealing, C3-synthetic-human-authority, adapter-sealed |  |
| broker-scenarios | `b-17-grant-bound-to-capabilities` | adapted coverage | D-037 item 7 | C2-approval-sealing, C3-synthetic-human-authority, adapter-sealed |  |
| broker-scenarios | `b-18-no-subagent-inheritance` | adapted coverage | D-037 item 7 | C2-approval-sealing, C3-synthetic-human-authority, adapter-sealed |  |
| broker-scenarios | `b-19-policy-change-invalidates-grant` | adapted coverage | D-037 item 7 | C2-approval-sealing, C3-synthetic-human-authority, adapter-sealed |  |
| broker-scenarios | `b-20-expired-grant` | adapted coverage | D-037 item 7 | C2-approval-sealing, C3-synthetic-human-authority, adapter-sealed |  |
| broker-scenarios | `b-21-denied-verdict` | adapted coverage | D-037 item 7 | C2-approval-sealing, C3-synthetic-human-authority, adapter-sealed |  |
| broker-scenarios | `b-22-forbid-not-approvable` | adapted coverage | D-037 item 7 | C2-approval-sealing, C3-synthetic-human-authority, adapter-sealed |  |
| broker-scenarios | `b-23-unknown-grant` | exact pass |  | none |  |
| broker-scenarios | `b-24-wrong-credential` | adapted coverage | D-037 item 7 | C2-approval-sealing, adapter-sealed |  |
| broker-scenarios | `b-25-untrusted-channel` | adapted coverage | D-037 item 7 | C2-approval-sealing, adapter-sealed |  |
| broker-scenarios | `b-26-self-approval` | adapted coverage | D-037 item 7 | C2-approval-sealing, C3-synthetic-human-authority, adapter-sealed |  |
| broker-scenarios | `b-27-grant-race-16` | adapted coverage | D-037 item 7 | C2-approval-sealing, C3-synthetic-human-authority, adapter-sealed |  |
| broker-scenarios | `b-28-refusal-before-reservation-keeps-grant` | adapted coverage | D-037 item 7 | C2-approval-sealing, C3-synthetic-human-authority, C4-partial-transaction-unavailability, adapter-sealed |  |
| broker-scenarios | `b-29-crash-before-reserve` | adapted coverage | D-037 item 7 | C2-approval-sealing, C3-synthetic-human-authority, C5-injected-crash-reopen, adapter-sealed |  |
| broker-scenarios | `b-30-idempotent-retry` | exact pass |  | none |  |
| broker-scenarios | `b-31-same-key-different-request` | exact pass |  | none |  |
| broker-scenarios | `b-32-same-key-other-grant` | adapted coverage | D-037 item 7 | C2-approval-sealing, C3-synthetic-human-authority, adapter-sealed |  |
| broker-scenarios | `b-33-same-key-same-grant` | adapted coverage | D-037 item 7 | C2-approval-sealing, C3-synthetic-human-authority, adapter-sealed |  |
| broker-scenarios | `b-34-concurrent-duplicates` | exact pass |  | none |  |
| broker-scenarios | `b-35-crash-after-reserve-resumes` | adapted coverage | D-037 item 7 | C2-approval-sealing, C3-synthetic-human-authority, C5-injected-crash-reopen, adapter-sealed |  |
| broker-scenarios | `b-36-crash-after-dispatch-reconcile` | approved difference | D-036 item 4; D-035 item 3 | C5-injected-crash-reopen | steps [2]; final ['receipts', 'refusals'] |
| broker-scenarios | `b-37-crash-after-dispatch-retry` | adapted coverage | D-037 item 7 | C5-injected-crash-reopen |  |
| broker-scenarios | `b-38-effect-failure-recorded` | adapted coverage | D-037 item 7 | C6-controlled-effect-failure |  |
| broker-scenarios | `b-39-policy-down` | exact pass |  | none |  |
| broker-scenarios | `b-40-reconcile-nothing` | exact pass |  | none |  |
| broker-scenarios | `b-41-grant-after-deadline` | adapted coverage | D-037 item 7 | C2-approval-sealing, C3-synthetic-human-authority, adapter-sealed |  |
| broker-scenarios | `b-42-permit-bounded-by-deadline` | exact pass |  | none |  |
| broker-scenarios | `b-43-unknown-capability` | exact pass |  | none |  |
| broker-scenarios | `b-44-permit-replay-after-restart` | exact pass |  | none |  |
| broker-scenarios | `b-45-retry-after-restart` | adapted coverage | D-037 item 7 | C2-approval-sealing, C3-synthetic-human-authority, adapter-sealed |  |
| broker-scenarios | `b-46-reconcile-after-restart` | adapted coverage | D-037 item 7 | C5-injected-crash-reopen |  |
| protocol-broker | `p-09-broker-unknown-op` | adapted coverage | D-037 item 7 | C7-protocol-unknown-op |  |
| protocol-broker | `p-10-broker-grant-ref-reuse` | adapted coverage | D-037 item 7 | C2-approval-sealing, C3-synthetic-human-authority, adapter-sealed |  |
| protocol-broker | `p-11-broker-permit-never-issued` | approved difference | D-037 item 5 | C2-approval-sealing, C3-synthetic-human-authority, adapter-sealed, production-difference | steps [1]; final ['refusals'] |
| protocol-broker | `p-12-broker-unknown-dependency-name` | adapted coverage | D-037 item 7 | C8-protocol-input-validation |  |
| protocol-broker | `p-13-broker-extra-dependency` | adapted coverage | D-037 item 7 | C8-protocol-input-validation |  |
| protocol-broker | `p-14-broker-unknown-fault` | adapted coverage | D-037 item 7 | C8-protocol-input-validation |  |
| protocol-broker | `p25-01-broker-dependency-policy-missing` | adapted coverage | D-037 item 7 | C8-protocol-input-validation |  |
| protocol-broker | `p25-02-broker-dependency-ledger-missing` | adapted coverage | D-037 item 7 | C8-protocol-input-validation |  |
| protocol-broker | `p25-03-broker-dependency-both-missing` | adapted coverage | D-037 item 7 | C8-protocol-input-validation |  |
| transition-scenarios | `sc-01-promote` | deferred to M4 | D-037 item 4 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, seeded-preconditions, test-authenticated-evidence | steps [0]; final ['consumed_record_ids', 'receipts', 'refusals', 'revision', 'state'] |
| transition-scenarios | `sc-02-identical-retry` | deferred to M4 | D-037 item 4 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, seeded-preconditions, test-authenticated-evidence | steps [0, 1]; final ['consumed_record_ids', 'receipts', 'refusals', 'revision', 'state'] |
| transition-scenarios | `sc-03-same-key-different-request` | deferred to M4 | D-037 item 4 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, seeded-preconditions, test-authenticated-evidence | steps [0, 1]; final ['consumed_record_ids', 'receipts', 'refusals', 'revision', 'state'] |
| transition-scenarios | `sc-04-record-replay-new-key` | deferred to M4 | D-037 item 4; D-038 item 2 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, seeded-preconditions, test-authenticated-evidence | steps [0, 1]; final ['consumed_record_ids', 'receipts', 'refusals', 'revision', 'state'] |
| transition-scenarios | `sc-05-artifact-replaced-before-promotion` | adapted coverage | D-037 item 1; D-037 item 2; D-037 item 3; D-037 item 5; D-037 item 7 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, seeded-preconditions, test-authenticated-evidence |  |
| transition-scenarios | `sc-06-stale-cas` | adapted coverage | D-037 item 1; D-037 item 2; D-037 item 5; D-037 item 7; D-038 item 2 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, seeded-preconditions |  |
| transition-scenarios | `sc-07-snapshot-swapped-same-digest` | adapted coverage | D-037 item 1; D-037 item 2; D-037 item 3; D-037 item 5; D-037 item 7 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, seeded-preconditions, test-authenticated-evidence |  |
| transition-scenarios | `sc-08-policy-changed` | adapted coverage | D-037 item 1; D-037 item 2; D-037 item 3; D-037 item 5; D-037 item 7 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, seeded-preconditions, test-authenticated-evidence |  |
| transition-scenarios | `sc-09-kernel-did-not-allow` | deferred to M4 | D-037 item 4; D-036 item 2 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, seeded-preconditions, test-authenticated-evidence | steps [0]; final [] |
| transition-scenarios | `sc-10-race-16` | deferred to M4 | D-037 item 4; D-038 item 2 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, seeded-preconditions, test-authenticated-evidence | steps [0]; final ['consumed_record_ids', 'receipts', 'refusals', 'revision', 'state'] |
| transition-scenarios | `sc-11-expired` | adapted coverage | D-037 item 1; D-037 item 2; D-037 item 3; D-037 item 5; D-037 item 7 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, seeded-preconditions, test-authenticated-evidence |  |
| transition-scenarios | `sc-12-refusal-is-side-effect-free` | deferred to M4 | D-037 item 4 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, seeded-preconditions, test-authenticated-evidence | steps [0, 1]; final ['consumed_record_ids', 'receipts', 'refusals', 'revision', 'state'] |
| transition-scenarios | `sc-13-refused-key-reusable` | deferred to M4 | D-037 item 4; D-036 item 2 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, seeded-preconditions, test-authenticated-evidence | steps [0, 1]; final ['consumed_record_ids', 'receipts', 'refusals', 'revision', 'state'] |
| transition-scenarios | `sc-14-malformed-record` | adapted coverage | D-037 item 1; D-037 item 2; D-037 item 3; D-037 item 5; D-037 item 7 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, seeded-preconditions, test-authenticated-evidence |  |
| transition-scenarios | `sc-15-workflow-already-moved` | deferred to M4 | D-037 item 4; D-038 item 2 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, seeded-preconditions, test-authenticated-evidence | steps [0]; final ['consumed_record_ids', 'receipts', 'refusals', 'revision', 'state'] |
| transition-scenarios | `sc-16-cross-project-record` | adapted coverage | D-037 item 1; D-037 item 2; D-037 item 3; D-037 item 5; D-037 item 7 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, seeded-preconditions, test-authenticated-evidence |  |
| transition-scenarios | `sc-17-transition-not-evaluated` | adapted coverage | D-037 item 1; D-037 item 2; D-037 item 3; D-037 item 5; D-037 item 7 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, seeded-preconditions, test-authenticated-evidence |  |
| transition-scenarios | `sc-18-permit-reused-for-other-target` | approved difference | D-036 item 2; D-037 item 2 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, seeded-preconditions | steps [0]; final [] |
| transition-scenarios | `sc-19-permit-actor-changed` | deferred to M4 | D-037 item 4; D-036 item 2 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, seeded-preconditions, test-authenticated-evidence | steps [0]; final [] |
| transition-scenarios | `sc-20-permit-revision-changed` | approved difference | D-036 item 2; D-038 item 2 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, seeded-preconditions | steps [0]; final [] |
| transition-scenarios | `sc-21-permit-scope-changed` | approved difference | D-036 item 2; D-038 item 3 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, seeded-preconditions | steps [0]; final [] |
| transition-scenarios | `sc-22-permit-snapshot-attribute-changed` | approved difference | D-036 item 2 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, seeded-preconditions, test-authenticated-evidence | steps [0]; final [] |
| transition-scenarios | `sc-23-permit-digest-attribute-changed` | approved difference | D-036 item 2 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, seeded-preconditions, test-authenticated-evidence | steps [0]; final [] |
| transition-scenarios | `sc-24-permit-record-id-attribute-changed` | approved difference | D-036 item 2 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, seeded-preconditions | steps [0]; final [] |
| transition-scenarios | `sc-25-record-swapped-under-permit` | deferred to M4 | D-037 item 4; D-036 item 2 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, seeded-preconditions, test-authenticated-evidence | steps [0]; final [] |
| transition-scenarios | `sc-26-attributes-disagree-with-record` | approved difference | D-038 item 4 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, seeded-preconditions, test-authenticated-evidence | steps [0]; final [] |
| transition-scenarios | `sc-27-other-workflow` | approved difference | D-038 item 3 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, seeded-preconditions | steps [0]; final [] |
| transition-scenarios | `sc-28-composite-at-broker` | adapted coverage | D-037 item 1; D-037 item 2; D-037 item 5; D-037 item 7 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, seeded-preconditions |  |
| transition-scenarios | `sc-29-incomplete-record` | adapted coverage | D-037 item 1; D-037 item 2; D-037 item 3; D-037 item 5; D-037 item 7 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, seeded-preconditions, test-authenticated-evidence |  |
| transition-scenarios | `sc-30-evaluator-revoked` | adapted coverage | D-037 item 1; D-037 item 2; D-037 item 3; D-037 item 5; D-037 item 7 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, seeded-preconditions, test-authenticated-evidence |  |
| transition-scenarios | `sc-31-unprotected-without-acceptance` | adapted coverage | D-037 item 2; D-037 item 5; D-037 item 7 | adapter-sealed, client-permit-dropped, harness-authorization-bundle |  |
| transition-scenarios | `sc-32-protected-without-acceptance` | approved difference | D-037 item 6 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, seeded-preconditions | steps [0]; final [] |
| transition-scenarios | `sc-33-alternate-route-to-protected-state` | approved difference | D-037 item 6 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, seeded-preconditions | steps [1]; final [] |
| transition-scenarios | `sc-34-alternate-route-with-old-record` | adapted coverage | D-037 item 1; D-037 item 2; D-037 item 3; D-037 item 5; D-037 item 7 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, seeded-preconditions, test-authenticated-evidence |  |
| transition-scenarios | `sc-35-unprotected-with-acceptance-binding` | adapted coverage | D-037 item 2; D-037 item 5; D-037 item 7 | adapter-sealed, client-permit-dropped, harness-authorization-bundle |  |
| transition-scenarios | `sc-36-authorization-bundle-changed` | deferred to M4 | D-037 item 4; D-036 item 2; D-037 item 2 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, seeded-preconditions, test-authenticated-evidence | steps [1]; final [] |
| transition-scenarios | `sc-37-own-kind-in-capabilities` | adapted coverage | D-037 item 1; D-037 item 2; D-037 item 5; D-037 item 7 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, seeded-preconditions |  |
| transition-scenarios | `sc-38-repeated-refusals-then-success` | deferred to M4 | D-037 item 4; D-036 item 2 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, seeded-preconditions, test-authenticated-evidence | steps [0, 1, 2]; final ['consumed_record_ids', 'receipts', 'refusals', 'revision', 'state'] |
| transition-scenarios | `sc-39-crash-before-commit-then-retry` | deferred to M4 | D-037 item 4 | C5-injected-crash-reopen, adapter-sealed, client-permit-dropped, harness-authorization-bundle, seeded-preconditions, test-authenticated-evidence | steps [1]; final ['consumed_record_ids', 'receipts', 'refusals', 'revision', 'state'] |
| transition-scenarios | `sc-40-effect-retry` | adapted coverage | D-037 item 1; D-037 item 2; D-037 item 5; D-037 item 7 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, sealed-execute-effect, seeded-preconditions |  |
| transition-scenarios | `sc-41-interrupted-dispatch-retry` | approved difference | D-037 item 5; D-035 item 3 | C5-injected-crash-reopen, adapter-sealed, client-permit-dropped, harness-authorization-bundle, sealed-execute-effect, seeded-preconditions | steps []; final ['effects'] |
| transition-scenarios | `sc-42-interrupted-dispatch-other-request` | approved difference | D-037 item 5; D-035 item 3 | C5-injected-crash-reopen, adapter-sealed, client-permit-dropped, harness-authorization-bundle, sealed-execute-effect, seeded-preconditions | steps []; final ['effects'] |
| transition-scenarios | `sc-43-reconciled-applied` | approved difference | D-036 item 3; D-037 item 5 | C5-injected-crash-reopen, adapter-sealed, client-permit-dropped, harness-authorization-bundle, production-difference, sealed-execute-effect, seeded-preconditions | steps [1]; final [] |
| transition-scenarios | `sc-44-reconciled-not-applied` | approved difference | D-036 item 3; D-037 item 5 | C5-injected-crash-reopen, adapter-sealed, client-permit-dropped, harness-authorization-bundle, production-difference, sealed-execute-effect, seeded-preconditions | steps [1, 2]; final [] |
| transition-scenarios | `sc-45-crash-before-reserve` | adapted coverage | D-037 item 1; D-037 item 2; D-037 item 5; D-037 item 7 | C5-injected-crash-reopen, adapter-sealed, client-permit-dropped, harness-authorization-bundle, sealed-execute-effect, seeded-preconditions |  |
| transition-scenarios | `sc-46-effect-refused-then-valid` | approved difference | D-037 item 5 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, sealed-execute-effect, seeded-preconditions | steps [0, 1]; final ['refusals'] |
| transition-scenarios | `sc-47-reconcile-nothing` | approved difference | D-037 item 6; D-036 item 3 | harness-authorization-bundle, production-difference, seeded-preconditions | steps [0]; final ['refusals'] |
| transition-scenarios | `sc-48-duplicate-key-input` | adapted coverage | D-037 item 7 | C8-protocol-input-validation |  |
| transition-scenarios | `sc-49-decimal-revision-input` | adapted coverage | D-037 item 7 | C8-protocol-input-validation |  |
| transition-scenarios | `sc-08b-policy-changed-compatible` | adapted coverage | D-037 item 1; D-037 item 2; D-037 item 3; D-037 item 5; D-037 item 7 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, seeded-preconditions, test-authenticated-evidence |  |
| transition-scenarios | `sc-50-protection-classification-missing` | deferred to M4 | D-037 item 4; D-037 item 1 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, seeded-preconditions, test-authenticated-evidence | steps [0]; final [] |
| transition-scenarios | `sc-51-protection-from-other-bundle` | deferred to M4 | D-037 item 4; D-037 item 1 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, seeded-preconditions, test-authenticated-evidence | steps [0]; final [] |
| transition-scenarios | `sc-52-protected-edge-without-record` | approved difference | D-037 item 6 | adapter-sealed, client-permit-dropped, harness-authorization-bundle | steps [0]; final [] |
| transition-scenarios | `sc-53-protected-edge-with-record` | deferred to M4 | D-037 item 4 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, test-authenticated-evidence | steps [0]; final ['consumed_record_ids', 'receipts', 'refusals', 'revision', 'state'] |
| transition-scenarios | `sc-54-edge-record-reused-for-destination` | deferred to M4 | D-037 item 4; D-038 item 2 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, test-authenticated-evidence | steps [0, 1]; final ['consumed_record_ids', 'receipts', 'refusals', 'revision', 'state'] |
| transition-scenarios | `sc-55-publish-before-acceptance` | adapted coverage | D-037 item 1; D-037 item 2; D-037 item 5; D-037 item 7 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, sealed-execute-effect, seeded-preconditions |  |
| transition-scenarios | `sc-56-accept-a-publish-b` | deferred to M4 | D-037 item 4 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, sealed-execute-effect, seeded-preconditions, test-authenticated-evidence | steps [0, 1]; final ['consumed_record_ids', 'receipts', 'refusals', 'revision', 'state'] |
| transition-scenarios | `sc-57-accepted-artifact-modified` | deferred to M4 | D-037 item 4 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, sealed-execute-effect, seeded-preconditions, test-authenticated-evidence | steps [0, 1, 2]; final ['consumed_record_ids', 'receipts', 'refusals', 'revision', 'state'] |
| transition-scenarios | `sc-58-acceptance-policy-changed-after-promotion` | deferred to M4 | D-037 item 4 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, sealed-execute-effect, seeded-preconditions, test-authenticated-evidence | steps [0, 2]; final ['consumed_record_ids', 'receipts', 'refusals', 'revision', 'state'] |
| transition-scenarios | `sc-59-authorization-changed-after-promotion` | deferred to M4 | D-037 item 4 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, sealed-execute-effect, seeded-preconditions, test-authenticated-evidence | steps [0, 2]; final ['consumed_record_ids', 'receipts', 'refusals', 'revision', 'state'] |
| transition-scenarios | `sc-60-missing-promotion-receipt` | adapted coverage | D-037 item 1; D-037 item 2; D-037 item 5; D-037 item 7 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, sealed-execute-effect, seeded-preconditions |  |
| transition-scenarios | `sc-61-forged-promotion-receipt` | adapted coverage | D-037 item 1; D-037 item 2; D-037 item 5; D-037 item 7 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, sealed-execute-effect, seeded-preconditions |  |
| transition-scenarios | `sc-62-publication-permit-other-target` | approved difference | D-037 item 5; D-037 item 2 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, sealed-execute-effect, seeded-preconditions | steps [0]; final [] |
| transition-scenarios | `sc-63-publish-after-promotion` | deferred to M4 | D-037 item 4 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, sealed-execute-effect, seeded-preconditions, test-authenticated-evidence | steps [0, 1]; final ['consumed_record_ids', 'effects', 'receipts', 'refusals', 'revision', 'state'] |
| transition-scenarios | `sc-64-concurrent-invalidation-and-publication` | adapted coverage | D-037 item 1; D-037 item 2; D-037 item 5; D-037 item 7 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, sealed-execute-effect, seeded-preconditions |  |
| transition-scenarios | `sc-65-uncertain-publication` | deferred to M4 | D-037 item 4 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, sealed-execute-effect, seeded-preconditions, test-authenticated-evidence | steps [0, 1, 2]; final ['consumed_record_ids', 'effects', 'receipts', 'refusals', 'revision', 'state'] |
| protocol-transition | `p-04-transition-missing-steps` | adapted coverage | D-037 item 7 | C8-protocol-input-validation |  |
| protocol-transition | `p-05-transition-initial-missing-state` | adapted coverage | D-037 item 7 | C8-protocol-input-validation |  |
| protocol-transition | `p-06-transition-count-zero` | adapted coverage | D-037 item 7 | C8-protocol-input-validation |  |
| protocol-transition | `p-07-transition-unknown-fault` | adapted coverage | D-037 item 7 | C8-protocol-input-validation |  |
| protocol-transition | `p-08-transition-unknown-op` | adapted coverage | D-037 item 1; D-037 item 2; D-037 item 7 | C7-protocol-unknown-op, harness-authorization-bundle, seeded-preconditions |  |
| protocol-transition | `p-16-scenario-member-order` | deferred to M4 | D-037 item 4 | adapter-sealed, client-permit-dropped, harness-authorization-bundle, seeded-preconditions, test-authenticated-evidence | steps [0]; final ['consumed_record_ids', 'receipts', 'refusals', 'revision', 'state'] |


## The 45 M1 ledger tests (`m1/ledger_tests`)

They drive the M1 prototype CLI (`--ledger-db`, triggers added to the ledger file, stderr checks), so none runs unchanged against the production broker; production schema strictness forbids trigger injection (D-032, D-033 item 5). Classification:

| File (tests) | Test | Production status |
|---|---|---|
| test_d025 (14) | 7 `StoredGrantIntegrity` tests (unparsable, wrong members, extra or duplicate member, not canonical, wrong member type, well-formed but different binding) | Superseded: `grant_tests::a_malformed_stored_binding_is_a_ledger_integrity_failure_without_a_grant_use`, `a_grant_for_another_binding_is_a_mismatch`, `d035_tests::a_grant_with_damaged_provenance_is_not_executable` (production adds provenance, D-035 item 5) |
| | `test_resumed_reservation_changes_no_durable_state_and_uses_no_second_grant` | Superseded: `grant_tests::an_existing_receipt_gives_conflict_replay_reconciliation_or_resume`, `execute_tests::restart_resumes_reserved_and_never_redispatches_dispatched_or_reconciled`; production also revalidates (D-035 item 1, `d035_tests`) |
| | `test_unverified_permit_applies_no_effect` | Superseded: `execute_tests::adapter_verification_follows_the_d025_b_precedence`, `a_presented_permit_is_verified_but_never_applies_an_effect` |
| | `test_rejected_non_wal_file_is_byte_identical` | Superseded: `tests/initialization.rs` `non_database_file_is_refused_unchanged`, `unrelated_existing_database_is_refused_without_initialization` |
| | `test_valid_non_wal_ledger_is_accepted_and_switched_to_wal` | Approved production difference (D-037 item 8): `valid_schema_not_in_wal_mode_is_refused_unchanged` |
| | `test_created_file_removed_when_initialization_fails` | Superseded: `failed_creation_removes_the_files_it_created` |
| | `test_pre_existing_file_never_removed` | Superseded: `create_refuses_an_existing_file_and_open_refuses_a_missing_one` |
| | `test_missing_directory_stderr_is_clean` | Not applicable (M1 CLI stderr); production reports sanitized `StoreError` categories (S-5, `execute_tests` sanitized diagnostics) |
| test_storage_failures (13) | `test_missing_directory`, `test_not_a_database`, `test_wrong_schema_version`, `test_missing_table` | Superseded: `tests/initialization.rs` (create and open refusals, exact schema objects and metadata) |
| | `test_read_only_file`, `test_read_only_ledger_dir` | Covered: `tests/storage_failures.rs` (permission failure proven under the test identity first; fails loudly under elevated access) |
| | `test_lock_held_beyond_busy_timeout` | Superseded in substance: production fails closed on a second owner immediately (`tests/ownership.rs`); `busy_timeout` only governs foreign readers |
| | `test_reserve_failure_rolls_back_grant_use`, `test_adapter_failure_leaves_dispatched_and_never_redispatches`, `test_observe_failure_after_effect_is_uncertain` | Superseded: `grant_tests::injected_reserve_faults_at_begin_body_and_commit_leave_no_grant_use_and_no_receipt`, `execute_tests::storage_failure_at_every_lifecycle_stage_fails_closed_with_the_implied_crash_state`, `an_ambiguous_outcome_is_never_redispatched_and_reconciles_from_adapter_evidence` |
| | `test_audit_failure_keeps_refusal_and_continues` | Superseded: audit faults in `grant_tests` and `execute_tests` (`inject("audit", ...)`) |
| | `test_ledger_created_from_schema_file_is_accepted` | Approved production difference (D-037 item 8): production refuses M1-schema databases; only `writ-ledger-5` opens, upgrades are explicit operator operations |
| | `test_ledger_unavailable_maps_to_promotion_refused` | Kernel external-refusal mapping (M2 `external-refusal` set), not the broker |
| test_d029 (6) | `test_reconcile_never_touches_an_in_flight_execute`, `test_64_way_executes_and_reconciles_do_not_deadlock` | Superseded: `execute_tests::reconcile_waits_for_an_in_flight_execution_and_never_changes_it`, `concurrent_executions_and_reconciles_never_interfere` |
| | `test_post_return_reconciliation_is_unchanged` | Production differs by the approved D-035/D-036 change (evidence-based reconciliation, as `b-36`); `d035_tests::concurrent_reconciliations_record_each_outcome_once_from_evidence_only` |
| | `test_zero_row_transition_is_a_state_conflict_not_corruption`, `test_state_conflict_evidence_is_category_5` | Superseded: `execute_tests::a_receipt_changed_behind_the_broker_is_a_state_conflict_not_corruption` |
| | `test_cross_process_reconcile_stays_fail_closed` | Superseded: `tests/ownership.rs::reconciler_process_is_refused_while_a_broker_owns_the_ledger` |
| test_chaos (2) | `test_kill_restart_reconcile` | Superseded: `kill_tests::sigkill_at_every_lifecycle_boundary_leaves_the_implied_crash_state`, `sigkill_at_seeded_random_points_never_breaks_the_invariants` |
| | `test_unwritable_ledger_fails_closed_after_kill` | Covered: `tests/storage_failures.rs` (child SIGKILLed, permissions removed, reopen fails closed, files byte-identical) |
| test_d027 (7) | `test_start_rule_and_setup_accounting`, `test_top_level_steps_carry_no_worker_members`, `test_profile_option_accepted_with_either_ledger`, `test_bad_profile_option_is_a_usage_error`, `test_outputs_do_not_depend_on_the_profile` | Not applicable: M1 prototype concurrency profiles and timing instrumentation (D-027) |
| | `test_generated_groups_are_serializable_in_both_profiles`, `test_contention_invariants_in_both_profiles` | Superseded in substance: `grant_tests::sixty_four_threads_*`, `execute_tests::sixty_four_threads_executing_one_request_dispatch_it_once`; the adapter's `concurrent` path (`b-27`, `b-34` exact) |
| test_timing_integrity (3) | all three | Not applicable: M1 bench timing protocol (`bench-broker-scenario`); production has no timing output |

Totals (45): superseded by production tests 29; approved production differences 3 (non-WAL acceptance and M1 schema file, D-037 item 8; post-return reconciliation, D-035/D-036); covered by new production tests 3 (read-only file, read-only directory, unwritable ledger after kill: `tests/storage_failures.rs`); not applicable 10 (M1 CLI stderr 1, kernel external-refusal mapping 1, D-027 profiles 5, timing protocol 3).

