//! The approval ledger interface (M4-03, M4-04, M4-05, M4-08 storage, M4-09,
//! M4-10; D-040 O-3..O-9, O-14; M4-INTERFACES I-2..I-8, C1, C2).
//!
//! Storage-independent: the trait, its argument and result types, and the
//! pure functions that derive what a challenge binds from stored state. The
//! SQLite implementation is `writ_ledger_sqlite` (`approval_store.rs`).
//!
//! **Ceremonies.** Every human operation (approve, deny, revoke, signoff,
//! enroll, role change) is two fenced transactions of the ledger:
//! [`ApprovalLedger::begin_ceremony`] reads the target and the actor's stored
//! records, selects the actor's [`crate::human::ActiveCredentials`], calls the
//! verifier's `begin` and stores the [`Challenge`] (unique id and digest, C1);
//! [`ApprovalLedger::finish_ceremony`] calls the verifier's `finish` with the
//! stored ceremony state and the active credentials read inside the same
//! transaction, accepts the answer only through
//! [`crate::human::VerifiedAssertion::check`] against the stored challenge
//! and the binding recomputed from current stored state, reads the actor's
//! subject, enrollment and role rows inside that transaction (C2), decides
//! with the broker's pure functions ([`crate::approval::decide`],
//! [`crate::approval::Revocation::record`], [`crate::approval::Signoff::sign`])
//! and writes the result, the consumed challenge, an approval event and one
//! I-8 `ledger_sequence` row in the same transaction. The operation and verdict always come from the stored
//! challenge (R5): a finish for another operation or target is
//! `CHALLENGE_INVALID`.
//!
//! **Unknown commit.** An identical retry of a finish (same challenge, same
//! idempotency key, an assertion of the same subject, credential and
//! ceremony, the same operation and verdict, and the same target id and
//! complete context fingerprint recomputed from stored state) after a commit
//! whose outcome is unknown replays the committed outcome
//! ([`CeremonyOutcome::replayed`]) and never mints another grant, signoff,
//! revocation or role change; any difference on a consumed challenge is
//! `CHALLENGE_INVALID` (binding mismatch, D-042 X3), never the earlier
//! result.
//!
//! **Refusal burns the challenge (D-042 X4).** A terminal refusal of an
//! identified ceremony (the challenge row exists and is `issued`) commits the
//! challenge as `refused` with the refusal code in the same transaction; a
//! later finish on it is `CHALLENGE_INVALID` (consumed). When that commit
//! fails or its outcome is unknown, the answer is the storage failure (fail
//! closed), and the next transaction reads the stored status again. A
//! verifier that reports itself unavailable (`ASSERTION_INVALID`, verifier
//! unavailable) and a ledger without a configured approval channel are not
//! terminal refusals: the challenge stays `issued` (still single use and
//! expiring) and the answer is the unavailability.
//!
//! **Role changes (D-042 X5).** [`CeremonyOp::RoleChange`] grants or revokes
//! one assignment; the binding scope is the assignment's scope and the actor
//! needs `operator` over a scope containing it.
//!
//! **Read sessions (D-044 item 3, Z1..Z4).** A login is one more ceremony,
//! [`CeremonyOp::ReadSession`] (purpose `read`, its own operation in every
//! binding, so no login challenge is accepted for a mutation and no mutating
//! challenge for a login); it produces a [`ReadSession`] of at most
//! [`READ_SESSION_MAX_SECONDS`]. Every protected read of stored record context
//! for the approval page is [`ApprovalLedger::read_record`], which rechecks the
//! session, the subject, the credential and the scoped permission inside its
//! read transaction and answers one [`ReadRefused`] for every failure.
//!
//! **Approval requests.** [`approval_request_for`] builds the immutable
//! request from the guard-bound use-time decision when it is
//! `require_approval` and no grant is presented ([`crate::execute::Broker::execute`]);
//! the agent's answer stays `APPROVAL_REQUIRED`. One request per kernel
//! approval request id: an existing one is kept unchanged, whatever its
//! status (a denied or expired request is never reopened or replaced).

use writ_kernel::json::{self, CanonicalError, Digest, Value};
use writ_kernel::time::Timestamp;

use crate::approval::{
    context_fingerprint, ApprovalRequest, ApprovalStatus, ChallengeBinding, ChallengeOperation,
    NewApprovalRequest, NewSignoff, RevocationTarget, Signoff, SignoffExclusions,
};
use crate::authorize::{Authorized, UseTimeStatus};
use crate::clock::ObservedTime;
use crate::grants::GrantLabel;
use crate::human::{
    LedgerCapability, Permission, PrincipalKind, RelyingParty, SubjectId, VerifiedRegistration,
};
use crate::refusal::Refusal;
use crate::store::{ExecuteLedger, StoreError, TxFailure};

