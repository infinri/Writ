//! Broker scenarios (`broker-scenario`, m1/fixtures/broker/BROKER.md):
//! sealed ingress, use-time re-decision, approval grants, idempotent
//! receipts, permits verified by the effect adapter, crashes, reconciliation.
//!
//! SIMULATED, not verified:
//! - ingress seals and permit MACs are HMAC-SHA256 under fixture keys held in
//!   scenario state (no key management, rotation or registration);
//! - the ledger is an in-memory model behind one mutex (each operation is one
//!   serializable transaction); `fault` injects a crash at a named point;
//! - an "authenticated human" is a credential match on a fixture channel;
//! - the effect adapter only increments a counter.

use crate::json::{self, obj, s, Value};
use crate::kernel::{self, Bundle, Decision, Policy};
use crate::time::{self, Instant};
use hmac::{Hmac, Mac};
use sha2::Sha256;
use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::{Barrier, Mutex};
use std::thread;

type HmacSha256 = Hmac<Sha256>;
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

struct Broker {
    now: Instant,
    state_revision: i64,
    bundle: Result<Bundle, ()>,
    ingress_keys: Value,
    permit_key: Vec<u8>,
    channels: Value,
    policy_up: bool,
    ledger_up: bool,
    grants: BTreeMap<String, Grant>,
    receipts: HashMap<(String, String), Receipt>,
    permits: HashMap<usize, Permit>,
    consumed_permits: HashSet<String>,
    next_permit: u64,
    effects_applied: i64,
    refusals: i64,
}

fn refused(code: &str) -> Value {
    obj(vec![("result", s("refused")), ("code", s(code))])
}

fn result(r: &str) -> Value {
    obj(vec![("result", s(r))])
}

