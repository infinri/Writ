//! `transition-scenario` (m1/fixtures/transition/SCENARIOS.md) on the production broker
//! under D-037 items 1, 2, 3, 5 and 6 (m3/closure/bindings.md):
//!
//! - **Harness policy (item 2):** the active authorization bundle is a real validated
//!   harness bundle from `m3/closure/bundles/` (its digest computed by `load_bundle`, never
//!   asserted); `activate_authorization_bundle` activates a real distinct bundle whose
//!   protection equals the step's protection through `ActivePolicy::activate`.
//! - **Preconditions (item 1, I-2):** production `SqliteLedger::create`, then production
//!   administration for everything it can express (evaluator registration and revocation,
//!   acceptance-policy activation, creation in an unprotected state, artifact updates to the
//!   revision); only what production cannot express (a protected state, a revision without
//!   history, the last promotion with its receipt and event, consumed records) is written
//!   by the test seeder in one rusqlite transaction while no store is open, then validated,
//!   then the stores are opened with production `open`. An inconsistent seed is refused
//!   (`refused_setup`) and the case never runs.
//! - **Evidence (item 3):** inline fixture records are issued, translated and MACed, by the
//!   test acceptance source (`evidence.rs`); the broker reads them by id only.
//! - **Effects (item 5):** an M1 `effect` is a sealed `Broker::execute` with prerequisites
//!   checked in T-reserve; the client permit is never read. A client-asserted `reconcile`
//!   has no production counterpart: the step runs production evidence-based reconciliation
//!   instead and answers it as a production difference.
//! - Final state is read only after every store is closed (item 7).

use std::collections::{BTreeMap, BTreeSet};
use std::sync::{Arc, Barrier, Mutex};

use rusqlite::OptionalExtension;
use writ_broker::ingress::{verify_ingress, Seal, VerifiedRequest};
use writ_broker::keys::{
    AcceptanceRecordKey, IngressKey, MemoryKeyRing, MemoryKeys, PermitKey, RunnerAttestationKey,
};
use writ_broker::policy::PolicyState;
use writ_broker::refusal::Refusal;
use writ_broker::store::Timestamp;
use writ_broker::workflow::{
    AdminRequest, PromotionOutcome, SnapshotCheck, SnapshotStore, SourceUnavailable, Trust,
    WorkflowKey, WorkflowLedger,
};
use writ_broker::writ_kernel::contracts::{StateRevision, WorkflowProtection};
use writ_broker::writ_kernel::json::{self, Map, Value};
use writ_broker::writ_kernel::policy::{load_bundle, LoadedBundle};

use crate::broker::{executed, Step};
use crate::common::{
    adapter_error, count, int, malformed, production_difference, refused, refused_setup, result,
    strings, text, unsupported, with, Labels, Point, TestOperators, World, ADMIN, C1, C5, C7, C8,
    DIFFERENCE, HARNESS_BUNDLE, SEALED_EFFECT, SEEDED,
};
use crate::evidence::{record_keys, registry_entries, runner_keys, Docs};

const BUNDLE_BASE: &str =
    include_str!("../../../../m3/closure/bundles/t1p1-transition.bundle.json");
const BUNDLE_PUBLISH_OTHER: &str =
    include_str!("../../../../m3/closure/bundles/t1p1-transition-publish-other.bundle.json");
/// Replacement bundles for `activate_authorization_bundle`, chosen by protection.
const ACTIVATION_BUNDLES: [&str; 2] = [
    include_str!("../../../../m3/closure/bundles/t1p1-transition-v2.bundle.json"),
    include_str!("../../../../m3/closure/bundles/t1p1-transition-v2-accept-e.bundle.json"),
];
const OTHER_TARGET: &str = "repository:t1/p1/other";
/// The ingress adapter the fixture requests name, with a fixed test key.
const INGRESS_ADAPTER: &str = "writ-host-adapter:fixture";
const INGRESS_KEY: [u8; 32] = [0x5a; 32];
/// Test-only permit key (the transition fixtures carry none).
const PERMIT_KEY: [u8; 32] = [0x6b; 32];

const TRANSITION_STATE: [&str; 18] = [
    "tenant_id",
    "project_id",
    "task_id",
    "workflow_id",
    "state",
    "revision",
    "artifact_snapshot_id",
    "artifact_digest",
    "acceptance_policy_id",
    "acceptance_policy_version",
    "acceptance_policy_bundle_sha",
    "acceptance_requirements",
    "authorization_bundle_sha",
    "effect_prerequisites",
    "last_promotion",
    "now",
    "consumed_record_ids",
    "revoked_evaluator_ids",
];
const TRANSITION_FAULTS: [&str; 3] = [
    "crash_before_commit",
    "crash_before_reserve",
    "crash_after_dispatch",
];

fn transition_step_usable(op: &Value) -> bool {
    if op.as_obj().is_none()
        || op
            .get("fault")
            .is_some_and(|f| !f.as_str().is_some_and(|f| TRANSITION_FAULTS.contains(&f)))
    {
        return false;
    }
    match op.get("op").and_then(Value::as_str) {
        Some("concurrent") => op
            .get("operations")
            .and_then(Value::as_arr)
            .is_some_and(|ops| ops.iter().all(transition_step_usable)),
        Some("promote_concurrent") => {
            op.get("count")
                .and_then(Value::as_int)
                .is_some_and(|n| n > 0)
                && op.get("promote").is_some_and(transition_step_usable)
        }
        _ => true,
    }
}

