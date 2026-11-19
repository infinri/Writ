#![cfg(feature = "fixtures")]
#![allow(clippy::unwrap_used)]

use std::{
    cell::Cell,
    fmt::Write as _,
    fs::{self, DirBuilder},
    io::Write,
    os::unix::fs::DirBuilderExt,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::atomic::{AtomicU64, Ordering},
};

use ed25519_dalek::{Signer as _, SigningKey};
use writ_kernel::{
    contracts::StateRevision,
    decision::{decide, Policy},
    json::{self, Value},
    policy::load_bundle,
    time::Timestamp,
};
use writ_policy_management_lab::{
    contract_fixture, execute_v1_with_service, fixture, Authenticator, PinnedPrincipal, Principal,
    Service, Store,
};

const MANAGE: &str = env!("CARGO_BIN_EXE_writ-manage-fixture");
const AUTHORITY: &str = env!("CARGO_BIN_EXE_writ-contract-authority-fixture");
const AUTHOR: &[u8] = b"fixture-author";
const PUBLISHER: &[u8] = b"fixture-publisher";
const VIEWER: &[u8] = b"fixture-viewer";
const ID: &str = "tenant:m6-lab:cli-equivalence";
const PERMIT: &[u8] = include_bytes!("../../compiler/fixtures/permit.input.json");
const PERMIT_CASES: &[u8] = include_bytes!("../../compiler/fixtures/permit.cases.json");
const APPROVAL: &[u8] = include_bytes!("../../compiler/fixtures/approval.input.json");
const APPROVAL_CASES: &[u8] = include_bytes!("../../compiler/fixtures/approval.cases.json");
const DENY: &[u8] = include_bytes!("../../compiler/fixtures/deny.input.json");
const DENY_CASES: &[u8] = include_bytes!("../../compiler/fixtures/deny.cases.json");

struct Lab {
    directory: PathBuf,
    drafts: PathBuf,
    owner: PathBuf,
}

impl Lab {
    fn new() -> Self {
        static NEXT: AtomicU64 = AtomicU64::new(0);
        let directory = std::env::temp_dir().join(format!(
            "writ-contract-workflow-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed),
        ));
        DirBuilder::new().mode(0o700).create(&directory).unwrap();
        let drafts = directory.join("management.sqlite");
        let owner = directory.join("contracts.sqlite");
        Store::create(&drafts).unwrap();
        contract_fixture::initialize(&owner).unwrap();
        Self {
            directory,
            drafts,
            owner,
        }
    }

    fn credential(&self, name: &str, content: &[u8]) -> PathBuf {
        let path = self.directory.join(name);
        fs::write(&path, content).unwrap();
        path
    }

    fn api(&self, credential: &[u8], input: &Value) -> Vec<u8> {
        let store = Store::open(&self.drafts).unwrap();
        let registry = contract_fixture::open(&self.owner).unwrap();
        let authority = fixture::authority();
        let service = Service::new(&store, Some(&authority))
            .with_contracts(&registry)
            .allow_self_publication();
        execute_v1_with_service(&service, credential, canonical(input).as_bytes())
    }

    fn cli(&self, credential: &Path, input: &Value) -> Vec<u8> {
        let mut command = Command::new(MANAGE);
        command
            .arg("v1-contracts")
            .arg(&self.drafts)
            .arg(credential)
            .arg(&self.owner);
        invoke(command, input)
    }
}

impl Drop for Lab {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.directory);
    }
}

fn invoke(mut command: Command, input: &Value) -> Vec<u8> {
    let mut child = command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(canonical(input).as_bytes())
        .unwrap();
    let output = child.wait_with_output().unwrap();
    let response = json::parse(&output.stdout).unwrap();
    assert_eq!(
        output.status.success(),
        response.get("ok").and_then(Value::as_bool).unwrap_or(true),
    );
    output.stdout.strip_suffix(b"\n").unwrap().to_vec()
}

fn canonical(input: &Value) -> String {
    json::canonical(input).unwrap()
}

fn request(operation: &str, fields: impl IntoIterator<Item = (&'static str, Value)>) -> Value {
    let scope = contract_fixture::scope();
    Value::Obj(
        [
            ("operation", json::s(operation)),
            ("version", json::s("/manage/v1")),
            (
                "scope",
                json::obj([
                    ("tenant", json::s(&scope.tenant)),
                    ("project", json::s(&scope.project)),
                ]),
            ),
        ]
        .into_iter()
        .chain(fields)
        .map(|(key, value)| (key.into(), value))
        .collect(),
    )
}

fn result(response: &[u8]) -> Value {
    let decoded = json::parse(response).unwrap();
    assert_eq!(decoded.get("ok"), Some(&Value::Bool(true)), "{decoded:?}");
    decoded.get("result").unwrap().clone()
}

fn assert_error(response: &[u8], code: &str) {
    let decoded = json::parse(response).unwrap();
    assert_eq!(
        decoded.get("error").and_then(|error| error.get("code")),
        Some(&json::s(code)),
        "{decoded:?}",
    );
}

fn create() -> Value {
    request(
        "contracts.create",
        [
            ("contract_id", json::s(ID)),
            ("contract_version", json::s("1.0.0")),
            ("expected_revision", Value::Int(0)),
            ("idempotency_key", json::s("create")),
            (
                "schema",
                json::parse(
                    br#"{
                    "$schema":"https://json-schema.org/draft/2020-12/schema",
                    "type":"object",
                    "properties":{"a/b~c":{"type":"integer"}},
                    "required":["a/b~c"],
                    "additionalProperties":false
                }"#,
                )
                .unwrap(),
            ),
        ],
    )
}

