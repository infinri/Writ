//! The transition service's ledger transactions (M3-05, D-036, schema `writ-ledger-5`; since
//! `writ-ledger-6` each `admin` and `promote` commit also takes one I-8 `ledger_sequence` row,
//! `promote` reads every signoff the record names and consumes the checked ones atomically
//! (M4-06), and `signoff_request` stores promotion contexts (X7)):
//! [`WorkflowLedger`] for [`SqliteLedger`] and the effect-prerequisite read used inside
//! T-reserve. Every write is one fenced `BEGIN IMMEDIATE` transaction; the semantics are
//! the broker's (`writ_broker::workflow`), the store reads rows, hands them to the broker's
//! checks inside the transaction and writes what they decide.

use std::collections::BTreeSet;

use json::{Digest, Value};
use rusqlite::{OptionalExtension, Transaction};
use writ_broker::acceptance::AcceptanceCode;
use writ_broker::clock::timestamp_from_parts;
use writ_broker::contracts::RegistryEntry;
use writ_broker::grants::{GrantCheck, PrerequisiteView, ReserveRequest};
use writ_broker::keys::MaterialDigest;
use writ_broker::refusal::Refusal;
use writ_broker::store::{StoreError, TxFailure};
use writ_broker::workflow::{
    AdminChange, AdminRequest, LastPromotion, PromotionIntent, PromotionOutcome, PromotionView,
    SignoffRequest, StoredEntry, StoredPolicy, StoredWorkflow, WorkflowKey, WorkflowLedger,
};
use writ_broker::writ_kernel::{ids, json};

use crate::approval_store::{consume_signoffs, next_seq, signoff_use};
use crate::{code_name, stored_grant, unavailable, BodyError, End, SqliteLedger};

pub(crate) fn read_workflow(
    conn: &rusqlite::Connection,
    tenant: &str,
    project: &str,
    workflow: &str,
) -> rusqlite::Result<Option<StoredWorkflow>> {
    conn.query_row(
        "SELECT task_id, state, revision, artifact_snapshot_id, artifact_digest, \
         last_receipt_id, last_record_id, last_snapshot_id, last_artifact_digest, \
         last_acceptance_sha, last_authorization_sha \
         FROM workflows WHERE tenant_id = ?1 AND project_id = ?2 AND workflow_id = ?3",
        (tenant, project, workflow),
        |r| {
            let last: [Option<String>; 6] = [
                r.get(5)?,
                r.get(6)?,
                r.get(7)?,
                r.get(8)?,
                r.get(9)?,
                r.get(10)?,
            ];
            let last_promotion = match last {
                [Some(receipt_id), Some(record_id), Some(s), Some(d), Some(a), Some(z)] => {
                    Some(LastPromotion {
                        receipt_id,
                        record_id,
                        artifact_snapshot_id: s,
                        artifact_digest: d,
                        acceptance_policy_bundle_sha: a,
                        authorization_bundle_sha: z,
                    })
                }
                _ => None,
            };
            Ok(StoredWorkflow {
                task_id: r.get(0)?,
                state: r.get(1)?,
                revision: r.get(2)?,
                artifact_snapshot_id: r.get(3)?,
                artifact_digest: r.get(4)?,
                last_promotion,
            })
        },
    )
    .optional()
}

