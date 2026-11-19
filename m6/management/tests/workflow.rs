#![cfg(feature = "fixtures")]
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::{
    fs,
    io::Write,
    path::PathBuf,
    process::{Command, Stdio},
    sync::{
        atomic::{AtomicU64, Ordering},
        Barrier,
    },
    thread,
};

use rusqlite::Connection;
use writ_kernel::{
    contracts::StateRevision,
    decision::{decide, Policy},
    json::{self, Value},
    policy::load_bundle,
    time::Timestamp,
};
use writ_policy_management_lab::{
    execute_v1, fixture, Authenticator, Principal, RequestV1, Service, Store, MAX_REQUEST,
};

const AUTHOR: &[u8] = b"fixture-author";
const VIEWER: &[u8] = b"fixture-viewer";
const PERMIT: &[u8] = include_bytes!("../../compiler/fixtures/permit.input.json");
const DENY: &[u8] = include_bytes!("../../compiler/fixtures/deny.input.json");
const PERMIT_CASES: &[u8] = include_bytes!("../../compiler/fixtures/permit.cases.json");

struct Lab {
    directory: PathBuf,
    path: PathBuf,
}

impl Lab {
    fn new() -> Self {
        static SEQUENCE: AtomicU64 = AtomicU64::new(0);
        let directory = std::env::temp_dir().join(format!(
            "writ-management-{}-{}",
            std::process::id(),
            SEQUENCE.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&directory).unwrap();
        let path = directory.join("drafts.sqlite");
        Store::create(&path).unwrap();
        Self { directory, path }
    }

    fn open(&self) -> Store {
        Store::open(&self.path).unwrap()
    }
}

impl Drop for Lab {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.directory);
    }
}