fn release_target() -> [(&'static str, Value); 3] {
    [
        ("contract_id", json::s(ID)),
        ("contract_version", json::s("1.0.0")),
        ("expected_revision", Value::Int(1)),
    ]
}

fn preview_release() -> Value {
    request(
        "contracts.preview",
        release_target()
            .into_iter()
            .chain([("change", json::s("publish"))]),
    )
}

fn authorize_release(preview: &[u8], step_up: &Path, key: &str) -> Value {
    let envelope = json::parse(preview).unwrap();
    let mut command = Command::new(AUTHORITY);
    command.arg(step_up).arg(key);
    let presentation = json::parse(&invoke(command, &envelope)).unwrap();
    request(
        "contracts.publish",
        release_target().into_iter().chain([
            (
                "intent_digest",
                result(preview).get("intent_digest").unwrap().clone(),
            ),
            (
                "presentation",
                presentation.get("presentation").unwrap().clone(),
            ),
            ("idempotency_key", json::s(key)),
        ]),
    )
}

#[test]
fn contract_api_and_cli_share_receipts_diagnostics_and_refusals() {
    let lab = Lab::new();
    let author = lab.credential("author", AUTHOR);
    let publisher = lab.credential("publisher", PUBLISHER);
    let viewer = lab.credential("viewer", VIEWER);
    let agent = lab.credential("agent", b"fixture-agent");
    let step_up = lab.credential("step-up", b"fixture-contract-publisher-step-up");

    let creation = create();
    let created = lab.cli(&author, &creation);
    assert_eq!(created, lab.api(AUTHOR, &creation));
    assert_eq!(result(&created).get("revision"), Some(&Value::Int(1)));

    let invalid = request(
        "contracts.validate-example",
        [
            ("contract_id", json::s(ID)),
            ("contract_version", json::s("1.0.0")),
            ("example", json::obj([("a/b~c", json::s("wrong"))])),
        ],
    );
    let diagnostics = lab.api(VIEWER, &invalid);
    assert_eq!(diagnostics, lab.cli(&viewer, &invalid));
    let diagnostics = result(&diagnostics);
    assert_eq!(diagnostics.get("valid"), Some(&Value::Bool(false)));
    assert!(
        canonical(&diagnostics).contains("/a~1b~0c"),
        "{diagnostics:?}"
    );

    for (credential, credential_path, code) in [
        (VIEWER, &viewer, "NOT_FOUND"),
        (b"fixture-agent", &agent, "UNAUTHORIZED"),
    ] {
        let reply = lab.api(credential, &creation);
        assert_eq!(reply, lab.cli(credential_path, &creation));
        assert_error(&reply, code);
    }

    let preview = preview_release();
    let before = fs::read(&lab.owner).unwrap();
    let response = lab.cli(&publisher, &preview);
    assert_eq!(response, lab.api(PUBLISHER, &preview));
    assert_eq!(fs::read(&lab.owner).unwrap(), before);
    let publish = authorize_release(&response, &step_up, "release");
    let mut fields = publish.as_obj().unwrap().clone();
    let digest = fields.get("intent_digest").unwrap().as_str().unwrap();
    let mut policy_proof = format!(
        "fixture-publisher|policy-purpose|{}|{digest}",
        contract_fixture::FIXTURE_TIME,
    );
    let signed = format!("{}{policy_proof}", fixture::CEREMONY_DOMAIN);
    let signature = SigningKey::from_bytes(&[0x63; 32]).sign(signed.as_bytes());
    policy_proof.push('|');
    for byte in signature.to_bytes() {
        write!(policy_proof, "{byte:02x}").unwrap();
    }
    fields.insert("presentation".into(), json::s(&policy_proof));
    assert_error(&lab.api(PUBLISHER, &Value::Obj(fields)), "UNAUTHORIZED");
    assert_eq!(fs::read(&lab.owner).unwrap(), before);

    let published = lab.api(PUBLISHER, &publish);
    assert_eq!(published, lab.cli(&publisher, &publish));
    assert_eq!(
        result(&published).get("status"),
        Some(&json::s("published"))
    );
    let recover = request(
        "contracts.recover",
        [("idempotency_key", json::s("release"))],
    );
    assert_eq!(lab.cli(&publisher, &recover), published);
    assert_eq!(lab.api(PUBLISHER, &recover), published);
    assert_error(&lab.api(AUTHOR, &recover), "NOT_FOUND");
}

#[test]
fn contract_owner_unavailability_does_not_change_core_decisions() {
    let policies = [
        (PERMIT, PERMIT_CASES),
        (APPROVAL, APPROVAL_CASES),
        (DENY, DENY_CASES),
    ]
    .map(|(input, corpus)| {
        let compiled = writ_policy_compile::evaluate(input);
        assert!(compiled.accepted);
        let policy = load_bundle(compiled.document.get("bundle").unwrap()).unwrap();
        (policy, json::parse(corpus).unwrap())
    });
    let now = Timestamp::parse_strict("2026-11-19T00:00:00Z").unwrap();
    let decisions = || {
        policies
            .iter()
            .flat_map(|(policy, cases)| {
                cases.as_arr().unwrap().iter().map(move |case| {
                    let decision = decide(
                        case.get("request").unwrap(),
                        Policy::Loaded(policy),
                        StateRevision::new(1).unwrap(),
                        now,
                    )
                    .to_value();
                    assert_eq!(&decision, case.get("expected").unwrap());
                    decision
                })
            })
            .collect::<Vec<_>>()
    };
    let baseline = decisions();

    let lab = Lab::new();
    let author = lab.credential("author", AUTHOR);
    let publisher = lab.credential("publisher", PUBLISHER);
    let step_up = lab.credential("step-up", b"fixture-contract-publisher-step-up");
    result(&lab.cli(&author, &create()));
    let preview = lab.cli(&publisher, &preview_release());
    let publish = authorize_release(&preview, &step_up, "pre-outage-release");
    let published = lab.cli(&publisher, &publish);
    assert_eq!(
        result(&published).get("status"),
        Some(&json::s("published"))
    );
    assert_eq!(decisions(), baseline);

    let browse = request("contracts.browse", []);
    let store = Store::open(&lab.drafts).unwrap();
    let authority = fixture::authority();
    let unconfigured = Service::new(&store, Some(&authority));
    assert_error(
        &execute_v1_with_service(&unconfigured, VIEWER, canonical(&browse).as_bytes()),
        "UNAVAILABLE",
    );
    assert_eq!(decisions(), baseline);

    let unavailable = lab.directory.join("unavailable.sqlite");
    fs::rename(&lab.owner, &unavailable).unwrap();
    let viewer = lab.credential("viewer", VIEWER);
    assert_error(&lab.cli(&viewer, &browse), "UNAVAILABLE");
    assert_eq!(decisions(), baseline);

    fs::rename(&unavailable, &lab.owner).unwrap();
    let recover = request(
        "contracts.recover",
        [("idempotency_key", json::s("pre-outage-release"))],
    );
    assert_eq!(lab.cli(&publisher, &recover), published);
    assert_eq!(decisions(), baseline);
}

struct ExpiringAuthority<'a> {
    fixture: &'a dyn Authenticator,
    checks: Cell<u32>,
}

