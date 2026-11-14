//! Human subjects, roles and the verifier boundary (M4-01; D-040 O-1, O-7,
//! O-8; D-041; M4-INTERFACES I-1).
//!
//! Storage-independent: no network, HTTP, WebAuthn library, randomness or SQL.
//! The production [`HumanVerifier`] lives in the `writ-approval` crate; test
//! implementations live only in non-shipping test code.
//!
//! **Subjects** are identified by [`SubjectId`] (stable issuer or realm plus
//! subject), never by a display name. Every subject has a [`PrincipalKind`].
//!
//! **Separation** ([`check_separation`], [`approval_separation`],
//! [`signoff_separation`]): the acting subject must differ from every excluded
//! party. Comparison is fail closed: two subjects are the same when their
//! [`SubjectId`]s are equal *or* their `subject` texts are equal under
//! different issuers ([`same_subject`]). The requester principal of a sealed
//! request is named by the ingress adapter's realm, not the human realm, so
//! an exact issuer comparison alone would let the same person approve under a
//! second realm. A false refusal is the safe direction (O-7: separation is
//! never weakened).
//!
//! **Services** (O-8) may hold only [`Permission::Operator`]; never approver,
//! signoff authority or revoker ([`check_service_permission`]).

use std::fmt;

use writ_kernel::json::Digest;
use writ_kernel::time::Timestamp;

use crate::approval::ApprovalCode;

/// The only authentication method of the local profile (D-040 O-1).
pub const WEBAUTHN_PASSKEY: &str = "webauthn-passkey";

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
}

/// What kind of principal a subject is.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum PrincipalKind {
    /// A human.
    Human,
    /// A service or automation identity (O-8).
    Service,
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

/// Opaque server-side ceremony state: persisted only in the ledger, never
/// sent to or accepted from the client (D-041). `Debug` never prints the
/// contents.
#[derive(Clone, PartialEq, Eq)]
pub struct CeremonyState(Vec<u8>);

impl CeremonyState {
    /// Wrap verifier-produced bytes.
    pub fn from_bytes(bytes: Vec<u8>) -> CeremonyState {
        CeremonyState(bytes)
    }

    /// The bytes (for the ledger only).
    pub fn as_bytes(&self) -> &[u8] {
        &self.0
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

/// A verified user-verified assertion.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct VerifiedAssertion {
    /// The authenticated subject.
    pub subject: SubjectId,
    /// The credential that signed.
    pub credential_id: String,
    /// User verification; required to be `true`.
    pub user_verified: bool,
    /// Authentication method; [`WEBAUTHN_PASSKEY`].
    pub method: String,
    /// Credential state to persist with the decision, if any.
    pub credential_update: Option<CredentialUpdate>,
}

impl VerifiedAssertion {
    /// Broker-side acceptance of a verifier's answer: user verification,
    /// method, the challenge's subject and one of its allowed credentials,
    /// and a credential update only for the signing credential.
    pub fn check(&self, subject: &SubjectId, allowed: &[String]) -> Result<(), ApprovalCode> {
        let refuse = ApprovalCode::AssertionInvalid(VerifierError::Rejected);
        if !self.user_verified {
            return Err(ApprovalCode::AssertionInvalid(
                VerifierError::UserNotVerified,
            ));
        }
        if self.method != WEBAUTHN_PASSKEY
            || &self.subject != subject
            || !allowed.contains(&self.credential_id)
        {
            return Err(refuse);
        }
        if self
            .credential_update
            .as_ref()
            .is_some_and(|u| u.credential_id != self.credential_id)
        {
            return Err(refuse);
        }
        Ok(())
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

/// The human verifier boundary (implemented in `writ-approval`). Only active
/// credentials are passed in.
pub trait HumanVerifier {
    /// Start a ceremony for `subject` over its active `credentials`.
    fn begin(
        &self,
        subject: &SubjectId,
        credentials: &[StoredCredential],
    ) -> Result<CeremonyStart, VerifierError>;

    /// Finish a ceremony from the ledger-held `state` and the client `response`.
    fn finish(
        &self,
        state: &CeremonyState,
        response: &[u8],
        credentials: &[StoredCredential],
    ) -> Result<VerifiedAssertion, VerifierError>;
}

/// Same subject, fail closed: equal ids, or equal subject text under any issuer.
pub fn same_subject(a: &SubjectId, b: &SubjectId) -> bool {
    a == b || a.subject == b.subject
}

/// `SEPARATION_VIOLATED` when `actor` is any of `excluded`.
pub fn check_separation<'a>(
    actor: &SubjectId,
    excluded: impl IntoIterator<Item = &'a SubjectId>,
) -> Result<(), ApprovalCode> {
    if excluded.into_iter().any(|x| same_subject(actor, x)) {
        return Err(ApprovalCode::SeparationViolated);
    }
    Ok(())
}

/// The parties of a request that may never approve or sign off for it.
#[derive(Clone, Copy, Debug)]
pub struct Parties<'a> {
    /// Requester principal.
    pub requester: &'a SubjectId,
    /// Agent, if any.
    pub agent: Option<&'a SubjectId>,
    /// Every member of the trusted delegation chain.
    pub delegation_chain: &'a [SubjectId],
}

impl<'a> Parties<'a> {
    fn all(&self) -> impl Iterator<Item = &'a SubjectId> {
        std::iter::once(self.requester)
            .chain(self.agent)
            .chain(self.delegation_chain.iter())
    }
}

