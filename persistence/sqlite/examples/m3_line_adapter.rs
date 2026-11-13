//! M3-08 closure adapter (development only; a Cargo example, never shipped).
//!
//! Speaks the M1 line protocol (D-021; m1/driver/parity.py) for the frozen
//! `broker-scenario` and `transition-scenario` sets and drives the production
//! `writ-broker` `Broker` (execute, approvals, present_permit, reconcile,
//! promote, administer) on a fresh production SQLite ledger and effect store
//! per case (`writ-ledger-sqlite`). Case-by-case expectations and exceptions:
//! docs/implementation/M3-08-CLOSURE-MAP.md; gate: m3/closure/check.py.
//!
//! **Test-only authorities.** Every key, channel, approval authority,
//! workflow (operator) authority, snapshot store and trusted source here is a
//! test stand-in built from fixture values or fixed test constants. None of
//! them establishes production authentication: production human approval,
//! operator authentication, key provisioning, the acceptance service and the
//! immutable snapshot store remain open obligations (M4, M5, M6). A passing
//! case shows the broker logic on a production ledger, not a deployed trust
//! chain.
//!
//! **Harness composition (each is recorded in the closure map):**
//! - Storage faults and crashes are injected at the storage boundary by a
//!   wrapper around the production `SqliteLedger` (D-033 item 5: test-only
//!   injection at the storage boundary, schema unchanged). A crash makes the
//!   named lifecycle transaction fail at begin and every later ledger write of
//!   that broker fail (the process is gone, so no refusal audit is written);
//!   the adapter then answers `{"result": "crashed"}` and restarts: ledger
//!   and effect store are closed and reopened with `open`, and a new broker is
//!   built. `ledger: down` fails the lifecycle transactions (reserve,
//!   dispatch, observe, approve, reconcile, promote, administer) at begin
//!   while the refusal audit and trusted-time floor stay available.
//! - `effect_fails` uses the effect store's public `set_effects_fail`.
//! - An ingress refusal comes from the production `verify_ingress`; the
//!   production broker has no operation that accepts an unverified request,
//!   so the adapter audits that refusal through the ledger's public
//!   `LedgerStore::record_refusal` (the broker's own audit transaction). The
//!   same applies to `OPERATION_UNKNOWN`, a line-protocol answer.
//! - `approve` carries an unsealed `for_request`; production approval takes a
//!   verified request, so the adapter seals it with the fixture's ingress key
//!   for the request's `principal.verified_by`.
//! - The approval authority authenticates exactly the fixture's channel
//!   credentials and reports every authenticated principal as a human (as the
//!   fixtures model it).
//! - Transition scenarios carry no authorization bundle and no ingress keys:
//!   the frozen `m1-transition.bundle.json` (whose workflow protection equals
//!   the scenarios' protection) is the active policy, requests are sealed with
//!   a fixed test ingress key, the initial workflow is established only
//!   through production administration (create, acceptance-policy
//!   activation, artifact updates to reach the revision), and the trusted
//!   record and attestation sources hold nothing: inline fixture records are
//!   never read (D-036 item 1) and client permits are ignored (D-036 item 2).
//! - Final counts are read from the durable ledger and effect store through a
//!   separate read-only SQLite connection (inspection only).
//!
//! **Unsupported steps.** Where the production API cannot express a fixture
//! step without a new semantic choice, the case answers
//! `{"unsupported": {"step", "op", "reason", ...}, "steps": [...]}` and stops.
//!
//!   cargo run --release --example m3_line_adapter -- broker-scenario < lines.jsonl

use std::collections::{BTreeMap, BTreeSet};
use std::io::{self, BufRead, Write};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Barrier, Mutex};

use rusqlite::{Connection, OpenFlags};
use writ_broker::clock::{ClockError, ClockEvent, Drift, EffectiveClock, Reading};
use writ_broker::execute::{
    Broker, DispatchIntent, Dispatched, Executed, FailureRecord, Observation, PendingReceipt,
    Reconciled, Resolution,
};
use writ_broker::grants::{
    Approval, ApprovalAuthority, ApprovalPresentation, ApproverRefusal, AuthenticatedApprover,
    GrantInsert, GrantLabel, NewGrant, PrincipalKind, Replay, ReserveOutcome, ReserveRequest,
    Verdict,
};
use writ_broker::ingress::{verify_ingress, Seal};
use writ_broker::keys::{
    AcceptanceRecordKey, IngressKey, MemoryKeyRing, MemoryKeys, PermitKey, RunnerAttestationKey,
};
use writ_broker::permit::Permit;
use writ_broker::policy::{ActivePolicy, ExclusiveHold, PolicyState};
use writ_broker::refusal::Refusal;
use writ_broker::store::{
    ExecuteLedger, GrantLedger, LedgerStore, StoreError, Timestamp, TxFailure, TxStage,
};
use writ_broker::workflow::{
    AcceptanceRecordSource, AdminChange, AdminOperation, AdminPresentation, AdminRefusal,
    AdminRequest, AttestationSource, AuthorizedOperator, PromotionIntent, PromotionOutcome,
    SnapshotCheck, SnapshotStore, SourceUnavailable, StoredWorkflow, Trust, WorkflowAuthority,
    WorkflowKey, WorkflowLedger,
};
use writ_broker::writ_kernel::contracts::StateRevision;
use writ_broker::writ_kernel::json::{self, Map, Value};
use writ_broker::writ_kernel::policy::load_bundle;
use writ_ledger_sqlite::{SqliteEffectStore, SqliteLedger};

/// The frozen M1 transition bundle (the only fixture bundle with the
/// scenarios' workflow protection).
const TRANSITION_BUNDLE: &str =
    include_str!("../../../m1/fixtures/transition/m1-transition.bundle.json");
/// Test-only ingress key the adapter seals transition requests with.
const TRANSITION_INGRESS_KEY: [u8; 32] = [0x5a; 32];
/// Test-only permit key for transition scenarios (they have none).
const TRANSITION_PERMIT_KEY: [u8; 32] = [0x6b; 32];
/// Test-only administrative channel and credential.
const ADMIN_CHANNEL: &str = "m3-closure-admin";
const ADMIN_OPERATOR: &str = "operator:m3-closure";
const ADMIN_CREDENTIAL: &[u8] = b"m3-closure-test-operator";
const TEST_ISSUER: &str = "m3-closure-test-authority";

static CASE_COUNTER: AtomicU64 = AtomicU64::new(0);

// ---------------------------------------------------------------------------
// JSON helpers

fn text(v: &Value, key: &str) -> Option<String> {
    v.get(key).and_then(Value::as_str).map(str::to_string)
}

fn refused(code: &str) -> Value {
    json::obj([("code", json::s(code)), ("result", json::s("refused"))])
}

fn result(r: &str) -> Value {
    json::obj([("result", json::s(r))])
}

fn int(n: u64) -> Value {
    Value::Int(i64::try_from(n).unwrap_or(i64::MAX))
}