fn replayed(r: &str) -> Value {
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

impl Broker {
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

    /// Effect adapter (simulated): fingerprint, MAC, expiry, single use; then apply.
    fn adapter(&mut self, permit: &Permit, req: &Value, fails: bool) -> Result<bool, &'static str> {
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
        let id = permit.body.str_at("permit_id").unwrap_or("").to_string();
        if !self.consumed_permits.insert(id) {
            return Err("PERMIT_REPLAYED");
        }
        if fails {
            return Ok(false);
        }
        self.effects_applied += 1;
        Ok(true)
    }

    fn execute(&mut self, op: &Value, step: usize) -> Value {
        let req = op.get("request").cloned().unwrap_or(Value::Null);
        if !self.seal_valid(&req, op.get("seal")) {
            return refused("INGRESS_UNAUTHENTICATED");
        }
        if !self.ledger_up {
            return refused("LEDGER_UNAVAILABLE");
        }
        let d = self.decide(&req);
        if !matches!(d.status, "allow" | "require_approval") {
            return refused(&d.reason);
        }
        let grant_ref = op.str_at("grant_ref");
        if d.status == "require_approval" && grant_ref.is_none() {
            return refused("APPROVAL_REQUIRED");
        }
        let fault = op.str_at("fault").unwrap_or("");
        let effective_grant = if d.status == "require_approval" { grant_ref.map(str::to_string) } else { None };
        let fingerprint = json::digest(&req).unwrap_or_default();
        let rkey = (req.str_at("tenant_id").unwrap_or("").to_string(), req.str_at("idempotency_key").unwrap_or("").to_string());

        let resume = match self.receipts.get(&rkey) {
            Some(r) if r.fingerprint != fingerprint || r.grant != effective_grant => return refused("IDEMPOTENCY_CONFLICT"),
            Some(r) => match r.status {
                ReceiptStatus::ObservedSuccess => return replayed("effect_completed"),
                ReceiptStatus::ObservedFailure => return replayed("effect_failed"),
                ReceiptStatus::Dispatched | ReceiptStatus::UnknownReconcile => return refused("RECONCILIATION_REQUIRED"),
                ReceiptStatus::Reserved => true,
            },
            None => false,
        };
        if !resume {
            if fault == "crash_before_reserve" {
                return result("crashed");
            }
            if let Some(label) = &effective_grant {
                let Some(g) = self.grants.get_mut(label) else { return refused("GRANT_UNKNOWN") };
                if g.binding != grant_binding(&req, &d, "approved") {
                    return refused("GRANT_MISMATCH");
                }
                if self.now >= g.expires {
                    return refused("GRANT_EXPIRED");
                }
                if g.uses >= g.max_uses {
                    return refused("GRANT_EXHAUSTED");
                }
                g.uses += 1;
            }
            self.receipts.insert(rkey.clone(), Receipt { fingerprint: fingerprint.clone(), grant: effective_grant, status: ReceiptStatus::Reserved });
        }
        if fault == "crash_after_reserve" {
            return result("crashed");
        }

        // Permit bound to the whole request, the bundle and min(now + 30 s, deadline).
        self.next_permit += 1;
        let mut expires = time::add_seconds(self.now, PERMIT_TTL_SECONDS);
        if let Some(dl) = req.str_at("deadline_at").and_then(time::parse_rfc3339) {
            expires = expires.min(dl);
        }
        let body = obj(vec![
            ("permit_id", s(&format!("permit-{}", self.next_permit))),
            ("request_fingerprint", s(&fingerprint)),
            ("policy_bundle_sha", s(&d.policy_sha)),
            ("expires_at", s(&time::format(expires))),
        ]);
        let permit = Permit { mac: self.sign_permit(&body), body };
        self.permits.insert(step, permit.clone());
        self.receipts.get_mut(&rkey).unwrap().status = ReceiptStatus::Dispatched;
        let applied = match self.adapter(&permit, &req, fault == "effect_fails") {
            Ok(applied) => applied,
            Err(code) => return refused(code),
        };
        if fault == "crash_after_dispatch" {
            return result("crashed");
        }
        let receipt = self.receipts.get_mut(&rkey).unwrap();
        if applied {
            receipt.status = ReceiptStatus::ObservedSuccess;
            result("effect_completed")
        } else {
            receipt.status = ReceiptStatus::ObservedFailure;
            result("effect_failed")
        }
    }

    /// Separate trusted approval channel (simulated credential match).
    fn approve(&mut self, op: &Value) -> Value {
        let channel = op.str_at("channel").and_then(|c| self.channels.get(c));
        let human = op.str_at("human").unwrap_or("");
        let credential = op.str_at("credential");
        if credential.is_none() || channel.and_then(|c| c.str_at(human)) != credential {
            return refused("HUMAN_UNAUTHENTICATED");
        }
        let req = op.get("for_request").cloned().unwrap_or(Value::Null);
        let d = self.decide(&req);
        if d.status != "require_approval" {
            return refused("NOT_APPROVABLE");
        }
        let principal = req.get("principal").and_then(|p| p.str_at("id"));
        if Some(human) == principal || Some(human) == req.str_at("agent_id") {
            return refused("SELF_APPROVAL_FORBIDDEN");
        }
        let label = op.str_at("grant_ref").unwrap_or("").to_string();
        // A label names one grant for its whole life; reusing it would reset
        // the grant's uses.
        if self.grants.contains_key(&label) {
            return refused("GRANT_REF_IN_USE");
        }
        let ttl = op.get("ttl_seconds").and_then(Value::as_int).unwrap_or(DEFAULT_GRANT_TTL_SECONDS);
        let verdict = op.str_at("verdict").unwrap_or("");
        self.grants.insert(
            label,
            Grant { binding: grant_binding(&req, &d, verdict), expires: time::add_seconds(self.now, ttl), max_uses: 1, uses: 0 },
        );
        result("grant_issued")
    }

    fn present_permit(&mut self, op: &Value) -> Value {
        let step = op.get("of_step").and_then(Value::as_int).unwrap_or(-1);
        let Some(mut permit) = usize::try_from(step).ok().and_then(|i| self.permits.get(&i)).cloned() else {
            return refused("PERMIT_MISSING");
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
        match self.adapter(&permit, &req, false) {
            Ok(_) => result("effect_applied"),
            Err(code) => refused(code),
        }
    }

    /// `restart_broker` (D-020): simulated process restart. The exhaustive
    /// pattern forces every broker field to be classified as surviving a
    /// restart. Ledger: grants and their uses, receipts and their status,
    /// consumed permit ids, the permit id counter (new ids never collide with
    /// consumed ones) and the counters reported in `final`. Trusted
    /// configuration reloaded at start: clock, kernel state, active bundle,
    /// keys, channels, dependency status. Issued permits are client-held, so
    /// they stay presentable. Volatile state (the lock, in-flight transaction
    /// values) exists only on the stack of `apply` and is gone once an
    /// operation returns, so nothing in this struct is dropped.
    fn restart(&mut self) {
        let Broker {
            now: _,
            state_revision: _,
            bundle: _,
            ingress_keys: _,
            permit_key: _,
            channels: _,
            policy_up: _,
            ledger_up: _,
            grants: _,
            receipts: _,
            permits: _,
            consumed_permits: _,
            next_permit: _,
            effects_applied: _,
            refusals: _,
        } = self;
    }

    fn apply_locked(&mut self, op: &Value, step: usize) -> Value {
        let out = match op.str_at("op").unwrap_or("") {
            "execute" => self.execute(op, step),
            "approve" => self.approve(op),
            "present_permit" => self.present_permit(op),
            "apply_without_permit" => refused("PERMIT_MISSING"),
            "restart_broker" => {
                self.restart();
                result("restarted")
            }
            "activate_bundle" => {
                self.bundle = load(op.get("bundle"));
                result("bundle_activated")
            }
            "advance_time" => match op.str_at("now").and_then(time::parse_rfc3339) {
                Some(t) => {
                    self.now = t;
                    result("time_advanced")
                }
                None => refused("TIME_INVALID"),
            },
            "set_dependency" => {
                let up = op.str_at("status") == Some("up");
                match op.str_at("name") {
                    Some("policy") => self.policy_up = up,
                    Some("ledger") => self.ledger_up = up,
                    _ => {}
                }
                result("dependency_set")
            }
            "reconcile" => {
                let mut n = 0;
                for r in self.receipts.values_mut().filter(|r| r.status == ReceiptStatus::Dispatched) {
                    r.status = ReceiptStatus::UnknownReconcile;
                    n += 1;
                }
                obj(vec![("result", s("reconciled")), ("count", Value::Int(n))])
            }
            _ => refused("OPERATION_UNKNOWN"),
        };
        if out.str_at("result") == Some("refused") {
            self.refusals += 1;
        }
        out
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

fn apply(ledger: &Mutex<Broker>, op: &Value, step: usize) -> Value {
    if op.str_at("op") == Some("concurrent") {
        let ops = op.get("operations").and_then(Value::as_arr).cloned().unwrap_or_default();
        let barrier = Barrier::new(ops.len());
        let results: Vec<Value> = thread::scope(|scope| {
            let handles: Vec<_> = ops
                .iter()
                .map(|o| {
                    let barrier = &barrier;
                    scope.spawn(move || {
                        barrier.wait();
                        apply(ledger, o, step)
                    })
                })
                .collect();
            handles.into_iter().map(|h| h.join().unwrap_or_else(|_| refused("INTERNAL_ERROR"))).collect()
        });
        let mut outcomes: BTreeMap<String, i64> = BTreeMap::new();
        for r in &results {
            *outcomes.entry(outcome_key(r)).or_default() += 1;
        }
        return obj(vec![
            ("result", s("concurrent")),
            ("outcomes", Value::Obj(outcomes.into_iter().map(|(k, n)| (k, Value::Int(n))).collect())),
        ]);
    }
    let mut b = ledger.lock().unwrap_or_else(|e| e.into_inner());
    b.apply_locked(op, step)
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
            && i.get("dependencies").and_then(Value::as_obj).is_some_and(|d| d.iter().all(|(k, v)| dependency_declared(k) && dependency_status(v)))
    });
    let steps_ok = input.get("steps").and_then(Value::as_arr).is_some_and(|ops| ops.iter().all(step_usable));
    initial_ok && steps_ok
}

pub fn run(input: &Value) -> Value {
    if !input_usable(input) {
        return obj(vec![("input_error", s("INPUT_MALFORMED"))]);
    }
    let init = input.get("initial").cloned().unwrap_or(Value::Obj(vec![]));
    let deps = init.get("dependencies");
    let broker = Broker {
        now: init.str_at("now").and_then(time::parse_rfc3339).unwrap_or((0, 0)),
        state_revision: init.get("state_revision").and_then(Value::as_int).unwrap_or(0),
        bundle: load(init.get("bundle")),
        ingress_keys: init.get("ingress_keys").cloned().unwrap_or(Value::Obj(vec![])),
        permit_key: init.str_at("permit_key").and_then(unhex).unwrap_or_default(),
        channels: init.get("channels").cloned().unwrap_or(Value::Obj(vec![])),
        policy_up: deps.and_then(|d| d.str_at("policy")) != Some("down"),
        ledger_up: deps.and_then(|d| d.str_at("ledger")) != Some("down"),
        grants: BTreeMap::new(),
        receipts: HashMap::new(),
        permits: HashMap::new(),
        consumed_permits: HashSet::new(),
        next_permit: 0,
        effects_applied: 0,
        refusals: 0,
    };
    let ledger = Mutex::new(broker);
    let steps: Vec<Value> = input
        .get("steps")
        .and_then(Value::as_arr)
        .map(|ops| ops.iter().enumerate().map(|(i, op)| apply(&ledger, op, i)).collect())
        .unwrap_or_default();
    let b = ledger.into_inner().unwrap_or_else(|e| e.into_inner());
    let mut receipts: BTreeMap<&str, i64> = BTreeMap::new();
    for r in b.receipts.values() {
        *receipts.entry(r.status.name()).or_default() += 1;
    }
    obj(vec![
        ("steps", Value::Arr(steps)),
        (
            "final",
            obj(vec![
                ("effects_applied", Value::Int(b.effects_applied)),
                ("grant_uses", Value::Obj(b.grants.iter().map(|(k, g)| (k.clone(), Value::Int(g.uses))).collect())),
                ("receipts", Value::Obj(receipts.into_iter().map(|(k, n)| (k.to_string(), Value::Int(n))).collect())),
                ("consumed_permits", Value::Int(b.consumed_permits.len() as i64)),
                ("refusals", Value::Int(b.refusals)),
            ]),
        ),
    ])
}

#[cfg(test)]
mod tests {
    use super::*;

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
}