fn read_policy(
    tx: &Transaction<'_>,
    tenant: &str,
    project: &str,
) -> rusqlite::Result<Option<(StoredPolicy, String)>> {
    let row: Option<(String, String)> = tx
        .query_row(
            "SELECT requirements, bundle_sha FROM acceptance_policies \
             WHERE tenant_id = ?1 AND project_id = ?2",
            (tenant, project),
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .optional()?;
    let Some((requirements, sha)) = row else {
        return Ok(None);
    };
    let mut stmt = tx.prepare(
        "SELECT effect_kind, state FROM effect_prerequisites \
         WHERE tenant_id = ?1 AND project_id = ?2 ORDER BY effect_kind, state",
    )?;
    let rows = stmt
        .query_map((tenant, project), |r| {
            Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    let mut prerequisites: Vec<(String, Vec<String>)> = Vec::new();
    for (kind, state) in rows {
        match prerequisites.last_mut() {
            Some((k, states)) if *k == kind => states.push(state),
            _ => prerequisites.push((kind, vec![state])),
        }
    }
    Ok(Some((
        StoredPolicy {
            requirements,
            effect_prerequisites: prerequisites,
        },
        sha,
    )))
}

/// Effect prerequisites inside T-reserve: `Ok(())` when the project's active acceptance
/// policy lists no prerequisite for the request's kind, else the broker's check.
pub(crate) fn check_prerequisite(
    tx: &Transaction<'_>,
    request: &ReserveRequest<'_>,
) -> Result<Result<(), Refusal>, BodyError> {
    let effect = request.effect();
    let mut stmt = tx.prepare(
        "SELECT state FROM effect_prerequisites \
         WHERE tenant_id = ?1 AND project_id = ?2 AND effect_kind = ?3 ORDER BY state",
    )?;
    let required_states = stmt
        .query_map((request.tenant_id(), effect.project_id, effect.kind), |r| {
            r.get(0)
        })?
        .collect::<rusqlite::Result<Vec<String>>>()?;
    if required_states.is_empty() {
        return Ok(Ok(()));
    }
    let sha: Option<String> = tx
        .query_row(
            "SELECT bundle_sha FROM acceptance_policies WHERE tenant_id = ?1 AND project_id = ?2",
            (request.tenant_id(), effect.project_id),
            |r| r.get(0),
        )
        .optional()?;
    let workflow = match effect.workflow_id {
        Some(w) => read_workflow(tx, request.tenant_id(), effect.project_id, w)?,
        None => None,
    };
    Ok(request.check_prerequisite(&PrerequisiteView {
        required_states,
        acceptance_policy_bundle_sha: sha,
        workflow,
    }))
}

/// The durable outcome of an authenticated evaluator publication or revocation.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct EvaluatorRegistryCommit {
    /// Scoped registry revision after this mutation.
    pub revision: i64,
    /// The operation's position in the ledger-wide commit order.
    pub sequence: i64,
}

impl SqliteLedger {
    /// Read one evaluator and its scoped revision from the authenticated registry.
    pub fn evaluator_registry_view(
        &self,
        tenant: &str,
        project: &str,
        evaluator: &str,
    ) -> Result<(i64, Option<RegistryEntry>), StoreError> {
        if !ids::is_id(tenant) || !ids::is_id(project) || !ids::is_name(evaluator) {
            return Err(unavailable("evaluator registry", "scope"));
        }
        self.transact("evaluator registry read", |tx| {
            let prefix = scope_prefix(tenant, project);
            let revision = secure_head(tx, tenant, project, &prefix)?;
            let row = read_registry_row(tx, tenant, project, evaluator)?;
            let entry = validate_registry_row(tx, &prefix, evaluator, row.as_ref())?;
            if let Some(validated) = entry.as_ref().filter(|entry| entry.active()) {
                if validated.key_revoked(tx)? {
                    return Err(BodyError::Provenance);
                }
            }
            let entry = entry.map(|validated| validated.entry);
            Ok(End::Rollback((revision, entry)))
        })
        .map_err(|failure| failure.error)
    }

    /// After an uncertain write, read the outcome for the exact authorized intent.
    pub fn evaluator_publication_commit(
        &self,
        tenant: &str,
        project: &str,
        intent_sha: &str,
    ) -> Result<Option<EvaluatorRegistryCommit>, StoreError> {
        if !ids::is_id(tenant) || !ids::is_id(project) || !ids::is_digest(intent_sha) {
            return Err(unavailable("evaluator registry", "intent"));
        }
        self.transact("evaluator registry receipt", |tx| {
            let prefix = scope_prefix(tenant, project);
            let head = secure_head(tx, tenant, project, &prefix)?;
            let digest_len = i64::try_from(intent_sha.len()).map_err(|_| BodyError::Integrity)?;
            let matching = tx
                .query_row(
                    "SELECT seq, reference FROM ledger_sequence WHERE kind = 'administration' \
                     AND substr(reference, 1, length(?1)) = ?1 \
                     AND substr(reference, -(3 * ?3 + 2), ?3) = ?2 LIMIT 1",
                    (&prefix, intent_sha, digest_len),
                    |r| Ok((r.get(0)?, r.get::<_, String>(1)?)),
                )
                .optional()?;
            let Some((sequence, reference)) = matching else {
                return Ok(End::Rollback(None));
            };
            let audit = registry_audit(tx, &prefix, sequence, &reference)?;
            if audit.revision > head || audit.intent_sha != intent_sha {
                return Err(BodyError::Integrity);
            }
            Ok(End::Rollback(Some(EvaluatorRegistryCommit {
                revision: audit.revision,
                sequence,
            })))
        })
        .map_err(|failure| failure.error)
    }

    pub(crate) fn registry_entry_for_runner(
        &self,
        tenant: &str,
        project: &str,
        evaluator: &str,
    ) -> Result<Option<StoredEntry>, StoreError> {
        self.transact("runner registry read", |tx| {
            let prefix = scope_prefix(tenant, project);
            let row = read_registry_row(tx, tenant, project, evaluator)?;
            if secure_scope_exists(tx, &prefix)? {
                secure_head(tx, tenant, project, &prefix)?;
                if let Some(validated) =
                    validate_registry_row(tx, &prefix, evaluator, row.as_ref())?
                        .filter(ValidatedRegistryEntry::active)
                {
                    if validated.key_revoked(tx)? {
                        return Err(BodyError::Provenance);
                    }
                }
            }
            Ok(End::Rollback(row.map(RegistryRow::stored)))
        })
        .map_err(|failure| failure.error)
    }
}

const PUBLICATION_PREFIX: &str = "evaluator-publication:";

fn scope_prefix(tenant: &str, project: &str) -> String {
    format!("{PUBLICATION_PREFIX}{tenant}/{project}/")
}

struct RegistryRow {
    revision: i64,
    entry: String,
    status: String,
}

impl RegistryRow {
    fn stored(self) -> StoredEntry {
        StoredEntry {
            entry: self.entry,
            revoked: self.status != "active",
        }
    }
}

fn read_registry_row(
    tx: &Transaction<'_>,
    tenant: &str,
    project: &str,
    evaluator: &str,
) -> rusqlite::Result<Option<RegistryRow>> {
    tx.query_row(
        "SELECT registry_revision, entry, status FROM evaluator_registry \
         WHERE tenant_id = ?1 AND project_id = ?2 AND evaluator_id = ?3",
        (tenant, project, evaluator),
        |r| {
            Ok(RegistryRow {
                revision: r.get(0)?,
                entry: r.get(1)?,
                status: r.get(2)?,
            })
        },
    )
    .optional()
}

struct RegistryAudit<'a> {
    evaluator: &'a str,
    revision: i64,
    intent_sha: &'a str,
    entry_sha: &'a str,
    runner_digest: MaterialDigest,
    status: &'static str,
}

fn registry_audit<'a>(
    tx: &Transaction<'_>,
    prefix: &str,
    sequence: i64,
    reference: &'a str,
) -> Result<RegistryAudit<'a>, BodyError> {
    let raw = reference.strip_prefix(prefix).ok_or(BodyError::Integrity)?;
    let parts: Vec<_> = raw.splitn(6, '/').collect();
    let [evaluator, revision, runner_sha, intent_sha, entry_sha, ceremony_sha] = parts.as_slice()
    else {
        return Err(BodyError::Integrity);
    };
    let revision: i64 = revision.parse().map_err(|_| BodyError::Integrity)?;
    if !ids::is_name(evaluator)
        || revision < 1
        || ![intent_sha, entry_sha, ceremony_sha]
            .into_iter()
            .all(|sha| ids::is_digest(sha))
    {
        return Err(BodyError::Integrity);
    }
    let runner_digest = MaterialDigest::parse(runner_sha).ok_or(BodyError::Integrity)?;
    let Some((tenant, project)) = prefix
        .strip_prefix(PUBLICATION_PREFIX)
        .and_then(|p| p.strip_suffix('/'))
        .and_then(|p| p.split_once('/'))
    else {
        return Err(BodyError::Integrity);
    };
    let operation: Option<String> = tx
        .query_row(
            "SELECT operation FROM admin_events WHERE tenant_id = ?1 AND project_id = ?2 \
             AND target = ?3 AND operator_id <> '' AND operator_issuer <> '' AND channel <> '' \
             AND evidence_ref <> '' AND at <> ''",
            (tenant, project, reference),
            |r| r.get(0),
        )
        .optional()?;
    let status = match operation.as_deref() {
        Some("register_evaluator") => "active",
        Some("revoke_evaluator") => "revoked",
        _ => return Err(BodyError::Provenance),
    };
    if sequence < 1 {
        return Err(BodyError::Integrity);
    }
    Ok(RegistryAudit {
        evaluator,
        revision,
        intent_sha,
        entry_sha,
        runner_digest,
        status,
    })
}

