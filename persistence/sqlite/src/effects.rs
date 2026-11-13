//! The controlled effect adapter on its own SQLite store (M3-04; M3-START-PLAN
//! "Controlled effect adapter"; LEDGER-SQLITE.md T-adapter and storage rule 6).
//!
//! [`SqliteEffectStore`] is the local **test** adapter: its effect is a row in its own
//! database file (`effects.sql`, schema `writ-effects-1`), separate from the broker
//! ledger. The broker reaches it only through
//! [`writ_broker::effects::EffectAdapter::apply`]; tests observe it by reading the file
//! directly. It is not an external system and makes no exactly-once claim for one.
//!
//! **The `adapter` transaction** (one `BEGIN IMMEDIATE` transaction on the effect store):
//! begin (a failure is `LEDGER_UNAVAILABLE` before any permit check, D-025 B); the
//! broker's verification callback; `PERMIT_REPLAYED` if the verified permit id is already
//! in `consumed_permits`; otherwise insert the permit id and, unless the adapter is set
//! to fail effects, one `effects` row, then commit. Any refusal rolls back; a failing
//! commit leaves neither row. The verification callback persists the trusted-time floor
//! in a ledger transaction while this transaction is open: the lock order is always the
//! effect store's connection, then the ledger's (the ledger never calls the adapter).
//!
//! **Create versus open** follow the ledger: [`SqliteEffectStore::create`] makes a new
//! file (exclusive create, schema, WAL; removes what it created on failure);
//! [`SqliteEffectStore::open`] verifies an existing one read-only (exact schema
//! objects, version, WAL, `quick_check`) and never initializes it. There is no
//! ownership lock: the adapter is used by the one broker process that owns the ledger.

use std::fs::{self, OpenOptions};
use std::io::ErrorKind;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;

use rusqlite::{Connection, OpenFlags, TransactionBehavior};
use writ_broker::effects::{EffectAdapter, EffectResult, Verify};
use writ_broker::refusal::Refusal;
use writ_broker::store::{StoreError, TxFailure};

use crate::{
    code_name, connect, meta_value, schema_objects, sidecar, transact_on, unavailable, End, Inner,
};

/// The effect store schema, applied once to a new store inside one transaction.
pub const EFFECTS_SCHEMA: &str = include_str!("effects.sql");
/// The effect store schema version this crate writes and accepts.
pub const EFFECTS_SCHEMA_VERSION: &str = "writ-effects-1";

/// The controlled test effect adapter and its own store (module documentation).
pub struct SqliteEffectStore {
    inner: Mutex<Inner>,
    failing: AtomicBool,
}

fn effects_unavailable(code: &str) -> StoreError {
    unavailable("effects", code)
}

fn verify_store(conn: &Connection) -> Result<(), StoreError> {
    let sql = |e: rusqlite::Error| effects_unavailable(code_name(&e));
    let expected = {
        let mem = Connection::open_in_memory().map_err(sql)?;
        mem.execute_batch(EFFECTS_SCHEMA).map_err(sql)?;
        schema_objects(&mem).map_err(sql)?
    };
    if schema_objects(conn).map_err(sql)? != expected {
        return Err(effects_unavailable("schema"));
    }
    if meta_value(conn, "schema_version").map_err(sql)?.as_deref() != Some(EFFECTS_SCHEMA_VERSION) {
        return Err(effects_unavailable("schema"));
    }
    let mode: String = conn
        .query_row("PRAGMA journal_mode", [], |r| r.get(0))
        .map_err(sql)?;
    if mode != "wal" {
        return Err(effects_unavailable("schema"));
    }
    let check: String = conn
        .query_row("PRAGMA quick_check", [], |r| r.get(0))
        .map_err(sql)?;
    if check != "ok" {
        return Err(effects_unavailable("SQLITE_CORRUPT"));
    }
    Ok(())
}

fn ready(conn: Connection) -> Result<SqliteEffectStore, StoreError> {
    conn.execute_batch("PRAGMA synchronous = FULL;")
        .map_err(|e| effects_unavailable(code_name(&e)))?;
    Ok(SqliteEffectStore {
        inner: Mutex::new(Inner {
            conn,
            #[cfg(test)]
            fault: None,
        }),
        failing: AtomicBool::new(false),
    })
}