/// Lifetime of a stored approval request, bounded by the request deadline.
pub const APPROVAL_REQUEST_TTL_SECONDS: i64 = 600;
/// Lifetime of the grant an approval produces.
pub const APPROVAL_GRANT_TTL_SECONDS: u32 = 600;
/// The administrative scope of identity operations (credential and subject
/// revocation, enrollment): tenant and project `*`.
pub const GLOBAL_SCOPE: &str = "*";
/// Policy bundle digest bound by operations that are not decided under a
/// policy bundle (revocation, enrollment).
pub const NO_POLICY: &str = "none";
/// Context member naming a revocation context (domain separation).
pub const REVOCATION_CONTEXT: &str = "writ.revocation-context.v1";
/// Context member naming an enrollment context (domain separation).
pub const ENROLLMENT_CONTEXT: &str = "writ.enrollment-context.v1";
/// Context member naming a role-change context (domain separation, X5).
pub const ROLE_CHANGE_CONTEXT: &str = "writ.role-change-context.v1";
/// Context member naming a registration-start context (domain separation,
/// D-043 Y2).
pub const REGISTRATION_START_CONTEXT: &str = "writ.registration-start-context.v1";
/// Context member naming a read-session context (domain separation, D-044
/// item 3, Z1).
pub const READ_SESSION_CONTEXT: &str = "writ.read-session-context.v1";
/// The lifetime of an authenticated read session (D-040 O-3: at most 15
/// minutes; D-044 Z1, Z2): absolute from the login ceremony's commit, never
/// extended.
pub const READ_SESSION_MAX_SECONDS: i64 = 900;
/// The enrolling administrator recorded for the bootstrap enrollment.
pub const BOOTSTRAP_ISSUER: &str = "writ-local";
/// See [`BOOTSTRAP_ISSUER`].
pub const BOOTSTRAP_SUBJECT: &str = "bootstrap";

/// The stored text of a permission.
pub fn permission_text(p: Permission) -> &'static str {
    match p {
        Permission::Approver => "approver",
        Permission::SignoffAuthority => "signoff_authority",
        Permission::Operator => "operator",
        Permission::Revoker => "revoker",
    }
}

/// Parse a stored permission.
pub fn parse_permission(text: &str) -> Option<Permission> {
    use Permission::*;
    [Approver, SignoffAuthority, Operator, Revoker]
        .into_iter()
        .find(|p| permission_text(*p) == text)
}

/// The stored text of a principal kind.
pub fn kind_text(k: PrincipalKind) -> &'static str {
    match k {
        PrincipalKind::Human => "human",
        PrincipalKind::Service => "service",
    }
}

/// Parse a stored principal kind.
pub fn parse_kind(text: &str) -> Option<PrincipalKind> {
    match text {
        "human" => Some(PrincipalKind::Human),
        "service" => Some(PrincipalKind::Service),
        _ => None,
    }
}

fn subject_value(s: &SubjectId) -> Value {
    json::obj([
        ("issuer", json::s(&s.issuer)),
        ("subject", json::s(&s.subject)),
    ])
}

/// The immutable approval request of a guard-bound use-time decision that
/// is `require_approval` (M4-03): request id the kernel's approval request
/// id; requester the sealed principal under its verifying adapter; agent the
/// request's agent id under the same adapter; no delegation chain (the
/// sealed request carries no trusted chain); the complete action body;
/// created at the decision's observed time and expiring
/// [`APPROVAL_REQUEST_TTL_SECONDS`] later or at the request deadline.
/// `None` for any other decision or when a field is missing or malformed
/// (nothing is stored; the answer is unchanged).
pub fn approval_request_for(
    authorized: &Authorized<'_>,
    observed: ObservedTime,
) -> Option<ApprovalRequest> {
    if authorized.status() != UseTimeStatus::RequireApproval {
        return None;
    }
    let request = authorized.request();
    let created_at = observed.at();
    let ttl = created_at.plus_seconds(APPROVAL_REQUEST_TTL_SECONDS);
    let expires_at = request.deadline_at.map_or(ttl, |d| d.min(ttl));
    let realm = &request.principal.verified_by;
    let action = authorized.verified().request().get("action")?.clone();
    ApprovalRequest::new(NewApprovalRequest {
        request_id: authorized.decision().approval_request_reference.clone()?,
        tenant_id: request.tenant_id.clone(),
        project_id: request.project_id.clone(),
        requester: SubjectId::new(realm, &request.principal.id),
        agent: request
            .agent_id
            .as_deref()
            .map(|a| SubjectId::new(realm, a)),
        action_kind: request.action.kind.clone(),
        canonical_resource_id: request.action.resource.canonical_resource_id.clone()?,
        action_digest: request.action.action_digest.clone(),
        action,
        capabilities: request.capability_set(),
        policy_bundle_sha: authorized.policy_bundle_sha().to_string(),
        request_fingerprint: authorized.verified().fingerprint().as_str().to_string(),
        created_at,
        expires_at,
        required_approvers: 1,
    })
    .ok()
}

/// What a challenge for approving or denying `request` binds: the request's
/// scope, id, displayed fingerprint and policy bundle digest; revision 0
/// (an approval request has no revision).
pub fn approval_binding(
    request: &ApprovalRequest,
    operation: ChallengeOperation,
) -> ChallengeBinding {
    let r = request.fields();
    ChallengeBinding {
        operation,
        tenant_id: r.tenant_id.clone(),
        project_id: r.project_id.clone(),
        target_id: r.request_id.clone(),
        context_fingerprint: request.fingerprint().clone(),
        policy_bundle_sha: r.policy_bundle_sha.clone(),
        revision: 0,
    }
}

/// The displayed context of a revocation: the target, its scope, the
/// operator's reason and the target's immutable stored record (`record`,
/// built by the ledger from the stored row).
pub fn revocation_context(
    target: &RevocationTarget,
    reason: &str,
    tenant_id: &str,
    project_id: &str,
    record: Value,
) -> Value {
    json::obj([
        ("context", json::s(REVOCATION_CONTEXT)),
        ("project_id", json::s(project_id)),
        ("reason", json::s(reason)),
        ("record", record),
        ("target", json::s(&target.target_id())),
        ("tenant_id", json::s(tenant_id)),
    ])
}

