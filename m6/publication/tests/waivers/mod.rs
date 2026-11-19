//! D-053 signed policy selection composed with scoped M4 waiver recording.

use super::*;
use writ_broker::{
    approval_store::CeremonyResult,
    clock::{observe, FixedClock},
    execute::Broker,
    keys::PermitKey,
    policy::{ActivePolicy, PolicyState},
    waiver::{WaiverPolicyGuard, WaiverRequest},
    waiver_lifecycle::WaiverLedger,
    workflow::{
        AdminOperation, AdminPresentation, AdminRefusal, AdminRequest, AuthorizedOperator,
        SnapshotCheck, SnapshotStore, SourceUnavailable, WaiverInvocation, WorkflowAuthority,
        WorkflowKey,
    },
};
use writ_kernel::{json::Digest, policy::load_bundle};

fn criterion(id: &str, class: &str, origin: &str, seconds: i64) -> Value {
    json::obj([
        ("criterion_id", json::s(id)),
        ("required_class", json::s(class)),
        ("origin", json::s(origin)),
        ("max_waiver_seconds", Value::Int(seconds)),
    ])
}

fn waiver_publication(signer: &FixtureSigner, version: i64, rules: Vec<Value>) -> Publication {
    let mut publication = signer.publication(ALLOW, version);
    let payload = edit(
        &edit(
            &publication.acceptance.payload,
            "version",
            json::s("writ-acceptance-policy/2"),
        ),
        "criteria",
        Value::Arr(rules),
    );
    publication.acceptance = signer.sign_policy(Purpose::Acceptance, payload);
    publication
}

struct Operator;

impl WorkflowAuthority for Operator {
    fn authorize(
        &self,
        presentation: &AdminPresentation<'_>,
        _: AdminOperation,
        tenant: &str,
        project: &str,
    ) -> Result<AuthorizedOperator, AdminRefusal> {
        let scope = scope();
        if tenant != scope.tenant()
            || project != scope.project()
            || presentation.credential != b"fixture-operator-proof"
        {
            return Err(AdminRefusal::Unauthorized);
        }
        Ok(AuthorizedOperator {
            issuer: "lab-operator-realm".into(),
            id: "workflow-operator".into(),
            channel: "lab".into(),
            evidence_ref: "fixture-bootstrap".into(),
        })
    }
}

struct Snapshot(Vec<u8>);

impl SnapshotStore for Snapshot {
    fn check(
        &self,
        tenant: &str,
        project: &str,
        snapshot: &str,
        digest: &str,
    ) -> Result<SnapshotCheck, SourceUnavailable> {
        let scope = scope();
        Ok(
            if tenant != scope.tenant() || project != scope.project() || snapshot != "snapshot" {
                SnapshotCheck::Unknown
            } else if digest == Digest::of_bytes(&self.0).as_str() {
                SnapshotCheck::Matches
            } else {
                SnapshotCheck::Differs
            },
        )
    }
}

fn transition_policy(acceptance_digest: &str) -> ActivePolicy {
    let bundle = json::parse(
        include_str!("../../../../m1/fixtures/transition/m1-transition.bundle.json")
            .replace("fixture-tenant/fixture-project/", "m6-lab/offline-proof/")
            .replace("writ-host-adapter:fixture", "m6-synthetic")
            .as_bytes(),
    )
    .unwrap();
    let protection = edit(
        &edit(
            bundle.get("workflow_protection").unwrap(),
            "acceptance_policy_bundle_sha",
            json::s(acceptance_digest),
        ),
        "protected_transitions",
        Value::Arr(vec![]),
    );
    ActivePolicy::new(PolicyState::loaded(
        load_bundle(&edit(&bundle, "workflow_protection", protection)).unwrap(),
    ))
}

fn transition(digest: &str) -> writ_broker::ingress::VerifiedRequest {
    seal_request(&json::parse(format!(
        r#"{{"contract_version":"2.0","request_id":"req-waiver","trace_id":"trace-waiver",
        "tenant_id":"m6-lab","project_id":"offline-proof",
        "principal":{{"id":"fixture-requester","verified_by":"m6-synthetic",
        "authn_method":"os_peer_credential","roles":["agent"]}},
        "session_id":"waiver-session","agent_id":null,"parent_session_id":null,
        "idempotency_key":"waiver","expected_state_revision":0,
        "policy_bundle_sha":null,"deadline_at":"2026-11-19T12:04:00Z",
        "action":{{"kind":"workflow.transition","resource":{{
        "uri":"writ-workflow://workflow","canonical_resource_id":"workflow:m6-lab/offline-proof/workflow"}},
        "action_digest":"{digest}","attributes":{{"from_state":"verifying","to_state":"accepted",
        "acceptance_record_id":"future-record","artifact_snapshot_id":"snapshot","artifact_digest":"{digest}"}},
        "requested_capabilities":[]}}}}"#
    ).as_bytes()).unwrap())
}

