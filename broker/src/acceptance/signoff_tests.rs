//! M4-06: the I-5 use-time checks of a signoff in [`super::check_use`], each in
//! isolation (D-040 O-4, O-5, O-7; M4-INTERFACES I-5). Test values only.

use std::collections::BTreeSet;

use super::record_v2_tests::{
    current, human_requirements, keys, sha, signed, v2_record, with_human,
};
use super::tests::good_record;
use super::*;
use crate::approval::{NewSignoff, SignoffFields};
use crate::human::{LedgerCapability, PrincipalKind};
use writ_kernel::json::Value;

fn bob() -> SubjectId {
    SubjectId::new("writ-local", "bob")
}

fn at(s: &str) -> Timestamp {
    Timestamp::parse_strict(s).unwrap()
}

fn signoff_binding() -> NewSignoff {
    NewSignoff {
        signoff_id: "so-1".into(),
        tenant_id: "t1".into(),
        project_id: "p1".into(),
        task_id: "task-1".into(),
        workflow_id: "wf-1".into(),
        from_state: "verifying".into(),
        to_state: "accepted".into(),
        acceptance_record_id: "acc-1".into(),
        criterion_id: "signoff".into(),
        artifact_snapshot_id: "snap-1".into(),
        artifact_digest: sha('b'),
        acceptance_policy_id: "accept-code-review".into(),
        acceptance_policy_version: "3".into(),
        acceptance_policy_bundle_sha: sha('c'),
        state_revision: 7,
    }
}

/// A stored signoff issued at 10:55 expiring at 11:05 (600 s, Y1); trusted time is 11:00.
fn stored(binding: NewSignoff, exclusions: SignoffExclusions) -> Signoff {
    stored_at(
        binding,
        exclusions,
        at("2026-11-07T10:55:00Z"),
        at("2026-11-07T11:05:00Z"),
    )
}

fn stored_at(
    binding: NewSignoff,
    exclusions: SignoffExclusions,
    issued_at: Timestamp,
    expires_at: Timestamp,
) -> Signoff {
    Signoff::from_stored(
        &LedgerCapability::for_ledger_implementation(),
        SignoffFields {
            binding,
            expires_at,
            exclusions: exclusions.normalized(),
            issued_at,
            subject: bob(),
            credential_id: "cred-bob".into(),
            challenge_id: "ch-1".into(),
            provenance_version: PROVENANCE_VERSION_2.into(),
        },
    )
    .unwrap()
}

fn requester() -> SubjectId {
    SubjectId::new("writ-host-adapter:fixture", "agent:session-1")
}

/// A valid stored signoff `so-1` by `bob` (enrolled, active, SignoffAuthority in
/// t1/p1), excluding the requester.
fn signoff_use() -> SignoffUse {
    let exclusions = SignoffExclusions {
        requesters: vec![requester()],
        ..SignoffExclusions::default()
    };
    SignoffUse {
        signoff: stored(signoff_binding(), exclusions),
        provenance_intact: true,
        signoff_revoked: false,
        credential_revoked: false,
        signer: Some(SubjectRecord {
            subject: bob(),
            kind: PrincipalKind::Human,
            status: SubjectStatus::Active,
        }),
        enrollment: Some(
            EnrollmentRecord::new(
                &LedgerCapability::for_ledger_implementation(),
                bob(),
                vec![],
                at("2026-11-01T00:00:00Z"),
                SubjectId::new("writ-local", "root"),
            )
            .unwrap(),
        ),
        assignments: vec![RoleAssignment {
            assignment_id: "role-1".into(),
            subject: bob(),
            kind: PrincipalKind::Human,
            permission: Permission::SignoffAuthority,
            tenant_id: "t1".into(),
            project_id: "p1".into(),
            granted_at: at("2026-11-01T00:00:00Z"),
            revoked_at: None,
        }],
        exclusions: SignoffExclusions::default(),
        consumed: false,
    }
}

