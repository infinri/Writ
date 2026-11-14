//! Production-native transition property coverage (D-038 item 7): seeded, bounded
//! sequences over the production broker and ledger with mechanical-only trusted test
//! policies. Owned by worker W2.
//!
//! Every sequence runs on the production [`Broker`] and [`SqliteLedger`] with a trusted
//! test policy whose only criterion is the mechanical `build` (human signoff would mask
//! the invariants under test), purpose-separated test keys, test sources and the test
//! snapshot store of `transition_tests`. A small xorshift generator (fixed seed list, no
//! new dependency) mixes valid promotions, stale and current expected revisions, identical
//! retries, the same key with another request, reuse of a consumed record or attestation,
//! binding mutations (record id, snapshot id, digest, record snapshot, policy, evaluator,
//! revision) and invalidations (artifact update, acceptance-policy activation, evaluator
//! revocation and re-registration). Each step is checked against an independent model
//! whose expected answer follows the documented check order of `broker::workflow` and
//! `broker::acceptance` (never production output): state and revision advance only by
//! committed promotions and artifact updates, each record and attestation is consumed at
//! most once, an identical retry replays the original outcome, nothing commits with a
//! stale or mutated binding, and a refusal changes nothing but the refusal audit.
//!
//! A concurrent phase then runs two promoters against an invalidating operator thread;
//! the interleaving is not predicted, so the outcomes are checked against the allowed set
//! and the durable history: workflow events form one gap-free revision sequence, every
//! committed protected promotion was bound to the revision and artifact current at its
//! commit, refused evidence is never consumed, and the model resynchronizes from the
//! ledger before the sequence continues. Test keys and sources only.

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::collections::{BTreeMap, BTreeSet};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicI64, AtomicU64, Ordering};
use std::sync::{Barrier, Mutex};
use std::thread;

use writ_broker::acceptance::{MAC_ALG, RECORD_CONTRACT, RECORD_PURPOSE};
use writ_broker::execute::Broker;
use writ_broker::keys::{AcceptanceRecordKey, MemoryKeys, PermitKey, RunnerAttestationKey};
use writ_broker::policy::{ActivePolicy, PolicyState};
use writ_broker::refusal::Refusal;
use writ_broker::workflow::{AdminRequest, PromotionOutcome, Trust, WorkflowLedger};
use writ_broker::writ_kernel::json::{self, Value};
use writ_broker::writ_kernel::policy::{load_bundle, LoadedBundle};
use writ_broker::writ_kernel::time::Timestamp;

use super::grant_tests::{set, verify, TestClock};
use super::transition_tests::{
    alice, parse, sha, sign, transition, wf, Docs, Operators, Snapshots, RECORD_KEY,
};
use super::{SqliteEffectStore, SqliteLedger};

const T: &str = "fixture-tenant";
const P: &str = "fixture-project";
const RUNNER_KEY: [u8; 32] = [0x7c; 32];
const SEEDS: [u64; 6] = [
    0x9e37_79b9_7f4a_7c15,
    20_261_113,
    0x0123_4567_89ab_cdef,
    7,
    0xdead_beef_cafe_f00d,
    424_242,
];
const SEQUENTIAL_STEPS: usize = 70;
const AFTER_CONCURRENT_STEPS: usize = 30;
const PROMOTERS: usize = 2;
const PROMOTIONS_PER_THREAD: usize = 12;
const INVALIDATIONS: usize = 9;

// ---------------------------------------------------------------------------
// Deterministic generator.

struct Rng(u64);

impl Rng {
    fn new(seed: u64) -> Rng {
        Rng(seed | 1)
    }

    fn next(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        self.0 = x;
        x
    }

    fn below(&mut self, n: usize) -> usize {
        usize::try_from(self.next() % (n as u64)).unwrap()
    }

    fn pick<'a, X>(&mut self, items: &'a [X]) -> Option<&'a X> {
        if items.is_empty() {
            None
        } else {
            Some(&items[self.below(items.len())])
        }
    }
}

// ---------------------------------------------------------------------------
// Trusted test documents (mechanical-only policy).

fn now() -> Timestamp {
    Timestamp::parse_strict("2026-11-07T11:00:00Z").unwrap()
}

fn digest_n(n: u64) -> String {
    format!("sha256:{n:064x}")
}

fn requirements(version: u32) -> Value {
    parse(&format!(
        r#"{{"acceptance_policy_id":"accept-code-review","acceptance_policy_version":"{version}","acceptance_policy_bundle_sha":"{}","criteria":[{{"criterion_id":"build","required_class":"mechanical"}}]}}"#,
        sha('c')
    ))
}

