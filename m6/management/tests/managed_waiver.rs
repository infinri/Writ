#![cfg(feature = "fixtures")]
#![allow(clippy::unwrap_used)]

use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
    process::{Command, Output, Stdio},
    time::{SystemTime, UNIX_EPOCH},
};

use rusqlite::Connection;
use writ_kernel::json::{self, Map, Value};
use writ_policy_management_lab::{
    execute_v1_with_service, fixture, waiver_fixture::WaiverFixture, Service, Store,
};

const AUTHORITY: &str = env!("CARGO_BIN_EXE_writ-waiver-authority-fixture");
const CLI: &str = env!("CARGO_BIN_EXE_writ-manage-fixture");
const REQUESTER: &[u8] = b"fixture-waiver-requester";
const OPERATOR: &[u8] = b"fixture-waiver-operator";
const VIEWER: &[u8] = b"fixture-viewer";

struct Lab {
    directory: PathBuf,
    policy: PathBuf,
    ledger: PathBuf,
    store: Store,
    manage: PathBuf,
    step_up: PathBuf,
}

impl Lab {
    fn new() -> Self {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let directory = std::env::temp_dir().join(format!(
            "writ-managed-waiver-{}-{nonce}",
            std::process::id()
        ));
        fs::create_dir(&directory).unwrap();
        let policy = directory.join("policy.sqlite");
        let ledger = directory.join("ledger.sqlite");
        let manage = directory.join("management.sqlite");
        let step_up = directory.join("step-up");
        fs::write(&step_up, b"fixture-waiver-approver-step-up").unwrap();
        success(process(
            AUTHORITY,
            &[Path::new("seed-policy"), &policy],
            None,
        ));
        success(process(
            AUTHORITY,
            &[Path::new("init-waiver"), &ledger, &policy],
            None,
        ));
        let store = Store::create(&manage).unwrap();
        Self {
            directory,
            policy,
            ledger,
            store,
            manage,
            step_up,
        }
    }

    fn api(&self, credential: &[u8], request: &Value) -> Value {
        self.api_with_policy(&self.policy, credential, request)
    }

    fn api_with_policy(&self, policy: &Path, credential: &[u8], request: &Value) -> Value {
        let fixture = WaiverFixture::open(&self.ledger, policy).unwrap();
        self.api_open(&fixture, credential, request)
    }

    fn api_open(&self, owner: &WaiverFixture, credential: &[u8], request: &Value) -> Value {
        let authority = fixture::authority();
        let service = Service::new(&self.store, Some(&authority)).with_waiver(owner);
        let input = json::canonical(request).unwrap();
        json::parse(&execute_v1_with_service(
            &service,
            credential,
            input.as_bytes(),
        ))
        .unwrap()
    }

    fn assert(&self, begun: &Value) -> Output {
        process(
            AUTHORITY,
            &[Path::new("assert-waiver"), &self.ledger, &self.step_up],
            Some(begun),
        )
    }

    fn cli(&self, request: &Value) -> Value {
        let operator = self.directory.join("operator");
        fs::write(&operator, OPERATOR).unwrap();
        success(process(
            CLI,
            &[
                Path::new("v1-waiver"),
                &self.manage,
                &operator,
                &self.ledger,
                &self.policy,
            ],
            Some(request),
        ))
    }

    fn proof_counts(&self, challenge: &str) -> (String, i64, i64, i64, String, i64) {
        let conn = Connection::open(&self.ledger).unwrap();
        conn.query_row(
            "SELECT (SELECT status FROM challenges WHERE challenge_id=?1),
                    (SELECT count(*) FROM waivers),
                    (SELECT count(*) FROM consumed_signoffs),
                    (SELECT count(*) FROM grant_uses), state, revision
             FROM workflows WHERE workflow_id='workflow'",
            [challenge],
            |r| {
                Ok((
                    r.get(0)?,
                    r.get(1)?,
                    r.get(2)?,
                    r.get(3)?,
                    r.get(4)?,
                    r.get(5)?,
                ))
            },
        )
        .unwrap()
    }
}

impl Drop for Lab {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.directory);
    }
}

fn process(bin: &str, args: &[&Path], input: Option<&Value>) -> Output {
    let mut child = Command::new(bin)
        .args(args)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    if let Some(input) = input {
        let text = json::canonical(input).unwrap();
        child
            .stdin
            .take()
            .unwrap()
            .write_all(text.as_bytes())
            .unwrap();
    }
    child.wait_with_output().unwrap()
}

