//! D-053 disposable workflow and cross-purpose controls.

use std::sync::atomic::AtomicI64;

use writ_broker::acceptance::RECORD_CONTRACT_V2;
use writ_broker::approval::{ApprovalCode, ChallengeRefusal, RevocationTarget};
use writ_broker::approval_store::{
    ApprovalLedger, CeremonyBegin, CeremonyFinish, CeremonyOp, CeremonyOutcome, CeremonyResult,
    IssuedChallenge, NewEnrollment, NewRole,
};
use writ_broker::clock::ObservedTime;
use writ_broker::human::{Permission, PrincipalKind, SubjectId};
use writ_broker::ingress::VerifiedRequest;
use writ_broker::waiver::{
    WaiverBinding, WaiverPolicyGuard, WaiverPolicyIdentity, WaiverPolicyView, WaiverRequest,
    WAIVER_PURPOSE,
};
use writ_broker::waiver_lifecycle::WaiverLedger;
use writ_broker::workflow::WaiverInvocation;
use writ_broker::writ_kernel::acceptance::AcceptanceRefusal;
use writ_broker::writ_kernel::contracts::{AcceptanceRequirements, StateRevision};

use super::super::grant_tests::{file_write, label, ts as rfc3339};
use super::super::m4_support::{
    enroll_op, ensure_approver, issue, registered, support_approver, support_root,
};
use super::*;

const APPROVER_RESPONSE: &[u8] = b"ok:cred-waiver-reviewer";

struct Policy {
    revision: AtomicI64,
    unavailable: AtomicBool,
    max_seconds: u32,
    expires_at: Timestamp,
    requirements: AcceptanceRequirements,
}

impl WaiverPolicyGuard for Policy {
    fn current(&self, now: ObservedTime) -> Result<WaiverPolicyView, Refusal> {
        if self.unavailable.load(Ordering::SeqCst) {
            return Err(Refusal::AcceptancePolicyUnavailable);
        }
        WaiverPolicyView::new(
            WaiverPolicyIdentity {
                tenant_id: T.into(),
                project_id: P.into(),
                authorization_digest: sha('a'),
                acceptance_digest: sha('c'),
                activation_revision: self.revision.load(Ordering::SeqCst),
                publication_digest: sha('f'),
            },
            BTreeMap::from([("docs".into(), self.max_seconds)]),
            vec![SubjectId::new("fixture-policy-authority", "publisher")],
            now.at(),
            self.expires_at,
            self.requirements.clone(),
        )
    }
}

fn policy() -> Policy {
    Policy {
        revision: AtomicI64::new(1),
        unavailable: AtomicBool::new(false),
        max_seconds: 600,
        expires_at: ts("2026-11-07T12:00:00Z"),
        requirements: AcceptanceRequirements::from_value(&requirements("3")).unwrap(),
    }
}

fn reviewer() -> SubjectId {
    SubjectId::new("writ-local", "waiver-reviewer")
}

fn observed(w: &W) -> ObservedTime {
    w.broker().grants().observe().unwrap()
}

fn ordinary_ceremony(w: &W, op: &CeremonyOp, key: &str) -> CeremonyResult {
    let root = support_root();
    let challenge = w
        .ledger
        .begin_ceremony(&CeremonyBegin {
            op,
            subject: &root,
            idempotency_key: key,
            now: observed(w),
        })
        .unwrap()
        .unwrap();
    w.ledger
        .finish_ceremony(&CeremonyFinish {
            op,
            challenge_id: &challenge.challenge_id,
            response: format!("ok:cred-{}", root.subject).as_bytes(),
            idempotency_key: key,
            now: observed(w),
        })
        .unwrap()
        .unwrap()
        .result
}

fn enroll(
    w: &W,
    subject: SubjectId,
    kind: PrincipalKind,
    project: &str,
    linked_identities: Vec<SubjectId>,
) {
    let enrollment = registered(
        &w.ledger,
        &|| observed(w),
        &support_root(),
        NewEnrollment {
            subject: subject.clone(),
            kind,
            credential: None,
            linked_identities,
            roles: [Permission::WaiverAuthority, Permission::Approver]
                .into_iter()
                .map(|permission| NewRole {
                    permission,
                    tenant_id: T.into(),
                    project_id: project.into(),
                })
                .collect(),
        },
    );
    let _ = ordinary_ceremony(
        w,
        &enroll_op(enrollment),
        &format!("d053-enroll-{}", subject.subject),
    );
}

