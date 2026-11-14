//! Non-shipping fixture of the end-to-end browser suite (approval/browser-tests/; D-043 item
//! 4). Never part of a release build (a Cargo example); it only prepares and inspects a
//! disposable ledger that the suite created in its own temporary directory.
//!
//! `setup --ledger <new path> --origin http://localhost:<port> --issuer <issuer>
//! --admin <subject> --approver <subject> --requests <id,id,...>`:
//! 1. creates the ledger (`SqliteLedger::create`) and releases it;
//! 2. bootstraps the administrator through [`BootstrapSession`] (the documented one-shot
//!    entry point, exclusive ownership) with the production [`PasskeyVerifier`]: the
//!    `bootstrap` registration's creation options go to the browser, whose CDP virtual
//!    authenticator creates the credential (simulated user verification);
//! 3. reopens the ledger through the normal ownership path and enrolls the approver (role
//!    `approver` in every scope), and with `--target <subject>` that subject too (role
//!    `approver` in `t-browser`/`p-browser`), each through the administrator's
//!    user-verified registration-start ceremony (D-043 Y2), the new credential created in
//!    the browser, and the administrator's `Enroll` ceremony, all performed in the browser;
//! 4. records one pending approval request per id (expiry 600 s) and exits, releasing the
//!    ledger for `writ-approval-server`.
//!
//! The protocol is JSON lines: the fixture writes `{"step":"create"|"get","subject":..,
//! "options":..}` and reads `{"credential":"<JSON of the PublicKeyCredential>"}`; it ends
//! with `{"step":"done","requests":{id: fingerprint}}`.
//!
//! `status --ledger <path> --requests <id,...>` prints `{"status":{id: status}}` (reads only;
//! no approval channel is configured, so no ceremony can run).

use std::collections::BTreeMap;
use std::io::{BufRead, Write};
use std::path::PathBuf;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use std::sync::Mutex;
use writ_approval::page::json_string;
use writ_approval::verifier::PasskeyVerifier;
use writ_broker::acceptance::{mac_bytes, MAC_ALG, RECORD_CONTRACT_V2, RECORD_PURPOSE};
use writ_broker::approval::{ApprovalRequest, NewApprovalRequest};
use writ_broker::approval_store::{
    ApprovalLedger, BootstrapEnrollment, CeremonyBegin, CeremonyFinish, CeremonyOp, CeremonyResult,
    NewEnrollment, NewRole, OperatorSession, RegistrationBegin, RegistrationFinish,
    RegistrationLedger,
};
use writ_broker::clock::EffectiveClock;
use writ_broker::clock::{
    observe, timestamp_from_parts, Boottime, FixedClock, HostClock, ObservedTime, SystemWall,
};
use writ_broker::execute::Broker;
use writ_broker::human::{ApprovalChannel, Permission, PrincipalKind, SubjectId};
use writ_broker::ingress::{verify_ingress, Seal, VerifiedRequest};
use writ_broker::keys::{
    AcceptanceRecordKey, IngressKey, MemoryKeyRing, MemoryKeys, PermitKey, RunnerAttestationKey,
};
use writ_broker::policy::{ActivePolicy, PolicyState};
use writ_broker::store::ExecuteLedger;
use writ_broker::workflow::{
    AcceptanceRecordSource, AdminOperation, AdminPresentation, AdminRefusal, AdminRequest,
    AttestationSource, AuthorizedOperator, SnapshotCheck, SnapshotStore, SourceUnavailable, Trust,
    WorkflowAuthority, WorkflowKey,
};
use writ_broker::writ_kernel::json as kjson;
use writ_broker::writ_kernel::json::{Digest, Value};
use writ_broker::writ_kernel::policy::load_bundle;
use writ_broker::writ_kernel::time::Timestamp;
use writ_ledger_sqlite::{BootstrapSession, SqliteEffectStore, SqliteLedger};

type Fallible<T> = Result<T, String>;

fn args() -> Fallible<(String, BTreeMap<String, String>)> {
    let mut it = std::env::args().skip(1);
    let command = it.next().ok_or("usage: browser_fixture setup|status ...")?;
    let mut out = BTreeMap::new();
    while let Some(k) = it.next() {
        let k = k.strip_prefix("--").ok_or(format!("bad argument {k}"))?;
        let v = it.next().ok_or(format!("--{k} has no value"))?;
        out.insert(k.to_string(), v);
    }
    Ok((command, out))
}

