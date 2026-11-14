//! The WebAuthn passkey verifier (M4-02; D-040 O-1; D-041): `webauthn-rs`'s safe passkey
//! APIs only (user verification required by the library's passkey flows), no handwritten
//! cryptography or protocol checks.
//!
//! - **Authentication** ([`HumanVerifier`]): `start_passkey_authentication` over the active
//!   credentials' stored passkeys; the library's `PasskeyAuthentication` state is serialized
//!   (feature `danger-allow-state-serialisation`) into [`CeremonyState`], which the ledger
//!   stores only in its challenge row and never sends to the client; the challenge digest is
//!   the digest of those state bytes, so an assertion verified against a state identifies
//!   exactly that challenge row. `finish_passkey_authentication` verifies challenge, exact
//!   origin, RP id hash, credential binding (the credential must be one of the passed
//!   ones), signature, user presence and verification, and the counter and backup flags as
//!   the library defines them (synchronized passkeys with counter 0 are accepted; a counter
//!   that does not increase over a nonzero stored one is refused). A changed counter or
//!   backup state comes back as a [`CredentialUpdate`] for the ledger to persist with the
//!   decision.
//! - **Registration** ([`RegistrationVerifier`]): `start_passkey_registration` and
//!   `finish_passkey_registration`; only an ES256 credential is reported as `ES256` (D-041
//!   DR-3: the ledger refuses anything else).
//! - Library errors map to [`VerifierError`] categories (`ASSERTION_INVALID`).

use webauthn_rs::prelude::{
    COSEAlgorithm, CredentialID, Passkey, PasskeyAuthentication, PasskeyRegistration,
    PublicKeyCredential, RegisterPublicKeyCredential, Url, Uuid, WebauthnError,
};
use webauthn_rs::{Webauthn, WebauthnBuilder};
use writ_broker::human::{
    ActiveCredentials, CeremonyStart, CeremonyState, CredentialUpdate, HumanVerifier,
    RegistrationBinding, RegistrationStart, RegistrationVerifier, RelyingParty, SubjectId,
    VerifiedAssertion, VerifiedRegistration, VerifierCapability, VerifierError, ES256,
    WEBAUTHN_PASSKEY,
};
use writ_broker::writ_kernel::json::Digest;

/// The production verifier for one relying party (RP id and exact origin).
pub struct PasskeyVerifier {
    webauthn: Webauthn,
    rp: RelyingParty,
}

impl PasskeyVerifier {
    /// The verifier for `listener`'s fixed RP id and origin.
    pub fn for_listener(
        listener: &crate::listener::ApprovalListener,
    ) -> Result<PasskeyVerifier, VerifierError> {
        PasskeyVerifier::new(listener.rp_id(), listener.origin())
    }

    /// A verifier for `rp_id` and exactly `origin` (no subdomains, no other port).
    pub fn new(rp_id: &str, origin: &str) -> Result<PasskeyVerifier, VerifierError> {
        let url = Url::parse(origin).map_err(|_| VerifierError::Unavailable)?;
        let webauthn = WebauthnBuilder::new(rp_id, &url)
            .and_then(|b| b.rp_name("Writ approval").build())
            .map_err(|_| VerifierError::Unavailable)?;
        Ok(PasskeyVerifier {
            webauthn,
            rp: RelyingParty {
                rp_id: rp_id.to_string(),
                origin: origin.to_string(),
            },
        })
    }
}

/// The library's error as a refusal category.
fn category(e: &WebauthnError) -> VerifierError {
    use WebauthnError::*;
    match e {
        InvalidClientDataType
        | MismatchedChallenge
        | ChallengeNotFound
        | InvalidRPOrigin
        | InvalidRPIDHash
        | CredentialCrossOrigin => VerifierError::BindingMismatch,
        UserNotPresent | UserNotVerified | InconsistentUserVerificationPolicy => {
            VerifierError::UserNotVerified
        }
        CredentialNotFound | CredentialExcludedFromRequest => VerifierError::CredentialUnknown,
        CredentialPossibleCompromise
        | CredentialCounterUpdateFailure
        | CredentialBackupEligibilityInconsistent
        | CredentialMayNotBeHardwareBound => VerifierError::CredentialStateRejected,
        ParseBase64Failure(_)
        | ParseCBORFailure(_)
        | ParseJSONFailure(_)
        | ParseNOMFailure
        | ParseInsufficientBytesAvailable => VerifierError::Malformed,
        Configuration => VerifierError::Unavailable,
        _ => VerifierError::SignatureInvalid,
    }
}

