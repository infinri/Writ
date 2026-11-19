//! Approval listener over local loopback only (M4-11; D-040 O-2, O-3, O-11, O-12; D-041
//! listener conditions). A fake [`Backend`] records what the listener asks of the broker;
//! the ledger semantics behind it are tested in `writ-ledger-sqlite`. Nothing here binds a
//! non-loopback address, uses a real credential or enrolls a real user.

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::io::{Read, Write};
use std::net::TcpStream;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use writ_approval::backend::{Backend, Unavailable};
use writ_approval::listener::{
    ApprovalListener, Limits, ListenerConfig, ServeControl, MAX_SESSION_SECONDS,
};
use writ_broker::approval::{
    context_fingerprint, ApprovalCode, ApprovalRequest, ApprovalStatus, ChallengeBinding,
    ChallengeOperation, NewApprovalRequest, NewSignoff, RevocationTarget, SignoffExclusions,
};
use writ_broker::approval_store::{
    revocation_context, role_change_context, CeremonyContext, CeremonyOp, CeremonyOutcome,
    CeremonyResult, IssuedChallenge, IssuedRegistration, NewRole, OperatorSession, ReadRefused,
    ReadSession, ReadTarget, RecordView, RoleChange, SignoffParties,
};
use writ_broker::human::{
    ActiveCredentials, ApprovalChannel, CeremonyStart, CeremonyState, HumanVerifier,
    LedgerCapability, Permission, PrincipalKind, RegistrationBinding, RegistrationOperation,
    RegistrationStart, RegistrationVerifier, RelyingParty, SubjectId, VerifiedAssertion,
    VerifiedRegistration, VerifierCapability, VerifierError, ES256,
};
use writ_broker::refusal::Refusal;
use writ_broker::writ_kernel::json::{self, Value};
use writ_broker::writ_kernel::time::Timestamp;

const HOSTILE: &str = "</td><script>alert(1)</script><img src=x onerror=alert(2)>";

#[derive(Clone, Debug, PartialEq, Eq)]
enum Call {
    Begin {
        op: CeremonyOp,
        subject: SubjectId,
        key: String,
    },
    Finish {
        op: CeremonyOp,
        challenge_id: String,
        response: Vec<u8>,
        key: String,
    },
    FinishRegistration {
        registration_id: String,
        response: Vec<u8>,
        operator: OperatorSession,
    },
}

/// A stub registration verifier: the only way a test outside the broker obtains a
/// [`VerifiedRegistration`] is through an [`ApprovalChannel`] (it lends the capability).
struct StubVerifier;

