//! SQLite broker ledger (`--ledger sqlite`, m1/fixtures/broker/LEDGER-SQLITE.md,
//! D-023): connection settings, schema creation and one function per logical
//! transaction. Each write transaction is `BEGIN IMMEDIATE`; dropping a
//! `Transaction` without `commit` rolls it back.
//!
//! SIMULATED, not verified: the effect adapter's consumed permits and effect
//! counter live in the same file (no external system); power-loss durability
//! is not tested.

use crate::broker::{refused, replayed, result};
use crate::json::{obj, s, Value};
use crate::time::Instant;
use crate::timing::{since, StepTiming, TxAt, TxError, TxTime};
use rusqlite::{Connection, DropBehavior, OpenFlags, OptionalExtension, Transaction, TransactionBehavior};
use std::fs::OpenOptions;
use std::io::ErrorKind;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

pub const SCHEMA: &str = include_str!("../../fixtures/broker/ledger.sql");
pub const DRIVER: &str = "rusqlite 0.40.2";

/// Where a ledger error happened (LEDGER-SQLITE.md "Storage failures"
/// rule 4): the only context a diagnostic carries.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Category {
    Open,
    Initialize,
    Transaction(&'static str),
    Final,
    /// A `concurrent` worker's connection setup (D-027).
    Worker,
}

/// A ledger error: its category (set where it is first known) and an SQLite
/// primary result code name or `io`. It never holds a path, file name,
/// SQLite or OS message text, credential or key (rule 4, D-025).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DbError {
    pub category: Option<Category>,
    pub code: &'static str,
}

impl DbError {
    pub fn new(code: &'static str) -> DbError {
        DbError { category: None, code }
    }

    pub fn io() -> DbError {
        DbError::new("io")
    }

    /// Set the category unless an inner step already set one.
    pub fn within(mut self, category: Category) -> DbError {
        self.category.get_or_insert(category);
        self
    }

    /// The sanitized stderr diagnostic.
    pub fn diagnostic(&self) -> String {
        let category = match self.category {
            Some(Category::Open) | None => "open".to_string(),
            Some(Category::Initialize) => "initialize".to_string(),
            Some(Category::Transaction(name)) => format!("transaction {name}"),
            Some(Category::Final) => "final".to_string(),
            Some(Category::Worker) => "worker".to_string(),
        };
        format!("{category}: {}", self.code)
    }
}

/// The name of an SQLite primary result code.
pub fn primary_code_name(code: i32) -> &'static str {
    match code & 0xff {
        1 => "SQLITE_ERROR",
        2 => "SQLITE_INTERNAL",
        3 => "SQLITE_PERM",
        4 => "SQLITE_ABORT",
        5 => "SQLITE_BUSY",
        6 => "SQLITE_LOCKED",
        7 => "SQLITE_NOMEM",
        8 => "SQLITE_READONLY",
        9 => "SQLITE_INTERRUPT",
        10 => "SQLITE_IOERR",
        11 => "SQLITE_CORRUPT",
        12 => "SQLITE_NOTFOUND",
        13 => "SQLITE_FULL",
        14 => "SQLITE_CANTOPEN",
        15 => "SQLITE_PROTOCOL",
        16 => "SQLITE_EMPTY",
        17 => "SQLITE_SCHEMA",
        18 => "SQLITE_TOOBIG",
        19 => "SQLITE_CONSTRAINT",
        20 => "SQLITE_MISMATCH",
        21 => "SQLITE_MISUSE",
        22 => "SQLITE_NOLFS",
        23 => "SQLITE_AUTH",
        24 => "SQLITE_FORMAT",
        25 => "SQLITE_RANGE",
        26 => "SQLITE_NOTADB",
        27 => "SQLITE_NOTICE",
        28 => "SQLITE_WARNING",
        _ => "SQLITE_ERROR",
    }
}