fn success(output: Output) -> Value {
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    json::parse(&output.stdout).unwrap()
}

fn payload(operation: &str, fields: impl IntoIterator<Item = (&'static str, Value)>) -> Value {
    let mut values: Map = fields
        .into_iter()
        .map(|(name, value)| (name.into(), value))
        .collect();
    values.insert("version".into(), json::s("/manage/v1"));
    values.insert("operation".into(), json::s(operation));
    values.insert(
        "scope".into(),
        json::obj([
            ("tenant", json::s(fixture::TENANT)),
            ("project", json::s(fixture::PROJECT)),
        ]),
    );
    Value::Obj(values)
}

fn request(criterion: &str, reason: &str) -> Value {
    payload(
        "waiver_request",
        [
            ("workflow_id", json::s("workflow")),
            ("from_state", json::s("verifying")),
            ("to_state", json::s("accepted")),
            ("expected_revision", Value::Int(0)),
            ("transition_ref", json::s("fixture-transition")),
            ("criterion_id", json::s(criterion)),
            ("reason", json::s(reason)),
        ],
    )
}

fn begin(id: &str, approver: &str, key: &str) -> Value {
    payload(
        "waiver_begin",
        [
            ("waiver_id", json::s(id)),
            ("transition_ref", json::s("fixture-transition")),
            ("approver_issuer", json::s("fixture-waiver-human")),
            ("approver_subject", json::s(approver)),
            ("idempotency_key", json::s(key)),
        ],
    )
}

fn finish(id: &str, challenge: &str, attempt: &str, presented: &str) -> Value {
    payload(
        "waiver_finish",
        [
            ("waiver_id", json::s(id)),
            ("transition_ref", json::s("fixture-transition")),
            ("idempotency_key", json::s("tested-ceremony")),
            ("challenge_id", json::s(challenge)),
            ("dispatch_key", json::s(attempt)),
            ("presentation", json::s(presented)),
        ],
    )
}

fn recovery(attempt: &str) -> Value {
    payload("waiver_recover", [("dispatch_key", json::s(attempt))])
}

fn text<'a>(value: &'a Value, name: &str) -> &'a str {
    value.get(name).and_then(Value::as_str).unwrap()
}

fn result(response: &Value) -> &Value {
    assert_eq!(response.get("ok"), Some(&Value::Bool(true)), "{response:?}");
    response.get("result").unwrap()
}

fn refused(response: Value, code: &str) {
    assert_eq!(
        response.get("ok"),
        Some(&Value::Bool(false)),
        "{response:?}"
    );
    assert_eq!(text(response.get("error").unwrap(), "code"), code);
}

