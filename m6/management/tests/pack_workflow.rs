#![cfg(feature = "fixtures")]
#![allow(clippy::unwrap_used)]

use std::{
    fs::{self, DirBuilder},
    io::Write,
    os::unix::fs::DirBuilderExt,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::{
        atomic::{AtomicU64, Ordering},
        Barrier,
    },
    thread,
};

use rusqlite::Connection;
use writ_kernel::{
    commands,
    contracts::StateRevision,
    decision::{decide, Policy},
    json::{self, Value},
    policy::load_bundle,
    time::Timestamp,
};
use writ_policy_management_lab::{
    contract_fixture, evaluator_fixture, execute_v1_with_service, fixture,
    pack_fixture::{self, FixturePackAuthority},
    pack_registry::PackRegistry,
    packs::PACK_SIGNATURE_PURPOSE,
    Error, PackOperation, Service, Store,
};

const MANAGE: &str = env!("CARGO_BIN_EXE_writ-manage-fixture");
const PACK_SIGNER: &str = env!("CARGO_BIN_EXE_writ-pack-authority-fixture");
const CONTRACT_SIGNER: &str = env!("CARGO_BIN_EXE_writ-contract-authority-fixture");
const EVALUATOR_SIGNER: &str = env!("CARGO_BIN_EXE_writ-evaluator-authority-fixture");
const PUBLISHER: &str = "fixture-publisher";
const AUTHOR: &str = "fixture-author";
const VIEWER: &str = "fixture-viewer";
const CONTRACT: &str = "tenant:m6-lab:pack-report";
const PACK: &str = "sample-pack";

struct Lab {
    dir: PathBuf,
    store: PathBuf,
    contracts: PathBuf,
    evaluator: PathBuf,
    trust: FixturePackAuthority,
}