fn request(operation: &str, fields: impl IntoIterator<Item = (&'static str, Value)>) -> Value {
    Value::Obj(
        [
            ("version", json::s("/manage/v1")),
            ("operation", json::s(operation)),
            (
                "scope",
                json::obj([
                    ("tenant", json::s("m6-lab")),
                    ("project", json::s("offline-proof")),
                ]),
            ),
        ]
        .into_iter()
        .chain(fields)
        .map(|(field, value)| (field.into(), value))
        .collect(),
    )
}

fn body(document: &Value) -> String {
    json::canonical(document).unwrap()
}

fn reference(id: &str, revision: i64) -> Value {
    json::obj([("id", json::s(id)), ("revision", Value::Int(revision))])
}

fn mutation(
    operation: &str,
    id: &str,
    revision: i64,
    key: &str,
    preview: bool,
    field: (&'static str, Value),
) -> Value {
    request(
        operation,
        [
            ("id", json::s(id)),
            ("expected_revision", Value::Int(revision)),
            ("idempotency_key", json::s(key)),
            ("preview", Value::Bool(preview)),
            field,
        ],
    )
}

fn create(id: &str, key: &str, policy: &[u8], preview: bool) -> Value {
    mutation(
        "create",
        id,
        0,
        key,
        preview,
        ("candidate", json::parse(policy).unwrap()),
    )
}

fn call(store: &Store, credential: &[u8], input: &Value) -> Value {
    json::parse(&execute_v1(
        store,
        Some(&fixture::authority()),
        credential,
        body(input).as_bytes(),
    ))
    .unwrap()
}

fn result(response: &Value) -> &Value {
    assert_eq!(response.get("ok"), Some(&Value::Bool(true)), "{response:?}");
    response.get("result").unwrap()
}

fn error(response: &Value) -> &str {
    assert_eq!(
        response.get("ok"),
        Some(&Value::Bool(false)),
        "{response:?}"
    );
    response
        .get("error")
        .unwrap()
        .get("code")
        .unwrap()
        .as_str()
        .unwrap()
}

fn set(document: &mut Value, field: &str, value: Value) {
    let Value::Obj(fields) = document else {
        panic!("expected object")
    };
    fields.insert(field.into(), value);
}

fn history(id: &str, after: i64, limit: i64) -> Value {
    request(
        "history",
        [
            ("id", json::s(id)),
            ("after", Value::Int(after)),
            ("limit", Value::Int(limit)),
        ],
    )
}

fn analysis_request(operation: &str, id: &str, revision: i64) -> Value {
    request(operation, [("draft", reference(id, revision))])
}

fn corpus() -> Value {
    json::parse(
        br#"[
      {"kind":"synthetic.inspect","resource":"synthetic:m6-lab/offline-proof/inventory/restricted/sealed/report"},
      {"kind":"synthetic.unknown","resource":"synthetic:m6-lab/offline-proof/inventory/restricted/sealed/report"}
    ]"#,
    )
    .unwrap()
}

#[test]
fn lifecycle_preview_exact_replay_clone_and_history_across_restart() {
    let lab = Lab::new();
    let store = lab.open();
    let preview = create("policy", "create", PERMIT, true);
    let proposed = call(&store, AUTHOR, &preview);
    assert_eq!(
        result(&proposed),
        &json::obj([("revision", Value::Int(1)), ("audit_id", Value::Null)])
    );
    assert_eq!(
        error(&call(&store, VIEWER, &history("policy", 0, 1))),
        "NOT_FOUND"
    );

    let creation = create("policy", "create", PERMIT, false);
    let created = call(&store, AUTHOR, &creation);
    assert_eq!(result(&created).get("audit_id"), Some(&Value::Int(1)));
    let edit = mutation(
        "edit",
        "policy",
        1,
        "edit",
        false,
        ("candidate", json::parse(DENY).unwrap()),
    );
    assert_eq!(
        result(&call(&store, AUTHOR, &edit)).get("revision"),
        Some(&Value::Int(2))
    );
    assert_eq!(
        error(&call(
            &store,
            AUTHOR,
            &mutation(
                "edit",
                "policy",
                1,
                "stale",
                false,
                ("candidate", json::parse(PERMIT).unwrap())
            )
        )),
        "REVISION_CONFLICT"
    );
    let clone = mutation(
        "clone",
        "copy",
        0,
        "clone",
        false,
        ("source", reference("policy", 1)),
    );
    assert_eq!(
        result(&call(&store, AUTHOR, &clone)).get("audit_id"),
        Some(&Value::Int(3))
    );

    let reopened = lab.open();
    assert_eq!(call(&reopened, AUTHOR, &creation), created);
    let typed = Service::new(&reopened, Some(&fixture::authority()))
        .execute(
            AUTHOR,
            RequestV1::parse(body(&creation).as_bytes()).unwrap(),
        )
        .unwrap()
        .encode();
    assert_eq!(json::parse(&typed).unwrap(), created);
    let first = call(&reopened, VIEWER, &history("policy", 0, 1));
    assert_eq!(result(&first).get("next"), Some(&Value::Int(1)));
    let second = call(&reopened, VIEWER, &history("policy", 1, 1));
    assert_eq!(result(&second).get("next"), Some(&Value::Null));
    let original = call(&reopened, VIEWER, &analysis_request("get", "policy", 1));
    let copy = call(&reopened, VIEWER, &analysis_request("get", "copy", 1));
    assert_eq!(result(&copy).get("source"), Some(&reference("policy", 1)));
    assert_eq!(result(&copy).get("digest"), result(&original).get("digest"));
    assert_eq!(
        error(&call(
            &reopened,
            AUTHOR,
            &create("another", "create", PERMIT, false)
        )),
        "IDEMPOTENCY_CONFLICT"
    );

    let db = Connection::open(&lab.path).unwrap();
    let events: i64 = db
        .query_row("SELECT COUNT(*) FROM draft_audit", [], |row| row.get(0))
        .unwrap();
    assert_eq!(events, 3);
}

#[test]
fn realm_role_scope_and_credential_spoofing_are_refused() {
    let lab = Lab::new();
    let store = lab.open();
    let create = create("policy", "create", PERMIT, false);
    assert!(call(&store, AUTHOR, &create).get("result").is_some());
    let get = analysis_request("get", "policy", 1);
    for credential in [
        b"fixture-agent".as_slice(),
        b"fixture-adapter",
        b"unknown",
        b"",
    ] {
        assert_eq!(error(&call(&store, credential, &get)), "UNAUTHORIZED");
    }
    assert_eq!(
        error(&json::parse(&execute_v1(&store, None, AUTHOR, body(&get).as_bytes())).unwrap()),
        "UNAUTHORIZED"
    );
    let absent = call(&store, AUTHOR, &analysis_request("get", "absent", 1));
    for (tenant, project) in [("another", "offline-proof"), ("m6-lab", "another")] {
        let mut outside = get.clone();
        set(
            &mut outside,
            "scope",
            json::obj([("tenant", json::s(tenant)), ("project", json::s(project))]),
        );
        assert_eq!(call(&store, AUTHOR, &outside), absent);
    }
    assert_eq!(call(&store, VIEWER, &create), absent);
    assert_eq!(
        error(&call(
            &store,
            VIEWER,
            &analysis_request("validate", "policy", 1)
        )),
        "NOT_FOUND"
    );
    for field in ["role", "subject", "identity", "owner", "grants"] {
        let mut spoof = get.clone();
        set(&mut spoof, field, json::s("private-canary"));
        let reply = call(&store, AUTHOR, &spoof);
        assert_eq!(error(&reply), "INVALID_REQUEST");
        assert!(!body(&reply).contains("private-canary"));
    }
}

#[test]
fn concurrent_different_requests_have_one_edit_and_exact_replays_share_receipt() {
    let lab = Lab::new();
    for (id, same_key, same_body) in [
        ("different-keys", false, true),
        ("same-key-replay", true, true),
        ("same-key-mismatch", true, false),
    ] {
        assert!(call(&lab.open(), AUTHOR, &create(id, id, PERMIT, false))
            .get("result")
            .is_some());
        let start = Barrier::new(2);
        let replies = thread::scope(|threads| {
            let mut operations = Vec::new();
            for index in 0..2 {
                let key = if same_key {
                    format!("{id}-edit-shared")
                } else {
                    format!("{id}-edit-{index}")
                };
                let edit = mutation(
                    "edit",
                    id,
                    1,
                    &key,
                    false,
                    (
                        "candidate",
                        json::parse(if same_body || index == 0 {
                            DENY
                        } else {
                            PERMIT
                        })
                        .unwrap(),
                    ),
                );
                let start = &start;
                let lab = &lab;
                operations.push(threads.spawn(move || {
                    let store = lab.open();
                    start.wait();
                    call(&store, AUTHOR, &edit)
                }));
            }
            operations
                .into_iter()
                .map(|operation| operation.join().unwrap())
                .collect::<Vec<_>>()
        });
        if same_key && same_body {
            assert_eq!(replies[0], replies[1]);
            assert_eq!(result(&replies[0]).get("revision"), Some(&Value::Int(2)));
        } else {
            assert_eq!(
                replies
                    .iter()
                    .filter(|reply| reply.get("result").is_some())
                    .count(),
                1
            );
            assert!(replies
                .iter()
                .filter(|reply| reply.get("error").is_some())
                .all(|reply| error(reply)
                    == if same_key {
                        "IDEMPOTENCY_CONFLICT"
                    } else {
                        "REVISION_CONFLICT"
                    }));
        }
        assert_eq!(
            result(&call(&lab.open(), VIEWER, &history(id, 0, 50)))
                .get("versions")
                .unwrap()
                .as_arr()
                .unwrap()
                .len(),
            2
        );
    }
}

struct ChangingAuthority {
    calls: AtomicU64,
    change_subject: bool,
}

impl Authenticator for ChangingAuthority {
    fn authenticate(&self, credential: &[u8]) -> Option<Principal> {
        if self.calls.fetch_add(1, Ordering::SeqCst) == 0 {
            fixture::authority().authenticate(credential)
        } else {
            let mut viewer = fixture::authority().authenticate(VIEWER)?;
            if !self.change_subject {
                viewer.subject = "fixture-author".into();
            }
            Some(viewer)
        }
    }
}

#[test]
fn commit_time_authority_and_storage_failures_roll_back_without_losing_retry() {
    let lab = Lab::new();
    let store = lab.open();
    let creation = create("policy", "same-key", PERMIT, false);
    for (change_subject, code) in [(true, "UNAUTHORIZED"), (false, "NOT_FOUND")] {
        let changed = ChangingAuthority {
            calls: AtomicU64::new(0),
            change_subject,
        };
        let response = execute_v1(&store, Some(&changed), AUTHOR, body(&creation).as_bytes());
        assert_eq!(error(&json::parse(&response).unwrap()), code);
    }

    let db = Connection::open(&lab.path).unwrap();
    for table in ["draft_audit", "draft_versions"] {
        db.execute_batch(&format!(
            "CREATE TRIGGER fail_write BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT, 'private-canary'); END"
        )).unwrap();
        let failure = call(&store, AUTHOR, &creation);
        assert_eq!(error(&failure), "UNAVAILABLE");
        assert!(!body(&failure).contains("private-canary"));
        for table in ["draft_audit", "draft_versions"] {
            let count: i64 = db
                .query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |row| {
                    row.get(0)
                })
                .unwrap();
            assert_eq!(count, 0);
        }
        db.execute_batch("DROP TRIGGER fail_write").unwrap();
    }

    let committed = call(&lab.open(), AUTHOR, &creation);
    assert_eq!(result(&committed).get("audit_id"), Some(&Value::Int(1)));
    assert_eq!(call(&lab.open(), AUTHOR, &creation), committed);
}