/// A credential id as stored (lowercase hex of the raw id).
fn credential_text(id: &CredentialID) -> Result<String, VerifierError> {
    if id.is_empty() {
        return Err(VerifierError::Malformed);
    }
    Ok(id.iter().map(|b| format!("{b:02x}")).collect())
}

fn passkeys(credentials: &ActiveCredentials) -> Result<Vec<(String, Passkey)>, VerifierError> {
    credentials
        .as_slice()
        .iter()
        .map(|c| {
            serde_json::from_slice::<Passkey>(&c.public_material)
                .map(|p| (c.credential_id.clone(), p))
                .map_err(|_| VerifierError::Malformed)
        })
        .collect()
}

impl HumanVerifier for PasskeyVerifier {
    fn begin(
        &self,
        _subject: &SubjectId,
        credentials: &ActiveCredentials,
    ) -> Result<CeremonyStart, VerifierError> {
        let keys: Vec<Passkey> = passkeys(credentials)?.into_iter().map(|(_, p)| p).collect();
        if keys.is_empty() {
            return Err(VerifierError::CredentialUnknown);
        }
        let (options, state) = self
            .webauthn
            .start_passkey_authentication(&keys)
            .map_err(|e| category(&e))?;
        let state = serde_json::to_vec(&state).map_err(|_| VerifierError::Unavailable)?;
        let client_options =
            serde_json::to_vec(&options).map_err(|_| VerifierError::Unavailable)?;
        Ok(CeremonyStart {
            client_options,
            challenge_digest: Digest::of_bytes(&state),
            state: CeremonyState::from_bytes(state),
        })
    }

    fn finish(
        &self,
        capability: &VerifierCapability,
        state: &CeremonyState,
        response: &[u8],
        credentials: &ActiveCredentials,
    ) -> Result<VerifiedAssertion, VerifierError> {
        let checked = self.verify_assertion(state, response, credentials)?;
        Ok(VerifiedAssertion::new(
            capability,
            credentials.subject().clone(),
            &checked.credential_id,
            true,
            WEBAUTHN_PASSKEY,
            Digest::of_bytes(state.as_bytes()),
            checked.credential_update,
        ))
    }
}

/// What the library verified for one assertion (no trusted value: the broker accepts
/// only [`VerifiedAssertion`]s checked against their stored challenge).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CheckedAssertion {
    /// The signing credential (one of those passed in).
    pub credential_id: String,
    /// Its updated library-serialized passkey (counter or backup state changed), if any.
    pub credential_update: Option<CredentialUpdate>,
}

impl PasskeyVerifier {
    /// The library's verification of `response` against the ledger-held `state` over
    /// `credentials` (module documentation); [`HumanVerifier::finish`] builds its answer
    /// from this.
    pub fn verify_assertion(
        &self,
        state: &CeremonyState,
        response: &[u8],
        credentials: &ActiveCredentials,
    ) -> Result<CheckedAssertion, VerifierError> {
        let auth: PasskeyAuthentication =
            serde_json::from_slice(state.as_bytes()).map_err(|_| VerifierError::Malformed)?;
        let pkc: PublicKeyCredential =
            serde_json::from_slice(response).map_err(|_| VerifierError::Malformed)?;
        let result = self
            .webauthn
            .finish_passkey_authentication(&pkc, &auth)
            .map_err(|e| category(&e))?;
        if !result.user_verified() {
            return Err(VerifierError::UserNotVerified);
        }
        let (id, mut key) = passkeys(credentials)?
            .into_iter()
            .find(|(_, p)| p.cred_id() == result.cred_id())
            .ok_or(VerifierError::CredentialUnknown)?;
        let update = match key.update_credential(&result) {
            Some(true) => Some(CredentialUpdate {
                credential_id: id.clone(),
                public_material: serde_json::to_vec(&key)
                    .map_err(|_| VerifierError::Unavailable)?,
            }),
            _ => None,
        };
        Ok(CheckedAssertion {
            credential_id: id,
            credential_update: update,
        })
    }
}

impl RegistrationVerifier for PasskeyVerifier {
    fn relying_party(&self) -> RelyingParty {
        self.rp.clone()
    }

    fn begin_registration(&self, subject: &SubjectId) -> Result<RegistrationStart, VerifierError> {
        let name = format!("{}/{}", subject.issuer, subject.subject);
        let (options, state) = self
            .webauthn
            .start_passkey_registration(Uuid::new_v4(), &name, &name, None)
            .map_err(|e| category(&e))?;
        Ok(RegistrationStart {
            client_options: serde_json::to_vec(&options).map_err(|_| VerifierError::Unavailable)?,
            state: CeremonyState::from_bytes(
                serde_json::to_vec(&state).map_err(|_| VerifierError::Unavailable)?,
            ),
        })
    }

