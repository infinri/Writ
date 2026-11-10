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
use crate::timing::{between, since, ScenarioTiming, StepTiming};
use hmac::{Hmac, Mac};
use rusqlite::Connection;
use sha2::Sha256;
use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::PathBuf;
use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::{Arc, Barrier, Mutex, MutexGuard, RwLock};
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
}

impl SqlLedger {
    fn connect(&self) -> R<Connection> {
        sqlite::open(self.path.as_ref().ok_or_else(|| DbError::new("SQLITE_CANTOPEN").within(Category::Open))?)
    }

    /// A `concurrent` worker's connection (D-027 worker connection setup).
    fn connect_worker(&self) -> R<Connection> {
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

struct Broker {
    profile: Profile,
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
        Broker { profile: Profile::Primary, config: RwLock::new(config), permits: Mutex::new(HashMap::new()), ledger }
    }

    /// The in-process lock of one (tenant, idempotency key); SQLite only (the
    /// in-memory ledger runs every operation exclusively).
    fn key_lock(&self, key: &Key) -> Option<Arc<Mutex<()>>> {
        match &self.ledger {
            Ledger::Sqlite(l) => Some(lock(&l.key_locks).entry(key.clone()).or_default().clone()),
            Ledger::Memory(_) => None,
        }
    }

    fn execute(&self, cfg: &Config, store: &Store, t: &mut StepTiming, op: &Value, step: usize) -> R<Value> {
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
        let started = Clock::now();
        let key_lock = self.key_lock(&key);
        let _held = key_lock.as_deref().map(lock);
        if key_lock.is_some() {
            t.key_lock_wait_ns = Some(since(started));
        }

        let expected = grant_binding(&req, &d, "approved");
        if let Some(done) = store.reserve(t, &key, &fingerprint, effective_grant, &expected, cfg.now, fault == "crash_before_reserve")? {
            return Ok(done);
        }
        if fault == "crash_after_reserve" {
            return Ok(result("crashed"));
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

    fn present_permit(&self, cfg: &Config, store: &Store, t: &mut StepTiming, op: &Value) -> R<Value> {
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

    fn operate(&self, cfg: &Config, store: &Store, t: &mut StepTiming, op: &Value, step: usize) -> R<Value> {
        match op.str_at("op").unwrap_or("") {
            "execute" => self.execute(cfg, store, t, op, step),
            "approve" => self.approve(cfg, store, t, op),
            "present_permit" => self.present_permit(cfg, store, t, op),
            "apply_without_permit" => Ok(refused("PERMIT_MISSING")),
            "reconcile" => Ok(obj(vec![("result", s("reconciled")), ("count", Value::Int(store.reconcile(t)?))])),
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
        let attempt = if configures {
            let mut cfg = self.config.write().unwrap_or_else(|e| e.into_inner());
            self.configure(&mut cfg, op)
        } else if matches!(self.ledger, Ledger::Memory(_)) {
            let cfg = self.config.write().unwrap_or_else(|e| e.into_inner());
            self.with_store(own, |st| self.operate(&cfg, st, t, op, step))
        } else {
            let cfg = self.config.read().unwrap_or_else(|e| e.into_inner());
            self.with_store(own, |st| self.operate(&cfg, st, t, op, step))
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

    /// A `concurrent` group under the selected profile (LEDGER-SQLITE.md
    /// "Concurrency profiles", D-027). One OS thread per operation, released
    /// together by a `std::sync::Barrier` (the release mechanism is unchanged).
    ///
    /// Each worker records its ready point (`primary` SQLite: the end of its
    /// connection setup; otherwise the instant the thread starts with its
    /// operation) into a shared maximum before it waits at the barrier. The
    /// barrier returns in any thread only after every thread has called
    /// `wait`, so every ready reading is taken before release and is visible
    /// once the threads are joined; each worker's `start_ns` is read after its
    /// `wait` returns, hence `start_ns >= ready_ns`. A worker's `total_ns` runs
    /// from that reading to its result; its connection is closed afterwards,
    /// inside the group's `total_ns`.
    fn group(&self, ops: &[Value], step: usize, t: &mut StepTiming, dispatched: Clock) -> (Vec<Value>, Vec<StepTiming>) {
        let barrier = Barrier::new(ops.len());
        let ready = AtomicI64::new(0);
        let cold = self.profile == Profile::Cold;
        let done: Vec<(Value, StepTiming)> = thread::scope(|scope| {
            let handles: Vec<_> = ops
                .iter()
                .map(|o| {
                    let (barrier, ready) = (&barrier, &ready);
                    scope.spawn(move || {
                        let created = Clock::now();
                        let (mut conn, mut setup_ns, mut ready_at) = (None, None, created);
                        if !cold {
                            let ended;
                            (conn, setup_ns, ended) = self.worker_setup();
                            ready_at = ended.unwrap_or(created);
                        }
                        ready.fetch_max(between(dispatched, ready_at), Ordering::SeqCst);
                        #[cfg(test)]
                        self.trace(sqlite::trace::Event::Arrive);
                        barrier.wait();
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
                                (wt.ready_ns, wt.operations) = (Some(0), Some(Vec::new()));
                            }
                            refused("LEDGER_UNAVAILABLE")
                        } else {
                            self.dispatch_step(o, step, conn.as_ref(), &mut wt, passed)
                        };
                        wt.total_ns = between(passed, Clock::now());
                        wt.worker = Some((setup_ns, between(dispatched, passed)));
                        drop(conn);
                        (out, wt)
                    })
                })
                .collect();
            handles
                .into_iter()
                .zip(ops)
                .map(|(h, o)| h.join().unwrap_or_else(|_| (refused("INTERNAL_ERROR"), StepTiming::new(o.str_at("op")))))
                .collect()
        });
        t.ready_ns = Some(ready.into_inner());
        done.into_iter().unzip()
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
    Ok(Ledger::Sqlite(SqlLedger { path: Some(path), main: Mutex::new(Some(c)), key_locks: Mutex::new(HashMap::new()) }))
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

    #[test]
    fn contention_on_one_grant_and_one_key() {
        let (initial, approve) = approvable();
        for rep in 0..20 {
            let dir = temp_dir(&format!("contention-{rep}"));
            let b = sqlite_broker(&initial, &dir);
            assert_eq!(json::to_text(&b.apply(&with_label(&approve, "g1"), 0, None).0), r#"{"result":"grant_issued"}"#);
            let ops = (0..64).map(|i| sealed(&initial, &requests(&approve, &format!("k{i}")).0, Some("g1"))).collect();
            let out = b.apply(&concurrent(ops), 1, None).0;
            assert_eq!(json::to_text(&out), r#"{"outcomes":{"GRANT_EXHAUSTED":63,"effect_completed":1},"result":"concurrent"}"#, "rep {rep}");
            assert_eq!(count(&b, "SELECT count(*) FROM receipts"), 1);
            assert_eq!(count(&b, "SELECT count(*) FROM receipt_events WHERE cause = 'reserve'"), 1);
            assert_eq!(count(&b, "SELECT count(*) FROM grant_uses"), 1);
            assert_eq!(count(&b, "SELECT uses FROM grants WHERE label = 'g1'"), 1);

            let write = requests(&approve, "same").1;
            let out = b.apply(&concurrent((0..64).map(|_| sealed(&initial, &write, None)).collect()), 2, None).0;
            assert_eq!(json::to_text(&out), r#"{"outcomes":{"effect_completed":1,"replayed":63},"result":"concurrent"}"#, "rep {rep}");
            assert_eq!(count(&b, "SELECT count(*) FROM receipt_events WHERE idempotency_key = 'same' AND cause = 'dispatch'"), 1);
            assert_eq!(count(&b, "SELECT count(*) FROM adapter_effects"), 2);
            assert_eq!(count(&b, "SELECT count(*) FROM audit_refusals"), 63);
            drop(b);
            std::fs::remove_dir_all(&dir).unwrap();
        }
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

    // ------------------------------------------------------------- D-027

    use sqlite::trace::Event;

    /// A group of `n` allowed writes with distinct keys, plus one reconcile.
    fn write_group(initial: &Value, approve: &Value, n: usize) -> Value {
        let mut ops: Vec<Value> = (0..n).map(|i| sealed(initial, &requests(approve, &format!("d027-{i}")).1, None)).collect();
        ops.push(obj(vec![("op", s("reconcile"))]));
        concurrent(ops)
    }

    /// Run `group` as one step and return its output, timing and the trace
    /// events of the ledger file recorded while it ran.
    fn traced_group(b: &Broker, group: &Value) -> (String, StepTiming, Vec<Event>) {
        let path = ledger_path(b);
        let before = sqlite::trace::events(&path).len();
        let (out, t) = b.apply(group, 0, None);
        (json::to_text(&out), t, sqlite::trace::events(&path).split_off(before))
    }

    fn positions(events: &[Event], e: Event) -> Vec<usize> {
        events.iter().enumerate().filter(|(_, x)| **x == e).map(|(i, _)| i).collect()
    }

    /// The D-027 timing members of one group: `start_ns >= ready_ns`, workers
    /// inside the group, setup accounting per profile.
    fn check_group_timing(t: &StepTiming, profile: Profile, file: bool) {
        let ready = t.ready_ns.unwrap();
        assert!(ready <= t.total_ns);
        for w in t.operations.as_ref().unwrap() {
            let (setup, start) = w.worker.unwrap();
            assert!(start >= ready, "worker started before the group was ready");
            assert!(start + w.total_ns <= t.total_ns);
            let parts: i64 = w.validate_ns.unwrap_or(0)
                + w.decide_ns.unwrap_or(0)
                + w.key_lock_wait_ns.unwrap_or(0)
                + w.tx.iter().map(|(_, x)| x.begin_ns.unwrap_or(0) + x.body_ns + x.end_ns.unwrap_or(0)).sum::<i64>();
            match (file, profile) {
                (false, _) => assert!(setup.is_none() && parts <= w.total_ns),
                (true, Profile::Primary) => assert!(setup.unwrap() <= ready && parts <= w.total_ns),
                (true, Profile::Cold) => assert!(parts + setup.unwrap() <= w.total_ns),
            }
        }
    }

    /// Required tests item 9, `primary`: every worker's connection setup has
    /// finished, and every worker has reached the barrier, before any worker
    /// passes it or begins a transaction.
    #[test]
    fn d027_primary_sets_up_every_connection_before_any_transaction() {
        let (initial, approve) = approvable();
        let dir = temp_dir("d027-primary");
        for rep in 0..10 {
            let b = sqlite_broker(&initial, &dir);
            let n = 16;
            let (out, t, ev) = traced_group(&b, &write_group(&initial, &approve, n));
            assert_eq!(out, format!(r#"{{"outcomes":{{"effect_completed":{n},"reconciled":1}},"result":"concurrent"}}"#), "rep {rep}");
            let (setup_end, arrive, pass, begin) = (positions(&ev, Event::SetupEnd), positions(&ev, Event::Arrive), positions(&ev, Event::Pass), positions(&ev, Event::Begin));
            assert_eq!((setup_end.len(), arrive.len(), pass.len()), (n + 1, n + 1, n + 1));
            assert_eq!(begin.len(), 4 * n + 1);
            let ready_point = setup_end.iter().chain(&arrive).max().unwrap();
            assert!(ready_point < pass.iter().min().unwrap(), "a worker passed the barrier before every setup finished: {ev:?}");
            assert!(ready_point < begin.iter().min().unwrap(), "a transaction began before every setup finished: {ev:?}");
            check_group_timing(&t, Profile::Primary, true);
        }
        std::fs::remove_dir_all(&dir).unwrap();
    }

    /// Required tests item 9, `cold`: every worker connection is created
    /// after release (after the last worker reached the barrier), and its
    /// setup is inside the worker's `total_ns`.
    #[test]
    fn d027_cold_creates_every_connection_after_release() {
        let (initial, approve) = approvable();
        let dir = temp_dir("d027-cold");
        for rep in 0..10 {
            let mut b = sqlite_broker(&initial, &dir);
            b.profile = Profile::Cold;
            let n = 16;
            let (out, t, ev) = traced_group(&b, &write_group(&initial, &approve, n));
            assert_eq!(out, format!(r#"{{"outcomes":{{"effect_completed":{n},"reconciled":1}},"result":"concurrent"}}"#), "rep {rep}");
            let (setup_start, arrive, begin) = (positions(&ev, Event::SetupStart), positions(&ev, Event::Arrive), positions(&ev, Event::Begin));
            assert_eq!((setup_start.len(), arrive.len()), (n + 1, n + 1));
            let released = arrive.iter().max().unwrap();
            assert!(released < setup_start.iter().min().unwrap(), "a connection was created before release: {ev:?}");
            assert!(released < begin.iter().min().unwrap());
            check_group_timing(&t, Profile::Cold, true);
        }
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn d027_memory_ledger_timing_in_both_profiles() {
        let (initial, approve) = approvable();
        for profile in [Profile::Primary, Profile::Cold] {
            let mut b = Broker::new(&initial, Ledger::Memory(Mutex::new(MemLedger::default())));
            b.profile = profile;
            let (out, t) = b.apply(&write_group(&initial, &approve, 8), 0, None);
            assert_eq!(json::to_text(&out), r#"{"outcomes":{"effect_completed":8,"reconciled":1},"result":"concurrent"}"#);
            check_group_timing(&t, profile, false);
            let v = json::to_text(&t.to_value());
            assert!(v.contains(r#""setup_ns":null,"start_ns":"#) && v.contains(r#""ready_ns":"#), "{v}");
        }
        let (_, t) = Broker::new(&initial, Ledger::Memory(Mutex::new(MemLedger::default()))).apply(&concurrent(vec![]), 0, None);
        assert_eq!(t.ready_ns, Some(0));
        assert!(!json::to_text(&t.to_value()).contains("start_ns"), "top-level steps carry no worker members");
    }

    /// A worker whose connection setup fails answers LEDGER_UNAVAILABLE, runs
    /// no ledger transaction and no audit (`tx` `{}`, not counted in
    /// `final.refusals`), in both profiles; nothing is created at the path.
    #[test]
    fn d027_worker_setup_failure() {
        let (initial, approve) = approvable();
        let dir = temp_dir("d027-setup-failure");
        let missing = dir.join("missing").join("ledger.db");
        for profile in [Profile::Primary, Profile::Cold] {
            let mut b = sqlite_broker(&initial, &dir);
            b.profile = profile;
            let Ledger::Sqlite(l) = &mut b.ledger else { unreachable!() };
            l.path = Some(missing.clone());
            let mut group = write_group(&initial, &approve, 3);
            if let Value::Obj(m) = &mut group {
                if let Some((_, Value::Arr(ops))) = m.iter_mut().find(|(k, _)| k == "operations") {
                    ops.push(concurrent(vec![obj(vec![("op", s("reconcile"))])]));
                }
            }
            let (out, t) = b.apply(&group, 0, None);
            assert_eq!(json::to_text(&out), r#"{"outcomes":{"LEDGER_UNAVAILABLE":5},"result":"concurrent"}"#);
            for w in t.operations.as_ref().unwrap() {
                assert!(w.tx.is_empty() && w.validate_ns.is_none() && w.worker.unwrap().0.is_some(), "{w:?}");
            }
            check_group_timing(&t, profile, true);
            let nested = &t.operations.as_ref().unwrap()[4];
            assert_eq!((nested.op.as_str(), nested.ready_ns, nested.operations.as_ref().map(Vec::len)), ("concurrent", Some(0), Some(0)));
            assert_eq!(count(&b, "SELECT count(*) FROM audit_refusals"), 0);
            assert_eq!(count(&b, "SELECT count(*) FROM receipts"), 0);
            assert!(!missing.parent().unwrap().exists());
        }
        let e = sqlite::open_worker(&missing).unwrap_err();
        assert_eq!(e.diagnostic(), "worker: SQLITE_CANTOPEN");
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
}
