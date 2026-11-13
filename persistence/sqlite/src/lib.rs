//! Writ V2 local ledger profile on SQLite (M3-01; grants and T-reserve M3-03; execute
//! lifecycle and the controlled effect adapter's store M3-04).
//!
//! **Ledger identity** (docs/implementation/M3-START-PLAN.md "Coordination boundary"):
//! a ledger is identified by the canonical path of an existing regular file (every
//! symbolic link and `.`/`..` component resolved), so every supported path alias of one
//! file maps to the same identity and the same ownership lock `<canonical path>.owner`.
//! A ledger file with more than one hard link, or that is not a regular file, is refused
//! (`ownership AMBIGUOUS_IDENTITY`): different names for one inode would also give SQLite
//! different `-wal` and `-shm` files. Network filesystems are unsupported and not
//! detected. The directory holding the ledger must not be writable by untrusted parties
//! (path substitution between the identity check and the open is out of scope; M5).
//!
//! **Single-owner coordination:** the ownership lock is an exclusive OS lock
//! (`File::try_lock`) on `<canonical path>.owner`, acquired and released under the
//! exclusive guard of the process lifecycle gate ([`writ_broker::process::OwnerLock`];
//! no child spawn of this process overlaps it, and it is unlocked before close so a
//! child's inherited duplicate never keeps it alive, E-066), held for the lifetime of the
//! [`SqliteLedger`]; a second instance (broker or reconciler) fails closed with
//! [`StoreError::OwnedElsewhere`] (externally `LEDGER_UNAVAILABLE`, operator diagnostic
//! `OWNED_ELSEWHERE`) instead of waiting; the OS releases it when the process exits or is
//! killed. After locking, the instance increments `meta.owner_epoch`; every state-changing
//! operation runs in one `BEGIN IMMEDIATE` transaction that first checks the stored epoch
//! is still its own ([`StoreError::Fenced`] otherwise). The only state-changing public
//! operations are the typed ones on [`SqliteLedger`] and [`LedgerStore`]; no public
//! method exposes a connection or transaction.
//!
//! **Create versus open:** [`SqliteLedger::create`] makes a new ledger at a path that
//! must not exist (exclusive create), initializes it and switches it to WAL; on any
//! failure it removes the files it created. [`SqliteLedger::open`] never initializes: it
//! verifies an existing ledger read-only (exact schema objects and definitions, required
//! metadata, WAL mode, `quick_check`) and refuses anything else without modifying it;
//! only a verified ledger gets its owner epoch incremented.
//!
//! No public escape hatch: neither a raw read callback nor the fenced write path is
//! reachable from outside the crate.
//!
//! ```compile_fail
//! fn f(l: &writ_ledger_sqlite::SqliteLedger) { let _ = l.read(|_c| Ok(())); }
//! ```
//!
//! ```compile_fail
//! fn f(l: &mut writ_ledger_sqlite::SqliteLedger) { let _ = l.write("x", |_tx| Ok(())); }
//! ```
//!
//! ```compile_fail
//! fn f(l: &writ_ledger_sqlite::SqliteLedger) { let _ = &l.inner; }
//! ```
//!
//! **Concurrency (M3-03):** one [`SqliteLedger`] is shared by every thread of the owning
//! process (`SqliteLedger: Sync`; share it by reference or in an `Arc`). It holds exactly
//! one connection behind a mutex, so logical transactions of the owning process are
//! serialized in-process: each one locks the mutex, runs `BEGIN IMMEDIATE`, checks the
//! fencing epoch, runs its body and commits or rolls back before the mutex is released.
//! There is no second owner connection, so in-process callers never contend inside SQLite
//! (no `SQLITE_BUSY` between them); `busy_timeout` still applies to foreign readers. The
//! connection mutex is the innermost lock (D-029 order: policy gate, per-key lock, then
//! the transaction); no other lock is ever taken while it is held. A panic while it is
//! held (never expected) poisons it and every later operation fails closed
//! (`LEDGER_UNAVAILABLE`, code `poisoned`) until restart. The shared-reference store
//! operations are [`LedgerStore`] for `&SqliteLedger` and [`GrantLedger`]; the
//! `LedgerStore` implementation for `SqliteLedger` itself delegates to them.
//!
//! **Grants and T-reserve (M3-03):** `approve` inserts a grant (`uses` 0) unless its label
//! exists; `reserve` is LEDGER-SQLITE.md T-reserve with the broker's semantics
//! ([`writ_broker::grants`]): receipt lookup, grant checks, a conditional
//! `UPDATE grants SET uses = uses + 1 WHERE label = ? AND uses < max_uses` that must change
//! exactly one row, one `grant_uses` row, the `reserved` receipt and its `reserve` event,
//! all in one fenced transaction. A stored grant that fails the C-4 integrity check, an
//! unrepresentable stored expiry or an unknown stored receipt status is
//! `transaction reserve` / `integrity` (fail closed, no grant use, no receipt).
//!
//! **Execute lifecycle (M3-04; D-035, schema `writ-ledger-4`):** [`ExecuteLedger`] adds the
//! fenced `dispatch` (grant revalidation, `next_permit` incremented, receipt `reserved` to
//! `dispatched`, permit number recorded in `receipt_permits`, `dispatch` event), `observe`
//! (`dispatched` to `observed_success` or `observed_failure`, `observe` event),
//! `reconcile_scan` and `reconcile` (adapter evidence recorded by checked transitions,
//! otherwise `dispatched` to `unknown_reconcile`; `reconcile` events and `reconciliations`
//! records) transactions, and the `audit` of a refusal with its `failure_records` row. A
//! transition that matches no row rolls back with the diagnostic code `STATE_CONFLICT`
//! (D-029). Every transaction failure reports its stage (begin, body, commit); a `COMMIT`
//! that was issued and failed reports an unknown outcome (D-035 item 4). Consumed permits
//! and effects live in the controlled effect adapter's own store, [`SqliteEffectStore`]
//! (module `effects`), which is bound to this ledger's [`LedgerId`].
//!
//! **Schema versions:** a new ledger is `writ-ledger-5` (`schema.sql`, `schema_v4.sql`, then
//! `schema_v5.sql`, plus a random `ledger_id`). [`SqliteLedger::open`] accepts only
//! `writ-ledger-5`. [`SqliteLedger::upgrade`] is the explicit operator upgrade of an exact
//! `writ-ledger-3` or `writ-ledger-4` ledger: it adds the missing tables in one transaction
//! and keeps every row; grants carried over from `writ-ledger-3` have no provenance and stay
//! non-executable (D-035 item 5).
//!
//! **Transition service (M3-05, D-036, schema `writ-ledger-5`):** workflows, project
//! acceptance policies and effect prerequisites, the evaluator registry, promotion
//! receipts, ledger-wide consumed records and attestations, workflow and administrative
//! events; the fenced `admin` and `promote` transactions and the effect-prerequisite check
//! inside T-reserve are in module `workflow`.
//!
//! **Trusted time (since schema `writ-ledger-2`):** `time_floor` holds the high-water mark `H`
//! (at most one row, raised only, in the fenced `time_floor` transaction, which returns the
//! stored maximum); `clock_events` holds startup, drift and acknowledgement records (fenced
//! `clock_event` transaction). See docs/implementation/M3-TRUSTED-TIME.md.
//!
//! Engine and settings follow D-023 (SQLite 3.54.1, WAL, `synchronous=FULL`,
//! `busy_timeout=5000`, `foreign_keys=ON`, `trusted_schema=OFF`).

use std::collections::BTreeSet;
use std::fs::{self, File, OpenOptions};
use std::io::{ErrorKind, Read};
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, MutexGuard};

use rusqlite::{
    Connection, ErrorCode, OpenFlags, OptionalExtension, Transaction, TransactionBehavior,
};
use writ_broker::clock::{timestamp_from_parts, ClockEvent};
use writ_broker::execute::{
    DispatchIntent, Dispatched, FailureRecord, Observation, PendingReceipt, Reconciled, Resolution,
};
use writ_broker::grants::{
    GrantCheck, GrantInsert, NewGrant, ReceiptStatus, ReserveOutcome, ReserveRequest, Revalidation,
    StoredGrant, StoredProvenance, StoredReceipt,
};
use writ_broker::policy::ExclusiveHold;
use writ_broker::process::{OwnerLock, OwnerLockError};
use writ_broker::refusal::Refusal;
use writ_broker::store::Timestamp;
pub use writ_broker::store::{ExecuteLedger, GrantLedger, LedgerStore, StoreError};

mod effects;
mod workflow;
pub use effects::{SqliteEffectStore, EFFECTS_SCHEMA, EFFECTS_SCHEMA_VERSION};
use writ_broker::store::{TxFailure, TxStage};

