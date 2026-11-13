//! M3-05 transition service tests on the SQLite ledger (D-036): authenticated records by
//! id, promotion without client permits, atomic state and evidence consumption, replay,
//! changed bindings, stale authorization, storage faults with unknown commit outcomes,
//! concurrent promotion, authenticated administration and effect prerequisites held
//! against concurrent workflow changes. Test keys, sources and authorities only.

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Barrier, Mutex};
use std::thread;
use std::time::Duration;

use writ_broker::acceptance::{
    mac_bytes, AcceptanceCode, MAC_ALG, RECORD_CONTRACT, RECORD_PURPOSE,
};
use writ_broker::effects::{EffectAdapter, EffectEvidence, EffectResult, Verify};
use writ_broker::execute::{Broker, Executed};
use writ_broker::keys::{AcceptanceRecordKey, MemoryKeys, PermitKey, RunnerAttestationKey};
use writ_broker::policy::{ActivePolicy, PolicyState};
use writ_broker::refusal::Refusal;
use writ_broker::store::TxFailure;
use writ_broker::workflow::{
    AcceptanceRecordSource, AdminOperation, AdminPresentation, AdminRefusal, AdminRequest,
    AttestationSource, AuthorizedOperator, PromotionOutcome, SnapshotCheck, SnapshotStore,
    SourceUnavailable, Trust, WorkflowAuthority, WorkflowKey,
};
use writ_broker::writ_kernel::json::{self, Digest, Value};
use writ_broker::writ_kernel::policy::{load_bundle, LoadedBundle};
use writ_broker::writ_kernel::time::Timestamp;

use super::grant_tests::{set, verify, TestClock};
use super::{fault, SqliteEffectStore, SqliteLedger};

const T: &str = "fixture-tenant";
const P: &str = "fixture-project";
const RECORD_KEY: [u8; 32] = [0x5a; 32];
const RUNNER_KEY: [u8; 32] = [0x6b; 32];

fn sha(c: char) -> String {
    format!("sha256:{}", c.to_string().repeat(64))
}

fn ts(s: &str) -> Timestamp {
    Timestamp::parse_strict(s).unwrap()
}

fn now() -> Timestamp {
    ts("2026-11-07T11:00:00Z")
}

fn parse(text: &str) -> Value {
    json::parse(text.as_bytes()).unwrap()
}

// ---------------------------------------------------------------------------
// HMAC-SHA256 from the kernel's SHA-256 (no new dependency), for signing test documents.

fn unhex(text: &str) -> Vec<u8> {
    (0..text.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&text[i..i + 2], 16).unwrap())
        .collect()
}

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

fn sign(doc: &Value, key: &[u8]) -> Value {
    let tag = hmac_hex(key, mac_bytes(doc).unwrap().as_bytes());
    set(doc, &["auth", "mac"], json::s(&tag))
}

// ---------------------------------------------------------------------------
// Trusted documents.

fn requirements(version: &str) -> Value {
    parse(&format!(
        r#"{{"acceptance_policy_id":"accept-code-review","acceptance_policy_version":"{version}","acceptance_policy_bundle_sha":"{}","criteria":[{{"criterion_id":"build","required_class":"mechanical"}},{{"criterion_id":"docs","required_class":"evidence"}}]}}"#,
        sha('c')
    ))
}

/// The authenticated record `id` for verifying to accepted at `revision` on `snapshot`,
/// requiring attestation `att`.
fn record(id: &str, revision: i64, digest: &str, att: &str) -> Value {
    let mut r = parse(&format!(
        r#"{{"contract_version":"m1-draft-3","acceptance_record_id":"{id}","tenant_id":"{T}","project_id":"{P}","task_id":"task-1","workflow_id":"wf-1","transition":{{"from_state":"verifying","to_state":"accepted"}},"artifact_snapshot_id":"snap-1","artifact_digest":"{digest}","acceptance_policy_id":"accept-code-review","acceptance_policy_version":"3","acceptance_policy_bundle_sha":"{}","state_revision":{revision},"status":"accepted","issued_at":"2026-11-07T10:00:00Z","expires_at":"2026-11-07T12:00:00Z","criteria":[{{"criterion_id":"build","mandatory":true,"required_class":"mechanical","status":"pass","evaluator_id":"ci-runner","evaluator_trust":"M","fresh_until":"2026-11-07T11:30:00Z"}},{{"criterion_id":"docs","mandatory":true,"required_class":"evidence","status":"pass","evaluator_id":"doc-check","evaluator_trust":"E","fresh_until":"2026-11-07T11:30:00Z"}}],"record_digest":"{}"}}"#,
        sha('c'),
        sha('0')
    ));
    let d = json::digest(&r.without("record_digest")).unwrap();
    r = set(&r, &["record_digest"], json::s(d.as_str()));
    let eval = |criterion: &str, evaluator: &str, attestation: Value| {
        json::obj([
            ("attestation_id", attestation),
            ("criterion_id", json::s(criterion)),
            ("environment_digest", json::s(&sha('2'))),
            ("evaluation_id", json::s(&format!("eval-{criterion}"))),
            ("evaluator_id", json::s(evaluator)),
            ("evaluator_version", json::s("1.4.0")),
            ("evidence_digests", Value::Arr(vec![])),
            ("implementation_digest", json::s(&sha('1'))),
            ("registry_revision", Value::Int(12)),
        ])
    };
    let doc = json::obj([
        (
            "auth",
            json::obj([
                ("alg", json::s(MAC_ALG)),
                ("key_id", json::s("acc-key-1")),
                ("mac", json::s(&"0".repeat(64))),
            ]),
        ),
        ("contract_version", json::s(RECORD_CONTRACT)),
        (
            "evaluations",
            Value::Arr(vec![
                eval("build", "ci-runner", json::s(att)),
                eval("docs", "doc-check", Value::Null),
            ]),
        ),
        ("issuer", json::s("acceptance-service")),
        ("mode", json::s("governed")),
        ("purpose", json::s(RECORD_PURPOSE)),
        ("record", r),
    ]);
    sign(&doc, &RECORD_KEY)
}