fn need<'a>(a: &'a BTreeMap<String, String>, k: &str) -> Fallible<&'a str> {
    a.get(k).map(String::as_str).ok_or(format!("missing --{k}"))
}

/// One JSON line (`v` is already JSON text).
fn emit(v: &str) -> Fallible<()> {
    let mut out = std::io::stdout().lock();
    writeln!(out, "{v}")
        .and_then(|()| out.flush())
        .map_err(|e| e.to_string())
}

/// Send `options` to the browser and read back the credential JSON it produced.
fn browser(step: &str, subject: &SubjectId, options: &[u8]) -> Fallible<Vec<u8>> {
    // The verifier's options are compact JSON (checked by parsing, embedded verbatim).
    kjson::parse(options).map_err(|e| format!("options: {e:?}"))?;
    let options = std::str::from_utf8(options).map_err(|e| e.to_string())?;
    if options.contains('\n') {
        return Err("options span lines".into());
    }
    emit(&format!(
        "{{\"step\":\"{}\",\"subject\":\"{}\",\"options\":{options}}}",
        json_string(step),
        json_string(&subject.subject)
    ))?;
    let mut line = String::new();
    std::io::stdin()
        .lock()
        .read_line(&mut line)
        .map_err(|e| e.to_string())?;
    let reply = kjson::parse(line.trim_end().as_bytes()).map_err(|e| format!("reply: {e:?}"))?;
    match reply.get("credential").and_then(|v| v.as_str()) {
        Some(c) => Ok(c.as_bytes().to_vec()),
        None => Err("browser did not return a credential".into()),
    }
}

fn wall_now() -> Fallible<Timestamp> {
    let d = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|e| e.to_string())?;
    let secs = i64::try_from(d.as_secs()).map_err(|e| e.to_string())?;
    timestamp_from_parts(secs, d.subsec_nanos()).ok_or_else(|| "time".to_string())
}

fn channel(origin: &str, identity: &str) -> Fallible<ApprovalChannel> {
    let v = Arc::new(PasskeyVerifier::new("localhost", origin).map_err(|e| format!("{e:?}"))?);
    ApprovalChannel::new(v.clone(), v, identity).ok_or_else(|| "channel".to_string())
}

/// The bootstrap session's own registration (no operator session).
fn register(
    ledger: &dyn RegistrationLedger,
    now: &dyn Fn() -> Fallible<ObservedTime>,
    subject: &SubjectId,
) -> Fallible<writ_broker::human::VerifiedRegistration> {
    let issued = ledger
        .begin_registration(&RegistrationBegin {
            subject,
            operator: None,
            now: now()?,
        })
        .map_err(|f| format!("{:?}", f.error))?
        .map_err(|r| format!("registration begin refused: {}", r.code()))?;
    let response = browser("create", subject, &issued.client_options)?;
    ledger
        .finish_registration(&RegistrationFinish {
            registration_id: &issued.registration_id,
            response: &response,
            operator: None,
            now: now()?,
        })
        .map_err(|f| format!("{:?}", f.error))?
        .map_err(|r| format!("registration finish refused: {}", r.code()))
}

/// The page-session id of the fixture's own enrollments (D-043 Y2).
const FIXTURE_SESSION: &str = "browser-fixture-session";

/// One ceremony of `actor`, its assertion made in the browser.
fn ceremony(
    ledger: &SqliteLedger,
    now: &dyn Fn() -> Fallible<ObservedTime>,
    actor: &SubjectId,
    op: &CeremonyOp,
    key: &str,
) -> Fallible<CeremonyResult> {
    let issued = ledger
        .begin_ceremony(&CeremonyBegin {
            op,
            subject: actor,
            idempotency_key: key,
            now: now()?,
        })
        .map_err(|f| format!("{:?}", f.error))?
        .map_err(|r| format!("{key} begin refused: {}", r.code()))?;
    let response = browser("get", actor, &issued.client_options)?;
    Ok(ledger
        .finish_ceremony(&CeremonyFinish {
            op,
            challenge_id: &issued.challenge_id,
            response: &response,
            idempotency_key: key,
            now: now()?,
        })
        .map_err(|f| format!("{:?}", f.error))?
        .map_err(|r| format!("{key} finish refused: {}", r.code()))?
        .result)
}

