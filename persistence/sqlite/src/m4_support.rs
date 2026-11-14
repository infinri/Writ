//! Test support (test code only, declared `#[cfg(test)]`): the test human verifier and grant
//! issuance through the real M4 lifecycle on `writ-ledger-6` (D-042 X1). On this ledger the
//! M3 `approve` path is closed, so every test that needs an executable grant records the
//! approval request the broker would record, has an enrolled approver approve it through a
//! ceremony (`begin_ceremony`, `finish_ceremony`) and uses the ledger's own grant.
//! The verifier reaches the ledger only as its configured approval channel (M1); it builds
//! its answers with the capability the channel lends it for each call.

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::cell::RefCell;
use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;

use writ_broker::approval_store::{
    approval_request_for, ApprovalLedger, BootstrapEnrollment, CeremonyBegin, CeremonyFinish,
    CeremonyOp, CeremonyResult, NewEnrollment, NewRole, OperatorSession, RegistrationBegin,
    RegistrationFinish, RegistrationLedger,
};
use writ_broker::authorize::{authorize, UseTimeStatus};
use writ_broker::clock::ObservedTime;
use writ_broker::grants::{approval_grant_label, GrantIssued, GrantLabel, GrantService};
use writ_broker::human::{
    ActiveCredentials, ApprovalChannel, CeremonyStart, CeremonyState, CredentialUpdate,
    HumanVerifier, Permission, PrincipalKind, RegistrationBinding, RegistrationStart,
    RegistrationVerifier, RelyingParty, SubjectId, VerifiedAssertion, VerifiedRegistration,
    VerifierCapability, VerifierError, ES256, WEBAUTHN_PASSKEY,
};
use writ_broker::refusal::Refusal;
use writ_broker::store::ExecuteLedger;
use writ_broker::writ_kernel::json::{Digest, Value};

use super::grant_tests::{revision, verify};
use super::{BootstrapSession, SqliteLedger};

/// Responses are `<mode>:<credential id>`: `ok`, `nouv` (no user verification), `other`
/// (an assertion from another ceremony), `update` (with a credential update), `down` (the
/// verifier reports itself unavailable); anything else is a forged signature. `fixed`
/// makes every ceremony reuse one challenge (C1 test).
static NONCES: AtomicU64 = AtomicU64::new(0);

#[derive(Default)]
pub(crate) struct TestVerifier {
    pub(crate) next: AtomicU64,
    pub(crate) fixed: AtomicBool,
    /// Report another origin than [`ORIGIN`] (read once, when a channel is built).
    pub(crate) other_origin: AtomicBool,
}

/// The test relying party's origin.
pub(crate) const ORIGIN: &str = "http://localhost:8443";

/// The channel identity of the test approval channel.
pub(crate) const CHANNEL: &str = "approval-listener";

/// `v` as a configured approval channel.
pub(crate) fn channel_of(v: &Arc<TestVerifier>) -> ApprovalChannel {
    ApprovalChannel::new(v.clone(), v.clone(), CHANNEL).unwrap()
}

/// A fresh test verifier as a configured approval channel.
pub(crate) fn support_channel() -> ApprovalChannel {
    channel_of(&Arc::new(TestVerifier::default()))
}

/// Registration responses are `<mode>|<credential id>|<public material>`: `ok`, `nouv` (no
/// user verification), `rs256` (an RS256 credential), `other` (a result for another
/// registration row), `down` (the verifier reports itself unavailable); anything else is a
/// forged attestation.
impl RegistrationVerifier for TestVerifier {
    fn relying_party(&self) -> RelyingParty {
        let origin = if self.other_origin.load(Ordering::SeqCst) {
            "http://localhost:9999"
        } else {
            ORIGIN
        };
        RelyingParty {
            rp_id: "localhost".into(),
            origin: origin.into(),
        }
    }

