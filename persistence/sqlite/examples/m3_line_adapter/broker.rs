//! `broker-scenario` (m1/fixtures/broker/BROKER.md) on the production broker.

use std::collections::BTreeMap;
use std::sync::atomic::Ordering;
use std::sync::{Arc, Barrier};

use writ_broker::execute::Executed;
use writ_broker::grants::{Approval, ApprovalPresentation, GrantLabel, Replay, Verdict};
use writ_broker::ingress::{verify_ingress, Seal};
use writ_broker::keys::{IngressKey, MemoryKeyRing, PermitKey};
use writ_broker::permit::Permit;
use writ_broker::policy::PolicyState;
use writ_broker::refusal::Refusal;
use writ_broker::store::Timestamp;
use writ_broker::writ_kernel::contracts::StateRevision;
use writ_broker::writ_kernel::json::{self, Map, Value};
use writ_broker::writ_kernel::policy::load_bundle;

use crate::common::{
    adapter_error, int, malformed, pairs, production_difference, refused, result, text, unhex,
    unsupported, ChannelAuthority, Labels, Point, ProdBroker, World, ADAPTER_SEALED, C1, C2, C6,
    C7, C8, DIFFERENCE,
};

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

pub enum Step {
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

pub fn executed(r: &Result<Executed, Refusal>) -> Value {
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

struct BrokerCase {
    world: World,
    bundle: Option<Value>,
    policy_up: bool,
    revision: StateRevision,
    authority: ChannelAuthority,
    ingress: BTreeMap<String, Vec<u8>>,
    permits: BTreeMap<usize, Permit>,
}

impl BrokerCase {
    fn new(initial: &Value, labels: Arc<Labels>) -> Result<BrokerCase, String> {
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
        let world = World::create(policy, now, permit_key, ring, labels.clone())?;
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
                labels,
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
                self.world.labels.add(C1);
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
        let Ok(verified) = verify_ingress(&req, Some(&seal.to_value()), &self.world.ring) else {
            return Step::Unsupported(
                "APPROVAL_REQUEST_NOT_SEALABLE",
                "sealed for_request did not verify".into(),
            );
        };
        self.world.labels.add(C2);
        self.world.labels.add(ADAPTER_SEALED);
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
            // D-037 item 5 (p-11): nothing was issued, so there is nothing to present and no
            // production operation runs; zero effects are shown by the final state.
            self.world.labels.add(DIFFERENCE);
            return Step::Done(production_difference("NO_PERMIT_TO_PRESENT", Vec::new()));
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
                if fault == Some("effect_fails") {
                    self.world.labels.add(C6);
                }
                stores
                    .effects
                    .set_effects_fail(fault == Some("effect_fails"));
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
                            self.world.reopen()?;
                            Step::Done(result("crashed"))
                        } else {
                            Step::Done(value)
                        }
                    }
                }
            }
            "approve" => self.approve(&self.world.broker()?, op),
            "present_permit" => self.present_permit(&self.world.broker()?, op),
            "apply_without_permit" => {
                // D-037 item 5 (b-08): production has no effect path without a permit; no
                // broker operation runs and the final state shows zero effects.
                self.world.labels.add(DIFFERENCE);
                Step::Done(production_difference("NO_PRODUCTION_OPERATION", Vec::new()))
            }
            "reconcile" => Step::Done(match self.world.broker()?.reconcile() {
                Ok(r) => json::obj([
                    ("count", int(r.changed())),
                    ("result", json::s("reconciled")),
                ]),
                Err(r) => refused(r.code()),
            }),
            "concurrent" => self.concurrent(op)?,
            "restart_broker" => {
                self.world.reopen()?;
                Step::Done(result("restarted"))
            }
            "activate_bundle" => {
                self.bundle = op.get("bundle").cloned();
                self.reactivate();
                Step::Done(result("bundle_activated"))
            }
            "advance_time" => match op
                .get("now")
                .and_then(Value::as_str)
                .and_then(Timestamp::parse_rfc3339)
            {
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
                    Some("ledger") => self
                        .world
                        .stores()?
                        .ledger
                        .down
                        .store(!up, Ordering::SeqCst),
                    _ => {}
                }
                Step::Done(result("dependency_set"))
            }
            _ => {
                self.world.labels.add(C7);
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

    /// Final counts, read only after every store of the case is closed (quiescent).
    fn final_state(&mut self) -> Result<Value, String> {
        self.world.close();
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
                int(crate::common::count(
                    &e,
                    "SELECT count(*) FROM consumed_permits",
                )?),
            ),
            (
                "effects_applied",
                int(crate::common::count(&e, "SELECT count(*) FROM effects")?),
            ),
            ("grant_uses", Value::Obj(grants)),
            ("receipts", Value::Obj(receipts)),
            (
                "refusals",
                int(crate::common::count(
                    &l,
                    "SELECT count(*) FROM audit_refusals",
                )?),
            ),
        ]))
    }
}

pub fn broker_scenario(input: &Value, labels: Arc<Labels>) -> Value {
    if !broker_input_usable(input) {
        labels.add(C8);
        return malformed();
    }
    let initial = input.get("initial").cloned().unwrap_or(Value::Null);
    let mut case = match BrokerCase::new(&initial, labels) {
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
