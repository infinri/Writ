//! Broker side of workflow promotion (`transition-scenario`,
//! m1/fixtures/transition/SCENARIOS.md).
//!
//! SIMULATED: the ledger is an in-memory model guarded by one mutex (each
//! operation is one serializable transaction); `fault` injects a crash at a
//! named point; effect "dispatch" only records a state. Durable storage,
//! crash recovery and multi-process locking are not covered.

use crate::acceptance;
use crate::json::{self, obj, s, Value};
use crate::kernel::{Protection, ACCEPTANCE_ATTRS, TRANSITION};
use crate::ids::{is_digest, is_name};
use std::collections::{BTreeMap, HashSet};
use std::sync::{Barrier, Mutex};
use std::thread;

#[derive(Clone, Copy, PartialEq, Eq)]
enum EffectStatus {
    Completed,
    UnknownReconcile,
}

struct State {
    fields: Value,
    revision: i64,
    consumed: Vec<String>,
    revoked: Vec<String>,
    /// Promotion receipts: idempotency key -> (binding fingerprint, committed revision).
    receipts: BTreeMap<String, (String, i64)>,
    /// Effect store: idempotency key -> (binding fingerprint, status).
    effects: BTreeMap<String, (String, EffectStatus)>,
    refusals: u64,
}

impl State {
    fn f(&self, key: &str) -> Option<&Value> {
        self.fields.get(key)
    }
    fn fs(&self, key: &str) -> &str {
        self.fields.str_at(key).unwrap_or("")
    }
    fn set(&mut self, key: &str, v: Value) {
        if let Value::Obj(m) = &mut self.fields {
            match m.iter_mut().find(|(k, _)| k == key) {
                Some(slot) => slot.1 = v,
                None => m.push((key.to_string(), v)),
            }
        }
    }
}

fn refused(code: &str) -> Value {
    obj(vec![("result", s("refused")), ("code", s(code))])
}

fn result(r: &str) -> Value {
    obj(vec![("result", s(r))])
}

fn str_list(v: Option<&Value>) -> Vec<String> {
    v.and_then(Value::as_arr).map(|a| a.iter().filter_map(|x| x.as_str().map(str::to_string)).collect()).unwrap_or_default()
}

/// Binding fingerprint: D-006 digest of the operation without `fault`.
fn binding_fingerprint(op: &Value) -> String {
    json::digest(&op.without("fault")).unwrap_or_default()
}

fn request_fingerprint(req: Option<&Value>) -> Option<String> {
    req.and_then(|r| json::digest(r).ok())
}

/// The active classification, only if it comes from the active authorization
/// bundle and is well formed (fail closed otherwise).
fn active_protection(st: &State) -> Option<Protection> {
    let p = st.f("protection").filter(|p| !p.is_null())?;
    if p.str_at("authorization_bundle_sha") != Some(st.fs("authorization_bundle_sha")) {
        return None;
    }
    let to_states = p.get("protected_to_states").and_then(Value::as_arr)?;
    let edges = p.get("protected_transitions").and_then(Value::as_arr)?;
    let to_states: Option<Vec<String>> = to_states.iter().map(|x| x.as_str().filter(|x| is_name(x)).map(str::to_string)).collect();
    let edges: Option<Vec<(String, String)>> = edges
        .iter()
        .map(|e| {
            let f = e.str_at("from_state").filter(|x| is_name(x))?;
            let t = e.str_at("to_state").filter(|x| is_name(x))?;
            Some((f.to_string(), t.to_string()))
        })
        .collect();
    let (to_states, edges) = (to_states?, edges?);
    let mut seen = HashSet::new();
    if !to_states.iter().all(|x| seen.insert(x.clone())) {
        return None;
    }
    let mut seen_e = HashSet::new();
    if !edges.iter().all(|e| e.0 != e.1 && seen_e.insert(e.clone())) {
        return None;
    }
    let sha = p.str_at("acceptance_policy_bundle_sha").filter(|x| is_digest(x))?;
    Some(Protection { to_states, edges, acceptance_policy_bundle_sha: sha.to_string() })
}