fn attestation(id: &str, record_id: &str, revision: i64) -> Value {
    let mut a = parse(include_str!(
        "../../../docs/contracts/proposed/examples/runner-attestation.valid.json"
    ));
    for (k, v) in [
        ("attestation_id", json::s(id)),
        ("tenant_id", json::s(T)),
        ("project_id", json::s(P)),
        (
            "canonical_target",
            json::s(&format!("workflow:{T}/{P}/wf-1")),
        ),
        ("acceptance_record_id", json::s(record_id)),
        ("state_revision", Value::Int(revision)),
    ] {
        a = set(&a, &[k], v);
    }
    sign(&a, &RUNNER_KEY)
}

fn registry_entry(evaluator: &str, revision: i64) -> Value {
    let mut v = parse(include_str!(
        "../../../docs/contracts/proposed/examples/evaluator-registry-entry.valid.json"
    ));
    v = set(&v, &["scope", "tenant_id"], json::s(T));
    v = set(&v, &["scope", "project_id"], json::s(P));
    v = set(&v, &["registry_revision"], Value::Int(revision));
    if evaluator == "doc-check" {
        v = set(&v, &["evaluator_id"], json::s("doc-check"));
        v = set(&v, &["class"], json::s("evidence"));
        v = set(&v, &["trust"], json::s("E"));
        v = set(&v, &["criteria"], Value::Arr(vec![json::s("docs")]));
        if let Value::Obj(m) = &mut v {
            m.remove("command");
        }
    }
    v
}

fn bundle() -> LoadedBundle {
    let mut b = parse(include_str!(
        "../../../m1/fixtures/transition/m1-transition.bundle.json"
    ));
    let mut statements = b.get("statements").unwrap().as_arr().unwrap().to_vec();
    statements.push(parse(
        r#"{"statement_id":"permit-publish","effect":"permit","action_kinds":["repository.publish"],"resource_prefixes":["repository:fixture-tenant/fixture-project/"],"reason_code":"PUBLISH_PERMITTED"}"#,
    ));
    b = set(&b, &["statements"], Value::Arr(statements));
    load_bundle(&b).unwrap()
}

/// The same bundle compatible with another acceptance-policy bundle.
fn bundle_other_dependency() -> LoadedBundle {
    let mut b = parse(include_str!(
        "../../../m1/fixtures/transition/m1-transition.bundle.json"
    ));
    b = set(
        &b,
        &["workflow_protection", "acceptance_policy_bundle_sha"],
        json::s(&sha('d')),
    );
    b = set(&b, &["version"], json::s("0.4.1"));
    load_bundle(&b).unwrap()
}

// ---------------------------------------------------------------------------
// Test sources, snapshot store and authority.

#[derive(Default)]
struct Docs {
    docs: Mutex<BTreeMap<String, Value>>,
    down: AtomicBool,
}

impl Docs {
    fn put(&self, id: &str, doc: Value) {
        self.docs.lock().unwrap().insert(id.to_string(), doc);
    }

    fn get(&self, id: &str) -> Result<Option<Value>, SourceUnavailable> {
        if self.down.load(Ordering::SeqCst) {
            return Err(SourceUnavailable);
        }
        Ok(self.docs.lock().unwrap().get(id).cloned())
    }
}

impl AcceptanceRecordSource for Docs {
    fn record(&self, t: &str, p: &str, id: &str) -> Result<Option<Value>, SourceUnavailable> {
        assert_eq!((t, p), (T, P));
        self.get(id)
    }
}

impl AttestationSource for Docs {
    fn attestation(&self, _: &str, _: &str, id: &str) -> Result<Option<Value>, SourceUnavailable> {
        self.get(id)
    }
}

#[derive(Default)]
struct Snapshots {
    content: Mutex<BTreeMap<String, String>>,
    down: AtomicBool,
}

impl SnapshotStore for Snapshots {
    fn check(
        &self,
        _: &str,
        _: &str,
        id: &str,
        digest: &str,
    ) -> Result<SnapshotCheck, SourceUnavailable> {
        if self.down.load(Ordering::SeqCst) {
            return Err(SourceUnavailable);
        }
        Ok(match self.content.lock().unwrap().get(id) {
            None => SnapshotCheck::Unknown,
            Some(d) if d == digest => SnapshotCheck::Matches,
            Some(_) => SnapshotCheck::Differs,
        })
    }
}

/// `ops:alice` on `ops-console` may administer the fixture project; `ops:bob` only another.
struct Operators;

impl WorkflowAuthority for Operators {
    fn authorize(
        &self,
        p: &AdminPresentation<'_>,
        _: AdminOperation,
        tenant: &str,
        project: &str,
    ) -> Result<AuthorizedOperator, AdminRefusal> {
        let scope = match (p.channel, p.operator, p.credential) {
            ("ops-console", "ops:alice", b"alice-cred") => P,
            ("ops-console", "ops:bob", b"bob-cred") => "other-project",
            _ => return Err(AdminRefusal::Unauthenticated),
        };
        if tenant != T || project != scope {
            return Err(AdminRefusal::Unauthorized);
        }
        Ok(AuthorizedOperator {
            id: p.operator.into(),
            issuer: "realm:ops".into(),
            channel: p.channel.into(),
            evidence_ref: format!("authn-event:{}", p.operator),
        })
    }
}

fn alice() -> AdminPresentation<'static> {
    AdminPresentation {
        channel: "ops-console",
        operator: "ops:alice",
        credential: b"alice-cred",
    }
}

fn wf() -> WorkflowKey {
    WorkflowKey {
        tenant_id: T.into(),
        project_id: P.into(),
        workflow_id: "wf-1".into(),
    }
}

// ---------------------------------------------------------------------------
// The world.

struct W {
    dir: PathBuf,
    ledger: SqliteLedger,
    effects: SqliteEffectStore,
    active: ActivePolicy,
    clock: TestClock,
    key: PermitKey,
    docs: Docs,
    snapshots: Snapshots,
    record_keys: MemoryKeys<AcceptanceRecordKey>,
    runner_keys: MemoryKeys<RunnerAttestationKey>,
}

