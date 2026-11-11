//! Broker scenarios (`broker-scenario`, m1/fixtures/broker/BROKER.md):
//! sealed ingress, use-time re-decision, approval grants, idempotent
//! receipts, permits verified by the effect adapter, crashes, reconciliation.
//!
//! Two ledgers give byte-identical outputs: the in-memory model (default;
//! every operation holds the configuration lock exclusively, so it is one
//! serializable transaction) and SQLite (`--ledger sqlite`,
//! LEDGER-SQLITE.md: one `BEGIN IMMEDIATE` transaction per logical step,
//! in-process key locks, one connection per concurrent operation).
//!
//! SIMULATED, not verified:
//! - ingress seals and permit MACs are HMAC-SHA256 under fixture keys held in
//!   scenario state (no key management, rotation or registration);
//! - `fault` injects a crash at a named point (nothing is killed);
//! - an "authenticated human" is a credential match on a fixture channel;
//! - the effect adapter only records consumed permits and counts effects.

use crate::json::{self, obj, s, Value};
use crate::kernel::{self, Bundle, Decision, Policy};
use crate::sqlite::{self, Category, DbError};
use crate::time::{self, Instant};
use crate::timing::{between, since, GroupTiming, ScenarioTiming, StepTiming, WorkerTiming};
use hmac::{Hmac, Mac};
use rusqlite::Connection;
use sha2::Sha256;
use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::PathBuf;
use std::sync::{Arc, Condvar, Mutex, MutexGuard, RwLock, RwLockReadGuard, RwLockWriteGuard};
use std::thread;
use std::time::Instant as Clock;

type HmacSha256 = Hmac<Sha256>;
type Key = (String, String);
type R<T> = Result<T, DbError>;
const PERMIT_TTL_SECONDS: i64 = 30;
const DEFAULT_GRANT_TTL_SECONDS: i64 = 600;

fn unhex(h: &str) -> Option<Vec<u8>> {
    if h.len() % 2 != 0 {
        return None;
    }
    (0..h.len()).step_by(2).map(|i| u8::from_str_radix(h.get(i..i + 2)?, 16).ok()).collect()
}

fn mac_hex(key: &[u8], msg: &[u8]) -> String {
    let mut m = HmacSha256::new_from_slice(key).expect("HMAC accepts any key length");
    m.update(msg);
    json::hex(&m.finalize().into_bytes())
}

fn mac_verify(key: &[u8], msg: &[u8], tag_hex: &str) -> bool {
    let Some(tag) = unhex(tag_hex) else { return false };
    let mut m = HmacSha256::new_from_slice(key).expect("HMAC accepts any key length");
    m.update(msg);
    m.verify_slice(&tag).is_ok()
}

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

#[derive(Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
enum ReceiptStatus {
    Reserved,
    Dispatched,
    UnknownReconcile,
    ObservedSuccess,
    ObservedFailure,
}

impl ReceiptStatus {
    fn name(self) -> &'static str {
        match self {
            ReceiptStatus::Reserved => "reserved",
            ReceiptStatus::Dispatched => "dispatched",
            ReceiptStatus::UnknownReconcile => "unknown_reconcile",
            ReceiptStatus::ObservedSuccess => "observed_success",
            ReceiptStatus::ObservedFailure => "observed_failure",
        }
    }
}

struct Receipt {
    fingerprint: String,
    grant: Option<String>,
    status: ReceiptStatus,
}

/// Approval grant: bindings fixed at approval time.
struct Grant {
    binding: Value,
    expires: Instant,
    max_uses: i64,
    uses: i64,
}

#[derive(Clone)]
struct Permit {
    body: Value,
    mac: String,
}

/// Trusted configuration: never stored in a ledger, survives a restart.
struct Config {
    now: Instant,
    state_revision: i64,
    bundle: Result<Bundle, ()>,
    ingress_keys: Value,
    permit_key: Vec<u8>,
    channels: Value,
    policy_up: bool,
    ledger_up: bool,
}

/// In-memory ledger (the default).
#[derive(Default)]
struct MemLedger {
    grants: BTreeMap<String, Grant>,
    receipts: HashMap<Key, Receipt>,
    consumed_permits: HashSet<String>,
    next_permit: u64,
    effects_applied: i64,
    refusals: i64,
}

/// SQLite ledger: the file, the connection used outside concurrent groups and
/// the volatile per-key locks.
struct SqlLedger {
    path: Option<PathBuf>,
    main: Mutex<Option<Connection>>,
    key_locks: Mutex<HashMap<Key, Arc<Mutex<()>>>>,
    /// Test fault: the next this many worker setups fail.
    #[cfg(test)]
    fail_setups: std::sync::atomic::AtomicUsize,
}

impl SqlLedger {
    fn connect(&self) -> R<Connection> {
        sqlite::open(self.path.as_ref().ok_or_else(|| DbError::new("SQLITE_CANTOPEN").within(Category::Open))?)
    }

    /// A `concurrent` worker's connection (D-027 worker connection setup).
    fn connect_worker(&self) -> R<Connection> {
        #[cfg(test)]
        if self.fail_setups.try_update(std::sync::atomic::Ordering::SeqCst, std::sync::atomic::Ordering::SeqCst, |n| n.checked_sub(1)).is_ok() {
            return Err(DbError::new("SQLITE_CANTOPEN").within(Category::Worker));
        }
        sqlite::open_worker(self.path.as_ref().ok_or_else(|| DbError::new("SQLITE_CANTOPEN").within(Category::Worker))?)
    }
}

enum Ledger {
    Memory(Mutex<MemLedger>),
    Sqlite(SqlLedger),
}

/// `broker-scenario` ledger selection (LEDGER-SQLITE.md "Selecting the ledger").
pub enum LedgerChoice {
    Memory,
    SqliteDir(PathBuf),
    SqliteDb(PathBuf),
}

/// When a `concurrent` worker's connection is set up (LEDGER-SQLITE.md
/// "Concurrency profiles", D-027): `Primary` before the start barrier is
/// released, `Cold` after it, inside the worker's measured operation.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Profile {
    #[default]
    Primary,
    Cold,
}

