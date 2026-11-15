//! The immutable content-addressed snapshot store (M5a-06; D-045 M5-D5 and M5-D17;
//! [M5-INTERFACES](../../../docs/implementation/M5-INTERFACES.md) J-4; INV-40).
//!
//! [`SqliteSnapshotStore`] (`snapshots.sql`, schema `writ-snapshots-1`) is a separate
//! SQLite file with the identity and ownership rules of the other stores of this crate
//! (canonical path of a regular file with one link; exclusive OS lock on
//! `<canonical path>.owner` for the store's lifetime; bound at creation to one ledger's
//! [`LedgerId`], `snapshots LEDGER_MISMATCH` for another) plus **owner-epoch fencing** like
//! the ledger: `meta.owner_epoch` is incremented at every create and open, and every write
//! transaction first checks it is still this instance's ([`StoreError::Fenced`] otherwise).
//! WAL with `synchronous=FULL`.
//!
//! **Model.** A snapshot is `(tenant_id, project_id, snapshot_id)` with the
//! `writ-snapshot-manifest/1` digest over its `[{path, digest}]` sorted by path (the format
//! of [`writ_broker::runner::snapshot_digest`]) and
//! `snapshot_id = "snap-" + hex(D-006 digest of {"contract":"writ-snapshot-id/1",
//! "tenant_id", "project_id", "manifest_digest"})` ([`snapshot_id_for`]). File content is in
//! `blobs` by the digest the store computed from the received bytes. Published rows
//! (`blobs`, `snapshots`, `snapshot_files`) are never updated or deleted: the API has no
//! such operation and schema triggers refuse it.
//!
//! **Ingest (M5-D17).** The service calls this API for the authenticated host socket's
//! `snapshot.ingest.*` requests; the scope comes from the connection binding, never from a
//! request field. [`SqliteSnapshotStore::begin`] opens an [`IngestSession`];
//! [`IngestSession::file`] declares one file (`path`, `size`) and
//! [`IngestSession::data`] appends its bytes (any split into frames). When a file's bytes
//! reach its declared size the store computes its digest and stages it (fenced transaction
//! `snapshot_stage`, table `ingest_staging`). [`IngestSession::commit`] runs one fenced
//! transaction (`snapshot_commit`) that re-reads and re-digests the staged bytes, validates
//! the complete manifest, computes the manifest digest, checks the optional claimed digest
//! (a mismatch refuses; a claim is never authority), checks the scope quotas, inserts the
//! blobs, the snapshot and its files, and removes the staging rows. A crash before that
//! commit leaves nothing visible; staging rows are removed at every open and when a
//! session ends without committing. No agent pathname is ever opened.
//!
//! **Manifest rules** ([`SnapshotLimits`] gives the bounds): at least one file; every path
//! passes [`writ_broker::runner::valid_snapshot_path`] (relative, `/`-separated, no empty,
//! `.` or `..` component, no NUL), is at most `max_path_bytes` long with at most
//! `max_path_depth` components of at most 255 bytes each, and consists of printable ASCII
//! (`0x20..=0x7e`): no control character and no non-ASCII character, so no two accepted
//! paths can be equal after Unicode normalization (fail closed until a reviewed normalizer
//! exists); no duplicate path; no path that is a proper prefix directory of another
//! (`a` with `a/b`). Case-only differences are accepted: the reference deployment forbids
//! case-folding filesystems (J-7, J-9 P-FS-TYPE). Every file's received byte count must
//! equal its declared size.
//!
//! **Bounds** (each refuses `SNAPSHOT_LIMIT`): bytes per file, files and bytes per
//! snapshot (one session builds one snapshot), open sessions per store and per scope,
//! snapshots and bytes per scope (logical bytes of committed snapshots; quotas bound growth
//! because nothing is deleted in M5).
//!
//! **Reads** ([`SqliteSnapshotStore::read`], [`SnapshotContent::content`],
//! [`SnapshotStore::check`]) re-verify on every consumption: the snapshot id is recomputed
//! from scope and manifest digest, every blob's length and digest are recomputed from its
//! bytes, the manifest rules are checked again, the file count and total size must match,
//! and the manifest digest is recomputed. Any mismatch fails closed (transaction
//! `snapshot_read`, code `integrity`; the broker traits answer [`SourceUnavailable`]).
//!
//! **Trust.** [`SnapshotContent::immutable`] is `true` only while a deployment proof
//! ([`DeploymentProof`]) attached with [`SqliteSnapshotStore::with_deployment`] reports a
//! currently verified deployment (J-9 `VerifiedDeployment`, which can be withdrawn at a
//! recheck). [`DeploymentProof`] is sealed: no caller outside this crate can implement it,
//! and no flag or constructor argument sets `immutable`. In this revision the only
//! implementation is a test-only proof compiled into this crate's unit tests; the service
//! integration (M5a-04) implements it for `writ_hostsys::VerifiedDeployment`. Without a
//! current proof, `content` and `check` refuse ([`SourceUnavailable`]), so neither the
//! runner nor a promotion accepts a snapshot from an unverified deployment.
//!
//! **Retention.** No deletion in M5 (quotas bound growth); deletion with reference checks
//! is deferred to CO-M4X-03 (M9). Because nothing is ever removed, no active approval, job
//! or promotion can lose the snapshot it references.
//!
//! **Binding.** The broker's `Trust::snapshots` and the runner's `SnapshotContent` must be
//! the same instance (share it in an [`Arc`]).
//!
//! The deployment proof cannot be implemented outside this crate:
//!
//! ```compile_fail
//! struct Forged;
//! impl writ_ledger_sqlite::DeploymentProof for Forged {
//!     fn verified(&self) -> bool { true }
//! }
//! ```