impl W {
    fn new(name: &str) -> W {
        let dir = std::env::temp_dir().join(format!("writ-m305-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        W::at(dir, false)
    }

    /// The world over `dir`: new stores, or (`open`) the existing ones.
    fn at(dir: PathBuf, open: bool) -> W {
        let (ledger, effects) = if open {
            let ledger = SqliteLedger::open(&dir.join("ledger.db")).unwrap();
            let effects =
                SqliteEffectStore::open(&dir.join("effects.db"), ledger.ledger_id()).unwrap();
            (ledger, effects)
        } else {
            let ledger = SqliteLedger::create(&dir.join("ledger.db")).unwrap();
            let effects =
                SqliteEffectStore::create(&dir.join("effects.db"), ledger.ledger_id()).unwrap();
            (ledger, effects)
        };
        let mut record_keys = MemoryKeys::new();
        record_keys.register(
            "acc-key-1",
            AcceptanceRecordKey::from_bytes(RECORD_KEY.to_vec()).unwrap(),
        );
        let mut runner_keys = MemoryKeys::new();
        runner_keys.register(
            "runner-key-1",
            RunnerAttestationKey::from_bytes(RUNNER_KEY.to_vec()).unwrap(),
        );
        let w = W {
            dir,
            ledger,
            effects,
            active: ActivePolicy::new(PolicyState::loaded(bundle())),
            clock: TestClock::at(now()),
            key: PermitKey::from_bytes(vec![0x44; 32]).unwrap(),
            docs: Docs::default(),
            snapshots: Snapshots::default(),
            record_keys,
            runner_keys,
        };
        w.snapshots
            .content
            .lock()
            .unwrap()
            .insert("snap-1".into(), sha('b'));
        w.snapshots
            .content
            .lock()
            .unwrap()
            .insert("snap-2".into(), sha('e'));
        w
    }

    fn broker(&self) -> Broker<'_, SqliteLedger, SqliteEffectStore> {
        Broker::new(
            &self.active,
            &self.clock,
            &self.ledger,
            &self.effects,
            &self.key,
        )
    }

    fn trust(&self) -> Trust<'_> {
        Trust {
            records: &self.docs,
            attestations: &self.docs,
            record_keys: &self.record_keys,
            runner_keys: &self.runner_keys,
            snapshots: &self.snapshots,
        }
    }

    fn admin(&self, request: AdminRequest) -> Result<(), Refusal> {
        self.broker()
            .administer(&Operators, &alice(), &self.snapshots, request)
    }

    /// Workflow wf-1 in `planning`, policy and registry set up, then promoted (unprotected)
    /// to `verifying` at revision 1; record acc-1 and attestation att-1 published.
    fn ready(name: &str) -> W {
        W::new(name).prepared()
    }

    fn prepared(self) -> W {
        let w = self;
        w.admin(AdminRequest::CreateWorkflow {
            key: wf(),
            task_id: "task-1".into(),
            initial_state: "planning".into(),
            artifact_snapshot_id: "snap-1".into(),
            artifact_digest: sha('b'),
        })
        .unwrap();
        w.admin(AdminRequest::ActivateAcceptancePolicy {
            tenant_id: T.into(),
            project_id: P.into(),
            requirements: requirements("3"),
            effect_prerequisites: vec![("repository.publish".into(), vec!["accepted".into()])],
        })
        .unwrap();
        for e in ["ci-runner", "doc-check"] {
            w.admin(AdminRequest::RegisterEvaluator {
                entry: registry_entry(e, 12),
            })
            .unwrap();
        }
        assert_eq!(
            w.promote(&transition("k0", "planning", "verifying", 0, None)),
            Ok(committed(1))
        );
        w.publish_evidence();
        w
    }

    fn publish_evidence(&self) {
        self.docs
            .put("acc-1", record("acc-1", 1, &sha('b'), "att-1"));
        self.docs.put("att-1", attestation("att-1", "acc-1", 1));
    }

    fn promote(&self, request: &Value) -> Result<PromotionOutcome, Refusal> {
        self.broker()
            .promote(&self.trust(), &verify(request), None)
            .result
    }

    fn one(&self, sql: &str) -> i64 {
        self.ledger
            .db()
            .conn
            .query_row(sql, [], |r| r.get(0))
            .unwrap()
    }

    fn text(&self, sql: &str) -> String {
        self.ledger
            .db()
            .conn
            .query_row(sql, [], |r| r.get(0))
            .unwrap()
    }

