//! The ledger side of the key source (M5a-04 with M5a-05; D-045 M5-D6, D-046 Q-3;
//! M5-INTERFACES J-3 and J-11.8): the `key_registry` and `key_revocations` tables of
//! `writ-ledger-6` (schema_v6.sql) behind [`KeyRegistry`] and [`KeyRevocations`], and the
//! operator revocation [`SqliteLedger::revoke_key`] that `admin.sock` `keys.revoke` calls.
//!
//! - **Registry** ([`KeyRegistry::admit`], one fenced `BEGIN IMMEDIATE` transaction
//!   `key_admit`): an id or a material digest that was ever revoked is
//!   [`Admission::Revoked`]; an id registered with other material or purpose is
//!   [`Admission::IdConflict`]; material registered under another id is
//!   [`Admission::MaterialConflict`]; otherwise the key is recorded (once) and admitted. Ids
//!   are never reused and rows are never updated or deleted (schema triggers).
//! - **Lookup at use** ([`KeyRevocations::is_revoked`], transaction `key_revocation_lookup`):
//!   no cache; every call reads the ledger. The broker calls it inside the transaction that
//!   consumes the authenticated object (permits: from the verification callback inside the
//!   adapter transaction). Seals are additionally checked by T-reserve and T-dispatch
//!   themselves ([`writ_broker::grants::ReserveRequest::seal_key`]); the keys of the
//!   acceptance record and runner attestations by the promotion transaction
//!   ([`writ_broker::workflow::PromotionIntent::signing_keys`], M5a-13 M2).
//! - **Revocation** ([`SqliteLedger::revoke_key`], transaction `key_revoke`): explicit scope
//!   only ([`RevocationScope`]); for a key id the purpose must equal the registered purpose
//!   and the recorded digest is the registered one; for a material digest a registered
//!   digest must have the requested purpose, and an unregistered digest is accepted as a
//!   pre-emptive revocation. The row and one `ledger_sequence` row (kind `key_revocation`)
//!   commit together, so a revocation is ordered against every reserve, dispatch, permit
//!   verification and promotion. A repeated identical revocation returns the existing row.
//!   Revocation only reduces authority: nothing else is changed.

use rusqlite::{OptionalExtension, Transaction};
use writ_broker::keys::{
    Admission, KeyId, KeyPurpose, KeyRegistry, KeyRevocation, KeyRevocations, KeysUnavailable,
    MaterialDigest,
};
use writ_broker::writ_kernel::time::Timestamp;

use crate::{BodyError, End, SqliteLedger, StoreError};

/// Longest revocation reason in bytes (J-11.8: bounded untrusted text).
pub const MAX_REVOCATION_REASON_BYTES: usize = 512;

/// What one `keys.revoke` names (J-11.8): exactly one key id or one material digest, each
/// of one purpose. There is no wildcard and no un-revoke.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RevocationScope {
    /// One registered key id of `purpose`.
    KeyId {
        /// The purpose the operator states (must equal the registered purpose).
        purpose: KeyPurpose,
        /// The key id.
        key_id: KeyId,
    },
    /// One material digest of `purpose` (registered under any id, or not registered).
    Material {
        /// The purpose the operator states.
        purpose: KeyPurpose,
        /// The material digest.
        digest: MaterialDigest,
    },
}

impl RevocationScope {
    fn name(&self) -> &'static str {
        match self {
            RevocationScope::KeyId { .. } => "key_id",
            RevocationScope::Material { .. } => "material",
        }
    }

    fn purpose(&self) -> KeyPurpose {
        match self {
            RevocationScope::KeyId { purpose, .. } | RevocationScope::Material { purpose, .. } => {
                *purpose
            }
        }
    }
}

pub use writ_broker::keys::OsActor;

/// Why a revocation request was refused (nothing was recorded).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RevokeRefusal {
    /// Scope `key_id`: the id is not registered.
    Unregistered,
    /// The stated purpose differs from the registered purpose.
    PurposeMismatch,
    /// The reason is empty, longer than [`MAX_REVOCATION_REASON_BYTES`] or contains a NUL.
    Reason,
}