impl Lab {
    fn new() -> Self {
        static NEXT: AtomicU64 = AtomicU64::new(0);
        let dir = std::env::temp_dir().join(format!(
            "writ-pack-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        DirBuilder::new().mode(0o700).create(&dir).unwrap();
        let store = dir.join("management.sqlite");
        let contracts = dir.join("contracts.sqlite");
        let evaluator = dir.join("evaluator.sqlite");
        Store::create(&store).unwrap();
        evaluator_fixture::initialize(&evaluator).unwrap();
        pack_fixture::initialize(&contracts, &evaluator).unwrap();
        for credential in [
            AUTHOR,
            PUBLISHER,
            VIEWER,
            "fixture-service",
            "fixture-agent",
            "fixture-pack-build-signer",
            "fixture-pack-publisher-step-up",
            "fixture-contract-publisher-step-up",
            "fixture-evaluator-human",
        ] {
            fs::write(dir.join(credential), credential).unwrap();
        }
        Self {
            dir,
            store,
            contracts,
            evaluator,
            trust: FixturePackAuthority::default(),
        }
    }

    fn api_at(&self, credential: &str, request: &Value, evaluator_path: &Path) -> Value {
        let store = Store::open_without_recovery(&self.store).unwrap();
        let contracts = contract_fixture::open(&self.contracts).unwrap();
        let packs = PackRegistry::new(
            &contracts,
            evaluator_path,
            evaluator_fixture::pack_dependencies(),
            &self.trust,
        );
        let evaluator = evaluator_fixture::open(evaluator_path).unwrap();
        let authority = fixture::authority();
        let service = Service::new(&store, Some(&authority))
            .with_packs(&packs)
            .with_contracts(&contracts)
            .with_evaluator(&evaluator);
        json::parse(&execute_v1_with_service(
            &service,
            credential.as_bytes(),
            json::canonical(request).unwrap().as_bytes(),
        ))
        .unwrap()
    }

    fn api(&self, credential: &str, request: &Value) -> Value {
        self.api_at(credential, request, &self.evaluator)
    }

    fn cli(&self, credential: &str, request: &Value) -> Value {
        let mut command = Command::new(MANAGE);
        command
            .arg("v1-packs")
            .arg(&self.store)
            .arg(self.dir.join(credential))
            .arg(&self.contracts)
            .arg(&self.evaluator);
        invoke(command, request)
    }

    fn pack_preview(
        &self,
        op: &str,
        version: &str,
        expected: i64,
        envelope: Option<Value>,
    ) -> Value {
        let mut fields = pack_fields(PACK, version, expected);
        put(&mut fields, "pack_operation", json::s(op));
        if let Some(envelope) = envelope {
            put(&mut fields, "envelope", envelope);
        }
        request("packs.preview", &fields)
    }

    fn sign(&self, payload: &Value) -> Value {
        let mut signer = Command::new(PACK_SIGNER);
        signer
            .arg("sign")
            .arg(self.dir.join("fixture-pack-build-signer"));
        invoke(signer, payload)
    }

    fn pack_request_from_preview(
        &self,
        preview: &Value,
        operation: &str,
        ceremony: &str,
        intent: &Value,
    ) -> Value {
        let mut signer = Command::new(PACK_SIGNER);
        signer
            .arg("approve")
            .arg(self.dir.join("fixture-pack-publisher-step-up"))
            .arg(ceremony);
        let approval = invoke(signer, intent);
        let mut request = preview.clone();
        if let Value::Obj(fields) = &mut request {
            fields.remove("pack_operation");
        }
        put(
            &mut request,
            "operation",
            json::s(&format!("packs.{operation}")),
        );
        proof(&mut request, intent, approval, ceremony);
        request
    }

    fn pack_request(&self, preview: &Value, operation: &str, ceremony: &str) -> Value {
        let intent = self.api(PUBLISHER, preview);
        assert_eq!(intent, self.cli(PUBLISHER, preview));
        result(&intent);
        self.pack_request_from_preview(preview, operation, ceremony, &intent)
    }

    fn contract_fields(version: &str, expected: i64) -> Value {
        json::obj([
            ("contract_id", json::s(CONTRACT)),
            ("contract_version", json::s(version)),
            ("expected_revision", Value::Int(expected)),
        ])
    }

    fn contract_request(&self, operation: &str, fields: &Value, key: &str) -> Value {
        let mut preview = fields.clone();
        put(&mut preview, "change", json::s(operation));
        let shown = self.api(PUBLISHER, &request("contracts.preview", &preview));
        result(&shown);
        let mut signer = Command::new(CONTRACT_SIGNER);
        signer
            .arg(self.dir.join("fixture-contract-publisher-step-up"))
            .arg(key);
        let approval = invoke(signer, &shown);
        let mut commit = request(&format!("contracts.{operation}"), fields);
        proof(&mut commit, &shown, approval, key);
        commit
    }

    fn publish_contract(&self, version: &str, expected: i64) -> String {
        let mut fields = Self::contract_fields(version, expected);
        put(
            &mut fields,
            "idempotency_key",
            json::s(&format!("create-{expected}")),
        );
        put(
            &mut fields,
            "schema",
            json::parse(
                br#"{
            "$schema":"https://json-schema.org/draft/2020-12/schema",
            "type":"object","properties":{"summary":{"type":"string"}},
            "required":["summary"],"additionalProperties":false
        }"#,
            )
            .unwrap(),
        );
        result(&self.api(AUTHOR, &request("contracts.create", &fields)));
        let publish = self.contract_request(
            "publish",
            &Self::contract_fields(version, expected + 1),
            &format!("publish-{expected}"),
        );
        let receipt = self.api(PUBLISHER, &publish);
        result(&receipt)
            .get("contract_digest")
            .unwrap()
            .as_str()
            .unwrap()
            .into()
    }

    fn deprecate_contract(&self) {
        let mut fields = Self::contract_fields("1.0.0", 4);
        put(&mut fields, "replacement_version", json::s("2.0.0"));
        put(&mut fields, "end_date", json::s("2026-11-19"));
        let deprecate = self.contract_request("deprecate", &fields, "deprecate-contract");
        result(&self.api(PUBLISHER, &deprecate));
    }

    fn evaluator_request(&self, op: &str, fields: &Value, key: &str) -> Value {
        let preview = self.api(
            PUBLISHER,
            &request(&format!("evaluator_preview_{op}"), fields),
        );
        result(&preview);
        let mut signer = Command::new(EVALUATOR_SIGNER);
        signer
            .arg(self.dir.join("fixture-evaluator-human"))
            .arg(key);
        let mut commit = request(&format!("evaluator_{op}"), fields);
        proof(&mut commit, &preview, invoke(signer, &preview), key);
        commit
    }

    fn publish_evaluator(&self) -> Value {
        let mut entry = json::parse(include_bytes!(
            "../../../docs/contracts/proposed/examples/evaluator-registry-entry.valid.json"
        ))
        .unwrap();
        if let Value::Obj(fields) = &mut entry {
            fields.remove("command");
        }
        for (field, value) in [
            ("evaluator_id", json::s("doc-check")),
            ("trust", json::s("E")),
            ("publisher", json::s(PUBLISHER)),
            ("published_at", json::s("2026-11-01T09:00:00Z")),
            ("registry_revision", Value::Int(1)),
            ("class", json::s("evidence")),
            (
                "scope",
                json::obj([
                    ("tenant_id", json::s(fixture::TENANT)),
                    ("project_id", json::s(fixture::PROJECT)),
                ]),
            ),
        ] {
            put(&mut entry, field, value);
        }
        let fields = json::obj([
            ("expected_revision", Value::Int(0)),
            ("entry", entry.clone()),
        ]);
        let publish = self.evaluator_request("publish", &fields, "publish-evaluator");
        result(&self.api(PUBLISHER, &publish));
        // Read the exact normalized entry from the owning registry.
        let inspect = request(
            "evaluator_inspect",
            &json::obj([
                ("evaluator_id", json::s("doc-check")),
                ("after", Value::Int(0)),
                ("limit", Value::Int(1)),
            ]),
        );
        result(&self.api(VIEWER, &inspect))
            .get("entry")
            .unwrap()
            .clone()
    }

    fn revoke_evaluator(&self) -> Value {
        self.evaluator_request(
            "revoke",
            &json::obj([
                ("expected_revision", Value::Int(1)),
                ("evaluator_id", json::s("doc-check")),
            ]),
            "revoke-evaluator",
        )
    }

    fn install(&self, version: &str, expected: i64, envelope: Value, key: &str) -> Value {
        let preview = self.pack_preview("install", version, expected, Some(envelope));
        let commit = self.pack_request(&preview, "install", key);
        let receipt = self.cli(PUBLISHER, &commit);
        result(&receipt);
        assert_eq!(self.api(PUBLISHER, &commit), receipt);
        receipt
    }

    fn history(&self, id: &str) -> Value {
        let request = request("packs.history", &json::obj([("pack_id", json::s(id))]));
        let response = self.cli(VIEWER, &request);
        assert_eq!(self.api(VIEWER, &request), response);
        result(&response).clone()
    }
}

impl Drop for Lab {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.dir);
    }
}