use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, OpenOptions};
use std::io::ErrorKind;
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use rusqlite::{Connection, OpenFlags, OptionalExtension, Transaction, TransactionBehavior};
use writ_broker::runner::{valid_snapshot_path, SnapshotContent, SnapshotFile};
use writ_broker::store::{StoreError, TxFailure, TxStage};
use writ_broker::workflow::{SnapshotCheck, SnapshotStore, SourceUnavailable};
use writ_broker::writ_kernel::json::{self, Digest, Value};
use writ_broker::writ_kernel::refusal::external_code;

use crate::{
    bump_epoch, code_name, connect, existing_identity, meta_value, new_identity, schema_objects,
    sidecar, take_ownership, transact_on, unavailable, BodyError, End, Inner, LedgerId,
};

/// The snapshot store schema, applied once to a new store inside one transaction.
pub const SNAPSHOTS_SCHEMA: &str = include_str!("snapshots.sql");
/// The snapshot store schema version this crate writes and accepts.
pub const SNAPSHOTS_SCHEMA_VERSION: &str = "writ-snapshots-1";

/// The longest accepted path component in bytes (Linux `NAME_MAX`).
const MAX_COMPONENT_BYTES: usize = 255;
/// The longest accepted tenant or project id in bytes.
const MAX_SCOPE_ID_BYTES: usize = 128;
/// SQLite's default maximum string or blob length (`SQLITE_MAX_LENGTH`).
const SQLITE_MAX_LENGTH: u64 = 1_000_000_000;

mod sealed {
    /// Seals [`super::DeploymentProof`]: only this crate implements it.
    pub trait Sealed {}
}

/// Proof that the deployment is verified (J-9 `VerifiedDeployment`). Sealed: implemented
/// only inside this crate (module documentation).
pub trait DeploymentProof: sealed::Sealed + Send + Sync {
    /// `true` while the deployment is currently verified; a withdrawn proof answers `false`.
    fn verified(&self) -> bool;
}

/// Size, count and quota bounds of ingest (module documentation). Every bound must be
/// positive; `max_file_bytes` and `max_snapshot_bytes` are at most SQLite's maximum blob
/// length.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct SnapshotLimits {
    /// Bytes per file.
    pub max_file_bytes: u64,
    /// Files per snapshot.
    pub max_snapshot_files: u64,
    /// Bytes per snapshot (the sum of its file sizes).
    pub max_snapshot_bytes: u64,
    /// Bytes per path.
    pub max_path_bytes: usize,
    /// Components per path.
    pub max_path_depth: usize,
    /// Committed snapshots per (tenant, project).
    pub max_scope_snapshots: u64,
    /// Committed logical bytes per (tenant, project).
    pub max_scope_bytes: u64,
    /// Open ingest sessions per store.
    pub max_open_sessions: usize,
    /// Open ingest sessions per (tenant, project).
    pub max_scope_sessions: usize,
}

impl SnapshotLimits {
    /// The reference bounds of the local profile (configuration may lower them).
    pub const REFERENCE: SnapshotLimits = SnapshotLimits {
        max_file_bytes: 16 * 1024 * 1024,
        max_snapshot_files: 10_000,
        max_snapshot_bytes: 256 * 1024 * 1024,
        max_path_bytes: 1024,
        max_path_depth: 32,
        max_scope_snapshots: 1_000,
        max_scope_bytes: 4 * 1024 * 1024 * 1024,
        max_open_sessions: 8,
        max_scope_sessions: 2,
    };

    fn valid(&self) -> bool {
        self.max_file_bytes > 0
            && self.max_file_bytes <= SQLITE_MAX_LENGTH
            && self.max_snapshot_files > 0
            && self.max_snapshot_bytes > 0
            && self.max_snapshot_bytes <= i64::MAX as u64
            && self.max_path_bytes > 0
            && self.max_path_depth > 0
            && self.max_scope_snapshots > 0
            && self.max_scope_bytes > 0
            && self.max_scope_bytes <= i64::MAX as u64
            && self.max_open_sessions > 0
            && self.max_scope_sessions > 0
    }
}

/// Why ingest refused caller input (internal code; externally the kernel's generic
/// mapping, INV-44).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SnapshotRefusal {
    /// A size, count, session or quota bound was exceeded (`SNAPSHOT_LIMIT`).
    Limit,
    /// A path violates the manifest path rules (`SNAPSHOT_PATH_INVALID`).
    PathInvalid,
    /// The manifest is empty, has a duplicate path or a path that is a proper prefix
    /// directory of another (`SNAPSHOT_MANIFEST_INVALID`).
    ManifestInvalid,
    /// Received bytes differ from a declared size (`SNAPSHOT_SIZE_MISMATCH`).
    SizeMismatch,
    /// The claimed manifest digest differs from the computed one (`SNAPSHOT_DIGEST_MISMATCH`).
    DigestMismatch,
    /// The tenant or project id is not acceptable (`SNAPSHOT_SCOPE_INVALID`).
    ScopeInvalid,
    /// The request does not fit the session's state: data without a declared file, or a
    /// session already ended by a refusal or failure (`SNAPSHOT_SESSION_STATE`).
    SessionState,
}

