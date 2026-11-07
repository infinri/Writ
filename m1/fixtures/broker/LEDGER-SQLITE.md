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

`proto broker-scenario` keeps the in-memory ledger. `proto broker-scenario --ledger sqlite --ledger-dir <dir>`: every scenario line gets a new database file in `<dir>` (an existing file is never reused; the name is the prototype's choice, e.g. `<pid>-<sequence>.db`), created with [ledger.sql](ledger.sql) inside one transaction. `--ledger-db <path>` instead opens one database for the whole process (created if absent, never reset): this is how a second process continues from the ledger a first process left. Outputs (`steps`, `final`) are byte-for-byte the same JSON as with the in-memory ledger for every scenario; only timing and the files differ. Trusted configuration (`now`, `state_revision`, bundle, keys, channels, dependency status) and client-held issued permits are never stored in the database.

## Logical transactions (same in both; one row = one transaction)

Every refusal, from any operation, appends one `audit_refusals` row in its own `BEGIN IMMEDIATE` transaction after any other transaction of that operation has ended. `final.refusals` is the row count.

**execute** (BROKER.md order; steps 1-4 touch no database):

1. Seal, `LEDGER_UNAVAILABLE` if `dependencies.ledger` is down, use-time decision, `APPROVAL_REQUIRED`.
2. Acquire the in-process **key lock** for (tenant, idempotency key) (volatile; held until the operation returns). Operations on the same key therefore never interleave inside one process; different keys run in parallel and contend only in SQLite.
3. **T-reserve** (`BEGIN IMMEDIATE`): read the receipt. Exists with a different fingerprint or effective grant: rollback, `IDEMPOTENCY_CONFLICT`. `observed_success` / `observed_failure`: rollback, replay result. `dispatched` / `unknown_reconcile`: rollback, `RECONCILIATION_REQUIRED`. `reserved`: commit nothing, resume at step 5 (no second grant use). Absent: `crash_before_reserve` rolls back and returns `crashed`; with a grant, `GRANT_UNKNOWN`, `GRANT_MISMATCH` (binding compared as D-006 canonical JSON), `GRANT_EXPIRED`, `GRANT_EXHAUSTED` roll back; otherwise `UPDATE grants SET uses = uses + 1 WHERE label = ? AND uses < max_uses` (exactly one row, else `GRANT_EXHAUSTED`) and insert one `grant_uses` row; insert the receipt `reserved` and a `reserve` event; commit.
4. `crash_after_reserve`: return `crashed` (the reservation and grant use are committed).
5. **T-dispatch** (`BEGIN IMMEDIATE`): increment `next_permit` and read it; `UPDATE receipts SET status = 'dispatched' WHERE ... AND status = 'reserved'` (exactly one row); insert a `dispatch` event; commit. Then sign the permit (`permit-<n>`, fingerprint, bundle sha, expiry `min(now + 30 s, deadline)`) and keep it client-side for `present_permit`.
6. **T-adapter** (`BEGIN IMMEDIATE`, simulated adapter): fingerprint differs `PERMIT_REQUEST_MISMATCH`, MAC invalid `PERMIT_INVALID`, expired `PERMIT_EXPIRED`, id in `adapter_consumed_permits` `PERMIT_REPLAYED` (each rolls back); otherwise insert the consumed id and, unless `effect_fails`, one `adapter_effects` row; commit. An adapter refusal returns that code and leaves the receipt `dispatched`.
7. `crash_after_dispatch`: return `crashed` (receipt stays `dispatched`).
8. **T-observe** (`BEGIN IMMEDIATE`): `dispatched` to `observed_success` or `observed_failure` (exactly one row), `observe` event, commit.

**approve:** authentication, `NOT_APPROVABLE`, `SELF_APPROVAL_FORBIDDEN` touch no database; then `BEGIN IMMEDIATE`: label exists `GRANT_REF_IN_USE` (rollback; never resets uses); else insert the grant (`uses` 0, `max_uses` 1); commit.

**present_permit:** the client-held permit of that step (`PERMIT_MISSING` if none), optionally tampered, through T-adapter.

**reconcile:** `BEGIN IMMEDIATE`; every `dispatched` receipt becomes `unknown_reconcile` with a `reconcile` event; `count` is the number of rows changed; commit. It never creates a receipt or marks an effect completed.

**restart_broker:** close every connection, drop all volatile state (key locks, caches, prepared statements), open a new connection to the same file. Client-held permits and trusted configuration survive (D-020). A `reserved` receipt resumes; a `dispatched` or `unknown_reconcile` one is never dispatched again.

**concurrent:** each operation runs on its own thread with its own connection opened for the group (closed afterwards); all start together; outcomes must equal some serial order of whole operations (M1-05 oracle).

**final:** `effects_applied` = rows in `adapter_effects`; `grant_uses` = `{label: uses}` from `grants`; `receipts` = counts per status; `consumed_permits` = rows in `adapter_consumed_permits`; `refusals` = rows in `audit_refusals`.

A database error other than a busy timeout is a prototype defect: answer `{"result": "refused", "code": "LEDGER_UNAVAILABLE"}` for that operation, report it on stderr, never crash, and never return a success.

## Required tests (each prototype, own code)

1. Every `broker-scenarios` and `protocol-broker` fixture with `--ledger sqlite` gives exactly the expected output (driver run with the flag).
2. Atomicity: a failing statement inside T-reserve leaves no receipt, event or grant use (test hook or injected constraint violation).
3. Persistence: after `restart_broker`, and in a **second process** using `--ledger-db` on the same file: receipts, grant uses, consumed permits, refusal count and `next_permit` persist; a `crash_after_dispatch` receipt answers `RECONCILIATION_REQUIRED` and `adapter_effects` does not grow; a `crash_after_reserve` receipt resumes without a second grant use.
4. Contention: at least 64 threads, one connection each, racing for one single-use grant with distinct keys: exactly one reservation and one grant use; racing on one idempotency key: exactly one dispatch; repeated at least 20 times.
5. Schema drift: the embedded schema is byte-identical to `m1/fixtures/broker/ledger.sql`.
6. `proto sqlite-info` reports 3.54.1 and the settings above.

## Implementation notes (recorded 2026-11-07 after both implementations; no semantic change)

- **Build revision 2:** revision 1 omitted `-DHAVE_USLEEP=1`, so SQLite's default busy handler slept in whole seconds (about 5 retries within `busy_timeout=5000`). Found by the Rust implementer; fixed for both before any measurement. `HAVE_USLEEP` is not listed by `pragma compile_options`; the linked library path is recorded in the Rust build output and the Go binary's build info.
- **Driver-issued pragmas:** `mattn/go-sqlite3` always runs `busy_timeout=5000`, `locking_mode=NORMAL` (the default) and `synchronous=NORMAL` before the connect hook, which then applies the table above, so the effective settings are identical; `rusqlite` sets a 5000 ms busy timeout on open. `proto sqlite-info` reads back the effective values in both.
- **Grant verdict:** stored inside the canonical `binding` JSON in both (the binding includes the verdict, BROKER.md step 5.3).
- **Readings chosen identically by both implementers:** an `INPUT_MALFORMED` scenario creates no database file; a ledger that cannot be opened or a `final` that cannot be read is reported as `LEDGER_UNAVAILABLE` (the exact shape differs and is untested: Rust `final: {"error": ...}`, Go answers the line `{"error": ...}` when the ledger cannot open); a `restart_broker` inside a `concurrent` group reopens only that worker's connection (no fixture or generator produces it).
- **Go only:** the receipt key is two columns in SQLite but `tenant + NUL + key` in the in-memory map; no fixture or generator produces a tenant or idempotency key containing NUL, so this is untested, not observed.