fn put(doc: &mut Value, key: &str, value: Value) {
    if let Value::Obj(fields) = doc {
        fields.insert(key.into(), value);
    } else {
        assert!(doc.as_obj().is_some());
    }
}

fn pack_fields(id: &str, version: &str, expected: i64) -> Value {
    json::obj([
        ("pack_id", json::s(id)),
        ("pack_version", json::s(version)),
        ("expected_revision", Value::Int(expected)),
    ])
}

fn target(version: &str) -> Value {
    json::obj([
        ("pack_id", json::s(PACK)),
        ("pack_version", json::s(version)),
    ])
}

fn request(operation: &str, data: &Value) -> Value {
    let mut doc = data.clone();
    put(&mut doc, "version", json::s("/manage/v1"));
    put(
        &mut doc,
        "scope",
        json::obj([
            ("tenant", json::s(fixture::TENANT)),
            ("project", json::s(fixture::PROJECT)),
        ]),
    );
    put(&mut doc, "operation", json::s(operation));
    doc
}

fn result(response: &Value) -> &Value {
    assert_eq!(response.get("ok"), Some(&Value::Bool(true)), "{response:?}");
    response.get("result").unwrap()
}

fn code(response: &Value) -> Option<&str> {
    response
        .get("error")
        .and_then(|error| error.get("code"))
        .and_then(Value::as_str)
}

