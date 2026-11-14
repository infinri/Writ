//! The approval ledger on SQLite (M4-03, M4-04, M4-05, M4-08 storage, M4-09, M4-10; schema
//! `writ-ledger-6`): [`ApprovalLedger`] for [`SqliteLedger`], the server-held registration
//! ceremonies ([`RegistrationLedger`]; S5, X8), the separate bootstrap entry point
//! ([`BootstrapSession`], docs/implementation/M4-ENROLLMENT-CEREMONY.md), the grant
//! revocation read used by T-reserve, T-dispatch and promotion, the signoff reads of a
//! promotion transaction (M4-06: [`signoff_use`], [`consume_signoffs`]), the signoff
//! exclusions read from authoritative state (X7: [`signoff_exclusions`]), and the I-8
//! sequence.
//!
//! Every state change is one fenced `BEGIN IMMEDIATE` transaction on the single owner
//! connection, so ceremonies, revocations, T-reserve, T-dispatch and promotions are totally
//! ordered (O-5): a revocation committed before a reservation refuses it; one committed after
//! the reservation and before the dispatch makes T-dispatch refuse with the receipt kept
//! `reserved` and its grant use kept; one committed after the dispatch undoes nothing and
//! records `use_state` `dispatched`. The semantics are the broker's: rows are read inside the
//! transaction and handed to `writ_broker::approval` (C2); a refusal rolls back and is then
//! audited in its own transaction (a failed audit never changes the answer).
//!
//! Stored digests: the kernel's `Digest` has no constructor from text, so a stored challenge
//! digest or context fingerprint is compared as text with the value presented (the
//! assertion's ceremony digest, the binding recomputed from current state); the stored
//! challenge is rebuilt with that presented value when the texts are equal and with a fixed
//! sentinel digest otherwise, so `Challenge::check` refuses in its own order.

use rusqlite::{Connection, OptionalExtension, Transaction};
use writ_broker::acceptance::SignoffUse;
use writ_broker::approval::context_fingerprint;
use writ_broker::approval::{
    decide, signoff_expiry, ApprovalCode, ApprovalRequest, ApprovalStatus, Challenge,
    ChallengeBinding, ChallengeOperation, ChallengeRefusal, ChallengeStatus, NewApprovalRequest,
    NewChallenge, NewSignoff, OwnUnusedGrant, RevocationTarget, Signoff, SignoffExclusions,
    SignoffFields, StoredChallenge, CHALLENGE_TTL_SECONDS, PROVENANCE_VERSION_2,
};
use writ_broker::approval_store::{
    approval_binding, kind_text, parse_kind, parse_permission, permission_text,
    registration_start_binding, registration_start_context, revocation_binding, revocation_context,
    role_change_binding, role_change_context, signoff_binding, ApprovalLedger, BootstrapEnrollment,
    CeremonyBegin, CeremonyContext, CeremonyFinish, CeremonyOp, CeremonyOutcome, CeremonyResult,
    Expired, IssuedChallenge, IssuedRegistration, NewEnrollment, OperatorSession,
    RegistrationBegin, RegistrationFinish, RegistrationLedger, RequestRecorded, RoleChange,
    SignoffState, UseState, APPROVAL_GRANT_TTL_SECONDS, BOOTSTRAP_ISSUER, BOOTSTRAP_SUBJECT,
    GLOBAL_SCOPE, REGISTRATION_TTL_SECONDS,
};
use writ_broker::clock::ObservedTime;
use writ_broker::grants::{ApprovalLink, GrantBinding, GrantLabel, NewGrant, Verdict};
use writ_broker::human::{
    check_actor, check_service_permission, same_subject, ActiveCredentials, Actor, ApprovalChannel,
    CeremonyState, CredentialStatus, EnrollmentRecord, LedgerCapability, Permission, PrincipalKind,
    RegistrationBinding, RegistrationOperation, RelyingParty, RoleAssignment, StoredCredential,
    SubjectId, SubjectRecord, SubjectStatus, VerifiedAssertion, VerifiedRegistration,
    VerifierError, ES256,
};
use writ_broker::refusal::Refusal;
use writ_broker::store::{StoreError, TxFailure, TxStage};
use writ_broker::writ_kernel::json::{self, Digest, Value};
use writ_broker::writ_kernel::time::Timestamp;

use crate::workflow::read_workflow;
use crate::{code_name, stored_grant, unavailable, BodyError, End, SqliteLedger};

/// A transaction body's answer: a refusal rolls back, a store failure fails closed.
type Outcome<T> = Result<Result<T, Refusal>, BodyError>;

/// Early return of a refusal from an [`Outcome`] body.
macro_rules! check {
    ($e:expr) => {
        match $e {
            Ok(v) => v,
            Err(r) => return Ok(Err(r)),
        }
    };
}

fn ap(code: ApprovalCode) -> Refusal {
    Refusal::Approval(code)
}

fn challenge_refused(r: ChallengeRefusal) -> Refusal {
    ap(ApprovalCode::ChallengeInvalid(r))
}

/// The answer of a ledger without a configured approval channel (M1), and of a verifier
/// that reports itself unavailable: not a terminal refusal (L6), nothing is burnt.
fn no_channel() -> Refusal {
    ap(ApprovalCode::AssertionInvalid(VerifierError::Unavailable))
}

fn cap() -> LedgerCapability {
    LedgerCapability::for_ledger_implementation()
}

fn ts(text: &str) -> Result<Timestamp, BodyError> {
    Timestamp::parse_rfc3339(text).ok_or(BodyError::Integrity)
}

/// A digest that no presented value equals (see the module notes).
fn sentinel() -> Digest {
    Digest::of_bytes(b"writ-ledger-6: stored digest differs from the presented value")
}

/// I-8: append one row to the ledger-wide sequence inside the calling transaction.
pub(crate) fn next_seq(tx: &Transaction<'_>, kind: &str, reference: &str) -> rusqlite::Result<i64> {
    tx.execute(
        "INSERT INTO ledger_sequence (kind, reference) VALUES (?1, ?2)",
        (kind, reference),
    )?;
    Ok(tx.last_insert_rowid())
}

/// The grant is invalidated by a committed revocation (I-4, O-5): revoked itself, or minted
/// by an M4 decision whose approver's subject or credential is revoked, or whose approver no
/// longer holds an unrevoked `approver` role whose scope contains the request's scope (the
/// containment of `writ_broker::human::scope_contains`, D-042 X5).
pub(crate) fn grant_revoked(conn: &Connection, label: &str) -> rusqlite::Result<bool> {
    conn.query_row(
        "SELECT EXISTS (SELECT 1 FROM revocations WHERE target_kind = 'grant' \
           AND target_id = 'grant:' || ?1) \
         OR EXISTS (SELECT 1 FROM approval_grants g \
           JOIN approval_decisions d ON d.challenge_id = g.challenge_id \
           JOIN approval_requests r ON r.request_id = g.request_id \
           WHERE g.label = ?1 AND ( \
             EXISTS (SELECT 1 FROM subjects s WHERE s.issuer = d.issuer \
               AND s.subject = d.subject AND s.revoked_at IS NOT NULL) \
             OR EXISTS (SELECT 1 FROM credentials c WHERE c.credential_id = d.credential_id \
               AND c.status = 'revoked') \
             OR NOT EXISTS (SELECT 1 FROM role_assignments a WHERE a.issuer = d.issuer \
               AND a.subject = d.subject AND a.kind = 'human' AND a.permission = 'approver' \
               AND ((a.tenant_id = '*' AND a.project_id = '*') \
                 OR (a.tenant_id = r.tenant_id AND (a.project_id = r.project_id \
                   OR (a.project_id = '*' AND a.tenant_id <> '*')))) \
               AND a.revoked_at IS NULL)))",
        [label],
        |r| r.get(0),
    )
}

/// D-042 X1, X2: the M4 approval behind grant `label` (stored binding `binding`), read inside
/// the calling T-reserve or T-dispatch transaction. `Some` only when an `approval_grants` row,
/// which only [`finish_in`] writes, links the grant to an `approved` request, its `approved`
/// decision naming this grant, and the consumed `approve` challenge of that decision for that
/// request, with matching provenance v2; `None` makes the grant non-executable. A link whose
/// grant binding differs from the binding of the approved request is an integrity failure.
/// The result carries the approved request's agent (X2).
pub(crate) fn approval_link(
    conn: &Connection,
    label: &str,
    binding: &str,
) -> Result<Option<ApprovalLink>, BodyError> {
    let request_id: Option<String> = conn
        .query_row(
            "SELECT g.request_id FROM approval_grants g \
             JOIN approval_requests r ON r.request_id = g.request_id AND r.status = 'approved' \
             JOIN approval_decisions d ON d.challenge_id = g.challenge_id \
               AND d.request_id = g.request_id AND d.verdict = 'approved' \
               AND d.grant_label = g.label \
             JOIN challenges c ON c.challenge_id = g.challenge_id AND c.status = 'consumed' \
               AND c.operation = 'approve' AND c.target_id = g.request_id \
               AND c.issuer = d.issuer AND c.subject = d.subject \
             JOIN grant_provenance p ON p.label = g.label AND p.version = ?2 \
               AND p.auth_evidence_ref = 'challenge:' || g.challenge_id \
               AND p.approver_issuer = d.issuer AND p.approver_id = d.subject \
             WHERE g.label = ?1",
            (label, PROVENANCE_VERSION_2),
            |r| r.get(0),
        )
        .optional()?;
    let Some(request_id) = request_id else {
        return Ok(None);
    };
    let Some((request, _)) = load_request(conn, &request_id)? else {
        return Err(BodyError::Integrity);
    };
    let expected = GrantBinding::for_approval_request(&request, Verdict::Approved)
        .canonical_text()
        .ok_or(BodyError::Integrity)?;
    if expected != binding {
        return Err(BodyError::Integrity);
    }
    Ok(Some(ApprovalLink {
        agent: request.fields().agent.clone(),
    }))
}

/// The stored state of `signoff_id` for the I-5 use-time checks, read inside the calling
/// promotion transaction (M4-06, C2): the revalidated signoff; whether the consumed
/// `signoff` challenge and `signed_off` event that produced it bind its subject,
/// credential, target, scope, policy, revision and complete context fingerprint
/// (provenance); signoff and credential revocation; the signer's subject, enrollment and
/// roles; the exclusions re-read now ([`signoff_exclusions`]); consumption. `None`: no
/// such signoff. The broker decides ([`writ_broker::acceptance::check_use`]).
pub(crate) fn signoff_use(
    conn: &Connection,
    signoff_id: &str,
) -> Result<Option<SignoffUse>, BodyError> {
    let Some(signoff) = read_signoff(conn, signoff_id)? else {
        return Ok(None);
    };
    let f = signoff.fields();
    let b = &f.binding;
    let fingerprint = signoff.fingerprint().map_err(|_| BodyError::Integrity)?;
    let challenge = challenge_row(conn, &f.challenge_id)?;
    let event: Option<Option<String>> = conn
        .query_row(
            "SELECT credential_id FROM approval_events WHERE challenge_id = ?1 \
             AND kind = 'signed_off' AND target_id = ?2",
            (&f.challenge_id, signoff_id),
            |r| r.get(0),
        )
        .optional()?;
    let provenance_intact = challenge.is_some_and(|c| {
        c.status == ChallengeStatus::Consumed
            && c.operation == ChallengeOperation::Signoff
            && c.subject == f.subject
            && c.allowed.iter().any(|a| a == &f.credential_id)
            && c.target_id == b.signoff_id
            && c.tenant_id == b.tenant_id
            && c.project_id == b.project_id
            && c.policy == b.acceptance_policy_bundle_sha
            && c.revision == b.state_revision
            && c.fingerprint == fingerprint.as_str()
    }) && event.flatten().as_deref() == Some(f.credential_id.as_str());
    let signoff_revoked: bool = conn.query_row(
        "SELECT EXISTS (SELECT 1 FROM revocations WHERE target_kind = 'signoff' \
           AND target_id = 'signoff:' || ?1)",
        [signoff_id],
        |r| r.get(0),
    )?;
    let credential_active: bool = conn.query_row(
        "SELECT EXISTS (SELECT 1 FROM credentials WHERE credential_id = ?1 AND issuer = ?2 \
           AND subject = ?3 AND status = 'active')",
        (&f.credential_id, &f.subject.issuer, &f.subject.subject),
        |r| r.get(0),
    )?;
    let consumed: bool = conn.query_row(
        "SELECT EXISTS (SELECT 1 FROM consumed_signoffs WHERE signoff_id = ?1)",
        [signoff_id],
        |r| r.get(0),
    )?;
    let exclusions = signoff_exclusions(conn, b)?
        .map(|(e, _)| e)
        .unwrap_or_default();
    Ok(Some(SignoffUse {
        signer: subject_record(conn, &f.subject)?,
        enrollment: enrollment(conn, &f.subject)?,
        assignments: assignments(conn, &f.subject)?,
        provenance_intact,
        signoff_revoked,
        credential_revoked: !credential_active,
        exclusions,
        consumed,
        signoff,
    }))
}

/// Consume the checked signoffs single-use ledger-wide inside the calling promotion
/// transaction (I-5): one `consumed_signoffs` row each (the primary key refuses a second).
pub(crate) fn consume_signoffs(
    tx: &Transaction<'_>,
    signoff_ids: &[String],
    tenant_id: &str,
    idempotency_key: &str,
) -> rusqlite::Result<()> {
    for id in signoff_ids {
        tx.execute(
            "INSERT INTO consumed_signoffs (signoff_id, tenant_id, idempotency_key) \
             VALUES (?1, ?2, ?3)",
            (id, tenant_id, idempotency_key),
        )?;
    }
    Ok(())
}