/// The exact `writ-ledger-3` schema (the base objects of every ledger).
pub const SCHEMA_V3: &str = include_str!("schema.sql");
/// The tables `writ-ledger-4` adds (D-035).
pub const SCHEMA_V4_ADDITIONS: &str = include_str!("schema_v4.sql");
/// The production schema, applied once to a newly created ledger inside one transaction.
/// The tables `writ-ledger-5` adds (M3-05, D-036).
pub const SCHEMA_V5_ADDITIONS: &str = include_str!("schema_v5.sql");
/// The exact `writ-ledger-4` schema.
pub const SCHEMA_V4: &str = concat!(include_str!("schema.sql"), include_str!("schema_v4.sql"));
/// The production schema, applied once to a newly created ledger inside one transaction.
pub const SCHEMA: &str = concat!(
    include_str!("schema.sql"),
    include_str!("schema_v4.sql"),
    include_str!("schema_v5.sql")
);
/// The schema version this crate writes and accepts.
pub const SCHEMA_VERSION: &str = "writ-ledger-5";
/// Older versions [`SqliteLedger::upgrade`] accepts.
pub const SCHEMA_VERSION_V3: &str = "writ-ledger-3";
/// See [`SCHEMA_VERSION_V3`].
pub const SCHEMA_VERSION_V4: &str = "writ-ledger-4";

/// The random identity of one ledger (32 lowercase hexadecimal digits), fixed at creation
/// and stored in its metadata. An effect store is bound to exactly one ledger identity.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LedgerId(String);

impl LedgerId {
    /// `Some` for exactly 32 lowercase hexadecimal digits.
    pub fn parse(text: &str) -> Option<LedgerId> {
        (text.len() == 32 && text.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f')))
            .then(|| LedgerId(text.to_string()))
    }

    /// A new random identity from the operating system's random source.
    fn random() -> Result<LedgerId, StoreError> {
        let mut bytes = [0u8; 16];
        File::open("/dev/urandom")
            .and_then(|mut f| f.read_exact(&mut bytes))
            .map_err(|_| unavailable("initialize", "io"))?;
        Ok(LedgerId(bytes.iter().map(|b| format!("{b:02x}")).collect()))
    }

    /// The identity text.
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

/// A SQLite ledger owned by this process; safe to share between its threads.
pub struct SqliteLedger {
    // Field order matters (no custom Drop): fields drop in declaration order, so the
    // connection closes before the ownership lock is released.
    inner: Mutex<Inner>,
    epoch: u64,
    id: LedgerId,
    _lock: Option<OwnerLock>,
}

/// The owner connection and (in unit tests) the armed fault, behind one mutex.
struct Inner {
    conn: Connection,
    #[cfg(test)]
    fault: Option<fault::Fault>,
}

/// How a transaction body ends when it does not fail.
enum End<T> {
    Commit(T),
    Rollback(T),
}

/// Why a transaction body failed (rolled back, fail closed).
enum BodyError {
    Sql(rusqlite::Error),
    /// Stored content violates the ledger's own invariants (C-4).
    Integrity,
    /// A stored grant has no intact approver provenance (D-035 item 5).
    Provenance,
    /// A receipt transition matched no row, or a stored status is outside the schema
    /// where a transition expected one (D-029 `STATE_CONFLICT`).
    StateConflict,
}

#[cfg(test)]
impl Inner {
    /// Disarm and return the fault armed for transaction `name`, if any (test only).
    fn take_fault(&mut self, name: &str) -> Option<fault::Fault> {
        match &self.fault {
            Some(f) if f.transaction == name => self.fault.take(),
            _ => None,
        }
    }
}

impl From<rusqlite::Error> for BodyError {
    fn from(e: rusqlite::Error) -> BodyError {
        BodyError::Sql(e)
    }
}

/// Test-only storage fault injection at the transaction boundary (owner decision C of
/// 2026-11-12; S-7). Compiled only for this crate's unit tests (`#[cfg(test)]`): no
/// production build, feature or public API can reach it, and it adds no trigger or
/// schema object, so schema validation is unchanged.
///
/// A [`fault::Fault`] armed on a ledger makes the next logical transaction with the
/// given name fail once, at begin (before `BEGIN IMMEDIATE`), in the body (after the
/// fenced body ran its statements) or at commit (instead of `COMMIT`), with a
/// SQLite-like primary result code that goes through the same error mapping as a real
/// engine error. A failed transaction is rolled back when it drops. The same mechanism
/// arms the effect store's `adapter` transaction. Partial-progress accounting across the
/// transactions of one execution (M3-04) is tested in `execute_tests`; a `hang` fault
/// instead blocks at the stage so that `kill_tests` can SIGKILL a real process there.
#[cfg(test)]
mod fault {
    /// Where the transaction fails.
    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    pub enum Stage {
        Begin,
        Body,
        Commit,
    }

    /// A one-shot fault for the transaction named `transaction`.
    #[derive(Clone, Debug)]
    pub struct Fault {
        pub transaction: String,
        pub stage: Stage,
        /// SQLite primary result code (for example `rusqlite::ffi::SQLITE_IOERR`).
        pub code: std::ffi::c_int,
        /// Instead of failing, announce `AT <transaction> <stage>` on stdout and block
        /// forever at the stage (the real process-kill tests SIGKILL the process there).
        /// `Begin` blocks before `BEGIN IMMEDIATE`; `Body` and `Commit` block with the
        /// transaction open, after its statements and before `COMMIT`.
        pub hang: bool,
    }

    /// The code to raise when `armed` fails at `stage`.
    pub fn injected(armed: &Option<Fault>, stage: Stage) -> Option<std::ffi::c_int> {
        armed
            .as_ref()
            .filter(|f| f.stage == stage && !f.hang)
            .map(|f| f.code)
    }

    /// Block forever if `armed` hangs at `stage` (after announcing it).
    pub fn hang(armed: &Option<Fault>, stage: Stage) {
        if let Some(f) = armed.as_ref().filter(|f| f.stage == stage && f.hang) {
            use std::io::Write;
            let out = std::io::stdout();
            let mut out = out.lock();
            let _ = writeln!(out, "AT {} {:?}", f.transaction, f.stage);
            let _ = out.flush();
            drop(out);
            loop {
                std::thread::sleep(std::time::Duration::from_secs(60));
            }
        }
    }

    /// The SQLite-like error the fault raises.
    pub fn error(code: std::ffi::c_int) -> rusqlite::Error {
        rusqlite::Error::SqliteFailure(rusqlite::ffi::Error::new(code), None)
    }
}

fn unavailable(category: &str, code: &str) -> StoreError {
    StoreError::Unavailable {
        category: category.to_string(),
        code: code.to_string(),
    }
}

fn code_name(e: &rusqlite::Error) -> &'static str {
    match e {
        rusqlite::Error::SqliteFailure(f, _) => match f.code {
            ErrorCode::DatabaseBusy => "SQLITE_BUSY",
            ErrorCode::DatabaseLocked => "SQLITE_LOCKED",
            ErrorCode::ReadOnly => "SQLITE_READONLY",
            ErrorCode::SystemIoFailure => "SQLITE_IOERR",
            ErrorCode::DatabaseCorrupt => "SQLITE_CORRUPT",
            ErrorCode::DiskFull => "SQLITE_FULL",
            ErrorCode::CannotOpen => "SQLITE_CANTOPEN",
            ErrorCode::NotADatabase => "SQLITE_NOTADB",
            ErrorCode::ConstraintViolation => "SQLITE_CONSTRAINT",
            _ => "SQLITE_ERROR",
        },
        _ => "SQLITE_ERROR",
    }
}

fn sidecar(path: &Path, suffix: &str) -> PathBuf {
    let mut p = path.as_os_str().to_owned();
    p.push(suffix);
    PathBuf::from(p)
}

/// Canonical identity of an existing ledger file: a regular file with exactly one link.
fn existing_identity(path: &Path) -> Result<PathBuf, StoreError> {
    let canonical = fs::canonicalize(path).map_err(|_| unavailable("open", "io"))?;
    let meta = fs::metadata(&canonical).map_err(|_| unavailable("open", "io"))?;
    if !meta.is_file() || meta.nlink() != 1 {
        return Err(unavailable("ownership", "AMBIGUOUS_IDENTITY"));
    }
    Ok(canonical)
}

/// Canonical identity for a new ledger: canonical parent directory plus the file name.
fn new_identity(path: &Path) -> Result<PathBuf, StoreError> {
    let name = path.file_name().ok_or_else(|| unavailable("open", "io"))?;
    let parent = match path.parent() {
        Some(p) if !p.as_os_str().is_empty() => p,
        _ => Path::new("."),
    };
    let dir = fs::canonicalize(parent).map_err(|_| unavailable("open", "io"))?;
    Ok(dir.join(name))
}

/// Take the ownership lock `<canonical>.owner` of a store identity without waiting
/// ([`OwnerLock`]: acquired and released under the exclusive guard of the process
/// lifecycle gate, and unlocked before close, so a duplicate held by a child spawned by
/// this process never keeps a released lock alive, E-066).
/// Every store of this crate (ledger, effect store, and any later one) takes its ownership
/// through this function and keeps the returned lock as its last field.
pub(crate) fn take_ownership(canonical: &Path) -> Result<OwnerLock, StoreError> {
    OwnerLock::acquire(&sidecar(canonical, ".owner")).map_err(|e| match e {
        OwnerLockError::Held => StoreError::OwnedElsewhere,
        OwnerLockError::Io => unavailable("ownership", "io"),
    })
}

fn connect(canonical: &Path, flags: OpenFlags) -> Result<Connection, StoreError> {
    let conn = Connection::open_with_flags(canonical, flags | OpenFlags::SQLITE_OPEN_NO_MUTEX)
        .map_err(|e| unavailable("open", code_name(&e)))?;
    conn.execute_batch(
        "PRAGMA busy_timeout = 5000; PRAGMA foreign_keys = ON; PRAGMA trusted_schema = OFF;",
    )
    .map_err(|e| unavailable("open", code_name(&e)))?;
    Ok(conn)
}

type SchemaObject = (String, String, String, String);

fn schema_objects(conn: &Connection) -> rusqlite::Result<BTreeSet<SchemaObject>> {
    let mut stmt = conn.prepare(
        "SELECT type, name, tbl_name, coalesce(sql, '') FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'",
    )?;
    let rows = stmt
        .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)))?
        .collect::<rusqlite::Result<BTreeSet<SchemaObject>>>();
    rows
}

