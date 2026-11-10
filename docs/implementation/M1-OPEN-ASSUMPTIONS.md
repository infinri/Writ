# M1 open assumptions: exact wording and proposed resolutions

**Status:** all **OPEN** (owner instruction 2026-11-10, D-024 item 6). Prepared by Claude for owner decision; nothing here changes behaviour, fixtures or expectations until the owner decides. Every fixture stays **PENDING INDEPENDENT REVIEW** (D-024 item 7, REL-01).

Each item: the assumption as it stands, current behaviour with evidence, a proposed resolution, and what accepting it would change.

## Owner decision table (compact)

All OPEN. "Proposed" is Claude's recommendation, not a decision.

| ID | Exact assumption (short) | Proposed | Alternatives | Security implication | Tests affected |
|---|---|---|---|---|---|
| DEP-1 | Declared dependency absent from `initial.dependencies` starts `up` | Absent = `INPUT_MALFORMED`, no step runs | Keep default `up`; default `down` | Current default is fail-open on missing trusted configuration | New `protocol-broker` cases; no existing expectation changes |
| C-1 | One generic external code `PROMOTION_REFUSED`; `ACCEPTANCE_BINDING_REQUIRED` hidden | Confirm | Distinct external code for binding-required | A distinct code reveals that a transition is protected (D-020) | None |
| C-2 | Statement without `from_states` treated as any state in bypass analysis | Confirm | Treat as no state (would admit more bundles) | Alternative can admit a protected-edge bypass | None |
| C-3 | Crash after reservation resumes on retry | Confirm for M1; M3 reservation lease/expiry | Treat as uncertain (`RECONCILIATION_REQUIRED`) | Nothing dispatched, no duplicate effect; abandoned reservation can block a key until M3 | None now |
| C-4 | `GRANT_MALFORMED` unreachable via `approve` | Corrupt stored grant = `LEDGER_UNAVAILABLE` (rule 2), never allow; test by editing `grants.binding` | Keep `GRANT_MALFORMED` code reachable; leave untested | Untested path could mis-handle a tampered grant | 1 new `m1/ledger_tests` case |
| C-5 | Broker compared with M0 by correspondence, not execution | Accept for M1 | Build an M0 `broker-scenario` adapter | Divergence from M0 found only by reading | None |
| C-6 | Concurrency checked as serial-equivalent outcomes | Confirm as M1 criterion; linearizability and kills under load in M3 | Per-history linearizability checker now | Interleavings with equal outcome counts are not distinguished | None |
| S-1 | `journal_mode=WAL` applied to an existing non-WAL file before verification changes its header | Verify schema read-only first, switch to WAL only after verification succeeds | Accept (documented limit) | A rejected foreign file is modified (integrity, not authorization) | 1 new storage test (non-WAL foreign file unchanged) |
| S-2 | A file newly created by a run whose initialization fails is left behind | Remove the file the run created | Accept | Leftover empty files; no authorization effect | 1 new storage test |
| S-3 | Busy timeout 5,000 ms; beyond it the operation (or open) is `LEDGER_UNAVAILABLE` | Keep 5,000 ms for M1; record contention timeouts in benchmark results | Longer timeout; retry loop | Fail closed under contention (availability cost only) | Benchmark must count `LEDGER_UNAVAILABLE` outcomes |
| S-4 | Unreadable `final` = `{"error": "LEDGER_UNAVAILABLE"}`, untested | Accept as untested for M1 (no portable way to fail only the final read; triggers cannot fire on reads) | Test-only hook in each prototype | Reporting only; no effect | None |
| S-5 | stderr diagnostics may contain paths and SQLite error text | Accept: stderr is the operator channel; production logging policy decided in M3 | Sanitize stderr too | Operator logs reveal local paths | None |
| S-6 | Write-access check before open has a time-of-check gap; read-only `-wal`/`-shm` not checked | Accept for M1 (local single-user prototype) | Open with `SQLITE_OPEN_READWRITE` only and check `sqlite3_db_readonly` | A file made read-only between check and use fails later as operation failures (still fail closed) | None |
| S-7 | Disk full and I/O errors are not injected | Accept for M1; fault-injection VFS or real device tests in M3 | Add a failing VFS now | Same rule-2 path as other errors, but unexercised | M3 tests |

## DEP-1: declared broker dependency absent from `initial.dependencies`

