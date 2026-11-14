//! Production tests required by D-038 (M3-08 closure follow-up): one deterministic
//! revision-conflict code (item 2), `WORKFLOW_UNKNOWN` versus real scope mismatches
//! (item 3), integrity versus binding precedence (item 4) and stage-aware refusal
//! precedence under partial storage outage (item 5). Owned by worker W1.
//!
//! Production components only (the broker, the SQLite ledger and effect store) with the
//! existing test keys, sources and authorities; the workflow world uses a genuine
//! mechanical-only acceptance policy (one mechanical criterion, no human signoff) so that
//! no human criterion masks the invariant under test. The partial-outage wrapper is test
//! infrastructure around the real ledger: it fails lifecycle transactions at begin and
//! forwards everything else.

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::Barrier;
use std::thread;

use writ_broker::acceptance::{AcceptanceCode, MAC_ALG, RECORD_CONTRACT, RECORD_PURPOSE};
use writ_broker::clock::ClockEvent;
use writ_broker::execute::{
    Broker, DispatchIntent, Dispatched, Execution, FailureRecord, Observation, PendingReceipt,
    Progress, Reconciled, Resolution, TxName,
};
use writ_broker::grants::{GrantInsert, NewGrant, ReserveOutcome, ReserveRequest};
use writ_broker::keys::{AcceptanceRecordKey, MemoryKeys, PermitKey, RunnerAttestationKey};
use writ_broker::policy::{ActivePolicy, ExclusiveHold, PolicyState};
use writ_broker::refusal::{ConflictStage, Refusal};
use writ_broker::store::{ExecuteLedger, GrantLedger, LedgerStore, StoreError, TxFailure, TxStage};
use writ_broker::workflow::{
    AdminChange, AdminOperation, AdminPresentation, AdminRefusal, AdminRequest, AuthorizedOperator,
    PromotionIntent, PromotionOutcome, SnapshotCheck, SnapshotStore, SourceUnavailable,
    StoredWorkflow, Trust, WorkflowAuthority, WorkflowKey, WorkflowLedger,
};
use writ_broker::writ_kernel::json::{self, Value};
use writ_broker::writ_kernel::policy::{load_bundle, LoadedBundle};
use writ_broker::writ_kernel::refusal::{external_code, PROMOTION_REFUSED};
use writ_broker::writ_kernel::time::Timestamp;

use super::execute_tests::World;
use super::grant_tests::{
    approve, audited, file_write, publish, publish_forbidden, revision, set, uses, verify,
    TestClock,
};
use super::transition_tests::{
    alice, committed, parse, sha, sign, transition, wf, Docs, Snapshots, RECORD_KEY,
};
use super::{SqliteEffectStore, SqliteLedger};

const T: &str = "fixture-tenant";
const P: &str = "fixture-project";
/// Tenant B of the no-probing tests.
const B: &str = "other-tenant";
const RUNNER_KEY: [u8; 32] = [0x6b; 32];

// ---------------------------------------------------------------------------
// Mechanical-only trusted documents (one mechanical criterion `build`).

fn requirements() -> Value {
    parse(&format!(
        r#"{{"acceptance_policy_id":"accept-code-review","acceptance_policy_version":"3","acceptance_policy_bundle_sha":"{}","criteria":[{{"criterion_id":"build","required_class":"mechanical"}}]}}"#,
        sha('c')
    ))
}