fn transition_input_usable(input: &Value) -> bool {
    let initial_ok = input.get("initial").is_some_and(|i| {
        i.as_obj().is_some() && TRANSITION_STATE.iter().all(|k| i.get(k).is_some())
    });
    let steps_ok = input
        .get("steps")
        .and_then(Value::as_arr)
        .is_some_and(|ops| ops.iter().all(transition_step_usable));
    initial_ok && steps_ok
}

/// Every operation of `steps`, with the members of `concurrent` and `promote_concurrent`.
fn walk(steps: &[Value]) -> Vec<&Value> {
    let mut out = Vec::new();
    for s in steps {
        match s.get("op").and_then(Value::as_str) {
            Some("concurrent") => out.extend(walk(
                s.get("operations").and_then(Value::as_arr).unwrap_or(&[]),
            )),
            Some("promote_concurrent") => {
                if let Some(p) = s.get("promote") {
                    out.push(p);
                }
            }
            _ => out.push(s),
        }
    }
    out
}

/// The idempotency keys the steps use (promote_concurrent: `<key>-1` .. `<key>-<count>`).
fn step_keys(steps: &[Value]) -> BTreeSet<String> {
    let mut keys = BTreeSet::new();
    for s in steps {
        match s.get("op").and_then(Value::as_str) {
            Some("concurrent") => keys.extend(step_keys(
                s.get("operations").and_then(Value::as_arr).unwrap_or(&[]),
            )),
            Some("promote_concurrent") => {
                let base = s
                    .get("promote")
                    .and_then(|p| text(p, "idempotency_key"))
                    .unwrap_or_default();
                for i in 1..=s.get("count").and_then(Value::as_int).unwrap_or(0) {
                    keys.insert(format!("{base}-{i}"));
                }
            }
            _ => keys.extend(text(s, "idempotency_key")),
        }
    }
    keys
}

/// The record ids any step presents (inline record id or acceptance attribute).
fn presented_records(steps: &[Value]) -> BTreeSet<String> {
    let mut ids = BTreeSet::new();
    for s in walk(steps) {
        if let Some(r) = s.get("record") {
            ids.extend(text(r, "acceptance_record_id"));
        }
        if let Some(a) = s
            .get("request")
            .and_then(|r| r.get("action"))
            .and_then(|a| a.get("attributes"))
        {
            ids.extend(text(a, "acceptance_record_id"));
        }
    }
    ids
}

fn strs(v: Option<&Value>) -> Vec<String> {
    v.and_then(Value::as_arr)
        .unwrap_or(&[])
        .iter()
        .filter_map(|x| x.as_str().map(str::to_string))
        .collect()
}

// ---------------------------------------------------------------------------
// Harness bundles (D-037 item 2)

struct Harness {
    doc: Value,
    loaded: LoadedBundle,
}

fn harness(text: &str) -> Result<Harness, String> {
    let doc = json::parse(text.as_bytes()).map_err(|e| format!("harness bundle: {}", e.code()))?;
    let loaded = load_bundle(&doc).map_err(|e| format!("harness bundle rejected: {e}"))?;
    Ok(Harness { doc, loaded })
}

/// The harness bundle of a case: `publish-other` exactly when the case holds a
/// fingerprint-valid fixture permit for publication to `repository:t1/p1/other`.
fn choose_bundle(steps: &[Value]) -> &'static str {
    let authorizes_other = walk(steps).iter().any(|s| {
        let Some(req) = s.get("request") else {
            return false;
        };
        let target = req
            .get("action")
            .and_then(|a| a.get("resource"))
            .and_then(|r| r.get("canonical_resource_id"))
            .and_then(Value::as_str);
        let fingerprint = s
            .get("permit")
            .and_then(|p| p.get("request_fingerprint"))
            .and_then(Value::as_str);
        s.get("op").and_then(Value::as_str) == Some("effect")
            && target == Some(OTHER_TARGET)
            && fingerprint.is_some_and(|f| json::digest(req).is_ok_and(|d| d.as_str() == f))
    });
    if authorizes_other {
        BUNDLE_PUBLISH_OTHER
    } else {
        BUNDLE_BASE
    }
}

/// Every workflow state a bundle names (statements and protection).
fn bundle_states(doc: &Value) -> BTreeSet<String> {
    let mut states = BTreeSet::new();
    for st in doc.get("statements").and_then(Value::as_arr).unwrap_or(&[]) {
        states.extend(strs(st.get("from_states")));
        states.extend(strs(st.get("to_states")));
    }
    if let Some(p) = doc.get("workflow_protection") {
        states.extend(strs(p.get("protected_to_states")));
        for e in p
            .get("protected_transitions")
            .and_then(Value::as_arr)
            .unwrap_or(&[])
        {
            states.extend(text(e, "from_state"));
            states.extend(text(e, "to_state"));
        }
    }
    states
}

fn protection_equals(p: &WorkflowProtection, v: &Value) -> bool {
    let states: BTreeSet<String> = strs(v.get("protected_to_states")).into_iter().collect();
    let edges: BTreeSet<(String, String)> = v
        .get("protected_transitions")
        .and_then(Value::as_arr)
        .unwrap_or(&[])
        .iter()
        .filter_map(|e| Some((text(e, "from_state")?, text(e, "to_state")?)))
        .collect();
    states == p.protected_to_states.iter().cloned().collect()
        && edges
            == p.protected_transitions
                .iter()
                .map(|e| (e.from_state.clone(), e.to_state.clone()))
                .collect()
        && text(v, "acceptance_policy_bundle_sha").as_deref()
            == Some(p.acceptance_policy_bundle_sha.as_str())
}

