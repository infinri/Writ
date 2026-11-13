# M3-08 closure map: frozen M1 broker and transition cases against the production broker

**Status:** gate implemented and run; **gate FAILS** (recorded below, not tuned). Prepared by Claude for owner review; nothing here changes a frozen fixture, a decision or a recovery document. Task M3-08 ([TASKS](TASKS.md)); decisions [D-032 to D-036](DECISION-LOG.md); evidence of earlier milestones E-064 to E-068 in [TEST-EVIDENCE](TEST-EVIDENCE.md) (this run is not yet recorded there).

## What runs

- **Adapter:** `persistence/sqlite/examples/m3_line_adapter.rs` (Cargo example of `writ-ledger-sqlite`, never shipped). Speaks the M1 line protocol for `broker-scenario` and `transition-scenario` and drives the production `writ_broker::execute::Broker` (`execute`, `grants().approve`, `present_permit`, `reconcile`, `promote`, `administer`) on a fresh production `SqliteLedger` (`writ-ledger-5`) and `SqliteEffectStore` per case. Test keys, test authorities and test trusted sources only: **they do not establish production authentication** (approval service, operator authentication, key provisioning, acceptance service and immutable snapshot store remain M4 to M6 obligations).
- **Gate:** `m3/closure/check.py` with `m3/closure/exceptions.json`; self-tests `m3/closure/test_check.py`. The gate imports the M1 driver's primitives read-only, pins the four frozen fixture files by SHA-256, and passes only if every case is an exact pass or an approved exception whose recorded actual output matches exactly; an unlisted failure, an exception whose output changed, a stale exception, an undriven case, a missing answer or nondeterminism across repeated runs fails it.

```
source m1/tools/env.sh
cargo build --release --locked --example m3_line_adapter -p writ-ledger-sqlite
python3 m3/closure/check.py --proto target/release/examples/m3_line_adapter --repeat 2 --report /tmp/m3-08.json
python3 -m unittest discover -s m3/closure
```

## Result of this run (2026-11-13, base `e52e6ca`, `--repeat 2`, deterministic)

| | Cases |
|---|---|
| Exact passes | **59** (broker-scenarios 44, protocol-broker 8, transition-scenarios 3, protocol-transition 4) |
| Approved exceptions matched exactly | **1** (`b-36`) |
| Not driven (adapter answers `unsupported`) | **63** (2 broker, 61 transition; 14 of them are approved exceptions that cannot be driven yet) |
| Unexplained failures (driven, differ, no approved exception) | **4**: `sc-31`, `sc-52`, `sc-53`, `sc-54` |
| Exception mismatches, stale exceptions, missing answers | 0, 0, 0 |
| **Gate** | **FAIL** |

Unexplained failures, one line each:

- `sc-31-unprotected-without-acceptance`: production refuses `DEFAULT_DENY` (expected `committed` rev 8): the fixture transition bundle's statements cover only `workflow:fixture-tenant/fixture-project/`, the scenario's workflow is `t1/p1` (OQ-4).
- `sc-52-protected-edge-without-record`: production use-time kernel decision answers `ACCEPTANCE_BINDING_REQUIRED`, the frozen broker expectation is `ACCEPTANCE_REQUIRED`; state, revision and refusal count match (OQ-5).
- `sc-53-protected-edge-with-record`: `DEFAULT_DENY` instead of `committed` (OQ-4; even with a matching bundle, OQ-6 and OQ-7 apply).
- `sc-54-edge-record-reused-for-destination`: step 0 `DEFAULT_DENY`, step 1 `STATE_REVISION_CONFLICT` instead of `committed` then `ACCEPTANCE_REPLAYED` (OQ-4, OQ-6, OQ-7).

Cases the adapter does not yet drive (63): `b-08`, `p-11` (no production operation, OQ-1, OQ-2); `sc-50`, `sc-51` (protection supplied as scenario state, OQ-3); and every other transition case whose initial workflow is in `verifying` or `accepted` (production administration refuses to create it: `WORKFLOW_INITIAL_STATE_PROTECTED`, OQ-3): `sc-01` to `sc-30`, `sc-32` to `sc-34`, `sc-36` to `sc-47`, `sc-08b`, `sc-55` to `sc-65`, `p-08`, `p-16`.

## Approved exceptions (exceptions.json)

| Case | Decision | Exact difference | Driven |
|---|---|---|---|
| `b-36-crash-after-dispatch-reconcile` | D-036 item 4, D-035 item 3 | reconcile records `observed_success` from adapter evidence (count 1 as expected); the retry replays `{replayed: true, result: effect_completed}` instead of `RECONCILIATION_REQUIRED`; final receipts `{observed_success: 1}` instead of `{unknown_reconcile: 1}`, refusals 0 instead of 1; other final members equal | yes, matched exactly |
| `sc-43-reconciled-applied` | D-036 item 3 | client-asserted `reconcile {observed: applied}` is not provided | no (OQ-3) |
| `sc-44-reconciled-not-applied` | D-036 item 3 | client-asserted `reconcile {observed: not_applied}` is not provided; an uncertain key is never released for redispatch | no (OQ-3) |
| Promotion-permit cases (12, below) | D-036 item 2 | the expected code comes from the client-held promotion permit, which production does not have | no (OQ-3) |

