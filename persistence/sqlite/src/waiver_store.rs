//! Transactional request, receipt and current validation for D-053 waivers.

use std::collections::{BTreeMap, BTreeSet};

use rusqlite::{Connection, OptionalExtension, Transaction};
use writ_broker::approval::{
    context_fingerprint, ApprovalCode, SignoffExclusions, PROVENANCE_VERSION_2,
};
use writ_broker::approval_store::{CeremonyBegin, CeremonyFinish, CeremonyOp, CeremonyOutcome};
use writ_broker::human::{
    check_human_permission, signoff_separation, AcceptedAssertion, ApprovalChannel, Permission,
    SubjectId,
};
use writ_broker::refusal::Refusal;
use writ_broker::store::{StoreError, TxFailure};
use writ_broker::waiver::{WaiverBinding, WaiverPolicyView, WAIVER_MAX_SECONDS};
use writ_broker::waiver_lifecycle::{
    ceremony_binding, WaiverChallenge, WaiverLedger, WaiverReceipt,
};
use writ_broker::workflow::WaiverIntent;
use writ_broker::writ_kernel::{
    contracts::AcceptanceRequirements,
    json::{self, Digest, Value},
    time::Timestamp,
};

use crate::approval_store::{
    ap, approval_event, assignments, changers, end, enrollment, next_seq, no_channel,
    subject_record, ts, Outcome,
};
use crate::workflow::read_workflow;
use crate::{code_name, unavailable, BodyError, End, SqliteLedger};

pub(crate) struct StoredWaiverRequest {
    pub(crate) binding: WaiverBinding,
    created_at: Timestamp,
}

fn read_context(text: &str, waiver_id: &str) -> Result<WaiverBinding, BodyError> {
    let value = json::parse(text.as_bytes()).map_err(|_| BodyError::Integrity)?;
    let binding = WaiverBinding::from_value(&value).ok_or(BodyError::Integrity)?;
    let fingerprint = binding.fingerprint().map_err(|_| BodyError::Integrity)?;
    if fingerprint.as_str() != waiver_id
        || json::canonical(&value).map_err(|_| BodyError::Integrity)? != text
    {
        return Err(BodyError::Integrity);
    }
    Ok(binding)
}