fn malformed() -> Value {
    json::obj([("input_error", json::s("INPUT_MALFORMED"))])
}

/// The structured answer for a step the production API cannot express.
fn unsupported(
    step: Option<usize>,
    op: &str,
    reason: &str,
    detail: &str,
    steps: Vec<Value>,
) -> Value {
    let step = step.map_or(Value::Null, |i| int(i as u64));
    json::obj([
        (
            "unsupported",
            json::obj([
                ("detail", json::s(detail)),
                ("op", json::s(op)),
                ("reason", json::s(reason)),
                ("step", step),
            ]),
        ),
        ("steps", Value::Arr(steps)),
    ])
}

fn unhex(s: &str) -> Option<Vec<u8>> {
    if !s.len().is_multiple_of(2) {
        return None;
    }
    (0..s.len())
        .step_by(2)
        .map(|i| s.get(i..i + 2).and_then(|b| u8::from_str_radix(b, 16).ok()))
        .collect()
}

fn adapter_error(detail: &str) -> Value {
    json::obj([("adapter_error", json::s(detail))])
}

// ---------------------------------------------------------------------------
// Trusted time (injected test clock)

struct TestClock(Mutex<Timestamp>);

impl TestClock {
    fn set(&self, at: Timestamp) {
        if let Ok(mut now) = self.0.lock() {
            *now = at;
        }
    }
}

impl EffectiveClock for TestClock {
    fn read(&self) -> Result<Reading, ClockError> {
        let now = *self.0.lock().map_err(|_| ClockError::Source)?;
        Ok(Reading {
            effective: now,
            wall: None,
            drift: Drift::Normal,
            drift_changed: false,
        })
    }
}

// ---------------------------------------------------------------------------
// The production ledger behind a test-only storage-boundary fault wrapper

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Point {
    Reserve,
    Dispatch,
    Observe,
    Promote,
}

struct Ledger {
    inner: SqliteLedger,
    armed: Mutex<Option<Point>>,
    dead: AtomicBool,
    down: AtomicBool,
}

fn store_down(name: &str) -> StoreError {
    StoreError::Unavailable {
        category: format!("transaction {name}"),
        code: "io".into(),
    }
}

impl Ledger {
    fn new(inner: SqliteLedger) -> Ledger {
        Ledger {
            inner,
            armed: Mutex::new(None),
            dead: AtomicBool::new(false),
            down: AtomicBool::new(false),
        }
    }

    fn arm(&self, point: Option<Point>) {
        if let Ok(mut armed) = self.armed.lock() {
            *armed = point;
        }
    }

    fn crashed(&self) -> bool {
        self.dead.load(Ordering::SeqCst)
    }

    /// Gate a lifecycle transaction `name` at `point`.
    fn gate(&self, name: &str, point: Option<Point>) -> Result<(), TxFailure> {
        let fail = || TxFailure::new(TxStage::Begin, store_down(name));
        if self.crashed() || self.down.load(Ordering::SeqCst) {
            return Err(fail());
        }
        if let (Some(p), Ok(mut armed)) = (point, self.armed.lock()) {
            if *armed == Some(p) {
                *armed = None;
                self.dead.store(true, Ordering::SeqCst);
                return Err(fail());
            }
        }
        Ok(())
    }

    /// Gate a write that stays available while the ledger is `down`.
    fn alive(&self, name: &str) -> Result<(), StoreError> {
        if self.crashed() {
            Err(store_down(name))
        } else {
            Ok(())
        }
    }
}

impl GrantLedger for Ledger {
    fn insert_grant(&self, grant: &NewGrant) -> Result<GrantInsert, StoreError> {
        self.gate("approve", None).map_err(|f| f.error)?;
        self.inner.insert_grant(grant)
    }

    fn reserve(
        &self,
        request: &ReserveRequest<'_>,
    ) -> Result<Result<ReserveOutcome, Refusal>, TxFailure> {
        self.gate("reserve", Some(Point::Reserve))?;
        self.inner.reserve(request)
    }
}

impl ExecuteLedger for Ledger {
    fn dispatch(
        &self,
        intent: &DispatchIntent<'_>,
    ) -> Result<Result<Dispatched, Refusal>, TxFailure> {
        self.gate("dispatch", Some(Point::Dispatch))?;
        self.inner.dispatch(intent)
    }

    fn observe_effect(&self, observation: &Observation<'_>) -> Result<(), TxFailure> {
        self.gate("observe", Some(Point::Observe))?;
        self.inner.observe_effect(observation)
    }

    fn pending_receipts(&self, gate: &ExclusiveHold<'_>) -> Result<Vec<PendingReceipt>, TxFailure> {
        self.gate("reconcile_scan", None)?;
        self.inner.pending_receipts(gate)
    }

    fn reconcile(
        &self,
        gate: &ExclusiveHold<'_>,
        resolutions: &[Resolution],
    ) -> Result<Reconciled, TxFailure> {
        self.gate("reconcile", None)?;
        self.inner.reconcile(gate, resolutions)
    }

    fn record_failure(&self, code: &str, record: &FailureRecord) -> Result<(), StoreError> {
        self.alive("audit")?;
        self.inner.record_failure(code, record)
    }
}

impl WorkflowLedger for Ledger {
    fn workflow(&self, key: &WorkflowKey) -> Result<Option<StoredWorkflow>, StoreError> {
        self.alive("read")?;
        self.inner.workflow(key)
    }

    fn promotion_receipt(
        &self,
        tenant_id: &str,
        idempotency_key: &str,
    ) -> Result<Option<(String, PromotionOutcome)>, StoreError> {
        self.alive("read")?;
        self.inner.promotion_receipt(tenant_id, idempotency_key)
    }

    fn administer(&self, change: &AdminChange) -> Result<Result<(), Refusal>, TxFailure> {
        self.gate("admin", None)?;
        self.inner.administer(change)
    }

    fn promote(
        &self,
        intent: &PromotionIntent<'_>,
    ) -> Result<Result<PromotionOutcome, Refusal>, TxFailure> {
        self.gate("promote", Some(Point::Promote))?;
        self.inner.promote(intent)
    }
}

impl LedgerStore for &Ledger {
    fn owner_epoch(&self) -> u64 {
        LedgerStore::owner_epoch(&&self.inner)
    }

    fn record_refusal(&mut self, code: &str) -> Result<(), StoreError> {
        self.alive("audit")?;
        let mut inner: &SqliteLedger = &self.inner;
        inner.record_refusal(code)
    }

    fn time_floor(&self) -> Result<Option<Timestamp>, StoreError> {
        self.alive("read")?;
        LedgerStore::time_floor(&&self.inner)
    }

    fn raise_time_floor(&mut self, at: Timestamp) -> Result<Timestamp, StoreError> {
        self.alive("time_floor")?;
        let mut inner: &SqliteLedger = &self.inner;
        inner.raise_time_floor(at)
    }