**Exact promotion-permit case ids (D-036 item 2):** `sc-09-kernel-did-not-allow`, `sc-13-refused-key-reusable`, `sc-38-repeated-refusals-then-success` (null permit: `PERMIT_MISSING`); `sc-18-permit-reused-for-other-target`, `sc-19-permit-actor-changed`, `sc-20-permit-revision-changed`, `sc-21-permit-scope-changed`, `sc-22-permit-snapshot-attribute-changed`, `sc-23-permit-digest-attribute-changed`, `sc-24-permit-record-id-attribute-changed` (permit fingerprint differs: `PERMIT_REQUEST_MISMATCH`); `sc-25-record-swapped-under-permit` (permit `record_digest` differs from the inline record: `PERMIT_BINDING_MISMATCH`); `sc-36-authorization-bundle-changed` (permit names the replaced authorization bundle: `PERMIT_POLICY_STALE`). Identified mechanically: these are exactly the `promote` steps whose permit is null, whose `request_fingerprint` is not the D-006 digest of the request, whose `record_digest` differs from the record, or whose bundle digest was replaced before the step. `sc-26-attributes-disagree-with-record` is **not** a permit case (request attributes versus record; production keeps `PERMIT_BINDING_MISMATCH` for it). The effect-permit cases `sc-46` and `sc-62` are not promotion permits and are not covered (OQ-8).

`sc-47-reconcile-nothing` stays covered where its semantics apply (D-036 item 3): "reconciliation never fabricates an outcome for an unknown key" is exercised by `b-40` (exact pass) and the production reconcile tests; its exact output (`NOTHING_TO_RECONCILE`) has no production counterpart (OQ-10).

## Harness compositions an exact pass depends on (to confirm, OQ-11)