pub(crate) fn read_request(
    conn: &Connection,
    waiver_id: &str,
) -> Result<Option<StoredWaiverRequest>, BodyError> {
    let row: Option<(String, String, i64)> = conn
        .query_row(
            "SELECT context, created_at, ledger_seq FROM waiver_requests WHERE waiver_id = ?1",
            [waiver_id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .optional()?;
    row.map(|(context, created_at, seq)| {
        let binding = read_context(&context, waiver_id)?;
        let key = &binding.request.workflow;
        let audited: bool = conn.query_row(
            "SELECT EXISTS (
               SELECT 1 FROM approval_events e JOIN ledger_sequence s ON s.seq = e.ledger_seq
                WHERE e.kind = 'waiver_requested' AND e.target_id = ?1
                  AND e.request_fingerprint = ?1 AND e.tenant_id = ?2 AND e.project_id = ?3
                  AND e.actor_issuer = ?4 AND e.actor_subject = ?5 AND e.policy_bundle_sha = ?6
                  AND e.at = ?7 AND e.ledger_seq = ?8 AND e.provenance_version = ?9
                  AND e.challenge_id IS NULL AND s.kind = 'waiver_request' AND s.reference = ?1
             )",
            rusqlite::params![
                waiver_id,
                key.tenant_id,
                key.project_id,
                binding.requester.issuer,
                binding.requester.subject,
                binding.policy.acceptance_digest,
                created_at,
                seq,
                PROVENANCE_VERSION_2,
            ],
            |r| r.get(0),
        )?;
        if !audited {
            return Err(BodyError::Integrity);
        }
        Ok(StoredWaiverRequest {
            binding,
            created_at: ts(&created_at)?,
        })
    })
    .transpose()
}

fn check_policy(intent: &WaiverIntent<'_>) -> Result<(WaiverPolicyView, Timestamp, i64), Refusal> {
    let observed = intent.now();
    let policy = intent.policy().current(observed)?;
    let now = observed.at();
    let request = intent.request();
    let identity = policy.identity();
    if identity.tenant_id != request.workflow.tenant_id
        || identity.project_id != request.workflow.project_id
        || identity.acceptance_digest != intent.acceptance_digest()
    {
        return Err(Refusal::PolicyDependencyMismatch);
    }
    if policy.checked_at() != now || now >= policy.expires_at() {
        return Err(Refusal::AcceptancePolicyUnavailable);
    }
    let maximum = policy
        .permits(&request.criterion_id)
        .ok_or_else(|| ap(ApprovalCode::WaiverIneligible))?;
    Ok((policy, now, i64::from(maximum.min(WAIVER_MAX_SECONDS))))
}

fn check_workflow(
    conn: &Connection,
    intent: &WaiverIntent<'_>,
    policy: &WaiverPolicyView,
) -> Outcome<()> {
    let request = intent.request();
    let key = &request.workflow;
    let workflow = read_workflow(conn, &key.tenant_id, &key.project_id, &key.workflow_id)?;
    if workflow.as_ref() != Some(intent.workflow())
        || intent.workflow().state != request.from_state
        || intent.workflow().revision != request.expected_revision
    {
        return Ok(Err(ap(ApprovalCode::WaiverMismatch)));
    }
    let active: Option<(String, String)> = conn
        .query_row(
            "SELECT bundle_sha, requirements FROM acceptance_policies
             WHERE tenant_id = ?1 AND project_id = ?2",
            (&key.tenant_id, &key.project_id),
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .optional()?;
    let Some((digest, requirements)) = active else {
        return Ok(Err(Refusal::AcceptancePolicyUnavailable));
    };
    let value = json::parse(requirements.as_bytes()).map_err(|_| BodyError::Integrity)?;
    let requirements =
        AcceptanceRequirements::from_value(&value).map_err(|_| BodyError::Integrity)?;
    if digest != intent.acceptance_digest() || &requirements != policy.requirements() {
        return Ok(Err(Refusal::PolicyDependencyMismatch));
    }
    Ok(Ok(()))
}

fn exclusions(
    conn: &Connection,
    intent: &WaiverIntent<'_>,
    policy: &WaiverPolicyView,
) -> Result<SignoffExclusions, BodyError> {
    let key = &intent.request().workflow;
    let (artifact_changers, mut policy_changers) =
        changers(conn, &key.tenant_id, &key.project_id, &key.workflow_id)?;
    policy_changers.extend_from_slice(policy.publishers());
    let mut excluded = SignoffExclusions {
        requesters: vec![intent.requester().clone()],
        agents: intent.agent().cloned().into_iter().collect(),
        artifact_changers,
        policy_changers,
        ..SignoffExclusions::default()
    };
    excluded.linked_subjects = linked_exclusions(conn, excluded.all())?;
    Ok(excluded.normalized())
}

fn linked_exclusions(
    conn: &Connection,
    mut pending: Vec<SubjectId>,
) -> Result<Vec<SubjectId>, BodyError> {
    let mut neighbors = BTreeMap::<String, Vec<SubjectId>>::new();
    let mut stmt = conn
        .prepare("SELECT issuer, subject, linked_issuer, linked_subject FROM enrollment_links")?;
    let links = stmt.query_map([], |row| {
        let issuer: String = row.get(0)?;
        let subject: String = row.get(1)?;
        let linked_issuer: String = row.get(2)?;
        let linked_subject: String = row.get(3)?;
        Ok((
            SubjectId::new(&issuer, &subject),
            SubjectId::new(&linked_issuer, &linked_subject),
        ))
    })?;
    for link in links {
        let (from, to) = link?;
        if !from.is_well_formed() || !to.is_well_formed() {
            return Err(BodyError::Integrity);
        }
        neighbors
            .entry(from.subject.to_lowercase())
            .or_default()
            .extend([from.clone(), to.clone()]);
        neighbors
            .entry(to.subject.to_lowercase())
            .or_default()
            .extend([from, to]);
    }
    let originals: BTreeSet<SubjectId> = pending.iter().cloned().collect();
    let mut linked = BTreeSet::new();
    while let Some(party) = pending.pop() {
        if let Some(adjacent) = neighbors.remove(&party.subject.to_lowercase()) {
            for identity in adjacent {
                if !originals.contains(&identity) && linked.insert(identity.clone()) {
                    pending.push(identity);
                }
            }
        }
    }
    Ok(linked.into_iter().collect())
}

fn effective_expiry(
    created_at: Timestamp,
    maximum: i64,
    policy: &WaiverPolicyView,
    deadline: Option<Timestamp>,
) -> Timestamp {
    let cap = created_at.plus_seconds(maximum).min(policy.expires_at());
    deadline.map_or(cap, |deadline| deadline.min(cap))
}

fn binding(
    conn: &Connection,
    intent: &WaiverIntent<'_>,
    policy: &WaiverPolicyView,
    expires_at: Timestamp,
) -> Result<WaiverBinding, BodyError> {
    let workflow = intent.workflow();
    Ok(WaiverBinding {
        request: intent.request().clone(),
        task_id: workflow.task_id.clone(),
        artifact_snapshot_id: workflow.artifact_snapshot_id.clone(),
        artifact_digest: workflow.artifact_digest.clone(),
        policy: policy.identity().clone(),
        workflow_authorization_digest: intent.workflow_authorization_digest().to_string(),
        requester: intent.requester().clone(),
        exclusions: exclusions(conn, intent, policy)?,
        expires_at,
    })
}

fn intent_digest(
    binding: &WaiverBinding,
    deadline: Option<Timestamp>,
) -> Result<Digest, BodyError> {
    let Value::Obj(mut body) = binding.value() else {
        return Err(BodyError::Integrity);
    };
    body.remove("expires_at");
    let deadline = deadline.map_or(Value::Null, |value| json::s(&value.to_string()));
    context_fingerprint(&json::obj([
        ("purpose", json::s("writ-waiver-request-intent/1")),
        ("binding", Value::Obj(body)),
        ("deadline", deadline),
    ]))
    .map_err(|_| BodyError::Integrity)
}

pub(crate) fn check_request(
    conn: &Connection,
    request: &StoredWaiverRequest,
    intent: &WaiverIntent<'_>,
) -> Outcome<Timestamp> {
    let id = request
        .binding
        .fingerprint()
        .map_err(|_| BodyError::Integrity)?;
    let revoked: bool = conn.query_row(
        "SELECT EXISTS (
           SELECT 1 FROM revocations WHERE target_kind = 'waiver' AND target_id = 'waiver:' || ?1
         )",
        [id.as_str()],
        |r| r.get(0),
    )?;
    if revoked {
        return Ok(Err(ap(ApprovalCode::WaiverRevoked)));
    }
    if let Err(refusal) = intent.reauthorize_management() {
        return Ok(Err(refusal));
    }
    let (policy, _, maximum) = match check_policy(intent) {
        Ok(view) => view,
        Err(refusal) => return Ok(Err(refusal)),
    };
    if &request.binding.policy != policy.identity() {
        return Ok(Err(Refusal::PolicyDependencyMismatch));
    }
    if let Err(refusal) = check_workflow(conn, intent, &policy)? {
        return Ok(Err(refusal));
    }
    let now = match intent.recheck_management_time() {
        Ok(time) => time,
        Err(refusal) => return Ok(Err(refusal)),
    };
    let expires_at = request.binding.expires_at;
    if request.created_at > now
        || expires_at <= request.created_at
        || now >= expires_at
        || expires_at > effective_expiry(request.created_at, maximum, &policy, intent.deadline())
    {
        return Ok(Err(ap(ApprovalCode::WaiverExpired)));
    }
    if request.binding != binding(conn, intent, &policy, expires_at)? {
        return Ok(Err(ap(ApprovalCode::WaiverMismatch)));
    }
    Ok(Ok(now))
}

fn request_in(
    tx: &Transaction<'_>,
    intent: &WaiverIntent<'_>,
) -> Result<End<Result<WaiverBinding, Refusal>>, BodyError> {
    let (policy, now, maximum) = match check_policy(intent) {
        Ok(view) => view,
        Err(refusal) => return Ok(End::Rollback(Err(refusal))),
    };
    if let Err(refusal) = check_workflow(tx, intent, &policy)? {
        return Ok(End::Rollback(Err(refusal)));
    }
    let created_at = intent.now().at();
    let expires_at = effective_expiry(created_at, maximum, &policy, intent.deadline());
    if now >= expires_at {
        return Ok(End::Rollback(Err(ap(ApprovalCode::WaiverExpired))));
    }
    let binding = binding(tx, intent, &policy, expires_at)?;
    let value = binding.value();
    if WaiverBinding::from_value(&value).as_ref() != Some(&binding) {
        return Ok(End::Rollback(Err(ap(ApprovalCode::WaiverMismatch))));
    }
    let digest = intent_digest(&binding, intent.deadline())?;
    let prior: Option<String> = tx
        .query_row(
            "SELECT waiver_id FROM waiver_requests WHERE intent_digest = ?1",
            [digest.as_str()],
            |r| r.get(0),
        )
        .optional()?;
    if let Some(prior) = prior {
        let request = read_request(tx, &prior)?.ok_or(BodyError::Integrity)?;
        return Ok(End::Rollback(
            check_request(tx, &request, intent)?.map(|_| request.binding),
        ));
    }
    let id = binding.fingerprint().map_err(|_| BodyError::Integrity)?;
    if let Some(prior) = read_request(tx, id.as_str())? {
        return Ok(End::Rollback(
            check_request(tx, &prior, intent)?.map(|_| prior.binding),
        ));
    }
    let seq = next_seq(tx, "waiver_request", id.as_str())?;
    tx.execute(
        "INSERT INTO waiver_requests (waiver_id, intent_digest, context, created_at, ledger_seq)
         VALUES (?1, ?2, ?3, ?4, ?5)",
        (
            id.as_str(),
            digest.as_str(),
            json::canonical(&value).map_err(|_| BodyError::Integrity)?,
            created_at.to_string(),
            seq,
        ),
    )?;
    let key = &binding.request.workflow;
    approval_event(
        tx,
        "waiver_requested",
        id.as_str(),
        &key.tenant_id,
        &key.project_id,
        Some(&binding.requester),
        None,
        None,
        created_at,
        None,
        Some((id.as_str(), &binding.policy.acceptance_digest)),
        Some(seq),
    )?;
    match check_request(
        tx,
        &StoredWaiverRequest {
            binding: binding.clone(),
            created_at,
        },
        intent,
    )? {
        Ok(_) => Ok(End::Commit(Ok(binding))),
        Err(refusal) => Ok(End::Rollback(Err(refusal))),
    }
}

pub(crate) fn insert_receipt(
    tx: &Transaction<'_>,
    waiver_id: &str,
    accepted: &AcceptedAssertion,
    seq: i64,
) -> rusqlite::Result<()> {
    let subject = accepted.subject();
    tx.execute(
        "INSERT INTO waivers
         (waiver_id, result, issuer, subject, credential_id, challenge_id, recorded_at, ledger_seq)
         VALUES (?1, 'waived', ?2, ?3, ?4, ?5, ?6, ?7)",
        (
            waiver_id,
            &subject.issuer,
            &subject.subject,
            accepted.credential_id(),
            accepted.challenge_id(),
            accepted.accepted_at().to_string(),
            seq,
        ),
    )?;
    Ok(())
}

const MANAGED_REFERENCE: &str = "|writ-managed-waiver/1|";

pub(crate) fn receipt_reference(waiver_id: &str, dispatch: Option<&Digest>) -> (String, String) {
    dispatch.map_or_else(
        || (waiver_id.to_owned(), waiver_id.to_owned()),
        |digest| {
            let reference = format!("{waiver_id}{MANAGED_REFERENCE}{digest}");
            let fingerprint = Digest::of_bytes(reference.as_bytes()).to_string();
            (reference, fingerprint)
        },
    )
}

struct ReceiptProvenance {
    receipt: WaiverReceipt,
    dispatch: Option<String>,
    rp_id: String,
    origin: String,
    channel: String,
}

fn read_provenance(
    conn: &Connection,
    waiver_id: &str,
) -> Result<Option<ReceiptProvenance>, BodyError> {
    type Stored = (String, String, String, String, String, i64);
    let row: Option<Stored> = conn
        .query_row(
            "SELECT issuer, subject, credential_id, challenge_id, recorded_at, ledger_seq
             FROM waivers WHERE waiver_id = ?1 AND result = 'waived'",
            [waiver_id],
            |r| {
                Ok((
                    r.get(0)?,
                    r.get(1)?,
                    r.get(2)?,
                    r.get(3)?,
                    r.get(4)?,
                    r.get(5)?,
                ))
            },
        )
        .optional()?;
    let Some((issuer, subject, credential, challenge_id, recorded_at, ledger_seq)) = row else {
        return Ok(None);
    };
    let request = read_request(conn, waiver_id)?.ok_or(BodyError::Integrity)?;
    let expected = ceremony_binding(&request.binding).map_err(|_| BodyError::Integrity)?;
    type StoredProof = (
        String,
        String,
        String,
        String,
        String,
        String,
        String,
        String,
    );
    let row: Option<StoredProof> = conn
        .query_row(
            "SELECT c.rp_id, c.origin, c.allowed_credentials, c.issued_at, c.expires_at, e.channel,
                    s.reference, e.request_fingerprint
             FROM challenges c JOIN approval_events e ON e.challenge_id = c.challenge_id
             JOIN ledger_sequence s ON s.seq = e.ledger_seq
             WHERE c.challenge_id = ?1 AND c.status = 'consumed' AND c.operation = 'waive'
               AND c.verdict = 'waived' AND c.target_id = ?2 AND c.context_fingerprint = ?2
               AND c.issuer = ?3 AND c.subject = ?4 AND c.tenant_id = ?5 AND c.project_id = ?6
               AND c.policy_bundle_sha = ?7 AND c.revision = ?8
               AND e.kind = 'waived' AND e.target_id = ?2 AND e.request_fingerprint IS NOT NULL
               AND e.actor_issuer = ?3 AND e.actor_subject = ?4 AND e.tenant_id = ?5
               AND e.project_id = ?6 AND e.policy_bundle_sha = ?7 AND e.credential_id = ?9
               AND e.at = ?10 AND e.ledger_seq = ?11 AND e.provenance_version = ?12
               AND e.channel IS NOT NULL AND s.kind = 'waiver'",
            rusqlite::params![
                &challenge_id,
                waiver_id,
                &issuer,
                &subject,
                expected.tenant_id,
                expected.project_id,
                expected.policy_bundle_sha,
                expected.revision,
                &credential,
                &recorded_at,
                ledger_seq,
                PROVENANCE_VERSION_2,
            ],
            |r| {
                Ok((
                    r.get(0)?,
                    r.get(1)?,
                    r.get(2)?,
                    r.get(3)?,
                    r.get(4)?,
                    r.get(5)?,
                    r.get(6)?,
                    r.get(7)?,
                ))
            },
        )
        .optional()?;
    let Some((
        rp_id,
        origin,
        allowed,
        issued_at,
        challenge_expiry,
        channel,
        reference,
        audit_fingerprint,
    )) = row
    else {
        return Err(BodyError::Integrity);
    };
    let dispatch = if reference == waiver_id {
        None
    } else {
        Some(
            reference
                .strip_prefix(waiver_id)
                .and_then(|tail| tail.strip_prefix(MANAGED_REFERENCE))
                .filter(|digest| writ_broker::writ_kernel::ids::is_digest(digest))
                .ok_or(BodyError::Integrity)?
                .to_owned(),
        )
    };
    let fingerprint = if dispatch.is_some() {
        Digest::of_bytes(reference.as_bytes()).to_string()
    } else {
        waiver_id.to_owned()
    };
    if fingerprint != audit_fingerprint {
        return Err(BodyError::Integrity);
    }
    let allowed = json::parse(allowed.as_bytes()).map_err(|_| BodyError::Integrity)?;
    let approved_credential = allowed.as_arr().is_some_and(|ids| {
        ids.iter()
            .any(|id| id.as_str() == Some(credential.as_str()))
    });
    let recorded_at = ts(&recorded_at)?;
    let issued_at = ts(&issued_at)?;
    let challenge_expiry = ts(&challenge_expiry)?;
    if !approved_credential
        || issued_at < request.created_at
        || recorded_at < issued_at
        || recorded_at >= challenge_expiry
        || recorded_at >= request.binding.expires_at
    {
        return Err(BodyError::Integrity);
    }
    Ok(Some(ReceiptProvenance {
        receipt: WaiverReceipt {
            binding: request.binding,
            approver: SubjectId::new(&issuer, &subject),
            credential_id: credential,
            challenge_id,
            recorded_at,
            ledger_seq,
        },
        dispatch,
        rp_id,
        origin,
        channel,
    }))
}

pub(crate) fn read_receipt(
    conn: &Connection,
    waiver_id: &str,
) -> Result<Option<WaiverReceipt>, BodyError> {
    Ok(read_provenance(conn, waiver_id)?.map(|proof| proof.receipt))
}

fn validate_in(
    conn: &Connection,
    waiver_id: &str,
    intent: &WaiverIntent<'_>,
    channel: &ApprovalChannel,
) -> Outcome<WaiverBinding> {
    let Some(proof) = read_provenance(conn, waiver_id)? else {
        return Ok(Err(ap(ApprovalCode::WaiverUnknown)));
    };
    let request = read_request(conn, waiver_id)?.ok_or(BodyError::Integrity)?;
    let now = match check_request(conn, &request, intent)? {
        Ok(now) => now,
        Err(refusal) => return Ok(Err(refusal)),
    };
    let rp = channel.relying_party();
    if proof.rp_id != rp.rp_id || proof.origin != rp.origin || proof.channel != channel.channel() {
        return Ok(Err(no_channel()));
    }

    let approver = &proof.receipt.approver;
    let credential_active: bool = conn.query_row(
        "SELECT EXISTS (
           SELECT 1 FROM credentials WHERE credential_id = ?1 AND issuer = ?2
            AND subject = ?3 AND status = 'active'
         )",
        (
            &proof.receipt.credential_id,
            &approver.issuer,
            &approver.subject,
        ),
        |r| r.get(0),
    )?;
    if !credential_active {
        return Ok(Err(ap(ApprovalCode::CredentialRevoked)));
    }
    let Some(record) = subject_record(conn, approver)? else {
        return Ok(Err(ap(ApprovalCode::SubjectRevoked)));
    };
    let mut roles = assignments(conn, approver)?;
    roles.retain(|role| role.active_at(proof.receipt.recorded_at));
    let key = &request.binding.request.workflow;
    if let Err(code) = check_human_permission(
        &record,
        &roles,
        Permission::WaiverAuthority,
        &key.tenant_id,
        &key.project_id,
        now,
    ) {
        return Ok(Err(ap(code)));
    }
    let enrollment = enrollment(conn, approver)?;
    if let Err(code) = signoff_separation(
        approver,
        enrollment.as_ref(),
        &request.binding.exclusions.all(),
    ) {
        return Ok(Err(ap(code)));
    }
    Ok(Ok(request.binding))
}

