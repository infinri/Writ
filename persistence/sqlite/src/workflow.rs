//! The transition service's ledger transactions (M3-05, D-036, schema `writ-ledger-5`):
//! [`WorkflowLedger`] for [`SqliteLedger`] and the effect-prerequisite read used inside
//! T-reserve. Every write is one fenced `BEGIN IMMEDIATE` transaction; the semantics are
//! the broker's (`writ_broker::workflow`), the store reads rows, hands them to the broker's
//! checks inside the transaction and writes what they decide.

use std::collections::BTreeSet;

use rusqlite::{OptionalExtension, Transaction};
use writ_broker::grants::{GrantCheck, PrerequisiteView, ReserveRequest};
use writ_broker::refusal::Refusal;
use writ_broker::store::{StoreError, TxFailure};
use writ_broker::workflow::{
    AdminChange, AdminRequest, LastPromotion, PromotionIntent, PromotionOutcome, PromotionView,
    StoredEntry, StoredPolicy, StoredWorkflow, WorkflowKey, WorkflowLedger,
};
use writ_broker::writ_kernel::json;

use crate::{code_name, stored_grant, unavailable, BodyError, End, SqliteLedger};

fn read_workflow(
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
            let workflow = read_workflow(tx, &key.tenant_id, &key.project_id, &key.workflow_id)?;
            let policy = read_policy(tx, &key.tenant_id, &key.project_id)?.map(|(p, _)| p);
            let mut registry = Vec::new();
            for evaluator in intent.evaluator_ids() {
                let row: Option<(String, String)> = tx
                    .query_row(
                        "SELECT entry, status FROM evaluator_registry \
                         WHERE tenant_id = ?1 AND project_id = ?2 AND evaluator_id = ?3",
                        (&key.tenant_id, &key.project_id, evaluator),
                        |r| Ok((r.get(0)?, r.get(1)?)),
                    )
                    .optional()?;
                if let Some((entry, status)) = row {
                    registry.push((
                        evaluator.to_string(),
                        StoredEntry {
                            entry,
                            revoked: status == "revoked",
                        },
                    ));
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
            let view = PromotionView {
                workflow,
                policy,
                registry,
                record_consumed,
                consumed_attestations,
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
            Ok(End::Commit(Ok(PromotionOutcome::Committed {
                revision: write.revision,
                receipt_id: write.receipt_id,
            })))
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
    Ok(())
}