    fn record_clock_event(&mut self, event: &ClockEvent) -> Result<(), StoreError> {
        self.alive("clock_event")?;
        let mut inner: &SqliteLedger = &self.inner;
        inner.record_clock_event(event)
    }
}

// ---------------------------------------------------------------------------
// Test authorities and trusted sources

/// Authenticates exactly the fixture's channel credentials; every
/// authenticated principal is reported as a human (the fixtures' model).
struct ChannelAuthority {
    channels: Value,
}

impl ApprovalAuthority for ChannelAuthority {
    fn authenticate(
        &self,
        p: &ApprovalPresentation<'_>,
    ) -> Result<AuthenticatedApprover, ApproverRefusal> {
        let channel = self
            .channels
            .get(p.channel)
            .ok_or(ApproverRefusal::UntrustedChannel)?;
        let expected = channel
            .get(p.approver)
            .and_then(Value::as_str)
            .ok_or(ApproverRefusal::Unauthenticated)?;
        if expected.as_bytes() != p.credential {
            return Err(ApproverRefusal::Unauthenticated);
        }
        Ok(AuthenticatedApprover {
            id: p.approver.to_string(),
            kind: PrincipalKind::Human,
            channel: p.channel.to_string(),
            issuer: TEST_ISSUER.to_string(),
            evidence_ref: format!(
                "test-authentication:{}",
                CASE_COUNTER.load(Ordering::SeqCst)
            ),
        })
    }
}

struct TestOperators;

impl WorkflowAuthority for TestOperators {
    fn authorize(
        &self,
        p: &AdminPresentation<'_>,
        _operation: AdminOperation,
        _tenant_id: &str,
        _project_id: &str,
    ) -> Result<AuthorizedOperator, AdminRefusal> {
        if p.channel != ADMIN_CHANNEL
            || p.operator != ADMIN_OPERATOR
            || p.credential != ADMIN_CREDENTIAL
        {
            return Err(AdminRefusal::Unauthenticated);
        }
        Ok(AuthorizedOperator {
            id: ADMIN_OPERATOR.to_string(),
            issuer: TEST_ISSUER.to_string(),
            channel: ADMIN_CHANNEL.to_string(),
            evidence_ref: "test-operator-authentication".to_string(),
        })
    }
}

/// Confirms exactly the (snapshot id, digest) pairs the scenario declares.
struct TestSnapshots(BTreeSet<(String, String)>);

impl SnapshotStore for TestSnapshots {
    fn check(
        &self,
        _tenant_id: &str,
        _project_id: &str,
        snapshot_id: &str,
        digest: &str,
    ) -> Result<SnapshotCheck, SourceUnavailable> {
        if self
            .0
            .contains(&(snapshot_id.to_string(), digest.to_string()))
        {
            Ok(SnapshotCheck::Matches)
        } else if self.0.iter().any(|(id, _)| id == snapshot_id) {
            Ok(SnapshotCheck::Differs)
        } else {
            Ok(SnapshotCheck::Unknown)
        }
    }
}

/// The fixtures supply no authenticated record or attestation.
struct NoEvidence;

impl AcceptanceRecordSource for NoEvidence {
    fn record(&self, _t: &str, _p: &str, _id: &str) -> Result<Option<Value>, SourceUnavailable> {
        Ok(None)
    }
}

impl AttestationSource for NoEvidence {
    fn attestation(
        &self,
        _t: &str,
        _p: &str,
        _id: &str,
    ) -> Result<Option<Value>, SourceUnavailable> {
        Ok(None)
    }
}

// ---------------------------------------------------------------------------
// One case: production ledger, effect store and broker inputs

struct Stores {
    ledger: Ledger,
    effects: SqliteEffectStore,
}

struct World {
    dir: PathBuf,
    stores: Option<Stores>,
    active: ActivePolicy,
    clock: TestClock,
    permit_key: PermitKey,
    ring: MemoryKeyRing,
}

type ProdBroker<'a> = Broker<'a, Ledger, SqliteEffectStore>;

impl World {
    fn create(
        policy: PolicyState,
        now: Timestamp,
        permit_key: PermitKey,
        ring: MemoryKeyRing,
    ) -> Result<World, String> {
        let n = CASE_COUNTER.fetch_add(1, Ordering::SeqCst);
        let dir = std::env::temp_dir().join(format!("writ-m3-closure-{}-{n}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).map_err(|_| "case directory".to_string())?;
        let ledger = SqliteLedger::create(&dir.join("ledger.db"))
            .map_err(|e| format!("ledger create: {e}"))?;
        let effects = SqliteEffectStore::create(&dir.join("effects.db"), ledger.ledger_id())
            .map_err(|e| format!("effects create: {e}"))?;
        Ok(World {
            dir,
            stores: Some(Stores {
                ledger: Ledger::new(ledger),
                effects,
            }),
            active: ActivePolicy::new(policy),
            clock: TestClock(Mutex::new(now)),
            permit_key,
            ring,
        })
    }

    fn stores(&self) -> Result<&Stores, String> {
        self.stores
            .as_ref()
            .ok_or_else(|| "stores closed".to_string())
    }

    fn broker(&self) -> Result<ProdBroker<'_>, String> {
        let s = self.stores()?;
        Ok(Broker::new(
            &self.active,
            &self.clock,
            &s.ledger,
            &s.effects,
            &self.permit_key,
        ))
    }

    /// Close the ledger and effect store and reopen them (D-020 restart).
    fn restart(&mut self) -> Result<(), String> {
        self.stores = None;
        let ledger = SqliteLedger::open(&self.dir.join("ledger.db"))
            .map_err(|e| format!("ledger reopen: {e}"))?;
        let effects = SqliteEffectStore::open(&self.dir.join("effects.db"), ledger.ledger_id())
            .map_err(|e| format!("effects reopen: {e}"))?;
        self.stores = Some(Stores {
            ledger: Ledger::new(ledger),
            effects,
        });
        Ok(())
    }

    fn audit(&self, code: &str) {
        if let Ok(s) = self.stores() {
            let mut l: &Ledger = &s.ledger;
            let _ = l.record_refusal(code);
        }
    }

    fn reader(&self, file: &str) -> Result<Connection, String> {
        Connection::open_with_flags(self.dir.join(file), OpenFlags::SQLITE_OPEN_READ_ONLY)
            .map_err(|e| format!("read {file}: {e}"))
    }
}

impl Drop for World {
    fn drop(&mut self) {
        self.stores = None;
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

fn count(c: &Connection, sql: &str) -> Result<u64, String> {
    c.query_row(sql, [], |r| r.get::<_, i64>(0))
        .map(|n| u64::try_from(n).unwrap_or(0))
        .map_err(|e| format!("{sql}: {e}"))
}

fn pairs(c: &Connection, sql: &str) -> Result<Vec<(String, i64)>, String> {
    let mut st = c.prepare(sql).map_err(|e| format!("{sql}: {e}"))?;
    let rows = st
        .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?)))
        .map_err(|e| format!("{sql}: {e}"))?;
    rows.collect::<Result<Vec<_>, _>>()
        .map_err(|e| format!("{sql}: {e}"))
}