fn authorization() -> LoadedBundle {
    let base = parse(include_str!(
        "../../../../m1/fixtures/transition/m1-transition.bundle.json"
    ));
    let mut statements = base.get("statements").unwrap().as_arr().unwrap().to_vec();
    statements.push(parse(
        r#"{"statement_id":"approve-fixture-write","effect":"require_approval","action_kinds":["file.write"],"reason_code":"FIXTURE_WRITE_REQUIRES_APPROVAL"}"#,
    ));
    load_bundle(&set(&base, &["statements"], Value::Arr(statements))).unwrap()
}

fn scenario(name: &str) -> W {
    let dir = std::env::temp_dir().join(format!("writ-d053-{}-{name}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let ledger = SqliteLedger::create_waiver_lab(&dir.join("ledger.db")).unwrap();
    let effects = SqliteEffectStore::create(&dir.join("effects.db"), ledger.ledger_id()).unwrap();
    let w = W::with_stores(dir, ledger, effects);
    w.active.activate(PolicyState::loaded(authorization()));
    let w = w.prepared();
    ensure_approver(&w.ledger, observed(&w)).unwrap();
    enroll(&w, reviewer(), PrincipalKind::Human, P, vec![]);
    w
}

fn reopen(dir: PathBuf) -> W {
    let path = dir.join("ledger.db");
    assert!(SqliteLedger::open(&path).is_err());
    let ledger = SqliteLedger::open_waiver_lab(&path).unwrap();
    let effects = SqliteEffectStore::open(&dir.join("effects.db"), ledger.ledger_id()).unwrap();
    W::with_stores(dir, ledger, effects)
}

fn waiver_request() -> WaiverRequest {
    WaiverRequest {
        workflow: wf(),
        from_state: "verifying".into(),
        to_state: "accepted".into(),
        expected_revision: 1,
        criterion_id: "docs".into(),
        reason: "Temporary exception for the disposable documentation check".into(),
    }
}

fn invocation<'a>(
    verified: &'a VerifiedRequest,
    request: &'a WaiverRequest,
) -> WaiverInvocation<'a> {
    WaiverInvocation { verified, request }
}

fn waiver_broker<'a>(
    w: &'a W,
    policy: &'a dyn WaiverPolicyGuard,
) -> Broker<'a, SqliteLedger, SqliteEffectStore> {
    w.broker().with_waiver_authority(policy, &w.snapshots)
}

fn waiver_id(binding: &WaiverBinding) -> String {
    binding.fingerprint().unwrap().as_str().to_string()
}

fn request_and_begin(
    w: &W,
    policy: &dyn WaiverPolicyGuard,
    invocation: &WaiverInvocation<'_>,
    key: &str,
) -> (WaiverBinding, IssuedChallenge) {
    let binding = waiver_broker(w, policy)
        .request_waiver(invocation)
        .unwrap()
        .unwrap();
    let challenge = waiver_broker(w, policy)
        .begin_waiver(invocation, &waiver_id(&binding), &reviewer(), key)
        .unwrap()
        .unwrap();
    assert_eq!(challenge.context, binding);
    assert!(challenge.issued.expires_at < challenge.context.expires_at);
    assert_eq!(
        challenge.issued.expires_at,
        observed(w).at().plus_seconds(120)
    );
    (binding, challenge.issued)
}

fn finish(
    w: &W,
    policy: &dyn WaiverPolicyGuard,
    invocation: &WaiverInvocation<'_>,
    id: &str,
    challenge: &IssuedChallenge,
    key: &str,
) -> Result<Result<CeremonyOutcome, Refusal>, TxFailure> {
    waiver_broker(w, policy).finish_waiver(
        invocation,
        id,
        &challenge.challenge_id,
        APPROVER_RESPONSE,
        key,
    )
}

fn record_waiver(
    w: &W,
    policy: &dyn WaiverPolicyGuard,
    invocation: &WaiverInvocation<'_>,
) -> (WaiverBinding, String) {
    let (binding, challenge) = request_and_begin(w, policy, invocation, "record");
    let id = waiver_id(&binding);
    let recorded = finish(w, policy, invocation, &id, &challenge, "record")
        .unwrap()
        .unwrap();
    assert_eq!(
        recorded.result,
        CeremonyResult::Waived {
            waiver_id: id.clone()
        }
    );
    assert!(!recorded.replayed);
    (binding, id)
}

