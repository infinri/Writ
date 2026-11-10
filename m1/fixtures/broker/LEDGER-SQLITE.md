# M1 SQLite broker ledger (D-023)

**Version:** `m1-ledger-1` (2026-11-07). Normative for both prototypes. Written by Claude from [D-023](../../../docs/implementation/DECISION-LOG.md), [BROKER.md](BROKER.md) and D-008 to D-012 before either implementation. The in-memory ledger remains the default and stays available for isolated kernel or simulation measurements; it is never a substitute for SQLite workload results.

## What is real and what is simulated

| Real (tested) | Simulated (not claimed) |
|---|---|
| File-backed SQLite database, WAL, `synchronous=FULL`, `BEGIN IMMEDIATE` transactions, real lock contention between connections in separate threads, reopen after restart, a second process opening the same file | The effect adapter (consumed permits and an effect counter in the same file; no external system, no real exactly-once execution); HMAC fixture keys; injected crashes (a crash point returns early after the transactions it names have committed; nothing is killed); power-loss durability (not tested) |

## Engine and build (identical in both)

SQLite **3.54.1** (source id `2026-10-16 14:45:20 555e07ef...`), built once by [`m1/tools/install-sqlite.sh`](../../tools/install-sqlite.sh) into a static `libsqlite3.a` (build revision 2, directory `sqlite-3.54.1-r2`) with `-O2 -fPIC -DHAVE_USLEEP=1 -DSQLITE_THREADSAFE=1 -DSQLITE_DQS=0 -DSQLITE_OMIT_LOAD_EXTENSION -DSQLITE_ENABLE_COLUMN_METADATA`. Neither driver's bundled copy is used.

| | Rust | Go |
|---|---|---|
| Driver | `rusqlite =0.40.2`, `default-features = false` (links the shared library through `libsqlite3-sys` with `SQLITE3_LIB_DIR`, `SQLITE3_INCLUDE_DIR`, `SQLITE3_STATIC=1`) | `github.com/mattn/go-sqlite3 v1.14.52` with build tags `libsqlite3 sqlite_omit_load_extension` (cgo; `CGO_CFLAGS`, `CGO_LDFLAGS` from `m1/tools/env.sh`) |
| Access | direct connection API | `database/sql` is allowed only with one dedicated `*sql.Conn` per worker (no pool reuse across workers), or the driver's connection API directly |

Smoke-verified 2026-11-07: both report version 3.54.1, the same source id and the same `pragma compile_options`, with no dynamic `libsqlite3` dependency.

## Connection settings (every connection, set right after open, in this order)

| Setting | Value | Why |
|---|---|---|
| `PRAGMA journal_mode` | `WAL` | readers do not block the writer; persisted in the file |
| `PRAGMA synchronous` | `FULL` | a committed intent or status survives an OS crash in WAL mode |
| `PRAGMA busy_timeout` | `5000` (ms) | contention waits instead of failing; a timeout after 5 s is `LEDGER_UNAVAILABLE` |
| `PRAGMA foreign_keys` | `ON` | event and use rows must reference their receipt or grant |
| `PRAGMA trusted_schema` | `OFF` | no functions from schema content |
| Write transactions | `BEGIN IMMEDIATE` ... `COMMIT` (or `ROLLBACK`) | the write lock is taken at the start, so check-then-write is atomic |

No other pragmas, no shared cache, no memory-mapped I/O, no `PRAGMA locking_mode`. `proto sqlite-info` (diagnostic command, both prototypes) prints `{"sqlite_version", "source_id", "compile_options": [...], "settings": {"journal_mode", "synchronous", "busy_timeout", "foreign_keys", "trusted_schema"}, "schema_version", "driver"}` read back from a fresh connection to a new temporary database.

## Selecting the ledger