fn strings(c: &Connection, sql: &str) -> Result<Vec<String>, String> {
    let mut st = c.prepare(sql).map_err(|e| format!("{sql}: {e}"))?;
    let rows = st
        .query_map([], |r| r.get::<_, String>(0))
        .map_err(|e| format!("{sql}: {e}"))?;
    rows.collect::<Result<Vec<_>, _>>()
        .map_err(|e| format!("{sql}: {e}"))
}

// ---------------------------------------------------------------------------
// broker-scenario (m1/fixtures/broker/BROKER.md)

const BROKER_STATE: [&str; 7] = [
    "now",
    "state_revision",
    "bundle",
    "ingress_keys",
    "permit_key",
    "channels",
    "dependencies",
];
const BROKER_FAULTS: [&str; 4] = [
    "crash_before_reserve",
    "crash_after_reserve",
    "crash_after_dispatch",
    "effect_fails",
];
const DEPENDENCIES: [&str; 2] = ["policy", "ledger"];

fn dependency_status(v: &Value) -> bool {
    matches!(v.as_str(), Some("up" | "down"))
}

fn broker_step_usable(op: &Value) -> bool {
    if op.as_obj().is_none()
        || op
            .get("fault")
            .is_some_and(|f| !f.as_str().is_some_and(|f| BROKER_FAULTS.contains(&f)))
    {
        return false;
    }
    match op.get("op").and_then(Value::as_str) {
        Some("concurrent") => op
            .get("operations")
            .and_then(Value::as_arr)
            .is_some_and(|ops| ops.iter().all(broker_step_usable)),
        Some("set_dependency") => {
            op.get("name")
                .and_then(Value::as_str)
                .is_some_and(|n| DEPENDENCIES.contains(&n))
                && op.get("status").is_some_and(dependency_status)
        }
        _ => true,
    }
}

fn broker_input_usable(input: &Value) -> bool {
    let initial_ok = input.get("initial").is_some_and(|i| {
        i.as_obj().is_some()
            && BROKER_STATE.iter().all(|k| i.get(k).is_some())
            && i.get("dependencies")
                .and_then(Value::as_obj)
                .is_some_and(|d| {
                    d.iter()
                        .all(|(k, v)| DEPENDENCIES.contains(&k.as_str()) && dependency_status(v))
                        && DEPENDENCIES.iter().all(|n| d.contains_key(*n))
                })
    });
    let steps_ok = input
        .get("steps")
        .and_then(Value::as_arr)
        .is_some_and(|ops| ops.iter().all(broker_step_usable));
    initial_ok && steps_ok
}

fn policy_state(bundle: Option<&Value>) -> PolicyState {
    match bundle.map(load_bundle) {
        Some(Ok(b)) => PolicyState::loaded(b),
        _ => PolicyState::Rejected,
    }
}

struct BrokerCase {
    world: World,
    bundle: Option<Value>,
    policy_up: bool,
    revision: StateRevision,
    authority: ChannelAuthority,
    ingress: BTreeMap<String, Vec<u8>>,
    permits: BTreeMap<usize, Permit>,
}

enum Step {
    Done(Value),
    Unsupported(&'static str, String),
}

fn outcome_key(v: &Value) -> String {
    if v.get("replayed") == Some(&Value::Bool(true)) {
        return "replayed".into();
    }
    match v.get("result").and_then(Value::as_str) {
        Some("refused") => text(v, "code").unwrap_or_default(),
        Some(r) => r.to_string(),
        None => String::new(),
    }
}

fn executed(r: &Result<Executed, Refusal>) -> Value {
    match r {
        Ok(Executed::EffectCompleted) => result("effect_completed"),
        Ok(Executed::EffectFailed) => result("effect_failed"),
        Ok(Executed::Replayed(Replay::EffectCompleted)) => json::obj([
            ("replayed", Value::Bool(true)),
            ("result", json::s("effect_completed")),
        ]),
        Ok(Executed::Replayed(Replay::EffectFailed)) => json::obj([
            ("replayed", Value::Bool(true)),
            ("result", json::s("effect_failed")),
        ]),
        Err(refusal) => refused(refusal.code()),
    }
}

impl BrokerCase {
    fn new(initial: &Value) -> Result<BrokerCase, String> {
        let now = initial
            .get("now")
            .and_then(Value::as_str)
            .and_then(Timestamp::parse_rfc3339)
            .ok_or("now")?;
        let revision = initial
            .get("state_revision")
            .and_then(Value::as_int)
            .and_then(StateRevision::new)
            .ok_or("state_revision")?;
        let permit_key = initial
            .get("permit_key")
            .and_then(Value::as_str)
            .and_then(unhex)
            .and_then(|k| PermitKey::from_bytes(k).ok())
            .ok_or("permit_key")?;
        let mut ring = MemoryKeyRing::new();
        let mut ingress = BTreeMap::new();
        for (adapter, key) in initial
            .get("ingress_keys")
            .and_then(Value::as_obj)
            .ok_or("ingress_keys")?
        {
            let bytes = key.as_str().and_then(unhex).ok_or("ingress key")?;
            let key =
                IngressKey::from_bytes(bytes.clone()).map_err(|e| format!("ingress key: {e}"))?;
            ring.register(adapter, key);
            ingress.insert(adapter.clone(), bytes);
        }
        let deps = initial.get("dependencies");
        let policy_up = deps.and_then(|d| d.get("policy")).and_then(Value::as_str) != Some("down");
        let ledger_up = deps.and_then(|d| d.get("ledger")).and_then(Value::as_str) != Some("down");
        let bundle = initial.get("bundle").cloned();
        let policy = if policy_up {
            policy_state(bundle.as_ref())
        } else {
            PolicyState::Absent
        };
        let world = World::create(policy, now, permit_key, ring)?;
        world
            .stores()?
            .ledger
            .down
            .store(!ledger_up, Ordering::SeqCst);
        Ok(BrokerCase {
            world,
            bundle,
            policy_up,
            revision,
            authority: ChannelAuthority {
                channels: initial.get("channels").cloned().unwrap_or(Value::Null),
            },
            ingress,
            permits: BTreeMap::new(),
        })
    }

    fn reactivate(&self) {
        let state = if self.policy_up {
            policy_state(self.bundle.as_ref())
        } else {
            PolicyState::Absent
        };
        self.world.active.activate(state);
    }