// ---------------------------------------------------------------------------
// Test snapshot store: the immutable snapshots the scenario declares

#[derive(Default)]
struct TestSnapshots(Mutex<BTreeSet<(String, String)>>);

impl TestSnapshots {
    fn add(&self, id: Option<String>, digest: Option<String>) {
        if let (Some(id), Some(digest), Ok(mut s)) = (id, digest, self.0.lock()) {
            s.insert((id, digest));
        }
    }
}

impl SnapshotStore for TestSnapshots {
    fn check(
        &self,
        _t: &str,
        _p: &str,
        snapshot_id: &str,
        digest: &str,
    ) -> Result<SnapshotCheck, SourceUnavailable> {
        let s = self.0.lock().map_err(|_| SourceUnavailable)?;
        Ok(
            if s.contains(&(snapshot_id.to_string(), digest.to_string())) {
                SnapshotCheck::Matches
            } else if s.iter().any(|(id, _)| id == snapshot_id) {
                SnapshotCheck::Differs
            } else {
                SnapshotCheck::Unknown
            },
        )
    }
}

// ---------------------------------------------------------------------------
// One transition case

struct TransitionCase {
    world: World,
    key: WorkflowKey,
    snapshots: TestSnapshots,
    docs: Docs,
    record_keys: MemoryKeys<AcceptanceRecordKey>,
    runner_keys: MemoryKeys<RunnerAttestationKey>,
    prerequisites: Vec<(String, Vec<String>)>,
    active_sha: Mutex<String>,
    seeded_receipts: u64,
}

enum Setup {
    Ready(Box<TransitionCase>),
    Refused(Value),
}

fn promotion_value(r: &Result<PromotionOutcome, Refusal>) -> Value {
    match r {
        Ok(PromotionOutcome::Committed { revision, .. }) => json::obj([
            ("result", json::s("committed")),
            ("revision", Value::Int(*revision)),
        ]),
        Ok(PromotionOutcome::Replayed { revision, .. }) => json::obj([
            ("replayed", Value::Bool(true)),
            ("result", json::s("committed")),
            ("revision", Value::Int(*revision)),
        ]),
        Err(r) => refused(r.code()),
    }
}

fn setup(
    case_id: &str,
    initial: &Value,
    steps: &[Value],
    labels: Arc<Labels>,
) -> Result<Setup, String> {
    let active = harness(choose_bundle(steps))?;
    labels.add(HARNESS_BUNDLE);
    let protection = active
        .loaded
        .protection()
        .cloned()
        .ok_or("harness bundle without protection")?;
    let now = initial
        .get("now")
        .and_then(Value::as_str)
        .and_then(Timestamp::parse_rfc3339)
        .ok_or("now")?;
    let mut ring = MemoryKeyRing::new();
    ring.register(
        INGRESS_ADAPTER,
        IngressKey::from_bytes(INGRESS_KEY.to_vec()).map_err(|e| e.to_string())?,
    );
    let permit_key = PermitKey::from_bytes(PERMIT_KEY.to_vec()).map_err(|e| e.to_string())?;
    let world = World::create(
        PolicyState::loaded(active.loaded.clone()),
        now,
        permit_key,
        ring,
        labels.clone(),
    )?;
    let field = |k: &str| text(initial, k).unwrap_or_default();
    let key = WorkflowKey {
        tenant_id: field("tenant_id"),
        project_id: field("project_id"),
        workflow_id: field("workflow_id"),
    };
    let snapshots = TestSnapshots::default();
    snapshots.add(
        text(initial, "artifact_snapshot_id"),
        text(initial, "artifact_digest"),
    );
    let last = initial
        .get("last_promotion")
        .filter(|v| v.as_obj().is_some());
    if let Some(l) = last {
        snapshots.add(text(l, "artifact_snapshot_id"), text(l, "artifact_digest"));
    }
    let mut prerequisites = Vec::new();
    for (kind, spec) in initial
        .get("effect_prerequisites")
        .and_then(Value::as_obj)
        .into_iter()
        .flatten()
    {
        prerequisites.push((kind.clone(), strs(spec.get("required_states"))));
    }
    let mut case = TransitionCase {
        world,
        key: key.clone(),
        snapshots,
        docs: Docs::new(labels.clone()),
        record_keys: record_keys(),
        runner_keys: runner_keys(),
        prerequisites,
        active_sha: Mutex::new(active.loaded.sha.as_str().to_string()),
        seeded_receipts: 0,
    };
    // Production administration for everything it can express.
    let mut admin = vec![];
    for entry in registry_entries(&key.tenant_id, &key.project_id) {
        admin.push((
            "register_evaluator",
            AdminRequest::RegisterEvaluator { entry },
        ));
    }
    for evaluator_id in strs(initial.get("revoked_evaluator_ids")) {
        admin.push((
            "revoke_evaluator",
            AdminRequest::RevokeEvaluator {
                tenant_id: key.tenant_id.clone(),
                project_id: key.project_id.clone(),
                evaluator_id,
            },
        ));
    }
    admin.push((
        "activate_acceptance_policy",
        AdminRequest::ActivateAcceptancePolicy {
            tenant_id: key.tenant_id.clone(),
            project_id: key.project_id.clone(),
            requirements: initial
                .get("acceptance_requirements")
                .cloned()
                .unwrap_or(Value::Null),
            effect_prerequisites: case.prerequisites.clone(),
        },
    ));
    let state = field("state");
    let revision = initial
        .get("revision")
        .and_then(Value::as_int)
        .unwrap_or(-1);
    let protected = protection.protected_to_states.contains(&state)
        || protection
            .protected_transitions
            .iter()
            .any(|e| e.to_state == state);
    let creatable = !protected && last.is_none();
    if creatable {
        admin.push((
            "create_workflow",
            AdminRequest::CreateWorkflow {
                key: key.clone(),
                task_id: field("task_id"),
                initial_state: state.clone(),
                artifact_snapshot_id: field("artifact_snapshot_id"),
                artifact_digest: field("artifact_digest"),
            },
        ));
        for _ in 0..revision.max(0) {
            admin.push((
                "update_artifact",
                AdminRequest::UpdateArtifact {
                    key: key.clone(),
                    artifact_snapshot_id: field("artifact_snapshot_id"),
                    artifact_digest: field("artifact_digest"),
                },
            ));
        }
    }
    for (what, request) in admin {
        if let Err(r) = case.admin(request)? {
            return Ok(Setup::Refused(refused_setup(
                case_id,
                &format!("production administration refused {what}: {}", r.code()),
            )));
        }
    }
    // The test seeder: only what production cannot express, while no store is open.
    case.world.close();
    let rows = case.seed(initial, creatable, active.loaded.sha.as_str())?;
    if rows > 0 {
        labels.add(SEEDED);
    }
    if let Err(reason) = case.validate(initial, steps, &active.doc) {
        return Ok(Setup::Refused(refused_setup(case_id, &reason)));
    }
    case.world.reopen()?;
    Ok(Setup::Ready(Box::new(case)))
}