fn patch_record(doc: &Value, index: usize, fields: &[(&str, &str)]) -> Value {
    let r = doc.get("record").unwrap();
    let mut criteria = r.get("criteria").unwrap().as_arr().unwrap().to_vec();
    for &(key, value) in fields {
        criteria[index] = set(&criteria[index], &[key], json::s(value));
    }
    let r = set(r, &["criteria"], Value::Arr(criteria));
    let digest = json::digest(&r.without("record_digest")).unwrap();
    let r = set(&r, &["record_digest"], json::s(digest.as_str()));
    set(doc, &["record"], r)
}

fn approved_count(w: &W) -> i64 {
    w.one("SELECT count(*) FROM approval_events WHERE kind = 'waived'")
}

#[test]
fn disposable_human_waiver_keeps_mandatory_criterion_incomplete() {
    let w = scenario("mandatory");
    let request = waiver_request();
    let policy = policy();
    let verified = verify(&accept("d053-mandatory", 1));
    let invocation = invocation(&verified, &request);
    let (binding, id) = record_waiver(&w, &policy, &invocation);
    let receipt = w.ledger.waiver_receipt(&id).unwrap().unwrap();
    assert_eq!(receipt.binding, binding);
    assert_eq!(receipt.approver, reviewer());
    assert_eq!(
        binding.value().get("purpose").and_then(Value::as_str),
        Some(WAIVER_PURPOSE)
    );
    assert_eq!(binding.expires_at, ts("2026-11-07T11:10:00Z"));
    let validated = waiver_broker(&w, &policy)
        .validate_waiver(&invocation, &id)
        .unwrap()
        .unwrap();
    assert_eq!(validated, binding);
    assert_eq!(approved_count(&w), 1);

    let r = record("acc-1", 1, &sha('b'), "att-1");
    let criterion_index = r
        .get("record")
        .unwrap()
        .get("criteria")
        .unwrap()
        .as_arr()
        .unwrap()
        .iter()
        .position(|criterion| {
            criterion.get("criterion_id").and_then(Value::as_str)
                == Some(validated.request.criterion_id.as_str())
        })
        .unwrap();
    let waived = patch_record(&r, criterion_index, &[("status", "waived")]);
    w.docs.put("acc-1", sign(&waived, &RECORD_KEY));
    assert_eq!(
        w.promote(&accept("d053-mandatory", 1)),
        Err(acceptance(AcceptanceCode::Kernel(
            AcceptanceRefusal::Incomplete
        )))
    );
    unchanged(&w);
    assert_eq!(w.one("SELECT count(*) FROM consumed_signoffs"), 0);
    assert_eq!(w.one("SELECT count(*) FROM grant_uses"), 0);

    w.docs.put("acc-1", r);
    assert_eq!(w.promote(&accept("d053-genuine", 1)), Ok(committed(2)));
    assert_eq!(w.one("SELECT count(*) FROM consumed_records"), 1);
    assert_eq!(w.one("SELECT count(*) FROM consumed_attestations"), 1);
    assert_eq!(w.ledger.waiver_receipt(&id).unwrap().unwrap(), receipt);
    assert_eq!(
        waiver_broker(&w, &policy).validate_waiver(&invocation, &id),
        Ok(Err(Refusal::RevisionConflict(
            ConflictStage::UseTimeDecision
        )))
    );
}