/// What a revocation challenge binds ([`revocation_context`]); policy
/// [`NO_POLICY`], revision 0.
pub fn revocation_binding(
    target: &RevocationTarget,
    reason: &str,
    tenant_id: &str,
    project_id: &str,
    record: Value,
) -> Result<ChallengeBinding, CanonicalError> {
    let context = revocation_context(target, reason, tenant_id, project_id, record);
    Ok(ChallengeBinding {
        operation: ChallengeOperation::Revoke,
        tenant_id: tenant_id.to_string(),
        project_id: project_id.to_string(),
        target_id: target.target_id(),
        context_fingerprint: context_fingerprint(&context)?,
        policy_bundle_sha: NO_POLICY.to_string(),
        revision: 0,
    })
}

/// What a signoff challenge binds: the signoff's scope, its id, the
/// fingerprint of its complete displayed context (the binding, the
/// exclusions the ledger read, X7, and the effective expiry the ledger fixed
/// at begin, Y1), its acceptance policy bundle digest and the state revision.
pub fn signoff_binding(
    binding: &NewSignoff,
    excluded: &SignoffExclusions,
    expires_at: Timestamp,
) -> Result<ChallengeBinding, CanonicalError> {
    Ok(ChallengeBinding {
        operation: ChallengeOperation::Signoff,
        tenant_id: binding.tenant_id.clone(),
        project_id: binding.project_id.clone(),
        target_id: binding.signoff_id.clone(),
        context_fingerprint: binding
            .displayed(excluded, expires_at)?
            .fingerprint()
            .clone(),
        policy_bundle_sha: binding.acceptance_policy_bundle_sha.clone(),
        revision: binding.state_revision,
    })
}

/// One role-assignment change (D-042 X5): grant or revoke exactly one
/// assignment. Needs a fresh user-verified ceremony by a human holding
/// [`Permission::Operator`] over a scope containing the assignment's scope
/// ([`crate::human::scope_contains`]); a subject never changes its own
/// assignments; a service never receives anything but `operator` (O-8).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RoleChange {
    /// Grant `permission` over (tenant, project) to an existing subject.
    Grant {
        /// The subject receiving the permission.
        subject: SubjectId,
        /// The permission.
        permission: Permission,
        /// Tenant scope (`*` only when explicitly intended).
        tenant_id: String,
        /// Project scope (`*` only when explicitly intended).
        project_id: String,
    },
    /// Revoke one assignment by id.
    Revoke {
        /// The assignment id.
        assignment_id: String,
    },
}

impl RoleChange {
    /// The challenge target: `role-grant:` plus the subject's revocation
    /// target id, permission and length-prefixed scope; or `role-revoke:`
    /// plus the assignment id.
    pub fn target_id(&self) -> String {
        match self {
            RoleChange::Grant {
                subject,
                permission,
                tenant_id,
                project_id,
            } => format!(
                "role-grant:{}:{}:{}:{}:{}",
                RevocationTarget::Subject(subject.clone()).target_id(),
                permission_text(*permission),
                tenant_id.len(),
                tenant_id,
                project_id
            ),
            RoleChange::Revoke { assignment_id } => format!("role-revoke:{assignment_id}"),
        }
    }
}

/// The displayed context of a role change: the change, the assignment's
/// scope and the target's immutable stored record (`record`, built by the
/// ledger: the subject row for a grant, the assignment row for a revoke).
pub fn role_change_context(
    change: &RoleChange,
    tenant_id: &str,
    project_id: &str,
    record: Value,
) -> Value {
    let (kind, permission) = match change {
        RoleChange::Grant { permission, .. } => ("grant", json::s(permission_text(*permission))),
        RoleChange::Revoke { .. } => ("revoke", Value::Null),
    };
    json::obj([
        ("change", json::s(kind)),
        ("context", json::s(ROLE_CHANGE_CONTEXT)),
        ("permission", permission),
        ("project_id", json::s(project_id)),
        ("record", record),
        ("target", json::s(&change.target_id())),
        ("tenant_id", json::s(tenant_id)),
    ])
}

/// What a role-change challenge binds ([`role_change_context`]) in the
/// assignment's scope; policy [`NO_POLICY`], revision 0.
pub fn role_change_binding(
    change: &RoleChange,
    tenant_id: &str,
    project_id: &str,
    record: Value,
) -> Result<ChallengeBinding, CanonicalError> {
    let context = role_change_context(change, tenant_id, project_id, record);
    Ok(ChallengeBinding {
        operation: ChallengeOperation::RoleChange,
        tenant_id: tenant_id.to_string(),
        project_id: project_id.to_string(),
        target_id: change.target_id(),
        context_fingerprint: context_fingerprint(&context)?,
        policy_bundle_sha: NO_POLICY.to_string(),
        revision: 0,
    })
}

/// The challenge target of a registration start (D-043 Y2): the intended
/// subject's revocation target id plus the length-prefixed RP id and the
/// origin of the configured channel.
pub fn registration_start_target(subject: &SubjectId, rp: &RelyingParty) -> String {
    format!(
        "registration-start:{}:{}:{}:{}",
        RevocationTarget::Subject(subject.clone()).target_id(),
        rp.rp_id.len(),
        rp.rp_id,
        rp.origin
    )
}

/// The displayed context of a registration start (D-043 Y2): the operator,
/// the intended subject, the operation (`enroll`), the RP id and origin of
/// the configured channel and the row's lifetime
/// ([`REGISTRATION_TTL_SECONDS`], counted from the start ceremony's commit).
/// Roles and linked identities are not part of it: they are displayed and
/// bound by the later `Enroll` ceremony.
pub fn registration_start_context(
    subject: &SubjectId,
    operator: &SubjectId,
    rp: &RelyingParty,
) -> Value {
    json::obj([
        ("context", json::s(REGISTRATION_START_CONTEXT)),
        ("operation", json::s("enroll")),
        ("operator", subject_value(operator)),
        ("origin", json::s(&rp.origin)),
        (
            "registration_lifetime_seconds",
            Value::Int(REGISTRATION_TTL_SECONDS),
        ),
        ("rp_id", json::s(&rp.rp_id)),
        ("subject", subject_value(subject)),
    ])
}