#[test]
fn waiver_signed_selection_records_scoped_human_approval_and_enforces_current_policy() {
    let dir = Disposable::create().unwrap();
    let (authority, signer) = FixtureSigner::pair();
    let registry = Registry::create(
        &dir.path().join("registry.sqlite"),
        scope(),
        authority.clone(),
    )
    .unwrap();
    let rules = || {
        vec![
            criterion("review", "human", "invariant", 0),
            criterion("waiver-review", "mechanical", "project", 300),
        ]
    };
    let original = waiver_publication(&signer, 1, rules());
    publish(&registry, &authority, &original, 0, 1, "waiver-policy");

    let ledger_path = dir.path().join("waiver.sqlite");
    let (ledger, approver) = grants::waiver_ledger(&ledger_path);
    let effects =
        SqliteEffectStore::create(&dir.path().join("effects.sqlite"), ledger.ledger_id()).unwrap();
    let snapshot = Snapshot(b"immutable synthetic artifact".to_vec());
    let artifact = Digest::of_bytes(&snapshot.0);
    let verified = transition(artifact.as_str());
    let clock = FixedClock::new(now());
    let permit_key = PermitKey::from_bytes(vec![0x42; 32]).unwrap();
    let key = WorkflowKey {
        tenant_id: "m6-lab".into(),
        project_id: "offline-proof".into(),
        workflow_id: "workflow".into(),
    };
    let reason = WaiverRequest {
        workflow: key.clone(),
        from_state: "verifying".into(),
        to_state: "accepted".into(),
        expected_revision: 0,
        criterion_id: "waiver-review".into(),
        reason: "Synthetic criterion review".into(),
    };
    let active = transition_policy(json::digest(&original.acceptance.payload).unwrap().as_str());
    let id = registry
        .with_selection(|selection| {
            let broker = Broker::new(&active, &clock, &ledger, &effects, &permit_key)
                .with_waiver_authority(&selection, &snapshot);
            let view = selection
                .current(broker.grants().observe().unwrap())
                .unwrap();
            assert_eq!(view.permits("waiver-review"), Some(300));
            assert_eq!(view.permits("review"), None);
            let presentation = AdminPresentation {
                operator: "workflow-operator",
                credential: b"fixture-operator-proof",
                channel: "lab",
            };
            let mut requirements = selection
                .publication()
                .acceptance
                .payload
                .as_obj()
                .unwrap()
                .clone();
            requirements.remove("version");
            requirements.remove("policy_id");
            requirements.remove("policy_version");
            requirements.insert("acceptance_policy_id".into(), json::s("m6-acceptance"));
            requirements.insert("acceptance_policy_version".into(), json::s("1"));
            requirements.insert(
                "acceptance_policy_bundle_sha".into(),
                json::s(&view.identity().acceptance_digest),
            );
            requirements.insert(
                "criteria".into(),
                Value::Arr(
                    selection
                        .publication()
                        .acceptance
                        .payload
                        .get("criteria")
                        .unwrap()
                        .as_arr()
                        .unwrap()
                        .iter()
                        .map(|rule| rule.without("origin").without("max_waiver_seconds"))
                        .collect(),
                ),
            );
            broker
                .administer(
                    &Operator,
                    &presentation,
                    &snapshot,
                    AdminRequest::ActivateAcceptancePolicy {
                        tenant_id: key.tenant_id.clone(),
                        project_id: key.project_id.clone(),
                        requirements: Value::Obj(requirements),
                        effect_prerequisites: vec![],
                    },
                )
                .unwrap();
            broker
                .administer(
                    &Operator,
                    &presentation,
                    &snapshot,
                    AdminRequest::CreateWorkflow {
                        key,
                        task_id: "task".into(),
                        initial_state: "verifying".into(),
                        artifact_snapshot_id: "snapshot".into(),
                        artifact_digest: artifact.to_string(),
                    },
                )
                .unwrap();
            let invocation = WaiverInvocation {
                verified: &verified,
                request: &reason,
            };
            let binding = broker.request_waiver(&invocation).unwrap().unwrap();
            assert_eq!(binding.expires_at, now().plus_seconds(240));
            assert!(binding
                .exclusions
                .policy_changers
                .iter()
                .any(|party| party.subject == "lab-human-publisher"));
            let id = binding.fingerprint().unwrap().to_string();
            let challenge = broker
                .begin_waiver(&invocation, &id, &approver, "signed-waiver")
                .unwrap()
                .unwrap();
            assert_eq!(challenge.context, binding);
            assert_eq!(challenge.issued.expires_at, now().plus_seconds(120));
            let recorded = broker
                .finish_waiver(
                    &invocation,
                    &id,
                    &challenge.issued.challenge_id,
                    approver.subject.as_bytes(),
                    "signed-waiver",
                )
                .unwrap()
                .unwrap();
            assert_eq!(
                recorded.result,
                CeremonyResult::Waived {
                    waiver_id: id.clone()
                }
            );
            assert_eq!(
                broker.validate_waiver(&invocation, &id).unwrap(),
                Ok(binding)
            );
            Ok(id)
        })
        .unwrap();
    let replacement = waiver_publication(&signer, 2, rules());
    let next = intent(Operation::Publish, 1, 2, &replacement);
    assert_eq!(
        registry.publish(&next, &replacement, id.as_bytes()),
        Err(Error::Authority)
    );
    publish(&registry, &authority, &replacement, 1, 2, "new-policy");
    let rollback = intent(Operation::Rollback, 2, 1, &original);
    registry
        .change(
            &rollback,
            None,
            &authority.authorize(&rollback, "same-bytes-new-revision"),
        )
        .unwrap();
    registry
        .with_selection(|selection| {
            let broker = Broker::new(&active, &clock, &ledger, &effects, &permit_key)
                .with_waiver_authority(&selection, &snapshot);
            let invocation = WaiverInvocation {
                verified: &verified,
                request: &reason,
            };
            assert!(broker.validate_waiver(&invocation, &id).unwrap().is_err());
            Ok(())
        })
        .unwrap();
    assert!(ledger.waiver_receipt(&id).unwrap().is_some());
}