/// A recorded key revocation row.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RevocationRow {
    /// `key_revocations.revocation_id`.
    pub revocation_id: i64,
    /// `key_id` or `material`.
    pub scope: String,
    /// The revoked id (the registered id for scope `material`, if any).
    pub key_id: Option<String>,
    /// The revoked material digest (the registered one for scope `key_id`).
    pub material_digest: String,
    /// The purpose.
    pub purpose: String,
    /// Trusted time of the revocation (RFC 3339).
    pub revoked_at: String,
    /// The canonical actor document.
    pub actor: String,
    /// The reason as given (untrusted text; escape wherever displayed).
    pub reason: String,
    /// The `ledger_sequence` row committed with the revocation.
    pub ledger_seq: i64,
    /// `false` when an identical revocation already existed and was returned unchanged.
    pub created: bool,
}

fn reason_valid(reason: &str) -> bool {
    !reason.is_empty() && reason.len() <= MAX_REVOCATION_REASON_BYTES && !reason.contains('\0')
}

fn revoked(tx: &Transaction<'_>, id: Option<&str>, digest: &str) -> rusqlite::Result<bool> {
    tx.query_row(
        "SELECT EXISTS (SELECT 1 FROM key_revocations \
         WHERE material_digest = ?2 OR (?1 IS NOT NULL AND key_id = ?1))",
        (id, digest),
        |r| r.get(0),
    )
}

/// Inside an open ledger transaction: `true` when `id` or `digest` was ever revoked. Used by
/// T-reserve and T-dispatch for the seal key of the request they consume (J-3).
pub(crate) fn revoked_in(
    tx: &Transaction<'_>,
    id: &KeyId,
    digest: &MaterialDigest,
) -> Result<bool, BodyError> {
    Ok(revoked(tx, Some(id.as_str()), digest.as_str())?)
}

/// As [`revoked_in`] for a key id in its text form (acceptance records and runner
/// attestations name keys by string; an id that is not a [`KeyId`] still matches a
/// revocation of its material). Used by the promotion transaction (J-3).
pub(crate) fn revoked_text_in(
    tx: &Transaction<'_>,
    id: &str,
    digest: &MaterialDigest,
) -> Result<bool, BodyError> {
    Ok(revoked(tx, Some(id), digest.as_str())?)
}

fn other_purpose_revoked(
    tx: &Transaction<'_>,
    digest: &str,
    purpose: &str,
) -> rusqlite::Result<bool> {
    tx.query_row(
        "SELECT EXISTS (SELECT 1 FROM key_revocations WHERE material_digest = ?1 AND purpose <> ?2)",
        (digest, purpose),
        |r| r.get(0),
    )
}

fn registered_by_id(tx: &Transaction<'_>, id: &str) -> rusqlite::Result<Option<(String, String)>> {
    tx.query_row(
        "SELECT purpose, material_digest FROM key_registry WHERE key_id = ?1",
        [id],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )
    .optional()
}

fn registered_by_digest(
    tx: &Transaction<'_>,
    digest: &str,
) -> rusqlite::Result<Option<(String, String)>> {
    tx.query_row(
        "SELECT key_id, purpose FROM key_registry WHERE material_digest = ?1",
        [digest],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )
    .optional()
}

fn read_row(
    tx: &Transaction<'_>,
    scope: &str,
    digest: &str,
) -> rusqlite::Result<Option<RevocationRow>> {
    tx.query_row(
        "SELECT revocation_id, scope, key_id, material_digest, purpose, revoked_at, actor, \
         reason, ledger_seq FROM key_revocations WHERE scope = ?1 AND material_digest = ?2",
        (scope, digest),
        |r| {
            Ok(RevocationRow {
                revocation_id: r.get(0)?,
                scope: r.get(1)?,
                key_id: r.get(2)?,
                material_digest: r.get(3)?,
                purpose: r.get(4)?,
                revoked_at: r.get(5)?,
                actor: r.get(6)?,
                reason: r.get(7)?,
                ledger_seq: r.get(8)?,
                created: false,
            })
        },
    )
    .optional()
}

