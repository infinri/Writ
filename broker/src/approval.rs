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
//! **Challenge** ([`Challenge`]): the server-side binding of one ceremony
//! (its opaque [`CeremonyState`] and challenge digest) to the subject, its
//! allowed credentials, operation (whose closed verdict is derived from it,
//! R5), tenant, project, target, complete context fingerprint
//! ([`context_fingerprint`], the D-006 digest of the target context as
//! displayed), policy bundle digest, relevant revision and the decision's
//! idempotency key; `expires_at = min(issued_at + 120 s, target expiry)`
//! ([`challenge_expiry`]); single use ([`Challenge::consume`]). Every
//! failure is `CHALLENGE_INVALID` with a [`ChallengeRefusal`] diagnostic.
//!
//! **Trusted values** (R3): [`Challenge`], [`ApprovalDecision`],
//! [`Signoff`] and [`Revocation`] have private fields. New ones are built
//! only from an [`AcceptedAssertion`] (itself only from
//! [`crate::human::VerifiedAssertion::check`]): [`decide`],
//! [`Signoff::sign`], [`Revocation::record`]. Each also has a `from_stored`
//! constructor for the ledger only, which revalidates the record's
//! invariants when a stored row is read back.
//!
//! **Displayed context** (R6): the approval and signoff pages render only a
//! [`DisplayedContext`], whose fingerprint is the D-006 digest of exactly
//! the value rendered; the approval context includes the complete stored
//! action body.
//!
//! **Codes** ([`ApprovalCode`]): the I-6 internal codes; every one maps to
//! the kernel's generic external refusal ([`crate::refusal::Refusal::Approval`]).

use std::fmt;

use writ_kernel::json::{self, CanonicalError, Digest, Value};
use writ_kernel::time::Timestamp;

use crate::grants::Verdict;
use crate::human::{
    approval_separation, AcceptedAssertion, ActiveCredentials, CeremonyState, EnrollmentRecord,
    HumanVerifier, Parties, SubjectId, VerifiedAssertion, VerifierError,
};

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
    /// The assertion's ceremony is not this challenge's (challenge digest).
    CeremonyMismatch,
    /// Another idempotency key.
    IdempotencyMismatch,
    /// An empty or inconsistent field.
    Malformed,
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
            CeremonyMismatch => "challenge: ceremony mismatch",
            IdempotencyMismatch => "challenge: idempotency key mismatch",
            Malformed => "challenge: malformed",
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

/// A stored record failed revalidation (`from_stored`, enrollment); the
/// text names the record and the broken invariant.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct InvalidRecord(pub &'static str);

/// What a page renders (R6): the context value and its fingerprint, the
/// D-006 digest of exactly that value. A page renders nothing else.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DisplayedContext {
    context: Value,
    fingerprint: Digest,
}

impl DisplayedContext {
    fn of(context: Value) -> Result<DisplayedContext, CanonicalError> {
        let fingerprint = context_fingerprint(&context)?;
        Ok(DisplayedContext {
            context,
            fingerprint,
        })
    }

    /// The value the page renders.
    pub fn context(&self) -> &Value {
        &self.context
    }

    /// Its fingerprint (the challenge binds this).
    pub fn fingerprint(&self) -> &Digest {
        &self.fingerprint
    }
}

fn nonempty(texts: &[&str]) -> bool {
    texts.iter().all(|t| !t.is_empty())
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
    /// The complete stored action body (a JSON object), displayed and
    /// fingerprinted (R6).
    pub action: Value,
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
    /// An empty identifier or subject (R7), an action body that is not a
    /// canonical JSON object, or expiry not after creation.
    Malformed,
}

/// An immutable stored approval request (I-3).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ApprovalRequest {
    fields: NewApprovalRequest,
    displayed: DisplayedContext,
}

impl ApprovalRequest {
    /// Validate and freeze. Capabilities are sorted and deduplicated. The
    /// requester, the agent and every delegation-chain member must be
    /// well-formed subjects (R7).
    pub fn new(mut fields: NewApprovalRequest) -> Result<ApprovalRequest, RequestInvalid> {
        if fields.required_approvers != 1 {
            return Err(RequestInvalid::QuorumUnsupported);
        }
        let texts = [
            fields.request_id.as_str(),
            &fields.tenant_id,
            &fields.project_id,
            &fields.action_kind,
            &fields.canonical_resource_id,
            &fields.action_digest,
            &fields.policy_bundle_sha,
            &fields.request_fingerprint,
        ];
        let subjects_ok = fields.requester.is_well_formed()
            && fields.agent.as_ref().is_none_or(SubjectId::is_well_formed)
            && fields
                .delegation_chain
                .iter()
                .all(SubjectId::is_well_formed);
        if !nonempty(&texts)
            || !subjects_ok
            || !matches!(fields.action, Value::Obj(_))
            || fields.capabilities.iter().any(String::is_empty)
            || fields.expires_at <= fields.created_at
        {
            return Err(RequestInvalid::Malformed);
        }
        fields.capabilities.sort();
        fields.capabilities.dedup();
        let displayed = DisplayedContext::of(Self::context_of(&fields))
            .map_err(|_| RequestInvalid::Malformed)?;
        Ok(ApprovalRequest { fields, displayed })
    }

    /// The stored fields (read-only).
    pub fn fields(&self) -> &NewApprovalRequest {
        &self.fields
    }

    /// Request id.
    pub fn request_id(&self) -> &str {
        &self.fields.request_id
    }

    /// Expiry.
    pub fn expires_at(&self) -> Timestamp {
        self.fields.expires_at
    }