impl SnapshotRefusal {
    /// Internal code (audit and tests only).
    pub fn code(&self) -> &'static str {
        match self {
            SnapshotRefusal::Limit => "SNAPSHOT_LIMIT",
            SnapshotRefusal::PathInvalid => "SNAPSHOT_PATH_INVALID",
            SnapshotRefusal::ManifestInvalid => "SNAPSHOT_MANIFEST_INVALID",
            SnapshotRefusal::SizeMismatch => "SNAPSHOT_SIZE_MISMATCH",
            SnapshotRefusal::DigestMismatch => "SNAPSHOT_DIGEST_MISMATCH",
            SnapshotRefusal::ScopeInvalid => "SNAPSHOT_SCOPE_INVALID",
            SnapshotRefusal::SessionState => "SNAPSHOT_SESSION_STATE",
        }
    }

    /// Agent-facing code (the kernel's external refusal mapping).
    pub fn external_code(&self) -> &'static str {
        external_code(self.code())
    }
}

/// An ingest failure: refused input, or a store failure (fail closed).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum SnapshotError {
    /// Caller input refused; nothing of the session is kept.
    Refused(SnapshotRefusal),
    /// The store failed (unavailable, fenced, integrity); a commit failure with
    /// `outcome_unknown` may have published the snapshot: ingesting the same content again
    /// is safe and answers the same id.
    Store(TxFailure),
}

impl SnapshotError {
    /// Agent-facing code: the refusal's mapping, or `LEDGER_UNAVAILABLE`.
    pub fn external_code(&self) -> &'static str {
        match self {
            SnapshotError::Refused(r) => r.external_code(),
            SnapshotError::Store(_) => StoreError::EXTERNAL_CODE,
        }
    }
}

impl From<SnapshotRefusal> for SnapshotError {
    fn from(r: SnapshotRefusal) -> SnapshotError {
        SnapshotError::Refused(r)
    }
}

impl From<TxFailure> for SnapshotError {
    fn from(f: TxFailure) -> SnapshotError {
        SnapshotError::Store(f)
    }
}

/// A committed snapshot.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Committed {
    /// The derived snapshot id.
    pub snapshot_id: String,
    /// The computed `writ-snapshot-manifest/1` digest.
    pub manifest_digest: String,
    /// `false` when the identical snapshot already existed in this scope (nothing added).
    pub created: bool,
}

/// A snapshot whose content was verified by this read.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct VerifiedSnapshot {
    /// The recomputed manifest digest.
    pub manifest_digest: String,
    /// The files, sorted by path.
    pub files: Vec<SnapshotFile>,
}

/// The `writ-snapshot-manifest/1` digest of `(path, file digest)` entries sorted by path
/// (the format of [`writ_broker::runner::snapshot_digest`]).
fn manifest_digest(entries: &[(String, String)]) -> Option<String> {
    let doc = json::obj([
        ("contract", json::s("writ-snapshot-manifest/1")),
        (
            "files",
            Value::Arr(
                entries
                    .iter()
                    .map(|(p, d)| json::obj([("path", json::s(p)), ("digest", json::s(d))]))
                    .collect(),
            ),
        ),
    ]);
    json::digest(&doc).ok().map(|d| d.as_str().to_string())
}

/// The snapshot id of a manifest digest in a scope (module documentation).
pub fn snapshot_id_for(tenant_id: &str, project_id: &str, manifest_digest: &str) -> Option<String> {
    let doc = json::obj([
        ("contract", json::s("writ-snapshot-id/1")),
        ("tenant_id", json::s(tenant_id)),
        ("project_id", json::s(project_id)),
        ("manifest_digest", json::s(manifest_digest)),
    ]);
    json::digest(&doc).ok().map(|d| format!("snap-{}", d.hex()))
}

fn scope_valid(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= MAX_SCOPE_ID_BYTES
        && id.bytes().all(|b| (0x21..=0x7e).contains(&b))
}

/// The per-path manifest rules (module documentation).
fn path_valid(path: &str, limits: &SnapshotLimits) -> bool {
    valid_snapshot_path(path)
        && path.len() <= limits.max_path_bytes
        && path.bytes().all(|b| (0x20..=0x7e).contains(&b))
        && path.split('/').count() <= limits.max_path_depth
        && path.split('/').all(|c| c.len() <= MAX_COMPONENT_BYTES)
}

/// The whole-manifest rules for `paths` sorted by path: non-empty, each path valid, no
/// duplicate, no path that is a proper prefix directory of another.
fn manifest_valid<'a>(
    paths: impl Iterator<Item = &'a str> + Clone,
    limits: &SnapshotLimits,
) -> Result<(), SnapshotRefusal> {
    let mut set = BTreeSet::new();
    for p in paths.clone() {
        if !path_valid(p, limits) {
            return Err(SnapshotRefusal::PathInvalid);
        }
        if !set.insert(p) {
            return Err(SnapshotRefusal::ManifestInvalid);
        }
    }
    if set.is_empty() {
        return Err(SnapshotRefusal::ManifestInvalid);
    }
    for p in paths {
        for (i, b) in p.bytes().enumerate() {
            if b == b'/' && set.contains(&p[..i]) {
                return Err(SnapshotRefusal::ManifestInvalid);
            }
        }
    }
    Ok(())
}

