# Disposable evaluator publication under D-052

Run the scenario and its fault, binding and ordering controls from the repository
root with pinned Rust 1.99.1 and the shared SQLite library from D-023.

```sh
. m1/tools/env.sh
m3/tools/run-with-clean-fds.sh cargo test --locked -p writ-ledger-sqlite --lib evaluator_
```

The scenario creates separate disposable stores with the same registry, workflow
and authenticated evidence. It promotes successfully in the first store. It
revokes the evaluator before promotion in the second, checks that refusal
consumes neither records nor attestations, then closes and reopens that store
and verifies the durable revocation and revision. The fixture uses the broker's
existing protected promotion and artifact checks.

The configured human authority authenticates
`writ-human-evaluator-publisher/1`, the D-006 canonical document containing the
operation, exact mutation and expected scoped registry revision. A publication
mutation contains the full D-034 evaluator entry. A revocation names its tenant,
project and evaluator. A fresh approval binds that digest to one ceremony and
expires within 120 seconds. The broker holds the publication authority and
exclusive policy fence through the transaction. The existing SQLite registry
updates evaluator status and the scoped revision in the same transaction as
publisher provenance, the consumed ceremony and ledger-wide commit order.
A failed commit can have an uncertain outcome. Reopen the ledger and query the
durable commit by its exact intent digest before retrying. A missing receipt
permits a new attempt only if the prior revision and fresh authority still hold.

The existing registered command digest and runner-purpose key source check the
publication against independently configured implementation and environment
identities. The ledger pins the attestation key's material digest and checks
current revocation and key binding in the same transaction as promotion. The
trusted inputs are the configured authority, key and dependency sources and
the owned ledger. Establish a fresh evaluator scope for this disposable profile.
Existing development fixtures retain their original administrative path in
legacy scopes. Once a scope uses D-052, that path cannot write its evaluator
entries.

The [closed rubric proposal](../../docs/contracts/proposed/pack-rubric-binding.schema.json)
pins an eligible evaluator's exact tenant, project, identity, version, revision
and class, and names criteria registered for it. A binding carries no
permissions, trusted keys or evidence. Model-assisted bindings are advisory.
Acceptance policy and the existing MAC, registry, snapshot and consumption
checks still govern promotion. The fixed 26-case golden corpus exercises full
authorization Decisions with a fixture pack absent, enabled, upgraded and
disabled.

D-052 covers disposable stores and test authorities. D-061 extends this development evidence below. Governed AG-ADV-16 verification remains open. M5's designated guest
and isolated containment CI are separate obligations.

## Signed high-stakes quorum under D-061

Run the public signed-policy scenario and the owning ledger fault and ordering tests from the repository root.

```sh
. m1/tools/env.sh
m3/tools/run-with-clean-fds.sh cargo test --manifest-path m6/publication/Cargo.toml --locked --test lifecycle quorum::signed_quorum_promotion_and_recovery -- --exact
m3/tools/run-with-clean-fds.sh cargo test --locked -p writ-ledger-sqlite --lib quorum_
cargo test --locked -p writ-broker --lib quorum
```

The scenario publishes and selects a signed acceptance policy, registers three eligible evidence evaluators and a separate human signoff evaluator, and requests a protected transition with the broker. The disposable transition authorization fixture pins the digest of the acceptance selection. The signed synthetic authorization compiler still supports only its existing inspect profile. Distinct stores retain distinct transactions. The selection and its verification authority stay pinned until the promotion transaction finishes.

Acceptance policy `writ-acceptance-policy/3` adds a required `quorum` field to each /2 criterion. Null retains ordinary semantics. A high-stakes rule names its complete eligible set and independence groups. Limits are 64 criteria, 16 high-stakes criteria and 2 to 16 eligible evaluators per high-stakes rule. The detached Ed25519 envelope authenticates the payload format, scope, purpose, validity and canonical payload digest with the established policy signing bytes. Publishing or selecting a rollback must preserve every existing quorum rule.

In `authenticated-acceptance-record/3`, the acceptance service authenticates an attempt ID and up to 256 contribution references ordered by criterion and evaluator. Each `runner-attestation/2` separately authenticates one outcome, its attempt, evidence identity, registered evaluator and exact workflow, policy and artifact bindings. Mechanical contributors must bind their registered command. Each MAC covers UTF-8 D-006 canonical JSON of the entire document with only `auth.mac` removed. Record and contributor use separate key types and purpose strings (`writ.acceptance-record.v1` and `writ.runner-attestation.v1`). The contract version is inside the authenticated bytes. Existing /1 and /2 records retain their original signing bytes.

The configured source chooses the current finalized attempt by tenant, project, workflow, task, revision, transition, artifact and policy digest. It pins the complete map of evidence IDs to full signed-document digests, including adverse outcomes, and the digest of the signed record. A request cannot select an alternate passing attempt. The source is trusted to report every result and keep its selection pinned. Authentication verifies what it reported. A dishonest source could suppress a result before finalization.

Two eligible passes must differ in evaluator ID, key ID, key material and reviewed independence group. An eligible authenticated failure vetoes promotion. Ordinary human signoff retains its own ceremony and consumption. The fixture declares groups to exercise the check. Deployment would require independent review of ownership, implementation, evidence provenance, execution environment and key custody. The test uses immutable in-process source data and disposable keys. It supplies no host or independent human-review evidence.

The dedicated quorum ledger rechecks the signed policy, current keys, registry bindings and freshness at use. The existing SQLite promotion transaction commits workflow revision, consumed identities, quorum audit, receipt and commit order together. After an uncertain result, reopen and submit the identical sealed request for receipt lookup. A reused idempotency key with a different fingerprint conflicts.

The ordinary selection accessor and the existing waiver view refuse a selection containing quorum rules. Callers that need to promote under that policy must use the signed quorum guard. Policies with only ordinary criteria continue to use the existing acceptance path.