fn proof(request: &mut Value, preview: &Value, approval: Value, key: &str) {
    let shown = result(preview);
    put(
        request,
        "intent_digest",
        shown.get("intent_digest").unwrap().clone(),
    );
    put(request, "idempotency_key", json::s(key));
    put(
        request,
        "presentation",
        approval.get("presentation").unwrap().clone(),
    );
    if let Some(confirmation) = shown
        .get("intent")
        .and_then(|intent| intent.get("confirmation"))
        .and_then(Value::as_str)
    {
        put(request, "confirmation", json::s(confirmation));
    }
}

fn invoke(mut command: Command, value: &Value) -> Value {
    let mut child = command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(json::canonical(value).unwrap().as_bytes())
        .unwrap();
    let output = child.wait_with_output().unwrap();
    let response = json::parse(&output.stdout).unwrap();
    assert_eq!(
        output.status.success(),
        code(&response).is_none(),
        "{response:?}"
    );
    response
}

fn payload(id: &str, version: &str, contract_digest: &str, evaluator: &Value) -> Value {
    let mut doc = json::parse(br#"{
      "scope":{"tenant":"m6-lab","project":"offline-proof"},
      "compatibility":"2.0",
      "key_id":"fixture-pack-signature",
      "manifest":{
        "contract_version":"2.0","publisher":"fixture-pack-publisher","domain":"review",
        "signature_ref":"fixture-pack-signature","task_types":["document-review"],
        "supported_modes":["external_host"],"tool_capabilities":["fs.read"],
        "workflows":[{"workflow_id":"document-review","task_type":"document-review",
                      "steps":["review","request-signoff","record"],"human_gates":["review-signoff"]}],
        "artifacts":[{"artifact_type":"report","schema_ref":"tenant:m6-lab:pack-report@1.0.0"}],
        "evaluators":[{"evaluator_id":"doc-check","rubric_version":"1.0.0","kind":"evidence","blocking":false}],
        "memory":{"read_types":[],"write_types":[],"retention_class":"ephemeral"},
        "limits":{"max_tool_calls":1,"max_duration_seconds":60,"max_model_calls":0}
      }
    }"#).unwrap();
    put(&mut doc, "purpose", json::s(PACK_SIGNATURE_PURPOSE));
    let mut manifest = doc.get("manifest").unwrap().clone();
    put(&mut manifest, "pack_id", json::s(id));
    put(&mut manifest, "version", json::s(version));
    put(&mut doc, "manifest", manifest);
    put(
        &mut doc,
        "contracts",
        Value::Arr(vec![json::obj([
            ("id", json::s(CONTRACT)),
            ("version", json::s("1.0.0")),
            ("digest", json::s(contract_digest)),
        ])]),
    );
    put(
        &mut doc,
        "rubrics",
        Value::Arr(vec![json::obj([
            (
                "entry_digest",
                json::s(json::digest(evaluator).unwrap().as_str()),
            ),
            (
                "binding",
                json::obj([
                    ("contract_version", json::s("pack-rubric-binding/1")),
                    ("pack_id", json::s(id)),
                    ("pack_version", json::s(version)),
                    ("rubric_id", json::s("document-rubric")),
                    ("rubric_version", json::s("1.0.0")),
                    ("tenant_id", json::s(fixture::TENANT)),
                    ("project_id", json::s(fixture::PROJECT)),
                    ("evaluator_id", json::s("doc-check")),
                    (
                        "evaluator_version",
                        evaluator.get("evaluator_version").unwrap().clone(),
                    ),
                    ("class", json::s("evidence")),
                    ("registry_revision", Value::Int(1)),
                    ("criteria", evaluator.get("criteria").unwrap().clone()),
                ]),
            ),
        ])]),
    );
    doc
}

