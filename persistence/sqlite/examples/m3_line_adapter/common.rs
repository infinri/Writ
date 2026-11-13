//! Shared harness pieces: JSON helpers, the injected clock, the storage-boundary fault
//! wrapper around the production ledger, test authorities, the per-case world and the
//! I-1 adaptation labels.

use std::collections::BTreeSet;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use rusqlite::{Connection, OpenFlags};
use writ_broker::clock::{ClockError, ClockEvent, Drift, EffectiveClock, Reading};
use writ_broker::execute::{
    Broker, DispatchIntent, Dispatched, FailureRecord, Observation, PendingReceipt, Reconciled,
    Resolution,
};
use writ_broker::grants::{
    ApprovalAuthority, ApprovalPresentation, ApproverRefusal, AuthenticatedApprover, GrantInsert,
    NewGrant, PrincipalKind, ReserveOutcome, ReserveRequest,
};
use writ_broker::keys::{MemoryKeyRing, PermitKey};
use writ_broker::policy::{ActivePolicy, ExclusiveHold, PolicyState};
use writ_broker::refusal::Refusal;
use writ_broker::store::{
    ExecuteLedger, GrantLedger, LedgerStore, StoreError, Timestamp, TxFailure, TxStage,
};
use writ_broker::workflow::{
    AdminChange, AdminOperation, AdminPresentation, AdminRefusal, AuthorizedOperator,
    PromotionIntent, PromotionOutcome, StoredWorkflow, WorkflowAuthority, WorkflowKey,
    WorkflowLedger,
};
use writ_broker::writ_kernel::json::{self, Value};
use writ_ledger_sqlite::{SqliteEffectStore, SqliteLedger};

/// Test-only administrative channel and credential.
pub const ADMIN_CHANNEL: &str = "m3-closure-admin";
pub const ADMIN_OPERATOR: &str = "operator:m3-closure";
pub const ADMIN_CREDENTIAL: &[u8] = b"m3-closure-test-operator";
pub const TEST_ISSUER: &str = "m3-closure-test-authority";

static CASE_COUNTER: AtomicU64 = AtomicU64::new(0);

// ---------------------------------------------------------------------------
// I-1 adaptation labels (closed vocabulary)

pub const C1: &str = "C1-ingress-audit";
pub const C2: &str = "C2-approval-sealing";
pub const C3: &str = "C3-synthetic-human-authority";
pub const C4: &str = "C4-partial-transaction-unavailability";
pub const C5: &str = "C5-injected-crash-reopen";
pub const C6: &str = "C6-controlled-effect-failure";
pub const C7: &str = "C7-protocol-unknown-op";
pub const C8: &str = "C8-protocol-input-validation";
pub const SEEDED: &str = "seeded-preconditions";
pub const HARNESS_BUNDLE: &str = "harness-authorization-bundle";
pub const TEST_EVIDENCE: &str = "test-authenticated-evidence";
pub const SEALED_EFFECT: &str = "sealed-execute-effect";
pub const DIFFERENCE: &str = "production-difference";
pub const PERMIT_DROPPED: &str = "client-permit-dropped";
pub const ADAPTER_SEALED: &str = "adapter-sealed";

/// The adaptations one case actually exercised.
#[derive(Default)]
pub struct Labels(Mutex<BTreeSet<&'static str>>);

impl Labels {
    pub fn add(&self, label: &'static str) {
        if let Ok(mut set) = self.0.lock() {
            set.insert(label);
        }
    }

    pub fn list(&self) -> Vec<&'static str> {
        self.0
            .lock()
            .map(|s| s.iter().copied().collect())
            .unwrap_or_default()
    }
}

// ---------------------------------------------------------------------------
// JSON helpers

pub fn text(v: &Value, key: &str) -> Option<String> {
    v.get(key).and_then(Value::as_str).map(str::to_string)
}

pub fn refused(code: &str) -> Value {
    json::obj([("code", json::s(code)), ("result", json::s("refused"))])
}

pub fn result(r: &str) -> Value {
    json::obj([("result", json::s(r))])
}

pub fn int(n: u64) -> Value {
    Value::Int(i64::try_from(n).unwrap_or(i64::MAX))
}

pub fn malformed() -> Value {
    json::obj([("input_error", json::s("INPUT_MALFORMED"))])
}