    fn begin_registration(&self, _subject: &SubjectId) -> Result<RegistrationStart, VerifierError> {
        let n = NONCES.fetch_add(1, Ordering::SeqCst);
        Ok(RegistrationStart {
            client_options: b"creation-options".to_vec(),
            state: CeremonyState::from_bytes(format!("registration-nonce-{n}").into_bytes()),
        })
    }

    fn finish_registration(
        &self,
        cap: &VerifierCapability,
        binding: &RegistrationBinding,
        _state: &CeremonyState,
        response: &[u8],
    ) -> Result<VerifiedRegistration, VerifierError> {
        let text = std::str::from_utf8(response).map_err(|_| VerifierError::Malformed)?;
        let mut parts = text.splitn(3, '|');
        let (Some(mode), Some(credential), Some(material)) =
            (parts.next(), parts.next(), parts.next())
        else {
            return Err(VerifierError::Malformed);
        };
        let make = |b: &RegistrationBinding, algorithm: &str, uv: bool| {
            VerifiedRegistration::new(
                cap,
                b,
                credential,
                material.as_bytes().to_vec(),
                algorithm,
                uv,
            )
        };
        match mode {
            "ok" => Ok(make(binding, ES256, true)),
            "nouv" => Ok(make(binding, ES256, false)),
            "rs256" => Ok(make(binding, "RS256", true)),
            "other" => {
                let mut b = binding.clone();
                b.registration_id = "reg-another".into();
                Ok(make(&b, ES256, true))
            }
            "down" => Err(VerifierError::Unavailable),
            _ => Err(VerifierError::SignatureInvalid),
        }
    }
}

/// The operator page-session id test enrollments use (D-043 Y2).
pub(crate) const TEST_SESSION: &str = "test-operator-session";

/// `operator`'s test page session ([`TEST_SESSION`]).
pub(crate) fn operator_session(operator: &SubjectId) -> OperatorSession {
    OperatorSession {
        operator: operator.clone(),
        session_id: TEST_SESSION.into(),
    }
}

/// The `Enroll` operation of `e` in [`TEST_SESSION`].
pub(crate) fn enroll_op(e: NewEnrollment) -> CeremonyOp {
    CeremonyOp::Enroll {
        enrollment: e,
        operator_session: TEST_SESSION.into(),
    }
}

/// One registration ceremony on `ledger` (through its configured channel), in `operator`'s
/// [`TEST_SESSION`] (none for a bootstrap).
pub(crate) fn session_register(
    ledger: &dyn RegistrationLedger,
    now: &dyn Fn() -> ObservedTime,
    subject: &SubjectId,
    response: &str,
    operator: Option<&SubjectId>,
) -> Result<VerifiedRegistration, Refusal> {
    let session = operator.map(operator_session);
    let issued = ledger
        .begin_registration(&RegistrationBegin {
            subject,
            operator: session.as_ref(),
            now: now(),
        })
        .expect("store")?;
    ledger
        .finish_registration(&RegistrationFinish {
            registration_id: &issued.registration_id,
            response: response.as_bytes(),
            operator: session.as_ref(),
            now: now(),
        })
        .expect("store")
}

/// `e` with a credential `cred-<name>` registered through `ledger`'s `enroll` registration
/// ceremony (S5, X8) in `operator`'s [`TEST_SESSION`], when it is a human without one.
pub(crate) fn registered(
    ledger: &dyn RegistrationLedger,
    now: &dyn Fn() -> ObservedTime,
    operator: &SubjectId,
    mut e: NewEnrollment,
) -> NewEnrollment {
    if e.kind == PrincipalKind::Human && e.credential.is_none() {
        let name = &e.subject.subject;
        let response = format!("ok|cred-{name}|pk-{name}");
        e.credential =
            Some(session_register(ledger, now, &e.subject, &response, Some(operator)).unwrap());
    }
    e
}