#[test]
fn unsupported_and_malformed_rules_refuse_with_redacted_pointers() {
    let lab = Lab::new();
    let store = lab.open();
    for (index, candidate) in [
        br#"{"manifests":"/private-canary","companions":[]}"#.as_slice(),
        include_bytes!("../../compiler/fixtures/permit.input.json").as_slice(),
    ]
    .into_iter()
    .enumerate()
    {
        let id = format!("invalid-{index}");
        let bytes = if index == 1 {
            String::from_utf8(candidate.to_vec())
                .unwrap()
                .replace(
                    "\"failure_posture\": \"deny\"",
                    "\"failure_posture\": \"deny\", \"private-canary\": \"/tmp/private-path\"",
                )
                .into_bytes()
        } else {
            candidate.to_vec()
        };
        assert!(call(&store, AUTHOR, &create(&id, &id, &bytes, false))
            .get("result")
            .is_some());
        let report = call(&store, AUTHOR, &analysis_request("validate", &id, 1));
        assert_eq!(result(&report).get("compilable"), Some(&Value::Bool(false)));
        let diagnostics = result(&report)
            .get("diagnostics")
            .unwrap()
            .as_arr()
            .unwrap();
        assert!(!diagnostics.is_empty());
        assert!(diagnostics.iter().all(|diagnostic| diagnostic
            .get("pointer")
            .unwrap()
            .as_str()
            .unwrap()
            .starts_with('/')));
        assert!(!body(&report).contains("private-canary"));
        assert!(!body(&report).contains("private-path"));
        let mut simulation = analysis_request("simulate", &id, 1);
        set(&mut simulation, "corpus", corpus());
        assert_eq!(error(&call(&store, AUTHOR, &simulation)), "INVALID_POLICY");
    }

    let too_large = vec![b' '; MAX_REQUEST + 1];
    let output = execute_v1(&store, Some(&fixture::authority()), AUTHOR, &too_large);
    assert_eq!(error(&json::parse(&output).unwrap()), "LIMIT_EXCEEDED");
    let unsupported = execute_v1(
        &store,
        Some(&fixture::authority()),
        AUTHOR,
        br#"{"version":"/manage/v1","version":"/manage/v1"}"#,
    );
    assert_eq!(
        error(&json::parse(&unsupported).unwrap()),
        "INVALID_REQUEST"
    );
}