pub fn adapter_error(detail: &str) -> Value {
    json::obj([("adapter_error", json::s(detail))])
}

/// The answer for an M1 operation that has no production counterpart (D-037 item 5):
/// never a canned pass; `extra` carries what production actually did instead, if anything.
pub fn production_difference(reason: &str, extra: Vec<(&str, Value)>) -> Value {
    let mut m = json::Map::new();
    m.insert("reason".into(), json::s(reason));
    m.insert("result".into(), json::s("production_difference"));
    for (k, v) in extra {
        m.insert(k.to_string(), v);
    }
    Value::Obj(m)
}

/// The structured answer for a step the production API cannot express.
pub fn unsupported(
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

pub fn refused_setup(case: &str, reason: &str) -> Value {
    json::obj([(
        "refused_setup",
        json::obj([("case", json::s(case)), ("reason", json::s(reason))]),
    )])
}

pub fn unhex(s: &str) -> Option<Vec<u8>> {
    if !s.len().is_multiple_of(2) {
        return None;
    }
    (0..s.len())
        .step_by(2)
        .map(|i| s.get(i..i + 2).and_then(|b| u8::from_str_radix(b, 16).ok()))
        .collect()
}

/// `v` with the member at `path` set to `value` (a non-object on the path is kept).
pub fn with(v: &Value, path: &[&str], value: Value) -> Value {
    match (v, path) {
        (_, []) => value,
        (Value::Obj(m), [k, rest @ ..]) => {
            let mut m = m.clone();
            let inner = m.get(*k).cloned().unwrap_or(Value::Null);
            m.insert((*k).to_string(), with(&inner, rest, value));
            Value::Obj(m)
        }
        _ => v.clone(),
    }
}

// ---------------------------------------------------------------------------
// Trusted time (injected test clock)

pub struct TestClock(Mutex<Timestamp>);

impl TestClock {
    pub fn set(&self, at: Timestamp) {
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
// The production ledger behind a test-only storage-boundary fault wrapper (C4, C5)

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Point {
    Reserve,
    Dispatch,
    Observe,
    Promote,
}

pub struct Ledger {
    inner: SqliteLedger,
    armed: Mutex<Option<Point>>,
    dead: AtomicBool,
    pub down: AtomicBool,
    labels: Arc<Labels>,
}

fn store_down(name: &str) -> StoreError {
    StoreError::Unavailable {
        category: format!("transaction {name}"),
        code: "io".into(),
    }
}

impl Ledger {
    fn new(inner: SqliteLedger, labels: Arc<Labels>) -> Ledger {
        Ledger {
            inner,
            armed: Mutex::new(None),
            dead: AtomicBool::new(false),
            down: AtomicBool::new(false),
            labels,
        }
    }

    pub fn arm(&self, point: Option<Point>) {
        if let Ok(mut armed) = self.armed.lock() {
            *armed = point;
        }
    }

    pub fn crashed(&self) -> bool {
        self.dead.load(Ordering::SeqCst)
    }

    /// Gate a lifecycle transaction `name` at `point`.
    fn gate(&self, name: &str, point: Option<Point>) -> Result<(), TxFailure> {
        let fail = || TxFailure::new(TxStage::Begin, store_down(name));
        if self.crashed() {
            return Err(fail());
        }
        if self.down.load(Ordering::SeqCst) {
            self.labels.add(C4);
            return Err(fail());
        }
        if let (Some(p), Ok(mut armed)) = (point, self.armed.lock()) {
            if *armed == Some(p) {
                *armed = None;
                self.dead.store(true, Ordering::SeqCst);
                self.labels.add(C5);
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
// Test authorities

/// Authenticates exactly the fixture's channel credentials; every authenticated principal
/// is reported as a human (C3: tests the self-approval defense only, never human
/// authentication).
pub struct ChannelAuthority {
    pub channels: Value,
    pub labels: Arc<Labels>,
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
        self.labels.add(C3);
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

pub struct TestOperators;

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

pub const ADMIN: AdminPresentation<'static> = AdminPresentation {
    channel: ADMIN_CHANNEL,
    operator: ADMIN_OPERATOR,
    credential: ADMIN_CREDENTIAL,
};

// ---------------------------------------------------------------------------
// One case: production ledger, effect store and broker inputs

pub struct Stores {
    pub ledger: Ledger,
    pub effects: SqliteEffectStore,
}

pub struct World {
    pub dir: PathBuf,
    stores: Option<Stores>,
    pub active: ActivePolicy,
    pub clock: TestClock,
    permit_key: PermitKey,
    pub ring: MemoryKeyRing,
    pub labels: Arc<Labels>,
}

pub type ProdBroker<'a> = Broker<'a, Ledger, SqliteEffectStore>;

impl World {
    pub fn create(
        policy: PolicyState,
        now: Timestamp,
        permit_key: PermitKey,
        ring: MemoryKeyRing,
        labels: Arc<Labels>,
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
                ledger: Ledger::new(ledger, labels.clone()),
                effects,
            }),
            active: ActivePolicy::new(policy),
            clock: TestClock(Mutex::new(now)),
            permit_key,
            ring,
            labels,
        })
    }

    pub fn stores(&self) -> Result<&Stores, String> {
        self.stores
            .as_ref()
            .ok_or_else(|| "stores closed".to_string())
    }

    pub fn broker(&self) -> Result<ProdBroker<'_>, String> {
        let s = self.stores()?;
        Ok(Broker::new(
            &self.active,
            &self.clock,
            &s.ledger,
            &s.effects,
            &self.permit_key,
        ))
    }

    /// Close the ledger and effect store (no owner remains).
    pub fn close(&mut self) {
        self.stores = None;
    }

    /// Open the closed ledger and effect store with the production `open` (D-020 restart).
    pub fn reopen(&mut self) -> Result<(), String> {
        self.stores = None;
        let ledger = SqliteLedger::open(&self.dir.join("ledger.db"))
            .map_err(|e| format!("ledger reopen: {e}"))?;
        let effects = SqliteEffectStore::open(&self.dir.join("effects.db"), ledger.ledger_id())
            .map_err(|e| format!("effects reopen: {e}"))?;
        self.stores = Some(Stores {
            ledger: Ledger::new(ledger, self.labels.clone()),
            effects,
        });
        Ok(())
    }

    pub fn audit(&self, code: &str) {
        if let Ok(s) = self.stores() {
            let mut l: &Ledger = &s.ledger;
            let _ = l.record_refusal(code);
        }
    }

    /// A read-only connection for inspection, only while every store is closed
    /// (quiescent; never while a broker owns the ledger, D-037 item 7).
    pub fn reader(&self, file: &str) -> Result<Connection, String> {
        if self.stores.is_some() {
            return Err("inspection refused while the stores are open".into());
        }
        Connection::open_with_flags(self.dir.join(file), OpenFlags::SQLITE_OPEN_READ_ONLY)
            .map_err(|e| format!("read {file}: {e}"))
    }

    /// A read-write connection for the test seeder, only while every store is closed.
    pub fn seeder(&self) -> Result<Connection, String> {
        if self.stores.is_some() {
            return Err("seeding refused while the stores are open".into());
        }
        Connection::open_with_flags(
            self.dir.join("ledger.db"),
            OpenFlags::SQLITE_OPEN_READ_WRITE,
        )
        .map_err(|e| format!("seed ledger: {e}"))
    }
}

impl Drop for World {
    fn drop(&mut self) {
        self.stores = None;
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

pub fn count(c: &Connection, sql: &str) -> Result<u64, String> {
    c.query_row(sql, [], |r| r.get::<_, i64>(0))
        .map(|n| u64::try_from(n).unwrap_or(0))
        .map_err(|e| format!("{sql}: {e}"))
}

pub fn pairs(c: &Connection, sql: &str) -> Result<Vec<(String, i64)>, String> {
    let mut st = c.prepare(sql).map_err(|e| format!("{sql}: {e}"))?;
    let rows = st
        .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?)))
        .map_err(|e| format!("{sql}: {e}"))?;
    rows.collect::<Result<Vec<_>, _>>()
        .map_err(|e| format!("{sql}: {e}"))
}

pub fn strings(c: &Connection, sql: &str) -> Result<Vec<String>, String> {
    let mut st = c.prepare(sql).map_err(|e| format!("{sql}: {e}"))?;
    let rows = st
        .query_map([], |r| r.get::<_, String>(0))
        .map_err(|e| format!("{sql}: {e}"))?;
    rows.collect::<Result<Vec<_>, _>>()
        .map_err(|e| format!("{sql}: {e}"))
}
