//! The mechanical runner's own SQLite store (M3-07; INV-35; D-035 items 2 to 4) and the
//! ledger reads the runner needs ([`RunnerState`] for [`SqliteLedger`]).
//!
//! [`SqliteRunnerStore`] implements [`RunnerStore`] (`runner.sql`, schema `writ-runner-1`):
//! a separate database file holding every permit the runner accepted, the outcome of its
//! run and the authenticated attestations it produced. It also implements
//! [`AttestationSource`]: the transition service reads attestations from it by tenant,
//! project and id (only those of `applied` runs).
//!
//! **Transactions** (each one `BEGIN IMMEDIATE` on the runner store):
//! - `runner_start` ([`RunnerStore::start`]): begin (a failure is `LEDGER_UNAVAILABLE`
//!   before any permit check, D-025 B); the broker's verification callback;
//!   `PERMIT_REPLAYED` when the verified permit id already has a run; otherwise one
//!   `started` run for the verified request; commit. The verification callback persists the
//!   trusted-time floor in a ledger transaction while this one is open: the lock order is
//!   always the runner store's connection, then the ledger's.
//! - `runner_finish` ([`RunnerStore::finish`]): the `started` run becomes `applied` with
//!   exactly one attestation row, or `failed` with its diagnostic. A run in another state,
//!   or none, is `STATE_CONFLICT` (an outcome is never overwritten).
//! - `runner_read` (inspect, record and attestation reads): read-only, rolled back.
//!
//! **Create versus open, identity and ownership** are those of the controlled effect store
//! and use the same helpers: the canonical path of a regular file with one link, an
//! exclusive OS lock on `<canonical path>.owner` for the store's lifetime (a second opener
//! fails at once, `runner OWNED_ELSEWHERE`), and a binding at creation to one ledger's
//! [`LedgerId`] (`runner LEDGER_MISMATCH` for another ledger). This is the local profile's
//! store; a production runner service needs its own concurrency, replay and outcome-recovery
//! contract.

use std::fs::{self, OpenOptions};
use std::io::ErrorKind;
use std::path::Path;
use std::sync::Mutex;

use rusqlite::{Connection, OpenFlags, OptionalExtension, Transaction, TransactionBehavior};
use writ_broker::effects::{VerifiedEffect, Verify};
use writ_broker::process::OwnerLock;
use writ_broker::refusal::Refusal;
use writ_broker::runner::{RunOutcome, RunRecord, RunState, RunnerState, RunnerStore};
use writ_broker::store::{StoreError, TxFailure};
use writ_broker::workflow::{
    AttestationSource, SourceUnavailable, StoredEntry, StoredPolicy, StoredWorkflow, WorkflowKey,
    WorkflowLedger,
};
use writ_broker::writ_kernel::json::{self, Value};

use crate::{
    code_name, connect, existing_identity, meta_value, new_identity, schema_objects, sidecar,
    take_ownership, transact_on, unavailable, BodyError, End, Inner, LedgerId, SqliteLedger,
};

/// The runner store schema, applied once to a new store inside one transaction.
pub const RUNNER_SCHEMA: &str = include_str!("runner.sql");
/// The runner store schema version this crate writes and accepts.
pub const RUNNER_SCHEMA_VERSION: &str = "writ-runner-1";

/// The runner's own store (module documentation).
pub struct SqliteRunnerStore {
    // Field order matters (no custom Drop): the connection closes before the ownership
    // lock is released (the lock stays the last field).
    inner: Mutex<Inner>,
    _lock: OwnerLock,
}

fn runner_unavailable(code: &str) -> StoreError {
    unavailable("runner", code)
}

/// Identity and ownership errors under the `runner` category.
fn as_runner(e: StoreError) -> StoreError {
    match e {
        StoreError::OwnedElsewhere => runner_unavailable("OWNED_ELSEWHERE"),
        StoreError::Unavailable { code, .. } => runner_unavailable(&code),
        other => other,
    }
}

