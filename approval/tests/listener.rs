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
use writ_approval::listener::{ApprovalListener, Limits, ListenerConfig, MAX_SESSION_SECONDS};
use writ_broker::approval::{
    context_fingerprint, ApprovalCode, ApprovalRequest, ApprovalStatus, ChallengeBinding,
    ChallengeOperation, NewApprovalRequest, NewSignoff, RevocationTarget, SignoffExclusions,
};
use writ_broker::approval_store::{
    revocation_context, role_change_context, CeremonyContext, CeremonyOp, CeremonyOutcome,
    CeremonyResult, IssuedChallenge, IssuedRegistration, NewRole, OperatorSession, RoleChange,
    SignoffParties,
};
use writ_broker::human::{
    ActiveCredentials, ApprovalChannel, CeremonyStart, CeremonyState, HumanVerifier, Permission,
    PrincipalKind, RegistrationBinding, RegistrationOperation, RegistrationStart,
    RegistrationVerifier, RelyingParty, SubjectId, VerifiedAssertion, VerifiedRegistration,
    VerifierCapability, VerifierError, ES256,
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
    BeginRegistration {
        subject: SubjectId,
        operator: OperatorSession,
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
        expires_at: Timestamp::parse_strict("2026-11-14T10:10:00Z").unwrap(),
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
        }
    }

    fn calls(&self) -> Vec<Call> {
        self.calls.lock().unwrap().clone()
    }
}

impl Backend for Fake {
    fn request(
        &self,
        request_id: &str,
    ) -> Result<Option<(ApprovalRequest, ApprovalStatus)>, Unavailable> {
        let r = self.request.lock().unwrap().clone();
        Ok((request_id == r.request_id()).then_some((r, ApprovalStatus::Pending)))
    }

