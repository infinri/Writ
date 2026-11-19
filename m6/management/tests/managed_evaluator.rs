#![cfg(feature = "fixtures")]
#![allow(clippy::unwrap_used, clippy::panic)]

use std::{
    cell::Cell,
    fmt::Write as _,
    fs,
    io::Write as _,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::{SystemTime, UNIX_EPOCH},
};

use ed25519_dalek::{Signer as _, SigningKey};
use rusqlite::Connection;
use writ_kernel::json::{self, Value};
use writ_ledger_sqlite::SqliteLedger;
use writ_policy_management_lab::{
    evaluator_fixture::{self, CEREMONY_DOMAIN, EVALUATOR, FIXTURE_TIME, PUBLISHER},
    execute_v1_with_service,
    fixture::{self, PROJECT, TENANT},
    Authenticator, PinnedPrincipal, Principal, RequestV1, Service, Store,
};

const VIEWER: &[u8] = b"fixture-viewer";
const WRITER: &[u8] = b"fixture-publisher";
const SEED: [u8; 32] = [
    0x9d, 0x61, 0xb1, 0x9d, 0xef, 0xfd, 0x5a, 0x60, 0xba, 0x84, 0x4a, 0xf4, 0x92, 0xec, 0x2c, 0xc4,
    0x44, 0x49, 0xc5, 0x69, 0x7b, 0x32, 0x69, 0x19, 0x70, 0x3b, 0xac, 0x03, 0x1c, 0xae, 0x7f, 0x60,
];

struct Lab {
    dir: PathBuf,
    management: PathBuf,
    ledger: PathBuf,
}

impl Lab {
    fn new() -> Self {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let dir =
            std::env::temp_dir().join(format!("managed-evaluator-{}-{nonce}", std::process::id()));
        fs::create_dir(&dir).unwrap();
        let lab = Self {
            management: dir.join("management.db"),
            ledger: dir.join("ledger.db"),
            dir,
        };
        Store::create(&lab.management).unwrap();
        evaluator_fixture::initialize(&lab.ledger).unwrap();
        lab
    }

    fn call(&self, credential: &[u8], request: &Value) -> Value {
        self.call_with(&fixture::authority(), credential, request)
    }

    fn call_with(
        &self,
        authority: &dyn Authenticator,
        credential: &[u8],
        request: &Value,
    ) -> Value {
        let store = Store::open(&self.management).unwrap();
        let evaluator = evaluator_fixture::open(&self.ledger).unwrap();
        let service = Service::new(&store, Some(authority)).with_evaluator(&evaluator);
        json::parse(&execute_v1_with_service(
            &service,
            credential,
            json::canonical(request).unwrap().as_bytes(),
        ))
        .unwrap()
    }

    fn cli(&self, credential: &[u8], request: &Value) -> Value {
        let credential_path = self.dir.join("management-credential");
        fs::write(&credential_path, credential).unwrap();
        let mut child = Command::new(env!("CARGO_BIN_EXE_writ-manage-fixture"))
            .arg("v1-evaluator")
            .arg(&self.management)
            .arg(&credential_path)
            .arg(&self.ledger)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        child
            .stdin
            .take()
            .unwrap()
            .write_all(json::canonical(request).unwrap().as_bytes())
            .unwrap();
        let output = child.wait_with_output().unwrap();
        let response = json::parse(&output.stdout).unwrap();
        assert_eq!(
            response.get("ok").and_then(Value::as_bool),
            Some(output.status.success())
        );
        response
    }

    fn owner_epoch(&self) -> String {
        Connection::open(&self.ledger)
            .unwrap()
            .query_row(
                "SELECT value FROM meta WHERE key = 'owner_epoch'",
                [],
                |row| row.get(0),
            )
            .unwrap()
    }

    fn history(&self) -> Value {
        ok(&self.call(VIEWER, &inspect(0, 10))).clone()
    }

    fn count(&self, database: &Path, sql: &str) -> i64 {
        Connection::open(database)
            .unwrap()
            .query_row(sql, [], |row| row.get(0))
            .unwrap()
    }
}

impl Drop for Lab {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.dir);
    }
}