    fn state(&self) -> (String, i64) {
        self.ledger
            .db()
            .conn
            .query_row(
                "SELECT state, revision FROM workflows WHERE workflow_id = 'wf-1'",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap()
    }
}

fn committed(revision: i64) -> PromotionOutcome {
    PromotionOutcome::Committed {
        revision,
        receipt_id: format!("promotion:wf-1:{revision}"),
    }
}

/// A sealed transition request with idempotency key `key`.
fn transition(
    key: &str,
    from: &str,
    to: &str,
    expected: i64,
    record: Option<(&str, &str, &str)>,
) -> Value {
    let mut attrs = json::obj([("from_state", json::s(from)), ("to_state", json::s(to))]);
    if let Some((id, snapshot, digest)) = record {
        attrs = set(&attrs, &["acceptance_record_id"], json::s(id));
        attrs = set(&attrs, &["artifact_snapshot_id"], json::s(snapshot));
        attrs = set(&attrs, &["artifact_digest"], json::s(digest));
    }
    let base = parse(&format!(
        r#"{{"contract_version":"2.0","request_id":"req-{key}","trace_id":"trace-1","tenant_id":"{T}","principal":{{"id":"agent:session-1","verified_by":"writ-host-adapter:fixture","authn_method":"os_peer_credential","roles":["agent"]}},"project_id":"{P}","session_id":"session-1","agent_id":null,"parent_session_id":null,"idempotency_key":"{key}","expected_state_revision":{expected},"policy_bundle_sha":null,"deadline_at":null,"action":{{"kind":"workflow.transition","resource":{{"uri":"writ-workflow://wf-1","canonical_resource_id":"workflow:{T}/{P}/wf-1"}},"action_digest":"{}","attributes":{{}},"requested_capabilities":[]}}}}"#,
        sha('b')
    ));
    set(&base, &["action", "attributes"], attrs)
}

fn accept(key: &str, expected: i64) -> Value {
    transition(
        key,
        "verifying",
        "accepted",
        expected,
        Some(("acc-1", "snap-1", &sha('b'))),
    )
}

fn acceptance(code: AcceptanceCode) -> Refusal {
    Refusal::Acceptance(code)
}

/// Nothing of a refused promotion is durable.
fn unchanged(w: &W) {
    assert_eq!(w.state(), ("verifying".into(), 1));
    assert_eq!(w.one("SELECT count(*) FROM consumed_records"), 0);
    assert_eq!(w.one("SELECT count(*) FROM consumed_attestations"), 0);
    assert_eq!(
        w.one("SELECT count(*) FROM promotion_receipts"),
        1,
        "only k0"
    );
}

// ---------------------------------------------------------------------------
// Promotion.

#[test]
fn a_protected_promotion_commits_state_and_consumes_its_evidence_atomically() {
    let w = W::ready("protected");
    assert_eq!(w.promote(&accept("k1", 1)), Ok(committed(2)));
    assert_eq!(w.state(), ("accepted".into(), 2));
    assert_eq!(
        w.text("SELECT acceptance_record_id FROM consumed_records"),
        "acc-1"
    );
    assert_eq!(
        w.text("SELECT attestation_id || ' ' || acceptance_record_id FROM consumed_attestations"),
        "att-1 acc-1"
    );
    let last: (String, String, String, String) = w
        .ledger
        .db()
        .conn
        .query_row(
            "SELECT last_receipt_id, last_record_id, last_acceptance_sha, last_authorization_sha \
             FROM workflows",
            [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
        )
        .unwrap();
    assert_eq!(
        last,
        (
            "promotion:wf-1:2".into(),
            "acc-1".into(),
            sha('c'),
            bundle().sha.as_str().to_string()
        )
    );
    assert_eq!(
        w.text(
            "SELECT group_concat(cause, ',') FROM (SELECT cause FROM workflow_events ORDER BY seq)"
        ),
        "create,promote,promote"
    );
    // An identical retry replays the durable receipt; a changed request is a conflict.
    let replay = PromotionOutcome::Replayed {
        revision: 2,
        receipt_id: "promotion:wf-1:2".into(),
    };
    assert_eq!(w.promote(&accept("k1", 1)), Ok(replay));
    let changed = set(&accept("k1", 1), &["trace_id"], json::s("trace-2"));
    assert_eq!(w.promote(&changed), Err(Refusal::IdempotencyConflict));
    assert_eq!(w.state(), ("accepted".into(), 2));
}

#[test]
fn records_come_only_from_the_trusted_source_and_must_authenticate() {
    let w = W::ready("record-source");
    // An id the source does not know (an agent cannot supply the record inline).
    let unknown = transition(
        "k1",
        "verifying",
        "accepted",
        1,
        Some(("acc-9", "snap-1", &sha('b'))),
    );
    assert_eq!(
        w.promote(&unknown),
        Err(acceptance(AcceptanceCode::RecordUnknown))
    );
    w.docs.down.store(true, Ordering::SeqCst);
    assert_eq!(
        w.promote(&accept("k1", 1)),
        Err(acceptance(AcceptanceCode::SourceUnavailable))
    );
    w.docs.down.store(false, Ordering::SeqCst);
    // The request's binding must equal the record.
    let other = transition(
        "k1",
        "verifying",
        "accepted",
        1,
        Some(("acc-1", "snap-2", &sha('e'))),
    );
    assert_eq!(
        w.promote(&other),
        Err(acceptance(AcceptanceCode::BindingMismatch))
    );
    // Tampered, or authenticated by a revoked or unavailable key.
    let tampered = set(
        &record("acc-1", 1, &sha('b'), "att-1"),
        &["issuer"],
        json::s("agent"),
    );
    w.docs.put("acc-1", tampered);
    assert_eq!(
        w.promote(&accept("k1", 1)),
        Err(acceptance(AcceptanceCode::RecordUnauthenticated))
    );
    w.docs.put("acc-1", record("acc-1", 1, &sha('b'), "att-1"));
    let mut w = w;
    w.record_keys.revoke("acc-key-1");
    assert_eq!(
        w.promote(&accept("k1", 1)),
        Err(acceptance(AcceptanceCode::RecordUnauthenticated))
    );
    w.record_keys.register(
        "acc-key-1",
        AcceptanceRecordKey::from_bytes(RECORD_KEY.to_vec()).unwrap(),
    );
    w.record_keys.set_unavailable(true);
    assert_eq!(
        w.promote(&accept("k1", 1)),
        Err(acceptance(AcceptanceCode::KeyUnavailable))
    );
    w.record_keys.set_unavailable(false);
    // An advisory record never satisfies a protected transition.
    let advisory = sign(
        &set(
            &record("acc-1", 1, &sha('b'), "att-1"),
            &["mode"],
            json::s("advisory"),
        ),
        &RECORD_KEY,
    );
    w.docs.put("acc-1", advisory);
    assert_eq!(
        w.promote(&accept("k1", 1)),
        Err(acceptance(AcceptanceCode::Advisory))
    );
    // The attestation must come from its source and authenticate with the runner key.
    w.docs.put("acc-1", record("acc-1", 1, &sha('b'), "att-1"));
    w.docs.docs.lock().unwrap().remove("att-1");
    assert_eq!(
        w.promote(&accept("k1", 1)),
        Err(acceptance(AcceptanceCode::AttestationUnknown))
    );
    w.docs.put(
        "att-1",
        set(
            &attestation("att-1", "acc-1", 1),
            &["exit_status"],
            Value::Int(1),
        ),
    );
    assert_eq!(
        w.promote(&accept("k1", 1)),
        Err(acceptance(AcceptanceCode::AttestationUnauthenticated))
    );
    unchanged(&w);
    // A protected transition without the binding never reaches the store (kernel).
    let bare = transition("k1", "verifying", "accepted", 1, None);
    assert!(matches!(w.promote(&bare), Err(Refusal::Decision { .. })));
    unchanged(&w);
    assert!(w.one("SELECT count(*) FROM audit_refusals") >= 9);
}

#[test]
fn consumed_records_and_attestations_never_satisfy_another_promotion() {
    let w = W::ready("replay");
    assert_eq!(w.promote(&accept("k1", 1)), Ok(committed(2)));
    // Put the workflow back where it was (a direct test write, not an API) so that only
    // the ledger-wide single use can refuse.
    let reset = || {
        w.ledger
            .db()
            .conn
            .execute("UPDATE workflows SET state = 'verifying', revision = 1", [])
            .unwrap();
    };
    reset();
    assert_eq!(
        w.promote(&accept("k2", 1)),
        Err(acceptance(AcceptanceCode::Kernel(
            writ_broker::writ_kernel::acceptance::AcceptanceRefusal::Replayed
        )))
    );
    // Another record reusing the consumed attestation.
    w.docs.put("acc-2", record("acc-2", 1, &sha('b'), "att-1"));
    w.docs.put("att-1", attestation("att-1", "acc-2", 1));
    let other = transition(
        "k3",
        "verifying",
        "accepted",
        1,
        Some(("acc-2", "snap-1", &sha('b'))),
    );
    assert_eq!(
        w.promote(&other),
        Err(acceptance(AcceptanceCode::AttestationReplayed))
    );
    assert_eq!(w.one("SELECT count(*) FROM consumed_records"), 1);
    assert_eq!(w.one("SELECT count(*) FROM consumed_attestations"), 1);
}

#[test]
fn changed_bindings_and_stale_authorization_refuse_at_use_time() {
    // Artifact updated between acceptance and promotion: revision and artifact change.
    let w = W::ready("artifact");
    w.admin(AdminRequest::UpdateArtifact {
        key: wf(),
        artifact_snapshot_id: "snap-2".into(),
        artifact_digest: sha('e'),
    })
    .unwrap();
    assert_eq!(w.state(), ("verifying".into(), 2));
    assert!(
        matches!(w.promote(&accept("k1", 1)), Err(Refusal::Decision { .. })),
        "stale expected revision"
    );
    assert_eq!(
        w.promote(&accept("k1", 2)),
        Err(acceptance(AcceptanceCode::Kernel(
            writ_broker::writ_kernel::acceptance::AcceptanceRefusal::ArtifactMismatch
        )))
    );
    // Acceptance policy re-activated at another version.
    let w = W::ready("policy");
    w.admin(AdminRequest::ActivateAcceptancePolicy {
        tenant_id: T.into(),
        project_id: P.into(),
        requirements: requirements("4"),
        effect_prerequisites: vec![],
    })
    .unwrap();
    assert_eq!(
        w.promote(&accept("k1", 1)),
        Err(acceptance(AcceptanceCode::Kernel(
            writ_broker::writ_kernel::acceptance::AcceptanceRefusal::PolicyMismatch
        )))
    );
    unchanged(&w);
    // Authorization bundle whose protection names another acceptance-policy bundle.
    let w = W::ready("dependency");
    w.active
        .activate(PolicyState::loaded(bundle_other_dependency()));
    assert_eq!(
        w.promote(&accept("k1", 1)),
        Err(Refusal::PolicyDependencyMismatch)
    );
    unchanged(&w);
    // Evaluator revoked, or re-registered at a later registry revision.
    let w = W::ready("revoked");
    w.admin(AdminRequest::RevokeEvaluator {
        tenant_id: T.into(),
        project_id: P.into(),
        evaluator_id: "ci-runner".into(),
    })
    .unwrap();
    assert_eq!(
        w.promote(&accept("k1", 1)),
        Err(acceptance(AcceptanceCode::Kernel(
            writ_broker::writ_kernel::acceptance::AcceptanceRefusal::EvaluatorRevoked
        )))
    );
    let w = W::ready("reregistered");
    w.admin(AdminRequest::RegisterEvaluator {
        entry: registry_entry("doc-check", 13),
    })
    .unwrap();
    assert_eq!(
        w.promote(&accept("k1", 1)),
        Err(acceptance(AcceptanceCode::EvaluatorBindingMismatch))
    );
    unchanged(&w);
    // The immutable snapshot store must confirm the artifact (INV-40).
    let w = W::ready("snapshot");
    w.snapshots.down.store(true, Ordering::SeqCst);
    assert_eq!(
        w.promote(&accept("k1", 1)),
        Err(acceptance(AcceptanceCode::ArtifactIntegrityUnavailable))
    );
    w.snapshots.down.store(false, Ordering::SeqCst);
    w.snapshots
        .content
        .lock()
        .unwrap()
        .insert("snap-1".into(), sha('f'));
    assert_eq!(
        w.promote(&accept("k1", 1)),
        Err(acceptance(AcceptanceCode::ArtifactIntegrityViolation))
    );
    unchanged(&w);
    // Trusted time past the attestation's freshness.
    let w = W::ready("stale");
    w.clock.set(ts("2026-11-07T11:30:00Z"));
    assert!(w.promote(&accept("k1", 1)).is_err());
    unchanged(&w);
}

#[test]
fn approval_requirements_are_preserved_for_promotions() {
    let w = W::ready("approval");
    assert_eq!(w.promote(&accept("k1", 1)), Ok(committed(2)));
    // accepted to deployed requires approval under the bundle and is protected.
    let deploy = transition(
        "k2",
        "accepted",
        "deployed",
        2,
        Some(("acc-d", "snap-1", &sha('b'))),
    );
    assert!(matches!(w.promote(&deploy), Err(Refusal::ApprovalRequired)));
    assert_eq!(w.state(), ("accepted".into(), 2));
}

const STAGES: [(fault::Stage, std::ffi::c_int, &str); 3] = [
    (
        fault::Stage::Begin,
        rusqlite::ffi::SQLITE_BUSY,
        "SQLITE_BUSY",
    ),
    (
        fault::Stage::Body,
        rusqlite::ffi::SQLITE_IOERR,
        "SQLITE_IOERR",
    ),
    (
        fault::Stage::Commit,
        rusqlite::ffi::SQLITE_FULL,
        "SQLITE_FULL",
    ),
];

#[test]
fn storage_faults_fail_closed_and_an_unknown_commit_is_recovered_from_durable_state() {
    for (i, (stage, code, name)) in STAGES.into_iter().enumerate() {
        let w = W::ready(&format!("fault-{i}"));
        w.ledger.inject("promote", stage, code);
        let p = w
            .broker()
            .promote(&w.trust(), &verify(&accept("k1", 1)), None);
        assert_eq!(
            p.result,
            Err(Refusal::LedgerUnavailable(
                writ_broker::store::StoreError::Unavailable {
                    category: "transaction promote".into(),
                    code: name.into(),
                }
            )),
            "{stage:?}"
        );
        let failure: TxFailure = p.failure.unwrap();
        assert_eq!(
            failure.outcome_unknown,
            stage == fault::Stage::Commit,
            "{stage:?}"
        );
        unchanged(&w);
        assert!(w.ledger.db().conn.is_autocommit());
        // Recovery: the identical retry reads the durable state (here nothing committed)
        // and commits exactly once.
        assert_eq!(w.promote(&accept("k1", 1)), Ok(committed(2)), "{stage:?}");
        assert_eq!(
            w.promote(&accept("k1", 1)),
            Ok(PromotionOutcome::Replayed {
                revision: 2,
                receipt_id: "promotion:wf-1:2".into()
            })
        );
    }
}

#[test]
fn concurrent_promotions_of_one_transition_commit_exactly_once() {
    const WORKERS: usize = 32;
    for round in 0..3 {
        let w = W::ready(&format!("race-{round}"));
        let requests: Vec<_> = (0..WORKERS)
            .map(|i| verify(&accept(&format!("c{i}"), 1)))
            .collect();
        let start = Barrier::new(WORKERS);
        let results: Vec<_> = thread::scope(|s| {
            let handles: Vec<_> = requests
                .iter()
                .map(|r| {
                    let (w, start) = (&w, &start);
                    s.spawn(move || {
                        start.wait();
                        w.broker().promote(&w.trust(), r, None).result
                    })
                })
                .collect();
            handles.into_iter().map(|h| h.join().unwrap()).collect()
        });
        let ok = results.iter().filter(|r| **r == Ok(committed(2))).count();
        assert_eq!(ok, 1, "round {round}: {results:?}");
        assert!(results.iter().all(|r| r.is_ok() || r.is_err()));
        assert_eq!(w.state(), ("accepted".into(), 2));
        assert_eq!(w.one("SELECT count(*) FROM consumed_records"), 1);
        assert_eq!(w.one("SELECT count(*) FROM consumed_attestations"), 1);
        assert_eq!(
            w.one("SELECT count(*) FROM promotion_receipts"),
            2,
            "k0 and the winner"
        );
    }
}

#[test]
fn a_promotion_survives_restart_and_its_retry_replays() {
    let w = W::ready("restart");
    assert_eq!(w.promote(&accept("k1", 1)), Ok(committed(2)));
    let dir = w.dir.clone();
    let W {
        ledger,
        effects,
        active,
        clock,
        key,
        docs,
        snapshots,
        record_keys,
        runner_keys,
        ..
    } = w;
    drop(effects);
    drop(ledger);
    let ledger = SqliteLedger::open(&dir.join("ledger.db")).unwrap();
    let effects = SqliteEffectStore::open(&dir.join("effects.db"), ledger.ledger_id()).unwrap();
    let b = Broker::new(&active, &clock, &ledger, &effects, &key);
    let trust = Trust {
        records: &docs,
        attestations: &docs,
        record_keys: &record_keys,
        runner_keys: &runner_keys,
        snapshots: &snapshots,
    };
    assert_eq!(
        b.promote(&trust, &verify(&accept("k1", 1)), None).result,
        Ok(PromotionOutcome::Replayed {
            revision: 2,
            receipt_id: "promotion:wf-1:2".into()
        })
    );
    assert!(b
        .promote(&trust, &verify(&accept("k2", 2)), None)
        .result
        .is_err());
}

// ---------------------------------------------------------------------------
// Administration.

#[test]
fn administration_is_authenticated_scoped_audited_and_never_moves_state() {
    let w = W::new("admin");
    let create = |state: &str| AdminRequest::CreateWorkflow {
        key: wf(),
        task_id: "task-1".into(),
        initial_state: state.into(),
        artifact_snapshot_id: "snap-1".into(),
        artifact_digest: sha('b'),
    };
    let b = w.broker();
    let bob = AdminPresentation {
        channel: "ops-console",
        operator: "ops:bob",
        credential: b"bob-cred",
    };
    let forged = AdminPresentation {
        channel: "ops-console",
        operator: "ops:alice",
        credential: b"guess",
    };
    assert_eq!(
        b.administer(&Operators, &forged, &w.snapshots, create("planning")),
        Err(Refusal::OperatorUnauthenticated)
    );
    assert_eq!(
        b.administer(&Operators, &bob, &w.snapshots, create("planning")),
        Err(Refusal::OperatorUnauthorized)
    );
    // Never a protected state or the target of a protected edge.
    for protected in ["accepted", "deployed", "released", "verifying"] {
        assert_eq!(
            w.admin(create(protected)),
            Err(Refusal::WorkflowInitialStateProtected),
            "{protected}"
        );
    }
    // The artifact must be in the immutable snapshot store.
    let unknown = AdminRequest::CreateWorkflow {
        key: wf(),
        task_id: "task-1".into(),
        initial_state: "planning".into(),
        artifact_snapshot_id: "snap-9".into(),
        artifact_digest: sha('b'),
    };
    assert_eq!(
        w.admin(unknown),
        Err(acceptance(AcceptanceCode::ArtifactIntegrityViolation))
    );
    assert_eq!(w.one("SELECT count(*) FROM workflows"), 0);
    assert_eq!(w.one("SELECT count(*) FROM admin_events"), 0);
    w.admin(create("planning")).unwrap();
    assert_eq!(w.admin(create("planning")), Err(Refusal::WorkflowExists));
    w.admin(AdminRequest::UpdateArtifact {
        key: wf(),
        artifact_snapshot_id: "snap-2".into(),
        artifact_digest: sha('e'),
    })
    .unwrap();
    assert_eq!(
        w.state(),
        ("planning".into(), 1),
        "artifact updates bump the revision only"
    );
    // Registry revisions only increase.
    w.admin(AdminRequest::RegisterEvaluator {
        entry: registry_entry("ci-runner", 12),
    })
    .unwrap();
    assert_eq!(
        w.admin(AdminRequest::RegisterEvaluator {
            entry: registry_entry("ci-runner", 12)
        }),
        Err(Refusal::RegistryRevisionStale)
    );
    // Provenance of every successful change.
    let rows: Vec<(String, String, String, String, String)> = {
        let db = w.ledger.db();
        let mut s = db
            .conn
            .prepare("SELECT operation, operator_id, operator_issuer, channel, evidence_ref FROM admin_events ORDER BY seq")
            .unwrap();
        s.query_map([], |r| {
            Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?))
        })
        .unwrap()
        .collect::<rusqlite::Result<_>>()
        .unwrap()
    };
    let ops: Vec<&str> = rows.iter().map(|r| r.0.as_str()).collect();
    assert_eq!(
        ops,
        ["create_workflow", "update_artifact", "register_evaluator"]
    );
    assert!(rows.iter().all(|r| r.1 == "ops:alice"
        && r.2 == "realm:ops"
        && r.3 == "ops-console"
        && r.4 == "authn-event:ops:alice"));
    assert!(w.one("SELECT count(*) FROM audit_refusals WHERE code IN ('OPERATOR_UNAUTHENTICATED', 'OPERATOR_UNAUTHORIZED')") == 2);
}