fn secure_scope_exists(tx: &Transaction<'_>, prefix: &str) -> rusqlite::Result<bool> {
    tx.query_row(
        "SELECT EXISTS (SELECT 1 FROM meta WHERE key = ?1) \
         OR EXISTS (SELECT 1 FROM ledger_sequence WHERE kind = 'administration' \
         AND substr(reference, 1, length(?1)) = ?1) OR EXISTS (SELECT 1 FROM admin_events \
         WHERE substr(target, 1, length(?1)) = ?1)",
        [prefix],
        |r| r.get(0),
    )
}

fn latest_registry_audit(
    tx: &Transaction<'_>,
    prefix: &str,
) -> rusqlite::Result<Option<(i64, String)>> {
    tx.query_row(
        "SELECT seq, reference FROM ledger_sequence WHERE kind = 'administration' \
         AND substr(reference, 1, length(?1)) = ?1 ORDER BY seq DESC LIMIT 1",
        [prefix],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )
    .optional()
}

fn secure_head(
    tx: &Transaction<'_>,
    tenant: &str,
    project: &str,
    prefix: &str,
) -> Result<i64, BodyError> {
    let marker: Option<String> = tx
        .query_row("SELECT value FROM meta WHERE key = ?1", [prefix], |r| {
            r.get(0)
        })
        .optional()?;
    let revision: Option<i64> = tx.query_row(
        "SELECT max(registry_revision) FROM evaluator_registry \
         WHERE tenant_id = ?1 AND project_id = ?2",
        (tenant, project),
        |r| r.get(0),
    )?;
    let admin_target: Option<String> = tx
        .query_row(
            "SELECT target FROM admin_events WHERE tenant_id = ?1 AND project_id = ?2 \
             AND substr(target, 1, length(?3)) = ?3 ORDER BY seq DESC LIMIT 1",
            (tenant, project, prefix),
            |r| r.get(0),
        )
        .optional()?;
    match (
        marker,
        revision,
        admin_target,
        latest_registry_audit(tx, prefix)?,
    ) {
        (None, None, None, None) => Ok(0),
        (Some(marker), Some(revision), Some(target), Some((sequence, reference)))
            if target == reference && marker == revision.to_string() =>
        {
            let audit = registry_audit(tx, prefix, sequence, &reference)?;
            if audit.revision != revision {
                return Err(BodyError::Integrity);
            }
            let row = read_registry_row(tx, tenant, project, audit.evaluator)?;
            validate_registry_row(tx, prefix, audit.evaluator, row.as_ref())?;
            Ok(revision)
        }
        _ => Err(BodyError::Integrity),
    }
}