/// Enroll `subject` (human, `roles`) on the serving ledger: `admin`'s registration-start
/// ceremony (D-043 Y2), the new credential created in the browser, finished in the same
/// operator session, then `admin`'s `Enroll` ceremony; every assertion made in the browser.
fn enroll(
    ledger: &SqliteLedger,
    now: &dyn Fn() -> Fallible<ObservedTime>,
    admin: &SubjectId,
    subject: &SubjectId,
    roles: Vec<NewRole>,
    linked_identities: Vec<SubjectId>,
) -> Fallible<()> {
    let start = CeremonyOp::RegistrationStart {
        subject: subject.clone(),
        operator: admin.clone(),
        operator_session: FIXTURE_SESSION.into(),
    };
    let key = format!("browser-fixture-start-{}", subject.subject);
    let registration = match ceremony(ledger, now, admin, &start, &key)? {
        CeremonyResult::RegistrationStarted { registration } => registration,
        other => return Err(format!("registration start answered {other:?}")),
    };
    let response = browser("create", subject, &registration.client_options)?;
    let session = OperatorSession {
        operator: admin.clone(),
        session_id: FIXTURE_SESSION.into(),
    };
    let credential = ledger
        .finish_registration(&RegistrationFinish {
            registration_id: &registration.registration_id,
            response: &response,
            operator: Some(&session),
            now: now()?,
        })
        .map_err(|f| format!("{:?}", f.error))?
        .map_err(|r| format!("registration finish refused: {}", r.code()))?;
    let op = CeremonyOp::Enroll {
        enrollment: NewEnrollment {
            subject: subject.clone(),
            kind: PrincipalKind::Human,
            credential: Some(credential),
            linked_identities,
            roles,
        },
        operator_session: FIXTURE_SESSION.into(),
    };
    let key = format!("browser-fixture-enroll-{}", subject.subject);
    match ceremony(ledger, now, admin, &op, &key)? {
        CeremonyResult::Enrolled { .. } => Ok(()),
        other => Err(format!("enroll answered {other:?}")),
    }
}

