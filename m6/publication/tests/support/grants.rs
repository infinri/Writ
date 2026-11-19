//! An enrolled fixture human using the normal M4 ledger ceremonies for effect grants.

use std::{
    path::Path,
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc,
    },
};

use writ_broker::{
    approval_store::{
        ApprovalLedger, BootstrapEnrollment, CeremonyBegin, CeremonyFinish, CeremonyOp,
        CeremonyResult, NewEnrollment, NewRole, OperatorSession, RegistrationBegin,
        RegistrationFinish, RegistrationLedger,
    },
    clock::{observe, FixedClock, ObservedTime},
    grants::GrantLabel,
    human::{
        ActiveCredentials, ApprovalChannel, CeremonyStart, CeremonyState, HumanVerifier,
        Permission, PrincipalKind, RegistrationBinding, RegistrationStart, RegistrationVerifier,
        RelyingParty, SubjectId, VerifiedAssertion, VerifiedRegistration, VerifierCapability,
        VerifierError, ES256, WEBAUTHN_PASSKEY,
    },
};
use writ_kernel::json::Digest;
use writ_ledger_sqlite::{BootstrapSession, SqliteLedger};

use crate::support::{now, scope};

const SESSION: &str = "m6-lab-operator";
static NONCE: AtomicU64 = AtomicU64::new(0);

fn nonce() -> CeremonyState {
    let count = NONCE.fetch_add(1, Ordering::Relaxed);
    CeremonyState::from_bytes(format!("m6-lab-ceremony-{count}").into_bytes())
}

struct Verifier;

impl RegistrationVerifier for Verifier {
    fn relying_party(&self) -> RelyingParty {
        RelyingParty {
            rp_id: "localhost".into(),
            origin: "http://localhost:8443".into(),
        }
    }

    fn begin_registration(&self, _: &SubjectId) -> Result<RegistrationStart, VerifierError> {
        Ok(RegistrationStart {
            client_options: vec![],
            state: nonce(),
        })
    }

    fn finish_registration(
        &self,
        cap: &VerifierCapability,
        binding: &RegistrationBinding,
        _: &CeremonyState,
        response: &[u8],
    ) -> Result<VerifiedRegistration, VerifierError> {
        let credential = std::str::from_utf8(response).map_err(|_| VerifierError::Malformed)?;
        Ok(VerifiedRegistration::new(
            cap,
            binding,
            credential,
            response.to_vec(),
            ES256,
            true,
        ))
    }
}

impl HumanVerifier for Verifier {
    fn relying_party(&self) -> RelyingParty {
        RegistrationVerifier::relying_party(self)
    }

    fn begin(&self, _: &SubjectId, _: &ActiveCredentials) -> Result<CeremonyStart, VerifierError> {
        let state = nonce();
        Ok(CeremonyStart {
            client_options: vec![],
            challenge_digest: Digest::of_bytes(state.as_bytes()),
            state,
        })
    }