#[test]
fn waiver_uses_server_expiry_and_fails_closed_after_policy_or_context_changes() {
    let w = scenario("bindings");
    let mut policy = policy();
    policy.max_seconds = 300;
    policy.expires_at = ts("2026-11-07T11:04:00Z");
    let request = waiver_request();
    let req = set(
        &accept("d053-bindings", 1),
        &["deadline_at"],
        json::s("2026-11-07T11:03:00Z"),
    );
    let verified = verify(&req);
    let context = invocation(&verified, &request);
    let (binding, id) = record_waiver(&w, &policy, &context);
    let old_receipt = w.ledger.waiver_receipt(&id).unwrap().unwrap();
    assert_eq!(binding.expires_at, ts("2026-11-07T11:03:00Z"));

    let forged_id = sha('4');
    assert_eq!(
        waiver_broker(&w, &policy).validate_waiver(&context, &forged_id),
        Ok(Err(Refusal::Approval(ApprovalCode::WaiverUnknown)))
    );

    let mut other_reason = request.clone();
    other_reason.reason.push_str(" updated");
    assert!(waiver_broker(&w, &policy)
        .validate_waiver(&invocation(&verified, &other_reason), &id,)
        .unwrap()
        .is_err());

    let mut wrong_scope = request.clone();
    wrong_scope.workflow.project_id = "other-project".into();
    assert_eq!(
        waiver_broker(&w, &policy).validate_waiver(&invocation(&verified, &wrong_scope), &id),
        Ok(Err(Refusal::RequestScopeMismatch))
    );

    let other_requester = verify(&set(&req, &["principal", "id"], json::s("other-requester")));
    assert!(waiver_broker(&w, &policy)
        .validate_waiver(&invocation(&other_requester, &request), &id,)
        .unwrap()
        .is_err());

    let delegated = verify(&set(&req, &["parent_session_id"], json::s("parent")));
    assert_eq!(
        waiver_broker(&w, &policy).validate_waiver(&invocation(&delegated, &request), &id),
        Ok(Err(Refusal::RequestScopeMismatch))
    );

    let changed_requirements = requirements("4");
    w.admin(AdminRequest::ActivateAcceptancePolicy {
        tenant_id: T.into(),
        project_id: P.into(),
        requirements: changed_requirements,
        effect_prerequisites: Vec::new(),
    })
    .unwrap();
    assert_eq!(
        waiver_broker(&w, &policy).validate_waiver(&context, &id),
        Ok(Err(Refusal::PolicyDependencyMismatch))
    );
    w.admin(AdminRequest::ActivateAcceptancePolicy {
        tenant_id: T.into(),
        project_id: P.into(),
        requirements: requirements("3"),
        effect_prerequisites: Vec::new(),
    })
    .unwrap();

    policy.revision.store(2, Ordering::SeqCst);
    assert_eq!(
        waiver_broker(&w, &policy).validate_waiver(&context, &id),
        Ok(Err(Refusal::PolicyDependencyMismatch))
    );
    policy.revision.store(1, Ordering::SeqCst);
    policy.unavailable.store(true, Ordering::SeqCst);
    assert_eq!(
        waiver_broker(&w, &policy).validate_waiver(&context, &id),
        Ok(Err(Refusal::AcceptancePolicyUnavailable))
    );
    policy.unavailable.store(false, Ordering::SeqCst);

    w.snapshots
        .content
        .lock()
        .unwrap()
        .insert("snap-1".into(), sha('e'));
    assert_eq!(
        waiver_broker(&w, &policy).validate_waiver(&context, &id),
        Ok(Err(acceptance(AcceptanceCode::ArtifactIntegrityViolation)))
    );
    w.snapshots
        .content
        .lock()
        .unwrap()
        .insert("snap-1".into(), sha('b'));

    w.clock.set(binding.expires_at);
    assert!(waiver_broker(&w, &policy)
        .validate_waiver(&context, &id)
        .unwrap()
        .is_err());
    unchanged(&w);
    assert_eq!(w.ledger.waiver_receipt(&id).unwrap().unwrap(), old_receipt);
}

