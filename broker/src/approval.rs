//! Approval requests, decisions, challenges, revocations and signoffs (M4-01;
//! D-040 O-3..O-9; M4-INTERFACES I-2..I-6). Storage-independent records and
//! pure checks; persistence is `writ-ledger-6`.
//!
//! **Approval request** ([`ApprovalRequest`]): immutable once built (private
//! fields, getters only); exactly one required approver (O-9, otherwise
//! `APPROVAL_QUORUM_UNSUPPORTED`). Its [`ApprovalStatus`] moves only through
//! [`transition`] (approve or deny while pending and unexpired) and
//! [`expire`] (pending at or after expiry); `approved`, `denied` and
//! `expired` never reopen.
//!
//! **Challenge** ([`Challenge`]): the server-side binding of one ceremony to
//! the subject, its allowed credentials, operation, verdict, tenant, project,
//! target, complete context fingerprint ([`context_fingerprint`], the D-006
//! digest of the target context as displayed), policy bundle digest and
//! relevant revision; `expires_at = min(issued_at + 120 s, target expiry)`
//! ([`challenge_expiry`]); single use ([`Challenge::consume`]). Every failure
//! is `CHALLENGE_INVALID` with a [`ChallengeRefusal`] diagnostic.
//!
//! **Codes** ([`ApprovalCode`]): the I-6 internal codes; every one maps to
//! the kernel's generic external refusal ([`crate::refusal::Refusal::Approval`]).

use std::fmt;

use writ_kernel::json::{self, CanonicalError, Digest, Value};
use writ_kernel::time::Timestamp;

use crate::grants::Verdict;
use crate::human::{Parties, SubjectId, VerifierError};

/// Version of approval, revocation and signoff provenance (successor of
/// `writ-provenance-1`).
pub const PROVENANCE_VERSION_2: &str = "writ-provenance-2";
/// Maximum challenge lifetime in seconds (O-3).
pub const CHALLENGE_TTL_SECONDS: i64 = 120;
/// Context member naming an approval-request context (domain separation).
pub const APPROVAL_CONTEXT: &str = "writ.approval-request-context.v1";
/// Context member naming a signoff context (domain separation).
pub const SIGNOFF_CONTEXT: &str = "writ.signoff-context.v1";

/// I-6 internal codes. Agents receive only the kernel's generic refusal.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ApprovalCode {
    /// No stored approval request with that id in scope.
    RequestUnknown,
    /// The request is already approved or denied.
    RequestClosed,
    /// The request is expired.
    RequestExpired,
    /// The policy needs more than one approver (O-9).
    QuorumUnsupported,
    /// Unknown, consumed, expired or mismatched challenge.
    ChallengeInvalid(ChallengeRefusal),
    /// The verifier refused the assertion (category only).
    AssertionInvalid(VerifierError),
    /// The credential is revoked.
    CredentialRevoked,
    /// The subject is revoked.
    SubjectRevoked,
    /// The subject lacks the permission for the scope.
    PermissionMissing,
    /// The subject is an excluded party (O-7).
    SeparationViolated,
    /// A service attempted a human-only permission (O-8).
    ServiceNotPermitted,
    /// Enrollment or recovery outside the administrative ceremony.
    EnrollmentUnsupported,
    /// The grant is revoked.
    GrantRevoked,
    /// No such signoff in scope.
    SignoffUnknown,
    /// The signoff is revoked.
    SignoffRevoked,
    /// The signoff binding differs from the record, requirements or state.
    SignoffMismatch,
    /// The signoff is expired at trusted time.
    SignoffExpired,
    /// The signoff was already consumed.
    SignoffConsumed,
}

impl ApprovalCode {
    /// Internal code.
    pub fn code(self) -> &'static str {
        use ApprovalCode::*;
        match self {
            RequestUnknown => "APPROVAL_REQUEST_UNKNOWN",
            RequestClosed => "APPROVAL_REQUEST_CLOSED",
            RequestExpired => "APPROVAL_REQUEST_EXPIRED",
            QuorumUnsupported => "APPROVAL_QUORUM_UNSUPPORTED",
            ChallengeInvalid(_) => "CHALLENGE_INVALID",
            AssertionInvalid(_) => "ASSERTION_INVALID",
            CredentialRevoked => "CREDENTIAL_REVOKED",
            SubjectRevoked => "SUBJECT_REVOKED",
            PermissionMissing => "PERMISSION_MISSING",
            SeparationViolated => "SEPARATION_VIOLATED",
            ServiceNotPermitted => "SERVICE_NOT_PERMITTED",
            EnrollmentUnsupported => "ENROLLMENT_UNSUPPORTED",
            GrantRevoked => "GRANT_REVOKED",
            SignoffUnknown => "SIGNOFF_UNKNOWN",
            SignoffRevoked => "SIGNOFF_REVOKED",
            SignoffMismatch => "SIGNOFF_MISMATCH",
            SignoffExpired => "SIGNOFF_EXPIRED",
            SignoffConsumed => "SIGNOFF_CONSUMED",
        }
    }

    /// Operator diagnostic (challenge and assertion refusals only).
    pub fn diagnostic(self) -> Option<&'static str> {
        match self {
            ApprovalCode::ChallengeInvalid(r) => Some(r.diagnostic()),
            ApprovalCode::AssertionInvalid(e) => Some(e.category()),
            _ => None,
        }
    }
}