#[test]
fn no_effect_simulations_and_complete_decision_differences() {
    let lab = Lab::new();
    let store = lab.open();
    for (id, candidate) in [
        ("permit", PERMIT),
        ("deny", DENY),
        (
            "approval",
            include_bytes!("../../compiler/fixtures/approval.input.json"),
        ),
    ] {
        assert!(call(&store, AUTHOR, &create(id, id, candidate, false))
            .get("result")
            .is_some());
    }
    let draft_bytes = fs::read(&lab.path).unwrap();
    let validate = call(&store, AUTHOR, &analysis_request("validate", "permit", 1));
    assert_eq!(
        result(&validate).get("offline_proven"),
        Some(&Value::Bool(true))
    );

    let simulate = |id| {
        let mut analysis = analysis_request("simulate", id, 1);
        set(&mut analysis, "corpus", corpus());
        call(&store, AUTHOR, &analysis)
    };
    let before = simulate("permit");
    let after = simulate("deny");
    let approval = simulate("approval");
    assert_eq!(simulate("permit"), before);
    for reply in [&before, &after, &approval] {
        assert_eq!(
            result(reply).get("runtime_authority"),
            Some(&Value::Bool(false))
        );
        for row in result(reply).get("cases").unwrap().as_arr().unwrap() {
            let decision = row.get("decision").unwrap();
            assert_eq!(decision.get("permit_reference"), Some(&Value::Null));
        }
    }

    let comparison = call(
        &store,
        AUTHOR,
        &request(
            "compare",
            [
                ("before", reference("permit", 1)),
                ("after", reference("deny", 1)),
                ("corpus", corpus()),
            ],
        ),
    );
    let comparison = result(&comparison);
    assert!(comparison
        .get("structure")
        .unwrap()
        .as_arr()
        .unwrap()
        .contains(&json::s("/companions/2")));
    let rows = |reply| result(reply).get("cases").unwrap().as_arr().unwrap();
    for (pair, (left, right)) in comparison
        .get("cases")
        .unwrap()
        .as_arr()
        .unwrap()
        .iter()
        .zip(rows(&before).iter().zip(rows(&after)))
    {
        assert_eq!(pair.get("before"), left.get("decision"));
        assert_eq!(pair.get("after"), right.get("decision"));
    }
    let pairs = comparison.get("cases").unwrap().as_arr().unwrap();
    for pointer in [
        "/status",
        "/policy_bundle_sha",
        "/decision_id",
        "/matched_rule_ids/0",
    ] {
        assert!(
            pairs[0]
                .get("changed")
                .unwrap()
                .as_arr()
                .unwrap()
                .contains(&json::s(pointer)),
            "{pairs:?}"
        );
    }
    assert!(pairs[1]
        .get("changed")
        .unwrap()
        .as_arr()
        .unwrap()
        .contains(&json::s("/policy_bundle_sha")));
    assert_eq!(fs::read(&lab.path).unwrap(), draft_bytes);
    let db = Connection::open(&lab.path).unwrap();
    assert_eq!(
        db.query_row("SELECT COUNT(*) FROM draft_audit", [], |row| row
            .get::<_, i64>(0))
            .unwrap(),
        3
    );
}