#[test]
fn waiver_rejects_wrong_authority_wrong_purpose_forgery_and_self_approval() {
    let w = scenario("authority");
    let policy = policy();
    let req = accept("d053-authority", 1);
    let verified = verify(&req);
    let request = waiver_request();
    let context = invocation(&verified, &request);
    assert_eq!(
        w.broker().request_waiver(&context),
        Ok(Err(Refusal::AcceptancePolicyUnavailable))
    );

    let mut missing = request.clone();
    missing.criterion_id = "build".into();
    assert_eq!(
        waiver_broker(&w, &policy).request_waiver(&invocation(&verified, &missing)),
        Ok(Err(Refusal::Approval(ApprovalCode::WaiverIneligible)))
    );
    missing.criterion_id = "undeclared".into();
    assert_eq!(
        waiver_broker(&w, &policy).request_waiver(&invocation(&verified, &missing)),
        Ok(Err(Refusal::Approval(ApprovalCode::WaiverIneligible)))
    );

    let self_subject = SubjectId::new("writ-local", "requester-linked-human");
    enroll(
        &w,
        self_subject.clone(),
        PrincipalKind::Human,
        P,
        vec![SubjectId::new(
            "writ-host-adapter:fixture",
            "agent:session-1",
        )],
    );
    let service = SubjectId::new("writ-local", "waiver-service");
    // A human-only role may be refused by enrollment. A successfully enrolled
    // service has no human credential and still cannot start the ceremony.
    let service_enrollment = NewEnrollment {
        subject: service.clone(),
        kind: PrincipalKind::Service,
        credential: None,
        linked_identities: vec![],
        roles: vec![],
    };
    let _ = ordinary_ceremony(&w, &enroll_op(service_enrollment), "service-enrollment");
    let wrong_scope = SubjectId::new("writ-local", "other-project-reviewer");
    enroll(
        &w,
        wrong_scope.clone(),
        PrincipalKind::Human,
        "other-project",
        vec![],
    );

    let binding = waiver_broker(&w, &policy)
        .request_waiver(&context)
        .unwrap()
        .unwrap();
    let id = waiver_id(&binding);
    assert_eq!(
        waiver_broker(&w, &policy).begin_waiver(&context, &id, &self_subject, "self"),
        Ok(Err(Refusal::Approval(ApprovalCode::SeparationViolated)))
    );
    for (subject, key) in [
        (service, "service"),
        (wrong_scope, "wrong-scope"),
        (support_approver(), "ordinary-approver"),
    ] {
        assert!(waiver_broker(&w, &policy)
            .begin_waiver(&context, &id, &subject, key)
            .unwrap()
            .is_err());
    }
    let raw = CeremonyOp::Waive {
        waiver_id: id.clone(),
    };
    assert!(w
        .ledger
        .begin_ceremony(&CeremonyBegin {
            op: &raw,
            subject: &reviewer(),
            idempotency_key: "unscoped",
            now: observed(&w),
        })
        .unwrap()
        .is_err());

    let ordinary = verify(&file_write("d053-purpose-challenge"));
    assert_eq!(
        w.broker()
            .execute(&ordinary, StateRevision::new(1).unwrap(), None)
            .result,
        Err(Refusal::ApprovalRequired)
    );
    let approve = CeremonyOp::Approve {
        request_id: w.text("SELECT request_id FROM approval_requests WHERE status = 'pending'"),
    };
    let challenge = waiver_broker(&w, &policy)
        .begin_waiver(&context, &id, &reviewer(), "wrong-purpose")
        .unwrap()
        .unwrap();
    assert_eq!(
        w.ledger.finish_ceremony(&CeremonyFinish {
            op: &approve,
            challenge_id: &challenge.issued.challenge_id,
            response: APPROVER_RESPONSE,
            idempotency_key: "wrong-purpose",
            now: observed(&w),
        }),
        Ok(Err(Refusal::Approval(ApprovalCode::ChallengeInvalid(
            ChallengeRefusal::OperationMismatch
        ))))
    );

    let ordinary = w
        .ledger
        .begin_ceremony(&CeremonyBegin {
            op: &approve,
            subject: &reviewer(),
            idempotency_key: "ordinary-purpose",
            now: observed(&w),
        })
        .unwrap()
        .unwrap();
    assert_eq!(
        finish(&w, &policy, &context, &id, &ordinary, "ordinary-purpose"),
        Ok(Err(Refusal::Approval(ApprovalCode::ChallengeInvalid(
            ChallengeRefusal::OperationMismatch
        ))))
    );

    let unavailable = waiver_broker(&w, &policy)
        .begin_waiver(&context, &id, &reviewer(), "unavailable")
        .unwrap()
        .unwrap();
    assert_eq!(
        waiver_broker(&w, &policy).finish_waiver(
            &context,
            &id,
            &unavailable.issued.challenge_id,
            b"down:cred-waiver-reviewer",
            "unavailable"
        ),
        Ok(Err(Refusal::Approval(ApprovalCode::AssertionInvalid(
            writ_broker::human::VerifierError::Unavailable
        ))))
    );
    let forged = waiver_broker(&w, &policy)
        .begin_waiver(&context, &id, &reviewer(), "forged")
        .unwrap()
        .unwrap();
    assert!(matches!(
        waiver_broker(&w, &policy).finish_waiver(
            &context,
            &id,
            &forged.issued.challenge_id,
            b"forged",
            "forged"
        ),
        Ok(Err(Refusal::Approval(ApprovalCode::AssertionInvalid(_))))
    ));
    assert!(w.ledger.waiver_receipt(&id).unwrap().is_none());

    let broker = w.broker();
    let grant = issue(
        broker.grants(),
        &file_write("d053-ordinary"),
        "d053-ordinary",
    )
    .unwrap();
    assert!(waiver_broker(&w, &policy)
        .validate_waiver(&context, grant.label.as_str())
        .unwrap()
        .is_err());
    assert_eq!(approved_count(&w), 0);
    unchanged(&w);
}