struct ExpiringPrincipal<'a> {
    principal: Principal,
    checks: &'a Cell<u32>,
}

impl Authenticator for ExpiringAuthority<'_> {
    fn authenticate(&self, credential: &[u8]) -> Option<Principal> {
        self.fixture.authenticate(credential)
    }

    fn pin(&self, credential: &[u8]) -> Option<Box<dyn PinnedPrincipal + '_>> {
        Some(Box::new(ExpiringPrincipal {
            principal: self.authenticate(credential)?,
            checks: &self.checks,
        }))
    }
}

impl PinnedPrincipal for ExpiringPrincipal<'_> {
    fn principal(&self) -> &Principal {
        &self.principal
    }

    fn still_valid(&self) -> bool {
        let checks = self.checks.get();
        self.checks.set(checks + 1);
        checks < 3
    }
}

#[test]
fn pinned_management_identity_must_remain_valid_through_owner_commit() {
    let lab = Lab::new();
    let fixture = fixture::authority();
    let authority = ExpiringAuthority {
        fixture: &fixture,
        checks: Cell::new(0),
    };
    let store = Store::open(&lab.drafts).unwrap();
    let registry = contract_fixture::open(&lab.owner).unwrap();
    let service = Service::new(&store, Some(&authority)).with_contracts(&registry);
    let reply = execute_v1_with_service(&service, AUTHOR, canonical(&create()).as_bytes());
    assert_error(&reply, "UNAUTHORIZED");
    assert_eq!(authority.checks.get(), 4);
    let recover = request(
        "contracts.recover",
        [("idempotency_key", json::s("create"))],
    );
    assert_error(&lab.api(AUTHOR, &recover), "NOT_FOUND");
    let retried = lab.api(AUTHOR, &create());
    assert_eq!(result(&retried).get("revision"), Some(&Value::Int(1)));
}