- **C1** ingress refusal audit: `INGRESS_UNAUTHENTICATED` comes from the production `verify_ingress`; the production broker has no operation that takes an unverified request, so the adapter audits the refusal through `LedgerStore::record_refusal` (the broker's own audit transaction).
- **C2** approval sealing: the fixture's `approve.for_request` is unsealed; production approval takes a `VerifiedRequest`, so the adapter seals it with the fixture ingress key of its `principal.verified_by`.
- **C3** the test approval authority authenticates exactly the fixture channel credentials and reports every authenticated principal as a human (so `b-26` reaches `SELF_APPROVAL_FORBIDDEN`; a real authority would report the agent as non-human, `HUMAN_UNAUTHENTICATED`).
- **C4** `ledger: down`: the lifecycle transactions fail at begin at the storage boundary while the refusal audit and trusted-time floor stay available (a fully unavailable store would answer `TIME_UNAVAILABLE` before any decision and write no audit).
- **C5** crash: the named lifecycle transaction (reserve, dispatch, observe, promote) fails at begin at the storage boundary, every later write of that broker fails (no audit), then ledger and effect store are closed and reopened (`open`) and a new broker is built (D-033 item 5 injection; production fault injection itself is `#[cfg(test)]`).
- **C6** `effect_fails`: the effect store's public `set_effects_fail`.
- **C7** `OPERATION_UNKNOWN`: a line-protocol answer, audited through `LedgerStore::record_refusal`.
- **C8** input validation (`INPUT_MALFORMED`, `INPUT_JSON_INVALID`): adapter line protocol mirroring the M1 structural rules plus the kernel strict parser; no broker operation runs.
- **C9** transition cases: the frozen `m1-transition.bundle.json` is the active policy (its protection equals the scenarios'), requests are sealed with a fixed test ingress key, the workflow is created through `administer` (create, acceptance-policy activation, artifact updates to reach the revision), and the trusted record and attestation sources are empty (inline records are never read, client permits ignored).

## Owner questions: differences not covered by an approved exception (not decided here)

- **OQ-1** `b-08-apply-without-permit`: production has no effect path without a permit (execute.rs "production differences"); the expected `PERMIT_MISSING` cannot be produced by production code. Accept as a documented difference, or keep it as an adapter-level answer?
- **OQ-2** `p-11-broker-permit-never-issued`: presenting the permit of a step that issued none; `PERMIT_MISSING` is not a production answer (nothing exists to present). Same choice as OQ-1.
- **OQ-3** transition initial state: 59 transition cases and `p-08`, `p-16` start in `verifying` (target of the protected edge `draft` to `verifying`) or `accepted` (protected) at revision 7 or 8, some with a prior promotion, consumed records or protection supplied as state (`sc-50`, `sc-51`). Production administration refuses to create these (`WORKFLOW_INITIAL_STATE_PROTECTED`, D-036 item 5) and reaching them through production promotions needs evidence the fixtures do not have (OQ-6, OQ-7). Options: a reviewed test-only seeding path in production code (not added here), re-expressing the cases through production operations, or recording them as not applicable. This blocks the 14 approved transition exceptions too.
- **OQ-4** the transition fixtures carry no authorization bundle (only the placeholder digest `sha256:aaaa...`); the only fixture bundle with their protection, `m1-transition.bundle.json`, permits transitions only for `fixture-tenant/fixture-project`, while the scenarios use `t1/p1`, so production answers `DEFAULT_DENY` (`sc-31`, `sc-53`, `sc-54` observed; every allow-dependent transition case once seeded). Needs an owner-approved harness bundle or acceptance as a difference.
- **OQ-5** refusal code for a protected transition without its acceptance binding: production's use-time kernel decision answers `ACCEPTANCE_BINDING_REQUIRED` (`sc-52` observed), the frozen broker expectation is `ACCEPTANCE_REQUIRED` (`sc-52`; also expected to affect `sc-32`, `sc-33` once seeded).
- **OQ-6** acceptance evidence: every protected promotion in the fixtures carries an inline, unauthenticated `m1-draft-3` record; production reads an authenticated record by id from the trusted source and needs registry entries and runner attestations (D-036 item 1), none of which the fixtures contain. Generating test-authenticated records, registry entries and attestations from the fixture records would be a new semantic choice (marked OQ-6 per case).
- **OQ-7** human criterion: every fixture requirement set includes the human criterion `signoff`; production refuses required human criteria (`ACCEPTANCE_HUMAN_UNAVAILABLE`, M4) so no expected protected commit can be reproduced before M4: `sc-01`, `sc-02`, `sc-03`, `sc-04`, `sc-10`, `sc-12`, `sc-13`, `sc-15`, `sc-38`, `sc-39`, `sc-53`, `sc-54`, `sc-56`, `sc-57`, `sc-58`, `sc-59`, `sc-63`, `sc-65`, `p-16`.
- **OQ-8** `effect` operations (`sc-40` to `sc-42`, `sc-45`, `sc-46`, `sc-55` to `sc-65`): the M1 effect carries a client permit and no seal; production effects run through the sealed execute lifecycle with prerequisites in T-reserve. The effect-permit cases `sc-46` (`PERMIT_MISSING`) and `sc-62` (`PERMIT_REQUEST_MISMATCH`) are not promotion permits, so D-036 item 2 does not cover them; after `crash_after_dispatch` production keeps the receipt `dispatched` (not `unknown_reconcile`) until reconcile.
- **OQ-9** `activate_authorization_bundle` (`sc-08b`, `sc-36`, `sc-59`): the step supplies a digest and protection, not a loadable bundle.
- **OQ-10** `sc-47-reconcile-nothing`: no production `NOTHING_TO_RECONCILE` refusal (production reconcile returns count 0 and audits nothing); confirm the coverage statement above.
- **OQ-11** confirm harness compositions C1 to C9 (they decide whether the corresponding exact passes count as production evidence).
- **OQ-12** ledger tests (below): M1 accepts a valid non-WAL ledger and switches it to WAL, production refuses it unchanged; M1 accepts a ledger created from its own `ledger.sql`, production accepts only `writ-ledger-5`. Neither is covered by D-032's recorded deviation (extra schema objects).

## Production API gaps met by the adapter (integration requirements, production code unchanged)

- **G-1** storage fault injection is `#[cfg(test)]` only; the adapter wraps the public store traits (`GrantLedger`, `ExecuteLedger`, `WorkflowLedger`, `LedgerStore`) to inject at the storage boundary.
- **G-2** no broker operation audits an ingress refusal (`verify_ingress` is a free function, `Broker::execute` takes a `VerifiedRequest`).
- **G-3** no public read API for receipts by status, grant uses, consumed permits, applied effects, consumed records or promotion receipts; the adapter reads them through a separate read-only SQLite connection.
- **G-4** no way to establish a workflow in a protected state, at a revision, or with a prior promotion for tests (by design, D-036 item 5).
- **G-5** no test acceptance service, registry fixtures or attestation fixtures to turn frozen inline records into authenticated inputs.
- **G-6** approval requires a sealed request; the M1 approval channel carries an unsealed `for_request`.
- **G-7** no counterpart for `apply_without_permit` or for presenting an absent permit; no client-permit effect operation.

## Per-case map (127)

"Expected status" is what the production broker is expected to give relative to the frozen expectation: exact pass, approved exception (decision cited; exact difference in the table above and in exceptions.json), or owner question. Operation names refer to `Broker` methods (`execute`, `grants().approve`, `present_permit`, `reconcile`, `promote`, `administer`), `ActivePolicy::activate`, the injected clock and the restart path (`SqliteLedger::open`, `SqliteEffectStore::open`).

### broker-scenarios (46)

| Case | Production component and operations | Expected status | Result (this run) |
|---|---|---|---|
| `b-01-allow-path` | Broker: execute | exact pass | exact pass |
| `b-02-deny-never-reaches-adapter` | Broker: execute | exact pass | exact pass |
| `b-03-unsealed` | Broker: execute | exact pass (harness composition C1) | exact pass |
| `b-04-forged-seal` | Broker: execute | exact pass (harness composition C1) | exact pass |
| `b-05-unregistered-adapter` | Broker: execute | exact pass (harness composition C1) | exact pass |
| `b-06-adapter-vouches-for-other-issuer` | Broker: execute | exact pass (harness composition C1) | exact pass |
| `b-07-altered-after-sealing` | Broker: execute | exact pass (harness composition C1) | exact pass |
| `b-08-apply-without-permit` | Broker: apply_without_permit (no counterpart) | owner question OQ-1 | not driven: unsupported at step 0 (apply_without_permit): NO_PRODUCTION_OPERATION |
| `b-09-forged-permit` | Broker: execute, present_permit | exact pass | exact pass |
| `b-10-permit-replay` | Broker: execute, present_permit | exact pass | exact pass |
| `b-11-permit-for-other-operation` | Broker: execute, present_permit | exact pass | exact pass |
| `b-12-expired-permit` | Broker: execute, clock, present_permit | exact pass | exact pass |
| `b-13-extended-expiry-without-resign` | Broker: execute, present_permit | exact pass | exact pass |
| `b-14-approval-required-without-grant` | Broker: execute | exact pass | exact pass |
| `b-15-grant-single-use` | Broker: approve, execute, execute | exact pass | exact pass |
| `b-16-grant-bound-to-resource` | Broker: approve, execute | exact pass | exact pass |
| `b-17-grant-bound-to-capabilities` | Broker: approve, execute | exact pass | exact pass |
| `b-18-no-subagent-inheritance` | Broker: approve, execute | exact pass | exact pass |
| `b-19-policy-change-invalidates-grant` | Broker: approve, policy activation, execute | exact pass | exact pass |
| `b-20-expired-grant` | Broker: approve, clock, execute | exact pass | exact pass |
| `b-21-denied-verdict` | Broker: approve, execute | exact pass | exact pass |
| `b-22-forbid-not-approvable` | Broker: approve, execute | exact pass | exact pass |
| `b-23-unknown-grant` | Broker: execute | exact pass | exact pass |
| `b-24-wrong-credential` | Broker: approve | exact pass | exact pass |
| `b-25-untrusted-channel` | Broker: approve | exact pass | exact pass |
| `b-26-self-approval` | Broker: approve | exact pass (harness composition C2, C3) | exact pass |
| `b-27-grant-race-16` | Broker: approve, concurrent execute | exact pass | exact pass |
| `b-28-refusal-before-reservation-keeps-grant` | Broker: approve, dependency, execute, dependency, execute | exact pass (harness composition C2, C4) | exact pass |
| `b-29-crash-before-reserve` | Broker: approve, execute(crash_before_reserve), execute | exact pass (harness composition C2, C5) | exact pass |
| `b-30-idempotent-retry` | Broker: execute, execute | exact pass | exact pass |
| `b-31-same-key-different-request` | Broker: execute, execute | exact pass | exact pass |
| `b-32-same-key-other-grant` | Broker: approve, approve, execute, execute | exact pass | exact pass |
| `b-33-same-key-same-grant` | Broker: approve, execute, execute | exact pass | exact pass |
| `b-34-concurrent-duplicates` | Broker: concurrent execute | exact pass | exact pass |
| `b-35-crash-after-reserve-resumes` | Broker: approve, execute(crash_after_reserve), execute | exact pass (harness composition C2, C5) | exact pass |
| `b-36-crash-after-dispatch-reconcile` | Broker: execute(crash_after_dispatch), reconcile, execute | approved exception (D-036 item 4; D-035 item 3) | matched exception |
| `b-37-crash-after-dispatch-retry` | Broker: execute(crash_after_dispatch), execute | exact pass (harness composition C5) | exact pass |
| `b-38-effect-failure-recorded` | Broker: execute(effect_fails), execute | exact pass (harness composition C6) | exact pass |
| `b-39-policy-down` | Broker: dependency, execute | exact pass | exact pass |
| `b-40-reconcile-nothing` | Broker: reconcile | exact pass | exact pass |
| `b-41-grant-after-deadline` | Broker: approve, clock, execute | exact pass | exact pass |
| `b-42-permit-bounded-by-deadline` | Broker: execute, clock, present_permit | exact pass | exact pass |
| `b-43-unknown-capability` | Broker: execute | exact pass | exact pass |
| `b-44-permit-replay-after-restart` | Broker: execute, restart, present_permit | exact pass | exact pass |
| `b-45-retry-after-restart` | Broker: approve, execute, restart, execute | exact pass | exact pass |
| `b-46-reconcile-after-restart` | Broker: execute(crash_after_dispatch), restart, execute | exact pass (harness composition C5) | exact pass |

### protocol-broker (9)

| Case | Production component and operations | Expected status | Result (this run) |
|---|---|---|---|
| `p-09-broker-unknown-op` | Broker: teleport (unknown op: line protocol) | exact pass (harness composition C7) | exact pass |
| `p-10-broker-grant-ref-reuse` | Broker: approve, execute, approve, execute | exact pass | exact pass |
| `p-11-broker-permit-never-issued` | Broker: approve, present_permit | owner question OQ-2 | not driven: unsupported at step 1 (present_permit): NO_PERMIT_TO_PRESENT |
| `p-12-broker-unknown-dependency-name` | Broker: dependency, execute | exact pass (harness composition C8) | exact pass |
| `p-13-broker-extra-dependency` | Broker: execute | exact pass (harness composition C8) | exact pass |
| `p-14-broker-unknown-fault` | Broker: execute(crash_sometime) | exact pass (harness composition C8) | exact pass |
| `p25-01-broker-dependency-policy-missing` | Broker: execute | exact pass (harness composition C8) | exact pass |
| `p25-02-broker-dependency-ledger-missing` | Broker: execute | exact pass (harness composition C8) | exact pass |
| `p25-03-broker-dependency-both-missing` | Broker: execute | exact pass (harness composition C8) | exact pass |

### transition-scenarios (66)

| Case | Production component and operations | Expected status | Result (this run) |
|---|---|---|---|
| `sc-01-promote` | administer (seed) then promote | owner question OQ-3, OQ-6, OQ-7 | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-02-identical-retry` | administer (seed) then promote, promote | owner question OQ-3, OQ-6, OQ-7 | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-03-same-key-different-request` | administer (seed) then promote, promote | owner question OQ-3, OQ-6, OQ-7 | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-04-record-replay-new-key` | administer (seed) then promote, promote | owner question OQ-3, OQ-6, OQ-7 | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-05-artifact-replaced-before-promotion` | administer (seed) then administer update_artifact, promote | owner question OQ-3, OQ-6 | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-06-stale-cas` | administer (seed) then administer update_artifact, promote | owner question OQ-3, OQ-6 | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-07-snapshot-swapped-same-digest` | administer (seed) then administer update_artifact, promote | owner question OQ-3, OQ-6 | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-08-policy-changed` | administer (seed) then administer activate_acceptance_policy, promote | owner question OQ-3, OQ-6 | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-09-kernel-did-not-allow` | administer (seed) then promote | approved exception (D-036 item 2) | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-10-race-16` | administer (seed) then concurrent promote | owner question OQ-3, OQ-6, OQ-7 | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-11-expired` | administer (seed) then clock, promote | owner question OQ-3, OQ-6 | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-12-refusal-is-side-effect-free` | administer (seed) then promote, promote | owner question OQ-3, OQ-6, OQ-7 | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-13-refused-key-reusable` | administer (seed) then promote, promote | approved exception (D-036 item 2) | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-14-malformed-record` | administer (seed) then promote | owner question OQ-3, OQ-6 | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-15-workflow-already-moved` | administer (seed) then promote, promote | owner question OQ-3, OQ-6, OQ-7 | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-16-cross-project-record` | administer (seed) then promote | owner question OQ-3, OQ-6 | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-17-transition-not-evaluated` | administer (seed) then promote | owner question OQ-3, OQ-6 | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-18-permit-reused-for-other-target` | administer (seed) then promote | approved exception (D-036 item 2) | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-19-permit-actor-changed` | administer (seed) then promote | approved exception (D-036 item 2) | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-20-permit-revision-changed` | administer (seed) then promote | approved exception (D-036 item 2) | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-21-permit-scope-changed` | administer (seed) then promote | approved exception (D-036 item 2) | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-22-permit-snapshot-attribute-changed` | administer (seed) then promote | approved exception (D-036 item 2) | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-23-permit-digest-attribute-changed` | administer (seed) then promote | approved exception (D-036 item 2) | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-24-permit-record-id-attribute-changed` | administer (seed) then promote | approved exception (D-036 item 2) | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-25-record-swapped-under-permit` | administer (seed) then promote | approved exception (D-036 item 2 (and item 1)) | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-26-attributes-disagree-with-record` | administer (seed) then promote | owner question OQ-3, OQ-6 | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-27-other-workflow` | administer (seed) then promote | owner question OQ-3, OQ-6 | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-28-composite-at-broker` | administer (seed) then promote | owner question OQ-3, OQ-6 | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-29-incomplete-record` | administer (seed) then promote | owner question OQ-3, OQ-6 | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-30-evaluator-revoked` | administer (seed) then administer revoke_evaluator, promote | owner question OQ-3, OQ-6 | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-31-unprotected-without-acceptance` | administer (seed) then promote | owner question OQ-4 | differs (unexplained): step 0: got {"code": "DEFAULT_DENY", "result": "refused"}, want {"result": "committed", "revision": 8} |
| `sc-32-protected-without-acceptance` | administer (seed) then promote | owner question OQ-3, OQ-5 | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-33-alternate-route-to-protected-state` | administer (seed) then promote, promote | owner question OQ-3, OQ-5 | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-34-alternate-route-with-old-record` | administer (seed) then promote, promote | owner question OQ-3, OQ-6 | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-35-unprotected-with-acceptance-binding` | administer (seed) then promote | exact pass (harness composition C9) | exact pass |
| `sc-36-authorization-bundle-changed` | administer (seed) then policy activation (no bundle in fixture), promote | approved exception (D-036 item 2) | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-37-own-kind-in-capabilities` | administer (seed) then promote | owner question OQ-3, OQ-6 | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-38-repeated-refusals-then-success` | administer (seed) then promote, promote, promote | approved exception (D-036 item 2) | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-39-crash-before-commit-then-retry` | administer (seed) then promote(crash_before_commit), promote | owner question OQ-3, OQ-6, OQ-7 | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-40-effect-retry` | administer (seed) then effect (execute lifecycle), effect (execute lifecycle) | owner question OQ-3, OQ-8 | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-41-interrupted-dispatch-retry` | administer (seed) then effect (execute lifecycle)(crash_after_dispatch), effect (execute lifecycle) | owner question OQ-3, OQ-8 | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-42-interrupted-dispatch-other-request` | administer (seed) then effect (execute lifecycle)(crash_after_dispatch), effect (execute lifecycle) | owner question OQ-3, OQ-8 | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-43-reconciled-applied` | administer (seed) then effect (execute lifecycle)(crash_after_dispatch), client reconcile (not provided), effect (execute lifecycle) | approved exception (D-036 item 3) | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-44-reconciled-not-applied` | administer (seed) then effect (execute lifecycle)(crash_after_dispatch), client reconcile (not provided), effect (execute lifecycle) | approved exception (D-036 item 3) | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-45-crash-before-reserve` | administer (seed) then effect (execute lifecycle)(crash_before_reserve), effect (execute lifecycle) | owner question OQ-3, OQ-8 | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-46-effect-refused-then-valid` | administer (seed) then effect (execute lifecycle), effect (execute lifecycle) | owner question OQ-3, OQ-8 (effect permit) | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-47-reconcile-nothing` | administer (seed) then client reconcile (not provided) | owner question OQ-3, OQ-10 | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-48-duplicate-key-input` | administer (seed) then promote | exact pass (harness composition C8) | exact pass |
| `sc-49-decimal-revision-input` | administer (seed) then promote | exact pass (harness composition C8) | exact pass |
| `sc-08b-policy-changed-compatible` | administer (seed) then administer activate_acceptance_policy, policy activation (no bundle in fixture), promote | owner question OQ-3, OQ-6, OQ-9 | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-50-protection-classification-missing` | administer (seed) then promote | owner question OQ-3 (protection as scenario state), OQ-6 | not driven: unsupported at step None (initial): PROTECTION_NOT_EXPRESSIBLE |
| `sc-51-protection-from-other-bundle` | administer (seed) then promote | owner question OQ-3 (protection as scenario state), OQ-6 | not driven: unsupported at step None (initial): PROTECTION_NOT_EXPRESSIBLE |
| `sc-52-protected-edge-without-record` | administer (seed) then promote | owner question OQ-5 | differs (unexplained): step 0: got {"code": "ACCEPTANCE_BINDING_REQUIRED", "result": "refused"}, want {"code": "ACCEPTANCE_REQUIRED", "result": "refused"} |
| `sc-53-protected-edge-with-record` | administer (seed) then promote | owner question OQ-4, OQ-6, OQ-7 | differs (unexplained): step 0: got {"code": "DEFAULT_DENY", "result": "refused"}, want {"result": "committed", "revision": 8} |
| `sc-54-edge-record-reused-for-destination` | administer (seed) then promote, promote | owner question OQ-4, OQ-6, OQ-7 | differs (unexplained): step 0: got {"code": "DEFAULT_DENY", "result": "refused"}, want {"result": "committed", "revision": 8} |
| `sc-55-publish-before-acceptance` | administer (seed) then effect (execute lifecycle) | owner question OQ-3, OQ-8 | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-56-accept-a-publish-b` | administer (seed) then promote, effect (execute lifecycle) | owner question OQ-3, OQ-6, OQ-7, OQ-8 | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-57-accepted-artifact-modified` | administer (seed) then promote, administer update_artifact, effect (execute lifecycle) | owner question OQ-3, OQ-6, OQ-7, OQ-8 | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-58-acceptance-policy-changed-after-promotion` | administer (seed) then promote, administer activate_acceptance_policy, effect (execute lifecycle) | owner question OQ-3, OQ-6, OQ-7, OQ-8 | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-59-authorization-changed-after-promotion` | administer (seed) then promote, policy activation (no bundle in fixture), effect (execute lifecycle) | owner question OQ-3, OQ-6, OQ-7, OQ-8, OQ-9 | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-60-missing-promotion-receipt` | administer (seed) then effect (execute lifecycle) | owner question OQ-3, OQ-8 | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-61-forged-promotion-receipt` | administer (seed) then effect (execute lifecycle) | owner question OQ-3, OQ-8 | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-62-publication-permit-other-target` | administer (seed) then effect (execute lifecycle) | owner question OQ-3, OQ-8 (effect permit) | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-63-publish-after-promotion` | administer (seed) then promote, effect (execute lifecycle) | owner question OQ-3, OQ-6, OQ-7, OQ-8 | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-64-concurrent-invalidation-and-publication` | administer (seed) then concurrent[effect,update_artifact] (unknown op: line protocol) | owner question OQ-3 | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `sc-65-uncertain-publication` | administer (seed) then promote, effect (execute lifecycle)(crash_after_dispatch), effect (execute lifecycle) | owner question OQ-3, OQ-6, OQ-7, OQ-8 | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |

### protocol-transition (6)

| Case | Production component and operations | Expected status | Result (this run) |
|---|---|---|---|
| `p-04-transition-missing-steps` | line protocol: strict JSON parse (kernel `json::parse`) | exact pass (harness composition C8) | exact pass |
| `p-05-transition-initial-missing-state` | administer (seed) then promote | exact pass (harness composition C8) | exact pass |
| `p-06-transition-count-zero` | administer (seed) then concurrent promote | exact pass (harness composition C8) | exact pass |
| `p-07-transition-unknown-fault` | administer (seed) then promote(crash_sometime) | exact pass (harness composition C8) | exact pass |
| `p-08-transition-unknown-op` | administer (seed) then teleport (unknown op: line protocol) | owner question OQ-3 | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |
| `p-16-scenario-member-order` | administer (seed) then promote | owner question OQ-3, OQ-6, OQ-7 | not driven: unsupported at step None (initial): INITIAL_STATE_NOT_ESTABLISHABLE [WORKFLOW_INITIAL_STATE_PROTECTED] |

## The 45 M1 ledger tests (`m1/ledger_tests`)

They drive the M1 prototype CLI (`--ledger-db`, triggers added to the ledger file, stderr checks), so none runs unchanged against the production broker; production schema strictness forbids trigger injection (D-032, D-033 item 5). Classification:

| File (tests) | Test | Production status |
|---|---|---|
| test_d025 (14) | 7 `StoredGrantIntegrity` tests (unparsable, wrong members, extra or duplicate member, not canonical, wrong member type, well-formed but different binding) | Superseded: `grant_tests::a_malformed_stored_binding_is_a_ledger_integrity_failure_without_a_grant_use`, `a_grant_for_another_binding_is_a_mismatch`, `d035_tests::a_grant_with_damaged_provenance_is_not_executable` (production adds provenance, D-035 item 5) |
| | `test_resumed_reservation_changes_no_durable_state_and_uses_no_second_grant` | Superseded: `grant_tests::an_existing_receipt_gives_conflict_replay_reconciliation_or_resume`, `execute_tests::restart_resumes_reserved_and_never_redispatches_dispatched_or_reconciled`; production also revalidates (D-035 item 1, `d035_tests`) |
| | `test_unverified_permit_applies_no_effect` | Superseded: `execute_tests::adapter_verification_follows_the_d025_b_precedence`, `a_presented_permit_is_verified_but_never_applies_an_effect` |
| | `test_rejected_non_wal_file_is_byte_identical` | Superseded: `tests/initialization.rs` `non_database_file_is_refused_unchanged`, `unrelated_existing_database_is_refused_without_initialization` |
| | `test_valid_non_wal_ledger_is_accepted_and_switched_to_wal` | Production differs: `valid_schema_not_in_wal_mode_is_refused_unchanged` (OQ-12) |
| | `test_created_file_removed_when_initialization_fails` | Superseded: `failed_creation_removes_the_files_it_created` |
| | `test_pre_existing_file_never_removed` | Superseded: `create_refuses_an_existing_file_and_open_refuses_a_missing_one` |
| | `test_missing_directory_stderr_is_clean` | Not applicable (M1 CLI stderr); production reports sanitized `StoreError` categories (S-5, `execute_tests` sanitized diagnostics) |
| test_storage_failures (13) | `test_missing_directory`, `test_not_a_database`, `test_wrong_schema_version`, `test_missing_table` | Superseded: `tests/initialization.rs` (create and open refusals, exact schema objects and metadata) |
| | `test_read_only_file`, `test_read_only_ledger_dir` | Apply to the production profile; no production test identified (integration requirement) |
| | `test_lock_held_beyond_busy_timeout` | Superseded in substance: production fails closed on a second owner immediately (`tests/ownership.rs`); `busy_timeout` only governs foreign readers |
| | `test_reserve_failure_rolls_back_grant_use`, `test_adapter_failure_leaves_dispatched_and_never_redispatches`, `test_observe_failure_after_effect_is_uncertain` | Superseded: `grant_tests::injected_reserve_faults_at_begin_body_and_commit_leave_no_grant_use_and_no_receipt`, `execute_tests::storage_failure_at_every_lifecycle_stage_fails_closed_with_the_implied_crash_state`, `an_ambiguous_outcome_is_never_redispatched_and_reconciles_from_adapter_evidence` |
| | `test_audit_failure_keeps_refusal_and_continues` | Superseded: audit faults in `grant_tests` and `execute_tests` (`inject("audit", ...)`) |
| | `test_ledger_created_from_schema_file_is_accepted` | Not applicable: M1 `ledger.sql` schema; production accepts only `writ-ledger-5` (OQ-12) |
| | `test_ledger_unavailable_maps_to_promotion_refused` | Kernel external-refusal mapping (M2 `external-refusal` set), not the broker |
| test_d029 (6) | `test_reconcile_never_touches_an_in_flight_execute`, `test_64_way_executes_and_reconciles_do_not_deadlock` | Superseded: `execute_tests::reconcile_waits_for_an_in_flight_execution_and_never_changes_it`, `concurrent_executions_and_reconciles_never_interfere` |
| | `test_post_return_reconciliation_is_unchanged` | Production differs by the approved D-035/D-036 change (evidence-based reconciliation, as `b-36`); `d035_tests::concurrent_reconciliations_record_each_outcome_once_from_evidence_only` |
| | `test_zero_row_transition_is_a_state_conflict_not_corruption`, `test_state_conflict_evidence_is_category_5` | Superseded: `execute_tests::a_receipt_changed_behind_the_broker_is_a_state_conflict_not_corruption` |
| | `test_cross_process_reconcile_stays_fail_closed` | Superseded: `tests/ownership.rs::reconciler_process_is_refused_while_a_broker_owns_the_ledger` |
| test_chaos (2) | `test_kill_restart_reconcile` | Superseded: `kill_tests::sigkill_at_every_lifecycle_boundary_leaves_the_implied_crash_state`, `sigkill_at_seeded_random_points_never_breaks_the_invariants` |
| | `test_unwritable_ledger_fails_closed_after_kill` | Applies; no production test identified (integration requirement) |
| test_d027 (7) | `test_start_rule_and_setup_accounting`, `test_top_level_steps_carry_no_worker_members`, `test_profile_option_accepted_with_either_ledger`, `test_bad_profile_option_is_a_usage_error`, `test_outputs_do_not_depend_on_the_profile` | Not applicable: M1 prototype concurrency profiles and timing instrumentation (D-027) |
| | `test_generated_groups_are_serializable_in_both_profiles`, `test_contention_invariants_in_both_profiles` | Superseded in substance: `grant_tests::sixty_four_threads_*`, `execute_tests::sixty_four_threads_executing_one_request_dispatch_it_once`; the adapter's `concurrent` path (`b-27`, `b-34` exact) |
| test_timing_integrity (3) | all three | Not applicable: M1 bench timing protocol (`bench-broker-scenario`); production has no timing output |

Totals (45): superseded by production tests 29; production differs 3 (non-WAL acceptance and M1 schema file, OQ-12; post-return reconciliation, the approved D-035/D-036 change); apply to the production profile without an identified production test 3 (read-only file, read-only directory, unwritable ledger after kill); not applicable 10 (M1 CLI stderr 1, kernel external-refusal mapping 1, D-027 profiles 5, timing protocol 3).

## M1 property campaigns

- **`broker`** (`m1/props/wprops/c_broker.py`): generated `broker-scenario` inputs with per-operation oracles, run invariants, restart and refusal-insertion metamorphic relations and 16/32/64-way serializability. It speaks the same protocol, so it can run against this adapter, but it is differential (two prototypes) and its generators include `apply_without_permit` and presenting absent permits (OQ-1, OQ-2) and reconcile after `crash_after_dispatch` (approved `b-36` difference), so an oracle-only mode with those operations excluded or remapped is required first. **Not run in this task.**
- **`transition`** (`c_transition.py`): generated promotions with client permits and inline records from `draft`, `planning`, `verifying` and `reviewing`; its oracles depend on the permit check order (D-036 item 2), inline records (OQ-6), protected initial states (OQ-3) and client reconcile (D-036 item 3). Not applicable to the production broker without re-specification. **Not run.**