#[test]
fn waiver_revocation_preserves_the_immutable_receipt() {
    let w = scenario("revocation");
    let policy = policy();
    let request = waiver_request();
    let verified = verify(&accept("d053-revoke", 1));
    let context = invocation(&verified, &request);
    let (_, id) = record_waiver(&w, &policy, &context);
    let receipt = w.ledger.waiver_receipt(&id).unwrap().unwrap();
    assert!(matches!(
        ordinary_ceremony(
            &w,
            &CeremonyOp::Revoke {
                target: RevocationTarget::Waiver(id.clone()),
                reason: "Fixture waiver revoked".into(),
            },
            "revoke-waiver"
        ),
        CeremonyResult::Revoked { .. }
    ));
    assert_eq!(
        waiver_broker(&w, &policy).validate_waiver(&context, &id),
        Ok(Err(Refusal::Approval(ApprovalCode::WaiverRevoked)))
    );
    assert_eq!(w.ledger.waiver_receipt(&id).unwrap().unwrap(), receipt);
    unchanged(&w);
}

#[test]
fn waiver_is_not_an_effect_grant_a_signoff_or_publisher_authority() {
    use super::evaluator_scenario_tests::{publication, submit_with_credential, Publishers};

    let w = scenario("cross-purpose");
    let policy_requirements = requirements("3");
    let mut criteria = policy_requirements
        .get("criteria")
        .unwrap()
        .as_arr()
        .unwrap()
        .to_vec();
    criteria[1] = set(&criteria[1], &["required_class"], json::s("human"));
    let policy_requirements = set(&policy_requirements, &["criteria"], Value::Arr(criteria));
    w.admin(AdminRequest::ActivateAcceptancePolicy {
        tenant_id: T.into(),
        project_id: P.into(),
        requirements: policy_requirements.clone(),
        effect_prerequisites: Vec::new(),
    })
    .unwrap();
    let human_entry = set(
        &set(
            &registry_entry("doc-check", 13),
            &["class"],
            json::s("human"),
        ),
        &["trust"],
        json::s("H"),
    );
    w.admin(AdminRequest::RegisterEvaluator { entry: human_entry })
        .unwrap();

    let mut policy = policy();
    policy.requirements = AcceptanceRequirements::from_value(&policy_requirements).unwrap();
    let request = waiver_request();
    let verified = verify(&accept("d053-cross-purpose", 1));
    let context = invocation(&verified, &request);
    let (_, id) = record_waiver(&w, &policy, &context);
    assert!(waiver_broker(&w, &policy)
        .validate_waiver(&context, &id)
        .unwrap()
        .is_ok());
    let broker = w.broker();
    let write = verify(&file_write("waiver-as-effect-grant"));
    let revision = StateRevision::new(1).unwrap();
    assert_eq!(
        broker.execute(&write, revision, None).result,
        Err(Refusal::ApprovalRequired)
    );
    assert_eq!(
        broker.execute(&write, revision, Some(&label(&id))).result,
        Err(Refusal::GrantUnknown)
    );
    assert_eq!(w.one("SELECT count(*) FROM receipts"), 0);

    let intent = publication("doc-check", 0);
    let publishers = Publishers::default();
    publishers.approve_fixture(&intent);
    let prior_publications =
        w.one("SELECT count(*) FROM admin_events WHERE operation = 'register_evaluator'");
    assert_eq!(
        submit_with_credential(&w, &publishers, &intent, id.as_bytes()),
        Err(Refusal::OperatorUnauthenticated)
    );
    assert_eq!(
        w.one("SELECT count(*) FROM admin_events WHERE operation = 'register_evaluator'"),
        prior_publications
    );

    let record = record("acc-1", 1, &sha('b'), "att-1");
    let envelope = patch_record(
        &record,
        1,
        &[("required_class", "human"), ("evaluator_trust", "H")],
    );
    let envelope = set(
        &envelope,
        &["contract_version"],
        json::s(RECORD_CONTRACT_V2),
    );
    let mut evaluations = envelope
        .get("evaluations")
        .unwrap()
        .as_arr()
        .unwrap()
        .to_vec();
    evaluations[0] = set(&evaluations[0], &["signoff_id"], Value::Null);
    evaluations[1] = set(&evaluations[1], &["registry_revision"], Value::Int(13));
    // The full waiver digest is outside the signoff ID grammar. Its hex
    // payload fits the grammar, but still resolves to no trusted signoff.
    let signoff_id = id.strip_prefix("sha256:").unwrap();
    evaluations[1] = set(&evaluations[1], &["signoff_id"], json::s(signoff_id));
    let envelope = set(&envelope, &["evaluations"], Value::Arr(evaluations));
    w.docs.put("acc-1", sign(&envelope, &RECORD_KEY));
    assert!(w.ledger.signoff(&id).unwrap().is_none());
    assert_eq!(
        w.promote(&accept("waiver-as-signoff", 1)),
        Err(Refusal::Approval(ApprovalCode::SignoffUnknown))
    );
    unchanged(&w);
    assert_eq!(w.one("SELECT count(*) FROM consumed_signoffs"), 0);
    assert_eq!(w.one("SELECT count(*) FROM grant_uses"), 0);

    let denied = transition(
        "waiver-forbidden-release",
        "verifying",
        "released",
        1,
        Some(("acc-1", "snap-1", &sha('b'))),
    );
    assert!(matches!(
        w.broker().promote(&w.trust(), &verify(&denied), Some(&label(&id))).result,
        Err(Refusal::Decision { reason, .. }) if reason.as_str() == "AGENT_RELEASE_FORBIDDEN"
    ));
    unchanged(&w);
    assert_eq!(approved_count(&w), 1);
}