fn full_check(
    verified: &VerifiedRecord,
    signoffs: &[SignoffUse],
    parties: &[SubjectId],
) -> Result<Consumption, AcceptanceCode> {
    use super::tests::{attestation, registry_entry, runner_keys};
    let current = current();
    let requirements = human_requirements();
    let entries = [
        registry_entry("ci-runner"),
        registry_entry("doc-check"),
        registry_entry("approval-service"),
    ];
    let registry = |id: &str| {
        entries
            .iter()
            .find(|e| e.as_value().get("evaluator_id").and_then(Value::as_str) == Some(id))
            .cloned()
    };
    let atts = [verify_attestation(&attestation(), &runner_keys(), current.now).unwrap()];
    check_use(
        verified,
        &UseContext {
            current: &current,
            requirements: &requirements,
            registry: &registry,
            attestations: &atts,
            consumed_attestations: &BTreeSet::new(),
            signoffs,
            promotion_parties: parties,
        },
    )
}

fn with_use(change: impl FnOnce(&mut SignoffUse)) -> Result<Consumption, AcceptanceCode> {
    let v = verify_record(&v2_record(), &keys()).unwrap();
    let mut u = signoff_use();
    change(&mut u);
    full_check(&v, &[u], &[requester()])
}

fn signoff_code(code: ApprovalCode) -> Result<Consumption, AcceptanceCode> {
    Err(AcceptanceCode::Signoff(code))
}

#[test]
fn a_valid_signoff_is_named_for_consumption_with_the_attestations() {
    assert_eq!(
        with_use(|_| {}),
        Ok(Consumption {
            attestation_ids: vec!["att-1".into()],
            signoff_ids: vec!["so-1".into()],
        })
    );
    // The same /1 record never reaches a signoff.
    let v1 = verify_record(&signed(&with_human(&good_record())), &keys()).unwrap();
    assert_eq!(
        full_check(&v1, &[signoff_use()], &[]),
        Err(AcceptanceCode::HumanUnavailable)
    );
}

#[test]
fn missing_out_of_scope_or_broken_signoffs_are_refused() {
    let v = verify_record(&v2_record(), &keys()).unwrap();
    assert_eq!(
        full_check(&v, &[], &[]),
        signoff_code(ApprovalCode::SignoffUnknown)
    );
    let mut other_tenant = signoff_binding();
    other_tenant.tenant_id = "t2".into();
    assert_eq!(
        with_use(|u| u.signoff = stored(other_tenant, SignoffExclusions::default())),
        signoff_code(ApprovalCode::SignoffUnknown)
    );
    assert_eq!(
        with_use(|u| u.provenance_intact = false),
        signoff_code(ApprovalCode::SignoffMismatch)
    );
    assert_eq!(
        with_use(|u| u.signer = None),
        signoff_code(ApprovalCode::SignoffMismatch)
    );
}

#[test]
fn every_binding_member_must_equal_the_record_policy_and_current_state() {
    let changes: Vec<fn(&mut NewSignoff)> = vec![
        |b| b.acceptance_record_id = "acc-2".into(),
        |b| b.criterion_id = "review".into(),
        |b| b.task_id = "task-2".into(),
        |b| b.workflow_id = "wf-2".into(),
        |b| b.from_state = "draft".into(),
        |b| b.to_state = "deployed".into(),
        |b| b.artifact_snapshot_id = "snap-2".into(),
        |b| b.artifact_digest = format!("sha256:{}", "d".repeat(64)),
        |b| b.acceptance_policy_id = "other-policy".into(),
        |b| b.acceptance_policy_version = "4".into(),
        |b| b.acceptance_policy_bundle_sha = format!("sha256:{}", "e".repeat(64)),
        |b| b.state_revision = 6,
    ];
    for (i, change) in changes.into_iter().enumerate() {
        let mut b = signoff_binding();
        change(&mut b);
        assert_eq!(
            with_use(|u| u.signoff = stored(b, u.signoff.fields().exclusions.clone())),
            signoff_code(ApprovalCode::SignoffMismatch),
            "change {i}"
        );
    }
}