impl SqliteEffectStore {
    /// Create a new effect store at `path`, which must not exist (its directory must).
    /// On any failure after the exclusive creation, the files this call created are
    /// removed.
    pub fn create(path: &Path) -> Result<SqliteEffectStore, StoreError> {
        match OpenOptions::new().write(true).create_new(true).open(path) {
            Ok(_) => {}
            Err(e) if e.kind() == ErrorKind::AlreadyExists => {
                return Err(effects_unavailable("EXISTS"))
            }
            Err(_) => return Err(effects_unavailable("io")),
        }
        let preexisting: Vec<bool> = ["-journal", "-wal", "-shm"]
            .iter()
            .map(|s| sidecar(path, s).exists())
            .collect();
        let result = (|| {
            let mut conn = connect(path, OpenFlags::SQLITE_OPEN_READ_WRITE)
                .map_err(|e| effects_unavailable(e.code()))?;
            let sql = |e: rusqlite::Error| effects_unavailable(code_name(&e));
            let tx = conn
                .transaction_with_behavior(TransactionBehavior::Immediate)
                .map_err(sql)?;
            tx.execute_batch(EFFECTS_SCHEMA).map_err(sql)?;
            tx.commit().map_err(sql)?;
            let mode: String = conn
                .query_row("PRAGMA journal_mode = WAL", [], |r| r.get(0))
                .map_err(sql)?;
            if mode != "wal" {
                return Err(effects_unavailable("SQLITE_ERROR"));
            }
            ready(conn)
        })();
        if result.is_err() {
            let _ = fs::remove_file(path);
            for (suffix, existed) in ["-journal", "-wal", "-shm"].iter().zip(preexisting) {
                if !existed {
                    let _ = fs::remove_file(sidecar(path, suffix));
                }
            }
        }
        result
    }

    /// Open and verify an existing effect store at `path` (never initializes it).
    pub fn open(path: &Path) -> Result<SqliteEffectStore, StoreError> {
        if !fs::metadata(path).is_ok_and(|m| m.is_file()) {
            return Err(effects_unavailable("io"));
        }
        let conn = connect(path, OpenFlags::SQLITE_OPEN_READ_WRITE)
            .map_err(|e| effects_unavailable(e.code()))?;
        verify_store(&conn)?;
        ready(conn)
    }

    /// Test control of the simulated external system: while `true`, a fully verified
    /// permit is still consumed but its effect fails (nothing applied; the broker
    /// records `observed_failure`).
    pub fn set_effects_fail(&self, fail: bool) {
        self.failing.store(fail, Ordering::SeqCst);
    }

    /// Arm a one-shot fault for the `adapter` transaction (test only).
    #[cfg(test)]
    pub(crate) fn inject(&self, stage: crate::fault::Stage, code: std::ffi::c_int) {
        if let Ok(mut inner) = self.inner.lock() {
            inner.fault = Some(crate::fault::Fault {
                transaction: "adapter".to_string(),
                stage,
                code,
                hang: false,
            });
        }
    }

    /// Arm a one-shot hang of the `adapter` transaction (test only).
    #[cfg(test)]
    pub(crate) fn inject_hang(&self, stage: crate::fault::Stage) {
        if let Ok(mut inner) = self.inner.lock() {
            inner.fault = Some(crate::fault::Fault {
                transaction: "adapter".to_string(),
                stage,
                code: 0,
                hang: true,
            });
        }
    }

    /// The connection for direct inspection (test only).
    #[cfg(test)]
    pub(crate) fn db(&self) -> std::sync::MutexGuard<'_, Inner> {
        self.inner
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }
}

impl EffectAdapter for SqliteEffectStore {
    fn apply(&self, verify: &mut Verify<'_>) -> Result<Result<EffectResult, Refusal>, TxFailure> {
        let result = if self.failing.load(Ordering::SeqCst) {
            EffectResult::Failed
        } else {
            EffectResult::Applied
        };
        transact_on(&self.inner, None, "adapter", |tx| {
            let effect = match verify() {
                Ok(effect) => effect,
                Err(refusal) => return Ok(End::Rollback(Err(refusal))),
            };
            let consumed: bool = tx.query_row(
                "SELECT EXISTS (SELECT 1 FROM consumed_permits WHERE permit_id = ?1)",
                [effect.permit_id()],
                |r| r.get(0),
            )?;
            if consumed {
                return Ok(End::Rollback(Err(Refusal::PermitReplayed)));
            }
            tx.execute(
                "INSERT INTO consumed_permits (permit_id, result) VALUES (?1, ?2)",
                (
                    effect.permit_id(),
                    match result {
                        EffectResult::Applied => "applied",
                        EffectResult::Failed => "failed",
                    },
                ),
            )?;
            if result == EffectResult::Applied {
                tx.execute(
                    "INSERT INTO effects (permit_id, tenant_id, idempotency_key, request_fingerprint) \
                     VALUES (?1, ?2, ?3, ?4)",
                    (
                        effect.permit_id(),
                        effect.tenant_id(),
                        effect.idempotency_key(),
                        effect.request_fingerprint(),
                    ),
                )?;
            }
            Ok(End::Commit(Ok(result)))
        })
    }
}