/// Approval separation (O-7): not the requester, agent or a delegation-chain member.
pub fn approval_separation(
    approver: &SubjectId,
    parties: &Parties<'_>,
) -> Result<(), ApprovalCode> {
    check_separation(approver, parties.all())
}

/// Signoff separation (O-7): as approval, and not a subject who changed the
/// artifact or the acceptance policy for the revision being accepted.
pub fn signoff_separation(
    signer: &SubjectId,
    parties: &Parties<'_>,
    changers: &[SubjectId],
) -> Result<(), ApprovalCode> {
    check_separation(signer, parties.all().chain(changers.iter()))
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

/// The subject holds `permission` for (tenant, project) at `now`: the
/// service rule first (`SERVICE_NOT_PERMITTED`), then an active assignment of
/// the same subject, kind, permission and exact scope (`PERMISSION_MISSING`).
/// An assignment that breaks the service rule never counts.
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
            && a.tenant_id == tenant_id
            && a.project_id == project_id
            && a.active_at(now)
            && check_service_permission(a.kind, a.permission).is_ok()
    });
    if !held {
        return Err(ApprovalCode::PermissionMissing);
    }
    Ok(())
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
    fn separation_covers_requester_agent_and_every_delegation_member() {
        let requester = sid("adapter-claude", "alice");
        let agent = sid("agent-realm", "agent-7");
        let chain = [
            sid("agent-realm", "parent-1"),
            sid("agent-realm", "parent-2"),
        ];
        let parties = Parties {
            requester: &requester,
            agent: Some(&agent),
            delegation_chain: &chain,
        };
        for bad in [&requester, &agent, &chain[0], &chain[1]] {
            assert_eq!(
                approval_separation(bad, &parties),
                Err(ApprovalCode::SeparationViolated)
            );
        }
        // The same subject under the human realm is still the requester (fail closed).
        assert_eq!(
            approval_separation(&sid("writ-local", "alice"), &parties),
            Err(ApprovalCode::SeparationViolated)
        );
        assert_eq!(
            approval_separation(&sid("writ-local", "parent-2"), &parties),
            Err(ApprovalCode::SeparationViolated)
        );
        assert_eq!(
            approval_separation(&sid("writ-local", "bob"), &parties),
            Ok(())
        );
        let no_agent = Parties {
            requester: &requester,
            agent: None,
            delegation_chain: &[],
        };
        assert_eq!(
            approval_separation(&sid("writ-local", "bob"), &no_agent),
            Ok(())
        );
    }

    #[test]
    fn signoff_separation_also_excludes_artifact_and_policy_changers() {
        let requester = sid("adapter", "alice");
        let chain = [sid("agent-realm", "parent-1")];
        let parties = Parties {
            requester: &requester,
            agent: None,
            delegation_chain: &chain,
        };
        let changers = [sid("writ-local", "carol"), sid("writ-local", "dave")];
        let bob = sid("writ-local", "bob");
        assert_eq!(signoff_separation(&bob, &parties, &changers), Ok(()));
        for bad in [&changers[0], &changers[1], &chain[0], &requester] {
            assert_eq!(
                signoff_separation(bad, &parties, &changers),
                Err(ApprovalCode::SeparationViolated)
            );
        }
        // An approver who changed the artifact is not excluded from approval.
        assert_eq!(approval_separation(&changers[0], &parties), Ok(()));
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
    fn assertions_need_user_verification_subject_and_allowed_credential() {
        let alice = sid("writ-local", "alice");
        let allowed = vec!["cred-1".to_string()];
        let good = VerifiedAssertion {
            subject: alice.clone(),
            credential_id: "cred-1".into(),
            user_verified: true,
            method: WEBAUTHN_PASSKEY.into(),
            credential_update: Some(CredentialUpdate {
                credential_id: "cred-1".into(),
                public_material: vec![1],
            }),
        };
        assert_eq!(good.check(&alice, &allowed), Ok(()));
        let refused = |a: &VerifiedAssertion| a.check(&alice, &allowed).is_err();
        let mut a = good.clone();
        a.user_verified = false;
        assert_eq!(
            a.check(&alice, &allowed),
            Err(ApprovalCode::AssertionInvalid(
                VerifierError::UserNotVerified
            ))
        );
        let mut a = good.clone();
        a.method = "password".into();
        assert!(refused(&a));
        let mut a = good.clone();
        a.subject = sid("writ-local", "bob");
        assert!(refused(&a));
        let mut a = good.clone();
        a.credential_id = "cred-2".into();
        assert!(refused(&a));
        let mut a = good;
        a.credential_update = Some(CredentialUpdate {
            credential_id: "cred-2".into(),
            public_material: vec![],
        });
        assert!(refused(&a));
    }
}