    /// `execute` without faults on a shared broker (also used by `concurrent`).
    fn execute(
        &self,
        broker: &ProdBroker<'_>,
        op: &Value,
    ) -> Result<(Value, Option<Permit>), Step> {
        let req = op.get("request").cloned().unwrap_or(Value::Null);
        let verified = match verify_ingress(&req, op.get("seal"), &self.world.ring) {
            Ok(v) => v,
            Err(r) => {
                self.world.audit(r.code());
                return Ok((refused(r.code()), None));
            }
        };
        let label = match op.get("grant_ref") {
            None => None,
            Some(g) => Some(g.as_str().and_then(GrantLabel::new).ok_or_else(|| {
                Step::Unsupported(
                    "GRANT_LABEL_NOT_REPRESENTABLE",
                    "grant_ref is not a production grant label".into(),
                )
            })?),
        };
        let exec = broker.execute(&verified, self.revision, label.as_ref());
        Ok((executed(&exec.result), exec.permit))
    }

    fn approve(&self, broker: &ProdBroker<'_>, op: &Value) -> Step {
        let req = op.get("for_request").cloned().unwrap_or(Value::Null);
        let issuer = req
            .get("principal")
            .and_then(|p| p.get("verified_by"))
            .and_then(Value::as_str)
            .unwrap_or("");
        let Some(key) = self
            .ingress
            .get(issuer)
            .and_then(|k| IngressKey::from_bytes(k.clone()).ok())
        else {
            return Step::Unsupported(
                "APPROVAL_REQUEST_NOT_SEALABLE",
                "for_request names no registered ingress adapter".into(),
            );
        };
        let Some(seal) = Seal::create(&req, issuer, &key) else {
            return Step::Unsupported(
                "APPROVAL_REQUEST_NOT_SEALABLE",
                "for_request cannot be sealed".into(),
            );
        };
        let verified = match verify_ingress(&req, Some(&seal.to_value()), &self.world.ring) {
            Ok(v) => v,
            Err(_) => {
                return Step::Unsupported(
                    "APPROVAL_REQUEST_NOT_SEALABLE",
                    "sealed for_request did not verify".into(),
                )
            }
        };
        let verdict = match op.get("verdict").and_then(Value::as_str) {
            Some("approved") => Verdict::Approved,
            Some("denied") => Verdict::Denied,
            _ => {
                return Step::Unsupported(
                    "VERDICT_NOT_REPRESENTABLE",
                    "verdict is neither approved nor denied".into(),
                )
            }
        };
        let Some(label) = op
            .get("grant_ref")
            .and_then(Value::as_str)
            .and_then(GrantLabel::new)
        else {
            return Step::Unsupported(
                "GRANT_LABEL_NOT_REPRESENTABLE",
                "grant_ref is not a production grant label".into(),
            );
        };
        let ttl = match op.get("ttl_seconds") {
            None => None,
            Some(v) => match v.as_int().and_then(|n| u32::try_from(n).ok()) {
                Some(n) => Some(n),
                None => {
                    return Step::Unsupported(
                        "TTL_NOT_REPRESENTABLE",
                        "ttl_seconds outside u32".into(),
                    )
                }
            },
        };
        let channel = text(op, "channel").unwrap_or_default();
        let human = text(op, "human").unwrap_or_default();
        let credential = text(op, "credential").unwrap_or_default();
        let approval = Approval {
            request: &verified,
            state_revision: self.revision,
            label: &label,
            verdict,
            ttl_seconds: ttl,
        };
        let presentation = ApprovalPresentation {
            channel: &channel,
            approver: &human,
            credential: credential.as_bytes(),
        };
        Step::Done(
            match broker
                .grants()
                .approve(&approval, &presentation, &self.authority)
            {
                Ok(_) => result("grant_issued"),
                Err(r) => refused(r.code()),
            },
        )
    }

    fn present_permit(&self, broker: &ProdBroker<'_>, op: &Value) -> Step {
        let step = op
            .get("of_step")
            .and_then(Value::as_int)
            .and_then(|n| usize::try_from(n).ok());
        let Some(permit) = step.and_then(|i| self.permits.get(&i)) else {
            return Step::Unsupported(
                "NO_PERMIT_TO_PRESENT",
                "the named step issued no permit; production has no operation that presents an absent permit (PERMIT_MISSING is not a production answer)".into(),
            );
        };
        let permit = match op.get("tamper").and_then(Value::as_str) {
            None => permit.clone(),
            Some(kind) => {
                let mut v = permit.to_value();
                let Value::Obj(m) = &mut v else {
                    return Step::Unsupported(
                        "PERMIT_NOT_TAMPERABLE",
                        "permit is not an object".into(),
                    );
                };
                match kind {
                    "mac" => {
                        if let Some(Value::Str(mac)) = m.get_mut("mac") {
                            let flipped = if mac.starts_with('0') { "1" } else { "0" };
                            mac.replace_range(0..1, flipped);
                        }
                    }
                    "expiry" => {
                        let later = m
                            .get("expires_at")
                            .and_then(Value::as_str)
                            .and_then(Timestamp::parse_rfc3339)
                            .map(|t| t.plus_seconds(3600).to_string());
                        if let Some(later) = later {
                            m.insert("expires_at".into(), json::s(&later));
                        }
                    }
                    _ => return Step::Unsupported("TAMPER_UNKNOWN", kind.to_string()),
                }
                match Permit::from_value(&v) {
                    Ok(p) => p,
                    Err(_) => {
                        return Step::Unsupported(
                            "PERMIT_NOT_TAMPERABLE",
                            "tampered permit does not parse".into(),
                        )
                    }
                }
            }
        };
        let req = op.get("request").cloned().unwrap_or(Value::Null);
        Step::Done(refused(broker.present_permit(&permit, &req).code()))
    }