/// Open-session accounting (per store and per scope).
#[derive(Default)]
struct Sessions {
    total: usize,
    per_scope: BTreeMap<(String, String), usize>,
}

/// The immutable snapshot store (module documentation).
pub struct SqliteSnapshotStore {
    // Field order matters (no custom Drop): the connection closes before the ownership
    // lock is released (the lock stays the last field).
    inner: Mutex<Inner>,
    epoch: u64,
    limits: SnapshotLimits,
    sessions: Mutex<Sessions>,
    next_session: AtomicU64,
    deployment: Option<Arc<dyn DeploymentProof>>,
    _lock: writ_broker::process::OwnerLock,
}

fn snap_unavailable(code: &str) -> StoreError {
    unavailable("snapshots", code)
}

/// Identity, ownership and initialization errors under the `snapshots` category.
fn as_snap(e: StoreError) -> StoreError {
    match e {
        StoreError::OwnedElsewhere => snap_unavailable("OWNED_ELSEWHERE"),
        StoreError::Unavailable { code, .. } => snap_unavailable(&code),
        other => other,
    }
}

fn verify_store(conn: &Connection, ledger: &LedgerId) -> Result<(), StoreError> {
    let sql = |e: rusqlite::Error| snap_unavailable(code_name(&e));
    let expected = {
        let mem = Connection::open_in_memory().map_err(sql)?;
        mem.execute_batch(SNAPSHOTS_SCHEMA).map_err(sql)?;
        schema_objects(&mem).map_err(sql)?
    };
    if schema_objects(conn).map_err(sql)? != expected {
        return Err(snap_unavailable("schema"));
    }
    if meta_value(conn, "schema_version").map_err(sql)?.as_deref() != Some(SNAPSHOTS_SCHEMA_VERSION)
    {
        return Err(snap_unavailable("schema"));
    }
    if meta_value(conn, "owner_epoch")
        .map_err(sql)?
        .and_then(|e| e.parse::<u64>().ok())
        .is_none()
    {
        return Err(snap_unavailable("schema"));
    }
    match meta_value(conn, "ledger_id").map_err(sql)?.as_deref() {
        Some(id) if id == ledger.as_str() => {}
        Some(id) if LedgerId::parse(id).is_some() => {
            return Err(snap_unavailable("LEDGER_MISMATCH"))
        }
        _ => return Err(snap_unavailable("schema")),
    }
    let mode: String = conn
        .query_row("PRAGMA journal_mode", [], |r| r.get(0))
        .map_err(sql)?;
    if mode != "wal" {
        return Err(snap_unavailable("schema"));
    }
    let check: String = conn
        .query_row("PRAGMA quick_check", [], |r| r.get(0))
        .map_err(sql)?;
    if check != "ok" {
        return Err(snap_unavailable("SQLITE_CORRUPT"));
    }
    Ok(())
}

/// Finish opening: `synchronous=FULL`, a new owner epoch, then removal of every staging
/// row left by an earlier owner (fenced).
fn ready(
    mut conn: Connection,
    lock: writ_broker::process::OwnerLock,
    limits: SnapshotLimits,
) -> Result<SqliteSnapshotStore, StoreError> {
    conn.execute_batch("PRAGMA synchronous = FULL;")
        .map_err(|e| snap_unavailable(code_name(&e)))?;
    let epoch = bump_epoch(&mut conn).map_err(as_snap)?;
    let store = SqliteSnapshotStore {
        inner: Mutex::new(Inner {
            conn,
            #[cfg(test)]
            fault: None,
        }),
        epoch,
        limits,
        sessions: Mutex::new(Sessions::default()),
        next_session: AtomicU64::new(0),
        deployment: None,
        _lock: lock,
    };
    transact_on(&store.inner, Some(epoch), "snapshot_purge", |tx| {
        tx.execute("DELETE FROM ingest_staging", [])?;
        Ok(End::Commit(()))
    })
    .map_err(StoreError::from)?;
    Ok(store)
}

