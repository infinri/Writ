//! Human subjects, roles and the verifier boundary (M4-01, M4-01b; D-040
//! O-1, O-7, O-8; D-041; M4-INTERFACES I-1 and review fixes R1..R8).
//!
//! Storage-independent: no network, HTTP, WebAuthn library, randomness or SQL.
//! The production [`HumanVerifier`] lives in the `writ-approval` crate; test
//! implementations live only in non-shipping test code.
//!
//! **Subjects** are identified by [`SubjectId`] (stable issuer or realm plus
//! subject), never by a display name. Every subject has a [`PrincipalKind`]
//! and a [`SubjectStatus`] (`active | revoked` with time, R7).
//!
//! **Verifier boundary** (R2..R4): a verifier receives only
//! [`ActiveCredentials`] (filtered by credential and subject status) and
//! returns a [`VerifiedAssertion`], which is untrusted output. The only way
//! to an [`AcceptedAssertion`] is [`VerifiedAssertion::check`] against the
//! stored [`Challenge`]: user verification, method, the ceremony's challenge
//! digest, the idempotency key, subject, credential and every bound field.
//! Decisions, signoffs and revocations are built only from an
//! [`AcceptedAssertion`]; the operation and verdict come from the checked
//! challenge, never from a separate parameter (R5).
//!
//! **Separation** (R1; [`approval_separation`], [`signoff_separation`]): the
//! acting subject must have an [`EnrollmentRecord`] (written only by the
//! M4-08 administrative ceremony) that declares, explicitly and possibly
//! empty, the requester identities that subject controls. Separation refuses
//! (`SEPARATION_VIOLATED`) when there is no enrollment record for the actor,
//! when any linked identity is an excluded party, or, as an additional fail
//! closed rule, when the actor's or a linked identity's `subject` text equals
//! an excluded party's text case-insensitively under any issuer
//! ([`same_subject`]). A false refusal is the safe direction (O-7).
//!
//! **Services** (O-8) may hold only [`Permission::Operator`]; never approver,
//! signoff authority or revoker ([`check_service_permission`]). Credential
//! selection, decisions, signoffs and revocations additionally refuse any
//! non-human subject (`SERVICE_NOT_PERMITTED`) and a subject without the
//! operation's permission in scope (`PERMISSION_MISSING`): see
//! [`check_human_permission`] and [`Actor`].
//!
//! **Capabilities** ([`VerifierCapability`], [`LedgerCapability`]): every
//! constructor that yields a value without the intended path (a verifier
//! answer, an enrollment record, a stored row read back) takes a capability
//! token. A [`VerifierCapability`] has no public constructor: it exists only
//! for the duration of a verifier call made by an [`ApprovalChannel`] (lent
//! by reference to [`HumanVerifier::finish`] and
//! [`RegistrationVerifier::finish_registration`]) and in this crate's unit
//! tests. The ledger capability's constructor stays public because the
//! SQLite ledger is another crate and Rust cannot restrict callers across
//! crates without a feature; `broker/tests/trusted_constructors.rs` confines
//! it to the SQLite ledger and test code, and nothing built with it is ever
//! accepted by a ledger as authority (the ledger reads its own rows, D-042 X1).
//!
//! **Approval channel** ([`ApprovalChannel`]): the verifier, its relying
//! party (RP id and exact origin) and the channel identity are configured
//! once on the ledger that serves approvals; ceremony and registration calls
//! never take a verifier. Code linking these crates in process is inside the
//! trust boundary (docs/implementation/M4-INTERFACES.md, "Configured approval
//! channel").
//!
//! **Unicode** (review finding, LOW): subject text is compared after
//! `to_lowercase` only; no Unicode normalization (NFC/NFKC) or confusable
//! folding is applied. Separation rests on the explicit enrollment links
//! (R1); the text match is only an additional fail-closed check, and
//! normalization would add a Unicode-data dependency to the broker.

use std::fmt;
use std::sync::Arc;

use writ_kernel::json::Digest;
use writ_kernel::time::Timestamp;

use crate::approval::{
    ApprovalCode, Challenge, ChallengeBinding, ChallengeOperation, InvalidRecord,
};

/// The only authentication method of the local profile (D-040 O-1).
pub const WEBAUTHN_PASSKEY: &str = "webauthn-passkey";

/// Capability to build a [`VerifiedAssertion`] or a
/// [`VerifiedRegistration`]. No public constructor: an [`ApprovalChannel`]
/// lends one, by reference, to its verifier for the duration of
/// [`HumanVerifier::finish`] or [`RegistrationVerifier::finish_registration`].
/// Not `Clone`, not `Copy`.
///
/// ```compile_fail
/// use writ_broker::human::VerifierCapability;
/// let forged = VerifierCapability(());
/// ```
///
/// ```compile_fail
/// use writ_broker::human::VerifierCapability;
/// let forged = VerifierCapability::for_verifier_implementation();
/// ```
#[derive(Debug)]
pub struct VerifierCapability(());

impl VerifierCapability {
    /// This crate's unit tests only.
    #[cfg(test)]
    pub(crate) fn for_verifier_implementation() -> VerifierCapability {
        VerifierCapability(())
    }
}

/// Capability to reconstruct stored rows (`from_stored`) and enrollment
/// records. Allowed only in the SQLite ledger (`persistence/sqlite/src`) and
/// in test code; enforced by `broker/tests/trusted_constructors.rs`. Not
/// `Clone`.
///
/// ```compile_fail
/// use writ_broker::human::LedgerCapability;
/// let forged = LedgerCapability(());
/// ```
#[derive(Debug)]
pub struct LedgerCapability(());

impl LedgerCapability {
    /// For the ledger implementation (and test code) only.
    pub fn for_ledger_implementation() -> LedgerCapability {
        LedgerCapability(())
    }
}

/// A stable subject identity: issuer or realm plus subject. Never a display name.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct SubjectId {
    /// Issuer or realm.
    pub issuer: String,
    /// Stable subject within the issuer.
    pub subject: String,
}

impl SubjectId {
    /// A subject id.
    pub fn new(issuer: &str, subject: &str) -> SubjectId {
        SubjectId {
            issuer: issuer.to_string(),
            subject: subject.to_string(),
        }
    }

    /// Both issuer and subject are non-empty (R7).
    pub fn is_well_formed(&self) -> bool {
        !self.issuer.is_empty() && !self.subject.is_empty()
    }
}

/// What kind of principal a subject is.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum PrincipalKind {
    /// A human.
    Human,
    /// A service or automation identity (O-8).
    Service,
}

/// Status of a subject (R7). Revocation is the ledger lookup.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SubjectStatus {
    /// Usable.
    Active,
    /// Revoked from `at` on.
    Revoked {
        /// Revocation time.
        at: Timestamp,
    },
}

/// A stored subject row.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SubjectRecord {
    /// The subject.
    pub subject: SubjectId,
    /// Its principal kind.
    pub kind: PrincipalKind,
    /// Its status.
    pub status: SubjectStatus,
}