/// The fixture transition bundle plus an unprotected `accepted` to `verifying` reopening,
/// so a sequence can promote repeatedly.
fn bundle() -> LoadedBundle {
    let mut b = parse(include_str!(
        "../../../m1/fixtures/transition/m1-transition.bundle.json"
    ));
    let mut statements = b.get("statements").unwrap().as_arr().unwrap().to_vec();
    statements.push(parse(
        r#"{"statement_id":"permit-reopen","effect":"permit","action_kinds":["workflow.transition"],"resource_prefixes":["workflow:fixture-tenant/fixture-project/"],"from_states":["accepted"],"to_states":["verifying"],"reason_code":"REOPEN_PERMITTED"}"#,
    ));
    b = set(&b, &["statements"], Value::Arr(statements));
    load_bundle(&b).unwrap()
}

fn registry_entry(revision: i64) -> Value {
    let mut v = parse(include_str!(
        "../../../docs/contracts/proposed/examples/evaluator-registry-entry.valid.json"
    ));
    v = set(&v, &["scope", "tenant_id"], json::s(T));
    v = set(&v, &["scope", "project_id"], json::s(P));
    set(&v, &["registry_revision"], Value::Int(revision))
}

/// What an issued record (and the attestation it names) is bound to.
#[derive(Clone, Debug, PartialEq, Eq)]
struct Binding {
    revision: i64,
    snapshot: String,
    digest: String,
    policy_version: u32,
    registry_revision: i64,
    evaluator_version: String,
    attestation: String,
}