/// A held coordination gate (D-029); dropping it releases the gate.
enum GateHold<'a> {
    Shared(#[allow(dead_code)] RwLockReadGuard<'a, ()>),
    Exclusive(#[allow(dead_code)] RwLockWriteGuard<'a, ()>),
}

struct Broker {
    profile: Profile,
    /// The process-local coordination gate (LEDGER-SQLITE.md "Broker
    /// coordination gate and state conflicts", D-029): `execute` and
    /// `present_permit` hold it shared, `reconcile` exclusive. Created with
    /// the broker and never replaced (`restart_broker` keeps it).
    gate: RwLock<()>,
    config: RwLock<Config>,
    permits: Mutex<HashMap<usize, Permit>>,
    ledger: Ledger,
}

/// The ledger as one operation sees it: the in-memory model, or the SQLite
/// connection of that operation (`None` if it could not be opened).
enum Store<'a> {
    Memory(&'a Mutex<MemLedger>),
    Sqlite(Option<&'a Connection>),
}

/// Operator diagnostic (LEDGER-SQLITE.md "Storage failures" rule 4): a fixed
/// category and an SQLite primary result code name or `io`; never a path,
/// message text, credential, key or seal.
fn report(e: &DbError) {
    eprintln!("proto: ledger error: {}", e.diagnostic());
}

pub(crate) fn refused(code: &str) -> Value {
    obj(vec![("result", s("refused")), ("code", s(code))])
}

pub(crate) fn result(r: &str) -> Value {
    obj(vec![("result", s(r))])
}

pub(crate) fn replayed(r: &str) -> Value {
    obj(vec![("result", s(r)), ("replayed", Value::Bool(true))])
}

fn load(doc: Option<&Value>) -> Result<Bundle, ()> {
    doc.filter(|d| !d.is_null()).ok_or(()).and_then(kernel::load_bundle)
}

/// Grant binding derived from a request and its use-time decision.
fn grant_binding(req: &Value, d: &Decision, verdict: &str) -> Value {
    let action = req.get("action").cloned().unwrap_or(Value::Null);
    obj(vec![
        ("verdict", s(verdict)),
        ("approval_request_id", d.approval_request_id.as_deref().map(s).unwrap_or(Value::Null)),
        ("tenant_id", req.get("tenant_id").cloned().unwrap_or(Value::Null)),
        ("principal_id", req.get("principal").and_then(|p| p.get("id")).cloned().unwrap_or(Value::Null)),
        ("action_kind", action.get("kind").cloned().unwrap_or(Value::Null)),
        ("canonical_resource_id", action.get("resource").and_then(|r| r.get("canonical_resource_id")).cloned().unwrap_or(Value::Null)),
        ("action_digest", action.get("action_digest").cloned().unwrap_or(Value::Null)),
        ("requested_capabilities", Value::Arr(kernel::capability_set(req).iter().map(|x| s(x)).collect())),
        ("policy_bundle_sha", s(&d.policy_sha)),
    ])
}

impl Config {
    fn decide(&self, req: &Value) -> Decision {
        let policy = match (&self.bundle, self.policy_up) {
            (_, false) => Policy::Absent,
            (Err(()), true) => Policy::Rejected,
            (Ok(b), true) => Policy::Loaded(b),
        };
        kernel::decide(req, policy, self.state_revision, self.now)
    }

    /// Sealed ingress (D-010): registered adapter, MAC over the canonical
    /// request, adapter id equal to `principal.verified_by`.
    fn seal_valid(&self, req: &Value, seal: Option<&Value>) -> bool {
        let Some(seal) = seal.filter(|x| x.as_obj().is_some()) else { return false };
        let (Some(adapter), Some(mac)) = (seal.str_at("adapter_id"), seal.str_at("mac")) else { return false };
        let Some(key) = self.ingress_keys.str_at(adapter).and_then(unhex) else { return false };
        let Ok(bytes) = json::canonical(req) else { return false };
        mac_verify(&key, bytes.as_bytes(), mac) && req.get("principal").and_then(|p| p.str_at("verified_by")) == Some(adapter)
    }

    fn sign_permit(&self, body: &Value) -> String {
        mac_hex(&self.permit_key, json::canonical(body).unwrap_or_default().as_bytes())
    }

    /// Effect adapter permit verification (simulated): fingerprint, MAC,
    /// expiry. The permit id on success.
    fn permit_check(&self, permit: &Permit, req: &Value) -> Result<String, &'static str> {
        let fp = json::digest(req).ok();
        if permit.body.str_at("request_fingerprint") != fp.as_deref() {
            return Err("PERMIT_REQUEST_MISMATCH");
        }
        let signed = json::canonical(&permit.body).unwrap_or_default();
        if !mac_verify(&self.permit_key, signed.as_bytes(), &permit.mac) {
            return Err("PERMIT_INVALID");
        }
        let expires = permit.body.str_at("expires_at").and_then(time::parse_rfc3339);
        if !expires.is_some_and(|e| self.now < e) {
            return Err("PERMIT_EXPIRED");
        }
        Ok(permit.body.str_at("permit_id").unwrap_or("").to_string())
    }
}

fn conn(c: Option<&Connection>) -> R<&Connection> {
    c.ok_or_else(|| DbError::new("SQLITE_CANTOPEN").within(Category::Open))
}

impl Store<'_> {
    /// Receipt lookup, grant checks and use, reservation. `None`: continue
    /// with dispatch (new or resumed reservation); `Some`: the result.
    fn reserve(&self, t: &mut StepTiming, key: &Key, fingerprint: &str, grant: Option<&str>, expected: &Value, now: Instant, crash_before: bool) -> R<Option<Value>> {
        let m = match self {
            Store::Sqlite(c) => {
                let binding = json::canonical(expected).unwrap_or_default();
                return sqlite::reserve(conn(*c)?, t, key, fingerprint, grant, &binding, now, crash_before);
            }
            Store::Memory(m) => m,
        };
        // (kept, result) with the same outcomes as T-reserve.
        Ok(t.memory("reserve", || {
            let mut l = lock(m);
            let resume = match l.receipts.get(key) {
                Some(r) if r.fingerprint != fingerprint || r.grant.as_deref() != grant => return (false, Some(refused("IDEMPOTENCY_CONFLICT"))),
                Some(r) => match r.status {
                    ReceiptStatus::ObservedSuccess => return (false, Some(replayed("effect_completed"))),
                    ReceiptStatus::ObservedFailure => return (false, Some(replayed("effect_failed"))),
                    ReceiptStatus::Dispatched | ReceiptStatus::UnknownReconcile => return (false, Some(refused("RECONCILIATION_REQUIRED"))),
                    ReceiptStatus::Reserved => true,
                },
                None => false,
            };
            if !resume {
                if crash_before {
                    return (false, Some(result("crashed")));
                }
                if let Some(label) = grant {
                    let Some(g) = l.grants.get_mut(label) else { return (false, Some(refused("GRANT_UNKNOWN"))) };
                    if g.binding != *expected {
                        return (false, Some(refused("GRANT_MISMATCH")));
                    }
                    if now >= g.expires {
                        return (false, Some(refused("GRANT_EXPIRED")));
                    }
                    if g.uses >= g.max_uses {
                        return (false, Some(refused("GRANT_EXHAUSTED")));
                    }
                    g.uses += 1;
                }
                let receipt = Receipt { fingerprint: fingerprint.to_string(), grant: grant.map(str::to_string), status: ReceiptStatus::Reserved };
                l.receipts.insert(key.clone(), receipt);
            }
            (true, None)
        }))
    }

    /// Next permit number; the receipt becomes `dispatched`.
    fn dispatch(&self, t: &mut StepTiming, key: &Key) -> R<u64> {
        match self {
            Store::Sqlite(c) => sqlite::dispatch(conn(*c)?, t, key),
            Store::Memory(m) => Ok(t.memory("dispatch", || {
                let mut l = lock(m);
                l.next_permit += 1;
                let n = l.next_permit;
                if let Some(r) = l.receipts.get_mut(key) {
                    r.status = ReceiptStatus::Dispatched;
                }
                (true, n)
            })),
        }
    }

    /// Effect adapter (simulated): verification, single use, then apply.
    fn adapter(&self, t: &mut StepTiming, check: impl FnOnce() -> Result<String, &'static str>, fails: bool) -> R<Result<bool, &'static str>> {
        let m = match self {
            Store::Sqlite(c) => return sqlite::adapter(conn(*c)?, t, check, fails),
            Store::Memory(m) => m,
        };
        // The verification is inside the section, as it is inside T-adapter.
        Ok(t.memory("adapter", || {
            let id = match check() {
                Ok(id) => id,
                Err(code) => return (false, Err(code)),
            };
            let mut l = lock(m);
            if !l.consumed_permits.insert(id) {
                return (false, Err("PERMIT_REPLAYED"));
            }
            if fails {
                return (true, Ok(false));
            }
            l.effects_applied += 1;
            (true, Ok(true))
        }))
    }

    fn observe(&self, t: &mut StepTiming, key: &Key, success: bool) -> R<()> {
        match self {
            Store::Sqlite(c) => sqlite::observe(conn(*c)?, t, key, success),
            Store::Memory(m) => Ok(t.memory("observe", || {
                if let Some(r) = lock(m).receipts.get_mut(key) {
                    r.status = if success { ReceiptStatus::ObservedSuccess } else { ReceiptStatus::ObservedFailure };
                }
                (true, ())
            })),
        }
    }

    /// `false` if the label already names a grant (never reset).
    fn insert_grant(&self, t: &mut StepTiming, label: &str, binding: Value, expires: Instant) -> R<bool> {
        match self {
            Store::Sqlite(c) => sqlite::insert_grant(conn(*c)?, t, label, &json::canonical(&binding).unwrap_or_default(), expires),
            Store::Memory(m) => Ok(t.memory("approve", || {
                let mut l = lock(m);
                if l.grants.contains_key(label) {
                    return (false, false);
                }
                l.grants.insert(label.to_string(), Grant { binding, expires, max_uses: 1, uses: 0 });
                (true, true)
            })),
        }
    }

    fn reconcile(&self, t: &mut StepTiming) -> R<i64> {
        match self {
            Store::Sqlite(c) => sqlite::reconcile(conn(*c)?, t),
            Store::Memory(m) => Ok(t.memory("reconcile", || {
                let mut n = 0;
                for r in lock(m).receipts.values_mut().filter(|r| r.status == ReceiptStatus::Dispatched) {
                    r.status = ReceiptStatus::UnknownReconcile;
                    n += 1;
                }
                (true, n)
            })),
        }
    }

    fn refuse(&self, t: &mut StepTiming, code: &str) -> R<()> {
        match self {
            Store::Sqlite(c) => sqlite::refuse(conn(*c)?, t, code),
            Store::Memory(m) => Ok(t.memory("audit", || {
                lock(m).refusals += 1;
                (true, ())
            })),
        }
    }

    fn final_state(&self) -> R<Value> {
        let m = match self {
            Store::Sqlite(c) => return sqlite::final_state(conn(*c)?),
            Store::Memory(m) => m,
        };
        let l = lock(m);
        let mut receipts: BTreeMap<&str, i64> = BTreeMap::new();
        for r in l.receipts.values() {
            *receipts.entry(r.status.name()).or_default() += 1;
        }
        Ok(obj(vec![
            ("effects_applied", Value::Int(l.effects_applied)),
            ("grant_uses", Value::Obj(l.grants.iter().map(|(k, g)| (k.clone(), Value::Int(g.uses))).collect())),
            ("receipts", Value::Obj(receipts.into_iter().map(|(k, n)| (k.to_string(), Value::Int(n))).collect())),
            ("consumed_permits", Value::Int(l.consumed_permits.len() as i64)),
            ("refusals", Value::Int(l.refusals)),
        ]))
    }
}

impl Broker {
    fn new(init: &Value, ledger: Ledger) -> Broker {
        let deps = init.get("dependencies");
        let config = Config {
            now: init.str_at("now").and_then(time::parse_rfc3339).unwrap_or((0, 0)),
            state_revision: init.get("state_revision").and_then(Value::as_int).unwrap_or(0),
            bundle: load(init.get("bundle")),
            ingress_keys: init.get("ingress_keys").cloned().unwrap_or(Value::Obj(vec![])),
            permit_key: init.str_at("permit_key").and_then(unhex).unwrap_or_default(),
            channels: init.get("channels").cloned().unwrap_or(Value::Obj(vec![])),
            policy_up: deps.and_then(|d| d.str_at("policy")) != Some("down"),
            ledger_up: deps.and_then(|d| d.str_at("ledger")) != Some("down"),
        };
        Broker { profile: Profile::Primary, gate: RwLock::new(()), config: RwLock::new(config), permits: Mutex::new(HashMap::new()), ledger }
    }

    /// The in-process lock of one (tenant, idempotency key); SQLite only (the
    /// in-memory ledger runs every operation exclusively).
    fn key_lock(&self, key: &Key) -> Option<Arc<Mutex<()>>> {
        match &self.ledger {
            Ledger::Sqlite(l) => Some(lock(&l.key_locks).entry(key.clone()).or_default().clone()),
            Ledger::Memory(_) => None,
        }
    }

    /// Acquire the coordination gate into `hold`, once per operation (never
    /// re-entrant). Callers hold no per-key lock and no open transaction.
    fn enter_gate<'a>(&'a self, hold: &mut Option<GateHold<'a>>, exclusive: bool) {
        debug_assert!(hold.is_none(), "the gate is never acquired re-entrantly");
        *hold = Some(if exclusive {
            GateHold::Exclusive(self.gate.write().unwrap_or_else(|e| e.into_inner()))
        } else {
            GateHold::Shared(self.gate.read().unwrap_or_else(|e| e.into_inner()))
        });
        #[cfg(test)]
        self.trace(if exclusive { sqlite::trace::Event::GateExclusive } else { sqlite::trace::Event::GateShared });
    }

    fn execute<'a>(&'a self, cfg: &Config, store: &Store, t: &mut StepTiming, op: &Value, step: usize, hold: &mut Option<GateHold<'a>>) -> R<Value> {
        let req = op.get("request").cloned().unwrap_or(Value::Null);
        let started = Clock::now();
        let sealed = cfg.seal_valid(&req, op.get("seal"));
        let ledger_up = cfg.ledger_up;
        t.validate_ns = Some(since(started));
        if !sealed {
            return Ok(refused("INGRESS_UNAUTHENTICATED"));
        }
        if !ledger_up {
            return Ok(refused("LEDGER_UNAVAILABLE"));
        }
        let started = Clock::now();
        let d = cfg.decide(&req);
        t.decide_ns = Some(since(started));
        if !matches!(d.status, "allow" | "require_approval") {
            return Ok(refused(&d.reason));
        }
        let grant_ref = op.str_at("grant_ref");
        if d.status == "require_approval" && grant_ref.is_none() {
            return Ok(refused("APPROVAL_REQUIRED"));
        }
        let fault = op.str_at("fault").unwrap_or("");
        let effective_grant = if d.status == "require_approval" { grant_ref } else { None };
        let fingerprint = json::digest(&req).unwrap_or_default();
        let key = (req.str_at("tenant_id").unwrap_or("").to_string(), req.str_at("idempotency_key").unwrap_or("").to_string());
        // Lock order (D-029): gate (shared, held until the operation returns,
        // after its audit), then the per-key lock, then SQLite transactions.
        self.enter_gate(hold, false);
        let started = Clock::now();
        let key_lock = self.key_lock(&key);
        let _held = key_lock.as_deref().map(lock);
        if key_lock.is_some() {
            t.key_lock_wait_ns = Some(since(started));
            #[cfg(test)]
            self.trace(sqlite::trace::Event::KeyLock);
        }

        let expected = grant_binding(&req, &d, "approved");
        if let Some(done) = store.reserve(t, &key, &fingerprint, effective_grant, &expected, cfg.now, fault == "crash_before_reserve")? {
            return Ok(done);
        }
        if fault == "crash_after_reserve" {
            return Ok(result("crashed"));
        }
        #[cfg(test)]
        if fault == "test_panic_after_reserve" {
            panic!("test fault: panic with the gate and the key lock held");
        }

        // Permit bound to the whole request, the bundle and min(now + 30 s, deadline).
        let n = store.dispatch(t, &key)?;
        let mut expires = time::add_seconds(cfg.now, PERMIT_TTL_SECONDS);
        if let Some(dl) = req.str_at("deadline_at").and_then(time::parse_rfc3339) {
            expires = expires.min(dl);
        }
        let body = obj(vec![
            ("permit_id", s(&format!("permit-{n}"))),
            ("request_fingerprint", s(&fingerprint)),
            ("policy_bundle_sha", s(&d.policy_sha)),
            ("expires_at", s(&time::format(expires))),
        ]);
        let permit = Permit { mac: cfg.sign_permit(&body), body };
        lock(&self.permits).insert(step, permit.clone());
        let applied = match store.adapter(t, || cfg.permit_check(&permit, &req), fault == "effect_fails")? {
            Ok(applied) => applied,
            Err(code) => return Ok(refused(code)),
        };
        if fault == "crash_after_dispatch" {
            return Ok(result("crashed"));
        }
        store.observe(t, &key, applied)?;
        Ok(result(if applied { "effect_completed" } else { "effect_failed" }))
    }

    /// Separate trusted approval channel (simulated credential match).
    fn approve(&self, cfg: &Config, store: &Store, t: &mut StepTiming, op: &Value) -> R<Value> {
        let started = Clock::now();
        let channel = op.str_at("channel").and_then(|c| cfg.channels.get(c));
        let human = op.str_at("human").unwrap_or("");
        let credential = op.str_at("credential");
        let authenticated = credential.is_some() && channel.and_then(|c| c.str_at(human)) == credential;
        t.validate_ns = Some(since(started));
        if !authenticated {
            return Ok(refused("HUMAN_UNAUTHENTICATED"));
        }
        let req = op.get("for_request").cloned().unwrap_or(Value::Null);
        let started = Clock::now();
        let d = cfg.decide(&req);
        t.decide_ns = Some(since(started));
        if d.status != "require_approval" {
            return Ok(refused("NOT_APPROVABLE"));
        }
        let principal = req.get("principal").and_then(|p| p.str_at("id"));
        if Some(human) == principal || Some(human) == req.str_at("agent_id") {
            return Ok(refused("SELF_APPROVAL_FORBIDDEN"));
        }
        let label = op.str_at("grant_ref").unwrap_or("");
        let ttl = op.get("ttl_seconds").and_then(Value::as_int).unwrap_or(DEFAULT_GRANT_TTL_SECONDS);
        let verdict = op.str_at("verdict").unwrap_or("");
        // A label names one grant for its whole life; reusing it would reset
        // the grant's uses.
        if !store.insert_grant(t, label, grant_binding(&req, &d, verdict), time::add_seconds(cfg.now, ttl))? {
            return Ok(refused("GRANT_REF_IN_USE"));
        }
        Ok(result("grant_issued"))
    }

    fn present_permit<'a>(&'a self, cfg: &Config, store: &Store, t: &mut StepTiming, op: &Value, hold: &mut Option<GateHold<'a>>) -> R<Value> {
        let step = op.get("of_step").and_then(Value::as_int).unwrap_or(-1);
        let Some(mut permit) = usize::try_from(step).ok().and_then(|i| lock(&self.permits).get(&i).cloned()) else {
            return Ok(refused("PERMIT_MISSING"));
        };
        match op.str_at("tamper") {
            Some("mac") => {
                let flipped = if permit.mac.starts_with('0') { "1" } else { "0" };
                permit.mac.replace_range(0..1, flipped);
            }
            Some("expiry") => {
                let later = permit.body.str_at("expires_at").and_then(time::parse_rfc3339).map(|t| time::add_seconds(t, 3600));
                if let (Some(t), Value::Obj(m)) = (later, &mut permit.body) {
                    if let Some(slot) = m.iter_mut().find(|(k, _)| k == "expires_at") {
                        slot.1 = s(&time::format(t));
                    }
                }
            }
            _ => {}
        }
        let req = op.get("request").cloned().unwrap_or(Value::Null);
        self.enter_gate(hold, false);
        Ok(match store.adapter(t, || cfg.permit_check(&permit, &req), false)? {
            Ok(_) => result("effect_applied"),
            Err(code) => refused(code),
        })
    }

    /// `restart_broker` (D-020). Trusted configuration and client-held
    /// permits survive. In memory the whole ledger survives: the exhaustive
    /// pattern forces every field to be classified (grants and their uses,
    /// receipts, consumed permit ids, the permit id counter and the counters
    /// reported in `final`); volatile state exists only on the stack of an
    /// operation. With SQLite every connection is closed, the key locks are
    /// dropped and a new connection is opened to the same file.
    fn restart(&self) -> R<()> {
        match &self.ledger {
            Ledger::Memory(m) => {
                let MemLedger { grants: _, receipts: _, consumed_permits: _, next_permit: _, effects_applied: _, refusals: _ } = &*lock(m);
                Ok(())
            }
            Ledger::Sqlite(l) => {
                lock(&l.key_locks).clear();
                let mut main = lock(&l.main);
                if let Some(c) = main.take() {
                    c.close().map_err(|(_, e)| DbError::from(e).within(Category::Open))?;
                }
                *main = Some(l.connect()?);
                Ok(())
            }
        }
    }

    /// Operations that change trusted configuration or restart the broker:
    /// they run alone.
    fn configure(&self, cfg: &mut Config, op: &Value) -> R<Value> {
        Ok(match op.str_at("op").unwrap_or("") {
            "restart_broker" => {
                self.restart()?;
                result("restarted")
            }
            "activate_bundle" => {
                cfg.bundle = load(op.get("bundle"));
                result("bundle_activated")
            }
            "advance_time" => match op.str_at("now").and_then(time::parse_rfc3339) {
                Some(t) => {
                    cfg.now = t;
                    result("time_advanced")
                }
                None => refused("TIME_INVALID"),
            },
            _ => {
                let up = op.str_at("status") == Some("up");
                match op.str_at("name") {
                    Some("policy") => cfg.policy_up = up,
                    Some("ledger") => cfg.ledger_up = up,
                    _ => {}
                }
                result("dependency_set")
            }
        })
    }

    /// `hold` receives the coordination gate if the operation takes it; the
    /// caller keeps it until the operation has returned and been audited.
    fn operate<'a>(&'a self, cfg: &Config, store: &Store, t: &mut StepTiming, op: &Value, step: usize, hold: &mut Option<GateHold<'a>>) -> R<Value> {
        match op.str_at("op").unwrap_or("") {
            "execute" => self.execute(cfg, store, t, op, step, hold),
            "approve" => self.approve(cfg, store, t, op),
            "present_permit" => self.present_permit(cfg, store, t, op, hold),
            "apply_without_permit" => Ok(refused("PERMIT_MISSING")),
            "reconcile" => {
                // Exclusive before BEGIN IMMEDIATE: no in-flight execute of
                // this process is between T-reserve and its return (D-029).
                self.enter_gate(hold, true);
                Ok(obj(vec![("result", s("reconciled")), ("count", Value::Int(store.reconcile(t)?))]))
            }
            _ => Ok(refused("OPERATION_UNKNOWN")),
        }
    }

    /// Run `f` with the ledger of one operation: `own` is the connection of
    /// an operation in a concurrent group; otherwise the main connection.
    fn with_store<T>(&self, own: Option<&R<Connection>>, f: impl FnOnce(&Store) -> T) -> T {
        match (&self.ledger, own) {
            (Ledger::Memory(m), _) => f(&Store::Memory(m)),
            (Ledger::Sqlite(_), Some(c)) => f(&Store::Sqlite(c.as_ref().ok())),
            (Ledger::Sqlite(l), None) => {
                let main = lock(&l.main);
                f(&Store::Sqlite(main.as_ref()))
            }
        }
    }

    fn apply_one(&self, op: &Value, step: usize, own: Option<&R<Connection>>, t: &mut StepTiming) -> Value {
        let configures = matches!(op.str_at("op"), Some("restart_broker" | "activate_bundle" | "advance_time" | "set_dependency"));
        // Declared before the attempt so that it outlives the audit below and
        // is released on every path, including unwinding (D-029).
        let mut hold: Option<GateHold> = None;
        let attempt = if configures {
            let mut cfg = self.config.write().unwrap_or_else(|e| e.into_inner());
            self.configure(&mut cfg, op)
        } else if matches!(self.ledger, Ledger::Memory(_)) {
            let cfg = self.config.write().unwrap_or_else(|e| e.into_inner());
            self.with_store(own, |st| self.operate(&cfg, st, t, op, step, &mut hold))
        } else {
            let cfg = self.config.read().unwrap_or_else(|e| e.into_inner());
            self.with_store(own, |st| self.operate(&cfg, st, t, op, step, &mut hold))
        };
        let out = attempt.unwrap_or_else(|e| {
            report(&e);
            refused("LEDGER_UNAVAILABLE")
        });
        if out.str_at("result") == Some("refused") {
            if let Err(e) = self.with_store(own, |st| st.refuse(t, out.str_at("code").unwrap_or(""))) {
                report(&e);
            }
        }
        drop(hold);
        #[cfg(test)]
        self.trace(sqlite::trace::Event::Returned);
        out
    }

    /// Worker connection setup (SQLite only, D-027): the connection (an
    /// error is reported on stderr with category `worker`), `setup_ns` and the
    /// instant setup finished. The in-memory ledger has no worker connection.
    fn worker_setup(&self) -> (Option<R<Connection>>, Option<i64>, Option<Clock>) {
        let Ledger::Sqlite(l) = &self.ledger else { return (None, None, None) };
        #[cfg(test)]
        let traced = |e| l.path.as_deref().inspect(|p| sqlite::trace::record(p, e));
        #[cfg(test)]
        traced(sqlite::trace::Event::SetupStart);
        let started = Clock::now();
        let c = l.connect_worker().inspect_err(report);
        let ended = Clock::now();
        #[cfg(test)]
        traced(sqlite::trace::Event::SetupEnd);
        (Some(c), Some(between(started, ended)), Some(ended))
    }

    #[cfg(test)]
    fn trace(&self, e: sqlite::trace::Event) {
        if let Ledger::Sqlite(SqlLedger { path: Some(p), .. }) = &self.ledger {
            sqlite::trace::record(p, e);
        }
    }

    /// One step and its timing: `total_ns` runs from dispatch of the parsed
    /// step to its result value.
    fn apply(&self, op: &Value, step: usize, own: Option<&R<Connection>>) -> (Value, StepTiming) {
        let started = Clock::now();
        let mut t = StepTiming::new(op.str_at("op"));
        let out = self.dispatch_step(op, step, own, &mut t, started);
        t.total_ns = since(started);
        (out, t)
    }

    /// `dispatched` is the start of this step's `total_ns`.
    fn dispatch_step(&self, op: &Value, step: usize, own: Option<&R<Connection>>, t: &mut StepTiming, dispatched: Clock) -> Value {
        if op.str_at("op") != Some("concurrent") {
            return self.apply_one(op, step, own, t);
        }
        let ops = op.get("operations").and_then(Value::as_arr).cloned().unwrap_or_default();
        let (results, timings) = self.group(&ops, step, t, dispatched);
        t.operations = Some(timings);
        let mut outcomes: BTreeMap<String, i64> = BTreeMap::new();
        for r in &results {
            *outcomes.entry(outcome_key(r)).or_default() += 1;
        }
        obj(vec![
            ("result", s("concurrent")),
            ("outcomes", Value::Obj(outcomes.into_iter().map(|(k, n)| (k, Value::Int(n))).collect())),
        ])
    }

    /// A `concurrent` group under the selected profile and the D-028 release
    /// protocol (LEDGER-SQLITE.md "Concurrency profiles and release
    /// protocol"). The calling thread is the coordinator; there is one OS
    /// thread per operation.
    ///
    /// 1. The coordinator spawns every worker before anything is released.
    /// 2. Each worker (`primary` SQLite: after its own connection setup,
    ///    concurrently with the others) records `ready_at`, then, under the
    ///    gate mutex, counts itself ready, wakes the coordinator and waits on
    ///    the gate condition variable; the wait releases the mutex
    ///    atomically, so nothing happens between signalling ready and
    ///    waiting. A failed setup still counts as ready.
    /// 3. The coordinator waits until every worker is ready, reads `ready_ns`,
    ///    then `release_ns`, opens the gate and wakes every worker with one
    ///    `notify_all`.
    /// 4. Each worker reads its start instant after it sees the gate open;
    ///    `cold` SQLite then sets up its connection; then it runs its operation.
    /// 5. The coordinator joins every worker, then closes the worker
    ///    connections.
    ///
    /// Hence `ready_at_ns <= ready_ns <= release_ns <= start_ns` for every
    /// worker, on one monotonic clock.
    fn group(&self, ops: &[Value], step: usize, t: &mut StepTiming, dispatched: Clock) -> (Vec<Value>, Vec<StepTiming>) {
        struct Gate {
            ready: usize,
            open: bool,
        }
        let gate = Mutex::new(Gate { ready: 0, open: false });
        let (ready_cv, open_cv) = (Condvar::new(), Condvar::new());
        let cold = self.profile == Profile::Cold;
        let mut release = None;
        let done: Vec<(Value, StepTiming, Option<R<Connection>>)> = thread::scope(|scope| {
            let handles: Vec<_> = ops
                .iter()
                .map(|o| {
                    let (gate, ready_cv, open_cv) = (&gate, &ready_cv, &open_cv);
                    scope.spawn(move || {
                        let created = Clock::now();
                        #[cfg(test)]
                        self.trace(sqlite::trace::Event::Spawned);
                        let (mut conn, mut setup_ns, mut ready_at) = (None, None, created);
                        if !cold {
                            let ended;
                            (conn, setup_ns, ended) = self.worker_setup();
                            ready_at = ended.unwrap_or(created);
                        }
                        #[cfg(test)]
                        {
                            if matches!(&conn, Some(Ok(c)) if !c.is_autocommit()) {
                                self.trace(sqlite::trace::Event::TxOpenAtReady);
                            }
                            self.trace(sqlite::trace::Event::Ready);
                        }
                        let mut g = lock(gate);
                        g.ready += 1;
                        ready_cv.notify_one();
                        while !g.open {
                            g = open_cv.wait(g).unwrap_or_else(|e| e.into_inner());
                        }
                        drop(g);
                        let passed = Clock::now();
                        #[cfg(test)]
                        self.trace(sqlite::trace::Event::Pass);
                        if cold {
                            (conn, setup_ns, _) = self.worker_setup();
                        }
                        let mut wt = StepTiming::new(o.str_at("op"));
                        let out = if matches!(conn, Some(Err(_))) {
                            // Setup failed: no ledger transaction, no audit.
                            if wt.op == "concurrent" {
                                wt.operations = Some(Vec::new());
                            }
                            refused("LEDGER_UNAVAILABLE")
                        } else {
                            self.dispatch_step(o, step, conn.as_ref(), &mut wt, passed)
                        };
                        wt.total_ns = between(passed, Clock::now());
                        let start_ns = between(dispatched, passed);
                        wt.worker = Some(WorkerTiming { setup_ns, ready_at_ns: between(dispatched, ready_at), start_ns, release_delay_ns: 0 });
                        (out, wt, conn)
                    })
                })
                .collect();
            if !ops.is_empty() {
                let mut g = lock(&gate);
                while g.ready < ops.len() {
                    g = ready_cv.wait(g).unwrap_or_else(|e| e.into_inner());
                }
                let ready_ns = since(dispatched);
                let release_ns = since(dispatched);
                #[cfg(test)]
                self.trace(sqlite::trace::Event::Release);
                g.open = true;
                drop(g);
                open_cv.notify_all();
                release = Some((ready_ns, release_ns));
            }
            handles
                .into_iter()
                .zip(ops)
                .map(|(h, o)| h.join().unwrap_or_else(|_| (refused("INTERNAL_ERROR"), StepTiming::new(o.str_at("op")), None)))
                .collect()
        });
        let (mut results, mut timings) = (Vec::with_capacity(ops.len()), Vec::with_capacity(ops.len()));
        let mut conns = Vec::with_capacity(ops.len());
        for (out, wt, conn) in done {
            results.push(out);
            timings.push(wt);
            conns.push(conn);
        }
        drop(conns);
        let (ready_ns, release_ns) = release.unwrap_or((0, 0));
        let starts: Vec<i64> = timings.iter().filter_map(|w| w.worker.map(|x| x.start_ns)).collect();
        for w in timings.iter_mut().filter_map(|w| w.worker.as_mut()) {
            w.release_delay_ns = w.start_ns - release_ns;
        }
        let start_skew_ns = match (starts.iter().max(), starts.iter().min()) {
            (Some(max), Some(min)) if starts.len() > 1 => max - min,
            _ => 0,
        };
        t.group = GroupTiming { ready_ns, release_ns, start_skew_ns };
        (results, timings)
    }

    fn run_steps(&self, input: &Value, timing: &mut ScenarioTiming) -> Value {
        let (steps, timings): (Vec<Value>, Vec<StepTiming>) = input
            .get("steps")
            .and_then(Value::as_arr)
            .map(|ops| ops.iter().enumerate().map(|(i, op)| self.apply(op, i, None)).unzip())
            .unwrap_or_default();
        timing.steps = timings;
        let started = Clock::now();
        let fin = self.with_store(None, |st| st.final_state()).unwrap_or_else(|e| {
            report(&e);
            obj(vec![("error", s("LEDGER_UNAVAILABLE"))])
        });
        timing.final_ns = Some(since(started));
        obj(vec![("steps", Value::Arr(steps)), ("final", fin)])
    }
}