impl TransitionCase {
    fn admin(&self, request: AdminRequest) -> Result<Result<(), Refusal>, String> {
        Ok(self
            .world
            .broker()?
            .administer(&TestOperators, &ADMIN, &self.snapshots, request))
    }

    /// Write the preconditions production cannot express in one transaction; returns the
    /// number of rows written. The authorization-bundle placeholder of the fixture's
    /// last promotion becomes the computed digest of the active harness bundle.
    fn seed(&mut self, initial: &Value, creatable: bool, bundle_sha: &str) -> Result<u64, String> {
        let db = |e: rusqlite::Error| format!("seed: {e}");
        let mut conn = self.world.seeder()?;
        let tx = conn.transaction().map_err(db)?;
        let k = &self.key;
        let mut rows = 0;
        let last = initial
            .get("last_promotion")
            .filter(|v| v.as_obj().is_some());
        let placeholder = text(initial, "authorization_bundle_sha");
        let lp = |m: &str| last.and_then(|l| text(l, m));
        if !creatable {
            let auth = lp("authorization_bundle_sha").map(|s| {
                if Some(&s) == placeholder.as_ref() {
                    bundle_sha.to_string()
                } else {
                    s
                }
            });
            tx.execute(
                "INSERT INTO workflows (tenant_id, project_id, workflow_id, task_id, state, revision, \
                 artifact_snapshot_id, artifact_digest, last_receipt_id, last_record_id, last_snapshot_id, \
                 last_artifact_digest, last_acceptance_sha, last_authorization_sha) \
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14)",
                rusqlite::params![
                    k.tenant_id,
                    k.project_id,
                    k.workflow_id,
                    text(initial, "task_id").unwrap_or_default(),
                    text(initial, "state").unwrap_or_default(),
                    initial.get("revision").and_then(Value::as_int).unwrap_or(-1).max(0),
                    text(initial, "artifact_snapshot_id").unwrap_or_default(),
                    text(initial, "artifact_digest").unwrap_or_default(),
                    lp("receipt_id"),
                    lp("record_id"),
                    lp("artifact_snapshot_id"),
                    lp("artifact_digest"),
                    lp("acceptance_policy_bundle_sha"),
                    auth,
                ],
            )
            .map_err(db)?;
            rows += 1;
        }
        let mut promotion_key = None;
        if let Some(l) = last {
            let receipt_id = text(l, "receipt_id").unwrap_or_default();
            let revision = receipt_id
                .rsplit(':')
                .next()
                .and_then(|n| n.parse::<i64>().ok())
                .unwrap_or(0);
            let idem = format!("m3c-seed-{receipt_id}");
            let fingerprint =
                json::digest(&json::obj([("m3c_seeded_promotion", json::s(&receipt_id))]))
                    .map_err(|_| "seed fingerprint".to_string())?;
            tx.execute(
                "INSERT INTO promotion_receipts (tenant_id, idempotency_key, fingerprint, receipt_id, \
                 project_id, workflow_id, revision) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
                rusqlite::params![
                    k.tenant_id,
                    idem,
                    fingerprint.as_str(),
                    receipt_id,
                    k.project_id,
                    k.workflow_id,
                    revision
                ],
            )
            .map_err(db)?;
            tx.execute(
                "INSERT INTO workflow_events (tenant_id, project_id, workflow_id, cause, from_state, to_state, \
                 revision, receipt_id, record_id) VALUES (?1, ?2, ?3, 'promote', NULL, ?4, ?5, ?6, ?7)",
                rusqlite::params![
                    k.tenant_id,
                    k.project_id,
                    k.workflow_id,
                    text(l, "to_state").or_else(|| text(initial, "state")).unwrap_or_default(),
                    revision,
                    receipt_id,
                    text(l, "record_id"),
                ],
            )
            .map_err(db)?;
            rows += 2;
            self.seeded_receipts = 1;
            promotion_key = Some((text(l, "record_id"), idem));
        }
        for id in strs(initial.get("consumed_record_ids")) {
            let idem = match &promotion_key {
                Some((Some(r), idem)) if *r == id => idem.clone(),
                _ => format!("m3c-seed-consumed-{id}"),
            };
            tx.execute(
                "INSERT INTO consumed_records (acceptance_record_id, tenant_id, idempotency_key) VALUES (?1, ?2, ?3)",
                rusqlite::params![id, k.tenant_id, idem],
            )
            .map_err(db)?;
            rows += 1;
        }
        tx.commit().map_err(db)?;
        Ok(rows)
    }