/// Bootstrap `admin` (credential `cred-<subject>`) through the separate entry point (S5,
/// X8) on the closed ledger file at `path`, with `v` as the session's approval channel: an
/// exclusive session, its `bootstrap` registration, then the bootstrap enrollment.
pub(crate) fn bootstrap_root_at(
    path: &std::path::Path,
    clock: &dyn writ_broker::clock::EffectiveClock,
    v: &Arc<TestVerifier>,
    admin: &SubjectId,
) -> Result<i64, Refusal> {
    let s = BootstrapSession::open(path)
        .expect("store")?
        .with_approval_channel(channel_of(v))
        .expect("store");
    let response = format!("ok|{}|pk-{}", credential(admin), admin.subject);
    let reg = session_register(&s, &|| s.observe(clock).unwrap(), admin, &response, None)?;
    let now = s.observe(clock).unwrap();
    s.bootstrap(
        &BootstrapEnrollment {
            subject: admin.clone(),
            credential: reg,
            linked_identities: vec![],
        },
        now,
    )
    .expect("store")
}

impl HumanVerifier for TestVerifier {
    fn relying_party(&self) -> writ_broker::human::RelyingParty {
        writ_broker::human::RegistrationVerifier::relying_party(self)
    }

    fn begin(
        &self,
        _subject: &SubjectId,
        _credentials: &ActiveCredentials,
    ) -> Result<CeremonyStart, VerifierError> {
        // Unique per process (challenge digests are unique ledger-wide, C1), even across
        // verifier instances.
        let n = self.next.fetch_add(1, Ordering::SeqCst) + NONCES.fetch_add(1, Ordering::SeqCst);
        let state = if self.fixed.load(Ordering::SeqCst) {
            b"fixed-nonce".to_vec()
        } else {
            format!("nonce-{n}-{}", NONCES.fetch_add(1, Ordering::SeqCst)).into_bytes()
        };
        Ok(CeremonyStart {
            client_options: b"options".to_vec(),
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
        let (mode, credential) = text.split_once(':').ok_or(VerifierError::Malformed)?;
        if mode == "down" {
            return Err(VerifierError::Unavailable);
        }
        if !credentials.ids().iter().any(|c| c == credential) {
            return Err(VerifierError::CredentialUnknown);
        }
        let digest = Digest::of_bytes(state.as_bytes());
        let make = |uv: bool, digest: Digest, update: Option<CredentialUpdate>| {
            VerifiedAssertion::new(
                cap,
                credentials.subject().clone(),
                credential,
                uv,
                WEBAUTHN_PASSKEY,
                digest,
                update,
            )
        };
        match mode {
            "ok" => Ok(make(true, digest, None)),
            "nouv" => Ok(make(false, digest, None)),
            "other" => Ok(make(true, Digest::of_bytes(b"another ceremony"), None)),
            "update" => Ok(make(
                true,
                digest,
                Some(CredentialUpdate {
                    credential_id: credential.to_string(),
                    public_material: b"material-2".to_vec(),
                }),
            )),
            _ => Err(VerifierError::SignatureInvalid),
        }
    }
}

/// The bootstrap administrator of a support ledger.
pub(crate) fn support_root() -> SubjectId {
    SubjectId::new("writ-local", "support-root")
}

/// The approver every support ledger enrolls: `approver` over every scope.
pub(crate) fn support_approver() -> SubjectId {
    SubjectId::new("writ-local", "support-approver")
}

fn credential(s: &SubjectId) -> String {
    format!("cred-{}", s.subject)
}

fn ceremony(
    ledger: &SqliteLedger,
    who: &SubjectId,
    op: &CeremonyOp,
    key: &str,
    now: ObservedTime,
) -> Result<CeremonyResult, Refusal> {
    let c = ledger
        .begin_ceremony(&CeremonyBegin {
            op,
            subject: who,
            idempotency_key: key,
            now,
        })
        .map_err(|f| Refusal::LedgerUnavailable(f.error))??;
    let response = format!("ok:{}", credential(who));
    ledger
        .finish_ceremony(&CeremonyFinish {
            op,
            challenge_id: &c.challenge_id,
            response: response.as_bytes(),
            idempotency_key: key,
            now,
        })
        .map_err(|f| Refusal::LedgerUnavailable(f.error))?
        .map(|o| o.result)
}

/// Bootstrap [`support_root`] and enroll [`support_approver`] once per ledger (only in a
/// ledger with no subject yet). A support ledger without an approval channel gets a test
/// channel ([`support_channel`]).
pub(crate) fn ensure_approver(ledger: &SqliteLedger, now: ObservedTime) -> Result<(), Refusal> {
    ledger.approval_channel_for_tests(support_channel());
    let subjects: i64 = ledger
        .db()
        .conn
        .query_row("SELECT count(*) FROM subjects", [], |r| r.get(0))
        .unwrap();
    if subjects > 0 {
        return Ok(());
    }
    let root = support_root();
    // The support ledger is already exclusively owned by this test (production bootstraps
    // only through `BootstrapSession`); the bootstrap registration and enrollment still go
    // through the ledger's own registration row (S5, X8).
    ledger
        .bootstrap_owned_for_tests(
            &root,
            format!("ok|{}|pk-support-root", credential(&root)).as_bytes(),
            now,
        )
        .map_err(|f| Refusal::LedgerUnavailable(f.error))??;
    let approver = support_approver();
    let enrollment = registered(
        ledger,
        &|| now,
        &root,
        NewEnrollment {
            subject: approver.clone(),
            kind: PrincipalKind::Human,
            credential: None,
            linked_identities: vec![],
            roles: vec![NewRole {
                permission: Permission::Approver,
                tenant_id: "*".into(),
                project_id: "*".into(),
            }],
        },
    );
    ceremony(
        ledger,
        &root,
        &enroll_op(enrollment),
        "support-enroll-approver",
        now,
    )
    .map(|_| ())
}

thread_local! {
    /// Test aliases (`g1`) for the ledger-assigned grant labels (`approval/<request id>`)
    /// issued on this thread.
    static ALIASES: RefCell<HashMap<String, String>> = RefCell::new(HashMap::new());
}

/// The label an alias names on this thread (the text itself when it names none).
pub(crate) fn resolve(alias: &str) -> String {
    ALIASES.with(|a| {
        a.borrow()
            .get(alias)
            .cloned()
            .unwrap_or_else(|| alias.to_string())
    })
}

/// Record the approval request the broker records for `req` (its use-time decision must be
/// `require_approval`, otherwise `NOT_APPROVABLE` and nothing is stored) and make sure the
/// support approver is enrolled. Returns the request id and the observed time.
fn request_for(
    svc: &GrantService<'_, SqliteLedger>,
    req: &Value,
) -> Result<(String, ObservedTime), Refusal> {
    let ledger = svc.ledger();
    let observed = svc.observe()?;
    let request = {
        let shared = svc.active().shared();
        let inputs = shared.trusted_inputs(revision());
        let verified = verify(req);
        let authorized = match authorize(&verified, &inputs, &observed) {
            Ok(a) => a,
            Err(Refusal::Decision { .. }) => return Err(Refusal::NotApprovable),
            Err(other) => return Err(other),
        };
        if authorized.status() != UseTimeStatus::RequireApproval {
            return Err(Refusal::NotApprovable);
        }
        approval_request_for(&authorized, observed).ok_or(Refusal::NotApprovable)?
    };
    ledger
        .record_approval_request(&request)
        .map_err(|f| Refusal::LedgerUnavailable(f.error))?;
    ensure_approver(ledger, observed)?;
    Ok((request.request_id().to_string(), observed))
}

/// Deny the approval request of `req` through a ceremony (terminal; no grant). Returns the
/// label the request's grant would have had.
pub(crate) fn deny(
    svc: &GrantService<'_, SqliteLedger>,
    req: &Value,
) -> Result<GrantLabel, Refusal> {
    let (request_id, observed) = request_for(svc, req)?;
    let op = CeremonyOp::Deny {
        request_id: request_id.clone(),
    };
    let key = format!("support-deny-{request_id}");
    match ceremony(svc.ledger(), &support_approver(), &op, &key, observed)? {
        CeremonyResult::Denied => Ok(approval_grant_label(&request_id).unwrap()),
        other => panic!("a denial ceremony answers Denied, got {other:?}"),
    }
}

/// Issue an executable grant for `req` through the M4 lifecycle (D-042 X1): the use-time
/// decision must be `require_approval` (otherwise `NOT_APPROVABLE`, nothing stored); the
/// approval request the broker records for it is stored; [`support_approver`] approves it in
/// a ceremony. The grant's label is the ledger's `approval/<request id>`, registered as
/// `alias` on this thread ([`resolve`]).
pub(crate) fn issue(
    svc: &GrantService<'_, SqliteLedger>,
    req: &Value,
    alias: &str,
) -> Result<GrantIssued, Refusal> {
    let ledger = svc.ledger();
    let (request_id, observed) = request_for(svc, req)?;
    let op = CeremonyOp::Approve {
        request_id: request_id.clone(),
    };
    let key = format!("support-approve-{request_id}");
    let CeremonyResult::Approved { grant } =
        ceremony(ledger, &support_approver(), &op, &key, observed)?
    else {
        panic!("an approval ceremony answers Approved");
    };
    let (binding, expires_s, expires_ns): (String, i64, i64) = ledger
        .db()
        .conn
        .query_row(
            "SELECT binding, expires_s, expires_ns FROM grants WHERE label = ?1",
            [grant.as_str()],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .unwrap();
    ALIASES.with(|a| {
        a.borrow_mut()
            .insert(alias.to_string(), grant.as_str().to_string())
    });
    Ok(GrantIssued {
        label: grant,
        expires_at: writ_broker::clock::timestamp_from_parts(
            expires_s,
            u32::try_from(expires_ns).unwrap(),
        )
        .unwrap(),
        binding,
    })
}

/// A stub verifier that answers every registration with a fixed result (an attacker linking
/// the crates can build one; M1). Its assertions are always refused.
struct Forger {
    credential: String,
    material: Vec<u8>,
    algorithm: String,
    user_verified: bool,
}

impl RegistrationVerifier for Forger {
    fn relying_party(&self) -> RelyingParty {
        RelyingParty {
            rp_id: "localhost".into(),
            origin: ORIGIN.into(),
        }
    }

    fn begin_registration(&self, _subject: &SubjectId) -> Result<RegistrationStart, VerifierError> {
        Err(VerifierError::Unavailable)
    }

    fn finish_registration(
        &self,
        cap: &VerifierCapability,
        binding: &RegistrationBinding,
        _state: &CeremonyState,
        _response: &[u8],
    ) -> Result<VerifiedRegistration, VerifierError> {
        Ok(VerifiedRegistration::new(
            cap,
            binding,
            &self.credential,
            self.material.clone(),
            &self.algorithm,
            self.user_verified,
        ))
    }
}

impl HumanVerifier for Forger {
    fn relying_party(&self) -> writ_broker::human::RelyingParty {
        writ_broker::human::RegistrationVerifier::relying_party(self)
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
        _cap: &VerifierCapability,
        _state: &CeremonyState,
        _response: &[u8],
        _credentials: &ActiveCredentials,
    ) -> Result<VerifiedAssertion, VerifierError> {
        Err(VerifierError::Unavailable)
    }
}

/// A registration result for `binding` minted outside any ledger, through a stub approval
/// channel (what any linking code can build; the ledger accepts only what its own
/// registration row recorded, S5, X8).
pub(crate) fn forged_registration(
    binding: &RegistrationBinding,
    credential: &str,
    material: Vec<u8>,
    algorithm: &str,
    user_verified: bool,
) -> VerifiedRegistration {
    let f = Arc::new(Forger {
        credential: credential.into(),
        material,
        algorithm: algorithm.into(),
        user_verified,
    });
    ApprovalChannel::new(f.clone(), f, "forger")
        .unwrap()
        .finish_registration(binding, &CeremonyState::from_bytes(Vec::new()), b"")
        .unwrap()
}
