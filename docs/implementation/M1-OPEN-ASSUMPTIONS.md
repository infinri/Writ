# M1 open assumptions: exact wording and proposed resolutions

**Status:** all **OPEN** (owner instruction 2026-11-10, D-024 item 6). Prepared by Claude for owner decision; nothing here changes behaviour, fixtures or expectations until the owner decides. Every fixture stays **PENDING INDEPENDENT REVIEW** (D-024 item 7, REL-01).

Each item: the assumption as it stands, current behaviour with evidence, a proposed resolution, and what accepting it would change.

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