impl SqliteLedger {
    /// Read an audited waiver request within the supplied scope.
    pub fn waiver_request_in_scope(
        &self,
        tenant: &str,
        project: &str,
        waiver_id: &str,
    ) -> Result<Option<WaiverBinding>, StoreError> {
        self.read_scoped_waiver(tenant, project, waiver_id, |conn| {
            Ok(read_request(conn, waiver_id)?.map(|request| request.binding))
        })
    }

    /// Read a historical receipt and optionally require its exact managed dispatch digest.
    pub fn waiver_receipt_in_scope(
        &self,
        tenant: &str,
        project: &str,
        waiver_id: &str,
        dispatch: Option<&str>,
    ) -> Result<Option<WaiverReceipt>, StoreError> {
        self.read_scoped_waiver(tenant, project, waiver_id, |conn| {
            Ok(read_provenance(conn, waiver_id)?
                .filter(|proof| {
                    dispatch.is_none_or(|digest| proof.dispatch.as_deref() == Some(digest))
                })
                .map(|proof| proof.receipt))
        })
    }

    /// Match a disposable authority's displayed waiver and options with an issued ceremony.
    pub fn waiver_challenge_matches(
        &self,
        binding: &WaiverBinding,
        challenge_id: &str,
        subject: &SubjectId,
        challenge_digest: &Digest,
        now: Timestamp,
    ) -> Result<bool, StoreError> {
        let key = &binding.request.workflow;
        let id = binding
            .fingerprint()
            .map_err(|_| unavailable("read scoped waiver challenge", "context"))?;
        let matched =
            self.read_scoped_waiver(&key.tenant_id, &key.project_id, id.as_str(), |conn| {
                let Some(request) = read_request(conn, id.as_str())? else {
                    return Ok(None);
                };
                if request.binding != *binding {
                    return Ok(None);
                }
                let matches: bool = conn.query_row(
                    "SELECT EXISTS (SELECT 1 FROM challenges
                  WHERE challenge_id = ?1 AND operation = 'waive' AND verdict = 'waived'
                    AND status = 'issued' AND target_id = ?2 AND context_fingerprint = ?2
                    AND challenge_digest = ?3 AND tenant_id = ?4 AND project_id = ?5
                    AND issuer = ?6 AND subject = ?7 AND policy_bundle_sha = ?8
                    AND revision = ?9 AND issued_at <= ?10 AND expires_at > ?10)",
                    rusqlite::params![
                        challenge_id,
                        id.as_str(),
                        challenge_digest.as_str(),
                        key.tenant_id,
                        key.project_id,
                        subject.issuer,
                        subject.subject,
                        binding.policy.acceptance_digest,
                        binding.request.expected_revision,
                        now.to_string(),
                    ],
                    |row| row.get(0),
                )?;
                Ok(Some(matches))
            })?;
        Ok(matched.unwrap_or(false))
    }

    fn read_scoped_waiver<T>(
        &self,
        tenant: &str,
        project: &str,
        waiver_id: &str,
        read: impl FnOnce(&Connection) -> Result<Option<T>, BodyError>,
    ) -> Result<Option<T>, StoreError> {
        if !self.waiver_lab_enabled() {
            return Ok(None);
        }
        let inner = self.lock("read scoped waiver")?;
        let scoped: bool = inner
            .conn
            .query_row(
                "SELECT EXISTS (SELECT 1 FROM approval_events
             WHERE kind = 'waiver_requested' AND target_id = ?1
               AND tenant_id = ?2 AND project_id = ?3)",
                (waiver_id, tenant, project),
                |r| r.get(0),
            )
            .map_err(|error| unavailable("read scoped waiver", code_name(&error)))?;
        if !scoped {
            return Ok(None);
        }
        read(&inner.conn).map_err(|error| match error {
            BodyError::Sql(sql) => unavailable("read scoped waiver", code_name(&sql)),
            _ => unavailable("read scoped waiver", "integrity"),
        })
    }
}