fn setup(a: &BTreeMap<String, String>) -> Fallible<()> {
    let path = PathBuf::from(need(a, "ledger")?);
    let origin = need(a, "origin")?;
    let issuer = need(a, "issuer")?;
    let admin = SubjectId::new(issuer, need(a, "admin")?);
    let approver = SubjectId::new(issuer, need(a, "approver")?);
    drop(SqliteLedger::create(&path).map_err(|e| format!("create: {e:?}"))?);

    // The one-shot bootstrap (M4-ENROLLMENT-CEREMONY section 2).
    let session = BootstrapSession::open(&path)
        .map_err(|e| format!("bootstrap open: {e:?}"))?
        .map_err(|r| format!("bootstrap refused: {}", r.code()))?
        .with_approval_channel(channel(origin, "browser-fixture-bootstrap")?)
        .map_err(|e| format!("{e:?}"))?;
    let observe_now = || -> Fallible<ObservedTime> {
        let clock = FixedClock::new(wall_now()?);
        session.observe(&clock).map_err(|e| format!("{e:?}"))
    };
    let credential = register(&session, &observe_now, &admin)?;
    let now = observe_now()?;
    session
        .bootstrap(
            &BootstrapEnrollment {
                subject: admin.clone(),
                credential,
                linked_identities: vec![],
            },
            now,
        )
        .map_err(|f| format!("{:?}", f.error))?
        .map_err(|r| format!("bootstrap refused: {}", r.code()))?;

    // Normal service: enroll the approver through the administrator's ceremony.
    let normal = channel(origin, "browser-fixture")?;
    let ledger = SqliteLedger::open(&path)
        .and_then(|l| l.with_approval_channel(normal))
        .map_err(|e| format!("open: {e:?}"))?;
    let clock = {
        let mut store: &SqliteLedger = &ledger;
        HostClock::start(SystemWall, Boottime, &mut store).map_err(|e| format!("{e:?}"))?
    };
    let now = || -> Fallible<ObservedTime> {
        let mut store: &SqliteLedger = &ledger;
        observe(&clock, &mut store).map_err(|e| format!("{e:?}"))
    };
    enroll(
        &ledger,
        &now,
        &admin,
        &approver,
        vec![NewRole {
            permission: Permission::Approver,
            tenant_id: "*".into(),
            project_id: "*".into(),
        }],
        vec![],
    )?;
    // An optional further subject (`--target`), a target for the revocation pages.
    if let Some(target) = a.get("target") {
        enroll(
            &ledger,
            &now,
            &admin,
            &SubjectId::new(issuer, target),
            vec![NewRole {
                permission: Permission::Approver,
                tenant_id: "t-browser".into(),
                project_id: "p-browser".into(),
            }],
            vec![],
        )?;
    }
    // An optional pending signoff (`--signoff <id> --reviewer <subject> --excluded
    // <subject>`): the stored promotion context through the real broker path, a reviewer
    // holding `signoff_authority` in its scope, and a second one who links the requester
    // identity (an enrollment link, R1), so the ledger excludes it from signing.
    if let Some(signoff_id) = a.get("signoff") {
        let dir = path.parent().ok_or("ledger directory")?;
        signoff_world(&ledger, &clock, dir, signoff_id)?;
        let role = || NewRole {
            permission: Permission::SignoffAuthority,
            tenant_id: SO_TENANT.into(),
            project_id: SO_PROJECT.into(),
        };
        let reviewer = SubjectId::new(issuer, need(a, "reviewer")?);
        enroll(&ledger, &now, &admin, &reviewer, vec![role()], vec![])?;
        let excluded = SubjectId::new(issuer, need(a, "excluded")?);
        enroll(
            &ledger,
            &now,
            &admin,
            &excluded,
            vec![role()],
            vec![so_requester()],
        )?;
    }

    let created = now()?.at();
    let mut fingerprints = Vec::new();
    for (n, id) in need(a, "requests")?.split(',').enumerate() {
        let request = ApprovalRequest::new(NewApprovalRequest {
            request_id: id.into(),
            tenant_id: "t-browser".into(),
            project_id: "p-browser".into(),
            requester: SubjectId::new("adapter", "agent:browser-suite"),
            agent: None,
            action_kind: "repository.publish".into(),
            canonical_resource_id: format!("repo:browser-suite/{id}"),
            action_digest: format!("sha256:{:064x}", n + 1),
            action: kjson::obj([
                ("kind", kjson::s("repository.publish")),
                (
                    "note",
                    kjson::s("untrusted <b>requester</b> text \"quoted\" & escaped"),
                ),
            ]),
            capabilities: vec!["publish".into()],
            policy_bundle_sha: "sha256:browser-suite-policy".into(),
            request_fingerprint: format!("sha256:browser-suite-request-{id}"),
            created_at: created,
            expires_at: created.plus_seconds(600),
            required_approvers: 1,
        })
        .map_err(|e| format!("request {id}: {e:?}"))?;
        ledger
            .record_approval_request(&request)
            .map_err(|f| format!("{:?}", f.error))?;
        fingerprints.push(format!(
            "\"{}\":\"{}\"",
            json_string(id),
            json_string(request.fingerprint().as_str())
        ));
    }
    emit(&format!(
        "{{\"step\":\"done\",\"requests\":{{{}}}}}",
        fingerprints.join(",")
    ))
}

fn status(a: &BTreeMap<String, String>) -> Fallible<()> {
    let ledger = SqliteLedger::open(&PathBuf::from(need(a, "ledger")?))
        .map_err(|e| format!("open: {e:?}"))?;
    let mut out = Vec::new();
    for id in need(a, "requests")?.split(',') {
        let s = match ledger.approval_request(id).map_err(|e| format!("{e:?}"))? {
            Some((_, s)) => s.as_str().to_string(),
            None => "unknown".to_string(),
        };
        out.push(format!("\"{}\":\"{}\"", json_string(id), json_string(&s)));
    }
    // `--signoffs <id,...>`: each stored signoff's signer, issue time and expiry.
    let mut signoffs = Vec::new();
    for id in a.get("signoffs").map_or("", String::as_str).split(',') {
        if id.is_empty() {
            continue;
        }
        let v = match ledger.signoff(id).map_err(|e| format!("{e:?}"))? {
            Some(state) => {
                let f = state.signoff.fields();
                format!(
                    "{{\"subject\":\"{}\",\"issued_at\":\"{}\",\"expires_at\":\"{}\"}}",
                    json_string(&f.subject.subject),
                    json_string(&f.issued_at.to_string()),
                    json_string(&f.expires_at.to_string())
                )
            }
            None => "null".to_string(),
        };
        signoffs.push(format!("\"{}\":{v}", json_string(id)));
    }
    emit(&format!(
        "{{\"status\":{{{}}},\"signoffs\":{{{}}}}}",
        out.join(","),
        signoffs.join(",")
    ))
}

