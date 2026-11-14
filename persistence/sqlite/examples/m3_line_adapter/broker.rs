//! `broker-scenario` (m1/fixtures/broker/BROKER.md) on the production broker.

use std::collections::BTreeMap;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Barrier, Mutex};

use writ_broker::approval_store::{
    approval_request_for, CeremonyBegin, CeremonyFinish, CeremonyOp, CeremonyResult, NewEnrollment,
    NewRole,
};
use writ_broker::authorize::{authorize, UseTimeStatus};
use writ_broker::clock::ObservedTime;
use writ_broker::execute::Executed;
use writ_broker::grants::{GrantLabel, Replay};
use writ_broker::human::{ApprovalChannel, Permission, PrincipalKind, SubjectId};
use writ_broker::ingress::{verify_ingress, Seal};
use writ_broker::keys::{IngressKey, MemoryKeyRing, PermitKey};
use writ_broker::permit::Permit;
use writ_broker::policy::PolicyState;
use writ_broker::refusal::Refusal;
use writ_broker::store::ExecuteLedger;
use writ_broker::store::Timestamp;
use writ_broker::writ_kernel::contracts::StateRevision;
use writ_broker::writ_kernel::json::{self, Map, Value};
use writ_broker::writ_kernel::policy::load_bundle;

use crate::common::{
    adapter_error, int, malformed, operator_session, pairs, production_difference, refused,
    register, result, test_credential, text, unhex, unsupported, with_storage_fault,
    ChannelVerifier, Labels, Point, ProdBroker, Registrar, StorageFault, World, ADAPTER_SEALED,
    ADMIN_RESPONSE, C1, C2, C6, C7, C8, DIFFERENCE, LISTENER_CHANNEL, OPERATOR_SESSION, TEST_ADMIN,
    TEST_SUBJECT_ISSUER,
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

/// Generated-only operations (no frozen fixture uses these op names, D-038 I-4):
/// `concurrent_faults` is a concurrent group of executes whose operations may carry a
/// per-operation `storage_fault` (common::StorageFault, `<reserve|dispatch|observe>_
/// <unavailable|commit_lost|commit_unknown>`); `ledger_detail` closes the stores, reads the
/// ledger and effect store read-only at that quiescent boundary and reopens them (a
/// `restart_broker` that also answers the durable per-key state).
fn fault_usable(op: &Value) -> bool {
    op.get("op").and_then(Value::as_str) == Some("execute")
        && op.get("fault").is_none()
        && op.get("storage_fault").is_none_or(|f| {
            f.as_str()
                .and_then(crate::common::StorageFault::parse)
                .is_some()
        })
}

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
        Some("concurrent_faults") => op
            .get("operations")
            .and_then(Value::as_arr)
            .is_some_and(|ops| !ops.is_empty() && ops.iter().all(fault_usable)),
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
    verifier: Arc<ChannelVerifier>,
    /// Whether the case's test administrator and fixture humans are enrolled.
    enrolled: AtomicBool,
    /// Fixture grant reference to the ledger-assigned M4 grant label (`approval/<id>`).
    grants: Mutex<BTreeMap<String, GrantLabel>>,
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
        let admin = SubjectId::new(TEST_SUBJECT_ISSUER, TEST_ADMIN);
        let verifier = Arc::new(ChannelVerifier {
            channels: initial.get("channels").cloned().unwrap_or(Value::Null),
            labels: labels.clone(),
            nonce: AtomicU64::new(0),
        });
        let channel = ApprovalChannel::new(verifier.clone(), Arc::new(Registrar), LISTENER_CHANNEL)
            .ok_or("approval channel")?;
        let world = World::create(
            policy,
            now,
            permit_key,
            ring,
            labels.clone(),
            (&admin, &test_credential(TEST_ADMIN)),
            channel,
        )?;
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
            verifier,
            enrolled: AtomicBool::new(false),
            grants: Mutex::new(BTreeMap::new()),
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
            Some(g) => Some(self.production_label(g).ok_or_else(|| {
                Step::Unsupported(
                    "GRANT_LABEL_NOT_REPRESENTABLE",
                    "grant_ref is not a production grant label".into(),
                )
            })?),
        };
        let exec = broker.execute(&verified, self.revision, label.as_ref());
        Ok((executed(&exec.result), exec.permit))
    }

    /// The grant a fixture `grant_ref` presents: the ledger-assigned label of the M4 grant
    /// issued for that reference (`approval/<request id>`), otherwise the reference itself
    /// (which names no grant).
    fn production_label(&self, g: &Value) -> Option<GrantLabel> {
        let text = g.as_str()?;
        let mapped = self.grants.lock().ok()?.get(text).cloned();
        mapped.or_else(|| GrantLabel::new(text))
    }

    /// The fixture reference of a production grant label (final state and detail).
    fn fixture_label(&self, label: &str) -> String {
        self.grants
            .lock()
            .ok()
            .and_then(|g| {
                g.iter()
                    .find(|(_, l)| l.as_str() == label)
                    .map(|(k, _)| k.clone())
            })
            .unwrap_or_else(|| label.to_string())
    }

    /// Once per case: through the production enrollment ceremony of the case's test
    /// administrator (bootstrapped at world creation through the production bootstrap entry
    /// point), every human of the fixture's channel table as a test subject holding
    /// `approver` over every scope, with one registered test credential each (S5, X8). Test
    /// identities only.
    fn enroll_once(&self, now: ObservedTime) -> Result<(), String> {
        if self.enrolled.swap(true, Ordering::SeqCst) {
            return Ok(());
        }
        let stores = self.world.stores()?;
        let admin = SubjectId::new(TEST_SUBJECT_ISSUER, TEST_ADMIN);
        let mut humans: Vec<String> = self
            .verifier
            .channels
            .as_obj()
            .map(|channels| {
                channels
                    .values()
                    .filter_map(Value::as_obj)
                    .flat_map(|c| c.keys().cloned())
                    .collect()
            })
            .unwrap_or_default();
        humans.sort();
        humans.dedup();
        for human in humans {
            let op = CeremonyOp::Enroll {
                enrollment: NewEnrollment {
                    subject: SubjectId::new(TEST_SUBJECT_ISSUER, &human),
                    kind: PrincipalKind::Human,
                    credential: Some(register(
                        stores.ledger.production(),
                        &|| Ok(now),
                        &SubjectId::new(TEST_SUBJECT_ISSUER, &human),
                        &test_credential(&human),
                        Some((
                            stores.ledger.production(),
                            &operator_session(&admin),
                            ADMIN_RESPONSE,
                        )),
                    )?),
                    linked_identities: Vec::new(),
                    roles: vec![NewRole {
                        permission: Permission::Approver,
                        tenant_id: "*".into(),
                        project_id: "*".into(),
                    }],
                },
                operator_session: OPERATOR_SESSION.into(),
            };
            let key = format!("m3c-enroll-{human}");
            let c = stores
                .ledger
                .begin_ceremony(&CeremonyBegin {
                    op: &op,
                    subject: &admin,
                    idempotency_key: &key,
                    now,
                })
                .map_err(|f| format!("enroll {human}: {:?}", f.error))?
                .map_err(|r| format!("enroll {human}: {}", r.code()))?;
            stores
                .ledger
                .finish_ceremony(&CeremonyFinish {
                    op: &op,
                    challenge_id: &c.challenge_id,
                    response: ADMIN_RESPONSE.as_bytes(),
                    idempotency_key: &key,
                    now,
                })
                .map_err(|f| format!("enroll {human}: {:?}", f.error))?
                .map_err(|r| format!("enroll {human}: {}", r.code()))?;
        }
        Ok(())
    }

    /// A fixture `approve` on the M4 lifecycle (D-042): the approval request the broker
    /// records when the agent asks (M4-03, from the sealed `for_request` and the use-time
    /// decision), then the human's ceremony on the approval listener (`begin_ceremony`,
    /// `finish_ceremony`) through the non-shipping test verifier. The M3 approval-authority
    /// path is closed on `writ-ledger-6`.
    fn approve(&self, broker: &ProdBroker<'_>, i: usize, op: &Value) -> Result<Step, String> {
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
            return Ok(Step::Unsupported(
                "APPROVAL_REQUEST_NOT_SEALABLE",
                "for_request names no registered ingress adapter".into(),
            ));
        };
        let Some(seal) = Seal::create(&req, issuer, &key) else {
            return Ok(Step::Unsupported(
                "APPROVAL_REQUEST_NOT_SEALABLE",
                "for_request cannot be sealed".into(),
            ));
        };
        let Ok(verified) = verify_ingress(&req, Some(&seal.to_value()), &self.world.ring) else {
            return Ok(Step::Unsupported(
                "APPROVAL_REQUEST_NOT_SEALABLE",
                "sealed for_request did not verify".into(),
            ));
        };
        self.world.labels.add(C2);
        self.world.labels.add(ADAPTER_SEALED);
        let approved = match op.get("verdict").and_then(Value::as_str) {
            Some("approved") => true,
            Some("denied") => false,
            _ => {
                return Ok(Step::Unsupported(
                    "VERDICT_NOT_REPRESENTABLE",
                    "verdict is neither approved nor denied".into(),
                ))
            }
        };
        let Some(fixture_label) = op
            .get("grant_ref")
            .and_then(Value::as_str)
            .filter(|g| GrantLabel::new(g).is_some())
        else {
            return Ok(Step::Unsupported(
                "GRANT_LABEL_NOT_REPRESENTABLE",
                "grant_ref is not a production grant label".into(),
            ));
        };
        // A fixture `ttl_seconds` has no production counterpart: an M4 grant always lives
        // APPROVAL_GRANT_TTL_SECONDS from its decision (bindings.md section 2).
        // A refusal the adapter answers itself (no trusted time, the ledger unavailable for the
        // request or a ceremony) is audited like every other refusal of the broker.
        let refuse = |code: &str| {
            self.world.audit(code);
            Ok(Step::Done(refused(code)))
        };
        let observed = match broker.grants().observe() {
            Ok(t) => t,
            Err(r) => return refuse(r.code()),
        };
        // M4-03: the stored approval request (only for a `require_approval` decision).
        let request = {
            let shared = self.world.active.shared();
            let inputs = shared.trusted_inputs(self.revision);
            match authorize(&verified, &inputs, &observed) {
                Ok(a) if a.status() == UseTimeStatus::RequireApproval => {
                    approval_request_for(&a, observed)
                }
                _ => None,
            }
        };
        let Some(request) = request else {
            // No approval request exists for a request that does not require approval: no
            // human ceremony can name one (D-040 I-3, M4-03).
            self.world.labels.add(DIFFERENCE);
            return Ok(Step::Done(production_difference(
                "NO_APPROVAL_REQUEST",
                Vec::new(),
            )));
        };
        let stores = self.world.stores()?;
        if let Err(f) = stores.ledger.record_approval_request(&request) {
            return refuse(Refusal::LedgerUnavailable(f.error).code());
        }
        self.enroll_once(observed)?;
        let human = text(op, "human").unwrap_or_default();
        let channel = text(op, "channel").unwrap_or_default();
        let credential = text(op, "credential").unwrap_or_default();
        let subject = SubjectId::new(TEST_SUBJECT_ISSUER, &human);
        let request_id = request.request_id().to_string();
        let ceremony = if approved {
            CeremonyOp::Approve { request_id }
        } else {
            CeremonyOp::Deny { request_id }
        };
        let key = format!("m3c-approve-{i}");
        let c = match stores.ledger.begin_ceremony(&CeremonyBegin {
            op: &ceremony,
            subject: &subject,
            idempotency_key: &key,
            now: observed,
        }) {
            Err(f) => return refuse(Refusal::LedgerUnavailable(f.error).code()),
            Ok(Err(r)) => return Ok(Step::Done(refused(r.code()))),
            Ok(Ok(c)) => c,
        };
        let response = format!("{channel}\0{human}\0{credential}");
        let out = match stores.ledger.finish_ceremony(&CeremonyFinish {
            op: &ceremony,
            challenge_id: &c.challenge_id,
            response: response.as_bytes(),
            idempotency_key: &key,
            now: observed,
        }) {
            Err(f) => return refuse(Refusal::LedgerUnavailable(f.error).code()),
            Ok(Err(r)) => return Ok(Step::Done(refused(r.code()))),
            Ok(Ok(o)) => o,
        };
        Ok(Step::Done(match out.result {
            CeremonyResult::Approved { grant } => {
                self.grants
                    .lock()
                    .map_err(|_| "grant map poisoned".to_string())?
                    .insert(fixture_label.to_string(), grant);
                result("grant_issued")
            }
            CeremonyResult::Denied => result("approval_denied"),
            other => return Err(format!("unexpected ceremony result {other:?}")),
        }))
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
            "approve" => self.approve(&self.world.broker()?, i, op)?,
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
            "concurrent_faults" => self.concurrent_faults(op)?,
            "ledger_detail" => {
                self.world.close();
                let detail = self.ledger_detail();
                self.world.reopen()?;
                Step::Done(detail?)
            }
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
        self.group(ops, |_| None)
    }

    /// A concurrent group whose executes carry per-operation storage faults (validated by
    /// `broker_input_usable`); each fault applies to its own thread's transactions only.
    fn concurrent_faults(&self, op: &Value) -> Result<Step, String> {
        let ops = op.get("operations").and_then(Value::as_arr).unwrap_or(&[]);
        self.group(ops, |o| {
            o.get("storage_fault")
                .and_then(Value::as_str)
                .and_then(StorageFault::parse)
        })
    }

    fn group(
        &self,
        ops: &[Value],
        fault: impl Fn(&Value) -> Option<StorageFault> + Sync,
    ) -> Result<Step, String> {
        let broker = self.world.broker()?;
        let barrier = Barrier::new(ops.len());
        let outcomes: Vec<Result<(Value, Option<Permit>), Step>> = std::thread::scope(|scope| {
            let handles: Vec<_> = ops
                .iter()
                .map(|o| {
                    let (broker, barrier, fault) = (&broker, &barrier, &fault);
                    scope.spawn(move || {
                        barrier.wait();
                        with_storage_fault(fault(o), || self.execute(broker, o))
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

    /// `m` keyed by fixture grant references instead of production labels.
    fn relabel(&self, m: Map) -> Map {
        m.into_iter()
            .map(|(k, v)| (self.fixture_label(&k), v))
            .collect()
    }

    /// Durable per-key state, read only while every store is closed (quiescent).
    fn ledger_detail(&self) -> Result<Value, String> {
        let l = self.world.reader("ledger.db")?;
        let e = self.world.reader("effects.db")?;
        let rows =
            |c: &rusqlite::Connection, sql: &str, n: usize| -> Result<Vec<Vec<Value>>, String> {
                let mut st = c.prepare(sql).map_err(|e| format!("{sql}: {e}"))?;
                let out = st
                    .query_map([], |r| {
                        (0..n)
                            .map(|i| {
                                Ok(match r.get::<_, rusqlite::types::Value>(i)? {
                                    rusqlite::types::Value::Integer(v) => Value::Int(v),
                                    rusqlite::types::Value::Text(t) => json::s(&t),
                                    _ => Value::Null,
                                })
                            })
                            .collect::<Result<Vec<_>, rusqlite::Error>>()
                    })
                    .map_err(|e| format!("{sql}: {e}"))?;
                out.collect::<Result<Vec<_>, _>>()
                    .map_err(|e| format!("{sql}: {e}"))
            };
        let keyed = |c: &rusqlite::Connection, sql: &str| -> Result<Map, String> {
            Ok(rows(c, sql, 2)?
                .into_iter()
                .filter_map(|r| match (r.first(), r.get(1)) {
                    (Some(Value::Str(k)), Some(v)) => Some((k.clone(), v.clone())),
                    _ => None,
                })
                .collect())
        };
        let mut receipts = Map::new();
        for r in rows(
            &l,
            "SELECT r.tenant_id || '/' || r.idempotency_key, r.status, \
             (SELECT count(*) FROM receipt_events v WHERE v.tenant_id = r.tenant_id \
              AND v.idempotency_key = r.idempotency_key AND v.cause = 'dispatch'), \
             (SELECT p.permit_number FROM receipt_permits p WHERE p.tenant_id = r.tenant_id \
              AND p.idempotency_key = r.idempotency_key), r.fingerprint, r.grant_label \
             FROM receipts r",
            6,
        )? {
            if let [Value::Str(k), status, dispatches, permit, fingerprint, grant] = r.as_slice() {
                receipts.insert(
                    k.clone(),
                    json::obj([
                        ("dispatches", dispatches.clone()),
                        ("fingerprint", fingerprint.clone()),
                        (
                            "grant",
                            match grant {
                                Value::Str(g) => json::s(&self.fixture_label(g)),
                                other => other.clone(),
                            },
                        ),
                        ("permit", permit.clone()),
                        ("status", status.clone()),
                    ]),
                );
            }
        }
        let failures = rows(
            &l,
            "SELECT transaction_name, stage, progress, possible_progress, outcome_unknown \
             FROM failure_records ORDER BY audit_seq",
            5,
        )?
        .into_iter()
        .map(|r| {
            let names = [
                "transaction",
                "stage",
                "progress",
                "possible_progress",
                "outcome_unknown",
            ];
            Value::Obj(names.iter().map(|n| n.to_string()).zip(r).collect())
        })
        .collect();
        Ok(json::obj([
            (
                "consumed",
                Value::Obj(keyed(
                    &e,
                    "SELECT tenant_id || '/' || idempotency_key || '/' || result, count(*) \
                     FROM consumed_permits GROUP BY 1",
                )?),
            ),
            (
                "dispatch_events",
                int(crate::common::count(
                    &l,
                    "SELECT count(*) FROM receipt_events WHERE cause = 'dispatch'",
                )?),
            ),
            (
                "effects",
                Value::Obj(keyed(
                    &e,
                    "SELECT tenant_id || '/' || idempotency_key, count(*) FROM effects GROUP BY 1",
                )?),
            ),
            ("failures", Value::Arr(failures)),
            (
                "grant_use_rows",
                Value::Obj(self.relabel(keyed(
                    &l,
                    "SELECT label, count(*) FROM grant_uses GROUP BY 1",
                )?)),
            ),
            (
                "grant_uses",
                Value::Obj(self.relabel(keyed(&l, "SELECT label, uses FROM grants")?)),
            ),
            ("receipts", Value::Obj(receipts)),
            (
                "refusals",
                int(crate::common::count(
                    &l,
                    "SELECT count(*) FROM audit_refusals",
                )?),
            ),
            ("result", json::s("ledger_detail")),
        ]))
    }

    /// Final counts, read only after every store of the case is closed (quiescent).
    fn final_state(&mut self) -> Result<Value, String> {
        self.world.close();
        let l = self.world.reader("ledger.db")?;
        let e = self.world.reader("effects.db")?;
        let grants: Map = pairs(&l, "SELECT label, uses FROM grants")?
            .into_iter()
            .map(|(k, n)| (self.fixture_label(&k), Value::Int(n)))
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