impl WaiverLedger for SqliteLedger {
    fn allows_disposable_waiver_fixture_ingress(&self) -> bool {
        self.waiver_lab_enabled()
    }

    fn request_waiver(
        &self,
        intent: &WaiverIntent<'_>,
    ) -> Result<Result<WaiverBinding, Refusal>, TxFailure> {
        if !self.waiver_lab_enabled() || self.approval_channel().is_none() {
            return self.audited(Ok(Err(no_channel())));
        }
        self.audited(self.transact("waiver_request", |tx| {
            if let Err(refusal) = intent.reauthorize_management() {
                return Ok(End::Rollback(Err(refusal)));
            }
            request_in(tx, intent)
        }))
    }

    fn begin_waiver(
        &self,
        begin: &CeremonyBegin<'_>,
        intent: &WaiverIntent<'_>,
    ) -> Result<Result<WaiverChallenge, Refusal>, TxFailure> {
        if !matches!(begin.op, CeremonyOp::Waive { .. }) || begin.now.at() != intent.now().at() {
            return self.audited(Ok(Err(ap(ApprovalCode::WaiverMismatch))));
        }
        self.begin_ceremony_for(begin, Some(intent), |issued, context| {
            Ok(WaiverChallenge {
                issued,
                context: context.ok_or(BodyError::Integrity)?,
            })
        })
    }