#[test]
fn kernel_decisions_stay_identical_before_during_and_after_management_unavailability() {
    let compiler = writ_policy_compile::evaluate(PERMIT);
    assert!(compiler.accepted);
    let active = load_bundle(compiler.document.get("bundle").unwrap()).unwrap();
    let cases = json::parse(PERMIT_CASES).unwrap();
    let decisions = || {
        cases
            .as_arr()
            .unwrap()
            .iter()
            .map(|fixture| {
                let decision = decide(
                    fixture.get("request").unwrap(),
                    Policy::Loaded(&active),
                    StateRevision::new(1).unwrap(),
                    Timestamp::parse_strict("2026-11-19T00:00:00Z").unwrap(),
                )
                .to_value();
                assert_eq!(&decision, fixture.get("expected").unwrap());
                decision
            })
            .collect::<Vec<_>>()
    };
    let baseline = decisions();
    let lab = Lab::new();
    let store = lab.open();
    let edit = create("draft", "draft", DENY, false);
    assert!(call(&store, AUTHOR, &edit).get("result").is_some());
    assert_eq!(decisions(), baseline);
    fs::rename(&lab.path, lab.directory.join("unavailable")).unwrap();
    assert_eq!(error(&call(&store, AUTHOR, &edit)), "UNAVAILABLE");
    assert_eq!(decisions(), baseline);
}

#[test]
fn fixture_cli_and_api_return_identical_results_including_refusals() {
    let lab = Lab::new();
    let store = lab.open();
    let token = lab.directory.join("credential");
    fs::write(&token, AUTHOR).unwrap();
    let invoke = |request: &[u8]| {
        let mut process = Command::new(env!("CARGO_BIN_EXE_writ-manage-fixture"))
            .arg("v1")
            .arg(&lab.path)
            .arg(&token)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        let _ = process.stdin.take().unwrap().write_all(request);
        let output = process.wait_with_output().unwrap();
        let response = json::parse(&output.stdout).unwrap();
        assert_eq!(
            output.status.success(),
            response.get("ok") == Some(&Value::Bool(true))
        );
        response
    };
    let creation = body(&create("policy", "create", PERMIT, false));
    for input in [
        creation.as_bytes(),
        body(&history("policy", 0, 50)).as_bytes(),
        b"malformed",
    ] {
        let cli = invoke(input);
        let api = execute_v1(&store, Some(&fixture::authority()), AUTHOR, input);
        assert_eq!(cli, json::parse(&api).unwrap());
    }

    let too_large = vec![b' '; MAX_REQUEST + 1];
    for credential in [b"invalid".as_slice(), &[b'a'; 513]] {
        fs::write(&token, credential).unwrap();
        let api = execute_v1(&store, Some(&fixture::authority()), credential, &too_large);
        assert_eq!(invoke(&too_large), json::parse(&api).unwrap());
    }
}