fn main() -> std::process::ExitCode {
    let result = args().and_then(|(command, a)| match command.as_str() {
        "setup" => setup(&a),
        "status" => status(&a),
        other => Err(format!("unknown command {other}")),
    });
    match result {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("browser_fixture: {e}");
            std::process::ExitCode::from(1)
        }
    }
}

// ---------------------------------------------------------------------------
// The stored promotion context of the signoff page (`--signoff <id> --reviewer <subject>
// --excluded <subject>`): through the real broker path, as the closure adapter does. Test
// keys, documents and snapshot store only (non-shipping).

const SO_TENANT: &str = "fixture-tenant";
const SO_PROJECT: &str = "fixture-project";
const SO_ADAPTER: &str = "writ-host-adapter:fixture";
const SO_REQUESTER: &str = "agent:session-1";
const RECORD_KEY: [u8; 32] = [0x5a; 32];
const RUNNER_KEY: [u8; 32] = [0x6b; 32];
const INGRESS_KEY: [u8; 32] = [0x11; 32];

fn sha(c: char) -> String {
    format!("sha256:{}", c.to_string().repeat(64))
}

fn parse(text: &str) -> Fallible<Value> {
    kjson::parse(text.as_bytes()).map_err(|e| format!("parse: {e:?}"))
}

/// `v` with the member at `path` set to `value`.
fn set(v: &Value, path: &[&str], value: Value) -> Value {
    let mut out = v.clone();
    let mut cur = &mut out;
    for (i, key) in path.iter().enumerate() {
        let Value::Obj(m) = cur else {
            return out;
        };
        if i == path.len() - 1 {
            m.insert((*key).to_string(), value);
            return out;
        }
        let Some(next) = m.get_mut(*key) else {
            return out;
        };
        cur = next;
    }
    out
}

fn unhex(text: &str) -> Vec<u8> {
    (0..text.len())
        .step_by(2)
        .filter_map(|i| u8::from_str_radix(text.get(i..i + 2)?, 16).ok())
        .collect()
}

/// HMAC-SHA256 from the kernel's SHA-256 (test documents only).
fn hmac_hex(key: &[u8], message: &[u8]) -> String {
    let mut k = key.to_vec();
    k.resize(64, 0);
    let pad = |b: u8| k.iter().map(|x| x ^ b).collect::<Vec<u8>>();
    let mut inner = pad(0x36);
    inner.extend_from_slice(message);
    let inner = unhex(Digest::of_bytes(&inner).hex());
    let mut outer = pad(0x5c);
    outer.extend_from_slice(&inner);
    Digest::of_bytes(&outer).hex().to_string()
}

fn sign(doc: &Value, key: &[u8]) -> Fallible<Value> {
    let bytes = mac_bytes(doc).ok_or("mac bytes")?;
    Ok(set(
        doc,
        &["auth", "mac"],
        kjson::s(&hmac_hex(key, bytes.as_bytes())),
    ))
}

#[derive(Default)]
struct Docs(Mutex<BTreeMap<String, Value>>);

impl Docs {
    fn put(&self, id: &str, doc: Value) {
        if let Ok(mut m) = self.0.lock() {
            m.insert(id.to_string(), doc);
        }
    }

    fn get(&self, id: &str) -> Result<Option<Value>, SourceUnavailable> {
        self.0
            .lock()
            .map(|m| m.get(id).cloned())
            .map_err(|_| SourceUnavailable)
    }
}

impl AcceptanceRecordSource for Docs {
    fn record(&self, _: &str, _: &str, id: &str) -> Result<Option<Value>, SourceUnavailable> {
        self.get(id)
    }
}

impl AttestationSource for Docs {
    fn attestation(&self, _: &str, _: &str, id: &str) -> Result<Option<Value>, SourceUnavailable> {
        self.get(id)
    }
}

/// `snap-1` has digest `sha('b')`.
struct Snapshots;

impl SnapshotStore for Snapshots {
    fn check(
        &self,
        _: &str,
        _: &str,
        id: &str,
        digest: &str,
    ) -> Result<SnapshotCheck, SourceUnavailable> {
        Ok(match id {
            "snap-1" if digest == sha('b') => SnapshotCheck::Matches,
            "snap-1" => SnapshotCheck::Differs,
            _ => SnapshotCheck::Unknown,
        })
    }
}