/// Insert one revocation and its sequence row inside `tx`.
#[allow(clippy::too_many_arguments)]
fn insert(
    tx: &Transaction<'_>,
    scope: &str,
    key_id: Option<&str>,
    digest: &str,
    purpose: &str,
    at: &str,
    actor: &str,
    reason: &str,
) -> rusqlite::Result<RevocationRow> {
    tx.execute(
        "INSERT INTO ledger_sequence (kind, reference) VALUES ('key_revocation', 'pending')",
        [],
    )?;
    let seq = tx.last_insert_rowid();
    tx.execute(
        "INSERT INTO key_revocations (scope, key_id, material_digest, purpose, revoked_at, \
         actor, reason, ledger_seq) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
        (scope, key_id, digest, purpose, at, actor, reason, seq),
    )?;
    let id = tx.last_insert_rowid();
    tx.execute(
        "UPDATE ledger_sequence SET reference = ?1 WHERE seq = ?2",
        (format!("key_revocation/{id}"), seq),
    )?;
    Ok(RevocationRow {
        revocation_id: id,
        scope: scope.to_string(),
        key_id: key_id.map(str::to_string),
        material_digest: digest.to_string(),
        purpose: purpose.to_string(),
        revoked_at: at.to_string(),
        actor: actor.to_string(),
        reason: reason.to_string(),
        ledger_seq: seq,
        created: true,
    })
}

impl SqliteLedger {
    /// Record an operator key revocation (`admin.sock` `keys.revoke`, J-11.8; module
    /// documentation). `at` is trusted time; `actor` the authenticated OS peer.
    pub fn revoke_key(
        &self,
        scope: &RevocationScope,
        reason: &str,
        actor: &OsActor,
        at: Timestamp,
    ) -> Result<Result<RevocationRow, RevokeRefusal>, StoreError> {
        if !reason_valid(reason) {
            return Ok(Err(RevokeRefusal::Reason));
        }
        let actor = actor
            .document()
            .ok_or_else(|| crate::unavailable("key_revoke", "actor"))?;
        let at = at.to_string();
        let purpose = scope.purpose().as_str();
        self.transact("key_revoke", |tx| {
            let (key_id, digest) = match scope {
                RevocationScope::KeyId { key_id, .. } => {
                    match registered_by_id(tx, key_id.as_str())? {
                        None => return Ok(End::Rollback(Err(RevokeRefusal::Unregistered))),
                        Some((p, _)) if p != purpose => {
                            return Ok(End::Rollback(Err(RevokeRefusal::PurposeMismatch)))
                        }
                        Some((_, d)) => (Some(key_id.as_str().to_string()), d),
                    }
                }
                RevocationScope::Material { digest, .. } => {
                    match registered_by_digest(tx, digest.as_str())? {
                        Some((_, p)) if p != purpose => {
                            return Ok(End::Rollback(Err(RevokeRefusal::PurposeMismatch)))
                        }
                        Some((id, _)) => (Some(id), digest.as_str().to_string()),
                        None => (None, digest.as_str().to_string()),
                    }
                }
            };
            // M5a-13 L9: material already revoked under another purpose (a pre-emptive
            // revocation of an unregistered digest, or any earlier row) refuses; an identical
            // revocation returns the existing row.
            if other_purpose_revoked(tx, &digest, purpose)? {
                return Ok(End::Rollback(Err(RevokeRefusal::PurposeMismatch)));
            }
            if let Some(existing) = read_row(tx, scope.name(), &digest)? {
                return Ok(End::Rollback(Ok(existing)));
            }
            let row = insert(
                tx,
                scope.name(),
                key_id.as_deref(),
                &digest,
                purpose,
                &at,
                &actor,
                reason,
            )?;
            Ok(End::Commit(Ok(row)))
        })
        .map_err(StoreError::from)
    }