    /// I-2 validation on the closed ledger; `Err` is the refusal reason.
    fn validate(&self, initial: &Value, steps: &[Value], bundle: &Value) -> Result<(), String> {
        let c = self.world.reader("ledger.db")?;
        let k = &self.key;
        let db = |e: rusqlite::Error| format!("validate: {e}");
        type Row = (String, i64, [Option<String>; 6]);
        let row: Option<Row> = c
            .query_row(
                "SELECT state, revision, last_receipt_id, last_record_id, last_snapshot_id, last_artifact_digest, \
                 last_acceptance_sha, last_authorization_sha FROM workflows \
                 WHERE tenant_id = ?1 AND project_id = ?2 AND workflow_id = ?3",
                (&k.tenant_id, &k.project_id, &k.workflow_id),
                |r| {
                    Ok((
                        r.get(0)?,
                        r.get(1)?,
                        [r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?, r.get(6)?, r.get(7)?],
                    ))
                },
            )
            .optional()
            .map_err(db)?;
        let Some((state, revision, last)) = row else {
            return Err("no workflow for the scenario's tenant, project and workflow id".into());
        };
        if !bundle_states(bundle).contains(&state) {
            return Err(format!(
                "state {state} is not a state of the active bundle's workflow"
            ));
        }
        if revision < 1 {
            return Err(format!("revision {revision} is below 1"));
        }
        let events: i64 = c
            .query_row(
                "SELECT count(*) FROM workflow_events WHERE tenant_id = ?1 AND project_id = ?2 \
                 AND workflow_id = ?3 AND cause <> 'create'",
                (&k.tenant_id, &k.project_id, &k.workflow_id),
                |r| r.get(0),
            )
            .map_err(db)?;
        if revision < events {
            return Err(format!(
                "revision {revision} is below the {events} revision-changing workflow events"
            ));
        }
        let consumed: BTreeSet<String> =
            strings(&c, "SELECT acceptance_record_id FROM consumed_records")?
                .into_iter()
                .collect();
        match &last {
            [None, None, None, None, None, None] => {}
            [Some(receipt), Some(record), Some(_), Some(_), Some(_), Some(_)] => {
                let r: Option<(String, String, i64)> = c
                    .query_row(
                        "SELECT project_id, workflow_id, revision FROM promotion_receipts WHERE receipt_id = ?1",
                        [receipt],
                        |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
                    )
                    .optional()
                    .map_err(db)?;
                let Some((project, workflow, at)) = r else {
                    return Err(format!("last promotion {receipt} has no promotion receipt"));
                };
                if project != k.project_id
                    || workflow != k.workflow_id
                    || at < 1
                    || at > revision
                    || *receipt != format!("promotion:{}:{at}", k.workflow_id)
                {
                    return Err(format!(
                        "last promotion {receipt} does not belong to this workflow at or before revision {revision}"
                    ));
                }
                let event: i64 = c
                    .query_row(
                        "SELECT count(*) FROM workflow_events WHERE tenant_id = ?1 AND project_id = ?2 \
                         AND workflow_id = ?3 AND cause = 'promote' AND receipt_id = ?4 AND record_id = ?5 \
                         AND revision = ?6",
                        rusqlite::params![k.tenant_id, k.project_id, k.workflow_id, receipt, record, at],
                        |r| r.get(0),
                    )
                    .map_err(db)?;
                if event != 1 {
                    return Err(format!("last promotion {receipt} has no promote event"));
                }
                if !consumed.contains(record) {
                    return Err(format!(
                        "the record {record} of the last promotion is not consumed"
                    ));
                }
            }
            _ => return Err("last promotion fields are partially set".into()),
        }
        let fixture: BTreeSet<String> = strs(initial.get("consumed_record_ids"))
            .into_iter()
            .collect();
        if consumed != fixture {
            return Err("consumed records differ from the scenario's consumed_record_ids".into());
        }
        let revoked: BTreeSet<String> = strings(
            &c,
            "SELECT evaluator_id FROM evaluator_registry WHERE status = 'revoked'",
        )?
        .into_iter()
        .collect();
        if revoked
            != strs(initial.get("revoked_evaluator_ids"))
                .into_iter()
                .collect()
        {
            return Err(
                "revoked evaluators differ from the scenario's revoked_evaluator_ids".into(),
            );
        }
        let keys = step_keys(steps);
        let receipts: BTreeSet<String> =
            strings(&c, "SELECT idempotency_key FROM promotion_receipts")?
                .into_iter()
                .chain(strings(&c, "SELECT idempotency_key FROM receipts")?)
                .collect();
        if let Some(key) = keys.intersection(&receipts).next() {
            return Err(format!(
                "seeded state already holds a receipt for step key {key} (the outcome under test)"
            ));
        }
        if let Some(id) = presented_records(steps).intersection(&consumed).next() {
            return Err(format!("seeded state already consumes record {id}, which a step presents (the outcome under test)"));
        }
        Ok(())
    }

