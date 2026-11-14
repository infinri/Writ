//! Harness signoffs for the carried transition cases (M4-11; D-040 O-1, O-4, O-7; D-042;
//! M4-INTERFACES I-5, X7). NON-SHIPPING test infrastructure (a Cargo example): the
//! signoff a `/2` record names is issued on the production approval-ledger ceremony
//! (`ApprovalLedger::begin_ceremony` and `finish_ceremony`: challenge, assertion check
//! against the stored challenge, separation from the exclusions the ledger reads, role,
//! single use) by a human reviewer enrolled through the production bootstrap and
//! enrollment ceremony, with the user verification answered by [`TestVerifier`], a test
//! verifier. The verifier is test-only: it establishes no human authentication (the
//! production verifier is `writ-approval`, D-041) and is never reachable from a release
//! build. Production promotion then verifies and consumes the signoff at use time through
//! `check_use`; nothing here switches, removes or reclassifies the human criterion.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use writ_broker::approval::NewSignoff;
use writ_broker::approval_store::{
    ApprovalLedger, CeremonyBegin, CeremonyFinish, CeremonyOp, NewEnrollment, NewRole,
    SignoffParties,
};
use writ_broker::clock::ObservedTime;
use writ_broker::human::{
    ActiveCredentials, ApprovalChannel, CeremonyStart, CeremonyState, HumanVerifier, Permission,
    PrincipalKind, SubjectId, VerifiedAssertion, VerifierCapability, VerifierError,
    WEBAUTHN_PASSKEY,
};
use writ_broker::writ_kernel::json::Digest;
use writ_ledger_sqlite::SqliteLedger;

use crate::common::{register, Registrar};

/// Adaptation label of a case whose signoff was issued through the test verifier.
pub const SIGNOFF_LABEL: &str = "M4-test-verifier-signoff";
/// The test approval realm of the harness's human subjects.
const REALM: &str = "m3c-test-approval-realm";
const ADMINISTRATOR: &str = "m3c-administrator";
const REVIEWER: &str = "m3c-signoff-reviewer";

fn subject(name: &str) -> SubjectId {
    SubjectId::new(REALM, name)
}

fn credential(name: &str) -> String {
    format!("m3c-cred-{name}")
}

/// Test verifier (non-shipping): every ceremony gets a distinct server-side state; the
/// only accepted response is `ok:<credential id>` of an active credential, answered as a
/// user-verified passkey assertion over that ceremony's challenge digest.
#[derive(Default)]
pub struct TestVerifier {
    next: AtomicU64,
}

impl HumanVerifier for TestVerifier {
    fn relying_party(&self) -> writ_broker::human::RelyingParty {
        writ_broker::human::RelyingParty {
            rp_id: "localhost".into(),
            origin: "http://localhost:8443".into(),
        }
    }

    fn begin(
        &self,
        _subject: &SubjectId,
        _credentials: &ActiveCredentials,
    ) -> Result<CeremonyStart, VerifierError> {
        let n = self.next.fetch_add(1, Ordering::SeqCst);
        let state = format!("m3c-signoff-ceremony-{n}").into_bytes();
        Ok(CeremonyStart {
            client_options: Vec::new(),
            challenge_digest: Digest::of_bytes(&state),
            state: CeremonyState::from_bytes(state),
        })
    }

    fn finish(
        &self,
        cap: &VerifierCapability,
        state: &CeremonyState,
        response: &[u8],
        credentials: &ActiveCredentials,
    ) -> Result<VerifiedAssertion, VerifierError> {
        let text = std::str::from_utf8(response).map_err(|_| VerifierError::Malformed)?;
        let credential = text
            .strip_prefix("ok:")
            .ok_or(VerifierError::SignatureInvalid)?;
        if !credentials.ids().iter().any(|c| c == credential) {
            return Err(VerifierError::CredentialUnknown);
        }
        Ok(VerifiedAssertion::new(
            cap,
            credentials.subject().clone(),
            credential,
            true,
            WEBAUTHN_PASSKEY,
            Digest::of_bytes(state.as_bytes()),
            None,
        ))
    }
}

/// The approval channel the transition harness configures on its ledger (M1): this test
/// verifier and the harness's test registrar, under the harness's channel identity.
pub fn channel() -> Result<ApprovalChannel, String> {
    ApprovalChannel::new(
        Arc::new(TestVerifier::default()),
        Arc::new(Registrar),
        "m3c-approval-listener",
    )
    .ok_or_else(|| "approval channel".to_string())
}

/// One user-verified ceremony of `who` for `op`.
fn ceremony(
    ledger: &SqliteLedger,
    now: ObservedTime,
    who: &str,
    op: &CeremonyOp,
    key: &str,
) -> Result<(), String> {
    let actor = subject(who);
    let issued = ledger
        .begin_ceremony(&CeremonyBegin {
            op,
            subject: &actor,
            idempotency_key: key,
            now,
        })
        .map_err(|f| format!("ceremony begin: {}", f.error))?
        .map_err(|r| format!("ceremony begin refused: {}", r.code()))?;
    let response = format!("ok:{}", credential(who));
    ledger
        .finish_ceremony(&CeremonyFinish {
            op,
            challenge_id: &issued.challenge_id,
            response: response.as_bytes(),
            idempotency_key: key,
            now,
        })
        .map_err(|f| format!("ceremony finish: {}", f.error))?
        .map_err(|r| format!("ceremony finish refused: {}", r.code()))?;
    Ok(())
}

/// The case's bootstrap administrator (bootstrapped at world creation through the
/// production bootstrap entry point) and its test credential id.
pub fn administrator() -> (SubjectId, String) {
    (subject(ADMINISTRATOR), credential(ADMINISTRATOR))
}

/// The signoff reviewer of (tenant, project), enrolled by the bootstrap administrator
/// through the production enrollment ceremony with a registered test credential (S5, X8).
/// The reviewer links no requester identity (an explicit, empty set, R1).
pub fn enroll_reviewer(
    ledger: &SqliteLedger,
    now: ObservedTime,
    tenant: &str,
    project: &str,
) -> Result<(), String> {
    let enrollment = NewEnrollment {
        subject: subject(REVIEWER),
        kind: PrincipalKind::Human,
        credential: Some(register(
            ledger,
            &|| Ok(now),
            &subject(REVIEWER),
            &credential(REVIEWER),
        )?),
        linked_identities: Vec::new(),
        roles: vec![NewRole {
            permission: Permission::SignoffAuthority,
            tenant_id: tenant.to_string(),
            project_id: project.to_string(),
        }],
    };
    ceremony(
        ledger,
        now,
        ADMINISTRATOR,
        &CeremonyOp::Enroll { enrollment },
        "m3c-enroll-reviewer",
    )
}

/// The reviewer signs `binding` (the ledger reads the exclusions itself; nothing added).
pub fn sign(ledger: &SqliteLedger, now: ObservedTime, binding: NewSignoff) -> Result<(), String> {
    let key = format!("m3c-signoff-{}", binding.signoff_id);
    let op = CeremonyOp::Signoff {
        binding: Box::new(binding),
        parties: SignoffParties::default(),
    };
    ceremony(ledger, now, REVIEWER, &op, &key)
}