/// The exact schema objects (type, name, table, definition) of a ledger with `schema`.
fn expected_objects(schema: &str) -> rusqlite::Result<BTreeSet<SchemaObject>> {
    let mem = Connection::open_in_memory()?;
    mem.execute_batch(schema)?;
    schema_objects(&mem)
}

fn meta_value(conn: &Connection, key: &str) -> rusqlite::Result<Option<String>> {
    match conn.query_row("SELECT value FROM meta WHERE key = ?1", [key], |r| r.get(0)) {
        Ok(v) => Ok(Some(v)),
        Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
        Err(e) => Err(e),
    }
}

/// Read-only verification of an existing ledger against the exact objects of `schema`
/// and `version` (with a valid `ledger_id` for `writ-ledger-4`). Any failure is
/// `initialize schema` (or the SQLite code) and nothing in the file is changed.
fn verify(conn: &Connection, schema_text: &str, want: &str) -> Result<(), StoreError> {
    let schema = |_| unavailable("initialize", "schema");
    let sql = |e: rusqlite::Error| unavailable("initialize", code_name(&e));
    let found = schema_objects(conn).map_err(sql)?;
    let expected = expected_objects(schema_text).map_err(sql)?;
    if found != expected {
        return Err(unavailable("initialize", "schema"));
    }
    let id = meta_value(conn, "ledger_id").map_err(sql)?;
    let id_ok = match want {
        SCHEMA_VERSION_V3 => id.is_none(),
        _ => id.as_deref().and_then(LedgerId::parse).is_some(),
    };
    if !id_ok {
        return Err(schema(()));
    }
    let version = meta_value(conn, "schema_version").map_err(sql)?;
    let epoch = meta_value(conn, "owner_epoch").map_err(sql)?;
    let permit: Option<i64> = match conn.query_row(
        "SELECT value FROM counters WHERE name = 'next_permit'",
        [],
        |r| r.get(0),
    ) {
        Ok(v) => Some(v),
        Err(rusqlite::Error::QueryReturnedNoRows) => None,
        Err(e) => return Err(sql(e)),
    };
    if version.as_deref() != Some(want)
        || epoch.and_then(|e| e.parse::<u64>().ok()).is_none()
        || permit.is_none_or(|p| p < 0)
    {
        return Err(schema(()));
    }
    let mode: String = conn
        .query_row("PRAGMA journal_mode", [], |r| r.get(0))
        .map_err(sql)?;
    if mode != "wal" {
        return Err(schema(()));
    }
    let check: String = conn
        .query_row("PRAGMA quick_check", [], |r| r.get(0))
        .map_err(sql)?;
    if check != "ok" {
        return Err(unavailable("initialize", "SQLITE_CORRUPT"));
    }
    let fk_violation: bool = conn
        .query_row(
            "SELECT EXISTS (SELECT 1 FROM pragma_foreign_key_check)",
            [],
            |r| r.get(0),
        )
        .map_err(sql)?;
    if fk_violation {
        return Err(unavailable("initialize", "SQLITE_CONSTRAINT"));
    }
    Ok(())
}

fn bump_epoch(conn: &mut Connection) -> Result<u64, StoreError> {
    let sql = |e: rusqlite::Error| unavailable("initialize", code_name(&e));
    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(sql)?;
    let current = meta_value(&tx, "owner_epoch")
        .map_err(sql)?
        .and_then(|e| e.parse::<u64>().ok())
        .ok_or_else(|| unavailable("initialize", "schema"))?;
    let next = current
        .checked_add(1)
        .ok_or_else(|| unavailable("initialize", "schema"))?;
    tx.execute(
        "UPDATE meta SET value = ?1 WHERE key = 'owner_epoch'",
        [next.to_string()],
    )
    .map_err(sql)?;
    tx.commit().map_err(sql)?;
    Ok(next)
}

fn finish(mut conn: Connection, lock: OwnerLock) -> Result<SqliteLedger, StoreError> {
    conn.execute_batch("PRAGMA synchronous = FULL;")
        .map_err(|e| unavailable("initialize", code_name(&e)))?;
    let id = meta_value(&conn, "ledger_id")
        .map_err(|e| unavailable("initialize", code_name(&e)))?
        .as_deref()
        .and_then(LedgerId::parse)
        .ok_or_else(|| unavailable("initialize", "schema"))?;
    let epoch = bump_epoch(&mut conn)?;
    Ok(SqliteLedger {
        inner: Mutex::new(Inner {
            conn,
            #[cfg(test)]
            fault: None,
        }),
        epoch,
        id,
        _lock: Some(lock),
    })
}

/// Run one `BEGIN IMMEDIATE` logical transaction named `name` on the connection behind
/// `inner`, whose body decides to commit or roll back. With `epoch`, the transaction
/// first checks the stored owner epoch (fencing). The connection mutex is held for the
/// whole transaction. Any failure rolls back and fails closed, reporting its stage:
/// `begin` (mutex poisoned, `BEGIN IMMEDIATE`), `body` (fencing check, statements,
/// integrity and state conflicts) or `commit` (`COMMIT` or `ROLLBACK`).
fn transact_on<T>(
    inner: &Mutex<Inner>,
    epoch: Option<u64>,
    name: &str,
    body: impl FnOnce(&Transaction<'_>) -> Result<End<T>, BodyError>,
) -> Result<T, TxFailure> {
    let category = format!("transaction {name}");
    let at = |stage: TxStage, code: &str| TxFailure::new(stage, unavailable(&category, code));
    let mut inner = inner.lock().map_err(|_| at(TxStage::Begin, "poisoned"))?;
    #[cfg(test)]
    let armed = inner.take_fault(name);
    #[cfg(test)]
    fault::hang(&armed, fault::Stage::Begin);
    #[cfg(test)]
    if let Some(code) = fault::injected(&armed, fault::Stage::Begin) {
        return Err(at(TxStage::Begin, code_name(&fault::error(code))));
    }
    let tx = inner
        .conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|e| at(TxStage::Begin, code_name(&e)))?;
    if let Some(mine) = epoch {
        let stored = meta_value(&tx, "owner_epoch")
            .map_err(|e| at(TxStage::Body, code_name(&e)))?
            .and_then(|e| e.parse::<u64>().ok())
            .ok_or_else(|| at(TxStage::Body, "schema"))?;
        if stored != mine {
            return Err(TxFailure::new(
                TxStage::Body,
                StoreError::Fenced { mine, stored },
            ));
        }
    }
    let end = body(&tx).map_err(|e| match e {
        BodyError::Sql(e) => at(TxStage::Body, code_name(&e)),
        BodyError::Integrity => at(TxStage::Body, "integrity"),
        BodyError::Provenance => at(TxStage::Body, "provenance"),
        BodyError::StateConflict => at(TxStage::Body, StoreError::STATE_CONFLICT),
    })?;
    // A failed `COMMIT` that was issued has an unknown outcome (D-035 item 4).
    let commit_failed = |code: &str| TxFailure::commit_unknown(unavailable(&category, code));
    #[cfg(test)]
    {
        fault::hang(&armed, fault::Stage::Body);
        if let Some(code) = fault::injected(&armed, fault::Stage::Body) {
            // `tx` drops here without committing: rolled back.
            return Err(at(TxStage::Body, code_name(&fault::error(code))));
        }
        fault::hang(&armed, fault::Stage::Commit);
        if let Some(code) = fault::injected(&armed, fault::Stage::Commit) {
            // Simulates a failed COMMIT (`tx` drops: actually rolled back). For a body that
            // ended in commit, the caller cannot know the outcome; a rollback committed
            // nothing.
            let code = code_name(&fault::error(code));
            return Err(match end {
                End::Commit(_) => commit_failed(code),
                End::Rollback(_) => at(TxStage::Commit, code),
            });
        }
    }
    match end {
        End::Commit(out) => {
            tx.commit().map_err(|e| commit_failed(code_name(&e)))?;
            Ok(out)
        }
        End::Rollback(out) => {
            tx.rollback()
                .map_err(|e| at(TxStage::Commit, code_name(&e)))?;
            Ok(out)
        }
    }
}