fn request(operation: &str, fields: impl IntoIterator<Item = (&'static str, Value)>) -> Value {
    let mut request = json::obj([
        ("version", json::s("/manage/v1")),
        (
            "scope",
            json::obj([("tenant", json::s(TENANT)), ("project", json::s(PROJECT))]),
        ),
        ("operation", json::s(operation)),
    ]);
    for (name, value) in fields {
        request = set(request, name, value);
    }
    request
}

fn set(mut document: Value, name: &str, value: Value) -> Value {
    let Value::Obj(fields) = &mut document else {
        panic!("fixture must be an object")
    };
    fields.insert(name.into(), value);
    document
}

fn inspect(after: i64, limit: i64) -> Value {
    request(
        "evaluator_inspect",
        [
            ("evaluator_id", json::s(EVALUATOR)),
            ("after", Value::Int(after)),
            ("limit", Value::Int(limit)),
        ],
    )
}

fn candidate(expected: i64) -> Value {
    let sample = json::parse(include_bytes!(
        "../../../docs/contracts/proposed/examples/evaluator-registry-entry.valid.json"
    ))
    .unwrap();
    let mut sample = sample.without("command");
    for (name, value) in [
        (
            "scope",
            json::obj([
                ("tenant_id", json::s(TENANT)),
                ("project_id", json::s(PROJECT)),
            ]),
        ),
        ("evaluator_id", json::s(EVALUATOR)),
        ("class", json::s("evidence")),
        ("trust", json::s("E")),
        ("publisher", json::s(PUBLISHER)),
        ("published_at", json::s(FIXTURE_TIME)),
        ("registry_revision", Value::Int(expected + 1)),
    ] {
        sample = set(sample, name, value);
    }
    sample
}

fn preview_publish(expected: i64, entry: Value) -> Value {
    request(
        "evaluator_preview_publish",
        [
            ("expected_revision", Value::Int(expected)),
            ("entry", entry),
        ],
    )
}

fn preview_revoke(expected: i64) -> Value {
    request(
        "evaluator_preview_revoke",
        [
            ("expected_revision", Value::Int(expected)),
            ("evaluator_id", json::s(EVALUATOR)),
        ],
    )
}

fn commit(preview: Value, key: &str, digest: &str, presentation: &str) -> Value {
    let operation = match preview.get("operation").unwrap().as_str().unwrap() {
        "evaluator_preview_publish" => "evaluator_publish",
        "evaluator_preview_revoke" => "evaluator_revoke",
        _ => panic!("unexpected preview"),
    };
    let mut commit = set(preview, "operation", json::s(operation));
    for (name, value) in [
        ("idempotency_key", json::s(key)),
        ("intent_digest", json::s(digest)),
        ("presentation", json::s(presentation)),
    ] {
        commit = set(commit, name, value);
    }
    commit
}

fn presentation(digest: &str, ceremony: &str, at: &str, domain: &str) -> String {
    let claim = format!("{PUBLISHER}|{ceremony}|{at}|{digest}");
    let signed = format!("{domain}{claim}");
    let signature = SigningKey::from_bytes(&SEED).sign(signed.as_bytes());
    let mut result = format!("{claim}|");
    for byte in signature.to_bytes() {
        write!(result, "{byte:02x}").unwrap();
    }
    result
}

fn ok(response: &Value) -> &Value {
    assert_eq!(response.get("ok"), Some(&Value::Bool(true)), "{response:?}");
    response.get("result").unwrap()
}

fn err(response: &Value, code: &str) {
    assert_eq!(
        response.get("ok"),
        Some(&Value::Bool(false)),
        "{response:?}"
    );
    assert_eq!(
        response.get("error").unwrap().get("code").unwrap().as_str(),
        Some(code),
        "{response:?}"
    );
}

fn digest(preview: &Value) -> &str {
    ok(preview).get("intent_digest").unwrap().as_str().unwrap()
}

#[test]
fn exact_authorization_and_history_survive_reopen() {
    let lab = Lab::new();
    assert!(SqliteLedger::open(&lab.ledger).is_err());
    let ordinary = lab.dir.join("ordinary.db");
    drop(SqliteLedger::create(&ordinary).unwrap());
    assert!(SqliteLedger::open_evaluator_lab(&ordinary).is_err());
    let epoch = lab.owner_epoch();
    let preview = preview_publish(0, candidate(0));
    let ready = lab.call(WRITER, &preview);
    let fingerprint = digest(&ready);
    {
        let store = Store::open(&lab.management).unwrap();
        let evaluator = evaluator_fixture::open(&lab.ledger).unwrap();
        let authority = fixture::authority();
        let service = Service::new(&store, Some(&authority)).with_evaluator(&evaluator);
        let request = RequestV1::parse(json::canonical(&preview).unwrap().as_bytes()).unwrap();
        assert_eq!(
            json::parse(&service.execute(WRITER, request).unwrap().encode()).unwrap(),
            ready
        );
    }
    assert_eq!(lab.cli(WRITER, &preview), ready);
    assert_eq!(lab.owner_epoch(), epoch);
    assert_eq!(
        lab.count(
            &lab.management,
            "SELECT count(*) FROM sqlite_schema WHERE name = 'lifecycle_intents'"
        ),
        0
    );
    let baseline = lab.history();
    assert_eq!(baseline.get("revision"), Some(&Value::Int(0)));

    for (credential, code) in [
        (VIEWER, "NOT_FOUND"),
        (b"fixture-service", "NOT_FOUND"),
        (b"fixture-agent", "UNAUTHORIZED"),
        (b"fixture-adapter", "UNAUTHORIZED"),
    ] {
        err(&lab.call(credential, &preview), code);
    }
    let outside = set(
        preview.clone(),
        "scope",
        json::obj([("tenant", json::s(TENANT)), ("project", json::s("outside"))]),
    );
    err(&lab.call(WRITER, &outside), "NOT_FOUND");
    let outside_entry = set(
        candidate(0),
        "scope",
        json::obj([
            ("tenant_id", json::s(TENANT)),
            ("project_id", json::s("outside")),
        ]),
    );
    err(
        &lab.call(WRITER, &preview_publish(0, outside_entry)),
        "NOT_FOUND",
    );
    let invalid_impl = set(
        candidate(0),
        "implementation_digest",
        json::s(&format!("sha256:{}", "3".repeat(64))),
    );
    err(
        &lab.call(WRITER, &preview_publish(0, invalid_impl)),
        "INVALID_POLICY",
    );

    let good = presentation(fingerprint, "same-ceremony", FIXTURE_TIME, CEREMONY_DOMAIN);
    let wrong_purpose = presentation(
        fingerprint,
        "same-ceremony",
        FIXTURE_TIME,
        fixture::CEREMONY_DOMAIN,
    );
    let expired = presentation(
        fingerprint,
        "same-ceremony",
        "2026-11-01T08:58:00Z",
        CEREMONY_DOMAIN,
    );
    for (key, proof) in [("wrong-purpose", &wrong_purpose), ("expired", &expired)] {
        err(
            &lab.call(WRITER, &commit(preview.clone(), key, fingerprint, proof)),
            "UNAUTHORIZED",
        );
    }
    let changed_entry = set(
        candidate(0),
        "criteria",
        Value::Arr(vec![json::s("other-criterion")]),
    );
    let changed = preview_publish(0, changed_entry);
    let changed_ready = lab.call(WRITER, &changed);
    err(
        &lab.call(
            WRITER,
            &commit(changed.clone(), "old-binding", fingerprint, &good),
        ),
        "REVISION_CONFLICT",
    );
    err(
        &lab.call(
            WRITER,
            &commit(changed, "new-digest", digest(&changed_ready), &good),
        ),
        "UNAUTHORIZED",
    );
    assert_eq!(lab.history(), baseline);
    assert_eq!(lab.count(&lab.ledger, "SELECT count(*) FROM workflows"), 0);
    assert_eq!(
        lab.count(&lab.ledger, "SELECT count(*) FROM consumed_records"),
        0
    );
    assert_eq!(
        lab.count(&lab.ledger, "SELECT count(*) FROM consumed_attestations"),
        0
    );

    let publish = commit(preview.clone(), "publish-1", fingerprint, &good);
    let published = lab.call(WRITER, &publish);
    assert_eq!(
        ok(&published).get("status").and_then(Value::as_str),
        Some("active")
    );
    assert_eq!(lab.call(WRITER, &publish), published);
    let another = presentation(
        fingerprint,
        "another-ceremony",
        FIXTURE_TIME,
        CEREMONY_DOMAIN,
    );
    err(
        &lab.call(
            WRITER,
            &commit(preview.clone(), "publish-1", fingerprint, &another),
        ),
        "IDEMPOTENCY_CONFLICT",
    );
    err(
        &lab.call(
            WRITER,
            &commit(preview, "stale-revision", fingerprint, &another),
        ),
        "REVISION_CONFLICT",
    );

    let revoke_preview = preview_revoke(1);
    let revoke_ready = lab.call(WRITER, &revoke_preview);
    let revoke_digest = digest(&revoke_ready);
    let repeated = presentation(
        revoke_digest,
        "same-ceremony",
        FIXTURE_TIME,
        CEREMONY_DOMAIN,
    );
    err(
        &lab.call(
            WRITER,
            &commit(
                revoke_preview.clone(),
                "reused-ceremony",
                revoke_digest,
                &repeated,
            ),
        ),
        "UNAUTHORIZED",
    );
    let unused = presentation(
        revoke_digest,
        "another-ceremony",
        FIXTURE_TIME,
        CEREMONY_DOMAIN,
    );
    let revoked = lab.call(
        WRITER,
        &commit(revoke_preview, "revoke-1", revoke_digest, &unused),
    );
    assert_eq!(
        ok(&revoked).get("status").and_then(Value::as_str),
        Some("revoked")
    );

    let recover = request(
        "evaluator_recover",
        [("idempotency_key", json::s("publish-1"))],
    );
    let epoch = lab.owner_epoch();
    assert_eq!(lab.call(WRITER, &recover), published);
    assert_eq!(lab.cli(WRITER, &recover), published);
    assert_eq!(lab.owner_epoch(), epoch);
    let first = lab.call(VIEWER, &inspect(0, 1));
    assert_eq!(
        ok(&first).get("history"),
        Some(&Value::Arr(vec![ok(&published).clone()]))
    );
    assert_eq!(ok(&first).get("next"), Some(&Value::Int(1)));
    let second = lab.call(VIEWER, &inspect(1, 1));
    assert_eq!(
        ok(&second).get("history"),
        Some(&Value::Arr(vec![ok(&revoked).clone()]))
    );
    assert_eq!(ok(&second).get("next"), Some(&Value::Null));
}

struct ExpiresAtOwner<A> {
    authority: A,
    seen: Cell<usize>,
}

struct ExpiringPin<'a> {
    pinned: Box<dyn PinnedPrincipal + 'a>,
    seen: &'a Cell<usize>,
}