/// What a registration-start challenge binds ([`registration_start_context`])
/// in the global scope; policy [`NO_POLICY`], revision 0.
pub fn registration_start_binding(
    subject: &SubjectId,
    operator: &SubjectId,
    rp: &RelyingParty,
) -> Result<ChallengeBinding, CanonicalError> {
    let context = registration_start_context(subject, operator, rp);
    Ok(ChallengeBinding {
        operation: ChallengeOperation::RegistrationStart,
        tenant_id: GLOBAL_SCOPE.to_string(),
        project_id: GLOBAL_SCOPE.to_string(),
        target_id: registration_start_target(subject, rp),
        context_fingerprint: context_fingerprint(&context)?,
        policy_bundle_sha: NO_POLICY.to_string(),
        revision: 0,
    })
}

/// The challenge target of a read-session login (D-044 Z1): the subject's
/// revocation target id, length-prefixed, plus the page-session id the
/// session will be bound to.
pub fn read_session_target(subject: &SubjectId, page_session: &str) -> String {
    let subject = RevocationTarget::Subject(subject.clone()).target_id();
    format!("read-session:{}:{subject}:{page_session}", subject.len())
}

/// The displayed and fingerprinted context of a read-session login (D-044
/// Z1): the subject, the page session it binds, the RP id and origin of the
/// configured channel, the purpose `read` and the session lifetime
/// ([`READ_SESSION_MAX_SECONDS`]).
pub fn read_session_context(subject: &SubjectId, page_session: &str, rp: &RelyingParty) -> Value {
    json::obj([
        ("context", json::s(READ_SESSION_CONTEXT)),
        ("origin", json::s(&rp.origin)),
        ("page_session", json::s(page_session)),
        ("purpose", json::s("read")),
        ("rp_id", json::s(&rp.rp_id)),
        (
            "session_lifetime_seconds",
            Value::Int(READ_SESSION_MAX_SECONDS),
        ),
        ("subject", subject_value(subject)),
    ])
}

/// What a read-session challenge binds ([`read_session_context`]) in the
/// global scope; policy [`NO_POLICY`], revision 0. Its operation
/// ([`ChallengeOperation::ReadSession`]) separates it from every mutating
/// ceremony (purpose separation).
pub fn read_session_binding(
    subject: &SubjectId,
    page_session: &str,
    rp: &RelyingParty,
) -> Result<ChallengeBinding, CanonicalError> {
    let context = read_session_context(subject, page_session, rp);
    Ok(ChallengeBinding {
        operation: ChallengeOperation::ReadSession,
        tenant_id: GLOBAL_SCOPE.to_string(),
        project_id: GLOBAL_SCOPE.to_string(),
        target_id: read_session_target(subject, page_session),
        context_fingerprint: context_fingerprint(&context)?,
        policy_bundle_sha: NO_POLICY.to_string(),
        revision: 0,
    })
}

/// An authenticated read session (D-044 item 3, Z1, Z2): the result of a
/// consumed [`CeremonyOp::ReadSession`] ceremony. It is a reference, not
/// authority: every protected read ([`ApprovalLedger::read_record`]) looks up
/// the consumed login challenge and its event by `session_id` inside the read
/// transaction, requires every field to equal what the ledger recorded, and
/// rechecks the subject, the credential and the scoped permission of the
/// target. It never authorizes a state change. Built only by the ledger
/// implementation ([`LedgerCapability`]).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ReadSession {
    session_id: String,
    subject: SubjectId,
    credential_id: String,
    page_session: String,
    issued_at: Timestamp,
    expires_at: Timestamp,
}

impl ReadSession {
    /// The read session of the consumed login challenge `session_id`, issued
    /// at `issued_at`; it expires [`READ_SESSION_MAX_SECONDS`] later.
    pub fn from_ledger(
        _capability: &LedgerCapability,
        session_id: &str,
        subject: SubjectId,
        credential_id: &str,
        page_session: &str,
        issued_at: Timestamp,
    ) -> ReadSession {
        ReadSession {
            session_id: session_id.to_string(),
            subject,
            credential_id: credential_id.to_string(),
            page_session: page_session.to_string(),
            issued_at,
            expires_at: issued_at.plus_seconds(READ_SESSION_MAX_SECONDS),
        }
    }

    /// The id (the consumed login challenge's id).
    pub fn session_id(&self) -> &str {
        &self.session_id
    }

    /// The authenticated subject.
    pub fn subject(&self) -> &SubjectId {
        &self.subject
    }

    /// The credential that authenticated it.
    pub fn credential_id(&self) -> &str {
        &self.credential_id
    }

    /// The approval page session it is bound to.
    pub fn page_session(&self) -> &str {
        &self.page_session
    }

    /// When the login committed (trusted time).
    pub fn issued_at(&self) -> Timestamp {
        self.issued_at
    }

    /// Absolute expiry (trusted time).
    pub fn expires_at(&self) -> Timestamp {
        self.expires_at
    }
}