#[test]
fn competing_waiver_ceremonies_commit_once_and_same_challenge_recovers() {
    let w = scenario("concurrency");
    let policy = policy();
    let verified = verify(&accept("concurrent-waiver", 1));
    let request = waiver_request();
    let context = invocation(&verified, &request);
    let (binding, first) = request_and_begin(&w, &policy, &context, "first");
    let (duplicate, second) = request_and_begin(&w, &policy, &context, "second");
    assert_eq!(binding, duplicate);
    assert_ne!(first.challenge_id, second.challenge_id);
    let id = waiver_id(&binding);
    let race = Barrier::new(2);
    let outcomes = thread::scope(|scope| {
        let one = scope.spawn(|| {
            race.wait();
            let context = invocation(&verified, &request);
            finish(&w, &policy, &context, &id, &first, "first").unwrap()
        });
        let two = scope.spawn(|| {
            race.wait();
            let context = invocation(&verified, &request);
            finish(&w, &policy, &context, &id, &second, "second").unwrap()
        });
        [one.join().unwrap(), two.join().unwrap()]
    });
    assert_eq!(outcomes.iter().filter(|outcome| outcome.is_ok()).count(), 1);
    let (challenge, key) = if outcomes[0].is_ok() {
        (&first, "first")
    } else {
        (&second, "second")
    };
    let recovered = finish(&w, &policy, &context, &id, challenge, key)
        .unwrap()
        .unwrap();
    assert!(recovered.replayed);
    assert_eq!(w.one("SELECT count(*) FROM waiver_requests"), 1);
    assert_eq!(w.one("SELECT count(*) FROM waivers"), 1);
    assert_eq!(w.text("SELECT result FROM waivers"), "waived");
    assert_eq!(approved_count(&w), 1);
    unchanged(&w);
}

#[test]
fn waiver_audit_and_storage_faults_roll_back_before_reopen_and_recovery() {
    let w = scenario("faults");
    let selection = policy();
    let verified = verify(&accept("faulted-waiver", 1));
    let request = waiver_request();
    let context = invocation(&verified, &request);
    let (binding, challenge) = request_and_begin(&w, &selection, &context, "record");
    let id = waiver_id(&binding);
    w.ledger
        .db()
        .conn
        .execute_batch(
            "CREATE TEMP TRIGGER refuse_waiver_audit BEFORE INSERT ON approval_events
         WHEN NEW.kind = 'waived' BEGIN SELECT RAISE(ABORT, 'fixture failure'); END;",
        )
        .unwrap();
    let failed = finish(&w, &selection, &context, &id, &challenge, "record").unwrap_err();
    assert!(!failed.outcome_unknown);
    assert!(w.ledger.waiver_receipt(&id).unwrap().is_none());
    assert_eq!(approved_count(&w), 0);
    assert_eq!(w.one("SELECT count(*) FROM waivers"), 0);
    w.ledger
        .db()
        .conn
        .execute_batch("DROP TRIGGER refuse_waiver_audit")
        .unwrap();

    w.ledger.inject(
        "ceremony",
        fault::Stage::Commit,
        rusqlite::ffi::SQLITE_IOERR,
    );
    let uncertain = finish(&w, &selection, &context, &id, &challenge, "record").unwrap_err();
    assert!(uncertain.outcome_unknown);
    let dir = w.dir.clone();
    drop(w);

    let w = reopen(dir.clone());
    w.active.activate(PolicyState::loaded(authorization()));
    ensure_approver(&w.ledger, observed(&w)).unwrap();
    assert!(w.ledger.waiver_receipt(&id).unwrap().is_none());
    let selection = policy();
    let context = invocation(&verified, &request);
    assert_eq!(
        waiver_broker(&w, &selection).request_waiver(&context),
        Ok(Ok(binding.clone()))
    );
    assert!(
        !finish(&w, &selection, &context, &id, &challenge, "record")
            .unwrap()
            .unwrap()
            .replayed
    );
    let receipt = w.ledger.waiver_receipt(&id).unwrap().unwrap();
    assert_eq!(approved_count(&w), 1);
    drop(w);

    let w = reopen(dir);
    w.active.activate(PolicyState::loaded(authorization()));
    ensure_approver(&w.ledger, observed(&w)).unwrap();
    let selection = policy();
    let context = invocation(&verified, &request);
    assert_eq!(w.ledger.waiver_receipt(&id), Ok(Some(receipt)));
    assert!(
        finish(&w, &selection, &context, &id, &challenge, "record")
            .unwrap()
            .unwrap()
            .replayed
    );
    assert_eq!(approved_count(&w), 1);
    unchanged(&w);
}

