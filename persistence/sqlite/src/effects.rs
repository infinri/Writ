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
//! file (exclusive create, schema, the bound ledger identity, WAL; removes what it created
//! on failure); [`SqliteEffectStore::open`] verifies an existing one read-only (exact
//! schema objects, version, WAL, `quick_check`, bound ledger) and never initializes it.
//!
//! **Ownership (D-035 item 2):** like the ledger, the store is identified by the canonical
//! path of a regular file with exactly one link (every symbolic-link and `.`/`..` alias maps
//! to one identity; a hard-linked file is refused, `effects AMBIGUOUS_IDENTITY`) and is
//! held through an exclusive OS lock on `<canonical path>.owner` for the store's lifetime;
//! a second opener in any process fails closed at once (`effects OWNED_ELSEWHERE`),
//! and the OS releases the lock when the owner exits or is killed. The store is bound at
//! creation to one ledger's [`LedgerId`]; opening it for another ledger is refused
//! (`effects LEDGER_MISMATCH`), so a different broker ledger can never acquire control of
//! it, concurrently or later. These are properties of this local test adapter; future
//! remote adapters need their own concurrency, replay, authorization and outcome-recovery
//! contract and are not required to use OS file locks.
//!
//! **Evidence (D-035 item 3):** every consumed permit records the tenant, idempotency key
//! and request fingerprint it was consumed for and whether its effect was applied; an
//! applied one has exactly one matching `effects` row. [`EffectAdapter::evidence`] returns
//! that record (an inconsistent record is `effects integrity`); [`EffectAdapter::inspect`]
//! verifies a presented permit and returns its record without applying anything.

use std::fs::{self, File, OpenOptions};
use std::io::ErrorKind;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;

use rusqlite::{Connection, OpenFlags, OptionalExtension, Transaction, TransactionBehavior};
use writ_broker::effects::{EffectAdapter, EffectEvidence, EffectResult, Verify};
use writ_broker::refusal::Refusal;
use writ_broker::store::{StoreError, TxFailure};

use crate::{
    code_name, connect, existing_identity, meta_value, new_identity, schema_objects, sidecar,
    take_ownership, transact_on, unavailable, BodyError, End, Inner, LedgerId,
};

/// The effect store schema, applied once to a new store inside one transaction.
pub const EFFECTS_SCHEMA: &str = include_str!("effects.sql");
/// The effect store schema version this crate writes and accepts.
pub const EFFECTS_SCHEMA_VERSION: &str = "writ-effects-2";

/// The controlled test effect adapter and its own store (module documentation).
pub struct SqliteEffectStore {
    // Field order matters (no custom Drop): the connection closes before the ownership
    // lock is released.
    inner: Mutex<Inner>,
    failing: AtomicBool,
    _lock: File,
}

fn effects_unavailable(code: &str) -> StoreError {
    unavailable("effects", code)
}

/// Identity and ownership errors under the `effects` category.
fn as_effects(e: StoreError) -> StoreError {
    match e {
        StoreError::OwnedElsewhere => effects_unavailable("OWNED_ELSEWHERE"),
        StoreError::Unavailable { code, .. } => effects_unavailable(&code),
        other => other,
    }
}