impl From<rusqlite::Error> for DbError {
    fn from(e: rusqlite::Error) -> Self {
        DbError::new(match &e {
            rusqlite::Error::SqliteFailure(f, _) => primary_code_name(f.extended_code),
            rusqlite::Error::QueryReturnedNoRows => "SQLITE_NOTFOUND",
            _ => "SQLITE_MISMATCH",
        })
    }
}

/// A ledger that is not this schema (verification failure).
const NOT_LEDGER: &str = "SQLITE_NOTADB";
/// Stored content that violates the ledger's own invariants (C-4).
const CORRUPT: &str = "SQLITE_CORRUPT";

type R<T> = Result<T, DbError>;
type Key = (String, String);

/// Run a statement that may return rows (most pragmas) and discard them.
fn pragma(c: &Connection, sql: &str) -> R<()> {
    let mut st = c.prepare(sql)?;
    let mut rows = st.query([])?;
    while rows.next()?.is_some() {}
    Ok(())
}

/// Connection-only settings (not persisted in the file), in the specified
/// order.
fn connection_settings(c: &Connection) -> R<()> {
    for sql in ["PRAGMA busy_timeout = 5000", "PRAGMA foreign_keys = ON", "PRAGMA trusted_schema = OFF"] {
        pragma(c, sql)?;
    }
    Ok(())
}

/// Persistent settings: `journal_mode=WAL` (must read back `wal`), then
/// `synchronous=FULL`. Applied only to a verified ledger (S-1).
fn persistent_settings(c: &Connection) -> R<()> {
    let mode: String = c.query_row("PRAGMA journal_mode = WAL", [], |r| r.get(0))?;
    if mode != "wal" {
        return Err(DbError::new("SQLITE_ERROR"));
    }
    pragma(c, "PRAGMA synchronous = FULL")
}

/// A connection to an already initialized ledger (after `restart_broker`, or
/// for a `concurrent` worker): connection-only settings, then the persistent
/// ones (already in effect for the file). Initialization is not re-run.
pub fn open(path: &Path) -> R<Connection> {
    let c = Connection::open(path).map_err(|e| DbError::from(e).within(Category::Open))?;
    connection_settings(&c).map_err(|e| e.within(Category::Open))?;
    persistent_settings(&c).map_err(|e| e.within(Category::Open))?;
    Ok(c)
}

/// Connection settings in the order of LEDGER-SQLITE.md "Connection
/// settings", as a `concurrent` worker applies them (D-027).
const WORKER_SETTINGS: [&str; 5] = [
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = FULL",
    "PRAGMA busy_timeout = 5000",
    "PRAGMA foreign_keys = ON",
    "PRAGMA trusted_schema = OFF",
];

/// Worker connection setup (LEDGER-SQLITE.md "Concurrency profiles", D-027):
/// open the existing ledger file (never created here), apply the connection
/// settings in their order, then verify them by reading them back. No
/// transaction is begun and nothing is written. Errors are categorized
/// `worker`.
pub fn open_worker(path: &Path) -> R<Connection> {
    let setup = || -> R<Connection> {
        let c = Connection::open_with_flags(path, OpenFlags::default() - OpenFlags::SQLITE_OPEN_CREATE)?;
        for sql in WORKER_SETTINGS {
            pragma(&c, sql)?;
        }
        verify_settings(&c)?;
        Ok(c)
    };
    setup().map_err(|e| e.within(Category::Worker))
}

/// Read back `journal_mode` = `wal`, `synchronous` = 2, `busy_timeout` =
/// 5000, `foreign_keys` = 1, `trusted_schema` = 0; a difference is
/// `SQLITE_ERROR`.
pub fn verify_settings(c: &Connection) -> R<()> {
    let mode: String = c.query_row("PRAGMA journal_mode", [], |r| r.get(0))?;
    let int = |sql: &str| -> R<i64> { Ok(c.query_row(sql, [], |r| r.get(0))?) };
    let expected = [("PRAGMA synchronous", 2), ("PRAGMA busy_timeout", 5000), ("PRAGMA foreign_keys", 1), ("PRAGMA trusted_schema", 0)];
    if mode != "wal" {
        return Err(DbError::new("SQLITE_ERROR"));
    }
    for (sql, value) in expected {
        if int(sql)? != value {
            return Err(DbError::new("SQLITE_ERROR"));
        }
    }
    Ok(())
}