#[test]
fn fractional_trusted_expiry_round_trips_and_refuses_at_the_bound_instant() {
    let w = scenario("fractional-expiry");
    let received_at = rfc3339("2026-11-07T11:00:00.037Z");
    w.clock.set(received_at);
    let selection = policy();
    let request = waiver_request();
    let verified = verify(&accept("fractional-expiry", 1));
    let context = invocation(&verified, &request);
    let (binding, id) = record_waiver(&w, &selection, &context);
    assert_eq!(binding.expires_at, rfc3339("2026-11-07T11:10:00.037Z"));
    assert_eq!(
        binding.value().get("expires_at").and_then(Value::as_str),
        Some("2026-11-07T11:10:00.037Z")
    );
    let receipt = w.ledger.waiver_receipt(&id).unwrap().unwrap();
    assert_eq!(receipt.recorded_at, received_at);
    let dir = w.dir.clone();
    drop(w);

    let w = reopen(dir);
    w.active.activate(PolicyState::loaded(authorization()));
    w.clock.set(rfc3339("2026-11-07T11:10:00.036999999Z"));
    ensure_approver(&w.ledger, observed(&w)).unwrap();
    let context = invocation(&verified, &request);
    assert_eq!(
        waiver_broker(&w, &selection).validate_waiver(&context, &id),
        Ok(Ok(binding.clone()))
    );
    w.clock.set(binding.expires_at);
    assert_eq!(
        waiver_broker(&w, &selection).validate_waiver(&context, &id),
        Ok(Err(Refusal::Approval(ApprovalCode::WaiverExpired)))
    );
    assert_eq!(w.ledger.waiver_receipt(&id).unwrap().unwrap(), receipt);
    unchanged(&w);
}

#[test]
fn waiver_excludes_the_alias_reached_from_a_trusted_artifact_and_policy_changer() {
    let w = scenario("changer-alias");
    let selection = policy();
    let request = waiver_request();
    let verified = verify(&accept("changer-alias", 1));
    let context = invocation(&verified, &request);
    let changer = SubjectId::new("realm:ops", "ops:alice");
    let bridge = SubjectId::new("writ-local", "changer-bridge");
    let alias = SubjectId::new("writ-local", "changer-alias");
    enroll(&w, alias.clone(), PrincipalKind::Human, P, vec![]);
    enroll(
        &w,
        bridge.clone(),
        PrincipalKind::Human,
        P,
        vec![alias.clone()],
    );
    enroll(&w, changer.clone(), PrincipalKind::Human, P, vec![bridge]);
    let binding = waiver_broker(&w, &selection)
        .request_waiver(&context)
        .unwrap()
        .unwrap();
    assert!(binding.exclusions.artifact_changers.contains(&changer));
    assert!(binding.exclusions.policy_changers.contains(&changer));
    assert!(binding.exclusions.linked_subjects.contains(&alias));
    let id = waiver_id(&binding);
    assert_eq!(
        waiver_broker(&w, &selection).begin_waiver(&context, &id, &alias, "changer-alias"),
        Ok(Err(Refusal::Approval(ApprovalCode::SeparationViolated)))
    );
    assert!(w.ledger.waiver_receipt(&id).unwrap().is_none());
    unchanged(&w);
}