    fn begin(
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

    fn finish(
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
            CeremonyOp::Enroll { enrollment, .. } => CeremonyResult::Enrolled {
                subject: enrollment.subject.clone(),
            },
            CeremonyOp::RoleChange { .. } => CeremonyResult::RoleChanged {
                assignment_id: "role-1".into(),
            },
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
            CeremonyOp::Approve { .. } | CeremonyOp::Deny { .. } => {
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
            CeremonyOp::Signoff { binding, parties } => {
                let excluded = SignoffExclusions {
                    requesters: vec![SubjectId::new("adapter", HOSTILE)],
                    agents: vec![SubjectId::new("adapter", "agent-7")],
                    added: parties.added.clone(),
                    ..SignoffExclusions::default()
                };
                let mut b = (**binding).clone();
                b.state_revision += revision;
                (
                    b.context_with(&excluded),
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

    fn pending_signoff(&self, signoff_id: &str) -> Result<Option<NewSignoff>, Unavailable> {
        Ok((signoff_id == "so-1").then(signoff_binding))
    }

    fn begin_registration(
        &self,
        subject: &SubjectId,
        operator: &OperatorSession,
    ) -> Result<Result<IssuedRegistration, Refusal>, Unavailable> {
        self.calls.lock().unwrap().push(Call::BeginRegistration {
            subject: subject.clone(),
            operator: operator.clone(),
        });
        if operator.operator.subject == "mallory" {
            return Ok(Err(Refusal::Approval(ApprovalCode::PermissionMissing)));
        }
        let mut rows = self.rows.lock().unwrap();
        let id = format!("reg-{}", rows.len() + 1);
        rows.push((id.clone(), subject.clone()));
        Ok(Ok(IssuedRegistration {
            registration_id: id,
            client_options: br#"{"publicKey":{"challenge":"AAAA","user":{"id":"AAAA"}}}"#.to_vec(),
            expires_at: Timestamp::parse_strict("2026-11-14T10:10:00Z").unwrap(),
        }))
    }

    fn finish_registration(
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
}

struct Server {
    fake: Arc<Fake>,
    port: u16,
    origin: String,
    stop: Arc<AtomicBool>,
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
        let thread = thread::spawn(move || listener.serve(s).unwrap());
        Server {
            fake,
            port: addr.port(),
            origin,
            stop,
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

    fn get(&self, path: &str, extra: &str) -> String {
        self.raw(
            format!(
                "GET {path} HTTP/1.1\r\nHost: localhost:{}\r\n{extra}Connection: close\r\n\r\n",
                self.port
            )
            .as_bytes(),
        )
    }

    fn post(&self, path: &str, cookie: &str, body: &str, extra: &str) -> String {
        self.raw(
            format!(
                "POST {path} HTTP/1.1\r\nHost: localhost:{}\r\nOrigin: {}\r\n\
                 Content-Type: application/x-www-form-urlencoded\r\nCookie: writ_session={cookie}\r\n\
                 Content-Length: {}\r\n{extra}Connection: close\r\n\r\n{body}",
                self.port,
                self.origin,
                body.len()
            )
            .as_bytes(),
        )
    }

    /// View the request: (session cookie, CSRF token, fingerprint).
    fn view(&self) -> (String, String, String) {
        let page = self.get("/request/req-1", "");
        assert_eq!(status(&page), 200, "{page}");
        let cookie = between(&page, "writ_session=", ";");
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
    let page = s.get("/request/req-1", "");
    assert_eq!(status(&page), 200);
    let csp = header_of(&page, "content-security-policy").unwrap();
    assert!(csp.contains("default-src 'none'") && csp.contains("script-src 'self'"));
    assert!(csp.contains("frame-ancestors 'none'"));
    assert_eq!(header_of(&page, "x-content-type-options"), Some("nosniff"));
    assert_eq!(header_of(&page, "cache-control"), Some("no-store"));
    let cookie = header_of(&page, "set-cookie").unwrap();
    assert!(cookie.contains("HttpOnly") && cookie.contains("SameSite=Strict"));
    assert!(cookie.contains(&format!("Max-Age={MAX_SESSION_SECONDS}")));
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
    let unknown = s.get("/request/nope", "");
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
    assert_eq!(status(&s.get("/request/req-1", "")), 200);
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
    assert_eq!(status(&s.get("/", "")), 404);
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
    let (a, b) = (s.view(), s.view());
    let first = begin_in(&s, &a);
    assert_eq!(status(&first), 200);
    assert_eq!(status(&begin_in(&s, &a)), 200);
    assert_eq!(status(&begin_in(&s, &b)), 200);
    // Each session is under its own bound (4), the listener is at its global bound (3).
    let over = begin_in(&s, &b);
    assert_eq!(status(&over), 429, "{over}");
    let third = s.view();
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
    // Both sessions are pending: a third view is refused, and nothing was evicted.
    let refused = s.get("/request/req-1", "");
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

/// A GET that opens a page session: (cookie, CSRF token, the response).
fn open(s: &Server, path: &str) -> (String, String, String) {
    let page = s.get(path, "");
    assert_eq!(status(&page), 200, "{page}");
    let cookie = between(&page, "writ_session=", ";");
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
    s.get("/view", &format!("Cookie: writ_session={cookie}\r\n"))
}

/// Prepare `fields` in a session of form page `path`, then display it: (cookie, csrf,
/// fingerprint, the displayed page).
fn prepared(s: &Server, path: &str, fields: &[(&str, &str)]) -> (String, String, String, String) {
    let (cookie, csrf, _) = open(s, path);
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
    let (cookie, csrf, page) = open(&s, "/signoff/so-1");
    let op = CeremonyOp::Signoff {
        binding: Box::new(signoff_binding()),
        parties: SignoffParties::default(),
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
    assert_eq!(status(&s.get("/signoff/so-unknown", "")), 404);
    // The verdict of a request page is not accepted on a confirmation page.
    let (cookie, csrf, page) = open(&s, "/signoff/so-1");
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
    let (cookie, csrf, _) = open(&s, "/role");
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
    let (cookie, csrf, page) = open(&s, "/enroll");
    assert!(page.contains("id=\"register\"") && page.contains("id=\"prepare\""));
    let begun = s.post(
        "/registration/begin",
        &cookie,
        &body(&csrf, &[("operator", "root"), ("subject", "newbie")]),
        "",
    );
    assert_eq!(status(&begun), 200, "{begun}");
    let id = between(&begun, "\"registration_id\":\"", "\"");
    let Some(Call::BeginRegistration { subject, operator }) = s.fake.calls().last().cloned() else {
        panic!("registration begin");
    };
    assert_eq!(subject, SubjectId::new("writ-local", "newbie"));
    assert_eq!(operator.operator, SubjectId::new("writ-local", "root"));
    // The page-session id is server-held and is not the session cookie.
    assert!(!operator.session_id.is_empty());
    assert!(!operator.session_id.contains(&cookie) && !begun.contains(&operator.session_id));
    // Another page session can neither finish nor enroll this row.
    let (other_cookie, other_csrf, _) = open(&s, "/enroll");
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
        finish_operator, operator,
        "the same operator session finishes it"
    );
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
    // In its own session: displayed, then begun only by the registering operator.
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
        operator_session,
    } = op
    else {
        panic!("enroll");
    };
    assert_eq!(operator_session, operator.session_id);
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
    // A refused registration begin is one generic refusal; a refused finish forgets the row.
    let r = s.post(
        "/registration/begin",
        &cookie,
        &body(&csrf, &[("operator", "mallory"), ("subject", "x")]),
        "",
    );
    assert_eq!(status(&r), 403);
    assert!(r.ends_with("{\"error\":\"refused\"}"), "{r}");
    let begun = s.post(
        "/registration/begin",
        &cookie,
        &body(&csrf, &[("operator", "root"), ("subject", "y")]),
        "",
    );
    let id = between(&begun, "\"registration_id\":\"", "\"");
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
    let (cookie, csrf, _) = open(&s, "/revoke");
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
    assert_eq!(status(&s.get("/view", "")), 404);
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
    let (cookie, csrf, fp, _) = prepared(&s, "/revoke", &fields);
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