#[test]
fn revoked_expired_unauthorized_unseparated_or_consumed_signoffs_are_refused() {
    let revoked = signoff_code(ApprovalCode::SignoffRevoked);
    assert_eq!(with_use(|u| u.signoff_revoked = true), revoked);
    assert_eq!(with_use(|u| u.credential_revoked = true), revoked);
    assert_eq!(
        with_use(|u| {
            if let Some(s) = &mut u.signer {
                // Revoked later than now still counts (committed revocation, fail closed).
                s.status = SubjectStatus::Revoked {
                    at: at("2026-11-07T12:00:00Z"),
                };
            }
        }),
        revoked
    );
    assert_eq!(
        with_use(|u| u.assignments[0].revoked_at = Some(at("2026-11-07T12:00:00Z"))),
        revoked
    );
    // A revoked wildcard assignment containing the scope (X5) counts as a role revocation;
    // an active one holds the permission.
    assert_eq!(
        with_use(|u| {
            u.assignments[0].project_id = "*".into();
            u.assignments[0].revoked_at = Some(at("2026-11-07T12:00:00Z"));
        }),
        revoked
    );
    assert!(with_use(|u| u.assignments[0].project_id = "*".into()).is_ok());
    // Y1 exact boundary on trusted time 11:00: expiring at 11:00 is refused, one
    // second later is accepted.
    let expiring = |issued: &str, expires: &str| {
        let (issued, expires) = (at(issued), at(expires));
        with_use(move |u| {
            u.signoff = stored_at(
                signoff_binding(),
                u.signoff.fields().exclusions.clone(),
                issued,
                expires,
            )
        })
    };
    assert_eq!(
        expiring("2026-11-07T10:55:00Z", "2026-11-07T11:00:00Z"),
        signoff_code(ApprovalCode::SignoffExpired)
    );
    assert!(expiring("2026-11-07T10:55:00Z", "2026-11-07T11:00:01Z").is_ok());
    // A stored (pre-cap) lifetime above 600 s fails closed at use, even when unexpired;
    // exactly 600 s is within the maximum.
    let refused = expiring("2026-11-07T10:50:00Z", "2026-11-07T11:20:00Z");
    assert_eq!(refused, signoff_code(ApprovalCode::SignoffLifetimeExceeded));
    assert!(expiring("2026-11-07T10:50:01Z", "2026-11-07T11:00:02Z").is_err());
    assert!(expiring("2026-11-07T10:51:00Z", "2026-11-07T11:01:00Z").is_ok());
    assert_eq!(
        with_use(|u| u.assignments[0].project_id = "p2".into()),
        signoff_code(ApprovalCode::PermissionMissing)
    );
    assert_eq!(
        with_use(|u| u.assignments[0].permission = Permission::Approver),
        signoff_code(ApprovalCode::PermissionMissing)
    );
    assert_eq!(
        with_use(|u| {
            if let Some(s) = &mut u.signer {
                s.kind = PrincipalKind::Service;
            }
        }),
        signoff_code(ApprovalCode::ServiceNotPermitted)
    );
    let separation = signoff_code(ApprovalCode::SeparationViolated);
    assert_eq!(with_use(|u| u.enrollment = None), separation);
    // Stored exclusions, exclusions re-read now, and this promotion's parties.
    assert_eq!(
        with_use(|u| {
            let ex = SignoffExclusions {
                artifact_changers: vec![bob()],
                ..SignoffExclusions::default()
            };
            u.signoff = stored(signoff_binding(), ex);
        }),
        separation
    );
    assert_eq!(
        with_use(|u| u.exclusions.policy_changers = vec![SubjectId::new("realm:ops", "BOB")]),
        separation
    );
    let v = verify_record(&v2_record(), &keys()).unwrap();
    assert_eq!(
        full_check(&v, &[signoff_use()], &[SubjectId::new("adapter", "bob")]),
        separation
    );
    assert_eq!(
        with_use(|u| u.consumed = true),
        signoff_code(ApprovalCode::SignoffConsumed)
    );
    // Revocation is decided before expiry, expiry before role, role before
    // separation, separation before consumption.
    assert_eq!(
        with_use(|u| {
            u.consumed = true;
            u.enrollment = None;
            u.signoff_revoked = true;
        }),
        revoked
    );
}