fn verify_store(conn: &Connection, ledger: &LedgerId) -> Result<(), StoreError> {
    let sql = |e: rusqlite::Error| runner_unavailable(code_name(&e));
    let expected = {
        let mem = Connection::open_in_memory().map_err(sql)?;
        mem.execute_batch(RUNNER_SCHEMA).map_err(sql)?;
        schema_objects(&mem).map_err(sql)?
    };
    if schema_objects(conn).map_err(sql)? != expected {
        return Err(runner_unavailable("schema"));
    }
    if meta_value(conn, "schema_version").map_err(sql)?.as_deref() != Some(RUNNER_SCHEMA_VERSION) {
        return Err(runner_unavailable("schema"));
    }
    match meta_value(conn, "ledger_id").map_err(sql)?.as_deref() {
        Some(id) if id == ledger.as_str() => {}
        Some(id) if LedgerId::parse(id).is_some() => {
            return Err(runner_unavailable("LEDGER_MISMATCH"))
        }
        _ => return Err(runner_unavailable("schema")),
    }
    let mode: String = conn
        .query_row("PRAGMA journal_mode", [], |r| r.get(0))
        .map_err(sql)?;
    if mode != "wal" {
        return Err(runner_unavailable("schema"));
    }
    let check: String = conn
        .query_row("PRAGMA quick_check", [], |r| r.get(0))
        .map_err(sql)?;
    if check != "ok" {
        return Err(runner_unavailable("SQLITE_CORRUPT"));
    }
    Ok(())
}

fn ready(conn: Connection, lock: OwnerLock) -> Result<SqliteRunnerStore, StoreError> {
    conn.execute_batch("PRAGMA synchronous = FULL;")
        .map_err(|e| runner_unavailable(code_name(&e)))?;
    Ok(SqliteRunnerStore {
        inner: Mutex::new(Inner {
            conn,
            #[cfg(test)]
            fault: None,
        }),
        _lock: lock,
    })
}