    fn step(&mut self, i: usize, op: &Value) -> Result<Step, String> {
        let name = op.get("op").and_then(Value::as_str).unwrap_or("");
        Ok(match name {
            "execute" => {
                let fault = op.get("fault").and_then(Value::as_str);
                let point = match fault {
                    Some("crash_before_reserve") => Some(Point::Reserve),
                    Some("crash_after_reserve") => Some(Point::Dispatch),
                    Some("crash_after_dispatch") => Some(Point::Observe),
                    _ => None,
                };
                let stores = self.world.stores()?;
                stores.ledger.arm(point);
                stores.effects.set_effects_fail(fault == Some("effect_fails"));
                let outcome = {
                    let broker = self.world.broker()?;
                    self.execute(&broker, op)
                };
                let stores = self.world.stores()?;
                stores.ledger.arm(None);
                stores.effects.set_effects_fail(false);
                let crashed = stores.ledger.crashed();
                match outcome {
                    Err(s) => s,
                    Ok((value, permit)) => {
                        if let Some(p) = permit {
                            self.permits.insert(i, p);
                        }
                        if crashed {
                            self.world.restart()?;
                            Step::Done(result("crashed"))
                        } else {
                            Step::Done(value)
                        }
                    }
                }
            }
            "approve" => self.approve(&self.world.broker()?, op),
            "present_permit" => self.present_permit(&self.world.broker()?, op),
            "apply_without_permit" => Step::Unsupported(
                "NO_PRODUCTION_OPERATION",
                "production has no effect path without a permit; the M1 apply_without_permit operation has no counterpart (execute.rs production differences)".into(),
            ),
            "reconcile" => Step::Done(match self.world.broker()?.reconcile() {
                Ok(r) => json::obj([("count", int(r.changed())), ("result", json::s("reconciled"))]),
                Err(r) => refused(r.code()),
            }),
            "concurrent" => self.concurrent(op)?,
            "restart_broker" => {
                self.world.restart()?;
                Step::Done(result("restarted"))
            }
            "activate_bundle" => {
                self.bundle = op.get("bundle").cloned();
                self.reactivate();
                Step::Done(result("bundle_activated"))
            }
            "advance_time" => match op.get("now").and_then(Value::as_str).and_then(Timestamp::parse_rfc3339) {
                Some(t) => {
                    self.world.clock.set(t);
                    Step::Done(result("time_advanced"))
                }
                None => Step::Unsupported("TIME_NOT_REPRESENTABLE", "now does not parse".into()),
            },
            "set_dependency" => {
                let up = op.get("status").and_then(Value::as_str) == Some("up");
                match op.get("name").and_then(Value::as_str) {
                    Some("policy") => {
                        self.policy_up = up;
                        self.reactivate();
                    }
                    Some("ledger") => self.world.stores()?.ledger.down.store(!up, Ordering::SeqCst),
                    _ => {}
                }
                Step::Done(result("dependency_set"))
            }
            _ => {
                self.world.audit("OPERATION_UNKNOWN");
                Step::Done(refused("OPERATION_UNKNOWN"))
            }
        })
    }

    fn concurrent(&self, op: &Value) -> Result<Step, String> {
        let ops = op.get("operations").and_then(Value::as_arr).unwrap_or(&[]);
        if let Some(bad) = ops.iter().find(|o| {
            o.get("op").and_then(Value::as_str) != Some("execute") || o.get("fault").is_some()
        }) {
            return Ok(Step::Unsupported(
                "CONCURRENT_OPERATION_NOT_DRIVEN",
                format!(
                    "concurrent groups are driven for unfaulted execute only, got {}",
                    bad.to_json_text()
                ),
            ));
        }
        let broker = self.world.broker()?;
        let barrier = Barrier::new(ops.len());
        let outcomes: Vec<Result<(Value, Option<Permit>), Step>> = std::thread::scope(|scope| {
            let handles: Vec<_> = ops
                .iter()
                .map(|o| {
                    let (broker, barrier) = (&broker, &barrier);
                    scope.spawn(move || {
                        barrier.wait();
                        self.execute(broker, o)
                    })
                })
                .collect();
            handles
                .into_iter()
                .map(|h| {
                    h.join().unwrap_or_else(|_| {
                        Err(Step::Unsupported("WORKER_PANICKED", String::new()))
                    })
                })
                .collect()
        });
        let mut tally: BTreeMap<String, u64> = BTreeMap::new();
        for o in outcomes {
            match o {
                Ok((v, _)) => *tally.entry(outcome_key(&v)).or_default() += 1,
                Err(s) => return Ok(s),
            }
        }
        let outcomes: Map = tally.into_iter().map(|(k, n)| (k, int(n))).collect();
        Ok(Step::Done(json::obj([
            ("outcomes", Value::Obj(outcomes)),
            ("result", json::s("concurrent")),
        ])))
    }

