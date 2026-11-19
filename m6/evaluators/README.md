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

D-052 covers disposable stores and test authorities. High-stakes evaluator
quorum and the remaining AG-ADV-16 controls are open. M5's designated guest
and isolated containment CI are separate obligations.