`proto broker-scenario` keeps the in-memory ledger. Both `broker-scenario` and `bench-broker-scenario` also accept `--concurrency-profile primary|cold` (at most once, any position among the options, with either ledger; default `primary`; any other value or a repeated option is the same usage error as an unknown option) (D-027, "Concurrency profiles"). `proto broker-scenario --ledger sqlite --ledger-dir <dir>`: every scenario line gets a new database file in `<dir>` (an existing file is never reused; the name is the prototype's choice, e.g. `<pid>-<sequence>.db`), created with [ledger.sql](ledger.sql) inside one transaction. `--ledger-db <path>` instead opens one database for the whole process (created if absent, never reset): this is how a second process continues from the ledger a first process left. Outputs (`steps`, `final`) are byte-for-byte the same JSON as with the in-memory ledger for every scenario; only timing and the files differ. Trusted configuration (`now`, `state_revision`, bundle, keys, channels, dependency status) and client-held issued permits are never stored in the database.

## Logical transactions (same in both; one row = one transaction)

Every refusal, from any operation, appends one `audit_refusals` row in its own `BEGIN IMMEDIATE` transaction after any other transaction of that operation has ended. `final.refusals` is the row count.

**execute** (BROKER.md order; steps 1-4 touch no database):

1. Seal, `LEDGER_UNAVAILABLE` if `dependencies.ledger` is down, use-time decision, `APPROVAL_REQUIRED`.
2. Acquire the in-process **key lock** for (tenant, idempotency key) (volatile; held until the operation returns). Operations on the same key therefore never interleave inside one process; different keys run in parallel and contend only in SQLite.
3. **T-reserve** (`BEGIN IMMEDIATE`): read the receipt. Exists with a different fingerprint or effective grant: rollback, `IDEMPOTENCY_CONFLICT`. `observed_success` / `observed_failure`: rollback, replay result. `dispatched` / `unknown_reconcile`: rollback, `RECONCILIATION_REQUIRED`. `reserved`: commit the transaction without writing anything (outcome `commit`; D-025 A), resume at step 5 (no second grant use). Resuming is allowed only from `reserved`: T-dispatch commits `dispatched` before T-adapter begins, so a receipt whose effect may have started is never `reserved` (C-3). Absent: `crash_before_reserve` rolls back and returns `crashed`; with a grant, `GRANT_UNKNOWN`, `GRANT_MISMATCH` (binding compared as D-006 canonical JSON), `GRANT_EXPIRED`, `GRANT_EXHAUSTED` roll back; otherwise `UPDATE grants SET uses = uses + 1 WHERE label = ? AND uses < max_uses` (exactly one row, else `GRANT_EXHAUSTED`) and insert one `grant_uses` row; insert the receipt `reserved` and a `reserve` event; commit.
4. `crash_after_reserve`: return `crashed` (the reservation and grant use are committed).
5. **T-dispatch** (`BEGIN IMMEDIATE`): increment `next_permit` and read it; `UPDATE receipts SET status = 'dispatched' WHERE ... AND status = 'reserved'` (exactly one row); insert a `dispatch` event; commit. Then sign the permit (`permit-<n>`, fingerprint, bundle sha, expiry `min(now + 30 s, deadline)`) and keep it client-side for `present_permit`.
6. **T-adapter** (`BEGIN IMMEDIATE`, simulated adapter): fingerprint differs `PERMIT_REQUEST_MISMATCH`, MAC invalid `PERMIT_INVALID`, expired `PERMIT_EXPIRED`, id in `adapter_consumed_permits` `PERMIT_REPLAYED` (each rolls back); otherwise insert the consumed id and, unless `effect_fails`, one `adapter_effects` row; commit. An adapter refusal returns that code and leaves the receipt `dispatched`.
7. `crash_after_dispatch`: return `crashed` (receipt stays `dispatched`).
8. **T-observe** (`BEGIN IMMEDIATE`): `dispatched` to `observed_success` or `observed_failure` (exactly one row), `observe` event, commit.

**approve:** authentication, `NOT_APPROVABLE`, `SELF_APPROVAL_FORBIDDEN` touch no database; then `BEGIN IMMEDIATE`: label exists `GRANT_REF_IN_USE` (rollback; never resets uses); else insert the grant (`uses` 0, `max_uses` 1); commit.

**present_permit:** the client-held permit of that step (`PERMIT_MISSING` if none), optionally tampered, through T-adapter.

**reconcile:** `BEGIN IMMEDIATE`; every `dispatched` receipt becomes `unknown_reconcile` with a `reconcile` event; `count` is the number of rows changed; commit. It never creates a receipt or marks an effect completed.

**restart_broker:** close every connection, drop all volatile state (key locks, caches, prepared statements), open a new connection to the same file. Client-held permits and trusted configuration survive (D-020). A `reserved` receipt resumes; a `dispatched` or `unknown_reconcile` one is never dispatched again.

**concurrent:** each operation runs on its own thread with its own connection opened for the group (closed afterwards); all start together under the selected concurrency profile ("Concurrency profiles", D-027); outcomes must equal some serial order of whole operations (M1-05 oracle).

**final:** `effects_applied` = rows in `adapter_effects`; `grant_uses` = `{label: uses}` from `grants`; `receipts` = counts per status; `consumed_permits` = rows in `adapter_consumed_permits`; `refusals` = rows in `audit_refusals`.

A database error other than a busy timeout is a prototype defect: answer `{"result": "refused", "code": "LEDGER_UNAVAILABLE"}` for that operation, report it on stderr, never crash, and never return a success.

## Storage failures (D-024: fail closed, one sanitized response)

1. **Open and initialization happen before any step** of a scenario, in this order (S-1, D-025): open or create the file; apply the connection-only settings (`busy_timeout`, `foreign_keys`, `trusted_schema`); one `BEGIN IMMEDIATE` transaction that either creates the schema from [ledger.sql](ledger.sql) (new, empty database) or verifies that `meta.schema_version` is `'1'` and every table in ledger.sql exists, then commit; only then apply `journal_mode=WAL` (persistent; the result must read back `wal`) and `synchronous=FULL`. An existing file that fails verification is therefore never switched to WAL or otherwise changed. Any failure (directory missing or not writable, file not a database, read-only file, wrong or missing schema version, missing table, lock not obtained within `busy_timeout`) makes the **whole scenario answer exactly** `{"error": "LEDGER_UNAVAILABLE"}`: no step runs, no `steps` or `final` member, and an existing file is not modified. Extra schema objects are not an error (they are not checked). Because SQLite opens a write-protected file read-only and, in WAL mode, `BEGIN IMMEDIATE` on it can still succeed, an existing ledger file must also be checked for write access before SQLite opens it (opening it for read and write and closing it changes nothing); a failure is an open failure. Connections opened later (after `restart_broker`, or per `concurrent` worker) do not re-run initialization; their failures are operation failures (rule 2). **Cleanup (S-2, D-025):** if the run itself created the database file (exclusive create) and initialization then fails, the run removes that file and any `-journal`, `-wal` or `-shm` file it created; it never removes or changes a file that existed before the run or that another process created.
2. **Failure inside an operation** (any SQLite error in one of the operation's transactions, including a busy timeout): that transaction rolls back, the operation answers `{"result": "refused", "code": "LEDGER_UNAVAILABLE"}`, the refusal audit row is attempted in its own transaction (if that also fails, the operation still answers the same refusal and `final.refusals` does not count it), and the scenario continues with the next step. Transactions the operation committed earlier stay committed (for example a receipt already `dispatched` stays `dispatched`, so a retry is `RECONCILIATION_REQUIRED`, never a second dispatch).
3. **`final` unreadable:** `"final": {"error": "LEDGER_UNAVAILABLE"}` with `steps` kept (not covered by the language-neutral tests).
4. **Sanitized:** stdout carries only these codes; never a path, file name, SQLite or OS error text. Diagnostics go to stderr (operator channel) and, from D-025, contain no database path, directory or file name and no credential, key or seal: only a fixed category (`open`, `initialize`, `transaction <name>`, `final`, and from D-027 `worker` for a concurrent worker's connection setup) and the SQLite primary result code name (for example `SQLITE_BUSY`) or `io`. Externally `LEDGER_UNAVAILABLE` maps to `PROMOTION_REFUSED` (`external-refusal`).
5. **Stored-grant integrity (C-4, D-025):** a grant row whose `binding` is not strict JSON (D-017), is not exactly its own D-006 canonical text, or is not an object with exactly the members `action_digest`, `action_kind`, `approval_request_id`, `canonical_resource_id`, `policy_bundle_sha`, `principal_id`, `tenant_id`, `verdict` (strings) and `requested_capabilities` (array of strings) is a ledger-integrity failure: the operation that reads it is refused `LEDGER_UNAVAILABLE` (rule 2, transaction rolled back, no grant use, no receipt). A well-formed binding that differs from the request remains `GRANT_MISMATCH`.
6. **Adapter error precedence (D-025 B):** `T-adapter` cannot begin: `LEDGER_UNAVAILABLE` (no permit check result is disclosed); inside the transaction, in order: fingerprint `PERMIT_REQUEST_MISMATCH`, MAC `PERMIT_INVALID`, expiry `PERMIT_EXPIRED`, replay `PERMIT_REPLAYED` (each rolls back); only a fully verified permit inserts the consumed id and the effect row, in the same transaction; a failing `COMMIT` is `LEDGER_UNAVAILABLE` and leaves neither.

Language-neutral tests: `m1/ledger_tests/test_storage_failures.py` (Python standard library; runs both binaries; failures injected through the file system, a held lock, or `RAISE(ABORT)` triggers added to the ledger file; results checked in the database afterwards).

## Per-operation timing (M1-06d, `bench-broker-scenario`)

`proto bench-broker-scenario [--ledger sqlite (--ledger-dir DIR | --ledger-db PATH)]` takes the same input as `broker-scenario` and answers `{"scenario": S, "timing": T}` where `S` is **exactly** the `broker-scenario` output for the same input and ledger (the harness checks byte equality against a plain run). Timing never changes behaviour, authorization or durability settings. All times are integers in nanoseconds from a monotonic clock, measured in-process; instrumentation must be equivalent in both prototypes (same start and stop points as defined here).

`T = {"parse_ns": n, "open_ns": n | null, "steps": [step...], "final_ns": n | null, "run_ns": n}`:

| Field | Start and stop |
|---|---|
| `parse_ns` | strict parsing of this line's envelope and scenario input (protocol overhead inside the process; output serialization cannot be measured in its own output, so the harness derives the remaining protocol and process overhead as process wall time minus the sum of `parse_ns + run_ns`) |
| `open_ns` | ledger open and initialization (Storage failures rule 1); `null` for the in-memory ledger |
| `run_ns` | from just after parsing to the scenario output value being complete (includes `open_ns`, all steps and `final_ns`) |
| `final_ns` | computing `final`; `null` when `S` is an `input_error` or the open failed (then `steps` is `[]`) |

`step = {"op": <the op string, or "unknown">, "total_ns": n, "validate_ns": n | null, "decide_ns": n | null, "key_lock_wait_ns": n | null, "tx": {name: tx, ...}}`:

| Field | Meaning |
|---|---|
| `total_ns` | end-to-end operation: from dispatch of the parsed step to its result value |
| `validate_ns` | broker request validation before the decision: `execute` seal verification and the ledger-availability check; `approve` channel and credential check; `null` for other operations |
| `decide_ns` | the use-time kernel decision inside `execute` or `approve`; `null` when not reached |
| `key_lock_wait_ns` | waiting for the per-key lock (SQLite `execute` only); `null` otherwise |
| `tx` | one member per logical transaction that ran, named `reserve`, `dispatch`, `adapter`, `observe`, `approve`, `reconcile`, `audit`; `{}` if none |

`tx = {"begin_ns": n | null, "body_ns": n, "end_ns": n | null, "outcome": "commit" | "rollback", "error": null | {"code": "busy" | "other", "at": "begin" | "body" | "end"}}` (`error` added by D-026: `null` when the transaction raised no database error, including ordinary refusals that roll back; otherwise `code` is `busy` when the SQLite primary result code is `SQLITE_BUSY` and `other` for any other database or integrity error, and `at` says whether `BEGIN IMMEDIATE`, the body, or `COMMIT`/`ROLLBACK` failed; an error always has outcome `rollback`; the in-memory ledger never reports an error): SQLite: `begin_ns` is `BEGIN IMMEDIATE` returning (lock acquisition, including busy waits), `body_ns` the statements and in-process checks between begin and end, `end_ns` `COMMIT` or `ROLLBACK` returning. In-memory ledger: `begin_ns` and `end_ns` are `null` and `body_ns` times the corresponding in-memory section.

A failed `BEGIN IMMEDIATE` (busy timeout or error) is still recorded as that transaction with outcome `rollback`, the measured `begin_ns`, and `body_ns` and `end_ns` 0; a failed `COMMIT` is recorded as `rollback`. `open_ns` is a number whenever an open was attempted (also when it failed) and `null` when none was (input error). For a `concurrent` worker, `total_ns` starts when the worker passes the start barrier (the wait is excluded) and ends at its result; connection setup is outside it in the `primary` profile and inside it in the `cold` profile (D-027).

A `concurrent` step is `{"op": "concurrent", "total_ns": n, "validate_ns": null, "decide_ns": null, "key_lock_wait_ns": null, "tx": {}, "ready_ns": n, "operations": [worker, ...]}` with one entry per operation in input order. A `worker` is a `step` with two more members, `setup_ns` and `start_ns` (D-027, "Concurrency profiles"); top-level steps never carry them.

**Integrity rules** (checked by `m1/ledger_tests/test_timing_integrity.py`): every value is a non-negative integer or `null` exactly as specified; for each step `validate_ns + decide_ns + key_lock_wait_ns + sum(begin_ns + body_ns + end_ns)` (nulls as 0) is at most `total_ns`; the sum of top-level step `total_ns` plus `open_ns` and `final_ns` is at most `run_ns`; `tx` names and outcomes match the specification for the operation's result (for example a successful allow `execute` has exactly `reserve`, `dispatch`, `adapter`, `observe`, all `commit`; a refused operation adds `audit`; `GRANT_EXHAUSTED` has `reserve` with `rollback`).

## Concurrency profiles (D-027, amendment `m1-ledger-1a`, 2026-11-10)

Written by Claude from [D-027](../../../docs/implementation/DECISION-LOG.md) before either implementation. Supersedes the pre-D-027 rule that a worker's `total_ns` includes its connection setup. Behaviour (`steps`, `final`) is the same in both profiles; only when a worker's connection is created, and the timing members, differ.

**Worker connection setup** (SQLite, every worker of a `concurrent` group, both profiles): open a new connection to the scenario's ledger file; apply the "Connection settings" in their order; then **verify** by reading back `PRAGMA journal_mode` = `wal`, `PRAGMA synchronous` = `2` (FULL), `PRAGMA busy_timeout` = `5000`, `PRAGMA foreign_keys` = `1`, `PRAGMA trusted_schema` = `0`. Setup never begins a transaction explicitly (no `BEGIN`, in particular no `BEGIN IMMEDIATE`) and writes nothing. Any failure (open error, a setting that cannot be applied, a read-back that differs) is that worker's operation failure (Storage failures rule 2): it answers `{"result": "refused", "code": "LEDGER_UNAVAILABLE"}`, runs no ledger transaction and no `audit` transaction (it has no usable connection, so `final.refusals` does not count it), its `tx` is `{}`, and stderr carries category `worker` with the code; the other workers run normally. The in-memory ledger has no worker connections.

| Profile | Selected by | Order inside one `concurrent` group |
|---|---|---|
| `primary` (default) | no option, or `--concurrency-profile primary` | 1. create every worker (thread or goroutine) and perform **every** worker's connection setup; 2. release the common start barrier only after all setups have finished (successfully or not); 3. each worker runs its operation. No worker's operation, and therefore no write transaction, begins before release |
| `cold` (supplemental) | `--concurrency-profile cold` | 1. create every worker; 2. release the start barrier; 3. each worker performs its own connection setup and then runs its operation |

The release mechanism itself (for example an OS-thread barrier or a closed channel) is unchanged by D-027 and remains each prototype's own; whether release simultaneity must also be made equivalent is an open owner question, and `start_ns` below makes the difference observable without changing it.

**Timing members** (`bench-broker-scenario`; all integers in nanoseconds on the same monotonic clock, offsets measured from the group's dispatch, that is the start of the `concurrent` step's `total_ns`):

| Member | Where | Meaning |
|---|---|---|
| `ready_ns` | `concurrent` step | offset of the group's ready point: the latest, over its workers, of each worker's ready point (`primary`: the instant its connection setup finished, successfully or not; `cold`, and the in-memory ledger in both profiles: the instant the worker was created and handed its operation); `0` for a group with no operations. Taken before the release is signalled |
| `setup_ns` | each worker | duration of that worker's connection setup (open, settings, read-back verification); `null` for the in-memory ledger |
| `start_ns` | each worker | offset at which that worker passed the start barrier (`primary`: just before its operation begins; `cold`: just before its connection setup begins) |
| `total_ns` | each worker | from passing the barrier to its result: `primary` excludes `setup_ns`; `cold` includes it |

The group's `total_ns` includes creating the workers, every setup, the release, all operations and closing the worker connections afterwards.

**Integrity rules** (checked by `m1/ledger_tests/test_d027.py`, in addition to the rules above): for every `concurrent` step and each of its workers: `start_ns >= ready_ns` (no worker starts before the group is ready); `start_ns + total_ns <= ` the group's `total_ns`; SQLite: `setup_ns` is an integer, `primary` `setup_ns <= ready_ns` and the step-part sum (validate, decide, key lock wait, transactions) is at most `total_ns`; `cold`: that sum plus `setup_ns` is at most `total_ns`; memory: `setup_ns` is `null`. Outputs (`scenario`) are identical across profiles and ledgers for every broker fixture. A worker whose setup failed answers `LEDGER_UNAVAILABLE` with `tx` `{}` (D-026 category 5: never an accepted contention abort).

**Not changed:** busy timeout 5 s; 64-way stress tests; the D-026 categories and checks. Timeout counts measured before D-027 (E-034..E-036) are preliminary and not directly comparable with counts measured under a named profile.

## Contention and the concurrency oracle (D-026)

A `concurrent` group's outcome counts and `final` must equal a serial order of whole operations (M1-05), with these per-operation exceptions derived from each operation's `bench-broker-scenario` transaction evidence (input order) and, where its idempotency key is used by no other operation in the group, its rows in the ledger file:

| Category | Evidence | Treatment |
|---|---|---|
| 1 successful, 2 ordinary refusal | no transaction has an `error` | modelled as written (strict) |
| 3 verified lock-contention abort | the operation's first ledger transaction (`reserve` for `execute`) has `error: {"code": "busy", "at": "begin"}`; no other transaction except `audit`; for an operation-unique key, no receipt, receipt event or grant-use row for it | removed from the model as an aborted no-op; its `LEDGER_UNAVAILABLE` outcome is matched to it |
| 4 failure after partial durable progress | `reserve` committed and a later transaction failed at `begin` with `busy` | modelled as the crash state the committed transactions imply: `dispatch` failed: reserved (grant use kept, a later same-key operation resumes); `adapter` failed: dispatched with no consumed permit and no effect (uncertain); `observe` failed: dispatched with the consumed permit and the effect (or no effect for `effect_fails`) (uncertain) |
| 5 unexpected | any other `error` (`other`, or failure `at` body or end) | always a failed check |

Every `LEDGER_UNAVAILABLE` outcome must be matched to a category 3 or 4 operation (counts equal). An `audit` transaction with an error removes that refusal from `final.refusals` (D-024 rule 2); all other refusal accounting is exact. Availability is reported separately from correctness.

## Required tests (each prototype, own code)

1. Every `broker-scenarios` and `protocol-broker` fixture with `--ledger sqlite` gives exactly the expected output (driver run with the flag).
2. Atomicity: a failing statement inside T-reserve leaves no receipt, event or grant use (test hook or injected constraint violation).
3. Persistence: after `restart_broker`, and in a **second process** using `--ledger-db` on the same file: receipts, grant uses, consumed permits, refusal count and `next_permit` persist; a `crash_after_dispatch` receipt answers `RECONCILIATION_REQUIRED` and `adapter_effects` does not grow; a `crash_after_reserve` receipt resumes without a second grant use.
4. Contention: at least 64 threads, one connection each, racing for one single-use grant with distinct keys: exactly one reservation and one grant use; racing on one idempotency key: exactly one dispatch; repeated at least 20 times.
5. Schema drift: the embedded schema is byte-identical to `m1/fixtures/broker/ledger.sql`.
6. `proto sqlite-info` reports 3.54.1 and the settings above.
7. `m1/ledger_tests` (language-neutral, D-024) all pass.
8. `bench-broker-scenario`: `m1/ledger_tests/test_timing_integrity.py` passes (scenario equality on every broker fixture with both ledgers, the integrity rules above).
9. Concurrency profiles (D-027): a test in the prototype's own code proves that in `primary` no worker begins a transaction before every worker's connection setup has finished, and that `cold` creates each worker connection after release; `m1/ledger_tests/test_d027.py` passes.

## Implementation notes (recorded 2026-11-07 after both implementations; no semantic change)

- **Build revision 2:** revision 1 omitted `-DHAVE_USLEEP=1`, so SQLite's default busy handler slept in whole seconds (about 5 retries within `busy_timeout=5000`). Found by the Rust implementer; fixed for both before any measurement. `HAVE_USLEEP` is not listed by `pragma compile_options`; the linked library path is recorded in the Rust build output and the Go binary's build info.
- **Driver-issued pragmas:** `mattn/go-sqlite3` always runs `busy_timeout=5000`, `locking_mode=NORMAL` (the default) and `synchronous=NORMAL` before the connect hook, which then applies the table above, so the effective settings are identical; `rusqlite` sets a 5000 ms busy timeout on open. `proto sqlite-info` reads back the effective values in both.
- **Grant verdict:** stored inside the canonical `binding` JSON in both (the binding includes the verdict, BROKER.md step 5.3).
- **Readings chosen identically by both implementers:** an `INPUT_MALFORMED` scenario creates no database file; a `restart_broker` inside a `concurrent` group reopens only that worker's connection (no fixture or generator produces it). The earlier divergent open-failure shapes are superseded by "Storage failures" (D-024).
- **D-025 diagnostics (recorded 2026-11-10):** both prototypes print `<category> <code>` lines only; for failures the prototype detects itself (schema verification, stored-grant integrity, WAL read-back) Rust prints the nearest SQLite code name (`SQLITE_NOTADB`, `SQLITE_CORRUPT`, `SQLITE_ERROR`) and Go prints `io`. stderr is an operator channel and not compared between prototypes; both satisfy rule 4. Both treat an absent `dependencies` object as `INPUT_MALFORMED` (DEP-1).
- **Go only:** the receipt key is two columns in SQLite but `tenant + NUL + key` in the in-memory map; no fixture or generator produces a tenant or idempotency key containing NUL, so this is untested, not observed.
