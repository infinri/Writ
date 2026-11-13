//! Production tests required by D-037 (M3-08 closure): production cannot create protected
//! initial states or bypass promotion (item 1); effects never apply with absent, tampered
//! or incorrectly bound internal authorization (item 5); agents, models and services cannot
//! approve through the approval-authority boundary (item 7, C3); authorization refuses wrong
//! scope, missing policy and default deny (item 2); a missing or foreign protection
//! classification is refused at policy load or at binding (item 1, `sc-50`/`sc-51`).
//! Production components only, with the existing test keys, sources and authorities.

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::path::PathBuf;

use writ_broker::acceptance::AcceptanceCode;
use writ_broker::effects::{EffectAdapter, VerifiedEffect};
use writ_broker::execute::{Broker, Executed};
use writ_broker::grants::{
    ApprovalAuthority, ApprovalPresentation, ApproverRefusal, AuthenticatedApprover, GrantService,
    PrincipalKind, Verdict,
};
use writ_broker::keys::{AcceptanceRecordKey, MemoryKeys, PermitKey, RunnerAttestationKey};
use writ_broker::permit::Permit;
use writ_broker::policy::{ActivePolicy, PolicyState};
use writ_broker::refusal::Refusal;
use writ_broker::workflow::{AdminRequest, PromotionOutcome, Trust};
use writ_broker::writ_kernel::json::{self, Value};
use writ_broker::writ_kernel::policy::{load_bundle, BundleRejection, LoadedBundle};
use writ_broker::writ_kernel::time::Timestamp;

use super::execute_tests::World;
use super::grant_tests::{
    approve_as, file_write, label, publish, reference, revision, set, verify, TestClock,
};
use super::transition_tests::{
    alice, committed, parse, sha, transition, wf, Docs, Operators, Snapshots, RECORD_KEY,
};
use super::{fault, SqliteEffectStore, SqliteLedger};

// ---------------------------------------------------------------------------
// A workflow world: the M1 transition bundle (the authoritative protection
// classification) active, the transition-test sources, snapshots and operators.

fn transition_bundle_value() -> Value {
    parse(include_str!(
        "../../../m1/fixtures/transition/m1-transition.bundle.json"
    ))
}

fn transition_bundle() -> LoadedBundle {
    load_bundle(&transition_bundle_value()).unwrap()
}

struct Wf {
    ledger: SqliteLedger,
    effects: SqliteEffectStore,
    active: ActivePolicy,
    clock: TestClock,
    key: PermitKey,
    docs: Docs,
    snapshots: Snapshots,
    record_keys: MemoryKeys<AcceptanceRecordKey>,
    runner_keys: MemoryKeys<RunnerAttestationKey>,
}