/// What a protected read asks for (D-044 Z3).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ReadTarget {
    /// A stored approval request (needs `approver` over a scope containing
    /// the request's).
    ApprovalRequest {
        /// The request id.
        request_id: String,
    },
    /// The pending signoff of a stored promotion context, with the context a
    /// signoff ceremony binds and its effective expiry pinned (D-043 Y1; needs
    /// `signoff_authority` over a scope containing the signoff's).
    Signoff {
        /// The signoff id.
        signoff_id: String,
    },
    /// The context a ceremony for this operation would bind (R6): the
    /// permission the operation needs over a scope containing its binding's
    /// (`approver` for approve and deny, `signoff_authority` for a signoff,
    /// `revoker` for a revocation, `operator` for enrollment, registration
    /// start and role changes). A read-session operation is never readable.
    Ceremony(Box<CeremonyOp>),
    /// An operator form page (D-044 review LOW-3): no record, only whether the
    /// session's subject currently holds `permission` over some scope (`revoker`
    /// for the revoke form, `operator` for the role and enrollment forms).
    Form {
        /// The permission the form's operations need.
        permission: Permission,
    },
}

/// The result of a protected read.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RecordView {
    /// A stored approval request and its status.
    ApprovalRequest {
        /// The request.
        request: Box<ApprovalRequest>,
        /// Its status.
        status: ApprovalStatus,
    },
    /// The operation (for a signoff: with the effective expiry pinned as its
    /// requested expiry) and the context and binding a begin would compute.
    Ceremony {
        /// The operation the page displays and later begins.
        op: Box<CeremonyOp>,
        /// Its context and binding.
        context: CeremonyContext,
    },
    /// The form is available to the session ([`ReadTarget::Form`]).
    Permitted,
}

/// The one refusal of every protected read (D-044 Z3): an unknown, expired
/// or mismatched read session, a revoked subject or credential, a missing or
/// out-of-scope permission and an unknown target are indistinguishable.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ReadRefused;

/// One protected read.
pub struct RecordRead<'a> {
    /// The authenticated read session.
    pub session: &'a ReadSession,
    /// What to read.
    pub target: &'a ReadTarget,
    /// Trusted time (persisted floor).
    pub now: ObservedTime,
}

/// One permission to assign at enrollment.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NewRole {
    /// The permission.
    pub permission: Permission,
    /// Tenant scope.
    pub tenant_id: String,
    /// Project scope.
    pub project_id: String,
}

/// An enrollment (docs/implementation/M4-ENROLLMENT-CEREMONY.md section 3).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NewEnrollment {
    /// The new subject.
    pub subject: SubjectId,
    /// Its principal kind.
    pub kind: PrincipalKind,
    /// Its credential (required for a human, absent for a service): only a
    /// [`VerifiedRegistration`] that the ledger's own `enroll` registration
    /// row recorded for this subject (S5, X8); raw public material is never
    /// enrolled.
    pub credential: Option<VerifiedRegistration>,
    /// The requester identities it controls (R1; explicit, possibly empty).
    pub linked_identities: Vec<SubjectId>,
    /// Its role assignments.
    pub roles: Vec<NewRole>,
}

impl NewEnrollment {
    /// The challenge target: `enroll:` plus the subject's revocation target id.
    pub fn target_id(&self) -> String {
        format!(
            "enroll:{}",
            RevocationTarget::Subject(self.subject.clone()).target_id()
        )
    }

    /// The displayed context: every field, the credential's public material
    /// as its digest. Linked identities and roles in stored order.
    pub fn context(&self) -> Value {
        let mut linked = self.linked_identities.clone();
        linked.sort();
        linked.dedup();
        let mut roles: Vec<(&str, &str, &str)> = self
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
        let credential = self.credential.as_ref().map_or(Value::Null, |c| {
            json::obj([
                ("algorithm", json::s(c.algorithm())),
                ("credential_id", json::s(c.credential_id())),
                (
                    "public_material_digest",
                    json::s(Digest::of_bytes(c.public_material()).as_str()),
                ),
                ("registration_id", json::s(c.registration_id())),
            ])
        });
        json::obj([
            ("context", json::s(ENROLLMENT_CONTEXT)),
            ("credential", credential),
            ("kind", json::s(kind_text(self.kind))),
            (
                "linked_identities",
                Value::Arr(linked.iter().map(subject_value).collect()),
            ),
            (
                "roles",
                Value::Arr(
                    roles
                        .iter()
                        .map(|(p, t, q)| {
                            json::obj([
                                ("permission", json::s(p)),
                                ("project_id", json::s(q)),
                                ("tenant_id", json::s(t)),
                            ])
                        })
                        .collect(),
                ),
            ),
            ("subject", subject_value(&self.subject)),
        ])
    }

    /// What an enrollment challenge binds: the global scope, the target, the
    /// fingerprint of [`NewEnrollment::context`], policy [`NO_POLICY`],
    /// revision 0.
    pub fn binding(&self) -> Result<ChallengeBinding, CanonicalError> {
        Ok(ChallengeBinding {
            operation: ChallengeOperation::Enroll,
            tenant_id: GLOBAL_SCOPE.to_string(),
            project_id: GLOBAL_SCOPE.to_string(),
            target_id: self.target_id(),
            context_fingerprint: context_fingerprint(&self.context())?,
            policy_bundle_sha: NO_POLICY.to_string(),
            revision: 0,
        })
    }
}

/// The bootstrap enrollment of the first administrator
/// (M4-ENROLLMENT-CEREMONY section 2), accepted only on an exclusively owned
/// ledger without subjects (`writ_ledger_sqlite::BootstrapSession`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BootstrapEnrollment {
    /// The human subject.
    pub subject: SubjectId,
    /// Its credential: a [`VerifiedRegistration`] recorded by the ledger's
    /// own `bootstrap` registration row for this subject (S5, X8).
    pub credential: VerifiedRegistration,
    /// The requester identities it controls (R1).
    pub linked_identities: Vec<SubjectId>,
}