// ---------------------------------------------------------------------------
// Effect prerequisites (D-019 N-3) held against concurrent workflow changes.

fn publish(key: &str, receipt: &str, snapshot: &str, digest: &str) -> Value {
    parse(&format!(
        r#"{{"contract_version":"2.0","request_id":"req-{key}","trace_id":"trace-1","tenant_id":"{T}","principal":{{"id":"agent:session-1","verified_by":"writ-host-adapter:fixture","authn_method":"os_peer_credential","roles":["agent"]}},"project_id":"{P}","session_id":"session-1","agent_id":null,"parent_session_id":null,"idempotency_key":"{key}","expected_state_revision":null,"policy_bundle_sha":null,"deadline_at":null,"action":{{"kind":"repository.publish","resource":{{"uri":"git://main","canonical_resource_id":"repository:{T}/{P}/main"}},"action_digest":"{}","attributes":{{"workflow_id":"wf-1","artifact_snapshot_id":"{snapshot}","artifact_digest":"{digest}","promotion_receipt_id":"{receipt}"}},"requested_capabilities":[]}}}}"#,
        sha('b')
    ))
}

fn exec(w: &W, req: &Value) -> Result<Executed, Refusal> {
    w.broker()
        .execute(
            &verify(req),
            writ_broker::writ_kernel::contracts::StateRevision::new(5).unwrap(),
            None,
        )
        .result
}

