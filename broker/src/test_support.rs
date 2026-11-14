//! Test-only helpers: fixture keys, requests and bundles. Compiled only for
//! tests; no key here is ever available to non-test code.

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use crate::ingress::{verify_ingress, Seal, VerifiedRequest};
use crate::keys::{IngressKey, MemoryKeyRing, PermitKey};
use writ_kernel::contracts::StateRevision;
use writ_kernel::json::{self, Value};
use writ_kernel::policy::{load_bundle, LoadedBundle};
use writ_kernel::time::Timestamp;

pub const FIXTURE_ADAPTER: &str = "writ-host-adapter:fixture";
pub const OTHER_ADAPTER: &str = "writ-host-adapter:other";
pub const ROGUE_ADAPTER: &str = "writ-host-adapter:rogue";
/// Test keys (the M1 broker-scenario fixture values).
pub const FIXTURE_KEY: [u8; 32] = [0x11; 32];
pub const OTHER_KEY: [u8; 32] = [0x22; 32];
pub const ROGUE_KEY: [u8; 32] = [0x33; 32];
pub const PERMIT_KEY: [u8; 32] = [0x44; 32];

pub fn ingress_key(bytes: [u8; 32]) -> IngressKey {
    IngressKey::from_bytes(bytes.to_vec()).unwrap()
}

pub fn permit_key(bytes: [u8; 32]) -> PermitKey {
    PermitKey::from_bytes(bytes.to_vec()).unwrap()
}

/// The fixture and other adapters registered; the rogue adapter is not.
pub fn fixture_ring() -> MemoryKeyRing {
    let mut ring = MemoryKeyRing::new();
    ring.register(FIXTURE_ADAPTER, ingress_key(FIXTURE_KEY));
    ring.register(OTHER_ADAPTER, ingress_key(OTHER_KEY));
    ring
}

pub fn now() -> Timestamp {
    Timestamp::parse_strict("2026-11-05T12:00:00Z").unwrap()
}

pub fn transition_now() -> Timestamp {
    Timestamp::parse_strict("2026-11-07T11:00:00Z").unwrap()
}

pub fn revision(n: i64) -> StateRevision {
    StateRevision::new(n).unwrap()
}

fn parse(text: &str) -> Value {
    json::parse(text.as_bytes()).unwrap()
}

