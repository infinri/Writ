//! M3-02 scenario tests: TOCTOU (decision versus use) and ingress key
//! rotation, through the public API only.

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use crate::authorize::{authorize, Authorized, UseTimeStatus};
use crate::clock::FixedClock;
use crate::ingress::verify_ingress;
use crate::keys::MemoryKeyRing;
use crate::permit::{Permit, PermitId};
use crate::policy::{ActivePolicy, PolicyState};
use crate::refusal::Refusal;
use crate::test_support::*;
use writ_kernel::json::{self, Value};
use writ_kernel::policy::LoadedBundle;

/// What a use-time decision produced, copied out of the hold: the
/// [`Authorized`] capability itself cannot leave it.
#[derive(Debug)]
struct Decided {
    status: UseTimeStatus,
    bundle_sha: String,
    approval_request_reference: bool,
    permit: Permit,
}

/// Decide `request` now under `bundle` through the internal-gate entry point
/// and issue a permit for it inside the hold.
fn decide_now(
    request: &Value,
    seal: &Value,
    ring: &MemoryKeyRing,
    bundle: &LoadedBundle,
) -> Result<Decided, Refusal> {
    let verified = verify_ingress(request, Some(seal), ring)?;
    let active = ActivePolicy::new(PolicyState::loaded(bundle.clone()));
    active.execute(
        &verified,
        revision(5),
        &FixedClock::new(now()),
        |authorized| Decided {
            status: authorized.status(),
            bundle_sha: authorized.policy_bundle_sha().to_string(),
            approval_request_reference: authorized.decision().approval_request_reference.is_some(),
            permit: permit_for(&authorized),
        },
    )
}

/// The reference bundle with `statements` replaced and a new version.
fn bundle_with_statements(version: &str, statements: &str) -> LoadedBundle {
    let doc = with(&reference_bundle_value(), &["version"], json::s(version));
    let doc = with(
        &doc,
        &["statements"],
        json::parse(statements.as_bytes()).unwrap(),
    );
    load(&doc)
}

fn forbid_workspace_writes() -> LoadedBundle {
    bundle_with_statements(
        "0.2.0",
        r#"[{"statement_id":"forbid-all-writes","effect":"forbid","action_kinds":["file.write"],"reason_code":"WRITES_FROZEN"}]"#,
    )
}

fn approve_workspace_writes() -> LoadedBundle {
    bundle_with_statements(
        "0.3.0",
        r#"[{"statement_id":"approve-writes","effect":"require_approval","action_kinds":["file.write"],"reason_code":"WRITES_NEED_APPROVAL"}]"#,
    )
}

fn permit_for(authorized: &Authorized<'_>) -> Permit {
    Permit::issue(
        &PermitId::new("permit-1").unwrap(),
        authorized,
        &FixedClock::new(now()),
        &permit_key(PERMIT_KEY),
    )
    .unwrap()
}

// ---------------------------------------------------------------------------
// TOCTOU

#[test]
fn toctou_request_altered_after_sealing_is_refused() {
    let ring = fixture_ring();
    let bundle = reference_bundle();
    let req = base_request();
    let seal = sealed(&req, FIXTURE_ADAPTER, &FIXTURE_KEY);
    assert!(decide_now(&req, &seal, &ring, &bundle).is_ok());
    // Altered to another permitted target: still refused, before any decision.
    let retargeted = with(
        &req,
        &["action", "resource", "canonical_resource_id"],
        json::s("workspace:fixture-tenant/fixture-project/src/other.py"),
    );
    assert_eq!(
        decide_now(&retargeted, &seal, &ring, &bundle).unwrap_err(),
        Refusal::IngressUnauthenticated
    );
    // Altered into an operator: refused at ingress, not by policy.
    let escalated = with(
        &with(&req, &["action", "kind"], json::s("process.execute")),
        &["principal", "roles"],
        Value::Arr(vec![json::s("operator")]),
    );
    assert_eq!(
        decide_now(&escalated, &seal, &ring, &bundle).unwrap_err(),
        Refusal::IngressUnauthenticated
    );
    // Altered idempotency key (a different receipt slot): refused.
    let rekeyed = with(&req, &["idempotency_key"], json::s("idem-2"));
    assert_eq!(
        decide_now(&rekeyed, &seal, &ring, &bundle).unwrap_err(),
        Refusal::IngressUnauthenticated
    );
}