impl fmt::Display for ApprovalCode {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.code())
    }
}

/// Why a challenge was refused (`CHALLENGE_INVALID`; operator diagnostic).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ChallengeRefusal {
    /// No such challenge.
    Unknown,
    /// Already consumed.
    Consumed,
    /// Expired (status or trusted time), or the target expired before issue.
    Expired,
    /// Another subject.
    SubjectMismatch,
    /// The credential is not among the allowed ones.
    CredentialNotAllowed,
    /// Another operation.
    OperationMismatch,
    /// Another verdict.
    VerdictMismatch,
    /// Another tenant.
    TenantMismatch,
    /// Another project.
    ProjectMismatch,
    /// Another target.
    TargetMismatch,
    /// Another context fingerprint (the displayed context changed).
    ContextMismatch,
    /// Another policy bundle digest.
    PolicyMismatch,
    /// Another revision.
    RevisionMismatch,
}

impl ChallengeRefusal {
    /// Operator diagnostic.
    pub fn diagnostic(self) -> &'static str {
        use ChallengeRefusal::*;
        match self {
            Unknown => "challenge: unknown",
            Consumed => "challenge: consumed",
            Expired => "challenge: expired",
            SubjectMismatch => "challenge: subject mismatch",
            CredentialNotAllowed => "challenge: credential not allowed",
            OperationMismatch => "challenge: operation mismatch",
            VerdictMismatch => "challenge: verdict mismatch",
            TenantMismatch => "challenge: tenant mismatch",
            ProjectMismatch => "challenge: project mismatch",
            TargetMismatch => "challenge: target mismatch",
            ContextMismatch => "challenge: context mismatch",
            PolicyMismatch => "challenge: policy mismatch",
            RevisionMismatch => "challenge: revision mismatch",
        }
    }
}

fn ts(t: Timestamp) -> Value {
    json::s(&t.to_string())
}

fn subject_value(s: &SubjectId) -> Value {
    json::obj([
        ("issuer", json::s(&s.issuer)),
        ("subject", json::s(&s.subject)),
    ])
}

/// The D-006 canonical digest of a stored target context as displayed.
pub fn context_fingerprint(context: &Value) -> Result<Digest, CanonicalError> {
    json::digest(context)
}

/// The fields of a new approval request (built only by the broker's decision).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NewApprovalRequest {
    /// The kernel `approval_request_id`.
    pub request_id: String,
    /// Tenant.
    pub tenant_id: String,
    /// Project.
    pub project_id: String,
    /// Requester principal.
    pub requester: SubjectId,
    /// Agent, if any.
    pub agent: Option<SubjectId>,
    /// Trusted delegation chain.
    pub delegation_chain: Vec<SubjectId>,
    /// Action kind.
    pub action_kind: String,
    /// Canonical resource id.
    pub canonical_resource_id: String,
    /// Action digest.
    pub action_digest: String,
    /// Requested capabilities (stored sorted and unique).
    pub capabilities: Vec<String>,
    /// Policy bundle digest.
    pub policy_bundle_sha: String,
    /// D-012 request fingerprint.
    pub request_fingerprint: String,
    /// Creation (trusted time).
    pub created_at: Timestamp,
    /// Expiry.
    pub expires_at: Timestamp,
    /// Required approvers (only 1 is supported).
    pub required_approvers: u32,
}

/// Why a new approval request could not be built.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RequestInvalid {
    /// More (or fewer) than one approver required (`APPROVAL_QUORUM_UNSUPPORTED`).
    QuorumUnsupported,
    /// An empty identifier, or expiry not after creation.
    Malformed,
}

/// An immutable stored approval request (I-3).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ApprovalRequest(NewApprovalRequest);

impl ApprovalRequest {
    /// Validate and freeze. Capabilities are sorted and deduplicated.
    pub fn new(mut fields: NewApprovalRequest) -> Result<ApprovalRequest, RequestInvalid> {
        if fields.required_approvers != 1 {
            return Err(RequestInvalid::QuorumUnsupported);
        }
        let texts = [
            &fields.request_id,
            &fields.tenant_id,
            &fields.project_id,
            &fields.action_kind,
            &fields.canonical_resource_id,
            &fields.action_digest,
            &fields.policy_bundle_sha,
            &fields.request_fingerprint,
            &fields.requester.issuer,
            &fields.requester.subject,
        ];
        if texts.iter().any(|t| t.is_empty()) || fields.expires_at <= fields.created_at {
            return Err(RequestInvalid::Malformed);
        }
        fields.capabilities.sort();
        fields.capabilities.dedup();
        Ok(ApprovalRequest(fields))
    }