impl SqliteLedger {
    /// Create a new ledger at `path`, which must not exist (its directory must).
    ///
    /// Order: ownership lock on the canonical identity; exclusive creation of the file;
    /// schema in one transaction; `journal_mode=WAL` (read back); owner epoch. On any
    /// failure after the exclusive creation, the file and any `-journal`, `-wal` or
    /// `-shm` file this call created are removed (S-2); the `.owner` lock file may
    /// remain (empty, harmless).
    pub fn create(path: &Path) -> Result<SqliteLedger, StoreError> {
        let canonical = new_identity(path)?;
        let lock = take_ownership(&canonical)?;
        match OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&canonical)
        {
            Ok(_) => {}
            Err(e) if e.kind() == ErrorKind::AlreadyExists => {
                return Err(unavailable("open", "EXISTS"))
            }
            Err(_) => return Err(unavailable("open", "io")),
        }
        let preexisting: Vec<bool> = ["-journal", "-wal", "-shm"]
            .iter()
            .map(|s| sidecar(&canonical, s).exists())
            .collect();
        let result = (|| {
            let mut conn = connect(&canonical, OpenFlags::SQLITE_OPEN_READ_WRITE)?;
            let tx = conn
                .transaction_with_behavior(TransactionBehavior::Immediate)
                .map_err(|e| unavailable("initialize", code_name(&e)))?;
            tx.execute_batch(SCHEMA)
                .map_err(|e| unavailable("initialize", code_name(&e)))?;
            tx.execute(
                "INSERT INTO meta (key, value) VALUES ('ledger_id', ?1)",
                [LedgerId::random()?.as_str()],
            )
            .map_err(|e| unavailable("initialize", code_name(&e)))?;
            tx.commit()
                .map_err(|e| unavailable("initialize", code_name(&e)))?;
            let mode: String = conn
                .query_row("PRAGMA journal_mode = WAL", [], |r| r.get(0))
                .map_err(|e| unavailable("initialize", code_name(&e)))?;
            if mode != "wal" {
                return Err(unavailable("initialize", "SQLITE_ERROR"));
            }
            Ok(conn)
        })();
        match result.and_then(|conn| finish(conn, lock)) {
            Ok(ledger) => Ok(ledger),
            Err(e) => {
                let _ = fs::remove_file(&canonical);
                for (suffix, existed) in ["-journal", "-wal", "-shm"].iter().zip(preexisting) {
                    if !existed {
                        let _ = fs::remove_file(sidecar(&canonical, suffix));
                    }
                }
                Err(e)
            }
        }
    }

    /// Open an existing ledger at `path` and take ownership of it.
    ///
    /// Order: canonical identity (regular file, one link); ownership lock; read-only
    /// verification ([`verify`]); owner epoch increment. A file that fails verification
    /// is refused without any change (no initialization, no WAL switch). A failure after
    /// the epoch increment cannot occur in this implementation; epochs are monotonic and
    /// gaps from failed starts are harmless.
    pub fn open(path: &Path) -> Result<SqliteLedger, StoreError> {
        let canonical = existing_identity(path)?;
        let lock = take_ownership(&canonical)?;
        let conn = connect(&canonical, OpenFlags::SQLITE_OPEN_READ_WRITE)?;
        verify(&conn, SCHEMA, SCHEMA_VERSION)?;
        finish(conn, lock)
    }

    /// Explicit operator upgrade of an existing `writ-ledger-3` or `writ-ledger-4` ledger
    /// at `path` to the current schema, then take ownership of it (D-035 item 5, D-036).
    ///
    /// Order: canonical identity; ownership lock; read-only verification of the exact
    /// older schema (any other file, including a current ledger, is refused unchanged);
    /// in one `BEGIN IMMEDIATE` transaction the missing tables, the new version and, from
    /// `writ-ledger-3`, a new random `ledger_id`; verification as the current schema;
    /// owner epoch. Every existing row is kept and nothing is invented: carried-over
    /// grants get no provenance (not executable until an explicit reviewed migration or
    /// recovery), carried-over `dispatched` receipts get no permit number (their
    /// uncertainty is retained by reconciliation), and there are no workflows until an
    /// operator creates them.
    pub fn upgrade(path: &Path) -> Result<SqliteLedger, StoreError> {
        let canonical = existing_identity(path)?;
        let lock = take_ownership(&canonical)?;
        let mut conn = connect(&canonical, OpenFlags::SQLITE_OPEN_READ_WRITE)?;
        let from_v3 = match verify(&conn, SCHEMA_V3, SCHEMA_VERSION_V3) {
            Ok(()) => true,
            Err(_) => {
                verify(&conn, SCHEMA_V4, SCHEMA_VERSION_V4)?;
                false
            }
        };
        let sql = |e: rusqlite::Error| unavailable("upgrade", code_name(&e));
        let tx = conn
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(sql)?;
        if from_v3 {
            tx.execute_batch(SCHEMA_V4_ADDITIONS).map_err(sql)?;
            tx.execute(
                "INSERT INTO meta (key, value) VALUES ('ledger_id', ?1)",
                [LedgerId::random()?.as_str()],
            )
            .map_err(sql)?;
        }
        tx.execute_batch(SCHEMA_V5_ADDITIONS).map_err(sql)?;
        tx.commit().map_err(sql)?;
        verify(&conn, SCHEMA, SCHEMA_VERSION)?;
        finish(conn, lock)
    }

    /// This ledger's identity (bind its effect store to it).
    pub fn ledger_id(&self) -> &LedgerId {
        &self.id
    }

    /// The owner connection, or fail closed if a holder panicked (`category` / `poisoned`).
    fn lock(&self, category: &str) -> Result<MutexGuard<'_, Inner>, StoreError> {
        self.inner
            .lock()
            .map_err(|_| unavailable(category, "poisoned"))
    }

    /// Run a typed state change in one fenced `BEGIN IMMEDIATE` transaction named `name`.
    /// Crate-private: public callers only reach typed operations.
    fn write<T>(
        &self,
        name: &str,
        body: impl FnOnce(&Transaction<'_>) -> rusqlite::Result<T>,
    ) -> Result<T, StoreError> {
        self.transact(name, |tx| Ok(End::Commit(body(tx)?)))
            .map_err(StoreError::from)
    }

    /// Run one fenced `BEGIN IMMEDIATE` logical transaction named `name` whose body
    /// decides to commit or roll back ([`transact_on`] with this owner's epoch).
    fn transact<T>(
        &self,
        name: &str,
        body: impl FnOnce(&Transaction<'_>) -> Result<End<T>, BodyError>,
    ) -> Result<T, TxFailure> {
        transact_on(&self.inner, Some(self.epoch), name, body)
    }

    /// Arm a one-shot fault (test only).
    #[cfg(test)]
    fn inject(&self, transaction: &str, stage: fault::Stage, code: std::ffi::c_int) {
        if let Ok(mut inner) = self.inner.lock() {
            inner.fault = Some(fault::Fault {
                transaction: transaction.to_string(),
                stage,
                code,
                hang: false,
            });
        }
    }

    /// Arm a one-shot hang (test only; see [`fault::Fault::hang`]).
    #[cfg(test)]
    fn inject_hang(&self, transaction: &str, stage: fault::Stage) {
        if let Ok(mut inner) = self.inner.lock() {
            inner.fault = Some(fault::Fault {
                transaction: transaction.to_string(),
                stage,
                code: 0,
                hang: true,
            });
        }
    }

    /// The connection for direct inspection (test only).
    #[cfg(test)]
    fn db(&self) -> MutexGuard<'_, Inner> {
        self.inner
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }

    /// Number of recorded refusals (`final.refusals`).
    pub fn refusal_count(&self) -> Result<u64, StoreError> {
        let n: i64 = self
            .lock("read")?
            .conn
            .query_row("SELECT count(*) FROM audit_refusals", [], |r| r.get(0))
            .map_err(|e| unavailable("read", code_name(&e)))?;
        u64::try_from(n).map_err(|_| unavailable("read", "schema"))
    }

    /// The linked SQLite engine version (D-023 check).
    pub fn engine_version(&self) -> Result<String, StoreError> {
        self.lock("read")?
            .conn
            .query_row("SELECT sqlite_version()", [], |r| r.get(0))
            .map_err(|e| unavailable("read", code_name(&e)))
    }
}

impl SqliteLedger {
    fn audit(&self, code: &str) -> Result<(), StoreError> {
        self.write("audit", |tx| {
            tx.execute("INSERT INTO audit_refusals (code) VALUES (?1)", [code])
                .map(|_| ())
        })
    }

    fn floor(&self) -> Result<Option<Timestamp>, StoreError> {
        let row = match self.lock("read")?.conn.query_row(
            "SELECT floor_s, floor_ns FROM time_floor WHERE id = 1",
            [],
            |r| Ok((r.get::<_, i64>(0)?, r.get::<_, i64>(1)?)),
        ) {
            Ok(row) => row,
            Err(rusqlite::Error::QueryReturnedNoRows) => return Ok(None),
            Err(e) => return Err(unavailable("read", code_name(&e))),
        };
        floor_timestamp(row, "read").map(Some)
    }

    fn raise_floor(&self, at: Timestamp) -> Result<Timestamp, StoreError> {
        let row = self.write("time_floor", |tx| {
            tx.execute(
                "INSERT INTO time_floor (id, floor_s, floor_ns) VALUES (1, ?1, ?2) \
                 ON CONFLICT (id) DO UPDATE SET floor_s = excluded.floor_s, floor_ns = excluded.floor_ns \
                 WHERE (excluded.floor_s, excluded.floor_ns) > (time_floor.floor_s, time_floor.floor_ns)",
                (at.unix_seconds(), i64::from(at.subsec_nanos())),
            )?;
            tx.query_row(
                "SELECT floor_s, floor_ns FROM time_floor WHERE id = 1",
                [],
                |r| Ok((r.get::<_, i64>(0)?, r.get::<_, i64>(1)?)),
            )
        })?;
        floor_timestamp(row, "transaction time_floor")
    }