    /// Every recorded key revocation, in commit order.
    pub fn key_revocations(&self) -> Result<Vec<RevocationRow>, StoreError> {
        self.transact("key_revocation_list", |tx| {
            let mut stmt = tx.prepare(
                "SELECT scope, material_digest FROM key_revocations ORDER BY revocation_id",
            )?;
            let keys = stmt
                .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))?
                .collect::<rusqlite::Result<Vec<_>>>()?;
            drop(stmt);
            let mut rows = Vec::with_capacity(keys.len());
            for (scope, digest) in keys {
                rows.push(read_row(tx, &scope, &digest)?.ok_or(BodyError::Integrity)?);
            }
            Ok(End::Rollback(rows))
        })
        .map_err(StoreError::from)
    }
}

impl KeyRevocations for SqliteLedger {
    fn is_revoked(&self, id: &KeyId, material: &MaterialDigest) -> Result<bool, KeysUnavailable> {
        self.transact("key_revocation_lookup", |tx| {
            Ok(End::Rollback(revoked(
                tx,
                Some(id.as_str()),
                material.as_str(),
            )?))
        })
        .map_err(|_| KeysUnavailable)
    }
}

impl KeyRevocations for &SqliteLedger {
    fn is_revoked(&self, id: &KeyId, material: &MaterialDigest) -> Result<bool, KeysUnavailable> {
        (**self).is_revoked(id, material)
    }
}

impl KeyRegistry for &SqliteLedger {
    fn admit(
        &mut self,
        id: &KeyId,
        purpose: KeyPurpose,
        material: &MaterialDigest,
    ) -> Result<Admission, KeysUnavailable> {
        self.transact("key_admit", |tx| {
            if revoked(tx, Some(id.as_str()), material.as_str())? {
                return Ok(End::Rollback(Admission::Revoked));
            }
            if let Some((p, d)) = registered_by_id(tx, id.as_str())? {
                let same = p == purpose.as_str() && d == material.as_str();
                return Ok(End::Rollback(if same {
                    Admission::Admitted
                } else {
                    Admission::IdConflict
                }));
            }
            if registered_by_digest(tx, material.as_str())?.is_some() {
                return Ok(End::Rollback(Admission::MaterialConflict));
            }
            if id.purpose() != purpose {
                return Ok(End::Rollback(Admission::IdConflict));
            }
            tx.execute(
                "INSERT INTO key_registry (key_id, purpose, material_digest) VALUES (?1, ?2, ?3)",
                (id.as_str(), purpose.as_str(), material.as_str()),
            )?;
            Ok(End::Commit(Admission::Admitted))
        })
        .map_err(|_| KeysUnavailable)
    }

    /// Record `revocation` of a registered key by id (the `FileKeySource::revoke` path):
    /// the id must be registered with the revocation's purpose and material, otherwise the
    /// registry is reported unavailable and nothing is recorded. `actor` must be an
    /// [`OsActor`] document (M5a-13 L9); the `admin.sock` path is
    /// [`SqliteLedger::revoke_key`].
    fn revoke(&mut self, revocation: KeyRevocation) -> Result<(), KeysUnavailable> {
        // M5a-13 L9: the actor is the structured OS-peer document, never free text.
        if !reason_valid(&revocation.reason) || OsActor::from_document(&revocation.actor).is_none()
        {
            return Err(KeysUnavailable);
        }
        let at = revocation.revoked_at.to_string();
        let ok = self
            .transact("key_revoke", |tx| {
                match registered_by_id(tx, revocation.key_id.as_str())? {
                    Some((p, d))
                        if p == revocation.purpose.as_str()
                            && d == revocation.material.as_str() => {}
                    _ => return Ok(End::Rollback(false)),
                }
                if other_purpose_revoked(
                    tx,
                    revocation.material.as_str(),
                    revocation.purpose.as_str(),
                )? {
                    return Ok(End::Rollback(false));
                }
                if read_row(tx, "key_id", revocation.material.as_str())?.is_some() {
                    return Ok(End::Rollback(true));
                }
                insert(
                    tx,
                    "key_id",
                    Some(revocation.key_id.as_str()),
                    revocation.material.as_str(),
                    revocation.purpose.as_str(),
                    &at,
                    &revocation.actor,
                    &revocation.reason,
                )?;
                Ok(End::Commit(true))
            })
            .map_err(|_| KeysUnavailable)?;
        if ok {
            Ok(())
        } else {
            Err(KeysUnavailable)
        }
    }
}