    /// The stored fields (read-only).
    pub fn fields(&self) -> &NewApprovalRequest {
        &self.0
    }

    /// Request id.
    pub fn request_id(&self) -> &str {
        &self.0.request_id
    }

    /// Expiry.
    pub fn expires_at(&self) -> Timestamp {
        self.0.expires_at
    }

    /// The parties excluded from approving it (O-7).
    pub fn parties(&self) -> Parties<'_> {
        Parties {
            requester: &self.0.requester,
            agent: self.0.agent.as_ref(),
            delegation_chain: &self.0.delegation_chain,
        }
    }

    /// The complete context as displayed to the approver.
    pub fn context(&self) -> Value {
        let r = &self.0;
        json::obj([
            ("action_digest", json::s(&r.action_digest)),
            ("action_kind", json::s(&r.action_kind)),
            ("agent", r.agent.as_ref().map_or(Value::Null, subject_value)),
            ("canonical_resource_id", json::s(&r.canonical_resource_id)),
            (
                "capabilities",
                Value::Arr(r.capabilities.iter().map(|c| json::s(c)).collect()),
            ),
            ("context", json::s(APPROVAL_CONTEXT)),
            ("created_at", ts(r.created_at)),
            (
                "delegation_chain",
                Value::Arr(r.delegation_chain.iter().map(subject_value).collect()),
            ),
            ("expires_at", ts(r.expires_at)),
            ("policy_bundle_sha", json::s(&r.policy_bundle_sha)),
            ("project_id", json::s(&r.project_id)),
            ("request_fingerprint", json::s(&r.request_fingerprint)),
            ("request_id", json::s(&r.request_id)),
            ("requester", subject_value(&r.requester)),
            (
                "required_approvers",
                Value::Int(i64::from(r.required_approvers)),
            ),
            ("tenant_id", json::s(&r.tenant_id)),
        ])
    }

    /// [`context_fingerprint`] of [`ApprovalRequest::context`].
    pub fn fingerprint(&self) -> Result<Digest, CanonicalError> {
        context_fingerprint(&self.context())
    }
}

/// Status of a stored approval request.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ApprovalStatus {
    /// Awaiting a decision.
    Pending,
    /// Approved (produced exactly one grant).
    Approved,
    /// Denied (terminal).
    Denied,
    /// Expired (terminal).
    Expired,
}

impl ApprovalStatus {
    /// The stored text.
    pub fn as_str(self) -> &'static str {
        match self {
            ApprovalStatus::Pending => "pending",
            ApprovalStatus::Approved => "approved",
            ApprovalStatus::Denied => "denied",
            ApprovalStatus::Expired => "expired",
        }
    }

    /// Parse the stored text.
    pub fn parse(text: &str) -> Option<ApprovalStatus> {
        match text {
            "pending" => Some(ApprovalStatus::Pending),
            "approved" => Some(ApprovalStatus::Approved),
            "denied" => Some(ApprovalStatus::Denied),
            "expired" => Some(ApprovalStatus::Expired),
            _ => None,
        }
    }
}

/// The only status changes a decision may make: `pending` and unexpired at
/// trusted time `now` to `approved` or `denied`. A closed request is
/// `APPROVAL_REQUEST_CLOSED`; an expired one (stored or due) is
/// `APPROVAL_REQUEST_EXPIRED`. Nothing ever reopens.
pub fn transition(
    from: ApprovalStatus,
    verdict: Verdict,
    now: Timestamp,
    expires_at: Timestamp,
) -> Result<ApprovalStatus, ApprovalCode> {
    match from {
        ApprovalStatus::Approved | ApprovalStatus::Denied => Err(ApprovalCode::RequestClosed),
        ApprovalStatus::Expired => Err(ApprovalCode::RequestExpired),
        ApprovalStatus::Pending if now >= expires_at => Err(ApprovalCode::RequestExpired),
        ApprovalStatus::Pending => Ok(match verdict {
            Verdict::Approved => ApprovalStatus::Approved,
            Verdict::Denied => ApprovalStatus::Denied,
        }),
    }
}

/// `Some(Expired)` exactly when a `pending` request is due at `now`.
pub fn expire(
    from: ApprovalStatus,
    now: Timestamp,
    expires_at: Timestamp,
) -> Option<ApprovalStatus> {
    (from == ApprovalStatus::Pending && now >= expires_at).then_some(ApprovalStatus::Expired)
}

/// One human decision on an approval request (several rows may exist; one
/// suffices, no quorum implied).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ApprovalDecision {
    /// Approval request id.
    pub request_id: String,
    /// Verdict.
    pub verdict: Verdict,
    /// The deciding subject.
    pub subject: SubjectId,
    /// The credential that signed the assertion.
    pub credential_id: String,
    /// Channel identity from the listener configuration.
    pub channel: String,
    /// Trusted decision time.
    pub decided_at: Timestamp,
    /// The consumed challenge.
    pub challenge_id: String,
    /// [`PROVENANCE_VERSION_2`].
    pub provenance_version: &'static str,
}