impl SubjectRecord {
    /// `SUBJECT_REVOKED` when revoked at or before `now`.
    pub fn check_active(&self, now: Timestamp) -> Result<(), ApprovalCode> {
        match self.status {
            SubjectStatus::Revoked { at } if at <= now => Err(ApprovalCode::SubjectRevoked),
            _ => Ok(()),
        }
    }
}

/// A permission, granted per (tenant, project) scope (O-7).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Permission {
    /// Approve or deny stored approval requests.
    Approver,
    /// Sign off human acceptance criteria.
    SignoffAuthority,
    /// Administer workflows, policies, registry and enrollment.
    Operator,
    /// Revoke grants, signoffs, credentials, subjects or roles.
    Revoker,
}

/// One permission granted to one subject for one (tenant, project) scope.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RoleAssignment {
    /// The assignment id (the target of a role revocation, I-4).
    pub assignment_id: String,
    /// The subject.
    pub subject: SubjectId,
    /// The subject's principal kind.
    pub kind: PrincipalKind,
    /// The granted permission.
    pub permission: Permission,
    /// Tenant scope.
    pub tenant_id: String,
    /// Project scope.
    pub project_id: String,
    /// Grant time.
    pub granted_at: Timestamp,
    /// Revocation time, if revoked.
    pub revoked_at: Option<Timestamp>,
}

impl RoleAssignment {
    /// Active at `now`: granted at or before `now`, not revoked at or before `now`.
    pub fn active_at(&self, now: Timestamp) -> bool {
        self.granted_at <= now && self.revoked_at.is_none_or(|r| now < r)
    }
}

/// Status of a stored credential. Revocation is the ledger lookup.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CredentialStatus {
    /// Usable.
    Active,
    /// Revoked; never passed to a verifier.
    Revoked,
}

/// A stored passkey credential (public material only).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StoredCredential {
    /// Credential id.
    pub credential_id: String,
    /// The subject it authenticates.
    pub subject: SubjectId,
    /// Library-serialized passkey (public key material and counters).
    pub public_material: Vec<u8>,
    /// Status.
    pub status: CredentialStatus,
    /// Enrollment time.
    pub enrolled_at: Timestamp,
    /// The administrator who enrolled it.
    pub enrolled_by: SubjectId,
}

/// The active credentials of one active human subject (R4): the only
/// credential input to [`HumanVerifier`]. Built only by
/// [`ActiveCredentials::select`], which refuses a non-human, revoked or
/// unpermitted subject and drops revoked credentials and credentials of any
/// other subject.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ActiveCredentials {
    subject: SubjectId,
    credentials: Vec<StoredCredential>,
}

impl ActiveCredentials {
    /// The active credentials of `subject` among `stored`, for an operation
    /// needing `permission` in (tenant, project): [`check_human_permission`]
    /// first (`SERVICE_NOT_PERMITTED`, `SUBJECT_REVOKED`,
    /// `PERMISSION_MISSING`). The result may be empty.
    pub fn select(
        subject: &SubjectRecord,
        stored: impl IntoIterator<Item = StoredCredential>,
        assignments: &[RoleAssignment],
        permission: Permission,
        tenant_id: &str,
        project_id: &str,
        now: Timestamp,
    ) -> Result<ActiveCredentials, ApprovalCode> {
        check_human_permission(subject, assignments, permission, tenant_id, project_id, now)?;
        let credentials = stored
            .into_iter()
            .filter(|c| c.status == CredentialStatus::Active && c.subject == subject.subject)
            .collect();
        Ok(ActiveCredentials {
            subject: subject.subject.clone(),
            credentials,
        })
    }

    /// The subject.
    pub fn subject(&self) -> &SubjectId {
        &self.subject
    }

    /// The credentials (every one active and of [`ActiveCredentials::subject`]).
    pub fn as_slice(&self) -> &[StoredCredential] {
        &self.credentials
    }

    /// Their ids (a challenge's allowed credentials).
    pub fn ids(&self) -> Vec<String> {
        self.credentials
            .iter()
            .map(|c| c.credential_id.clone())
            .collect()
    }
}

/// Opaque server-side ceremony state: persisted only in the ledger, never
/// sent to or accepted from the client (D-041). `Debug` never prints the
/// contents.
#[derive(Clone, PartialEq, Eq)]
pub struct CeremonyState(Vec<u8>);

impl CeremonyState {
    /// Wrap verifier-produced bytes (or the ledger's stored bytes).
    pub fn from_bytes(bytes: Vec<u8>) -> CeremonyState {
        CeremonyState(bytes)
    }

    /// The bytes (for the verifier and the ledger only).
    pub fn as_bytes(&self) -> &[u8] {
        &self.0
    }

    /// No bytes.
    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }
}

impl fmt::Debug for CeremonyState {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "CeremonyState(<{} bytes>)", self.0.len())
    }
}

/// The start of an authentication ceremony.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CeremonyStart {
    /// Options sent to the approval page.
    pub client_options: Vec<u8>,
    /// Opaque state, persisted only in the ledger.
    pub state: CeremonyState,
    /// Digest of the server challenge.
    pub challenge_digest: Digest,
}

/// A credential's library-updated public material (for example its counter
/// or backup flags) after a successful assertion.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CredentialUpdate {
    /// Credential id.
    pub credential_id: String,
    /// The updated library-serialized passkey.
    pub public_material: Vec<u8>,
}

/// A verifier's answer: **untrusted** until [`VerifiedAssertion::check`]
/// accepts it against the stored challenge (R3). Constructible only with a
/// [`VerifierCapability`], which an [`ApprovalChannel`] lends to its verifier;
/// nothing consumes it except that check.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct VerifiedAssertion {
    subject: SubjectId,
    credential_id: String,
    user_verified: bool,
    method: String,
    challenge_digest: Digest,
    credential_update: Option<CredentialUpdate>,
}

impl VerifiedAssertion {
    /// A verifier's answer: the authenticated subject, the signing
    /// credential, the user-verification flag, the method, the digest of the
    /// server challenge the ceremony produced, and the credential update.
    pub fn new(
        _capability: &VerifierCapability,
        subject: SubjectId,
        credential_id: &str,
        user_verified: bool,
        method: &str,
        challenge_digest: Digest,
        credential_update: Option<CredentialUpdate>,
    ) -> VerifiedAssertion {
        VerifiedAssertion {
            subject,
            credential_id: credential_id.to_string(),
            user_verified,
            method: method.to_string(),
            challenge_digest,
            credential_update,
        }
    }

    /// The claimed subject.
    pub fn subject(&self) -> &SubjectId {
        &self.subject
    }

    /// The claimed credential.
    pub fn credential_id(&self) -> &str {
        &self.credential_id
    }

    /// The challenge digest the ceremony produced.
    pub fn challenge_digest(&self) -> &Digest {
        &self.challenge_digest
    }