/// Caller-supplied signoff exclusions (S3, X7). They can only add: the
/// ledger reads the requesters and agents of the stored promotion contexts,
/// the enrolled subjects linked to them and the artifact and policy changers
/// itself inside the transaction ([`SignoffExclusions`]); these subjects are
/// added to that set (displayed and fingerprinted as `added`), never
/// replacing or removing anything. The I-5 use-time check repeats separation
/// against the promotion request.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct SignoffParties {
    /// Additional excluded subjects.
    pub added: Vec<SubjectId>,
}

/// The human operation of one ceremony.
// One value per ceremony, passed by reference: the enrollment variant's size (it carries
// its verified registration) costs nothing worth a box at every caller.
#[allow(clippy::large_enum_variant)]
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum CeremonyOp {
    /// Approve a stored approval request.
    Approve {
        /// The request id.
        request_id: String,
    },
    /// Deny a stored approval request (terminal).
    Deny {
        /// The request id.
        request_id: String,
    },
    /// Revoke a grant, signoff, credential, subject or role.
    Revoke {
        /// The target.
        target: RevocationTarget,
        /// Operator reason (displayed and bound).
        reason: String,
    },
    /// Sign off one human criterion.
    Signoff {
        /// The binding to sign.
        binding: Box<NewSignoff>,
        /// Additional excluded subjects (they only add, X7).
        parties: SignoffParties,
        /// An earlier expiry the caller asks for (D-043 item 2, Y1); it only
        /// shortens. The ledger computes the effective expiry at begin on
        /// trusted time ([`crate::approval::signoff_expiry`]: at most 600 s,
        /// bounded by the stored promotion contexts' trusted deadline), stores
        /// it with the challenge, displays and fingerprints it; finish takes
        /// exactly that stored value and never reads this field.
        requested_expires_at: Option<Timestamp>,
    },
    /// Enroll a subject (administrative ceremony).
    Enroll {
        /// The enrollment.
        enrollment: NewEnrollment,
        /// The operator page session that began the credential's registration row
        /// ([`OperatorSession::session_id`], D-043 Y2): the enrollment of a human is
        /// refused unless it equals the row's and the acting operator is the row's
        /// operator. Held server-side by the approval listener, never a request field;
        /// not part of the displayed context.
        operator_session: String,
    },
    /// Grant or revoke one role assignment (administrative ceremony, X5).
    RoleChange {
        /// The change.
        change: RoleChange,
    },
    /// Start a normal registration row for a new human (D-043 Y2): the
    /// operator's fresh user-verified ceremony, consumed atomically with
    /// creating the row bound to the operator, `operator_session`, the
    /// subject and the configured channel's RP id and origin. The only way
    /// the serving ledger creates an `enroll` registration row.
    RegistrationStart {
        /// The intended new subject.
        subject: SubjectId,
        /// The operator performing the ceremony (must be the acting subject).
        operator: SubjectId,
        /// The approval page session the row is bound to (server-held; not
        /// displayed or fingerprinted).
        operator_session: String,
    },
    /// Open an authenticated read session (D-044 item 3, Z1): a fresh
    /// user-verified assertion by `subject` over a challenge bound to the
    /// subject, its active credentials, the page session, the configured
    /// channel's RP id and origin and the purpose `read`. It authorizes reads
    /// only ([`ApprovalLedger::read_record`]), never a state change.
    ReadSession {
        /// The subject logging in (must be the acting subject).
        subject: SubjectId,
        /// The approval page session the read session is bound to.
        page_session: String,
    },
}

impl CeremonyOp {
    /// The challenge operation.
    pub fn operation(&self) -> ChallengeOperation {
        match self {
            CeremonyOp::Approve { .. } => ChallengeOperation::Approve,
            CeremonyOp::Deny { .. } => ChallengeOperation::Deny,
            CeremonyOp::Revoke { .. } => ChallengeOperation::Revoke,
            CeremonyOp::Signoff { .. } => ChallengeOperation::Signoff,
            CeremonyOp::Enroll { .. } => ChallengeOperation::Enroll,
            CeremonyOp::RoleChange { .. } => ChallengeOperation::RoleChange,
            CeremonyOp::RegistrationStart { .. } => ChallengeOperation::RegistrationStart,
            CeremonyOp::ReadSession { .. } => ChallengeOperation::ReadSession,
        }
    }
}

/// The start of a ceremony.
pub struct CeremonyBegin<'a> {
    /// The operation (its target and displayed context).
    pub op: &'a CeremonyOp,
    /// The acting subject (from the approval listener's session, never trusted
    /// for anything but selecting credentials).
    pub subject: &'a SubjectId,
    /// Idempotency key of the decision this challenge is for.
    pub idempotency_key: &'a str,
    /// Trusted time (persisted floor). (No verifier: the ledger uses its
    /// configured [`crate::human::ApprovalChannel`].)
    pub now: ObservedTime,
}

/// A stored challenge as returned to the approval page.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct IssuedChallenge {
    /// The challenge id.
    pub challenge_id: String,
    /// The verifier's client options for the page.
    pub client_options: Vec<u8>,
    /// Expiry.
    pub expires_at: Timestamp,
}

/// The end of a ceremony.
pub struct CeremonyFinish<'a> {
    /// The operation; must be the challenge's (otherwise `CHALLENGE_INVALID`).
    pub op: &'a CeremonyOp,
    /// The challenge id.
    pub challenge_id: &'a str,
    /// The client's assertion response.
    pub response: &'a [u8],
    /// Idempotency key (must equal the challenge's).
    pub idempotency_key: &'a str,
    /// Trusted time (persisted floor). (No verifier and no channel identity:
    /// both come from the ledger's configured
    /// [`crate::human::ApprovalChannel`].)
    pub now: ObservedTime,
}