#[test]
fn effect_prerequisites_are_checked_before_reservation() {
    let w = W::ready("prereq");
    let good = |k: &str| publish(k, "promotion:wf-1:2", "snap-1", &sha('b'));
    // Not yet accepted: refused before anything is reserved; the key stays unbound.
    assert_eq!(exec(&w, &good("p1")), Err(Refusal::EffectPrerequisiteUnmet));
    assert_eq!(w.one("SELECT count(*) FROM receipts"), 0);
    assert_eq!(w.promote(&accept("k1", 1)), Ok(committed(2)));
    let missing = set(
        &good("p2"),
        &["action", "attributes"],
        json::obj([("workflow_id", json::s("wf-1"))]),
    );
    assert_eq!(exec(&w, &missing), Err(Refusal::PromotionReceiptInvalid));
    assert_eq!(
        exec(&w, &publish("p3", "promotion:wf-1:1", "snap-1", &sha('b'))),
        Err(Refusal::PromotionReceiptInvalid)
    );
    assert_eq!(
        exec(&w, &publish("p4", "promotion:wf-1:2", "snap-2", &sha('e'))),
        Err(Refusal::EffectArtifactMismatch)
    );
    let other_workflow = set(
        &good("p5"),
        &["action", "attributes", "workflow_id"],
        json::s("wf-9"),
    );
    assert_eq!(
        exec(&w, &other_workflow),
        Err(Refusal::RequestScopeMismatch)
    );
    assert_eq!(w.one("SELECT count(*) FROM receipts"), 0);
    // The promoted artifact publishes once.
    assert_eq!(exec(&w, &good("p1")), Ok(Executed::EffectCompleted));
    // An artifact update after the promotion makes it stale.
    w.admin(AdminRequest::UpdateArtifact {
        key: wf(),
        artifact_snapshot_id: "snap-2".into(),
        artifact_digest: sha('e'),
    })
    .unwrap();
    assert_eq!(exec(&w, &good("p6")), Err(Refusal::PromotionStale));
    // So does a new authorization bundle.
    let w = W::ready("prereq-bundle");
    assert_eq!(w.promote(&accept("k1", 1)), Ok(committed(2)));
    let mut b = parse(include_str!(
        "../../../m1/fixtures/transition/m1-transition.bundle.json"
    ));
    let mut statements = b.get("statements").unwrap().as_arr().unwrap().to_vec();
    statements.push(parse(r#"{"statement_id":"permit-publish","effect":"permit","action_kinds":["repository.publish"],"resource_prefixes":["repository:fixture-tenant/fixture-project/"],"reason_code":"PUBLISH_PERMITTED"}"#));
    b = set(&b, &["statements"], Value::Arr(statements));
    b = set(&b, &["version"], json::s("0.4.2"));
    w.active
        .activate(PolicyState::loaded(load_bundle(&b).unwrap()));
    assert_eq!(exec(&w, &good("p1")), Err(Refusal::PromotionStale));
}

/// The effect store with a hook inside the adapter transaction.
struct Paused<'a> {
    inner: &'a SqliteEffectStore,
    hook: &'a (dyn Fn() + Sync),
}

impl EffectAdapter for Paused<'_> {
    fn apply(&self, verify: &mut Verify<'_>) -> Result<Result<EffectResult, Refusal>, TxFailure> {
        (self.hook)();
        self.inner.apply(verify)
    }

    fn inspect(
        &self,
        verify: &mut Verify<'_>,
    ) -> Result<Result<Option<EffectEvidence>, Refusal>, TxFailure> {
        self.inner.inspect(verify)
    }

    fn evidence(&self, permit_id: &str) -> Result<Option<EffectEvidence>, TxFailure> {
        self.inner.evidence(permit_id)
    }
}