fn promote(st: &mut State, op: &Value) -> Value {
    let key = op.str_at("idempotency_key").unwrap_or("").to_string();
    let fp = binding_fingerprint(op);
    if let Some((stored_fp, rev)) = st.receipts.get(&key) {
        return if *stored_fp == fp {
            obj(vec![("result", s("committed")), ("revision", Value::Int(*rev)), ("replayed", Value::Bool(true))])
        } else {
            refused("IDEMPOTENCY_CONFLICT")
        };
    }
    let permit = match op.get("permit") {
        None | Some(Value::Null) => return refused("PERMIT_MISSING"),
        Some(p) => p,
    };
    let request = op.get("request");
    if request_fingerprint(request).is_none() || permit.str_at("request_fingerprint") != request_fingerprint(request).as_deref() {
        return refused("PERMIT_REQUEST_MISMATCH");
    }
    let request = request.unwrap();
    if permit.str_at("authorization_bundle_sha") != Some(st.fs("authorization_bundle_sha")) {
        return refused("PERMIT_POLICY_STALE");
    }
    let Some(protection) = active_protection(st) else { return refused("PROTECTION_UNAVAILABLE") };
    if protection.acceptance_policy_bundle_sha != st.fs("acceptance_policy_bundle_sha") {
        return refused("POLICY_DEPENDENCY_MISMATCH");
    }

    let action = request.get("action").cloned().unwrap_or(Value::Null);
    let resource = format!("workflow:{}/{}/{}", st.fs("tenant_id"), st.fs("project_id"), st.fs("workflow_id"));
    if action.str_at("kind") != Some(TRANSITION)
        || request.str_at("tenant_id") != Some(st.fs("tenant_id"))
        || request.str_at("project_id") != Some(st.fs("project_id"))
        || action.get("resource").and_then(|r| r.str_at("canonical_resource_id")) != Some(resource.as_str())
    {
        return refused("REQUEST_SCOPE_MISMATCH");
    }
    let caps = str_list(action.get("requested_capabilities"));
    if caps.iter().any(|c| c == TRANSITION) {
        return refused("REQUESTED_CAPABILITIES_INVALID");
    }
    if !caps.is_empty() || action.get("requested_capabilities").is_some_and(|c| c.as_arr().is_none()) {
        return refused("COMPOSITE_NOT_SUPPORTED");
    }

    let attrs = action.get("attributes").cloned().unwrap_or(Value::Obj(vec![]));
    let from = attrs.str_at("from_state").unwrap_or("");
    let to = attrs.str_at("to_state").unwrap_or("");
    let record = op.get("record").filter(|r| !r.is_null());
    let protected = protection.is_protected(from, to);
    if protected {
        let complete = ACCEPTANCE_ATTRS.iter().all(|k| attrs.get(k).is_some());
        let Some(record) = record.filter(|_| complete) else { return refused("ACCEPTANCE_REQUIRED") };
        let digest_bound = permit.get("record_digest").is_some_and(|d| Some(d) == record.get("record_digest"));
        let attrs_bound = ACCEPTANCE_ATTRS.iter().all(|k| attrs.get(k) == record.get(k));
        if !digest_bound || !attrs_bound {
            return refused("PERMIT_BINDING_MISMATCH");
        }
    } else if ACCEPTANCE_ATTRS.iter().any(|k| attrs.get(k).is_some())
        || record.is_some()
        || permit.get("record_digest").is_some_and(|d| !d.is_null())
    {
        return refused("ACCEPTANCE_BINDING_UNEXPECTED");
    }

    if request.get("expected_state_revision").and_then(Value::as_int) != Some(st.revision) || from != st.fs("state") {
        return refused("REVISION_CONFLICT");
    }
    if protected {
        let current = obj(vec![
            ("tenant_id", s(st.fs("tenant_id"))),
            ("project_id", s(st.fs("project_id"))),
            ("task_id", s(st.fs("task_id"))),
            ("workflow_id", s(st.fs("workflow_id"))),
            ("transition", obj(vec![("from_state", s(st.fs("state"))), ("to_state", s(to))])),
            ("artifact_snapshot_id", s(st.fs("artifact_snapshot_id"))),
            ("artifact_digest", s(st.fs("artifact_digest"))),
            ("acceptance_policy_id", s(st.fs("acceptance_policy_id"))),
            ("acceptance_policy_version", s(st.fs("acceptance_policy_version"))),
            ("acceptance_policy_bundle_sha", s(st.fs("acceptance_policy_bundle_sha"))),
            ("state_revision", Value::Int(st.revision)),
            ("now", s(st.fs("now"))),
            ("consumed_record_ids", Value::Arr(st.consumed.iter().map(|x| s(x)).collect())),
            ("revoked_evaluator_ids", Value::Arr(st.revoked.iter().map(|x| s(x)).collect())),
        ]);
        if let Err(code) = acceptance::check(record, Some(&current), st.f("acceptance_requirements")) {
            return refused(code);
        }
    }
    if op.str_at("fault") == Some("crash_before_commit") {
        return result("crashed");
    }

    // Commit (one transaction): state, revision, receipt; protected also
    // consumes the record and records the promotion receipt.
    st.revision += 1;
    let rev = st.revision;
    st.set("state", s(to));
    st.receipts.insert(key, (fp, rev));
    if protected {
        let record = record.unwrap();
        st.consumed.push(record.str_at("acceptance_record_id").unwrap_or("").to_string());
        let lp = obj(vec![
            ("receipt_id", s(&format!("promotion:{}:{}", st.fs("workflow_id"), rev))),
            ("record_id", record.get("acceptance_record_id").cloned().unwrap_or(Value::Null)),
            ("to_state", s(to)),
            ("artifact_snapshot_id", s(st.fs("artifact_snapshot_id"))),
            ("artifact_digest", s(st.fs("artifact_digest"))),
            ("acceptance_policy_bundle_sha", s(st.fs("acceptance_policy_bundle_sha"))),
            ("authorization_bundle_sha", s(st.fs("authorization_bundle_sha"))),
        ]);
        st.set("last_promotion", lp);
    }
    obj(vec![("result", s("committed")), ("revision", Value::Int(rev))])
}