impl Wf {
    fn new(name: &str) -> Wf {
        let dir: PathBuf =
            std::env::temp_dir().join(format!("writ-d037-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let ledger = SqliteLedger::create(&dir.join("ledger.db")).unwrap();
        let effects =
            SqliteEffectStore::create(&dir.join("effects.db"), ledger.ledger_id()).unwrap();
        let mut record_keys = MemoryKeys::new();
        record_keys.register(
            "acc-key-1",
            AcceptanceRecordKey::from_bytes(RECORD_KEY.to_vec()).unwrap(),
        );
        let w = Wf {
            ledger,
            effects,
            active: ActivePolicy::new(PolicyState::loaded(transition_bundle())),
            clock: TestClock::at(Timestamp::parse_strict("2026-11-07T11:00:00Z").unwrap()),
            key: PermitKey::from_bytes(vec![0x44; 32]).unwrap(),
            docs: Docs::default(),
            snapshots: Snapshots::default(),
            record_keys,
            runner_keys: MemoryKeys::new(),
        };
        for (id, digest) in [("snap-1", sha('b')), ("snap-2", sha('e'))] {
            w.snapshots
                .content
                .lock()
                .unwrap()
                .insert(id.into(), digest);
        }
        w
    }

    fn broker(&self) -> Broker<'_, SqliteLedger, SqliteEffectStore> {
        Broker::new(
            &self.active,
            &self.clock,
            &self.ledger,
            &self.effects,
            &self.key,
        )
    }

    fn admin(&self, request: AdminRequest) -> Result<(), Refusal> {
        self.broker()
            .administer(&Operators, &alice(), &self.snapshots, request)
    }

    fn promote(&self, request: &Value) -> Result<PromotionOutcome, Refusal> {
        let trust = Trust {
            records: &self.docs,
            attestations: &self.docs,
            record_keys: &self.record_keys,
            runner_keys: &self.runner_keys,
            snapshots: &self.snapshots,
        };
        self.broker().promote(&trust, &verify(request), None).result
    }

    fn one(&self, sql: &str) -> i64 {
        self.ledger
            .db()
            .conn
            .query_row(sql, [], |r| r.get(0))
            .unwrap()
    }

    fn state(&self) -> (String, i64) {
        self.ledger
            .db()
            .conn
            .query_row(
                "SELECT state, revision FROM workflows WHERE workflow_id = 'wf-1'",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap()
    }
}

fn create(state: &str) -> AdminRequest {
    AdminRequest::CreateWorkflow {
        key: wf(),
        task_id: "task-1".into(),
        initial_state: state.into(),
        artifact_snapshot_id: "snap-1".into(),
        artifact_digest: sha('b'),
    }
}

fn requirements() -> Value {
    parse(&format!(
        r#"{{"acceptance_policy_id":"accept-code-review","acceptance_policy_version":"3","acceptance_policy_bundle_sha":"{}","criteria":[{{"criterion_id":"build","required_class":"mechanical"}},{{"criterion_id":"docs","required_class":"evidence"}}]}}"#,
        sha('c')
    ))
}

// ---------------------------------------------------------------------------
// D-037 item 1: no protected initial state, no promotion bypass.

#[test]
fn production_administration_never_creates_a_workflow_in_a_protected_state_or_protected_edge_target(
) {
    let w = Wf::new("create");
    let protection = transition_bundle().protection().unwrap().clone();
    let mut targets = protection.protected_to_states.clone();
    targets.extend(
        protection
            .protected_transitions
            .iter()
            .map(|e| e.to_state.clone()),
    );
    assert_eq!(targets, ["accepted", "deployed", "released", "verifying"]);
    for state in &targets {
        assert_eq!(
            w.admin(create(state)),
            Err(Refusal::WorkflowInitialStateProtected),
            "{state}"
        );
    }
    // Without an authoritative classification creation fails closed: no active policy, a
    // rejected one, or an active bundle that has no workflow protection.
    for state in [
        PolicyState::Absent,
        PolicyState::Rejected,
        PolicyState::loaded(reference()),
    ] {
        w.active.activate(state);
        for initial in ["planning", "accepted"] {
            assert_eq!(
                w.admin(create(initial)),
                Err(Refusal::Kernel("PROTECTION_UNAVAILABLE")),
                "{initial}"
            );
        }
    }
    assert_eq!(w.one("SELECT count(*) FROM workflows"), 0);
    assert_eq!(w.one("SELECT count(*) FROM admin_events"), 0);
    assert_eq!(w.one("SELECT count(*) FROM promotion_receipts"), 0);
}

#[test]
fn administration_never_moves_workflow_state_and_protected_states_are_reached_only_by_promotion() {
    // The administrative interface has exactly these operations, and only creation names a
    // state (checked above). A new operation fails to compile here until it is reviewed.
    fn names_a_state(r: &AdminRequest) -> bool {
        match r {
            AdminRequest::CreateWorkflow { .. } => true,
            AdminRequest::UpdateArtifact { .. }
            | AdminRequest::ActivateAcceptancePolicy { .. }
            | AdminRequest::RegisterEvaluator { .. }
            | AdminRequest::RevokeEvaluator { .. } => false,
        }
    }
    let w = Wf::new("no-bypass");
    w.admin(create("planning")).unwrap();
    assert_eq!(
        w.promote(&transition("k0", "planning", "verifying", 0, None)),
        Ok(committed(1))
    );
    assert_eq!(w.state(), ("verifying".into(), 1));
    // Every other administrative operation, in a state adjacent to a protected one, leaves
    // the state as it is; an artifact update increments the revision only.
    let changes = [
        AdminRequest::ActivateAcceptancePolicy {
            tenant_id: wf().tenant_id,
            project_id: wf().project_id,
            requirements: requirements(),
            effect_prerequisites: vec![],
        },
        AdminRequest::UpdateArtifact {
            key: wf(),
            artifact_snapshot_id: "snap-2".into(),
            artifact_digest: sha('e'),
        },
    ];
    for change in changes {
        assert!(!names_a_state(&change));
        w.admin(change).unwrap();
        assert_eq!(w.state().0, "verifying");
    }
    assert_eq!(w.state(), ("verifying".into(), 2));
    // Promotion without acceptance never reaches a protected state: no binding, or a
    // binding naming a record the trusted source does not have.
    for to in ["accepted", "deployed", "released"] {
        assert_eq!(
            w.promote(&transition(&format!("nb-{to}"), "verifying", to, 2, None))
                .unwrap_err()
                .code(),
            "ACCEPTANCE_BINDING_REQUIRED",
            "{to}"
        );
    }
    assert_eq!(
        w.promote(&transition(
            "unknown-record",
            "verifying",
            "accepted",
            2,
            Some(("acc-9", "snap-2", &sha('e')))
        )),
        Err(Refusal::Acceptance(AcceptanceCode::RecordUnknown))
    );
    assert_eq!(w.state(), ("verifying".into(), 2));
    assert_eq!(
        w.one("SELECT count(*) FROM promotion_receipts"),
        1,
        "k0 only"
    );
    assert_eq!(w.one("SELECT count(*) FROM consumed_records"), 0);
}

// ---------------------------------------------------------------------------
// D-037 items 1 and 2 (`sc-50`, `sc-51`): the protection classification exists only as
// part of a validated, active authorization bundle.

#[test]
fn a_missing_or_malformed_protection_classification_is_refused_at_policy_load() {
    let base = transition_bundle_value();
    let without = match &base {
        Value::Obj(m) => {
            let mut m = m.clone();
            m.remove("workflow_protection");
            Value::Obj(m)
        }
        _ => unreachable!(),
    };
    assert_eq!(
        load_bundle(&without).err(),
        Some(BundleRejection::ProtectionMissing),
        "sc-50: a bundle governing transitions without a classification"
    );
    let protection = |path: &[&str], v: Value| {
        let mut p = vec!["workflow_protection"];
        p.extend_from_slice(path);
        load_bundle(&set(&base, &p, v)).err()
    };
    assert_eq!(
        protection(
            &["protected_to_states"],
            parse(r#"["accepted","accepted"]"#)
        ),
        Some(BundleRejection::DuplicateProtectedState)
    );
    for edges in [
        r#"[{"from_state":"draft","to_state":"draft"}]"#,
        r#"[{"from_state":"draft","to_state":"verifying"},{"from_state":"draft","to_state":"verifying"}]"#,
    ] {
        assert_eq!(
            protection(&["protected_transitions"], parse(edges)),
            Some(BundleRejection::InvalidProtectedEdge),
            "{edges}"
        );
    }
    for (path, v) in [
        (&["acceptance_policy_bundle_sha"][..], json::s("sha256:C")),
        (&["protected_to_states"][..], json::s("accepted")),
        (&["extra"][..], Value::Int(1)),
    ] {
        assert!(
            matches!(protection(path, v), Some(BundleRejection::Schema(_))),
            "{path:?}"
        );
    }
    assert!(matches!(
        load_bundle(&set(&base, &["workflow_protection"], Value::Null)).err(),
        Some(BundleRejection::Schema(_) | BundleRejection::ProtectionMissing)
    ));
}

#[test]
fn promotion_is_classified_only_by_the_active_bundles_protection_never_another_or_none() {
    let w = Wf::new("classification");
    w.admin(create("planning")).unwrap();
    // sc-51 substitute: the classification in force is the active bundle's, read at use
    // time; a bundle that protects planning to verifying makes that edge require
    // acceptance immediately after activation.
    let stricter = set(
        &set(&transition_bundle_value(), &["version"], json::s("0.4.2")),
        &["workflow_protection", "protected_transitions"],
        parse(
            r#"[{"from_state":"draft","to_state":"verifying"},{"from_state":"planning","to_state":"verifying"}]"#,
        ),
    );
    w.active
        .activate(PolicyState::loaded(load_bundle(&stricter).unwrap()));
    assert_eq!(
        w.promote(&transition("k1", "planning", "verifying", 0, None))
            .unwrap_err()
            .code(),
        "ACCEPTANCE_BINDING_REQUIRED"
    );
    // sc-50 substitute: an active bundle without a classification never lets a
    // promotion proceed.
    for (state, code) in [
        (PolicyState::loaded(reference()), "UNSUPPORTED_CAPABILITY"),
        (PolicyState::Absent, "POLICY_UNAVAILABLE"),
        (PolicyState::Rejected, "POLICY_BUNDLE_REJECTED"),
    ] {
        w.active.activate(state);
        assert_eq!(
            w.promote(&transition("k2", "planning", "verifying", 0, None))
                .unwrap_err()
                .code(),
            code
        );
    }
    assert_eq!(w.state(), ("planning".into(), 0));
    assert_eq!(w.one("SELECT count(*) FROM promotion_receipts"), 0);
    // Under the original bundle the same edge is unprotected and commits.
    w.active.activate(PolicyState::loaded(transition_bundle()));
    assert_eq!(
        w.promote(&transition("k3", "planning", "verifying", 0, None)),
        Ok(committed(1))
    );
}

// ---------------------------------------------------------------------------
// D-037 item 5: internal authorization (`sc-46`, `sc-62`).

fn effect_rows(e: &SqliteEffectStore) -> i64 {
    e.db()
        .conn
        .query_row("SELECT count(*) FROM effects", [], |r| r.get(0))
        .unwrap()
}

fn consumed_rows(e: &SqliteEffectStore) -> i64 {
    e.db()
        .conn
        .query_row("SELECT count(*) FROM consumed_permits", [], |r| r.get(0))
        .unwrap()
}

fn tampered(permit: &Permit, member: &str, value: &str) -> Permit {
    let v = set(&permit.to_value(), &[member], json::s(value));
    Permit::from_value(&v).unwrap()
}

#[test]
fn the_adapter_applies_no_effect_without_a_verified_internal_authorization() {
    let w = World::new("d037-absent");
    for refusal in [
        Refusal::PermitInvalid,
        Refusal::PermitRequestMismatch,
        Refusal::PermitExpired,
        Refusal::PermitStale,
        Refusal::TimeUnavailable,
    ] {
        let r = refusal.clone();
        let mut absent = move || -> Result<VerifiedEffect, Refusal> { Err(r.clone()) };
        assert_eq!(w.effects.apply(&mut absent), Ok(Err(refusal)));
    }
    assert_eq!(effect_rows(&w.effects), 0);
    assert_eq!(consumed_rows(&w.effects), 0);
    let b = w.broker();
    // A permit-shaped document bound to the presented request that the broker never issued.
    let req = file_write("k1");
    let never = Permit::from_value(&parse(&format!(
        r#"{{"permit_id":"permit-1","request_fingerprint":"{}","policy_bundle_sha":"{}","expires_at":"2026-11-05T12:00:30Z","mac":"{}"}}"#,
        verify(&req).fingerprint().as_str(),
        reference().sha.as_str(),
        "0".repeat(64)
    )))
    .unwrap();
    assert_eq!(b.present_permit(&never, &req), Refusal::PermitInvalid);
    assert!(b.execute(&verify(&req), revision(), None).permit.is_some());
    assert_eq!(b.present_permit(&never, &req), Refusal::PermitInvalid);
    assert_eq!(
        effect_rows(&w.effects),
        1,
        "only the broker's own execution"
    );
    assert_eq!(consumed_rows(&w.effects), 1);
}

#[test]
fn a_refused_execution_is_issued_no_internal_authorization_and_applies_nothing() {
    let w = World::new("d037-refused");
    let b = w.broker();
    let denied = set(
        &file_write("k1"),
        &["action", "kind"],
        json::s("agent.dispatch"),
    );
    let e = b.execute(&verify(&denied), revision(), None);
    assert!(e.result.is_err() && e.permit.is_none());
    assert_eq!(effect_rows(&w.effects), 0);
    assert_eq!(consumed_rows(&w.effects), 0);
}

#[test]
fn a_tampered_or_incorrectly_bound_internal_authorization_never_applies_an_effect() {
    let w = World::new("d037-tampered");
    let b = w.broker();
    let req = file_write("k1");
    // An issued, unconsumed permit: the adapter transaction could not begin.
    w.effects
        .inject(fault::Stage::Begin, rusqlite::ffi::SQLITE_IOERR);
    let permit = b
        .execute(&verify(&req), revision(), None)
        .permit
        .expect("issued at dispatch");
    let mac = permit
        .to_value()
        .get("mac")
        .unwrap()
        .as_str()
        .unwrap()
        .to_string();
    let flipped = format!(
        "{}{}",
        if mac.starts_with('0') { "1" } else { "0" },
        &mac[1..]
    );
    for (member, value, want) in [
        ("mac", flipped.as_str(), Refusal::PermitInvalid),
        ("permit_id", "permit-999", Refusal::PermitInvalid),
        (
            "policy_bundle_sha",
            sha('9').as_str(),
            Refusal::PermitInvalid,
        ),
        ("expires_at", "2099-01-01T00:00:00Z", Refusal::PermitInvalid),
        (
            "request_fingerprint",
            sha('9').as_str(),
            Refusal::PermitRequestMismatch,
        ),
    ] {
        assert_eq!(
            b.present_permit(&tampered(&permit, member, value), &req),
            want,
            "{member}"
        );
    }
    // Bound to another request: another key, resource or tenant.
    for other in [
        file_write("k2"),
        set(
            &req,
            &["action", "resource", "canonical_resource_id"],
            json::s("workspace:fixture-tenant/fixture-project/src/other.py"),
        ),
        set(&req, &["tenant_id"], json::s("other-tenant")),
    ] {
        assert_eq!(
            b.present_permit(&permit, &other),
            Refusal::PermitRequestMismatch
        );
    }
    assert_eq!(effect_rows(&w.effects), 0);
    assert_eq!(consumed_rows(&w.effects), 0);
    // A permit consumed by one execution never authorizes another request.
    let k3 = file_write("k3");
    let used = b.execute(&verify(&k3), revision(), None);
    assert_eq!(used.result, Ok(Executed::EffectCompleted));
    let used = used.permit.unwrap();
    assert_eq!(
        b.present_permit(&used, &file_write("k4")),
        Refusal::PermitRequestMismatch
    );
    assert_eq!(b.present_permit(&used, &k3), Refusal::PermitReplayed);
    assert_eq!(effect_rows(&w.effects), 1, "k3 only");
    assert_eq!(consumed_rows(&w.effects), 1);
}

// ---------------------------------------------------------------------------
// D-037 item 7 (C3): the approval-authority boundary.

/// An authority that authenticates every presentation, as the claimed identity on the
/// presented channel, with a fixed principal kind.
struct Vouches(PrincipalKind);

impl ApprovalAuthority for Vouches {
    fn authenticate(
        &self,
        p: &ApprovalPresentation<'_>,
    ) -> Result<AuthenticatedApprover, ApproverRefusal> {
        Ok(AuthenticatedApprover {
            id: p.approver.into(),
            kind: self.0,
            channel: p.channel.into(),
            issuer: "realm:fixture".into(),
            evidence_ref: "authn-event:vouched".into(),
        })
    }
}

#[test]
fn agents_models_and_services_authenticated_by_the_authority_never_approve_or_enable_an_effect() {
    let w = World::new("d037-not-human");
    let svc = GrantService::new(&w.active, &w.clock, &w.ledger);
    let req = publish("k1");
    for kind in [
        PrincipalKind::Agent,
        PrincipalKind::Model,
        PrincipalKind::Service,
    ] {
        for approver in ["human:alice", "agent:helper", "model:assistant", "svc:ci"] {
            for verdict in [Verdict::Approved, Verdict::Denied] {
                let p = ApprovalPresentation {
                    channel: "approval-ui",
                    approver,
                    credential: b"accepted-by-the-authority",
                };
                assert_eq!(
                    approve_as(&svc, &req, "g1", verdict, None, &p, &Vouches(kind)),
                    Err(Refusal::HumanUnauthenticated(ApproverRefusal::NotHuman)),
                    "{kind:?} {approver}"
                );
            }
        }
    }
    assert_eq!(
        w.ledger
            .db()
            .conn
            .query_row("SELECT count(*) FROM grants", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        0
    );
    // The request that needed the approval stays unexecutable.
    let b = w.broker();
    let g = label("g1");
    assert_eq!(
        b.execute(&verify(&req), revision(), Some(&g)).result,
        Err(Refusal::GrantUnknown)
    );
    assert_eq!(
        b.execute(&verify(&publish("k2")), revision(), None).result,
        Err(Refusal::ApprovalRequired)
    );
    assert_eq!(effect_rows(&w.effects), 0);
}

// ---------------------------------------------------------------------------
// D-037 item 2: authorization on the production execute path.

#[test]
fn execution_outside_the_request_scope_without_policy_or_without_a_permitting_statement_is_refused()
{
    let w = World::new("d037-authorization");
    let b = w.broker();
    let wrong_scope = [
        set(
            &file_write("s1"),
            &["action", "resource", "canonical_resource_id"],
            json::s("workspace:fixture-tenant/other-project/src/app.py"),
        ),
        set(
            &file_write("s2"),
            &["action", "resource", "canonical_resource_id"],
            json::s("workspace:other-tenant/fixture-project/src/app.py"),
        ),
    ];
    for req in &wrong_scope {
        let e = b.execute(&verify(req), revision(), None);
        assert_eq!(
            e.result.as_ref().map_err(Refusal::code),
            Err("RESOURCE_SCOPE_MISMATCH")
        );
        assert!(e.permit.is_none());
    }
    // Default deny: a known capability no statement permits.
    let unmatched = set(
        &file_write("d1"),
        &["action", "kind"],
        json::s("agent.dispatch"),
    );
    let e = b.execute(&verify(&unmatched), revision(), None);
    assert_eq!(
        e.result.as_ref().map_err(Refusal::code),
        Err("DEFAULT_DENY")
    );
    assert!(e.permit.is_none());
    // Missing or rejected policy fails closed for a request the reference bundle allows.
    for (state, code) in [
        (PolicyState::Absent, "POLICY_UNAVAILABLE"),
        (PolicyState::Rejected, "POLICY_BUNDLE_REJECTED"),
    ] {
        w.active.activate(state);
        let e = b.execute(&verify(&file_write("m1")), revision(), None);
        assert_eq!(e.result.as_ref().map_err(Refusal::code), Err(code));
        assert!(e.permit.is_none());
    }
    assert_eq!(
        w.ledger
            .db()
            .conn
            .query_row("SELECT count(*) FROM receipts", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        0
    );
    assert_eq!(effect_rows(&w.effects), 0);
    // The same request executes once the reference policy is active again.
    w.active.activate(PolicyState::loaded(reference()));
    assert_eq!(
        b.execute(&verify(&file_write("m1")), revision(), None)
            .result,
        Ok(Executed::EffectCompleted)
    );
}