struct ValidatedRegistryEntry {
    entry: RegistryEntry,
    runner_digest: MaterialDigest,
}

impl ValidatedRegistryEntry {
    fn active(&self) -> bool {
        self.entry.as_value().get("status").and_then(Value::as_str) == Some("active")
    }

    fn key_revoked(&self, tx: &Transaction<'_>) -> Result<bool, BodyError> {
        let (id, digest) = self.runner_key()?;
        crate::keys::revoked_text_in(tx, id, digest)
    }

    fn runner_key(&self) -> Result<(&str, &MaterialDigest), BodyError> {
        let id = self
            .entry
            .as_value()
            .get("attestation")
            .and_then(|v| v.get("key_id"))
            .and_then(Value::as_str)
            .ok_or(BodyError::Integrity)?;
        Ok((id, &self.runner_digest))
    }
}

fn validate_registry_row(
    tx: &Transaction<'_>,
    prefix: &str,
    evaluator: &str,
    row: Option<&RegistryRow>,
) -> Result<Option<ValidatedRegistryEntry>, BodyError> {
    let latest = latest_registry_audit(tx, &format!("{prefix}{evaluator}/"))?;
    let (row, sequence, reference) = match (row, latest) {
        (None, None) => return Ok(None),
        (Some(row), Some((sequence, reference))) => (row, sequence, reference),
        _ => return Err(BodyError::Integrity),
    };
    let audit = registry_audit(tx, prefix, sequence, &reference)?;
    if audit.revision != row.revision
        || audit.status != row.status
        || audit.entry_sha != Digest::of_bytes(row.entry.as_bytes()).as_str()
    {
        return Err(BodyError::Integrity);
    }
    let value = json::parse(row.entry.as_bytes()).map_err(|_| BodyError::Integrity)?;
    if value.get("registry_revision").and_then(Value::as_int) != Some(row.revision)
        || value.get("status").and_then(Value::as_str) != Some(row.status.as_str())
        || value.get("evaluator_id").and_then(Value::as_str) != Some(evaluator)
    {
        return Err(BodyError::Integrity);
    }
    let entry = RegistryEntry::validate(&value).map_err(|_| BodyError::Integrity)?;
    Ok(Some(ValidatedRegistryEntry {
        entry,
        runner_digest: audit.runner_digest,
    }))
}

fn mutate_evaluator(
    tx: &Transaction<'_>,
    change: &AdminChange<'_>,
    tenant: &str,
    project: &str,
    evaluator: &str,
    publication: Option<&Value>,
) -> Result<End<Result<(), Refusal>>, BodyError> {
    let authorized = change.publisher().ok_or(BodyError::Integrity)?;
    let prefix = scope_prefix(tenant, project);
    let head = secure_head(tx, tenant, project, &prefix)?;
    if head != authorized.expected_revision() {
        return Ok(End::Rollback(Err(Refusal::RegistryRevisionStale)));
    }
    let revision = head.checked_add(1).ok_or(BodyError::Integrity)?;
    let ceremony_sha = Digest::of_bytes(authorized.ceremony().as_bytes());
    let used: bool = tx.query_row(
        "SELECT EXISTS (SELECT 1 FROM ledger_sequence WHERE kind = 'administration' \
         AND substr(reference, 1, length(?1)) = ?1 \
         AND substr(reference, -length(?2)) = ?2)",
        (PUBLICATION_PREFIX, ceremony_sha.as_str()),
        |r| r.get(0),
    )?;
    if used {
        return Ok(End::Rollback(Err(Refusal::OperatorUnauthorized)));
    }
    let row = read_registry_row(tx, tenant, project, evaluator)?;
    let previous = validate_registry_row(tx, &prefix, evaluator, row.as_ref())?;
    let (value, status, runner_digest) = if let Some(entry) = publication {
        if change.registry_revision() != Some(revision) {
            return Ok(End::Rollback(Err(Refusal::RegistryRevisionStale)));
        }
        let (key, digest) = authorized.runner_key().ok_or(BodyError::Integrity)?;
        if crate::keys::revoked_text_in(tx, key, digest)? {
            return Ok(End::Rollback(Err(Refusal::Acceptance(
                AcceptanceCode::AttestationUnauthenticated,
            ))));
        }
        (entry.clone(), "active", digest.clone())
    } else {
        let Some(previous) = previous else {
            return Ok(End::Rollback(Err(Refusal::Acceptance(
                AcceptanceCode::EvaluatorUnregistered,
            ))));
        };
        let Value::Obj(mut doc) = previous.entry.as_value().clone() else {
            return Err(BodyError::Integrity);
        };
        if doc.get("status").and_then(Value::as_str) != Some("active") {
            return Ok(End::Rollback(Err(Refusal::Acceptance(
                AcceptanceCode::EvaluatorRevoked,
            ))));
        }
        doc.insert("status".into(), json::s("revoked"));
        doc.insert("registry_revision".into(), Value::Int(revision));
        let revoked_at =
            timestamp_from_parts(change.at().unix_seconds(), 0).ok_or(BodyError::Integrity)?;
        doc.insert("revoked_at".into(), json::s(&revoked_at.to_string()));
        let value = Value::Obj(doc);
        RegistryEntry::validate(&value).map_err(|_| BodyError::Integrity)?;
        (value, "revoked", previous.runner_digest)
    };
    let entry = json::canonical(&value).map_err(|_| BodyError::Integrity)?;
    let entry_sha = Digest::of_bytes(entry.as_bytes());
    let intent_sha = authorized.intent_sha();
    let reference = format!(
        "{prefix}{evaluator}/{revision}/{runner_digest}/{intent_sha}/{entry_sha}/{ceremony_sha}"
    );
    tx.execute(
        "INSERT INTO evaluator_registry (tenant_id, project_id, evaluator_id, \
         registry_revision, entry, status) VALUES (?1, ?2, ?3, ?4, ?5, ?6) \
         ON CONFLICT (tenant_id, project_id, evaluator_id) DO UPDATE SET \
         registry_revision = excluded.registry_revision, entry = excluded.entry, \
         status = excluded.status",
        (tenant, project, evaluator, revision, entry, status),
    )?;
    tx.execute(
        "INSERT INTO meta (key, value) VALUES (?1, ?2) \
         ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (&prefix, revision.to_string()),
    )?;
    admin_provenance(tx, change, tenant, project, &reference)?;
    next_seq(tx, "administration", &reference)?;
    if let Err(refusal) = authorized.recheck() {
        return Ok(End::Rollback(Err(refusal)));
    }
    Ok(End::Commit(Ok(())))
}