/// The operation a challenge authorizes.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ChallengeOperation {
    /// Approve an approval request.
    Approve,
    /// Deny an approval request.
    Deny,
    /// Revoke a grant, signoff, credential, subject or role.
    Revoke,
    /// Sign off a human criterion.
    Signoff,
    /// Enroll a credential (administrative ceremony).
    Enroll,
    /// Change a role assignment.
    RoleChange,
}

impl ChallengeOperation {
    /// The stored text.
    pub fn as_str(self) -> &'static str {
        match self {
            ChallengeOperation::Approve => "approve",
            ChallengeOperation::Deny => "deny",
            ChallengeOperation::Revoke => "revoke",
            ChallengeOperation::Signoff => "signoff",
            ChallengeOperation::Enroll => "enroll",
            ChallengeOperation::RoleChange => "role_change",
        }
    }
}

/// Status of a stored challenge.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ChallengeStatus {
    /// Issued, not yet used.
    Issued,
    /// Consumed with its durable decision.
    Consumed,
    /// Expired.
    Expired,
}

/// What a challenge is bound to, and what a decision presents.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ChallengeBinding {
    /// Operation.
    pub operation: ChallengeOperation,
    /// Operation-specific verdict text (for example `approved`, `denied`).
    pub verdict: String,
    /// Tenant.
    pub tenant_id: String,
    /// Project.
    pub project_id: String,
    /// Target id (approval request, grant, signoff or subject).
    pub target_id: String,
    /// [`context_fingerprint`] of the target context as displayed.
    pub context_fingerprint: Digest,
    /// Policy bundle digest.
    pub policy_bundle_sha: String,
    /// The relevant revision.
    pub revision: i64,
}

/// `min(issued_at + 120 s, target_expiry)`.
pub fn challenge_expiry(issued_at: Timestamp, target_expiry: Timestamp) -> Timestamp {
    issued_at
        .plus_seconds(CHALLENGE_TTL_SECONDS)
        .min(target_expiry)
}

/// A server-side challenge row (I-2).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Challenge {
    /// Challenge id.
    pub challenge_id: String,
    /// Digest of the server challenge (from the verifier's `begin`).
    pub challenge_digest: Digest,
    /// Subject the ceremony is for.
    pub subject: SubjectId,
    /// Credential ids allowed to answer.
    pub allowed_credentials: Vec<String>,
    /// The binding.
    pub binding: ChallengeBinding,
    /// Issue time.
    pub issued_at: Timestamp,
    /// Expiry ([`challenge_expiry`]).
    pub expires_at: Timestamp,
    /// Idempotency key of the decision.
    pub idempotency_key: String,
    /// Status.
    pub status: ChallengeStatus,
}

/// The fields of a challenge to issue.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NewChallenge {
    /// Challenge id.
    pub challenge_id: String,
    /// Digest of the server challenge.
    pub challenge_digest: Digest,
    /// Subject.
    pub subject: SubjectId,
    /// Allowed credential ids.
    pub allowed_credentials: Vec<String>,
    /// The binding.
    pub binding: ChallengeBinding,
    /// Trusted issue time.
    pub issued_at: Timestamp,
    /// The target's own expiry.
    pub target_expiry: Timestamp,
    /// Idempotency key.
    pub idempotency_key: String,
}

impl Challenge {
    /// Issue a challenge; refused (`Expired`) when the target already
    /// expired at `issued_at`, so the lifetime is never zero or negative.
    pub fn issue(new: NewChallenge) -> Result<Challenge, ChallengeRefusal> {
        let expires_at = challenge_expiry(new.issued_at, new.target_expiry);
        if expires_at <= new.issued_at {
            return Err(ChallengeRefusal::Expired);
        }
        Ok(Challenge {
            challenge_id: new.challenge_id,
            challenge_digest: new.challenge_digest,
            subject: new.subject,
            allowed_credentials: new.allowed_credentials,
            binding: new.binding,
            issued_at: new.issued_at,
            expires_at,
            idempotency_key: new.idempotency_key,
            status: ChallengeStatus::Issued,
        })
    }

