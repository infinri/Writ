#![cfg(feature = "fixtures")]
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::{
    ffi::OsStr,
    fs::{self, DirBuilder},
    io::Write,
    os::unix::fs::DirBuilderExt,
    path::{Path, PathBuf},
    process::{Command, Output, Stdio},
    sync::{
        atomic::{AtomicU64, Ordering},
        Barrier,
    },
    thread,
};

use writ_kernel::{
    contracts::CriterionClass,
    json::{self, Value},
};
use writ_policy_management_lab::{execute_v1_with_publication, fixture, Service, Store};
use writ_policy_publication_lab::{
    Error as RegistryError, Intent, Operation, Publication, Purpose, Registry,
};

const MANAGE: &str = env!("CARGO_BIN_EXE_writ-manage-fixture");
const AUTHORITY: &str = env!("CARGO_BIN_EXE_writ-publication-authority-fixture");
const AUTHOR: &[u8] = b"fixture-author";
const PUBLISHER: &[u8] = b"fixture-publisher";
const DUAL: &[u8] = b"fixture-author-publisher";
const PERMIT: &[u8] = include_bytes!("../../compiler/fixtures/permit.input.json");
const DENY: &[u8] = include_bytes!("../../compiler/fixtures/deny.input.json");

struct Lab {
    directory: PathBuf,
    drafts: PathBuf,
    registry: PathBuf,
}

impl Lab {
    fn new() -> Self {
        static NEXT: AtomicU64 = AtomicU64::new(0);
        let directory = std::env::temp_dir().join(format!(
            "writ-managed-publication-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        DirBuilder::new().mode(0o700).create(&directory).unwrap();
        let drafts = directory.join("drafts.sqlite");
        let registry = directory.join("registry.sqlite");
        result(&run_json(
            MANAGE,
            [OsStr::new("init"), drafts.as_os_str()],
            b"",
        ));
        result(&run_json(
            MANAGE,
            [OsStr::new("init-publication"), registry.as_os_str()],
            b"",
        ));
        Self {
            directory,
            drafts,
            registry,
        }
    }

    fn credential(&self, name: &str, bytes: &[u8]) -> PathBuf {
        let path = self.directory.join(name);
        fs::write(&path, bytes).unwrap();
        path
    }

    fn cli(&self, credential: &Path, input: &Value) -> Value {
        run_json(
            MANAGE,
            [
                OsStr::new("v1"),
                self.drafts.as_os_str(),
                credential.as_os_str(),
                self.registry.as_os_str(),
            ],
            canonical(input).as_bytes(),
        )
    }

    fn api(
        &self,
        trust: &fixture::FixtureTrust,
        credential: &[u8],
        input: &Value,
        allow_overlap: bool,
    ) -> Value {
        let drafts = Store::open(&self.drafts).unwrap();
        let registry = Registry::open_for_administration(
            &self.registry,
            fixture::publication_scope().unwrap(),
            trust.clone(),
        )
        .unwrap()
        .with_management_source(drafts.clone());
        let authority = fixture::authority();
        let service = Service::new(&drafts, Some(&authority)).with_publication(&registry);
        let service = if allow_overlap {
            service.allow_self_publication()
        } else {
            service
        };
        json::parse(&execute_v1_with_publication(
            &service,
            credential,
            canonical(input).as_bytes(),
        ))
        .unwrap()
    }

    fn open_registry(&self, trust: &fixture::FixtureTrust) -> Registry<fixture::FixtureTrust> {
        Registry::open(
            &self.registry,
            fixture::publication_scope().unwrap(),
            trust.clone(),
        )
        .unwrap()
        .with_management_source(Store::open(&self.drafts).unwrap())
    }
}

impl Drop for Lab {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.directory);
    }
}

fn run<const N: usize>(program: &str, args: [&OsStr; N], input: &[u8]) -> Output {
    let mut child = Command::new(program)
        .args(args)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    child.stdin.take().unwrap().write_all(input).unwrap();
    child.wait_with_output().unwrap()
}

fn run_json<const N: usize>(program: &str, args: [&OsStr; N], input: &[u8]) -> Value {
    let output = run(program, args, input);
    let response = json::parse(&output.stdout).unwrap_or_else(|error| {
        panic!(
            "fixture response {error:?}: {:?}",
            String::from_utf8_lossy(&output.stderr)
        )
    });
    assert_eq!(
        output.status.success(),
        response.get("ok").and_then(Value::as_bool).unwrap_or(true),
        "{response:?}",
    );
    response
}

fn canonical(value: &Value) -> String {
    json::canonical(value).unwrap()
}

