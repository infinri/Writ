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
//! 3. reopens the ledger through the normal ownership path, registers the approver's
//!    credential in the browser the same way and enrolls it (role `approver` in every scope)
//!    through the administrator's user-verified `Enroll` ceremony, performed in the browser;
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

use writ_approval::page::json_string;
use writ_approval::verifier::PasskeyVerifier;
use writ_broker::approval::{ApprovalRequest, NewApprovalRequest};
use writ_broker::approval_store::{
    ApprovalLedger, BootstrapEnrollment, CeremonyBegin, CeremonyFinish, CeremonyOp, CeremonyResult,
    NewEnrollment, NewRole, RegistrationBegin, RegistrationFinish, RegistrationLedger,
};
use writ_broker::clock::{
    observe, timestamp_from_parts, Boottime, FixedClock, HostClock, ObservedTime, SystemWall,
};
use writ_broker::human::{ApprovalChannel, Permission, PrincipalKind, SubjectId};
use writ_broker::store::ExecuteLedger;
use writ_broker::writ_kernel::json as kjson;
use writ_broker::writ_kernel::time::Timestamp;
use writ_ledger_sqlite::{BootstrapSession, SqliteLedger};

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

fn register(
    ledger: &dyn RegistrationLedger,
    now: &dyn Fn() -> Fallible<ObservedTime>,
    subject: &SubjectId,
) -> Fallible<writ_broker::human::VerifiedRegistration> {
    let issued = ledger
        .begin_registration(&RegistrationBegin {
            subject,
            now: now()?,
        })
        .map_err(|f| format!("{:?}", f.error))?
        .map_err(|r| format!("registration begin refused: {}", r.code()))?;
    let response = browser("create", subject, &issued.client_options)?;
    ledger
        .finish_registration(&RegistrationFinish {
            registration_id: &issued.registration_id,
            response: &response,
            now: now()?,
        })
        .map_err(|f| format!("{:?}", f.error))?
        .map_err(|r| format!("registration finish refused: {}", r.code()))
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
    let credential = register(&ledger, &now, &approver)?;
    let op = CeremonyOp::Enroll {
        enrollment: NewEnrollment {
            subject: approver.clone(),
            kind: PrincipalKind::Human,
            credential: Some(credential),
            linked_identities: vec![],
            roles: vec![NewRole {
                permission: Permission::Approver,
                tenant_id: "*".into(),
                project_id: "*".into(),
            }],
        },
    };
    let key = "browser-fixture-enroll-approver";
    let issued = ledger
        .begin_ceremony(&CeremonyBegin {
            op: &op,
            subject: &admin,
            idempotency_key: key,
            now: now()?,
        })
        .map_err(|f| format!("{:?}", f.error))?
        .map_err(|r| format!("enroll begin refused: {}", r.code()))?;
    let response = browser("get", &admin, &issued.client_options)?;
    let outcome = ledger
        .finish_ceremony(&CeremonyFinish {
            op: &op,
            challenge_id: &issued.challenge_id,
            response: &response,
            idempotency_key: key,
            now: now()?,
        })
        .map_err(|f| format!("{:?}", f.error))?
        .map_err(|r| format!("enroll finish refused: {}", r.code()))?;
    if !matches!(outcome.result, CeremonyResult::Enrolled { .. }) {
        return Err(format!("enroll answered {:?}", outcome.result));
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
    emit(&format!("{{\"status\":{{{}}}}}", out.join(",")))
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
