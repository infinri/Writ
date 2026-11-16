//! The `fs.write` adapter journal `writ-fseffects-1` (M5a-09; D-045 M5-D8;
//! [M5-INTERFACES](../../../docs/implementation/M5-INTERFACES.md) J-0, J-7.4, J-11.9).
//!
//! [`FsJournal`] is one SQLite file per configured root (`fs_journal.sql`), with the
//! identity and ownership rules of the other stores of this crate (canonical path of a
//! regular file with one link; exclusive OS lock on `<canonical path>.owner` for the
//! journal's lifetime, so an out-of-process recovery tool cannot run while the service owns
//! it), **owner-epoch fencing** (`meta.owner_epoch` incremented at every create and open;
//! every write transaction checks it), WAL with `synchronous=FULL` (F6), and a binding to one
//! ledger ([`LedgerId`]) and one root identity ([`JournalBinding`]): opening it with any
//! difference refuses (`fs_journal LEDGER_MISMATCH` or `fs_journal ROOT_MISMATCH`).
//!
//! The journal stores what the adapter decides and nothing else: its typed transaction
//! handle [`FsTx`] reads and writes `ops`, `outcomes`, `targets`, `root_state` and
//! `target_dispositions` rows; identities are opaque text the adapter writes. `outcomes`
//! rows are immutable (schema triggers). Every committed write transaction increments
//! `meta.change_counter` (the disposition revision, J-11.9). The previous owner's process
//! record (pid, start time, boot id) is kept for worker-gone evidence.
//!
//! **Fault injection** (`test-faults` feature or this crate's unit tests only): a one-shot
//! [`JournalFault`] fails the next transaction with a given name at begin, in the body, or
//! at commit either without committing (`CommitLost`) or after committing (`CommitDone`), so
//! callers can test both durable states of an ambiguous commit. No production build enables
//! it; it adds no schema object.

use std::fs::{self, OpenOptions};
use std::io::ErrorKind;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use rusqlite::{Connection, OpenFlags, OptionalExtension, Transaction, TransactionBehavior};
use writ_broker::process::OwnerLock;
use writ_broker::store::{StoreError, TxFailure, TxStage};

use crate::{
    code_name, connect, existing_identity, meta_value, new_identity, schema_objects, sidecar,
    take_ownership, unavailable, LedgerId,
};

/// The journal schema, applied once to a new journal inside one transaction.
pub const FS_JOURNAL_SCHEMA: &str = include_str!("fs_journal.sql");
/// The journal schema version this crate writes and accepts.
pub const FS_JOURNAL_SCHEMA_VERSION: &str = "writ-fseffects-1";

fn jerr(code: &str) -> StoreError {
    unavailable("fs_journal", code)
}

fn as_journal(e: StoreError) -> StoreError {
    match e {
        StoreError::OwnedElsewhere => jerr("OWNED_ELSEWHERE"),
        StoreError::Unavailable { code, .. } => jerr(&code),
        other => other,
    }
}

/// What a journal is bound to (J-7.2): the ledger, the root id, the filesystem UUID and the
/// root directory's inode and birth time (canonical text).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct JournalBinding {
    /// The ledger.
    pub ledger_id: LedgerId,
    /// The root id.
    pub root_id: String,
    /// The filesystem UUID.
    pub fs_uuid: String,
    /// The root directory's inode number.
    pub root_ino: u64,
    /// The root directory's birth time (`<sec>.<nsec>`).
    pub root_btime: String,
}

/// The process that owns a journal (J-11.9 worker-gone evidence).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OwnerRecord {
    /// Process id.
    pub pid: u32,
    /// Process start time (clock ticks since boot).
    pub start: u64,
    /// Boot id.
    pub boot_id: String,
}

/// State of an op (J-7.4).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum OpState {
    /// Durable intent; nothing staged is recorded.
    Intent,
    /// The live apply's own staged file failed and its identity is recorded (L-b).
    Abandoned,
    /// The staged file's identity is recorded; the rename may follow.
    Staged,
    /// An outcome is recorded; terminal (N2).
    Resolved,
}

impl OpState {
    /// The stored value.
    pub fn as_str(self) -> &'static str {
        match self {
            OpState::Intent => "intent",
            OpState::Abandoned => "abandoned",
            OpState::Staged => "staged",
            OpState::Resolved => "resolved",
        }
    }

    fn parse(text: &str) -> Option<OpState> {
        match text {
            "intent" => Some(OpState::Intent),
            "abandoned" => Some(OpState::Abandoned),
            "staged" => Some(OpState::Staged),
            "resolved" => Some(OpState::Resolved),
            _ => None,
        }
    }
}