/// Test-only order evidence for D-027 (Required tests item 9): events of
/// one ledger file, in the order they happened across threads. Compiled out
/// of the binary.
#[cfg(test)]
pub mod trace {
    use std::path::Path;
    use std::sync::Mutex;

    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    pub enum Event {
        /// A worker starts its connection setup.
        SetupStart,
        /// A worker's connection setup finished (successfully or not).
        SetupEnd,
        /// A worker reaches the start barrier.
        Arrive,
        /// A worker passed the start barrier.
        Pass,
        /// `BEGIN IMMEDIATE` is about to be issued.
        Begin,
    }

    static LOG: Mutex<Vec<(String, Event)>> = Mutex::new(Vec::new());

    fn name(path: &Path) -> String {
        path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default()
    }

    pub fn record(path: &Path, e: Event) {
        LOG.lock().unwrap_or_else(|e| e.into_inner()).push((name(path), e));
    }

    /// Events recorded so far for the ledger file `path`.
    pub fn events(path: &Path) -> Vec<Event> {
        let n = name(path);
        LOG.lock().unwrap_or_else(|e| e.into_inner()).iter().filter(|(f, _)| *f == n).map(|(_, e)| *e).collect()
    }
}

fn begin(c: &Connection) -> R<Transaction<'_>> {
    #[cfg(test)]
    if let Some(p) = c.path() {
        trace::record(Path::new(p), trace::Event::Begin);
    }
    Ok(Transaction::new_unchecked(c, TransactionBehavior::Immediate)?)
}

/// How a logical transaction ends: `COMMIT`, or `ROLLBACK` (by dropping the
/// `Transaction`, as before timing was added).
enum End<T> {
    Commit(T),
    Rollback(T),
}

/// One logical transaction named `name`, timed into `t` (LEDGER-SQLITE.md
/// "Per-operation timing"): `begin_ns` brackets `BEGIN IMMEDIATE`, `body_ns`
/// the body, `end_ns` `COMMIT` or `ROLLBACK`. A body error rolls back and is
/// returned; a failed `BEGIN` is recorded with a zero body and end. Every
/// error is categorized as `transaction <name>` and recorded as the
/// transaction's `error` with where it happened (D-026).
fn transact<T>(c: &Connection, t: &mut StepTiming, name: &'static str, body: impl FnOnce(&Transaction) -> R<End<T>>) -> R<T> {
    let error = |e: &DbError, at: TxAt| Some(TxError { busy: e.code == "SQLITE_BUSY", at });
    let started = std::time::Instant::now();
    let tx = begin(c);
    let begin_ns = Some(since(started));
    let tx = match tx {
        Ok(tx) => tx,
        Err(e) => {
            t.tx.push((name, TxTime { begin_ns, body_ns: 0, end_ns: Some(0), commit: false, error: error(&e, TxAt::Begin) }));
            return Err(e.within(Category::Transaction(name)));
        }
    };
    let started = std::time::Instant::now();
    let out = body(&tx);
    let body_ns = since(started);
    let started = std::time::Instant::now();
    let (out, commit, err) = match out {
        Ok(End::Commit(v)) => match tx.commit() {
            Ok(()) => (Ok(v), true, None),
            Err(e) => {
                let e = DbError::from(e);
                (Err(e), false, error(&e, TxAt::End))
            }
        },
        Ok(End::Rollback(v)) => {
            let failed = rollback(c, tx);
            (Ok(v), false, failed.and_then(|e| error(&e, TxAt::End)))
        }
        Err(e) => {
            rollback(c, tx);
            (Err(e), false, error(&e, TxAt::Body))
        }
    };
    t.tx.push((name, TxTime { begin_ns, body_ns, end_ns: Some(since(started)), commit, error: err }));
    out.map_err(|e| e.within(Category::Transaction(name)))
}