    /// The pure validity check at trusted time `now`: status, expiry,
    /// subject, credential, then every bound field in order.
    pub fn check(
        &self,
        presented: &ChallengeBinding,
        subject: &SubjectId,
        credential_id: &str,
        now: Timestamp,
    ) -> Result<(), ChallengeRefusal> {
        use ChallengeRefusal::*;
        match self.status {
            ChallengeStatus::Consumed => return Err(Consumed),
            ChallengeStatus::Expired => return Err(Expired),
            ChallengeStatus::Issued => {}
        }
        if now >= self.expires_at {
            return Err(Expired);
        }
        if &self.subject != subject {
            return Err(SubjectMismatch);
        }
        if !self.allowed_credentials.iter().any(|c| c == credential_id) {
            return Err(CredentialNotAllowed);
        }
        let b = &self.binding;
        let checks = [
            (b.operation == presented.operation, OperationMismatch),
            (b.verdict == presented.verdict, VerdictMismatch),
            (b.tenant_id == presented.tenant_id, TenantMismatch),
            (b.project_id == presented.project_id, ProjectMismatch),
            (b.target_id == presented.target_id, TargetMismatch),
            (
                b.context_fingerprint == presented.context_fingerprint,
                ContextMismatch,
            ),
            (
                b.policy_bundle_sha == presented.policy_bundle_sha,
                PolicyMismatch,
            ),
            (b.revision == presented.revision, RevisionMismatch),
        ];
        match checks.into_iter().find(|(ok, _)| !ok) {
            Some((_, refusal)) => Err(refusal),
            None => Ok(()),
        }
    }

    /// Single use: an issued challenge becomes consumed; anything else is refused.
    pub fn consume(&self) -> Result<Challenge, ChallengeRefusal> {
        match self.status {
            ChallengeStatus::Issued => Ok(Challenge {
                status: ChallengeStatus::Consumed,
                ..self.clone()
            }),
            ChallengeStatus::Consumed => Err(ChallengeRefusal::Consumed),
            ChallengeStatus::Expired => Err(ChallengeRefusal::Expired),
        }
    }
}

/// What a revocation targets (I-4).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RevocationTarget {
    /// A grant label.
    Grant(String),
    /// A signoff id.
    Signoff(String),
    /// A credential id.
    Credential(String),
    /// A subject.
    Subject(SubjectId),
    /// A role assignment id.
    Role(String),
}

/// A committed revocation (I-4). Never refunds, replenishes or substitutes.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Revocation {
    /// Target.
    pub target: RevocationTarget,
    /// The revoking subject.
    pub by: SubjectId,
    /// Trusted time.
    pub at: Timestamp,
    /// Reason (operator text).
    pub reason: String,
    /// The consumed challenge.
    pub challenge_id: String,
}

/// A human signoff of one criterion (I-5), stored in the ledger and consumed
/// single-use ledger-wide with the promotion.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Signoff {
    /// Signoff id.
    pub signoff_id: String,
    /// Tenant.
    pub tenant_id: String,
    /// Project.
    pub project_id: String,
    /// Task.
    pub task_id: String,
    /// Workflow.
    pub workflow_id: String,
    /// Transition source state.
    pub from_state: String,
    /// Transition destination state.
    pub to_state: String,
    /// Acceptance record id.
    pub acceptance_record_id: String,
    /// Criterion id.
    pub criterion_id: String,
    /// Artifact snapshot id.
    pub artifact_snapshot_id: String,
    /// Artifact digest.
    pub artifact_digest: String,
    /// Acceptance policy id.
    pub acceptance_policy_id: String,
    /// Acceptance policy version.
    pub acceptance_policy_version: String,
    /// Acceptance policy bundle digest.
    pub acceptance_policy_bundle_sha: String,
    /// Workflow state revision.
    pub state_revision: i64,
    /// Issue time.
    pub issued_at: Timestamp,
    /// Expiry.
    pub expires_at: Timestamp,
    /// The signing subject.
    pub subject: SubjectId,
    /// The credential that signed.
    pub credential_id: String,
    /// The consumed challenge.
    pub challenge_id: String,
    /// [`PROVENANCE_VERSION_2`].
    pub provenance_version: String,
}

impl Signoff {
    /// The signed context as displayed: every binding member, without the
    /// signoff's own id, issue time, signer, credential, challenge or provenance.
    pub fn context(&self) -> Value {
        json::obj([
            (
                "acceptance_policy_bundle_sha",
                json::s(&self.acceptance_policy_bundle_sha),
            ),
            ("acceptance_policy_id", json::s(&self.acceptance_policy_id)),
            (
                "acceptance_policy_version",
                json::s(&self.acceptance_policy_version),
            ),
            ("acceptance_record_id", json::s(&self.acceptance_record_id)),
            ("artifact_digest", json::s(&self.artifact_digest)),
            ("artifact_snapshot_id", json::s(&self.artifact_snapshot_id)),
            ("context", json::s(SIGNOFF_CONTEXT)),
            ("criterion_id", json::s(&self.criterion_id)),
            ("expires_at", ts(self.expires_at)),
            ("project_id", json::s(&self.project_id)),
            ("state_revision", Value::Int(self.state_revision)),
            ("task_id", json::s(&self.task_id)),
            ("tenant_id", json::s(&self.tenant_id)),
            (
                "transition",
                json::obj([
                    ("from_state", json::s(&self.from_state)),
                    ("to_state", json::s(&self.to_state)),
                ]),
            ),
            ("workflow_id", json::s(&self.workflow_id)),
        ])
    }