/// The authenticated mechanical-only record `id` for verifying to accepted at `revision`
/// on `snapshot` with `digest`, requiring attestation `att`.
fn record(id: &str, revision: i64, snapshot: &str, digest: &str, att: &str) -> Value {
    let mut r = parse(&format!(
        r#"{{"contract_version":"m1-draft-3","acceptance_record_id":"{id}","tenant_id":"{T}","project_id":"{P}","task_id":"task-1","workflow_id":"wf-1","transition":{{"from_state":"verifying","to_state":"accepted"}},"artifact_snapshot_id":"{snapshot}","artifact_digest":"{digest}","acceptance_policy_id":"accept-code-review","acceptance_policy_version":"3","acceptance_policy_bundle_sha":"{}","state_revision":{revision},"status":"accepted","issued_at":"2026-11-07T10:00:00Z","expires_at":"2026-11-07T12:00:00Z","criteria":[{{"criterion_id":"build","mandatory":true,"required_class":"mechanical","status":"pass","evaluator_id":"ci-runner","evaluator_trust":"M","fresh_until":"2026-11-07T11:30:00Z"}}],"record_digest":"{}"}}"#,
        sha('c'),
        sha('0')
    ));
    let d = json::digest(&r.without("record_digest")).unwrap();
    r = set(&r, &["record_digest"], json::s(d.as_str()));
    let evaluation = json::obj([
        ("attestation_id", json::s(att)),
        ("criterion_id", json::s("build")),
        ("environment_digest", json::s(&sha('2'))),
        ("evaluation_id", json::s("eval-build")),
        ("evaluator_id", json::s("ci-runner")),
        ("evaluator_version", json::s("1.4.0")),
        ("evidence_digests", Value::Arr(vec![])),
        ("implementation_digest", json::s(&sha('1'))),
        ("registry_revision", Value::Int(12)),
    ]);
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
        ("evaluations", Value::Arr(vec![evaluation])),
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

fn registry_entry() -> Value {
    let mut v = parse(include_str!(
        "../../../docs/contracts/proposed/examples/evaluator-registry-entry.valid.json"
    ));
    v = set(&v, &["scope", "tenant_id"], json::s(T));
    v = set(&v, &["scope", "project_id"], json::s(P));
    set(&v, &["registry_revision"], Value::Int(12))
}

/// The M1 transition bundle (its protection names acceptance-policy bundle `sha('c')`).
fn transition_bundle() -> LoadedBundle {
    load_bundle(&parse(include_str!(
        "../../../m1/fixtures/transition/m1-transition.bundle.json"
    )))
    .unwrap()
}

/// `ops:alice` on `ops-console` may administer the fixture project in tenant A and B.
struct Ops;

impl WorkflowAuthority for Ops {
    fn authorize(
        &self,
        p: &AdminPresentation<'_>,
        _: AdminOperation,
        tenant: &str,
        project: &str,
    ) -> Result<AuthorizedOperator, AdminRefusal> {
        if (p.channel, p.operator, p.credential) != ("ops-console", "ops:alice", b"alice-cred") {
            return Err(AdminRefusal::Unauthenticated);
        }
        if !(tenant == T || tenant == B) || project != P {
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

// ---------------------------------------------------------------------------
// The mechanical-only workflow world.

struct M {
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

impl M {
    fn new(name: &str) -> M {
        let dir: PathBuf =
            std::env::temp_dir().join(format!("writ-d038-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let ledger = SqliteLedger::create(&dir.join("ledger.db")).unwrap();
        let effects =
            SqliteEffectStore::create(&dir.join("effects.db"), ledger.ledger_id()).unwrap();
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
        let m = M {
            ledger,
            effects,
            active: ActivePolicy::new(PolicyState::loaded(transition_bundle())),
            clock: TestClock::at(Timestamp::parse_strict("2026-11-07T11:00:00Z").unwrap()),
            key: PermitKey::from_bytes(vec![0x44; 32]).unwrap(),
            docs: Docs::default(),
            snapshots: Snapshots::default(),
            record_keys,
            runner_keys,
        };
        // Two distinct trusted snapshots.
        for (id, digest) in [("snap-1", sha('b')), ("snap-2", sha('e'))] {
            m.snapshots
                .content
                .lock()
                .unwrap()
                .insert(id.into(), digest);
        }
        m
    }

    /// wf-1 created in `planning`, the mechanical-only policy active, `ci-runner`
    /// registered, promoted (unprotected, key k0) to `verifying` at revision 1; record
    /// acc-1 with attestation att-1 published.
    fn ready(name: &str) -> M {
        let m = M::new(name);
        m.admin(AdminRequest::CreateWorkflow {
            key: wf(),
            task_id: "task-1".into(),
            initial_state: "planning".into(),
            artifact_snapshot_id: "snap-1".into(),
            artifact_digest: sha('b'),
        })
        .unwrap();
        m.admin(AdminRequest::ActivateAcceptancePolicy {
            tenant_id: T.into(),
            project_id: P.into(),
            requirements: requirements(),
            effect_prerequisites: vec![],
        })
        .unwrap();
        m.admin(AdminRequest::RegisterEvaluator {
            entry: registry_entry(),
        })
        .unwrap();
        assert_eq!(
            m.promote(&transition("k0", "planning", "verifying", 0, None)),
            Ok(committed(1))
        );
        m.publish("acc-1", "att-1", "snap-1", &sha('b'));
        m
    }

    fn publish(&self, record_id: &str, att: &str, snapshot: &str, digest: &str) {
        self.docs
            .put(record_id, record(record_id, 1, snapshot, digest, att));
        self.docs.put(att, attestation(att, record_id, 1));
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

    fn trust_with<'a>(&'a self, snapshots: &'a dyn SnapshotStore) -> Trust<'a> {
        Trust {
            records: &self.docs,
            attestations: &self.docs,
            record_keys: &self.record_keys,
            runner_keys: &self.runner_keys,
            snapshots,
        }
    }

    fn trust(&self) -> Trust<'_> {
        self.trust_with(&self.snapshots)
    }

    fn admin(&self, request: AdminRequest) -> Result<(), Refusal> {
        self.broker()
            .administer(&Ops, &alice(), &self.snapshots, request)
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

    fn state(&self) -> (String, i64) {
        self.workflow_row(T, "wf-1").unwrap()
    }

    fn workflow_row(&self, tenant: &str, workflow: &str) -> Option<(String, i64)> {
        self.ledger
            .db()
            .conn
            .query_row(
                "SELECT state, revision FROM workflows WHERE tenant_id = ?1 AND workflow_id = ?2",
                [tenant, workflow],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .ok()
    }

    fn consumed(&self) -> Vec<String> {
        let db = self.ledger.db();
        let mut s = db
            .conn
            .prepare("SELECT acceptance_record_id FROM consumed_records ORDER BY 1")
            .unwrap();
        s.query_map([], |r| r.get(0))
            .unwrap()
            .collect::<rusqlite::Result<_>>()
            .unwrap()
    }
}

/// verifying to accepted under record `record_id` with the request's own binding.
fn accept(key: &str, expected: i64, record_id: &str, snapshot: &str, digest: &str) -> Value {
    transition(
        key,
        "verifying",
        "accepted",
        expected,
        Some((record_id, snapshot, digest)),
    )
}

fn accept1(key: &str, expected: i64) -> Value {
    accept(key, expected, "acc-1", "snap-1", &sha('b'))
}

/// Nothing of a refused protected promotion is durable: state and revision unchanged,
/// no record or attestation consumed, no promotion receipt besides k0.
fn unchanged(m: &M) {
    assert_eq!(m.state(), ("verifying".into(), 1));
    assert_eq!(m.one("SELECT count(*) FROM consumed_records"), 0);
    assert_eq!(m.one("SELECT count(*) FROM consumed_attestations"), 0);
    assert_eq!(
        m.one("SELECT count(*) FROM promotion_receipts"),
        1,
        "only k0"
    );
}

// ---------------------------------------------------------------------------
// D-038 item 2: one deterministic revision-conflict code.

#[test]
fn a_stale_expected_revision_is_revision_conflict_detected_at_the_use_time_decision() {
    let m = M::ready("early");
    // Stale (0) and future (5) expected revisions against the stored revision 1.
    for (key, expected) in [("e1", 0), ("e2", 5)] {
        let answer = m.promote(&accept1(key, expected));
        let refusal = answer.unwrap_err();
        assert_eq!(
            refusal,
            Refusal::RevisionConflict(ConflictStage::UseTimeDecision)
        );
        assert_eq!(refusal.code(), "REVISION_CONFLICT");
        assert_eq!(
            refusal.diagnostic(),
            Some("revision conflict: use-time decision (kernel STATE_REVISION_CONFLICT)")
        );
        assert_eq!(refusal.external_code(), PROMOTION_REFUSED);
    }
    unchanged(&m);
    assert_eq!(audited(&m.ledger, "REVISION_CONFLICT"), 2);
    assert_eq!(audited(&m.ledger, "STATE_REVISION_CONFLICT"), 0);
    // The same record still promotes at the current revision: nothing was consumed.
    assert_eq!(m.promote(&accept1("e3", 1)), Ok(committed(2)));
}

/// A snapshot store that, the first time it is consulted (after the use-time decision,
/// before the workflow lock and the promotion transaction), runs `hook` once.
struct Interleave<'a> {
    inner: &'a Snapshots,
    fired: AtomicBool,
    hook: &'a (dyn Fn() + Sync),
}

impl SnapshotStore for Interleave<'_> {
    fn check(
        &self,
        tenant: &str,
        project: &str,
        id: &str,
        digest: &str,
    ) -> Result<SnapshotCheck, SourceUnavailable> {
        if !self.fired.swap(true, Ordering::SeqCst) {
            (self.hook)();
        }
        self.inner.check(tenant, project, id, digest)
    }
}

#[test]
fn a_conflict_committed_between_the_use_time_decision_and_the_transaction_is_detected_in_the_transaction(
) {
    let m = M::ready("interleave");
    m.publish("acc-2", "att-2", "snap-1", &sha('b'));
    // The competing promotion is a real production promotion (another broker, another
    // key, another valid record) committed on another thread while the first promotion
    // is between its use-time decision (made at revision 1) and its transaction.
    let competitor = verify(&accept("k2", 1, "acc-2", "snap-1", &sha('b')));
    let hook = || {
        thread::scope(|s| {
            let answer = s
                .spawn(|| m.broker().promote(&m.trust(), &competitor, None).result)
                .join()
                .unwrap();
            assert_eq!(answer, Ok(committed(2)), "the competitor commits");
        });
        assert_eq!(m.state(), ("accepted".into(), 2), "revision moved for real");
    };
    let interleave = Interleave {
        inner: &m.snapshots,
        fired: AtomicBool::new(false),
        hook: &hook,
    };
    let p = m
        .broker()
        .promote(&m.trust_with(&interleave), &verify(&accept1("k1", 1)), None);
    assert!(interleave.fired.load(Ordering::SeqCst), "the hook ran");
    let refusal = p.result.unwrap_err();
    assert_eq!(
        refusal,
        Refusal::RevisionConflict(ConflictStage::Transaction),
        "the decision saw revision 1, the transaction's compare-and-set saw 2"
    );
    assert_eq!(refusal.code(), "REVISION_CONFLICT");
    assert_eq!(
        refusal.diagnostic(),
        Some("revision conflict: promotion transaction compare-and-set")
    );
    assert!(p.failure.is_none(), "a refusal, not a storage failure");
    // Only the competitor's promotion is durable; acc-1 and att-1 are not consumed.
    assert_eq!(m.state(), ("accepted".into(), 2));
    assert_eq!(m.consumed(), vec!["acc-2".to_string()]);
    assert_eq!(m.one("SELECT count(*) FROM consumed_attestations"), 1);
    assert_eq!(
        m.one("SELECT count(*) FROM consumed_attestations WHERE attestation_id = 'att-1'"),
        0
    );
    assert_eq!(m.one("SELECT count(*) FROM promotion_receipts"), 2);
    assert_eq!(
        m.one("SELECT count(*) FROM promotion_receipts WHERE idempotency_key = 'k1'"),
        0
    );
    // The same request again is now detected early, by the use-time decision.
    assert_eq!(
        m.promote(&accept1("k1", 1)),
        Err(Refusal::RevisionConflict(ConflictStage::UseTimeDecision))
    );
    assert_eq!(audited(&m.ledger, "REVISION_CONFLICT"), 2);
}

#[test]
fn durable_receipt_replay_precedes_re_evaluation_of_a_committed_promotion() {
    let m = M::ready("replay-first");
    assert_eq!(m.promote(&accept1("k1", 1)), Ok(committed(2)));
    // Move the stored revision further through a real administrative write.
    m.admin(AdminRequest::UpdateArtifact {
        key: wf(),
        artifact_snapshot_id: "snap-2".into(),
        artifact_digest: sha('e'),
    })
    .unwrap();
    assert_eq!(m.state(), ("accepted".into(), 3));
    // The identical retry (expected revision 1, consumed record) replays its durable
    // receipt: no re-evaluation, so neither REVISION_CONFLICT nor a replay refusal.
    let replayed = PromotionOutcome::Replayed {
        revision: 2,
        receipt_id: "promotion:wf-1:2".into(),
    };
    assert_eq!(m.promote(&accept1("k1", 1)), Ok(replayed.clone()));
    assert_eq!(m.promote(&accept1("k1", 1)), Ok(replayed));
    // A different request with the same key is IDEMPOTENCY_CONFLICT (also one that
    // would otherwise be a revision conflict or a well-formed request at revision 3).
    for other in [
        accept1("k1", 3),
        accept("k1", 1, "acc-1", "snap-2", &sha('e')),
        transition("k1", "accepted", "verifying", 3, None),
    ] {
        assert_eq!(m.promote(&other), Err(Refusal::IdempotencyConflict));
    }
    assert_eq!(m.state(), ("accepted".into(), 3));
    assert_eq!(m.consumed(), vec!["acc-1".to_string()]);
    assert_eq!(m.one("SELECT count(*) FROM promotion_receipts"), 2);
    assert_eq!(audited(&m.ledger, "REVISION_CONFLICT"), 0);
}

#[test]
fn concurrent_mechanical_only_promotions_commit_exactly_once_and_losers_get_revision_conflict() {
    const WORKERS: usize = 16;
    const ROUNDS: usize = 5;
    let mut aggregates = Vec::new();
    for round in 0..ROUNDS {
        let m = M::ready(&format!("race-{round}"));
        let requests: Vec<_> = (0..WORKERS)
            .map(|i| verify(&accept1(&format!("c{i}"), 1)))
            .collect();
        let start = Barrier::new(WORKERS);
        let results: Vec<_> = thread::scope(|s| {
            let handles: Vec<_> = requests
                .iter()
                .map(|r| {
                    let (m, start) = (&m, &start);
                    s.spawn(move || {
                        start.wait();
                        m.broker().promote(&m.trust(), r, None).result
                    })
                })
                .collect();
            handles.into_iter().map(|h| h.join().unwrap()).collect()
        });
        let commits = results.iter().filter(|r| **r == Ok(committed(2))).count();
        let conflicts = results
            .iter()
            .filter(|r| matches!(r, Err(Refusal::RevisionConflict(_))))
            .count();
        assert!(
            results.iter().all(|r| *r == Ok(committed(2))
                || matches!(
                    r,
                    Err(Refusal::RevisionConflict(
                        ConflictStage::UseTimeDecision | ConflictStage::Transaction
                    ))
                )),
            "round {round}: {results:?}"
        );
        let aggregate = (
            commits,
            conflicts,
            m.state(),
            m.consumed(),
            m.one("SELECT count(*) FROM consumed_attestations"),
            m.one("SELECT count(*) FROM promotion_receipts"),
            audited(&m.ledger, "REVISION_CONFLICT"),
        );
        assert_eq!(
            aggregate,
            (
                1,
                WORKERS - 1,
                ("accepted".to_string(), 2),
                vec!["acc-1".to_string()],
                1,
                2,
                i64::try_from(WORKERS - 1).unwrap()
            ),
            "round {round}"
        );
        aggregates.push(aggregate);
    }
    assert!(aggregates.windows(2).all(|w| w[0] == w[1]));
}

#[test]
fn the_kernel_and_the_execute_path_keep_state_revision_conflict() {
    let w = World::new("d038-execute-revision");
    // The trusted revision is 5; the request expects 4.
    let stale = set(
        &file_write("x1"),
        &["expected_state_revision"],
        Value::Int(4),
    );
    let e = w.broker().execute(&verify(&stale), revision(), None);
    let refusal = e.result.unwrap_err();
    assert!(matches!(refusal, Refusal::Decision { .. }), "{refusal:?}");
    assert_eq!(refusal.code(), "STATE_REVISION_CONFLICT");
    assert_eq!(refusal.diagnostic(), None);
    assert_eq!(
        refusal.external_code(),
        external_code("STATE_REVISION_CONFLICT")
    );
    assert!(e.permit.is_none());
    assert!(e.evidence.transactions.is_empty());
    assert_eq!(audited(&w.ledger, "STATE_REVISION_CONFLICT"), 1);
    assert_eq!(audited(&w.ledger, "REVISION_CONFLICT"), 0);
}

// ---------------------------------------------------------------------------
// D-038 item 3 (`sc-27`): WORKFLOW_UNKNOWN versus real scope mismatches.

/// An unprotected transition request of tenant A naming `resource`.
fn named(key: &str, resource: &str) -> Value {
    set(
        &transition(key, "planning", "verifying", 0, None),
        &["action", "resource", "canonical_resource_id"],
        json::s(resource),
    )
}

/// The M world plus wf-b in tenant B only (and nothing named wf-b in tenant A).
fn with_tenant_b(name: &str) -> M {
    let m = M::ready(name);
    m.admin(AdminRequest::CreateWorkflow {
        key: WorkflowKey {
            tenant_id: B.into(),
            project_id: P.into(),
            workflow_id: "wf-b".into(),
        },
        task_id: "task-b".into(),
        initial_state: "planning".into(),
        artifact_snapshot_id: "snap-1".into(),
        artifact_digest: sha('b'),
    })
    .unwrap();
    assert_eq!(m.workflow_row(B, "wf-b"), Some(("planning".into(), 0)));
    assert_eq!(m.workflow_row(T, "wf-b"), None);
    m
}

fn generic(refusal: &Refusal) {
    assert_eq!(refusal.external_code(), external_code(refusal.code()));
    assert_eq!(refusal.external_code(), PROMOTION_REFUSED);
}

#[test]
fn a_well_formed_in_scope_workflow_that_is_not_stored_is_workflow_unknown() {
    let m = M::ready("unknown");
    let refusal = m
        .promote(&named("u1", &format!("workflow:{T}/{P}/wf-never")))
        .unwrap_err();
    assert_eq!(refusal, Refusal::WorkflowUnknown);
    assert_eq!(refusal.code(), "WORKFLOW_UNKNOWN");
    generic(&refusal);
    assert_eq!(audited(&m.ledger, "WORKFLOW_UNKNOWN"), 1);
    unchanged(&m);
}

#[test]
fn a_cross_scope_or_malformed_resource_binding_is_request_scope_mismatch() {
    let m = with_tenant_b("scope");
    let resources = [
        // Another tenant's workflow (it exists) and one that does not exist there.
        format!("workflow:{B}/{P}/wf-b"),
        format!("workflow:{B}/{P}/wf-never"),
        // Another project of the request's tenant.
        format!("workflow:{T}/other-project/wf-1"),
        // Malformed bindings.
        format!("workflow:{T}/{P}/wf-1/extra"),
        format!("workflow:{T}/{P}"),
        format!("workflow:{T}/{P}/"),
        format!("workflow:{T}/{P}/WF 1"),
        format!("repository:{T}/{P}/wf-1"),
    ];
    for (i, resource) in resources.iter().enumerate() {
        let refusal = m.promote(&named(&format!("s{i}"), resource)).unwrap_err();
        assert_eq!(refusal, Refusal::RequestScopeMismatch, "{resource}");
        assert_eq!(refusal.code(), "REQUEST_SCOPE_MISMATCH");
        generic(&refusal);
    }
    // A request of another kind is not a promotion of this workflow.
    let other_kind = set(
        &transition("s-kind", "verifying", "accepted", 1, None),
        &["action", "kind"],
        json::s("file.write"),
    );
    assert_eq!(m.promote(&other_kind), Err(Refusal::RequestScopeMismatch));
    assert_eq!(
        audited(&m.ledger, "REQUEST_SCOPE_MISMATCH"),
        i64::try_from(resources.len() + 1).unwrap()
    );
    unchanged(&m);
    assert_eq!(m.workflow_row(B, "wf-b"), Some(("planning".into(), 0)));
}

#[test]
fn another_tenants_workflow_is_never_probed_or_revealed() {
    let m = with_tenant_b("probe");
    let events = |m: &M| m.one("SELECT count(*) FROM workflow_events");
    let before = (m.workflow_row(B, "wf-b"), events(&m));
    // Tenant A naming, in its own scope, a workflow id that exists only in tenant B,
    // and one that exists nowhere: the same answer, the same external code.
    let only_in_b = m
        .promote(&named("p1", &format!("workflow:{T}/{P}/wf-b")))
        .unwrap_err();
    let nowhere = m
        .promote(&named("p2", &format!("workflow:{T}/{P}/wf-zzz")))
        .unwrap_err();
    assert_eq!(only_in_b, Refusal::WorkflowUnknown);
    assert_eq!(only_in_b, nowhere);
    assert_eq!(only_in_b.external_code(), nowhere.external_code());
    generic(&only_in_b);
    // Naming B's scope directly: the same scope refusal whether B's workflow exists.
    let exists = m
        .promote(&named("p3", &format!("workflow:{B}/{P}/wf-b")))
        .unwrap_err();
    let absent = m
        .promote(&named("p4", &format!("workflow:{B}/{P}/wf-zzz")))
        .unwrap_err();
    assert_eq!(exists, Refusal::RequestScopeMismatch);
    assert_eq!(exists, absent);
    // Nothing of B's state changed and nothing was written for B.
    assert_eq!((m.workflow_row(B, "wf-b"), events(&m)), before);
    assert_eq!(
        m.one("SELECT count(*) FROM promotion_receipts WHERE tenant_id = 'other-tenant'"),
        0
    );
    unchanged(&m);
}

// ---------------------------------------------------------------------------
// D-038 item 4 (`sc-26`): integrity is checked before the request binding.

#[test]
fn a_record_naming_a_snapshot_absent_from_the_trusted_store_is_an_integrity_violation() {
    let m = M::ready("integrity");
    m.publish("acc-9", "att-9", "snap-9", &sha('9'));
    assert!(!m.snapshots.content.lock().unwrap().contains_key("snap-9"));
    // The request's attributes also disagree with the record (snap-1 versus snap-9): the
    // integrity refusal wins; snap-9 is not manufactured.
    let refusal = m
        .promote(&accept("k1", 1, "acc-9", "snap-1", &sha('b')))
        .unwrap_err();
    assert_eq!(
        refusal,
        Refusal::Acceptance(AcceptanceCode::ArtifactIntegrityViolation)
    );
    assert_eq!(refusal.code(), "ARTIFACT_INTEGRITY_VIOLATION");
    // With agreeing attributes the answer is the same.
    assert_eq!(
        m.promote(&accept("k2", 1, "acc-9", "snap-9", &sha('9'))),
        Err(Refusal::Acceptance(
            AcceptanceCode::ArtifactIntegrityViolation
        ))
    );
    unchanged(&m);
    assert_eq!(audited(&m.ledger, "ARTIFACT_INTEGRITY_VIOLATION"), 2);
    assert_eq!(audited(&m.ledger, "PERMIT_BINDING_MISMATCH"), 0);
}

#[test]
fn a_binding_mismatch_between_two_trusted_snapshots_is_permit_binding_mismatch() {
    let m = M::ready("binding");
    // Both snapshots are trusted preconditions, so the integrity check passes for the
    // record and the binding check is genuinely reached.
    for (id, digest) in [("snap-1", sha('b')), ("snap-2", sha('e'))] {
        assert_eq!(
            m.snapshots.check(T, P, id, &digest),
            Ok(SnapshotCheck::Matches)
        );
    }
    m.publish("acc-3", "att-3", "snap-2", &sha('e'));
    let cases = [
        // Record on snap-1, request names snap-2.
        accept("k1", 1, "acc-1", "snap-2", &sha('e')),
        // Record on snap-2, request names snap-1.
        accept("k2", 1, "acc-3", "snap-1", &sha('b')),
    ];
    for request in &cases {
        let refusal = m.promote(request).unwrap_err();
        assert_eq!(
            refusal,
            Refusal::Acceptance(AcceptanceCode::BindingMismatch)
        );
        assert_eq!(refusal.code(), "PERMIT_BINDING_MISMATCH");
    }
    unchanged(&m);
    assert_eq!(audited(&m.ledger, "PERMIT_BINDING_MISMATCH"), 2);
    // Nothing was consumed: the record and its attestation still promote once.
    assert_eq!(m.promote(&accept1("k3", 1)), Ok(committed(2)));
}

// ---------------------------------------------------------------------------
// D-038 item 5 (OQ-17): stage-aware precedence under the C4 partial outage.

/// The C4 partial outage around a real ledger: every lifecycle and workflow transaction
/// (approve, reserve, dispatch, observe, reconcile, admin, promote) fails at begin and is
/// counted; the refusal audit, the failure-record audit and the trusted-time floor reach
/// the real ledger unless `audit_down` or `floor_down`; the workflow reads (stored
/// workflow, durable promotion receipt) reach it unless `reads_down`.
struct C4<'a> {
    real: &'a SqliteLedger,
    audit_down: AtomicBool,
    floor_down: AtomicBool,
    reads_down: AtomicBool,
    attempts: AtomicUsize,
}

impl<'a> C4<'a> {
    fn new(real: &'a SqliteLedger) -> C4<'a> {
        C4 {
            real,
            audit_down: AtomicBool::new(false),
            floor_down: AtomicBool::new(false),
            reads_down: AtomicBool::new(false),
            attempts: AtomicUsize::new(0),
        }
    }

    fn attempts(&self) -> usize {
        self.attempts.load(Ordering::SeqCst)
    }

    fn down(&self, name: &str) -> TxFailure {
        self.attempts.fetch_add(1, Ordering::SeqCst);
        TxFailure::new(TxStage::Begin, down(&format!("transaction {name}")))
    }
}

fn down(category: &str) -> StoreError {
    StoreError::Unavailable {
        category: category.into(),
        code: "SQLITE_IOERR".into(),
    }
}

impl GrantLedger for C4<'_> {
    fn insert_grant(&self, _: &NewGrant) -> Result<GrantInsert, StoreError> {
        Err(self.down("approve").error)
    }

    fn reserve(
        &self,
        _: &ReserveRequest<'_>,
    ) -> Result<Result<ReserveOutcome, Refusal>, TxFailure> {
        Err(self.down("reserve"))
    }
}

impl ExecuteLedger for C4<'_> {
    fn dispatch(&self, _: &DispatchIntent<'_>) -> Result<Result<Dispatched, Refusal>, TxFailure> {
        Err(self.down("dispatch"))
    }

    fn observe_effect(&self, _: &Observation<'_>) -> Result<(), TxFailure> {
        Err(self.down("observe"))
    }

    fn pending_receipts(&self, _: &ExclusiveHold<'_>) -> Result<Vec<PendingReceipt>, TxFailure> {
        Err(self.down("reconcile_scan"))
    }

    fn reconcile(&self, _: &ExclusiveHold<'_>, _: &[Resolution]) -> Result<Reconciled, TxFailure> {
        Err(self.down("reconcile"))
    }

    fn record_failure(&self, code: &str, record: &FailureRecord) -> Result<(), StoreError> {
        if self.audit_down.load(Ordering::SeqCst) {
            return Err(down("transaction audit"));
        }
        self.real.record_failure(code, record)
    }
}

impl WorkflowLedger for C4<'_> {
    fn workflow(&self, key: &WorkflowKey) -> Result<Option<StoredWorkflow>, StoreError> {
        if self.reads_down.load(Ordering::SeqCst) {
            return Err(down("read"));
        }
        self.real.workflow(key)
    }

    fn promotion_receipt(
        &self,
        tenant_id: &str,
        idempotency_key: &str,
    ) -> Result<Option<(String, PromotionOutcome)>, StoreError> {
        if self.reads_down.load(Ordering::SeqCst) {
            return Err(down("read"));
        }
        self.real.promotion_receipt(tenant_id, idempotency_key)
    }

    fn administer(&self, _: &AdminChange) -> Result<Result<(), Refusal>, TxFailure> {
        Err(self.down("admin"))
    }

    fn promote(
        &self,
        _: &PromotionIntent<'_>,
    ) -> Result<Result<PromotionOutcome, Refusal>, TxFailure> {
        Err(self.down("promote"))
    }
}

impl LedgerStore for &C4<'_> {
    fn owner_epoch(&self) -> u64 {
        self.real.owner_epoch()
    }

    fn record_refusal(&mut self, code: &str) -> Result<(), StoreError> {
        if self.audit_down.load(Ordering::SeqCst) {
            return Err(down("transaction audit"));
        }
        let mut real: &SqliteLedger = self.real;
        real.record_refusal(code)
    }

    fn time_floor(&self) -> Result<Option<Timestamp>, StoreError> {
        if self.floor_down.load(Ordering::SeqCst) {
            return Err(down("read"));
        }
        LedgerStore::time_floor(&self.real)
    }

    fn raise_time_floor(&mut self, at: Timestamp) -> Result<Timestamp, StoreError> {
        if self.floor_down.load(Ordering::SeqCst) {
            return Err(down("transaction time_floor"));
        }
        let mut real: &SqliteLedger = self.real;
        real.raise_time_floor(at)
    }

    fn record_clock_event(&mut self, event: &ClockEvent) -> Result<(), StoreError> {
        if self.floor_down.load(Ordering::SeqCst) {
            return Err(down("transaction clock_event"));
        }
        let mut real: &SqliteLedger = self.real;
        real.record_clock_event(event)
    }
}

fn rows(l: &SqliteLedger, sql: &str) -> i64 {
    l.db().conn.query_row(sql, [], |r| r.get(0)).unwrap()
}

fn effect_rows(w: &World) -> (i64, i64) {
    let db = w.effects.db();
    let count = |sql: &str| -> i64 { db.conn.query_row(sql, [], |r| r.get(0)).unwrap() };
    (
        count("SELECT count(*) FROM effects"),
        count("SELECT count(*) FROM consumed_permits"),
    )
}

/// Nothing reserved, used, dispatched, consumed or applied.
fn nothing_progressed(w: &World, e: &Execution) {
    assert!(e.permit.is_none());
    assert_eq!(e.evidence.progress(), Progress::NoChange);
    assert_eq!(e.evidence.possible_progress(), Progress::NoChange);
    assert!(!e.evidence.reconciliation_required());
    assert_eq!(rows(&w.ledger, "SELECT count(*) FROM receipts"), 0);
    assert_eq!(rows(&w.ledger, "SELECT count(*) FROM receipt_events"), 0);
    assert_eq!(rows(&w.ledger, "SELECT count(*) FROM grant_uses"), 0);
    assert_eq!(effect_rows(w), (0, 0));
}

fn past_deadline(idem: &str) -> Value {
    set(
        &file_write(idem),
        &["deadline_at"],
        json::s("2026-11-05T11:00:00Z"),
    )
}

/// The stage-1 refusals (I-3): the decision's answer, returned before any lifecycle
/// transaction is attempted, whether or not the refusal audit can be written.
#[test]
fn under_partial_outage_stage_one_refusals_precede_any_lifecycle_transaction() {
    for audit_down in [false, true] {
        let w = World::new(&format!("d038-c4-stage1-{audit_down}"));
        let c4 = C4::new(&w.ledger);
        c4.audit_down.store(audit_down, Ordering::SeqCst);
        let b = Broker::new(&w.active, &w.clock, &c4, &w.effects, &w.key);
        let default_deny = set(
            &file_write("d1"),
            &["action", "kind"],
            json::s("agent.dispatch"),
        );
        let out_of_scope = set(
            &file_write("d2"),
            &["action", "resource", "canonical_resource_id"],
            json::s("workspace:other-tenant/fixture-project/src/app.py"),
        );
        let mut cases: Vec<(&str, Value, &str)> = vec![
            ("default deny", default_deny, "DEFAULT_DENY"),
            ("scope deny", out_of_scope, "RESOURCE_SCOPE_MISMATCH"),
            ("deadline", past_deadline("d3"), "DEADLINE_EXCEEDED"),
            (
                "stale revision",
                set(
                    &file_write("d4"),
                    &["expected_state_revision"],
                    Value::Int(4),
                ),
                "STATE_REVISION_CONFLICT",
            ),
            ("approval", publish("a1"), "APPROVAL_REQUIRED"),
        ];
        let mut answers = Vec::new();
        for (name, request, code) in cases.drain(..) {
            let audits = rows(&w.ledger, "SELECT count(*) FROM audit_refusals");
            let e = b.execute(&verify(&request), revision(), None);
            assert_eq!(
                e.result.as_ref().map_err(Refusal::code),
                Err(code),
                "{name}"
            );
            assert!(e.evidence.transactions.is_empty(), "{name}");
            assert_eq!(
                c4.attempts(),
                0,
                "{name}: no lifecycle transaction attempted"
            );
            nothing_progressed(&w, &e);
            assert_eq!(
                rows(&w.ledger, "SELECT count(*) FROM audit_refusals") - audits,
                i64::from(!audit_down),
                "{name}: the refusal is audited only when the audit write works"
            );
            answers.push(e.result.map(|_| ()));
        }
        // A forbidding statement of the current policy, and no policy at all.
        w.active.activate(PolicyState::loaded(publish_forbidden()));
        let e = b.execute(&verify(&publish("f1")), revision(), None);
        assert_eq!(
            e.result.as_ref().map_err(Refusal::code),
            Err("PUBLISH_FROZEN")
        );
        nothing_progressed(&w, &e);
        w.active.activate(PolicyState::Absent);
        let e = b.execute(&verify(&file_write("f2")), revision(), None);
        assert_eq!(
            e.result.as_ref().map_err(Refusal::code),
            Err("POLICY_UNAVAILABLE")
        );
        nothing_progressed(&w, &e);
        assert_eq!(c4.attempts(), 0);
        assert!(answers.iter().all(Result::is_err), "nothing authorized");
        assert_eq!(rows(&w.ledger, "SELECT count(*) FROM failure_records"), 0);
    }
}

/// Stage 2 (I-3): an operation the decision allows reaches T-reserve, which fails:
/// `LEDGER_UNAVAILABLE`, nothing reserved, dispatched or applied, no possible progress;
/// a failing refusal-audit write changes nothing.
#[test]
fn under_partial_outage_an_allowed_execution_is_ledger_unavailable_without_any_progress() {
    for audit_down in [false, true] {
        let w = World::new(&format!("d038-c4-allow-{audit_down}"));
        // An approved grant for a publish, issued before the outage.
        approve(w.broker().grants(), &publish("g"), "g1").unwrap();
        let c4 = C4::new(&w.ledger);
        c4.audit_down.store(audit_down, Ordering::SeqCst);
        let b = Broker::new(&w.active, &w.clock, &c4, &w.effects, &w.key);
        let audits = rows(&w.ledger, "SELECT count(*) FROM audit_refusals");
        let allowed = b.execute(&verify(&file_write("w1")), revision(), None);
        let granted = b.execute(
            &verify(&publish("g")),
            revision(),
            Some(&super::grant_tests::label("g1")),
        );
        for (name, e) in [("allow", &allowed), ("granted approval", &granted)] {
            assert_eq!(
                e.result,
                Err(Refusal::LedgerUnavailable(down("transaction reserve"))),
                "{name}"
            );
            let (tx, failure) = e.evidence.failure().unwrap();
            assert_eq!(tx, TxName::Reserve, "{name}");
            assert_eq!(failure.stage, TxStage::Begin, "{name}");
            assert!(!failure.outcome_unknown, "{name}");
            assert_eq!(
                e.evidence.transactions.len(),
                1,
                "{name}: nothing after reserve"
            );
            nothing_progressed(&w, e);
        }
        assert_eq!(c4.attempts(), 2, "exactly the two T-reserve attempts");
        assert_eq!(uses(&w.ledger, "g1"), Some(0), "no grant use");
        assert_eq!(
            rows(&w.ledger, "SELECT count(*) FROM audit_refusals") - audits,
            if audit_down { 0 } else { 2 }
        );
        assert_eq!(
            rows(
                &w.ledger,
                "SELECT count(*) FROM failure_records WHERE transaction_name = 'reserve' \
                 AND stage = 'begin' AND progress = 'no_change' \
                 AND possible_progress = 'no_change' AND outcome_unknown = 0"
            ),
            if audit_down { 0 } else { 2 }
        );
        // The same requests succeed once storage is back: nothing was half-done.
        drop(b);
        assert!(w
            .broker()
            .execute(&verify(&file_write("w1")), revision(), None)
            .result
            .is_ok());
    }
}

/// Trusted time (I-3 stage 1, before the decision): a failing floor is
/// `TIME_UNAVAILABLE` for a request the decision would deny and one it would allow.
#[test]
fn a_trusted_time_floor_failure_is_time_unavailable_before_the_decision() {
    let w = World::new("d038-c4-floor");
    let c4 = C4::new(&w.ledger);
    c4.floor_down.store(true, Ordering::SeqCst);
    let b = Broker::new(&w.active, &w.clock, &c4, &w.effects, &w.key);
    let deny = set(
        &file_write("t1"),
        &["action", "kind"],
        json::s("agent.dispatch"),
    );
    for request in [deny, file_write("t2"), publish("t3")] {
        let e = b.execute(&verify(&request), revision(), None);
        assert_eq!(e.result, Err(Refusal::TimeUnavailable));
        assert!(e.evidence.transactions.is_empty());
        nothing_progressed(&w, &e);
    }
    assert_eq!(c4.attempts(), 0);
    assert_eq!(audited(&w.ledger, "TIME_UNAVAILABLE"), 3);
    assert_eq!(audited(&w.ledger, "DEFAULT_DENY"), 0);
}

/// Promotions under the same partial outage: the durable receipt lookup (replay first,
/// D-036 item 2) and the workflow read are storage reads before the use-time decision;
/// with them available a deny or a missing approval is returned before the promotion
/// transaction is attempted and an allowed promotion is LEDGER_UNAVAILABLE at its
/// transaction; with the reads failing the answer is LEDGER_UNAVAILABLE before any
/// decision. Nothing is consumed and the state never moves.
#[test]
fn under_partial_outage_promotions_follow_the_same_stage_order() {
    let m = M::ready("c4-promote");
    let c4 = C4::new(&m.ledger);
    let b = Broker::new(&m.active, &m.clock, &c4, &m.effects, &m.key);
    let promote = |request: &Value| b.promote(&m.trust(), &verify(request), None);
    // Stage 1: the decision (agents may not release; deploy requires approval) and a
    // stale revision.
    let release = transition(
        "r1",
        "verifying",
        "released",
        1,
        Some(("acc-1", "snap-1", &sha('b'))),
    );
    let deploy = transition(
        "r2",
        "verifying",
        "deployed",
        1,
        Some(("acc-1", "snap-1", &sha('b'))),
    );
    let p = promote(&release);
    assert_eq!(
        p.result.as_ref().map_err(Refusal::code),
        Err("AGENT_RELEASE_FORBIDDEN")
    );
    assert!(p.failure.is_none());
    assert_eq!(promote(&deploy).result, Err(Refusal::ApprovalRequired));
    assert_eq!(
        promote(&accept1("r3", 0)).result,
        Err(Refusal::RevisionConflict(ConflictStage::UseTimeDecision))
    );
    assert_eq!(c4.attempts(), 0, "no promotion transaction attempted");
    // Stage 2: the promotion transaction fails at begin.
    let p = promote(&accept1("r4", 1));
    assert_eq!(
        p.result,
        Err(Refusal::LedgerUnavailable(down("transaction promote")))
    );
    let failure = p.failure.unwrap();
    assert_eq!(failure.stage, TxStage::Begin);
    assert!(!failure.outcome_unknown);
    assert_eq!(c4.attempts(), 1);
    // Storage failing at the durable receipt lookup: LEDGER_UNAVAILABLE first, even for
    // a request the decision would deny, and with the refusal audit failing too.
    c4.reads_down.store(true, Ordering::SeqCst);
    c4.audit_down.store(true, Ordering::SeqCst);
    let audits = m.one("SELECT count(*) FROM audit_refusals");
    for request in [&release, &accept1("r5", 1)] {
        let p = promote(request);
        assert_eq!(p.result, Err(Refusal::LedgerUnavailable(down("read"))));
        assert!(p.failure.is_none(), "a read, not a transaction");
    }
    assert_eq!(m.one("SELECT count(*) FROM audit_refusals"), audits);
    assert_eq!(c4.attempts(), 1);
    unchanged(&m);
    // Storage back: the record was never consumed.
    drop(b);
    assert_eq!(m.promote(&accept1("r4", 1)), Ok(committed(2)));
}