#[test]
fn waiver_signed_policy_rejects_invariant_relabel_and_uses_key_validity() {
    let dir = Disposable::create().unwrap();
    let (authority, signer) = FixtureSigner::pair();
    let registry = Registry::create(
        &dir.path().join("registry.sqlite"),
        scope(),
        authority.clone(),
    )
    .unwrap();
    let invariant = criterion("review", "human", "invariant", 0);
    let project = criterion("waiver-review", "mechanical", "project", 300);
    for (index, criteria) in [
        vec![
            criterion("review", "human", "project", 300),
            project.clone(),
        ],
        vec![
            criterion("review", "human", "invariant", 1),
            project.clone(),
        ],
        vec![invariant.clone(), project.clone(), project.clone()],
    ]
    .into_iter()
    .enumerate()
    {
        let refused = waiver_publication(&signer, 1, criteria);
        let request = intent(Operation::Publish, 0, 1, &refused);
        assert_eq!(
            registry.publish(
                &request,
                &refused,
                &authority.authorize(&request, &format!("bad-{index}"))
            ),
            Err(Error::InvalidPolicy)
        );
    }
    let legitimate = waiver_publication(&signer, 1, vec![invariant, project]);
    let mut tampered = legitimate.clone();
    tampered.acceptance.payload = edit(
        &tampered.acceptance.payload,
        "criteria",
        Value::Arr(vec![
            criterion("review", "human", "invariant", 0),
            criterion("waiver-review", "mechanical", "project", 1),
        ]),
    );
    let request = intent(Operation::Publish, 0, 1, &tampered);
    assert_eq!(
        registry.publish(
            &request,
            &tampered,
            &authority.authorize(&request, "tampered")
        ),
        Err(Error::Signature)
    );
    publish(&registry, &authority, &legitimate, 0, 1, "valid");
    authority.set_key_expiry(KEY_ACCEPTANCE, now().plus_seconds(90));
    let mut ledger = SqliteLedger::create(&dir.path().join("clock.sqlite")).unwrap();
    let observed = observe(&FixedClock::new(now()), &mut ledger).unwrap();
    registry
        .with_selection(|selection| {
            assert_eq!(
                selection.current(observed).unwrap().expires_at(),
                now().plus_seconds(90)
            );
            Ok(())
        })
        .unwrap();
    authority.advance_to(90);
    assert_eq!(registry.with_selection(|_| Ok(())), Err(Error::Time));
}