/// SIMULATED governed effect (for example a publish after promotion).
fn effect(st: &mut State, op: &Value) -> Value {
    let key = op.str_at("idempotency_key").unwrap_or("").to_string();
    let fp = binding_fingerprint(op);
    if let Some((stored_fp, status)) = st.effects.get(&key) {
        if *stored_fp != fp {
            return refused("IDEMPOTENCY_CONFLICT");
        }
        return match status {
            EffectStatus::Completed => obj(vec![("result", s("effect_completed")), ("replayed", Value::Bool(true))]),
            EffectStatus::UnknownReconcile => refused("RECONCILIATION_REQUIRED"),
        };
    }
    let permit = match op.get("permit") {
        None | Some(Value::Null) => return refused("PERMIT_MISSING"),
        Some(p) => p,
    };
    let request = op.get("request");
    if request_fingerprint(request).is_none() || permit.str_at("request_fingerprint") != request_fingerprint(request).as_deref() {
        return refused("PERMIT_REQUEST_MISMATCH");
    }
    if permit.str_at("authorization_bundle_sha") != Some(st.fs("authorization_bundle_sha")) {
        return refused("PERMIT_POLICY_STALE");
    }
    let request = request.unwrap();
    let action = request.get("action").cloned().unwrap_or(Value::Null);
    let kind = action.str_at("kind").unwrap_or("");
    if let Some(prereq) = st.f("effect_prerequisites").and_then(|p| p.get(kind)) {
        let attrs = action.get("attributes").cloned().unwrap_or(Value::Obj(vec![]));
        let needed = ["workflow_id", "artifact_snapshot_id", "artifact_digest", "promotion_receipt_id"];
        if !needed.iter().all(|k| attrs.str_at(k).is_some()) {
            return refused("PROMOTION_RECEIPT_INVALID");
        }
        if attrs.str_at("workflow_id") != Some(st.fs("workflow_id")) {
            return refused("REQUEST_SCOPE_MISMATCH");
        }
        if !str_list(prereq.get("required_states")).iter().any(|x| x == st.fs("state")) {
            return refused("EFFECT_PREREQUISITE_UNMET");
        }
        let Some(lp) = st.f("last_promotion").filter(|lp| !lp.is_null()).cloned() else {
            return refused("PROMOTION_RECEIPT_INVALID");
        };
        if lp.get("receipt_id") != attrs.get("promotion_receipt_id") {
            return refused("PROMOTION_RECEIPT_INVALID");
        }
        if lp.get("artifact_snapshot_id") != attrs.get("artifact_snapshot_id") || lp.get("artifact_digest") != attrs.get("artifact_digest") {
            return refused("EFFECT_ARTIFACT_MISMATCH");
        }
        let current_artifact = lp.str_at("artifact_snapshot_id") == Some(st.fs("artifact_snapshot_id"))
            && lp.str_at("artifact_digest") == Some(st.fs("artifact_digest"));
        let current_policies = lp.str_at("acceptance_policy_bundle_sha") == Some(st.fs("acceptance_policy_bundle_sha"))
            && lp.str_at("authorization_bundle_sha") == Some(st.fs("authorization_bundle_sha"));
        if !current_artifact || !current_policies {
            return refused("PROMOTION_STALE");
        }
    }
    if op.str_at("fault") == Some("crash_before_reserve") {
        return result("crashed");
    }
    // Reserve, then dispatch (simulated: no external side effect).
    if op.str_at("fault") == Some("crash_after_dispatch") {
        st.effects.insert(key, (fp, EffectStatus::UnknownReconcile));
        return result("crashed");
    }
    st.effects.insert(key, (fp, EffectStatus::Completed));
    result("effect_completed")
}