#[test]
fn workflow_changes_cannot_slip_between_prerequisite_validation_and_the_effect() {
    let w = W::ready("slip");
    assert_eq!(w.promote(&accept("k1", 1)), Ok(committed(2)));
    let (entered_tx, entered_rx) = mpsc::channel();
    let entered = Mutex::new(Some(entered_tx));
    let release = AtomicBool::new(false);
    let changed_during_effect = AtomicBool::new(false);
    let hook = || {
        if let Some(tx) = entered.lock().unwrap().take() {
            tx.send(()).unwrap();
            thread::sleep(Duration::from_millis(300));
            // Nothing may have changed the workflow while the effect is being authorized.
            if w.state() != ("accepted".to_string(), 2) {
                changed_during_effect.store(true, Ordering::SeqCst);
            }
            release.store(true, Ordering::SeqCst);
        }
    };
    let adapter = Paused {
        inner: &w.effects,
        hook: &hook,
    };
    let b = Broker::new(&w.active, &w.clock, &w.ledger, &adapter, &w.key);
    thread::scope(|s| {
        let effect = s.spawn(|| {
            b.execute(
                &verify(&publish("p1", "promotion:wf-1:2", "snap-1", &sha('b'))),
                writ_broker::writ_kernel::contracts::StateRevision::new(5).unwrap(),
                None,
            )
            .result
        });
        entered_rx.recv_timeout(Duration::from_secs(10)).unwrap();
        // An artifact update (exclusive gate) waits for the effect.
        let update = s.spawn(|| {
            b.administer(
                &Operators,
                &alice(),
                &w.snapshots,
                AdminRequest::UpdateArtifact {
                    key: wf(),
                    artifact_snapshot_id: "snap-2".into(),
                    artifact_digest: sha('e'),
                },
            )
        });
        assert_eq!(effect.join().unwrap(), Ok(Executed::EffectCompleted));
        assert_eq!(update.join().unwrap(), Ok(()));
    });
    assert!(release.load(Ordering::SeqCst));
    assert!(
        !changed_during_effect.load(Ordering::SeqCst),
        "a workflow change slipped in"
    );
    assert_eq!(w.state().1, 3, "the artifact update ran after the effect");
}