/// One `ops` row.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OpRow {
    /// `fsop-<permit id>`.
    pub op_id: String,
    /// The consumed permit id (unique).
    pub permit_id: String,
    /// Tenant.
    pub tenant_id: String,
    /// Idempotency key.
    pub idempotency_key: String,
    /// Request fingerprint.
    pub request_fingerprint: String,
    /// Root id.
    pub root_id: String,
    /// Canonical target key.
    pub target: String,
    /// `create_only` or `replace_if`.
    pub mode: String,
    /// The recorded prior identity (with digest) or `absent`.
    pub expected_prior: String,
    /// Content digest to write.
    pub content_digest: String,
    /// Snapshot id.
    pub snapshot_id: String,
    /// Staging file name (unique).
    pub staging_name: String,
    /// State.
    pub state: OpState,
    /// The staged file's identity (set at `abandoned` or `staged`).
    pub staged_identity: Option<String>,
    /// The owner epoch the op was written in (J-11.9).
    pub owner_epoch: u64,
}

/// One `outcomes` row (values checked by the schema).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OutcomeRow {
    /// Op id.
    pub op_id: String,
    /// `completed`, `not_completed` or `unknown`.
    pub operation_evidence: String,
    /// `verified`, `differs`, `absent` or `unknown` (L1).
    pub desired_state: String,
    /// `applied`, `failed` or `unknown`.
    pub result: String,
    /// The reason (a refusal-free failure reason or the recovery row).
    pub reason: String,
    /// The target identity after the op, when known.
    pub target_identity_after: Option<String>,
    /// The displaced prior identity (`replace_if`).
    pub displaced_identity: Option<String>,
    /// Boot id at recording.
    pub boot_id: String,
}

/// One `targets` row.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TargetRow {
    /// Canonical target key.
    pub target: String,
    /// The op blocking the target, if any.
    pub blocked_by: Option<String>,
    /// Tainted.
    pub tainted: bool,
    /// Taint reason.
    pub taint_reason: Option<String>,
    /// Last identity (with digest) or `absent`; `None` before the first op.
    pub last_identity: Option<String>,
}

/// One `target_dispositions` row (J-11.9 step 2).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DispositionRow {
    /// Unique disposition id.
    pub disposition_id: String,
    /// Target key.
    pub target: String,
    /// The decided state digest.
    pub state_digest: String,
    /// Previous `blocked_by`.
    pub prev_blocked_by: Option<String>,
    /// Previous taint.
    pub prev_tainted: bool,
    /// Previous taint reason.
    pub prev_taint_reason: Option<String>,
    /// Previous last identity.
    pub prev_last_identity: Option<String>,
    /// New last identity.
    pub new_last_identity: String,
}

/// How a write transaction body ends when it does not fail.
pub enum FsEnd<T> {
    /// Commit (and increment the change counter).
    Commit(T),
    /// Roll back.
    Rollback(T),
}

/// Why a transaction body failed (rolled back, fail closed).
#[derive(Debug)]
pub struct FsTxError(Inner);

#[derive(Debug)]
enum Inner {
    Sql(rusqlite::Error),
    Integrity,
    StateConflict,
}

impl FsTxError {
    /// Stored content violates the journal's invariants.
    pub fn integrity() -> FsTxError {
        FsTxError(Inner::Integrity)
    }
}

impl From<rusqlite::Error> for FsTxError {
    fn from(e: rusqlite::Error) -> FsTxError {
        FsTxError(Inner::Sql(e))
    }
}

/// Where an injected fault fails a transaction (test only).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FaultStage {
    /// Before `BEGIN IMMEDIATE`.
    Begin,
    /// After the body, rolled back.
    Body,
    /// Instead of `COMMIT`: rolled back, reported as an unknown commit outcome.
    CommitLost,
    /// After a successful `COMMIT`, reported as an unknown commit outcome.
    CommitDone,
}

/// The SQLite primary result an injected fault reports (test only).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FaultCode {
    /// `SQLITE_IOERR`.
    IoErr,
    /// `SQLITE_FULL`.
    Full,
    /// `SQLITE_CORRUPT`.
    Corrupt,
    /// `SQLITE_BUSY`.
    Busy,
}

/// A one-shot fault for the transaction named `transaction` (test only).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct JournalFault {
    /// Transaction name.
    pub transaction: String,
    /// Stage.
    pub stage: FaultStage,
    /// Reported code.
    pub code: FaultCode,
}

#[cfg_attr(not(any(test, feature = "test-faults")), allow(dead_code))]
fn fault_code(code: FaultCode) -> &'static str {
    match code {
        FaultCode::IoErr => "SQLITE_IOERR",
        FaultCode::Full => "SQLITE_FULL",
        FaultCode::Corrupt => "SQLITE_CORRUPT",
        FaultCode::Busy => "SQLITE_BUSY",
    }
}

struct Conn {
    conn: Connection,
    #[cfg(any(test, feature = "test-faults"))]
    fault: Option<JournalFault>,
}