#[test]
fn toctou_bundle_changed_between_decision_and_use_is_re_decided() {
    let ring = fixture_ring();
    let old = reference_bundle();
    let req = base_request();
    let seal = sealed(&req, FIXTURE_ADAPTER, &FIXTURE_KEY);
    let earlier = decide_now(&req, &seal, &ring, &old).unwrap();
    assert_eq!(earlier.status, UseTimeStatus::Allow);
    assert_eq!(earlier.bundle_sha, old.sha.as_str());

    // The active bundle now forbids the write: use is refused under the new bundle,
    // whatever the earlier decision said.
    let frozen = forbid_workspace_writes();
    assert_ne!(frozen.sha, old.sha);
    let refusal = decide_now(&req, &seal, &ring, &frozen).unwrap_err();
    assert_eq!(refusal.code(), "WRITES_FROZEN");

    // The active bundle now requires approval: the use-time status follows it and
    // is bound to the new bundle digest.
    let gated = approve_workspace_writes();
    let now_gated = decide_now(&req, &seal, &ring, &gated).unwrap();
    assert_eq!(now_gated.status, UseTimeStatus::RequireApproval);
    assert_eq!(now_gated.bundle_sha, gated.sha.as_str());
    assert!(now_gated.approval_request_reference);

    // Re-decision also works the other way: denied earlier, allowed now.
    let secrets = with(
        &req,
        &["action", "resource", "canonical_resource_id"],
        json::s("workspace:fixture-tenant/fixture-project/secrets/k"),
    );
    let secrets_seal = sealed(&secrets, FIXTURE_ADAPTER, &FIXTURE_KEY);
    assert_eq!(
        decide_now(&secrets, &secrets_seal, &ring, &old)
            .unwrap_err()
            .code(),
        "SECRETS_PATH_FORBIDDEN"
    );
    let open = bundle_with_statements(
        "0.4.0",
        r#"[{"statement_id":"permit-writes","effect":"permit","action_kinds":["file.write"],"reason_code":"WRITES_OK"}]"#,
    );
    assert_eq!(
        decide_now(&secrets, &secrets_seal, &ring, &open)
            .unwrap()
            .status,
        UseTimeStatus::Allow
    );
}

#[test]
fn toctou_request_pinned_to_the_old_bundle_is_refused_after_a_change() {
    let ring = fixture_ring();
    let old = reference_bundle();
    let req = with(
        &base_request(),
        &["policy_bundle_sha"],
        json::s(old.sha.as_str()),
    );
    let seal = sealed(&req, FIXTURE_ADAPTER, &FIXTURE_KEY);
    assert!(decide_now(&req, &seal, &ring, &old).is_ok());
    let new = bundle_with_statements(
        "0.1.1",
        r#"[{"statement_id":"permit-workspace-files","effect":"permit","action_kinds":["file.read","file.write"],"resource_prefixes":["workspace:fixture-tenant/fixture-project/"],"reason_code":"WORKSPACE_FILE_PERMITTED"}]"#,
    );
    assert_eq!(
        decide_now(&req, &seal, &ring, &new).unwrap_err().code(),
        "POLICY_VERSION_MISMATCH"
    );
}

#[test]
fn toctou_policy_unavailable_at_use_is_refused() {
    let ring = fixture_ring();
    let req = base_request();
    let seal = sealed(&req, FIXTURE_ADAPTER, &FIXTURE_KEY);
    assert!(decide_now(&req, &seal, &ring, &reference_bundle()).is_ok());
    let verified = verify_ingress(&req, Some(&seal), &ring).unwrap();
    let active = ActivePolicy::new(PolicyState::Absent);
    let refusal = active
        .execute(&verified, revision(5), &FixedClock::new(now()), |_| ())
        .unwrap_err();
    assert_eq!(refusal.code(), "POLICY_UNAVAILABLE");
}