    /// Broker-side acceptance at trusted time `now`: user verification,
    /// method, a credential update only for the signing credential
    /// (`ASSERTION_INVALID`), then the stored `challenge` against this
    /// assertion's challenge digest, the decision's `idempotency_key`, the
    /// subject and credential, and the `presented` binding recomputed from
    /// current stored state (`CHALLENGE_INVALID`). The only constructor of
    /// [`AcceptedAssertion`].
    pub fn check(
        self,
        challenge: &Challenge,
        presented: &ChallengeBinding,
        idempotency_key: &str,
        now: Timestamp,
    ) -> Result<AcceptedAssertion, ApprovalCode> {
        if !self.user_verified {
            return Err(ApprovalCode::AssertionInvalid(
                VerifierError::UserNotVerified,
            ));
        }
        let refuse = ApprovalCode::AssertionInvalid(VerifierError::Rejected);
        if self.method != WEBAUTHN_PASSKEY
            || self
                .credential_update
                .as_ref()
                .is_some_and(|u| u.credential_id != self.credential_id)
        {
            return Err(refuse);
        }
        challenge
            .check(
                presented,
                &self.challenge_digest,
                idempotency_key,
                &self.subject,
                &self.credential_id,
                now,
            )
            .map_err(ApprovalCode::ChallengeInvalid)?;
        Ok(AcceptedAssertion {
            challenge_id: challenge.challenge_id().to_string(),
            binding: challenge.binding().clone(),
            idempotency_key: idempotency_key.to_string(),
            subject: self.subject,
            credential_id: self.credential_id,
            credential_update: self.credential_update,
            accepted_at: now,
        })
    }
}

/// An assertion accepted against its stored challenge. Obtainable only from
/// [`VerifiedAssertion::check`]; never stored or reconstructed (decisions,
/// signoffs and revocations are stored instead).
///
/// ```compile_fail
/// use writ_broker::human::AcceptedAssertion;
/// fn forge() -> AcceptedAssertion {
///     AcceptedAssertion { challenge_id: String::new() }
/// }
/// ```
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AcceptedAssertion {
    challenge_id: String,
    binding: ChallengeBinding,
    idempotency_key: String,
    subject: SubjectId,
    credential_id: String,
    credential_update: Option<CredentialUpdate>,
    accepted_at: Timestamp,
}

impl AcceptedAssertion {
    /// The consumed challenge's id.
    pub fn challenge_id(&self) -> &str {
        &self.challenge_id
    }

    /// The checked challenge's binding.
    pub fn binding(&self) -> &ChallengeBinding {
        &self.binding
    }

    /// The checked challenge's operation (its verdict derives from it, R5).
    pub fn operation(&self) -> ChallengeOperation {
        self.binding.operation
    }

    /// The decision's idempotency key.
    pub fn idempotency_key(&self) -> &str {
        &self.idempotency_key
    }

    /// The authenticated subject.
    pub fn subject(&self) -> &SubjectId {
        &self.subject
    }

    /// The signing credential.
    pub fn credential_id(&self) -> &str {
        &self.credential_id
    }

    /// Credential state to persist with the decision, if any.
    pub fn credential_update(&self) -> Option<&CredentialUpdate> {
        self.credential_update.as_ref()
    }

    /// The trusted time of acceptance.
    pub fn accepted_at(&self) -> Timestamp {
        self.accepted_at
    }
}

/// Verifier refusal categories (the only diagnostic, `ASSERTION_INVALID`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum VerifierError {
    /// The response or state could not be parsed.
    Malformed,
    /// Challenge, origin or relying-party mismatch.
    BindingMismatch,
    /// The credential is not among those passed in.
    CredentialUnknown,
    /// The signature does not verify.
    SignatureInvalid,
    /// User presence or user verification flag missing.
    UserNotVerified,
    /// Counter or backup-state checks rejected the assertion.
    CredentialStateRejected,
    /// The broker refused the verifier's answer (subject, credential or method).
    Rejected,
    /// The verifier could not operate (fail closed).
    Unavailable,
}

impl VerifierError {
    /// Operator diagnostic category.
    pub fn category(self) -> &'static str {
        match self {
            VerifierError::Malformed => "assertion: malformed",
            VerifierError::BindingMismatch => "assertion: binding mismatch",
            VerifierError::CredentialUnknown => "assertion: credential unknown",
            VerifierError::SignatureInvalid => "assertion: signature invalid",
            VerifierError::UserNotVerified => "assertion: user not verified",
            VerifierError::CredentialStateRejected => "assertion: credential state rejected",
            VerifierError::Rejected => "assertion: rejected by broker",
            VerifierError::Unavailable => "assertion: verifier unavailable",
        }
    }
}

/// The human verifier boundary (implemented in `writ-approval`). Only
/// [`ActiveCredentials`] are passed in (R4); its output is never trusted
/// without [`VerifiedAssertion::check`].
pub trait HumanVerifier {
    /// The relying party this verifier checks assertions against (RP id and
    /// exact origin); an [`ApprovalChannel`] requires it to equal its
    /// registration verifier's.
    fn relying_party(&self) -> RelyingParty;

    /// Start a ceremony for `subject` over its active `credentials`.
    fn begin(
        &self,
        subject: &SubjectId,
        credentials: &ActiveCredentials,
    ) -> Result<CeremonyStart, VerifierError>;

    /// Finish a ceremony from the ledger-held `state` and the client
    /// `response`. `capability` is lent by the [`ApprovalChannel`] for this
    /// call only (the only way to build a [`VerifiedAssertion`]).
    fn finish(
        &self,
        capability: &VerifierCapability,
        state: &CeremonyState,
        response: &[u8],
        credentials: &ActiveCredentials,
    ) -> Result<VerifiedAssertion, VerifierError>;
}

/// The only credential algorithm enrollment accepts (D-041 DR-3 compensating
/// restriction: COSE ES256, ECDSA P-256 with SHA-256).
pub const ES256: &str = "ES256";

/// The relying party a verifier is configured for: the WebAuthn RP id and
/// the exact origin (scheme, host and port) of the approval listener.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RelyingParty {
    /// RP id (for the local profile, `localhost`).
    pub rp_id: String,
    /// Exact origin (for the local profile, `http://localhost:<port>`).
    pub origin: String,
}

/// What a registration ceremony is for (S5, X8): the bootstrap of the first
/// administrator, or an enrollment through the administrative ceremony.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum RegistrationOperation {
    /// [`crate::approval_store::BootstrapEnrollment`], only on an exclusively
    /// owned ledger without subjects.
    Bootstrap,
    /// [`crate::approval_store::NewEnrollment`] through an `Enroll` ceremony.
    Enroll,
}

impl RegistrationOperation {
    /// The stored text.
    pub fn as_str(self) -> &'static str {
        match self {
            RegistrationOperation::Bootstrap => "bootstrap",
            RegistrationOperation::Enroll => "enroll",
        }
    }

    /// Parse the stored text.
    pub fn parse(text: &str) -> Option<RegistrationOperation> {
        [
            RegistrationOperation::Bootstrap,
            RegistrationOperation::Enroll,
        ]
        .into_iter()
        .find(|o| o.as_str() == text)
    }
}

/// The server-held registration ceremony a verifier finishes: built by the
/// ledger from its registration row (never from client input) and copied
/// into the [`VerifiedRegistration`] the verifier returns.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RegistrationBinding {
    /// The registration row's id.
    pub registration_id: String,
    /// What it is for.
    pub operation: RegistrationOperation,
    /// The subject being registered.
    pub subject: SubjectId,
    /// The relying party the row was issued for.
    pub relying_party: RelyingParty,
}