fn decisions() -> Vec<Value> {
    let mut all = Vec::new();
    for line in include_str!("../../../m1/fixtures/sets/golden-decisions.jsonl").lines() {
        let case = json::parse(line.as_bytes()).unwrap();
        let input = case.get("input").unwrap();
        let outcome = commands::decide(json::canonical(input).unwrap().as_bytes());
        let decision = json::parse(outcome.as_bytes()).unwrap();
        assert!(decision.get("status").is_some());
        all.push(decision);
    }
    for (input, cases) in [
        (
            include_bytes!("../../compiler/fixtures/permit.input.json").as_slice(),
            include_bytes!("../../compiler/fixtures/permit.cases.json").as_slice(),
        ),
        (
            include_bytes!("../../compiler/fixtures/approval.input.json").as_slice(),
            include_bytes!("../../compiler/fixtures/approval.cases.json").as_slice(),
        ),
        (
            include_bytes!("../../compiler/fixtures/deny.input.json").as_slice(),
            include_bytes!("../../compiler/fixtures/deny.cases.json").as_slice(),
        ),
    ] {
        let compiled = writ_policy_compile::evaluate(input);
        assert!(compiled.accepted);
        let policy = load_bundle(compiled.document.get("bundle").unwrap()).unwrap();
        let now = Timestamp::parse_strict("2026-11-19T00:00:00Z").unwrap();
        for case in json::parse(cases).unwrap().as_arr().unwrap() {
            let decision = decide(
                case.get("request").unwrap(),
                Policy::Loaded(&policy),
                StateRevision::new(1).unwrap(),
                now,
            )
            .to_value();
            assert_eq!(&decision, case.get("expected").unwrap());
            all.push(decision);
        }
    }
    all
}

#[test]
fn complete_pack_lifecycle() {
    let baseline = decisions();
    let lab = Lab::new();
    let contract = lab.publish_contract("1.0.0", 0);
    lab.publish_contract("2.0.0", 2);
    let evaluator = lab.publish_evaluator();
    let retire_fields = Lab::contract_fields("1.0.0", 5);
    lab.deprecate_contract();
    let retirement = lab.contract_request("retire", &retire_fields, "retire-after-disable");
    let envelope1 = lab.sign(&payload(PACK, "1.0.0", &contract, &evaluator));
    let envelope2 = lab.sign(&payload(PACK, "2.0.0", &contract, &evaluator));
    let snapshots = [
        fs::read(&lab.store).unwrap(),
        fs::read(&lab.contracts).unwrap(),
        fs::read(&lab.evaluator).unwrap(),
    ];
    let preview = lab.pack_preview("install", "1.0.0", 0, Some(envelope1.clone()));
    result(&lab.api(PUBLISHER, &preview));
    assert_eq!(
        snapshots,
        [
            fs::read(&lab.store).unwrap(),
            fs::read(&lab.contracts).unwrap(),
            fs::read(&lab.evaluator).unwrap()
        ]
    );
    let mut receipts = vec![
        lab.install("1.0.0", 0, envelope1.clone(), "install-first"),
        lab.install("2.0.0", 1, envelope2.clone(), "install-second"),
    ];
    for (version, envelope) in [("1.0.0", envelope1), ("2.0.0", envelope2)] {
        let verify = request("packs.verify", &target(version));
        let response = lab.cli(VIEWER, &verify);
        assert_eq!(lab.api(VIEWER, &verify), response);
        assert_eq!(
            result(&response).get("envelope_digest").unwrap().as_str(),
            Some(json::digest(&envelope).unwrap().as_str())
        );
        let inspect = request("packs.inspect", &target(version));
        assert_eq!(lab.api(VIEWER, &inspect), lab.cli(VIEWER, &inspect));
        assert_eq!(
            result(&lab.api(VIEWER, &inspect)).get("envelope"),
            Some(&envelope)
        );
    }
    let enable = lab.pack_request(
        &lab.pack_preview("enable", "1.0.0", 2, None),
        "enable",
        "enable-first",
    );
    receipts.push(lab.cli(PUBLISHER, &enable));
    assert_eq!(lab.api(PUBLISHER, &enable), receipts[2]);
    assert_eq!(
        lab.history(PACK).get("active_version"),
        Some(&json::s("1.0.0"))
    );
    assert_eq!(decisions(), baseline);
    assert_eq!(
        code(&lab.api(PUBLISHER, &retirement)),
        Some("ACTIVE_DEPENDENTS")
    );

    let upgrade = lab.pack_request(
        &lab.pack_preview("upgrade", "2.0.0", 3, None),
        "upgrade",
        "upgrade-second",
    );
    receipts.push(lab.api(PUBLISHER, &upgrade));
    assert_eq!(lab.cli(PUBLISHER, &upgrade), receipts[3]);
    assert_eq!(
        lab.history(PACK).get("active_version"),
        Some(&json::s("2.0.0"))
    );
    assert_eq!(decisions(), baseline);
    assert_eq!(
        code(&lab.cli(PUBLISHER, &retirement)),
        Some("ACTIVE_DEPENDENTS")
    );

    let disable = lab.pack_request(
        &lab.pack_preview("disable", "2.0.0", 4, None),
        "disable",
        "disable-second",
    );
    let mut changed_confirmation = disable.clone();
    put(
        &mut changed_confirmation,
        "confirmation",
        json::s("disable another pack"),
    );
    let before = lab.history(PACK);
    assert_eq!(
        code(&lab.cli(PUBLISHER, &changed_confirmation)),
        Some("INVALID_REQUEST")
    );
    assert_eq!(lab.history(PACK), before);
    receipts.push(lab.cli(PUBLISHER, &disable));
    assert_eq!(lab.api(PUBLISHER, &disable), receipts[4]);
    let history = lab.history(PACK);
    assert_eq!(history.get("active_version"), Some(&Value::Null));
    let recorded = receipts.iter().map(result).cloned().collect::<Vec<_>>();
    assert_eq!(history.get("history"), Some(&Value::Arr(recorded)));
    for (key, receipt) in [
        "install-first",
        "install-second",
        "enable-first",
        "upgrade-second",
        "disable-second",
    ]
    .into_iter()
    .zip(receipts)
    {
        let recover = request(
            "packs.recover",
            &json::obj([("idempotency_key", json::s(key))]),
        );
        assert_eq!(lab.api(PUBLISHER, &recover), receipt);
        assert_eq!(lab.cli(PUBLISHER, &recover), receipt);
    }
    result(&lab.api(PUBLISHER, &retirement));
    assert_eq!(
        code(&lab.api(VIEWER, &request("packs.verify", &target("2.0.0")))),
        Some("INVALID_PACK")
    );
    assert_eq!(decisions(), baseline);
    println!("Installed and verified both versions, enabled, upgraded and disabled; reopened exact receipts; D-058 retirement was fenced; {} complete Decisions unchanged.", baseline.len());
}