/// The verified content of a snapshot (module documentation, "Reads"); `None` when there
/// is no such snapshot; [`BodyError::Integrity`] on any mismatch.
fn verified_read(
    tx: &Transaction<'_>,
    limits: &SnapshotLimits,
    tenant_id: &str,
    project_id: &str,
    snapshot_id: &str,
) -> Result<Option<VerifiedSnapshot>, BodyError> {
    let row: Option<(String, i64, i64)> = tx
        .query_row(
            "SELECT manifest_digest, file_count, total_size FROM snapshots \
             WHERE tenant_id = ?1 AND project_id = ?2 AND snapshot_id = ?3",
            (tenant_id, project_id, snapshot_id),
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .optional()?;
    let Some((stored_manifest, file_count, total_size)) = row else {
        return Ok(None);
    };
    if snapshot_id_for(tenant_id, project_id, &stored_manifest).as_deref() != Some(snapshot_id) {
        return Err(BodyError::Integrity);
    }
    let mut stmt = tx.prepare(
        "SELECT f.path, f.blob_digest, f.size, b.size, b.bytes FROM snapshot_files f \
         JOIN blobs b ON b.digest = f.blob_digest \
         WHERE f.tenant_id = ?1 AND f.project_id = ?2 AND f.snapshot_id = ?3 \
         ORDER BY f.path",
    )?;
    let mut rows = stmt.query((tenant_id, project_id, snapshot_id))?;
    let mut files = Vec::new();
    let mut entries = Vec::new();
    let mut total: i64 = 0;
    while let Some(r) = rows.next()? {
        let path: String = r.get(0)?;
        let digest: String = r.get(1)?;
        let size: i64 = r.get(2)?;
        let blob_size: i64 = r.get(3)?;
        let bytes: Vec<u8> = r.get(4)?;
        if size != blob_size
            || i64::try_from(bytes.len()).ok() != Some(size)
            || Digest::of_bytes(&bytes).as_str() != digest
        {
            return Err(BodyError::Integrity);
        }
        total = total.checked_add(size).ok_or(BodyError::Integrity)?;
        entries.push((path.clone(), digest));
        files.push(SnapshotFile { path, bytes });
    }
    if manifest_valid(entries.iter().map(|(p, _)| p.as_str()), limits).is_err()
        || i64::try_from(files.len()).ok() != Some(file_count)
        || total != total_size
        || manifest_digest(&entries).as_deref() != Some(stored_manifest.as_str())
    {
        return Err(BodyError::Integrity);
    }
    Ok(Some(VerifiedSnapshot {
        manifest_digest: stored_manifest,
        files,
    }))
}

/// Committed snapshots and logical bytes of a scope.
fn scope_usage(
    tx: &Transaction<'_>,
    tenant_id: &str,
    project_id: &str,
) -> rusqlite::Result<(u64, u64)> {
    let (count, bytes): (i64, i64) = tx.query_row(
        "SELECT count(*), coalesce(sum(total_size), 0) FROM snapshots \
         WHERE tenant_id = ?1 AND project_id = ?2",
        (tenant_id, project_id),
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    Ok((
        u64::try_from(count).unwrap_or(u64::MAX),
        u64::try_from(bytes).unwrap_or(u64::MAX),
    ))
}

impl SqliteSnapshotStore {
    /// Create a new snapshot store at `path`, which must not exist (its directory must),
    /// bound to `ledger` and owned by this process. On any failure after the exclusive
    /// creation, the files this call created are removed.
    pub fn create(
        path: &Path,
        ledger: &LedgerId,
        limits: SnapshotLimits,
    ) -> Result<SqliteSnapshotStore, StoreError> {
        if !limits.valid() {
            return Err(snap_unavailable("limits"));
        }
        let canonical = new_identity(path).map_err(as_snap)?;
        let lock = take_ownership(&canonical).map_err(as_snap)?;
        let path = canonical.as_path();
        match OpenOptions::new().write(true).create_new(true).open(path) {
            Ok(_) => {}
            Err(e) if e.kind() == ErrorKind::AlreadyExists => {
                return Err(snap_unavailable("EXISTS"))
            }
            Err(_) => return Err(snap_unavailable("io")),
        }
        let preexisting: Vec<bool> = ["-journal", "-wal", "-shm"]
            .iter()
            .map(|s| sidecar(path, s).exists())
            .collect();
        let result = (|| {
            let mut conn = connect(path, OpenFlags::SQLITE_OPEN_READ_WRITE)
                .map_err(|e| snap_unavailable(e.code()))?;
            let sql = |e: rusqlite::Error| snap_unavailable(code_name(&e));
            let tx = conn
                .transaction_with_behavior(TransactionBehavior::Immediate)
                .map_err(sql)?;
            tx.execute_batch(SNAPSHOTS_SCHEMA).map_err(sql)?;
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
                return Err(snap_unavailable("SQLITE_ERROR"));
            }
            Ok(conn)
        })()
        .and_then(|conn| ready(conn, lock, limits));
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

    /// Open and verify an existing snapshot store at `path` bound to `ledger`, take
    /// ownership of it, increment its owner epoch and remove staging rows (never
    /// initializes it).
    pub fn open(
        path: &Path,
        ledger: &LedgerId,
        limits: SnapshotLimits,
    ) -> Result<SqliteSnapshotStore, StoreError> {
        if !limits.valid() {
            return Err(snap_unavailable("limits"));
        }
        let canonical = existing_identity(path).map_err(as_snap)?;
        let lock = take_ownership(&canonical).map_err(as_snap)?;
        let conn = connect(&canonical, OpenFlags::SQLITE_OPEN_READ_WRITE)
            .map_err(|e| snap_unavailable(e.code()))?;
        verify_store(&conn, ledger)?;
        ready(conn, lock, limits)
    }

    /// Attach the deployment proof that makes [`SnapshotContent::immutable`] true while it
    /// is verified (module documentation, "Trust").
    pub fn with_deployment(mut self, proof: Arc<dyn DeploymentProof>) -> SqliteSnapshotStore {
        self.deployment = Some(proof);
        self
    }

    /// The bounds this store enforces.
    pub fn limits(&self) -> &SnapshotLimits {
        &self.limits
    }

    fn deployment_verified(&self) -> bool {
        self.deployment.as_ref().is_some_and(|p| p.verified())
    }

    /// Begin an ingest session for the scope of the connection binding.
    pub fn begin(
        self: &Arc<Self>,
        tenant_id: &str,
        project_id: &str,
    ) -> Result<IngestSession, SnapshotError> {
        if !scope_valid(tenant_id) || !scope_valid(project_id) {
            return Err(SnapshotRefusal::ScopeInvalid.into());
        }
        let (count, bytes) = transact_on(&self.inner, None, "snapshot_read", |tx| {
            Ok(End::Rollback(scope_usage(tx, tenant_id, project_id)?))
        })?;
        if count >= self.limits.max_scope_snapshots || bytes >= self.limits.max_scope_bytes {
            return Err(SnapshotRefusal::Limit.into());
        }
        let scope = (tenant_id.to_string(), project_id.to_string());
        {
            let mut s = self
                .sessions
                .lock()
                .map_err(|_| TxFailure::new(TxStage::Begin, snap_unavailable("poisoned")))?;
            let in_scope = s.per_scope.get(&scope).copied().unwrap_or(0);
            if s.total >= self.limits.max_open_sessions
                || in_scope >= self.limits.max_scope_sessions
            {
                return Err(SnapshotRefusal::Limit.into());
            }
            s.total += 1;
            s.per_scope.insert(scope.clone(), in_scope + 1);
        }
        let n = self.next_session.fetch_add(1, Ordering::Relaxed);
        Ok(IngestSession {
            store: Arc::clone(self),
            id: format!("{}-{n}", self.epoch),
            scope,
            scope_bytes: bytes,
            paths: BTreeSet::new(),
            total: 0,
            current: None,
            state: SessionState::Open,
        })
    }

    fn release(&self, scope: &(String, String)) {
        if let Ok(mut s) = self.sessions.lock() {
            s.total = s.total.saturating_sub(1);
            if let Some(n) = s.per_scope.get_mut(scope) {
                *n = n.saturating_sub(1);
                if *n == 0 {
                    s.per_scope.remove(scope);
                }
            }
        }
    }

    fn purge(&self, session_id: &str) {
        let _ = transact_on(&self.inner, Some(self.epoch), "snapshot_purge", |tx| {
            tx.execute(
                "DELETE FROM ingest_staging WHERE session_id = ?1",
                [session_id],
            )?;
            Ok(End::Commit(()))
        });
    }

    /// Read and verify a snapshot (module documentation, "Reads"), independent of the
    /// deployment proof; `Ok(None)` when there is no such snapshot. A verification failure
    /// is a [`TxFailure`] with code `integrity`.
    pub fn read(
        &self,
        tenant_id: &str,
        project_id: &str,
        snapshot_id: &str,
    ) -> Result<Option<VerifiedSnapshot>, TxFailure> {
        transact_on(&self.inner, None, "snapshot_read", |tx| {
            Ok(End::Rollback(verified_read(
                tx,
                &self.limits,
                tenant_id,
                project_id,
                snapshot_id,
            )?))
        })
    }

    /// Arm a one-shot fault (or hang) for the transaction `name` (test only).
    #[cfg(test)]
    pub(crate) fn inject(
        &self,
        name: &str,
        stage: crate::fault::Stage,
        code: std::ffi::c_int,
        hang: bool,
    ) {
        if let Ok(mut inner) = self.inner.lock() {
            inner.fault = Some(crate::fault::Fault {
                transaction: name.to_string(),
                stage,
                code,
                hang,
            });
        }
    }

    /// The connection for direct inspection and tampering (test only).
    #[cfg(test)]
    pub(crate) fn db(&self) -> std::sync::MutexGuard<'_, Inner> {
        self.inner
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }
}

impl SnapshotContent for SqliteSnapshotStore {
    fn content(
        &self,
        tenant_id: &str,
        project_id: &str,
        snapshot_id: &str,
    ) -> Result<Option<Vec<SnapshotFile>>, SourceUnavailable> {
        if !self.deployment_verified() {
            return Err(SourceUnavailable);
        }
        self.read(tenant_id, project_id, snapshot_id)
            .map(|s| s.map(|s| s.files))
            .map_err(|_| SourceUnavailable)
    }

    fn immutable(&self) -> bool {
        self.deployment_verified()
    }
}

impl SnapshotStore for SqliteSnapshotStore {
    fn check(
        &self,
        tenant_id: &str,
        project_id: &str,
        snapshot_id: &str,
        digest: &str,
    ) -> Result<SnapshotCheck, SourceUnavailable> {
        if !self.deployment_verified() {
            return Err(SourceUnavailable);
        }
        match self
            .read(tenant_id, project_id, snapshot_id)
            .map_err(|_| SourceUnavailable)?
        {
            None => Ok(SnapshotCheck::Unknown),
            Some(s) if s.manifest_digest == digest => Ok(SnapshotCheck::Matches),
            Some(_) => Ok(SnapshotCheck::Differs),
        }
    }
}

/// The file being received.
struct Pending {
    path: String,
    size: u64,
    bytes: Vec<u8>,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum SessionState {
    Open,
    /// Committed, refused or failed: every later request is refused.
    Ended,
}

/// One ingest session building one snapshot (module documentation, "Ingest"). Any refusal
/// or failure ends the session and removes its staging rows; dropping an unfinished
/// session does the same.
pub struct IngestSession {
    store: Arc<SqliteSnapshotStore>,
    id: String,
    scope: (String, String),
    /// Committed bytes of the scope when the session began (early quota check only; the
    /// commit transaction checks the quota again).
    scope_bytes: u64,
    paths: BTreeSet<String>,
    total: u64,
    current: Option<Pending>,
    state: SessionState,
}

impl IngestSession {
    fn end<T>(&mut self, e: impl Into<SnapshotError>) -> Result<T, SnapshotError> {
        self.finish();
        Err(e.into())
    }

    fn finish(&mut self) {
        if self.state == SessionState::Open {
            self.state = SessionState::Ended;
            self.current = None;
            self.store.purge(&self.id);
            self.store.release(&self.scope);
        }
    }

    fn open(&mut self) -> Result<(), SnapshotError> {
        if self.state != SessionState::Open {
            return Err(SnapshotRefusal::SessionState.into());
        }
        Ok(())
    }

    /// Declare the next file: its path and exact size in bytes. The previous file must be
    /// complete.
    pub fn file(&mut self, path: &str, size: u64) -> Result<(), SnapshotError> {
        self.open()?;
        let limits = self.store.limits;
        if self.current.is_some() {
            return self.end(SnapshotRefusal::SizeMismatch);
        }
        if !path_valid(path, &limits) {
            return self.end(SnapshotRefusal::PathInvalid);
        }
        if self.paths.contains(path)
            || self
                .paths
                .iter()
                .any(|p| p.starts_with(path) && p.as_bytes().get(path.len()) == Some(&b'/'))
            || path
                .bytes()
                .enumerate()
                .any(|(i, b)| b == b'/' && self.paths.contains(&path[..i]))
        {
            return self.end(SnapshotRefusal::ManifestInvalid);
        }
        let total = self.total.checked_add(size);
        if self.paths.len() as u64 >= limits.max_snapshot_files
            || size > limits.max_file_bytes
            || total.is_none_or(|t| t > limits.max_snapshot_bytes)
            || total
                .and_then(|t| t.checked_add(self.scope_bytes))
                .is_none_or(|t| t > limits.max_scope_bytes)
        {
            return self.end(SnapshotRefusal::Limit);
        }
        self.current = Some(Pending {
            path: path.to_string(),
            size,
            bytes: Vec::new(),
        });
        if size == 0 {
            return self.stage();
        }
        Ok(())
    }

    /// Append bytes of the declared file (any split into frames). Bytes beyond the
    /// declared size refuse; when the declared size is reached the file is digested and
    /// staged.
    pub fn data(&mut self, bytes: &[u8]) -> Result<(), SnapshotError> {
        self.open()?;
        let Some(cur) = self.current.as_mut() else {
            return self.end(SnapshotRefusal::SessionState);
        };
        let received = (cur.bytes.len() as u64).checked_add(bytes.len() as u64);
        if received.is_none_or(|r| r > cur.size) {
            return self.end(SnapshotRefusal::SizeMismatch);
        }
        if cur.bytes.try_reserve(bytes.len()).is_err() {
            return self.end(SnapshotRefusal::Limit);
        }
        cur.bytes.extend_from_slice(bytes);
        if cur.bytes.len() as u64 == cur.size {
            return self.stage();
        }
        Ok(())
    }

    /// Digest the complete current file and stage it (fenced `snapshot_stage`).
    fn stage(&mut self) -> Result<(), SnapshotError> {
        let Some(cur) = self.current.take() else {
            return self.end(SnapshotRefusal::SessionState);
        };
        let digest = Digest::of_bytes(&cur.bytes);
        let size = i64::try_from(cur.size).unwrap_or(i64::MAX);
        let staged = transact_on(
            &self.store.inner,
            Some(self.store.epoch),
            "snapshot_stage",
            |tx| {
                tx.execute(
                    "INSERT INTO ingest_staging (session_id, path, digest, size, bytes) \
                     VALUES (?1, ?2, ?3, ?4, ?5)",
                    (&self.id, &cur.path, digest.as_str(), size, &cur.bytes),
                )?;
                Ok(End::Commit(()))
            },
        );
        if let Err(f) = staged {
            return self.end(f);
        }
        self.total += cur.size;
        self.paths.insert(cur.path);
        Ok(())
    }

    /// Validate the manifest and publish the snapshot in one fenced transaction (module
    /// documentation, "Ingest"). `claimed_manifest_digest`, when present, must equal the
    /// computed digest; it is never used as the digest.
    pub fn commit(
        mut self,
        claimed_manifest_digest: Option<&str>,
    ) -> Result<Committed, SnapshotError> {
        self.open()?;
        if self.current.is_some() {
            return self.end(SnapshotRefusal::SizeMismatch);
        }
        let store = Arc::clone(&self.store);
        let limits = store.limits;
        let (tenant_id, project_id) = self.scope.clone();
        let id = self.id.clone();
        let outcome = transact_on(&store.inner, Some(store.epoch), "snapshot_commit", |tx| {
            let mut stmt = tx.prepare(
                "SELECT path, digest, size FROM ingest_staging WHERE session_id = ?1 ORDER BY path",
            )?;
            let staged = stmt
                .query_map([&id], |r| {
                    Ok((
                        r.get::<_, String>(0)?,
                        r.get::<_, String>(1)?,
                        r.get::<_, i64>(2)?,
                    ))
                })?
                .collect::<rusqlite::Result<Vec<_>>>()?;
            drop(stmt);
            if let Err(r) = manifest_valid(staged.iter().map(|(p, _, _)| p.as_str()), &limits) {
                return Ok(End::Rollback(Err(r)));
            }
            let mut total: u64 = 0;
            for (_, _, size) in &staged {
                let size = u64::try_from(*size).map_err(|_| BodyError::Integrity)?;
                if size > limits.max_file_bytes {
                    return Ok(End::Rollback(Err(SnapshotRefusal::Limit)));
                }
                total = total.checked_add(size).ok_or(BodyError::Integrity)?;
            }
            if staged.len() as u64 > limits.max_snapshot_files || total > limits.max_snapshot_bytes
            {
                return Ok(End::Rollback(Err(SnapshotRefusal::Limit)));
            }
            let entries: Vec<(String, String)> = staged
                .iter()
                .map(|(p, d, _)| (p.clone(), d.clone()))
                .collect();
            let Some(manifest) = manifest_digest(&entries) else {
                return Ok(End::Rollback(Err(SnapshotRefusal::ManifestInvalid)));
            };
            if claimed_manifest_digest.is_some_and(|c| c != manifest) {
                return Ok(End::Rollback(Err(SnapshotRefusal::DigestMismatch)));
            }
            let Some(snapshot_id) = snapshot_id_for(&tenant_id, &project_id, &manifest) else {
                return Ok(End::Rollback(Err(SnapshotRefusal::ManifestInvalid)));
            };
            let existing: Option<String> = tx
                .query_row(
                    "SELECT manifest_digest FROM snapshots \
                     WHERE tenant_id = ?1 AND project_id = ?2 AND snapshot_id = ?3",
                    (&tenant_id, &project_id, &snapshot_id),
                    |r| r.get(0),
                )
                .optional()?;
            let created = match existing {
                Some(m) if m == manifest => false,
                Some(_) => return Err(BodyError::Integrity),
                None => {
                    let (count, bytes) = scope_usage(tx, &tenant_id, &project_id)?;
                    if count >= limits.max_scope_snapshots
                        || bytes
                            .checked_add(total)
                            .is_none_or(|b| b > limits.max_scope_bytes)
                    {
                        return Ok(End::Rollback(Err(SnapshotRefusal::Limit)));
                    }
                    true
                }
            };
            if created {
                let total = i64::try_from(total).map_err(|_| BodyError::Integrity)?;
                tx.execute(
                    "INSERT INTO snapshots (tenant_id, project_id, snapshot_id, manifest_digest, \
                     file_count, total_size) VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
                    (
                        &tenant_id,
                        &project_id,
                        &snapshot_id,
                        &manifest,
                        staged.len() as i64,
                        total,
                    ),
                )?;
                for (path, digest, size) in &staged {
                    // Re-digest the staged bytes (never trust a stored digest).
                    let bytes: Vec<u8> = tx.query_row(
                        "SELECT bytes FROM ingest_staging WHERE session_id = ?1 AND path = ?2",
                        (&id, path),
                        |r| r.get(0),
                    )?;
                    if i64::try_from(bytes.len()).ok() != Some(*size)
                        || Digest::of_bytes(&bytes).as_str() != digest
                    {
                        return Err(BodyError::Integrity);
                    }
                    let inserted = tx.execute(
                        "INSERT INTO blobs (digest, size, bytes) VALUES (?1, ?2, ?3) \
                         ON CONFLICT (digest) DO NOTHING",
                        (digest, size, &bytes),
                    )?;
                    if inserted == 0 {
                        // An existing blob is consumed here: verify it.
                        let (stored_size, stored): (i64, Vec<u8>) = tx.query_row(
                            "SELECT size, bytes FROM blobs WHERE digest = ?1",
                            [digest],
                            |r| Ok((r.get(0)?, r.get(1)?)),
                        )?;
                        if stored_size != *size
                            || i64::try_from(stored.len()).ok() != Some(*size)
                            || Digest::of_bytes(&stored).as_str() != digest
                        {
                            return Err(BodyError::Integrity);
                        }
                    }
                    tx.execute(
                        "INSERT INTO snapshot_files (tenant_id, project_id, snapshot_id, path, \
                         blob_digest, size) VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
                        (&tenant_id, &project_id, &snapshot_id, path, digest, size),
                    )?;
                }
            }
            tx.execute("DELETE FROM ingest_staging WHERE session_id = ?1", [&id])?;
            Ok(End::Commit(Ok(Committed {
                snapshot_id,
                manifest_digest: manifest,
                created,
            })))
        });
        match outcome {
            Ok(Ok(c)) => {
                self.state = SessionState::Ended;
                store.release(&self.scope);
                Ok(c)
            }
            Ok(Err(r)) => self.end(r),
            Err(f) => self.end(f),
        }
    }

    /// End the session without publishing anything.
    pub fn abort(mut self) {
        self.finish();
    }
}

impl std::fmt::Debug for IngestSession {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("IngestSession")
            .field("id", &self.id)
            .field("files", &self.paths.len())
            .field("total", &self.total)
            .finish_non_exhaustive()
    }
}

impl Drop for IngestSession {
    fn drop(&mut self) {
        self.finish();
    }
}

/// A test-only deployment proof (never compiled outside this crate's unit tests).
#[cfg(test)]
pub(crate) struct TestDeployment(std::sync::atomic::AtomicBool);

#[cfg(test)]
impl TestDeployment {
    /// A currently verified test proof.
    pub(crate) fn verified() -> Arc<TestDeployment> {
        Arc::new(TestDeployment(std::sync::atomic::AtomicBool::new(true)))
    }

    /// Withdraw the proof (as a failed J-9 recheck would).
    pub(crate) fn withdraw(&self) {
        self.0.store(false, Ordering::SeqCst);
    }
}

#[cfg(test)]
impl sealed::Sealed for TestDeployment {}

#[cfg(test)]
impl DeploymentProof for TestDeployment {
    fn verified(&self) -> bool {
        self.0.load(Ordering::SeqCst)
    }
}