/// The fixture's workflow operator (test authority).
struct Operators;

impl WorkflowAuthority for Operators {
    fn authorize(
        &self,
        p: &AdminPresentation<'_>,
        _: AdminOperation,
        tenant: &str,
        project: &str,
    ) -> Result<AuthorizedOperator, AdminRefusal> {
        if (p.channel, p.operator, p.credential) != ("ops-console", "ops:fixture", b"ops-cred") {
            return Err(AdminRefusal::Unauthenticated);
        }
        if (tenant, project) != (SO_TENANT, SO_PROJECT) {
            return Err(AdminRefusal::Unauthorized);
        }
        Ok(AuthorizedOperator {
            id: p.operator.into(),
            issuer: "realm:ops".into(),
            channel: p.channel.into(),
            evidence_ref: "authn-event:ops-fixture".into(),
        })
    }
}

const OPS: AdminPresentation<'static> = AdminPresentation {
    channel: "ops-console",
    operator: "ops:fixture",
    credential: b"ops-cred",
};

/// A sealed `workflow.transition` request of the fixture adapter.
fn sealed_transition(
    key: &str,
    from: &str,
    to: &str,
    expected: i64,
    record: Option<&str>,
) -> Fallible<VerifiedRequest> {
    let mut attrs = kjson::obj([("from_state", kjson::s(from)), ("to_state", kjson::s(to))]);
    if let Some(id) = record {
        attrs = set(&attrs, &["acceptance_record_id"], kjson::s(id));
        attrs = set(&attrs, &["artifact_snapshot_id"], kjson::s("snap-1"));
        attrs = set(&attrs, &["artifact_digest"], kjson::s(&sha('b')));
    }
    let base = parse(&format!(
        r#"{{"contract_version":"2.0","request_id":"req-{key}","trace_id":"trace-1","tenant_id":"{SO_TENANT}","principal":{{"id":"{SO_REQUESTER}","verified_by":"{SO_ADAPTER}","authn_method":"os_peer_credential","roles":["agent"]}},"project_id":"{SO_PROJECT}","session_id":"session-1","agent_id":null,"parent_session_id":null,"idempotency_key":"{key}","expected_state_revision":{expected},"policy_bundle_sha":null,"deadline_at":null,"action":{{"kind":"workflow.transition","resource":{{"uri":"writ-workflow://wf-1","canonical_resource_id":"workflow:{SO_TENANT}/{SO_PROJECT}/wf-1"}},"action_digest":"{}","attributes":{{}},"requested_capabilities":[]}}}}"#,
        sha('b')
    ))?;
    let req = set(&base, &["action", "attributes"], attrs);
    let key =
        || IngressKey::from_bytes(INGRESS_KEY.to_vec()).map_err(|e| format!("ingress key: {e:?}"));
    let mut ring = MemoryKeyRing::new();
    ring.register(SO_ADAPTER, key()?);
    let seal = Seal::create(&req, SO_ADAPTER, &key()?)
        .ok_or("seal")?
        .to_value();
    verify_ingress(&req, Some(&seal), &ring).map_err(|e| format!("ingress: {e:?}"))
}

fn requirements() -> Fallible<Value> {
    parse(&format!(
        r#"{{"acceptance_policy_id":"accept-code-review","acceptance_policy_version":"3","acceptance_policy_bundle_sha":"{}","criteria":[{{"criterion_id":"build","required_class":"mechanical"}},{{"criterion_id":"docs","required_class":"evidence"}},{{"criterion_id":"signoff","required_class":"human"}}]}}"#,
        sha('c')
    ))
}

fn registry_entry(evaluator: &str) -> Fallible<Value> {
    let mut v = parse(include_str!(
        "../../docs/contracts/proposed/examples/evaluator-registry-entry.valid.json"
    ))?;
    v = set(&v, &["scope", "tenant_id"], kjson::s(SO_TENANT));
    v = set(&v, &["scope", "project_id"], kjson::s(SO_PROJECT));
    let (class, trust, criterion) = match evaluator {
        "doc-check" => ("evidence", "E", "docs"),
        "approval-service" => ("human", "H", "signoff"),
        _ => return Ok(v),
    };
    v = set(&v, &["evaluator_id"], kjson::s(evaluator));
    v = set(&v, &["class"], kjson::s(class));
    v = set(&v, &["trust"], kjson::s(trust));
    v = set(&v, &["criteria"], Value::Arr(vec![kjson::s(criterion)]));
    if let Value::Obj(m) = &mut v {
        m.remove("command");
    }
    Ok(v)
}