/// The signoff exclusions read from authoritative ledger state (X7, C2): the requesters and
/// agents of every stored promotion context (`signoff_requests`) of the signoff id in its
/// tenant and project; every enrolled subject whose enrollment links name one of them
/// ([`same_subject`], fail closed); the artifact and policy changers ([`changers`]).
/// `None` when no stored promotion context names the signoff id; otherwise also whether
/// every context agrees with the binding (record, criterion, workflow, transition,
/// revision).
pub(crate) fn signoff_exclusions(
    conn: &Connection,
    b: &NewSignoff,
) -> Result<Option<(SignoffExclusions, bool)>, BodyError> {
    type Row = (
        String,
        String,
        String,
        String,
        String,
        i64,
        String,
        String,
        Option<String>,
        Option<String>,
    );
    let rows: Vec<Row> = conn
        .prepare(
            "SELECT workflow_id, acceptance_record_id, criterion_id, from_state, to_state, \
             state_revision, requester_issuer, requester_subject, agent_issuer, agent_subject \
             FROM signoff_requests WHERE signoff_id = ?1 AND tenant_id = ?2 AND project_id = ?3 \
             ORDER BY request_fingerprint",
        )?
        .query_map((&b.signoff_id, &b.tenant_id, &b.project_id), |r| {
            Ok((
                r.get(0)?,
                r.get(1)?,
                r.get(2)?,
                r.get(3)?,
                r.get(4)?,
                r.get(5)?,
                r.get(6)?,
                r.get(7)?,
                r.get(8)?,
                r.get(9)?,
            ))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    if rows.is_empty() {
        return Ok(None);
    }
    let mut out = SignoffExclusions::default();
    let mut consistent = true;
    for (workflow, record, criterion, from, to, revision, ri, rs, ai, asub) in rows {
        consistent &= workflow == b.workflow_id
            && record == b.acceptance_record_id
            && criterion == b.criterion_id
            && from == b.from_state
            && to == b.to_state
            && revision == b.state_revision;
        out.requesters.push(SubjectId::new(&ri, &rs));
        match (ai, asub) {
            (Some(i), Some(a)) => out.agents.push(SubjectId::new(&i, &a)),
            (None, None) => {}
            _ => return Err(BodyError::Integrity),
        }
    }
    let parties: Vec<SubjectId> = out.requesters.iter().chain(&out.agents).cloned().collect();
    let links: Vec<(String, String, String, String)> = conn
        .prepare(
            "SELECT issuer, subject, linked_issuer, linked_subject FROM enrollment_links \
             ORDER BY 1, 2, 3, 4",
        )?
        .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    for (issuer, subject, li, ls) in links {
        let linked = SubjectId::new(&li, &ls);
        if parties.iter().any(|p| same_subject(p, &linked)) {
            out.linked_subjects.push(SubjectId::new(&issuer, &subject));
        }
    }
    let (artifact, policy) = changers(conn, b)?;
    out.artifact_changers = artifact;
    out.policy_changers = policy;
    Ok(Some((out.normalized(), consistent)))
}

fn signoff_revoked(conn: &Connection, signoff_id: &str) -> rusqlite::Result<bool> {
    conn.query_row(
        "SELECT EXISTS (SELECT 1 FROM revocations WHERE target_kind = 'signoff' \
           AND target_id = 'signoff:' || ?1) \
         OR EXISTS (SELECT 1 FROM signoffs g WHERE g.signoff_id = ?1 AND ( \
           EXISTS (SELECT 1 FROM subjects s WHERE s.issuer = g.issuer AND s.subject = g.subject \
             AND s.revoked_at IS NOT NULL) \
           OR EXISTS (SELECT 1 FROM credentials c WHERE c.credential_id = g.credential_id \
             AND c.status = 'revoked')))",
        [signoff_id],
        |r| r.get(0),
    )
}

// ---------------------------------------------------------------------------
// Stored records.

fn subject_value(s: &SubjectId) -> Value {
    json::obj([
        ("issuer", json::s(&s.issuer)),
        ("subject", json::s(&s.subject)),
    ])
}

fn subject_record(conn: &Connection, s: &SubjectId) -> Result<Option<SubjectRecord>, BodyError> {
    let row: Option<(String, Option<String>)> = conn
        .query_row(
            "SELECT kind, revoked_at FROM subjects WHERE issuer = ?1 AND subject = ?2",
            (&s.issuer, &s.subject),
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .optional()?;
    let Some((kind, revoked)) = row else {
        return Ok(None);
    };
    Ok(Some(SubjectRecord {
        subject: s.clone(),
        kind: parse_kind(&kind).ok_or(BodyError::Integrity)?,
        status: match revoked {
            None => SubjectStatus::Active,
            Some(at) => SubjectStatus::Revoked { at: ts(&at)? },
        },
    }))
}

fn assignments(conn: &Connection, s: &SubjectId) -> Result<Vec<RoleAssignment>, BodyError> {
    let mut stmt = conn.prepare(
        "SELECT assignment_id, kind, permission, tenant_id, project_id, granted_at, revoked_at \
         FROM role_assignments WHERE issuer = ?1 AND subject = ?2 ORDER BY assignment_id",
    )?;
    let rows = stmt
        .query_map((&s.issuer, &s.subject), |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, String>(2)?,
                r.get::<_, String>(3)?,
                r.get::<_, String>(4)?,
                r.get::<_, String>(5)?,
                r.get::<_, Option<String>>(6)?,
            ))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    let mut out = Vec::with_capacity(rows.len());
    for (id, kind, permission, tenant, project, granted, revoked) in rows {
        out.push(RoleAssignment {
            assignment_id: id,
            subject: s.clone(),
            kind: parse_kind(&kind).ok_or(BodyError::Integrity)?,
            permission: parse_permission(&permission).ok_or(BodyError::Integrity)?,
            tenant_id: tenant,
            project_id: project,
            granted_at: ts(&granted)?,
            revoked_at: revoked.as_deref().map(ts).transpose()?,
        });
    }
    Ok(out)
}

fn enrollment(conn: &Connection, s: &SubjectId) -> Result<Option<EnrollmentRecord>, BodyError> {
    let row: Option<(String, String, String)> = conn
        .query_row(
            "SELECT enrolled_at, enrolled_by_issuer, enrolled_by FROM enrollments \
             WHERE issuer = ?1 AND subject = ?2",
            (&s.issuer, &s.subject),
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .optional()?;
    let Some((at, by_issuer, by)) = row else {
        return Ok(None);
    };
    let mut stmt = conn.prepare(
        "SELECT linked_issuer, linked_subject FROM enrollment_links \
         WHERE issuer = ?1 AND subject = ?2 ORDER BY linked_issuer, linked_subject",
    )?;
    let linked = stmt
        .query_map((&s.issuer, &s.subject), |r| {
            Ok(SubjectId {
                issuer: r.get(0)?,
                subject: r.get(1)?,
            })
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    EnrollmentRecord::new(
        &cap(),
        s.clone(),
        linked,
        ts(&at)?,
        SubjectId::new(&by_issuer, &by),
    )
    .map(Some)
    .map_err(|_| BodyError::Integrity)
}

fn credentials(conn: &Connection, s: &SubjectId) -> Result<Vec<StoredCredential>, BodyError> {
    let mut stmt = conn.prepare(
        "SELECT credential_id, public_material, status, enrolled_at, enrolled_by_issuer, \
         enrolled_by FROM credentials WHERE issuer = ?1 AND subject = ?2 ORDER BY credential_id",
    )?;
    let rows = stmt
        .query_map((&s.issuer, &s.subject), |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, Vec<u8>>(1)?,
                r.get::<_, String>(2)?,
                r.get::<_, String>(3)?,
                r.get::<_, String>(4)?,
                r.get::<_, String>(5)?,
            ))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    let mut out = Vec::with_capacity(rows.len());
    for (id, material, status, at, by_issuer, by) in rows {
        out.push(StoredCredential {
            credential_id: id,
            subject: s.clone(),
            public_material: material,
            status: match status.as_str() {
                "active" => CredentialStatus::Active,
                "revoked" => CredentialStatus::Revoked,
                _ => return Err(BodyError::Integrity),
            },
            enrolled_at: ts(&at)?,
            enrolled_by: SubjectId::new(&by_issuer, &by),
        });
    }
    Ok(out)
}

/// An actor's stored records, read inside the deciding transaction (C2).
struct ActorRows {
    record: SubjectRecord,
    enrollment: Option<EnrollmentRecord>,
    assignments: Vec<RoleAssignment>,
    credentials: Vec<StoredCredential>,
}

impl ActorRows {
    fn read(conn: &Connection, s: &SubjectId) -> Result<Option<ActorRows>, BodyError> {
        let Some(record) = subject_record(conn, s)? else {
            return Ok(None);
        };
        Ok(Some(ActorRows {
            record,
            enrollment: enrollment(conn, s)?,
            assignments: assignments(conn, s)?,
            credentials: credentials(conn, s)?,
        }))
    }

    fn actor(&self) -> Actor<'_> {
        Actor {
            record: &self.record,
            enrollment: self.enrollment.as_ref(),
            assignments: &self.assignments,
        }
    }

    /// The active credentials for `permission` in the target's scope; for the revocation
    /// of the actor's own unused grant, the approver permission instead (I-4).
    fn select(&self, t: &Target, now: Timestamp) -> Result<ActiveCredentials, Refusal> {
        let b = &t.binding;
        let select = |p: Permission| {
            ActiveCredentials::select(
                &self.record,
                self.credentials.iter().cloned(),
                &self.assignments,
                p,
                &b.tenant_id,
                &b.project_id,
                now,
            )
        };
        let creds = match select(t.permission) {
            Err(ApprovalCode::PermissionMissing) if t.own_grant(&self.record.subject) => {
                select(Permission::Approver)
            }
            other => other,
        }
        .map_err(ap)?;
        if creds.as_slice().is_empty() {
            return Err(ap(ApprovalCode::CredentialRevoked));
        }
        Ok(creds)
    }
}

fn request_from_context(v: &Value) -> Option<NewApprovalRequest> {
    let s = |k: &str| v.get(k)?.as_str().map(str::to_string);
    let subject = |v: &Value| {
        Some(SubjectId::new(
            v.get("issuer")?.as_str()?,
            v.get("subject")?.as_str()?,
        ))
    };
    let t = |k: &str| Timestamp::parse_rfc3339(v.get(k)?.as_str()?);
    let agent = match v.get("agent")? {
        Value::Null => None,
        a => Some(subject(a)?),
    };
    // G4, L4: the stored context records the chain as unavailable, never a list.
    if s("delegation_chain")? != writ_broker::approval::DELEGATION_CHAIN_UNAVAILABLE {
        return None;
    }
    Some(NewApprovalRequest {
        request_id: s("request_id")?,
        tenant_id: s("tenant_id")?,
        project_id: s("project_id")?,
        requester: subject(v.get("requester")?)?,
        agent,
        action_kind: s("action_kind")?,
        canonical_resource_id: s("canonical_resource_id")?,
        action_digest: s("action_digest")?,
        action: v.get("action")?.clone(),
        capabilities: v
            .get("capabilities")?
            .as_arr()?
            .iter()
            .map(|c| c.as_str().map(str::to_string))
            .collect::<Option<Vec<_>>>()?,
        policy_bundle_sha: s("policy_bundle_sha")?,
        request_fingerprint: s("request_fingerprint")?,
        created_at: t("created_at")?,
        expires_at: t("expires_at")?,
        required_approvers: u32::try_from(v.get("required_approvers")?.as_int()?).ok()?,
    })
}

fn load_request(
    conn: &Connection,
    request_id: &str,
) -> Result<Option<(ApprovalRequest, ApprovalStatus)>, BodyError> {
    let row: Option<(String, String, String)> = conn
        .query_row(
            "SELECT context, context_fingerprint, status FROM approval_requests \
             WHERE request_id = ?1",
            [request_id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .optional()?;
    let Some((context, fingerprint, status)) = row else {
        return Ok(None);
    };
    let value = json::parse(context.as_bytes()).map_err(|_| BodyError::Integrity)?;
    let request = request_from_context(&value)
        .and_then(|fields| ApprovalRequest::new(fields).ok())
        .ok_or(BodyError::Integrity)?;
    if request.fingerprint().as_str() != fingerprint || request.request_id() != request_id {
        return Err(BodyError::Integrity);
    }
    let status = ApprovalStatus::parse(&status).ok_or(BodyError::Integrity)?;
    Ok(Some((request, status)))
}

fn read_signoff(conn: &Connection, signoff_id: &str) -> Result<Option<Signoff>, BodyError> {
    let row = conn
        .query_row(
            "SELECT tenant_id, project_id, task_id, workflow_id, from_state, to_state, \
             acceptance_record_id, criterion_id, artifact_snapshot_id, artifact_digest, \
             acceptance_policy_id, acceptance_policy_version, acceptance_policy_bundle_sha, \
             state_revision, issued_at, expires_at, issuer, subject, credential_id, \
             challenge_id, provenance_version, exclusions FROM signoffs WHERE signoff_id = ?1",
            [signoff_id],
            |r| {
                let text = |i: usize| r.get::<_, String>(i);
                Ok((
                    [
                        text(0)?,
                        text(1)?,
                        text(2)?,
                        text(3)?,
                        text(4)?,
                        text(5)?,
                        text(6)?,
                        text(7)?,
                        text(8)?,
                        text(9)?,
                        text(10)?,
                        text(11)?,
                        text(12)?,
                    ],
                    r.get::<_, i64>(13)?,
                    [
                        text(14)?,
                        text(15)?,
                        text(16)?,
                        text(17)?,
                        text(18)?,
                        text(19)?,
                        text(20)?,
                        text(21)?,
                    ],
                ))
            },
        )
        .optional()?;
    let Some((b, revision, p)) = row else {
        return Ok(None);
    };
    let [tenant, project, task, workflow, from, to, record, criterion, snapshot, digest, policy, version, sha] =
        b;
    let [issued, expires, issuer, subject, credential, challenge, provenance, exclusions] = p;
    let exclusions = json::parse(exclusions.as_bytes())
        .ok()
        .and_then(|v| SignoffExclusions::from_context(&v))
        .ok_or(BodyError::Integrity)?;
    let binding = NewSignoff {
        signoff_id: signoff_id.to_string(),
        tenant_id: tenant,
        project_id: project,
        task_id: task,
        workflow_id: workflow,
        from_state: from,
        to_state: to,
        acceptance_record_id: record,
        criterion_id: criterion,
        artifact_snapshot_id: snapshot,
        artifact_digest: digest,
        acceptance_policy_id: policy,
        acceptance_policy_version: version,
        acceptance_policy_bundle_sha: sha,
        state_revision: revision,
    };
    Signoff::from_stored(
        &cap(),
        SignoffFields {
            binding,
            expires_at: ts(&expires)?,
            exclusions,
            issued_at: ts(&issued)?,
            subject: SubjectId::new(&issuer, &subject),
            credential_id: credential,
            challenge_id: challenge,
            provenance_version: provenance,
        },
    )
    .map(Some)
    .map_err(|_| BodyError::Integrity)
}

// ---------------------------------------------------------------------------
// Targets: what a ceremony binds, recomputed from current stored state.

enum TargetKind {
    Request {
        request: Box<ApprovalRequest>,
        status: ApprovalStatus,
    },
    Revoke {
        /// For a grant minted by an M4 decision: its approver and whether it is unused.
        grant_approver: Option<(SubjectId, bool)>,
    },
    Signoff {
        /// The exclusions read in this transaction (X7).
        excluded: Box<SignoffExclusions>,
        /// The effective expiry (Y1): computed at begin, the stored value at finish.
        expires_at: Timestamp,
    },
    Enroll,
    /// A role change: the subject whose assignment changes.
    RoleChange {
        subject: SubjectId,
    },
    /// A registration start (D-043 Y2).
    RegistrationStart,
}

struct Target {
    binding: ChallengeBinding,
    /// The complete displayed context whose fingerprint `binding` carries (R6).
    context: Value,
    permission: Permission,
    expiry: Option<Timestamp>,
    /// A refusal that applies once the assertion is accepted (already revoked, signoff id
    /// taken, enrollment unsupported).
    blocked: Option<Refusal>,
    kind: TargetKind,
}

impl Target {
    fn own_grant(&self, subject: &SubjectId) -> bool {
        matches!(&self.kind, TargetKind::Revoke { grant_approver: Some((s, true)) } if s == subject)
    }
}

fn revoked_target(conn: &Connection, target: &RevocationTarget) -> rusqlite::Result<bool> {
    let kind = match target {
        RevocationTarget::Grant(_) => "grant",
        RevocationTarget::Signoff(_) => "signoff",
        RevocationTarget::Credential(_) => "credential",
        RevocationTarget::Subject(_) => "subject",
        RevocationTarget::Role(_) => "role",
    };
    conn.query_row(
        "SELECT EXISTS (SELECT 1 FROM revocations WHERE target_kind = ?1 AND target_id = ?2)",
        (kind, target.target_id()),
        |r| r.get(0),
    )
}

fn target_kind_text(target: &RevocationTarget) -> &'static str {
    match target {
        RevocationTarget::Grant(_) => "grant",
        RevocationTarget::Signoff(_) => "signoff",
        RevocationTarget::Credential(_) => "credential",
        RevocationTarget::Subject(_) => "subject",
        RevocationTarget::Role(_) => "role",
    }
}

fn unknown_target() -> Refusal {
    challenge_refused(ChallengeRefusal::TargetMismatch)
}

fn revoke_target(conn: &Connection, target: &RevocationTarget, reason: &str) -> Outcome<Target> {
    let global = (GLOBAL_SCOPE.to_string(), GLOBAL_SCOPE.to_string());
    let already = revoked_target(conn, target)?;
    let mut grant_approver = None;
    let (scope, record, blocked) = match target {
        RevocationTarget::Grant(label) => {
            let Some(stored) = stored_grant(conn, label)? else {
                return Ok(Err(Refusal::GrantUnknown));
            };
            let link: Option<(String, String, String, String)> = conn
                .query_row(
                    "SELECT r.tenant_id, r.project_id, d.issuer, d.subject FROM approval_grants g \
                     JOIN approval_requests r ON r.request_id = g.request_id \
                     JOIN approval_decisions d ON d.challenge_id = g.challenge_id \
                     WHERE g.label = ?1",
                    [label],
                    |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
                )
                .optional()?;
            let scope = match link {
                Some((tenant, project, issuer, subject)) => {
                    grant_approver = Some((SubjectId::new(&issuer, &subject), stored.uses == 0));
                    (tenant, project)
                }
                None => global,
            };
            let record = json::obj([
                ("binding", json::s(&stored.binding)),
                ("expires_at", json::s(&stored.expires_at.to_string())),
                ("label", json::s(label)),
            ]);
            (
                scope,
                record,
                already.then(|| ap(ApprovalCode::GrantRevoked)),
            )
        }
        RevocationTarget::Signoff(id) => {
            let Some(signoff) = read_signoff(conn, id)? else {
                return Ok(Err(ap(ApprovalCode::SignoffUnknown)));
            };
            let b = &signoff.fields().binding;
            (
                (b.tenant_id.clone(), b.project_id.clone()),
                signoff.context(),
                already.then(|| ap(ApprovalCode::SignoffRevoked)),
            )
        }
        RevocationTarget::Credential(id) => {
            let row: Option<(String, String)> = conn
                .query_row(
                    "SELECT issuer, subject FROM credentials WHERE credential_id = ?1",
                    [id],
                    |r| Ok((r.get(0)?, r.get(1)?)),
                )
                .optional()?;
            let Some((issuer, subject)) = row else {
                return Ok(Err(unknown_target()));
            };
            let record = json::obj([
                ("credential_id", json::s(id)),
                ("subject", subject_value(&SubjectId::new(&issuer, &subject))),
            ]);
            (
                global,
                record,
                already.then(|| ap(ApprovalCode::CredentialRevoked)),
            )
        }
        RevocationTarget::Subject(s) => {
            let Some(record) = subject_record(conn, s)? else {
                return Ok(Err(unknown_target()));
            };
            let value = json::obj([
                ("kind", json::s(kind_text(record.kind))),
                ("subject", subject_value(s)),
            ]);
            (
                global,
                value,
                already.then(|| ap(ApprovalCode::SubjectRevoked)),
            )
        }
        RevocationTarget::Role(id) => {
            let row: Option<[String; 7]> = conn
                .query_row(
                    "SELECT issuer, subject, kind, permission, tenant_id, project_id, granted_at \
                     FROM role_assignments WHERE assignment_id = ?1",
                    [id],
                    |r| {
                        Ok([
                            r.get(0)?,
                            r.get(1)?,
                            r.get(2)?,
                            r.get(3)?,
                            r.get(4)?,
                            r.get(5)?,
                            r.get(6)?,
                        ])
                    },
                )
                .optional()?;
            let Some([issuer, subject, kind, permission, tenant, project, granted]) = row else {
                return Ok(Err(unknown_target()));
            };
            let record = json::obj([
                ("assignment_id", json::s(id)),
                ("granted_at", json::s(&granted)),
                ("kind", json::s(&kind)),
                ("permission", json::s(&permission)),
                ("project_id", json::s(&project)),
                ("subject", subject_value(&SubjectId::new(&issuer, &subject))),
                ("tenant_id", json::s(&tenant)),
            ]);
            (
                global,
                record,
                already.then(|| ap(ApprovalCode::PermissionMissing)),
            )
        }
    };
    let context = revocation_context(target, reason, &scope.0, &scope.1, record.clone());
    let binding = revocation_binding(target, reason, &scope.0, &scope.1, record)
        .map_err(|_| BodyError::Integrity)?;
    Ok(Ok(Target {
        binding,
        context,
        permission: Permission::Revoker,
        expiry: None,
        blocked,
        kind: TargetKind::Revoke { grant_approver },
    }))
}

/// The signoff binding is current: the workflow is in the binding's source state at its
/// revision with its artifact, and the project's active acceptance policy is the bound one.
fn signoff_current(conn: &Connection, b: &NewSignoff) -> Result<bool, BodyError> {
    let Some(w) = read_workflow(conn, &b.tenant_id, &b.project_id, &b.workflow_id)? else {
        return Ok(false);
    };
    let policy: Option<(String, String)> = conn
        .query_row(
            "SELECT requirements, bundle_sha FROM acceptance_policies \
             WHERE tenant_id = ?1 AND project_id = ?2",
            (&b.tenant_id, &b.project_id),
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .optional()?;
    let Some((requirements, sha)) = policy else {
        return Ok(false);
    };
    let req = json::parse(requirements.as_bytes()).map_err(|_| BodyError::Integrity)?;
    let member = |k: &str| req.get(k).and_then(Value::as_str).map(str::to_string);
    Ok(w.task_id == b.task_id
        && w.state == b.from_state
        && w.revision == b.state_revision
        && w.artifact_snapshot_id == b.artifact_snapshot_id
        && w.artifact_digest == b.artifact_digest
        && sha == b.acceptance_policy_bundle_sha
        && member("acceptance_policy_id").as_deref() == Some(b.acceptance_policy_id.as_str())
        && member("acceptance_policy_version").as_deref()
            == Some(b.acceptance_policy_version.as_str()))
}

/// The verified registration equals a finished (`registered`) registration row of
/// `operation` for `subject`: same relying party, credential id, public material and
/// algorithm (ES256 only, D-041 DR-3), user verified (S5, X8). Anything else, including a
/// registration result that no row recorded (raw public material), is refused.
fn registration_recorded(
    conn: &Connection,
    reg: &VerifiedRegistration,
    subject: &SubjectId,
    operation: RegistrationOperation,
) -> Result<bool, BodyError> {
    let b = reg.binding();
    if b.operation != operation
        || &b.subject != subject
        || !reg.user_verified()
        || reg.algorithm() != ES256
    {
        return Ok(false);
    }
    let row = conn
        .query_row(
            "SELECT operation, issuer, subject, rp_id, origin, status, credential_id, \
             public_material, algorithm FROM registrations WHERE registration_id = ?1",
            [&b.registration_id],
            |r| {
                Ok((
                    [
                        r.get::<_, String>(0)?,
                        r.get::<_, String>(1)?,
                        r.get::<_, String>(2)?,
                        r.get::<_, String>(3)?,
                        r.get::<_, String>(4)?,
                        r.get::<_, String>(5)?,
                    ],
                    r.get::<_, Option<String>>(6)?,
                    r.get::<_, Option<Vec<u8>>>(7)?,
                    r.get::<_, Option<String>>(8)?,
                ))
            },
        )
        .optional()?;
    let Some(([op, issuer, sub, rp_id, origin, status], credential, material, algorithm)) = row
    else {
        return Ok(false);
    };
    Ok(op == operation.as_str()
        && issuer == subject.issuer
        && sub == subject.subject
        && rp_id == b.relying_party.rp_id
        && origin == b.relying_party.origin
        && status == "registered"
        && credential.as_deref() == Some(reg.credential_id())
        && material.as_deref() == Some(reg.public_material())
        && algorithm.as_deref() == Some(ES256))
}

/// The registration row of `e`'s credential is expired at `now` (its enrollment must commit
/// within [`REGISTRATION_TTL_SECONDS`] of issue).
fn registration_expired(
    conn: &Connection,
    e: &NewEnrollment,
    now: Timestamp,
) -> Result<bool, BodyError> {
    let Some(reg) = &e.credential else {
        return Ok(false);
    };
    let expires: Option<String> = conn
        .query_row(
            "SELECT expires_at FROM registrations WHERE registration_id = ?1",
            [reg.registration_id()],
            |r| r.get(0),
        )
        .optional()?;
    match expires {
        Some(at) => Ok(now >= ts(&at)?),
        None => Ok(true),
    }
}

/// The initiating operator and page session recorded on an `enroll` registration row
/// (D-043 Y2); `None` for an unknown row or one without them (bootstrap).
fn registration_operator(
    conn: &Connection,
    registration_id: &str,
) -> Result<Option<(SubjectId, String)>, BodyError> {
    let row: Option<(Option<String>, Option<String>, Option<String>)> = conn
        .query_row(
            "SELECT operator_issuer, operator_subject, operator_session FROM registrations \
             WHERE registration_id = ?1 AND operation = 'enroll'",
            [registration_id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .optional()?;
    Ok(match row {
        Some((Some(issuer), Some(subject), Some(session))) => {
            Some((SubjectId::new(&issuer, &subject), session))
        }
        _ => None,
    })
}

/// The enrollment of a human is performed by the operator who began its registration row,
/// under the relying party (RP id and origin) the row was issued for (D-043 Y2); a service
/// enrollment has no row.
fn enrollment_bound(
    conn: &Connection,
    e: &NewEnrollment,
    actor: &SubjectId,
    rp: &RelyingParty,
) -> Result<bool, BodyError> {
    let Some(c) = &e.credential else {
        return Ok(true);
    };
    let row_rp: Option<(String, String)> = conn
        .query_row(
            "SELECT rp_id, origin FROM registrations WHERE registration_id = ?1",
            [c.registration_id()],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .optional()?;
    let same_rp = row_rp.is_some_and(|(id, origin)| id == rp.rp_id && origin == rp.origin);
    Ok(same_rp
        && registration_operator(conn, c.registration_id())?
            .is_some_and(|(operator, _)| &operator == actor))
}

/// Why `e` cannot be enrolled (ENROLLMENT_UNSUPPORTED), if anything.
fn enrollment_blocked(
    conn: &Connection,
    e: &NewEnrollment,
    operation: RegistrationOperation,
) -> Result<bool, BodyError> {
    let identities_ok = e.subject.is_well_formed()
        && e.linked_identities.iter().all(SubjectId::is_well_formed)
        && e.roles
            .iter()
            .all(|r| !r.tenant_id.is_empty() && !r.project_id.is_empty());
    let credential_ok = match (e.kind, &e.credential) {
        (PrincipalKind::Human, Some(c)) => registration_recorded(conn, c, &e.subject, operation)?,
        (PrincipalKind::Service, None) => true,
        _ => false,
    };
    let roles_ok = e
        .roles
        .iter()
        .all(|r| check_service_permission(e.kind, r.permission).is_ok());
    if !(identities_ok && credential_ok && roles_ok) {
        return Ok(true);
    }
    let exists: bool = conn.query_row(
        "SELECT EXISTS (SELECT 1 FROM subjects WHERE issuer = ?1 AND subject = ?2)",
        (&e.subject.issuer, &e.subject.subject),
        |r| r.get(0),
    )?;
    let credential_taken = match &e.credential {
        Some(c) => conn.query_row(
            "SELECT EXISTS (SELECT 1 FROM credentials WHERE credential_id = ?1)",
            [c.credential_id()],
            |r| r.get(0),
        )?,
        None => false,
    };
    Ok(exists || credential_taken)
}

/// What a role change binds (D-042 X5), recomputed from stored state: the assignment's scope
/// (the requested scope of a grant, the stored scope of a revoked assignment) and the target's
/// stored record. Blocked (refused once the assertion is accepted, and at begin) when a grant's
/// subject is revoked or would break the service rule (O-8), or the assignment is revoked.
fn role_change_target(conn: &Connection, change: &RoleChange) -> Outcome<Target> {
    let (scope, record, subject, blocked) = match change {
        RoleChange::Grant {
            subject,
            permission,
            tenant_id,
            project_id,
        } => {
            if tenant_id.is_empty() || project_id.is_empty() || !subject.is_well_formed() {
                return Ok(Err(challenge_refused(ChallengeRefusal::Malformed)));
            }
            let Some(record) = subject_record(conn, subject)? else {
                return Ok(Err(unknown_target()));
            };
            let blocked = match (
                record.status,
                check_service_permission(record.kind, *permission),
            ) {
                (SubjectStatus::Revoked { .. }, _) => Some(ap(ApprovalCode::SubjectRevoked)),
                (_, Err(code)) => Some(ap(code)),
                _ => None,
            };
            let value = json::obj([
                ("kind", json::s(kind_text(record.kind))),
                ("subject", subject_value(subject)),
            ]);
            (
                (tenant_id.clone(), project_id.clone()),
                value,
                subject.clone(),
                blocked,
            )
        }
        RoleChange::Revoke { assignment_id } => {
            let row: Option<([String; 7], Option<String>)> = conn
                .query_row(
                    "SELECT issuer, subject, kind, permission, tenant_id, project_id, granted_at, \
                     revoked_at FROM role_assignments WHERE assignment_id = ?1",
                    [assignment_id],
                    |r| {
                        Ok((
                            [
                                r.get(0)?,
                                r.get(1)?,
                                r.get(2)?,
                                r.get(3)?,
                                r.get(4)?,
                                r.get(5)?,
                                r.get(6)?,
                            ],
                            r.get(7)?,
                        ))
                    },
                )
                .optional()?;
            let Some(([issuer, subject, kind, permission, tenant, project, granted], revoked)) =
                row
            else {
                return Ok(Err(unknown_target()));
            };
            let subject = SubjectId::new(&issuer, &subject);
            let value = json::obj([
                ("assignment_id", json::s(assignment_id)),
                ("granted_at", json::s(&granted)),
                ("kind", json::s(&kind)),
                ("permission", json::s(&permission)),
                ("project_id", json::s(&project)),
                ("subject", subject_value(&subject)),
                ("tenant_id", json::s(&tenant)),
            ]);
            let blocked = revoked.map(|_| ap(ApprovalCode::PermissionMissing));
            ((tenant, project), value, subject, blocked)
        }
    };
    let context = role_change_context(change, &scope.0, &scope.1, record.clone());
    let binding = role_change_binding(change, &scope.0, &scope.1, record)
        .map_err(|_| BodyError::Integrity)?;
    Ok(Ok(Target {
        binding,
        context,
        permission: Permission::Operator,
        expiry: None,
        blocked,
        kind: TargetKind::RoleChange { subject },
    }))
}

/// Where a signoff ceremony's effective expiry comes from (D-043 item 2, Y1).
#[derive(Clone, Copy)]
enum SignoffExpiry {
    /// Ceremony begin: computed now on this trusted time ([`signoff_expiry`]).
    Compute(Timestamp),
    /// Ceremony finish: exactly the value stored with the challenge (`None`: none stored,
    /// refused).
    Stored(Option<Timestamp>),
}

/// The trusted deadline of the stored promotion contexts of the signoff (Y1): the earliest
/// `deadline_at` of every `signoff_requests` row of the signoff id in its scope. `None`
/// when no row exists (the exclusions read refuses that case). A row without a deadline
/// bounds nothing: `SIGNOFF_MISMATCH` (fail closed).
fn context_deadline(conn: &Connection, b: &NewSignoff) -> Outcome<Option<Timestamp>> {
    let rows: Vec<Option<String>> = conn
        .prepare(
            "SELECT deadline_at FROM signoff_requests \
             WHERE signoff_id = ?1 AND tenant_id = ?2 AND project_id = ?3",
        )?
        .query_map((&b.signoff_id, &b.tenant_id, &b.project_id), |r| r.get(0))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    let mut deadline: Option<Timestamp> = None;
    for row in rows {
        let Some(text) = row else {
            return Ok(Err(ap(ApprovalCode::SignoffMismatch)));
        };
        let d = ts(&text)?;
        deadline = Some(deadline.map_or(d, |e| e.min(d)));
    }
    Ok(Ok(deadline))
}

fn target(
    conn: &Connection,
    op: &CeremonyOp,
    expiry: SignoffExpiry,
    rp: Option<&RelyingParty>,
) -> Outcome<Target> {
    match op {
        CeremonyOp::Approve { request_id } | CeremonyOp::Deny { request_id } => {
            let Some((request, status)) = load_request(conn, request_id)? else {
                return Ok(Err(ap(ApprovalCode::RequestUnknown)));
            };
            Ok(Ok(Target {
                binding: approval_binding(&request, op.operation()),
                context: request.context().clone(),
                permission: Permission::Approver,
                expiry: Some(request.expires_at()),
                blocked: None,
                kind: TargetKind::Request {
                    request: Box::new(request),
                    status,
                },
            }))
        }
        CeremonyOp::Revoke { target, reason } => revoke_target(conn, target, reason),
        CeremonyOp::Signoff {
            binding,
            parties,
            requested_expires_at,
        } => {
            // X7: the exclusions come from the ledger; the caller's list only adds.
            let (excluded, context_blocked) = match signoff_exclusions(conn, binding)? {
                None => (
                    SignoffExclusions::default(),
                    Some(ap(ApprovalCode::SignoffUnknown)),
                ),
                Some((stored, consistent)) => {
                    let added = SignoffExclusions {
                        added: parties.added.clone(),
                        ..SignoffExclusions::default()
                    };
                    (
                        stored.union(&added),
                        (!consistent).then(|| ap(ApprovalCode::SignoffMismatch)),
                    )
                }
            };
            if !excluded.well_formed() {
                return Ok(Err(challenge_refused(ChallengeRefusal::Malformed)));
            }
            // Y1: the effective expiry. At begin the ledger computes it on trusted time from
            // the 600 s maximum, the stored contexts' trusted deadline and the caller's
            // earlier request (which never extends it); at finish it is exactly the value
            // stored with the challenge (never recomputed, never moved).
            let (expires_at, expiry_blocked) = match expiry {
                SignoffExpiry::Compute(now) => {
                    match context_deadline(conn, binding)?
                        .and_then(|d| signoff_expiry(now, d, *requested_expires_at).map_err(ap))
                    {
                        Ok(e) => (e, None),
                        Err(refusal) => (now, Some(refusal)),
                    }
                }
                SignoffExpiry::Stored(Some(e)) => (e, None),
                SignoffExpiry::Stored(None) => {
                    return Ok(Err(challenge_refused(ChallengeRefusal::Malformed)))
                }
            };
            let Ok(challenge_binding) = signoff_binding(binding, &excluded, expires_at) else {
                return Ok(Err(challenge_refused(ChallengeRefusal::Malformed)));
            };
            let taken: bool = conn.query_row(
                "SELECT EXISTS (SELECT 1 FROM signoffs WHERE signoff_id = ?1)",
                [&binding.signoff_id],
                |r| r.get(0),
            )?;
            let current = signoff_current(conn, binding)?;
            let blocked = context_blocked
                .or_else(|| (taken || !current).then(|| ap(ApprovalCode::SignoffMismatch)))
                .or(expiry_blocked);
            Ok(Ok(Target {
                binding: challenge_binding,
                context: binding.context_with(&excluded, expires_at),
                permission: Permission::SignoffAuthority,
                expiry: Some(expires_at),
                blocked,
                kind: TargetKind::Signoff {
                    excluded: Box::new(excluded),
                    expires_at,
                },
            }))
        }
        CeremonyOp::Enroll {
            enrollment,
            operator_session,
        } => {
            let Ok(binding) = enrollment.binding() else {
                return Ok(Err(ap(ApprovalCode::EnrollmentUnsupported)));
            };
            // D-043 Y2: a human's registration row must belong to this operator session
            // (the operator is checked against the actor at begin and at finish).
            let session_ok = match &enrollment.credential {
                Some(c) => registration_operator(conn, c.registration_id())?
                    .is_some_and(|(_, session)| session == *operator_session),
                None => true,
            };
            let blocked = (!session_ok
                || enrollment_blocked(conn, enrollment, RegistrationOperation::Enroll)?)
            .then(|| ap(ApprovalCode::EnrollmentUnsupported));
            Ok(Ok(Target {
                binding,
                context: enrollment.context(),
                permission: Permission::Operator,
                expiry: None,
                blocked,
                kind: TargetKind::Enroll,
            }))
        }
        CeremonyOp::RoleChange { change } => role_change_target(conn, change),
        CeremonyOp::RegistrationStart {
            subject,
            operator,
            operator_session,
        } => {
            // M1: the row binds the configured channel's relying party; none configured,
            // no ceremony.
            let Some(rp) = rp else {
                return Ok(Err(no_channel()));
            };
            if !subject.is_well_formed() || !operator.is_well_formed() {
                return Ok(Err(ap(ApprovalCode::EnrollmentUnsupported)));
            }
            let exists: bool = conn.query_row(
                "SELECT EXISTS (SELECT 1 FROM subjects WHERE issuer = ?1 AND subject = ?2)",
                (&subject.issuer, &subject.subject),
                |r| r.get(0),
            )?;
            let blocked = if exists || operator_session.is_empty() || !bootstrapped(conn)? {
                Some(ap(ApprovalCode::EnrollmentUnsupported))
            } else if same_subject(operator, subject) {
                // No self-registration (no self-escalation), under any issuer.
                Some(ap(ApprovalCode::SeparationViolated))
            } else {
                None
            };
            let binding = registration_start_binding(subject, operator, rp)
                .map_err(|_| BodyError::Integrity)?;
            Ok(Ok(Target {
                binding,
                context: registration_start_context(subject, operator, rp),
                permission: Permission::Operator,
                expiry: None,
                blocked,
                kind: TargetKind::RegistrationStart,
            }))
        }
    }
}

// ---------------------------------------------------------------------------
// Ceremony transactions.

fn begin_in(
    tx: &Transaction<'_>,
    b: &CeremonyBegin<'_>,
    challenge_id: &str,
    channel: &ApprovalChannel,
) -> Outcome<IssuedChallenge> {
    let now = b.now.at();
    let t = check!(target(
        tx,
        b.op,
        SignoffExpiry::Compute(now),
        Some(channel.relying_party())
    )?);
    if let Some(refusal) = t.blocked.clone() {
        return Ok(Err(refusal));
    }
    // Y1: fixed before the challenge is issued; stored with it and fingerprinted.
    let signoff_expires_at = match &t.kind {
        TargetKind::Signoff { expires_at, .. } => Some(expires_at.to_string()),
        _ => None,
    };
    if let TargetKind::Request { request, status } = &t.kind {
        match status {
            ApprovalStatus::Pending if now >= request.expires_at() => {
                return Ok(Err(ap(ApprovalCode::RequestExpired)))
            }
            ApprovalStatus::Pending => {}
            ApprovalStatus::Expired => return Ok(Err(ap(ApprovalCode::RequestExpired))),
            ApprovalStatus::Approved | ApprovalStatus::Denied => {
                return Ok(Err(ap(ApprovalCode::RequestClosed)))
            }
        }
    }
    let Some(rows) = ActorRows::read(tx, b.subject)? else {
        return Ok(Err(ap(ApprovalCode::PermissionMissing)));
    };
    let creds = check!(rows.select(&t, now));
    // D-043 Y2: a registration start is performed by the operator it names.
    if let CeremonyOp::RegistrationStart { operator, .. } = b.op {
        if operator != b.subject {
            return Ok(Err(ap(ApprovalCode::EnrollmentUnsupported)));
        }
    }
    // D-043 Y2: only the operator who began the credential's registration row enrolls it,
    // under the row's relying party.
    if let CeremonyOp::Enroll { enrollment, .. } = b.op {
        if !enrollment_bound(tx, enrollment, b.subject, channel.relying_party())? {
            return Ok(Err(ap(ApprovalCode::EnrollmentUnsupported)));
        }
    }
    let start = check!(channel
        .begin(b.subject, &creds)
        .map_err(|e| ap(ApprovalCode::AssertionInvalid(e))));
    let challenge = check!(Challenge::issue(NewChallenge {
        challenge_id: challenge_id.to_string(),
        challenge_digest: start.challenge_digest,
        ceremony_state: start.state,
        subject: b.subject.clone(),
        allowed_credentials: creds.ids(),
        binding: t.binding,
        issued_at: now,
        target_expiry: t
            .expiry
            .unwrap_or_else(|| now.plus_seconds(CHALLENGE_TTL_SECONDS)),
        idempotency_key: b.idempotency_key.to_string(),
    })
    .map_err(challenge_refused));
    // C1: challenge ids and digests are unique ledger-wide (also UNIQUE in the schema).
    let duplicate: bool = tx.query_row(
        "SELECT EXISTS (SELECT 1 FROM challenges WHERE challenge_id = ?1 OR challenge_digest = ?2)",
        (
            challenge.challenge_id(),
            challenge.challenge_digest().as_str(),
        ),
        |r| r.get(0),
    )?;
    if duplicate {
        return Ok(Err(challenge_refused(ChallengeRefusal::Malformed)));
    }
    let allowed = Value::Arr(
        challenge
            .allowed_credentials()
            .iter()
            .map(|c| json::s(c))
            .collect(),
    );
    let bind = challenge.binding();
    tx.execute(
        "INSERT INTO challenges (challenge_id, challenge_digest, ceremony_state, issuer, subject, \
         allowed_credentials, operation, verdict, tenant_id, project_id, target_id, \
         context_fingerprint, policy_bundle_sha, revision, issued_at, expires_at, \
         idempotency_key, rp_id, origin, signoff_expires_at, status) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?17, \
         ?18, ?19, ?20, 'issued')",
        rusqlite::params![
            challenge.challenge_id(),
            challenge.challenge_digest().as_str(),
            challenge.ceremony_state().as_bytes(),
            challenge.subject().issuer,
            challenge.subject().subject,
            json::canonical(&allowed).map_err(|_| BodyError::Integrity)?,
            bind.operation.as_str(),
            challenge.verdict(),
            bind.tenant_id,
            bind.project_id,
            bind.target_id,
            bind.context_fingerprint.as_str(),
            bind.policy_bundle_sha,
            bind.revision,
            challenge.issued_at().to_string(),
            challenge.expires_at().to_string(),
            challenge.idempotency_key(),
            channel.relying_party().rp_id,
            channel.relying_party().origin,
            signoff_expires_at,
        ],
    )?;
    Ok(Ok(IssuedChallenge {
        challenge_id: challenge.challenge_id().to_string(),
        client_options: start.client_options,
        expires_at: challenge.expires_at(),
    }))
}

/// A stored challenge row with its digests as text.
struct ChallengeRow {
    digest: String,
    state: Vec<u8>,
    subject: SubjectId,
    allowed: Vec<String>,
    operation: ChallengeOperation,
    verdict: String,
    tenant_id: String,
    project_id: String,
    target_id: String,
    fingerprint: String,
    policy: String,
    revision: i64,
    issued_at: Timestamp,
    expires_at: Timestamp,
    key: String,
    relying_party: RelyingParty,
    status: ChallengeStatus,
    /// Y1: the effective signoff expiry stored at begin (signoff challenges only).
    signoff_expires_at: Option<Timestamp>,
}

fn challenge_row(conn: &Connection, id: &str) -> Result<Option<ChallengeRow>, BodyError> {
    let row = conn
        .query_row(
            "SELECT challenge_digest, ceremony_state, issuer, subject, allowed_credentials, \
             operation, verdict, tenant_id, project_id, target_id, context_fingerprint, \
             policy_bundle_sha, revision, issued_at, expires_at, idempotency_key, status, \
             rp_id, origin, signoff_expires_at \
             FROM challenges WHERE challenge_id = ?1",
            [id],
            |r| {
                let text = |i: usize| r.get::<_, String>(i);
                Ok((
                    r.get::<_, Vec<u8>>(1)?,
                    r.get::<_, i64>(12)?,
                    [
                        text(0)?,
                        text(2)?,
                        text(3)?,
                        text(4)?,
                        text(5)?,
                        text(6)?,
                        text(7)?,
                        text(8)?,
                        text(9)?,
                        text(10)?,
                        text(11)?,
                        text(13)?,
                        text(14)?,
                        text(15)?,
                        text(16)?,
                    ],
                    (text(17)?, text(18)?),
                    r.get::<_, Option<String>>(19)?,
                ))
            },
        )
        .optional()?;
    let Some((state, revision, t, (rp_id, origin), signoff_expires)) = row else {
        return Ok(None);
    };
    let [digest, issuer, subject, allowed, operation, verdict, tenant, project, target, fingerprint, policy, issued, expires, key, status] =
        t;
    let allowed = json::parse(allowed.as_bytes())
        .ok()
        .and_then(|v| {
            v.as_arr()?
                .iter()
                .map(|c| c.as_str().map(str::to_string))
                .collect::<Option<Vec<_>>>()
        })
        .ok_or(BodyError::Integrity)?;
    Ok(Some(ChallengeRow {
        digest,
        state,
        subject: SubjectId::new(&issuer, &subject),
        allowed,
        operation: ChallengeOperation::parse(&operation).ok_or(BodyError::Integrity)?,
        verdict,
        tenant_id: tenant,
        project_id: project,
        target_id: target,
        fingerprint,
        policy,
        revision,
        issued_at: ts(&issued)?,
        expires_at: ts(&expires)?,
        key,
        relying_party: RelyingParty { rp_id, origin },
        status: match status.as_str() {
            "issued" => ChallengeStatus::Issued,
            "consumed" => ChallengeStatus::Consumed,
            "expired" => ChallengeStatus::Expired,
            "refused" => ChallengeStatus::Refused,
            _ => return Err(BodyError::Integrity),
        },
        signoff_expires_at: signoff_expires.as_deref().map(ts).transpose()?,
    }))
}

/// Rebuild the stored challenge for `check` (module notes on stored digests).
fn rebuild(
    id: &str,
    row: ChallengeRow,
    assertion: &VerifiedAssertion,
    presented: &ChallengeBinding,
) -> Result<Challenge, Refusal> {
    let pick = |stored: &str, presented: &Digest| {
        if stored == presented.as_str() {
            presented.clone()
        } else {
            sentinel()
        }
    };
    Challenge::from_stored(
        &cap(),
        StoredChallenge {
            challenge_id: id.to_string(),
            challenge_digest: pick(&row.digest, assertion.challenge_digest()),
            ceremony_state: CeremonyState::from_bytes(row.state),
            subject: row.subject,
            allowed_credentials: row.allowed,
            binding: ChallengeBinding {
                operation: row.operation,
                tenant_id: row.tenant_id,
                project_id: row.project_id,
                target_id: row.target_id,
                context_fingerprint: pick(&row.fingerprint, &presented.context_fingerprint),
                policy_bundle_sha: row.policy,
                revision: row.revision,
            },
            verdict: row.verdict,
            issued_at: row.issued_at,
            expires_at: row.expires_at,
            idempotency_key: row.key,
            status: row.status,
        },
    )
    .map_err(challenge_refused)
}

/// An identical retry of a committed ceremony replays it (module documentation of
/// `writ_broker::approval_store`); anything else on a consumed challenge is refused.
fn replay(
    conn: &Connection,
    challenge_id: &str,
    row: &ChallengeRow,
    assertion: &VerifiedAssertion,
    presented: &ChallengeBinding,
    f: &CeremonyFinish<'_>,
) -> Outcome<CeremonyOutcome> {
    let consumed = || Ok(Err(challenge_refused(ChallengeRefusal::Consumed)));
    let event: Option<(String, Option<String>, Option<i64>)> = conn
        .query_row(
            "SELECT kind, credential_id, ledger_seq FROM approval_events WHERE challenge_id = ?1",
            [challenge_id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .optional()?;
    let Some((kind, credential, Some(seq))) = event else {
        return consumed();
    };
    // D-042 X3 (S4): the retry must equal the committed ceremony in idempotency key,
    // ceremony, subject, credential, operation, verdict, scope, target and the complete
    // context fingerprint recomputed from stored state; any difference is a binding mismatch,
    // never the earlier result.
    let operation = f.op.operation();
    let identical = row.key == f.idempotency_key
        && row.operation == operation
        && row.verdict == operation.verdict()
        && presented.operation == operation
        && assertion.subject() == &row.subject
        && credential.as_deref() == Some(assertion.credential_id())
        && assertion.challenge_digest().as_str() == row.digest
        && row.tenant_id == presented.tenant_id
        && row.project_id == presented.project_id
        && row.target_id == presented.target_id
        && row.fingerprint == presented.context_fingerprint.as_str();
    if !identical {
        return Ok(Err(challenge_refused(ChallengeRefusal::BindingMismatch)));
    }
    let result = match kind.as_str() {
        "approved" => {
            let label: Option<String> = conn.query_row(
                "SELECT grant_label FROM approval_decisions WHERE challenge_id = ?1",
                [challenge_id],
                |r| r.get(0),
            )?;
            CeremonyResult::Approved {
                grant: label
                    .as_deref()
                    .and_then(GrantLabel::new)
                    .ok_or(BodyError::Integrity)?,
            }
        }
        "denied" => CeremonyResult::Denied,
        "revoked" => {
            let state: String = conn.query_row(
                "SELECT use_state FROM revocations WHERE challenge_id = ?1",
                [challenge_id],
                |r| r.get(0),
            )?;
            CeremonyResult::Revoked {
                use_state: UseState::parse(&state).ok_or(BodyError::Integrity)?,
            }
        }
        "signed_off" => CeremonyResult::SignedOff {
            signoff_id: conn.query_row(
                "SELECT signoff_id FROM signoffs WHERE challenge_id = ?1",
                [challenge_id],
                |r| r.get(0),
            )?,
        },
        "enrolled" => {
            let (issuer, subject): (String, String) = conn.query_row(
                "SELECT issuer, subject FROM enrollments WHERE challenge_id = ?1",
                [challenge_id],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )?;
            CeremonyResult::Enrolled {
                subject: SubjectId::new(&issuer, &subject),
            }
        }
        "registration_started" => {
            let (id, options, expires, session): (String, Vec<u8>, String, String) = conn
                .query_row(
                    "SELECT registration_id, client_options, expires_at, operator_session \
                     FROM registrations WHERE started_by = ?1",
                    [challenge_id],
                    |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
                )?;
            // D-043 Y2: the page session is not fingerprinted; a retry from another session
            // is a binding mismatch, never the row of the original one.
            let same_session = matches!(
                f.op,
                CeremonyOp::RegistrationStart { operator_session, .. } if *operator_session == session
            );
            if !same_session {
                return Ok(Err(challenge_refused(ChallengeRefusal::BindingMismatch)));
            }
            CeremonyResult::RegistrationStarted {
                registration: IssuedRegistration {
                    registration_id: id,
                    client_options: options,
                    expires_at: ts(&expires)?,
                },
            }
        }
        "role_changed" => {
            let granted: Option<String> = conn
                .query_row(
                    "SELECT assignment_id FROM role_assignments WHERE assignment_id = ?1",
                    [format!("role:{challenge_id}")],
                    |r| r.get(0),
                )
                .optional()?;
            let assignment_id = match granted {
                Some(id) => id,
                None => conn.query_row(
                    "SELECT substr(target_id, 6) FROM revocations WHERE challenge_id = ?1 \
                     AND target_kind = 'role'",
                    [challenge_id],
                    |r| r.get(0),
                )?,
            };
            CeremonyResult::RoleChanged { assignment_id }
        }
        _ => return Err(BodyError::Integrity),
    };
    Ok(Ok(CeremonyOutcome {
        result,
        ledger_seq: seq,
        replayed: true,
    }))
}

/// Where a revocation target's use stands now (O-5), inside the revoking transaction.
fn use_state(conn: &Connection, target: &RevocationTarget) -> rusqlite::Result<UseState> {
    match target {
        RevocationTarget::Grant(label) => {
            let states: Vec<Option<String>> = conn
                .prepare(
                    "SELECT r.status FROM grant_uses u LEFT JOIN receipts r \
                     ON r.tenant_id = u.tenant_id AND r.idempotency_key = u.idempotency_key \
                     WHERE u.label = ?1 ORDER BY u.seq",
                )?
                .query_map([label], |r| r.get(0))?
                .collect::<rusqlite::Result<Vec<_>>>()?;
            Ok(match states.first() {
                None => UseState::Unused,
                Some(None) => UseState::Consumed,
                Some(Some(s)) if s == "reserved" => UseState::Reserved,
                Some(Some(_)) => UseState::Dispatched,
            })
        }
        RevocationTarget::Signoff(id) => {
            let consumed: bool = conn.query_row(
                "SELECT EXISTS (SELECT 1 FROM consumed_signoffs WHERE signoff_id = ?1)",
                [id],
                |r| r.get(0),
            )?;
            Ok(if consumed {
                UseState::Consumed
            } else {
                UseState::Unused
            })
        }
        _ => Ok(UseState::NotApplicable),
    }
}

#[allow(clippy::too_many_arguments)]
fn approval_event(
    tx: &Transaction<'_>,
    kind: &str,
    target_id: &str,
    tenant_id: &str,
    project_id: &str,
    actor: Option<&SubjectId>,
    credential_id: Option<&str>,
    channel: Option<&str>,
    at: Timestamp,
    challenge_id: Option<&str>,
    request: Option<(&str, &str)>,
    ledger_seq: Option<i64>,
) -> rusqlite::Result<()> {
    tx.execute(
        "INSERT INTO approval_events (kind, target_id, tenant_id, project_id, actor_issuer, \
         actor_subject, credential_id, channel, at, challenge_id, request_fingerprint, \
         policy_bundle_sha, provenance_version, ledger_seq) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14)",
        rusqlite::params![
            kind,
            target_id,
            tenant_id,
            project_id,
            actor.map(|a| a.issuer.as_str()),
            actor.map(|a| a.subject.as_str()),
            credential_id,
            channel,
            at.to_string(),
            challenge_id,
            request.map(|r| r.0),
            request.map(|r| r.1),
            PROVENANCE_VERSION_2,
            ledger_seq,
        ],
    )?;
    Ok(())
}

fn insert_subject(
    tx: &Transaction<'_>,
    s: &SubjectId,
    kind: PrincipalKind,
    at: Timestamp,
) -> rusqlite::Result<()> {
    tx.execute(
        "INSERT INTO subjects (issuer, subject, kind, created_at) VALUES (?1, ?2, ?3, ?4)",
        (&s.issuer, &s.subject, kind_text(kind), at.to_string()),
    )?;
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn write_enrollment(
    tx: &Transaction<'_>,
    e: &NewEnrollment,
    by: &SubjectId,
    at: Timestamp,
    challenge_id: Option<&str>,
    role_prefix: &str,
) -> Result<(), BodyError> {
    // Validated as an enrollment record before anything is written.
    let record = EnrollmentRecord::new(
        &cap(),
        e.subject.clone(),
        e.linked_identities.clone(),
        at,
        by.clone(),
    )
    .map_err(|_| BodyError::Integrity)?;
    insert_subject(tx, &e.subject, e.kind, at)?;
    if let Some(c) = &e.credential {
        // S5, X8: the registration row is consumed (single use) and the credential is copied
        // from it, never from the caller's value (checked equal by `enrollment_blocked`).
        let consumed = tx.execute(
            "UPDATE registrations SET status = 'enrolled', consumed_by = ?2 \
             WHERE registration_id = ?1 AND status = 'registered'",
            (
                c.registration_id(),
                challenge_id.unwrap_or(BOOTSTRAP_SUBJECT),
            ),
        )?;
        if consumed != 1 {
            return Err(BodyError::StateConflict);
        }
        tx.execute(
            "INSERT INTO credentials (credential_id, issuer, subject, public_material, status, \
             enrolled_at, enrolled_by_issuer, enrolled_by) \
             SELECT credential_id, ?2, ?3, public_material, 'active', ?4, ?5, ?6 \
             FROM registrations WHERE registration_id = ?1",
            rusqlite::params![
                c.registration_id(),
                e.subject.issuer,
                e.subject.subject,
                at.to_string(),
                by.issuer,
                by.subject,
            ],
        )?;
    }
    tx.execute(
        "INSERT INTO enrollments (issuer, subject, enrolled_at, enrolled_by_issuer, enrolled_by, \
         challenge_id) VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
        rusqlite::params![
            e.subject.issuer,
            e.subject.subject,
            at.to_string(),
            by.issuer,
            by.subject,
            challenge_id,
        ],
    )?;
    for l in record.linked_identities() {
        tx.execute(
            "INSERT INTO enrollment_links (issuer, subject, linked_issuer, linked_subject) \
             VALUES (?1, ?2, ?3, ?4)",
            (&e.subject.issuer, &e.subject.subject, &l.issuer, &l.subject),
        )?;
    }
    let mut roles: Vec<(&str, &str, &str)> = e
        .roles
        .iter()
        .map(|r| {
            (
                permission_text(r.permission),
                r.tenant_id.as_str(),
                r.project_id.as_str(),
            )
        })
        .collect();
    roles.sort();
    roles.dedup();
    for (i, (permission, tenant, project)) in roles.iter().enumerate() {
        tx.execute(
            "INSERT INTO role_assignments (assignment_id, issuer, subject, kind, permission, \
             tenant_id, project_id, granted_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
            rusqlite::params![
                format!("role:{role_prefix}:{i}"),
                e.subject.issuer,
                e.subject.subject,
                kind_text(e.kind),
                permission,
                tenant,
                project,
                at.to_string(),
            ],
        )?;
    }
    Ok(())
}

/// Subjects who changed the artifact of the signoff's workflow, and who activated the
/// project's acceptance policy, from durable administrative events (C2, X7). Administrative
/// events record no revision, so this is every such change of the workflow and project, a
/// superset of the changes for the exact revision (fail closed).
fn changers(
    conn: &Connection,
    b: &NewSignoff,
) -> rusqlite::Result<(Vec<SubjectId>, Vec<SubjectId>)> {
    let read = |sql: &str, args: &[&str]| -> rusqlite::Result<Vec<SubjectId>> {
        let mut stmt = conn.prepare(sql)?;
        let rows = stmt
            .query_map(rusqlite::params_from_iter(args.iter()), |r| {
                Ok(SubjectId {
                    issuer: r.get(0)?,
                    subject: r.get(1)?,
                })
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(rows)
    };
    let artifact = read(
        "SELECT DISTINCT operator_issuer, operator_id FROM admin_events \
         WHERE operation IN ('create_workflow', 'update_artifact') \
         AND tenant_id = ?1 AND project_id = ?2 AND target = ?3 ORDER BY 1, 2",
        &[&b.tenant_id, &b.project_id, &b.workflow_id],
    )?;
    let policy = read(
        "SELECT DISTINCT operator_issuer, operator_id FROM admin_events \
         WHERE operation = 'activate_acceptance_policy' AND tenant_id = ?1 AND project_id = ?2 \
         ORDER BY 1, 2",
        &[&b.tenant_id, &b.project_id],
    )?;
    Ok((artifact, policy))
}

/// Insert `grant` with `uses` 0 and its provenance (the label must be free). Private to
/// this module: reached only from the approval ceremony in [`finish_in`], which writes the
/// grant's `approval_grants` link in the same transaction (D-042 X1).
fn insert_grant_rows(tx: &Transaction<'_>, grant: &NewGrant) -> rusqlite::Result<()> {
    let expires = grant.expires_at();
    tx.execute(
        "INSERT INTO grants (label, binding, expires_s, expires_ns, max_uses, uses) \
         VALUES (?1, ?2, ?3, ?4, ?5, 0)",
        (
            grant.label().as_str(),
            grant.binding(),
            expires.unix_seconds(),
            i64::from(expires.subsec_nanos()),
            i64::from(grant.max_uses()),
        ),
    )?;
    let p = grant.provenance();
    tx.execute(
        "INSERT INTO grant_provenance (label, version, approver_id, approver_issuer, \
         channel, approved_at, verdict, auth_evidence_ref, request_fingerprint, \
         policy_bundle_sha) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)",
        rusqlite::params![
            grant.label().as_str(),
            p.version(),
            p.approver_id(),
            p.issuer(),
            p.channel(),
            p.approved_at().to_string(),
            p.verdict().as_str(),
            p.evidence_ref(),
            p.request_fingerprint(),
            p.policy_bundle_sha(),
        ],
    )?;
    Ok(())
}

fn finish_in(
    tx: &Transaction<'_>,
    f: &CeremonyFinish<'_>,
    channel: &ApprovalChannel,
) -> Outcome<CeremonyOutcome> {
    let now = f.now.at();
    let Some(row) = challenge_row(tx, f.challenge_id)? else {
        return Ok(Err(challenge_refused(ChallengeRefusal::Unknown)));
    };
    if row.status == ChallengeStatus::Refused {
        // X4: burned by an earlier terminal refusal; never fresh again.
        return Ok(Err(challenge_refused(ChallengeRefusal::Consumed)));
    }
    // M1: the challenge was issued for the configured channel's RP id and origin; another
    // channel never sees its ceremony state (refused before the verifier runs; burnt, X4).
    if &row.relying_party != channel.relying_party() {
        return Ok(Err(challenge_refused(ChallengeRefusal::ChannelMismatch)));
    }
    let t = check!(target(
        tx,
        f.op,
        SignoffExpiry::Stored(row.signoff_expires_at),
        Some(channel.relying_party())
    )?);
    let Some(rows) = ActorRows::read(tx, &row.subject)? else {
        return Ok(Err(ap(ApprovalCode::PermissionMissing)));
    };
    let creds = check!(rows.select(&t, now));
    let assertion = check!(channel
        .finish(
            &CeremonyState::from_bytes(row.state.clone()),
            f.response,
            &creds
        )
        .map_err(|e| ap(ApprovalCode::AssertionInvalid(e))));
    if row.status == ChallengeStatus::Consumed {
        return replay(tx, f.challenge_id, &row, &assertion, &t.binding, f);
    }
    let challenge = check!(rebuild(f.challenge_id, row, &assertion, &t.binding));
    let accepted = check!(assertion
        .check(&challenge, &t.binding, f.idempotency_key, now)
        .map_err(ap));
    if let Some(refusal) = t.blocked.clone() {
        return Ok(Err(refusal));
    }
    let actor = rows.actor();
    let subject = accepted.subject().clone();
    let credential = accepted.credential_id().to_string();
    let b = accepted.binding().clone();
    let (result, kind, request_ref): (CeremonyResult, &str, Option<(String, String)>);
    let seq;
    match (f.op, t.kind) {
        (
            CeremonyOp::Approve { .. } | CeremonyOp::Deny { .. },
            TargetKind::Request { request, status },
        ) => {
            let (new_status, decision) =
                check!(decide(&request, status, &accepted, &actor, channel.channel()).map_err(ap));
            let grant = match new_status {
                ApprovalStatus::Approved => Some(
                    NewGrant::for_decision(&request, &decision, APPROVAL_GRANT_TTL_SECONDS)
                        .ok_or(BodyError::Integrity)?,
                ),
                _ => None,
            };
            if let Some(g) = &grant {
                let taken: bool = tx.query_row(
                    "SELECT EXISTS (SELECT 1 FROM grants WHERE label = ?1)",
                    [g.label().as_str()],
                    |r| r.get(0),
                )?;
                if taken {
                    return Ok(Err(Refusal::GrantRefInUse));
                }
            }
            seq = next_seq(tx, "decision", f.challenge_id)?;
            let changed = tx.execute(
                "UPDATE approval_requests SET status = ?2 WHERE request_id = ?1 AND status = 'pending'",
                (request.request_id(), new_status.as_str()),
            )?;
            if changed != 1 {
                return Err(BodyError::StateConflict);
            }
            if let Some(g) = &grant {
                insert_grant_rows(tx, g)?;
                tx.execute(
                    "INSERT INTO approval_grants (label, request_id, challenge_id) VALUES (?1, ?2, ?3)",
                    (g.label().as_str(), request.request_id(), f.challenge_id),
                )?;
            }
            let d = decision.fields();
            tx.execute(
                "INSERT INTO approval_decisions (request_id, verdict, issuer, subject, \
                 credential_id, channel, decided_at, challenge_id, provenance_version, grant_label) \
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)",
                rusqlite::params![
                    d.request_id,
                    d.verdict.as_str(),
                    d.subject.issuer,
                    d.subject.subject,
                    d.credential_id,
                    d.channel,
                    d.decided_at.to_string(),
                    d.challenge_id,
                    d.provenance_version,
                    grant.as_ref().map(|g| g.label().as_str()),
                ],
            )?;
            let r = request.fields();
            request_ref = Some((r.request_fingerprint.clone(), r.policy_bundle_sha.clone()));
            (result, kind) = match grant {
                Some(g) => (
                    CeremonyResult::Approved {
                        grant: g.label().clone(),
                    },
                    "approved",
                ),
                None => {
                    debug_assert_eq!(d.verdict, Verdict::Denied);
                    (CeremonyResult::Denied, "denied")
                }
            };
        }
        (CeremonyOp::Revoke { target, reason }, TargetKind::Revoke { grant_approver }) => {
            let own = match (target, &grant_approver) {
                (RevocationTarget::Grant(label), Some((approver, true))) => Some((label, approver)),
                _ => None,
            };
            let revocation = check!(writ_broker::approval::Revocation::record(
                target.clone(),
                reason,
                &accepted,
                &actor,
                own.map(|(label, approver)| OwnUnusedGrant {
                    grant_label: label,
                    approved_by: approver,
                }),
            )
            .map_err(ap));
            let state = use_state(tx, target)?;
            seq = next_seq(tx, "revocation", f.challenge_id)?;
            let r = revocation.fields();
            tx.execute(
                "INSERT INTO revocations (target_kind, target_id, by_issuer, by_subject, at, reason, \
                 challenge_id, use_state, ledger_seq) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
                rusqlite::params![
                    target_kind_text(&r.target),
                    r.target.target_id(),
                    r.by.issuer,
                    r.by.subject,
                    r.at.to_string(),
                    r.reason,
                    r.challenge_id,
                    state.as_str(),
                    seq,
                ],
            )?;
            let marked = match &r.target {
                RevocationTarget::Credential(id) => tx.execute(
                    "UPDATE credentials SET status = 'revoked' WHERE credential_id = ?1 \
                     AND status = 'active'",
                    [id],
                )?,
                RevocationTarget::Subject(s) => tx.execute(
                    "UPDATE subjects SET revoked_at = ?3 WHERE issuer = ?1 AND subject = ?2 \
                     AND revoked_at IS NULL",
                    (&s.issuer, &s.subject, r.at.to_string()),
                )?,
                RevocationTarget::Role(id) => tx.execute(
                    "UPDATE role_assignments SET revoked_at = ?2 WHERE assignment_id = ?1 \
                     AND revoked_at IS NULL",
                    (id, r.at.to_string()),
                )?,
                RevocationTarget::Grant(_) | RevocationTarget::Signoff(_) => 1,
            };
            if marked != 1 {
                return Err(BodyError::StateConflict);
            }
            request_ref = None;
            result = CeremonyResult::Revoked { use_state: state };
            kind = "revoked";
        }
        (
            CeremonyOp::Signoff { binding, .. },
            TargetKind::Signoff {
                excluded,
                expires_at,
            },
        ) => {
            // X7: the exclusions read in this transaction; the challenge bound the ones read
            // at begin (a change in between is a context mismatch). Y1: the expiry is the
            // one stored with the challenge at begin.
            let signoff = check!(Signoff::sign(
                (**binding).clone(),
                expires_at,
                &accepted,
                &actor,
                &excluded
            )
            .map_err(ap));
            seq = next_seq(tx, "signoff", f.challenge_id)?;
            let s = signoff.fields();
            let n = &s.binding;
            tx.execute(
                "INSERT INTO signoffs (signoff_id, tenant_id, project_id, task_id, workflow_id, \
                 from_state, to_state, acceptance_record_id, criterion_id, artifact_snapshot_id, \
                 artifact_digest, acceptance_policy_id, acceptance_policy_version, \
                 acceptance_policy_bundle_sha, state_revision, issued_at, expires_at, issuer, \
                 subject, credential_id, challenge_id, provenance_version, exclusions) VALUES \
                 (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?17, \
                 ?18, ?19, ?20, ?21, ?22, ?23)",
                rusqlite::params![
                    n.signoff_id,
                    n.tenant_id,
                    n.project_id,
                    n.task_id,
                    n.workflow_id,
                    n.from_state,
                    n.to_state,
                    n.acceptance_record_id,
                    n.criterion_id,
                    n.artifact_snapshot_id,
                    n.artifact_digest,
                    n.acceptance_policy_id,
                    n.acceptance_policy_version,
                    n.acceptance_policy_bundle_sha,
                    n.state_revision,
                    s.issued_at.to_string(),
                    s.expires_at.to_string(),
                    s.subject.issuer,
                    s.subject.subject,
                    s.credential_id,
                    s.challenge_id,
                    s.provenance_version,
                    json::canonical(&s.exclusions.context()).map_err(|_| BodyError::Integrity)?,
                ],
            )?;
            request_ref = None;
            result = CeremonyResult::SignedOff {
                signoff_id: n.signoff_id.clone(),
            };
            kind = "signed_off";
        }
        (CeremonyOp::Enroll { enrollment, .. }, TargetKind::Enroll) => {
            check!(check_actor(&actor, &accepted, Permission::Operator).map_err(ap));
            // D-043 Y2: only the operator who began the registration row enrolls it, under
            // the row's relying party.
            if !enrollment_bound(tx, enrollment, &subject, channel.relying_party())? {
                return Ok(Err(ap(ApprovalCode::EnrollmentUnsupported)));
            }
            if same_subject(&subject, &enrollment.subject)
                || enrollment
                    .linked_identities
                    .iter()
                    .any(|l| same_subject(&subject, l))
            {
                return Ok(Err(ap(ApprovalCode::SeparationViolated)));
            }
            if registration_expired(tx, enrollment, now)? {
                return Ok(Err(ap(ApprovalCode::EnrollmentUnsupported)));
            }
            seq = next_seq(tx, "enrollment", f.challenge_id)?;
            write_enrollment(
                tx,
                enrollment,
                &subject,
                now,
                Some(f.challenge_id),
                f.challenge_id,
            )?;
            request_ref = None;
            result = CeremonyResult::Enrolled {
                subject: enrollment.subject.clone(),
            };
            kind = "enrolled";
        }
        (CeremonyOp::RoleChange { change }, TargetKind::RoleChange { subject: changed }) => {
            // X5: an operator over a scope containing the assignment's scope (the binding's
            // scope), never for its own assignments; the service rule was checked as `blocked`
            // and is checked again here and by the schema.
            check!(check_actor(&actor, &accepted, Permission::Operator).map_err(ap));
            if same_subject(&subject, &changed) {
                return Ok(Err(ap(ApprovalCode::SeparationViolated)));
            }
            seq = next_seq(tx, "administration", f.challenge_id)?;
            let assignment_id = match change {
                RoleChange::Grant {
                    subject: s,
                    permission,
                    tenant_id,
                    project_id,
                } => {
                    let record = subject_record(tx, s)?.ok_or(BodyError::Integrity)?;
                    check!(check_service_permission(record.kind, *permission).map_err(ap));
                    let id = format!("role:{}", f.challenge_id);
                    tx.execute(
                        "INSERT INTO role_assignments (assignment_id, issuer, subject, kind, \
                         permission, tenant_id, project_id, granted_at) \
                         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
                        rusqlite::params![
                            id,
                            s.issuer,
                            s.subject,
                            kind_text(record.kind),
                            permission_text(*permission),
                            tenant_id,
                            project_id,
                            now.to_string(),
                        ],
                    )?;
                    id
                }
                RoleChange::Revoke { assignment_id } => {
                    let marked = tx.execute(
                        "UPDATE role_assignments SET revoked_at = ?2 WHERE assignment_id = ?1 \
                         AND revoked_at IS NULL",
                        (assignment_id, now.to_string()),
                    )?;
                    if marked != 1 {
                        return Err(BodyError::StateConflict);
                    }
                    tx.execute(
                        "INSERT INTO revocations (target_kind, target_id, by_issuer, by_subject, \
                         at, reason, challenge_id, use_state, ledger_seq) \
                         VALUES ('role', ?1, ?2, ?3, ?4, 'role change', ?5, 'not_applicable', ?6)",
                        rusqlite::params![
                            RevocationTarget::Role(assignment_id.clone()).target_id(),
                            subject.issuer,
                            subject.subject,
                            now.to_string(),
                            f.challenge_id,
                            seq,
                        ],
                    )?;
                    assignment_id.clone()
                }
            };
            request_ref = None;
            result = CeremonyResult::RoleChanged { assignment_id };
            kind = "role_changed";
        }
        (
            CeremonyOp::RegistrationStart {
                subject: intended,
                operator,
                operator_session,
            },
            TargetKind::RegistrationStart,
        ) => {
            // D-043 Y2: an authenticated operator in the global scope, the one the operation
            // names, never registering itself (checked as `blocked` and again here).
            check!(check_actor(&actor, &accepted, Permission::Operator).map_err(ap));
            if &subject != operator {
                return Ok(Err(ap(ApprovalCode::EnrollmentUnsupported)));
            }
            if same_subject(&subject, intended) {
                return Ok(Err(ap(ApprovalCode::SeparationViolated)));
            }
            let start = match channel.begin_registration(intended) {
                Ok(start) if !start.state.is_empty() => start,
                Ok(_) => {
                    return Ok(Err(ap(ApprovalCode::AssertionInvalid(
                        VerifierError::Malformed,
                    ))))
                }
                // L6: an unavailable verifier is not a terminal refusal (not burnt).
                Err(VerifierError::Unavailable) => return Ok(Err(no_channel())),
                Err(e) => return Ok(Err(ap(ApprovalCode::AssertionInvalid(e)))),
            };
            seq = next_seq(tx, "registration", f.challenge_id)?;
            let rp = channel.relying_party();
            let registration_id = format!("reg-{}", f.challenge_id);
            let expires_at = now.plus_seconds(REGISTRATION_TTL_SECONDS);
            tx.execute(
                "INSERT INTO registrations (registration_id, operation, issuer, subject, rp_id, \
                 origin, ceremony_state, issued_at, expires_at, status, operator_issuer, \
                 operator_subject, operator_session, started_by, client_options) \
                 VALUES (?1, 'enroll', ?2, ?3, ?4, ?5, ?6, ?7, ?8, 'issued', ?9, ?10, ?11, ?12, \
                 ?13)",
                rusqlite::params![
                    registration_id,
                    intended.issuer,
                    intended.subject,
                    rp.rp_id,
                    rp.origin,
                    start.state.as_bytes(),
                    now.to_string(),
                    expires_at.to_string(),
                    operator.issuer,
                    operator.subject,
                    operator_session,
                    f.challenge_id,
                    start.client_options,
                ],
            )?;
            request_ref = None;
            result = CeremonyResult::RegistrationStarted {
                registration: IssuedRegistration {
                    registration_id,
                    client_options: start.client_options,
                    expires_at,
                },
            };
            kind = "registration_started";
        }
        _ => return Err(BodyError::Integrity),
    }
    let consumed = tx.execute(
        "UPDATE challenges SET status = 'consumed' WHERE challenge_id = ?1 AND status = 'issued'",
        [f.challenge_id],
    )?;
    if consumed != 1 {
        return Err(BodyError::StateConflict);
    }
    if let Some(u) = accepted.credential_update() {
        tx.execute(
            "UPDATE credentials SET public_material = ?2 WHERE credential_id = ?1 AND status = 'active'",
            (&u.credential_id, &u.public_material),
        )?;
    }
    approval_event(
        tx,
        kind,
        &b.target_id,
        &b.tenant_id,
        &b.project_id,
        Some(&subject),
        Some(&credential),
        Some(channel.channel()),
        now,
        Some(f.challenge_id),
        request_ref
            .as_ref()
            .map(|(fp, sha)| (fp.as_str(), sha.as_str())),
        Some(seq),
    )?;
    Ok(Ok(CeremonyOutcome {
        result,
        ledger_seq: seq,
        replayed: false,
    }))
}

/// One random identifier (`<prefix>-<32 hex>`) from the operating system's random source.
fn random_id(prefix: &str) -> Result<String, TxFailure> {
    use std::io::Read;
    let mut bytes = [0u8; 16];
    std::fs::File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut bytes))
        .map_err(|_| {
            TxFailure::new(
                TxStage::Begin,
                unavailable("transaction challenge_issue", "io"),
            )
        })?;
    let hex: String = bytes.iter().map(|b| format!("{b:02x}")).collect();
    Ok(format!("{prefix}-{hex}"))
}

/// Map a body answer to its transaction end: refusals and replays roll back.
fn end<T>(out: Result<T, Refusal>, replayed: impl Fn(&T) -> bool) -> End<Result<T, Refusal>> {
    match out {
        Ok(v) if !replayed(&v) => End::Commit(Ok(v)),
        other => End::Rollback(other),
    }
}

impl SqliteLedger {
    /// Audit a refusal of the approval channel in its own transaction (storage rule 2: a
    /// failed audit never changes the answer).
    fn audited<T>(
        &self,
        result: Result<Result<T, Refusal>, TxFailure>,
    ) -> Result<Result<T, Refusal>, TxFailure> {
        if let Ok(Err(refusal)) = &result {
            let _ = self.audit(refusal.code());
        }
        result
    }

    /// The `approval_request` transaction (`ExecuteLedger::record_approval_request`).
    pub(crate) fn record_request(
        &self,
        request: &ApprovalRequest,
    ) -> Result<RequestRecorded, TxFailure> {
        self.transact("approval_request", |tx| {
            let status: Option<String> = tx
                .query_row(
                    "SELECT status FROM approval_requests WHERE request_id = ?1",
                    [request.request_id()],
                    |r| r.get(0),
                )
                .optional()?;
            if let Some(status) = status {
                let status = ApprovalStatus::parse(&status).ok_or(BodyError::Integrity)?;
                return Ok(End::Rollback(RequestRecorded::Exists(status)));
            }
            let r = request.fields();
            let context = json::canonical(request.context()).map_err(|_| BodyError::Integrity)?;
            tx.execute(
                "INSERT INTO approval_requests (request_id, tenant_id, project_id, context, \
                 context_fingerprint, created_at, expires_at, status) \
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, 'pending')",
                rusqlite::params![
                    r.request_id,
                    r.tenant_id,
                    r.project_id,
                    context,
                    request.fingerprint().as_str(),
                    r.created_at.to_string(),
                    r.expires_at.to_string(),
                ],
            )?;
            approval_event(
                tx,
                "request_created",
                &r.request_id,
                &r.tenant_id,
                &r.project_id,
                None,
                None,
                None,
                r.created_at,
                None,
                Some((&r.request_fingerprint, &r.policy_bundle_sha)),
                None,
            )?;
            Ok(End::Commit(RequestRecorded::Created))
        })
    }

    /// The `registration_issue` transaction (S5, X8; [`RegistrationLedger`]) for
    /// `operation`, which is the caller's phase, never a request field: `Bootstrap` only on
    /// a ledger that has never had a subject or a bootstrap, `Enroll` only on one that has.
    fn begin_registration_as(
        &self,
        b: &RegistrationBegin<'_>,
        operation: RegistrationOperation,
    ) -> Result<Result<IssuedRegistration, Refusal>, TxFailure> {
        let Some(channel) = self.approval_channel() else {
            return self.audited(Ok(Err(no_channel())));
        };
        let registration_id = random_id("reg")?;
        let now = b.now.at();
        let result = self.transact("registration_issue", |tx| {
            let unsupported = || Ok(End::Rollback(Err(ap(ApprovalCode::EnrollmentUnsupported))));
            if !b.subject.is_well_formed()
                || bootstrapped(tx)? != (operation == RegistrationOperation::Enroll)
            {
                return unsupported();
            }
            let exists: bool = tx.query_row(
                "SELECT EXISTS (SELECT 1 FROM subjects WHERE issuer = ?1 AND subject = ?2)",
                (&b.subject.issuer, &b.subject.subject),
                |r| r.get(0),
            )?;
            if exists {
                return unsupported();
            }
            // D-043 Y2: the serving ledger creates an `enroll` row only in a consumed
            // `RegistrationStart` ceremony of an authenticated operator, never here; this
            // transaction issues only the one-shot bootstrap row.
            if operation != RegistrationOperation::Bootstrap || b.operator.is_some() {
                return unsupported();
            }
            let operator: Option<&OperatorSession> = None;
            let rp = channel.relying_party().clone();
            let start = match channel.begin_registration(b.subject) {
                Ok(s) => s,
                Err(e) => return Ok(End::Rollback(Err(ap(ApprovalCode::AssertionInvalid(e))))),
            };
            if start.state.is_empty() {
                return Ok(End::Rollback(Err(ap(ApprovalCode::AssertionInvalid(
                    VerifierError::Malformed,
                )))));
            }
            let expires_at = now.plus_seconds(REGISTRATION_TTL_SECONDS);
            tx.execute(
                "INSERT INTO registrations (registration_id, operation, issuer, subject, rp_id, \
                 origin, ceremony_state, issued_at, expires_at, status, operator_issuer, \
                 operator_subject, operator_session) \
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, 'issued', ?10, ?11, ?12)",
                rusqlite::params![
                    registration_id,
                    operation.as_str(),
                    b.subject.issuer,
                    b.subject.subject,
                    rp.rp_id,
                    rp.origin,
                    start.state.as_bytes(),
                    now.to_string(),
                    expires_at.to_string(),
                    operator.map(|o| o.operator.issuer.as_str()),
                    operator.map(|o| o.operator.subject.as_str()),
                    operator.map(|o| o.session_id.as_str()),
                ],
            )?;
            Ok(End::Commit(Ok(IssuedRegistration {
                registration_id: registration_id.clone(),
                client_options: start.client_options,
                expires_at,
            })))
        });
        self.audited(result)
    }

    /// The `registration` transaction (S5, X8; [`RegistrationLedger`]): the row must be
    /// `issued`, of `operation`, unexpired and for the configured channel's relying party
    /// (otherwise `ENROLLMENT_UNSUPPORTED`, the row burnt); the
    /// verifier's answer must be for exactly this row, user verified, ES256 (D-041 DR-3)
    /// and of an unused credential id. The row then records the credential (`registered`);
    /// a refused answer burns it (`refused`, committed with the refusal).
    fn finish_registration_as(
        &self,
        f: &RegistrationFinish<'_>,
        operation: RegistrationOperation,
    ) -> Result<Result<VerifiedRegistration, Refusal>, TxFailure> {
        let Some(channel) = self.approval_channel() else {
            return self.audited(Ok(Err(no_channel())));
        };
        let now = f.now.at();
        let result = self.transact("registration", |tx| {
            let row = tx
                .query_row(
                    "SELECT operation, issuer, subject, rp_id, origin, status, expires_at, \
                     ceremony_state FROM registrations WHERE registration_id = ?1",
                    [f.registration_id],
                    |r| {
                        Ok((
                            [
                                r.get::<_, String>(0)?,
                                r.get::<_, String>(1)?,
                                r.get::<_, String>(2)?,
                                r.get::<_, String>(3)?,
                                r.get::<_, String>(4)?,
                                r.get::<_, String>(5)?,
                                r.get::<_, String>(6)?,
                            ],
                            r.get::<_, Vec<u8>>(7)?,
                        ))
                    },
                )
                .optional()?;
            let recorded = registration_operator(tx, f.registration_id)?;
            let refuse = |r: ChallengeRefusal| Ok(End::Rollback(Err(challenge_refused(r))));
            let Some(([op, issuer, subject, rp_id, origin, status, expires], state)) = row else {
                return refuse(ChallengeRefusal::Unknown);
            };
            if op != operation.as_str() {
                return refuse(ChallengeRefusal::Unknown);
            }
            if status != "issued" {
                return refuse(ChallengeRefusal::Consumed);
            }
            if now >= ts(&expires)? {
                return refuse(ChallengeRefusal::Expired);
            }
            let burn = |refusal: Refusal| -> Result<End<Result<VerifiedRegistration, Refusal>>, BodyError> {
                tx.execute(
                    "UPDATE registrations SET status = 'refused', ceremony_state = x'' \
                     WHERE registration_id = ?1 AND status = 'issued'",
                    [f.registration_id],
                )?;
                Ok(End::Commit(Err(refusal)))
            };
            let assertion = |e: VerifierError| ap(ApprovalCode::AssertionInvalid(e));
            // D-043 Y2: only the operator session that began the row finishes it; another
            // operator or session (or none) burns the row.
            let same_session = match (&recorded, f.operator) {
                (Some((operator, session)), Some(o)) => {
                    operator == &o.operator && session == &o.session_id
                }
                (None, None) => operation == RegistrationOperation::Bootstrap,
                _ => false,
            };
            if !same_session {
                return burn(ap(ApprovalCode::EnrollmentUnsupported));
            }
            let binding = RegistrationBinding {
                registration_id: f.registration_id.to_string(),
                operation,
                subject: SubjectId::new(&issuer, &subject),
                relying_party: RelyingParty { rp_id, origin },
            };
            // M1: a configured channel with another RP id or origin never sees the row's
            // ceremony state; the row is burnt.
            if channel.relying_party() != &binding.relying_party {
                return burn(ap(ApprovalCode::EnrollmentUnsupported));
            }
            let reg = match channel.finish_registration(
                &binding,
                &CeremonyState::from_bytes(state),
                f.response,
            ) {
                Ok(reg) => reg,
                // L6: a transient verifier outage is not a terminal refusal; the row stays
                // issued (still single use and expiring).
                Err(VerifierError::Unavailable) => {
                    return Ok(End::Rollback(Err(assertion(VerifierError::Unavailable))))
                }
                Err(e) => return burn(assertion(e)),
            };
            if reg.binding() != &binding {
                return burn(assertion(VerifierError::Rejected));
            }
            if !reg.user_verified() {
                return burn(assertion(VerifierError::UserNotVerified));
            }
            let taken: bool = tx.query_row(
                "SELECT EXISTS (SELECT 1 FROM credentials WHERE credential_id = ?1) \
                 OR EXISTS (SELECT 1 FROM registrations WHERE credential_id = ?1)",
                [reg.credential_id()],
                |r| r.get(0),
            )?;
            if reg.algorithm() != ES256
                || reg.credential_id().is_empty()
                || reg.public_material().is_empty()
                || taken
            {
                return burn(ap(ApprovalCode::EnrollmentUnsupported));
            }
            let updated = tx.execute(
                "UPDATE registrations SET status = 'registered', credential_id = ?2, \
                 public_material = ?3, algorithm = ?4, ceremony_state = x'' \
                 WHERE registration_id = ?1 AND status = 'issued'",
                rusqlite::params![
                    f.registration_id,
                    reg.credential_id(),
                    reg.public_material(),
                    reg.algorithm(),
                ],
            )?;
            if updated != 1 {
                return Err(BodyError::StateConflict);
            }
            Ok(End::Commit(Ok(reg)))
        });
        self.audited(result)
    }

    /// The bootstrap enrollment of the first administrator
    /// (docs/implementation/M4-ENROLLMENT-CEREMONY.md section 2), reachable only through
    /// [`BootstrapSession::bootstrap`]: never from agent ingress, the serving ledger or the
    /// approval listener's normal service. Only in a ledger that has never had a subject or
    /// a bootstrap, with a verified `bootstrap` registration of the subject; otherwise
    /// `ENROLLMENT_UNSUPPORTED`, as for an ill-formed identity. The subject is human and
    /// receives exactly `operator` and `revoker` in the global scope. Returns its I-8
    /// sequence number.
    fn bootstrap_administrator(
        &self,
        bootstrap: &BootstrapEnrollment,
        now: ObservedTime,
    ) -> Result<Result<i64, Refusal>, TxFailure> {
        let at = now.at();
        let enrollment = NewEnrollment {
            subject: bootstrap.subject.clone(),
            kind: PrincipalKind::Human,
            credential: Some(bootstrap.credential.clone()),
            linked_identities: bootstrap.linked_identities.clone(),
            roles: [Permission::Operator, Permission::Revoker]
                .into_iter()
                .map(|permission| writ_broker::approval_store::NewRole {
                    permission,
                    tenant_id: GLOBAL_SCOPE.into(),
                    project_id: GLOBAL_SCOPE.into(),
                })
                .collect(),
        };
        let result = self.transact("bootstrap", |tx| {
            if bootstrapped(tx)?
                || enrollment_blocked(tx, &enrollment, RegistrationOperation::Bootstrap)?
                || registration_expired(tx, &enrollment, at)?
            {
                return Ok(End::Rollback(Err(ap(ApprovalCode::EnrollmentUnsupported))));
            }
            let by = SubjectId::new(BOOTSTRAP_ISSUER, BOOTSTRAP_SUBJECT);
            let target = enrollment.target_id();
            let seq = next_seq(tx, "bootstrap", &target)?;
            write_enrollment(tx, &enrollment, &by, at, None, "bootstrap")?;
            approval_event(
                tx,
                "bootstrap",
                &target,
                GLOBAL_SCOPE,
                GLOBAL_SCOPE,
                Some(&by),
                None,
                None,
                at,
                None,
                None,
                Some(seq),
            )?;
            Ok(End::Commit(Ok(seq)))
        });
        self.audited(result)
    }
}

/// Test builds only (never compiled into a library or release build): the bootstrap of a
/// ledger this test process already owns exclusively, for unit-test worlds that hold their
/// ledger. It goes through the same `bootstrap` registration row and the same bootstrap
/// transaction as [`BootstrapSession`]; production code has only [`BootstrapSession`].
#[cfg(test)]
impl SqliteLedger {
    pub(crate) fn bootstrap_owned_for_tests(
        &self,
        admin: &SubjectId,
        response: &[u8],
        now: ObservedTime,
    ) -> Result<Result<i64, Refusal>, TxFailure> {
        let begin = RegistrationBegin {
            subject: admin,
            operator: None,
            now,
        };
        let issued = match self.begin_registration_as(&begin, RegistrationOperation::Bootstrap)? {
            Ok(i) => i,
            Err(r) => return Ok(Err(r)),
        };
        let finish = RegistrationFinish {
            registration_id: &issued.registration_id,
            response,
            operator: None,
            now,
        };
        let reg = match self.finish_registration_as(&finish, RegistrationOperation::Bootstrap)? {
            Ok(r) => r,
            Err(r) => return Ok(Err(r)),
        };
        self.bootstrap_administrator(
            &BootstrapEnrollment {
                subject: admin.clone(),
                credential: reg,
                linked_identities: Vec::new(),
            },
            now,
        )
    }
}

/// The ledger has had a subject or a bootstrap (bootstrap is over for good).
fn bootstrapped(conn: &Connection) -> rusqlite::Result<bool> {
    conn.query_row(
        "SELECT EXISTS (SELECT 1 FROM subjects) \
         OR EXISTS (SELECT 1 FROM approval_events WHERE kind = 'bootstrap')",
        [],
        |r| r.get(0),
    )
}

/// The serving ledger registers credentials only for the administrative `Enroll`
/// ceremony (S5, X8), never for a bootstrap.
impl RegistrationLedger for SqliteLedger {
    fn begin_registration(
        &self,
        begin: &RegistrationBegin<'_>,
    ) -> Result<Result<IssuedRegistration, Refusal>, TxFailure> {
        self.begin_registration_as(begin, RegistrationOperation::Enroll)
    }

    fn finish_registration(
        &self,
        finish: &RegistrationFinish<'_>,
    ) -> Result<Result<VerifiedRegistration, Refusal>, TxFailure> {
        self.finish_registration_as(finish, RegistrationOperation::Enroll)
    }
}

/// The separate bootstrap entry point (S5, X8; docs/implementation/
/// M4-ENROLLMENT-CEREMONY.md section 2): an exclusively owned ledger in its bootstrap phase.
///
/// [`BootstrapSession::open`] takes the ledger through exactly the protocol of normal
/// service ([`SqliteLedger::open`]: canonical identity, the sidecar ownership lock
/// `<canonical>.owner` under the process lifecycle gate, read-only verification, owner
/// epoch and fencing on every transaction), so it fails closed (`OWNED_ELSEWHERE`) while a
/// broker or any other owner holds the ledger, and no normal service can open the ledger
/// while the session lives. There is no separate lock and no bypass. It refuses
/// (`ENROLLMENT_UNSUPPORTED`) a ledger that has ever had a subject or a bootstrap. The
/// session exposes only the `bootstrap` registration ceremony and the single
/// [`BootstrapSession::bootstrap`], which consumes the session and releases ownership.
pub struct BootstrapSession {
    ledger: SqliteLedger,
}

impl BootstrapSession {
    /// Open the ledger at `path` for its bootstrap (struct documentation).
    pub fn open(path: &std::path::Path) -> Result<Result<BootstrapSession, Refusal>, StoreError> {
        let ledger = SqliteLedger::open(path)?;
        let used = {
            let inner = ledger.lock("read")?;
            bootstrapped(&inner.conn).map_err(|e| unavailable("read", code_name(&e)))?
        };
        if used {
            let _ = ledger.audit(ApprovalCode::EnrollmentUnsupported.code());
            return Ok(Err(ap(ApprovalCode::EnrollmentUnsupported)));
        }
        Ok(Ok(BootstrapSession { ledger }))
    }

    /// Configure the approval channel of this session's `bootstrap` registration, once
    /// ([`SqliteLedger::with_approval_channel`]); without one the registration is refused.
    pub fn with_approval_channel(
        self,
        channel: ApprovalChannel,
    ) -> Result<BootstrapSession, StoreError> {
        Ok(BootstrapSession {
            ledger: self.ledger.with_approval_channel(channel)?,
        })
    }

    /// Trusted time for this session's operations (raises the ledger's persisted floor).
    pub fn observe(
        &self,
        clock: &dyn writ_broker::clock::EffectiveClock,
    ) -> Result<ObservedTime, writ_broker::clock::ClockError> {
        let mut store: &SqliteLedger = &self.ledger;
        writ_broker::clock::observe(clock, &mut store)
    }

    /// Enroll the first administrator with a credential from this session's `bootstrap`
    /// registration of the same subject, then release ownership. Returns its I-8 sequence
    /// number; `ENROLLMENT_UNSUPPORTED` when the ledger already has a subject or a
    /// bootstrap, or the registration is not a finished, unexpired `bootstrap` row of the
    /// subject.
    pub fn bootstrap(
        self,
        bootstrap: &BootstrapEnrollment,
        now: ObservedTime,
    ) -> Result<Result<i64, Refusal>, TxFailure> {
        self.ledger.bootstrap_administrator(bootstrap, now)
    }
}

impl RegistrationLedger for BootstrapSession {
    fn begin_registration(
        &self,
        begin: &RegistrationBegin<'_>,
    ) -> Result<Result<IssuedRegistration, Refusal>, TxFailure> {
        self.ledger
            .begin_registration_as(begin, RegistrationOperation::Bootstrap)
    }

    fn finish_registration(
        &self,
        finish: &RegistrationFinish<'_>,
    ) -> Result<Result<VerifiedRegistration, Refusal>, TxFailure> {
        self.ledger
            .finish_registration_as(finish, RegistrationOperation::Bootstrap)
    }
}

impl ApprovalLedger for SqliteLedger {
    fn approval_request(
        &self,
        request_id: &str,
    ) -> Result<Option<(ApprovalRequest, ApprovalStatus)>, StoreError> {
        let inner = self.lock("read")?;
        load_request(&inner.conn, request_id).map_err(|e| match e {
            BodyError::Sql(e) => unavailable("read", code_name(&e)),
            _ => unavailable("read", "integrity"),
        })
    }

    fn expire(&self, now: ObservedTime) -> Result<Expired, TxFailure> {
        let now = now.at();
        self.transact("expire", |tx| {
            let mut out = Expired::default();
            // X6 (S8): one I-8 row for an expiry step that changes anything, in its transaction.
            let mut seq: Option<i64> = None;
            let mut sequenced = |tx: &Transaction<'_>| -> rusqlite::Result<i64> {
                match seq {
                    Some(s) => Ok(s),
                    None => {
                        let s = next_seq(tx, "expiry", &now.to_string())?;
                        seq = Some(s);
                        Ok(s)
                    }
                }
            };
            let pending: Vec<(String, String, String, String)> = tx
                .prepare(
                    "SELECT request_id, tenant_id, project_id, expires_at FROM approval_requests \
                     WHERE status = 'pending' ORDER BY request_id",
                )?
                .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)))?
                .collect::<rusqlite::Result<Vec<_>>>()?;
            for (id, tenant, project, expires) in pending {
                if now < ts(&expires)? {
                    continue;
                }
                let s = sequenced(tx)?;
                let changed = tx.execute(
                    "UPDATE approval_requests SET status = 'expired' \
                     WHERE request_id = ?1 AND status = 'pending'",
                    [&id],
                )?;
                if changed != 1 {
                    return Err(BodyError::StateConflict);
                }
                approval_event(
                    tx,
                    "expired",
                    &id,
                    &tenant,
                    &project,
                    None,
                    None,
                    None,
                    now,
                    None,
                    None,
                    Some(s),
                )?;
                out.requests += 1;
            }
            let issued: Vec<(String, String)> = tx
                .prepare(
                    "SELECT challenge_id, expires_at FROM challenges WHERE status = 'issued' \
                     ORDER BY challenge_id",
                )?
                .query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?
                .collect::<rusqlite::Result<Vec<_>>>()?;
            for (id, expires) in issued {
                if now < ts(&expires)? {
                    continue;
                }
                sequenced(tx)?;
                tx.execute(
                    "UPDATE challenges SET status = 'expired' WHERE challenge_id = ?1 \
                     AND status = 'issued'",
                    [&id],
                )?;
                out.challenges += 1;
            }
            Ok(End::Commit(out))
        })
    }

    fn begin_ceremony(
        &self,
        begin: &CeremonyBegin<'_>,
    ) -> Result<Result<IssuedChallenge, Refusal>, TxFailure> {
        // M1: only the configured channel's verifier; none configured, no ceremony.
        let Some(channel) = self.approval_channel() else {
            return self.audited(Ok(Err(no_channel())));
        };
        let id = random_id("chl")?;
        let result = self.transact("challenge_issue", |tx| {
            Ok(end(begin_in(tx, begin, &id, channel)?, |_| false))
        });
        self.audited(result)
    }

    fn finish_ceremony(
        &self,
        finish: &CeremonyFinish<'_>,
    ) -> Result<Result<CeremonyOutcome, Refusal>, TxFailure> {
        // M1: without a configured channel nothing is verified and nothing is burnt (the
        // challenge stays issued, single use and expiring).
        let Some(channel) = self.approval_channel() else {
            return self.audited(Ok(Err(no_channel())));
        };
        let result = self.transact("ceremony", |tx| {
            // X4 (S6): the ceremony body runs inside a savepoint; a refusal undoes the body
            // and, when it identified an issued challenge, burns that challenge in this same
            // transaction (status `refused` with the code). The burn commits with the refusal;
            // if that commit fails or its outcome is unknown the answer is the storage failure
            // (fail closed), and the next ceremony reads the stored status again.
            tx.execute_batch("SAVEPOINT ceremony_body")?;
            match finish_in(tx, finish, channel)? {
                Ok(outcome) if outcome.replayed => Ok(End::Rollback(Ok(outcome))),
                Ok(outcome) => {
                    tx.execute_batch("RELEASE ceremony_body")?;
                    Ok(End::Commit(Ok(outcome)))
                }
                Err(refusal) => {
                    tx.execute_batch("ROLLBACK TO ceremony_body; RELEASE ceremony_body")?;
                    // A challenge expired on trusted time is already unusable (time never moves
                    // back past the persisted floor); the `expire` step marks it.
                    // L6: a verifier that reports itself unavailable is a transient outage,
                    // not a terminal refusal; the challenge stays issued (single use and
                    // expiring all the same).
                    if refusal == challenge_refused(ChallengeRefusal::Expired)
                        || refusal == no_channel()
                    {
                        return Ok(End::Rollback(Err(refusal)));
                    }
                    let burned = tx.execute(
                        "UPDATE challenges SET status = 'refused', refusal_code = ?2 \
                         WHERE challenge_id = ?1 AND status = 'issued'",
                        (finish.challenge_id, refusal.code()),
                    )?;
                    Ok(if burned == 1 {
                        End::Commit(Err(refusal))
                    } else {
                        End::Rollback(Err(refusal))
                    })
                }
            }
        });
        self.audited(result)
    }

    fn signoff(&self, signoff_id: &str) -> Result<Option<SignoffState>, StoreError> {
        let inner = self.lock("read")?;
        let conn = &inner.conn;
        let read = || -> Result<Option<SignoffState>, BodyError> {
            let Some(signoff) = read_signoff(conn, signoff_id)? else {
                return Ok(None);
            };
            let consumed: bool = conn.query_row(
                "SELECT EXISTS (SELECT 1 FROM consumed_signoffs WHERE signoff_id = ?1)",
                [signoff_id],
                |r| r.get(0),
            )?;
            Ok(Some(SignoffState {
                signoff,
                revoked: signoff_revoked(conn, signoff_id)?,
                consumed,
            }))
        };
        read().map_err(|e| match e {
            BodyError::Sql(e) => unavailable("read", code_name(&e)),
            _ => unavailable("read", "integrity"),
        })
    }
    fn ceremony_context(
        &self,
        op: &CeremonyOp,
        now: ObservedTime,
    ) -> Result<Result<CeremonyContext, Refusal>, StoreError> {
        let inner = self.lock("read")?;
        let read = || -> Outcome<CeremonyContext> {
            let rp = self.approval_channel().map(|c| c.relying_party().clone());
            // Exactly the begin's computation at this trusted time (a signoff's effective
            // expiry included, Y1).
            let t = check!(target(
                &inner.conn,
                op,
                SignoffExpiry::Compute(now.at()),
                rp.as_ref()
            )?);
            // R6: the displayed context is exactly the one whose fingerprint is bound.
            if context_fingerprint(&t.context).ok().as_ref() != Some(&t.binding.context_fingerprint)
            {
                return Err(BodyError::Integrity);
            }
            Ok(Ok(CeremonyContext {
                context: t.context,
                binding: t.binding,
            }))
        };
        read().map_err(|e| match e {
            BodyError::Sql(e) => unavailable("read", code_name(&e)),
            _ => unavailable("read", "integrity"),
        })
    }

    fn pending_signoff(
        &self,
        signoff_id: &str,
        _now: ObservedTime,
    ) -> Result<Option<NewSignoff>, StoreError> {
        let inner = self.lock("read")?;
        let read = || -> Result<Option<NewSignoff>, BodyError> {
            pending_signoff_in(&inner.conn, signoff_id)
        };
        read().map_err(|e| match e {
            BodyError::Sql(e) => unavailable("read", code_name(&e)),
            _ => unavailable("read", "integrity"),
        })
    }
}

/// [`ApprovalLedger::pending_signoff`]: the first stored promotion context of `signoff_id`
/// (the others must agree; [`target`] refuses a disagreement as `SIGNOFF_MISMATCH`), the
/// workflow's current task and artifact and the project's active acceptance policy.
fn pending_signoff_in(
    conn: &Connection,
    signoff_id: &str,
) -> Result<Option<NewSignoff>, BodyError> {
    let signed: bool = conn.query_row(
        "SELECT EXISTS (SELECT 1 FROM signoffs WHERE signoff_id = ?1)",
        [signoff_id],
        |r| r.get(0),
    )?;
    if signed {
        return Ok(None);
    }
    let row: Option<([String; 7], i64)> = conn
        .query_row(
            "SELECT tenant_id, project_id, workflow_id, acceptance_record_id, criterion_id, \
             from_state, to_state, state_revision FROM signoff_requests WHERE signoff_id = ?1 \
             ORDER BY request_fingerprint LIMIT 1",
            [signoff_id],
            |r| {
                Ok((
                    [
                        r.get(0)?,
                        r.get(1)?,
                        r.get(2)?,
                        r.get(3)?,
                        r.get(4)?,
                        r.get(5)?,
                        r.get(6)?,
                    ],
                    r.get(7)?,
                ))
            },
        )
        .optional()?;
    let Some(([tenant, project, workflow, record, criterion, from, to], revision)) = row else {
        return Ok(None);
    };
    let Some(w) = read_workflow(conn, &tenant, &project, &workflow)? else {
        return Ok(None);
    };
    let policy: Option<(String, String)> = conn
        .query_row(
            "SELECT requirements, bundle_sha FROM acceptance_policies \
             WHERE tenant_id = ?1 AND project_id = ?2",
            (&tenant, &project),
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .optional()?;
    let Some((requirements, sha)) = policy else {
        return Ok(None);
    };
    let req = json::parse(requirements.as_bytes()).map_err(|_| BodyError::Integrity)?;
    let member = |k: &str| req.get(k).and_then(Value::as_str).map(str::to_string);
    let (Some(policy_id), Some(policy_version)) = (
        member("acceptance_policy_id"),
        member("acceptance_policy_version"),
    ) else {
        return Ok(None);
    };
    Ok(Some(NewSignoff {
        signoff_id: signoff_id.to_string(),
        tenant_id: tenant,
        project_id: project,
        task_id: w.task_id,
        workflow_id: workflow,
        from_state: from,
        to_state: to,
        acceptance_record_id: record,
        criterion_id: criterion,
        artifact_snapshot_id: w.artifact_snapshot_id,
        artifact_digest: w.artifact_digest,
        acceptance_policy_id: policy_id,
        acceptance_policy_version: policy_version,
        acceptance_policy_bundle_sha: sha,
        state_revision: revision,
    }))
}