    /// [`context_fingerprint`] of [`Signoff::context`].
    pub fn fingerprint(&self) -> Result<Digest, CanonicalError> {
        context_fingerprint(&self.context())
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;
    use crate::human::approval_separation;

    fn t(s: &str) -> Timestamp {
        Timestamp::parse_strict(s).unwrap()
    }

    fn sha(c: char) -> String {
        format!("sha256:{}", c.to_string().repeat(64))
    }

    fn new_request() -> NewApprovalRequest {
        NewApprovalRequest {
            request_id: "apr-1".into(),
            tenant_id: "t1".into(),
            project_id: "p1".into(),
            requester: SubjectId::new("adapter", "alice"),
            agent: Some(SubjectId::new("agent-realm", "agent-7")),
            delegation_chain: vec![SubjectId::new("agent-realm", "parent-1")],
            action_kind: "file.write".into(),
            canonical_resource_id: "file:t1/p1/src/a.rs".into(),
            action_digest: sha('a'),
            capabilities: vec!["write".into(), "read".into(), "write".into()],
            policy_bundle_sha: sha('c'),
            request_fingerprint: sha('f'),
            created_at: t("2026-11-07T10:00:00Z"),
            expires_at: t("2026-11-07T10:10:00Z"),
            required_approvers: 1,
        }
    }

    fn binding() -> ChallengeBinding {
        ChallengeBinding {
            operation: ChallengeOperation::Approve,
            verdict: "approved".into(),
            tenant_id: "t1".into(),
            project_id: "p1".into(),
            target_id: "apr-1".into(),
            context_fingerprint: ApprovalRequest::new(new_request())
                .unwrap()
                .fingerprint()
                .unwrap(),
            policy_bundle_sha: sha('c'),
            revision: 7,
        }
    }

    fn challenge(issued: &str, target_expiry: &str) -> Challenge {
        Challenge::issue(NewChallenge {
            challenge_id: "ch-1".into(),
            challenge_digest: Digest::of_bytes(b"nonce"),
            subject: SubjectId::new("writ-local", "bob"),
            allowed_credentials: vec!["cred-1".into()],
            binding: binding(),
            issued_at: t(issued),
            target_expiry: t(target_expiry),
            idempotency_key: "idem-1".into(),
        })
        .unwrap()
    }

    #[test]
    fn approval_request_is_validated_and_frozen() {
        let r = ApprovalRequest::new(new_request()).unwrap();
        assert_eq!(r.fields().capabilities, ["read", "write"]);
        let mut n = new_request();
        n.required_approvers = 2;
        assert_eq!(
            ApprovalRequest::new(n),
            Err(RequestInvalid::QuorumUnsupported)
        );
        let mut n = new_request();
        n.required_approvers = 0;
        assert_eq!(
            ApprovalRequest::new(n),
            Err(RequestInvalid::QuorumUnsupported)
        );
        let mut n = new_request();
        n.expires_at = n.created_at;
        assert_eq!(ApprovalRequest::new(n), Err(RequestInvalid::Malformed));
        let mut n = new_request();
        n.action_digest.clear();
        assert_eq!(ApprovalRequest::new(n), Err(RequestInvalid::Malformed));
    }

    #[test]
    fn transitions_from_pending_only_and_terminal_states_never_reopen() {
        use ApprovalStatus::*;
        let exp = t("2026-11-07T10:10:00Z");
        let before = t("2026-11-07T10:09:59Z");
        assert_eq!(
            transition(Pending, Verdict::Approved, before, exp),
            Ok(Approved)
        );
        assert_eq!(
            transition(Pending, Verdict::Denied, before, exp),
            Ok(Denied)
        );
        for v in [Verdict::Approved, Verdict::Denied] {
            // Expiry is decided at trusted time, inclusive.
            assert_eq!(
                transition(Pending, v, exp, exp),
                Err(ApprovalCode::RequestExpired)
            );
            for now in [before, exp, t("2030-01-01T00:00:00Z")] {
                assert_eq!(
                    transition(Denied, v, now, exp),
                    Err(ApprovalCode::RequestClosed)
                );
                assert_eq!(
                    transition(Approved, v, now, exp),
                    Err(ApprovalCode::RequestClosed)
                );
                assert_eq!(
                    transition(Expired, v, now, exp),
                    Err(ApprovalCode::RequestExpired)
                );
            }
        }
        assert_eq!(expire(Pending, exp, exp), Some(Expired));
        assert_eq!(expire(Pending, before, exp), None);
        for s in [Approved, Denied, Expired] {
            assert_eq!(expire(s, exp, exp), None, "{s:?}");
            assert_eq!(ApprovalStatus::parse(s.as_str()), Some(s));
        }
        assert_eq!(ApprovalStatus::parse("reopened"), None);
    }

    #[test]
    fn approval_separation_uses_the_stored_request_parties() {
        let r = ApprovalRequest::new(new_request()).unwrap();
        for bad in ["alice", "agent-7", "parent-1"] {
            assert_eq!(
                approval_separation(&SubjectId::new("writ-local", bad), &r.parties()),
                Err(ApprovalCode::SeparationViolated),
                "{bad}"
            );
        }
        assert_eq!(
            approval_separation(&SubjectId::new("writ-local", "bob"), &r.parties()),
            Ok(())
        );
    }

    #[test]
    fn the_context_fingerprint_is_the_d006_digest_of_every_displayed_field() {
        let base = ApprovalRequest::new(new_request()).unwrap();
        let fp = base.fingerprint().unwrap();
        assert_eq!(fp, json::digest(&base.context()).unwrap());
        assert_eq!(
            fp.as_str(),
            Digest::of_bytes(json::canonical(&base.context()).unwrap().as_bytes()).as_str()
        );
        let changes: Vec<fn(&mut NewApprovalRequest)> = vec![
            |n| n.request_id = "apr-2".into(),
            |n| n.tenant_id = "t2".into(),
            |n| n.project_id = "p2".into(),
            |n| n.requester.subject = "mallory".into(),
            |n| n.agent = None,
            |n| n.delegation_chain.clear(),
            |n| n.action_kind = "file.delete".into(),
            |n| n.canonical_resource_id = "file:t1/p1/src/b.rs".into(),
            |n| n.action_digest = format!("sha256:{}", "b".repeat(64)),
            |n| n.capabilities.push("exec".into()),
            |n| n.policy_bundle_sha = format!("sha256:{}", "d".repeat(64)),
            |n| n.request_fingerprint = format!("sha256:{}", "e".repeat(64)),
            |n| n.created_at = n.created_at.plus_seconds(1),
            |n| n.expires_at = n.expires_at.plus_seconds(1),
        ];
        for (i, change) in changes.into_iter().enumerate() {
            let mut n = new_request();
            change(&mut n);
            let other = ApprovalRequest::new(n).unwrap().fingerprint().unwrap();
            assert_ne!(other, fp, "change {i}");
        }
    }

    #[test]
    fn challenge_expiry_is_bound_by_120_seconds_and_by_the_target() {
        let c = challenge("2026-11-07T10:00:00Z", "2026-11-07T10:10:00Z");
        assert_eq!(c.expires_at, t("2026-11-07T10:02:00Z"));
        let c = challenge("2026-11-07T10:09:30Z", "2026-11-07T10:10:00Z");
        assert_eq!(c.expires_at, t("2026-11-07T10:10:00Z"));
        let bob = SubjectId::new("writ-local", "bob");
        assert_eq!(
            c.check(&binding(), &bob, "cred-1", t("2026-11-07T10:09:59Z")),
            Ok(())
        );
        assert_eq!(
            c.check(&binding(), &bob, "cred-1", t("2026-11-07T10:10:00Z")),
            Err(ChallengeRefusal::Expired)
        );
        // A target already expired cannot be challenged.
        for issued in ["2026-11-07T10:10:00Z", "2026-11-07T10:10:01Z"] {
            let refused = Challenge::issue(NewChallenge {
                challenge_id: "ch-2".into(),
                challenge_digest: Digest::of_bytes(b"n"),
                subject: bob.clone(),
                allowed_credentials: vec!["cred-1".into()],
                binding: binding(),
                issued_at: t(issued),
                target_expiry: t("2026-11-07T10:10:00Z"),
                idempotency_key: "idem-2".into(),
            });
            assert_eq!(refused, Err(ChallengeRefusal::Expired), "{issued}");
        }
        assert_eq!(
            challenge_expiry(t("2026-11-07T10:00:00Z"), t("2027-01-01T00:00:00Z")),
            t("2026-11-07T10:02:00Z")
        );
    }

    #[test]
    fn challenge_mismatch_on_every_bound_field_is_refused() {
        use ChallengeRefusal::*;
        let c = challenge("2026-11-07T10:00:00Z", "2026-11-07T10:10:00Z");
        let bob = SubjectId::new("writ-local", "bob");
        let now = t("2026-11-07T10:01:00Z");
        assert_eq!(c.check(&binding(), &bob, "cred-1", now), Ok(()));
        type Change = fn(&mut ChallengeBinding);
        let cases: Vec<(Change, ChallengeRefusal)> = vec![
            (
                |b| b.operation = ChallengeOperation::Deny,
                OperationMismatch,
            ),
            (
                |b| b.operation = ChallengeOperation::Signoff,
                OperationMismatch,
            ),
            (|b| b.verdict = "denied".into(), VerdictMismatch),
            (|b| b.tenant_id = "t2".into(), TenantMismatch),
            (|b| b.project_id = "p2".into(), ProjectMismatch),
            (|b| b.target_id = "apr-2".into(), TargetMismatch),
            (
                |b| b.context_fingerprint = Digest::of_bytes(b"other"),
                ContextMismatch,
            ),
            (
                |b| b.policy_bundle_sha = format!("sha256:{}", "d".repeat(64)),
                PolicyMismatch,
            ),
            (|b| b.revision = 8, RevisionMismatch),
        ];
        for (change, want) in cases {
            let mut presented = binding();
            change(&mut presented);
            assert_eq!(c.check(&presented, &bob, "cred-1", now), Err(want));
            assert_eq!(
                ApprovalCode::ChallengeInvalid(want).code(),
                "CHALLENGE_INVALID"
            );
        }
        assert_eq!(
            c.check(
                &binding(),
                &SubjectId::new("writ-local", "carol"),
                "cred-1",
                now
            ),
            Err(SubjectMismatch)
        );
        assert_eq!(
            c.check(
                &binding(),
                &SubjectId::new("other-realm", "bob"),
                "cred-1",
                now
            ),
            Err(SubjectMismatch)
        );
        assert_eq!(
            c.check(&binding(), &bob, "cred-2", now),
            Err(CredentialNotAllowed)
        );
    }

    #[test]
    fn a_challenge_is_single_use() {
        let c = challenge("2026-11-07T10:00:00Z", "2026-11-07T10:10:00Z");
        let used = c.consume().unwrap();
        assert_eq!(used.status, ChallengeStatus::Consumed);
        assert_eq!(used.consume(), Err(ChallengeRefusal::Consumed));
        let bob = SubjectId::new("writ-local", "bob");
        let now = t("2026-11-07T10:01:00Z");
        assert_eq!(
            used.check(&binding(), &bob, "cred-1", now),
            Err(ChallengeRefusal::Consumed)
        );
        let mut expired = c;
        expired.status = ChallengeStatus::Expired;
        assert_eq!(expired.consume(), Err(ChallengeRefusal::Expired));
        assert_eq!(
            expired.check(&binding(), &bob, "cred-1", now),
            Err(ChallengeRefusal::Expired)
        );
    }

    #[test]
    fn signoff_context_binds_every_acceptance_member() {
        let s = Signoff {
            signoff_id: "so-1".into(),
            tenant_id: "t1".into(),
            project_id: "p1".into(),
            task_id: "task-1".into(),
            workflow_id: "wf-1".into(),
            from_state: "verifying".into(),
            to_state: "accepted".into(),
            acceptance_record_id: "acc-1".into(),
            criterion_id: "signoff".into(),
            artifact_snapshot_id: "snap-1".into(),
            artifact_digest: sha('b'),
            acceptance_policy_id: "accept-code-review".into(),
            acceptance_policy_version: "3".into(),
            acceptance_policy_bundle_sha: sha('c'),
            state_revision: 7,
            issued_at: t("2026-11-07T10:00:00Z"),
            expires_at: t("2026-11-07T11:00:00Z"),
            subject: SubjectId::new("writ-local", "bob"),
            credential_id: "cred-1".into(),
            challenge_id: "ch-1".into(),
            provenance_version: PROVENANCE_VERSION_2.into(),
        };
        let fp = s.fingerprint().unwrap();
        let changes: Vec<fn(&mut Signoff)> = vec![
            |s| s.tenant_id = "t2".into(),
            |s| s.project_id = "p2".into(),
            |s| s.task_id = "task-2".into(),
            |s| s.workflow_id = "wf-2".into(),
            |s| s.from_state = "draft".into(),
            |s| s.to_state = "published".into(),
            |s| s.acceptance_record_id = "acc-2".into(),
            |s| s.criterion_id = "review".into(),
            |s| s.artifact_snapshot_id = "snap-2".into(),
            |s| s.artifact_digest = format!("sha256:{}", "d".repeat(64)),
            |s| s.acceptance_policy_id = "other".into(),
            |s| s.acceptance_policy_version = "4".into(),
            |s| s.acceptance_policy_bundle_sha = format!("sha256:{}", "e".repeat(64)),
            |s| s.state_revision = 8,
            |s| s.expires_at = s.expires_at.plus_seconds(1),
        ];
        for (i, change) in changes.into_iter().enumerate() {
            let mut other = s.clone();
            change(&mut other);
            assert_ne!(other.fingerprint().unwrap(), fp, "change {i}");
        }
    }

    #[test]
    fn every_code_has_its_i6_text_and_only_challenge_and_assertion_carry_diagnostics() {
        assert_eq!(
            ApprovalCode::AssertionInvalid(VerifierError::SignatureInvalid).diagnostic(),
            Some("assertion: signature invalid")
        );
        assert_eq!(
            ApprovalCode::ChallengeInvalid(ChallengeRefusal::Consumed).diagnostic(),
            Some("challenge: consumed")
        );
        assert_eq!(ApprovalCode::GrantRevoked.diagnostic(), None);
    }
}