/// `ROLLBACK` exactly as dropping the `Transaction` does it (only when a
/// transaction is still active), but returning its error, if any, so that it
/// can be observed (D-026). The result of the operation is not changed.
fn rollback(c: &Connection, mut tx: Transaction) -> Option<DbError> {
    if c.is_autocommit() {
        drop(tx);
        return None;
    }
    tx.set_drop_behavior(DropBehavior::Ignore);
    tx.rollback().err().map(DbError::from)
}

/// The tables ledger.sql creates, in schema order.
pub fn schema_tables() -> Vec<&'static str> {
    SCHEMA.lines().filter_map(|l| l.strip_prefix("CREATE TABLE ")).filter_map(|r| r.split_whitespace().next()).collect()
}

/// Initialization transaction (LEDGER-SQLITE.md "Storage failures" rule 1):
/// one `BEGIN IMMEDIATE` transaction that creates the schema in a new, empty
/// database, or verifies `meta.schema_version = '1'` and that every table of
/// ledger.sql exists (extra objects are not checked). Any error rolls back,
/// so an existing file is not modified; a read-only connection is refused
/// before the transaction.
fn initialize(c: &Connection) -> R<()> {
    if c.is_readonly("main")? {
        return Err(DbError::new("SQLITE_READONLY"));
    }
    let tx = begin(c)?;
    let objects: i64 = tx.query_row("SELECT count(*) FROM sqlite_schema", [], |r| r.get(0))?;
    if objects == 0 {
        tx.execute_batch(SCHEMA)?;
    } else {
        let has_meta: bool = tx.query_row("SELECT EXISTS (SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'meta')", [], |r| r.get(0))?;
        if !has_meta {
            return Err(DbError::new(NOT_LEDGER));
        }
        let version: Option<rusqlite::types::Value> = tx.query_row("SELECT value FROM meta WHERE key = 'schema_version'", [], |r| r.get(0)).optional()?;
        if version != Some(rusqlite::types::Value::Text("1".into())) {
            return Err(DbError::new(NOT_LEDGER));
        }
        for table in schema_tables() {
            let present: bool = tx.query_row("SELECT EXISTS (SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?1)", [table], |r| r.get(0))?;
            if !present {
                return Err(DbError::new(NOT_LEDGER));
            }
        }
    }
    tx.commit()?;
    Ok(())
}

const SIDECARS: [&str; 3] = ["-journal", "-wal", "-shm"];

fn sidecar(path: &Path, suffix: &str) -> PathBuf {
    let mut name = path.as_os_str().to_os_string();
    name.push(suffix);
    PathBuf::from(name)
}

/// What this run created (exclusive create), so that a failed
/// initialization removes exactly that (S-2): the database file (identified
/// by device and inode) and the sidecar names that did not exist before.
struct Created {
    file: (u64, u64),
    absent_sidecars: Vec<PathBuf>,
}

impl Created {
    /// Remove the created file if it is still the one created, and every
    /// sidecar that did not exist before and is now a regular file. Nothing
    /// else is touched; errors are ignored (the scenario already fails).
    fn remove(&self, path: &Path) {
        use std::os::unix::fs::MetadataExt;
        if std::fs::symlink_metadata(path).is_ok_and(|m| m.is_file() && (m.dev(), m.ino()) == self.file) {
            let _ = std::fs::remove_file(path);
        }
        for p in &self.absent_sidecars {
            if std::fs::symlink_metadata(p).is_ok_and(|m| m.is_file()) {
                let _ = std::fs::remove_file(p);
            }
        }
    }
}