/// The start of a registration ceremony.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RegistrationStart {
    /// Creation options sent to the page.
    pub client_options: Vec<u8>,
    /// Opaque state, persisted only in the ledger's registration row.
    pub state: CeremonyState,
}

/// A verifier's registration result (S5, X8): **untrusted** until the
/// ledger accepts it against its registration row (bound to subject, RP id,
/// origin, operation and expiry, single use). Constructible only with a
/// [`VerifierCapability`] (lent by an [`ApprovalChannel`] to its
/// registration verifier); an enrollment accepts only this type and
/// only when the ledger's row recorded it, so raw public material is never
/// enrolled.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct VerifiedRegistration {
    binding: RegistrationBinding,
    credential_id: String,
    public_material: Vec<u8>,
    algorithm: String,
    user_verified: bool,
}

impl VerifiedRegistration {
    /// A verifier's registration result for the ceremony `binding`: the new
    /// credential's id, its library-serialized public material, its COSE
    /// algorithm name (as verified by the library) and the user-verification
    /// flag.
    pub fn new(
        _capability: &VerifierCapability,
        binding: &RegistrationBinding,
        credential_id: &str,
        public_material: Vec<u8>,
        algorithm: &str,
        user_verified: bool,
    ) -> VerifiedRegistration {
        VerifiedRegistration {
            binding: binding.clone(),
            credential_id: credential_id.to_string(),
            public_material,
            algorithm: algorithm.to_string(),
            user_verified,
        }
    }

    /// The registration ceremony it claims to finish.
    pub fn binding(&self) -> &RegistrationBinding {
        &self.binding
    }

    /// The registration row's id.
    pub fn registration_id(&self) -> &str {
        &self.binding.registration_id
    }

    /// The registered subject.
    pub fn subject(&self) -> &SubjectId {
        &self.binding.subject
    }

    /// The new credential's id.
    pub fn credential_id(&self) -> &str {
        &self.credential_id
    }

    /// The library-serialized public material (never a secret).
    pub fn public_material(&self) -> &[u8] {
        &self.public_material
    }

    /// The credential's COSE algorithm name.
    pub fn algorithm(&self) -> &str {
        &self.algorithm
    }

    /// The authenticator verified the user during registration.
    pub fn user_verified(&self) -> bool {
        self.user_verified
    }
}

/// The registration side of the verifier boundary (S5, X8; implemented in
/// `writ-approval`). Its output is never trusted without the ledger's
/// registration row.
pub trait RegistrationVerifier {
    /// The relying party this verifier checks (RP id and exact origin).
    fn relying_party(&self) -> RelyingParty;

    /// Start a registration ceremony for the new `subject` (user
    /// verification required).
    fn begin_registration(&self, subject: &SubjectId) -> Result<RegistrationStart, VerifierError>;

    /// Finish the ceremony `binding` from the ledger-held `state` and the
    /// client `response` (user verification required). `capability` is lent
    /// by the [`ApprovalChannel`] for this call only.
    fn finish_registration(
        &self,
        capability: &VerifierCapability,
        binding: &RegistrationBinding,
        state: &CeremonyState,
        response: &[u8],
    ) -> Result<VerifiedRegistration, VerifierError>;
}

/// The approval channel a ledger serves (D-040 O-2): the verifier, its
/// relying party (RP id and exact origin, read once from the registration
/// verifier when the channel is built) and the channel identity recorded on
/// decisions. Configured once on the ledger when it is opened for approval
/// service and immutable afterwards; ceremony and registration calls never
/// take a verifier. Challenges and registration rows record the RP id and
/// origin they were issued for, and a finish under a channel with another RP
/// id or origin is refused.
///
/// This does not authenticate the verifier: whoever builds the channel
/// chooses it. Code that links these crates in process and opens the ledger
/// is inside the trust boundary, equivalent to access to the ledger file
/// (docs/implementation/M4-INTERFACES.md, "Configured approval channel").
#[derive(Clone)]
pub struct ApprovalChannel {
    human: Arc<dyn HumanVerifier + Send + Sync>,
    registration: Arc<dyn RegistrationVerifier + Send + Sync>,
    relying_party: RelyingParty,
    channel: String,
}

impl ApprovalChannel {
    /// The channel of `human` and `registration` (in production one
    /// `writ-approval` passkey verifier for both, configured for the same
    /// relying party) under the channel identity `channel`. `None` when the
    /// two verifiers report different relying parties (assertions would be
    /// verified for one origin while challenges record another), or when the
    /// RP id, the origin or the channel identity is empty.
    pub fn new(
        human: Arc<dyn HumanVerifier + Send + Sync>,
        registration: Arc<dyn RegistrationVerifier + Send + Sync>,
        channel: &str,
    ) -> Option<ApprovalChannel> {
        let relying_party = registration.relying_party();
        if human.relying_party() != relying_party
            || relying_party.rp_id.is_empty()
            || relying_party.origin.is_empty()
            || channel.is_empty()
        {
            return None;
        }
        Some(ApprovalChannel {
            human,
            registration,
            relying_party,
            channel: channel.to_string(),
        })
    }

    /// The relying party fixed when the channel was built.
    pub fn relying_party(&self) -> &RelyingParty {
        &self.relying_party
    }

    /// The channel identity recorded on decisions.
    pub fn channel(&self) -> &str {
        &self.channel
    }

    /// [`HumanVerifier::begin`] of the channel's verifier.
    pub fn begin(
        &self,
        subject: &SubjectId,
        credentials: &ActiveCredentials,
    ) -> Result<CeremonyStart, VerifierError> {
        self.human.begin(subject, credentials)
    }

    /// [`HumanVerifier::finish`] of the channel's verifier, with the
    /// capability lent for this call. The answer stays untrusted until
    /// [`VerifiedAssertion::check`].
    pub fn finish(
        &self,
        state: &CeremonyState,
        response: &[u8],
        credentials: &ActiveCredentials,
    ) -> Result<VerifiedAssertion, VerifierError> {
        self.human
            .finish(&VerifierCapability(()), state, response, credentials)
    }

    /// [`RegistrationVerifier::begin_registration`] of the channel's verifier.
    pub fn begin_registration(
        &self,
        subject: &SubjectId,
    ) -> Result<RegistrationStart, VerifierError> {
        self.registration.begin_registration(subject)
    }

    /// [`RegistrationVerifier::finish_registration`] of the channel's
    /// verifier, with the capability lent for this call. The answer stays
    /// untrusted until the ledger accepts it against its registration row.
    pub fn finish_registration(
        &self,
        binding: &RegistrationBinding,
        state: &CeremonyState,
        response: &[u8],
    ) -> Result<VerifiedRegistration, VerifierError> {
        self.registration
            .finish_registration(&VerifierCapability(()), binding, state, response)
    }
}

impl fmt::Debug for ApprovalChannel {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("ApprovalChannel")
            .field("relying_party", &self.relying_party)
            .field("channel", &self.channel)
            .finish_non_exhaustive()
    }
}