    fn seal(&self, request: &Value) -> Result<VerifiedRequest, Refusal> {
        let issuer = request
            .get("principal")
            .and_then(|p| p.get("verified_by"))
            .and_then(Value::as_str)
            .unwrap_or("");
        let key = IngressKey::from_bytes(INGRESS_KEY.to_vec())
            .map_err(|_| Refusal::IngressUnauthenticated)?;
        let seal = Seal::create(request, issuer, &key).ok_or(Refusal::IngressUnauthenticated)?;
        verify_ingress(request, Some(&seal.to_value()), &self.world.ring)
    }

    /// The fixture request with the operation's idempotency key as its receipt key (the M1
    /// receipt namespace is the operation's `idempotency_key`, bindings.md).
    fn request(op: &Value, key: &str) -> Value {
        with(
            &op.get("request").cloned().unwrap_or(Value::Null),
            &["idempotency_key"],
            json::s(key),
        )
    }

    fn sealed(&self, op: &Value, key: &str) -> Result<VerifiedRequest, Value> {
        self.seal(&Self::request(op, key)).map_err(|r| {
            self.world.labels.add(C1);
            self.world.audit(r.code());
            refused(r.code())
        })
    }

    fn issue(&self, op: &Value) {
        if let Some(record) = op.get("record").filter(|r| r.as_obj().is_some()) {
            self.docs.issue(record);
        }
    }

    fn promote_once(&self, op: &Value, key: &str) -> Result<Value, String> {
        let verified = match self.sealed(op, key) {
            Ok(v) => v,
            Err(v) => return Ok(v),
        };
        let trust = Trust {
            records: &self.docs,
            attestations: &self.docs,
            record_keys: &self.record_keys,
            runner_keys: &self.runner_keys,
            snapshots: &self.snapshots,
        };
        Ok(promotion_value(
            &self.world.broker()?.promote(&trust, &verified, None).result,
        ))
    }

    fn effect_once(&self, op: &Value) -> Result<Value, String> {
        self.world.labels.add(SEALED_EFFECT);
        let key = text(op, "idempotency_key").unwrap_or_default();
        let verified = match self.sealed(op, &key) {
            Ok(v) => v,
            Err(v) => return Ok(v),
        };
        let revision = WorkflowLedger::workflow(&self.world.stores()?.ledger, &self.key)
            .ok()
            .flatten()
            .map_or(0, |w| w.revision);
        let revision = StateRevision::new(revision).ok_or("workflow revision")?;
        Ok(executed(
            &self
                .world
                .broker()?
                .execute(&verified, revision, None)
                .result,
        ))
    }

    fn update_artifact(&self, op: &Value) -> Result<Value, String> {
        self.snapshots.add(
            text(op, "artifact_snapshot_id"),
            text(op, "artifact_digest"),
        );
        let request = AdminRequest::UpdateArtifact {
            key: self.key.clone(),
            artifact_snapshot_id: text(op, "artifact_snapshot_id").unwrap_or_default(),
            artifact_digest: text(op, "artifact_digest").unwrap_or_default(),
        };
        Ok(match self.admin(request)? {
            Ok(()) => {
                let revision = WorkflowLedger::workflow(&self.world.stores()?.ledger, &self.key)
                    .map_err(|e| format!("workflow read: {e}"))?
                    .map_or(-1, |w| w.revision);
                json::obj([
                    ("result", json::s("artifact_updated")),
                    ("revision", Value::Int(revision)),
                ])
            }
            Err(r) => refused(r.code()),
        })
    }

    fn activate_policy(&self, op: &Value) -> Result<Value, String> {
        let request = AdminRequest::ActivateAcceptancePolicy {
            tenant_id: self.key.tenant_id.clone(),
            project_id: self.key.project_id.clone(),
            requirements: op
                .get("acceptance_requirements")
                .cloned()
                .unwrap_or(Value::Null),
            effect_prerequisites: self.prerequisites.clone(),
        };
        Ok(match self.admin(request)? {
            Ok(()) => result("policy_activated"),
            Err(r) => refused(r.code()),
        })
    }

    fn revoke_evaluator(&self, op: &Value) -> Result<Value, String> {
        let request = AdminRequest::RevokeEvaluator {
            tenant_id: self.key.tenant_id.clone(),
            project_id: self.key.project_id.clone(),
            evaluator_id: text(op, "evaluator_id").unwrap_or_default(),
        };
        Ok(match self.admin(request)? {
            Ok(()) => result("evaluator_revoked"),
            Err(r) => refused(r.code()),
        })
    }