impl PinnedPrincipal for ExpiringPin<'_> {
    fn principal(&self) -> &Principal {
        self.pinned.principal()
    }

    fn still_valid(&self) -> bool {
        let call = self.seen.get() + 1;
        self.seen.set(call);
        call <= 3 && self.pinned.still_valid()
    }
}

impl<A: Authenticator> Authenticator for ExpiresAtOwner<A> {
    fn authenticate(&self, credential: &[u8]) -> Option<Principal> {
        self.authority.authenticate(credential)
    }

    fn pin(&self, credential: &[u8]) -> Option<Box<dyn PinnedPrincipal + '_>> {
        Some(Box::new(ExpiringPin {
            pinned: self.authority.pin(credential)?,
            seen: &self.seen,
        }))
    }
}

#[test]
fn failed_storage_and_commit_reauthorization_leave_no_evidence() {
    let lab = Lab::new();
    let preview = preview_publish(0, candidate(0));
    let ready = lab.call(WRITER, &preview);
    let fingerprint = digest(&ready);
    let proof = presentation(
        fingerprint,
        "unconsumed-ceremony",
        FIXTURE_TIME,
        CEREMONY_DOMAIN,
    );
    let expired = ExpiresAtOwner {
        authority: fixture::authority(),
        seen: Cell::new(0),
    };
    err(
        &lab.call_with(
            &expired,
            WRITER,
            &commit(preview.clone(), "pending-attempt", fingerprint, &proof),
        ),
        "UNAUTHORIZED",
    );
    assert_eq!(expired.seen.get(), 4);
    let recover = request(
        "evaluator_recover",
        [("idempotency_key", json::s("pending-attempt"))],
    );
    err(&lab.call(WRITER, &recover), "UNAVAILABLE");
    err(
        &lab.call(
            WRITER,
            &commit(preview.clone(), "pending-attempt", fingerprint, &proof),
        ),
        "UNAVAILABLE",
    );
    assert_eq!(lab.history().get("revision"), Some(&Value::Int(0)));

    let management = Connection::open(&lab.management).unwrap();
    management.execute_batch("BEGIN IMMEDIATE").unwrap();
    let publish = commit(preview, "fresh-attempt", fingerprint, &proof);
    err(&lab.call(WRITER, &publish), "UNAVAILABLE");
    management.execute_batch("ROLLBACK").unwrap();
    assert_eq!(
        lab.count(&lab.ledger, "SELECT count(*) FROM admin_events"),
        0
    );
    assert_eq!(
        lab.count(
            &lab.ledger,
            "SELECT count(*) FROM ledger_sequence WHERE kind = 'administration'"
        ),
        0
    );
    assert_eq!(
        ok(&lab.call(WRITER, &publish)).get("revision"),
        Some(&Value::Int(1))
    );
}