    /// The parties excluded from approving it (O-7).
    pub fn parties(&self) -> Parties<'_> {
        Parties {
            requester: &self.fields.requester,
            agent: self.fields.agent.as_ref(),
            delegation_chain: &self.fields.delegation_chain,
        }
    }

    fn context_of(r: &NewApprovalRequest) -> Value {
        json::obj([
            ("action", r.action.clone()),
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

    /// The complete context as displayed: every stored field, including the
    /// complete action body. The approval page renders only this (R6).
    pub fn displayed(&self) -> &DisplayedContext {
        &self.displayed
    }

    /// The displayed context value.
    pub fn context(&self) -> &Value {
        self.displayed.context()
    }

    /// [`context_fingerprint`] of [`ApprovalRequest::context`].
    pub fn fingerprint(&self) -> &Digest {
        self.displayed.fingerprint()
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

fn next_status(
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

/// The only status changes a decision may make: `pending` and unexpired at
/// the acceptance time to `approved` or `denied`, the verdict derived from
/// the accepted challenge's operation (R5; another operation is
/// `CHALLENGE_INVALID`). A closed request is `APPROVAL_REQUEST_CLOSED`; an
/// expired one (stored or due) is `APPROVAL_REQUEST_EXPIRED`. Nothing ever
/// reopens.
pub fn transition(
    from: ApprovalStatus,
    accepted: &AcceptedAssertion,
    expires_at: Timestamp,
) -> Result<ApprovalStatus, ApprovalCode> {
    let verdict = accepted
        .operation()
        .approval_verdict()
        .ok_or(ApprovalCode::ChallengeInvalid(
            ChallengeRefusal::OperationMismatch,
        ))?;
    next_status(from, verdict, accepted.accepted_at(), expires_at)
}

/// `Some(Expired)` exactly when a `pending` request is due at `now`.
pub fn expire(
    from: ApprovalStatus,
    now: Timestamp,
    expires_at: Timestamp,
) -> Option<ApprovalStatus> {
    (from == ApprovalStatus::Pending && now >= expires_at).then_some(ApprovalStatus::Expired)
}

/// The fields of one stored human decision (several rows may exist; one
/// suffices, no quorum implied).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DecisionFields {
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
    pub provenance_version: String,
}

/// One human decision on an approval request. New ones come only from
/// [`decide`].
///
/// ```compile_fail
/// use writ_broker::approval::{ApprovalDecision, DecisionFields};
/// fn forge(f: DecisionFields) -> ApprovalDecision {
///     ApprovalDecision { fields: f }
/// }
/// ```
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ApprovalDecision {
    fields: DecisionFields,
}

impl ApprovalDecision {
    /// Reconstruct a stored row (the ledger only): revalidates non-empty
    /// identifiers and the provenance version.
    pub fn from_stored(fields: DecisionFields) -> Result<ApprovalDecision, InvalidRecord> {
        if !nonempty(&[
            &fields.request_id,
            &fields.credential_id,
            &fields.channel,
            &fields.challenge_id,
        ]) || !fields.subject.is_well_formed()
        {
            return Err(InvalidRecord("decision: empty field"));
        }
        if fields.provenance_version != PROVENANCE_VERSION_2 {
            return Err(InvalidRecord("decision: provenance version"));
        }
        Ok(ApprovalDecision { fields })
    }

    /// The fields (read-only).
    pub fn fields(&self) -> &DecisionFields {
        &self.fields
    }
}

/// Decide `request` (stored status `from`) from an accepted assertion: the
/// challenge must target this request in its scope with its current
/// fingerprint; separation (O-7, R1) with the approver's `enrollment`; the
/// transition with the verdict derived from the challenge (R5). Permission
/// and revocation lookups are the caller's (ledger) checks.
pub fn decide(
    request: &ApprovalRequest,
    from: ApprovalStatus,
    accepted: &AcceptedAssertion,
    enrollment: Option<&EnrollmentRecord>,
    channel: &str,
) -> Result<(ApprovalStatus, ApprovalDecision), ApprovalCode> {
    let b = accepted.binding();
    let r = request.fields();
    let refuse = |c| Err(ApprovalCode::ChallengeInvalid(c));
    if b.target_id != r.request_id {
        return refuse(ChallengeRefusal::TargetMismatch);
    }
    if b.tenant_id != r.tenant_id {
        return refuse(ChallengeRefusal::TenantMismatch);
    }
    if b.project_id != r.project_id {
        return refuse(ChallengeRefusal::ProjectMismatch);
    }
    if &b.context_fingerprint != request.fingerprint() {
        return refuse(ChallengeRefusal::ContextMismatch);
    }
    approval_separation(accepted.subject(), enrollment, &request.parties())?;
    let status = transition(from, accepted, request.expires_at())?;
    let verdict = match status {
        ApprovalStatus::Approved => Verdict::Approved,
        _ => Verdict::Denied,
    };
    let decision = ApprovalDecision {
        fields: DecisionFields {
            request_id: r.request_id.clone(),
            verdict,
            subject: accepted.subject().clone(),
            credential_id: accepted.credential_id().to_string(),
            channel: channel.to_string(),
            decided_at: accepted.accepted_at(),
            challenge_id: accepted.challenge_id().to_string(),
            provenance_version: PROVENANCE_VERSION_2.to_string(),
        },
    };
    Ok((status, decision))
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

    /// Parse the stored text.
    pub fn parse(text: &str) -> Option<ChallengeOperation> {
        use ChallengeOperation::*;
        [Approve, Deny, Revoke, Signoff, Enroll, RoleChange]
            .into_iter()
            .find(|o| o.as_str() == text)
    }

    /// The closed verdict text of the operation (R5; clarification 4).
    pub fn verdict(self) -> &'static str {
        match self {
            ChallengeOperation::Approve => "approved",
            ChallengeOperation::Deny => "denied",
            ChallengeOperation::Revoke => "revoked",
            ChallengeOperation::Signoff => "signed_off",
            ChallengeOperation::Enroll => "enrolled",
            ChallengeOperation::RoleChange => "role_changed",
        }
    }

    /// The approval verdict, for [`ChallengeOperation::Approve`] and
    /// [`ChallengeOperation::Deny`] only.
    pub fn approval_verdict(self) -> Option<Verdict> {
        match self {
            ChallengeOperation::Approve => Some(Verdict::Approved),
            ChallengeOperation::Deny => Some(Verdict::Denied),
            _ => None,
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

/// What a challenge is bound to, and what a decision presents (recomputed
/// from current stored state). The verdict is derived from the operation
/// ([`ChallengeBinding::verdict`]), never a separate value (R5).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ChallengeBinding {
    /// Operation.
    pub operation: ChallengeOperation,
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

impl ChallengeBinding {
    /// The closed verdict text of the operation.
    pub fn verdict(&self) -> &'static str {
        self.operation.verdict()
    }

    fn well_formed(&self) -> bool {
        nonempty(&[
            &self.tenant_id,
            &self.project_id,
            &self.target_id,
            &self.policy_bundle_sha,
        ])
    }
}

/// `min(issued_at + 120 s, target_expiry)`.
pub fn challenge_expiry(issued_at: Timestamp, target_expiry: Timestamp) -> Timestamp {
    issued_at
        .plus_seconds(CHALLENGE_TTL_SECONDS)
        .min(target_expiry)
}

/// A server-side challenge row (I-2, R2). Private fields: built by
/// [`Challenge::issue`] or, for the ledger only, [`Challenge::from_stored`].
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Challenge {
    challenge_id: String,
    challenge_digest: Digest,
    ceremony_state: CeremonyState,
    subject: SubjectId,
    allowed_credentials: Vec<String>,
    binding: ChallengeBinding,
    issued_at: Timestamp,
    expires_at: Timestamp,
    idempotency_key: String,
    status: ChallengeStatus,
}

/// The fields of a challenge to issue.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NewChallenge {
    /// Challenge id.
    pub challenge_id: String,
    /// Digest of the server challenge (from the verifier's `begin`).
    pub challenge_digest: Digest,
    /// The ceremony state from the same `begin`.
    pub ceremony_state: CeremonyState,
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
    /// Idempotency key of the decision.
    pub idempotency_key: String,
}

/// A stored challenge row, as read back by the ledger.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StoredChallenge {
    /// Challenge id.
    pub challenge_id: String,
    /// Digest of the server challenge.
    pub challenge_digest: Digest,
    /// Ceremony state.
    pub ceremony_state: CeremonyState,
    /// Subject.
    pub subject: SubjectId,
    /// Allowed credential ids.
    pub allowed_credentials: Vec<String>,
    /// The binding.
    pub binding: ChallengeBinding,
    /// The stored verdict text (must equal the operation's).
    pub verdict: String,
    /// Issue time.
    pub issued_at: Timestamp,
    /// Expiry.
    pub expires_at: Timestamp,
    /// Idempotency key.
    pub idempotency_key: String,
    /// Status.
    pub status: ChallengeStatus,
}

fn challenge_well_formed(
    challenge_id: &str,
    ceremony_state: &CeremonyState,
    subject: &SubjectId,
    allowed: &[String],
    binding: &ChallengeBinding,
    idempotency_key: &str,
) -> bool {
    nonempty(&[challenge_id, idempotency_key])
        && !ceremony_state.is_empty()
        && subject.is_well_formed()
        && !allowed.is_empty()
        && allowed.iter().all(|c| !c.is_empty())
        && binding.well_formed()
}

impl Challenge {
    /// Issue a challenge: `Malformed` when an identifier, the ceremony state
    /// or the allowed credentials are empty; `Expired` when the target
    /// already expired at `issued_at`, so the lifetime is never zero or
    /// negative.
    pub fn issue(new: NewChallenge) -> Result<Challenge, ChallengeRefusal> {
        if !challenge_well_formed(
            &new.challenge_id,
            &new.ceremony_state,
            &new.subject,
            &new.allowed_credentials,
            &new.binding,
            &new.idempotency_key,
        ) {
            return Err(ChallengeRefusal::Malformed);
        }
        let expires_at = challenge_expiry(new.issued_at, new.target_expiry);
        if expires_at <= new.issued_at {
            return Err(ChallengeRefusal::Expired);
        }
        Ok(Challenge {
            challenge_id: new.challenge_id,
            challenge_digest: new.challenge_digest,
            ceremony_state: new.ceremony_state,
            subject: new.subject,
            allowed_credentials: new.allowed_credentials,
            binding: new.binding,
            issued_at: new.issued_at,
            expires_at,
            idempotency_key: new.idempotency_key,
            status: ChallengeStatus::Issued,
        })
    }

    /// Reconstruct a stored row (the ledger only), revalidating: well-formed
    /// fields (`Malformed`), the stored verdict text equal to the
    /// operation's (`VerdictMismatch`), and `issued_at < expires_at <=
    /// issued_at + 120 s` (`Malformed`).
    pub fn from_stored(row: StoredChallenge) -> Result<Challenge, ChallengeRefusal> {
        if !challenge_well_formed(
            &row.challenge_id,
            &row.ceremony_state,
            &row.subject,
            &row.allowed_credentials,
            &row.binding,
            &row.idempotency_key,
        ) || row.expires_at <= row.issued_at
            || row.expires_at > row.issued_at.plus_seconds(CHALLENGE_TTL_SECONDS)
        {
            return Err(ChallengeRefusal::Malformed);
        }
        if row.verdict != row.binding.verdict() {
            return Err(ChallengeRefusal::VerdictMismatch);
        }
        Ok(Challenge {
            challenge_id: row.challenge_id,
            challenge_digest: row.challenge_digest,
            ceremony_state: row.ceremony_state,
            subject: row.subject,
            allowed_credentials: row.allowed_credentials,
            binding: row.binding,
            issued_at: row.issued_at,
            expires_at: row.expires_at,
            idempotency_key: row.idempotency_key,
            status: row.status,
        })
    }

    /// Challenge id.
    pub fn challenge_id(&self) -> &str {
        &self.challenge_id
    }

    /// Digest of the server challenge.
    pub fn challenge_digest(&self) -> &Digest {
        &self.challenge_digest
    }

    /// The ceremony state (for the verifier and the ledger only).
    pub fn ceremony_state(&self) -> &CeremonyState {
        &self.ceremony_state
    }

    /// Subject the ceremony is for.
    pub fn subject(&self) -> &SubjectId {
        &self.subject
    }

    /// Credential ids allowed to answer.
    pub fn allowed_credentials(&self) -> &[String] {
        &self.allowed_credentials
    }

    /// The binding.
    pub fn binding(&self) -> &ChallengeBinding {
        &self.binding
    }

    /// The closed verdict text (derived from the operation).
    pub fn verdict(&self) -> &'static str {
        self.binding.verdict()
    }

    /// Issue time.
    pub fn issued_at(&self) -> Timestamp {
        self.issued_at
    }

    /// Expiry ([`challenge_expiry`]).
    pub fn expires_at(&self) -> Timestamp {
        self.expires_at
    }

    /// Idempotency key of the decision.
    pub fn idempotency_key(&self) -> &str {
        &self.idempotency_key
    }

    /// Status.
    pub fn status(&self) -> ChallengeStatus {
        self.status
    }

    /// Finish this challenge's ceremony with `verifier`: always with this
    /// challenge's own ceremony state, and only over credentials of its
    /// subject. The answer is still untrusted until
    /// [`VerifiedAssertion::check`].
    pub fn finish<V: HumanVerifier + ?Sized>(
        &self,
        verifier: &V,
        response: &[u8],
        credentials: &ActiveCredentials,
    ) -> Result<VerifiedAssertion, ApprovalCode> {
        if credentials.subject() != &self.subject {
            return Err(ApprovalCode::ChallengeInvalid(
                ChallengeRefusal::SubjectMismatch,
            ));
        }
        verifier
            .finish(&self.ceremony_state, response, credentials)
            .map_err(ApprovalCode::AssertionInvalid)
    }

    /// The pure validity check at trusted time `now` (called only by
    /// [`VerifiedAssertion::check`]): status, expiry, the ceremony's
    /// challenge digest, the idempotency key, subject, credential, then
    /// every bound field in order.
    pub(crate) fn check(
        &self,
        presented: &ChallengeBinding,
        challenge_digest: &Digest,
        idempotency_key: &str,
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
        let b = &self.binding;
        let checks = [
            (&self.challenge_digest == challenge_digest, CeremonyMismatch),
            (self.idempotency_key == idempotency_key, IdempotencyMismatch),
            (&self.subject == subject, SubjectMismatch),
            (
                self.allowed_credentials.iter().any(|c| c == credential_id),
                CredentialNotAllowed,
            ),
            (b.operation == presented.operation, OperationMismatch),
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

impl RevocationTarget {
    /// The challenge target id: a kind prefix and the id; for a subject the
    /// issuer is length-prefixed so the text is unambiguous.
    pub fn target_id(&self) -> String {
        match self {
            RevocationTarget::Grant(id) => format!("grant:{id}"),
            RevocationTarget::Signoff(id) => format!("signoff:{id}"),
            RevocationTarget::Credential(id) => format!("credential:{id}"),
            RevocationTarget::Role(id) => format!("role:{id}"),
            RevocationTarget::Subject(s) => {
                format!("subject:{}:{}:{}", s.issuer.len(), s.issuer, s.subject)
            }
        }
    }

    fn well_formed(&self) -> bool {
        match self {
            RevocationTarget::Subject(s) => s.is_well_formed(),
            RevocationTarget::Grant(id)
            | RevocationTarget::Signoff(id)
            | RevocationTarget::Credential(id)
            | RevocationTarget::Role(id) => !id.is_empty(),
        }
    }
}

/// The fields of a committed revocation (I-4).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RevocationFields {
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

/// A committed revocation (I-4). Never refunds, replenishes or substitutes.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Revocation {
    fields: RevocationFields,
}

impl Revocation {
    /// Record a revocation of `target` from an accepted `Revoke` challenge
    /// that targets it (`CHALLENGE_INVALID` otherwise). Permission checks
    /// are the caller's.
    pub fn record(
        target: RevocationTarget,
        reason: &str,
        accepted: &AcceptedAssertion,
    ) -> Result<Revocation, ApprovalCode> {
        if accepted.operation() != ChallengeOperation::Revoke {
            return Err(ApprovalCode::ChallengeInvalid(
                ChallengeRefusal::OperationMismatch,
            ));
        }
        if !target.well_formed() || accepted.binding().target_id != target.target_id() {
            return Err(ApprovalCode::ChallengeInvalid(
                ChallengeRefusal::TargetMismatch,
            ));
        }
        Ok(Revocation {
            fields: RevocationFields {
                target,
                by: accepted.subject().clone(),
                at: accepted.accepted_at(),
                reason: reason.to_string(),
                challenge_id: accepted.challenge_id().to_string(),
            },
        })
    }

    /// Reconstruct a stored row (the ledger only), revalidating identifiers.
    pub fn from_stored(fields: RevocationFields) -> Result<Revocation, InvalidRecord> {
        if !fields.target.well_formed()
            || !fields.by.is_well_formed()
            || fields.challenge_id.is_empty()
        {
            return Err(InvalidRecord("revocation: empty field"));
        }
        Ok(Revocation { fields })
    }

    /// The fields (read-only).
    pub fn fields(&self) -> &RevocationFields {
        &self.fields
    }
}

/// The binding of a signoff to sign (I-5): everything the signoff page
/// displays.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NewSignoff {
    /// Signoff id (the challenge target).
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
    /// Expiry.
    pub expires_at: Timestamp,
}

impl NewSignoff {
    fn well_formed(&self) -> bool {
        nonempty(&[
            &self.signoff_id,
            &self.tenant_id,
            &self.project_id,
            &self.task_id,
            &self.workflow_id,
            &self.from_state,
            &self.to_state,
            &self.acceptance_record_id,
            &self.criterion_id,
            &self.artifact_snapshot_id,
            &self.artifact_digest,
            &self.acceptance_policy_id,
            &self.acceptance_policy_version,
            &self.acceptance_policy_bundle_sha,
        ])
    }

    /// The signed context as displayed: every binding member, including the
    /// signoff id (the challenge target).
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
            ("signoff_id", json::s(&self.signoff_id)),
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

    /// What the signoff page renders (R6).
    pub fn displayed(&self) -> Result<DisplayedContext, CanonicalError> {
        DisplayedContext::of(self.context())
    }
}

/// The fields of a stored signoff.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SignoffFields {
    /// The signed binding.
    pub binding: NewSignoff,
    /// Issue time.
    pub issued_at: Timestamp,
    /// The signing subject.
    pub subject: SubjectId,
    /// The credential that signed.
    pub credential_id: String,
    /// The consumed challenge.
    pub challenge_id: String,
    /// [`PROVENANCE_VERSION_2`].
    pub provenance_version: String,
}

/// A human signoff of one criterion (I-5), stored in the ledger and consumed
/// single-use ledger-wide with the promotion.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Signoff {
    fields: SignoffFields,
}

impl Signoff {
    /// Sign `binding` from an accepted `Signoff` challenge that targets its
    /// signoff id in its scope with its displayed fingerprint
    /// (`CHALLENGE_INVALID` otherwise). Separation, permission and the other
    /// I-5 checks run at use time.
    pub fn sign(
        binding: NewSignoff,
        accepted: &AcceptedAssertion,
    ) -> Result<Signoff, ApprovalCode> {
        let b = accepted.binding();
        let refuse = |c| Err(ApprovalCode::ChallengeInvalid(c));
        if accepted.operation() != ChallengeOperation::Signoff {
            return refuse(ChallengeRefusal::OperationMismatch);
        }
        if !binding.well_formed() {
            return refuse(ChallengeRefusal::Malformed);
        }
        if b.target_id != binding.signoff_id {
            return refuse(ChallengeRefusal::TargetMismatch);
        }
        if b.tenant_id != binding.tenant_id {
            return refuse(ChallengeRefusal::TenantMismatch);
        }
        if b.project_id != binding.project_id {
            return refuse(ChallengeRefusal::ProjectMismatch);
        }
        match binding.displayed() {
            Ok(d) if d.fingerprint() == &b.context_fingerprint => {}
            _ => return refuse(ChallengeRefusal::ContextMismatch),
        }
        if binding.expires_at <= accepted.accepted_at() {
            return refuse(ChallengeRefusal::Expired);
        }
        Ok(Signoff {
            fields: SignoffFields {
                binding,
                issued_at: accepted.accepted_at(),
                subject: accepted.subject().clone(),
                credential_id: accepted.credential_id().to_string(),
                challenge_id: accepted.challenge_id().to_string(),
                provenance_version: PROVENANCE_VERSION_2.to_string(),
            },
        })
    }

    /// Reconstruct a stored row (the ledger only), revalidating identifiers,
    /// `issued_at < expires_at` and the provenance version.
    pub fn from_stored(fields: SignoffFields) -> Result<Signoff, InvalidRecord> {
        if !fields.binding.well_formed()
            || !fields.subject.is_well_formed()
            || !nonempty(&[&fields.credential_id, &fields.challenge_id])
        {
            return Err(InvalidRecord("signoff: empty field"));
        }
        if fields.binding.expires_at <= fields.issued_at {
            return Err(InvalidRecord("signoff: expiry not after issue"));
        }
        if fields.provenance_version != PROVENANCE_VERSION_2 {
            return Err(InvalidRecord("signoff: provenance version"));
        }
        Ok(Signoff { fields })
    }

    /// The fields (read-only).
    pub fn fields(&self) -> &SignoffFields {
        &self.fields
    }

    /// The signed context as displayed ([`NewSignoff::context`]).
    pub fn context(&self) -> Value {
        self.fields.binding.context()
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
    use crate::human::{
        CeremonyStart, CredentialStatus, CredentialUpdate, PrincipalKind, StoredCredential,
        SubjectRecord, SubjectStatus, WEBAUTHN_PASSKEY,
    };

    fn t(s: &str) -> Timestamp {
        Timestamp::parse_strict(s).unwrap()
    }

    fn sha(c: char) -> String {
        format!("sha256:{}", c.to_string().repeat(64))
    }

    fn bob() -> SubjectId {
        SubjectId::new("writ-local", "bob")
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
            action: json::obj([
                ("content", json::s("fn main() {}")),
                ("path", json::s("src/a.rs")),
            ]),
            capabilities: vec!["write".into(), "read".into(), "write".into()],
            policy_bundle_sha: sha('c'),
            request_fingerprint: sha('f'),
            created_at: t("2026-11-07T10:00:00Z"),
            expires_at: t("2026-11-07T10:10:00Z"),
            required_approvers: 1,
        }
    }

    fn request() -> ApprovalRequest {
        ApprovalRequest::new(new_request()).unwrap()
    }

    fn binding_for(operation: ChallengeOperation) -> ChallengeBinding {
        ChallengeBinding {
            operation,
            tenant_id: "t1".into(),
            project_id: "p1".into(),
            target_id: "apr-1".into(),
            context_fingerprint: request().fingerprint().clone(),
            policy_bundle_sha: sha('c'),
            revision: 7,
        }
    }

    fn binding() -> ChallengeBinding {
        binding_for(ChallengeOperation::Approve)
    }

    fn new_challenge(id: &str, binding: ChallengeBinding, state: &[u8]) -> NewChallenge {
        NewChallenge {
            challenge_id: id.into(),
            challenge_digest: Digest::of_bytes(state),
            ceremony_state: CeremonyState::from_bytes(state.to_vec()),
            subject: bob(),
            allowed_credentials: vec!["cred-1".into()],
            binding,
            issued_at: t("2026-11-07T10:00:00Z"),
            target_expiry: t("2026-11-07T10:10:00Z"),
            idempotency_key: "idem-1".into(),
        }
    }

    fn challenge_for(binding: ChallengeBinding) -> Challenge {
        Challenge::issue(new_challenge("ch-1", binding, b"state-a")).unwrap()
    }

    fn challenge(issued: &str, target_expiry: &str) -> Challenge {
        let mut n = new_challenge("ch-1", binding(), b"state-a");
        n.issued_at = t(issued);
        n.target_expiry = t(target_expiry);
        Challenge::issue(n).unwrap()
    }

    fn assertion_for(c: &Challenge) -> VerifiedAssertion {
        VerifiedAssertion::new(
            bob(),
            "cred-1",
            true,
            WEBAUTHN_PASSKEY,
            c.challenge_digest().clone(),
            Some(CredentialUpdate {
                credential_id: "cred-1".into(),
                public_material: vec![2],
            }),
        )
    }

    fn now() -> Timestamp {
        t("2026-11-07T10:01:00Z")
    }

    fn accept(binding: ChallengeBinding) -> AcceptedAssertion {
        let c = challenge_for(binding.clone());
        assertion_for(&c)
            .check(&c, &binding, "idem-1", now())
            .unwrap()
    }

    fn enrollment() -> EnrollmentRecord {
        EnrollmentRecord::new(
            bob(),
            vec![SubjectId::new("adapter", "u-bob")],
            t("2026-11-01T00:00:00Z"),
            SubjectId::new("writ-local", "admin"),
        )
        .unwrap()
    }

    fn active_credentials() -> ActiveCredentials {
        let record = SubjectRecord {
            subject: bob(),
            kind: PrincipalKind::Human,
            status: SubjectStatus::Active,
        };
        let stored = ["cred-1", "cred-revoked"].map(|id| StoredCredential {
            credential_id: id.into(),
            subject: bob(),
            public_material: vec![1],
            status: if id == "cred-1" {
                CredentialStatus::Active
            } else {
                CredentialStatus::Revoked
            },
            enrolled_at: t("2026-11-01T00:00:00Z"),
            enrolled_by: SubjectId::new("writ-local", "admin"),
        });
        ActiveCredentials::select(&record, stored, now()).unwrap()
    }

    /// A deterministic test verifier: the challenge digest is the digest of
    /// the ceremony state it was given; it panics if handed a revoked credential.
    struct TestVerifier;

    impl HumanVerifier for TestVerifier {
        fn begin(
            &self,
            subject: &SubjectId,
            credentials: &ActiveCredentials,
        ) -> Result<CeremonyStart, VerifierError> {
            assert_eq!(credentials.subject(), subject);
            let state = format!("state-{}", subject.subject).into_bytes();
            Ok(CeremonyStart {
                client_options: vec![],
                challenge_digest: Digest::of_bytes(&state),
                state: CeremonyState::from_bytes(state),
            })
        }

        fn finish(
            &self,
            state: &CeremonyState,
            _response: &[u8],
            credentials: &ActiveCredentials,
        ) -> Result<VerifiedAssertion, VerifierError> {
            assert!(credentials
                .as_slice()
                .iter()
                .all(|c| c.status == CredentialStatus::Active));
            let cred = credentials
                .as_slice()
                .first()
                .ok_or(VerifierError::CredentialUnknown)?;
            Ok(VerifiedAssertion::new(
                credentials.subject().clone(),
                &cred.credential_id,
                true,
                WEBAUTHN_PASSKEY,
                Digest::of_bytes(state.as_bytes()),
                None,
            ))
        }
    }

    #[test]
    fn approval_request_is_validated_and_frozen() {
        let r = request();
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
        let malformed: Vec<fn(&mut NewApprovalRequest)> = vec![
            |n| n.expires_at = n.created_at,
            |n| n.action_digest.clear(),
            |n| n.action = json::s("not an object"),
            |n| n.capabilities.push(String::new()),
            // R7: non-empty requester, agent and every chain member.
            |n| n.requester.subject.clear(),
            |n| n.requester.issuer.clear(),
            |n| n.agent = Some(SubjectId::new("agent-realm", "")),
            |n| n.delegation_chain.push(SubjectId::new("", "parent-2")),
        ];
        for (i, change) in malformed.into_iter().enumerate() {
            let mut n = new_request();
            change(&mut n);
            assert_eq!(
                ApprovalRequest::new(n),
                Err(RequestInvalid::Malformed),
                "case {i}"
            );
        }
    }

    #[test]
    fn transitions_from_pending_only_and_terminal_states_never_reopen() {
        use ApprovalStatus::*;
        let exp = t("2026-11-07T10:10:00Z");
        let before = t("2026-11-07T10:09:59Z");
        assert_eq!(
            next_status(Pending, Verdict::Approved, before, exp),
            Ok(Approved)
        );
        assert_eq!(
            next_status(Pending, Verdict::Denied, before, exp),
            Ok(Denied)
        );
        for v in [Verdict::Approved, Verdict::Denied] {
            // Expiry is decided at trusted time, inclusive.
            assert_eq!(
                next_status(Pending, v, exp, exp),
                Err(ApprovalCode::RequestExpired)
            );
            for now in [before, exp, t("2030-01-01T00:00:00Z")] {
                assert_eq!(
                    next_status(Denied, v, now, exp),
                    Err(ApprovalCode::RequestClosed)
                );
                assert_eq!(
                    next_status(Approved, v, now, exp),
                    Err(ApprovalCode::RequestClosed)
                );
                assert_eq!(
                    next_status(Expired, v, now, exp),
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
    fn the_verdict_is_derived_from_the_checked_challenge_and_cannot_be_swapped() {
        let r = request();
        // A deny challenge only ever denies.
        let deny = accept(binding_for(ChallengeOperation::Deny));
        let (status, decision) = decide(
            &r,
            ApprovalStatus::Pending,
            &deny,
            Some(&enrollment()),
            "local",
        )
        .unwrap();
        assert_eq!(status, ApprovalStatus::Denied);
        assert_eq!(decision.fields().verdict, Verdict::Denied);
        let approve = accept(binding());
        assert_eq!(
            transition(ApprovalStatus::Pending, &approve, r.expires_at()),
            Ok(ApprovalStatus::Approved)
        );
        // Presenting an approve binding against a deny challenge is refused.
        let c = challenge_for(binding_for(ChallengeOperation::Deny));
        assert_eq!(
            assertion_for(&c).check(&c, &binding(), "idem-1", now()),
            Err(ApprovalCode::ChallengeInvalid(
                ChallengeRefusal::OperationMismatch
            ))
        );
        // A challenge for another operation never changes an approval status.
        let signoff = accept(binding_for(ChallengeOperation::Signoff));
        assert_eq!(
            transition(ApprovalStatus::Pending, &signoff, r.expires_at()),
            Err(ApprovalCode::ChallengeInvalid(
                ChallengeRefusal::OperationMismatch
            ))
        );
        // A stored row whose verdict text disagrees with its operation is refused.
        let mut row = stored(&challenge_for(binding_for(ChallengeOperation::Deny)));
        row.verdict = "approved".into();
        assert_eq!(
            Challenge::from_stored(row),
            Err(ChallengeRefusal::VerdictMismatch)
        );
        for op in [
            ChallengeOperation::Approve,
            ChallengeOperation::Deny,
            ChallengeOperation::Revoke,
            ChallengeOperation::Signoff,
            ChallengeOperation::Enroll,
            ChallengeOperation::RoleChange,
        ] {
            assert_eq!(ChallengeOperation::parse(op.as_str()), Some(op));
        }
    }

    #[test]
    fn decide_requires_the_approver_enrollment_and_the_target_binding() {
        let r = request();
        let accepted = accept(binding());
        assert_eq!(
            decide(&r, ApprovalStatus::Pending, &accepted, None, "local"),
            Err(ApprovalCode::SeparationViolated)
        );
        let linked_to_requester = EnrollmentRecord::new(
            bob(),
            vec![SubjectId::new("adapter", "alice")],
            t("2026-11-01T00:00:00Z"),
            SubjectId::new("writ-local", "admin"),
        )
        .unwrap();
        assert_eq!(
            decide(
                &r,
                ApprovalStatus::Pending,
                &accepted,
                Some(&linked_to_requester),
                "local"
            ),
            Err(ApprovalCode::SeparationViolated)
        );
        let (status, decision) = decide(
            &r,
            ApprovalStatus::Pending,
            &accepted,
            Some(&enrollment()),
            "local",
        )
        .unwrap();
        assert_eq!(status, ApprovalStatus::Approved);
        let f = decision.fields();
        assert_eq!(
            (
                f.request_id.as_str(),
                f.verdict,
                &f.subject,
                f.credential_id.as_str(),
                f.challenge_id.as_str(),
                f.decided_at,
                f.provenance_version.as_str()
            ),
            (
                "apr-1",
                Verdict::Approved,
                &bob(),
                "cred-1",
                "ch-1",
                now(),
                PROVENANCE_VERSION_2
            )
        );
        assert_eq!(
            decide(
                &r,
                ApprovalStatus::Approved,
                &accepted,
                Some(&enrollment()),
                "local"
            ),
            Err(ApprovalCode::RequestClosed)
        );
        // A challenge for another request, or for a changed context, decides nothing.
        let mut other = binding();
        other.target_id = "apr-2".into();
        assert_eq!(
            decide(
                &r,
                ApprovalStatus::Pending,
                &accept(other),
                Some(&enrollment()),
                "local"
            ),
            Err(ApprovalCode::ChallengeInvalid(
                ChallengeRefusal::TargetMismatch
            ))
        );
        let mut stale = binding();
        stale.context_fingerprint = Digest::of_bytes(b"old context");
        assert_eq!(
            decide(
                &r,
                ApprovalStatus::Pending,
                &accept(stale),
                Some(&enrollment()),
                "local"
            ),
            Err(ApprovalCode::ChallengeInvalid(
                ChallengeRefusal::ContextMismatch
            ))
        );
        // Stored decision rows are revalidated.
        assert_eq!(
            ApprovalDecision::from_stored(f.clone()),
            Ok(decision.clone())
        );
        let mut bad = f.clone();
        bad.provenance_version = "writ-provenance-1".into();
        assert!(ApprovalDecision::from_stored(bad).is_err());
        let mut bad = f.clone();
        bad.challenge_id.clear();
        assert!(ApprovalDecision::from_stored(bad).is_err());
    }

    #[test]
    fn every_displayed_field_is_fingerprinted_including_the_action_body() {
        let base = request();
        let shown = base.displayed();
        // The page renders exactly the fingerprinted value.
        assert_eq!(shown.fingerprint(), &json::digest(shown.context()).unwrap());
        assert_eq!(
            shown.fingerprint().as_str(),
            Digest::of_bytes(json::canonical(shown.context()).unwrap().as_bytes()).as_str()
        );
        // Every stored field is displayed (adding a field must extend this list).
        let Value::Obj(members) = shown.context() else {
            panic!("context is an object")
        };
        let keys: Vec<&str> = members.keys().map(String::as_str).collect();
        assert_eq!(
            keys,
            [
                "action",
                "action_digest",
                "action_kind",
                "agent",
                "canonical_resource_id",
                "capabilities",
                "context",
                "created_at",
                "delegation_chain",
                "expires_at",
                "policy_bundle_sha",
                "project_id",
                "request_fingerprint",
                "request_id",
                "requester",
                "required_approvers",
                "tenant_id",
            ]
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
            |n| {
                n.action = json::obj([
                    ("content", json::s("rm -rf /")),
                    ("path", json::s("src/a.rs")),
                ])
            },
            |n| n.capabilities.push("exec".into()),
            |n| n.policy_bundle_sha = format!("sha256:{}", "d".repeat(64)),
            |n| n.request_fingerprint = format!("sha256:{}", "e".repeat(64)),
            |n| n.created_at = n.created_at.plus_seconds(1),
            |n| n.expires_at = n.expires_at.plus_seconds(1),
        ];
        for (i, change) in changes.into_iter().enumerate() {
            let mut n = new_request();
            change(&mut n);
            let other = ApprovalRequest::new(n).unwrap();
            assert_ne!(other.fingerprint(), base.fingerprint(), "change {i}");
        }
    }

    #[test]
    fn challenge_expiry_is_bound_by_120_seconds_and_by_the_target() {
        let c = challenge("2026-11-07T10:00:00Z", "2026-11-07T10:10:00Z");
        assert_eq!(c.expires_at(), t("2026-11-07T10:02:00Z"));
        let c = challenge("2026-11-07T10:09:30Z", "2026-11-07T10:10:00Z");
        assert_eq!(c.expires_at(), t("2026-11-07T10:10:00Z"));
        assert!(assertion_for(&c)
            .check(&c, &binding(), "idem-1", t("2026-11-07T10:09:59Z"))
            .is_ok());
        assert_eq!(
            assertion_for(&c).check(&c, &binding(), "idem-1", t("2026-11-07T10:10:00Z")),
            Err(ApprovalCode::ChallengeInvalid(ChallengeRefusal::Expired))
        );
        // A target already expired cannot be challenged.
        for issued in ["2026-11-07T10:10:00Z", "2026-11-07T10:10:01Z"] {
            let mut n = new_challenge("ch-2", binding(), b"s");
            n.issued_at = t(issued);
            assert_eq!(
                Challenge::issue(n),
                Err(ChallengeRefusal::Expired),
                "{issued}"
            );
        }
        assert_eq!(
            challenge_expiry(t("2026-11-07T10:00:00Z"), t("2027-01-01T00:00:00Z")),
            t("2026-11-07T10:02:00Z")
        );
    }

    #[test]
    fn a_challenge_needs_ceremony_state_idempotency_key_and_credentials() {
        let malformed: Vec<fn(&mut NewChallenge)> = vec![
            |n| n.ceremony_state = CeremonyState::from_bytes(vec![]),
            |n| n.idempotency_key.clear(),
            |n| n.challenge_id.clear(),
            |n| n.allowed_credentials.clear(),
            |n| n.allowed_credentials.push(String::new()),
            |n| n.subject.subject.clear(),
            |n| n.binding.target_id.clear(),
        ];
        for (i, change) in malformed.into_iter().enumerate() {
            let mut n = new_challenge("ch-1", binding(), b"state-a");
            change(&mut n);
            assert_eq!(
                Challenge::issue(n),
                Err(ChallengeRefusal::Malformed),
                "case {i}"
            );
        }
        let c = challenge_for(binding());
        assert_eq!(c.ceremony_state().as_bytes(), b"state-a");
        assert_eq!(c.idempotency_key(), "idem-1");
        assert_eq!(c.verdict(), "approved");
        assert_eq!(c.status(), ChallengeStatus::Issued);
    }

    #[test]
    fn an_assertion_from_another_ceremony_is_refused() {
        let creds = active_credentials();
        let a = Challenge::issue(new_challenge("ch-a", binding(), b"state-a")).unwrap();
        let b = Challenge::issue(new_challenge("ch-b", binding(), b"state-b")).unwrap();
        // Ceremony A finished with A's own state verifies against A only.
        let from_a = a.finish(&TestVerifier, b"response", &creds).unwrap();
        assert!(from_a
            .clone()
            .check(&a, &binding(), "idem-1", now())
            .is_ok());
        assert_eq!(
            from_a.check(&b, &binding(), "idem-1", now()),
            Err(ApprovalCode::ChallengeInvalid(
                ChallengeRefusal::CeremonyMismatch
            ))
        );
        // The verifier never sees another subject's credentials.
        let mut n = new_challenge("ch-c", binding(), b"state-c");
        n.subject = SubjectId::new("writ-local", "carol");
        let carol = Challenge::issue(n).unwrap();
        assert_eq!(
            carol.finish(&TestVerifier, b"response", &creds),
            Err(ApprovalCode::ChallengeInvalid(
                ChallengeRefusal::SubjectMismatch
            ))
        );
        // begin and issue agree on the digest of the state.
        let start = TestVerifier.begin(&bob(), &creds).unwrap();
        assert_eq!(
            start.challenge_digest,
            Digest::of_bytes(start.state.as_bytes())
        );
    }

    #[test]
    fn an_idempotency_key_mismatch_is_refused() {
        let c = challenge_for(binding());
        assert_eq!(
            assertion_for(&c).check(&c, &binding(), "idem-2", now()),
            Err(ApprovalCode::ChallengeInvalid(
                ChallengeRefusal::IdempotencyMismatch
            ))
        );
        let accepted = assertion_for(&c)
            .check(&c, &binding(), "idem-1", now())
            .unwrap();
        assert_eq!(accepted.idempotency_key(), "idem-1");
        assert_eq!(accepted.challenge_id(), "ch-1");
        assert_eq!(accepted.credential_update().unwrap().public_material, [2]);
    }

    #[test]
    fn a_fabricated_assertion_cannot_reach_a_decision() {
        // Any code can build a verifier answer, but only the check against the
        // stored challenge yields the AcceptedAssertion a decision needs.
        let c = challenge_for(binding());
        let forged = VerifiedAssertion::new(
            bob(),
            "cred-1",
            true,
            WEBAUTHN_PASSKEY,
            Digest::of_bytes(b"made up"),
            None,
        );
        assert_eq!(
            forged.check(&c, &binding(), "idem-1", now()),
            Err(ApprovalCode::ChallengeInvalid(
                ChallengeRefusal::CeremonyMismatch
            ))
        );
        let refused = |a: VerifiedAssertion| a.check(&c, &binding(), "idem-1", now());
        let digest = c.challenge_digest().clone();
        let mk = |subject: SubjectId, cred: &str, uv: bool, method: &str, upd: Option<&str>| {
            VerifiedAssertion::new(
                subject,
                cred,
                uv,
                method,
                digest.clone(),
                upd.map(|id| CredentialUpdate {
                    credential_id: id.into(),
                    public_material: vec![],
                }),
            )
        };
        assert_eq!(
            refused(mk(bob(), "cred-1", false, WEBAUTHN_PASSKEY, None)),
            Err(ApprovalCode::AssertionInvalid(
                VerifierError::UserNotVerified
            ))
        );
        let rejected = Err(ApprovalCode::AssertionInvalid(VerifierError::Rejected));
        assert_eq!(
            refused(mk(bob(), "cred-1", true, "password", None)),
            rejected
        );
        assert_eq!(
            refused(mk(bob(), "cred-1", true, WEBAUTHN_PASSKEY, Some("cred-2"))),
            rejected
        );
        assert_eq!(
            refused(mk(
                SubjectId::new("writ-local", "carol"),
                "cred-1",
                true,
                WEBAUTHN_PASSKEY,
                None
            )),
            Err(ApprovalCode::ChallengeInvalid(
                ChallengeRefusal::SubjectMismatch
            ))
        );
        assert_eq!(
            refused(mk(bob(), "cred-2", true, WEBAUTHN_PASSKEY, None)),
            Err(ApprovalCode::ChallengeInvalid(
                ChallengeRefusal::CredentialNotAllowed
            ))
        );
    }

    #[test]
    fn challenge_mismatch_on_every_bound_field_is_refused() {
        use ChallengeRefusal::*;
        let c = challenge_for(binding());
        assert!(assertion_for(&c)
            .check(&c, &binding(), "idem-1", now())
            .is_ok());
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
            assert_eq!(
                assertion_for(&c).check(&c, &presented, "idem-1", now()),
                Err(ApprovalCode::ChallengeInvalid(want))
            );
            assert_eq!(
                ApprovalCode::ChallengeInvalid(want).code(),
                "CHALLENGE_INVALID"
            );
        }
    }

    fn stored(c: &Challenge) -> StoredChallenge {
        StoredChallenge {
            challenge_id: c.challenge_id().into(),
            challenge_digest: c.challenge_digest().clone(),
            ceremony_state: c.ceremony_state().clone(),
            subject: c.subject().clone(),
            allowed_credentials: c.allowed_credentials().to_vec(),
            binding: c.binding().clone(),
            verdict: c.verdict().into(),
            issued_at: c.issued_at(),
            expires_at: c.expires_at(),
            idempotency_key: c.idempotency_key().into(),
            status: c.status(),
        }
    }

    #[test]
    fn a_challenge_is_single_use_and_stored_rows_are_revalidated() {
        let c = challenge("2026-11-07T10:00:00Z", "2026-11-07T10:10:00Z");
        assert_eq!(Challenge::from_stored(stored(&c)), Ok(c.clone()));
        let used = c.consume().unwrap();
        assert_eq!(used.status(), ChallengeStatus::Consumed);
        assert_eq!(used.consume(), Err(ChallengeRefusal::Consumed));
        assert_eq!(
            assertion_for(&used).check(&used, &binding(), "idem-1", now()),
            Err(ApprovalCode::ChallengeInvalid(ChallengeRefusal::Consumed))
        );
        let mut row = stored(&c);
        row.status = ChallengeStatus::Expired;
        let expired = Challenge::from_stored(row).unwrap();
        assert_eq!(expired.consume(), Err(ChallengeRefusal::Expired));
        assert_eq!(
            assertion_for(&expired).check(&expired, &binding(), "idem-1", now()),
            Err(ApprovalCode::ChallengeInvalid(ChallengeRefusal::Expired))
        );
        let mut row = stored(&c);
        row.expires_at = row.issued_at.plus_seconds(CHALLENGE_TTL_SECONDS + 1);
        assert_eq!(
            Challenge::from_stored(row),
            Err(ChallengeRefusal::Malformed)
        );
        let mut row = stored(&c);
        row.ceremony_state = CeremonyState::from_bytes(vec![]);
        assert_eq!(
            Challenge::from_stored(row),
            Err(ChallengeRefusal::Malformed)
        );
    }

    fn new_signoff() -> NewSignoff {
        NewSignoff {
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
            expires_at: t("2026-11-07T11:00:00Z"),
        }
    }

    fn signoff_binding() -> ChallengeBinding {
        ChallengeBinding {
            operation: ChallengeOperation::Signoff,
            tenant_id: "t1".into(),
            project_id: "p1".into(),
            target_id: "so-1".into(),
            context_fingerprint: new_signoff().displayed().unwrap().fingerprint().clone(),
            policy_bundle_sha: sha('c'),
            revision: 7,
        }
    }

    #[test]
    fn signoff_context_binds_every_acceptance_member() {
        let s = Signoff::sign(new_signoff(), &accept(signoff_binding())).unwrap();
        assert_eq!(s.fields().subject, bob());
        assert_eq!(s.fields().issued_at, now());
        assert_eq!(s.fields().challenge_id, "ch-1");
        let fp = s.fingerprint().unwrap();
        let changes: Vec<fn(&mut NewSignoff)> = vec![
            |s| s.signoff_id = "so-2".into(),
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
            let mut other = new_signoff();
            change(&mut other);
            assert_ne!(other.displayed().unwrap().fingerprint(), &fp, "change {i}");
            // A changed binding cannot be signed with the original challenge.
            assert!(
                Signoff::sign(other, &accept(signoff_binding())).is_err(),
                "change {i}"
            );
        }
        // Only a signoff challenge signs.
        assert_eq!(
            Signoff::sign(new_signoff(), &accept(binding())),
            Err(ApprovalCode::ChallengeInvalid(
                ChallengeRefusal::OperationMismatch
            ))
        );
        // Stored rows are revalidated.
        assert_eq!(Signoff::from_stored(s.fields().clone()), Ok(s.clone()));
        let mut bad = s.fields().clone();
        bad.provenance_version = "writ-provenance-1".into();
        assert!(Signoff::from_stored(bad).is_err());
        let mut bad = s.fields().clone();
        bad.issued_at = bad.binding.expires_at;
        assert!(Signoff::from_stored(bad).is_err());
    }

    #[test]
    fn a_revocation_comes_only_from_a_revoke_challenge_for_its_target() {
        let target = RevocationTarget::Grant("g-1".into());
        let mut b = binding_for(ChallengeOperation::Revoke);
        b.target_id = target.target_id();
        let r = Revocation::record(target.clone(), "compromised", &accept(b)).unwrap();
        assert_eq!(r.fields().by, bob());
        assert_eq!(r.fields().at, now());
        assert_eq!(Revocation::from_stored(r.fields().clone()), Ok(r.clone()));
        assert_eq!(
            Revocation::record(
                RevocationTarget::Grant("g-2".into()),
                "x",
                &accept({
                    let mut b = binding_for(ChallengeOperation::Revoke);
                    b.target_id = target.target_id();
                    b
                })
            ),
            Err(ApprovalCode::ChallengeInvalid(
                ChallengeRefusal::TargetMismatch
            ))
        );
        assert_eq!(
            Revocation::record(target, "x", &accept(binding())),
            Err(ApprovalCode::ChallengeInvalid(
                ChallengeRefusal::OperationMismatch
            ))
        );
        // Subject target ids are unambiguous.
        assert_ne!(
            RevocationTarget::Subject(SubjectId::new("a:b", "c")).target_id(),
            RevocationTarget::Subject(SubjectId::new("a", "b:c")).target_id()
        );
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
        assert_eq!(
            ApprovalCode::ChallengeInvalid(ChallengeRefusal::CeremonyMismatch).diagnostic(),
            Some("challenge: ceremony mismatch")
        );
        assert_eq!(ApprovalCode::GrantRevoked.diagnostic(), None);
    }
}