impl WorkflowLedger for SqliteLedger {
    fn workflow(&self, key: &WorkflowKey) -> Result<Option<StoredWorkflow>, StoreError> {
        let inner = self.lock("read")?;
        read_workflow(
            &inner.conn,
            &key.tenant_id,
            &key.project_id,
            &key.workflow_id,
        )
        .map_err(|e| unavailable("read", code_name(&e)))
    }

    fn promotion_receipt(
        &self,
        tenant_id: &str,
        idempotency_key: &str,
    ) -> Result<Option<(String, PromotionOutcome)>, StoreError> {
        let inner = self.lock("read")?;
        inner
            .conn
            .query_row(
                "SELECT fingerprint, receipt_id, revision FROM promotion_receipts \
                 WHERE tenant_id = ?1 AND idempotency_key = ?2",
                (tenant_id, idempotency_key),
                |r| {
                    Ok((
                        r.get::<_, String>(0)?,
                        PromotionOutcome::Replayed {
                            receipt_id: r.get(1)?,
                            revision: r.get(2)?,
                        },
                    ))
                },
            )
            .optional()
            .map_err(|e| unavailable("read", code_name(&e)))
    }

    fn publish_evaluator(
        &self,
        change: &AdminChange<'_>,
    ) -> Result<Result<(), Refusal>, TxFailure> {
        if change.publisher().is_none() {
            return Ok(Err(Refusal::OperatorUnauthorized));
        }
        self.administer(change)
    }