/// Where a revoked target's use stood when the revocation committed (O-5).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum UseState {
    /// Never used: no use is possible any more.
    Unused,
    /// Reserved, not dispatched: T-dispatch refuses, the receipt stays
    /// `reserved` with its grant use (no refund).
    Reserved,
    /// Dispatched or observed: nothing is undone; recorded as after dispatch.
    Dispatched,
    /// Consumed by a promotion (grant or signoff): nothing is undone.
    Consumed,
    /// The target has no use (credential, subject, role).
    NotApplicable,
}

impl UseState {
    /// The stored text.
    pub fn as_str(self) -> &'static str {
        match self {
            UseState::Unused => "unused",
            UseState::Reserved => "reserved",
            UseState::Dispatched => "dispatched",
            UseState::Consumed => "consumed",
            UseState::NotApplicable => "not_applicable",
        }
    }

    /// Parse the stored text.
    pub fn parse(text: &str) -> Option<UseState> {
        use UseState::*;
        [Unused, Reserved, Dispatched, Consumed, NotApplicable]
            .into_iter()
            .find(|u| u.as_str() == text)
    }
}

/// What a ceremony committed.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum CeremonyResult {
    /// The request is `approved`; its single grant.
    Approved {
        /// The grant label ([`crate::grants::approval_grant_label`]).
        grant: GrantLabel,
    },
    /// The request is `denied` (terminal).
    Denied,
    /// The target is revoked.
    Revoked {
        /// Where its use stood.
        use_state: UseState,
    },
    /// The signoff is stored.
    SignedOff {
        /// Its id.
        signoff_id: String,
    },
    /// The subject is enrolled.
    Enrolled {
        /// The subject.
        subject: SubjectId,
    },
    /// The role assignment was granted (new id) or revoked.
    RoleChanged {
        /// The assignment id.
        assignment_id: String,
    },
    /// The bound registration row was created (D-043 Y2).
    RegistrationStarted {
        /// The row, with the verifier's creation options for the page.
        registration: IssuedRegistration,
    },
    /// An authenticated read session was opened (D-044 Z1).
    ReadSessionOpened {
        /// The session.
        session: ReadSession,
    },
}

/// A committed ceremony.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CeremonyOutcome {
    /// The result.
    pub result: CeremonyResult,
    /// Its I-8 ledger sequence number.
    pub ledger_seq: i64,
    /// `true` when this call replayed an already committed outcome (an
    /// identical retry after an unknown commit) and wrote nothing.
    pub replayed: bool,
}

/// Result of recording an approval request.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RequestRecorded {
    /// A new `pending` request was stored.
    Created,
    /// A request with this id exists and was kept unchanged.
    Exists(ApprovalStatus),
}

/// What one expiry step changed.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Expired {
    /// Pending approval requests now `expired`.
    pub requests: u64,
    /// Issued challenges now `expired`.
    pub challenges: u64,
}

/// A stored signoff with its ledger status (a reference only, R8: the I-5
/// use-time checks decide).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SignoffState {
    /// The signoff (revalidated with `from_stored`).
    pub signoff: Signoff,
    /// A committed revocation targets it.
    pub revoked: bool,
    /// It was consumed by a promotion.
    pub consumed: bool,
}

/// The approval ledger (M4): approval requests, ceremonies (challenges,
/// decisions, revocations, signoffs, enrollments), expiry and signoff reads,
/// on the same shared ledger as [`ExecuteLedger`]. Every state change is one
/// fenced `BEGIN IMMEDIATE` transaction; refusals roll back and are audited.
pub trait ApprovalLedger: ExecuteLedger {
    /// A stored approval request and its status (a read).
    fn approval_request(
        &self,
        request_id: &str,
    ) -> Result<Option<(ApprovalRequest, ApprovalStatus)>, StoreError>;

    /// The `expire` transaction at trusted time `now` (clarification 6):
    /// every `pending` request at or after its expiry becomes `expired` with
    /// an approval event; every `issued` challenge at or after its expiry
    /// becomes `expired`. Never early; terminal statuses never change.
    fn expire(&self, now: ObservedTime) -> Result<Expired, TxFailure>;

    /// The `challenge_issue` transaction (module documentation). Refusals:
    /// the target (`APPROVAL_REQUEST_UNKNOWN`, `APPROVAL_REQUEST_CLOSED`,
    /// `APPROVAL_REQUEST_EXPIRED`, `GRANT_UNKNOWN`, `SIGNOFF_UNKNOWN`,
    /// `SIGNOFF_MISMATCH`, `ENROLLMENT_UNSUPPORTED`), the actor
    /// (`SERVICE_NOT_PERMITTED`, `SUBJECT_REVOKED`, `PERMISSION_MISSING`,
    /// `CREDENTIAL_REVOKED` when it has no active credential), the verifier
    /// (`ASSERTION_INVALID`), a duplicate id or digest (`CHALLENGE_INVALID`).
    /// The verifier is the ledger's configured
    /// [`crate::human::ApprovalChannel`], whose RP id and origin the
    /// challenge records; a ledger without one refuses every ceremony
    /// (`ASSERTION_INVALID`, verifier unavailable).
    fn begin_ceremony(
        &self,
        begin: &CeremonyBegin<'_>,
    ) -> Result<Result<IssuedChallenge, Refusal>, TxFailure>;

    /// The `ceremony` transaction (module documentation): replay of an
    /// identical retry, or the checked decision, revocation, signoff or
    /// enrollment with the challenge consumed atomically. A configured
    /// channel whose RP id or origin differs from the challenge's is
    /// `CHALLENGE_INVALID` (binding mismatch; the challenge is burnt, X4); a
    /// ledger without a configured channel refuses (`ASSERTION_INVALID`,
    /// verifier unavailable) and leaves the challenge issued, as does a
    /// verifier that reports itself unavailable (a transient outage is not a
    /// terminal refusal).
    fn finish_ceremony(
        &self,
        finish: &CeremonyFinish<'_>,
    ) -> Result<Result<CeremonyOutcome, Refusal>, TxFailure>;