    fn finish_registration(
        &self,
        capability: &VerifierCapability,
        binding: &RegistrationBinding,
        state: &CeremonyState,
        response: &[u8],
    ) -> Result<VerifiedRegistration, VerifierError> {
        if binding.relying_party != self.rp {
            return Err(VerifierError::BindingMismatch);
        }
        let reg: PasskeyRegistration =
            serde_json::from_slice(state.as_bytes()).map_err(|_| VerifierError::Malformed)?;
        let rpkc: RegisterPublicKeyCredential =
            serde_json::from_slice(response).map_err(|_| VerifierError::Malformed)?;
        let key = self
            .webauthn
            .finish_passkey_registration(&rpkc, &reg)
            .map_err(|e| category(&e))?;
        let algorithm = match key.cred_algorithm() {
            COSEAlgorithm::ES256 => ES256.to_string(),
            other => format!("{other:?}"),
        };
        Ok(VerifiedRegistration::new(
            capability,
            binding,
            &credential_text(key.cred_id())?,
            serde_json::to_vec(&key).map_err(|_| VerifierError::Unavailable)?,
            &algorithm,
            // The library's passkey registration requires user verification.
            true,
        ))
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;
    use webauthn_authenticator_rs::softpasskey::SoftPasskey;
    use webauthn_authenticator_rs::WebauthnAuthenticator;
    use webauthn_rs::prelude::{CreationChallengeResponse, RequestChallengeResponse};
    use writ_broker::human::{
        ApprovalChannel, CredentialStatus, Permission, PrincipalKind, RegistrationOperation,
        RoleAssignment, StoredCredential, SubjectRecord, SubjectStatus,
    };
    use writ_broker::writ_kernel::time::Timestamp;

    const ORIGIN: &str = "http://localhost:8443";

    /// The production verifier for `rp_id` and `origin` as a configured approval channel
    /// (the only caller that can lend it a verifier capability, M1).
    fn channel(rp_id: &str, origin: &str) -> ApprovalChannel {
        let v = std::sync::Arc::new(PasskeyVerifier::new(rp_id, origin).unwrap());
        ApprovalChannel::new(v.clone(), v, "approval-listener").unwrap()
    }

    fn t() -> Timestamp {
        Timestamp::parse_strict("2026-11-14T10:00:00Z").unwrap()
    }

    fn bob() -> SubjectId {
        SubjectId::new("writ-local", "bob")
    }

    fn register(v: &ApprovalChannel, auth: &mut SoftPasskey) -> VerifiedRegistration {
        let start = v.begin_registration(&bob()).unwrap();
        let ccr: CreationChallengeResponse = serde_json::from_slice(&start.client_options).unwrap();
        let rpkc = auth
            .do_registration(Url::parse(ORIGIN).unwrap(), ccr)
            .unwrap();
        let binding = RegistrationBinding {
            registration_id: "reg-1".into(),
            operation: RegistrationOperation::Enroll,
            subject: bob(),
            relying_party: v.relying_party().clone(),
        };
        v.finish_registration(&binding, &start.state, &serde_json::to_vec(&rpkc).unwrap())
            .unwrap()
    }

    fn active(reg: &VerifiedRegistration) -> ActiveCredentials {
        let record = SubjectRecord {
            subject: bob(),
            kind: PrincipalKind::Human,
            status: SubjectStatus::Active,
        };
        let role = RoleAssignment {
            assignment_id: "r".into(),
            subject: bob(),
            kind: PrincipalKind::Human,
            permission: Permission::Approver,
            tenant_id: "t".into(),
            project_id: "p".into(),
            granted_at: t(),
            revoked_at: None,
        };
        let stored = StoredCredential {
            credential_id: reg.credential_id().into(),
            subject: bob(),
            public_material: reg.public_material().to_vec(),
            status: CredentialStatus::Active,
            enrolled_at: t(),
            enrolled_by: SubjectId::new("writ-local", "root"),
        };
        ActiveCredentials::select(
            &record,
            [stored],
            &[role],
            Permission::Approver,
            "t",
            "p",
            t(),
        )
        .unwrap()
    }

    fn assert_once(
        v: &ApprovalChannel,
        auth: &mut SoftPasskey,
        creds: &ActiveCredentials,
        origin: &str,
    ) -> (CeremonyStart, Vec<u8>) {
        let start = v.begin(&bob(), creds).unwrap();
        let rcr: RequestChallengeResponse = serde_json::from_slice(&start.client_options).unwrap();
        let pkc = auth
            .do_authentication(Url::parse(origin).unwrap(), rcr)
            .unwrap();
        let response = serde_json::to_vec(&pkc).unwrap();
        (start, response)
    }

    #[test]
    fn softpasskey_registers_es256_and_authenticates_through_the_real_verifier() {
        let v = channel("localhost", ORIGIN);
        let mut auth = SoftPasskey::new(true);
        let reg = register(&v, &mut auth);
        assert_eq!(reg.algorithm(), ES256);
        assert!(reg.user_verified());
        let creds = active(&reg);
        let (start, response) = assert_once(&v, &mut auth, &creds, ORIGIN);
        // The state never appears in what the page receives.
        let options = String::from_utf8(start.client_options.clone()).unwrap();
        assert!(!options.contains("ar\":"), "{options}");
        let a = v.finish(&start.state, &response, &creds).unwrap();
        assert_eq!(a.subject(), &bob());
        assert_eq!(a.credential_id(), reg.credential_id());
        assert_eq!(a.challenge_digest(), &start.challenge_digest);
        // Replay against another ceremony's state: refused by the library.
        let (other, _) = assert_once(&v, &mut auth, &creds, ORIGIN);
        assert_eq!(
            v.finish(&other.state, &response, &creds).err(),
            Some(VerifierError::BindingMismatch)
        );
        // A forged signature (one byte changed).
        let mut pkc: PublicKeyCredential = serde_json::from_slice(&response).unwrap();
        let mut sig: Vec<u8> = pkc.response.signature.clone();
        let last = sig.len() - 1;
        sig[last] ^= 1;
        pkc.response.signature = sig;
        let forged = serde_json::to_vec(&pkc).unwrap();
        assert!(v.finish(&start.state, &forged, &creds).is_err());
        // Malformed state or response.
        assert_eq!(
            v.finish(&CeremonyState::from_bytes(b"x".to_vec()), &response, &creds)
                .err(),
            Some(VerifierError::Malformed)
        );
        assert_eq!(
            v.finish(&start.state, b"{}", &creds).err(),
            Some(VerifierError::Malformed)
        );
    }

    #[test]
    fn wrong_origin_or_rp_id_is_refused() {
        let v = channel("localhost", ORIGIN);
        let mut auth = SoftPasskey::new(true);
        let reg = register(&v, &mut auth);
        let creds = active(&reg);
        // The authenticator signs for another origin (the browser would report it).
        let start = v.begin(&bob(), &creds).unwrap();
        let rcr: RequestChallengeResponse = serde_json::from_slice(&start.client_options).unwrap();
        let other = auth
            .do_authentication(Url::parse("http://localhost:9999").unwrap(), rcr)
            .unwrap();
        assert_eq!(
            v.finish(&start.state, &serde_json::to_vec(&other).unwrap(), &creds)
                .err(),
            Some(VerifierError::BindingMismatch)
        );
        // A verifier for another RP id cannot use the credential's assertion.
        let other_rp = channel("example.localhost", "http://example.localhost:8443");
        let (start, response) = assert_once(&v, &mut auth, &creds, ORIGIN);
        assert!(other_rp.finish(&start.state, &response, &creds).is_err());
    }

    #[test]
    fn the_verifier_is_the_ledger_approval_channel() {
        let v = channel("localhost", ORIGIN);
        assert_eq!(v.relying_party().origin, ORIGIN);
        assert_eq!(v.relying_party().rp_id, "localhost");
        // Only an exact, parseable origin configures a verifier.
        assert!(PasskeyVerifier::new("localhost", "not a url").is_err());
    }

    #[test]
    fn missing_user_verification_never_registers() {
        let v = channel("localhost", ORIGIN);
        let mut auth = SoftPasskey::new(false);
        let start = v.begin_registration(&bob()).unwrap();
        let ccr: CreationChallengeResponse = serde_json::from_slice(&start.client_options).unwrap();
        // The soft authenticator refuses or produces an unverified credential; neither registers.
        if let Ok(rpkc) = auth.do_registration(Url::parse(ORIGIN).unwrap(), ccr) {
            let binding = RegistrationBinding {
                registration_id: "reg-1".into(),
                operation: RegistrationOperation::Enroll,
                subject: bob(),
                relying_party: v.relying_party().clone(),
            };
            assert_eq!(
                v.finish_registration(&binding, &start.state, &serde_json::to_vec(&rpkc).unwrap())
                    .err(),
                Some(VerifierError::UserNotVerified)
            );
        }
    }
}