    fn administer(&self, change: &AdminChange) -> Result<Result<(), Refusal>, TxFailure> {
        self.transact("admin", |tx| {
            let (tenant, project, target) = match change.request() {
                AdminRequest::CreateWorkflow {
                    key,
                    task_id,
                    initial_state,
                    artifact_snapshot_id,
                    artifact_digest,
                } => {
                    if read_workflow(tx, &key.tenant_id, &key.project_id, &key.workflow_id)?
                        .is_some()
                    {
                        return Ok(End::Rollback(Err(Refusal::WorkflowExists)));
                    }
                    tx.execute(
                        "INSERT INTO workflows (tenant_id, project_id, workflow_id, task_id, \
                         state, revision, artifact_snapshot_id, artifact_digest) \
                         VALUES (?1, ?2, ?3, ?4, ?5, 0, ?6, ?7)",
                        (
                            &key.tenant_id,
                            &key.project_id,
                            &key.workflow_id,
                            task_id,
                            initial_state,
                            artifact_snapshot_id,
                            artifact_digest,
                        ),
                    )?;
                    workflow_event(tx, key, "create", None, initial_state, 0, None, None)?;
                    (&key.tenant_id, &key.project_id, key.workflow_id.clone())
                }
                AdminRequest::UpdateArtifact {
                    key,
                    artifact_snapshot_id,
                    artifact_digest,
                } => {
                    let Some(w) =
                        read_workflow(tx, &key.tenant_id, &key.project_id, &key.workflow_id)?
                    else {
                        return Ok(End::Rollback(Err(Refusal::WorkflowUnknown)));
                    };
                    let revision = w.revision.checked_add(1).ok_or(BodyError::Integrity)?;
                    let changed = tx.execute(
                        "UPDATE workflows SET artifact_snapshot_id = ?4, artifact_digest = ?5, \
                         revision = ?6 WHERE tenant_id = ?1 AND project_id = ?2 \
                         AND workflow_id = ?3 AND revision = ?7",
                        (
                            &key.tenant_id,
                            &key.project_id,
                            &key.workflow_id,
                            artifact_snapshot_id,
                            artifact_digest,
                            revision,
                            w.revision,
                        ),
                    )?;
                    if changed != 1 {
                        return Err(BodyError::StateConflict);
                    }
                    workflow_event(
                        tx,
                        key,
                        "artifact",
                        Some(&w.state),
                        &w.state,
                        revision,
                        None,
                        None,
                    )?;
                    (&key.tenant_id, &key.project_id, key.workflow_id.clone())
                }
                AdminRequest::ActivateAcceptancePolicy {
                    tenant_id,
                    project_id,
                    requirements,
                    effect_prerequisites,
                } => {
                    let text = change.requirements_text().ok_or(BodyError::Integrity)?;
                    let sha = requirements
                        .get("acceptance_policy_bundle_sha")
                        .and_then(|v| v.as_str())
                        .ok_or(BodyError::Integrity)?;
                    tx.execute(
                        "INSERT INTO acceptance_policies (tenant_id, project_id, requirements, \
                         bundle_sha) VALUES (?1, ?2, ?3, ?4) ON CONFLICT (tenant_id, project_id) \
                         DO UPDATE SET requirements = excluded.requirements, \
                         bundle_sha = excluded.bundle_sha",
                        (tenant_id, project_id, text, sha),
                    )?;
                    tx.execute(
                        "DELETE FROM effect_prerequisites WHERE tenant_id = ?1 AND project_id = ?2",
                        (tenant_id, project_id),
                    )?;
                    for (kind, states) in effect_prerequisites {
                        for state in states {
                            tx.execute(
                                "INSERT OR IGNORE INTO effect_prerequisites (tenant_id, \
                                 project_id, effect_kind, state) VALUES (?1, ?2, ?3, ?4)",
                                (tenant_id, project_id, kind, state),
                            )?;
                        }
                    }
                    let version = requirements
                        .get("acceptance_policy_version")
                        .and_then(|v| v.as_str())
                        .unwrap_or("");
                    let id = requirements
                        .get("acceptance_policy_id")
                        .and_then(|v| v.as_str())
                        .unwrap_or("");
                    (tenant_id, project_id, format!("{id}/{version}"))
                }
                AdminRequest::RegisterEvaluator { entry } => {
                    let s = |path: &[&str]| {
                        let mut v = entry;
                        for k in path {
                            v = v.get(k)?;
                        }
                        v.as_str()
                    };
                    let (Some(tenant), Some(project), Some(evaluator)) = (
                        s(&["scope", "tenant_id"]),
                        s(&["scope", "project_id"]),
                        s(&["evaluator_id"]),
                    ) else {
                        return Err(BodyError::Integrity);
                    };
                    if change.publisher().is_some() {
                        return mutate_evaluator(
                            tx,
                            change,
                            tenant,
                            project,
                            evaluator,
                            Some(entry),
                        );
                    }
                    if secure_scope_exists(tx, &scope_prefix(tenant, project))? {
                        return Ok(End::Rollback(Err(Refusal::OperatorUnauthorized)));
                    }
                    let revision = change.registry_revision().ok_or(BodyError::Integrity)?;
                    let existing: Option<i64> = tx
                        .query_row(
                            "SELECT registry_revision FROM evaluator_registry \
                             WHERE tenant_id = ?1 AND project_id = ?2 AND evaluator_id = ?3",
                            (tenant, project, evaluator),
                            |r| r.get(0),
                        )
                        .optional()?;
                    if existing.is_some_and(|r| r >= revision) {
                        return Ok(End::Rollback(Err(Refusal::RegistryRevisionStale)));
                    }
                    let text = json::canonical(entry).map_err(|_| BodyError::Integrity)?;
                    tx.execute(
                        "INSERT INTO evaluator_registry (tenant_id, project_id, evaluator_id, \
                         registry_revision, entry, status) VALUES (?1, ?2, ?3, ?4, ?5, 'active') \
                         ON CONFLICT (tenant_id, project_id, evaluator_id) DO UPDATE SET \
                         registry_revision = excluded.registry_revision, entry = excluded.entry, \
                         status = 'active'",
                        (tenant, project, evaluator, revision, text),
                    )?;
                    return admin_event(tx, change, tenant, project, evaluator)
                        .map(|()| End::Commit(Ok(())));
                }
                AdminRequest::RevokeEvaluator {
                    tenant_id,
                    project_id,
                    evaluator_id,
                } => {
                    if change.publisher().is_some() {
                        return mutate_evaluator(
                            tx,
                            change,
                            tenant_id,
                            project_id,
                            evaluator_id,
                            None,
                        );
                    }
                    if secure_scope_exists(tx, &scope_prefix(tenant_id, project_id))? {
                        return Ok(End::Rollback(Err(Refusal::OperatorUnauthorized)));
                    }
                    let changed = tx.execute(
                        "UPDATE evaluator_registry SET status = 'revoked' \
                         WHERE tenant_id = ?1 AND project_id = ?2 AND evaluator_id = ?3",
                        (tenant_id, project_id, evaluator_id),
                    )?;
                    if changed != 1 {
                        return Ok(End::Rollback(Err(Refusal::Acceptance(
                            writ_broker::acceptance::AcceptanceCode::EvaluatorUnregistered,
                        ))));
                    }
                    (tenant_id, project_id, evaluator_id.clone())
                }
            };
            admin_event(tx, change, tenant, project, &target)?;
            Ok(End::Commit(Ok(())))
        })
    }