    fn clock_event(&self, event: &ClockEvent) -> Result<(), StoreError> {
        let text = |t: Option<Timestamp>| t.map(|t| t.to_string());
        self.write("clock_event", |tx| {
            tx.execute(
                "INSERT INTO clock_events (kind, effective, wall, floor, operator) VALUES (?1, ?2, ?3, ?4, ?5)",
                (
                    event.kind.as_str(),
                    text(event.effective),
                    text(event.wall),
                    text(event.floor),
                    event.operator.as_deref(),
                ),
            )
            .map(|_| ())
        })
    }
}

/// The store operations through a shared reference (any thread of the owner).
impl LedgerStore for &SqliteLedger {
    fn owner_epoch(&self) -> u64 {
        self.epoch
    }

    fn record_refusal(&mut self, code: &str) -> Result<(), StoreError> {
        self.audit(code)
    }

    fn time_floor(&self) -> Result<Option<Timestamp>, StoreError> {
        self.floor()
    }

    fn raise_time_floor(&mut self, at: Timestamp) -> Result<Timestamp, StoreError> {
        self.raise_floor(at)
    }

    fn record_clock_event(&mut self, event: &ClockEvent) -> Result<(), StoreError> {
        self.clock_event(event)
    }
}

impl LedgerStore for SqliteLedger {
    fn owner_epoch(&self) -> u64 {
        self.epoch
    }

    fn record_refusal(&mut self, code: &str) -> Result<(), StoreError> {
        self.audit(code)
    }

    fn time_floor(&self) -> Result<Option<Timestamp>, StoreError> {
        self.floor()
    }

    fn raise_time_floor(&mut self, at: Timestamp) -> Result<Timestamp, StoreError> {
        self.raise_floor(at)
    }

    fn record_clock_event(&mut self, event: &ClockEvent) -> Result<(), StoreError> {
        self.clock_event(event)
    }
}

impl GrantLedger for SqliteLedger {
    fn insert_grant(&self, grant: &NewGrant) -> Result<GrantInsert, StoreError> {
        let expires = grant.expires_at();
        self.transact("approve", |tx| {
            let exists: bool = tx.query_row(
                "SELECT EXISTS (SELECT 1 FROM grants WHERE label = ?1)",
                [grant.label().as_str()],
                |r| r.get(0),
            )?;
            if exists {
                return Ok(End::Rollback(GrantInsert::LabelInUse));
            }
            tx.execute(
                "INSERT INTO grants (label, binding, expires_s, expires_ns, max_uses, uses) \
                 VALUES (?1, ?2, ?3, ?4, ?5, 0)",
                (
                    grant.label().as_str(),
                    grant.binding(),
                    expires.unix_seconds(),
                    i64::from(expires.subsec_nanos()),
                    i64::from(grant.max_uses()),
                ),
            )?;
            let p = grant.provenance();
            tx.execute(
                "INSERT INTO grant_provenance (label, version, approver_id, approver_issuer, \
                 channel, approved_at, verdict, auth_evidence_ref, request_fingerprint, \
                 policy_bundle_sha) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)",
                rusqlite::params![
                    grant.label().as_str(),
                    p.version(),
                    p.approver_id(),
                    p.issuer(),
                    p.channel(),
                    p.approved_at().to_string(),
                    p.verdict().as_str(),
                    p.evidence_ref(),
                    p.request_fingerprint(),
                    p.policy_bundle_sha(),
                ],
            )?;
            Ok(End::Commit(GrantInsert::Inserted))
        })
        .map_err(StoreError::from)
    }

    fn reserve(
        &self,
        request: &ReserveRequest<'_>,
    ) -> Result<Result<ReserveOutcome, Refusal>, TxFailure> {
        let key = (request.tenant_id(), request.idempotency_key());
        self.transact("reserve", |tx| {
            let found: Option<(String, Option<String>, String)> = tx
                .query_row(
                    "SELECT fingerprint, grant_label, status FROM receipts \
                     WHERE tenant_id = ?1 AND idempotency_key = ?2",
                    key,
                    |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
                )
                .optional()?;
            if let Some((fingerprint, grant_label, status)) = found {
                let status = ReceiptStatus::parse(&status).ok_or(BodyError::Integrity)?;
                let stored = StoredReceipt {
                    fingerprint,
                    grant_label,
                    status,
                };
                return Ok(match request.existing(&stored) {
                    // Resume from `reserved`: a write-free commit (D-025 A), after the
                    // effect prerequisites are checked again (M3-05).
                    Ok(ReserveOutcome::Resumed) => match workflow::check_prerequisite(tx, request)? {
                        Ok(()) => End::Commit(Ok(ReserveOutcome::Resumed)),
                        Err(refusal) => End::Rollback(Err(refusal)),
                    },
                    other => End::Rollback(other),
                });
            }
            // Effect prerequisites (D-019 N-3, M3-05) before anything is reserved or consumed.
            if let Err(refusal) = workflow::check_prerequisite(tx, request)? {
                return Ok(End::Rollback(Err(refusal)));
            }
            if let Some(label) = request.grant_label() {
                let stored = stored_grant(tx, label)?;
                match request.check_grant(stored.as_ref()) {
                    GrantCheck::Consume => {}
                    GrantCheck::Refuse(refusal) => return Ok(End::Rollback(Err(refusal))),
                    GrantCheck::Integrity => return Err(BodyError::Integrity),
                    GrantCheck::Provenance => return Err(BodyError::Provenance),
                }
                let used = tx.execute(
                    "UPDATE grants SET uses = uses + 1 WHERE label = ?1 AND uses < max_uses",
                    [label],
                )?;
                if used != 1 {
                    return Ok(End::Rollback(Err(Refusal::GrantExhausted)));
                }
                tx.execute(
                    "INSERT INTO grant_uses (label, tenant_id, idempotency_key) VALUES (?1, ?2, ?3)",
                    (label, key.0, key.1),
                )?;
            }
            tx.execute(
                "INSERT INTO receipts (tenant_id, idempotency_key, fingerprint, grant_label, status) \
                 VALUES (?1, ?2, ?3, ?4, 'reserved')",
                (key.0, key.1, request.fingerprint(), request.grant_label()),
            )?;
            tx.execute(
                "INSERT INTO receipt_events (tenant_id, idempotency_key, status, cause) \
                 VALUES (?1, ?2, 'reserved', 'reserve')",
                key,
            )?;
            Ok(End::Commit(Ok(ReserveOutcome::Reserved)))
        })
    }
}

/// The grant `label` with its provenance as read inside a transaction (`None`: no such
/// grant). An unrepresentable stored expiry is an integrity failure.
fn stored_grant(tx: &Transaction<'_>, label: &str) -> Result<Option<StoredGrant>, BodyError> {
    let row: Option<(String, i64, i64, i64, i64)> = tx
        .query_row(
            "SELECT binding, expires_s, expires_ns, max_uses, uses FROM grants WHERE label = ?1",
            [label],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)),
        )
        .optional()?;
    let Some((binding, secs, nanos, max_uses, uses)) = row else {
        return Ok(None);
    };
    let provenance = tx
        .query_row(
            "SELECT version, approver_id, approver_issuer, channel, approved_at, verdict, \
             auth_evidence_ref, request_fingerprint, policy_bundle_sha \
             FROM grant_provenance WHERE label = ?1",
            [label],
            |r| {
                Ok(StoredProvenance {
                    version: r.get(0)?,
                    approver_id: r.get(1)?,
                    issuer: r.get(2)?,
                    channel: r.get(3)?,
                    approved_at: r.get(4)?,
                    verdict: r.get(5)?,
                    evidence_ref: r.get(6)?,
                    request_fingerprint: r.get(7)?,
                    policy_bundle_sha: r.get(8)?,
                })
            },
        )
        .optional()?;
    Ok(Some(StoredGrant {
        binding,
        expires_at: u32::try_from(nanos)
            .ok()
            .and_then(|n| timestamp_from_parts(secs, n))
            .ok_or(BodyError::Integrity)?,
        max_uses,
        uses,
        provenance,
    }))
}