impl HumanVerifier for StubVerifier {
    fn relying_party(&self) -> RelyingParty {
        RegistrationVerifier::relying_party(self)
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

impl RegistrationVerifier for StubVerifier {
    fn relying_party(&self) -> RelyingParty {
        RelyingParty {
            rp_id: "localhost".into(),
            origin: "http://localhost:1".into(),
        }
    }

    fn begin_registration(&self, _subject: &SubjectId) -> Result<RegistrationStart, VerifierError> {
        Err(VerifierError::Unavailable)
    }

    fn finish_registration(
        &self,
        capability: &VerifierCapability,
        binding: &RegistrationBinding,
        _state: &CeremonyState,
        _response: &[u8],
    ) -> Result<VerifiedRegistration, VerifierError> {
        Ok(VerifiedRegistration::new(
            capability,
            binding,
            &format!("cred-{}", binding.subject.subject),
            b"public-material".to_vec(),
            ES256,
            true,
        ))
    }
}

/// The pending signoff the fake offers.
fn signoff_binding() -> NewSignoff {
    NewSignoff {
        signoff_id: "so-1".into(),
        tenant_id: "t1".into(),
        project_id: "p1".into(),
        task_id: "task-1".into(),
        workflow_id: "wf-1".into(),
        from_state: "review".into(),
        to_state: "done".into(),
        acceptance_record_id: "rec-1".into(),
        criterion_id: "crit-1".into(),
        artifact_snapshot_id: "snap-1".into(),
        artifact_digest: format!("sha256:{:064x}", 9),
        acceptance_policy_id: "policy-1".into(),
        acceptance_policy_version: "1".into(),
        acceptance_policy_bundle_sha: "sha256:acceptance".into(),
        state_revision: 3,
    }
}

/// One stored request whose context can change (stale display); a finish succeeds only for
/// the response `valid-assertion` (anything else is a forged assertion).
struct Fake {
    request: Mutex<ApprovalRequest>,
    calls: Mutex<Vec<Call>>,
    next: Mutex<u32>,
    /// When set, `begin` refuses with it (the ledger's specific code).
    begin_refusal: Mutex<Option<Refusal>>,
    /// Bumped to change every displayed ceremony context (stale display).
    revision: Mutex<i64>,
    /// When set, `context` refuses (an unknown target, for instance).
    context_refusal: Mutex<bool>,
    /// Registration rows the fake issued: id to subject.
    rows: Mutex<Vec<(String, SubjectId)>>,
    channel: ApprovalChannel,
    /// Read sessions the fake's logins opened (D-044 Z1).
    sessions: Mutex<Vec<ReadSession>>,
    /// Subjects whose sessions the fake's ledger now refuses (revoked).
    revoked: Mutex<Vec<String>>,
    /// The login operations begun (kept apart from `calls`).
    logins: Mutex<Vec<CeremonyOp>>,
    /// The byte lengths of the enrolled credential ids the fake reports.
    lengths: Mutex<Vec<usize>>,
}

fn request(action_note: &str) -> ApprovalRequest {
    ApprovalRequest::new(NewApprovalRequest {
        request_id: "req-1".into(),
        tenant_id: "t1".into(),
        project_id: "p1".into(),
        requester: SubjectId::new("adapter", "agent:session-1"),
        agent: None,
        action_kind: "repository.publish".into(),
        canonical_resource_id: "repo:writ".into(),
        action_digest: format!("sha256:{:064x}", 1),
        action: json::obj([
            ("kind", json::s("repository.publish")),
            ("note", json::s(action_note)),
        ]),
        capabilities: vec!["publish".into()],
        policy_bundle_sha: "sha256:policy".into(),
        request_fingerprint: "sha256:request".into(),
        created_at: Timestamp::parse_strict("2026-11-14T10:00:00Z").unwrap(),
        expires_at: Timestamp::parse_strict("2026-11-14T10:10:00Z").unwrap(),
        required_approvers: 1,
    })
    .unwrap()
}

impl Fake {
    fn new() -> Fake {
        Fake {
            request: Mutex::new(request(HOSTILE)),
            calls: Mutex::new(Vec::new()),
            next: Mutex::new(0),
            begin_refusal: Mutex::new(None),
            revision: Mutex::new(0),
            context_refusal: Mutex::new(false),
            rows: Mutex::new(Vec::new()),
            channel: ApprovalChannel::new(
                Arc::new(StubVerifier),
                Arc::new(StubVerifier),
                "approval-listener",
            )
            .unwrap(),
            sessions: Mutex::new(Vec::new()),
            revoked: Mutex::new(Vec::new()),
            logins: Mutex::new(Vec::new()),
            lengths: Mutex::new(vec![20, 32]),
        }
    }

    fn calls(&self) -> Vec<Call> {
        self.calls.lock().unwrap().clone()
    }
}

impl Fake {
    fn request(
        &self,
        request_id: &str,
    ) -> Result<Option<(ApprovalRequest, ApprovalStatus)>, Unavailable> {
        let r = self.request.lock().unwrap().clone();
        Ok((request_id == r.request_id()).then_some((r, ApprovalStatus::Pending)))
    }

    fn begin_op(
        &self,
        op: &CeremonyOp,
        subject: &SubjectId,
        idempotency_key: &str,
    ) -> Result<Result<IssuedChallenge, Refusal>, Unavailable> {
        self.calls.lock().unwrap().push(Call::Begin {
            op: op.clone(),
            subject: subject.clone(),
            key: idempotency_key.into(),
        });
        if let Some(refusal) = self.begin_refusal.lock().unwrap().clone() {
            return Ok(Err(refusal));
        }
        let mut n = self.next.lock().unwrap();
        *n += 1;
        Ok(Ok(IssuedChallenge {
            challenge_id: format!("ch-{n}"),
            client_options: br#"{"publicKey":{"challenge":"AAAA","rpId":"localhost"}}"#.to_vec(),
            expires_at: Timestamp::parse_strict("2026-11-14T10:02:00Z").unwrap(),
        }))
    }

    fn finish_op(
        &self,
        op: &CeremonyOp,
        challenge_id: &str,
        response: &[u8],
        idempotency_key: &str,
    ) -> Result<Result<CeremonyOutcome, Refusal>, Unavailable> {
        self.calls.lock().unwrap().push(Call::Finish {
            op: op.clone(),
            challenge_id: challenge_id.into(),
            response: response.to_vec(),
            key: idempotency_key.into(),
        });
        if response == b"verifier-down" {
            return Ok(Err(Refusal::Approval(ApprovalCode::AssertionInvalid(
                VerifierError::Unavailable,
            ))));
        }
        if response != b"valid-assertion" {
            return Ok(Err(Refusal::Approval(ApprovalCode::AssertionInvalid(
                VerifierError::SignatureInvalid,
            ))));
        }
        let result = match op {
            CeremonyOp::Approve { .. } | CeremonyOp::Deny { .. } => CeremonyResult::Denied,
            CeremonyOp::Revoke { .. } => CeremonyResult::Revoked {
                use_state: writ_broker::approval_store::UseState::Unused,
            },
            CeremonyOp::Signoff { binding, .. } => CeremonyResult::SignedOff {
                signoff_id: binding.signoff_id.clone(),
            },
            CeremonyOp::Waive { .. } => {
                return Ok(Err(Refusal::Approval(ApprovalCode::WaiverUnknown)))
            }
            CeremonyOp::Enroll { enrollment, .. } => CeremonyResult::Enrolled {
                subject: enrollment.subject.clone(),
            },
            CeremonyOp::RoleChange { .. } => CeremonyResult::RoleChanged {
                assignment_id: "role-1".into(),
            },
            CeremonyOp::ReadSession { .. } => panic!("a login is the trait's"),
            CeremonyOp::RegistrationStart { subject, .. } => {
                let mut rows = self.rows.lock().unwrap();
                let id = format!("reg-{}", rows.len() + 1);
                rows.push((id.clone(), subject.clone()));
                CeremonyResult::RegistrationStarted {
                    registration: IssuedRegistration {
                        registration_id: id,
                        client_options:
                            br#"{"publicKey":{"challenge":"AAAA","user":{"id":"AAAA"}}}"#.to_vec(),
                        expires_at: Timestamp::parse_strict("2026-11-14T10:10:00Z").unwrap(),
                    },
                }
            }
        };
        Ok(Ok(CeremonyOutcome {
            result,
            ledger_seq: 7,
            replayed: false,
        }))
    }

    fn context(&self, op: &CeremonyOp) -> Result<Result<CeremonyContext, Refusal>, Unavailable> {
        if *self.context_refusal.lock().unwrap() {
            return Ok(Err(Refusal::Approval(ApprovalCode::EnrollmentUnsupported)));
        }
        let revision = *self.revision.lock().unwrap();
        let record = json::obj([
            ("note", json::s(HOSTILE)),
            ("revision", Value::Int(revision)),
        ]);
        let (context, operation, target_id) = match op {
            CeremonyOp::Approve { .. }
            | CeremonyOp::Deny { .. }
            | CeremonyOp::Waive { .. }
            | CeremonyOp::ReadSession { .. } => {
                return Ok(Err(Refusal::Approval(ApprovalCode::RequestUnknown)))
            }
            CeremonyOp::Revoke { target, reason } => (
                revocation_context(target, reason, "*", "*", record),
                ChallengeOperation::Revoke,
                target.target_id(),
            ),
            CeremonyOp::RoleChange { change } => (
                role_change_context(change, "t1", "p1", record),
                ChallengeOperation::RoleChange,
                change.target_id(),
            ),
            CeremonyOp::Enroll { enrollment, .. } => (
                json::obj([("enrollment", enrollment.context()), ("record", record)]),
                ChallengeOperation::Enroll,
                enrollment.target_id(),
            ),
            CeremonyOp::RegistrationStart {
                subject, operator, ..
            } => (
                json::obj([
                    ("operator", json::s(&operator.subject)),
                    ("record", record),
                    ("subject", json::s(&subject.subject)),
                ]),
                ChallengeOperation::RegistrationStart,
                subject.subject.clone(),
            ),
            CeremonyOp::Signoff {
                binding,
                parties,
                requested_expires_at,
            } => {
                let excluded = SignoffExclusions {
                    requesters: vec![SubjectId::new("adapter", HOSTILE)],
                    agents: vec![SubjectId::new("adapter", "agent-7")],
                    added: parties.added.clone(),
                    ..SignoffExclusions::default()
                };
                let mut b = (**binding).clone();
                b.state_revision += revision;
                (
                    b.context_with(&excluded, {
                        // The ledger's effective expiry (Y1): its own bound, lowered by
                        // a request.
                        let bound = Timestamp::parse_strict("2026-11-14T10:10:00Z").unwrap();
                        requested_expires_at.map_or(bound, |r| r.min(bound))
                    }),
                    ChallengeOperation::Signoff,
                    b.signoff_id.clone(),
                )
            }
        };
        Ok(Ok(CeremonyContext {
            binding: ChallengeBinding {
                operation,
                tenant_id: "t1".into(),
                project_id: "p1".into(),
                target_id,
                context_fingerprint: context_fingerprint(&context).unwrap(),
                policy_bundle_sha: "none".into(),
                revision: 0,
            },
            context,
        }))
    }

    fn finish_registration_op(
        &self,
        registration_id: &str,
        response: &[u8],
        operator: &OperatorSession,
    ) -> Result<Result<VerifiedRegistration, Refusal>, Unavailable> {
        self.calls.lock().unwrap().push(Call::FinishRegistration {
            registration_id: registration_id.into(),
            response: response.to_vec(),
            operator: operator.clone(),
        });
        if response == b"registration-down" {
            return Ok(Err(Refusal::Approval(ApprovalCode::AssertionInvalid(
                VerifierError::Unavailable,
            ))));
        }
        if response != b"valid-registration" {
            return Ok(Err(Refusal::Approval(ApprovalCode::AssertionInvalid(
                VerifierError::SignatureInvalid,
            ))));
        }
        let rows = self.rows.lock().unwrap();
        let Some((_, subject)) = rows.iter().find(|(id, _)| id == registration_id) else {
            return Ok(Err(Refusal::Approval(ApprovalCode::EnrollmentUnsupported)));
        };
        let binding = RegistrationBinding {
            registration_id: registration_id.into(),
            operation: RegistrationOperation::Enroll,
            subject: subject.clone(),
            relying_party: self.channel.relying_party().clone(),
        };
        Ok(self
            .channel
            .finish_registration(
                &binding,
                &CeremonyState::from_bytes(b"s".to_vec()),
                response,
            )
            .map_err(|e| Refusal::Approval(ApprovalCode::AssertionInvalid(e))))
    }

    /// Revoke `subject` (its subject, credential or role): its open sessions stop reading.
    fn revoke(&self, subject: &str) {
        self.revoked.lock().unwrap().push(subject.into());
    }
}

/// The fake's real credential id of `subject` (base64url-safe text).
fn real_credential(subject: &str) -> String {
    format!("real-credential-of-{subject}")
}

/// Subjects the fake's ledger refuses every target to (no role anywhere).
const NO_ROLES: &str = "mallory";
/// A subject whose login the fake's ledger refuses (unknown).
const UNKNOWN: &str = "nobody";
/// A subject whose login begin meets a verifier outage.
const OUTAGE: &str = "outage";
/// A subject the fake reports with five credentials (more than the padded count).
const MANY: &str = "many";

/// The fake ledger's read sessions and protected read (D-044 Z1, Z3): a session counts only
/// if this fake's login issued it and its subject is not revoked; `mallory` holds no role;
/// unknown targets and a set `context_refusal` are the one refusal.
impl Backend for Fake {
    fn read(
        &self,
        session: &ReadSession,
        target: &ReadTarget,
    ) -> Result<Result<RecordView, ReadRefused>, Unavailable> {
        let subject = &session.subject().subject;
        if !self.sessions.lock().unwrap().contains(session)
            || self.revoked.lock().unwrap().contains(subject)
            || subject == NO_ROLES
        {
            return Ok(Err(ReadRefused));
        }
        Ok(match target {
            ReadTarget::ApprovalRequest { request_id } => match self.request(request_id)? {
                Some((request, status)) => Ok(RecordView::ApprovalRequest {
                    request: Box::new(request),
                    status,
                }),
                None => Err(ReadRefused),
            },
            ReadTarget::Signoff { signoff_id } if signoff_id == "so-1" => {
                // The ledger pins its effective expiry (Y1) as the requested one.
                let op = CeremonyOp::Signoff {
                    binding: Box::new(signoff_binding()),
                    parties: SignoffParties::default(),
                    requested_expires_at: Some(
                        Timestamp::parse_strict("2026-11-14T10:10:00Z").unwrap(),
                    ),
                };
                match self.context(&op)? {
                    Ok(context) => Ok(RecordView::Ceremony {
                        op: Box::new(op),
                        context,
                    }),
                    Err(_) => Err(ReadRefused),
                }
            }
            ReadTarget::Signoff { .. } => Err(ReadRefused),
            ReadTarget::Form { .. } => Ok(RecordView::Permitted),
            ReadTarget::Ceremony(op) => match self.context(op)? {
                Ok(context) => Ok(RecordView::Ceremony {
                    op: op.clone(),
                    context,
                }),
                Err(_) => Err(ReadRefused),
            },
        })
    }

    fn begin(
        &self,
        op: &CeremonyOp,
        subject: &SubjectId,
        idempotency_key: &str,
    ) -> Result<Result<IssuedChallenge, Refusal>, Unavailable> {
        let CeremonyOp::ReadSession { .. } = op else {
            return self.begin_op(op, subject, idempotency_key);
        };
        self.logins.lock().unwrap().push(op.clone());
        if subject.subject.starts_with(UNKNOWN) {
            return Ok(Err(Refusal::Approval(ApprovalCode::PermissionMissing)));
        }
        if subject.subject == OUTAGE {
            return Ok(Err(Refusal::Approval(ApprovalCode::AssertionInvalid(
                VerifierError::Unavailable,
            ))));
        }
        let n = self.logins.lock().unwrap().len();
        let ids = if subject.subject == MANY {
            (0..5)
                .map(|i| format!(r#"{{"type":"public-key","id":"many-{i}"}}"#))
                .collect::<Vec<_>>()
                .join(",")
        } else {
            format!(
                r#"{{"type":"public-key","id":"{}","transports":["internal"]}}"#,
                real_credential(&subject.subject)
            )
        };
        // The verifier's options as webauthn-rs shapes them (one real credential with a
        // transport, its own timeout); the listener normalizes and pads them (M1).
        let options = format!(
            r#"{{"publicKey":{{"challenge":"Q2hhbGxlbmdlLW51bWJlci0{n:03}","timeout":300000,"rpId":"localhost","allowCredentials":[{ids}],"userVerification":"required"}}}}"#
        );
        Ok(Ok(IssuedChallenge {
            challenge_id: format!("chl-{n:032x}"),
            client_options: options.into_bytes(),
            expires_at: Timestamp::parse_rfc3339("2026-11-14T10:02:00.123456789Z").unwrap(),
        }))
    }

    fn finish(
        &self,
        op: &CeremonyOp,
        challenge_id: &str,
        response: &[u8],
        idempotency_key: &str,
    ) -> Result<Result<CeremonyOutcome, Refusal>, Unavailable> {
        let CeremonyOp::ReadSession {
            subject,
            page_session,
        } = op
        else {
            return self.finish_op(op, challenge_id, response, idempotency_key);
        };
        if response == b"verifier-down" {
            return Ok(Err(Refusal::Approval(ApprovalCode::AssertionInvalid(
                VerifierError::Unavailable,
            ))));
        }
        if response != b"valid-assertion" {
            return Ok(Err(Refusal::Approval(ApprovalCode::AssertionInvalid(
                VerifierError::SignatureInvalid,
            ))));
        }
        let session = ReadSession::from_ledger(
            &LedgerCapability::for_ledger_implementation(),
            challenge_id,
            subject.clone(),
            &format!("cred-{}", subject.subject),
            page_session,
            Timestamp::parse_strict("2026-11-14T10:01:00Z").unwrap(),
        );
        self.sessions.lock().unwrap().push(session.clone());
        Ok(Ok(CeremonyOutcome {
            result: CeremonyResult::ReadSessionOpened { session },
            ledger_seq: 1,
            replayed: false,
        }))
    }

    fn finish_registration(
        &self,
        registration_id: &str,
        response: &[u8],
        operator: &OperatorSession,
    ) -> Result<Result<VerifiedRegistration, Refusal>, Unavailable> {
        self.finish_registration_op(registration_id, response, operator)
    }

    fn credential_id_lengths(&self) -> Result<Vec<usize>, Unavailable> {
        Ok(self.lengths.lock().unwrap().clone())
    }
}

struct Server {
    fake: Arc<Fake>,
    port: u16,
    origin: String,
    stop: Arc<AtomicBool>,
    control: Arc<ServeControl>,
    thread: Option<thread::JoinHandle<()>>,
}

impl Server {
    fn start(limits: Limits) -> Server {
        let fake = Arc::new(Fake::new());
        let listener = ApprovalListener::bind(
            ListenerConfig {
                port: 0,
                subject_issuer: "writ-local".into(),
                limits,
            },
            Arc::clone(&fake) as Arc<dyn Backend>,
        )
        .unwrap();
        let addr = listener.local_addr().unwrap();
        assert!(addr.ip().is_loopback(), "bound to loopback only");
        assert_eq!(listener.rp_id(), "localhost");
        let origin = listener.origin().to_string();
        assert_eq!(origin, format!("http://localhost:{}", addr.port()));
        let stop = Arc::new(AtomicBool::new(false));
        let s = Arc::clone(&stop);
        let control = listener.control();
        let thread = thread::spawn(move || listener.serve(s).unwrap());
        Server {
            fake,
            port: addr.port(),
            origin,
            stop,
            control,
            thread: Some(thread),
        }
    }

    fn raw(&self, bytes: &[u8]) -> String {
        let mut s = TcpStream::connect(("127.0.0.1", self.port)).unwrap();
        s.set_read_timeout(Some(Duration::from_secs(10))).unwrap();
        let _ = s.write_all(bytes);
        let mut out = Vec::new();
        let _ = s.read_to_end(&mut out);
        String::from_utf8_lossy(&out).into_owned()
    }

    /// A GET as a browser's direct navigation (`Sec-Fetch-Site: none`) unless `extra` names
    /// another `Sec-Fetch-Site`.
    fn get(&self, path: &str, extra: &str) -> String {
        let site = if extra.contains("Sec-Fetch-Site") {
            ""
        } else {
            "Sec-Fetch-Site: none\r\n"
        };
        self.raw(
            format!(
                "GET {path} HTTP/1.1\r\nHost: localhost:{}\r\n{site}{extra}Connection: close\r\n\r\n",
                self.port
            )
            .as_bytes(),
        )
    }

    /// A POST as this origin's page script (`Sec-Fetch-Site: same-origin`) unless `extra`
    /// names another `Sec-Fetch-Site`.
    fn post(&self, path: &str, cookie: &str, body: &str, extra: &str) -> String {
        let site = if extra.contains("Sec-Fetch-Site") {
            ""
        } else {
            "Sec-Fetch-Site: same-origin\r\n"
        };
        self.raw(
            format!(
                "POST {path} HTTP/1.1\r\nHost: localhost:{}\r\nOrigin: {}\r\n\
                 Content-Type: application/x-www-form-urlencoded\r\nCookie: writ_session={cookie}\r\n\
                 Content-Length: {}\r\n{site}{extra}Connection: close\r\n\r\n{body}",
                self.port,
                self.origin,
                body.len()
            )
            .as_bytes(),
        )
    }

    /// Sign in as `subject` (D-044 Z1): the login begin, then its finish; the rotated,
    /// authenticated session's (cookie, CSRF token).
    fn login(&self, subject: &str) -> (String, String) {
        let begun = self.post("/login/begin", "", &format!("subject={subject}"), "");
        assert_eq!(status(&begun), 200, "{begun}");
        let pre = between(&begun, "writ_session=", ";");
        let csrf = between(&begun, "\"csrf\":\"", "\"");
        let challenge = between(&begun, "\"challenge_id\":\"", "\"");
        let done = self.post(
            "/login/finish",
            &pre,
            &format!("csrf={csrf}&challenge_id={challenge}&response=valid-assertion"),
            "",
        );
        assert_eq!(status(&done), 200, "{done}");
        let cookie = between(&done, "writ_session=", ";");
        assert_ne!(cookie, pre, "a login rotates the session token");
        (cookie, between(&done, "\"csrf\":\"", "\""))
    }

    /// A GET in the session `cookie` (a direct navigation).
    fn get_as(&self, path: &str, cookie: &str) -> String {
        self.get(path, &format!("Cookie: writ_session={cookie}\r\n"))
    }

    /// Sign in as `bob` and view the request: (session cookie, CSRF token, fingerprint).
    fn view(&self) -> (String, String, String) {
        let (cookie, _) = self.login("bob");
        let page = self.get_as("/request/req-1", &cookie);
        assert_eq!(status(&page), 200, "{page}");
        let csrf = between(&page, "data-csrf=\"", "\"");
        let fp = between(&page, "data-fingerprint=\"", "\"");
        (cookie, csrf, fp)
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        if let Some(t) = self.thread.take() {
            let _ = t.join();
        }
    }
}

fn status(response: &str) -> u16 {
    response
        .split(' ')
        .nth(1)
        .and_then(|s| s.parse().ok())
        .unwrap_or(0)
}

fn between(text: &str, start: &str, end: &str) -> String {
    let from = text.find(start).expect(start) + start.len();
    let len = text[from..].find(end).expect(end);
    text[from..from + len].to_string()
}

fn enc(text: &str) -> String {
    text.bytes()
        .map(|b| match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' => (b as char).to_string(),
            _ => format!("%{b:02X}"),
        })
        .collect()
}

fn header_of<'a>(response: &'a str, name: &str) -> Option<&'a str> {
    let head = response.split("\r\n\r\n").next()?;
    head.lines()
        .find_map(|l| {
            l.split_once(':')
                .filter(|(k, _)| k.eq_ignore_ascii_case(name))
        })
        .map(|(_, v)| v.trim())
}

#[test]
fn the_page_renders_only_the_stored_context_escaped_with_a_strict_policy() {
    let s = Server::start(Limits::default());
    let (cookie, _) = s.login("bob");
    let page = s.get_as("/request/req-1", &cookie);
    assert_eq!(status(&page), 200);
    let csp = header_of(&page, "content-security-policy").unwrap();
    assert!(csp.contains("default-src 'none'") && csp.contains("script-src 'self'"));
    assert!(csp.contains("frame-ancestors 'none'"));
    assert_eq!(header_of(&page, "x-content-type-options"), Some("nosniff"));
    assert_eq!(header_of(&page, "cache-control"), Some("no-store"));
    // A view creates no session: the session is the login's (Z2).
    assert!(header_of(&page, "set-cookie").is_none());
    // The untrusted note is shown, escaped; no inline script exists at all.
    assert!(!page.contains("<script>alert"));
    assert!(!page.contains("<img"));
    assert!(page.contains("&lt;script&gt;alert(1)&lt;&#47;script&gt;"));
    assert_eq!(
        page.matches("<script").count(),
        1,
        "only the external page script"
    );
    assert!(page.contains("<script src=\"/static/approval.js\" defer></script>"));
    // The displayed fingerprint is the stored context's.
    let stored = s.fake.request("req-1").unwrap().unwrap().0;
    assert!(page.contains(stored.fingerprint().as_str()));
    // Every member of the stored context is displayed (R6).
    for member in ["kind", "note", "requester", "repository.publish"] {
        assert!(page.contains(member), "{member}");
    }
    // A view never starts or finishes anything.
    assert!(s.fake.calls().is_empty());
    let unknown = s.get_as("/request/nope", &cookie);
    assert_eq!(status(&unknown), 404);
    assert_eq!(status(&s.get("/static/approval.js", "")), 200);
}

#[test]
fn host_and_origin_must_be_exact() {
    let s = Server::start(Limits::default());
    for host in [
        "127.0.0.1".to_string(),
        format!("127.0.0.1:{}", s.port),
        "localhost".to_string(),
        format!("localhost:{}", s.port + 1),
        format!("evil.example:{}", s.port),
        format!("LOCALHOST:{}", s.port),
    ] {
        let r = s.raw(format!("GET /request/req-1 HTTP/1.1\r\nHost: {host}\r\n\r\n").as_bytes());
        assert_eq!(status(&r), 421, "{host}");
    }
    // Absolute-form target.
    let r = s.raw(
        format!(
            "GET http://localhost:{0}/request/req-1 HTTP/1.1\r\nHost: localhost:{0}\r\n\r\n",
            s.port
        )
        .as_bytes(),
    );
    assert_eq!(status(&r), 421);
    // A foreign Origin is refused on GET, and POST requires the exact origin.
    assert_eq!(
        status(&s.get("/request/req-1", "Origin: http://evil.example\r\n")),
        403
    );
    let (cookie, csrf, fp) = s.view();
    let body = format!(
        "csrf={csrf}&request_id=req-1&fingerprint={}&verdict=deny&subject=bob",
        enc(&fp)
    );
    for origin in [
        format!("http://127.0.0.1:{}", s.port),
        format!("https://localhost:{}", s.port),
        "null".to_string(),
    ] {
        let r = s.raw(
            format!(
                "POST /ceremony/begin HTTP/1.1\r\nHost: localhost:{}\r\nOrigin: {origin}\r\n\
                 Content-Type: application/x-www-form-urlencoded\r\nCookie: writ_session={cookie}\r\n\
                 Content-Length: {}\r\n\r\n{body}",
                s.port,
                body.len()
            )
            .as_bytes(),
        );
        assert_eq!(status(&r), 403, "{origin}");
    }
    let no_origin = s.raw(
        format!(
            "POST /ceremony/begin HTTP/1.1\r\nHost: localhost:{}\r\n\
             Content-Type: application/x-www-form-urlencoded\r\nCookie: writ_session={cookie}\r\n\
             Content-Length: {}\r\n\r\n{body}",
            s.port,
            body.len()
        )
        .as_bytes(),
    );
    assert_eq!(status(&no_origin), 403);
    assert!(s.fake.calls().is_empty());
}

#[test]
fn a_session_and_csrf_token_alone_never_change_state() {
    let s = Server::start(Limits::default());
    let (cookie, csrf, fp) = s.view();
    let fp = enc(&fp);
    let begin = |cookie: &str, csrf: &str, extra: &str| {
        s.post(
            "/ceremony/begin",
            cookie,
            &format!(
                "csrf={csrf}&request_id=req-1&fingerprint={fp}&verdict=deny&subject=bob{extra}"
            ),
            "",
        )
    };
    // No session, another session, a wrong CSRF token.
    assert_eq!(status(&begin("", &csrf, "")), 403);
    assert_eq!(status(&begin("0123", &csrf, "")), 403);
    assert_eq!(status(&begin(&cookie, "wrong", "")), 403);
    assert_eq!(status(&begin(&cookie, "", "")), 403);
    assert!(s.fake.calls().is_empty());
    // Another target than the viewed one.
    let r = s.post(
        "/ceremony/begin",
        &cookie,
        &format!("csrf={csrf}&request_id=req-2&fingerprint={fp}&verdict=deny&subject=bob"),
        "",
    );
    assert_eq!(status(&r), 403);
    // A finish for a challenge this session never began, with a valid-looking assertion.
    let r = s.post(
        "/ceremony/finish",
        &cookie,
        &format!("csrf={csrf}&challenge_id=ch-1&response=valid-assertion"),
        "",
    );
    assert_eq!(status(&r), 403);
    assert!(s.fake.calls().is_empty());
    // A real begin then a forged assertion: the ledger refuses (the session did not help).
    let r = begin(
        &cookie,
        &csrf,
        "&channel=evil&op=approve&idempotency_key=mine",
    );
    assert_eq!(status(&r), 200, "{r}");
    let challenge = between(&r, "\"challenge_id\":\"", "\"");
    assert!(r.contains("\"options\":{\"publicKey\""));
    let forged = s.post(
        "/ceremony/finish",
        &cookie,
        &format!("csrf={csrf}&challenge_id={challenge}&response=forged"),
        "",
    );
    assert_eq!(status(&forged), 403);
    assert!(forged.contains("ASSERTION_INVALID"));
    // The challenge is spent for this session after a definitive answer.
    let again = s.post(
        "/ceremony/finish",
        &cookie,
        &format!("csrf={csrf}&challenge_id={challenge}&response=valid-assertion"),
        "",
    );
    assert_eq!(status(&again), 403);
    // What the broker saw: the server's operation, target and key (the channel identity is
    // the ledger's configured approval channel, never a request field).
    let calls = s.fake.calls();
    let deny = CeremonyOp::Deny {
        request_id: "req-1".into(),
    };
    let Call::Begin { op, subject, key } = &calls[0] else {
        panic!("{calls:?}")
    };
    assert_eq!(op, &deny);
    assert_eq!(subject, &SubjectId::new("writ-local", "bob"));
    assert!(key.starts_with("page-") && key != "mine");
    let Call::Finish {
        op: fop,
        challenge_id,
        key: fkey,
        response,
    } = &calls[1]
    else {
        panic!("{calls:?}")
    };
    assert_eq!((fop, challenge_id.as_str()), (&deny, challenge.as_str()));
    assert_eq!(fkey, key);
    assert_eq!(response, b"forged");
    assert_eq!(calls.len(), 2);
    // A fresh ceremony with a valid assertion decides.
    let r = begin(&cookie, &csrf, "");
    let challenge = between(&r, "\"challenge_id\":\"", "\"");
    let done = s.post(
        "/ceremony/finish",
        &cookie,
        &format!("csrf={csrf}&challenge_id={challenge}&response=valid-assertion"),
        "",
    );
    assert_eq!(status(&done), 200, "{done}");
    assert!(done.contains("\"result\":\"denied\""));
}

#[test]
fn a_stale_display_never_starts_a_ceremony() {
    let s = Server::start(Limits::default());
    let (cookie, csrf, fp) = s.view();
    // The stored context changed since it was displayed.
    *s.fake.request.lock().unwrap() = request("something else");
    let r = s.post(
        "/ceremony/begin",
        &cookie,
        &format!(
            "csrf={csrf}&request_id=req-1&fingerprint={}&verdict=approve&subject=bob",
            enc(&fp)
        ),
        "",
    );
    assert_eq!(status(&r), 409);
    // A fingerprint other than the displayed one.
    let r = s.post(
        "/ceremony/begin",
        &cookie,
        &format!(
            "csrf={csrf}&request_id=req-1&fingerprint=sha256%3Aother&verdict=approve&subject=bob"
        ),
        "",
    );
    assert_eq!(status(&r), 409);
    assert!(s.fake.calls().is_empty());
}

#[test]
fn sessions_expire() {
    let s = Server::start(Limits {
        session_ttl: Duration::from_secs(1),
        ..Limits::default()
    });
    let (cookie, csrf, fp) = s.view();
    thread::sleep(Duration::from_millis(1100));
    let r = s.post(
        "/ceremony/begin",
        &cookie,
        &format!(
            "csrf={csrf}&request_id=req-1&fingerprint={}&verdict=deny&subject=bob",
            enc(&fp)
        ),
        "",
    );
    assert_eq!(status(&r), 403);
    assert!(s.fake.calls().is_empty());
}

#[test]
fn bodies_headers_connections_and_time_are_bounded() {
    let s = Server::start(Limits {
        max_body_bytes: 256,
        max_connections: 2,
        header_timeout: Duration::from_millis(300),
        ..Limits::default()
    });
    let (cookie, csrf, _) = s.view();
    // Body over the bound (declared, and undeclared chunked).
    let big = format!("csrf={csrf}&response={}", "a".repeat(400));
    assert_eq!(status(&s.post("/ceremony/finish", &cookie, &big, "")), 413);
    let chunked = s.raw(
        format!(
            "POST /ceremony/finish HTTP/1.1\r\nHost: localhost:{}\r\nOrigin: {}\r\n\
             Content-Type: application/x-www-form-urlencoded\r\nCookie: writ_session={cookie}\r\n\
             Transfer-Encoding: chunked\r\n\r\n190\r\n{}\r\n0\r\n\r\n",
            s.port,
            s.origin,
            "a".repeat(400)
        )
        .as_bytes(),
    );
    assert_eq!(status(&chunked), 413);
    // Header bytes and header count.
    let long = s.get(
        "/request/req-1",
        &format!("X-Long: {}\r\n", "a".repeat(20_000)),
    );
    assert_ne!(status(&long), 200);
    let many: String = (0..40).map(|i| format!("X-H{i}: v\r\n")).collect();
    assert_ne!(status(&s.get("/request/req-1", &many)), 200);
    // HTTP/2 is not spoken.
    let h2 = s.raw(b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n");
    assert!(!h2.contains(" 200 "), "{h2}");
    // A slow header is cut off at the header timeout.
    let started = Instant::now();
    let mut slow = TcpStream::connect(("127.0.0.1", s.port)).unwrap();
    slow.set_read_timeout(Some(Duration::from_secs(10)))
        .unwrap();
    slow.write_all(b"GET /request/req-1 HTTP/1.1\r\n").unwrap();
    let mut out = Vec::new();
    let _ = slow.read_to_end(&mut out);
    assert!(started.elapsed() < Duration::from_secs(5));
    // Connections over the bound are closed unanswered while two idle ones are held.
    let hold: Vec<TcpStream> = (0..2)
        .map(|_| TcpStream::connect(("127.0.0.1", s.port)).unwrap())
        .collect();
    thread::sleep(Duration::from_millis(100));
    let over = s.get("/request/req-1", "");
    assert_eq!(over, "", "over the connection bound");
    drop(hold);
    thread::sleep(Duration::from_millis(500));
    assert_eq!(status(&s.get("/", "")), 200);
}

#[test]
fn only_the_listed_methods_and_paths_exist() {
    let s = Server::start(Limits::default());
    let r = s.raw(
        format!(
            "PUT /request/req-1 HTTP/1.1\r\nHost: localhost:{}\r\nContent-Length: 0\r\n\r\n",
            s.port
        )
        .as_bytes(),
    );
    assert_eq!(status(&r), 405);
    assert_eq!(status(&s.get("/request/req-1?x=1", "")), 400);
    assert_eq!(status(&s.get("/nope", "")), 404);
    assert_eq!(status(&s.get("/", "")), 200);
    let (cookie, csrf, _) = s.view();
    let r = s.raw(
        format!(
            "POST /ceremony/begin HTTP/1.1\r\nHost: localhost:{}\r\nOrigin: {}\r\n\
             Content-Type: text/plain\r\nCookie: writ_session={cookie}\r\nContent-Length: 6\r\n\r\ncsrf={}",
            s.port, s.origin, &csrf[..1]
        )
        .as_bytes(),
    );
    assert_eq!(status(&r), 415);
}

/// Begin a deny ceremony in the session `(cookie, csrf, fingerprint)`.
fn begin_in(s: &Server, session: &(String, String, String)) -> String {
    let (cookie, csrf, fp) = session;
    s.post(
        "/ceremony/begin",
        cookie,
        &format!(
            "csrf={csrf}&request_id=req-1&fingerprint={}&verdict=deny&subject=bob",
            enc(fp)
        ),
        "",
    )
}

/// Finish `challenge` in the session with `response`.
fn finish_in(
    s: &Server,
    session: &(String, String, String),
    challenge: &str,
    response: &str,
) -> String {
    let (cookie, csrf, _) = session;
    s.post(
        "/ceremony/finish",
        cookie,
        &format!("csrf={csrf}&challenge_id={challenge}&response={response}"),
        "",
    )
}

/// L1: whatever the ledger's specific refusal of a begin (subject missing, service, revoked,
/// permission, no credential), the page receives one generic refusal (before the fix, the
/// body carried the ledger's code).
#[test]
fn a_refused_begin_answers_one_generic_refusal() {
    let s = Server::start(Limits::default());
    let session = s.view();
    for code in [
        ApprovalCode::PermissionMissing,
        ApprovalCode::ServiceNotPermitted,
        ApprovalCode::SubjectRevoked,
        ApprovalCode::CredentialRevoked,
        ApprovalCode::AssertionInvalid(VerifierError::CredentialUnknown),
    ] {
        let refusal = Refusal::Approval(code);
        *s.fake.begin_refusal.lock().unwrap() = Some(refusal.clone());
        let r = begin_in(&s, &session);
        assert_eq!(status(&r), 403, "{r}");
        assert!(r.ends_with("{\"error\":\"refused\"}"), "{r}");
        assert!(!r.contains(refusal.code()), "{r}");
    }
    // A refused begin leaves nothing pending: the cap is not consumed.
    *s.fake.begin_refusal.lock().unwrap() = None;
    for _ in 0..Limits::default().max_pending {
        assert_eq!(status(&begin_in(&s, &session)), 200);
    }
}

/// L2: issued, unexpired challenges begun through the listener are capped across all
/// sessions, not only per session; a finished ceremony frees its slot.
#[test]
fn outstanding_challenges_are_capped_across_sessions() {
    let s = Server::start(Limits {
        max_pending: 4,
        max_outstanding: 3,
        ..Limits::default()
    });
    let (a, b, third) = (s.view(), s.view(), s.view());
    let first = begin_in(&s, &a);
    assert_eq!(status(&first), 200);
    assert_eq!(status(&begin_in(&s, &a)), 200);
    assert_eq!(status(&begin_in(&s, &b)), 200);
    // Each session is under its own bound (4), the listener is at its global bound (3).
    let over = begin_in(&s, &b);
    assert_eq!(status(&over), 429, "{over}");
    assert_eq!(status(&begin_in(&s, &third)), 429);
    // D-044 review M2: a login never takes a mutation slot, and the mutation bound never
    // blocks a login (its own bound and rate limit apply).
    let (_extra, _) = s.login("bob");
    assert_eq!(status(&begin_in(&s, &third)), 429);
    // A decided ceremony frees one slot.
    let challenge = between(&first, "\"challenge_id\":\"", "\"");
    assert_eq!(
        status(&finish_in(&s, &a, &challenge, "valid-assertion")),
        200
    );
    assert_eq!(status(&begin_in(&s, &b)), 200);
    assert_eq!(status(&begin_in(&s, &b)), 429);
}

/// L2: when `max_sessions` is reached, a session with a pending ceremony is never evicted;
/// with every session pending, a new session is refused (fail closed).
#[test]
fn a_session_with_a_pending_ceremony_is_never_evicted() {
    let s = Server::start(Limits {
        max_sessions: 2,
        ..Limits::default()
    });
    let a = s.view();
    let a_begin = begin_in(&s, &a);
    assert_eq!(status(&a_begin), 200);
    let b = s.view();
    let b_begin = begin_in(&s, &b);
    assert_eq!(status(&b_begin), 200);
    // Both sessions are pending: a third authenticated session is refused at its login
    // finish (the login session itself is bounded separately, M2), and nothing was evicted.
    let begun = s.post("/login/begin", "", "subject=carol", "");
    assert_eq!(status(&begun), 200, "{begun}");
    let pre = between(&begun, "writ_session=", ";");
    let csrf = between(&begun, "\"csrf\":\"", "\"");
    let challenge = between(&begun, "\"challenge_id\":\"", "\"");
    let refused = s.post(
        "/login/finish",
        &pre,
        &format!("csrf={csrf}&challenge_id={challenge}&response=valid-assertion"),
        "",
    );
    assert_eq!(status(&refused), 503, "{refused}");
    assert!(header_of(&refused, "set-cookie").is_none());
    let a_challenge = between(&a_begin, "\"challenge_id\":\"", "\"");
    assert_eq!(
        status(&finish_in(&s, &a, &a_challenge, "valid-assertion")),
        200
    );
    // A has nothing pending now: a new session evicts it, never B.
    let _c = s.view();
    assert_eq!(status(&begin_in(&s, &a)), 403, "A was evicted");
    let b_challenge = between(&b_begin, "\"challenge_id\":\"", "\"");
    assert_eq!(
        status(&finish_in(&s, &b, &b_challenge, "valid-assertion")),
        200
    );
}

/// L6: a verifier outage answers `unavailable` and keeps the pending ceremony, so the same
/// challenge can still be finished (the ledger left it issued).
#[test]
fn a_verifier_outage_keeps_the_pending_ceremony() {
    let s = Server::start(Limits::default());
    let a = s.view();
    let r = begin_in(&s, &a);
    let challenge = between(&r, "\"challenge_id\":\"", "\"");
    let down = finish_in(&s, &a, &challenge, "verifier-down");
    assert_eq!(status(&down), 503, "{down}");
    let done = finish_in(&s, &a, &challenge, "valid-assertion");
    assert_eq!(status(&done), 200, "{done}");
    // Decided: the pending entry is gone.
    assert_eq!(
        status(&finish_in(&s, &a, &challenge, "valid-assertion")),
        403
    );
}

// ---------------------------------------------------------------------------
// Signoff, revocation, role-change and enrollment pages (W-F, D-043).

/// Sign in as `subject`, then GET `path` in that session: (cookie, CSRF token, the
/// response).
fn open(s: &Server, subject: &str, path: &str) -> (String, String, String) {
    let (cookie, _) = s.login(subject);
    let page = s.get_as(path, &cookie);
    assert_eq!(status(&page), 200, "{page}");
    let csrf = between(&page, "data-csrf=\"", "\"");
    (cookie, csrf, page)
}

/// A form body: `csrf` plus `fields`, every value percent-encoded.
fn body(csrf: &str, fields: &[(&str, &str)]) -> String {
    let mut out = format!("csrf={csrf}");
    for (k, v) in fields {
        out.push_str(&format!("&{k}={}", enc(v)));
    }
    out
}

/// GET `/view` in the session.
fn view_of(s: &Server, cookie: &str) -> String {
    // `/view` is reached only by the page script's same-origin navigation.
    s.get(
        "/view",
        &format!("Sec-Fetch-Site: same-origin\r\nCookie: writ_session={cookie}\r\n"),
    )
}

/// Prepare `fields` in a session of `subject` on form page `path`, then display it:
/// (cookie, csrf, fingerprint, the displayed page).
fn prepared(
    s: &Server,
    subject: &str,
    path: &str,
    fields: &[(&str, &str)],
) -> (String, String, String, String) {
    let (cookie, csrf, _) = open(s, subject, path);
    let r = s.post("/prepare", &cookie, &body(&csrf, fields), "");
    assert_eq!(status(&r), 200, "{r}");
    assert!(r.ends_with("{\"view\":\"/view\"}"), "{r}");
    let page = view_of(s, &cookie);
    assert_eq!(status(&page), 200, "{page}");
    let fp = between(&page, "data-fingerprint=\"", "\"");
    (cookie, csrf, fp, page)
}

/// Confirm the displayed target as `subject`, then finish it with a valid assertion;
/// returns the operation the backend saw at begin and at finish.
fn confirm(s: &Server, cookie: &str, csrf: &str, fp: &str, subject: &str) -> CeremonyOp {
    let begun = s.post(
        "/ceremony/begin",
        cookie,
        &body(
            csrf,
            &[
                ("fingerprint", fp),
                ("verdict", "confirm"),
                ("subject", subject),
            ],
        ),
        "",
    );
    assert_eq!(status(&begun), 200, "{begun}");
    let challenge = between(&begun, "\"challenge_id\":\"", "\"");
    let done = s.post(
        "/ceremony/finish",
        cookie,
        &body(
            csrf,
            &[
                ("challenge_id", &challenge),
                ("response", "valid-assertion"),
            ],
        ),
        "",
    );
    assert_eq!(status(&done), 200, "{done}");
    let calls = s.fake.calls();
    let n = calls.len();
    let (
        Call::Begin {
            op: begun_op,
            key: k1,
            ..
        },
        Call::Finish { op, key: k2, .. },
    ) = (&calls[n - 2], &calls[n - 1])
    else {
        panic!("{calls:?}");
    };
    assert_eq!(begun_op, op, "the server-held operation");
    assert_eq!(k1, k2, "the server-held idempotency key");
    op.clone()
}

/// Every displayed member of `context` is on `page`, escaped (R6).
fn assert_displays(page: &str, context: &Value) {
    match context {
        Value::Obj(members) => {
            for (k, v) in members {
                assert!(page.contains(&writ_approval::page::escape(k)), "{k}");
                assert_displays(page, v);
            }
        }
        Value::Arr(items) => items.iter().for_each(|v| assert_displays(page, v)),
        Value::Str(text) => {
            assert!(page.contains(&writ_approval::page::escape(text)), "{text}")
        }
        _ => {}
    }
    assert!(!page.contains("<script>alert"));
    assert_eq!(page.matches("<script").count(), 1);
}

#[test]
fn the_signoff_page_displays_the_complete_stored_context_and_confirms_it() {
    let s = Server::start(Limits::default());
    let (cookie, csrf, page) = open(&s, "rita", "/signoff/so-1");
    let op = CeremonyOp::Signoff {
        binding: Box::new(signoff_binding()),
        parties: SignoffParties::default(),
        // Y1: the page pins the ledger's effective expiry as the requested one, so the
        // begin fixes exactly the displayed value.
        requested_expires_at: Some(Timestamp::parse_strict("2026-11-14T10:10:00Z").unwrap()),
    };
    let shown = s.fake.context(&op).unwrap().unwrap();
    assert_displays(&page, &shown.context);
    // Reviewed parties and exclusions, the unavailable delegation chain and the ledger's
    // expiry are part of what is displayed.
    for text in [
        "excluded",
        "requesters",
        "agent-7",
        "delegation_chain",
        "unavailable",
        "expires_at",
        "2026-11-14T10:10:00Z",
    ] {
        assert!(page.contains(text), "{text}");
    }
    let fp = between(&page, "data-fingerprint=\"", "\"");
    assert_eq!(fp, shown.binding.context_fingerprint.as_str());
    assert!(s.fake.calls().is_empty(), "a view never begins anything");
    assert_eq!(confirm(&s, &cookie, &csrf, &fp, "rita"), op);
    // An id the ledger does not offer: one generic page.
    assert_eq!(status(&s.get_as("/signoff/so-unknown", &cookie)), 404);
    // The verdict of a request page is not accepted on a confirmation page.
    let (cookie, csrf, page) = open(&s, "rita", "/signoff/so-1");
    let fp = between(&page, "data-fingerprint=\"", "\"");
    for (extra, code) in [
        (vec![("verdict", "approve")], 400),
        (vec![("verdict", "confirm"), ("request_id", "req-1")], 403),
    ] {
        let mut fields = vec![("fingerprint", fp.as_str()), ("subject", "rita")];
        fields.extend(extra);
        let r = s.post("/ceremony/begin", &cookie, &body(&csrf, &fields), "");
        assert_eq!(status(&r), code, "{r}");
    }
}

#[test]
fn the_revoke_page_covers_every_target_kind() {
    let s = Server::start(Limits::default());
    let cases = [
        (
            "grant",
            "",
            "approval/req-1",
            RevocationTarget::Grant("approval/req-1".into()),
        ),
        (
            "signoff",
            "",
            "so-1",
            RevocationTarget::Signoff("so-1".into()),
        ),
        (
            "credential",
            "",
            "cred-x",
            RevocationTarget::Credential("cred-x".into()),
        ),
        (
            "subject",
            "",
            "dave",
            RevocationTarget::Subject(SubjectId::new("writ-local", "dave")),
        ),
        (
            "subject",
            "adapter",
            "svc",
            RevocationTarget::Subject(SubjectId::new("adapter", "svc")),
        ),
        (
            "role",
            "",
            "role:1",
            RevocationTarget::Role("role:1".into()),
        ),
    ];
    for (kind, issuer, target, expected) in cases {
        let (cookie, csrf, fp, page) = prepared(
            &s,
            "carol",
            "/revoke",
            &[
                ("operation", "revoke"),
                ("target_kind", kind),
                ("target_issuer", issuer),
                ("target", target),
                ("reason", HOSTILE),
            ],
        );
        let op = CeremonyOp::Revoke {
            target: expected,
            reason: HOSTILE.into(),
        };
        let shown = s.fake.context(&op).unwrap().unwrap();
        assert_eq!(fp, shown.binding.context_fingerprint.as_str());
        assert_displays(&page, &shown.context);
        assert_eq!(confirm(&s, &cookie, &csrf, &fp, "carol"), op, "{kind}");
    }
}

#[test]
fn the_role_page_grants_or_revokes_one_assignment() {
    let s = Server::start(Limits::default());
    let (cookie, csrf, fp, page) = prepared(
        &s,
        "root",
        "/role",
        &[
            ("operation", "role-grant"),
            ("target", "erin"),
            ("permission", "signoff_authority"),
            ("tenant_id", "t1"),
            ("project_id", "*"),
        ],
    );
    let grant = CeremonyOp::RoleChange {
        change: RoleChange::Grant {
            subject: SubjectId::new("writ-local", "erin"),
            permission: Permission::SignoffAuthority,
            tenant_id: "t1".into(),
            project_id: "*".into(),
        },
    };
    assert_displays(&page, &s.fake.context(&grant).unwrap().unwrap().context);
    assert_eq!(confirm(&s, &cookie, &csrf, &fp, "root"), grant);
    let (cookie, csrf, fp, _) = prepared(
        &s,
        "root",
        "/role",
        &[("operation", "role-revoke"), ("target", "role:7")],
    );
    let revoke = CeremonyOp::RoleChange {
        change: RoleChange::Revoke {
            assignment_id: "role:7".into(),
        },
    };
    assert_eq!(confirm(&s, &cookie, &csrf, &fp, "root"), revoke);
    // Malformed forms never reach the ledger.
    let (cookie, csrf, _) = open(&s, "root", "/role");
    for fields in [
        vec![
            ("operation", "role-grant"),
            ("target", "erin"),
            ("permission", "god"),
        ],
        vec![
            ("operation", "role-grant"),
            ("target", "erin"),
            ("permission", "approver"),
        ],
        vec![("operation", "role-revoke")],
        vec![("operation", "approve"), ("target", "req-1")],
    ] {
        let r = s.post("/prepare", &cookie, &body(&csrf, &fields), "");
        assert_eq!(status(&r), 400, "{r}");
    }
}

#[test]
fn enrollment_registers_in_the_operator_session_then_enrolls_through_that_operator() {
    let s = Server::start(Limits::default());
    // 1. The operator's registration start: displayed, then the operator's own ceremony.
    let (cookie, csrf, fp, page) = prepared(
        &s,
        "root",
        "/enroll",
        &[
            ("operation", "registration-start"),
            ("operator", "root"),
            ("subject", "newbie"),
        ],
    );
    assert!(page.contains("Registration start") && page.contains("newbie"));
    assert!(page.contains("id=\"register-new\" type=\"button\" hidden"));
    assert!(page.contains("id=\"prepare\" hidden"));
    let begin = |subject: &str| {
        s.post(
            "/ceremony/begin",
            &cookie,
            &body(
                &csrf,
                &[
                    ("fingerprint", &fp),
                    ("verdict", "confirm"),
                    ("subject", subject),
                ],
            ),
            "",
        )
    };
    // Only the operator the start names may begin it (nothing reaches the ledger).
    let r = begin("olga");
    assert_eq!(status(&r), 403, "{r}");
    assert!(r.ends_with("{\"error\":\"refused\"}"));
    assert!(s.fake.calls().is_empty());
    let begun = begin("root");
    assert_eq!(status(&begun), 200, "{begun}");
    let challenge = between(&begun, "\"challenge_id\":\"", "\"");
    let started = s.post(
        "/ceremony/finish",
        &cookie,
        &body(
            &csrf,
            &[
                ("challenge_id", &challenge),
                ("response", "valid-assertion"),
            ],
        ),
        "",
    );
    assert_eq!(status(&started), 200, "{started}");
    assert!(started.contains("\"result\":\"registration started\""));
    assert!(started.contains("\"options\":{\"publicKey\""));
    let id = between(&started, "\"registration_id\":\"", "\"");
    let Some(Call::Finish {
        op:
            CeremonyOp::RegistrationStart {
                subject,
                operator,
                operator_session,
            },
        ..
    }) = s.fake.calls().last().cloned()
    else {
        panic!("registration start");
    };
    assert_eq!(subject, SubjectId::new("writ-local", "newbie"));
    assert_eq!(operator, SubjectId::new("writ-local", "root"));
    // The page-session id is server-held and is not the session cookie.
    assert!(!operator_session.is_empty());
    assert!(!operator_session.contains(&cookie) && !started.contains(&operator_session));
    let session = OperatorSession {
        operator: operator.clone(),
        session_id: operator_session.clone(),
    };
    // 2. The new person's registration: another page session can neither finish nor
    // enroll this row.
    let (other_cookie, other_csrf, _) = open(&s, "root", "/enroll");
    let r = s.post(
        "/registration/finish",
        &other_cookie,
        &body(
            &other_csrf,
            &[("registration_id", &id), ("response", "valid-registration")],
        ),
        "",
    );
    assert_eq!(status(&r), 403, "{r}");
    // No registration row is ever begun without the start ceremony.
    let r = s.post(
        "/registration/begin",
        &cookie,
        &body(&csrf, &[("operator", "root"), ("subject", "x")]),
        "",
    );
    assert_eq!(status(&r), 404, "{r}");
    let finished = s.post(
        "/registration/finish",
        &cookie,
        &body(
            &csrf,
            &[("registration_id", &id), ("response", "valid-registration")],
        ),
        "",
    );
    assert_eq!(status(&finished), 200, "{finished}");
    assert!(finished.contains("\"result\":\"registered\""));
    let Some(Call::FinishRegistration {
        operator: finish_operator,
        ..
    }) = s.fake.calls().last().cloned()
    else {
        panic!("registration finish");
    };
    assert_eq!(
        finish_operator, session,
        "the same operator session finishes it"
    );
    // 3. The Enroll ceremony of the registered credential.
    let enroll_fields = [
        ("operation", "enroll"),
        ("registration_id", id.as_str()),
        ("linked", "adapter agent:session-9\n"),
        ("roles", "approver t1 p1\nrevoker t1 *"),
    ];
    let r = s.post(
        "/prepare",
        &other_cookie,
        &body(&other_csrf, &enroll_fields),
        "",
    );
    assert_eq!(status(&r), 400, "{r}");
    let r = s.post("/prepare", &cookie, &body(&csrf, &enroll_fields), "");
    assert_eq!(status(&r), 200, "{r}");
    let page = view_of(&s, &cookie);
    let fp = between(&page, "data-fingerprint=\"", "\"");
    for text in [
        "newbie",
        "agent:session-9",
        "approver",
        "revoker",
        "cred-newbie",
    ] {
        assert!(page.contains(text), "{text}");
    }
    let calls = s.fake.calls().len();
    let r = s.post(
        "/ceremony/begin",
        &cookie,
        &body(
            &csrf,
            &[
                ("fingerprint", &fp),
                ("verdict", "confirm"),
                ("subject", "olga"),
            ],
        ),
        "",
    );
    assert_eq!(status(&r), 403, "{r}");
    assert!(r.ends_with("{\"error\":\"refused\"}"));
    assert_eq!(s.fake.calls().len(), calls, "never reached the ledger");
    let op = confirm(&s, &cookie, &csrf, &fp, "root");
    let CeremonyOp::Enroll {
        enrollment,
        operator_session: enroll_session,
    } = op
    else {
        panic!("enroll");
    };
    assert_eq!(enroll_session, operator_session);
    assert_eq!(enrollment.subject, SubjectId::new("writ-local", "newbie"));
    assert_eq!(enrollment.kind, PrincipalKind::Human);
    assert_eq!(
        enrollment.credential.as_ref().map(|c| c.registration_id()),
        Some(id.as_str())
    );
    assert_eq!(
        enrollment.linked_identities,
        [SubjectId::new("adapter", "agent:session-9")]
    );
    assert_eq!(
        enrollment.roles,
        [
            NewRole {
                permission: Permission::Approver,
                tenant_id: "t1".into(),
                project_id: "p1".into()
            },
            NewRole {
                permission: Permission::Revoker,
                tenant_id: "t1".into(),
                project_id: "*".into()
            }
        ]
    );
    // The row is used: it cannot be prepared again in this session.
    let r = s.post("/prepare", &cookie, &body(&csrf, &enroll_fields), "");
    assert_eq!(status(&r), 400, "{r}");
    // A refused registration finish forgets the row; an outage keeps it (L6).
    let (cookie, csrf, fp, _) = prepared(
        &s,
        "root",
        "/enroll",
        &[("operation", "registration-start"), ("subject", "y")],
    );
    let begun = s.post(
        "/ceremony/begin",
        &cookie,
        &body(
            &csrf,
            &[
                ("fingerprint", &fp),
                ("verdict", "confirm"),
                ("subject", "root"),
            ],
        ),
        "",
    );
    let challenge = between(&begun, "\"challenge_id\":\"", "\"");
    let started = s.post(
        "/ceremony/finish",
        &cookie,
        &body(
            &csrf,
            &[
                ("challenge_id", &challenge),
                ("response", "valid-assertion"),
            ],
        ),
        "",
    );
    let id = between(&started, "\"registration_id\":\"", "\"");
    let fin = |response: &str| {
        s.post(
            "/registration/finish",
            &cookie,
            &body(&csrf, &[("registration_id", &id), ("response", response)]),
            "",
        )
    };
    assert_eq!(status(&fin("registration-down")), 503, "L6: kept");
    assert_eq!(status(&fin("forged")), 403);
    assert_eq!(status(&fin("valid-registration")), 403, "forgotten");
}

#[test]
fn prepared_pages_are_read_only_protected_and_never_stale() {
    let s = Server::start(Limits::default());
    let fields = [
        ("operation", "revoke"),
        ("target_kind", "credential"),
        ("target", "cred-x"),
        ("reason", "lost"),
    ];
    let (cookie, csrf, _) = open(&s, "carol", "/revoke");
    // CSRF, session and origin are required; nothing prepared means nothing to view.
    assert_eq!(
        status(&s.post("/prepare", &cookie, &body("bad", &fields), "")),
        403
    );
    assert_eq!(
        status(&s.post("/prepare", "no-session", &body(&csrf, &fields), "")),
        403
    );
    assert_eq!(status(&view_of(&s, &cookie)), 404);
    assert_eq!(
        status(&s.get("/view", "Sec-Fetch-Site: same-origin\r\n")),
        404
    );
    let r = s.raw(
        format!(
            "POST /prepare HTTP/1.1\r\nHost: localhost:{}\r\nOrigin: http://evil.example\r\n\
             Content-Type: application/x-www-form-urlencoded\r\nCookie: writ_session={cookie}\r\n\
             Content-Length: 0\r\n\r\n",
            s.port
        )
        .as_bytes(),
    );
    assert_eq!(status(&r), 403);
    // A ledger refusal of the target is one generic refusal.
    *s.fake.context_refusal.lock().unwrap() = true;
    let r = s.post("/prepare", &cookie, &body(&csrf, &fields), "");
    assert_eq!(status(&r), 403);
    assert!(r.ends_with("{\"error\":\"refused\"}"), "{r}");
    *s.fake.context_refusal.lock().unwrap() = false;
    // Preparing and viewing never begin anything.
    let (cookie, csrf, fp, _) = prepared(&s, "carol", "/revoke", &fields);
    assert!(s.fake.calls().is_empty());
    // The stored context changed after the display: stale, nothing begun.
    *s.fake.revision.lock().unwrap() += 1;
    let r = s.post(
        "/ceremony/begin",
        &cookie,
        &body(
            &csrf,
            &[
                ("fingerprint", &fp),
                ("verdict", "confirm"),
                ("subject", "carol"),
            ],
        ),
        "",
    );
    assert_eq!(status(&r), 409, "{r}");
    assert!(s.fake.calls().is_empty());
    // Re-displayed, it can be confirmed; while its ceremony is pending, the session's
    // target cannot be replaced.
    let r = s.post("/prepare", &cookie, &body(&csrf, &fields), "");
    assert_eq!(status(&r), 200);
    let fp = between(&view_of(&s, &cookie), "data-fingerprint=\"", "\"");
    let begun = s.post(
        "/ceremony/begin",
        &cookie,
        &body(
            &csrf,
            &[
                ("fingerprint", &fp),
                ("verdict", "confirm"),
                ("subject", "carol"),
            ],
        ),
        "",
    );
    assert_eq!(status(&begun), 200, "{begun}");
    let r = s.post("/prepare", &cookie, &body(&csrf, &fields), "");
    assert_eq!(status(&r), 409, "{r}");
    // The target page has the strict policy too.
    let page = view_of(&s, &cookie);
    let csp = header_of(&page, "content-security-policy").unwrap();
    assert!(csp.contains("default-src 'none'") && csp.contains("form-action 'none'"));
}

/// LOW-1: a page from another localhost port shares this origin's cookies (cookies are not
/// port-scoped) and its requests are `same-site`. A cookie planted there, and any page or
/// POST that is not a same-origin or direct navigation (or carries no Fetch Metadata), is
/// refused before a session is created, used or changed.
#[test]
fn a_cookie_planted_from_another_port_is_refused() {
    let s = Server::start(Limits::default());
    // The attacker's session on this listener (a direct navigation of its own).
    let (cookie, csrf, fp) = s.view();
    let planted = format!("Cookie: writ_session={cookie}\r\n");
    for site in ["same-site", "cross-site"] {
        let header = format!("Sec-Fetch-Site: {site}\r\n");
        // Pages that create a session: refused, no cookie set.
        for path in [
            "/request/req-1",
            "/signoff/so-1",
            "/revoke",
            "/role",
            "/enroll",
        ] {
            let r = s.get(path, &header);
            assert_eq!(status(&r), 403, "{site} {path}");
            assert!(header_of(&r, "set-cookie").is_none(), "{site} {path}");
        }
        // The page that uses the planted session.
        let r = s.get("/view", &format!("{header}{planted}"));
        assert_eq!(status(&r), 403, "{site} /view");
        // A POST in the planted session with this origin's CSRF token and Origin.
        let r = s.post(
            "/ceremony/begin",
            &cookie,
            &format!(
                "csrf={csrf}&request_id=req-1&fingerprint={}&verdict=deny&subject=bob",
                enc(&fp)
            ),
            &header,
        );
        assert_eq!(status(&r), 403, "{site} POST");
        assert!(r.ends_with("{\"error\":\"fetch site\"}"), "{r}");
    }
    // Missing Fetch Metadata is refused too (fail closed), for pages and POSTs.
    let r = s.raw(
        format!(
            "GET /request/req-1 HTTP/1.1\r\nHost: localhost:{}\r\nConnection: close\r\n\r\n",
            s.port
        )
        .as_bytes(),
    );
    assert_eq!(status(&r), 403, "{r}");
    let body = format!(
        "csrf={csrf}&request_id=req-1&fingerprint={}&verdict=deny&subject=bob",
        enc(&fp)
    );
    let r = s.raw(
        format!(
            "POST /ceremony/begin HTTP/1.1\r\nHost: localhost:{0}\r\nOrigin: {1}\r\n\
             Content-Type: application/x-www-form-urlencoded\r\nCookie: writ_session={cookie}\r\n\
             Content-Length: {2}\r\nConnection: close\r\n\r\n{body}",
            s.port,
            s.origin,
            body.len()
        )
        .as_bytes(),
    );
    assert_eq!(status(&r), 403, "{r}");
    // `none` (a user-initiated reload or bookmark) is refused on `/view` and on every POST:
    // both are reached only by this origin's own script (re-review residual of LOW-1).
    let none = "Sec-Fetch-Site: none\r\n";
    let r = s.get("/view", &format!("{none}{planted}"));
    assert_eq!(status(&r), 403, "none /view");
    let r = s.post("/ceremony/begin", &cookie, &body, none);
    assert_eq!(status(&r), 403, "none POST");
    assert!(r.ends_with("{\"error\":\"fetch site\"}"), "{r}");
    // Nothing reached the ledger.
    assert!(s.fake.calls().is_empty());
    // The static assets need no session and stay available to this origin's pages.
    assert_eq!(
        status(&s.get("/static/approval.js", "Sec-Fetch-Site: same-origin\r\n")),
        200
    );
}

// ---------------------------------------------------------------------------
// D-044 item 3 (M4-INTERFACES Z1..Z4): authenticated, scoped record reads.

/// The body of a response (after the header).
fn body_of(response: &str) -> &str {
    response.split_once("\r\n\r\n").map_or("", |(_, b)| b)
}

#[test]
fn z4_the_landing_page_offers_login_only() {
    let s = Server::start(Limits::default());
    let page = s.get("/", "");
    assert_eq!(status(&page), 200, "{page}");
    assert!(page.contains("<form id=\"login\">"));
    assert!(page.contains("<script src=\"/static/approval.js\" defer></script>"));
    assert_eq!(page.matches("<script").count(), 1);
    // No session, no CSRF token, no record and no record existence.
    assert!(header_of(&page, "set-cookie").is_none());
    for absent in [
        "data-csrf",
        "data-fingerprint",
        "req-1",
        "so-1",
        "&lt;script",
    ] {
        assert!(!page.contains(absent), "{absent}");
    }
    let csp = header_of(&page, "content-security-policy").unwrap();
    assert!(csp.contains("default-src 'none'") && csp.contains("frame-ancestors 'none'"));
    assert!(s.fake.calls().is_empty() && s.fake.logins.lock().unwrap().is_empty());
}

#[test]
fn z1_z2_a_login_binds_subject_and_credential_to_the_page_session_and_rotates_the_token() {
    let s = Server::start(Limits::default());
    // The login begin needs no session and creates a pre-authentication one.
    let begun = s.post("/login/begin", "", "subject=root", "");
    assert_eq!(status(&begun), 200, "{begun}");
    let pre = between(&begun, "writ_session=", ";");
    let cookie_attrs = header_of(&begun, "set-cookie").unwrap();
    assert!(cookie_attrs.contains("HttpOnly") && cookie_attrs.contains("SameSite=Strict"));
    let pre_csrf = between(&begun, "\"csrf\":\"", "\"");
    let challenge = between(&begun, "\"challenge_id\":\"", "\"");
    assert!(begun.contains("\"options\":{\"publicKey\""));
    let CeremonyOp::ReadSession {
        subject,
        page_session,
    } = s.fake.logins.lock().unwrap()[0].clone()
    else {
        panic!("a login begins a ReadSession ceremony")
    };
    assert_eq!(subject, SubjectId::new("writ-local", "root"));
    assert!(!page_session.is_empty() && !page_session.contains(&pre));
    // The pre-authentication session reads and changes nothing.
    assert_eq!(status(&s.get_as("/request/req-1", &pre)), 404);
    assert_eq!(status(&s.get_as("/revoke", &pre)), 404);
    let r = s.post(
        "/prepare",
        &pre,
        &body(&pre_csrf, &[("operation", "role-revoke"), ("target", "r")]),
        "",
    );
    assert_eq!((status(&r), body_of(&r)), (403, "{\"error\":\"refused\"}"));
    // A forged assertion: the one generic refusal (LOW-6), still unauthenticated.
    let forged = s.post(
        "/login/finish",
        &pre,
        &format!("csrf={pre_csrf}&challenge_id={challenge}&response=forged"),
        "",
    );
    assert_eq!(
        (status(&forged), body_of(&forged)),
        (403, "{\"error\":\"refused\"}")
    );
    assert_eq!(status(&s.get_as("/request/req-1", &pre)), 404);
    // A new login: success rotates the token and the CSRF token; the old ones are gone.
    let begun = s.post("/login/begin", &pre, "subject=root", "");
    let pre2 = between(&begun, "writ_session=", ";");
    let pre2_csrf = between(&begun, "\"csrf\":\"", "\"");
    let challenge = between(&begun, "\"challenge_id\":\"", "\"");
    assert_ne!(pre2, pre);
    let finish = format!("csrf={pre2_csrf}&challenge_id={challenge}&response=valid-assertion");
    let done = s.post("/login/finish", &pre2, &finish, "");
    assert_eq!(status(&done), 200, "{done}");
    assert!(done.contains("\"result\":\"signed in\"") && done.contains("\"subject\":\"root\""));
    let cookie = between(&done, "writ_session=", ";");
    let csrf = between(&done, "\"csrf\":\"", "\"");
    assert!(cookie != pre2 && csrf != pre2_csrf);
    assert!(header_of(&done, "set-cookie")
        .unwrap()
        .contains(&format!("Max-Age={MAX_SESSION_SECONDS}")));
    assert_eq!(status(&s.get_as("/revoke", &pre2)), 404, "old token");
    assert_eq!(status(&s.get_as("/revoke", &cookie)), 200);
    // A replayed login finish: with the old token or the new one, refused.
    assert_eq!(status(&s.post("/login/finish", &pre2, &finish, "")), 403);
    let replay = format!("csrf={csrf}&challenge_id={challenge}&response=valid-assertion");
    assert_eq!(status(&s.post("/login/finish", &cookie, &replay, "")), 403);
    // The page session the login bound is the one a registration start binds (D-043 Y2).
    let page_session = match s.fake.logins.lock().unwrap().last().cloned() {
        Some(CeremonyOp::ReadSession { page_session, .. }) => page_session,
        other => panic!("{other:?}"),
    };
    let r = s.post(
        "/prepare",
        &cookie,
        &body(
            &csrf,
            &[("operation", "registration-start"), ("subject", "newbie")],
        ),
        "",
    );
    assert_eq!(status(&r), 200, "{r}");
    let view = view_of(&s, &cookie);
    let fp = between(&view, "data-fingerprint=\"", "\"");
    let op = confirm(&s, &cookie, &csrf, &fp, "root");
    let CeremonyOp::RegistrationStart {
        operator,
        operator_session,
        ..
    } = op
    else {
        panic!("{op:?}")
    };
    assert_eq!(operator, SubjectId::new("writ-local", "root"));
    assert_eq!(operator_session, page_session);
}

/// The JSON skeleton of `body`: every string replaced by `""`, every integer by 0, arrays
/// keeping their length (the shape a probe can observe besides random values).
fn shape(body: &str) -> Value {
    fn walk(v: &Value) -> Value {
        match v {
            Value::Str(_) => json::s(""),
            Value::Int(_) => Value::Int(0),
            Value::Arr(items) => Value::Arr(items.iter().map(walk).collect()),
            Value::Obj(members) => {
                Value::Obj(members.iter().map(|(k, v)| (k.clone(), walk(v))).collect())
            }
            other => other.clone(),
        }
    }
    walk(&json::parse(body.as_bytes()).unwrap())
}

/// The header names of a response, without `date`, sorted.
fn header_names(response: &str) -> Vec<String> {
    let head = response.split("\r\n\r\n").next().unwrap();
    let mut names: Vec<String> = head
        .lines()
        .skip(1)
        .filter_map(|l| l.split_once(':').map(|(k, _)| k.to_ascii_lowercase()))
        .filter(|k| k != "date" && k != "content-length")
        .collect();
    names.sort();
    names
}

/// The sorted `allowCredentials` ids of a login begin's body.
fn allowed_ids(body: &str) -> Vec<String> {
    let v = json::parse(body.as_bytes()).unwrap();
    let ids: Vec<String> = v
        .get("options")
        .and_then(|o| o.get("publicKey"))
        .and_then(|p| p.get("allowCredentials"))
        .and_then(Value::as_arr)
        .unwrap()
        .iter()
        .map(|c| c.get("id").and_then(Value::as_str).unwrap().to_string())
        .collect();
    ids
}

/// D-044 review M1: a login begin never tells an enrolled subject from an unknown one.
#[test]
fn m1_a_refused_login_begin_is_indistinguishable_from_a_real_one() {
    let s = Server::start(Limits {
        login_burst: 100,
        ..Limits::default()
    });
    let real = s.post("/login/begin", "", "subject=bob", "");
    let decoy = s.post("/login/begin", "", &format!("subject={UNKNOWN}"), "");
    for r in [&real, &decoy] {
        assert_eq!(status(r), 200, "{r}");
        assert!(header_of(r, "set-cookie").unwrap().contains("HttpOnly"));
        assert!(between(r, "\"challenge_id\":\"", "\"").starts_with("chl-"));
        assert_eq!(between(r, "\"challenge_id\":\"", "\"").len(), 36);
    }
    assert_eq!(header_names(&real), header_names(&decoy));
    assert_eq!(shape(body_of(&real)), shape(body_of(&decoy)));
    let (real_ids, decoy_ids) = (allowed_ids(body_of(&real)), allowed_ids(body_of(&decoy)));
    assert_eq!(real_ids.len(), 4);
    assert_eq!(decoy_ids.len(), 4);
    // The real id is among the real begin's ids, padded with decoys; sorted either way.
    assert!(real_ids.contains(&real_credential("bob")));
    assert!(real_ids.windows(2).all(|w| w[0] <= w[1]));
    assert!(decoy_ids.windows(2).all(|w| w[0] <= w[1]));
    // Repeated probes see the same ids (no fresh randomness to tell a decoy by); the
    // challenges differ.
    for (subject, ids) in [("bob", &real_ids), (UNKNOWN, &decoy_ids)] {
        let again = s.post("/login/begin", "", &format!("subject={subject}"), "");
        assert_eq!(&allowed_ids(body_of(&again)), ids, "{subject}");
        assert_ne!(
            between(&again, "\"challenge\":\"", "\""),
            between(&real, "\"challenge\":\"", "\"")
        );
    }
    // Another unknown subject gets other decoys.
    let other = s.post("/login/begin", "", "subject=nobody-else-at-all", "");
    assert_ne!(allowed_ids(body_of(&other)), decoy_ids);
    // The decoy's finish fails exactly like a forged assertion on the real one.
    let finish = |r: &str, response: &str| {
        let pre = between(r, "writ_session=", ";");
        s.post(
            "/login/finish",
            &pre,
            &format!(
                "csrf={}&challenge_id={}&response={response}",
                between(r, "\"csrf\":\"", "\""),
                between(r, "\"challenge_id\":\"", "\"")
            ),
            "",
        )
    };
    let on_decoy = finish(&decoy, "valid-assertion");
    let on_real = finish(&real, "forged");
    assert_eq!(status(&on_decoy), status(&on_real));
    assert_eq!(body_of(&on_decoy), body_of(&on_real));
    assert_eq!(header_names(&on_decoy), header_names(&on_real));
    assert_eq!(body_of(&on_decoy), "{\"error\":\"refused\"}");
}

/// D-044 review M2: logins have their own bound and rate limit and never take the mutation
/// slots; a login flood leaves a signed-in approver's begin unaffected.
#[test]
fn m2_a_login_flood_never_locks_out_an_approver() {
    let s = Server::start(Limits {
        max_logins: 3,
        login_burst: 50,
        max_outstanding: 2,
        ..Limits::default()
    });
    let approver = s.view();
    // The login bound: three live login sessions; a fourth evicts the oldest (re-review
    // M2: a lockout needs the full sustained rate), whose finish is then refused.
    let first = s.post("/login/begin", "", "subject=x", "");
    for _ in 0..3 {
        assert_eq!(status(&s.post("/login/begin", "", "subject=x", "")), 200);
    }
    let evicted = s.post(
        "/login/finish",
        &between(&first, "writ_session=", ";"),
        &format!(
            "csrf={}&challenge_id={}&response=valid-assertion",
            between(&first, "\"csrf\":\"", "\""),
            between(&first, "\"challenge_id\":\"", "\"")
        ),
        "",
    );
    assert_eq!(status(&evicted), 403, "{evicted}");
    // The approver's mutation slots are untouched by the flood.
    assert_eq!(status(&begin_in(&s, &approver)), 200);
    assert_eq!(status(&begin_in(&s, &approver)), 200);
    assert_eq!(status(&begin_in(&s, &approver)), 429, "its own bound only");
    // The process-wide rate limit.
    let r = Server::start(Limits {
        login_burst: 2,
        login_per_second: 1,
        ..Limits::default()
    });
    assert_eq!(status(&r.post("/login/begin", "", "subject=x", "")), 200);
    assert_eq!(status(&r.post("/login/begin", "", "subject=x", "")), 200);
    let limited = r.post("/login/begin", "", "subject=x", "");
    assert_eq!(
        (status(&limited), body_of(&limited)),
        (429, "{\"error\":\"rate\"}")
    );
    thread::sleep(Duration::from_millis(1100));
    assert_eq!(status(&r.post("/login/begin", "", "subject=x", "")), 200);
}

/// D-044 review LOW-3: the operator forms need the live permission (the ledger's read);
/// a roleless or revoked session gets the one 404.
#[test]
fn low3_operator_forms_need_the_live_permission() {
    let s = Server::start(Limits::default());
    let reference = body_of(&s.get("/revoke", "")).to_string();
    let (root, _) = s.login("root");
    let (mallory, _) = s.login(NO_ROLES);
    let (dave, _) = s.login("dave");
    s.fake.revoke("dave");
    for path in ["/revoke", "/role", "/enroll"] {
        assert_eq!(status(&s.get_as(path, &root)), 200, "{path}");
        for cookie in [&mallory, &dave] {
            let r = s.get_as(path, cookie);
            assert_eq!(status(&r), 404, "{path}");
            assert_eq!(body_of(&r), reference, "{path}");
        }
    }
}

#[test]
fn z3_anonymous_guessed_out_of_scope_revoked_and_expired_reads_are_refused_identically() {
    let s = Server::start(Limits::default());
    let reference = s.get("/request/req-1", "");
    assert_eq!(status(&reference), 404);
    let refused = body_of(&reference).to_string();
    let same = |r: &str, what: &str| {
        assert_eq!(status(r), 404, "{what}: {r}");
        assert_eq!(body_of(r), refused, "{what}");
        assert!(header_of(r, "set-cookie").is_none(), "{what}");
    };
    // Anonymous: every page and direct URL, no session created.
    for path in [
        "/request/req-1",
        "/request/nope",
        "/signoff/so-1",
        "/signoff/so-x",
        "/revoke",
        "/role",
        "/enroll",
        "/nope",
    ] {
        same(&s.get(path, ""), path);
    }
    same(
        &s.get("/view", "Sec-Fetch-Site: same-origin\r\n"),
        "anonymous /view",
    );
    same(
        &s.get_as("/request/req-1", "guessed-token"),
        "guessed token",
    );
    // Signed in: guessed or unknown ids.
    let (bob, _) = s.login("bob");
    assert_eq!(status(&s.get_as("/request/req-1", &bob)), 200);
    for path in ["/request/nope", "/request/req-2", "/signoff/so-x"] {
        same(&s.get_as(path, &bob), path);
    }
    // Signed in without a role for the target (out of scope).
    let (mallory, mallory_csrf) = s.login(NO_ROLES);
    for path in ["/request/req-1", "/signoff/so-1"] {
        same(&s.get_as(path, &mallory), path);
    }
    // A session whose subject, credential or role was revoked after it opened.
    let (dave, _) = s.login("dave");
    let page = s.get_as("/request/req-1", &dave);
    assert_eq!(status(&page), 200);
    let dave_csrf = between(&page, "data-csrf=\"", "\"");
    let fp = between(&page, "data-fingerprint=\"", "\"");
    s.fake.revoke("dave");
    same(&s.get_as("/request/req-1", &dave), "revoked");
    // Its begin is refused too (the stale-display check is a protected read).
    let r = s.post(
        "/ceremony/begin",
        &dave,
        &format!(
            "csrf={dave_csrf}&request_id=req-1&fingerprint={}&verdict=approve",
            enc(&fp)
        ),
        "",
    );
    assert_eq!((status(&r), body_of(&r)), (403, "{\"error\":\"refused\"}"));
    // `/prepare` (a read) answers one refusal: anonymous, a wrong CSRF token, out of scope,
    // revoked.
    let fields = [("operation", "role-revoke"), ("target", "role:1")];
    for (cookie, csrf) in [
        ("", "x"),
        (mallory.as_str(), "wrong"),
        (mallory.as_str(), mallory_csrf.as_str()),
        (dave.as_str(), dave_csrf.as_str()),
    ] {
        let r = s.post("/prepare", cookie, &body(csrf, &fields), "");
        assert_eq!((status(&r), body_of(&r)), (403, "{\"error\":\"refused\"}"));
    }
    assert!(s.fake.calls().is_empty(), "nothing begun");
    // An expired session.
    let short = Server::start(Limits {
        session_ttl: Duration::from_secs(1),
        ..Limits::default()
    });
    let (cookie, _) = short.login("bob");
    assert_eq!(status(&short.get_as("/request/req-1", &cookie)), 200);
    thread::sleep(Duration::from_millis(1100));
    same(&short.get_as("/request/req-1", &cookie), "expired");
}

#[test]
fn z3_every_mutation_is_begun_for_the_signed_in_subject_and_never_by_the_login() {
    let s = Server::start(Limits::default());
    let (cookie, csrf, fp) = s.view();
    // Another subject than the signed-in one: refused before the ledger.
    let r = s.post(
        "/ceremony/begin",
        &cookie,
        &format!(
            "csrf={csrf}&request_id=req-1&fingerprint={}&verdict=approve&subject=alice",
            enc(&fp)
        ),
        "",
    );
    assert_eq!((status(&r), body_of(&r)), (403, "{\"error\":\"refused\"}"));
    assert!(s.fake.calls().is_empty());
    // Without a subject field: the read session's subject.
    let begun = s.post(
        "/ceremony/begin",
        &cookie,
        &format!(
            "csrf={csrf}&request_id=req-1&fingerprint={}&verdict=approve",
            enc(&fp)
        ),
        "",
    );
    assert_eq!(status(&begun), 200, "{begun}");
    let Call::Begin { subject, op, .. } = &s.fake.calls()[0] else {
        panic!()
    };
    assert_eq!(subject, &SubjectId::new("writ-local", "bob"));
    assert!(matches!(op, CeremonyOp::Approve { .. }));
    let challenge = between(&begun, "\"challenge_id\":\"", "\"");
    // A mutation's challenge is never finished as a login, and the login's challenge is
    // never finished as a mutation (purpose separation; the ledger checks it again).
    let r = s.post(
        "/login/finish",
        &cookie,
        &format!("csrf={csrf}&challenge_id={challenge}&response=valid-assertion"),
        "",
    );
    assert_eq!(status(&r), 403, "{r}");
    let login_challenge = format!("chl-{:032x}", s.fake.logins.lock().unwrap().len());
    let r = s.post(
        "/ceremony/finish",
        &cookie,
        &format!("csrf={csrf}&challenge_id={login_challenge}&response=valid-assertion"),
        "",
    );
    assert_eq!(status(&r), 403, "{r}");
    // A pre-authentication session cannot finish a login challenge as a mutation either.
    let begun_login = s.post("/login/begin", "", "subject=bob", "");
    let pre = between(&begun_login, "writ_session=", ";");
    let pre_csrf = between(&begun_login, "\"csrf\":\"", "\"");
    let login_challenge = between(&begun_login, "\"challenge_id\":\"", "\"");
    let r = s.post(
        "/ceremony/finish",
        &pre,
        &format!("csrf={pre_csrf}&challenge_id={login_challenge}&response=valid-assertion"),
        "",
    );
    assert_eq!(status(&r), 403, "{r}");
    // Only the real finish of the mutation reaches the ledger.
    let done = s.post(
        "/ceremony/finish",
        &cookie,
        &format!("csrf={csrf}&challenge_id={challenge}&response=valid-assertion"),
        "",
    );
    assert_eq!(status(&done), 200, "{done}");
    assert_eq!(s.fake.calls().len(), 2);
}

/// D-044 re-review M1 (timing): every login begin and finish answer, success or refusal,
/// real or decoy, takes at least the latency floor, and the refusals fall in one band.
#[test]
fn m1_every_login_answer_is_held_to_the_latency_floor() {
    let floor = Duration::from_millis(150);
    let s = Server::start(Limits {
        login_floor: floor,
        login_burst: 100,
        ..Limits::default()
    });
    let timed = |path: &str, cookie: &str, body: &str| {
        let started = Instant::now();
        let r = s.post(path, cookie, body, "");
        (r, started.elapsed())
    };
    let finish_body = |r: &str, response: &str| {
        format!(
            "csrf={}&challenge_id={}&response={response}",
            between(r, "\"csrf\":\"", "\""),
            between(r, "\"challenge_id\":\"", "\"")
        )
    };
    let mut refusals = Vec::new();
    for _ in 0..3 {
        let (real, t1) = timed("/login/begin", "", "subject=bob");
        let (decoy, t2) = timed("/login/begin", "", &format!("subject={UNKNOWN}"));
        let (bad, t3) = timed(
            "/login/finish",
            &between(&real, "writ_session=", ";"),
            &finish_body(&real, "forged"),
        );
        let (on_decoy, t4) = timed(
            "/login/finish",
            &between(&decoy, "writ_session=", ";"),
            &finish_body(&decoy, "valid-assertion"),
        );
        assert_eq!(body_of(&bad), body_of(&on_decoy));
        for t in [t1, t2, t3, t4] {
            assert!(t >= floor, "{t:?} below the floor");
        }
        refusals.extend([t3, t4]);
    }
    let (good, t5) = timed("/login/begin", "", "subject=bob");
    let (_, t6) = timed(
        "/login/finish",
        &between(&good, "writ_session=", ";"),
        &finish_body(&good, "valid-assertion"),
    );
    assert!(t5 >= floor && t6 >= floor);
    let (lo, hi) = (
        refusals.iter().min().unwrap(),
        refusals.iter().max().unwrap(),
    );
    assert!(*hi - *lo < Duration::from_millis(60), "band {lo:?}..{hi:?}");
}

/// D-044 re-review M2: refused and decoy logins free their slot at once, and at the bound
/// the oldest login session is evicted, so a burst of decoy begins never blocks a sign-in.
#[test]
fn m2_failed_logins_free_their_slot_and_the_bound_evicts_the_oldest() {
    let s = Server::start(Limits {
        login_burst: 100,
        login_floor: Duration::from_millis(1),
        ..Limits::default()
    });
    for _ in 0..16 {
        assert_eq!(
            status(&s.post("/login/begin", "", &format!("subject={UNKNOWN}"), "")),
            200
        );
    }
    // An approver still signs in (begin and finish).
    let (cookie, _) = s.login("bob");
    assert_eq!(status(&s.get_as("/request/req-1", &cookie)), 200);
    // A refused finish ends its login session: the same session cannot try again.
    let begun = s.post("/login/begin", "", "subject=bob", "");
    let pre = between(&begun, "writ_session=", ";");
    let body = |response: &str| {
        format!(
            "csrf={}&challenge_id={}&response={response}",
            between(&begun, "\"csrf\":\"", "\""),
            between(&begun, "\"challenge_id\":\"", "\"")
        )
    };
    assert_eq!(
        status(&s.post("/login/finish", &pre, &body("forged"), "")),
        403
    );
    assert_eq!(
        status(&s.post("/login/finish", &pre, &body("valid-assertion"), "")),
        403
    );
    // A refused finish never ends an authenticated session.
    let r = s.post(
        "/login/finish",
        &cookie,
        "csrf=x&challenge_id=y&response=z",
        "",
    );
    assert_eq!(status(&r), 403);
    assert_eq!(status(&s.get_as("/request/req-1", &cookie)), 200);
}

/// D-044 re-review item 4: decoy ids take the byte lengths of the enrolled credentials
/// (the default 16 bytes when none is enrolled).
#[test]
fn m1_decoy_ids_take_enrolled_credential_lengths() {
    let s = Server::start(Limits {
        login_burst: 100,
        login_floor: Duration::from_millis(1),
        ..Limits::default()
    });
    // Unpadded base64url: 20 bytes are 27 characters, 32 bytes 43, 16 bytes 22.
    let mut seen = std::collections::BTreeSet::new();
    for n in 0..8 {
        let r = s.post("/login/begin", "", &format!("subject={UNKNOWN}-{n}"), "");
        *s.fake.lengths.lock().unwrap() = vec![20, 32];
        for id in allowed_ids(body_of(&r)) {
            seen.insert(id.len());
        }
    }
    assert!(seen.iter().all(|l| [27, 43].contains(l)), "{seen:?}");
    *s.fake.lengths.lock().unwrap() = vec![];
    let r = s.post("/login/begin", "", &format!("subject={UNKNOWN}-x"), "");
    assert!(allowed_ids(body_of(&r)).iter().all(|id| id.len() == 22));
}

/// D-044 re-review items 3 and 5: a verifier outage is global (503 for every subject for a
/// while), and options with more credentials than the padded count fail closed.
#[test]
fn m1_outage_answers_503_for_every_subject_and_too_many_credentials_fail_closed() {
    let s = Server::start(Limits {
        login_burst: 100,
        login_floor: Duration::from_millis(1),
        ..Limits::default()
    });
    let many = s.post("/login/begin", "", &format!("subject={MANY}"), "");
    assert_eq!(status(&many), 503, "{many}");
    let out = s.post("/login/begin", "", &format!("subject={OUTAGE}"), "");
    assert_eq!(status(&out), 503, "{out}");
    for subject in [UNKNOWN, "bob"] {
        let r = s.post("/login/begin", "", &format!("subject={subject}"), "");
        assert_eq!(status(&r), 503, "{subject}: {r}");
        assert!(header_of(&r, "set-cookie").is_none());
    }
}

/// J-8 (M5a-11): while stopping, the listener keeps accepting and answers every new request
/// 503 `stopping`; a request already in progress finishes normally; the drain observes it;
/// exit then ends the accept loop.
#[test]
fn stopping_refuses_new_requests_with_503_and_lets_in_flight_requests_finish() {
    let limits = Limits {
        login_floor: Duration::from_millis(800),
        ..Limits::default()
    };
    let mut s = Server::start(limits);
    assert_eq!(status(&s.get("/", "")), 200);
    assert_eq!(s.control.in_flight(), 0);
    thread::scope(|scope| {
        // A login begin is held to the latency floor: in progress across the stop.
        let held = scope.spawn(|| s.post("/login/begin", "", "subject=bob", ""));
        let until = Instant::now() + Duration::from_secs(5);
        while s.control.in_flight() == 0 && Instant::now() < until {
            thread::sleep(Duration::from_millis(5));
        }
        assert_eq!(s.control.in_flight(), 1, "the login begin is in progress");
        s.control.stop_accepting_work();
        assert!(s.control.is_stopping());
        // New connections are still accepted and answered 503 (no new ceremony begins).
        for (path, post) in [("/", false), ("/login/begin", true)] {
            let r = if post {
                s.post(path, "", "subject=carol", "")
            } else {
                s.get(path, "")
            };
            assert_eq!(status(&r), 503, "{r}");
            assert!(r.contains("\"error\":\"stopping\""), "{r}");
        }
        assert!(!s
            .control
            .wait_idle(Instant::now() + Duration::from_millis(20)));
        assert!(s.control.wait_idle(Instant::now() + Duration::from_secs(5)));
        let done = held.join().unwrap();
        assert_eq!(status(&done), 200, "the in-flight request finished: {done}");
    });
    s.control.exit();
    s.thread.take().unwrap().join().unwrap();
}

/// J-8 socket activation: an inherited listening socket is served only when it is bound to
/// 127.0.0.1 on exactly the configured port (a non-loopback address is refused by the same
/// check; this file never binds one).
#[test]
fn an_inherited_socket_must_be_loopback_on_the_configured_port() {
    let config = |port| ListenerConfig {
        port,
        subject_issuer: "writ-local".into(),
        limits: Limits::default(),
    };
    let backend = || Arc::new(Fake::new()) as Arc<dyn Backend>;
    let held = std::net::TcpListener::bind(("127.0.0.1", 0)).unwrap();
    let port = held.local_addr().unwrap().port();
    let other = if port == 65535 { port - 1 } else { port + 1 };
    for bad in [0, other] {
        let l = held.try_clone().unwrap();
        let e = ApprovalListener::from_std(l, config(bad), backend()).err();
        assert_eq!(
            e.map(|e| e.kind()),
            Some(std::io::ErrorKind::InvalidInput),
            "{bad}"
        );
    }
    let l = ApprovalListener::from_std(held, config(port), backend()).unwrap();
    assert_eq!(l.origin(), format!("http://localhost:{port}"));
}