/// A human subject's enrollment (R1), written only by the M4-08
/// administrative ceremony: the explicit, possibly empty, set of requester
/// identities (ingress realm as issuer; principal or agent id as subject)
/// that subject controls. Separation refuses an actor without one.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EnrollmentRecord {
    subject: SubjectId,
    linked_identities: Vec<SubjectId>,
    enrolled_at: Timestamp,
    enrolled_by: SubjectId,
}

impl EnrollmentRecord {
    /// Validate (every identity well formed); linked identities are stored
    /// sorted and unique. Only with a [`LedgerCapability`]: the M4-08
    /// ceremony writes the record through the ledger, which reads it back.
    pub fn new(
        _capability: &LedgerCapability,
        subject: SubjectId,
        mut linked_identities: Vec<SubjectId>,
        enrolled_at: Timestamp,
        enrolled_by: SubjectId,
    ) -> Result<EnrollmentRecord, InvalidRecord> {
        if !subject.is_well_formed()
            || !enrolled_by.is_well_formed()
            || !linked_identities.iter().all(SubjectId::is_well_formed)
        {
            return Err(InvalidRecord("enrollment: empty identity"));
        }
        linked_identities.sort();
        linked_identities.dedup();
        Ok(EnrollmentRecord {
            subject,
            linked_identities,
            enrolled_at,
            enrolled_by,
        })
    }

    /// The enrolled human subject.
    pub fn subject(&self) -> &SubjectId {
        &self.subject
    }

    /// The requester identities the subject controls.
    pub fn linked_identities(&self) -> &[SubjectId] {
        &self.linked_identities
    }

    /// Enrollment time.
    pub fn enrolled_at(&self) -> Timestamp {
        self.enrolled_at
    }

    /// The enrolling administrator.
    pub fn enrolled_by(&self) -> &SubjectId {
        &self.enrolled_by
    }
}

/// Same subject, fail closed: equal ids, or equal subject text under any
/// issuer, compared case-insensitively (`to_lowercase`; no Unicode
/// normalization, see the module notes: enrollment links are primary).
pub fn same_subject(a: &SubjectId, b: &SubjectId) -> bool {
    a == b || a.subject.to_lowercase() == b.subject.to_lowercase()
}

/// `SEPARATION_VIOLATED` unless `enrollment` is the actor's own record and
/// neither the actor nor any linked identity is any of `excluded`.
fn check_separation<'a>(
    actor: &SubjectId,
    enrollment: Option<&EnrollmentRecord>,
    excluded: impl IntoIterator<Item = &'a SubjectId>,
) -> Result<(), ApprovalCode> {
    let Some(enrollment) = enrollment.filter(|e| &e.subject == actor) else {
        return Err(ApprovalCode::SeparationViolated);
    };
    let violated = excluded.into_iter().any(|x| {
        same_subject(actor, x)
            || enrollment
                .linked_identities
                .iter()
                .any(|l| same_subject(l, x))
    });
    if violated {
        return Err(ApprovalCode::SeparationViolated);
    }
    Ok(())
}

/// The parties of a request that may never approve or sign off for it: the
/// requester and the agent. M3 sealed ingress stores no trusted delegation
/// chain (G4), so there is none to exclude; the stored request context
/// records `delegation_chain: "unavailable"` and nothing claims
/// delegation-chain separation.
#[derive(Clone, Copy, Debug)]
pub struct Parties<'a> {
    /// Requester principal.
    pub requester: &'a SubjectId,
    /// Agent, if any.
    pub agent: Option<&'a SubjectId>,
}

impl<'a> Parties<'a> {
    fn all(&self) -> impl Iterator<Item = &'a SubjectId> {
        std::iter::once(self.requester).chain(self.agent)
    }
}

/// Approval separation (O-7, R1): the approver's own enrollment record is
/// required; neither the approver nor a linked identity may be the
/// requester or the agent. Delegation chains are not stored (G4): this
/// separation covers requester, agent and linked identities only.
pub fn approval_separation(
    approver: &SubjectId,
    enrollment: Option<&EnrollmentRecord>,
    parties: &Parties<'_>,
) -> Result<(), ApprovalCode> {
    check_separation(approver, enrollment, parties.all())
}

/// Signoff separation (O-7, R1, X7): the signer's own enrollment record is
/// required; neither the signer nor a linked identity may be any of
/// `excluded` (the stored promotion requesters and agents, the enrolled
/// subjects linked to them, the artifact and policy changers and any added
/// exclusion, read from the ledger by the caller; see
/// [`crate::approval::SignoffExclusions`]). Delegation chains are not stored
/// (G4): this separation covers requester, agent and linked identities, not a
/// delegation chain.
pub fn signoff_separation(
    signer: &SubjectId,
    enrollment: Option<&EnrollmentRecord>,
    excluded: &[SubjectId],
) -> Result<(), ApprovalCode> {
    check_separation(signer, enrollment, excluded.iter())
}

/// O-8: services hold only [`Permission::Operator`].
pub fn check_service_permission(
    kind: PrincipalKind,
    permission: Permission,
) -> Result<(), ApprovalCode> {
    if kind == PrincipalKind::Service && permission != Permission::Operator {
        return Err(ApprovalCode::ServiceNotPermitted);
    }
    Ok(())
}

/// The wildcard of an explicitly granted role scope (X5).
pub const SCOPE_WILDCARD: &str = "*";

/// Scope containment of a role assignment (M4-INTERFACES X5): an assignment
/// for (`*`, `*`) contains every scope; (`t`, `*`) contains every project of
/// tenant `t` (and (`t`, `*`) itself); (`t`, `p`) contains only itself. A
/// `*` is honoured only where explicitly granted: containment is never
/// inferred across tenants, an assignment with tenant `*` and a concrete
/// project contains only that exact scope, and a `*` in the target is never
/// matched by a narrower assignment.
pub fn scope_contains(
    assigned_tenant: &str,
    assigned_project: &str,
    tenant_id: &str,
    project_id: &str,
) -> bool {
    let w = SCOPE_WILDCARD;
    (assigned_tenant == w && assigned_project == w)
        || (assigned_tenant == tenant_id
            && (assigned_project == project_id || (assigned_project == w && assigned_tenant != w)))
}

/// The subject holds `permission` for (tenant, project) at `now`: the
/// service rule first (`SERVICE_NOT_PERMITTED`), then an active assignment of
/// the same subject, kind and permission whose scope contains (tenant,
/// project) ([`scope_contains`]; `PERMISSION_MISSING`). An assignment that
/// breaks the service rule never counts.
pub fn check_permission(
    assignments: &[RoleAssignment],
    subject: &SubjectId,
    kind: PrincipalKind,
    permission: Permission,
    tenant_id: &str,
    project_id: &str,
    now: Timestamp,
) -> Result<(), ApprovalCode> {
    check_service_permission(kind, permission)?;
    let held = assignments.iter().any(|a| {
        &a.subject == subject
            && a.kind == kind
            && a.permission == permission
            && scope_contains(&a.tenant_id, &a.project_id, tenant_id, project_id)
            && a.active_at(now)
            && check_service_permission(a.kind, a.permission).is_ok()
    });
    if !held {
        return Err(ApprovalCode::PermissionMissing);
    }
    Ok(())
}