/// The `/2` acceptance record `id` (verifying to accepted at `revision`) with the human
/// criterion naming `signoff`, valid around `t0` (issued 60 s before, fresh 1800 s, expiring
/// 3600 s after), MAC-authenticated with the test record key.
fn record(id: &str, revision: i64, signoff: &str, t0: Timestamp) -> Fallible<Value> {
    let issued = t0.plus_seconds(-60).to_string();
    let fresh = t0.plus_seconds(1800).to_string();
    let expires = t0.plus_seconds(3600).to_string();
    let mut r = parse(&format!(
        r#"{{"contract_version":"m1-draft-3","acceptance_record_id":"{id}","tenant_id":"{SO_TENANT}","project_id":"{SO_PROJECT}","task_id":"task-1","workflow_id":"wf-1","transition":{{"from_state":"verifying","to_state":"accepted"}},"artifact_snapshot_id":"snap-1","artifact_digest":"{b}","acceptance_policy_id":"accept-code-review","acceptance_policy_version":"3","acceptance_policy_bundle_sha":"{c}","state_revision":{revision},"status":"accepted","issued_at":"{issued}","expires_at":"{expires}","criteria":[{{"criterion_id":"build","mandatory":true,"required_class":"mechanical","status":"pass","evaluator_id":"ci-runner","evaluator_trust":"M","fresh_until":"{fresh}"}},{{"criterion_id":"docs","mandatory":true,"required_class":"evidence","status":"pass","evaluator_id":"doc-check","evaluator_trust":"E","fresh_until":"{fresh}"}},{{"criterion_id":"signoff","mandatory":true,"required_class":"human","status":"pass","evaluator_id":"approval-service","evaluator_trust":"H","fresh_until":"{fresh}"}}],"record_digest":"{z}"}}"#,
        b = sha('b'),
        c = sha('c'),
        z = sha('0'),
    ))?;
    let d = kjson::digest(&r.without("record_digest")).map_err(|e| format!("{e:?}"))?;
    r = set(&r, &["record_digest"], kjson::s(d.as_str()));
    let eval = |criterion: &str, evaluator: &str, attestation: Value, so: Value| {
        kjson::obj([
            ("attestation_id", attestation),
            ("criterion_id", kjson::s(criterion)),
            ("environment_digest", kjson::s(&sha('2'))),
            ("evaluation_id", kjson::s(&format!("eval-{criterion}"))),
            ("evaluator_id", kjson::s(evaluator)),
            ("evaluator_version", kjson::s("1.4.0")),
            ("evidence_digests", Value::Arr(vec![])),
            ("implementation_digest", kjson::s(&sha('1'))),
            ("registry_revision", Value::Int(12)),
            ("signoff_id", so),
        ])
    };
    let doc = kjson::obj([
        (
            "auth",
            kjson::obj([
                ("alg", kjson::s(MAC_ALG)),
                ("key_id", kjson::s("acc-key-1")),
                ("mac", kjson::s(&"0".repeat(64))),
            ]),
        ),
        ("contract_version", kjson::s(RECORD_CONTRACT_V2)),
        (
            "evaluations",
            Value::Arr(vec![
                eval("build", "ci-runner", kjson::s("att-1"), Value::Null),
                eval("docs", "doc-check", Value::Null, Value::Null),
                eval(
                    "signoff",
                    "approval-service",
                    Value::Null,
                    kjson::s(signoff),
                ),
            ]),
        ),
        ("issuer", kjson::s("acceptance-service")),
        ("mode", kjson::s("governed")),
        ("purpose", kjson::s(RECORD_PURPOSE)),
        ("record", r),
    ]);
    sign(&doc, &RECORD_KEY)
}

fn attestation(record_id: &str, revision: i64, t0: Timestamp) -> Fallible<Value> {
    let mut a = parse(include_str!(
        "../../docs/contracts/proposed/examples/runner-attestation.valid.json"
    ))?;
    for (k, v) in [
        ("attestation_id", kjson::s("att-1")),
        ("tenant_id", kjson::s(SO_TENANT)),
        ("project_id", kjson::s(SO_PROJECT)),
        (
            "canonical_target",
            kjson::s(&format!("workflow:{SO_TENANT}/{SO_PROJECT}/wf-1")),
        ),
        ("acceptance_record_id", kjson::s(record_id)),
        ("state_revision", Value::Int(revision)),
        ("started_at", kjson::s(&t0.plus_seconds(-120).to_string())),
        ("finished_at", kjson::s(&t0.plus_seconds(-60).to_string())),
        ("fresh_until", kjson::s(&t0.plus_seconds(1800).to_string())),
    ] {
        a = set(&a, &[k], v);
    }
    sign(&a, &RUNNER_KEY)
}