    fn finish(
        &self,
        cap: &VerifierCapability,
        state: &CeremonyState,
        response: &[u8],
        credentials: &ActiveCredentials,
    ) -> Result<VerifiedAssertion, VerifierError> {
        let credential = std::str::from_utf8(response).map_err(|_| VerifierError::Malformed)?;
        if !credentials.ids().iter().any(|id| id == credential) {
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

fn channel() -> ApprovalChannel {
    let verifier = Arc::new(Verifier);
    ApprovalChannel::new(verifier.clone(), verifier, "m6-lab-grants").unwrap()
}

fn human(name: &str) -> SubjectId {
    SubjectId::new("lab-grant-realm", name)
}

fn ceremony(
    mut ledger: &SqliteLedger,
    actor: &SubjectId,
    operation: &CeremonyOp,
    idempotency: &str,
) -> CeremonyResult {
    let clock = FixedClock::new(now());
    let time = observe(&clock, &mut ledger).unwrap();
    let issued = ledger
        .begin_ceremony(&CeremonyBegin {
            op: operation,
            subject: actor,
            idempotency_key: idempotency,
            now: time,
        })
        .unwrap()
        .unwrap();
    ledger
        .finish_ceremony(&CeremonyFinish {
            op: operation,
            challenge_id: &issued.challenge_id,
            response: actor.subject.as_bytes(),
            idempotency_key: idempotency,
            now: time,
        })
        .unwrap()
        .unwrap()
        .result
}

pub fn fixture_ledger(path: &Path) -> SqliteLedger {
    drop(SqliteLedger::create(path).unwrap());
    let operator = human("fixture-operator");
    let ApproverRegistration {
        approver,
        credential,
        ledger,
    } = enrollable_approver(path, &operator);
    let scope = scope();
    let enrolled = ceremony(
        &ledger,
        &operator,
        &CeremonyOp::Enroll {
            enrollment: NewEnrollment {
                subject: approver.clone(),
                kind: PrincipalKind::Human,
                credential: Some(credential),
                linked_identities: vec![],
                roles: vec![NewRole {
                    permission: Permission::Approver,
                    tenant_id: scope.tenant().into(),
                    project_id: scope.project().into(),
                }],
            },
            operator_session: SESSION.into(),
        },
        "m6-enroll-approver",
    );
    assert_eq!(enrolled, CeremonyResult::Enrolled { subject: approver });
    ledger
}

struct ApproverRegistration {
    approver: SubjectId,
    credential: VerifiedRegistration,
    ledger: SqliteLedger,
}

fn enrollable_approver(path: &Path, operator: &SubjectId) -> ApproverRegistration {
    let bootstrap = BootstrapSession::open(path)
        .unwrap()
        .unwrap()
        .with_approval_channel(channel())
        .unwrap();
    let clock = FixedClock::new(now());
    let time = bootstrap.observe(&clock).unwrap();
    let initial = bootstrap
        .begin_registration(&RegistrationBegin {
            subject: operator,
            operator: None,
            now: time,
        })
        .unwrap()
        .unwrap();
    let credential = register(&bootstrap, &initial.registration_id, operator, None, time);
    bootstrap
        .bootstrap(
            &BootstrapEnrollment {
                subject: operator.clone(),
                credential,
                linked_identities: vec![],
            },
            time,
        )
        .unwrap()
        .unwrap();

    let ledger = SqliteLedger::open(path)
        .unwrap()
        .with_approval_channel(channel())
        .unwrap();
    let approver = human("fixture-effect-approver");
    let session = OperatorSession {
        operator: operator.clone(),
        session_id: SESSION.into(),
    };
    let CeremonyResult::RegistrationStarted { registration } = ceremony(
        &ledger,
        operator,
        &CeremonyOp::RegistrationStart {
            subject: approver.clone(),
            operator: operator.clone(),
            operator_session: SESSION.into(),
        },
        "m6-approver-registration",
    ) else {
        panic!("registration ceremony must create a row");
    };
    let time = observe(&clock, &mut &ledger).unwrap();
    let credential = register(
        &ledger,
        &registration.registration_id,
        &approver,
        Some(&session),
        time,
    );
    ApproverRegistration {
        approver,
        credential,
        ledger,
    }
}

fn register(
    ledger: &dyn RegistrationLedger,
    registration_id: &str,
    human: &SubjectId,
    operator: Option<&OperatorSession>,
    now: ObservedTime,
) -> VerifiedRegistration {
    ledger
        .finish_registration(&RegistrationFinish {
            registration_id,
            response: human.subject.as_bytes(),
            operator,
            now,
        })
        .unwrap()
        .unwrap()
}

pub fn approve(ledger: &SqliteLedger, request_id: &str) -> GrantLabel {
    let outcome = ceremony(
        ledger,
        &human("fixture-effect-approver"),
        &CeremonyOp::Approve {
            request_id: request_id.into(),
        },
        &format!("m6-approve-{request_id}"),
    );
    let CeremonyResult::Approved { grant } = outcome else {
        panic!("approval ceremony must issue a grant");
    };
    grant
}