/// The record of `permit_id` (`None`: not consumed). An `applied` run must have exactly
/// one attestation for its tenant, any other run none: otherwise an integrity failure.
fn record_of(tx: &Transaction<'_>, permit_id: &str) -> Result<Option<RunRecord>, BodyError> {
    let row: Option<(String, String, String, String, Option<String>)> = tx
        .query_row(
            "SELECT tenant_id, idempotency_key, request_fingerprint, state, diagnostic \
             FROM runs WHERE permit_id = ?1",
            [permit_id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)),
        )
        .optional()?;
    let Some((tenant_id, idempotency_key, request_fingerprint, state, diagnostic)) = row else {
        return Ok(None);
    };
    let state = RunState::parse(&state).ok_or(BodyError::Integrity)?;
    let mut stmt =
        tx.prepare("SELECT attestation_id, tenant_id FROM attestations WHERE permit_id = ?1")?;
    let attestations = stmt
        .query_map([permit_id], |r| {
            Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    let attestation_id = match (state, attestations.as_slice()) {
        (RunState::Applied, [(id, tenant)]) if *tenant == tenant_id => Some(id.clone()),
        (RunState::Started | RunState::Failed, []) => None,
        _ => return Err(BodyError::Integrity),
    };
    Ok(Some(RunRecord {
        permit_id: permit_id.to_string(),
        tenant_id,
        idempotency_key,
        request_fingerprint,
        state,
        attestation_id,
        diagnostic,
    }))
}

impl SqliteRunnerStore {
    /// Create a new runner store at `path`, which must not exist (its directory must),
    /// bound to `ledger` and owned by this process. On any failure after the exclusive
    /// creation, the files this call created are removed.
    pub fn create(path: &Path, ledger: &LedgerId) -> Result<SqliteRunnerStore, StoreError> {
        let canonical = new_identity(path).map_err(as_runner)?;
        let lock = take_ownership(&canonical).map_err(as_runner)?;
        let path = canonical.as_path();
        match OpenOptions::new().write(true).create_new(true).open(path) {
            Ok(_) => {}
            Err(e) if e.kind() == ErrorKind::AlreadyExists => {
                return Err(runner_unavailable("EXISTS"))
            }
            Err(_) => return Err(runner_unavailable("io")),
        }
        let preexisting: Vec<bool> = ["-journal", "-wal", "-shm"]
            .iter()
            .map(|s| sidecar(path, s).exists())
            .collect();
        let result = (|| {
            let mut conn = connect(path, OpenFlags::SQLITE_OPEN_READ_WRITE)
                .map_err(|e| runner_unavailable(e.code()))?;
            let sql = |e: rusqlite::Error| runner_unavailable(code_name(&e));
            let tx = conn
                .transaction_with_behavior(TransactionBehavior::Immediate)
                .map_err(sql)?;
            tx.execute_batch(RUNNER_SCHEMA).map_err(sql)?;
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
                return Err(runner_unavailable("SQLITE_ERROR"));
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

    /// Open and verify an existing runner store at `path` bound to `ledger`, and take
    /// ownership of it (never initializes it).
    pub fn open(path: &Path, ledger: &LedgerId) -> Result<SqliteRunnerStore, StoreError> {
        let canonical = existing_identity(path).map_err(as_runner)?;
        let lock = take_ownership(&canonical).map_err(as_runner)?;
        let conn = connect(&canonical, OpenFlags::SQLITE_OPEN_READ_WRITE)
            .map_err(|e| runner_unavailable(e.code()))?;
        verify_store(&conn, ledger)?;
        ready(conn, lock)
    }

    /// Arm a one-shot fault for the transaction `name` (`runner_start`, `runner_finish`
    /// or `runner_read`; test only).
    #[cfg(test)]
    pub(crate) fn inject(&self, name: &str, stage: crate::fault::Stage, code: std::ffi::c_int) {
        if let Ok(mut inner) = self.inner.lock() {
            inner.fault = Some(crate::fault::Fault {
                transaction: name.to_string(),
                stage,
                code,
                hang: false,
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

impl RunnerStore for SqliteRunnerStore {
    fn start(&self, verify: &mut Verify<'_>) -> Result<Result<VerifiedEffect, Refusal>, TxFailure> {
        transact_on(&self.inner, None, "runner_start", |tx| {
            let effect = match verify() {
                Ok(effect) => effect,
                Err(refusal) => return Ok(End::Rollback(Err(refusal))),
            };
            let consumed: bool = tx.query_row(
                "SELECT EXISTS (SELECT 1 FROM runs WHERE permit_id = ?1)",
                [effect.permit_id()],
                |r| r.get(0),
            )?;
            if consumed {
                return Ok(End::Rollback(Err(Refusal::PermitReplayed)));
            }
            tx.execute(
                "INSERT INTO runs (permit_id, tenant_id, idempotency_key, request_fingerprint, \
                 state) VALUES (?1, ?2, ?3, ?4, 'started')",
                (
                    effect.permit_id(),
                    effect.tenant_id(),
                    effect.idempotency_key(),
                    effect.request_fingerprint(),
                ),
            )?;
            Ok(End::Commit(Ok(effect)))
        })
    }

    fn finish(&self, outcome: &RunOutcome) -> Result<(), TxFailure> {
        transact_on(&self.inner, None, "runner_finish", |tx| {
            let tenant: Option<String> = tx
                .query_row(
                    "SELECT tenant_id FROM runs WHERE permit_id = ?1 AND state = 'started'",
                    [outcome.permit_id()],
                    |r| r.get(0),
                )
                .optional()?;
            let Some(tenant) = tenant else {
                return Err(BodyError::StateConflict);
            };
            let (state, diagnostic) = match outcome.diagnostic() {
                Some(d) => (RunState::Failed, Some(d.code())),
                None => (RunState::Applied, None),
            };
            if let Some(a) = outcome.attestation() {
                if a.tenant_id() != tenant {
                    return Err(BodyError::Integrity);
                }
                tx.execute(
                    "INSERT INTO attestations (attestation_id, permit_id, tenant_id, project_id, \
                     document) VALUES (?1, ?2, ?3, ?4, ?5)",
                    (
                        a.attestation_id(),
                        outcome.permit_id(),
                        a.tenant_id(),
                        a.project_id(),
                        a.document(),
                    ),
                )?;
            }
            let changed = tx.execute(
                "UPDATE runs SET state = ?2, diagnostic = ?3 \
                 WHERE permit_id = ?1 AND state = 'started'",
                (outcome.permit_id(), state.as_str(), diagnostic),
            )?;
            if changed != 1 {
                return Err(BodyError::StateConflict);
            }
            Ok(End::Commit(()))
        })
    }

    fn inspect(
        &self,
        verify: &mut Verify<'_>,
    ) -> Result<Result<Option<RunRecord>, Refusal>, TxFailure> {
        transact_on(&self.inner, None, "runner_read", |tx| {
            let effect = match verify() {
                Ok(effect) => effect,
                Err(refusal) => return Ok(End::Rollback(Err(refusal))),
            };
            Ok(End::Rollback(Ok(record_of(tx, effect.permit_id())?)))
        })
    }

    fn record(&self, permit_id: &str) -> Result<Option<RunRecord>, TxFailure> {
        transact_on(&self.inner, None, "runner_read", |tx| {
            Ok(End::Rollback(record_of(tx, permit_id)?))
        })
    }
}

impl AttestationSource for SqliteRunnerStore {
    fn attestation(
        &self,
        tenant_id: &str,
        project_id: &str,
        attestation_id: &str,
    ) -> Result<Option<Value>, SourceUnavailable> {
        let text: Option<String> = transact_on(&self.inner, None, "runner_read", |tx| {
            Ok(End::Rollback(
                tx.query_row(
                    "SELECT a.document FROM attestations a JOIN runs r USING (permit_id) \
                     WHERE a.tenant_id = ?1 AND a.project_id = ?2 AND a.attestation_id = ?3 \
                     AND r.state = 'applied' AND r.tenant_id = a.tenant_id",
                    (tenant_id, project_id, attestation_id),
                    |r| r.get(0),
                )
                .optional()?,
            ))
        })
        .map_err(|_| SourceUnavailable)?;
        match text {
            None => Ok(None),
            Some(t) => json::parse(t.as_bytes())
                .map(Some)
                .map_err(|_| SourceUnavailable),
        }
    }
}

/// Ledger reads for the runner (outside any fenced transaction; the broker holds the
/// policy gate and the workflow's lock while a runner request executes).
impl RunnerState for SqliteLedger {
    fn workflow(&self, key: &WorkflowKey) -> Result<Option<StoredWorkflow>, SourceUnavailable> {
        WorkflowLedger::workflow(self, key).map_err(|_| SourceUnavailable)
    }

    fn acceptance_policy(
        &self,
        tenant_id: &str,
        project_id: &str,
    ) -> Result<Option<StoredPolicy>, SourceUnavailable> {
        let inner = self.lock("read").map_err(|_| SourceUnavailable)?;
        let read = || -> rusqlite::Result<Option<StoredPolicy>> {
            let requirements: Option<String> = inner
                .conn
                .query_row(
                    "SELECT requirements FROM acceptance_policies \
                     WHERE tenant_id = ?1 AND project_id = ?2",
                    (tenant_id, project_id),
                    |r| r.get(0),
                )
                .optional()?;
            let Some(requirements) = requirements else {
                return Ok(None);
            };
            let mut stmt = inner.conn.prepare(
                "SELECT effect_kind, state FROM effect_prerequisites \
                 WHERE tenant_id = ?1 AND project_id = ?2 ORDER BY effect_kind, state",
            )?;
            let rows = stmt
                .query_map((tenant_id, project_id), |r| {
                    Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
                })?
                .collect::<rusqlite::Result<Vec<_>>>()?;
            let mut effect_prerequisites: Vec<(String, Vec<String>)> = Vec::new();
            for (kind, state) in rows {
                match effect_prerequisites.last_mut() {
                    Some((k, states)) if *k == kind => states.push(state),
                    _ => effect_prerequisites.push((kind, vec![state])),
                }
            }
            Ok(Some(StoredPolicy {
                requirements,
                effect_prerequisites,
            }))
        };
        read().map_err(|_| SourceUnavailable)
    }

    fn registry_entry(
        &self,
        tenant_id: &str,
        project_id: &str,
        evaluator_id: &str,
    ) -> Result<Option<StoredEntry>, SourceUnavailable> {
        let inner = self.lock("read").map_err(|_| SourceUnavailable)?;
        inner
            .conn
            .query_row(
                "SELECT entry, status FROM evaluator_registry \
                 WHERE tenant_id = ?1 AND project_id = ?2 AND evaluator_id = ?3",
                (tenant_id, project_id, evaluator_id),
                |r| {
                    Ok(StoredEntry {
                        entry: r.get(0)?,
                        revoked: r.get::<_, String>(1)? != "active",
                    })
                },
            )
            .optional()
            .map_err(|_| SourceUnavailable)
    }
}