#[test]
fn toctou_permit_presented_for_a_changed_request_is_refused() {
    let ring = fixture_ring();
    let req = base_request();
    let seal = sealed(&req, FIXTURE_ADAPTER, &FIXTURE_KEY);
    let permit = decide_now(&req, &seal, &ring, &reference_bundle())
        .unwrap()
        .permit;
    let key = permit_key(PERMIT_KEY);
    let clock = FixedClock::new(now());
    let active = ActivePolicy::new(PolicyState::loaded(reference_bundle()));
    let active = active.shared();
    assert!(permit.verify(&req, &key, &clock, &active).is_ok());
    for (path, value) in [
        (
            &["action", "resource", "canonical_resource_id"][..],
            json::s("workspace:fixture-tenant/fixture-project/src/other.py"),
        ),
        (&["action", "kind"][..], json::s("file.read")),
        (&["idempotency_key"][..], json::s("idem-2")),
        (&["request_id"][..], json::s("req-2")),
        (&["trace_id"][..], json::s("trace-2")),
        (&["principal", "id"][..], json::s("agent:other")),
        (
            &["action", "attributes"][..],
            json::obj([("mode", json::s("append"))]),
        ),
    ] {
        let changed = with(&req, path, value);
        assert_eq!(
            permit.verify(&changed, &key, &clock, &active),
            Err(Refusal::PermitRequestMismatch),
            "{path:?}"
        );
    }
    // A reordered serialization of the same request is the same request.
    let reordered = json::parse(reorder_text(&req).as_bytes()).unwrap();
    assert!(permit.verify(&reordered, &key, &clock, &active).is_ok());
}

#[test]
fn toctou_permit_carries_the_bundle_of_its_use_time_decision() {
    let ring = fixture_ring();
    let req = base_request();
    let seal = sealed(&req, FIXTURE_ADAPTER, &FIXTURE_KEY);
    let old = reference_bundle();
    let first = decide_now(&req, &seal, &ring, &old).unwrap().permit;
    assert_eq!(first.policy_bundle_sha(), old.sha.as_str());
    let new = bundle_with_statements(
        "0.5.0",
        r#"[{"statement_id":"permit-writes","effect":"permit","action_kinds":["file.write"],"reason_code":"WRITES_OK"}]"#,
    );
    let second = decide_now(&req, &seal, &ring, &new).unwrap().permit;
    assert_eq!(second.policy_bundle_sha(), new.sha.as_str());
    let key = permit_key(PERMIT_KEY);
    let clock = FixedClock::new(now());
    let active = ActivePolicy::new(PolicyState::loaded(new.clone()));
    let verified = second.verify(&req, &key, &clock, &active.shared()).unwrap();
    assert_eq!(verified.policy_bundle_sha, new.sha.as_str());
    // The permit issued under the old bundle is stale once the new one is active.
    assert_eq!(
        first.verify(&req, &key, &clock, &active.shared()),
        Err(Refusal::PermitStale)
    );
}