- **Assumption (exact):** "A dependency the broker declares (`policy`, `ledger`) that is absent from a scenario's `initial.dependencies` starts `up`."
- **Current behaviour:** both prototypes start it `up` (agreement only; D-021 does not decide it; AMENDMENT-5 "Open reading"). No fixture relies on it: all 52 broker and protocol-broker scenarios list both `ledger` and `policy` (one also lists the undeclared `cache`, which is `INPUT_MALFORMED` by D-021 item 9).
- **Proposed resolution:** fail closed. A scenario whose `initial.dependencies` does not list every declared dependency is `{"input_error": "INPUT_MALFORMED"}` with no step run, exactly like an undeclared name. Rationale: an omitted dependency status is missing trusted configuration, and defaulting it to available is a fail-open default.
- **If accepted:** add fixtures (dependency `policy` missing, `ledger` missing, empty object) to `protocol-broker`; change both prototypes; no existing expected result changes.

## C-1: one generic external refusal code

- **Assumption (exact):** "One generic external code `PROMOTION_REFUSED` for all non-validation security refusals, including broker and effect refusals; `ACCEPTANCE_BINDING_REQUIRED` is hidden externally although it tells the caller what to supply."
- **Current behaviour:** `external-refusal` fixtures (63) and both prototypes map every acceptance, permit, scope, trusted-state, storage (`LEDGER_UNAVAILABLE`) and unknown code to `PROMOTION_REFUSED`; caller-input validation codes pass through (D-018 A7, D-020).
- **Proposed resolution:** confirm both parts. Revealing `ACCEPTANCE_BINDING_REQUIRED` would tell an unprivileged caller that the transition is protected, which D-020 forbids (policy predicates must not leak). The caller-facing remedy is documentation and the audit reference, not a distinct code.
- **If accepted:** no change; record as decided.

## C-2: protected-edge bypass analysis and statements without `from_states`

- **Assumption (exact):** "Protected-edge bypass analysis treats a statement without `from_states` as permitting any named state."
- **Current behaviour:** bundles with such a statement that would reach a protected edge without the protected route are rejected at load (`POLICY_BUNDLE_REJECTED`, D-019 N-4); both prototypes agree.
- **Proposed resolution:** confirm. It is the conservative reading: it can only reject more bundles, never admit a bypass.
- **If accepted:** no change.

## C-3: crash after reservation resumes on retry

- **Assumption (exact):** "A crash after reservation resumes on retry (differs from M0)."
- **Current behaviour:** a `reserved` receipt with the same fingerprint and grant resumes at dispatch without a second grant use, in memory and on SQLite (both prototypes' own tests; `m1/ledger_tests` and the H workload). A `dispatched` receipt is never resumed (`RECONCILIATION_REQUIRED`).
- **Proposed resolution:** confirm for M1: nothing was dispatched, so resuming cannot duplicate an effect. Record for M3 that a production broker also needs a reservation lease or expiry so an abandoned reservation does not block the key indefinitely.
- **If accepted:** no change in M1; an M3 task for reservation expiry.

## C-4: malformed stored grants (`GRANT_MALFORMED`)

- **Assumption (exact):** "Malformed stored grants (`GRANT_MALFORMED`) are not reachable through the fixture `approve` operation."
- **Current behaviour:** still unreachable through operations. With the SQLite ledger a stored grant can now be corrupted directly in the file (for example an unparsable `binding`), but no test does this and the specification does not say what happens.
- **Proposed resolution:** treat an unparsable or schema-invalid stored grant as a storage integrity failure: the operation is refused `LEDGER_UNAVAILABLE` (D-024 rule 2), never an allow; add a language-neutral test that edits `grants.binding` in the file. Retire `GRANT_MALFORMED` or keep it internal-only.
- **If accepted:** one spec sentence, one test in `m1/ledger_tests`, possible prototype changes.

## C-5: broker comparison with M0 by correspondence

- **Assumption (exact):** "The broker comparison with M0 is by test correspondence, not execution."
- **Current behaviour:** M1 broker semantics are specified in BROKER.md and LEDGER-SQLITE.md; disagreements with the M0 harness were found by reading (AMENDMENT-3), not by running M0 on the broker fixtures.
- **Proposed resolution:** accept for M1 (M0 is a reference harness, D-001, and its broker differs in documented places). Executing M0 against `broker-scenario` would need an M0 adapter for the protocol; not worth it unless a reviewer requests it.
- **If accepted:** no change.

## C-6: concurrency checked as serial-equivalent outcome only

- **Assumption (exact):** "Concurrency in fixtures is checked as a serial-equivalent outcome only."
- **Current behaviour:** fixtures check outcome counts against permitted serial orders; M1-05 properties search every serialization of 16 to 64 operations; the SQLite ledger adds real lock contention (64 threads or goroutines, 20 repetitions, in each prototype) and a Rust-SQLite versus Go-SQLite differential (0 mismatches).
- **Proposed resolution:** confirm as the M1 criterion. Linearizability of individual histories, real process kills and power loss under concurrency are M3 obligations (unverified, listed in SESSION-HANDOFF section E).
- **If accepted:** no change.