    /// A real distinct harness bundle whose protection equals the step's (never the step's
    /// bare digest), activated through the trusted `ActivePolicy::activate`.
    fn activate_bundle(&self, op: &Value) -> Result<Step, String> {
        let wanted = op.get("protection").cloned().unwrap_or(Value::Null);
        let mut active = self.active_sha.lock().map_err(|_| "active bundle")?;
        for text in ACTIVATION_BUNDLES {
            let h = harness(text)?;
            let matches = h
                .loaded
                .protection()
                .is_some_and(|p| protection_equals(p, &wanted));
            if matches && h.loaded.sha.as_str() != active.as_str() {
                *active = h.loaded.sha.as_str().to_string();
                self.world.active.activate(PolicyState::loaded(h.loaded));
                return Ok(Step::Done(result("authorization_bundle_activated")));
            }
        }
        Ok(Step::Unsupported(
            "NO_HARNESS_BUNDLE_FOR_PROTECTION",
            "no distinct harness bundle carries the step's protection".into(),
        ))
    }

    /// Client-asserted reconciliation has no production counterpart (D-036 item 3, D-037
    /// item 5): production evidence-based reconciliation runs instead; the client's
    /// `observed` assertion is never read.
    fn reconcile(&self) -> Result<Value, String> {
        self.world.labels.add(DIFFERENCE);
        const WHY: &str = "CLIENT_ASSERTED_RECONCILIATION_NOT_PROVIDED";
        Ok(match self.world.broker()?.reconcile() {
            Ok(r) => production_difference(WHY, vec![("evidence_reconciled", int(r.changed()))]),
            Err(r) => production_difference(WHY, vec![("code", json::s(r.code()))]),
        })
    }

    fn promote_concurrent(&self, op: &Value) -> Result<Step, String> {
        let promote = op.get("promote").cloned().unwrap_or(Value::Null);
        if promote.get("fault").is_some() {
            return Ok(Step::Unsupported(
                "CONCURRENT_FAULT_NOT_DRIVEN",
                "faults inside promote_concurrent".into(),
            ));
        }
        self.issue(&promote);
        let base = text(&promote, "idempotency_key").unwrap_or_default();
        let n = op.get("count").and_then(Value::as_int).unwrap_or(0);
        let keys: Vec<String> = (1..=n).map(|i| format!("{base}-{i}")).collect();
        let barrier = Barrier::new(keys.len());
        let outcomes: Vec<Result<Value, String>> = std::thread::scope(|scope| {
            let handles: Vec<_> = keys
                .iter()
                .map(|k| {
                    let (barrier, promote) = (&barrier, &promote);
                    scope.spawn(move || {
                        barrier.wait();
                        self.promote_once(promote, k)
                    })
                })
                .collect();
            handles
                .into_iter()
                .map(|h| h.join().unwrap_or_else(|_| Err("worker panicked".into())))
                .collect()
        });
        let mut committed = 0;
        let mut codes: BTreeMap<String, u64> = BTreeMap::new();
        for o in outcomes {
            let v = o?;
            if v.get("result").and_then(Value::as_str) == Some("committed") {
                committed += 1;
            } else {
                *codes
                    .entry(text(&v, "code").unwrap_or_default())
                    .or_default() += 1;
            }
        }
        let refused: Map = codes.into_iter().map(|(k, n)| (k, int(n))).collect();
        Ok(Step::Done(json::obj([
            ("committed", int(committed)),
            ("refused", Value::Obj(refused)),
            ("result", json::s("concurrent")),
        ])))
    }

    /// One unfaulted operation of a `concurrent` group.
    fn plain(&self, op: &Value) -> Result<Value, String> {
        match op.get("op").and_then(Value::as_str).unwrap_or("") {
            "promote" => self.promote_once(op, &text(op, "idempotency_key").unwrap_or_default()),
            "effect" => self.effect_once(op),
            "update_artifact" => self.update_artifact(op),
            "activate_policy" => self.activate_policy(op),
            "revoke_evaluator" => self.revoke_evaluator(op),
            _ => {
                self.world.labels.add(C7);
                self.world.audit("OPERATION_UNKNOWN");
                Ok(refused("OPERATION_UNKNOWN"))
            }
        }
    }

    fn concurrent(&self, op: &Value) -> Result<Step, String> {
        let ops = op.get("operations").and_then(Value::as_arr).unwrap_or(&[]);
        if ops.iter().any(|o| o.get("fault").is_some()) {
            return Ok(Step::Unsupported(
                "CONCURRENT_FAULT_NOT_DRIVEN",
                "faults inside concurrent".into(),
            ));
        }
        for o in ops {
            self.issue(o);
        }
        let barrier = Barrier::new(ops.len());
        let outcomes: Vec<Result<Value, String>> = std::thread::scope(|scope| {
            let handles: Vec<_> = ops
                .iter()
                .map(|o| {
                    let barrier = &barrier;
                    scope.spawn(move || {
                        barrier.wait();
                        self.plain(o)
                    })
                })
                .collect();
            handles
                .into_iter()
                .map(|h| h.join().unwrap_or_else(|_| Err("worker panicked".into())))
                .collect()
        });
        let results = outcomes.into_iter().collect::<Result<Vec<_>, _>>()?;
        Ok(Step::Done(json::obj([
            ("result", json::s("concurrent")),
            ("results", Value::Arr(results)),
        ])))
    }