// ---------------------------------------------------------------------------
// Real process kills during a promotion (run by `kill_tests`, alone).

/// Prepare a ready workflow (verifying at revision 1) in `dir`.
pub(crate) fn prepare_promotion(dir: &std::path::Path) {
    drop(W::at(dir.to_path_buf(), false).prepared());
}

/// Child: arm a hang of the `promote` transaction at `stage`, announce `READY`, promote.
pub(crate) fn promote_child(dir: &std::path::Path, stage: fault::Stage) {
    let w = W::at(dir.to_path_buf(), true);
    w.publish_evidence();
    w.ledger.inject_hang("promote", stage);
    println!("READY");
    let _ = w.promote(&accept("k1", 1));
    println!("FINISHED");
}

/// Parent check after the kill and a restart: nothing of the promotion is durable, and the
/// identical retry commits it exactly once (recovery from durable state).
pub(crate) fn after_promotion_kill(dir: &std::path::Path) {
    let w = W::at(dir.to_path_buf(), true);
    w.publish_evidence();
    unchanged(&w);
    assert_eq!(w.promote(&accept("k1", 1)), Ok(committed(2)));
    assert_eq!(w.one("SELECT count(*) FROM consumed_records"), 1);
    assert_eq!(
        w.promote(&accept("k1", 1)),
        Ok(PromotionOutcome::Replayed {
            revision: 2,
            receipt_id: "promotion:wf-1:2".into()
        })
    );
}

#[test]
fn a_writ_ledger_4_ledger_is_upgraded_explicitly_keeping_its_identity_and_rows() {
    let dir = std::env::temp_dir().join(format!("writ-m305-{}-upgrade-v4", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join("ledger.db");
    let c = rusqlite::Connection::open(&path).unwrap();
    c.execute_batch(super::SCHEMA_V4).unwrap();
    c.execute_batch(
        "INSERT INTO meta (key, value) VALUES ('ledger_id', '0123456789abcdef0123456789abcdef');
         INSERT INTO audit_refusals (code) VALUES ('KEPT');",
    )
    .unwrap();
    let _: String = c
        .query_row("PRAGMA journal_mode = WAL", [], |r| r.get(0))
        .unwrap();
    drop(c);
    let schema = || writ_broker::store::StoreError::Unavailable {
        category: "initialize".into(),
        code: "schema".into(),
    };
    assert_eq!(
        SqliteLedger::open(&path).err(),
        Some(schema()),
        "never upgraded silently"
    );
    let l = SqliteLedger::upgrade(&path).unwrap();
    assert_eq!(l.ledger_id().as_str(), "0123456789abcdef0123456789abcdef");
    let one = |sql: &str| -> i64 { l.db().conn.query_row(sql, [], |r| r.get(0)).unwrap() };
    assert_eq!(
        one("SELECT count(*) FROM audit_refusals WHERE code = 'KEPT'"),
        1
    );
    assert_eq!(one("SELECT count(*) FROM workflows"), 0, "nothing invented");
    drop(l);
    assert_eq!(
        SqliteLedger::upgrade(&path).err(),
        Some(schema()),
        "one-way"
    );
    assert!(SqliteLedger::open(&path).is_ok());
}