#[test]
fn toctou_policy_replaced_between_issue_and_presentation_is_stale() {
    let ring = fixture_ring();
    let req = base_request();
    let seal = sealed(&req, FIXTURE_ADAPTER, &FIXTURE_KEY);
    let active = ActivePolicy::new(PolicyState::loaded(reference_bundle()));
    let clock = FixedClock::new(now());
    let key = permit_key(PERMIT_KEY);
    let permit = {
        let shared = active.shared();
        let verified = verify_ingress(&req, Some(&seal), &ring).unwrap();
        let authorized = authorize(&verified, &shared.trusted_inputs(revision(5)), &clock).unwrap();
        permit_for(&authorized)
    };
    // Replaced by a bundle that still allows the request: the permit is stale
    // anyway; a fresh use-time decision under the new bundle is required.
    active.activate(PolicyState::loaded(bundle_with_statements(
        "0.6.0",
        r#"[{"statement_id":"permit-writes","effect":"permit","action_kinds":["file.write"],"reason_code":"WRITES_OK"}]"#,
    )));
    let shared = active.shared();
    let refusal = permit.verify(&req, &key, &clock, &shared).unwrap_err();
    assert_eq!(refusal, Refusal::PermitStale);
    assert_eq!(refusal.external_code(), "PROMOTION_REFUSED");
    let verified = verify_ingress(&req, Some(&seal), &ring).unwrap();
    let fresh = authorize(&verified, &shared.trusted_inputs(revision(5)), &clock).unwrap();
    let fresh_permit = permit_for(&fresh);
    assert!(fresh_permit.verify(&req, &key, &clock, &shared).is_ok());
    drop(shared);
    // The policy becomes unavailable: every permit fails closed.
    active.activate(PolicyState::Absent);
    assert_eq!(
        fresh_permit.verify(&req, &key, &clock, &active.shared()),
        Err(Refusal::PermitStale)
    );
}

// ---------------------------------------------------------------------------
// Ingress key rotation

#[test]
fn rotation_removed_adapter_key_refuses_seals_made_with_it() {
    let mut ring = fixture_ring();
    let req = base_request();
    let seal = sealed(&req, FIXTURE_ADAPTER, &FIXTURE_KEY);
    assert!(verify_ingress(&req, Some(&seal), &ring).is_ok());
    assert!(ring.remove(FIXTURE_ADAPTER));
    assert_eq!(
        verify_ingress(&req, Some(&seal), &ring),
        Err(Refusal::IngressUnauthenticated)
    );
    // A fresh seal with the removed key is refused too.
    let fresh = sealed(&req, FIXTURE_ADAPTER, &FIXTURE_KEY);
    assert_eq!(
        verify_ingress(&req, Some(&fresh), &ring),
        Err(Refusal::IngressUnauthenticated)
    );
}

#[test]
fn rotation_new_key_works_and_old_key_seals_refuse() {
    let mut ring = fixture_ring();
    let req = base_request();
    let old_seal = sealed(&req, FIXTURE_ADAPTER, &FIXTURE_KEY);
    let new_key = [0x55; 32];
    assert!(ring
        .register(FIXTURE_ADAPTER, ingress_key(new_key))
        .is_some());
    let new_seal = sealed(&req, FIXTURE_ADAPTER, &new_key);
    assert!(verify_ingress(&req, Some(&new_seal), &ring).is_ok());
    assert_eq!(
        verify_ingress(&req, Some(&old_seal), &ring),
        Err(Refusal::IngressUnauthenticated)
    );
    // The new key authorizes end to end.
    let decided = decide_now(&req, &new_seal, &ring, &reference_bundle()).unwrap();
    assert_eq!(decided.status, UseTimeStatus::Allow);
}

#[test]
fn rotation_of_one_adapter_does_not_affect_another() {
    let mut ring = fixture_ring();
    let req = with(
        &base_request(),
        &["principal", "verified_by"],
        json::s(OTHER_ADAPTER),
    );
    let seal = sealed(&req, OTHER_ADAPTER, &OTHER_KEY);
    assert!(ring.remove(FIXTURE_ADAPTER));
    assert!(verify_ingress(&req, Some(&seal), &ring).is_ok());
}

#[test]
fn rotation_newly_registered_adapter_is_accepted() {
    let mut ring = fixture_ring();
    let req = with(
        &base_request(),
        &["principal", "verified_by"],
        json::s(ROGUE_ADAPTER),
    );
    let seal = sealed(&req, ROGUE_ADAPTER, &ROGUE_KEY);
    assert_eq!(
        verify_ingress(&req, Some(&seal), &ring),
        Err(Refusal::IngressUnauthenticated)
    );
    ring.register(ROGUE_ADAPTER, ingress_key(ROGUE_KEY));
    assert!(verify_ingress(&req, Some(&seal), &ring).is_ok());
}