    /// Run `f` with `point` armed; a crash restarts the stores (C5) and answers `crashed`.
    fn faulted(
        &mut self,
        point: Option<Point>,
        f: impl FnOnce(&Self) -> Result<Value, String>,
    ) -> Result<Step, String> {
        self.world.stores()?.ledger.arm(point);
        let v = f(self)?;
        let stores = self.world.stores()?;
        stores.ledger.arm(None);
        if stores.ledger.crashed() {
            self.world.labels.add(C5);
            self.world.reopen()?;
            return Ok(Step::Done(result("crashed")));
        }
        Ok(Step::Done(v))
    }

    fn step(&mut self, op: &Value) -> Result<Step, String> {
        let fault = op.get("fault").and_then(Value::as_str);
        match op.get("op").and_then(Value::as_str).unwrap_or("") {
            "promote" => {
                self.issue(op);
                let point = (fault == Some("crash_before_commit")).then_some(Point::Promote);
                let key = text(op, "idempotency_key").unwrap_or_default();
                self.faulted(point, |c| c.promote_once(op, &key))
            }
            "effect" => {
                let point = match fault {
                    Some("crash_before_reserve") => Some(Point::Reserve),
                    Some("crash_after_dispatch") => Some(Point::Observe),
                    _ => None,
                };
                self.faulted(point, |c| c.effect_once(op))
            }
            "update_artifact" => self.update_artifact(op).map(Step::Done),
            "activate_policy" => self.activate_policy(op).map(Step::Done),
            "revoke_evaluator" => self.revoke_evaluator(op).map(Step::Done),
            "activate_authorization_bundle" => self.activate_bundle(op),
            "reconcile" => self.reconcile().map(Step::Done),
            "promote_concurrent" => self.promote_concurrent(op),
            "concurrent" => self.concurrent(op),
            "advance_time" => Ok(
                match op
                    .get("now")
                    .and_then(Value::as_str)
                    .and_then(Timestamp::parse_rfc3339)
                {
                    Some(t) => {
                        self.world.clock.set(t);
                        Step::Done(result("time_advanced"))
                    }
                    None => {
                        Step::Unsupported("TIME_NOT_REPRESENTABLE", "now does not parse".into())
                    }
                },
            ),
            _ => {
                self.world.labels.add(C7);
                self.world.audit("OPERATION_UNKNOWN");
                Ok(Step::Done(refused("OPERATION_UNKNOWN")))
            }
        }
    }

    /// The final state, read only after every store of the case is closed. `receipts`
    /// counts the promotion receipts written after setup (the fixture's receipt store starts
    /// empty; the seeded receipt behind `last_promotion` is a precondition, bindings.md).
    fn final_state(&mut self) -> Result<Value, String> {
        self.world.close();
        let l = self.world.reader("ledger.db")?;
        let k = &self.key;
        let (state, revision): (String, i64) = l
            .query_row(
                "SELECT state, revision FROM workflows WHERE tenant_id = ?1 AND project_id = ?2 AND workflow_id = ?3",
                (&k.tenant_id, &k.project_id, &k.workflow_id),
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .map_err(|e| format!("workflow: {e}"))?;
        let consumed = strings(
            &l,
            "SELECT acceptance_record_id FROM consumed_records ORDER BY acceptance_record_id",
        )?;
        let effects: Map = strings(
            &l,
            "SELECT idempotency_key || char(10) || status FROM receipts",
        )?
        .into_iter()
        .filter_map(|row| {
            row.split_once('\n')
                .map(|(k, s)| (k.to_string(), s.to_string()))
        })
        .map(|(k, status)| {
            let status = if status == "observed_success" {
                "completed".to_string()
            } else {
                status
            };
            (k, json::s(&status))
        })
        .collect();
        let receipts = count(&l, "SELECT count(*) FROM promotion_receipts")?
            .saturating_sub(self.seeded_receipts);
        Ok(json::obj([
            (
                "consumed_record_ids",
                Value::Arr(consumed.iter().map(|x| json::s(x)).collect()),
            ),
            ("effects", Value::Obj(effects)),
            ("receipts", int(receipts)),
            (
                "refusals",
                int(count(&l, "SELECT count(*) FROM audit_refusals")?),
            ),
            ("revision", Value::Int(revision)),
            ("state", json::s(&state)),
        ]))
    }
}

pub fn transition_scenario(case_id: &str, input: &Value, labels: Arc<Labels>) -> Value {
    if !transition_input_usable(input) {
        labels.add(C8);
        return malformed();
    }
    let initial = input.get("initial").cloned().unwrap_or(Value::Null);
    let steps = input
        .get("steps")
        .and_then(Value::as_arr)
        .unwrap_or(&[])
        .to_vec();
    let mut case = match setup(case_id, &initial, &steps, labels) {
        Ok(Setup::Ready(c)) => c,
        Ok(Setup::Refused(v)) => return v,
        Err(e) => return adapter_error(&e),
    };
    let mut out = Vec::new();
    for (i, op) in steps.iter().enumerate() {
        match case.step(op) {
            Ok(Step::Done(v)) => out.push(v),
            Ok(Step::Unsupported(reason, detail)) => {
                let name = text(op, "op").unwrap_or_default();
                return unsupported(Some(i), &name, reason, &detail, out);
            }
            Err(e) => return adapter_error(&e),
        }
    }
    match case.final_state() {
        Ok(f) => json::obj([("final", f), ("steps", Value::Arr(out))]),
        Err(e) => adapter_error(&e),
    }
}