fn outcome_key(v: &Value) -> String {
    if v.get("replayed") == Some(&Value::Bool(true)) {
        return "replayed".into();
    }
    match v.str_at("result") {
        Some("refused") => v.str_at("code").unwrap_or("").to_string(),
        Some(r) => r.to_string(),
        None => String::new(),
    }
}

/// State fields every scenario must provide (BROKER.md "State").
const REQUIRED_STATE: [&str; 7] = ["now", "state_revision", "bundle", "ingress_keys", "permit_key", "channels", "dependencies"];
const FAULTS: [&str; 4] = ["crash_before_reserve", "crash_after_reserve", "crash_after_dispatch", "effect_fails"];

/// The broker's declared dependencies (D-021): any other name is malformed.
const DEPENDENCIES: [&str; 2] = ["policy", "ledger"];

fn dependency_status(v: &Value) -> bool {
    matches!(v.as_str(), Some("up" | "down"))
}

fn dependency_declared(name: &str) -> bool {
    DEPENDENCIES.contains(&name)
}

/// Structural check of one step (M1-PARITY-SUITE protocol clarifications):
/// an object; a known `fault` if any; a declared dependency name and a known
/// status;
/// `concurrent` carries an array of such steps. Unknown operations are not
/// malformed (OPERATION_UNKNOWN).
fn step_usable(op: &Value) -> bool {
    if op.as_obj().is_none() || op.get("fault").is_some_and(|f| !f.as_str().is_some_and(|f| FAULTS.contains(&f))) {
        return false;
    }
    match op.str_at("op") {
        Some("concurrent") => op.get("operations").and_then(Value::as_arr).is_some_and(|ops| ops.iter().all(step_usable)),
        Some("set_dependency") => op.str_at("name").is_some_and(dependency_declared) && op.get("status").is_some_and(dependency_status),
        _ => true,
    }
}

fn input_usable(input: &Value) -> bool {
    let initial_ok = input.get("initial").is_some_and(|i| {
        i.as_obj().is_some()
            && REQUIRED_STATE.iter().all(|k| i.get(k).is_some())
            && i.get("dependencies").and_then(Value::as_obj).is_some_and(|d| {
                d.iter().all(|(k, v)| dependency_declared(k) && dependency_status(v)) && DEPENDENCIES.iter().all(|n| d.iter().any(|(k, _)| k == n))
            })
    });
    let steps_ok = input.get("steps").and_then(Value::as_arr).is_some_and(|ops| ops.iter().all(step_usable));
    initial_ok && steps_ok
}

/// An SQLite ledger, opened and initialized before any step (LEDGER-SQLITE.md
/// "Storage failures" rule 1). `Err` (reported on stderr only): the scenario
/// answers `{"error": "LEDGER_UNAVAILABLE"}`.
fn sqlite_ledger(target: sqlite::Target) -> R<Ledger> {
    let (path, c) = sqlite::open_ledger(target)?;
    Ok(Ledger::Sqlite(SqlLedger { path: Some(path), main: Mutex::new(Some(c)),
        key_locks: Mutex::new(HashMap::new()),
        #[cfg(test)]
        fail_setups: Default::default(),
    }))
}

#[cfg(test)]
fn run(input: &Value) -> Value {
    run_with(input, &LedgerChoice::Memory, Profile::Primary)
}

pub fn run_with(input: &Value, choice: &LedgerChoice, profile: Profile) -> Value {
    run_timed(input, choice, profile).0
}

