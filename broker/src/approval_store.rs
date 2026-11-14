//! The approval ledger interface (M4-03, M4-04, M4-05, M4-08 storage, M4-09,
//! M4-10; D-040 O-3..O-9, O-14; M4-INTERFACES I-2..I-8, C1, C2).
//!
//! Storage-independent: the trait, its argument and result types, and the
//! pure functions that derive what a challenge binds from stored state. The
//! SQLite implementation is `writ_ledger_sqlite` (`approval_store.rs`).
//!
//! **Ceremonies.** Every human operation (approve, deny, revoke, signoff,
//! enroll) is two fenced transactions of the ledger:
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
//! ceremony) after a commit whose outcome is unknown replays the committed
//! outcome ([`CeremonyOutcome::replayed`]) and never mints another grant,
//! signoff or revocation; anything else on a consumed challenge is
//! `CHALLENGE_INVALID` (consumed).
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
    NewApprovalRequest, NewSignoff, RevocationTarget, Signoff,
};
use crate::authorize::{Authorized, UseTimeStatus};
use crate::clock::ObservedTime;
use crate::grants::GrantLabel;
use crate::human::{HumanVerifier, Permission, PrincipalKind, SubjectId};
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
        delegation_chain: Vec::new(),
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
/// fingerprint of its displayed context, its acceptance policy bundle digest
/// and the state revision.
pub fn signoff_binding(binding: &NewSignoff) -> Result<ChallengeBinding, CanonicalError> {
    Ok(ChallengeBinding {
        operation: ChallengeOperation::Signoff,
        tenant_id: binding.tenant_id.clone(),
        project_id: binding.project_id.clone(),
        target_id: binding.signoff_id.clone(),
        context_fingerprint: binding.displayed()?.fingerprint().clone(),
        policy_bundle_sha: binding.acceptance_policy_bundle_sha.clone(),
        revision: binding.state_revision,
    })
}

/// A credential to enroll: id and library-serialized public material.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NewCredential {
    /// Credential id.
    pub credential_id: String,
    /// Public passkey material (never a secret).
    pub public_material: Vec<u8>,
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
    /// Its credential (required for a human, absent for a service).
    pub credential: Option<NewCredential>,
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
                ("credential_id", json::s(&c.credential_id)),
                (
                    "public_material_digest",
                    json::s(Digest::of_bytes(&c.public_material).as_str()),
                ),
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
/// (M4-ENROLLMENT-CEREMONY section 2).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BootstrapEnrollment {
    /// The human subject.
    pub subject: SubjectId,
    /// Its credential from a user-verified registration.
    pub credential: NewCredential,
    /// The requester identities it controls (R1).
    pub linked_identities: Vec<SubjectId>,
}

/// Parties a signoff excludes beyond the durable artifact and policy
/// changers the ledger reads itself (C2): they can only add exclusions; the
/// I-5 use-time check repeats separation against the promotion request.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SignoffParties {
    /// Requester of the promotion the signoff is for.
    pub requester: SubjectId,
    /// Its agent, if any.
    pub agent: Option<SubjectId>,
    /// Its trusted delegation chain.
    pub delegation_chain: Vec<SubjectId>,
}

/// The human operation of one ceremony.
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
        /// Additional excluded parties.
        parties: SignoffParties,
    },
    /// Enroll a subject (administrative ceremony).
    Enroll {
        /// The enrollment.
        enrollment: NewEnrollment,
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
    /// The verifier.
    pub verifier: &'a dyn HumanVerifier,
    /// Trusted time (persisted floor).
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
    /// Channel identity from the listener configuration.
    pub channel: &'a str,
    /// The verifier.
    pub verifier: &'a dyn HumanVerifier,
    /// Trusted time (persisted floor).
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
    fn begin_ceremony(
        &self,
        begin: &CeremonyBegin<'_>,
    ) -> Result<Result<IssuedChallenge, Refusal>, TxFailure>;

    /// The `ceremony` transaction (module documentation): replay of an
    /// identical retry, or the checked decision, revocation, signoff or
    /// enrollment with the challenge consumed atomically.
    fn finish_ceremony(
        &self,
        finish: &CeremonyFinish<'_>,
    ) -> Result<Result<CeremonyOutcome, Refusal>, TxFailure>;

    /// A stored signoff with its revocation and consumption status (a read).
    fn signoff(&self, signoff_id: &str) -> Result<Option<SignoffState>, StoreError>;
}