impl ExecuteLedger for SqliteLedger {
    fn dispatch(
        &self,
        intent: &DispatchIntent<'_>,
    ) -> Result<Result<Dispatched, Refusal>, TxFailure> {
        let key = (intent.tenant_id(), intent.idempotency_key());
        self.transact("dispatch", |tx| {
            let receipt: Option<(String, Option<String>, String)> = tx
                .query_row(
                    "SELECT fingerprint, grant_label, status FROM receipts \
                     WHERE tenant_id = ?1 AND idempotency_key = ?2",
                    key,
                    |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
                )
                .optional()?;
            let Some((fingerprint, grant_label, status)) = receipt else {
                return Err(BodyError::StateConflict);
            };
            if fingerprint != intent.fingerprint()
                || status != ReceiptStatus::Reserved.as_str()
                || grant_label.as_deref() != intent.grant_label()
            {
                return Err(BodyError::StateConflict);
            }
            let grant_expires_at = match intent.grant_label() {
                None => None,
                Some(label) => {
                    let stored = stored_grant(tx, label)?;
                    let used: bool = tx.query_row(
                        "SELECT EXISTS (SELECT 1 FROM grant_uses \
                         WHERE label = ?1 AND tenant_id = ?2 AND idempotency_key = ?3)",
                        (label, key.0, key.1),
                        |r| r.get(0),
                    )?;
                    match intent.revalidate(stored.as_ref(), used) {
                        Revalidation::Valid { grant_expires_at } => Some(grant_expires_at),
                        Revalidation::Refuse(refusal) => return Ok(End::Rollback(Err(refusal))),
                        Revalidation::Integrity => return Err(BodyError::Integrity),
                        Revalidation::Provenance => return Err(BodyError::Provenance),
                    }
                }
            };
            let counted = tx.execute(
                "UPDATE counters SET value = value + 1 WHERE name = 'next_permit'",
                [],
            )?;
            if counted != 1 {
                return Err(BodyError::Integrity);
            }
            let number: i64 = tx.query_row(
                "SELECT value FROM counters WHERE name = 'next_permit'",
                [],
                |r| r.get(0),
            )?;
            let permit_number = u64::try_from(number).map_err(|_| BodyError::Integrity)?;
            let changed = tx.execute(
                "UPDATE receipts SET status = 'dispatched' \
                 WHERE tenant_id = ?1 AND idempotency_key = ?2 AND fingerprint = ?3 \
                 AND status = 'reserved'",
                (key.0, key.1, intent.fingerprint()),
            )?;
            if changed != 1 {
                return Err(BodyError::StateConflict);
            }
            tx.execute(
                "INSERT INTO receipt_permits (tenant_id, idempotency_key, permit_number) \
                 VALUES (?1, ?2, ?3)",
                (key.0, key.1, number),
            )?;
            tx.execute(
                "INSERT INTO receipt_events (tenant_id, idempotency_key, status, cause) \
                 VALUES (?1, ?2, 'dispatched', 'dispatch')",
                key,
            )?;
            Ok(End::Commit(Ok(Dispatched {
                permit_number,
                grant_expires_at,
            })))
        })
    }

    fn observe_effect(&self, observation: &Observation<'_>) -> Result<(), TxFailure> {
        let receipt = observation.receipt();
        let key = (receipt.tenant_id(), receipt.idempotency_key());
        let status = observation.status().as_str();
        self.transact("observe", |tx| {
            if !matches!(
                observation.status(),
                ReceiptStatus::ObservedSuccess | ReceiptStatus::ObservedFailure
            ) {
                return Err(BodyError::StateConflict);
            }
            let changed = tx.execute(
                "UPDATE receipts SET status = ?4 \
                 WHERE tenant_id = ?1 AND idempotency_key = ?2 AND fingerprint = ?3 \
                 AND status = 'dispatched'",
                (key.0, key.1, receipt.fingerprint(), status),
            )?;
            if changed != 1 {
                return Err(BodyError::StateConflict);
            }
            tx.execute(
                "INSERT INTO receipt_events (tenant_id, idempotency_key, status, cause) \
                 VALUES (?1, ?2, ?3, 'observe')",
                (key.0, key.1, status),
            )?;
            Ok(End::Commit(()))
        })
    }

    fn pending_receipts(
        &self,
        _gate: &ExclusiveHold<'_>,
    ) -> Result<Vec<PendingReceipt>, TxFailure> {
        self.transact("reconcile_scan", |tx| {
            let mut stmt = tx.prepare(
                "SELECT r.tenant_id, r.idempotency_key, r.fingerprint, r.status, p.permit_number \
                 FROM receipts r LEFT JOIN receipt_permits p \
                 ON p.tenant_id = r.tenant_id AND p.idempotency_key = r.idempotency_key \
                 WHERE r.status IN ('dispatched', 'unknown_reconcile') \
                 ORDER BY r.tenant_id, r.idempotency_key",
            )?;
            let rows = stmt
                .query_map([], |r| {
                    Ok((
                        r.get::<_, String>(0)?,
                        r.get::<_, String>(1)?,
                        r.get::<_, String>(2)?,
                        r.get::<_, String>(3)?,
                        r.get::<_, Option<i64>>(4)?,
                    ))
                })?
                .collect::<rusqlite::Result<Vec<_>>>()?;
            let mut pending = Vec::with_capacity(rows.len());
            for (tenant_id, idempotency_key, fingerprint, status, number) in rows {
                pending.push(PendingReceipt {
                    tenant_id,
                    idempotency_key,
                    fingerprint,
                    status: ReceiptStatus::parse(&status).ok_or(BodyError::Integrity)?,
                    permit_number: match number {
                        None => None,
                        Some(n) => Some(u64::try_from(n).map_err(|_| BodyError::Integrity)?),
                    },
                });
            }
            Ok(End::Rollback(pending))
        })
    }

    fn reconcile(
        &self,
        _gate: &ExclusiveHold<'_>,
        resolutions: &[Resolution],
    ) -> Result<Reconciled, TxFailure> {
        self.transact("reconcile", |tx| {
            let mut out = Reconciled::default();
            let record = |r: &PendingReceipt, outcome: &str, evidence: &str| {
                let number = r.permit_number.map(i64::try_from).transpose();
                let number = number.map_err(|_| BodyError::Integrity)?;
                tx.execute(
                    "INSERT INTO receipt_events (tenant_id, idempotency_key, status, cause) \
                     VALUES (?1, ?2, ?3, 'reconcile')",
                    (&r.tenant_id, &r.idempotency_key, outcome),
                )?;
                tx.execute(
                    "INSERT INTO reconciliations (tenant_id, idempotency_key, permit_number, \
                     outcome, evidence) VALUES (?1, ?2, ?3, ?4, ?5)",
                    (&r.tenant_id, &r.idempotency_key, number, outcome, evidence),
                )?;
                Ok::<(), BodyError>(())
            };
            for resolution in resolutions {
                let r = resolution.receipt();
                match resolution.observed() {
                    Some(status) => {
                        let number = r
                            .permit_number
                            .and_then(|n| i64::try_from(n).ok())
                            .ok_or(BodyError::StateConflict)?;
                        let changed = tx.execute(
                            "UPDATE receipts SET status = ?4 \
                             WHERE tenant_id = ?1 AND idempotency_key = ?2 AND fingerprint = ?3 \
                             AND status IN ('dispatched', 'unknown_reconcile') \
                             AND EXISTS (SELECT 1 FROM receipt_permits p \
                               WHERE p.tenant_id = ?1 AND p.idempotency_key = ?2 \
                               AND p.permit_number = ?5)",
                            (
                                &r.tenant_id,
                                &r.idempotency_key,
                                &r.fingerprint,
                                status.as_str(),
                                number,
                            ),
                        )?;
                        if changed != 1 {
                            return Err(BodyError::StateConflict);
                        }
                        record(r, status.as_str(), "adapter_record")?;
                        match status {
                            ReceiptStatus::ObservedSuccess => out.observed_success += 1,
                            ReceiptStatus::ObservedFailure => out.observed_failure += 1,
                            _ => return Err(BodyError::StateConflict),
                        }
                    }
                    None => {
                        let changed = tx.execute(
                            "UPDATE receipts SET status = 'unknown_reconcile' \
                             WHERE tenant_id = ?1 AND idempotency_key = ?2 AND fingerprint = ?3 \
                             AND status = 'dispatched'",
                            (&r.tenant_id, &r.idempotency_key, &r.fingerprint),
                        )?;
                        if changed == 1 {
                            record(r, "unknown_reconcile", "none")?;
                            out.unknown += 1;
                        }
                    }
                }
            }
            // Fail safe: no `dispatched` receipt survives reconciliation without a decision.
            tx.execute(
                "INSERT INTO receipt_events (tenant_id, idempotency_key, status, cause) \
                 SELECT tenant_id, idempotency_key, 'unknown_reconcile', 'reconcile' \
                 FROM receipts WHERE status = 'dispatched' ORDER BY tenant_id, idempotency_key",
                [],
            )?;
            tx.execute(
                "INSERT INTO reconciliations (tenant_id, idempotency_key, permit_number, \
                 outcome, evidence) SELECT r.tenant_id, r.idempotency_key, p.permit_number, \
                 'unknown_reconcile', 'none' FROM receipts r LEFT JOIN receipt_permits p \
                 ON p.tenant_id = r.tenant_id AND p.idempotency_key = r.idempotency_key \
                 WHERE r.status = 'dispatched' ORDER BY r.tenant_id, r.idempotency_key",
                [],
            )?;
            let rest = tx.execute(
                "UPDATE receipts SET status = 'unknown_reconcile' WHERE status = 'dispatched'",
                [],
            )?;
            out.unknown += u64::try_from(rest).map_err(|_| BodyError::Integrity)?;
            Ok(End::Commit(out))
        })
    }

    fn record_failure(&self, code: &str, record: &FailureRecord) -> Result<(), StoreError> {
        self.write("audit", |tx| {
            tx.execute("INSERT INTO audit_refusals (code) VALUES (?1)", [code])?;
            tx.execute(
                "INSERT INTO failure_records (audit_seq, transaction_name, stage, category, \
                 code, progress, possible_progress, outcome_unknown) \
                 VALUES (last_insert_rowid(), ?1, ?2, ?3, ?4, ?5, ?6, ?7)",
                rusqlite::params![
                    record.transaction(),
                    record.stage(),
                    record.category(),
                    record.code(),
                    record.progress(),
                    record.possible_progress(),
                    record.outcome_unknown(),
                ],
            )
            .map(|_| ())
        })
    }
}