    fn promote(
        &self,
        intent: &PromotionIntent<'_>,
    ) -> Result<Result<PromotionOutcome, Refusal>, TxFailure> {
        let request = intent.request();
        let key = intent.key();
        self.transact("promote", |tx| {
            let receipt: Option<(String, String, i64)> = tx
                .query_row(
                    "SELECT fingerprint, receipt_id, revision FROM promotion_receipts \
                     WHERE tenant_id = ?1 AND idempotency_key = ?2",
                    (request.tenant_id(), request.idempotency_key()),
                    |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
                )
                .optional()?;
            if let Some((fingerprint, receipt_id, revision)) = receipt {
                return Ok(End::Rollback(if fingerprint == request.fingerprint() {
                    Ok(PromotionOutcome::Replayed {
                        revision,
                        receipt_id,
                    })
                } else {
                    Err(Refusal::IdempotencyConflict)
                }));
            }
            // J-3: the keys of the acceptance record and of every runner attestation are
            // looked up in `key_revocations` inside the promotion transaction (no cache): a
            // revocation over `admin.sock` stops promotion at once.
            for (id, digest, refusal) in intent.signing_keys() {
                if crate::keys::revoked_text_in(tx, id, digest)? {
                    return Ok(End::Rollback(Err(refusal)));
                }
            }
            let workflow = read_workflow(tx, &key.tenant_id, &key.project_id, &key.workflow_id)?;
            let policy = read_policy(tx, &key.tenant_id, &key.project_id)?.map(|(p, _)| p);
            let mut registry = Vec::new();
            let evaluator_ids = intent.evaluator_ids();
            if !evaluator_ids.is_empty() {
                let prefix = scope_prefix(&key.tenant_id, &key.project_id);
                let secure = secure_scope_exists(tx, &prefix)?;
                if secure {
                    secure_head(tx, &key.tenant_id, &key.project_id, &prefix)?;
                }
                for evaluator in evaluator_ids {
                    let row = read_registry_row(tx, &key.tenant_id, &key.project_id, evaluator)?;
                    if secure {
                        let validated = validate_registry_row(tx, &prefix, evaluator, row.as_ref())?;
                        if let Some(validated) = validated.filter(ValidatedRegistryEntry::active) {
                            if validated.key_revoked(tx)? {
                                return Ok(End::Rollback(Err(Refusal::Acceptance(
                                    AcceptanceCode::AttestationUnauthenticated,
                                ))));
                            }
                            let (key_id, digest) = validated.runner_key()?;
                            if let Err(refusal) = intent.check_evaluator_key(key_id, digest) {
                                return Ok(End::Rollback(Err(refusal)));
                            }
                        }
                    }
                    if let Some(row) = row {
                        registry.push((evaluator.to_string(), row.stored()));
                    }
                }
            }
            let record_consumed = match intent.record_id() {
                Some(id) => tx.query_row(
                    "SELECT EXISTS (SELECT 1 FROM consumed_records WHERE acceptance_record_id = ?1)",
                    [id],
                    |r| r.get(0),
                )?,
                None => false,
            };
            let mut consumed_attestations = BTreeSet::new();
            for id in intent.attestation_ids() {
                let used: bool = tx.query_row(
                    "SELECT EXISTS (SELECT 1 FROM consumed_attestations WHERE attestation_id = ?1)",
                    [id],
                    |r| r.get(0),
                )?;
                if used {
                    consumed_attestations.insert(id.to_string());
                }
            }
            // M4-06: the stored state of each signoff the record names, read here (C2).
            let mut signoffs = Vec::new();
            for id in intent.signoff_ids() {
                if let Some(u) = signoff_use(tx, id)? {
                    signoffs.push(u);
                }
            }
            let view = PromotionView {
                workflow,
                policy,
                registry,
                record_consumed,
                consumed_attestations,
                signoffs,
            };
            let write = match intent.check(&view) {
                Ok(write) => write,
                Err(refusal) => return Ok(End::Rollback(Err(refusal))),
            };
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
                    (label, request.tenant_id(), request.idempotency_key()),
                )?;
            }
            let previous = view.workflow.as_ref().map_or(-1, |w| w.revision);
            let last = write.last_promotion.as_ref();
            let changed = tx.execute(
                "UPDATE workflows SET state = ?4, revision = ?5, \
                 last_receipt_id = coalesce(?6, last_receipt_id), \
                 last_record_id = coalesce(?7, last_record_id), \
                 last_snapshot_id = coalesce(?8, last_snapshot_id), \
                 last_artifact_digest = coalesce(?9, last_artifact_digest), \
                 last_acceptance_sha = coalesce(?10, last_acceptance_sha), \
                 last_authorization_sha = coalesce(?11, last_authorization_sha) \
                 WHERE tenant_id = ?1 AND project_id = ?2 AND workflow_id = ?3 \
                 AND revision = ?12 AND state = ?13",
                rusqlite::params![
                    key.tenant_id,
                    key.project_id,
                    key.workflow_id,
                    intent.to_state(),
                    write.revision,
                    last.map(|l| l.receipt_id.as_str()),
                    last.map(|l| l.record_id.as_str()),
                    last.map(|l| l.artifact_snapshot_id.as_str()),
                    last.map(|l| l.artifact_digest.as_str()),
                    last.map(|l| l.acceptance_policy_bundle_sha.as_str()),
                    last.map(|l| l.authorization_bundle_sha.as_str()),
                    previous,
                    intent.from_state(),
                ],
            )?;
            if changed != 1 {
                return Err(BodyError::StateConflict);
            }
            if let Some(record_id) = &write.record_id {
                tx.execute(
                    "INSERT INTO consumed_records (acceptance_record_id, tenant_id, idempotency_key) \
                     VALUES (?1, ?2, ?3)",
                    (record_id, request.tenant_id(), request.idempotency_key()),
                )?;
                for id in &write.attestation_ids {
                    tx.execute(
                        "INSERT INTO consumed_attestations (attestation_id, acceptance_record_id, \
                         tenant_id, idempotency_key) VALUES (?1, ?2, ?3, ?4)",
                        (id, record_id, request.tenant_id(), request.idempotency_key()),
                    )?;
                }
            }
            consume_signoffs(
                tx,
                &write.signoff_ids,
                request.tenant_id(),
                request.idempotency_key(),
            )?;
            tx.execute(
                "INSERT INTO promotion_receipts (tenant_id, idempotency_key, fingerprint, \
                 receipt_id, project_id, workflow_id, revision) \
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
                (
                    request.tenant_id(),
                    request.idempotency_key(),
                    request.fingerprint(),
                    &write.receipt_id,
                    &key.project_id,
                    &key.workflow_id,
                    write.revision,
                ),
            )?;
            workflow_event(
                tx,
                key,
                "promote",
                Some(intent.from_state()),
                intent.to_state(),
                write.revision,
                Some(&write.receipt_id),
                write.record_id.as_deref(),
            )?;
            // I-8: the promotion's place in the ledger-wide order.
            next_seq(tx, "promotion", &write.receipt_id)?;
            Ok(End::Commit(Ok(PromotionOutcome::Committed {
                revision: write.revision,
                receipt_id: write.receipt_id,
            })))
        })
    }

    fn record_signoff_requests(&self, requests: &[SignoffRequest]) -> Result<(), TxFailure> {
        self.transact("signoff_request", |tx| {
            for r in requests {
                tx.execute(
                    "INSERT OR IGNORE INTO signoff_requests (signoff_id, request_fingerprint, \
                     tenant_id, project_id, workflow_id, acceptance_record_id, criterion_id, \
                     from_state, to_state, state_revision, requester_issuer, requester_subject, \
                     agent_issuer, agent_subject, created_at, deadline_at) \
                     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, \
                     ?16)",
                    rusqlite::params![
                        r.signoff_id(),
                        r.request_fingerprint(),
                        r.tenant_id(),
                        r.project_id(),
                        r.workflow_id(),
                        r.acceptance_record_id(),
                        r.criterion_id(),
                        r.from_state(),
                        r.to_state(),
                        r.state_revision(),
                        r.requester().issuer,
                        r.requester().subject,
                        r.agent().map(|a| a.issuer.as_str()),
                        r.agent().map(|a| a.subject.as_str()),
                        r.created_at().to_string(),
                        r.deadline_at().to_string(),
                    ],
                )?;
            }
            Ok(End::Commit(()))
        })
    }
}