/// The journal of one root (module documentation).
pub struct FsJournal {
    // Field order matters: the connection closes before the ownership lock is released.
    inner: Mutex<Conn>,
    path: PathBuf,
    binding: JournalBinding,
    epoch: u64,
    previous_owner: Option<OwnerRecord>,
    _lock: OwnerLock,
}

/// A transaction on the journal: typed reads and writes only.
pub struct FsTx<'t> {
    tx: &'t Transaction<'t>,
    epoch: u64,
}

fn verify_journal(conn: &Connection, binding: &JournalBinding) -> Result<(), StoreError> {
    let sql = |e: rusqlite::Error| jerr(code_name(&e));
    let expected = {
        let mem = Connection::open_in_memory().map_err(sql)?;
        mem.execute_batch(FS_JOURNAL_SCHEMA).map_err(sql)?;
        schema_objects(&mem).map_err(sql)?
    };
    if schema_objects(conn).map_err(sql)? != expected {
        return Err(jerr("schema"));
    }
    if meta_value(conn, "schema_version").map_err(sql)?.as_deref()
        != Some(FS_JOURNAL_SCHEMA_VERSION)
    {
        return Err(jerr("schema"));
    }
    match meta_value(conn, "ledger_id").map_err(sql)?.as_deref() {
        Some(id) if id == binding.ledger_id.as_str() => {}
        Some(id) if LedgerId::parse(id).is_some() => return Err(jerr("LEDGER_MISMATCH")),
        _ => return Err(jerr("schema")),
    }
    for (key, want) in [
        ("root_id", binding.root_id.clone()),
        ("fs_uuid", binding.fs_uuid.clone()),
        ("root_ino", binding.root_ino.to_string()),
        ("root_btime", binding.root_btime.clone()),
    ] {
        match meta_value(conn, key).map_err(sql)? {
            Some(v) if v == want => {}
            Some(_) => return Err(jerr("ROOT_MISMATCH")),
            None => return Err(jerr("schema")),
        }
    }
    let mode: String = conn
        .query_row("PRAGMA journal_mode", [], |r| r.get(0))
        .map_err(sql)?;
    if mode != "wal" {
        return Err(jerr("schema"));
    }
    let check: String = conn
        .query_row("PRAGMA quick_check", [], |r| r.get(0))
        .map_err(sql)?;
    if check != "ok" {
        return Err(jerr("SQLITE_CORRUPT"));
    }
    Ok(())
}

fn owner_of(conn: &Connection) -> rusqlite::Result<Option<OwnerRecord>> {
    let pid = meta_value(conn, "owner_pid")?;
    let start = meta_value(conn, "owner_start")?;
    let boot = meta_value(conn, "owner_boot_id")?;
    Ok(match (pid, start, boot) {
        (Some(p), Some(s), Some(b)) => match (p.parse(), s.parse()) {
            (Ok(pid), Ok(start)) => Some(OwnerRecord {
                pid,
                start,
                boot_id: b,
            }),
            _ => None,
        },
        _ => None,
    })
}

/// Increment the owner epoch and record `owner`; returns (epoch, previous owner).
fn take_epoch(
    conn: &mut Connection,
    owner: &OwnerRecord,
) -> Result<(u64, Option<OwnerRecord>), StoreError> {
    let sql = |e: rusqlite::Error| jerr(code_name(&e));
    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(sql)?;
    let previous = owner_of(&tx).map_err(sql)?;
    let current = meta_value(&tx, "owner_epoch")
        .map_err(sql)?
        .and_then(|e| e.parse::<u64>().ok())
        .ok_or_else(|| jerr("schema"))?;
    let next = current.checked_add(1).ok_or_else(|| jerr("schema"))?;
    for (k, v) in [
        ("owner_epoch", next.to_string()),
        ("owner_pid", owner.pid.to_string()),
        ("owner_start", owner.start.to_string()),
        ("owner_boot_id", owner.boot_id.clone()),
    ] {
        tx.execute(
            "INSERT INTO meta (key, value) VALUES (?1, ?2) \
             ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (k, v),
        )
        .map_err(sql)?;
    }
    tx.commit().map_err(sql)?;
    Ok((next, previous))
}

fn ready(
    mut conn: Connection,
    lock: OwnerLock,
    path: PathBuf,
    binding: &JournalBinding,
    owner: &OwnerRecord,
) -> Result<FsJournal, StoreError> {
    conn.execute_batch("PRAGMA synchronous = FULL;")
        .map_err(|e| jerr(code_name(&e)))?;
    let (epoch, previous_owner) = take_epoch(&mut conn, owner)?;
    Ok(FsJournal {
        inner: Mutex::new(Conn {
            conn,
            #[cfg(any(test, feature = "test-faults"))]
            fault: None,
        }),
        path,
        binding: binding.clone(),
        epoch,
        previous_owner,
        _lock: lock,
    })
}