/// The stored floor as a timestamp; an unrepresentable row is a schema error.
fn floor_timestamp((secs, nanos): (i64, i64), category: &str) -> Result<Timestamp, StoreError> {
    u32::try_from(nanos)
        .ok()
        .and_then(|n| timestamp_from_parts(secs, n))
        .ok_or_else(|| unavailable(category, "schema"))
}

#[cfg(test)]
mod grant_tests;

#[cfg(test)]
mod execute_tests;

#[cfg(test)]
mod kill_tests;

#[cfg(test)]
mod d035_tests;

#[cfg(test)]
mod transition_tests;

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;
    use std::cell::Cell;
    use std::rc::Rc;
    use writ_broker::clock::{
        observe, ClockError, ClockEventKind, EffectiveClock, ElapsedSource, HostClock,
        StartOutcome, WallSource,
    };
    use writ_broker::refusal::Refusal;

    fn ts(s: &str) -> Timestamp {
        Timestamp::parse_rfc3339(s).unwrap()
    }

    fn event(kind: ClockEventKind) -> ClockEvent {
        ClockEvent {
            kind,
            effective: None,
            wall: None,
            floor: None,
            operator: None,
        }
    }

    fn clock_event_kinds(l: &SqliteLedger) -> Vec<String> {
        let db = l.db();
        let mut s = db
            .conn
            .prepare("SELECT kind FROM clock_events ORDER BY seq")
            .unwrap();
        s.query_map([], |r| r.get(0))
            .unwrap()
            .collect::<rusqlite::Result<_>>()
            .unwrap()
    }

    const NANOS: i128 = 1_000_000_000;

    /// Injected wall or elapsed source (nanoseconds); `None` fails the read.
    #[derive(Clone)]
    struct Manual(Rc<Cell<Option<i128>>>);

    impl WallSource for Manual {
        fn wall_nanos(&self) -> Result<i128, ClockError> {
            self.0.get().ok_or(ClockError::Source)
        }
    }

    impl ElapsedSource for Manual {
        fn elapsed_nanos(&self) -> Result<i128, ClockError> {
            self.0.get().ok_or(ClockError::Source)
        }
    }

    /// Start a host clock with the wall clock at `wall` (plus `offset` seconds).
    fn host(
        ledger: &mut SqliteLedger,
        wall: &str,
        offset: i64,
    ) -> (HostClock<Manual, Manual>, Manual) {
        let t = ts(wall);
        let w = i128::from(t.unix_seconds() + offset) * NANOS + i128::from(t.subsec_nanos());
        let elapsed = Manual(Rc::new(Cell::new(Some(50 * NANOS))));
        let clock =
            HostClock::start(Manual(Rc::new(Cell::new(Some(w)))), elapsed.clone(), ledger).unwrap();
        (clock, elapsed)
    }

    fn tmp(name: &str) -> PathBuf {
        let d =
            std::env::temp_dir().join(format!("writ-ledger-unit-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&d);
        fs::create_dir_all(&d).unwrap();
        d.join("ledger.db")
    }

    #[test]
    fn stale_owner_is_fenced_on_every_public_state_change() {
        let db = tmp("fence");
        let mut a = SqliteLedger::create(&db).unwrap();
        a.record_refusal("X").unwrap();
        // Simulate a lost lock (unsupported filesystem or manual interference).
        a._lock = None;
        let mut b = SqliteLedger::open(&db).unwrap();
        assert_eq!(b.owner_epoch(), 2);
        // Every public state-changing operation of the store (LedgerStore methods).
        let fenced = Some(StoreError::Fenced { mine: 1, stored: 2 });
        assert_eq!(a.record_refusal("Y").err(), fenced);
        assert_eq!(a.raise_time_floor(ts("2026-11-05T12:00:00Z")).err(), fenced);
        assert_eq!(
            a.record_clock_event(&event(ClockEventKind::Startup)).err(),
            fenced
        );
        b.record_refusal("Z").unwrap();
        assert_eq!(b.refusal_count().unwrap(), 2);
        // Read operations do not change state and are allowed for the stale instance.
        assert_eq!(a.refusal_count().unwrap(), 2);
        assert_eq!(
            a.time_floor().unwrap(),
            None,
            "the fenced raise left nothing"
        );
        assert_eq!(clock_event_kinds(&a), Vec::<String>::new());
    }

    #[test]
    fn settings_and_engine_are_the_d023_ones() {
        let db = tmp("settings");
        let l = SqliteLedger::create(&db).unwrap();
        let conn = l.db();
        let got: (String, i64, i64, i64, i64) = (
            conn.conn
                .query_row("PRAGMA journal_mode", [], |r| r.get(0))
                .unwrap(),
            conn.conn
                .query_row("PRAGMA synchronous", [], |r| r.get(0))
                .unwrap(),
            conn.conn
                .query_row("PRAGMA busy_timeout", [], |r| r.get(0))
                .unwrap(),
            conn.conn
                .query_row("PRAGMA foreign_keys", [], |r| r.get(0))
                .unwrap(),
            conn.conn
                .query_row("PRAGMA trusted_schema", [], |r| r.get(0))
                .unwrap(),
        );
        drop(conn);
        assert_eq!(got, ("wal".to_string(), 2, 5000, 1, 0));
        assert_eq!(l.engine_version().unwrap(), "3.54.1");
        let db = l.db();
        let mut s = db.conn.prepare("PRAGMA compile_options").unwrap();
        let opts: Vec<String> = s
            .query_map([], |r| r.get(0))
            .unwrap()
            .collect::<rusqlite::Result<_>>()
            .unwrap();
        for want in ["OMIT_LOAD_EXTENSION", "DQS=0", "THREADSAFE=1"] {
            assert!(
                opts.iter().any(|o| o == want),
                "{want} missing from {opts:?}"
            );
        }
    }

    fn rows_with_code(l: &SqliteLedger, code: &str) -> i64 {
        l.db()
            .conn
            .query_row(
                "SELECT count(*) FROM audit_refusals WHERE code = ?1",
                [code],
                |r| r.get(0),
            )
            .unwrap()
    }

    /// One injected `audit` fault at `stage`: fail closed, rolled back, ledger usable
    /// and still fenced afterwards.
    fn check_audit_fault(name: &str, stage: fault::Stage, code: std::ffi::c_int, want: &str) {
        let db = tmp(name);
        let mut a = SqliteLedger::create(&db).unwrap();
        a.record_refusal("BEFORE").unwrap();
        a.inject("audit", stage, code);
        assert_eq!(
            a.record_refusal("FAULT").err(),
            Some(StoreError::Unavailable {
                category: "transaction audit".into(),
                code: want.into(),
            }),
            "{stage:?}"
        );
        assert_eq!(StoreError::EXTERNAL_CODE, "LEDGER_UNAVAILABLE");
        // Rolled back: no row, no transaction left open.
        assert_eq!(rows_with_code(&a, "FAULT"), 0, "{stage:?}");
        assert_eq!(a.refusal_count().unwrap(), 1);
        assert!(
            a.db().conn.is_autocommit(),
            "{stage:?}: transaction left open"
        );
        // One-shot: the ledger stays usable.
        a.record_refusal("AFTER").unwrap();
        assert_eq!(a.refusal_count().unwrap(), 2);
        // And stays fenced: a newer owner fences this instance.
        a._lock = None;
        let mut b = SqliteLedger::open(&db).unwrap();
        assert_eq!(
            a.record_refusal("STALE").err(),
            Some(StoreError::Fenced { mine: 1, stored: 2 })
        );
        assert_eq!(
            rows_with_code(&b, "FAULT"),
            0,
            "nothing durable from the fault"
        );
        b.record_refusal("NEW_OWNER").unwrap();
        assert_eq!(b.refusal_count().unwrap(), 3);
    }

    #[test]
    fn injected_fault_at_begin_fails_closed() {
        check_audit_fault(
            "fault-begin",
            fault::Stage::Begin,
            rusqlite::ffi::SQLITE_BUSY,
            "SQLITE_BUSY",
        );
    }

    #[test]
    fn injected_fault_in_body_rolls_back_and_fails_closed() {
        check_audit_fault(
            "fault-body",
            fault::Stage::Body,
            rusqlite::ffi::SQLITE_IOERR,
            "SQLITE_IOERR",
        );
    }

    #[test]
    fn injected_fault_at_commit_rolls_back_and_fails_closed() {
        check_audit_fault(
            "fault-commit",
            fault::Stage::Commit,
            rusqlite::ffi::SQLITE_FULL,
            "SQLITE_FULL",
        );
    }

    #[test]
    fn injected_fault_fires_only_for_its_named_transaction() {
        let db = tmp("fault-name");
        let mut l = SqliteLedger::create(&db).unwrap();
        l.inject(
            "dispatch",
            fault::Stage::Commit,
            rusqlite::ffi::SQLITE_IOERR,
        );
        l.record_refusal("X").unwrap();
        assert_eq!(l.refusal_count().unwrap(), 1);
        // Still armed for its own transaction name; the audit transaction never takes it.
        assert!(l
            .db()
            .fault
            .as_ref()
            .is_some_and(|f| f.transaction == "dispatch"));
        assert_eq!(
            l.db().take_fault("dispatch").map(|f| f.stage),
            Some(fault::Stage::Commit)
        );
        assert!(l.db().fault.is_none());
    }

    #[test]
    fn new_ledger_is_writ_ledger_5_with_an_identity_and_without_a_floor() {
        let db = tmp("v4");
        let l = SqliteLedger::create(&db).unwrap();
        assert_eq!(
            meta_value(&l.db().conn, "schema_version")
                .unwrap()
                .as_deref(),
            Some("writ-ledger-5")
        );
        let id = meta_value(&l.db().conn, "ledger_id").unwrap().unwrap();
        assert_eq!(LedgerId::parse(&id).as_ref(), Some(l.ledger_id()));
        let other = SqliteLedger::create(&tmp("v4-other")).unwrap();
        assert_ne!(other.ledger_id(), l.ledger_id(), "random per ledger");
        assert_eq!(l.time_floor().unwrap(), None);
        assert_eq!(clock_event_kinds(&l), Vec::<String>::new());
    }

    #[test]
    fn time_floor_is_raised_never_lowered_and_survives_restart() {
        let db = tmp("floor");
        let mut a = SqliteLedger::create(&db).unwrap();
        let t = ts("2026-11-05T12:00:00.25Z");
        assert_eq!(a.raise_time_floor(t).unwrap(), t);
        for lower in [
            "2026-11-05T12:00:00.249999999Z",
            "2026-11-05T11:00:00Z",
            "1970-01-01T00:00:00Z",
            "0000-01-01T00:00:00Z",
        ] {
            assert_eq!(a.raise_time_floor(ts(lower)).unwrap(), t, "{lower}");
            assert_eq!(a.time_floor().unwrap(), Some(t), "{lower}");
        }
        assert_eq!(a.raise_time_floor(t).unwrap(), t, "equal is a no-op");
        let later = ts("2026-11-05T12:00:00.250000001Z");
        assert_eq!(a.raise_time_floor(later).unwrap(), later);
        drop(a);
        let mut b = SqliteLedger::open(&db).unwrap();
        assert_eq!(
            b.time_floor().unwrap(),
            Some(later),
            "durable across restart"
        );
        assert_eq!(b.raise_time_floor(t).unwrap(), later);
        let rows: i64 = b
            .db()
            .conn
            .query_row("SELECT count(*) FROM time_floor", [], |r| r.get(0))
            .unwrap();
        assert_eq!(rows, 1);
    }

    #[test]
    fn unrepresentable_stored_floor_fails_closed() {
        let db = tmp("floor-range");
        let l = SqliteLedger::create(&db).unwrap();
        l.db()
            .conn
            .execute(
                "INSERT INTO time_floor (id, floor_s, floor_ns) VALUES (1, ?1, 0)",
                [i64::MAX],
            )
            .unwrap();
        assert_eq!(
            l.time_floor().err(),
            Some(StoreError::Unavailable {
                category: "read".into(),
                code: "schema".into(),
            })
        );
        assert!(l
            .db()
            .conn
            .execute("UPDATE time_floor SET floor_ns = 1000000000", [])
            .is_err());
        assert!(l
            .db()
            .conn
            .execute(
                "INSERT INTO time_floor (id, floor_s, floor_ns) VALUES (2, 0, 0)",
                []
            )
            .is_err());
    }

    #[test]
    fn clock_events_are_recorded_in_the_ledger() {
        let db = tmp("events");
        let mut l = SqliteLedger::create(&db).unwrap();
        l.record_clock_event(&ClockEvent {
            kind: ClockEventKind::Acknowledged,
            effective: None,
            wall: Some(ts("2026-11-04T12:00:00Z")),
            floor: Some(ts("2026-11-05T12:00:00.5Z")),
            operator: Some("operator:admin".into()),
        })
        .unwrap();
        let row: (
            String,
            Option<String>,
            Option<String>,
            Option<String>,
            Option<String>,
        ) = l
            .db()
            .conn
            .query_row(
                "SELECT kind, effective, wall, floor, operator FROM clock_events",
                [],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)),
            )
            .unwrap();
        assert_eq!(
            row,
            (
                "acknowledged".into(),
                None,
                Some("2026-11-04T12:00:00Z".into()),
                Some("2026-11-05T12:00:00.5Z".into()),
                Some("operator:admin".into()),
            )
        );
        // Every kind the broker can record is accepted by the schema.
        for kind in [
            ClockEventKind::Startup,
            ClockEventKind::StartupRollbackAbsorbed,
            ClockEventKind::StartupRollbackBlocked,
            ClockEventKind::RollbackDetected,
            ClockEventKind::ForwardJumpDetected,
            ClockEventKind::DriftNormal,
            ClockEventKind::Acknowledged,
            ClockEventKind::AcknowledgementUnauthenticated,
            ClockEventKind::AcknowledgementUnauthorized,
            ClockEventKind::AcknowledgementStale,
        ] {
            l.record_clock_event(&event(kind)).unwrap();
        }
        assert_eq!(clock_event_kinds(&l).len(), 11);
        assert!(l
            .db()
            .conn
            .execute("INSERT INTO clock_events (kind) VALUES ('reset')", [])
            .is_err());
    }

    /// An expiry decision persists its time in its own `time_floor` transaction before
    /// the governed operation's result is final. The governed operation (here its
    /// refusal audit) then fails by injection at each stage; the floor stays, survives
    /// a restart, and the expired item stays expired after a clock rollback.
    #[test]
    fn expiry_observation_persists_despite_injected_transaction_failure_and_restart() {
        let expires = ts("2026-11-05T12:00:30Z");
        for (i, stage) in [
            fault::Stage::Begin,
            fault::Stage::Body,
            fault::Stage::Commit,
        ]
        .into_iter()
        .enumerate()
        {
            let db = tmp(&format!("expiry-{i}"));
            let mut l = SqliteLedger::create(&db).unwrap();
            let (clock, elapsed) = host(&mut l, "2026-11-05T12:00:00Z", 0);
            elapsed.0.set(Some(80 * NANOS));
            let at = observe(&clock, &mut l).unwrap().at();
            assert!(at >= expires, "expired at {at}");
            l.inject("audit", stage, rusqlite::ffi::SQLITE_IOERR);
            assert!(l.record_refusal("PERMIT_EXPIRED").is_err(), "{stage:?}");
            assert_eq!(l.refusal_count().unwrap(), 0, "{stage:?}: rolled back");
            assert_eq!(l.time_floor().unwrap(), Some(expires), "{stage:?}");
            drop(clock);
            drop(l);
            // Restart with the wall clock 100 s behind the floor: absorbed, still expired.
            let mut l = SqliteLedger::open(&db).unwrap();
            assert_eq!(l.time_floor().unwrap(), Some(expires));
            let (clock, _) = host(&mut l, "2026-11-05T12:00:30Z", -100);
            assert_eq!(clock.outcome(), StartOutcome::AbsorbedRollback);
            assert!(observe(&clock, &mut l).unwrap().at() >= expires);
            drop(clock);
            drop(l);
            // Restart with the wall clock 121 s behind the floor: fail closed.
            let mut l = SqliteLedger::open(&db).unwrap();
            let (clock, _) = host(&mut l, "2026-11-05T12:00:30Z", -121);
            assert_eq!(clock.outcome(), StartOutcome::Blocked);
            let err = observe(&clock, &mut l).unwrap_err();
            assert_eq!(err.refusal(), Refusal::TimeUnavailable);
            assert_eq!(l.time_floor().unwrap(), Some(expires), "never lowered");
            assert_eq!(
                clock_event_kinds(&l),
                vec![
                    "startup",
                    "startup_rollback_absorbed",
                    "startup_rollback_blocked"
                ]
            );
        }
    }

    #[test]
    fn injected_floor_failure_gives_time_unavailable_and_keeps_the_floor() {
        for (i, stage) in [
            fault::Stage::Begin,
            fault::Stage::Body,
            fault::Stage::Commit,
        ]
        .into_iter()
        .enumerate()
        {
            let db = tmp(&format!("floor-fault-{i}"));
            let mut l = SqliteLedger::create(&db).unwrap();
            let (clock, elapsed) = host(&mut l, "2026-11-05T12:00:00Z", 0);
            elapsed.0.set(Some(60 * NANOS));
            l.inject("time_floor", stage, rusqlite::ffi::SQLITE_FULL);
            let err = observe(&clock, &mut l).unwrap_err();
            assert_eq!(
                err,
                ClockError::Floor(StoreError::Unavailable {
                    category: "transaction time_floor".into(),
                    code: "SQLITE_FULL".into(),
                }),
                "{stage:?}"
            );
            assert_eq!(err.refusal(), Refusal::TimeUnavailable);
            assert_eq!(err.refusal().external_code(), "PROMOTION_REFUSED");
            assert_eq!(
                l.time_floor().unwrap(),
                Some(ts("2026-11-05T12:00:00Z")),
                "{stage:?}: rolled back to the previous floor"
            );
            assert!(l.db().conn.is_autocommit());
            assert_eq!(
                observe(&clock, &mut l).unwrap().at(),
                ts("2026-11-05T12:00:10Z")
            );
            assert!(clock.read().is_ok());
        }
    }
}