#[test]
fn refusal_leaves_waiver_evidence_unchanged_and_recovery_binds_exact_dispatch() {
    let lab = Lab::new();
    refused(
        lab.api(OPERATOR, &request("waiver-review", "Wrong requester")),
        "UNAUTHORIZED",
    );
    refused(
        lab.api(REQUESTER, &request("review", "Invariant origin")),
        "UNAUTHORIZED",
    );

    let created = lab.api(
        REQUESTER,
        &request("waiver-review", "Managed fixture waiver"),
    );
    let id = text(result(&created), "waiver_id");
    let alternate = lab.api(
        REQUESTER,
        &request("waiver-review", "Alternate displayed binding"),
    );
    let alternate = result(&alternate);
    refused(
        lab.api(
            OPERATOR,
            &begin(id, "fixture-waiver-setup-operator", "wrong-role"),
        ),
        "UNAUTHORIZED",
    );
    let begun = lab.api(
        OPERATOR,
        &begin(id, "fixture-waiver-approver", "tested-ceremony"),
    );
    let challenge = text(result(&begun), "challenge_id");
    let mut altered = begun.as_obj().unwrap().clone();
    let mut altered_result = result(&begun).as_obj().unwrap().clone();
    altered_result.insert("waiver_id".into(), json::s(text(alternate, "waiver_id")));
    altered_result.insert("context".into(), alternate.get("context").unwrap().clone());
    altered.insert("result".into(), Value::Obj(altered_result));
    assert!(!lab.assert(&Value::Obj(altered)).status.success());

    let assertion = success(lab.assert(&begun));
    let proof = text(&assertion, "presentation");
    let initial = ("issued".into(), 0, 0, 0, "verifying".into(), 0);
    refused(
        lab.api(OPERATOR, &finish(id, challenge, "bad-proof", "0")),
        "UNAUTHORIZED",
    );
    assert_eq!(lab.proof_counts(challenge), initial);

    let fixture = WaiverFixture::open(&lab.ledger, &lab.policy).unwrap();
    let fault = Connection::open(&lab.ledger).unwrap();
    fault
        .execute_batch(
            "CREATE TRIGGER fixture_waiver_audit_failure BEFORE INSERT ON approval_events
         WHEN NEW.kind = 'waived' BEGIN SELECT RAISE(ABORT, 'injected audit failure'); END;",
        )
        .unwrap();
    refused(
        lab.api_open(
            &fixture,
            OPERATOR,
            &finish(id, challenge, "audit-fault", proof),
        ),
        "UNAVAILABLE",
    );
    assert_eq!(lab.proof_counts(challenge), initial);
    fault
        .execute_batch("DROP TRIGGER fixture_waiver_audit_failure")
        .unwrap();
    drop(fault);
    drop(fixture);

    let recorded = lab.api(OPERATOR, &finish(id, challenge, "accepted", proof));
    assert_eq!(text(result(&recorded), "result"), "waived");
    assert_eq!(
        lab.proof_counts(challenge),
        ("consumed".into(), 1, 0, 0, "verifying".into(), 0)
    );
    assert_eq!(lab.api(OPERATOR, &recovery("accepted")), recorded);
    assert_eq!(lab.cli(&recovery("accepted")), recorded);
    refused(lab.api(OPERATOR, &recovery("bad-proof")), "UNAVAILABLE");
    refused(lab.api(OPERATOR, &recovery("audit-fault")), "UNAVAILABLE");
    refused(
        lab.api(OPERATOR, &finish(id, challenge, "accepted", "1")),
        "IDEMPOTENCY_CONFLICT",
    );

    let missing = lab.directory.join("unavailable-policy");
    let inspect = payload("waiver_inspect", [("waiver_id", json::s(id))]);
    let historical = lab.api_with_policy(&missing, VIEWER, &inspect);
    assert_eq!(
        result(&historical).get("historical_receipt"),
        Some(result(&recorded))
    );
    let validate = payload(
        "waiver_validate",
        [
            ("waiver_id", json::s(id)),
            ("transition_ref", json::s("fixture-transition")),
        ],
    );
    refused(
        lab.api_with_policy(&missing, VIEWER, &validate),
        "UNAVAILABLE",
    );
    assert_eq!(
        result(&lab.api(VIEWER, &validate)).get("currently_valid"),
        Some(&Value::Bool(true))
    );
}

#[test]
fn pinned_ingress_refuses_an_ordinary_ledger() {
    use writ_broker::{
        clock::FixedClock,
        execute::{Broker, PermitKeys},
        ingress::verify_disposable_waiver_transition,
        keys::MemoryKeySource,
        policy::{ActivePolicy, PolicyState},
        refusal::Refusal,
        waiver::WaiverRequest,
        workflow::{WaiverInvocation, WorkflowKey},
    };
    use writ_ledger_sqlite::{SqliteEffectStore, SqliteLedger};

    let lab = Lab::new();
    let ledger = SqliteLedger::create(&lab.directory.join("ordinary.sqlite")).unwrap();
    let effects = SqliteEffectStore::create(
        &lab.directory.join("ordinary-effects.sqlite"),
        ledger.ledger_id(),
    )
    .unwrap();
    let active = ActivePolicy::new(PolicyState::Absent);
    let clock = FixedClock::new(fixture::fixture_time().unwrap());
    let keys = MemoryKeySource::default();
    let broker = Broker::with_permit_keys(
        &active,
        &clock,
        &ledger,
        &effects,
        PermitKeys::Keyed {
            source: &keys,
            revocations: &ledger,
        },
    );
    let sealed = json::parse(include_bytes!("../fixtures/waiver-transition.json")).unwrap();
    let transition = verify_disposable_waiver_transition(&sealed).unwrap();
    let request = WaiverRequest {
        workflow: WorkflowKey {
            tenant_id: fixture::TENANT.into(),
            project_id: fixture::PROJECT.into(),
            workflow_id: "workflow".into(),
        },
        from_state: "verifying".into(),
        to_state: "accepted".into(),
        expected_revision: 0,
        criterion_id: "waiver-review".into(),
        reason: "Disposable input".into(),
    };
    assert!(matches!(
        broker.request_waiver(&WaiverInvocation::from_transition(&transition, &request)),
        Ok(Err(Refusal::IngressUnauthenticated))
    ));
}