impl FsJournal {
    /// Create a new journal at `path` (which must not exist) bound to `binding`, owned by
    /// this process (`owner`). On any failure after the exclusive creation, the files this
    /// call created are removed.
    pub fn create(
        path: &Path,
        binding: &JournalBinding,
        owner: &OwnerRecord,
    ) -> Result<FsJournal, StoreError> {
        let canonical = new_identity(path).map_err(as_journal)?;
        let lock = take_ownership(&canonical).map_err(as_journal)?;
        match OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&canonical)
        {
            Ok(_) => {}
            Err(e) if e.kind() == ErrorKind::AlreadyExists => return Err(jerr("EXISTS")),
            Err(_) => return Err(jerr("io")),
        }
        let result = (|| {
            let mut conn = connect(&canonical, OpenFlags::SQLITE_OPEN_READ_WRITE)
                .map_err(|e| jerr(e.code()))?;
            let sql = |e: rusqlite::Error| jerr(code_name(&e));
            let tx = conn
                .transaction_with_behavior(TransactionBehavior::Immediate)
                .map_err(sql)?;
            tx.execute_batch(FS_JOURNAL_SCHEMA).map_err(sql)?;
            for (k, v) in [
                ("ledger_id", binding.ledger_id.as_str().to_string()),
                ("root_id", binding.root_id.clone()),
                ("fs_uuid", binding.fs_uuid.clone()),
                ("root_ino", binding.root_ino.to_string()),
                ("root_btime", binding.root_btime.clone()),
            ] {
                tx.execute("INSERT INTO meta (key, value) VALUES (?1, ?2)", (k, v))
                    .map_err(sql)?;
            }
            tx.commit().map_err(sql)?;
            let mode: String = conn
                .query_row("PRAGMA journal_mode = WAL", [], |r| r.get(0))
                .map_err(sql)?;
            if mode != "wal" {
                return Err(jerr("SQLITE_ERROR"));
            }
            Ok(conn)
        })()
        .and_then(|conn| ready(conn, lock, canonical.clone(), binding, owner));
        if result.is_err() {
            let _ = fs::remove_file(&canonical);
            for suffix in ["-journal", "-wal", "-shm"] {
                let _ = fs::remove_file(sidecar(&canonical, suffix));
            }
        }
        result
    }

    /// Open and verify an existing journal at `path` bound to `binding`, take ownership of
    /// it as `owner` (never initializes it).
    pub fn open(
        path: &Path,
        binding: &JournalBinding,
        owner: &OwnerRecord,
    ) -> Result<FsJournal, StoreError> {
        let canonical = existing_identity(path).map_err(as_journal)?;
        let lock = take_ownership(&canonical).map_err(as_journal)?;
        let conn =
            connect(&canonical, OpenFlags::SQLITE_OPEN_READ_WRITE).map_err(|e| jerr(e.code()))?;
        verify_journal(&conn, binding)?;
        ready(conn, lock, canonical, binding, owner)
    }

    /// The owner epoch this instance acquired.
    pub fn epoch(&self) -> u64 {
        self.epoch
    }

    /// The owner recorded before this instance took ownership, if any.
    pub fn previous_owner(&self) -> Option<&OwnerRecord> {
        self.previous_owner.as_ref()
    }

    /// Replace the connection with a fresh one, verified again (F6: after a failed commit,
    /// nothing reads the journal through the old connection).
    pub fn reopen(&self) -> Result<(), StoreError> {
        let conn =
            connect(&self.path, OpenFlags::SQLITE_OPEN_READ_WRITE).map_err(|e| jerr(e.code()))?;
        verify_journal(&conn, &self.binding)?;
        conn.execute_batch("PRAGMA synchronous = FULL;")
            .map_err(|e| jerr(code_name(&e)))?;
        let mut inner = self.inner.lock().map_err(|_| jerr("poisoned"))?;
        inner.conn = conn;
        Ok(())
    }

    /// Arm a one-shot fault (`test-faults` feature or unit tests only).
    #[cfg(any(test, feature = "test-faults"))]
    pub fn inject(&self, fault: JournalFault) {
        if let Ok(mut inner) = self.inner.lock() {
            inner.fault = Some(fault);
        }
    }

    /// A fenced write transaction named `name`; a committed body increments the change
    /// counter. Failures report their stage; an issued commit that failed has an unknown
    /// outcome.
    pub fn write<T>(
        &self,
        name: &str,
        body: impl FnOnce(&FsTx<'_>) -> Result<FsEnd<T>, FsTxError>,
    ) -> Result<T, TxFailure> {
        self.run(name, true, body)
    }

    /// A read-only transaction named `name` (always rolled back).
    pub fn read<T>(
        &self,
        name: &str,
        body: impl FnOnce(&FsTx<'_>) -> Result<T, FsTxError>,
    ) -> Result<T, TxFailure> {
        self.run(name, false, |tx| body(tx).map(FsEnd::Rollback))
    }

    fn run<T>(
        &self,
        name: &str,
        fenced: bool,
        body: impl FnOnce(&FsTx<'_>) -> Result<FsEnd<T>, FsTxError>,
    ) -> Result<T, TxFailure> {
        let category = format!("fs_journal {name}");
        let at = |stage: TxStage, code: &str| TxFailure::new(stage, unavailable(&category, code));
        let commit_failed = |code: &str| TxFailure::commit_unknown(unavailable(&category, code));
        let mut inner = self
            .inner
            .lock()
            .map_err(|_| at(TxStage::Begin, "poisoned"))?;
        #[cfg(any(test, feature = "test-faults"))]
        let armed = match &inner.fault {
            Some(f) if f.transaction == name => inner.fault.take(),
            _ => None,
        };
        #[cfg(any(test, feature = "test-faults"))]
        if let Some(f) = armed.as_ref().filter(|f| f.stage == FaultStage::Begin) {
            return Err(at(TxStage::Begin, fault_code(f.code)));
        }
        let tx = inner
            .conn
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|e| at(TxStage::Begin, code_name(&e)))?;
        let mapped = |e: FsTxError| match e.0 {
            Inner::Sql(e) => at(TxStage::Body, code_name(&e)),
            Inner::Integrity => at(TxStage::Body, "integrity"),
            Inner::StateConflict => at(TxStage::Body, StoreError::STATE_CONFLICT),
        };
        if fenced {
            let stored = meta_value(&tx, "owner_epoch")
                .map_err(|e| at(TxStage::Body, code_name(&e)))?
                .and_then(|e| e.parse::<u64>().ok())
                .ok_or_else(|| at(TxStage::Body, "schema"))?;
            if stored != self.epoch {
                return Err(TxFailure::new(
                    TxStage::Body,
                    StoreError::Fenced {
                        mine: self.epoch,
                        stored,
                    },
                ));
            }
        }
        let end = body(&FsTx {
            tx: &tx,
            epoch: self.epoch,
        })
        .map_err(mapped)?;
        #[cfg(any(test, feature = "test-faults"))]
        if let Some(f) = armed.as_ref() {
            match f.stage {
                FaultStage::Body => return Err(at(TxStage::Body, fault_code(f.code))),
                FaultStage::CommitLost => {
                    return Err(match end {
                        FsEnd::Commit(_) => commit_failed(fault_code(f.code)),
                        FsEnd::Rollback(_) => at(TxStage::Commit, fault_code(f.code)),
                    })
                }
                _ => {}
            }
        }
        match end {
            FsEnd::Commit(out) => {
                tx.execute(
                    "UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) \
                     WHERE key = 'change_counter'",
                    [],
                )
                .map_err(|e| at(TxStage::Body, code_name(&e)))?;
                tx.commit().map_err(|e| commit_failed(code_name(&e)))?;
                #[cfg(any(test, feature = "test-faults"))]
                if let Some(f) = armed.filter(|f| f.stage == FaultStage::CommitDone) {
                    return Err(commit_failed(fault_code(f.code)));
                }
                Ok(out)
            }
            FsEnd::Rollback(out) => {
                tx.rollback()
                    .map_err(|e| at(TxStage::Commit, code_name(&e)))?;
                Ok(out)
            }
        }
    }
}

const OP_COLUMNS: &str = "op_id, permit_id, tenant_id, idempotency_key, request_fingerprint, \
    root_id, target, mode, expected_prior, content_digest, snapshot_id, staging_name, state, \
    staged_identity, owner_epoch";

fn op_row(r: &rusqlite::Row<'_>) -> rusqlite::Result<(OpRow, String)> {
    let state: String = r.get(12)?;
    let epoch: i64 = r.get(14)?;
    Ok((
        OpRow {
            op_id: r.get(0)?,
            permit_id: r.get(1)?,
            tenant_id: r.get(2)?,
            idempotency_key: r.get(3)?,
            request_fingerprint: r.get(4)?,
            root_id: r.get(5)?,
            target: r.get(6)?,
            mode: r.get(7)?,
            expected_prior: r.get(8)?,
            content_digest: r.get(9)?,
            snapshot_id: r.get(10)?,
            staging_name: r.get(11)?,
            state: OpState::Intent,
            staged_identity: r.get(13)?,
            owner_epoch: u64::try_from(epoch).unwrap_or(u64::MAX),
        },
        state,
    ))
}

impl FsTx<'_> {
    fn ops_where(&self, clause: &str, arg: &str) -> Result<Vec<OpRow>, FsTxError> {
        let sql = format!("SELECT {OP_COLUMNS} FROM ops WHERE {clause} ORDER BY op_id");
        let mut stmt = self.tx.prepare(&sql)?;
        let rows = if clause.contains("?1") {
            stmt.query_map([arg], op_row)?
                .collect::<rusqlite::Result<Vec<_>>>()?
        } else {
            stmt.query_map([], op_row)?
                .collect::<rusqlite::Result<Vec<_>>>()?
        };
        rows.into_iter()
            .map(|(mut op, state)| {
                op.state = OpState::parse(&state).ok_or_else(FsTxError::integrity)?;
                Ok(op)
            })
            .collect()
    }

    fn one(&self, clause: &str, arg: &str) -> Result<Option<OpRow>, FsTxError> {
        let mut ops = self.ops_where(clause, arg)?;
        if ops.len() > 1 {
            return Err(FsTxError::integrity());
        }
        Ok(ops.pop())
    }

    /// The op that consumed `permit_id`.
    pub fn op_by_permit(&self, permit_id: &str) -> Result<Option<OpRow>, FsTxError> {
        self.one("permit_id = ?1", permit_id)
    }

    /// The op `op_id`.
    pub fn op(&self, op_id: &str) -> Result<Option<OpRow>, FsTxError> {
        self.one("op_id = ?1", op_id)
    }

    /// The op whose staging name is `name`.
    pub fn op_by_staging_name(&self, name: &str) -> Result<Option<OpRow>, FsTxError> {
        self.one("staging_name = ?1", name)
    }

    /// Every op in state `intent`, `abandoned` or `staged`.
    pub fn unresolved_ops(&self) -> Result<Vec<OpRow>, FsTxError> {
        self.ops_where("state <> 'resolved'", "")
    }

    /// Every op on `target`.
    pub fn ops_for_target(&self, target: &str) -> Result<Vec<OpRow>, FsTxError> {
        self.ops_where("target = ?1", target)
    }

    /// Insert `op` (its `owner_epoch` is this instance's epoch).
    pub fn insert_op(&self, op: &OpRow) -> Result<(), FsTxError> {
        let epoch = i64::try_from(self.epoch).map_err(|_| FsTxError::integrity())?;
        self.tx.execute(
            &format!(
                "INSERT INTO ops ({OP_COLUMNS}) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, \
                 ?10, ?11, ?12, ?13, ?14, ?15)"
            ),
            rusqlite::params![
                op.op_id,
                op.permit_id,
                op.tenant_id,
                op.idempotency_key,
                op.request_fingerprint,
                op.root_id,
                op.target,
                op.mode,
                op.expected_prior,
                op.content_digest,
                op.snapshot_id,
                op.staging_name,
                op.state.as_str(),
                op.staged_identity,
                epoch,
            ],
        )?;
        Ok(())
    }

    /// Move `op_id` from `from` to `to` (`staged_identity` set when given); exactly one row
    /// must change, otherwise a state conflict.
    pub fn set_op_state(
        &self,
        op_id: &str,
        from: OpState,
        to: OpState,
        staged_identity: Option<&str>,
    ) -> Result<(), FsTxError> {
        let changed = self.tx.execute(
            "UPDATE ops SET state = ?3, staged_identity = coalesce(?4, staged_identity), \
             updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') \
             WHERE op_id = ?1 AND state = ?2",
            (op_id, from.as_str(), to.as_str(), staged_identity),
        )?;
        if changed != 1 {
            return Err(FsTxError(Inner::StateConflict));
        }
        Ok(())
    }

    /// Record `outcome` and mark its op `resolved` (from any unresolved state).
    pub fn resolve(&self, outcome: &OutcomeRow) -> Result<(), FsTxError> {
        let changed = self.tx.execute(
            "UPDATE ops SET state = 'resolved', updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') \
             WHERE op_id = ?1 AND state <> 'resolved'",
            [&outcome.op_id],
        )?;
        if changed != 1 {
            return Err(FsTxError(Inner::StateConflict));
        }
        self.insert_outcome(outcome)
    }

    /// Record `outcome` for an op inserted `resolved` in this transaction (a consumed
    /// `Failed` result decided at intent); the op must be `resolved` without an outcome.
    pub fn insert_outcome(&self, outcome: &OutcomeRow) -> Result<(), FsTxError> {
        let resolved: bool = self.tx.query_row(
            "SELECT EXISTS (SELECT 1 FROM ops WHERE op_id = ?1 AND state = 'resolved')",
            [&outcome.op_id],
            |r| r.get(0),
        )?;
        if !resolved {
            return Err(FsTxError(Inner::StateConflict));
        }
        self.tx.execute(
            "INSERT INTO outcomes (op_id, operation_evidence, desired_state, result, reason, \
             target_identity_after, displaced_identity, boot_id) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
            rusqlite::params![
                outcome.op_id,
                outcome.operation_evidence,
                outcome.desired_state,
                outcome.result,
                outcome.reason,
                outcome.target_identity_after,
                outcome.displaced_identity,
                outcome.boot_id,
            ],
        )?;
        Ok(())
    }

    /// The outcome of `op_id`.
    pub fn outcome(&self, op_id: &str) -> Result<Option<OutcomeRow>, FsTxError> {
        Ok(self
            .tx
            .query_row(
                "SELECT op_id, operation_evidence, desired_state, result, reason, \
                 target_identity_after, displaced_identity, boot_id FROM outcomes WHERE op_id = ?1",
                [op_id],
                |r| {
                    Ok(OutcomeRow {
                        op_id: r.get(0)?,
                        operation_evidence: r.get(1)?,
                        desired_state: r.get(2)?,
                        result: r.get(3)?,
                        reason: r.get(4)?,
                        target_identity_after: r.get(5)?,
                        displaced_identity: r.get(6)?,
                        boot_id: r.get(7)?,
                    })
                },
            )
            .optional()?)
    }

    /// The `targets` row of `target`.
    pub fn target(&self, target: &str) -> Result<Option<TargetRow>, FsTxError> {
        Ok(self
            .tx
            .query_row(
                "SELECT target, blocked_by, tainted, taint_reason, last_identity FROM targets \
                 WHERE target = ?1",
                [target],
                |r| {
                    Ok(TargetRow {
                        target: r.get(0)?,
                        blocked_by: r.get(1)?,
                        tainted: r.get::<_, i64>(2)? == 1,
                        taint_reason: r.get(3)?,
                        last_identity: r.get(4)?,
                    })
                },
            )
            .optional()?)
    }

    /// Insert or replace the `targets` row of `row.target`.
    pub fn put_target(&self, row: &TargetRow) -> Result<(), FsTxError> {
        self.tx.execute(
            "INSERT INTO targets (target, blocked_by, tainted, taint_reason, last_identity) \
             VALUES (?1, ?2, ?3, ?4, ?5) ON CONFLICT (target) DO UPDATE SET \
             blocked_by = excluded.blocked_by, tainted = excluded.tainted, \
             taint_reason = excluded.taint_reason, last_identity = excluded.last_identity",
            rusqlite::params![
                row.target,
                row.blocked_by,
                i64::from(row.tainted),
                row.taint_reason,
                row.last_identity,
            ],
        )?;
        Ok(())
    }

    /// Every unrecoverable mark (boot id, reason), oldest first.
    pub fn root_marks(&self) -> Result<Vec<(String, String)>, FsTxError> {
        let mut stmt = self
            .tx
            .prepare("SELECT unrecoverable_boot_id, reason FROM root_state ORDER BY seq")?;
        let rows = stmt
            .query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(rows)
    }

    /// Record an unrecoverable mark for `boot_id`.
    pub fn add_root_mark(&self, boot_id: &str, reason: &str) -> Result<(), FsTxError> {
        self.tx.execute(
            "INSERT INTO root_state (unrecoverable_boot_id, reason) VALUES (?1, ?2)",
            (boot_id, reason),
        )?;
        Ok(())
    }

    /// The applied disposition `disposition_id`, if any.
    pub fn disposition(&self, disposition_id: &str) -> Result<Option<DispositionRow>, FsTxError> {
        Ok(self
            .tx
            .query_row(
                "SELECT disposition_id, target, state_digest, prev_blocked_by, prev_tainted, \
                 prev_taint_reason, prev_last_identity, new_last_identity \
                 FROM target_dispositions WHERE disposition_id = ?1",
                [disposition_id],
                |r| {
                    Ok(DispositionRow {
                        disposition_id: r.get(0)?,
                        target: r.get(1)?,
                        state_digest: r.get(2)?,
                        prev_blocked_by: r.get(3)?,
                        prev_tainted: r.get::<_, i64>(4)? == 1,
                        prev_taint_reason: r.get(5)?,
                        prev_last_identity: r.get(6)?,
                        new_last_identity: r.get(7)?,
                    })
                },
            )
            .optional()?)
    }

    /// Insert an applied disposition (unique id; a duplicate fails).
    pub fn insert_disposition(&self, row: &DispositionRow) -> Result<(), FsTxError> {
        self.tx.execute(
            "INSERT INTO target_dispositions (disposition_id, target, state_digest, \
             prev_blocked_by, prev_tainted, prev_taint_reason, prev_last_identity, \
             new_last_identity) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
            rusqlite::params![
                row.disposition_id,
                row.target,
                row.state_digest,
                row.prev_blocked_by,
                i64::from(row.prev_tainted),
                row.prev_taint_reason,
                row.prev_last_identity,
                row.new_last_identity,
            ],
        )?;
        Ok(())
    }

    /// The change counter (J-11.9 revision).
    pub fn change_counter(&self) -> Result<u64, FsTxError> {
        meta_value(self.tx, "change_counter")?
            .and_then(|v| v.parse().ok())
            .ok_or_else(FsTxError::integrity)
    }

    /// The owner epoch stored in the journal.
    pub fn stored_epoch(&self) -> Result<u64, FsTxError> {
        meta_value(self.tx, "owner_epoch")?
            .and_then(|v| v.parse().ok())
            .ok_or_else(FsTxError::integrity)
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;

    fn tmp(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("writ-fsj-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&d);
        fs::create_dir_all(&d).unwrap();
        d
    }

    fn binding(ledger: &LedgerId) -> JournalBinding {
        JournalBinding {
            ledger_id: ledger.clone(),
            root_id: "r1".into(),
            fs_uuid: "u".into(),
            root_ino: 7,
            root_btime: "1.2".into(),
        }
    }

    fn me() -> OwnerRecord {
        OwnerRecord {
            pid: 1,
            start: 2,
            boot_id: "b".into(),
        }
    }

    fn op(id: &str, target: &str) -> OpRow {
        OpRow {
            op_id: format!("fsop-{id}"),
            permit_id: id.into(),
            tenant_id: "t".into(),
            idempotency_key: "k".into(),
            request_fingerprint: "f".into(),
            root_id: "r1".into(),
            target: target.into(),
            mode: "create_only".into(),
            expected_prior: "absent".into(),
            content_digest: "d".into(),
            snapshot_id: "s".into(),
            staging_name: format!("x-{id}"),
            state: OpState::Intent,
            staged_identity: None,
            owner_epoch: 0,
        }
    }

    #[test]
    fn binding_ownership_epochs_and_one_unresolved_op_per_target() {
        let d = tmp("bind");
        let ledger = crate::SqliteLedger::create(&d.join("l.db")).unwrap();
        let id = ledger.ledger_id().clone();
        let p = d.join("j.db");
        let j = FsJournal::create(&p, &binding(&id), &me()).unwrap();
        assert_eq!(j.epoch(), 1);
        assert_eq!(
            FsJournal::open(&p, &binding(&id), &me()).err().unwrap(),
            jerr("OWNED_ELSEWHERE")
        );
        j.write("t", |tx| {
            tx.insert_op(&op("p1", "a"))?;
            Ok(FsEnd::Commit(()))
        })
        .unwrap();
        // A second unresolved op on the same target is refused by the schema.
        let dup = j.write("t", |tx| {
            tx.insert_op(&op("p2", "a"))?;
            Ok(FsEnd::Commit(()))
        });
        assert_eq!(dup.unwrap_err().stage, TxStage::Body);
        let outcome = OutcomeRow {
            op_id: "fsop-p1".into(),
            operation_evidence: "not_completed".into(),
            desired_state: "absent".into(),
            result: "failed".into(),
            reason: "R1a".into(),
            target_identity_after: None,
            displaced_identity: None,
            boot_id: "b".into(),
        };
        j.write("t", |tx| {
            tx.resolve(&outcome)?;
            tx.insert_op(&op("p2", "a"))?;
            Ok(FsEnd::Commit(()))
        })
        .unwrap();
        // Resolved is terminal and outcomes are immutable.
        assert!(j
            .write("t", |tx| {
                tx.resolve(&outcome)?;
                Ok(FsEnd::Commit(()))
            })
            .is_err());
        // Ambiguous commits: both durable states.
        j.inject(JournalFault {
            transaction: "t".into(),
            stage: FaultStage::CommitLost,
            code: FaultCode::IoErr,
        });
        let f = j
            .write("t", |tx| {
                tx.add_root_mark("b", "lost")?;
                Ok(FsEnd::Commit(()))
            })
            .unwrap_err();
        assert!(f.outcome_unknown && f.stage == TxStage::Commit);
        j.inject(JournalFault {
            transaction: "t".into(),
            stage: FaultStage::CommitDone,
            code: FaultCode::IoErr,
        });
        assert!(
            j.write("t", |tx| {
                tx.add_root_mark("b", "done")?;
                Ok(FsEnd::Commit(()))
            })
            .unwrap_err()
            .outcome_unknown
        );
        j.reopen().unwrap();
        let marks = j.read("r", |tx| tx.root_marks()).unwrap();
        assert_eq!(marks, vec![("b".to_string(), "done".to_string())]);
        drop(j);
        // Another ledger or another root identity refuses; reopening increments the epoch.
        let other = crate::SqliteLedger::create(&d.join("l2.db")).unwrap();
        assert_eq!(
            FsJournal::open(&p, &binding(other.ledger_id()), &me())
                .err()
                .unwrap(),
            jerr("LEDGER_MISMATCH")
        );
        let mut b = binding(&id);
        b.root_ino = 8;
        assert_eq!(
            FsJournal::open(&p, &b, &me()).err().unwrap(),
            jerr("ROOT_MISMATCH")
        );
        let j = FsJournal::open(&p, &binding(&id), &me()).unwrap();
        assert_eq!(j.epoch(), 2);
        assert_eq!(j.previous_owner(), Some(&me()));
        assert_eq!(j.read("r", |tx| tx.unresolved_ops()).unwrap().len(), 1);
        drop(j);
        fs::remove_dir_all(&d).unwrap();
    }
}