fn reconcile(st: &mut State, op: &Value) -> Value {
    let key = op.str_at("idempotency_key").unwrap_or("");
    let observed = op.str_at("observed").unwrap_or("");
    match st.effects.get(key) {
        Some((_, EffectStatus::UnknownReconcile)) if matches!(observed, "applied" | "not_applied") => {
            if observed == "applied" {
                st.effects.get_mut(key).unwrap().1 = EffectStatus::Completed;
            } else {
                st.effects.remove(key);
            }
            obj(vec![("result", s("reconciled")), ("outcome", s(observed))])
        }
        _ => refused("NOTHING_TO_RECONCILE"),
    }
}

/// One atomic operation under the ledger lock.
fn apply_locked(st: &mut State, op: &Value) -> Value {
    let out = match op.str_at("op").unwrap_or("") {
        "promote" => promote(st, op),
        "effect" => effect(st, op),
        "reconcile" => reconcile(st, op),
        "update_artifact" => {
            st.set("artifact_snapshot_id", op.get("artifact_snapshot_id").cloned().unwrap_or(Value::Null));
            st.set("artifact_digest", op.get("artifact_digest").cloned().unwrap_or(Value::Null));
            st.revision += 1;
            obj(vec![("result", s("artifact_updated")), ("revision", Value::Int(st.revision))])
        }
        "activate_policy" => {
            for k in ["acceptance_policy_version", "acceptance_policy_bundle_sha", "acceptance_requirements"] {
                st.set(k, op.get(k).cloned().unwrap_or(Value::Null));
            }
            if let Some(id) = op.get("acceptance_policy_id") {
                st.set("acceptance_policy_id", id.clone());
            }
            result("policy_activated")
        }
        "activate_authorization_bundle" => {
            st.set("authorization_bundle_sha", op.get("authorization_bundle_sha").cloned().unwrap_or(Value::Null));
            st.set("protection", op.get("protection").cloned().unwrap_or(Value::Null));
            result("authorization_bundle_activated")
        }
        "revoke_evaluator" => {
            if let Some(e) = op.str_at("evaluator_id") {
                st.revoked.push(e.to_string());
            }
            result("evaluator_revoked")
        }
        "advance_time" => {
            st.set("now", op.get("now").cloned().unwrap_or(Value::Null));
            result("time_advanced")
        }
        _ => refused("UNKNOWN_OPERATION"),
    };
    if out.str_at("result") == Some("refused") {
        st.refusals += 1;
    }
    out
}