/// A human actor: the authenticated subject's stored records, supplied by
/// the caller from the ledger; [`check_actor`] decides.
#[derive(Clone, Copy, Debug)]
pub struct Actor<'a> {
    /// The subject row.
    pub record: &'a SubjectRecord,
    /// The subject's enrollment record, if any (none refuses separation).
    pub enrollment: Option<&'a EnrollmentRecord>,
    /// Role assignments (any subject's; only the actor's count).
    pub assignments: &'a [RoleAssignment],
}

/// The subject is human (`SERVICE_NOT_PERMITTED`), active at `now`
/// (`SUBJECT_REVOKED`) and holds `permission` for (tenant, project) at `now`
/// (`PERMISSION_MISSING`).
pub fn check_human_permission(
    record: &SubjectRecord,
    assignments: &[RoleAssignment],
    permission: Permission,
    tenant_id: &str,
    project_id: &str,
    now: Timestamp,
) -> Result<(), ApprovalCode> {
    if record.kind != PrincipalKind::Human {
        return Err(ApprovalCode::ServiceNotPermitted);
    }
    record.check_active(now)?;
    check_permission(
        assignments,
        &record.subject,
        PrincipalKind::Human,
        permission,
        tenant_id,
        project_id,
        now,
    )
}

/// The actor's record is the accepted assertion's subject
/// (`CHALLENGE_INVALID`, subject mismatch), then
/// [`check_human_permission`] at the acceptance time in the challenge's
/// scope.
pub fn check_actor(
    actor: &Actor<'_>,
    accepted: &AcceptedAssertion,
    permission: Permission,
) -> Result<(), ApprovalCode> {
    if &actor.record.subject != accepted.subject() {
        return Err(ApprovalCode::ChallengeInvalid(
            crate::approval::ChallengeRefusal::SubjectMismatch,
        ));
    }
    let b = accepted.binding();
    check_human_permission(
        actor.record,
        actor.assignments,
        permission,
        &b.tenant_id,
        &b.project_id,
        accepted.accepted_at(),
    )
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;

    fn t(s: &str) -> Timestamp {
        Timestamp::parse_strict(s).unwrap()
    }

    fn sid(i: &str, s: &str) -> SubjectId {
        SubjectId::new(i, s)
    }

    fn enrolled(subject: &SubjectId, linked: &[SubjectId]) -> EnrollmentRecord {
        EnrollmentRecord::new(
            &LedgerCapability::for_ledger_implementation(),
            subject.clone(),
            linked.to_vec(),
            t("2026-11-01T00:00:00Z"),
            sid("writ-local", "admin"),
        )
        .unwrap()
    }

    fn role(subject: SubjectId, kind: PrincipalKind, permission: Permission) -> RoleAssignment {
        RoleAssignment {
            assignment_id: "role-1".into(),
            subject,
            kind,
            permission,
            tenant_id: "t1".into(),
            project_id: "p1".into(),
            granted_at: t("2026-11-01T00:00:00Z"),
            revoked_at: None,
        }
    }

    fn cred(id: &str, subject: &SubjectId, status: CredentialStatus) -> StoredCredential {
        StoredCredential {
            credential_id: id.into(),
            subject: subject.clone(),
            public_material: vec![1],
            status,
            enrolled_at: t("2026-11-01T00:00:00Z"),
            enrolled_by: sid("writ-local", "admin"),
        }
    }

    /// A verifier for one origin (assertions and registrations both refuse).
    struct For(&'static str);

    impl HumanVerifier for For {
        fn relying_party(&self) -> RelyingParty {
            RelyingParty {
                rp_id: "localhost".into(),
                origin: self.0.into(),
            }
        }

        fn begin(
            &self,
            _subject: &SubjectId,
            _credentials: &ActiveCredentials,
        ) -> Result<CeremonyStart, VerifierError> {
            Err(VerifierError::Unavailable)
        }

        fn finish(
            &self,
            _capability: &VerifierCapability,
            _state: &CeremonyState,
            _response: &[u8],
            _credentials: &ActiveCredentials,
        ) -> Result<VerifiedAssertion, VerifierError> {
            Err(VerifierError::Unavailable)
        }
    }

    impl RegistrationVerifier for For {
        fn relying_party(&self) -> RelyingParty {
            HumanVerifier::relying_party(self)
        }

        fn begin_registration(
            &self,
            _subject: &SubjectId,
        ) -> Result<RegistrationStart, VerifierError> {
            Err(VerifierError::Unavailable)
        }

        fn finish_registration(
            &self,
            _capability: &VerifierCapability,
            _binding: &RegistrationBinding,
            _state: &CeremonyState,
            _response: &[u8],
        ) -> Result<VerifiedRegistration, VerifierError> {
            Err(VerifierError::Unavailable)
        }
    }

    /// N1: a channel whose assertion verifier and registration verifier
    /// check different relying parties is never built (it would record one
    /// origin on challenges while assertions verify against the other).
    #[test]
    fn a_channel_refuses_verifiers_for_different_relying_parties() {
        let a = Arc::new(For("http://localhost:8443"));
        let b = Arc::new(For("http://localhost:9999"));
        assert!(ApprovalChannel::new(a.clone(), b.clone(), "listener").is_none());
        assert!(ApprovalChannel::new(b.clone(), a.clone(), "listener").is_none());
        let same =
            ApprovalChannel::new(a.clone(), Arc::new(For("http://localhost:8443")), "l").unwrap();
        assert_eq!(same.relying_party().origin, "http://localhost:8443");
        assert!(ApprovalChannel::new(a.clone(), a.clone(), "").is_none());
        assert!(ApprovalChannel::new(Arc::new(For("")), Arc::new(For("")), "l").is_none());
    }

    #[test]
    fn ceremony_state_debug_never_prints_contents() {
        let state = CeremonyState::from_bytes(b"secret-nonce-bytes".to_vec());
        let shown = format!("{state:?}");
        assert_eq!(shown, "CeremonyState(<18 bytes>)");
        let start = CeremonyStart {
            client_options: vec![],
            state,
            challenge_digest: Digest::of_bytes(b"c"),
        };
        assert!(!format!("{start:?}").contains("secret"));
    }

    #[test]
    fn separation_covers_requester_and_agent() {
        let requester = sid("adapter-claude", "alice");
        let agent = sid("agent-realm", "agent-7");
        let parties = Parties {
            requester: &requester,
            agent: Some(&agent),
        };
        for bad in [&requester, &agent] {
            assert_eq!(
                approval_separation(bad, Some(&enrolled(bad, &[])), &parties),
                Err(ApprovalCode::SeparationViolated)
            );
        }
        // The same subject text under the human realm is still the requester (fail closed).
        for text in ["alice", "agent-7"] {
            let s = sid("writ-local", text);
            assert_eq!(
                approval_separation(&s, Some(&enrolled(&s, &[])), &parties),
                Err(ApprovalCode::SeparationViolated)
            );
        }
        let bob = sid("writ-local", "bob");
        assert_eq!(
            approval_separation(&bob, Some(&enrolled(&bob, &[])), &parties),
            Ok(())
        );
        let no_agent = Parties {
            requester: &requester,
            agent: None,
        };
        assert_eq!(
            approval_separation(&bob, Some(&enrolled(&bob, &[])), &no_agent),
            Ok(())
        );
    }

    #[test]
    fn an_approver_without_their_own_enrollment_record_is_refused() {
        let requester = sid("adapter", "alice");
        let parties = Parties {
            requester: &requester,
            agent: None,
        };
        let bob = sid("writ-local", "bob");
        assert_eq!(
            approval_separation(&bob, None, &parties),
            Err(ApprovalCode::SeparationViolated)
        );
        assert_eq!(
            signoff_separation(&bob, None, std::slice::from_ref(&requester)),
            Err(ApprovalCode::SeparationViolated)
        );
        // Another subject's record does not count.
        let carol = sid("writ-local", "carol");
        assert_eq!(
            approval_separation(&bob, Some(&enrolled(&carol, &[])), &parties),
            Err(ApprovalCode::SeparationViolated)
        );
    }

    #[test]
    fn a_linked_identity_equal_to_any_party_is_refused() {
        let requester = sid("adapter-claude", "u-1234");
        let agent = sid("agent-realm", "agent-7");
        let chain = [sid("agent-realm", "parent-1")];
        let parties = Parties {
            requester: &requester,
            agent: Some(&agent),
        };
        let bob = sid("writ-local", "bob");
        for linked in [&requester, &agent] {
            let e = enrolled(&bob, &[sid("other-adapter", "x"), linked.clone()]);
            assert_eq!(
                approval_separation(&bob, Some(&e), &parties),
                Err(ApprovalCode::SeparationViolated)
            );
            assert_eq!(
                signoff_separation(
                    &bob,
                    Some(&e),
                    &[requester.clone(), agent.clone(), chain[0].clone()]
                ),
                Err(ApprovalCode::SeparationViolated)
            );
        }
        let unrelated = enrolled(&bob, &[sid("adapter-claude", "u-9999")]);
        assert_eq!(
            approval_separation(&bob, Some(&unrelated), &parties),
            Ok(())
        );
    }

    #[test]
    fn a_case_variant_subject_text_is_the_same_subject() {
        let requester = sid("adapter-claude", "alice");
        let parties = Parties {
            requester: &requester,
            agent: None,
        };
        for text in ["Alice", "ALICE", "aLiCe"] {
            let s = sid("writ-local", text);
            assert!(same_subject(&s, &requester), "{text}");
            assert_eq!(
                approval_separation(&s, Some(&enrolled(&s, &[])), &parties),
                Err(ApprovalCode::SeparationViolated)
            );
        }
        // A linked identity in another case is refused too.
        let bob = sid("writ-local", "bob");
        let e = enrolled(&bob, &[sid("adapter-x", "ALICE")]);
        assert_eq!(
            approval_separation(&bob, Some(&e), &parties),
            Err(ApprovalCode::SeparationViolated)
        );
    }

    #[test]
    fn enrollment_records_are_validated() {
        let bob = sid("writ-local", "bob");
        let e = enrolled(&bob, &[sid("a", "2"), sid("a", "1"), sid("a", "2")]);
        assert_eq!(e.linked_identities(), [sid("a", "1"), sid("a", "2")]);
        let cap = LedgerCapability::for_ledger_implementation();
        assert!(EnrollmentRecord::new(
            &cap,
            sid("", "bob"),
            vec![],
            t("2026-11-01T00:00:00Z"),
            sid("writ-local", "admin")
        )
        .is_err());
        assert!(EnrollmentRecord::new(
            &cap,
            bob,
            vec![sid("adapter", "")],
            t("2026-11-01T00:00:00Z"),
            sid("writ-local", "admin")
        )
        .is_err());
    }

    #[test]
    fn signoff_separation_also_excludes_artifact_and_policy_changers() {
        let requester = sid("adapter", "alice");
        let chain = [sid("agent-realm", "parent-1")];
        let parties = Parties {
            requester: &requester,
            agent: None,
        };
        let changers = [sid("writ-local", "carol"), sid("writ-local", "dave")];
        let excluded = [
            requester.clone(),
            chain[0].clone(),
            changers[0].clone(),
            changers[1].clone(),
        ];
        let bob = sid("writ-local", "bob");
        assert_eq!(
            signoff_separation(&bob, Some(&enrolled(&bob, &[])), &excluded),
            Ok(())
        );
        for bad in [&changers[0], &changers[1], &chain[0], &requester] {
            assert_eq!(
                signoff_separation(bad, Some(&enrolled(bad, &[])), &excluded),
                Err(ApprovalCode::SeparationViolated)
            );
        }
        // An approver who changed the artifact is not excluded from approval.
        assert_eq!(
            approval_separation(&changers[0], Some(&enrolled(&changers[0], &[])), &parties),
            Ok(())
        );
    }

    #[test]
    fn subject_status_is_active_until_its_revocation_time() {
        let now = t("2026-11-07T00:00:00Z");
        let mut r = SubjectRecord {
            subject: sid("writ-local", "bob"),
            kind: PrincipalKind::Human,
            status: SubjectStatus::Active,
        };
        assert_eq!(r.check_active(now), Ok(()));
        r.status = SubjectStatus::Revoked {
            at: now.plus_seconds(1),
        };
        assert_eq!(r.check_active(now), Ok(()));
        r.status = SubjectStatus::Revoked { at: now };
        assert_eq!(r.check_active(now), Err(ApprovalCode::SubjectRevoked));
    }

    #[test]
    fn a_revoked_credential_or_subject_cannot_reach_a_verifier() {
        let bob = sid("writ-local", "bob");
        let now = t("2026-11-07T00:00:00Z");
        let record = SubjectRecord {
            subject: bob.clone(),
            kind: PrincipalKind::Human,
            status: SubjectStatus::Active,
        };
        let stored = vec![
            cred("cred-1", &bob, CredentialStatus::Active),
            cred("cred-2", &bob, CredentialStatus::Revoked),
            cred(
                "cred-3",
                &sid("writ-local", "carol"),
                CredentialStatus::Active,
            ),
        ];
        let roles = [role(
            bob.clone(),
            PrincipalKind::Human,
            Permission::Approver,
        )];
        let select = |r: &SubjectRecord, stored: Vec<StoredCredential>| {
            ActiveCredentials::select(r, stored, &roles, Permission::Approver, "t1", "p1", now)
        };
        let active = select(&record, stored.clone()).unwrap();
        assert_eq!(active.ids(), ["cred-1"]);
        assert_eq!(active.subject(), &bob);
        assert!(active
            .as_slice()
            .iter()
            .all(|c| c.status == CredentialStatus::Active));
        let revoked = SubjectRecord {
            status: SubjectStatus::Revoked { at: now },
            ..record
        };
        assert_eq!(select(&revoked, stored), Err(ApprovalCode::SubjectRevoked));
    }

    #[test]
    fn credential_selection_refuses_services_and_missing_permission() {
        let bob = sid("writ-local", "bob");
        let now = t("2026-11-07T00:00:00Z");
        let human = SubjectRecord {
            subject: bob.clone(),
            kind: PrincipalKind::Human,
            status: SubjectStatus::Active,
        };
        let stored = || vec![cred("cred-1", &bob, CredentialStatus::Active)];
        let approver = [role(
            bob.clone(),
            PrincipalKind::Human,
            Permission::Approver,
        )];
        // A service subject never reaches a verifier, whatever it holds.
        let service = SubjectRecord {
            kind: PrincipalKind::Service,
            ..human.clone()
        };
        let service_roles = [role(
            bob.clone(),
            PrincipalKind::Service,
            Permission::Operator,
        )];
        for (roles, p) in [
            (&approver[..], Permission::Approver),
            (&service_roles[..], Permission::Operator),
        ] {
            assert_eq!(
                ActiveCredentials::select(&service, stored(), roles, p, "t1", "p1", now),
                Err(ApprovalCode::ServiceNotPermitted)
            );
        }
        // Missing permission, wrong permission or wrong scope.
        let missing = Err(ApprovalCode::PermissionMissing);
        assert_eq!(
            ActiveCredentials::select(&human, stored(), &[], Permission::Approver, "t1", "p1", now),
            missing
        );
        assert_eq!(
            ActiveCredentials::select(
                &human,
                stored(),
                &approver,
                Permission::SignoffAuthority,
                "t1",
                "p1",
                now
            ),
            missing
        );
        assert_eq!(
            ActiveCredentials::select(
                &human,
                stored(),
                &approver,
                Permission::Approver,
                "t2",
                "p1",
                now
            ),
            missing
        );
        assert!(ActiveCredentials::select(
            &human,
            stored(),
            &approver,
            Permission::Approver,
            "t1",
            "p1",
            now
        )
        .is_ok());
    }

    #[test]
    fn services_are_never_approver_signoff_or_revoker() {
        for p in [
            Permission::Approver,
            Permission::SignoffAuthority,
            Permission::Revoker,
        ] {
            assert_eq!(
                check_service_permission(PrincipalKind::Service, p),
                Err(ApprovalCode::ServiceNotPermitted)
            );
            assert_eq!(check_service_permission(PrincipalKind::Human, p), Ok(()));
        }
        assert_eq!(
            check_service_permission(PrincipalKind::Service, Permission::Operator),
            Ok(())
        );
        // Even a stored assignment cannot give a service a human-review role.
        let svc = sid("svc", "ci");
        let roles = [role(
            svc.clone(),
            PrincipalKind::Service,
            Permission::Approver,
        )];
        let now = t("2026-11-07T00:00:00Z");
        assert_eq!(
            check_permission(
                &roles,
                &svc,
                PrincipalKind::Service,
                Permission::Approver,
                "t1",
                "p1",
                now
            ),
            Err(ApprovalCode::ServiceNotPermitted)
        );
        // Nor does a service-kind assignment count for the same subject claimed human.
        assert_eq!(
            check_permission(
                &roles,
                &svc,
                PrincipalKind::Human,
                Permission::Approver,
                "t1",
                "p1",
                now
            ),
            Err(ApprovalCode::PermissionMissing)
        );
    }

    #[test]
    fn permission_is_scoped_timed_and_per_subject() {
        let alice = sid("writ-local", "alice");
        let mut r = role(alice.clone(), PrincipalKind::Human, Permission::Approver);
        let now = t("2026-11-07T00:00:00Z");
        let check = |roles: &[RoleAssignment], p, tenant, project| {
            check_permission(roles, &alice, PrincipalKind::Human, p, tenant, project, now)
        };
        assert_eq!(
            check(&[r.clone()], Permission::Approver, "t1", "p1"),
            Ok(())
        );
        let missing = Err(ApprovalCode::PermissionMissing);
        assert_eq!(
            check(&[r.clone()], Permission::SignoffAuthority, "t1", "p1"),
            missing
        );
        assert_eq!(
            check(&[r.clone()], Permission::Approver, "t2", "p1"),
            missing
        );
        assert_eq!(
            check(&[r.clone()], Permission::Approver, "t1", "p2"),
            missing
        );
        assert_eq!(
            check_permission(
                &[r.clone()],
                &sid("other-realm", "alice"),
                PrincipalKind::Human,
                Permission::Approver,
                "t1",
                "p1",
                now
            ),
            missing
        );
        r.revoked_at = Some(now);
        assert_eq!(
            check(&[r.clone()], Permission::Approver, "t1", "p1"),
            missing
        );
        r.revoked_at = None;
        r.granted_at = now.plus_seconds(1);
        assert_eq!(check(&[r], Permission::Approver, "t1", "p1"), missing);
    }

    #[test]
    fn wildcard_scopes_contain_only_what_was_explicitly_granted() {
        // X5 (S7): (*, *) everything; (t, *) every project of t; (t, p) itself.
        let yes = [
            ("*", "*", "t1", "p1"),
            ("*", "*", "*", "*"),
            ("*", "*", "t1", "*"),
            ("t1", "*", "t1", "p1"),
            ("t1", "*", "t1", "*"),
            ("t1", "p1", "t1", "p1"),
            ("*", "p1", "*", "p1"),
        ];
        let no = [
            ("t1", "*", "t2", "p1"),
            ("t1", "*", "*", "*"),
            ("t1", "p1", "t1", "p2"),
            ("t1", "p1", "t1", "*"),
            ("t1", "p1", "*", "*"),
            ("*", "p1", "t1", "p1"),
            ("*", "p1", "*", "*"),
            ("*", "p1", "*", "p2"),
        ];
        for (at, ap, t, p) in yes {
            assert!(
                scope_contains(at, ap, t, p),
                "({at},{ap}) contains ({t},{p})"
            );
        }
        for (at, ap, t, p) in no {
            assert!(
                !scope_contains(at, ap, t, p),
                "({at},{ap}) excludes ({t},{p})"
            );
        }
        // The rule is applied to every permission check.
        let now = t("2026-11-07T00:00:00Z");
        let alice = sid("writ-local", "alice");
        let mut r = role(alice.clone(), PrincipalKind::Human, Permission::Revoker);
        r.tenant_id = "*".into();
        r.project_id = "*".into();
        let check = |roles: &[RoleAssignment], tenant, project| {
            check_permission(
                roles,
                &alice,
                PrincipalKind::Human,
                Permission::Revoker,
                tenant,
                project,
                now,
            )
        };
        assert_eq!(check(&[r.clone()], "t9", "p9"), Ok(()));
        r.tenant_id = "t1".into();
        assert_eq!(check(&[r.clone()], "t1", "p9"), Ok(()));
        assert_eq!(
            check(&[r.clone()], "t2", "p9"),
            Err(ApprovalCode::PermissionMissing)
        );
        assert_eq!(check(&[r], "*", "*"), Err(ApprovalCode::PermissionMissing));
    }
}