    fn final_state(&self) -> Result<Value, String> {
        let l = self.world.reader("ledger.db")?;
        let e = self.world.reader("effects.db")?;
        let grants: Map = pairs(&l, "SELECT label, uses FROM grants")?
            .into_iter()
            .map(|(k, n)| (k, Value::Int(n)))
            .collect();
        let receipts: Map = pairs(&l, "SELECT status, count(*) FROM receipts GROUP BY status")?
            .into_iter()
            .map(|(k, n)| (k, Value::Int(n)))
            .collect();
        Ok(json::obj([
            (
                "consumed_permits",
                int(count(&e, "SELECT count(*) FROM consumed_permits")?),
            ),
            (
                "effects_applied",
                int(count(&e, "SELECT count(*) FROM effects")?),
            ),
            ("grant_uses", Value::Obj(grants)),
            ("receipts", Value::Obj(receipts)),
            (
                "refusals",
                int(count(&l, "SELECT count(*) FROM audit_refusals")?),
            ),
        ]))
    }
}

fn broker_scenario(input: &Value) -> Value {
    if !broker_input_usable(input) {
        return malformed();
    }
    let initial = input.get("initial").cloned().unwrap_or(Value::Null);
    let mut case = match BrokerCase::new(&initial) {
        Ok(c) => c,
        Err(e) => return adapter_error(&e),
    };
    let mut steps = Vec::new();
    for (i, op) in input
        .get("steps")
        .and_then(Value::as_arr)
        .unwrap_or(&[])
        .iter()
        .enumerate()
    {
        match case.step(i, op) {
            Ok(Step::Done(v)) => steps.push(v),
            Ok(Step::Unsupported(reason, detail)) => {
                let name = text(op, "op").unwrap_or_default();
                return unsupported(Some(i), &name, reason, &detail, steps);
            }
            Err(e) => return adapter_error(&e),
        }
    }
    match case.final_state() {
        Ok(f) => json::obj([("final", f), ("steps", Value::Arr(steps))]),
        Err(e) => adapter_error(&e),
    }
}

// ---------------------------------------------------------------------------
// transition-scenario (m1/fixtures/transition/SCENARIOS.md)

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

struct TransitionCase {
    world: World,
    key: WorkflowKey,
    snapshots: TestSnapshots,
}

fn admin(
    world: &World,
    snapshots: &TestSnapshots,
    request: AdminRequest,
) -> Result<Result<(), Refusal>, String> {
    let presentation = AdminPresentation {
        channel: ADMIN_CHANNEL,
        operator: ADMIN_OPERATOR,
        credential: ADMIN_CREDENTIAL,
    };
    Ok(world
        .broker()?
        .administer(&TestOperators, &presentation, snapshots, request))
}

enum Seeded {
    Ready(Box<TransitionCase>),
    Unsupported(Value),
}

fn seed(initial: &Value) -> Result<Seeded, String> {
    let not = |reason: &str, detail: &str, refusal: Option<&str>| {
        let mut v = unsupported(None, "initial", reason, detail, Vec::new());
        if let (Some(r), Value::Obj(m)) = (refusal, &mut v) {
            if let Some(Value::Obj(u)) = m.get_mut("unsupported") {
                u.insert("production_refusal".into(), json::s(r));
            }
        }
        Ok(Seeded::Unsupported(v))
    };
    let bundle_doc =
        json::parse(TRANSITION_BUNDLE.as_bytes()).map_err(|e| format!("bundle: {}", e.code()))?;
    let bundle = load_bundle(&bundle_doc).map_err(|_| "bundle rejected".to_string())?;
    let Some(protection) = bundle.protection() else {
        return Err("bundle without protection".into());
    };
    // The scenario's protection must be exactly the loaded bundle's protection.
    let fixture = initial.get("protection");
    let same = fixture.is_some_and(|p| {
        let list = |k: &str| p.get(k).and_then(Value::as_arr).map(<[Value]>::to_vec);
        let states: Option<Vec<String>> = list("protected_to_states").map(|v| {
            v.iter()
                .filter_map(|x| x.as_str().map(str::to_string))
                .collect()
        });
        let edges: Option<Vec<(String, String)>> = list("protected_transitions").map(|v| {
            v.iter()
                .filter_map(|e| Some((text(e, "from_state")?, text(e, "to_state")?)))
                .collect()
        });
        states.as_ref() == Some(&protection.protected_to_states)
            && edges
                == Some(
                    protection
                        .protected_transitions
                        .iter()
                        .map(|e| (e.from_state.clone(), e.to_state.clone()))
                        .collect(),
                )
            && text(p, "acceptance_policy_bundle_sha").as_deref()
                == Some(protection.acceptance_policy_bundle_sha.as_str())
            && text(p, "authorization_bundle_sha") == text(initial, "authorization_bundle_sha")
    });
    if !same {
        return not(
            "PROTECTION_NOT_EXPRESSIBLE",
            "the scenario's protection is not the protection of any loadable authorization bundle in the fixtures (production takes protection only from the active bundle)",
            None,
        );
    }
    let now = initial
        .get("now")
        .and_then(Value::as_str)
        .and_then(Timestamp::parse_rfc3339)
        .ok_or("now")?;
    let mut ring = MemoryKeyRing::new();
    let key = IngressKey::from_bytes(TRANSITION_INGRESS_KEY.to_vec()).map_err(|e| e.to_string())?;
    ring.register("writ-host-adapter:fixture", key);
    let permit_key =
        PermitKey::from_bytes(TRANSITION_PERMIT_KEY.to_vec()).map_err(|e| e.to_string())?;
    let world = World::create(PolicyState::loaded(bundle.clone()), now, permit_key, ring)?;
    let field = |k: &str| text(initial, k).unwrap_or_default();
    let key = WorkflowKey {
        tenant_id: field("tenant_id"),
        project_id: field("project_id"),
        workflow_id: field("workflow_id"),
    };
    let snapshots = TestSnapshots(BTreeSet::from([(
        field("artifact_snapshot_id"),
        field("artifact_digest"),
    )]));
    let create = AdminRequest::CreateWorkflow {
        key: key.clone(),
        task_id: field("task_id"),
        initial_state: field("state"),
        artifact_snapshot_id: field("artifact_snapshot_id"),
        artifact_digest: field("artifact_digest"),
    };
    if let Err(r) = admin(&world, &snapshots, create)? {
        return not(
            "INITIAL_STATE_NOT_ESTABLISHABLE",
            "production administration refuses to create the workflow in the scenario's initial state (D-036 item 5); no other production operation sets an arbitrary state",
            Some(r.code()),
        );
    }
    let history = initial
        .get("last_promotion")
        .is_some_and(|v| *v != Value::Null)
        || initial
            .get("consumed_record_ids")
            .and_then(Value::as_arr)
            .is_some_and(|a| !a.is_empty())
        || initial
            .get("revoked_evaluator_ids")
            .and_then(Value::as_arr)
            .is_some_and(|a| !a.is_empty());
    if history {
        return not(
            "INITIAL_HISTORY_NOT_ESTABLISHABLE",
            "the scenario starts with a prior promotion, consumed records or revoked evaluators, which only earlier production operations with evidence the fixture does not supply could create",
            None,
        );
    }
    let mut prerequisites = Vec::new();
    for (kind, spec) in initial
        .get("effect_prerequisites")
        .and_then(Value::as_obj)
        .into_iter()
        .flatten()
    {
        let states = spec
            .get("required_states")
            .and_then(Value::as_arr)
            .map(|a| {
                a.iter()
                    .filter_map(|s| s.as_str().map(str::to_string))
                    .collect()
            })
            .unwrap_or_default();
        prerequisites.push((kind.clone(), states));
    }
    let activate = AdminRequest::ActivateAcceptancePolicy {
        tenant_id: key.tenant_id.clone(),
        project_id: key.project_id.clone(),
        requirements: initial
            .get("acceptance_requirements")
            .cloned()
            .unwrap_or(Value::Null),
        effect_prerequisites: prerequisites,
    };
    if let Err(r) = admin(&world, &snapshots, activate)? {
        return not(
            "ACCEPTANCE_POLICY_NOT_ESTABLISHABLE",
            "production administration refuses the scenario's acceptance requirements",
            Some(r.code()),
        );
    }
    let revision = initial.get("revision").and_then(Value::as_int).unwrap_or(0);
    for _ in 0..revision {
        let update = AdminRequest::UpdateArtifact {
            key: key.clone(),
            artifact_snapshot_id: field("artifact_snapshot_id"),
            artifact_digest: field("artifact_digest"),
        };
        if let Err(r) = admin(&world, &snapshots, update)? {
            return not(
                "REVISION_NOT_ESTABLISHABLE",
                "production administration refuses the artifact updates that reach the scenario's revision",
                Some(r.code()),
            );
        }
    }
    Ok(Seeded::Ready(Box::new(TransitionCase {
        world,
        key,
        snapshots,
    })))
}

impl TransitionCase {
    fn promote(&mut self, op: &Value) -> Result<Step, String> {
        let req = op.get("request").cloned().unwrap_or(Value::Null);
        let issuer = req
            .get("principal")
            .and_then(|p| p.get("verified_by"))
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let key =
            IngressKey::from_bytes(TRANSITION_INGRESS_KEY.to_vec()).map_err(|e| e.to_string())?;
        let verified = match Seal::create(&req, &issuer, &key)
            .ok_or(Refusal::IngressUnauthenticated)
            .and_then(|seal| verify_ingress(&req, Some(&seal.to_value()), &self.world.ring))
        {
            Ok(v) => v,
            Err(r) => {
                self.world.audit(r.code());
                return Ok(Step::Done(refused(r.code())));
            }
        };
        let crash = op.get("fault").and_then(Value::as_str) == Some("crash_before_commit");
        let stores = self.world.stores()?;
        stores.ledger.arm(crash.then_some(Point::Promote));
        let record_keys: MemoryKeys<AcceptanceRecordKey> = MemoryKeys::new();
        let runner_keys: MemoryKeys<RunnerAttestationKey> = MemoryKeys::new();
        let trust = Trust {
            records: &NoEvidence,
            attestations: &NoEvidence,
            record_keys: &record_keys,
            runner_keys: &runner_keys,
            snapshots: &self.snapshots,
        };
        let promotion = self.world.broker()?.promote(&trust, &verified, None);
        let stores = self.world.stores()?;
        stores.ledger.arm(None);
        if stores.ledger.crashed() {
            self.world.restart()?;
            return Ok(Step::Done(result("crashed")));
        }
        Ok(Step::Done(match promotion.result {
            Ok(PromotionOutcome::Committed { revision, .. }) => json::obj([
                ("result", json::s("committed")),
                ("revision", Value::Int(revision)),
            ]),
            Ok(PromotionOutcome::Replayed { revision, .. }) => json::obj([
                ("replayed", Value::Bool(true)),
                ("result", json::s("committed")),
                ("revision", Value::Int(revision)),
            ]),
            Err(r) => refused(r.code()),
        }))
    }