fn verify_store(conn: &Connection, ledger: &LedgerId) -> Result<(), StoreError> {
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
    match meta_value(conn, "ledger_id").map_err(sql)?.as_deref() {
        Some(id) if id == ledger.as_str() => {}
        Some(id) if LedgerId::parse(id).is_some() => {
            return Err(effects_unavailable("LEDGER_MISMATCH"))
        }
        _ => return Err(effects_unavailable("schema")),
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

fn ready(conn: Connection, lock: File) -> Result<SqliteEffectStore, StoreError> {
    conn.execute_batch("PRAGMA synchronous = FULL;")
        .map_err(|e| effects_unavailable(code_name(&e)))?;
    Ok(SqliteEffectStore {
        inner: Mutex::new(Inner {
            conn,
            #[cfg(test)]
            fault: None,
        }),
        failing: AtomicBool::new(false),
        _lock: lock,
    })
}

/// The evidence recorded for `permit_id` (`None`: not consumed). A consumed permit whose
/// applied effect row is missing or binds another request is an integrity failure.
fn evidence_of(tx: &Transaction<'_>, permit_id: &str) -> Result<Option<EffectEvidence>, BodyError> {
    let row: Option<(String, String, String, String)> = tx
        .query_row(
            "SELECT tenant_id, idempotency_key, request_fingerprint, result \
             FROM consumed_permits WHERE permit_id = ?1",
            [permit_id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
        )
        .optional()?;
    let Some((tenant_id, idempotency_key, request_fingerprint, result)) = row else {
        return Ok(None);
    };
    let effects: i64 = tx.query_row(
        "SELECT count(*) FROM effects WHERE permit_id = ?1",
        [permit_id],
        |r| r.get(0),
    )?;
    let matching: i64 = tx.query_row(
        "SELECT count(*) FROM effects WHERE permit_id = ?1 AND tenant_id = ?2 \
         AND idempotency_key = ?3 AND request_fingerprint = ?4",
        (
            permit_id,
            &tenant_id,
            &idempotency_key,
            &request_fingerprint,
        ),
        |r| r.get(0),
    )?;
    let result = match (result.as_str(), effects, matching) {
        ("applied", 1, 1) => EffectResult::Applied,
        ("failed", 0, 0) => EffectResult::Failed,
        _ => return Err(BodyError::Integrity),
    };
    Ok(Some(EffectEvidence {
        permit_id: permit_id.to_string(),
        tenant_id,
        idempotency_key,
        request_fingerprint,
        result,
    }))
}

impl SqliteEffectStore {
    /// Create a new effect store at `path`, which must not exist (its directory must),
    /// bound to `ledger` and owned by this process. On any failure after the exclusive
    /// creation, the files this call created are removed.
    pub fn create(path: &Path, ledger: &LedgerId) -> Result<SqliteEffectStore, StoreError> {
        let canonical = new_identity(path).map_err(as_effects)?;
        let lock = take_ownership(&canonical).map_err(as_effects)?;
        let path = canonical.as_path();
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
            tx.execute(
                "INSERT INTO meta (key, value) VALUES ('ledger_id', ?1)",
                [ledger.as_str()],
            )
            .map_err(sql)?;
            tx.commit().map_err(sql)?;
            let mode: String = conn
                .query_row("PRAGMA journal_mode = WAL", [], |r| r.get(0))
                .map_err(sql)?;
            if mode != "wal" {
                return Err(effects_unavailable("SQLITE_ERROR"));
            }
            Ok(conn)
        })()
        .and_then(|conn| ready(conn, lock));
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

    /// Open and verify an existing effect store at `path` bound to `ledger`, and take
    /// ownership of it (never initializes it).
    pub fn open(path: &Path, ledger: &LedgerId) -> Result<SqliteEffectStore, StoreError> {
        let canonical = existing_identity(path).map_err(as_effects)?;
        let lock = take_ownership(&canonical).map_err(as_effects)?;
        let conn = connect(&canonical, OpenFlags::SQLITE_OPEN_READ_WRITE)
            .map_err(|e| effects_unavailable(e.code()))?;
        verify_store(&conn, ledger)?;
        ready(conn, lock)
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
        self.inject_named("adapter", stage, code);
    }

    /// Arm a one-shot fault for the transaction `name` (`adapter` or `evidence`; test only).
    #[cfg(test)]
    pub(crate) fn inject_named(
        &self,
        name: &str,
        stage: crate::fault::Stage,
        code: std::ffi::c_int,
    ) {
        if let Ok(mut inner) = self.inner.lock() {
            inner.fault = Some(crate::fault::Fault {
                transaction: name.to_string(),
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
                "INSERT INTO consumed_permits (permit_id, tenant_id, idempotency_key, \
                 request_fingerprint, result) VALUES (?1, ?2, ?3, ?4, ?5)",
                (
                    effect.permit_id(),
                    effect.tenant_id(),
                    effect.idempotency_key(),
                    effect.request_fingerprint(),
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

    fn inspect(
        &self,
        verify: &mut Verify<'_>,
    ) -> Result<Result<Option<EffectEvidence>, Refusal>, TxFailure> {
        transact_on(&self.inner, None, "adapter", |tx| {
            let effect = match verify() {
                Ok(effect) => effect,
                Err(refusal) => return Ok(End::Rollback(Err(refusal))),
            };
            Ok(End::Rollback(Ok(evidence_of(tx, effect.permit_id())?)))
        })
    }

    fn evidence(&self, permit_id: &str) -> Result<Option<EffectEvidence>, TxFailure> {
        transact_on(&self.inner, None, "evidence", |tx| {
            Ok(End::Rollback(evidence_of(tx, permit_id)?))
        })
    }
}