fn record_doc(id: &str, b: &Binding) -> Value {
    let mut r = parse(&format!(
        r#"{{"contract_version":"m1-draft-3","acceptance_record_id":"{id}","tenant_id":"{T}","project_id":"{P}","task_id":"task-1","workflow_id":"wf-1","transition":{{"from_state":"verifying","to_state":"accepted"}},"artifact_snapshot_id":"{}","artifact_digest":"{}","acceptance_policy_id":"accept-code-review","acceptance_policy_version":"{}","acceptance_policy_bundle_sha":"{}","state_revision":{},"status":"accepted","issued_at":"2026-11-07T10:00:00Z","expires_at":"2026-11-07T12:00:00Z","criteria":[{{"criterion_id":"build","mandatory":true,"required_class":"mechanical","status":"pass","evaluator_id":"ci-runner","evaluator_trust":"M","fresh_until":"2026-11-07T11:30:00Z"}}],"record_digest":"{}"}}"#,
        b.snapshot,
        b.digest,
        b.policy_version,
        sha('c'),
        b.revision,
        sha('0')
    ));
    let d = json::digest(&r.without("record_digest")).unwrap();
    r = set(&r, &["record_digest"], json::s(d.as_str()));
    let evaluation = json::obj([
        ("attestation_id", json::s(&b.attestation)),
        ("criterion_id", json::s("build")),
        ("environment_digest", json::s(&sha('2'))),
        ("evaluation_id", json::s(&format!("eval-{id}"))),
        ("evaluator_id", json::s("ci-runner")),
        ("evaluator_version", json::s(&b.evaluator_version)),
        ("evidence_digests", Value::Arr(vec![])),
        ("implementation_digest", json::s(&sha('1'))),
        ("registry_revision", Value::Int(b.registry_revision)),
    ]);
    let doc = json::obj([
        (
            "auth",
            json::obj([
                ("alg", json::s(MAC_ALG)),
                ("key_id", json::s("props-acc-key")),
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

fn attestation_doc(record_id: &str, b: &Binding) -> Value {
    let mut a = parse(include_str!(
        "../../../docs/contracts/proposed/examples/runner-attestation.valid.json"
    ));
    for (k, v) in [
        ("attestation_id", json::s(&b.attestation)),
        ("tenant_id", json::s(T)),
        ("project_id", json::s(P)),
        (
            "canonical_target",
            json::s(&format!("workflow:{T}/{P}/wf-1")),
        ),
        ("artifact_snapshot_id", json::s(&b.snapshot)),
        ("artifact_digest", json::s(&b.digest)),
        (
            "acceptance_policy_version",
            json::s(&b.policy_version.to_string()),
        ),
        ("acceptance_record_id", json::s(record_id)),
        ("state_revision", Value::Int(b.revision)),
        ("registry_revision", Value::Int(b.registry_revision)),
    ] {
        a = set(&a, &[k], v);
    }
    sign(&a, &RUNNER_KEY)
}

// ---------------------------------------------------------------------------
// The production world.

struct World {
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

impl World {
    fn new(seed: u64) -> World {
        let dir =
            std::env::temp_dir().join(format!("writ-d038-props-{}-{seed:x}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let ledger = SqliteLedger::create(&dir.join("ledger.db")).unwrap();
        let effects =
            SqliteEffectStore::create(&dir.join("effects.db"), ledger.ledger_id()).unwrap();
        let mut record_keys = MemoryKeys::new();
        record_keys.register(
            "props-acc-key",
            AcceptanceRecordKey::from_bytes(RECORD_KEY.to_vec()).unwrap(),
        );
        let mut runner_keys = MemoryKeys::new();
        runner_keys.register(
            "runner-key-1",
            RunnerAttestationKey::from_bytes(RUNNER_KEY.to_vec()).unwrap(),
        );
        World {
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
        }
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

    fn admin(&self, request: AdminRequest) {
        let r = self
            .broker()
            .administer(&Operators, &alice(), &self.snapshots, request);
        assert_eq!(r, Ok(()), "administration must succeed");
    }

    fn trust_snapshot(&self, id: &str, digest: &str) {
        self.snapshots
            .content
            .lock()
            .unwrap()
            .insert(id.into(), digest.into());
    }

    fn promote(&self, req: &Req) -> Outcome {
        self.broker()
            .promote(&self.trust(), &verify(&req.value()), None)
            .result
            .map_err(|r: Refusal| r.code().to_string())
    }

    fn count(&self, sql: &str) -> i64 {
        self.ledger
            .db()
            .conn
            .query_row(sql, [], |r| r.get(0))
            .unwrap()
    }

    fn strings(&self, sql: &str) -> BTreeSet<String> {
        let db = self.ledger.db();
        let mut st = db.conn.prepare(sql).unwrap();
        let rows = st.query_map([], |r| r.get::<_, String>(0)).unwrap();
        rows.map(Result::unwrap).collect()
    }

    fn stored(&self) -> (String, i64, String, String) {
        let w = WorkflowLedger::workflow(&self.ledger, &wf())
            .unwrap()
            .unwrap();
        (
            w.state,
            w.revision,
            w.artifact_snapshot_id,
            w.artifact_digest,
        )
    }
}

impl Drop for World {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

// ---------------------------------------------------------------------------
// The independent model.

type Outcome = Result<PromotionOutcome, String>;

/// A promotion request as the model sees it (every member is in the sealed request).
#[derive(Clone, Debug, PartialEq, Eq)]
struct Req {
    key: String,
    request_id: String,
    from: String,
    to: String,
    expected: i64,
    attrs: Option<(String, String, String)>,
}

impl Req {
    fn value(&self) -> Value {
        let attrs = self
            .attrs
            .as_ref()
            .map(|(i, s, d)| (i.as_str(), s.as_str(), d.as_str()));
        let v = transition(&self.key, &self.from, &self.to, self.expected, attrs);
        set(&v, &["request_id"], json::s(&self.request_id))
    }

    fn protected(&self) -> bool {
        self.to == "accepted"
    }
}

fn refused(code: &str) -> Outcome {
    Err(code.to_string())
}

#[derive(Clone, Debug)]
struct Model {
    state: String,
    revision: i64,
    snapshot: (String, String),
    trusted: BTreeMap<String, String>,
    policy_version: u32,
    registry_revision: i64,
    revoked: bool,
    /// Issued records by id.
    records: BTreeMap<String, Binding>,
    /// The record each issued attestation is bound to.
    attestation_owner: BTreeMap<String, String>,
    consumed_records: BTreeSet<String>,
    consumed_attestations: BTreeSet<String>,
    /// Committed requests by key: the request and its original outcome.
    receipts: BTreeMap<String, (Req, PromotionOutcome)>,
    refusals: i64,
}

impl Model {
    fn current(&self, attestation: &str) -> Binding {
        Binding {
            revision: self.revision,
            snapshot: self.snapshot.0.clone(),
            digest: self.snapshot.1.clone(),
            policy_version: self.policy_version,
            registry_revision: self.registry_revision,
            evaluator_version: "1.4.0".into(),
            attestation: attestation.into(),
        }
    }

    /// The expected answer, in the documented order of `Broker::promote`,
    /// `PromotionIntent::check` and `acceptance::check_use` (D-036, D-038 items 2 to 4).
    fn expect(&self, req: &Req) -> Outcome {
        if let Some((earlier, outcome)) = self.receipts.get(&req.key) {
            return if earlier == req {
                Ok(match outcome {
                    PromotionOutcome::Committed {
                        revision,
                        receipt_id,
                    }
                    | PromotionOutcome::Replayed {
                        revision,
                        receipt_id,
                    } => PromotionOutcome::Replayed {
                        revision: *revision,
                        receipt_id: receipt_id.clone(),
                    },
                })
            } else {
                refused("IDEMPOTENCY_CONFLICT")
            };
        }
        if req.expected != self.revision {
            return refused("REVISION_CONFLICT");
        }
        let record = if req.protected() {
            let (id, snap, digest) = req.attrs.as_ref().unwrap();
            let Some(b) = self.records.get(id) else {
                return refused("ACCEPTANCE_RECORD_UNKNOWN");
            };
            if self.trusted.get(&b.snapshot) != Some(&b.digest) {
                return refused("ARTIFACT_INTEGRITY_VIOLATION");
            }
            if (snap, digest) != (&b.snapshot, &b.digest) {
                return refused("PERMIT_BINDING_MISMATCH");
            }
            Some((id.clone(), b.clone()))
        } else {
            None
        };
        if self.state != req.from {
            return refused("REVISION_CONFLICT");
        }
        if let Some((id, b)) = &record {
            if self.consumed_records.contains(id) {
                return refused("ACCEPTANCE_REPLAYED");
            }
            if (&b.snapshot, &b.digest) != (&self.snapshot.0, &self.snapshot.1) {
                return refused("ACCEPTANCE_ARTIFACT_MISMATCH");
            }
            if b.policy_version != self.policy_version {
                return refused("ACCEPTANCE_POLICY_MISMATCH");
            }
            if b.revision != self.revision {
                return refused("ACCEPTANCE_REVISION_MISMATCH");
            }
            if self.revoked {
                return refused("ACCEPTANCE_EVALUATOR_REVOKED");
            }
            if b.registry_revision != self.registry_revision || b.evaluator_version != "1.4.0" {
                return refused("EVALUATOR_BINDING_MISMATCH");
            }
            if self.attestation_owner.get(&b.attestation) != Some(id) {
                return refused("ATTESTATION_BINDING_MISMATCH");
            }
            if self.consumed_attestations.contains(&b.attestation) {
                return refused("ATTESTATION_REPLAYED");
            }
        }
        let revision = self.revision + 1;
        Ok(PromotionOutcome::Committed {
            revision,
            receipt_id: format!("promotion:wf-1:{revision}"),
        })
    }

    fn apply(&mut self, req: &Req, outcome: &Outcome) {
        match outcome {
            Ok(PromotionOutcome::Committed { revision, .. }) => {
                self.state = req.to.clone();
                self.revision = *revision;
                if req.protected() {
                    let id = req.attrs.as_ref().unwrap().0.clone();
                    let att = self.records[&id].attestation.clone();
                    self.consumed_records.insert(id);
                    self.consumed_attestations.insert(att);
                }
                self.receipts
                    .insert(req.key.clone(), (req.clone(), outcome.clone().unwrap()));
            }
            Ok(PromotionOutcome::Replayed { .. }) => {}
            Err(_) => self.refusals += 1,
        }
    }
}

// ---------------------------------------------------------------------------
// One sequence.

struct Run {
    seed: u64,
    rng: Rng,
    world: World,
    model: Model,
    n: u64,
    sent: Vec<Req>,
    pending: Vec<String>,
    /// Observed outcomes by kind (commit, replay or refusal code), to show coverage.
    seen: BTreeMap<String, usize>,
}

fn kind(outcome: &Outcome) -> String {
    match outcome {
        Ok(PromotionOutcome::Committed { .. }) => "committed".into(),
        Ok(PromotionOutcome::Replayed { .. }) => "replayed".into(),
        Err(code) => code.clone(),
    }
}

impl Run {
    fn new(seed: u64) -> Run {
        let world = World::new(seed);
        world.trust_snapshot("snap-1", &sha('b'));
        world.admin(AdminRequest::CreateWorkflow {
            key: wf(),
            task_id: "task-1".into(),
            initial_state: "planning".into(),
            artifact_snapshot_id: "snap-1".into(),
            artifact_digest: sha('b'),
        });
        world.admin(AdminRequest::ActivateAcceptancePolicy {
            tenant_id: T.into(),
            project_id: P.into(),
            requirements: requirements(3),
            effect_prerequisites: vec![],
        });
        world.admin(AdminRequest::RegisterEvaluator {
            entry: registry_entry(12),
        });
        let mut model = Model {
            state: "planning".into(),
            revision: 0,
            snapshot: ("snap-1".into(), sha('b')),
            trusted: BTreeMap::from([("snap-1".to_string(), sha('b'))]),
            policy_version: 3,
            registry_revision: 12,
            revoked: false,
            records: BTreeMap::new(),
            attestation_owner: BTreeMap::new(),
            consumed_records: BTreeSet::new(),
            consumed_attestations: BTreeSet::new(),
            receipts: BTreeMap::new(),
            refusals: 0,
        };
        let start = Req {
            key: "start".into(),
            request_id: "req-start".into(),
            from: "planning".into(),
            to: "verifying".into(),
            expected: 0,
            attrs: None,
        };
        let got = world.promote(&start);
        assert_eq!(got, model.expect(&start), "seed {seed:x}: start");
        model.apply(&start, &got);
        Run {
            seed,
            rng: Rng::new(seed),
            world,
            model,
            n: 0,
            sent: vec![],
            pending: vec![],
            seen: BTreeMap::new(),
        }
    }

    fn fresh(&mut self, what: &str) -> String {
        self.n += 1;
        format!("{what}{}", self.n)
    }

    /// Issue record `id` with binding `b` to the trusted source; its attestation is
    /// issued with it unless that attestation id already exists (bound to another record).
    fn issue(&mut self, id: &str, b: &Binding) {
        self.world.docs.put(id, record_doc(id, b));
        if !self.model.attestation_owner.contains_key(&b.attestation) {
            self.world.docs.put(&b.attestation, attestation_doc(id, b));
            self.model
                .attestation_owner
                .insert(b.attestation.clone(), id.to_string());
        }
        self.model.records.insert(id.to_string(), b.clone());
    }

    fn fresh_record(&mut self) -> (String, Binding) {
        let id = self.fresh("rec-");
        let att = self.fresh("att-");
        let b = self.model.current(&att);
        (id, b)
    }

    fn request(
        &mut self,
        from: &str,
        to: &str,
        expected: i64,
        attrs: Option<&Binding>,
        id: &str,
    ) -> Req {
        let key = self.fresh("key-");
        Req {
            request_id: format!("req-{key}"),
            key,
            from: from.into(),
            to: to.into(),
            expected,
            attrs: attrs.map(|b| (id.to_string(), b.snapshot.clone(), b.digest.clone())),
        }
    }

    fn valid(&mut self) -> Req {
        let r = self.model.revision;
        if self.model.state == "accepted" {
            return self.request("accepted", "verifying", r, None, "");
        }
        let (id, b) = self.fresh_record();
        self.issue(&id, &b);
        self.request("verifying", "accepted", r, Some(&b), &id)
    }

    fn send(&mut self, step: &str, req: Req) {
        let want = self.model.expect(&req);
        let got = self.world.promote(&req);
        assert_eq!(got, want, "seed {:x} step {step}: {req:?}", self.seed);
        self.model.apply(&req, &got);
        *self.seen.entry(kind(&got)).or_default() += 1;
        self.sent.push(req);
        self.check_durable(step);
    }

    fn check_durable(&self, step: &str) {
        let m = &self.model;
        let w = &self.world;
        let at = format!("seed {:x} step {step}", self.seed);
        let (state, revision, snap, digest) = w.stored();
        assert_eq!((state, revision), (m.state.clone(), m.revision), "{at}");
        assert_eq!((snap, digest), m.snapshot.clone(), "{at}");
        assert_eq!(
            w.strings("SELECT acceptance_record_id FROM consumed_records"),
            m.consumed_records,
            "{at}: records consumed at most once and only by commits"
        );
        assert_eq!(
            w.strings("SELECT attestation_id FROM consumed_attestations"),
            m.consumed_attestations,
            "{at}: attestations consumed at most once and only by commits"
        );
        assert_eq!(
            w.count("SELECT count(*) FROM promotion_receipts"),
            i64::try_from(m.receipts.len()).unwrap(),
            "{at}: receipts only for commits"
        );
        assert_eq!(
            w.count("SELECT count(*) FROM audit_refusals"),
            m.refusals,
            "{at}: every refusal audited once"
        );
    }

    fn update_artifact(&mut self) {
        let id = self.fresh("snap-");
        let digest = digest_n(self.n + 0x1000);
        self.world.trust_snapshot(&id, &digest);
        self.world.admin(AdminRequest::UpdateArtifact {
            key: wf(),
            artifact_snapshot_id: id.clone(),
            artifact_digest: digest.clone(),
        });
        self.model.trusted.insert(id.clone(), digest.clone());
        self.model.snapshot = (id, digest);
        self.model.revision += 1;
    }

    fn activate(&mut self) {
        let v = self.model.policy_version + 1;
        self.world.admin(AdminRequest::ActivateAcceptancePolicy {
            tenant_id: T.into(),
            project_id: P.into(),
            requirements: requirements(v),
            effect_prerequisites: vec![],
        });
        self.model.policy_version = v;
    }

    fn revoke(&mut self) {
        self.world.admin(AdminRequest::RevokeEvaluator {
            tenant_id: T.into(),
            project_id: P.into(),
            evaluator_id: "ci-runner".into(),
        });
        self.model.revoked = true;
    }

    fn register(&mut self) {
        let r = self.model.registry_revision + 1;
        self.world.admin(AdminRequest::RegisterEvaluator {
            entry: registry_entry(r),
        });
        self.model.registry_revision = r;
        self.model.revoked = false;
    }

    /// One generated step.
    fn step(&mut self, i: usize) {
        let label = |s: &str| format!("{i} {s}");
        let r = self.model.revision;
        match self.rng.below(16) {
            0..=2 => {
                let req = self.valid();
                self.send(&label("valid"), req);
            }
            3 => {
                let mut req = self.valid();
                let back = if r >= 2 { 1 + self.rng.below(2) } else { 1 };
                req.expected = r - i64::try_from(back).unwrap();
                self.send(&label("stale expected revision"), req);
            }
            4 => {
                if let Some(req) = self.rng.pick(&self.sent).cloned() {
                    self.send(&label("identical retry"), req);
                }
            }
            5 => {
                let keys: Vec<String> = self.model.receipts.keys().cloned().collect();
                if let Some(key) = self.rng.pick(&keys).cloned() {
                    let mut req = self.model.receipts[&key].0.clone();
                    req.request_id = self.fresh("req-other-");
                    self.send(&label("same key, different request"), req);
                }
            }
            6 => {
                let consumed: Vec<String> = self.model.consumed_records.iter().cloned().collect();
                if let Some(id) = self.rng.pick(&consumed).cloned() {
                    let b = self.model.records[&id].clone();
                    let req = self.request("verifying", "accepted", r, Some(&b), &id);
                    self.send(&label("consumed record reused"), req);
                }
            }
            7 => {
                let consumed: Vec<String> =
                    self.model.consumed_attestations.iter().cloned().collect();
                if let Some(att) = self.rng.pick(&consumed).cloned() {
                    let id = self.fresh("rec-");
                    let b = self.model.current(&att);
                    self.issue(&id, &b);
                    let req = self.request("verifying", "accepted", r, Some(&b), &id);
                    self.send(&label("consumed attestation reused"), req);
                }
            }
            8 | 9 => self.mutation(i),
            10 => {
                let (id, b) = self.fresh_record();
                self.issue(&id, &b);
                self.pending.push(id);
            }
            11 => {
                if let Some(id) = self.rng.pick(&self.pending).cloned() {
                    let b = self.model.records[&id].clone();
                    let req = self.request("verifying", "accepted", r, Some(&b), &id);
                    self.send(&label("record issued before invalidation"), req);
                }
            }
            12 => self.update_artifact(),
            13 => self.activate(),
            14 => {
                if self.model.revoked {
                    self.register();
                } else {
                    self.revoke();
                }
            }
            _ => {
                self.revoke();
                self.register();
            }
        }
        self.check_durable(&label("after"));
    }

    fn mutation(&mut self, i: usize) {
        let r = self.model.revision;
        let (id, mut b) = self.fresh_record();
        let kind = self.rng.below(7);
        let mut attrs = Some((id.clone(), b.snapshot.clone(), b.digest.clone()));
        match kind {
            0 => {
                attrs = Some((
                    self.fresh("rec-unknown-"),
                    b.snapshot.clone(),
                    b.digest.clone(),
                ))
            }
            1 => {
                let other = self
                    .model
                    .trusted
                    .iter()
                    .find(|(s, _)| **s != b.snapshot)
                    .map(|(s, d)| (s.clone(), d.clone()))
                    .unwrap_or_else(|| ("snap-elsewhere".into(), b.digest.clone()));
                attrs = Some((id.clone(), other.0, other.1));
            }
            2 => attrs = Some((id.clone(), b.snapshot.clone(), sha('f'))),
            3 => {
                b.snapshot = self.fresh("snap-untrusted-");
                attrs = Some((id.clone(), b.snapshot.clone(), b.digest.clone()));
            }
            4 => b.policy_version = self.model.policy_version - 1,
            5 => b.evaluator_version = "9.9.9".into(),
            _ => b.revision = r - 1,
        }
        self.issue(&id, &b);
        let key = self.fresh("key-");
        let req = Req {
            request_id: format!("req-{key}"),
            key,
            from: "verifying".into(),
            to: "accepted".into(),
            expected: r,
            attrs,
        };
        let want = self.model.expect(&req);
        if self.model.state == "verifying" {
            assert!(want.is_err(), "a mutated binding never commits: {req:?}");
        }
        self.send(&format!("{i} binding mutation {kind}"), req);
    }

    /// Two promoters race an operator who updates the artifact, activates acceptance
    /// policies and revokes and re-registers the evaluator.
    fn concurrent(&mut self) {
        let w = &self.world;
        let start_seq = w.count("SELECT coalesce(max(seq), 0) FROM workflow_events");
        let start_revision = self.model.revision;
        let start_snapshot = self.model.snapshot.clone();
        let policy = AtomicI64::new(i64::from(self.model.policy_version));
        let registry = AtomicI64::new(self.model.registry_revision);
        let ids = AtomicU64::new(self.n + 1_000_000);
        let done = AtomicBool::new(false);
        let barrier = Barrier::new(PROMOTERS + 1);
        let issued: Mutex<Vec<(String, Binding)>> = Mutex::new(vec![]);
        let seed = self.seed;
        let mut ops = Rng::new(self.seed ^ 0x5555);
        let plan: Vec<usize> = (0..INVALIDATIONS).map(|_| ops.below(3)).collect();
        let (results, updates) = thread::scope(|s| {
            let promoters: Vec<_> = (0..PROMOTERS)
                .map(|t| {
                    let (policy, registry, ids, barrier, issued) =
                        (&policy, &registry, &ids, &barrier, &issued);
                    s.spawn(move || {
                        barrier.wait();
                        let mut out = vec![];
                        for _ in 0..PROMOTIONS_PER_THREAD {
                            let (state, revision, snap, digest) = w.stored();
                            let n = ids.fetch_add(1, Ordering::SeqCst);
                            let key = format!("c{t}-key-{n}");
                            let req = if state == "accepted" {
                                Req {
                                    request_id: format!("req-{key}"),
                                    key,
                                    from: "accepted".into(),
                                    to: "verifying".into(),
                                    expected: revision,
                                    attrs: None,
                                }
                            } else {
                                let id = format!("c-rec-{n}");
                                let b = Binding {
                                    revision,
                                    snapshot: snap,
                                    digest,
                                    policy_version: u32::try_from(policy.load(Ordering::SeqCst))
                                        .unwrap(),
                                    registry_revision: registry.load(Ordering::SeqCst),
                                    evaluator_version: "1.4.0".into(),
                                    attestation: format!("c-att-{n}"),
                                };
                                w.docs.put(&b.attestation, attestation_doc(&id, &b));
                                w.docs.put(&id, record_doc(&id, &b));
                                issued.lock().unwrap().push((id.clone(), b.clone()));
                                Req {
                                    request_id: format!("req-{key}"),
                                    key,
                                    from: "verifying".into(),
                                    to: "accepted".into(),
                                    expected: revision,
                                    attrs: Some((id, b.snapshot, b.digest)),
                                }
                            };
                            let got = w.promote(&req);
                            out.push((req, got));
                        }
                        out
                    })
                })
                .collect();
            let operator = {
                let (policy, registry, barrier, done, plan) =
                    (&policy, &registry, &barrier, &done, &plan);
                s.spawn(move || {
                    barrier.wait();
                    let mut updates = vec![];
                    for (n, op) in plan.iter().enumerate() {
                        match op {
                            0 => {
                                let id = format!("snap-c-{n}");
                                let digest = digest_n(0x2000 + n as u64 + (seed % 1000) * 64);
                                w.trust_snapshot(&id, &digest);
                                w.admin(AdminRequest::UpdateArtifact {
                                    key: wf(),
                                    artifact_snapshot_id: id.clone(),
                                    artifact_digest: digest.clone(),
                                });
                                updates.push((id, digest));
                            }
                            1 => {
                                let v = policy.load(Ordering::SeqCst) + 1;
                                w.admin(AdminRequest::ActivateAcceptancePolicy {
                                    tenant_id: T.into(),
                                    project_id: P.into(),
                                    requirements: requirements(u32::try_from(v).unwrap()),
                                    effect_prerequisites: vec![],
                                });
                                policy.store(v, Ordering::SeqCst);
                            }
                            _ => {
                                w.admin(AdminRequest::RevokeEvaluator {
                                    tenant_id: T.into(),
                                    project_id: P.into(),
                                    evaluator_id: "ci-runner".into(),
                                });
                                let r = registry.load(Ordering::SeqCst) + 1;
                                w.admin(AdminRequest::RegisterEvaluator {
                                    entry: registry_entry(r),
                                });
                                registry.store(r, Ordering::SeqCst);
                            }
                        }
                        thread::yield_now();
                    }
                    done.store(true, Ordering::SeqCst);
                    updates
                })
            };
            let results: Vec<(Req, Outcome)> = promoters
                .into_iter()
                .flat_map(|h| h.join().unwrap())
                .collect();
            (results, operator.join().unwrap())
        });
        assert!(done.load(Ordering::SeqCst));
        let at = format!("seed {seed:x} concurrent phase");
        let allowed = [
            "REVISION_CONFLICT",
            "ACCEPTANCE_POLICY_MISMATCH",
            "ACCEPTANCE_EVALUATOR_REVOKED",
            "EVALUATOR_BINDING_MISMATCH",
        ];
        let issued: BTreeMap<String, Binding> = issued.into_inner().unwrap().into_iter().collect();
        let mut committed = BTreeMap::new();
        for (req, got) in &results {
            match got {
                Ok(PromotionOutcome::Committed { revision, .. }) => {
                    assert!(
                        committed.insert(*revision, req.clone()).is_none(),
                        "{at}: two commits at revision {revision}"
                    );
                }
                Ok(other) => panic!("{at}: fresh key replayed {other:?}"),
                Err(code) => assert!(
                    allowed.contains(&code.as_str()),
                    "{at}: unexpected refusal {code} for {req:?}"
                ),
            }
        }
        // The durable history is one gap-free revision sequence of artifact updates (in
        // the operator's order) and the committed promotions.
        let events: Vec<(String, i64, Option<String>, String)> = {
            let db = w.ledger.db();
            let mut st = db
                .conn
                .prepare(
                    "SELECT cause, revision, record_id, to_state FROM workflow_events \
                     WHERE seq > ?1 ORDER BY seq",
                )
                .unwrap();
            let rows = st
                .query_map([start_seq], |r| {
                    Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?))
                })
                .unwrap();
            rows.map(Result::unwrap).collect()
        };
        let mut revision = start_revision;
        let mut snapshot = start_snapshot;
        let mut updates_left = updates.iter();
        let mut state = self.model.state.clone();
        for (cause, rev, record, to_state) in &events {
            assert_eq!(*rev, revision + 1, "{at}: revision sequence {events:?}");
            revision = *rev;
            match cause.as_str() {
                "artifact" => {
                    snapshot = updates_left.next().cloned().expect("an operator update");
                }
                "promote" => {
                    let req = committed
                        .get(rev)
                        .unwrap_or_else(|| panic!("{at}: promotion at {rev} nobody committed"));
                    assert_eq!(req.from, state, "{at}: committed from the stored state");
                    assert_eq!(&req.to, to_state);
                    if let Some(id) = record {
                        let b = &issued[id];
                        assert_eq!(
                            (b.revision, &b.snapshot, &b.digest),
                            (rev - 1, &snapshot.0, &snapshot.1),
                            "{at}: a commit bound to the revision and artifact current at commit"
                        );
                        assert_eq!(req.attrs.as_ref().map(|a| &a.0), Some(id));
                    } else {
                        assert!(!req.protected());
                    }
                    state = to_state.clone();
                }
                other => panic!("{at}: unexpected event {other}"),
            }
        }
        assert!(updates_left.next().is_none(), "{at}: every update recorded");
        assert_eq!(
            committed.len(),
            events.iter().filter(|e| e.0 == "promote").count(),
            "{at}: every returned commit is durable"
        );
        // Resynchronize the model from the operator's final values and the history.
        let m = &mut self.model;
        for (id, digest) in &updates {
            m.trusted.insert(id.clone(), digest.clone());
        }
        m.policy_version = u32::try_from(policy.load(Ordering::SeqCst)).unwrap();
        m.registry_revision = registry.load(Ordering::SeqCst);
        m.revoked = false;
        for (id, b) in &issued {
            m.records.insert(id.clone(), b.clone());
            m.attestation_owner
                .insert(b.attestation.clone(), id.clone());
        }
        for (req, got) in &results {
            m.apply(req, got);
            *self
                .seen
                .entry(format!("concurrent {}", kind(got)))
                .or_default() += 1;
            self.sent.push(req.clone());
        }
        m.state = state;
        m.revision = revision;
        m.snapshot = snapshot;
        self.n = ids.load(Ordering::SeqCst);
        self.pending.extend(issued.keys().cloned());
        self.check_durable("after concurrent phase");
    }
}

#[test]
fn seeded_transition_sequences_keep_the_promotion_invariants() {
    let mut seen: BTreeMap<String, usize> = BTreeMap::new();
    for seed in SEEDS {
        let mut run = Run::new(seed);
        for i in 0..SEQUENTIAL_STEPS {
            run.step(i);
        }
        run.concurrent();
        for i in 0..AFTER_CONCURRENT_STEPS {
            run.step(SEQUENTIAL_STEPS + i);
        }
        assert!(
            run.model.receipts.len() > 3,
            "seed {seed:x}: the sequence committed promotions"
        );
        assert!(
            run.model.refusals > 3,
            "seed {seed:x}: the sequence exercised refusals"
        );
        for (k, n) in run.seen {
            *seen.entry(k).or_default() += n;
        }
    }
    // The fixed seeds reach every modelled outcome (no vacuous run).
    for k in [
        "committed",
        "replayed",
        "IDEMPOTENCY_CONFLICT",
        "REVISION_CONFLICT",
        "ACCEPTANCE_RECORD_UNKNOWN",
        "ARTIFACT_INTEGRITY_VIOLATION",
        "PERMIT_BINDING_MISMATCH",
        "ACCEPTANCE_REPLAYED",
        "ACCEPTANCE_ARTIFACT_MISMATCH",
        "ACCEPTANCE_POLICY_MISMATCH",
        "ACCEPTANCE_REVISION_MISMATCH",
        "ACCEPTANCE_EVALUATOR_REVOKED",
        "EVALUATOR_BINDING_MISMATCH",
        "ATTESTATION_BINDING_MISMATCH",
        "concurrent committed",
        "concurrent REVISION_CONFLICT",
    ] {
        assert!(seen.contains_key(k), "no step produced {k}: {seen:?}");
    }
    eprintln!("transition properties: {seen:?}");
}