    fn step(&mut self, op: &Value) -> Result<Step, String> {
        let name = op.get("op").and_then(Value::as_str).unwrap_or("");
        let gap =
            |reason: &'static str, detail: &str| Ok(Step::Unsupported(reason, detail.to_string()));
        match name {
            "promote" => self.promote(op),
            "advance_time" => match op.get("now").and_then(Value::as_str).and_then(Timestamp::parse_rfc3339) {
                Some(t) => {
                    self.world.clock.set(t);
                    Ok(Step::Done(result("time_advanced")))
                }
                None => gap("TIME_NOT_REPRESENTABLE", "now does not parse"),
            },
            "reconcile" => gap(
                "CLIENT_ASSERTED_RECONCILIATION_NOT_PROVIDED",
                "reconcile {idempotency_key, observed} asserts an outcome; production reconciles only from adapter evidence (D-036 item 3, D-035 item 3)",
            ),
            "activate_authorization_bundle" => gap(
                "FIXTURE_SUPPLIES_NO_BUNDLE",
                "the step names a bundle digest and protection, not a loadable authorization bundle",
            ),
            "effect" => gap(
                "EFFECT_PERMIT_MODEL",
                "effect carries a client permit and no seal; production effects run through the sealed execute lifecycle",
            ),
            "update_artifact" | "activate_policy" | "revoke_evaluator" | "concurrent" | "promote_concurrent" => gap(
                "NOT_DRIVEN_BY_ADAPTER",
                "mappable to production administration or threads, but no seedable frozen case uses it; not implemented in this adapter revision",
            ),
            _ => {
                self.world.audit("OPERATION_UNKNOWN");
                Ok(Step::Done(refused("OPERATION_UNKNOWN")))
            }
        }
    }

    fn final_state(&self) -> Result<Value, String> {
        let l = self.world.reader("ledger.db")?;
        let (state, revision): (String, i64) = l
            .query_row(
                "SELECT state, revision FROM workflows WHERE tenant_id = ?1 AND project_id = ?2 AND workflow_id = ?3",
                (&self.key.tenant_id, &self.key.project_id, &self.key.workflow_id),
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
        Ok(json::obj([
            (
                "consumed_record_ids",
                Value::Arr(consumed.iter().map(|x| json::s(x)).collect()),
            ),
            ("effects", Value::Obj(effects)),
            (
                "receipts",
                int(count(&l, "SELECT count(*) FROM promotion_receipts")?),
            ),
            (
                "refusals",
                int(count(&l, "SELECT count(*) FROM audit_refusals")?),
            ),
            ("revision", Value::Int(revision)),
            ("state", json::s(&state)),
        ]))
    }
}

fn transition_scenario(input: &Value) -> Value {
    if !transition_input_usable(input) {
        return malformed();
    }
    let initial = input.get("initial").cloned().unwrap_or(Value::Null);
    let mut case = match seed(&initial) {
        Ok(Seeded::Ready(c)) => c,
        Ok(Seeded::Unsupported(v)) => return v,
        Err(e) => return adapter_error(&e),
    };
    let mut steps = Vec::new();
    for (i, op) in input
        .get("steps")
        .and_then(Value::as_arr)
        .unwrap_or(&[])
        .iter()
        .enumerate()
    {
        match case.step(op) {
            Ok(Step::Done(v)) => steps.push(v),
            Ok(Step::Unsupported(reason, detail)) => {
                let name = text(op, "op").unwrap_or_default();
                return unsupported(Some(i), &name, reason, &detail, steps);
            }
            Err(e) => return adapter_error(&e),
        }
    }
    match case.final_state() {
        Ok(f) => json::obj([("final", f), ("steps", Value::Arr(steps))]),
        Err(e) => adapter_error(&e),
    }
}

// ---------------------------------------------------------------------------
// Line protocol (as kernel/examples/m1_line_adapter.rs)

fn envelope_invalid(id: Option<&str>) -> String {
    let id = id.map_or_else(|| "null".to_string(), |s| json::s(s).to_json_text());
    format!("{{\"id\":{id},\"output\":{{\"error\":\"ENVELOPE_INVALID\"}}}}")
}

fn answer(command: &str, line: &[u8]) -> String {
    let Ok(env) = json::parse(line) else {
        return envelope_invalid(None);
    };
    let Some(map) = env.as_obj() else {
        return envelope_invalid(None);
    };
    let Some(Value::Str(id)) = map.get("id") else {
        return envelope_invalid(None);
    };
    let closed = map
        .keys()
        .all(|k| matches!(k.as_str(), "id" | "input" | "input_text"));
    let parsed = match (map.get("input"), map.get("input_text")) {
        (Some(v), None) if closed => Ok(v.clone()),
        (None, Some(Value::Str(t))) if closed => json::parse(t.as_bytes()),
        _ => return envelope_invalid(Some(id)),
    };
    let out = match parsed {
        Err(e) => json::obj([
            ("input_error", json::s("INPUT_JSON_INVALID")),
            ("json_error", json::s(e.code())),
        ]),
        Ok(input) if command == "broker-scenario" => broker_scenario(&input),
        Ok(input) => transition_scenario(&input),
    };
    format!(
        "{{\"id\":{},\"output\":{}}}",
        json::s(id).to_json_text(),
        out.to_json_text()
    )
}

fn main() -> std::process::ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let command = match args.first().map(String::as_str) {
        Some(c @ ("broker-scenario" | "transition-scenario")) => c.to_string(),
        _ => {
            eprintln!("usage: m3_line_adapter broker-scenario|transition-scenario");
            return std::process::ExitCode::from(2);
        }
    };
    let stdin = io::stdin();
    let mut out = io::BufWriter::new(io::stdout().lock());
    for line in stdin.lock().split(b'\n') {
        let Ok(mut line) = line else {
            return std::process::ExitCode::from(1);
        };
        if line.last() == Some(&b'\r') {
            line.pop();
        }
        if line.iter().all(u8::is_ascii_whitespace) {
            continue;
        }
        if writeln!(out, "{}", answer(&command, &line)).is_err() {
            return std::process::ExitCode::from(1);
        }
    }
    if out.flush().is_err() {
        return std::process::ExitCode::from(1);
    }
    std::process::ExitCode::SUCCESS
}