#[allow(clippy::too_many_arguments)]
fn workflow_event(
    tx: &Transaction<'_>,
    key: &WorkflowKey,
    cause: &str,
    from: Option<&str>,
    to: &str,
    revision: i64,
    receipt: Option<&str>,
    record: Option<&str>,
) -> rusqlite::Result<()> {
    tx.execute(
        "INSERT INTO workflow_events (tenant_id, project_id, workflow_id, cause, from_state, \
         to_state, revision, receipt_id, record_id) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
        rusqlite::params![
            key.tenant_id,
            key.project_id,
            key.workflow_id,
            cause,
            from,
            to,
            revision,
            receipt,
            record
        ],
    )
    .map(|_| ())
}

fn admin_event(
    tx: &Transaction<'_>,
    change: &AdminChange,
    tenant: &str,
    project: &str,
    target: &str,
) -> Result<(), BodyError> {
    let operation = admin_provenance(tx, change, tenant, project, target)?;
    // I-8: the administrative operation's place in the ledger-wide order.
    next_seq(
        tx,
        "administration",
        &format!("{operation}:{tenant}/{project}/{target}"),
    )?;
    Ok(())
}

fn admin_provenance(
    tx: &Transaction<'_>,
    change: &AdminChange,
    tenant: &str,
    project: &str,
    target: &str,
) -> Result<&'static str, BodyError> {
    let op = change.operator();
    let operation = match change.request() {
        AdminRequest::CreateWorkflow { .. } => "create_workflow",
        AdminRequest::UpdateArtifact { .. } => "update_artifact",
        AdminRequest::ActivateAcceptancePolicy { .. } => "activate_acceptance_policy",
        AdminRequest::RegisterEvaluator { .. } => "register_evaluator",
        AdminRequest::RevokeEvaluator { .. } => "revoke_evaluator",
    };
    tx.execute(
        "INSERT INTO admin_events (operation, tenant_id, project_id, target, operator_id, \
         operator_issuer, channel, evidence_ref, at) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
        rusqlite::params![
            operation,
            tenant,
            project,
            target,
            op.id,
            op.issuer,
            op.channel,
            op.evidence_ref,
            change.at().to_string()
        ],
    )?;
    Ok(operation)
}

#[cfg(test)]
#[path = "evaluator_publication_tests.rs"]
mod evaluator_publication_tests;