/// Create `path` exclusively. `Ok(Some)`: this run created it; `Ok(None)`:
/// it already existed; `Err`: it could not be created (an open failure).
fn create_exclusive(path: &Path) -> std::io::Result<Option<Created>> {
    use std::os::unix::fs::MetadataExt;
    let absent_sidecars = SIDECARS.iter().map(|x| sidecar(path, x)).filter(|p| std::fs::symlink_metadata(p).is_err()).collect();
    match OpenOptions::new().write(true).create_new(true).open(path) {
        Ok(f) => {
            let m = f.metadata()?;
            Ok(Some(Created { file: (m.dev(), m.ino()), absent_sidecars }))
        }
        Err(e) if e.kind() == ErrorKind::AlreadyExists => Ok(None),
        Err(e) => Err(e),
    }
}

/// Where a scenario's ledger lives (LEDGER-SQLITE.md "Selecting the ledger").
pub enum Target<'a> {
    /// A new file `<pid>-<sequence>.db` in this directory.
    Dir(&'a Path),
    /// This file, created if absent, never reset.
    Db(&'a Path),
}

static SEQUENCE: AtomicU64 = AtomicU64::new(0);

/// Open and initialize the scenario's ledger before any step (rule 1, S-1,
/// S-2): open or create the file; connection-only settings; the
/// initialization transaction; only then the persistent settings. A file
/// this run created is removed again if any of this fails.
pub fn open_ledger(target: Target) -> R<(PathBuf, Connection)> {
    let (path, created) = match target {
        Target::Dir(dir) => loop {
            let path = dir.join(format!("{}-{}.db", std::process::id(), SEQUENCE.fetch_add(1, Ordering::Relaxed)));
            match create_exclusive(&path) {
                Ok(Some(c)) => break (path, Some(c)),
                Ok(None) => continue,
                Err(_) => return Err(DbError::io().within(Category::Open)),
            }
        },
        Target::Db(path) => match create_exclusive(path) {
            Ok(created) => (path.to_path_buf(), created),
            Err(_) => return Err(DbError::io().within(Category::Open)),
        },
    };
    match initialize_at(&path, created.is_none()) {
        Ok(c) => Ok((path, c)),
        Err(e) => {
            if let Some(created) = &created {
                created.remove(&path);
            }
            Err(e)
        }
    }
}

/// The connection is dropped (closed) before any cleanup on failure.
fn initialize_at(path: &Path, existing: bool) -> R<Connection> {
    // SQLite opens a write-protected file read-only: check write access of
    // an existing file first (opening it for read and write changes nothing).
    if existing && OpenOptions::new().read(true).write(true).open(path).is_err() {
        return Err(DbError::io().within(Category::Open));
    }
    let c = Connection::open(path).map_err(|e| DbError::from(e).within(Category::Open))?;
    connection_settings(&c).map_err(|e| e.within(Category::Open))?;
    initialize(&c).map_err(|e| e.within(Category::Initialize))?;
    persistent_settings(&c).map_err(|e| e.within(Category::Initialize))?;
    Ok(c)
}

/// The members of a stored grant binding, all strings except
/// `requested_capabilities` (an array of strings).
const BINDING_MEMBERS: [&str; 9] = [
    "action_digest",
    "action_kind",
    "approval_request_id",
    "canonical_resource_id",
    "policy_bundle_sha",
    "principal_id",
    "requested_capabilities",
    "tenant_id",
    "verdict",
];

/// Stored-grant integrity (LEDGER-SQLITE.md "Storage failures" rule 5, C-4):
/// strict JSON (D-017), exactly its own D-006 canonical text, an object with
/// exactly the binding members and their types. `false` is a ledger-integrity
/// failure, never a mismatch.
fn binding_intact(text: &str) -> bool {
    let Ok(v) = crate::json::parse(text.as_bytes()) else { return false };
    if crate::json::canonical(&v).ok().as_deref() != Some(text) {
        return false;
    }
    let Value::Obj(members) = &v else { return false };
    members.len() == BINDING_MEMBERS.len()
        && BINDING_MEMBERS.iter().all(|name| match v.get(name) {
            Some(Value::Arr(items)) if *name == "requested_capabilities" => items.iter().all(|x| matches!(x, Value::Str(_))),
            Some(Value::Str(_)) => *name != "requested_capabilities",
            _ => false,
        })
}

/// Grant row as read in T-reserve.
struct GrantRow {
    binding: String,
    expires: Instant,
    max_uses: i64,
    uses: i64,
}

/// T-reserve. `None`: the receipt is `reserved` (new or resumed) and the
/// operation continues with T-dispatch; `Some`: the operation's result. A
/// resumed reservation ends with `COMMIT` of nothing ("commit nothing").
pub fn reserve(c: &Connection, t: &mut StepTiming, key: &Key, fingerprint: &str, grant: Option<&str>, binding: &str, now: Instant, crash_before: bool) -> R<Option<Value>> {
    transact(c, t, "reserve", |tx| {
        let found: Option<(String, Option<String>, String)> = tx
            .query_row(
                "SELECT fingerprint, grant_label, status FROM receipts WHERE tenant_id = ?1 AND idempotency_key = ?2",
                (&key.0, &key.1),
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .optional()?;
        if let Some((fp, label, status)) = found {
            if fp != fingerprint || label.as_deref() != grant {
                return Ok(End::Rollback(Some(refused("IDEMPOTENCY_CONFLICT"))));
            }
            return match status.as_str() {
                "observed_success" => Ok(End::Rollback(Some(replayed("effect_completed")))),
                "observed_failure" => Ok(End::Rollback(Some(replayed("effect_failed")))),
                "dispatched" | "unknown_reconcile" => Ok(End::Rollback(Some(refused("RECONCILIATION_REQUIRED")))),
                "reserved" => Ok(End::Commit(None)),
                _ => Err(DbError::new(CORRUPT)),
            };
        }
        if crash_before {
            return Ok(End::Rollback(Some(result("crashed"))));
        }
        if let Some(label) = grant {
            let row = tx
                .query_row("SELECT binding, expires_s, expires_ns, max_uses, uses FROM grants WHERE label = ?1", [label], |r| {
                    Ok(GrantRow { binding: r.get(0)?, expires: (r.get(1)?, r.get(2)?), max_uses: r.get(3)?, uses: r.get(4)? })
                })
                .optional()?;
            let Some(g) = row else { return Ok(End::Rollback(Some(refused("GRANT_UNKNOWN")))) };
            if !binding_intact(&g.binding) {
                return Err(DbError::new(CORRUPT));
            }
            if g.binding != binding {
                return Ok(End::Rollback(Some(refused("GRANT_MISMATCH"))));
            }
            if now >= g.expires {
                return Ok(End::Rollback(Some(refused("GRANT_EXPIRED"))));
            }
            if g.uses >= g.max_uses || tx.execute("UPDATE grants SET uses = uses + 1 WHERE label = ?1 AND uses < max_uses", [label])? != 1 {
                return Ok(End::Rollback(Some(refused("GRANT_EXHAUSTED"))));
            }
            tx.execute("INSERT INTO grant_uses (label, tenant_id, idempotency_key) VALUES (?1, ?2, ?3)", (label, &key.0, &key.1))?;
        }
        tx.execute(
            "INSERT INTO receipts (tenant_id, idempotency_key, fingerprint, grant_label, status) VALUES (?1, ?2, ?3, ?4, 'reserved')",
            (&key.0, &key.1, fingerprint, grant),
        )?;
        tx.execute("INSERT INTO receipt_events (tenant_id, idempotency_key, status, cause) VALUES (?1, ?2, 'reserved', 'reserve')", (&key.0, &key.1))?;
        Ok(End::Commit(None))
    })
}

/// Move a receipt from `from` to `to` (exactly one row) and append its event.
fn transition(tx: &Transaction, key: &Key, from: &str, to: &str, cause: &str) -> R<()> {
    let n = tx.execute(
        "UPDATE receipts SET status = ?3 WHERE tenant_id = ?1 AND idempotency_key = ?2 AND status = ?4",
        (&key.0, &key.1, to, from),
    )?;
    if n != 1 {
        return Err(DbError::new(CORRUPT));
    }
    tx.execute("INSERT INTO receipt_events (tenant_id, idempotency_key, status, cause) VALUES (?1, ?2, ?3, ?4)", (&key.0, &key.1, to, cause))?;
    Ok(())
}

/// T-dispatch: the new permit number.
pub fn dispatch(c: &Connection, t: &mut StepTiming, key: &Key) -> R<u64> {
    let n = transact(c, t, "dispatch", |tx| {
        if tx.execute("UPDATE counters SET value = value + 1 WHERE name = 'next_permit'", [])? != 1 {
            return Err(DbError::new(CORRUPT));
        }
        let n: i64 = tx.query_row("SELECT value FROM counters WHERE name = 'next_permit'", [], |r| r.get(0))?;
        transition(tx, key, "reserved", "dispatched", "dispatch")?;
        Ok(End::Commit(n))
    })?;
    u64::try_from(n).map_err(|_| DbError::new(CORRUPT).within(Category::Transaction("dispatch")))
}

/// T-adapter (simulated): `check` is the permit verification (fingerprint,
/// MAC, expiry) giving the permit id. `Ok(true)` applied, `Ok(false)`
/// consumed without effect, `Err(code)` refused with nothing written.
pub fn adapter(c: &Connection, t: &mut StepTiming, check: impl FnOnce() -> Result<String, &'static str>, fails: bool) -> R<Result<bool, &'static str>> {
    transact(c, t, "adapter", |tx| {
        let id = match check() {
            Ok(id) => id,
            Err(code) => return Ok(End::Rollback(Err(code))),
        };
        let consumed: bool = tx.query_row("SELECT EXISTS (SELECT 1 FROM adapter_consumed_permits WHERE permit_id = ?1)", [&id], |r| r.get(0))?;
        if consumed {
            return Ok(End::Rollback(Err("PERMIT_REPLAYED")));
        }
        tx.execute("INSERT INTO adapter_consumed_permits (permit_id) VALUES (?1)", [&id])?;
        if !fails {
            tx.execute("INSERT INTO adapter_effects (permit_id) VALUES (?1)", [&id])?;
        }
        Ok(End::Commit(Ok(!fails)))
    })
}

/// T-observe.
pub fn observe(c: &Connection, t: &mut StepTiming, key: &Key, success: bool) -> R<()> {
    transact(c, t, "observe", |tx| {
        transition(tx, key, "dispatched", if success { "observed_success" } else { "observed_failure" }, "observe")?;
        Ok(End::Commit(()))
    })
}

/// approve's transaction: `false` if the label is already in use.
pub fn insert_grant(c: &Connection, t: &mut StepTiming, label: &str, binding: &str, expires: Instant) -> R<bool> {
    transact(c, t, "approve", |tx| {
        let exists: bool = tx.query_row("SELECT EXISTS (SELECT 1 FROM grants WHERE label = ?1)", [label], |r| r.get(0))?;
        if exists {
            return Ok(End::Rollback(false));
        }
        tx.execute(
            "INSERT INTO grants (label, binding, expires_s, expires_ns, max_uses, uses) VALUES (?1, ?2, ?3, ?4, 1, 0)",
            (label, binding, expires.0, expires.1),
        )?;
        Ok(End::Commit(true))
    })
}

/// reconcile: every `dispatched` receipt becomes `unknown_reconcile`.
pub fn reconcile(c: &Connection, t: &mut StepTiming) -> R<i64> {
    transact(c, t, "reconcile", |tx| {
        tx.execute(
            "INSERT INTO receipt_events (tenant_id, idempotency_key, status, cause) \
             SELECT tenant_id, idempotency_key, 'unknown_reconcile', 'reconcile' FROM receipts WHERE status = 'dispatched' ORDER BY tenant_id, idempotency_key",
            [],
        )?;
        let n = tx.execute("UPDATE receipts SET status = 'unknown_reconcile' WHERE status = 'dispatched'", [])?;
        Ok(End::Commit(n as i64))
    })
}

/// One audited refusal.
pub fn refuse(c: &Connection, t: &mut StepTiming, code: &str) -> R<()> {
    transact(c, t, "audit", |tx| {
        tx.execute("INSERT INTO audit_refusals (code) VALUES (?1)", [code])?;
        Ok(End::Commit(()))
    })
}

fn count(c: &Connection, table: &str) -> R<i64> {
    Ok(c.query_row(&format!("SELECT count(*) FROM {table}"), [], |r| r.get(0))?)
}

fn pairs(c: &Connection, sql: &str) -> R<Vec<(String, Value)>> {
    let mut st = c.prepare(sql)?;
    let rows = st.query_map([], |r| Ok((r.get::<_, String>(0)?, Value::Int(r.get(1)?))))?;
    Ok(rows.collect::<Result<_, _>>()?)
}

/// `final` read from the database in one read transaction.
pub fn final_state(c: &Connection) -> R<Value> {
    final_read(c).map_err(|e| e.within(Category::Final))
}

fn final_read(c: &Connection) -> R<Value> {
    let tx = Transaction::new_unchecked(c, TransactionBehavior::Deferred)?;
    let v = obj(vec![
        ("effects_applied", Value::Int(count(&tx, "adapter_effects")?)),
        ("grant_uses", Value::Obj(pairs(&tx, "SELECT label, uses FROM grants ORDER BY label")?)),
        ("receipts", Value::Obj(pairs(&tx, "SELECT status, count(*) FROM receipts GROUP BY status ORDER BY status")?)),
        ("consumed_permits", Value::Int(count(&tx, "adapter_consumed_permits")?)),
        ("refusals", Value::Int(count(&tx, "audit_refusals")?)),
    ]);
    tx.commit()?;
    Ok(v)
}

/// `proto sqlite-info`: engine identity and the settings read back from a
/// fresh connection to a new temporary database (removed afterwards).
pub fn info() -> R<Value> {
    let (path, c) = open_ledger(Target::Dir(&std::env::temp_dir()))?;
    drop(c);
    let out = info_at(&path);
    let _ = std::fs::remove_file(&path);
    out
}

fn info_at(path: &Path) -> R<Value> {
    let c = open(path)?;
    let text = |sql: &str| -> R<String> { Ok(c.query_row(sql, [], |r| r.get(0))?) };
    let int = |sql: &str| -> R<Value> { Ok(Value::Int(c.query_row(sql, [], |r| r.get(0))?)) };
    let mut st = c.prepare("PRAGMA compile_options")?;
    let options = st.query_map([], |r| r.get::<_, String>(0))?.collect::<Result<Vec<_>, _>>()?;
    Ok(obj(vec![
        ("sqlite_version", s(&text("SELECT sqlite_version()")?)),
        ("source_id", s(&text("SELECT sqlite_source_id()")?)),
        ("compile_options", Value::Arr(options.iter().map(|o| s(o)).collect())),
        (
            "settings",
            obj(vec![
                ("journal_mode", s(&text("PRAGMA journal_mode")?)),
                ("synchronous", int("PRAGMA synchronous")?),
                ("busy_timeout", int("PRAGMA busy_timeout")?),
                ("foreign_keys", int("PRAGMA foreign_keys")?),
                ("trusted_schema", int("PRAGMA trusted_schema")?),
            ]),
        ),
        ("schema_version", s(&text("SELECT value FROM meta WHERE key = 'schema_version'")?)),
        ("driver", s(DRIVER)),
    ]))
}