/// `broker-scenario` output and its timing (LEDGER-SQLITE.md "Per-operation
/// timing"): `open_ns` brackets creating the file (`--ledger-dir`), opening
/// and initializing it; it is `null` for the in-memory ledger and when no
/// open was attempted (`INPUT_MALFORMED`).
pub fn run_timed(input: &Value, choice: &LedgerChoice, profile: Profile) -> (Value, ScenarioTiming) {
    let mut timing = ScenarioTiming::default();
    if !input_usable(input) {
        return (obj(vec![("input_error", s("INPUT_MALFORMED"))]), timing);
    }
    let started = Clock::now();
    let ledger = match choice {
        LedgerChoice::Memory => Ok(Ledger::Memory(Mutex::new(MemLedger::default()))),
        LedgerChoice::SqliteDir(dir) => sqlite_ledger(sqlite::Target::Dir(dir)),
        LedgerChoice::SqliteDb(path) => sqlite_ledger(sqlite::Target::Db(path)),
    };
    if !matches!(choice, LedgerChoice::Memory) {
        timing.open_ns = Some(since(started));
    }
    let ledger = match ledger {
        Ok(l) => l,
        Err(e) => {
            report(&e);
            return (obj(vec![("error", s("LEDGER_UNAVAILABLE"))]), timing);
        }
    };
    let init = input.get("initial").cloned().unwrap_or(Value::Obj(vec![]));
    let mut broker = Broker::new(&init, ledger);
    broker.profile = profile;
    let out = broker.run_steps(input, &mut timing);
    (out, timing)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The pre-D-027 call shape: the default (`primary`) profile.
    fn run_with(input: &Value, choice: &LedgerChoice) -> Value {
        super::run_with(input, choice, Profile::Primary)
    }

    fn run_timed(input: &Value, choice: &LedgerChoice) -> (Value, ScenarioTiming) {
        super::run_timed(input, choice, Profile::Primary)
    }

    const BUNDLE: &str = include_str!("../../fixtures/transition/m1-transition-transition-known-unsupported.bundle.json");
    const INGRESS_KEY: &str = "0101010101010101010101010101010101010101010101010101010101010101";
    const PERMIT_KEY: &str = "0202020202020202020202020202020202020202020202020202020202020202";

    fn request(idem: &str) -> Value {
        let text = format!(
            r#"{{"contract_version":"2.0","request_id":"req-b1","trace_id":"trace-b1","tenant_id":"fixture-tenant",
            "principal":{{"id":"agent:session-1","verified_by":"writ-host-adapter:fixture","authn_method":"os_peer_credential","roles":["agent"]}},
            "project_id":"fixture-project","session_id":"session-1","agent_id":null,"parent_session_id":null,"idempotency_key":"{idem}",
            "expected_state_revision":7,"policy_bundle_sha":null,"deadline_at":null,
            "action":{{"kind":"file.write","resource":{{"uri":"file:///work/a.txt","canonical_resource_id":"workspace:fixture-tenant/fixture-project/a.txt"}},
            "action_digest":"sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","requested_capabilities":[]}}}}"#
        );
        json::parse(text.as_bytes()).unwrap()
    }

    fn execute(idem: &str, fault: Option<&str>) -> Value {
        let req = request(idem);
        let mac = mac_hex(&unhex(INGRESS_KEY).unwrap(), json::canonical(&req).unwrap().as_bytes());
        let mut members = vec![
            ("op", s("execute")),
            ("request", req),
            ("seal", obj(vec![("adapter_id", s("writ-host-adapter:fixture")), ("mac", s(&mac))])),
        ];
        if let Some(f) = fault {
            members.push(("fault", s(f)));
        }
        obj(members)
    }

    fn scenario(steps: Vec<Value>) -> Value {
        scenario_with_deps(obj(vec![("policy", s("up")), ("ledger", s("up"))]), steps)
    }

    fn scenario_with_deps(dependencies: Value, steps: Vec<Value>) -> Value {
        let initial = obj(vec![
            ("now", s("2026-11-07T11:00:00Z")),
            ("state_revision", Value::Int(7)),
            ("bundle", json::parse(BUNDLE.as_bytes()).unwrap()),
            ("ingress_keys", obj(vec![("writ-host-adapter:fixture", s(INGRESS_KEY))])),
            ("permit_key", s(PERMIT_KEY)),
            ("channels", obj(vec![])),
            ("dependencies", dependencies),
        ]);
        run(&obj(vec![("initial", initial), ("steps", Value::Arr(steps))]))
    }

    fn restart() -> Value {
        obj(vec![("op", s("restart_broker"))])
    }

    fn step_results(out: &Value) -> Vec<String> {
        out.get("steps").and_then(Value::as_arr).unwrap().iter().map(json::to_text).collect()
    }

    fn final_int(out: &Value, key: &str) -> i64 {
        out.get("final").and_then(|f| f.get(key)).and_then(Value::as_int).unwrap()
    }

    #[test]
    fn d020_consumed_permit_stays_consumed_after_restart() {
        let present = obj(vec![("op", s("present_permit")), ("of_step", Value::Int(0)), ("request", request("idem-1"))]);
        let out = scenario(vec![execute("idem-1", None), restart(), present]);
        assert_eq!(
            step_results(&out),
            [r#"{"result":"effect_completed"}"#, r#"{"result":"restarted"}"#, r#"{"code":"PERMIT_REPLAYED","result":"refused"}"#]
        );
        assert_eq!((final_int(&out, "effects_applied"), final_int(&out, "consumed_permits")), (1, 1));
    }

    #[test]
    fn d020_receipts_survive_restart() {
        let out = scenario(vec![execute("idem-1", None), restart(), execute("idem-1", None)]);
        assert_eq!(step_results(&out)[2], r#"{"replayed":true,"result":"effect_completed"}"#);
        let out = scenario(vec![execute("idem-1", Some("crash_after_dispatch")), restart(), execute("idem-1", None)]);
        assert_eq!(step_results(&out)[2], r#"{"code":"RECONCILIATION_REQUIRED","result":"refused"}"#);
    }

    #[test]
    fn d020_new_permits_after_restart_do_not_collide_with_consumed_ids() {
        let out = scenario(vec![execute("idem-1", None), restart(), execute("idem-2", None)]);
        assert_eq!(step_results(&out)[2], r#"{"result":"effect_completed"}"#);
        assert_eq!((final_int(&out, "effects_applied"), final_int(&out, "consumed_permits")), (2, 2));
    }

    /// An approvable (initial, approve step) pair taken from the frozen
    /// broker fixture set: the first case whose first step issues a grant.
    fn approvable() -> (Value, Value) {
        include_str!("../../fixtures/sets/broker-scenarios.jsonl")
            .lines()
            .filter_map(|l| json::parse(l.as_bytes()).ok())
            .find_map(|c| {
                let first = c.get("input")?.get("steps")?.as_arr()?.first()?.clone();
                let issued = c.get("expect")?.get("steps")?.as_arr()?.first()?.str_at("result") == Some("grant_issued");
                (first.str_at("op") == Some("approve") && issued).then(|| (c.get("input").unwrap().get("initial").unwrap().clone(), first))
            })
            .expect("fixture set has an approvable request")
    }

    fn with_label(op: &Value, label: &str) -> Value {
        let mut m = op.as_obj().unwrap().clone();
        m.retain(|(k, _)| k != "grant_ref");
        m.push(("grant_ref".into(), s(label)));
        Value::Obj(m)
    }

    #[test]
    fn grant_ref_in_use_is_refused() {
        let (initial, approve) = approvable();
        let steps = vec![with_label(&approve, "g1"), with_label(&approve, "g1"), with_label(&approve, "g2")];
        let out = run(&obj(vec![("initial", initial), ("steps", Value::Arr(steps))]));
        assert_eq!(
            step_results(&out),
            [r#"{"result":"grant_issued"}"#, r#"{"code":"GRANT_REF_IN_USE","result":"refused"}"#, r#"{"result":"grant_issued"}"#]
        );
        assert_eq!(final_int(&out, "refusals"), 1);
    }

    #[test]
    fn present_permit_without_issued_permit_is_missing() {
        let present = |i: i64| obj(vec![("op", s("present_permit")), ("of_step", Value::Int(i)), ("request", request("idem-1"))]);
        let out = scenario(vec![restart(), present(0), present(7), present(-1)]);
        assert_eq!(step_results(&out)[1..], [r#"{"code":"PERMIT_MISSING","result":"refused"}"#; 3]);
    }

    #[test]
    fn unknown_operation_and_malformed_scenarios() {
        let out = scenario(vec![obj(vec![("op", s("zz_unknown_operation"))])]);
        assert_eq!(step_results(&out), [r#"{"code":"OPERATION_UNKNOWN","result":"refused"}"#]);
        let malformed = r#"{"input_error":"INPUT_MALFORMED"}"#;
        for steps in [
            vec![execute("idem-1", Some("zz_fault"))],
            vec![obj(vec![("op", s("set_dependency")), ("name", s("ledger")), ("status", s("sideways"))])],
            vec![Value::Int(1)],
            vec![obj(vec![("op", s("concurrent")), ("operations", Value::Int(1))])],
        ] {
            assert_eq!(json::to_text(&scenario(steps)), malformed);
        }
        assert_eq!(json::to_text(&run(&obj(vec![("initial", obj(vec![])), ("steps", Value::Arr(vec![]))]))), malformed);
        assert_eq!(json::to_text(&run(&obj(vec![("steps", Value::Arr(vec![]))]))), malformed);
        let no_steps = scenario(vec![]);
        assert!(no_steps.get("final").is_some());
    }

    #[test]
    fn undeclared_dependency_names_are_malformed() {
        // D-021: the broker's declared dependencies are exactly `policy` and
        // `ledger`; any other name makes the whole scenario INPUT_MALFORMED,
        // with no step executed.
        let malformed = r#"{"input_error":"INPUT_MALFORMED"}"#;
        let set = |name: Option<&str>| {
            let mut m = vec![("op", s("set_dependency")), ("status", s("down"))];
            if let Some(n) = name {
                m.push(("name", s(n)));
            }
            obj(m)
        };
        for steps in [
            vec![execute("idem-1", None), set(Some("cache"))],
            vec![set(None)],
            vec![set(Some("Policy"))],
            vec![obj(vec![("op", s("concurrent")), ("operations", Value::Arr(vec![set(Some("ledger")), set(Some("cache"))]))])],
        ] {
            assert_eq!(json::to_text(&scenario(steps)), malformed);
        }
        for deps in [
            obj(vec![("policy", s("up")), ("ledger", s("up")), ("cache", s("up"))]),
            obj(vec![("cache", s("down"))]),
        ] {
            assert_eq!(json::to_text(&scenario_with_deps(deps, vec![execute("idem-1", None)])), malformed);
        }
        let out = scenario(vec![set(Some("policy")), set(Some("ledger"))]);
        assert_eq!(step_results(&out), [r#"{"result":"dependency_set"}"#; 2]);
    }

    // ------------------------------------------------ SQLite ledger (D-023)

    fn temp_dir(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("writ-proto-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn sqlite_broker(initial: &Value, dir: &std::path::Path) -> Broker {
        Broker::new(initial, sqlite_ledger(sqlite::Target::Dir(dir)).unwrap())
    }

    fn main_conn<T>(b: &Broker, f: impl FnOnce(&Connection) -> T) -> T {
        let Ledger::Sqlite(l) = &b.ledger else { panic!("not sqlite") };
        f(lock(&l.main).as_ref().unwrap())
    }

    fn count(b: &Broker, sql: &str) -> i64 {
        main_conn(b, |c| c.query_row(sql, [], |r| r.get(0)).unwrap())
    }

    fn with_member(v: &Value, key: &str, value: Value) -> Value {
        let mut m = v.as_obj().unwrap().clone();
        m.retain(|(k, _)| k != key);
        m.push((key.into(), value));
        Value::Obj(m)
    }

    /// A sealed execute of `req` under the scenario's ingress key.
    fn sealed(initial: &Value, req: &Value, grant: Option<&str>) -> Value {
        let adapter = req.get("principal").and_then(|p| p.str_at("verified_by")).unwrap();
        let key = unhex(initial.get("ingress_keys").and_then(|k| k.str_at(adapter)).unwrap()).unwrap();
        let mac = mac_hex(&key, json::canonical(req).unwrap().as_bytes());
        let mut members = vec![("op", s("execute")), ("request", req.clone()), ("seal", obj(vec![("adapter_id", s(adapter)), ("mac", s(&mac))]))];
        if let Some(g) = grant {
            members.push(("grant_ref", s(g)));
        }
        obj(members)
    }

    /// The approvable request with another idempotency key, and an allowed
    /// file write from the same principal.
    fn requests(approve: &Value, idem: &str) -> (Value, Value) {
        let publish = with_member(approve.get("for_request").unwrap(), "idempotency_key", s(idem));
        let resource = obj(vec![("canonical_resource_id", s("workspace:fixture-tenant/fixture-project/a.txt")), ("uri", s("file:///work/a.txt"))]);
        let action = with_member(&with_member(publish.get("action").unwrap(), "kind", s("file.write")), "resource", resource);
        let write = with_member(&publish, "action", action);
        (publish, write)
    }

    fn concurrent(ops: Vec<Value>) -> Value {
        obj(vec![("op", s("concurrent")), ("operations", Value::Arr(ops))])
    }

    #[test]
    fn schema_is_byte_identical_to_the_fixture() {
        let fixture = std::fs::read(concat!(env!("CARGO_MANIFEST_DIR"), "/../fixtures/broker/ledger.sql")).unwrap();
        assert_eq!(sqlite::SCHEMA.as_bytes(), fixture.as_slice());
    }

    #[test]
    fn failing_statement_in_t_reserve_leaves_nothing() {
        let dir = temp_dir("atomicity");
        let (initial, approve) = approvable();
        let (publish, _) = requests(&approve, "idem-atomic");
        let b = sqlite_broker(&initial, &dir);
        assert_eq!(json::to_text(&b.apply(&with_label(&approve, "g1"), 0, None).0), r#"{"result":"grant_issued"}"#);
        // The last statement of T-reserve fails after the grant use, the
        // grant_uses row and the receipt were written.
        main_conn(&b, |c| c.execute_batch("CREATE TEMP TRIGGER fail_reserve BEFORE INSERT ON receipt_events BEGIN SELECT RAISE(ABORT, 'injected'); END").unwrap());
        let out = b.apply(&sealed(&initial, &publish, Some("g1")), 1, None).0;
        assert_eq!(json::to_text(&out), r#"{"code":"LEDGER_UNAVAILABLE","result":"refused"}"#);
        assert_eq!(count(&b, "SELECT count(*) FROM receipts"), 0);
        assert_eq!(count(&b, "SELECT count(*) FROM receipt_events"), 0);
        assert_eq!(count(&b, "SELECT count(*) FROM grant_uses"), 0);
        assert_eq!(count(&b, "SELECT uses FROM grants WHERE label = 'g1'"), 0);
        assert_eq!(count(&b, "SELECT count(*) FROM audit_refusals WHERE code = 'LEDGER_UNAVAILABLE'"), 1);
        main_conn(&b, |c| c.execute_batch("DROP TRIGGER fail_reserve").unwrap());
        let out = b.apply(&sealed(&initial, &publish, Some("g1")), 2, None).0;
        assert_eq!(json::to_text(&out), r#"{"result":"effect_completed"}"#);
        assert_eq!(count(&b, "SELECT uses FROM grants WHERE label = 'g1'"), 1);
        drop(b);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn restart_reopens_the_same_file_and_matches_memory() {
        let dir = temp_dir("restart");
        let (initial, approve) = approvable();
        let (p, _) = requests(&approve, "idem-p");
        let (_, w1) = requests(&approve, "idem-w1");
        let (_, w2) = requests(&approve, "idem-w2");
        let crash = |req: &Value, grant, fault: &str| with_member(&sealed(&initial, req, grant), "fault", s(fault));
        let steps = vec![
            with_label(&approve, "g1"),
            crash(&p, Some("g1"), "crash_after_reserve"),
            sealed(&initial, &w1, None),
            crash(&w2, None, "crash_after_dispatch"),
            restart(),
            sealed(&initial, &p, Some("g1")),
            sealed(&initial, &w1, None),
            sealed(&initial, &w2, None),
            obj(vec![("op", s("present_permit")), ("of_step", Value::Int(2)), ("request", w1.clone())]),
            restart(),
            obj(vec![("op", s("reconcile"))]),
        ];
        let input = obj(vec![("initial", initial.clone()), ("steps", Value::Arr(steps))]);
        let sqlite_out = run_with(&input, &LedgerChoice::SqliteDir(dir.clone()));
        assert_eq!(json::to_text(&sqlite_out), json::to_text(&run(&input)));
        assert_eq!(
            step_results(&sqlite_out)[5..],
            [
                r#"{"result":"effect_completed"}"#,
                r#"{"replayed":true,"result":"effect_completed"}"#,
                r#"{"code":"RECONCILIATION_REQUIRED","result":"refused"}"#,
                r#"{"code":"PERMIT_REPLAYED","result":"refused"}"#,
                r#"{"result":"restarted"}"#,
                r#"{"count":1,"result":"reconciled"}"#,
            ]
        );
        assert_eq!(
            json::to_text(sqlite_out.get("final").unwrap()),
            r#"{"consumed_permits":3,"effects_applied":3,"grant_uses":{"g1":1},"receipts":{"observed_success":2,"unknown_reconcile":1},"refusals":2}"#
        );
        std::fs::remove_dir_all(&dir).unwrap();
    }

    /// A valid ledger file created by this prototype, closed (WAL checkpointed).
    fn valid_ledger(dir: &std::path::Path) -> PathBuf {
        let path = dir.join("l.db");
        let (_, c) = sqlite::open_ledger(sqlite::Target::Db(&path)).unwrap();
        c.close().unwrap();
        path
    }

    fn one_write() -> Value {
        let (initial, approve) = approvable();
        let (_, write) = requests(&approve, "idem-open");
        obj(vec![("initial", initial.clone()), ("steps", Value::Arr(vec![sealed(&initial, &write, None)]))])
    }

    /// Rule 1 of "Storage failures": the whole scenario answers exactly
    /// {"error":"LEDGER_UNAVAILABLE"} and the existing file is unchanged.
    fn assert_unavailable_unmodified(path: &std::path::Path) {
        let before = std::fs::read(path).unwrap();
        let out = run_with(&one_write(), &LedgerChoice::SqliteDb(path.to_path_buf()));
        assert_eq!(json::to_text(&out), r#"{"error":"LEDGER_UNAVAILABLE"}"#);
        assert_eq!(std::fs::read(path).unwrap(), before);
    }

    #[test]
    fn schema_tables_are_those_of_ledger_sql() {
        assert_eq!(
            sqlite::schema_tables(),
            ["meta", "grants", "receipts", "receipt_events", "grant_uses", "audit_refusals", "counters", "adapter_consumed_permits", "adapter_effects"]
        );
    }

    #[test]
    fn open_failures_answer_the_whole_scenario_unavailable() {
        let dir = temp_dir("open-failures");
        let missing = dir.join("missing").join("l.db");
        let out = run_with(&one_write(), &LedgerChoice::SqliteDb(missing));
        assert_eq!(json::to_text(&out), r#"{"error":"LEDGER_UNAVAILABLE"}"#);
        assert!(!dir.join("missing").exists());

        let garbage = dir.join("garbage.db");
        std::fs::write(&garbage, b"not a database ".repeat(300)).unwrap();
        assert_unavailable_unmodified(&garbage);

        for (name, change) in [
            ("version", "UPDATE meta SET value = '2' WHERE key = 'schema_version'"),
            ("no-version", "DELETE FROM meta"),
            ("no-meta", "DROP TABLE meta"),
            ("no-table", "DROP TABLE adapter_effects"),
        ] {
            let sub = dir.join(name);
            std::fs::create_dir_all(&sub).unwrap();
            let path = valid_ledger(&sub);
            let c = Connection::open(&path).unwrap();
            c.execute_batch(change).unwrap();
            c.close().unwrap();
            assert_unavailable_unmodified(&path);
        }

        let sub = dir.join("extra");
        std::fs::create_dir_all(&sub).unwrap();
        let path = valid_ledger(&sub);
        let c = Connection::open(&path).unwrap();
        c.execute_batch("CREATE TABLE extra (x INTEGER)").unwrap();
        c.close().unwrap();
        let out = run_with(&one_write(), &LedgerChoice::SqliteDb(path));
        assert_eq!(step_results(&out), [r#"{"result":"effect_completed"}"#]);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn read_only_ledger_is_unavailable() {
        use std::os::unix::fs::PermissionsExt;
        let dir = temp_dir("read-only");
        let path = valid_ledger(&dir);
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o444)).unwrap();
        assert_unavailable_unmodified(&path);
        let ro = dir.join("ro-dir");
        std::fs::create_dir_all(&ro).unwrap();
        std::fs::set_permissions(&ro, std::fs::Permissions::from_mode(0o555)).unwrap();
        let out = run_with(&one_write(), &LedgerChoice::SqliteDir(ro.clone()));
        std::fs::set_permissions(&ro, std::fs::Permissions::from_mode(0o755)).unwrap();
        assert_eq!(json::to_text(&out), r#"{"error":"LEDGER_UNAVAILABLE"}"#);
        assert_eq!(std::fs::read_dir(&ro).unwrap().count(), 0);
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o644)).unwrap();
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn lock_held_beyond_busy_timeout_is_unavailable() {
        let dir = temp_dir("locked");
        let path = valid_ledger(&dir);
        let holder = Connection::open(&path).unwrap();
        holder.execute_batch("PRAGMA journal_mode = WAL; BEGIN EXCLUSIVE").unwrap();
        let out = run_with(&one_write(), &LedgerChoice::SqliteDb(path.clone()));
        holder.execute_batch("ROLLBACK").unwrap();
        holder.close().unwrap();
        assert_eq!(json::to_text(&out), r#"{"error":"LEDGER_UNAVAILABLE"}"#);
        let c = Connection::open(&path).unwrap();
        assert_eq!(c.query_row("SELECT count(*) FROM receipts", [], |r| r.get::<_, i64>(0)).unwrap(), 0);
        drop(c);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    // ------------------------------------------- per-operation timing (M1-06d)

    /// (op, {tx name: outcome}) of each top-level step.
    fn tx_outcomes(t: &ScenarioTiming) -> Vec<(String, Vec<(&'static str, &'static str)>)> {
        t.steps.iter().map(|st| (st.op.clone(), st.tx.iter().map(|(n, x)| (*n, if x.commit { "commit" } else { "rollback" })).collect())).collect()
    }

    #[test]
    fn timing_names_the_transactions_that_ran() {
        let (initial, approve) = approvable();
        let (p1, _) = requests(&approve, "idem-t1");
        let (p2, _) = requests(&approve, "idem-t2");
        let (_, w) = requests(&approve, "idem-tw");
        let mut unsealed = sealed(&initial, &w, None);
        unsealed = with_member(&unsealed, "seal", obj(vec![("adapter_id", s("writ-host-adapter:fixture")), ("mac", s("00"))]));
        let steps = vec![
            with_label(&approve, "g1"),
            sealed(&initial, &w, None),
            sealed(&initial, &p1, Some("g1")),
            sealed(&initial, &p2, Some("g1")),
            unsealed,
            sealed(&initial, &w, None),
            with_label(&approve, "g1"),
        ];
        let input = obj(vec![("initial", initial), ("steps", Value::Arr(steps))]);
        let full = vec![("reserve", "commit"), ("dispatch", "commit"), ("adapter", "commit"), ("observe", "commit")];
        let expected = vec![
            ("approve".to_string(), vec![("approve", "commit")]),
            ("execute".to_string(), full.clone()),
            ("execute".to_string(), full),
            ("execute".to_string(), vec![("reserve", "rollback"), ("audit", "commit")]),
            ("execute".to_string(), vec![("audit", "commit")]),
            ("execute".to_string(), vec![("reserve", "rollback")]),
            ("approve".to_string(), vec![("approve", "rollback"), ("audit", "commit")]),
        ];
        let dir = temp_dir("timing");
        for choice in [LedgerChoice::Memory, LedgerChoice::SqliteDir(dir.clone())] {
            let sql = !matches!(choice, LedgerChoice::Memory);
            let (out, t) = run_timed(&input, &choice);
            assert_eq!(json::to_text(&out), json::to_text(&run_with(&input, &choice)));
            assert_eq!(
                step_results(&out)[2..6],
                [
                    r#"{"result":"effect_completed"}"#,
                    r#"{"code":"GRANT_EXHAUSTED","result":"refused"}"#,
                    r#"{"code":"INGRESS_UNAUTHENTICATED","result":"refused"}"#,
                    r#"{"replayed":true,"result":"effect_completed"}"#,
                ]
            );
            assert_eq!(tx_outcomes(&t), expected);
            assert_eq!(t.open_ns.is_some(), sql);
            assert!(t.final_ns.is_some());
            let unsealed = &t.steps[4];
            assert!(unsealed.validate_ns.is_some() && unsealed.decide_ns.is_none() && unsealed.key_lock_wait_ns.is_none());
            let exhausted = &t.steps[3];
            assert!(exhausted.validate_ns.is_some() && exhausted.decide_ns.is_some());
            assert_eq!(exhausted.key_lock_wait_ns.is_some(), sql);
            for st in &t.steps {
                let parts: i64 = st.tx.iter().map(|(_, x)| x.begin_ns.unwrap_or(0) + x.body_ns + x.end_ns.unwrap_or(0)).sum::<i64>()
                    + st.validate_ns.unwrap_or(0)
                    + st.decide_ns.unwrap_or(0)
                    + st.key_lock_wait_ns.unwrap_or(0);
                assert!(parts <= st.total_ns);
                assert!(st.tx.iter().all(|(_, x)| x.begin_ns.is_some() == sql && x.end_ns.is_some() == sql));
            }
        }
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn timing_of_a_concurrent_group_and_an_input_error() {
        let (initial, approve) = approvable();
        let (_, w) = requests(&approve, "idem-c");
        let group = concurrent(vec![sealed(&initial, &w, None), obj(vec![("op", s("reconcile"))])]);
        let input = obj(vec![("initial", initial), ("steps", Value::Arr(vec![group]))]);
        let (_, t) = run_timed(&input, &LedgerChoice::Memory);
        let v = json::to_text(&t.to_value(1, 2));
        assert!(v.contains(r#""decide_ns":null,"key_lock_wait_ns":null,"op":"concurrent","operations":[{"#), "{v}");
        let ops = t.steps[0].operations.as_ref().unwrap();
        assert_eq!((ops[0].op.as_str(), ops[1].op.as_str()), ("execute", "reconcile"));
        assert!(t.steps[0].tx.is_empty() && ops.iter().all(|o| o.total_ns <= t.steps[0].total_ns));

        let (out, t) = run_timed(&obj(vec![("steps", Value::Arr(vec![]))]), &LedgerChoice::Memory);
        assert_eq!(json::to_text(&out), r#"{"input_error":"INPUT_MALFORMED"}"#);
        assert_eq!(json::to_text(&t.to_value(0, 0)), r#"{"final_ns":null,"open_ns":null,"parse_ns":0,"run_ns":0,"steps":[]}"#);
    }

    // ------------------------------------------------------------- D-025

    const LEDGER_REFUSED: &str = r#"{"code":"LEDGER_UNAVAILABLE","result":"refused"}"#;

    fn ledger_path(b: &Broker) -> PathBuf {
        let Ledger::Sqlite(l) = &b.ledger else { panic!("not sqlite") };
        l.path.clone().unwrap()
    }

    /// Every row of every ledger table, in a stable order.
    fn dump(c: &Connection) -> Vec<String> {
        let mut out = vec![];
        for table in sqlite::schema_tables() {
            let mut st = c.prepare(&format!("SELECT * FROM {table} ORDER BY 1")).unwrap();
            let n = st.column_count();
            let rows = st.query_map([], |r| (0..n).map(|i| r.get::<_, rusqlite::types::Value>(i)).collect::<Result<Vec<_>, _>>()).unwrap();
            for row in rows {
                out.push(format!("{table}: {:?}", row.unwrap()));
            }
        }
        out
    }

    fn present(req: &Value, tamper: Option<&str>) -> Value {
        let mut m = vec![("op", s("present_permit")), ("of_step", Value::Int(0)), ("request", req.clone())];
        if let Some(x) = tamper {
            m.push(("tamper", s(x)));
        }
        obj(m)
    }

    #[test]
    fn dep1_missing_declared_dependency_is_malformed_and_creates_no_file() {
        let malformed = r#"{"input_error":"INPUT_MALFORMED"}"#;
        let dir = temp_dir("dep1");
        for deps in [obj(vec![("policy", s("up"))]), obj(vec![("ledger", s("up"))]), obj(vec![]), obj(vec![("ledger", s("down"))])] {
            assert_eq!(json::to_text(&scenario_with_deps(deps.clone(), vec![execute("idem-1", None)])), malformed);
            let (initial, _) = approvable();
            let input = obj(vec![("initial", with_member(&initial, "dependencies", deps)), ("steps", Value::Arr(vec![]))]);
            for choice in [LedgerChoice::SqliteDir(dir.clone()), LedgerChoice::SqliteDb(dir.join("l.db"))] {
                let (out, t) = run_timed(&input, &choice);
                assert_eq!(json::to_text(&out), malformed);
                assert!(t.open_ns.is_none());
            }
        }
        assert_eq!(std::fs::read_dir(&dir).unwrap().count(), 0);
        let down = scenario_with_deps(obj(vec![("policy", s("down")), ("ledger", s("down"))]), vec![execute("idem-1", None)]);
        assert_eq!(step_results(&down), [r#"{"code":"LEDGER_UNAVAILABLE","result":"refused"}"#]);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    /// S-1: verification precedes every persistent setting.
    #[test]
    fn s1_rejected_non_wal_file_is_unchanged_and_valid_one_switches_to_wal() {
        let dir = temp_dir("s1");
        for (name, version) in [("rejected.db", "2"), ("valid.db", "1")] {
            let path = dir.join(name);
            let c = Connection::open(&path).unwrap();
            c.query_row("PRAGMA journal_mode = DELETE", [], |r| r.get::<_, String>(0)).unwrap();
            c.execute_batch(&format!("BEGIN IMMEDIATE; {} UPDATE meta SET value = '{version}' WHERE key = 'schema_version'; COMMIT;", sqlite::SCHEMA)).unwrap();
            c.close().unwrap();
        }
        assert_unavailable_unmodified(&dir.join("rejected.db"));
        let out = run_with(&one_write(), &LedgerChoice::SqliteDb(dir.join("valid.db")));
        assert_eq!(step_results(&out), [r#"{"result":"effect_completed"}"#]);
        let mut names: Vec<_> = std::fs::read_dir(&dir).unwrap().map(|e| e.unwrap().file_name().into_string().unwrap()).collect();
        names.sort();
        assert_eq!(names, ["rejected.db", "valid.db"]);
        let c = Connection::open(dir.join("valid.db")).unwrap();
        assert_eq!(c.query_row("PRAGMA journal_mode", [], |r| r.get::<_, String>(0)).unwrap(), "wal");
        drop(c);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    /// S-2: a file this run created is removed when initialization fails; a
    /// pre-existing path is never touched.
    #[test]
    fn s2_created_file_removed_pre_existing_kept() {
        let dir = temp_dir("s2");
        std::fs::create_dir(dir.join("new.db-wal")).unwrap();
        let out = run_with(&one_write(), &LedgerChoice::SqliteDb(dir.join("new.db")));
        assert_eq!(json::to_text(&out), r#"{"error":"LEDGER_UNAVAILABLE"}"#);
        let names: Vec<_> = std::fs::read_dir(&dir).unwrap().map(|e| e.unwrap().file_name().into_string().unwrap()).collect();
        assert_eq!(names, ["new.db-wal"]);
        assert!(dir.join("new.db-wal").is_dir());
        let garbage = dir.join("garbage.db");
        std::fs::write(&garbage, b"x".repeat(4096)).unwrap();
        assert_unavailable_unmodified(&garbage);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    /// Rule 4: a diagnostic is a fixed category and a result code name.
    #[test]
    fn diagnostics_carry_no_path() {
        let dir = temp_dir("diag-secret-dir");
        let missing = dir.join("secret-sub").join("l.db");
        let e = sqlite::open_ledger(sqlite::Target::Db(&missing)).unwrap_err();
        assert_eq!(e.diagnostic(), "open: io");
        let e = sqlite::open_ledger(sqlite::Target::Dir(&dir.join("secret-sub"))).unwrap_err();
        assert_eq!(e.diagnostic(), "open: io");

        let path = valid_ledger(&dir);
        let c = Connection::open(&path).unwrap();
        c.execute_batch("UPDATE meta SET value = '2' WHERE key = 'schema_version'").unwrap();
        c.close().unwrap();
        assert_eq!(sqlite::open_ledger(sqlite::Target::Db(&path)).unwrap_err().diagnostic(), "initialize: SQLITE_NOTADB");
        let garbage = dir.join("garbage.db");
        std::fs::write(&garbage, b"not a database ".repeat(300)).unwrap();
        assert_eq!(sqlite::open_ledger(sqlite::Target::Db(&garbage)).unwrap_err().diagnostic(), "initialize: SQLITE_NOTADB");

        let sub = dir.join("locked");
        std::fs::create_dir(&sub).unwrap();
        let path = valid_ledger(&sub);
        let holder = Connection::open(&path).unwrap();
        holder.execute_batch("PRAGMA journal_mode = WAL; BEGIN EXCLUSIVE").unwrap();
        assert_eq!(sqlite::open_ledger(sqlite::Target::Db(&path)).unwrap_err().diagnostic(), "initialize: SQLITE_BUSY");
        holder.execute_batch("ROLLBACK").unwrap();
        drop(holder);

        let mut t = StepTiming::new(Some("execute"));
        let (_, c) = sqlite::open_ledger(sqlite::Target::Db(&path)).unwrap();
        c.execute_batch("CREATE TEMP TRIGGER fail_audit BEFORE INSERT ON audit_refusals BEGIN SELECT RAISE(ABORT, 'secret text'); END").unwrap();
        assert_eq!(sqlite::refuse(&c, &mut t, "X").unwrap_err().diagnostic(), "transaction audit: SQLITE_CONSTRAINT");
        drop(c);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    /// C-4: a corrupted stored grant is a ledger-integrity failure (refused
    /// LEDGER_UNAVAILABLE, rolled back: no use, no receipt); a well-formed
    /// but different binding stays GRANT_MISMATCH.
    #[test]
    fn c4_stored_grant_integrity() {
        let (initial, approve) = approvable();
        let (publish, _) = requests(&approve, "idem-c4");
        let corrupt = |b: &str| -> Vec<String> {
            let v = json::parse(b.as_bytes()).unwrap();
            let without = |k: &str| {
                let mut m = v.as_obj().unwrap().clone();
                m.retain(|(n, _)| n != k);
                json::canonical(&Value::Obj(m)).unwrap()
            };
            vec![
                b[..b.len() - 5].to_string(),
                format!("{},\"zz\":\"x\"}}", &b[..b.len() - 1]),
                format!("{},\"verdict\":\"approved\"}}", &b[..b.len() - 1]),
                b.replacen("\",\"", "\", \"", 1),
                format!(" {b}"),
                json::canonical(&with_member(&v, "requested_capabilities", s("none"))).unwrap(),
                json::canonical(&with_member(&v, "requested_capabilities", Value::Arr(vec![Value::Int(1)]))).unwrap(),
                json::canonical(&with_member(&v, "verdict", Value::Null)).unwrap(),
                without("policy_bundle_sha"),
                r#"{"verdict":"approved"}"#.to_string(),
                "[]".to_string(),
                "null".to_string(),
                String::new(),
            ]
        };
        let dir = temp_dir("c4");
        let b = sqlite_broker(&initial, &dir);
        assert_eq!(json::to_text(&b.apply(&with_label(&approve, "g1"), 0, None).0), r#"{"result":"grant_issued"}"#);
        let stored: String = main_conn(&b, |c| c.query_row("SELECT binding FROM grants WHERE label = 'g1'", [], |r| r.get(0)).unwrap());
        let set = |text: &str| main_conn(&b, |c| c.execute("UPDATE grants SET binding = ?1 WHERE label = 'g1'", [text]).unwrap());
        for (i, bad) in corrupt(&stored).iter().enumerate() {
            set(bad);
            let (out, t) = b.apply(&sealed(&initial, &publish, Some("g1")), 1, None);
            assert_eq!(json::to_text(&out), LEDGER_REFUSED, "corruption {i}: {bad}");
            assert_eq!(tx_outcomes(&ScenarioTiming { steps: vec![t], ..Default::default() })[0].1, [("reserve", "rollback"), ("audit", "commit")]);
            for sql in ["SELECT uses FROM grants", "SELECT count(*) FROM grant_uses", "SELECT count(*) FROM receipts", "SELECT count(*) FROM receipt_events", "SELECT count(*) FROM adapter_effects"] {
                assert_eq!(count(&b, sql), 0, "corruption {i}: {sql}");
            }
        }
        let other = with_member(&json::parse(stored.as_bytes()).unwrap(), "tenant_id", s("other-tenant"));
        set(&json::canonical(&other).unwrap());
        assert_eq!(json::to_text(&b.apply(&sealed(&initial, &publish, Some("g1")), 2, None).0), r#"{"code":"GRANT_MISMATCH","result":"refused"}"#);
        set(&stored);
        assert_eq!(json::to_text(&b.apply(&sealed(&initial, &publish, Some("g1")), 3, None).0), r#"{"result":"effect_completed"}"#);
        assert_eq!(count(&b, "SELECT uses FROM grants"), 1);
        drop(b);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    /// D-025 A and C-3: T-reserve on a `reserved` receipt commits without
    /// writing anything and uses no second grant.
    #[test]
    fn resumed_reservation_changes_no_durable_state() {
        let dir = temp_dir("resume");
        let (initial, approve) = approvable();
        let (publish, _) = requests(&approve, "idem-resume");
        let b = sqlite_broker(&initial, &dir);
        assert_eq!(json::to_text(&b.apply(&with_label(&approve, "g1"), 0, None).0), r#"{"result":"grant_issued"}"#);
        let crash = with_member(&sealed(&initial, &publish, Some("g1")), "fault", s("crash_after_reserve"));
        assert_eq!(json::to_text(&b.apply(&crash, 1, None).0), r#"{"result":"crashed"}"#);
        let before = main_conn(&b, dump);
        let changes = |b: &Broker| main_conn(b, |c| c.query_row("SELECT total_changes()", [], |r| r.get::<_, i64>(0)).unwrap());
        let changes_before = changes(&b);
        let key = (publish.str_at("tenant_id").unwrap().to_string(), "idem-resume".to_string());
        let fingerprint = json::digest(&publish).unwrap();
        let mut t = StepTiming::new(Some("execute"));
        let r = main_conn(&b, |c| sqlite::reserve(c, &mut t, &key, &fingerprint, Some("g1"), "", (0, 0), false)).unwrap();
        assert!(r.is_none(), "resumes");
        assert_eq!(t.tx.iter().map(|(n, x)| (*n, x.commit)).collect::<Vec<_>>(), [("reserve", true)]);
        assert_eq!(main_conn(&b, dump), before);
        assert_eq!(changes(&b), changes_before);
        assert_eq!(json::to_text(&b.apply(&sealed(&initial, &publish, Some("g1")), 2, None).0), r#"{"result":"effect_completed"}"#);
        assert_eq!(count(&b, "SELECT uses FROM grants WHERE label = 'g1'"), 1);
        assert_eq!(count(&b, "SELECT count(*) FROM grant_uses"), 1);
        assert_eq!(count(&b, "SELECT count(*) FROM receipt_events WHERE cause = 'reserve'"), 1);
        drop(b);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    /// D-025 B: T-adapter cannot begin (write lock held by another
    /// connection beyond a short busy timeout): LEDGER_UNAVAILABLE takes
    /// precedence over any permit defect, and nothing is consumed or applied.
    #[test]
    fn adapter_that_cannot_begin_is_unavailable_before_permit_checks() {
        let dir = temp_dir("adapter-begin");
        let (initial, approve) = approvable();
        let (_, w) = requests(&approve, "idem-ab");
        let (_, other) = requests(&approve, "idem-ab-other");
        let b = sqlite_broker(&initial, &dir);
        assert_eq!(json::to_text(&b.apply(&sealed(&initial, &w, None), 0, None).0), r#"{"result":"effect_completed"}"#);
        main_conn(&b, |c| c.execute_batch("PRAGMA busy_timeout = 50").unwrap());
        let holder = Connection::open(ledger_path(&b)).unwrap();
        holder.execute_batch("BEGIN IMMEDIATE").unwrap();
        for (req, tamper) in [(&w, Some("mac")), (&w, Some("expiry")), (&other, None), (&w, None)] {
            let (out, t) = b.apply(&present(req, tamper), 1, None);
            assert_eq!(json::to_text(&out), LEDGER_REFUSED, "{tamper:?}");
            let (name, x) = &t.tx[0];
            assert_eq!((*name, x.commit, x.body_ns), ("adapter", false, 0));
        }
        holder.execute_batch("ROLLBACK").unwrap();
        drop(holder);
        assert_eq!(count(&b, "SELECT count(*) FROM adapter_consumed_permits"), 1);
        assert_eq!(count(&b, "SELECT count(*) FROM adapter_effects"), 1);
        drop(b);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    /// D-025 B: inside T-adapter the order is fingerprint, MAC, expiry,
    /// replay; every refusal rolls back.
    #[test]
    fn adapter_checks_in_order_inside_the_transaction() {
        let dir = temp_dir("adapter-order");
        let (initial, approve) = approvable();
        let (_, w) = requests(&approve, "idem-ao");
        let (_, other) = requests(&approve, "idem-ao-other");
        let later = time::format(time::add_seconds(initial.str_at("now").and_then(time::parse_rfc3339).unwrap(), 3600));
        let advance = obj(vec![("op", s("advance_time")), ("now", s(&later))]);
        let refusal = |c: &str| format!(r#"{{"code":"{c}","result":"refused"}}"#);
        for choice in [LedgerChoice::Memory, LedgerChoice::SqliteDir(dir.clone())] {
            let steps = vec![
                sealed(&initial, &w, None),
                present(&other, Some("mac")),
                present(&w, Some("mac")),
                present(&w, Some("expiry")),
                present(&w, None),
                advance.clone(),
                present(&other, Some("mac")),
                present(&w, Some("mac")),
                present(&w, None),
            ];
            let input = obj(vec![("initial", initial.clone()), ("steps", Value::Arr(steps))]);
            let (out, t) = run_timed(&input, &choice);
            assert_eq!(
                step_results(&out),
                [
                    r#"{"result":"effect_completed"}"#.to_string(),
                    refusal("PERMIT_REQUEST_MISMATCH"),
                    refusal("PERMIT_INVALID"),
                    refusal("PERMIT_INVALID"),
                    refusal("PERMIT_REPLAYED"),
                    r#"{"result":"time_advanced"}"#.to_string(),
                    refusal("PERMIT_REQUEST_MISMATCH"),
                    refusal("PERMIT_INVALID"),
                    refusal("PERMIT_EXPIRED"),
                ]
            );
            for (op, tx) in tx_outcomes(&t).iter().filter(|(op, _)| op == "present_permit") {
                assert_eq!(tx, &[("adapter", "rollback"), ("audit", "commit")], "{op}");
            }
            assert_eq!((final_int(&out, "consumed_permits"), final_int(&out, "effects_applied")), (1, 1));
        }
        std::fs::remove_dir_all(&dir).unwrap();
    }

    /// D-025 B: a failing COMMIT of T-adapter (a deferred foreign key
    /// violation injected by a temporary trigger) is LEDGER_UNAVAILABLE and
    /// leaves neither the consumed id nor the effect.
    #[test]
    fn failing_adapter_commit_leaves_no_consumed_id_or_effect() {
        let dir = temp_dir("adapter-commit");
        let (initial, approve) = approvable();
        let (_, w) = requests(&approve, "idem-ac");
        let b = sqlite_broker(&initial, &dir);
        main_conn(&b, |c| {
            c.execute_batch(
                "CREATE TEMP TABLE fk_parent (id TEXT PRIMARY KEY);
                 CREATE TEMP TABLE fk_child (id TEXT REFERENCES fk_parent (id) DEFERRABLE INITIALLY DEFERRED);
                 CREATE TEMP TRIGGER fail_commit AFTER INSERT ON adapter_effects BEGIN INSERT INTO fk_child VALUES (NEW.permit_id); END",
            )
            .unwrap()
        });
        let (out, t) = b.apply(&sealed(&initial, &w, None), 0, None);
        assert_eq!(json::to_text(&out), LEDGER_REFUSED);
        assert_eq!(
            tx_outcomes(&ScenarioTiming { steps: vec![t], ..Default::default() })[0].1,
            [("reserve", "commit"), ("dispatch", "commit"), ("adapter", "rollback"), ("audit", "commit")]
        );
        assert_eq!(count(&b, "SELECT count(*) FROM adapter_consumed_permits"), 0);
        assert_eq!(count(&b, "SELECT count(*) FROM adapter_effects"), 0);
        assert_eq!(count(&b, "SELECT count(*) FROM receipts WHERE status = 'dispatched'"), 1);
        assert_eq!(json::to_text(&b.apply(&sealed(&initial, &w, None), 1, None).0), r#"{"code":"RECONCILIATION_REQUIRED","result":"refused"}"#);
        drop(b);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    use crate::timing::{TxAt, TxError, TxTime};
    use rusqlite::OptionalExtension;

    /// Ledger rows of one idempotency key (D-029 decision B evidence).
    #[derive(Clone, Debug, Default)]
    struct KeyRows {
        receipts: i64,
        events: i64,
        grant_uses: i64,
        status: Option<String>,
        dispatch_events: i64,
        observe_events: i64,
    }

    fn key_rows(b: &Broker, idem: &str) -> KeyRows {
        main_conn(b, |c| {
            let n = |sql: &str| -> i64 { c.query_row(sql, [idem], |r| r.get(0)).unwrap() };
            KeyRows {
                receipts: n("SELECT count(*) FROM receipts WHERE idempotency_key = ?1"),
                events: n("SELECT count(*) FROM receipt_events WHERE idempotency_key = ?1"),
                grant_uses: n("SELECT count(*) FROM grant_uses WHERE idempotency_key = ?1"),
                status: c.query_row("SELECT status FROM receipts WHERE idempotency_key = ?1", [idem], |r| r.get(0)).optional().unwrap(),
                dispatch_events: n("SELECT count(*) FROM receipt_events WHERE idempotency_key = ?1 AND cause = 'dispatch'"),
                observe_events: n("SELECT count(*) FROM receipt_events WHERE idempotency_key = ?1 AND cause = 'observe'"),
            }
        })
    }

    /// How a failed `execute` is accepted (D-026 categories, D-029 B).
    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    enum Accepted {
        /// Category 3: verified lock-contention abort (an aborted no-op).
        Abort,
        /// Category 4: busy at the begin of this later transaction after the
        /// earlier ones committed; modelled as the implied crash state.
        Partial(&'static str),
    }

    const LEDGER_CHAIN: [&str; 4] = ["reserve", "dispatch", "adapter", "observe"];

    /// A refusal audit may itself fail only busy at begin (D-026: the
    /// refusal is then not counted in `final.refusals`).
    fn audit_error_allowed(x: &TxTime) -> bool {
        x.error.is_none() || x.error == Some(TxError { busy: true, at: TxAt::Begin })
    }

    /// Accept a failed `execute` only with matching evidence (LEDGER-SQLITE.md
    /// "Contention and the concurrency oracle"). Its ledger transactions must
    /// be a prefix of reserve, dispatch, adapter, observe whose last one
    /// failed busy at begin and whose earlier ones committed without error,
    /// plus at most an `audit` (error none or busy at begin). When its key is
    /// used by no other operation (`rows` is `Some`), the key's rows must be
    /// the state implied: reserve failed: no receipt, receipt event or grant
    /// use (category 3); dispatch failed: receipt `reserved`, no dispatch
    /// event; adapter or observe failed: receipt `dispatched` with exactly
    /// one dispatch event and no observe event. Consumed permits and effects
    /// carry no key; they are checked per group by `accounting_ok`.
    fn classify_failure(w: &StepTiming, rows: Option<&KeyRows>) -> Option<Accepted> {
        let busy_begin = Some(TxError { busy: true, at: TxAt::Begin });
        if w.op != "execute" || !w.tx.iter().filter(|(n, _)| *n == "audit").all(|(_, x)| audit_error_allowed(x)) {
            return None;
        }
        let ledger: Vec<&(&'static str, TxTime)> = w.tx.iter().filter(|(n, _)| *n != "audit").collect();
        let (last, earlier) = ledger.split_last()?;
        let names: Vec<&str> = ledger.iter().map(|(n, _)| *n).collect();
        if names != LEDGER_CHAIN[..names.len()] || last.1.commit || last.1.error != busy_begin {
            return None;
        }
        if !earlier.iter().all(|(_, x)| x.commit && x.error.is_none()) {
            return None;
        }
        let stage = last.0;
        let state_ok = rows.is_none_or(|r| match stage {
            "reserve" => r.receipts == 0 && r.events == 0 && r.grant_uses == 0,
            "dispatch" => r.status.as_deref() == Some("reserved") && r.dispatch_events == 0,
            _ => r.status.as_deref() == Some("dispatched") && r.dispatch_events == 1 && r.observe_events == 0,
        });
        state_ok.then_some(if stage == "reserve" { Accepted::Abort } else { Accepted::Partial(stage) })
    }

    /// Effects and consumed permits written by a group (database deltas)
    /// equal those its operations' evidence implies: one per committed
    /// adapter transaction (these groups have no `effect_fails`).
    fn accounting_ok(ws: &[StepTiming], effects: i64, consumed: i64) -> bool {
        let adapters = ws.iter().filter(|w| w.tx.iter().any(|(n, x)| *n == "adapter" && x.commit)).count() as i64;
        effects == adapters && consumed == adapters
    }

    fn effects_and_permits(b: &Broker) -> (i64, i64) {
        (count(b, "SELECT count(*) FROM adapter_effects"), count(b, "SELECT count(*) FROM adapter_consumed_permits"))
    }

    /// Classify one contention group: every operation with a failed ledger
    /// transaction must be accepted by `classify_failure` (else the test
    /// fails); their count must equal the LEDGER_UNAVAILABLE outcomes; and
    /// the group's effects and consumed permits must match the evidence.
    fn accept_group(b: &Broker, out: &Value, t: &StepTiming, keys: &[String], before: (i64, i64)) -> Vec<Option<Accepted>> {
        let ws = t.operations.as_ref().unwrap();
        let unique = |k: &String| keys.iter().filter(|x| *x == k).count() == 1;
        let mut accepted = Vec::new();
        for (w, k) in ws.iter().zip(keys) {
            assert!(w.tx.iter().filter(|(n, _)| *n == "audit").all(|(_, x)| audit_error_allowed(x)), "audit error: {:?}", tx_errors(w));
            if w.tx.iter().any(|(n, x)| *n != "audit" && x.error.is_some()) {
                let rows = unique(k).then(|| key_rows(b, k));
                let a = classify_failure(w, rows.as_ref());
                assert!(a.is_some(), "failure without matching evidence: {:?} {rows:?}", tx_errors(w));
                accepted.push(a);
            } else {
                accepted.push(None);
            }
        }
        let failed = accepted.iter().filter(|a| a.is_some()).count() as i64;
        assert_eq!(outcome(out, "LEDGER_UNAVAILABLE"), failed, "every LEDGER_UNAVAILABLE must be an accepted failure");
        let (effects, consumed) = effects_and_permits(b);
        assert!(accounting_ok(ws, effects - before.0, consumed - before.1), "effects or consumed permits not implied by the evidence");
        accepted
    }

    fn outcome(out: &Value, k: &str) -> i64 {
        out.get("outcomes").and_then(|o| o.get(k)).and_then(Value::as_int).unwrap_or(0)
    }

    fn committed_audits(t: &StepTiming) -> i64 {
        t.operations.as_ref().unwrap().iter().filter(|w| w.tx.iter().any(|(n, x)| *n == "audit" && x.commit)).count() as i64
    }

    fn committed(t: &StepTiming, name: &str) -> i64 {
        t.operations.as_ref().unwrap().iter().filter(|w| w.tx.iter().any(|(n, x)| *n == name && x.commit)).count() as i64
    }

    /// Required tests item 4 with D-029 decision B: 64 threads race for one
    /// single-use grant with distinct keys, then 64 on one idempotency key, 20
    /// times, busy timeout 5 s. Category 3 aborts and category 4 partial
    /// failures are accepted only with matching evidence and modelled as
    /// LEDGER-SQLITE.md defines; accepted failures are reported, not hidden.
    #[test]
    fn contention_on_one_grant_and_one_key() {
        let (initial, approve) = approvable();
        let (mut aborts, mut partial) = (0, 0);
        let tally = |acc: &[Option<Accepted>], aborts: &mut i64, partial: &mut i64| {
            for a in acc.iter().flatten() {
                if *a == Accepted::Abort { *aborts += 1 } else { *partial += 1 }
            }
        };
        for rep in 0..20 {
            let dir = temp_dir(&format!("contention-{rep}"));
            let b = sqlite_broker(&initial, &dir);
            assert_eq!(json::to_text(&b.apply(&with_label(&approve, "g1"), 0, None).0), r#"{"result":"grant_issued"}"#);

            // Grant race: exactly one reservation and one grant use; the
            // single winner completes, or ends in the state its partial
            // failure implies (reserved or dispatched); at most one effect.
            let keys: Vec<String> = (0..64).map(|i| format!("k{i}")).collect();
            let ops = keys.iter().map(|k| sealed(&initial, &requests(&approve, k).0, Some("g1"))).collect();
            let (out, t) = b.apply(&concurrent(ops), 1, None);
            let acc = accept_group(&b, &out, &t, &keys, (0, 0));
            let msg = format!("rep {rep}: {}", json::to_text(&out));
            let c3 = acc.iter().filter(|a| **a == Some(Accepted::Abort)).count() as i64;
            let c4: Vec<&'static str> = acc.iter().filter_map(|a| match a { Some(Accepted::Partial(s)) => Some(*s), _ => None }).collect();
            assert_eq!(committed(&t, "reserve"), 1, "{msg}");
            assert_eq!(outcome(&out, "effect_completed") + c4.len() as i64, 1, "{msg}");
            assert_eq!(outcome(&out, "GRANT_EXHAUSTED"), 63 - c3, "{msg}");
            assert_eq!(count(&b, "SELECT count(*) FROM receipts"), 1);
            assert_eq!(count(&b, "SELECT count(*) FROM receipt_events WHERE cause = 'reserve'"), 1);
            assert_eq!(count(&b, "SELECT count(*) FROM grant_uses"), 1);
            assert_eq!(count(&b, "SELECT uses FROM grants WHERE label = 'g1'"), 1);
            let expected_status = match c4.first() {
                None => "observed_success",
                Some(&"dispatch") => "reserved",
                Some(_) => "dispatched",
            };
            assert_eq!(count(&b, &format!("SELECT count(*) FROM receipts WHERE status = '{expected_status}'")), 1, "{msg}");
            assert!(count(&b, "SELECT count(*) FROM receipt_events WHERE cause = 'dispatch'") <= 1);
            let (effects, consumed) = effects_and_permits(&b);
            assert!(effects <= 1 && consumed == effects);
            tally(&acc, &mut aborts, &mut partial);
            let mut audits = committed_audits(&t);

            // Same-key race: operations serialize on the key lock. No
            // duplicate dispatch; at most one effect; later operations
            // replay a completed receipt, resume a reserved one, or answer
            // RECONCILIATION_REQUIRED for a dispatched one.
            let before = effects_and_permits(&b);
            let write = requests(&approve, "same").1;
            let same: Vec<String> = vec!["same".to_string(); 64];
            let (out, t) = b.apply(&concurrent((0..64).map(|_| sealed(&initial, &write, None)).collect()), 2, None);
            let acc = accept_group(&b, &out, &t, &same, before);
            let msg = format!("rep {rep}: {}", json::to_text(&out));
            let failed = acc.iter().flatten().count() as i64;
            let rows = key_rows(&b, "same");
            assert_eq!(rows.receipts, 1, "{msg}");
            assert_eq!(count(&b, "SELECT count(*) FROM receipt_events WHERE idempotency_key = 'same' AND cause = 'reserve'"), 1, "exactly one reservation");
            assert!(committed(&t, "dispatch") <= 1 && rows.dispatch_events == committed(&t, "dispatch"), "duplicate dispatch: {msg}");
            assert_eq!(outcome(&out, "effect_completed"), committed(&t, "observe"), "{msg}");
            assert_eq!(rows.observe_events, committed(&t, "observe"));
            let (replayed, rr) = (outcome(&out, "replayed"), outcome(&out, "RECONCILIATION_REQUIRED"));
            assert_eq!(outcome(&out, "effect_completed") + replayed + rr + failed, 64, "{msg}");
            match rows.status.as_deref() {
                Some("observed_success") => assert_eq!((outcome(&out, "effect_completed"), rr), (1, 0), "{msg}"),
                Some("dispatched") => assert_eq!((outcome(&out, "effect_completed"), replayed, rows.dispatch_events), (0, 0, 1), "{msg}"),
                Some("reserved") => assert_eq!((outcome(&out, "effect_completed"), replayed, rr, rows.dispatch_events), (0, 0, 0, 0), "{msg}"),
                other => panic!("unexpected receipt state {other:?}: {msg}"),
            }
            let (effects, consumed) = effects_and_permits(&b);
            assert!(effects - before.0 <= 1 && consumed - before.1 == effects - before.0);
            tally(&acc, &mut aborts, &mut partial);
            audits += committed_audits(&t);
            assert_eq!(count(&b, "SELECT count(*) FROM audit_refusals"), audits);
            drop(b);
            std::fs::remove_dir_all(&dir).unwrap();
        }
        eprintln!("contention: {aborts} category 3 aborts and {partial} category 4 partial failures accepted in 20 x 128 operations (availability)");
    }

    /// A real verified abort (reserve busy at begin) is accepted; fabricated
    /// evidence that hides an unauthorized effect, a committed receipt, a
    /// grant use or a non-busy error is rejected; category 4 evidence is
    /// accepted only when the database rows match the implied state.
    #[test]
    fn d029_acceptance_rule_rejects_fabricated_evidence() {
        let dir = temp_dir("d029-accept");
        let (initial, approve) = approvable();
        let b = sqlite_broker(&initial, &dir);
        main_conn(&b, |c| c.execute_batch("PRAGMA busy_timeout = 50").unwrap());
        let holder = Connection::open(ledger_path(&b)).unwrap();
        holder.execute_batch("BEGIN IMMEDIATE").unwrap();
        let (out, real) = b.apply(&sealed(&initial, &requests(&approve, "busy").1, None), 0, None);
        holder.execute_batch("ROLLBACK").unwrap();
        assert_eq!(json::to_text(&out), LEDGER_REFUSED);
        let clean = KeyRows::default();
        let abort = Some(Accepted::Abort);
        assert_eq!(classify_failure(&real, Some(&key_rows(&b, "busy"))), abort);
        assert_eq!(classify_failure(&real, None), abort);

        let tx = |commit: bool, error: Option<TxError>| TxTime { begin_ns: Some(1), body_ns: 1, end_ns: Some(1), commit, error };
        let busy = Some(TxError { busy: true, at: TxAt::Begin });
        let with = |extra: Vec<(&'static str, TxTime)>| {
            let mut w = real.clone();
            w.tx.extend(extra);
            w
        };
        let rows = |status: &str, dispatch_events: i64, observe_events: i64| KeyRows {
            receipts: 1,
            events: 1 + dispatch_events + observe_events,
            grant_uses: 0,
            status: Some(status.to_string()),
            dispatch_events,
            observe_events,
        };
        // An effect hidden behind a busy reserve: dispatch, adapter, observe.
        assert_eq!(classify_failure(&with(vec![("dispatch", tx(true, None)), ("adapter", tx(true, None)), ("observe", tx(true, None))]), Some(&clean)), None);
        assert_eq!(classify_failure(&with(vec![("adapter", tx(true, None))]), None), None);
        // A committed receipt, its event or a grant use for the key.
        assert_eq!(classify_failure(&real, Some(&KeyRows { receipts: 1, ..clean.clone() })), None);
        assert_eq!(classify_failure(&real, Some(&KeyRows { events: 1, ..clean.clone() })), None);
        assert_eq!(classify_failure(&real, Some(&KeyRows { grant_uses: 1, ..clean.clone() })), None);
        // A committed reserve as the failing one, or a non-busy or non-begin error.
        let mut w = real.clone();
        w.tx[0].1 = tx(true, None);
        assert_eq!(classify_failure(&w, Some(&clean)), None);
        for e in [TxError { busy: false, at: TxAt::Begin }, TxError { busy: true, at: TxAt::Body }, TxError { busy: true, at: TxAt::End }, TxError { busy: false, at: TxAt::Body }] {
            let mut w = real.clone();
            w.tx[0].1 = tx(false, Some(e));
            assert_eq!(classify_failure(&w, Some(&clean)), None, "{e:?}");
        }
        // An audit failing with a non-busy error.
        let mut w = real.clone();
        w.tx.truncate(1);
        w.tx.push(("audit", tx(false, Some(TxError { busy: false, at: TxAt::Body }))));
        assert_eq!(classify_failure(&w, Some(&clean)), None);
        // No transaction at all, or another operation.
        let mut w = real.clone();
        w.tx.clear();
        assert_eq!(classify_failure(&w, Some(&clean)), None);
        let mut w = real.clone();
        w.op = "reconcile".into();
        assert_eq!(classify_failure(&w, Some(&clean)), None);

        // Category 4: accepted only with rows matching the implied state.
        let partial = |stage: &'static str| {
            let mut w = real.clone();
            let k = LEDGER_CHAIN.iter().position(|s| *s == stage).unwrap();
            w.tx = LEDGER_CHAIN[..k].iter().map(|n| (*n, tx(true, None))).collect();
            w.tx.push((stage, tx(false, busy)));
            w.tx.push(("audit", tx(true, None)));
            w
        };
        let (d, a, o) = (partial("dispatch"), partial("adapter"), partial("observe"));
        assert_eq!(classify_failure(&d, Some(&rows("reserved", 0, 0))), Some(Accepted::Partial("dispatch")));
        assert_eq!(classify_failure(&a, Some(&rows("dispatched", 1, 0))), Some(Accepted::Partial("adapter")));
        assert_eq!(classify_failure(&o, Some(&rows("dispatched", 1, 0))), Some(Accepted::Partial("observe")));
        assert_eq!(classify_failure(&d, None), Some(Accepted::Partial("dispatch")));
        // A claimed dispatch failure while the receipt was dispatched or completed.
        assert_eq!(classify_failure(&d, Some(&rows("dispatched", 1, 0))), None);
        assert_eq!(classify_failure(&d, Some(&rows("observed_success", 1, 1))), None);
        assert_eq!(classify_failure(&d, Some(&clean)), None, "no committed receipt behind a committed reserve");
        // A claimed adapter or observe failure with a completed receipt.
        assert_eq!(classify_failure(&a, Some(&rows("observed_success", 1, 1))), None);
        assert_eq!(classify_failure(&o, Some(&rows("reserved", 0, 0))), None);
        // Inconsistent chains: a gap, or an uncommitted earlier transaction.
        let mut gap = d.clone();
        gap.tx[1].0 = "adapter";
        assert_eq!(classify_failure(&gap, None), None);
        let mut uncommitted = a.clone();
        uncommitted.tx[1].1 = tx(false, None);
        assert_eq!(classify_failure(&uncommitted, None), None);
        // A claimed dispatch failure while an effect row exists: the group's
        // effects and consumed permits must match the evidence.
        assert!(accounting_ok(&[d.clone()], 0, 0));
        assert!(!accounting_ok(&[d.clone()], 1, 0), "effect hidden behind a dispatch failure");
        assert!(!accounting_ok(&[d.clone()], 1, 1));
        assert!(!accounting_ok(&[a.clone()], 0, 1), "consumed permit hidden behind an adapter failure");
        assert!(accounting_ok(&[o.clone()], 1, 1));
        assert!(!accounting_ok(&[o, real.clone()], 2, 2));
        drop(holder);
        drop(b);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    // ------------------------------------------------------------- D-026

    /// (tx name, outcome, error JSON) of one step's transactions.
    fn tx_errors(t: &StepTiming) -> Vec<(&'static str, &'static str, String)> {
        t.to_value().get("tx").unwrap().as_obj().unwrap().iter().map(|(n, x)| {
            let name = t.tx.iter().find(|(m, _)| m == n).unwrap().0;
            (name, if x.str_at("outcome") == Some("commit") { "commit" } else { "rollback" }, json::to_text(x.get("error").unwrap()))
        }).collect()
    }

    const BUSY_BEGIN: &str = r#"{"at":"begin","code":"busy"}"#;

    /// D-026: a `BEGIN IMMEDIATE` that times out on SQLITE_BUSY (another
    /// connection holds the write lock beyond a short test busy timeout) is
    /// `{"code":"busy","at":"begin"}` with outcome rollback.
    #[test]
    fn d026_busy_begin_is_busy_at_begin() {
        let dir = temp_dir("d026-busy");
        let (initial, approve) = approvable();
        let (_, w) = requests(&approve, "idem-busy");
        let b = sqlite_broker(&initial, &dir);
        main_conn(&b, |c| c.execute_batch("PRAGMA busy_timeout = 50").unwrap());
        let holder = Connection::open(ledger_path(&b)).unwrap();
        holder.execute_batch("BEGIN IMMEDIATE").unwrap();
        let (out, t) = b.apply(&sealed(&initial, &w, None), 0, None);
        holder.execute_batch("ROLLBACK").unwrap();
        drop(holder);
        assert_eq!(json::to_text(&out), LEDGER_REFUSED);
        assert_eq!(tx_errors(&t), [("reserve", "rollback", BUSY_BEGIN.to_string()), ("audit", "rollback", BUSY_BEGIN.to_string())]);
        let x = &t.tx[0].1;
        assert!(x.begin_ns.is_some() && x.body_ns == 0 && x.end_ns == Some(0));
        assert_eq!(count(&b, "SELECT count(*) FROM receipts"), 0);
        drop(b);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    /// D-026: an injected statement failure inside the body (TEMP trigger
    /// RAISE) and a self-raised integrity failure (C-4) are
    /// `{"code":"other","at":"body"}`; the audit row after it has no error.
    #[test]
    fn d026_body_failure_is_other_at_body() {
        let dir = temp_dir("d026-body");
        let (initial, approve) = approvable();
        let (publish, _) = requests(&approve, "idem-c4b");
        let (_, w) = requests(&approve, "idem-body");
        let b = sqlite_broker(&initial, &dir);
        let body = r#"{"at":"body","code":"other"}"#.to_string();
        main_conn(&b, |c| c.execute_batch("CREATE TEMP TRIGGER fail_reserve BEFORE INSERT ON receipts BEGIN SELECT RAISE(ABORT, 'x'); END").unwrap());
        let (out, t) = b.apply(&sealed(&initial, &w, None), 0, None);
        assert_eq!(json::to_text(&out), LEDGER_REFUSED);
        assert_eq!(tx_errors(&t), [("reserve", "rollback", body.clone()), ("audit", "commit", "null".to_string())]);
        main_conn(&b, |c| c.execute_batch("DROP TRIGGER temp.fail_reserve").unwrap());

        assert_eq!(json::to_text(&b.apply(&with_label(&approve, "g1"), 1, None).0), r#"{"result":"grant_issued"}"#);
        main_conn(&b, |c| c.execute("UPDATE grants SET binding = '[]' WHERE label = 'g1'", []).unwrap());
        let (out, t) = b.apply(&sealed(&initial, &publish, Some("g1")), 2, None);
        assert_eq!(json::to_text(&out), LEDGER_REFUSED);
        assert_eq!(tx_errors(&t), [("reserve", "rollback", body), ("audit", "commit", "null".to_string())]);
        drop(b);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    /// D-026: a failing COMMIT (deferred foreign key violation) is
    /// `{"code":"other","at":"end"}` with outcome rollback; the committed
    /// transactions before it have no error.
    #[test]
    fn d026_commit_failure_is_other_at_end() {
        let dir = temp_dir("d026-end");
        let (initial, approve) = approvable();
        let (_, w) = requests(&approve, "idem-end");
        let b = sqlite_broker(&initial, &dir);
        main_conn(&b, |c| {
            c.execute_batch(
                "CREATE TEMP TABLE fk_parent (id TEXT PRIMARY KEY);
                 CREATE TEMP TABLE fk_child (id TEXT REFERENCES fk_parent (id) DEFERRABLE INITIALLY DEFERRED);
                 CREATE TEMP TRIGGER fail_commit AFTER INSERT ON adapter_effects BEGIN INSERT INTO fk_child VALUES (NEW.permit_id); END",
            )
            .unwrap()
        });
        let (out, t) = b.apply(&sealed(&initial, &w, None), 0, None);
        assert_eq!(json::to_text(&out), LEDGER_REFUSED);
        let null = || "null".to_string();
        assert_eq!(
            tx_errors(&t),
            [
                ("reserve", "commit", null()),
                ("dispatch", "commit", null()),
                ("adapter", "rollback", r#"{"at":"end","code":"other"}"#.to_string()),
                ("audit", "commit", null()),
            ]
        );
        drop(b);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    /// D-026: successes and ordinary refusals (including ones that roll
    /// back) have `error: null`, on both ledgers; the member is always
    /// present.
    #[test]
    fn d026_ordinary_outcomes_have_null_error() {
        let (initial, approve) = approvable();
        let (p1, _) = requests(&approve, "idem-n1");
        let (p2, _) = requests(&approve, "idem-n2");
        let (_, w) = requests(&approve, "idem-nw");
        let steps = vec![
            with_label(&approve, "g1"),
            sealed(&initial, &w, None),
            sealed(&initial, &p1, Some("g1")),
            sealed(&initial, &p2, Some("g1")),
            sealed(&initial, &w, None),
            with_label(&approve, "g1"),
            present(&w, Some("mac")),
            present(&w, None),
            obj(vec![("op", s("reconcile"))]),
        ];
        let input = obj(vec![("initial", initial), ("steps", Value::Arr(steps))]);
        let dir = temp_dir("d026-null");
        for choice in [LedgerChoice::Memory, LedgerChoice::SqliteDir(dir.clone())] {
            let (_, t) = run_timed(&input, &choice);
            let mut rollbacks = 0;
            for st in &t.steps {
                for (name, outcome, error) in tx_errors(st) {
                    assert_eq!(error, "null", "{name}");
                    rollbacks += usize::from(outcome == "rollback");
                }
                assert!(st.tx.iter().all(|(_, x)| x.error.is_none()));
            }
            assert!(rollbacks >= 3, "{rollbacks}");
        }
        std::fs::remove_dir_all(&dir).unwrap();
    }

    // ------------------------------------------------------- D-027, D-028

    use sqlite::trace::Event;
    use std::sync::mpsc;
    use std::time::Duration;

    /// A group of `n` allowed writes with distinct keys, plus one
    /// `apply_without_permit` (its refusal audit is one more transaction).
    /// Not `reconcile`: a reconcile committed between a write's T-dispatch and
    /// T-observe makes T-observe change no row, which is a separate,
    /// pre-existing behaviour, not part of the release protocol under test.
    fn write_group(initial: &Value, approve: &Value, n: usize) -> Value {
        let mut ops: Vec<Value> = (0..n).map(|i| sealed(initial, &requests(approve, &format!("d028-{i}")).1, None)).collect();
        ops.push(obj(vec![("op", s("apply_without_permit"))]));
        concurrent(ops)
    }

    /// Run `group` as one step on its own thread and wait at most 60 s for it
    /// (a deadlocked release protocol fails the test instead of hanging it).
    fn bounded(b: &Arc<Broker>, group: &Value) -> (String, StepTiming) {
        let (tx, rx) = mpsc::channel();
        let (b, group) = (Arc::clone(b), group.clone());
        thread::spawn(move || {
            let (out, t) = b.apply(&group, 0, None);
            let _ = tx.send((json::to_text(&out), t));
        });
        rx.recv_timeout(Duration::from_secs(60)).expect("the concurrent group did not finish within 60 s")
    }

    /// `group` with a bounded wait, and the trace events of the ledger file
    /// recorded while it ran.
    fn traced_group(b: &Arc<Broker>, group: &Value) -> (String, StepTiming, Vec<Event>) {
        let path = ledger_path(b);
        let before = sqlite::trace::events(&path).len();
        let (out, t) = bounded(b, group);
        (out, t, sqlite::trace::events(&path).split_off(before))
    }

    fn positions(events: &[Event], e: Event) -> Vec<usize> {
        events.iter().enumerate().filter(|(_, x)| **x == e).map(|(i, _)| i).collect()
    }

    /// The single `Release` position of a traced group.
    fn release_at(ev: &[Event]) -> usize {
        let r = positions(ev, Event::Release);
        assert_eq!(r.len(), 1, "one release per group: {ev:?}");
        r[0]
    }

    /// The D-028 timing members of one group (LEDGER-SQLITE.md integrity
    /// rules); returns (release_delay_ns of each worker, start_skew_ns).
    fn check_group_timing(t: &StepTiming, profile: Profile, file: bool) -> (Vec<i64>, i64) {
        let g = t.group;
        assert!(g.ready_ns <= g.release_ns && g.release_ns <= t.total_ns, "{g:?}");
        let ws = t.operations.as_ref().unwrap();
        let starts: Vec<i64> = ws.iter().map(|w| w.worker.unwrap().start_ns).collect();
        let skew = if starts.len() > 1 { starts.iter().max().unwrap() - starts.iter().min().unwrap() } else { 0 };
        assert_eq!(g.start_skew_ns, skew);
        let mut delays = Vec::new();
        for w in ws {
            let x = w.worker.unwrap();
            assert!(x.ready_at_ns <= g.ready_ns, "a worker became ready after the group was observed ready");
            assert!(x.start_ns >= g.release_ns, "a worker started before release");
            assert_eq!(x.release_delay_ns, x.start_ns - g.release_ns);
            assert!(x.start_ns + w.total_ns <= t.total_ns);
            let parts: i64 = w.validate_ns.unwrap_or(0)
                + w.decide_ns.unwrap_or(0)
                + w.key_lock_wait_ns.unwrap_or(0)
                + w.tx.iter().map(|(_, x)| x.begin_ns.unwrap_or(0) + x.body_ns + x.end_ns.unwrap_or(0)).sum::<i64>();
            match (file, profile) {
                (false, _) => assert!(x.setup_ns.is_none() && parts <= w.total_ns),
                (true, Profile::Primary) => assert!(x.setup_ns.unwrap() <= x.ready_at_ns && parts <= w.total_ns),
                (true, Profile::Cold) => assert!(parts + x.setup_ns.unwrap() <= w.total_ns),
            }
            delays.push(x.release_delay_ns);
        }
        (delays, g.start_skew_ns)
    }

    fn sqlite_broker_with(initial: &Value, dir: &std::path::Path, profile: Profile) -> Broker {
        let mut b = sqlite_broker(initial, dir);
        b.profile = profile;
        b
    }

    /// Required tests item 9 (D-028), `primary`: every worker exists, has
    /// finished its connection setup (with read-back) and has signalled ready
    /// before the single release; no BEGIN and no open transaction before it;
    /// release_delay_ns and start_skew_ns are reported.
    #[test]
    fn d028_primary_every_worker_ready_and_set_up_before_release() {
        let (initial, approve) = approvable();
        let dir = temp_dir("d028-primary");
        for rep in 0..10 {
            let b = Arc::new(sqlite_broker_with(&initial, &dir, Profile::Primary));
            let n = 16;
            let (out, t, ev) = traced_group(&b, &write_group(&initial, &approve, n));
            assert_eq!(out, format!(r#"{{"outcomes":{{"PERMIT_MISSING":1,"effect_completed":{n}}},"result":"concurrent"}}"#), "rep {rep}");
            let release = release_at(&ev);
            for e in [Event::Spawned, Event::SetupStart, Event::SetupEnd, Event::Ready, Event::Pass] {
                assert_eq!(positions(&ev, e).len(), n + 1, "{e:?}");
            }
            for e in [Event::Spawned, Event::SetupEnd, Event::Ready] {
                assert!(positions(&ev, e).iter().all(|&p| p < release), "{e:?} after release: {ev:?}");
            }
            assert!(positions(&ev, Event::Pass).iter().all(|&p| p > release));
            let begin = positions(&ev, Event::Begin);
            assert_eq!(begin.len(), 4 * n + 1);
            assert!(begin.iter().all(|&p| p > release), "a transaction began before release: {ev:?}");
            assert!(positions(&ev, Event::TxOpenAtReady).is_empty(), "a transaction was open at ready");
            let (delays, _skew) = check_group_timing(&t, Profile::Primary, true);
            assert_eq!(delays.len(), n + 1);
            let v = json::to_text(&t.to_value());
            assert!(["\"release_ns\":", "\"start_skew_ns\":", "\"release_delay_ns\":", "\"ready_at_ns\":"].iter().all(|k| v.contains(k)), "{v}");
        }
        std::fs::remove_dir_all(&dir).unwrap();
    }

    /// Required tests item 9 (D-028), `cold`: workers still exist and signal
    /// ready before the single release; every setup and every BEGIN follows it.
    #[test]
    fn d028_cold_setup_and_begin_follow_release() {
        let (initial, approve) = approvable();
        let dir = temp_dir("d028-cold");
        for rep in 0..10 {
            let b = Arc::new(sqlite_broker_with(&initial, &dir, Profile::Cold));
            let n = 16;
            let (out, t, ev) = traced_group(&b, &write_group(&initial, &approve, n));
            assert_eq!(out, format!(r#"{{"outcomes":{{"PERMIT_MISSING":1,"effect_completed":{n}}},"result":"concurrent"}}"#), "rep {rep}");
            let release = release_at(&ev);
            for e in [Event::Spawned, Event::Ready] {
                let p = positions(&ev, e);
                assert!(p.len() == n + 1 && p.iter().all(|&p| p < release), "{e:?}: {ev:?}");
            }
            for e in [Event::SetupStart, Event::SetupEnd, Event::Pass, Event::Begin] {
                assert!(positions(&ev, e).iter().all(|&p| p > release), "{e:?} before release: {ev:?}");
            }
            assert_eq!(positions(&ev, Event::SetupStart).len(), n + 1);
            check_group_timing(&t, Profile::Cold, true);
        }
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn d028_memory_ledger_timing_in_both_profiles() {
        let (initial, approve) = approvable();
        for profile in [Profile::Primary, Profile::Cold] {
            let mut b = Broker::new(&initial, Ledger::Memory(Mutex::new(MemLedger::default())));
            b.profile = profile;
            let (out, t) = bounded(&Arc::new(b), &write_group(&initial, &approve, 8));
            assert_eq!(out, r#"{"outcomes":{"PERMIT_MISSING":1,"effect_completed":8},"result":"concurrent"}"#);
            check_group_timing(&t, profile, false);
            let v = json::to_text(&t.to_value());
            assert!(v.contains(r#""setup_ns":null,"start_ns":"#) && v.contains(r#""release_ns":"#), "{v}");
        }
        let b = Arc::new(Broker::new(&initial, Ledger::Memory(Mutex::new(MemLedger::default()))));
        let (_, t) = bounded(&b, &concurrent(vec![]));
        assert_eq!((t.group.ready_ns, t.group.release_ns, t.group.start_skew_ns), (0, 0, 0));
        let (_, t) = bounded(&b, &concurrent(vec![obj(vec![("op", s("reconcile"))])]));
        assert_eq!(t.group.start_skew_ns, 0);
        assert!(!json::to_text(&t.to_value()).contains("\"start_ns\":0,\"tx\""), "top-level steps carry no worker members");
    }

    /// A worker whose connection setup fails answers LEDGER_UNAVAILABLE, runs
    /// no ledger transaction and no audit, still counts as ready (no
    /// deadlock), in both profiles; nothing is created at the path.
    #[test]
    fn d028_worker_setup_failure_all_workers() {
        let (initial, approve) = approvable();
        let dir = temp_dir("d028-setup-failure");
        let missing = dir.join("missing").join("ledger.db");
        for profile in [Profile::Primary, Profile::Cold] {
            let mut b = sqlite_broker_with(&initial, &dir, profile);
            let Ledger::Sqlite(l) = &mut b.ledger else { unreachable!() };
            l.path = Some(missing.clone());
            let mut group = write_group(&initial, &approve, 3);
            if let Value::Obj(m) = &mut group {
                if let Some((_, Value::Arr(ops))) = m.iter_mut().find(|(k, _)| k == "operations") {
                    ops.push(concurrent(vec![obj(vec![("op", s("reconcile"))])]));
                }
            }
            let b = Arc::new(b);
            let (out, t) = bounded(&b, &group);
            assert_eq!(out, r#"{"outcomes":{"LEDGER_UNAVAILABLE":5},"result":"concurrent"}"#);
            for w in t.operations.as_ref().unwrap() {
                assert!(w.tx.is_empty() && w.validate_ns.is_none() && w.worker.unwrap().setup_ns.is_some(), "{w:?}");
            }
            check_group_timing(&t, profile, true);
            let nested = &t.operations.as_ref().unwrap()[4];
            assert_eq!((nested.op.as_str(), nested.group.release_ns, nested.operations.as_ref().map(Vec::len)), ("concurrent", 0, Some(0)));
            assert_eq!(count(&b, "SELECT count(*) FROM audit_refusals"), 0);
            assert_eq!(count(&b, "SELECT count(*) FROM receipts"), 0);
            assert!(!missing.parent().unwrap().exists());
        }
        let e = sqlite::open_worker(&missing).unwrap_err();
        assert_eq!(e.diagnostic(), "worker: SQLITE_CANTOPEN");
        std::fs::remove_dir_all(&dir).unwrap();
    }

    /// Exactly one worker's setup fails (test fault counter) among working
    /// ones, in both profiles: the group finishes, the failing worker answers
    /// LEDGER_UNAVAILABLE with no transaction, the others commit normally.
    #[test]
    fn d028_one_failing_worker_does_not_affect_the_others() {
        let (initial, approve) = approvable();
        let dir = temp_dir("d028-one-fails");
        for profile in [Profile::Primary, Profile::Cold] {
            for rep in 0..5 {
                let b = sqlite_broker_with(&initial, &dir, profile);
                let Ledger::Sqlite(l) = &b.ledger else { unreachable!() };
                l.fail_setups.store(1, std::sync::atomic::Ordering::SeqCst);
                let b = Arc::new(b);
                let n = 8;
                let ops = (0..n).map(|i| sealed(&initial, &requests(&approve, &format!("one-{i}")).1, None)).collect();
                let (out, t, ev) = traced_group(&b, &concurrent(ops));
                assert_eq!(out, format!(r#"{{"outcomes":{{"LEDGER_UNAVAILABLE":1,"effect_completed":{}}},"result":"concurrent"}}"#, n - 1), "{profile:?} rep {rep}");
                assert_eq!(positions(&ev, Event::Ready).len(), n);
                let ws = t.operations.as_ref().unwrap();
                assert_eq!(ws.iter().filter(|w| w.tx.is_empty()).count(), 1);
                assert!(ws.iter().filter(|w| !w.tx.is_empty()).all(|w| w.tx.len() == 4 && w.tx.iter().all(|(_, x)| x.commit && x.error.is_none())));
                check_group_timing(&t, profile, true);
                assert_eq!(count(&b, "SELECT count(*) FROM receipts"), n as i64 - 1);
                assert_eq!(count(&b, "SELECT count(*) FROM audit_refusals"), 0);
            }
        }
        std::fs::remove_dir_all(&dir).unwrap();
    }

    /// Worker setup verifies every setting by reading it back, and never
    /// creates a ledger file.
    #[test]
    fn d027_worker_setup_verifies_the_settings() {
        let dir = temp_dir("d027-verify");
        let path = valid_ledger(&dir);
        let c = sqlite::open_worker(&path).unwrap();
        sqlite::verify_settings(&c).unwrap();
        for (change, restore) in [
            ("PRAGMA busy_timeout = 100", "PRAGMA busy_timeout = 5000"),
            ("PRAGMA synchronous = NORMAL", "PRAGMA synchronous = FULL"),
            ("PRAGMA foreign_keys = OFF", "PRAGMA foreign_keys = ON"),
            ("PRAGMA trusted_schema = ON", "PRAGMA trusted_schema = OFF"),
        ] {
            c.execute_batch(change).unwrap();
            assert_eq!(sqlite::verify_settings(&c).unwrap_err().within(Category::Worker).diagnostic(), "worker: SQLITE_ERROR", "{change}");
            c.execute_batch(restore).unwrap();
        }
        drop(c);
        let fresh = dir.join("never-created.db");
        assert!(sqlite::open_worker(&fresh).is_err() && !fresh.exists());
        std::fs::remove_dir_all(&dir).unwrap();
    }

    // ------------------------------------------------------------- D-029

    /// The gate is not held by anyone (a poisoned gate after a panic is free).
    fn gate_free(b: &Broker) -> bool {
        !matches!(b.gate.try_write(), Err(std::sync::TryLockError::WouldBlock))
    }

    /// One top-level step with its trace events.
    fn traced_step(b: &Broker, op: &Value, step: usize) -> (String, Vec<Event>) {
        let path = ledger_path(b);
        let before = sqlite::trace::events(&path).len();
        let out = json::to_text(&b.apply(op, step, None).0);
        (out, sqlite::trace::events(&path).split_off(before))
    }

    fn with_fault(op: &Value, fault: &str) -> Value {
        with_member(op, "fault", s(fault))
    }

    /// Gate order: gate, then key lock, then every BEGIN; released only after
    /// the operation (and its audit) returned.
    fn assert_gate_order(ev: &[Event], gate: Event, key_lock: bool) {
        let g = positions(ev, gate);
        assert_eq!(g.len(), 1, "gate acquired exactly once: {ev:?}");
        let begins = positions(ev, Event::Begin);
        let returned = positions(ev, Event::Returned);
        assert_eq!(returned.len(), 1);
        if key_lock {
            let k = positions(ev, Event::KeyLock);
            assert!(k.len() == 1 && g[0] < k[0] && begins.iter().all(|&b| b > k[0]), "{ev:?}");
        }
        assert!(begins.iter().all(|&b| b > g[0] && b < returned[0]), "{ev:?}");
    }

    /// Required tests item 10: the gate is acquired in the stated order and
    /// released on every path: success, refusal before and inside the ledger,
    /// storage failure (and failing audit), every crash fault, effect failure,
    /// present_permit, reconcile, and a panic with the gate and key lock held.
    #[test]
    fn d029_gate_order_and_release_on_every_path() {
        let (initial, approve) = approvable();
        let dir = temp_dir("d029-paths");
        let b = sqlite_broker(&initial, &dir);
        let w = |k: &str| sealed(&initial, &requests(&approve, k).1, None);
        let cases: Vec<(Value, &str)> = vec![
            (w("ok"), r#"{"result":"effect_completed"}"#),
            (with_fault(&w("c0"), "crash_before_reserve"), r#"{"result":"crashed"}"#),
            (with_fault(&w("c1"), "crash_after_reserve"), r#"{"result":"crashed"}"#),
            (with_fault(&w("c2"), "crash_after_dispatch"), r#"{"result":"crashed"}"#),
            (with_fault(&w("ef"), "effect_fails"), r#"{"result":"effect_failed"}"#),
            (sealed(&initial, &requests(&approve, "nogrant").0, Some("missing")), r#"{"code":"GRANT_UNKNOWN","result":"refused"}"#),
        ];
        for (i, (op, expected)) in cases.iter().enumerate() {
            let (out, ev) = traced_step(&b, op, i);
            assert_eq!(&out, expected);
            assert_gate_order(&ev, Event::GateShared, true);
            assert!(gate_free(&b), "gate held after {expected}");
        }
        // Refused before the protected lifecycle: no gate at all.
        let mut bad = w("unsealed");
        if let Value::Obj(m) = &mut bad {
            m.retain(|(k, _)| k != "seal");
        }
        let (out, ev) = traced_step(&b, &bad, 10);
        assert_eq!(out, r#"{"code":"INGRESS_UNAUTHENTICATED","result":"refused"}"#);
        assert!(positions(&ev, Event::GateShared).is_empty() && gate_free(&b));
        // present_permit of the crashed dispatch (step 3): replayed, audited.
        let present = obj(vec![("op", s("present_permit")), ("of_step", Value::Int(3)), ("request", requests(&approve, "c2").1)]);
        let (out, ev) = traced_step(&b, &present, 11);
        assert_eq!(out, r#"{"code":"PERMIT_REPLAYED","result":"refused"}"#);
        assert_gate_order(&ev, Event::GateShared, false);
        assert!(gate_free(&b));
        // reconcile: exclusive before its BEGIN IMMEDIATE.
        let (out, ev) = traced_step(&b, &obj(vec![("op", s("reconcile"))]), 12);
        assert_eq!(out, r#"{"count":1,"result":"reconciled"}"#);
        assert_gate_order(&ev, Event::GateExclusive, false);
        assert!(gate_free(&b));
        // Storage failure in T-reserve, then also a failing audit.
        main_conn(&b, |c| c.execute_batch("CREATE TEMP TRIGGER fail_reserve BEFORE INSERT ON receipts BEGIN SELECT RAISE(ABORT, 'x'); END").unwrap());
        let (out, ev) = traced_step(&b, &w("sf"), 13);
        assert_eq!(out, LEDGER_REFUSED);
        assert_gate_order(&ev, Event::GateShared, true);
        assert!(gate_free(&b));
        main_conn(&b, |c| c.execute_batch("CREATE TEMP TRIGGER fail_audit BEFORE INSERT ON audit_refusals BEGIN SELECT RAISE(ABORT, 'x'); END").unwrap());
        let (out, ev) = traced_step(&b, &w("sf2"), 14);
        assert_eq!(out, LEDGER_REFUSED);
        assert_gate_order(&ev, Event::GateShared, true);
        assert!(gate_free(&b));
        main_conn(&b, |c| c.execute_batch("DROP TRIGGER temp.fail_reserve; DROP TRIGGER temp.fail_audit").unwrap());
        // Panic with the gate and the key lock held: both released by unwinding.
        let panicked = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| b.apply(&with_fault(&w("p"), "test_panic_after_reserve"), 15, None)));
        assert!(panicked.is_err());
        assert!(gate_free(&b), "gate held after a panic");
        assert_eq!(json::to_text(&b.apply(&w("after-panic"), 16, None).0), r#"{"result":"effect_completed"}"#);
        assert_eq!(json::to_text(&b.apply(&obj(vec![("op", s("reconcile"))]), 17, None).0), r#"{"count":0,"result":"reconciled"}"#);
        drop(b);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    /// Run `op` as a top-level step on its own thread; the receiver gets its
    /// output.
    fn spawn_step(b: &Arc<Broker>, op: Value) -> mpsc::Receiver<String> {
        let (tx, rx) = mpsc::channel();
        let b = Arc::clone(b);
        thread::spawn(move || {
            let _ = tx.send(json::to_text(&b.apply(&op, 0, None).0));
        });
        rx
    }

    /// Shared holders do not block each other; a reconcile waits for an
    /// in-flight shared holder; a held exclusive gate blocks a new execute
    /// only until it is released. Every wait is bounded.
    #[test]
    fn d029_shared_holders_concurrent_reconcile_waits() {
        let (initial, approve) = approvable();
        let dir = temp_dir("d029-blocking");
        let b = Arc::new(sqlite_broker(&initial, &dir));
        let w = |k: &str| sealed(&initial, &requests(&approve, k).1, None);
        let bound = Duration::from_secs(60);
        let wait = Duration::from_millis(300);

        // An in-flight execute (simulated shared holder) does not block another.
        let shared = b.gate.read().unwrap();
        assert_eq!(spawn_step(&b, w("s1")).recv_timeout(bound).unwrap(), r#"{"result":"effect_completed"}"#);
        // A reconcile waits for it.
        let rx = spawn_step(&b, obj(vec![("op", s("reconcile"))]));
        assert!(matches!(rx.recv_timeout(wait), Err(mpsc::RecvTimeoutError::Timeout)), "reconcile ran during an in-flight execute");
        drop(shared);
        assert_eq!(rx.recv_timeout(bound).unwrap(), r#"{"count":0,"result":"reconciled"}"#);

        // While a reconcile holds the gate, a new execute waits, then runs.
        let exclusive = b.gate.write().unwrap();
        let rx = spawn_step(&b, w("s2"));
        assert!(matches!(rx.recv_timeout(wait), Err(mpsc::RecvTimeoutError::Timeout)), "execute ran while reconcile held the gate");
        drop(exclusive);
        assert_eq!(rx.recv_timeout(bound).unwrap(), r#"{"result":"effect_completed"}"#);
        assert!(gate_free(&b));
        drop(b);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    /// `n_exec` executes, `n_crash` with crash_after_dispatch, `n_rec`
    /// reconciles in one group; keys `{salt}e{i}` and `{salt}c{i}`.
    fn mixed_group(initial: &Value, approve: &Value, n_exec: usize, n_crash: usize, n_rec: usize, salt: &str) -> (Value, Vec<String>) {
        let mut ops = Vec::new();
        let mut keys = Vec::new();
        for i in 0..n_exec {
            keys.push(format!("{salt}e{i}"));
            ops.push(sealed(initial, &requests(approve, &keys[keys.len() - 1]).1, None));
        }
        for i in 0..n_crash {
            keys.push(format!("{salt}c{i}"));
            ops.push(with_fault(&sealed(initial, &requests(approve, &keys[keys.len() - 1]).1, None), "crash_after_dispatch"));
        }
        for _ in 0..n_rec {
            keys.push(String::new());
            ops.push(obj(vec![("op", s("reconcile"))]));
        }
        (concurrent(ops), keys)
    }

    /// No worker reports a state conflict or any non-busy error.
    fn no_state_conflict(t: &StepTiming) {
        for w in t.operations.as_ref().unwrap() {
            assert!(w.tx.iter().all(|(_, x)| x.error.is_none_or(|e| e.busy)), "non-busy error: {:?}", tx_errors(w));
        }
    }

    /// Reconcile and executes in one group, both profiles, repeated, bounded:
    /// reconcile never changes an in-flight execute (every non-crashed
    /// execute completes; only crashed operations' receipts are reconciled).
    #[test]
    fn d029_reconcile_never_touches_an_in_flight_execute() {
        let (initial, approve) = approvable();
        let dir = temp_dir("d029-mixed");
        for profile in [Profile::Primary, Profile::Cold] {
            for rep in 0..10 {
                let b = Arc::new(sqlite_broker_with(&initial, &dir, profile));
                let salt = format!("r{rep}");
                let (group, _) = mixed_group(&initial, &approve, 8, 3, 8, &salt);
                let (out, t) = bounded(&b, &group);
                assert_eq!(out, r#"{"outcomes":{"crashed":3,"effect_completed":8,"reconciled":8},"result":"concurrent"}"#, "{profile:?} rep {rep}");
                no_state_conflict(&t);
                assert_eq!(count(&b, "SELECT count(*) FROM receipts WHERE status = 'observed_success'"), 8);
                assert_eq!(count(&b, "SELECT count(*) FROM receipts WHERE status IN ('dispatched', 'unknown_reconcile')"), 3);
                let unknown = count(&b, "SELECT count(*) FROM receipts WHERE status = 'unknown_reconcile'");
                assert_eq!(count(&b, &format!("SELECT count(*) FROM receipts WHERE status = 'unknown_reconcile' AND idempotency_key LIKE '{salt}c%'")), unknown);
                assert_eq!(count(&b, "SELECT count(*) FROM receipt_events WHERE cause = 'reconcile'"), unknown);
                assert_eq!(count(&b, "SELECT count(*) FROM adapter_effects"), 11);
            }
        }
        std::fs::remove_dir_all(&dir).unwrap();
    }

    /// 64 executes and reconciles in one group, both profiles: no deadlock
    /// (bounded wait), no state conflict, every success durable.
    #[test]
    fn d029_64_way_executes_and_reconciles_do_not_deadlock() {
        let (initial, approve) = approvable();
        let dir = temp_dir("d029-64");
        for profile in [Profile::Primary, Profile::Cold] {
            for rep in 0..3 {
                let b = Arc::new(sqlite_broker_with(&initial, &dir, profile));
                let (group, keys) = mixed_group(&initial, &approve, 48, 8, 8, &format!("x{rep}"));
                let (out, t) = bounded(&b, &group);
                let v = json::parse(out.as_bytes()).unwrap();
                let total: i64 = v.get("outcomes").and_then(Value::as_obj).unwrap().iter().filter_map(|(_, n)| n.as_int()).sum();
                assert_eq!(total, 64, "{out}");
                no_state_conflict(&t);
                let exec_keys: Vec<String> = keys.iter().map(|k| if k.is_empty() { "reconcile".to_string() } else { k.clone() }).collect();
                let ws = t.operations.as_ref().unwrap();
                for (w, k) in ws.iter().zip(&exec_keys) {
                    if w.op == "execute" && w.tx.iter().any(|(n, x)| *n != "audit" && x.error.is_some()) {
                        let rows = key_rows(&b, k);
                        assert!(classify_failure(w, Some(&rows)).is_some(), "{:?} {rows:?}", tx_errors(w));
                    }
                }
                assert_eq!(count(&b, "SELECT count(*) FROM receipts WHERE status = 'observed_success'"), outcome(&v, "effect_completed"));
                assert_eq!(count(&b, "SELECT count(*) FROM adapter_effects"), count(&b, "SELECT count(*) FROM adapter_consumed_permits"));
                assert!(gate_free(&b));
            }
        }
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
