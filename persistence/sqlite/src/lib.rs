//! Writ V2 local ledger profile on SQLite (M3-01).
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
//! (`File::try_lock`) on `<canonical path>.owner`, held for the lifetime of the
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
//! fn f(l: &writ_ledger_sqlite::SqliteLedger) { let _ = &l.conn; }
//! ```
//!
//! Engine and settings follow D-023 (SQLite 3.54.1, WAL, `synchronous=FULL`,
//! `busy_timeout=5000`, `foreign_keys=ON`, `trusted_schema=OFF`).

use std::collections::BTreeSet;
use std::fs::{self, File, OpenOptions, TryLockError};
use std::io::ErrorKind;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};

use rusqlite::{Connection, ErrorCode, OpenFlags, Transaction, TransactionBehavior};
pub use writ_broker::store::{LedgerStore, StoreError};

/// The production schema, applied once to a newly created ledger inside one transaction.
pub const SCHEMA: &str = include_str!("schema.sql");
/// The schema version this crate writes and accepts.
pub const SCHEMA_VERSION: &str = "writ-ledger-1";

/// A SQLite ledger owned by this process.
pub struct SqliteLedger {
    // Field order matters (no custom Drop): fields drop in declaration order, so the
    // connection closes before the ownership lock is released.
    conn: Connection,
    epoch: u64,
    _lock: Option<File>,
    #[cfg(test)]
    fault: Option<fault::Fault>,
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
/// engine error. A failed transaction is rolled back when it drops. Partial-progress
/// accounting across several transactions of one operation is demonstrated when M3-04
/// adds the multi-transaction lifecycle.
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
    }

    /// The code to raise when `armed` fails at `stage`.
    pub fn injected(armed: &Option<Fault>, stage: Stage) -> Option<std::ffi::c_int> {
        armed.as_ref().filter(|f| f.stage == stage).map(|f| f.code)
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

fn take_ownership(canonical: &Path) -> Result<File, StoreError> {
    let lock = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(sidecar(canonical, ".owner"))
        .map_err(|_| unavailable("ownership", "io"))?;
    match lock.try_lock() {
        Ok(()) => Ok(lock),
        Err(TryLockError::WouldBlock) => Err(StoreError::OwnedElsewhere),
        Err(TryLockError::Error(_)) => Err(unavailable("ownership", "io")),
    }
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

/// The exact schema objects (type, name, table, definition) a valid ledger contains.
fn expected_objects() -> rusqlite::Result<BTreeSet<SchemaObject>> {
    let mem = Connection::open_in_memory()?;
    mem.execute_batch(SCHEMA)?;
    schema_objects(&mem)
}

fn meta_value(conn: &Connection, key: &str) -> rusqlite::Result<Option<String>> {
    match conn.query_row("SELECT value FROM meta WHERE key = ?1", [key], |r| r.get(0)) {
        Ok(v) => Ok(Some(v)),
        Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
        Err(e) => Err(e),
    }
}

/// Read-only verification of an existing ledger. Any failure is `initialize schema`
/// (or the SQLite code) and nothing in the file is changed.
fn verify(conn: &Connection) -> Result<(), StoreError> {
    let schema = |_| unavailable("initialize", "schema");
    let sql = |e: rusqlite::Error| unavailable("initialize", code_name(&e));
    let found = schema_objects(conn).map_err(sql)?;
    let expected = expected_objects().map_err(sql)?;
    if found != expected {
        return Err(unavailable("initialize", "schema"));
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
    if version.as_deref() != Some(SCHEMA_VERSION)
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

fn finish(mut conn: Connection, lock: File) -> Result<SqliteLedger, StoreError> {
    conn.execute_batch("PRAGMA synchronous = FULL;")
        .map_err(|e| unavailable("initialize", code_name(&e)))?;
    let epoch = bump_epoch(&mut conn)?;
    Ok(SqliteLedger {
        conn,
        epoch,
        _lock: Some(lock),
        #[cfg(test)]
        fault: None,
    })
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
        verify(&conn)?;
        finish(conn, lock)
    }

    /// Run a typed state change in one fenced `BEGIN IMMEDIATE` transaction named `name`.
    /// Crate-private: public callers only reach typed operations.
    fn write<T>(
        &mut self,
        name: &str,
        body: impl FnOnce(&Transaction<'_>) -> rusqlite::Result<T>,
    ) -> Result<T, StoreError> {
        let category = format!("transaction {name}");
        #[cfg(test)]
        let armed = self.take_fault(name);
        #[cfg(test)]
        if let Some(code) = fault::injected(&armed, fault::Stage::Begin) {
            return Err(unavailable(&category, code_name(&fault::error(code))));
        }
        let tx = self
            .conn
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|e| unavailable(&category, code_name(&e)))?;
        let stored = meta_value(&tx, "owner_epoch")
            .map_err(|e| unavailable(&category, code_name(&e)))?
            .and_then(|e| e.parse::<u64>().ok())
            .ok_or_else(|| unavailable(&category, "schema"))?;
        if stored != self.epoch {
            return Err(StoreError::Fenced {
                mine: self.epoch,
                stored,
            });
        }
        let out = body(&tx).map_err(|e| unavailable(&category, code_name(&e)))?;
        #[cfg(test)]
        for stage in [fault::Stage::Body, fault::Stage::Commit] {
            if let Some(code) = fault::injected(&armed, stage) {
                // `tx` drops here without committing: rolled back.
                return Err(unavailable(&category, code_name(&fault::error(code))));
            }
        }
        tx.commit()
            .map_err(|e| unavailable(&category, code_name(&e)))?;
        Ok(out)
    }

    /// Arm a one-shot fault (test only).
    #[cfg(test)]
    fn inject(&mut self, transaction: &str, stage: fault::Stage, code: std::ffi::c_int) {
        self.fault = Some(fault::Fault {
            transaction: transaction.to_string(),
            stage,
            code,
        });
    }

    /// Disarm and return the fault armed for transaction `name`, if any (test only).
    #[cfg(test)]
    fn take_fault(&mut self, name: &str) -> Option<fault::Fault> {
        match &self.fault {
            Some(f) if f.transaction == name => self.fault.take(),
            _ => None,
        }
    }

    /// Number of recorded refusals (`final.refusals`).
    pub fn refusal_count(&self) -> Result<u64, StoreError> {
        let n: i64 = self
            .conn
            .query_row("SELECT count(*) FROM audit_refusals", [], |r| r.get(0))
            .map_err(|e| unavailable("read", code_name(&e)))?;
        u64::try_from(n).map_err(|_| unavailable("read", "schema"))
    }

    /// The linked SQLite engine version (D-023 check).
    pub fn engine_version(&self) -> Result<String, StoreError> {
        self.conn
            .query_row("SELECT sqlite_version()", [], |r| r.get(0))
            .map_err(|e| unavailable("read", code_name(&e)))
    }
}

impl LedgerStore for SqliteLedger {
    fn owner_epoch(&self) -> u64 {
        self.epoch
    }

    fn record_refusal(&mut self, code: &str) -> Result<(), StoreError> {
        self.write("audit", |tx| {
            tx.execute("INSERT INTO audit_refusals (code) VALUES (?1)", [code])
                .map(|_| ())
        })
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;

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
        assert_eq!(
            a.record_refusal("Y").err(),
            Some(StoreError::Fenced { mine: 1, stored: 2 })
        );
        b.record_refusal("Z").unwrap();
        assert_eq!(b.refusal_count().unwrap(), 2);
        // Read operations do not change state and are allowed for the stale instance.
        assert_eq!(a.refusal_count().unwrap(), 2);
    }

    #[test]
    fn settings_and_engine_are_the_d023_ones() {
        let db = tmp("settings");
        let l = SqliteLedger::create(&db).unwrap();
        let got: (String, i64, i64, i64, i64) = (
            l.conn
                .query_row("PRAGMA journal_mode", [], |r| r.get(0))
                .unwrap(),
            l.conn
                .query_row("PRAGMA synchronous", [], |r| r.get(0))
                .unwrap(),
            l.conn
                .query_row("PRAGMA busy_timeout", [], |r| r.get(0))
                .unwrap(),
            l.conn
                .query_row("PRAGMA foreign_keys", [], |r| r.get(0))
                .unwrap(),
            l.conn
                .query_row("PRAGMA trusted_schema", [], |r| r.get(0))
                .unwrap(),
        );
        assert_eq!(got, ("wal".to_string(), 2, 5000, 1, 0));
        assert_eq!(l.engine_version().unwrap(), "3.54.1");
        let mut s = l.conn.prepare("PRAGMA compile_options").unwrap();
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
        l.conn
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
        assert!(a.conn.is_autocommit(), "{stage:?}: transaction left open");
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
            .fault
            .as_ref()
            .is_some_and(|f| f.transaction == "dispatch"));
        assert_eq!(
            l.take_fault("dispatch").map(|f| f.stage),
            Some(fault::Stage::Commit)
        );
        assert!(l.fault.is_none());
    }
}