/// The M0 golden base request (docs/contracts/fixtures/decisions/m0-golden.json).
pub fn base_request() -> Value {
    parse(
        r#"{"contract_version":"2.0","request_id":"req-1","trace_id":"trace-1","tenant_id":"fixture-tenant",
        "principal":{"id":"agent:claude-session-1","verified_by":"writ-host-adapter:fixture","authn_method":"os_peer_credential","roles":["agent"]},
        "project_id":"fixture-project","session_id":"session-1","agent_id":null,"parent_session_id":null,"idempotency_key":"idem-1",
        "expected_state_revision":null,"policy_bundle_sha":null,"deadline_at":null,
        "action":{"kind":"file.write","resource":{"uri":"file:///work/src/app.py","canonical_resource_id":"workspace:fixture-tenant/fixture-project/src/app.py"},
        "action_digest":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","attributes":{},"requested_capabilities":[]}}"#,
    )
}

/// A `repository.publish` request (`require_approval` under the reference bundle).
pub fn publish_request() -> Value {
    let req = with(
        &base_request(),
        &["action", "kind"],
        json::s("repository.publish"),
    );
    let req = with(&req, &["action", "resource", "uri"], json::s("git://main"));
    with(
        &req,
        &["action", "resource", "canonical_resource_id"],
        json::s("repository:fixture-tenant/fixture-project/main"),
    )
}

/// The transition-decisions base request (m1/fixtures/transition/transition-decisions.cases.json).
pub fn transition_request() -> Value {
    parse(
        r#"{"contract_version":"2.0","request_id":"req-t1","trace_id":"trace-t1","tenant_id":"fixture-tenant",
        "principal":{"id":"agent:session-1","verified_by":"writ-host-adapter:fixture","authn_method":"os_peer_credential","roles":["agent"]},
        "project_id":"fixture-project","session_id":"session-1","agent_id":null,"parent_session_id":null,"idempotency_key":"idem-t1",
        "expected_state_revision":7,"policy_bundle_sha":null,"deadline_at":null,
        "action":{"kind":"workflow.transition","resource":{"uri":"writ-workflow://wf-1","canonical_resource_id":"workflow:fixture-tenant/fixture-project/wf-1"},
        "action_digest":"sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "attributes":{"from_state":"verifying","to_state":"accepted","acceptance_record_id":"acc-1","artifact_snapshot_id":"snap-1",
        "artifact_digest":"sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},"requested_capabilities":[]}}"#,
    )
}

pub fn bundle_value(text: &str) -> Value {
    parse(text)
}

pub fn load(doc: &Value) -> LoadedBundle {
    load_bundle(doc).unwrap()
}

/// The M0 reference bundle.
pub fn reference_bundle_value() -> Value {
    bundle_value(include_str!(
        "../../docs/contracts/fixtures/policy/m0-reference.bundle.json"
    ))
}

pub fn reference_bundle() -> LoadedBundle {
    load(&reference_bundle_value())
}

/// The M1 transition bundle (protected `verifying` to `accepted`).
pub fn transition_bundle() -> LoadedBundle {
    load(&bundle_value(include_str!(
        "../../m1/fixtures/transition/m1-transition.bundle.json"
    )))
}

/// The wire seal of `request` made with `key` as `adapter_id`.
pub fn sealed(request: &Value, adapter_id: &str, key: &[u8; 32]) -> Value {
    Seal::create(request, adapter_id, &ingress_key(*key))
        .unwrap()
        .to_value()
}

/// `request` sealed by the fixture adapter and verified with the fixture ring.
pub fn verify(request: &Value) -> VerifiedRequest {
    let seal = sealed(request, FIXTURE_ADAPTER, &FIXTURE_KEY);
    verify_ingress(request, Some(&seal), &fixture_ring()).unwrap()
}

/// Copy of `v` with the member at `path` set to `value` (objects created as needed).
pub fn with(v: &Value, path: &[&str], value: Value) -> Value {
    let mut out = v.clone();
    let mut cur = &mut out;
    for (i, key) in path.iter().enumerate() {
        let Value::Obj(m) = cur else {
            panic!("not an object at {key}")
        };
        if i + 1 == path.len() {
            m.insert((*key).to_string(), value);
            return out;
        }
        cur = m
            .entry((*key).to_string())
            .or_insert_with(|| Value::Obj(Default::default()));
    }
    out
}

/// Copy of `v` without the member at `path`.
pub fn without(v: &Value, path: &[&str]) -> Value {
    let mut out = v.clone();
    let mut cur = &mut out;
    for (i, key) in path.iter().enumerate() {
        let Value::Obj(m) = cur else {
            panic!("not an object at {key}")
        };
        if i + 1 == path.len() {
            m.remove(*key);
            return out;
        }
        cur = m.get_mut(*key).unwrap();
    }
    out
}

/// JSON text of `v` with every object's members in reverse order.
pub fn reorder_text(v: &Value) -> String {
    match v {
        Value::Obj(m) => {
            let members: Vec<String> = m
                .iter()
                .rev()
                .map(|(k, x)| format!("{}:{}", json::s(k).to_json_text(), reorder_text(x)))
                .collect();
            format!("{{{}}}", members.join(","))
        }
        Value::Arr(items) => {
            let items: Vec<String> = items.iter().map(reorder_text).collect();
            format!("[{}]", items.join(","))
        }
        other => other.to_json_text(),
    }
}

/// An in-memory [`crate::store::LedgerStore`] with one-shot transaction faults
/// and a record of every stored floor value (to prove it is never lowered).
#[derive(Debug, Default)]
pub struct MemoryLedger {
    pub floor: Option<Timestamp>,
    pub floor_history: Vec<Timestamp>,
    pub events: Vec<crate::clock::ClockEvent>,
    pub refusals: Vec<String>,
    /// The next transaction with this name fails once (`time_floor`, `clock_event`, `audit`).
    pub fail: Option<&'static str>,
    /// Reads of the floor fail while set.
    pub fail_reads: bool,
}

impl MemoryLedger {
    fn fault(&mut self, name: &'static str) -> Result<(), crate::store::StoreError> {
        if self.fail == Some(name) {
            self.fail = None;
            return Err(crate::store::StoreError::Unavailable {
                category: format!("transaction {name}"),
                code: "SQLITE_IOERR".into(),
            });
        }
        Ok(())
    }

    pub fn kinds(&self) -> Vec<crate::clock::ClockEventKind> {
        self.events.iter().map(|e| e.kind).collect()
    }

    /// `true` when the stored floor never decreased.
    pub fn floor_never_lowered(&self) -> bool {
        self.floor_history.windows(2).all(|w| w[0] <= w[1])
    }
}

impl crate::store::LedgerStore for MemoryLedger {
    fn owner_epoch(&self) -> u64 {
        1
    }

    fn record_refusal(&mut self, code: &str) -> Result<(), crate::store::StoreError> {
        self.fault("audit")?;
        self.refusals.push(code.to_string());
        Ok(())
    }

    fn time_floor(&self) -> Result<Option<Timestamp>, crate::store::StoreError> {
        if self.fail_reads {
            return Err(crate::store::StoreError::Unavailable {
                category: "read".into(),
                code: "SQLITE_IOERR".into(),
            });
        }
        Ok(self.floor)
    }

    fn raise_time_floor(&mut self, at: Timestamp) -> Result<Timestamp, crate::store::StoreError> {
        self.fault("time_floor")?;
        let stored = self.floor.map_or(at, |h| h.max(at));
        self.floor = Some(stored);
        self.floor_history.push(stored);
        Ok(stored)
    }

    fn record_clock_event(
        &mut self,
        event: &crate::clock::ClockEvent,
    ) -> Result<(), crate::store::StoreError> {
        self.fault("clock_event")?;
        self.events.push(event.clone());
        Ok(())
    }
}