#[test]
fn owning_service_refuses_revoked_dependencies_without_management() {
    let lab = Lab::new();
    let contract = lab.publish_contract("1.0.0", 0);
    let evaluator = lab.publish_evaluator();
    let envelope = lab.sign(&payload(PACK, "1.0.0", &contract, &evaluator));
    lab.install("1.0.0", 0, envelope, "install-direct-probe");
    let preview = lab.pack_preview("enable", "1.0.0", 1, None);
    let enable = lab.pack_request(&preview, "enable", "direct-enable");
    let mut fields = enable.as_obj().unwrap().clone();
    for field in ["version", "scope", "operation"] {
        fields.remove(field);
    }
    let revoke = lab.revoke_evaluator();
    result(&lab.api(PUBLISHER, &revoke));
    let contracts = contract_fixture::open(&lab.contracts).unwrap();
    let packs = PackRegistry::new(
        &contracts,
        &lab.evaluator,
        evaluator_fixture::pack_dependencies(),
        &lab.trust,
    );
    let before = lab.history(PACK);
    assert_eq!(
        packs.execute_owner(
            &contract_fixture::scope(),
            PUBLISHER,
            &|| Ok(()),
            PackOperation::Enable,
            fields
        ),
        Err(Error::InvalidPack),
    );
    assert_eq!(lab.history(PACK), before);
}

#[test]
fn exact_dependencies_refuse_absence_substitution_and_unavailable_checks() {
    let lab = Lab::new();
    let contract = lab.publish_contract("1.0.0", 0);
    let evaluator = lab.publish_evaluator();
    let envelope = lab.sign(&payload(PACK, "1.0.0", &contract, &evaluator));
    let preview = lab.pack_preview("install", "1.0.0", 0, Some(envelope.clone()));
    let other = Lab::new();
    other.publish_evaluator();
    assert_eq!(code(&other.api(PUBLISHER, &preview)), Some("NOT_FOUND"));
    assert_eq!(
        code(&lab.api_at(PUBLISHER, &preview, &other.evaluator)),
        Some("UNAVAILABLE")
    );
    assert_eq!(
        code(&lab.api_at(
            PUBLISHER,
            &preview,
            &lab.dir.join("missing-evaluator.sqlite")
        )),
        Some("UNAVAILABLE")
    );
    let empty_evaluators = Lab::new();
    empty_evaluators.publish_contract("1.0.0", 0);
    assert_eq!(
        code(&empty_evaluators.api(PUBLISHER, &preview)),
        Some("INVALID_PACK")
    );
    assert_eq!(lab.history(PACK).get("revision"), Some(&Value::Int(0)));
}