/// Workflow `wf-1` in `verifying` under the human requirement set and the signoff
/// request of a `/2` record naming `signoff_id` (the stored promotion context), through
/// `Broker::administer`, `Broker::promote` and `Broker::request_signoff`.
fn signoff_world(
    ledger: &SqliteLedger,
    clock: &(dyn EffectiveClock + Sync),
    dir: &std::path::Path,
    signoff_id: &str,
) -> Fallible<()> {
    let effects = SqliteEffectStore::create(&dir.join("effects.db"), ledger.ledger_id())
        .map_err(|e| format!("effects: {e:?}"))?;
    let bundle = load_bundle(&parse(include_str!(
        "../../m1/fixtures/transition/m1-transition.bundle.json"
    ))?)
    .map_err(|e| format!("bundle: {e:?}"))?;
    let active = ActivePolicy::new(PolicyState::loaded(bundle));
    let permit = PermitKey::from_bytes(vec![0x44; 32]).map_err(|e| format!("permit key: {e:?}"))?;
    let broker = Broker::new(&active, clock, ledger, &effects, &permit);
    let admin = |request: AdminRequest| {
        broker
            .administer(&Operators, &OPS, &Snapshots, request)
            .map_err(|r| format!("administer: {}", r.code()))
    };
    admin(AdminRequest::CreateWorkflow {
        key: WorkflowKey {
            tenant_id: SO_TENANT.into(),
            project_id: SO_PROJECT.into(),
            workflow_id: "wf-1".into(),
        },
        task_id: "task-1".into(),
        initial_state: "planning".into(),
        artifact_snapshot_id: "snap-1".into(),
        artifact_digest: sha('b'),
    })?;
    admin(AdminRequest::ActivateAcceptancePolicy {
        tenant_id: SO_TENANT.into(),
        project_id: SO_PROJECT.into(),
        requirements: requirements()?,
        effect_prerequisites: vec![],
    })?;
    for e in ["ci-runner", "doc-check", "approval-service"] {
        admin(AdminRequest::RegisterEvaluator {
            entry: registry_entry(e)?,
        })?;
    }
    let docs = Docs::default();
    let mut record_keys = MemoryKeys::new();
    record_keys.register(
        "acc-key-1",
        AcceptanceRecordKey::from_bytes(RECORD_KEY.to_vec())
            .map_err(|e| format!("record key: {e:?}"))?,
    );
    let mut runner_keys = MemoryKeys::new();
    runner_keys.register(
        "runner-key-1",
        RunnerAttestationKey::from_bytes(RUNNER_KEY.to_vec())
            .map_err(|e| format!("runner key: {e:?}"))?,
    );
    let trust = Trust {
        records: &docs,
        attestations: &docs,
        record_keys: &record_keys,
        runner_keys: &runner_keys,
        snapshots: &Snapshots,
    };
    broker
        .promote(
            &trust,
            &sealed_transition("so-k0", "planning", "verifying", 0, None)?,
            None,
        )
        .result
        .map_err(|r| format!("promote: {}", r.code()))?;
    let t0 = clock.read().map_err(|e| format!("clock: {e:?}"))?.effective;
    let t0 = timestamp_from_parts(t0.unix_seconds(), 0).ok_or("time")?;
    docs.put("acc-1", record("acc-1", 1, signoff_id, t0)?);
    docs.put("att-1", attestation("acc-1", 1, t0)?);
    let ids = broker
        .request_signoff(
            &trust,
            &sealed_transition("so-k1", "verifying", "accepted", 1, Some("acc-1"))?,
        )
        .map_err(|r| format!("request_signoff: {}", r.code()))?;
    if ids != [signoff_id.to_string()] {
        return Err(format!("request_signoff answered {ids:?}"));
    }
    Ok(())
}

/// The requester identity of the stored promotion context (the excluded signer links it).
fn so_requester() -> SubjectId {
    SubjectId::new(SO_ADAPTER, SO_REQUESTER)
}
