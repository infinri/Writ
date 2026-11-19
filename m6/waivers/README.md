# Disposable scoped waiver under D-053

Run from the repository root using Rust 1.99.1 and the shared SQLite library
from D-023.

```sh
. m1/tools/env.sh
m3/tools/run-with-clean-fds.sh cargo test --locked --manifest-path m6/publication/Cargo.toml --test lifecycle waiver_
m3/tools/run-with-clean-fds.sh cargo test --locked -p writ-ledger-sqlite --lib waiver_
```

The signed selection scenario publishes an acceptance policy that permits
waiving the synthetic `waiver-review` criterion. An enrolled fixture human
reviews the exact context, including the effective waiver expiry, and records
one `waived` result through the existing approval lifecycle. Re-selecting the
same publication at a higher activation revision invalidates the reference.

The protected workflow scenario validates a recorded waiver and submits an
authenticated record with that mandatory criterion marked `waived`. The
existing kernel refuses promotion as incomplete, with the workflow revision
and consumption unchanged. The suite also covers purpose separation, role
and credential authority, revocation, state and policy changes, duplicate
submissions, audit and storage faults, and recovery after reopen.

The lab uses the explicit `create_waiver_lab` and `open_waiver_lab` ledger entry
points and a broker configured
with the guarded policy selection and immutable snapshot store. Its fixture
authority maps signed-policy audit publishers to qualified approval identities.
The broker's `validate_waiver` is the live reference boundary.
`waiver_receipt` looks up historical evidence by the approved context digest,
including after an uncertain commit, expiry or revocation.

See the [contract](CONTRACT.md) and [closed approval context schema](waiver-context-v1.schema.json).