#[test]
fn receipt_and_human_intent_are_bound_to_one_pack_owner() {
    let lab = Lab::new();
    let contract = lab.publish_contract("1.0.0", 0);
    let evaluator = lab.publish_evaluator();
    let envelope = lab.sign(&payload(PACK, "1.0.0", &contract, &evaluator));
    let preview = lab.pack_preview("install", "1.0.0", 0, Some(envelope));
    let commit = lab.pack_request(&preview, "install", "first-owner-install");
    result(&lab.api(PUBLISHER, &commit));

    let second_path = lab.dir.join("second-contract-owner.sqlite");
    pack_fixture::initialize(&second_path, &lab.evaluator).unwrap();
    let store = Store::open_without_recovery(&lab.store).unwrap();
    let second = contract_fixture::open(&second_path).unwrap();
    let packs = PackRegistry::new(
        &second,
        &lab.evaluator,
        evaluator_fixture::pack_dependencies(),
        &lab.trust,
    );
    let authority = fixture::authority();
    let service = Service::new(&store, Some(&authority)).with_packs(&packs);
    let recover = request(
        "packs.recover",
        &json::obj([("idempotency_key", json::s("first-owner-install"))]),
    );
    let bytes = execute_v1_with_service(
        &service,
        PUBLISHER.as_bytes(),
        json::canonical(&recover).unwrap().as_bytes(),
    );
    assert_eq!(code(&json::parse(&bytes).unwrap()), Some("UNAVAILABLE"));

    let other_auth = Store::create(&lab.dir.join("second-management.sqlite")).unwrap();
    let original = contract_fixture::open(&lab.contracts).unwrap();
    let original_packs = PackRegistry::new(
        &original,
        &lab.evaluator,
        evaluator_fixture::pack_dependencies(),
        &lab.trust,
    );
    let other_service = Service::new(&other_auth, Some(&authority)).with_packs(&original_packs);
    let bytes = execute_v1_with_service(
        &other_service,
        PUBLISHER.as_bytes(),
        json::canonical(&commit).unwrap().as_bytes(),
    );
    assert_eq!(
        code(&json::parse(&bytes).unwrap()),
        Some("IDEMPOTENCY_CONFLICT")
    );
    let preview = lab.pack_preview("enable", "1.0.0", 1, None);
    let enable = lab.pack_request(&preview, "enable", "pending-first-manager");
    let db = Connection::open(&lab.contracts).unwrap();
    db.execute_batch(
        "CREATE TRIGGER abort_pack_audit BEFORE INSERT ON pack_audit
        BEGIN SELECT RAISE(ABORT, 'fixture audit failure'); END;",
    )
    .unwrap();
    assert_eq!(code(&lab.api(PUBLISHER, &enable)), Some("UNAVAILABLE"));
    db.execute_batch("DROP TRIGGER abort_pack_audit").unwrap();
    let bytes = execute_v1_with_service(
        &other_service,
        PUBLISHER.as_bytes(),
        json::canonical(&enable).unwrap().as_bytes(),
    );
    assert_eq!(
        code(&json::parse(&bytes).unwrap()),
        Some("REVISION_CONFLICT")
    );
    let mut fields = enable.as_obj().unwrap().clone();
    for field in ["version", "scope", "operation"] {
        fields.remove(field);
    }
    assert_eq!(
        original_packs.execute_owner(
            &contract_fixture::scope(),
            PUBLISHER,
            &|| Ok(()),
            PackOperation::Enable,
            fields
        ),
        Err(Error::RevisionConflict),
    );
    assert_eq!(lab.history(PACK).get("active_version"), Some(&Value::Null));
}

#[path = "pack_workflow/ordering.rs"]
mod ordering;

#[path = "pack_workflow/refusals.rs"]
mod refusals;