    fn finish_waiver(
        &self,
        finish: &CeremonyFinish<'_>,
        intent: &WaiverIntent<'_>,
    ) -> Result<Result<CeremonyOutcome, Refusal>, TxFailure> {
        if !matches!(finish.op, CeremonyOp::Waive { .. }) || finish.now.at() != intent.now().at() {
            return self.audited(Ok(Err(ap(ApprovalCode::WaiverMismatch))));
        }
        self.finish_ceremony_for(finish, Some(intent))
    }

    fn validate_waiver(
        &self,
        waiver_id: &str,
        intent: &WaiverIntent<'_>,
    ) -> Result<Result<WaiverBinding, Refusal>, TxFailure> {
        let Some(channel) = self
            .approval_channel()
            .filter(|_| self.waiver_lab_enabled())
        else {
            return self.audited(Ok(Err(no_channel())));
        };
        self.audited(self.transact("waiver_validate", |tx| {
            Ok(end(validate_in(tx, waiver_id, intent, channel)?, |_| true))
        }))
    }

    fn waiver_receipt(&self, waiver_id: &str) -> Result<Option<WaiverReceipt>, StoreError> {
        if !self.waiver_lab_enabled() {
            return Ok(None);
        }
        let inner = self.lock("read waiver receipt")?;
        read_receipt(&inner.conn, waiver_id).map_err(|error| match error {
            BodyError::Sql(sql) => unavailable("read waiver receipt", code_name(&sql)),
            _ => unavailable("read waiver receipt", "integrity"),
        })
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests;