    /// A stored signoff with its revocation and consumption status (a read).
    fn signoff(&self, signoff_id: &str) -> Result<Option<SignoffState>, StoreError>;

    /// The one protected read of stored record context (D-044 item 3, Z3):
    /// every lookup or display of a stored approval request, a pending
    /// signoff or the context a ceremony would bind for the approval page
    /// goes through it. Inside one read transaction it requires
    /// `read.session` to equal a consumed read-session login under the
    /// configured channel (subject, credential, page session and issue time
    /// as recorded), unexpired at trusted time; rechecks that the subject is
    /// an active human and the credential active; then that the subject
    /// holds the target's permission over a scope containing the target's
    /// ([`ReadTarget`]). Only then is the context returned, exactly as a
    /// begin at the same trusted time computes it (R6; for a signoff the
    /// effective expiry is computed by the ledger and pinned, D-043 Y1).
    /// Every refusal is the one [`ReadRefused`]. Nothing is written.
    fn read_record(
        &self,
        read: &RecordRead<'_>,
    ) -> Result<Result<RecordView, ReadRefused>, StoreError>;
}

/// What a ceremony would bind and display ([`ApprovalLedger::read_record`]).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CeremonyContext {
    /// The complete displayed context (its canonical digest is
    /// `binding.context_fingerprint`).
    pub context: Value,
    /// The binding a challenge would carry.
    pub binding: ChallengeBinding,
}

/// Lifetime of a registration row (S5, X8): the registration must finish,
/// and the enrollment that uses it must commit, within it.
pub const REGISTRATION_TTL_SECONDS: i64 = 600;

/// The authenticated operator page session a normal (`Enroll`) registration
/// row belongs to (D-043 Y2): the operator who initiates it (an active human
/// holding `operator` in the global scope with an active credential) and the
/// approval listener's page-session id (a random, non-secret identifier held
/// server-side, distinct from the session cookie). The row records both; its
/// finish and the `Enroll` ceremony that consumes it are refused unless they
/// come from the same operator and session. The bootstrap registration has
/// none (the one-shot `BootstrapSession`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OperatorSession {
    /// The initiating operator.
    pub operator: SubjectId,
    /// The page-session id.
    pub session_id: String,
}

/// The start of a registration ceremony (S5, X8). The operation is the
/// ledger's, never the caller's: only the exclusive bootstrap session issues
/// a row here (`Bootstrap`); the serving ledger refuses
/// (`ENROLLMENT_UNSUPPORTED`) and creates `enroll` rows only in a consumed
/// [`CeremonyOp::RegistrationStart`] ceremony (D-043 Y2).
pub struct RegistrationBegin<'a> {
    /// The subject to register (must not exist yet).
    pub subject: &'a SubjectId,
    /// The operator session (required for `Enroll`, absent for `Bootstrap`;
    /// D-043 Y2).
    pub operator: Option<&'a OperatorSession>,
    /// Trusted time (persisted floor). (No verifier: the ledger's configured
    /// [`crate::human::ApprovalChannel`]; its relying party is bound into the
    /// row.)
    pub now: ObservedTime,
}

/// A stored registration row as returned to the page.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct IssuedRegistration {
    /// The registration id.
    pub registration_id: String,
    /// The verifier's creation options for the page.
    pub client_options: Vec<u8>,
    /// Expiry.
    pub expires_at: Timestamp,
}

/// The end of a registration ceremony.
pub struct RegistrationFinish<'a> {
    /// The registration id.
    pub registration_id: &'a str,
    /// The client's registration response.
    pub response: &'a [u8],
    /// The operator session; must equal the row's (D-043 Y2; absent for
    /// `Bootstrap`).
    pub operator: Option<&'a OperatorSession>,
    /// Trusted time (persisted floor). (No verifier: the ledger's configured
    /// [`crate::human::ApprovalChannel`], whose relying party must equal the
    /// row's.)
    pub now: ObservedTime,
}

/// Server-held WebAuthn registration ceremonies (S5, X8): a row bound to
/// subject, RP id, origin, operation and expiry, single use. A finished row
/// records the verified credential; an enrollment accepts only a
/// [`VerifiedRegistration`] equal to a finished row of its operation and
/// subject, and consumes that row in its own transaction. A refused finish
/// burns the row.
pub trait RegistrationLedger {
    /// Issue a registration row. Refusals (`ENROLLMENT_UNSUPPORTED`): an
    /// ill-formed subject, a subject that exists, or a ledger in the wrong
    /// phase for the operation; the verifier (`ASSERTION_INVALID`).
    fn begin_registration(
        &self,
        begin: &RegistrationBegin<'_>,
    ) -> Result<Result<IssuedRegistration, Refusal>, TxFailure>;

    /// Finish a registration row. Refusals: an unknown, finished, burnt or
    /// expired row (`CHALLENGE_INVALID`); the verifier, missing user
    /// verification or a result for another ceremony (`ASSERTION_INVALID`);
    /// a configured channel with another relying party than the row's, a
    /// credential that is not ES256 or whose id is already stored
    /// (`ENROLLMENT_UNSUPPORTED`). A ledger without a configured channel
    /// refuses every registration (`ASSERTION_INVALID`, verifier unavailable).
    fn finish_registration(
        &self,
        finish: &RegistrationFinish<'_>,
    ) -> Result<Result<VerifiedRegistration, Refusal>, TxFailure>;
}