fn request(operation: &str, fields: impl IntoIterator<Item = (&'static str, Value)>) -> Value {
    Value::Obj(
        [
            ("version", json::s("/manage/v1")),
            ("operation", json::s(operation)),
            ("scope", fixture::publication_scope().unwrap().to_value()),
        ]
        .into_iter()
        .chain(fields)
        .map(|(key, value)| (key.into(), value))
        .collect(),
    )
}

fn changed(mut value: Value, key: &str, field: Value) -> Value {
    let Value::Obj(fields) = &mut value else {
        panic!("expected an object")
    };
    fields.insert(key.into(), field);
    value
}

fn draft(id: &str, revision: i64) -> Value {
    json::obj([("id", json::s(id)), ("revision", Value::Int(revision))])
}

fn save(operation: &str, id: &str, expected: i64, key: &str, policy: &[u8]) -> Value {
    request(
        operation,
        [
            ("id", json::s(id)),
            ("expected_revision", Value::Int(expected)),
            ("idempotency_key", json::s(key)),
            ("preview", Value::Bool(false)),
            ("candidate", json::parse(policy).unwrap()),
        ],
    )
}

fn signed(candidate: &[u8], version: i64) -> Value {
    run_json(
        AUTHORITY,
        [
            OsStr::new("sign-publication"),
            OsStr::new(&version.to_string()),
        ],
        candidate,
    )
}

fn history() -> Value {
    request(
        "publication_history",
        [("after", Value::Int(0)), ("limit", Value::Int(10))],
    )
}

fn preview(
    expected: i64,
    version: i64,
    key: &str,
    candidate: Option<(Value, Value)>,
    ack: bool,
) -> Value {
    let fields = [
        ("expected_revision", Value::Int(expected)),
        ("publication_version", Value::Int(version)),
        ("idempotency_key", json::s(key)),
        ("acknowledge_self", Value::Bool(ack)),
    ];
    if let Some((draft, publication)) = candidate {
        request(
            "preview_publish",
            fields
                .into_iter()
                .chain([("draft", draft), ("publication", publication)]),
        )
    } else {
        request("preview_rollback", fields)
    }
}

fn result(response: &Value) -> &Value {
    assert_eq!(response.get("ok"), Some(&Value::Bool(true)), "{response:?}");
    response.get("result").unwrap()
}

fn refused(response: &Value) {
    assert_eq!(
        response.get("ok"),
        Some(&Value::Bool(false)),
        "{response:?}"
    );
}

fn refused_with(response: &Value, code: &str) {
    refused(response);
    assert_eq!(
        response
            .get("error")
            .and_then(|error| error.get("code"))
            .and_then(Value::as_str),
        Some(code),
        "{response:?}",
    );
}

fn token(preview: &Value, credential: &Path, ceremony: &str) -> Value {
    run_json(
        AUTHORITY,
        [
            OsStr::new("authorize-publication"),
            credential.as_os_str(),
            OsStr::new(ceremony),
        ],
        canonical(preview).as_bytes(),
    )
    .get("presentation")
    .unwrap()
    .clone()
}

fn commit(preview: Value, receipt: &Value, presentation: Value) -> Value {
    let operation = match preview.get("operation").and_then(Value::as_str) {
        Some("preview_publish") => "publish",
        Some("preview_rollback") => "rollback",
        Some("preview_retire") => "retire",
        Some("preview_deactivate") => "deactivate",
        _ => panic!("expected a preview"),
    };
    let preview = changed(preview, "operation", json::s(operation));
    let preview = changed(
        preview,
        "intent_digest",
        result(receipt).get("intent_digest").unwrap().clone(),
    );
    changed(preview, "presentation", presentation)
}

#[test]
fn headless_api_and_cli_share_the_same_workflow_and_authoritative_receipts() {
    let lab = Lab::new();
    let trust = fixture::FixtureTrust::default();
    let author = lab.credential("author", AUTHOR);
    let publisher = lab.credential("publisher", PUBLISHER);
    let step_up = lab.credential("step-up", b"fixture-publisher-step-up");

    for (id, candidate) in [("permit", PERMIT), ("deny", DENY)] {
        let create = save("create", id, 0, id, candidate);
        let created = lab.cli(&author, &create);
        assert_eq!(result(&created).get("revision"), Some(&Value::Int(1)));
        assert_eq!(created, lab.api(&trust, AUTHOR, &create, false));
        let validate = request("validate", [("draft", draft(id, 1))]);
        let validated = lab.cli(&author, &validate);
        assert_eq!(
            result(&validated).get("offline_proven"),
            Some(&Value::Bool(true))
        );
        assert_eq!(validated, lab.api(&trust, AUTHOR, &validate, false));
    }

    let empty = lab.cli(&publisher, &history());
    assert_eq!(result(&empty).get("selected"), Some(&Value::Null));
    for (expected, id, candidate) in [(0, "permit", PERMIT), (1, "deny", DENY)] {
        let version = expected + 1;
        let proposal = preview(
            expected,
            version,
            id,
            Some((draft(id, 1), signed(candidate, version))),
            false,
        );
        let shown = lab.cli(&publisher, &proposal);
        assert_eq!(
            result(&shown).get("publication_version"),
            Some(&Value::Int(version))
        );
        assert_eq!(shown, lab.api(&trust, PUBLISHER, &proposal, false));
        if expected == 0 {
            let alternative = preview(0, 1, id, Some((draft("deny", 1), signed(DENY, 1))), false);
            result(&lab.cli(&publisher, &alternative));
            assert_eq!(empty, lab.cli(&publisher, &history()));
        }
        let presentation = token(&shown, &step_up, id);
        let request = commit(proposal, &shown, presentation.clone());
        let applied = lab.cli(&publisher, &request);
        assert_eq!(result(&applied).get("revision"), Some(&Value::Int(version)));
        assert_eq!(applied, lab.api(&trust, PUBLISHER, &request, false));
        let state = canonical(&lab.cli(&publisher, &history()));
        assert!(!state.contains(presentation.as_str().unwrap()));
    }

    let history_before = lab.cli(&publisher, &history());
    assert_eq!(
        result(&history_before)
            .get("events")
            .unwrap()
            .as_arr()
            .unwrap()
            .len(),
        2
    );
    let rollback = preview(2, 1, "rollback", None, false);
    let shown = lab.cli(&publisher, &rollback);
    assert_eq!(shown, lab.api(&trust, PUBLISHER, &rollback, false));
    assert_eq!(history_before, lab.cli(&publisher, &history()));
    let request = commit(rollback, &shown, token(&shown, &step_up, "rollback"));
    let rolled_back = lab.api(&trust, PUBLISHER, &request, false);
    assert_eq!(result(&rolled_back).get("revision"), Some(&Value::Int(3)));
    assert_eq!(rolled_back, lab.cli(&publisher, &request));

    let reopened = lab.open_registry(&trust);
    reopened
        .with_selection(|selection| {
            assert_eq!(
                (selection.revision(), selection.publication_version()),
                (3, 1)
            );
            assert_eq!(selection.publication().to_value(), signed(PERMIT, 1));
            assert!(selection.acceptance()?.criteria.iter().any(|criterion| {
                criterion.criterion_id == "review"
                    && criterion.required_class == CriterionClass::Human
            }));
            Ok(())
        })
        .unwrap();
    let events = reopened.audit().unwrap();
    assert_eq!(
        events
            .iter()
            .map(|event| (event.revision, event.publication_version))
            .collect::<Vec<_>>(),
        [(1, 1), (2, 2), (3, 1)],
    );
    assert_eq!(
        events[2].intent_digest,
        result(&shown)
            .get("intent_digest")
            .unwrap()
            .as_str()
            .unwrap()
    );
    drop(reopened);
    let history_after = lab.cli(&publisher, &history());
    assert_eq!(
        history_after,
        lab.api(&trust, b"fixture-viewer", &history(), false)
    );
    assert_eq!(
        result(&history_after)
            .get("selected")
            .unwrap()
            .get("revision"),
        Some(&Value::Int(3)),
    );
}

#[test]
fn retirement_rechecks_exact_context_and_recovers_the_same_receipt_after_reopen() {
    let lab = Lab::new();
    let trust = fixture::FixtureTrust::default();
    let author = lab.credential("author", AUTHOR);
    let publisher = lab.credential("publisher", PUBLISHER);
    let step_up = lab.credential("step-up", b"fixture-publisher-step-up");
    for (expected, name, policy) in [(0, "permit", PERMIT), (1, "deny", DENY)] {
        result(&lab.cli(&author, &save("create", name, 0, name, policy)));
        let version = expected + 1;
        let proposal = preview(
            expected,
            version,
            name,
            Some((draft(name, 1), signed(policy, version))),
            false,
        );
        let shown = lab.cli(&publisher, &proposal);
        result(&lab.cli(
            &publisher,
            &commit(proposal, &shown, token(&shown, &step_up, name)),
        ));
    }

    let before = lab.cli(&publisher, &history());
    let proposal = changed(
        preview(2, 1, "retire", None, false),
        "operation",
        json::s("preview_retire"),
    );
    let proposal = changed(proposal, "confirmation", json::s("retire 1"));
    let shown = lab.cli(&publisher, &proposal);
    assert_eq!(shown, lab.api(&trust, PUBLISHER, &proposal, false));
    assert_eq!(result(&shown).get("revision"), Some(&Value::Int(2)));
    assert_eq!(
        result(&shown).get("intent").unwrap().get("confirmation"),
        Some(&json::s("retire 1"))
    );
    let current = changed(proposal.clone(), "publication_version", Value::Int(2));
    let current = changed(current, "confirmation", json::s("retire 2"));
    refused_with(&lab.cli(&publisher, &current), "REVISION_CONFLICT");

    for operation in ["preview_deactivate", "deactivate"] {
        for (target, code) in [
            ("review", "INVALID_POLICY"),
            ("m6.synthetic.deny", "UNSUPPORTED"),
        ] {
            let deactivate = request(
                operation,
                [
                    ("expected_revision", Value::Int(2)),
                    ("publication_version", Value::Int(2)),
                    ("policy_id", json::s(target)),
                    ("confirmation", json::s(&format!("deactivate {target}"))),
                ],
            );
            let response = lab.cli(&publisher, &deactivate);
            refused_with(&response, code);
            assert_eq!(response, lab.api(&trust, PUBLISHER, &deactivate, false));
        }
    }
    let dispatch_db = rusqlite::Connection::open(&lab.drafts).unwrap();
    let count_dispatches = || {
        dispatch_db
            .query_row("SELECT count(*) FROM publication_intents", [], |row| {
                row.get::<_, i64>(0)
            })
            .unwrap()
    };
    assert_eq!(count_dispatches(), 2);

    let retire = commit(proposal, &shown, token(&shown, &step_up, "retire"));
    for (field, replacement, code) in [
        ("confirmation", json::s("Retire 1"), "INVALID_REQUEST"),
        ("expected_revision", Value::Int(1), "REVISION_CONFLICT"),
        (
            "idempotency_key",
            json::s("changed-context"),
            "REVISION_CONFLICT",
        ),
    ] {
        let altered = changed(retire.clone(), field, replacement);
        let response = lab.cli(&publisher, &altered);
        refused_with(&response, code);
        assert_eq!(response, lab.api(&trust, PUBLISHER, &altered, false));
    }
    assert_eq!(count_dispatches(), 2);
    assert_eq!(before, lab.cli(&publisher, &history()));

    let receipt = lab.api(&trust, PUBLISHER, &retire, false);
    assert_eq!(result(&receipt).get("revision"), Some(&Value::Int(2)));
    assert_eq!(
        result(&receipt).get("publication_version"),
        Some(&Value::Int(1))
    );
    assert_eq!(result(&receipt).get("audit_id"), Some(&Value::Int(3)));
    assert_eq!(receipt, lab.cli(&publisher, &retire));
    let changed_confirmation = changed(retire, "confirmation", json::s("retire 2"));
    refused_with(
        &lab.cli(&publisher, &changed_confirmation),
        "IDEMPOTENCY_CONFLICT",
    );
    let after = lab.cli(&publisher, &history());
    assert_eq!(
        result(&before).get("selected"),
        result(&after).get("selected")
    );
    let events = result(&after).get("events").unwrap().as_arr().unwrap();
    assert_eq!(events.len(), 3);
    assert_eq!(events[2].get("operation"), Some(&json::s("retire")));
    assert_eq!(events[2].get("context"), result(&shown).get("context"));
    assert_eq!(
        events[..2],
        *result(&before).get("events").unwrap().as_arr().unwrap()
    );
    let reopened = lab.open_registry(&trust);
    assert_eq!(
        reopened
            .current_selection()
            .unwrap()
            .unwrap()
            .publication_version,
        2
    );
    assert_eq!(reopened.audit().unwrap()[2].operation, Operation::Retire);

    let rollback = preview(2, 1, "restore-retired", None, false);
    assert_eq!(reopened.preview(2, 1, None), Err(RegistryError::Retired));
    drop(reopened);
    refused_with(&lab.cli(&publisher, &rollback), "NOT_FOUND");
    assert_eq!(lab.cli(&publisher, &history()), after);
}

#[test]
fn author_viewer_services_and_other_realms_cannot_supply_human_authority() {
    let lab = Lab::new();
    let trust = fixture::FixtureTrust::default();
    let step_up = lab.credential("step-up", b"fixture-publisher-step-up");
    let management_only = lab.credential("management-only", PUBLISHER);
    result(&lab.api(
        &trust,
        AUTHOR,
        &save("create", "draft", 0, "save", PERMIT),
        false,
    ));
    let proposal = preview(
        0,
        1,
        "publish",
        Some((draft("draft", 1), signed(PERMIT, 1))),
        false,
    );
    let shown = lab.api(&trust, PUBLISHER, &proposal, false);
    let approved = token(&shown, &step_up, "publish");
    let request = commit(proposal, &shown, approved.clone());
    for credential in [
        AUTHOR,
        b"fixture-viewer",
        b"fixture-service",
        b"fixture-agent",
        b"fixture-adapter",
        b"fixture-publisher-step-up",
    ] {
        refused(&lab.api(&trust, credential, &request, false));
    }
    let wrong_scope = changed(
        request.clone(),
        "scope",
        json::obj([("tenant", json::s("m6-lab")), ("project", json::s("other"))]),
    );
    refused(&lab.api(&trust, PUBLISHER, &wrong_scope, false));

    let wrong_credential = run(
        AUTHORITY,
        [
            OsStr::new("authorize-publication"),
            management_only.as_os_str(),
            OsStr::new("bad"),
        ],
        canonical(&shown).as_bytes(),
    );
    assert!(!wrong_credential.status.success());
    let malformed_ceremony = run(
        AUTHORITY,
        [
            OsStr::new("authorize-publication"),
            step_up.as_os_str(),
            OsStr::new("Upper.case"),
        ],
        canonical(&shown).as_bytes(),
    );
    assert!(!malformed_ceremony.status.success());

    let make_proposal = |key| {
        let proposal = preview(
            0,
            1,
            key,
            Some((draft("draft", 1), signed(PERMIT, 1))),
            false,
        );
        let shown = lab.api(&trust, PUBLISHER, &proposal, false);
        (proposal, shown)
    };
    let forged = changed(request, "presentation", json::s("fixture-publisher"));
    refused_with(&lab.api(&trust, PUBLISHER, &forged, false), "UNAUTHORIZED");
    let (proposal, shown) = make_proposal("digest-only");
    let exact_digest = result(&shown).get("intent_digest").unwrap().clone();
    let digest_alone = commit(proposal, &shown, exact_digest);
    refused_with(
        &lab.api(&trust, PUBLISHER, &digest_alone, false),
        "UNAUTHORIZED",
    );

    let (proposal, shown) = make_proposal("expired");
    let expired = commit(proposal, &shown, token(&shown, &step_up, "expired"));
    trust
        .set_time(fixture::fixture_time().unwrap().plus_seconds(61))
        .unwrap();
    refused_with(&lab.api(&trust, PUBLISHER, &expired, false), "UNAUTHORIZED");
    trust.set_time(fixture::fixture_time().unwrap()).unwrap();
    let (proposal, shown) = make_proposal("revoked");
    let revoked = commit(proposal, &shown, token(&shown, &step_up, "revoked"));
    trust.revoke_publisher().unwrap();
    refused_with(&lab.api(&trust, PUBLISHER, &revoked, false), "UNAUTHORIZED");
    assert!(lab.open_registry(&trust).audit().unwrap().is_empty());
}

#[test]
fn self_publication_needs_host_opt_in_and_exact_audited_acknowledgment() {
    let lab = Lab::new();
    let trust = fixture::FixtureTrust::default();
    let step_up = lab.credential("step-up", b"fixture-author-publisher-step-up");
    result(&lab.api(
        &trust,
        DUAL,
        &save("create", "own", 0, "save", PERMIT),
        true,
    ));
    let pair = Some((draft("own", 1), signed(PERMIT, 1)));
    let no_ack = preview(0, 1, "self", pair.clone(), false);
    refused(&lab.api(&trust, DUAL, &no_ack, true));
    let acknowledged = preview(0, 1, "self", pair, true);
    refused(&lab.api(&trust, DUAL, &acknowledged, false));
    let shown = lab.api(&trust, DUAL, &acknowledged, true);
    let request = commit(acknowledged, &shown, token(&shown, &step_up, "self"));
    refused(&lab.api(
        &trust,
        DUAL,
        &changed(request.clone(), "acknowledge_self", Value::Bool(false)),
        true,
    ));
    result(&lab.api(&trust, DUAL, &request, true));
    let state = lab.api(&trust, b"fixture-viewer", &history(), false);
    let event = &result(&state).get("events").unwrap().as_arr().unwrap()[0];
    assert_eq!(event.get("context"), result(&shown).get("context"));
    assert_eq!(
        event.get("human").and_then(Value::as_str),
        Some("fixture-author-publisher")
    );
}

#[test]
fn edit_after_authorization_tampering_stale_proof_and_mixed_pairs_refuse() {
    let lab = Lab::new();
    let trust = fixture::FixtureTrust::default();
    let step_up = lab.credential("step-up", b"fixture-publisher-step-up");
    result(&lab.api(
        &trust,
        AUTHOR,
        &save("create", "draft", 0, "create", PERMIT),
        false,
    ));

    let original = signed(PERMIT, 1);
    let authorization = original.get("authorization").unwrap().clone();
    let envelope = changed(
        authorization.get("envelope").unwrap().clone(),
        "signature",
        json::s(&"00".repeat(64)),
    );
    let tampered = changed(
        original.clone(),
        "authorization",
        changed(authorization, "envelope", envelope),
    );
    let wrong_proof = changed(
        original.get("prerequisites").unwrap().clone(),
        "proof_digest",
        json::s(&format!("sha256:{}", "00".repeat(32))),
    );
    let stale = changed(original.clone(), "prerequisites", wrong_proof);
    let mixed = changed(
        original.clone(),
        "acceptance",
        signed(DENY, 2).get("acceptance").unwrap().clone(),
    );
    for candidate in [tampered, stale, mixed] {
        let proposal = preview(0, 1, "invalid", Some((draft("draft", 1), candidate)), false);
        refused(&lab.api(&trust, PUBLISHER, &proposal, false));
    }

    let proposal = preview(0, 1, "changed", Some((draft("draft", 1), original)), false);
    let shown = lab.api(&trust, PUBLISHER, &proposal, false);
    let request = commit(proposal.clone(), &shown, token(&shown, &step_up, "changed"));
    result(&lab.api(
        &trust,
        AUTHOR,
        &save("edit", "draft", 1, "edit", DENY),
        false,
    ));
    refused_with(
        &lab.api(&trust, PUBLISHER, &proposal, false),
        "REVISION_CONFLICT",
    );
    refused_with(
        &lab.api(&trust, PUBLISHER, &request, false),
        "REVISION_CONFLICT",
    );
    assert!(lab.open_registry(&trust).audit().unwrap().is_empty());
}

#[test]
fn rollback_uses_fresh_authority_and_rechecks_revocation() {
    let lab = Lab::new();
    let trust = fixture::FixtureTrust::default();
    let step_up = lab.credential("step-up", b"fixture-publisher-step-up");
    let mut earlier = Value::Null;
    for (expected, name, policy) in [(0, "permit", PERMIT), (1, "deny", DENY)] {
        result(&lab.api(
            &trust,
            AUTHOR,
            &save("create", name, 0, name, policy),
            false,
        ));
        let version = expected + 1;
        let proposal = preview(
            expected,
            version,
            name,
            Some((draft(name, 1), signed(policy, version))),
            false,
        );
        let shown = lab.api(&trust, PUBLISHER, &proposal, false);
        let presentation = token(&shown, &step_up, name);
        let request = commit(proposal, &shown, presentation.clone());
        result(&lab.api(&trust, PUBLISHER, &request, false));
        earlier = presentation;
    }
    let rollback = preview(2, 1, "stale-auth", None, false);
    let shown = lab.api(&trust, PUBLISHER, &rollback, false);
    refused(&lab.api(&trust, PUBLISHER, &commit(rollback, &shown, earlier), false));

    let rollback = preview(2, 1, "revoked", None, false);
    let shown = lab.api(&trust, PUBLISHER, &rollback, false);
    let request = commit(rollback, &shown, token(&shown, &step_up, "rollback"));
    trust.revoke_policy_key(Purpose::Authorization).unwrap();
    refused(&lab.api(&trust, PUBLISHER, &request, false));
    let history = lab.api(&trust, b"fixture-viewer", &history(), false);
    let selection = result(&history).get("selected").unwrap();
    assert_eq!(selection.get("verified"), Some(&Value::Bool(false)));
    assert_eq!(selection.get("revision"), Some(&Value::Int(2)));
    let administrative = Registry::open_for_administration(
        &lab.registry,
        fixture::publication_scope().unwrap(),
        trust,
    )
    .unwrap();
    assert_eq!(administrative.audit().unwrap().len(), 2);
    assert!(administrative.current_selection().is_err());
}

#[test]
fn competing_fresh_authorized_commits_select_at_most_one_pair() {
    let lab = Lab::new();
    let trust = fixture::FixtureTrust::default();
    let step_up = lab.credential("step-up", b"fixture-publisher-step-up");
    let mut requests = Vec::new();
    for (name, policy) in [("left", PERMIT), ("right", DENY)] {
        result(&lab.api(
            &trust,
            AUTHOR,
            &save("create", name, 0, name, policy),
            false,
        ));
        let proposal = preview(0, 1, name, Some((draft(name, 1), signed(policy, 1))), false);
        let shown = lab.api(&trust, PUBLISHER, &proposal, false);
        requests.push(commit(proposal, &shown, token(&shown, &step_up, name)));
    }
    let drafts = Store::open(&lab.drafts).unwrap();
    let registry = Registry::open(&lab.registry, fixture::publication_scope().unwrap(), trust)
        .unwrap()
        .with_management_source(drafts.clone());
    let start = Barrier::new(2);
    let responses = thread::scope(|threads| {
        let workers: Vec<_> = requests
            .iter()
            .map(|request| {
                let drafts = &drafts;
                let registry = &registry;
                let start = &start;
                threads.spawn(move || {
                    let authority = fixture::authority();
                    let service = Service::new(drafts, Some(&authority)).with_publication(registry);
                    start.wait();
                    json::parse(&execute_v1_with_publication(
                        &service,
                        PUBLISHER,
                        canonical(request).as_bytes(),
                    ))
                    .unwrap()
                })
            })
            .collect();
        workers
            .into_iter()
            .map(|worker| worker.join().unwrap())
            .collect::<Vec<_>>()
    });
    let events = registry.audit().unwrap();
    assert_eq!(events.len(), 1);
    assert_eq!(events[0].revision, 1);
    let succeeded: Vec<_> = responses
        .iter()
        .filter(|response| response.get("ok") == Some(&Value::Bool(true)))
        .collect();
    assert_eq!(succeeded.len(), 1, "{responses:?}");
    assert_eq!(
        result(succeeded[0])
            .get("intent_digest")
            .and_then(Value::as_str),
        Some(events[0].intent_digest.as_str()),
    );
}

#[test]
fn registry_audit_failure_preserves_state_and_exact_retry_never_replays() {
    let lab = Lab::new();
    let trust = fixture::FixtureTrust::default();
    let step_up = lab.credential("step-up", b"fixture-publisher-step-up");
    result(&lab.api(
        &trust,
        AUTHOR,
        &save("create", "draft", 0, "create", PERMIT),
        false,
    ));
    let pair = Some((draft("draft", 1), signed(PERMIT, 1)));
    let proposal = preview(0, 1, "failed", pair.clone(), false);
    let shown = lab.api(&trust, PUBLISHER, &proposal, false);
    let presentation = token(&shown, &step_up, "failed");
    let request = commit(proposal, &shown, presentation.clone());
    let db = rusqlite::Connection::open(&lab.registry).unwrap();
    db.execute_batch(
        "CREATE TRIGGER prevent_audit BEFORE INSERT ON publication_audit
         BEGIN SELECT RAISE(ABORT, 'injected audit failure'); END;",
    )
    .unwrap();

    refused_with(&lab.api(&trust, PUBLISHER, &request, false), "UNAVAILABLE");
    assert!(lab.open_registry(&trust).audit().unwrap().is_empty());
    db.execute_batch("DROP TRIGGER prevent_audit;").unwrap();
    let pending = rusqlite::Connection::open(&lab.drafts)
        .unwrap()
        .query_row("SELECT count(*) FROM publication_intents", [], |row| {
            row.get::<_, i64>(0)
        })
        .unwrap();
    assert_eq!(pending, 1);
    let candidate = Publication::from_value(&pair.as_ref().unwrap().1).unwrap();
    let intent = managed_intent(&shown, &candidate);
    let registry = lab.open_registry(&trust);
    let presentation = presentation.as_str().unwrap().as_bytes();
    assert_eq!(
        registry.change(&intent, Some(&candidate), presentation),
        Err(RegistryError::Authority),
    );
    assert_eq!(
        registry.change_managed(&intent, Some(&candidate), presentation, || Ok(())),
        Err(RegistryError::Authority),
    );
    drop(registry);
    refused_with(&lab.api(&trust, PUBLISHER, &request, false), "UNAVAILABLE");
    assert!(lab
        .open_registry(&trust)
        .current_selection()
        .unwrap()
        .is_none());

    let proposal = preview(0, 1, "fresh", pair, false);
    let shown = lab.api(&trust, PUBLISHER, &proposal, false);
    let request = commit(proposal, &shown, token(&shown, &step_up, "fresh"));
    result(&lab.api(&trust, PUBLISHER, &request, false));
    let reopened = lab.open_registry(&trust);
    assert_eq!(reopened.audit().unwrap().len(), 1);
    assert_eq!(reopened.current_selection().unwrap().unwrap().revision, 1);
}

#[test]
fn a_managed_presentation_cannot_bypass_the_live_draft_fence() {
    let lab = Lab::new();
    let trust = fixture::FixtureTrust::default();
    let step_up = lab.credential("step-up", b"fixture-publisher-step-up");
    result(&lab.api(
        &trust,
        AUTHOR,
        &save("create", "draft", 0, "create", PERMIT),
        false,
    ));
    let signed_pair = signed(PERMIT, 1);
    let proposal = preview(
        0,
        1,
        "managed",
        Some((draft("draft", 1), signed_pair.clone())),
        false,
    );
    let shown = lab.api(&trust, PUBLISHER, &proposal, false);
    let presentation = token(&shown, &step_up, "managed");
    let candidate = Publication::from_value(&signed_pair).unwrap();
    let scope = fixture::publication_scope().unwrap();
    let intent = managed_intent(&shown, &candidate);
    let registry = Registry::open(&lab.registry, scope, trust.clone()).unwrap();
    assert_eq!(
        registry.change(
            &intent,
            Some(&candidate),
            presentation.as_str().unwrap().as_bytes(),
        ),
        Err(RegistryError::Authority),
    );
    let drafts = Store::open(&lab.drafts).unwrap();
    let registry = registry.with_management_source(drafts);
    assert_eq!(
        registry.change_managed(
            &intent,
            Some(&candidate),
            presentation.as_str().unwrap().as_bytes(),
            || Ok(()),
        ),
        Err(RegistryError::Authority),
    );
    assert!(registry.audit().unwrap().is_empty());
    drop(registry);
    result(&lab.api(
        &trust,
        PUBLISHER,
        &commit(proposal, &shown, presentation),
        false,
    ));
}

fn managed_intent(shown: &Value, candidate: &Publication) -> Intent {
    let scope = fixture::publication_scope().unwrap();
    let context = json::digest(result(shown).get("context").unwrap()).unwrap();
    let intent = Intent::new(scope, Operation::Publish, 0, 1, candidate)
        .unwrap()
        .with_management(context, "fixture-publisher")
        .unwrap();
    assert_eq!(
        intent.fingerprint().unwrap().as_str(),
        result(shown)
            .get("intent_digest")
            .unwrap()
            .as_str()
            .unwrap(),
    );
    intent
}

#[test]
fn optional_removal_api_cli_and_authoritative_reopen() {
    let lab = Lab::new();
    let trust = fixture::FixtureTrust::default();
    let author = lab.credential("author", AUTHOR);
    let publisher = lab.credential("publisher", PUBLISHER);
    let step_up = lab.credential("step-up", b"fixture-publisher-step-up");
    let source = include_bytes!("../../compiler/fixtures/optional-inspect.input.json");
    for (name, input) in [("source", source.as_slice()), ("replacement", DENY)] {
        result(&lab.cli(&author, &save("create", name, 0, name, input)));
    }
    let source_publication = signed(source, 1);
    let proposal = preview(
        0,
        1,
        "select-source",
        Some((draft("source", 1), source_publication.clone())),
        false,
    );
    let shown = lab.cli(&publisher, &proposal);
    result(&lab.cli(
        &publisher,
        &commit(proposal, &shown, token(&shown, &step_up, "select-source")),
    ));
    let ordinary = preview(
        1,
        2,
        "ordinary",
        Some((draft("replacement", 1), signed(DENY, 2))),
        false,
    );
    refused_with(&lab.cli(&publisher, &ordinary), "INVALID_POLICY");
    let replacement = run_json(
        AUTHORITY,
        [OsStr::new("sign-removal"), OsStr::new("2")],
        canonical(&source_publication).as_bytes(),
    );
    let policy = writ_policy_compile::OPTIONAL_INSPECT_POLICY;
    let mut proposal = preview(
        1,
        2,
        "remove-optional",
        Some((draft("replacement", 1), replacement)),
        false,
    );
    refused_with(&lab.cli(&publisher, &proposal), "INVALID_POLICY");
    proposal = changed(proposal, "operation", json::s("preview_deactivate"));
    proposal = changed(proposal, "policy_id", json::s(policy));
    proposal = changed(
        proposal,
        "confirmation",
        json::s(&Intent::deactivation_phrase(policy)),
    );

    let before = lab.cli(&publisher, &history());
    let shown = lab.cli(&publisher, &proposal);
    assert_eq!(shown, lab.api(&trust, PUBLISHER, &proposal, false));
    assert_eq!(lab.cli(&publisher, &history()), before);
    let context = result(&shown).get("context").unwrap();
    let delta = context.get("decision_changes").unwrap();
    for (side, status) in [("before", "allow"), ("after", "deny")] {
        let cases = delta.get(side).unwrap().as_arr().unwrap();
        assert_eq!(cases.len(), 6);
        let changed_case = cases
            .iter()
            .find(|case| case.get("id") == Some(&json::s("scope")))
            .unwrap();
        assert_eq!(
            changed_case.get("decision").unwrap().get("status"),
            Some(&json::s(status))
        );
    }
    let shown_body = result(&shown);
    let changed_context = changed(context.clone(), "decision_changes", Value::Null);
    let exact_context = json::digest(&changed_context).unwrap();
    let mut intent = shown_body.get("intent").unwrap().clone();
    let management = changed(
        intent.get("management").unwrap().clone(),
        "digest",
        json::s(exact_context.as_str()),
    );
    intent = changed(intent, "management", management);
    let digest = json::digest(&intent).unwrap();
    let modified = changed(
        changed(
            changed(shown_body.clone(), "context", changed_context),
            "intent",
            intent,
        ),
        "intent_digest",
        json::s(digest.as_str()),
    );
    let displayed = changed(shown.clone(), "result", modified);
    let refusal = run(
        AUTHORITY,
        [
            OsStr::new("authorize-publication"),
            step_up.as_os_str(),
            OsStr::new("wrong-decisions"),
        ],
        canonical(&displayed).as_bytes(),
    );
    assert!(!refusal.status.success());
    let fault_preview = changed(
        proposal.clone(),
        "idempotency_key",
        json::s("uncertain-removal"),
    );
    let fault_shown = lab.cli(&publisher, &fault_preview);
    let fault_request = commit(
        fault_preview,
        &fault_shown,
        token(&fault_shown, &step_up, "uncertain-removal"),
    );
    let registry_db = rusqlite::Connection::open(&lab.registry).unwrap();
    registry_db
        .execute_batch(
            "CREATE TRIGGER fault BEFORE INSERT ON publication_audit
         BEGIN SELECT RAISE(ABORT, 'fixture audit failure'); END;",
        )
        .unwrap();
    refused_with(&lab.cli(&publisher, &fault_request), "UNAVAILABLE");
    registry_db.execute_batch("DROP TRIGGER fault").unwrap();
    refused_with(&lab.cli(&publisher, &fault_request), "UNAVAILABLE");
    assert_eq!(lab.cli(&publisher, &history()), before);

    let authorized = commit(proposal, &shown, token(&shown, &step_up, "remove-optional"));
    let mandatory = changed(
        changed(
            authorized.clone(),
            "policy_id",
            json::s("m6.synthetic.deny"),
        ),
        "confirmation",
        json::s("deactivate m6.synthetic.deny"),
    );
    refused_with(&lab.cli(&publisher, &mandatory), "INVALID_POLICY");
    let altered = changed(
        authorized.clone(),
        "idempotency_key",
        json::s("changed-removal"),
    );
    refused_with(&lab.cli(&publisher, &altered), "REVISION_CONFLICT");
    assert_eq!(lab.cli(&publisher, &history()), before);

    // Discard the committed response. Another authorized selection advances the
    // head before the original caller reopens both stores and asks for its receipt.
    let _undelivered = lab.api(&trust, PUBLISHER, &authorized, false);
    let advance = preview(2, 1, "restore-optional", None, false);
    let later = lab.cli(&publisher, &advance);
    result(&lab.cli(
        &publisher,
        &commit(advance, &later, token(&later, &step_up, "restore-optional")),
    ));
    let reopened = lab.cli(&publisher, &history());
    let receipt = lab.cli(&publisher, &authorized);
    assert_eq!(result(&receipt).get("revision"), Some(&Value::Int(2)));
    assert_eq!(result(&receipt).get("audit_id"), Some(&Value::Int(2)));
    assert_eq!(lab.cli(&publisher, &history()), reopened);
    let events = result(&reopened).get("events").unwrap().as_arr().unwrap();
    assert_eq!(events.len(), 3);
    assert_eq!(events[1].get("context"), Some(context));
    assert_eq!(
        events[1].get("intent_digest"),
        result(&receipt).get("intent_digest")
    );
}