/// Runs operations on real threads released together; each takes the
/// ledger lock for its whole transaction, so the outcome equals some
/// serialization. Results are returned in submission order.
fn run_concurrently(ledger: &Mutex<State>, ops: &[Value]) -> Vec<Value> {
    let barrier = Barrier::new(ops.len());
    thread::scope(|scope| {
        let handles: Vec<_> = ops
            .iter()
            .map(|op| {
                let barrier = &barrier;
                scope.spawn(move || {
                    barrier.wait();
                    apply(ledger, op)
                })
            })
            .collect();
        handles.into_iter().map(|h| h.join().unwrap_or_else(|_| refused("INTERNAL_ERROR"))).collect()
    })
}

fn apply(ledger: &Mutex<State>, op: &Value) -> Value {
    match op.str_at("op").unwrap_or("") {
        "concurrent" => {
            let ops = op.get("operations").and_then(Value::as_arr).cloned().unwrap_or_default();
            obj(vec![("result", s("concurrent")), ("results", Value::Arr(run_concurrently(ledger, &ops)))])
        }
        "promote_concurrent" => {
            let count = op.get("count").and_then(Value::as_int).unwrap_or(0).max(0);
            let inner = op.get("promote").cloned().unwrap_or(Value::Obj(vec![]));
            let base = inner.str_at("idempotency_key").unwrap_or("").to_string();
            let ops: Vec<Value> = (1..=count)
                .map(|i| {
                    let mut m = inner.as_obj().cloned().unwrap_or_default();
                    m.retain(|(k, _)| k != "idempotency_key" && k != "op");
                    m.push(("op".into(), s("promote")));
                    m.push(("idempotency_key".into(), s(&format!("{base}-{i}"))));
                    Value::Obj(m)
                })
                .collect();
            let results = run_concurrently(ledger, &ops);
            let committed = results.iter().filter(|r| r.str_at("result") == Some("committed")).count();
            let mut refused_counts: BTreeMap<String, i64> = BTreeMap::new();
            for r in &results {
                if r.str_at("result") == Some("refused") {
                    *refused_counts.entry(r.str_at("code").unwrap_or("").to_string()).or_default() += 1;
                }
            }
            obj(vec![
                ("result", s("concurrent")),
                ("committed", Value::Int(committed as i64)),
                ("refused", Value::Obj(refused_counts.into_iter().map(|(k, n)| (k, Value::Int(n))).collect())),
            ])
        }
        _ => {
            let mut st = ledger.lock().unwrap_or_else(|e| e.into_inner());
            apply_locked(&mut st, op)
        }
    }
}

pub fn run(input: &Value) -> Value {
    let initial = input.get("initial").cloned().unwrap_or(Value::Obj(vec![]));
    let state = State {
        revision: initial.get("revision").and_then(Value::as_int).unwrap_or(0),
        consumed: str_list(initial.get("consumed_record_ids")),
        revoked: str_list(initial.get("revoked_evaluator_ids")),
        fields: if initial.as_obj().is_some() { initial.clone() } else { Value::Obj(vec![]) },
        receipts: BTreeMap::new(),
        effects: BTreeMap::new(),
        refusals: 0,
    };
    let ledger = Mutex::new(state);
    let steps: Vec<Value> = input
        .get("steps")
        .and_then(Value::as_arr)
        .map(|ops| ops.iter().map(|op| apply(&ledger, op)).collect())
        .unwrap_or_default();
    let st = ledger.into_inner().unwrap_or_else(|e| e.into_inner());
    let mut consumed = st.consumed.clone();
    consumed.sort();
    let effects: Vec<(String, Value)> = st
        .effects
        .iter()
        .map(|(k, (_, status))| {
            (k.clone(), s(if *status == EffectStatus::Completed { "completed" } else { "unknown_reconcile" }))
        })
        .collect();
    obj(vec![
        ("steps", Value::Arr(steps)),
        (
            "final",
            obj(vec![
                ("state", st.f("state").cloned().unwrap_or(Value::Null)),
                ("revision", Value::Int(st.revision)),
                ("consumed_record_ids", Value::Arr(consumed.iter().map(|x| s(x)).collect())),
                ("receipts", Value::Int(st.receipts.len() as i64)),
                ("refusals", Value::Int(st.refusals as i64)),
                ("effects", Value::Obj(effects)),
            ]),
        ),
    ])
}
