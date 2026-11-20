//! D-061 signed acceptance through the protected promotion transaction.
use super::*;
use evidence::{material, Source, RECORD_KEY};
use writ_broker::{
    approval::NewSignoff,
    approval_store::{CeremonyOp, CeremonyResult, SignoffParties},
    clock::FixedClock,
    evaluator_publication::*,
    execute::Broker,
    keys::{AcceptanceRecordKey, MemoryKeys, PermitKey, RunnerAttestationKey},
    runner::{CommandRegistry, RegisteredCommand},
    workflow::*,
};
use writ_kernel::{contracts::CriterionClass, json::Digest, time::Timestamp};

mod evidence;

const TENANT: &str = "m6-lab";
const PROJECT: &str = "offline-proof";
const IDS: [&str; 4] = ["docs-a", "docs-b", "docs-c", "approval-service"];

#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub(super) enum Case {
    Pass,
    One,
    Failure,
    Omit,
    Revoke,
    Legacy,
    LegacyDenied,
    Expired,
}

fn key() -> WorkflowKey {
    WorkflowKey {
        tenant_id: TENANT.into(),
        project_id: PROJECT.into(),
        workflow_id: "workflow".into(),
    }
}

fn rules(legacy: bool) -> Vec<Value> {
    let eligible = IDS[..3]
        .iter()
        .enumerate()
        .map(|(i, id)| {
            json::obj([
                ("evaluator_id", json::s(id)),
                ("independence_group", json::s(&format!("fixture-group-{i}"))),
            ])
        })
        .collect();
    [
        (
            "docs",
            "evidence",
            if legacy {
                Value::Null
            } else {
                json::obj([("eligible", Value::Arr(eligible))])
            },
        ),
        ("review", "human", Value::Null),
    ]
    .into_iter()
    .map(|(id, class, quorum)| {
        json::obj([
            ("criterion_id", json::s(id)),
            ("required_class", json::s(class)),
            ("origin", json::s("invariant")),
            ("max_waiver_seconds", Value::Int(0)),
            ("quorum", quorum),
        ])
    })
    .collect()
}

struct Approved<'a>(&'a EvaluatorIntent);
impl EvaluatorPublisher for Approved<'_> {
    fn current(&self) -> Result<impl CurrentEvaluatorPublisher + '_, AdminRefusal> {
        Ok(Approved(self.0))
    }
}
impl CurrentEvaluatorPublisher for Approved<'_> {
    fn approve(
        &self,
        intent: &EvaluatorIntent,
        p: &AdminPresentation<'_>,
        _: Timestamp,
    ) -> Result<PublisherApproval, AdminRefusal> {
        if intent.fingerprint() != self.0.fingerprint()
            || p.credential != b"fixture-operator-proof"
            || p.operator != "alice"
        {
            return Err(AdminRefusal::Unauthorized);
        }
        Ok(PublisherApproval {
            operator: AuthorizedOperator {
                id: "alice".into(),
                issuer: "fixture-human-realm".into(),
                channel: "lab".into(),
                evidence_ref: "fixture-authorization".into(),
            },
            ceremony: intent.fingerprint().into(),
            issued_at: now(),
            expires_at: now().plus_seconds(60),
        })
    }
}

struct Evaluators;
impl CommandRegistry for Evaluators {
    fn command(
        &self,
        _: &str,
        _: &str,
        _: &str,
    ) -> Result<Option<RegisteredCommand>, SourceUnavailable> {
        Ok(None)
    }
}
impl EvaluatorImplementations for Evaluators {
    fn get(
        &self,
        t: &str,
        p: &str,
        id: &str,
        version: &str,
    ) -> Result<Option<EvaluatorImplementation>, SourceUnavailable> {
        if (t, p, version) != (TENANT, PROJECT, "1.4.0") {
            return Ok(None);
        }
        Ok(IDS
            .iter()
            .position(|name| *name == id)
            .map(|i| EvaluatorImplementation {
                class: if i == 3 {
                    CriterionClass::Human
                } else {
                    CriterionClass::Evidence
                },
                command: None,
                implementation_digest: format!("sha256:{}", "1".repeat(64)),
                environment_digest: format!("sha256:{}", "2".repeat(64)),
                attestation_key_id: format!("key-{i}"),
            }))
    }
}

fn register(i: usize) -> EvaluatorIntent {
    let base = json::parse(include_bytes!(
        "../../../../docs/contracts/proposed/examples/evaluator-registry-entry.valid.json"
    ))
    .unwrap()
    .without("command");
    let mut entry = base.as_obj().unwrap().clone();
    for (k, v) in [
        ("evaluator_id", IDS[i]),
        ("class", if i == 3 { "human" } else { "evidence" }),
        ("trust", if i == 3 { "H" } else { "E" }),
        ("published_at", EPOCH),
    ] {
        entry.insert(k.into(), json::s(v));
    }
    entry.insert("registry_revision".into(), Value::Int(i as i64 + 1));
    entry.insert(
        "criteria".into(),
        Value::Arr(vec![json::s(if i == 3 { "review" } else { "docs" })]),
    );
    entry.insert(
        "scope".into(),
        json::obj([
            ("tenant_id", json::s(TENANT)),
            ("project_id", json::s(PROJECT)),
        ]),
    );
    entry.insert(
        "attestation".into(),
        json::obj([
            ("alg", json::s("hmac-sha256")),
            ("key_id", json::s(&format!("key-{i}"))),
        ]),
    );
    EvaluatorIntent::new(
        i as i64,
        AdminRequest::RegisterEvaluator {
            entry: Value::Obj(entry),
        },
    )
    .unwrap()
}

fn exercise(case: Case) {
    let dir = Disposable::create().unwrap();
    let (authority, signer) = FixtureSigner::pair();
    let mut publication = signer.publication(ALLOW, 1);
    let payload = edit(
        &edit(
            &publication.acceptance.payload,
            "version",
            json::s("writ-acceptance-policy/3"),
        ),
        "criteria",
        Value::Arr(rules(case == Case::Legacy)),
    );
    publication.acceptance = signer.sign_until(
        Purpose::Acceptance,
        payload,
        now().plus_seconds(if case == Case::Expired { 1 } else { 600 }),
    );
    let policy_path = dir.path().join("policy.sqlite");
    let registry = Registry::create(&policy_path, scope(), authority.clone()).unwrap();
    publish(&registry, &authority, &publication, 0, 1, "quorum-policy");
    let policy = json::digest(&publication.acceptance.payload).unwrap();
    let active = waivers::transition_policy(policy.as_str());
    let bytes = b"immutable synthetic artifact";
    let snapshot = waivers::Snapshot(bytes.to_vec());
    let artifact = Digest::of_bytes(bytes);
    let ledger_path = dir.path().join("ledger.sqlite");
    let (ledger, signer) = grants::quorum_ledger(&ledger_path);
    let effects =
        SqliteEffectStore::create(&dir.path().join("effects.sqlite"), ledger.ledger_id()).unwrap();
    let clock = FixedClock::new(now());
    let permit = PermitKey::from_bytes(vec![0x42; 32]).unwrap();
    let presentation = AdminPresentation {
        operator: "alice",
        channel: "lab",
        credential: b"fixture-operator-proof",
    };
    let source = Source::new(policy.as_str(), artifact.as_str(), case);
    let mut record_keys = MemoryKeys::new();
    record_keys.register(
        "acc-key-1",
        AcceptanceRecordKey::from_bytes(RECORD_KEY.to_vec()).unwrap(),
    );
    let mut runner_keys = MemoryKeys::new();
    for i in 0..4 {
        runner_keys.register(
            &format!("key-{i}"),
            RunnerAttestationKey::from_bytes(material(i)).unwrap(),
        );
    }
    let trust = Trust {
        records: &source,
        attestations: &source,
        record_keys: &record_keys,
        runner_keys: &runner_keys,
        snapshots: &snapshot,
    };
    let verified = waivers::transition(artifact.as_str());
    let succeeded = matches!(case, Case::Pass | Case::Legacy);
    registry
        .with_selection(|selection| {
            let broker = Broker::new(&active, &clock, &ledger, &effects, &permit)
                .with_quorum_policy(&selection);
            if case != Case::Legacy {
                assert_eq!(selection.acceptance(), Err(Error::Unsupported));
                assert!(matches!(
                    writ_broker::waiver::WaiverPolicyGuard::current(
                        &selection,
                        broker.grants().observe().unwrap()
                    ),
                    Err(Refusal::AcceptancePolicyUnavailable)
                ));
            }
            let criteria = rules(true)
                .iter()
                .map(|c| {
                    c.without("quorum")
                        .without("origin")
                        .without("max_waiver_seconds")
                })
                .collect();
            let requirements = json::obj([
                ("acceptance_policy_id", json::s("m6-acceptance")),
                ("acceptance_policy_version", json::s("1")),
                ("acceptance_policy_bundle_sha", json::s(policy.as_str())),
                ("criteria", Value::Arr(criteria)),
            ]);
            for request in [
                AdminRequest::ActivateAcceptancePolicy {
                    tenant_id: TENANT.into(),
                    project_id: PROJECT.into(),
                    requirements,
                    effect_prerequisites: vec![],
                },
                AdminRequest::CreateWorkflow {
                    key: key(),
                    task_id: "task".into(),
                    initial_state: "verifying".into(),
                    artifact_snapshot_id: "snapshot".into(),
                    artifact_digest: artifact.to_string(),
                },
            ] {
                broker
                    .administer(
                        &waivers::Operator,
                        &AdminPresentation {
                            operator: "workflow-operator",
                            ..presentation
                        },
                        &snapshot,
                        request,
                    )
                    .unwrap();
            }
            let dependencies = EvaluatorDependencies {
                runner_keys: &runner_keys,
                commands: &Evaluators,
                implementations: &Evaluators,
            };
            let publish = |intent: EvaluatorIntent| {
                broker
                    .publish_evaluator(&Approved(&intent), &presentation, &dependencies, &intent)
                    .unwrap()
                    .unwrap()
            };
            for i in 0..4 {
                publish(register(i));
            }
            assert!(broker.promote(&trust, &verified, None).result.is_err());
            let requested = broker.request_signoff(&trust, &verified);
            assert_eq!(requested, Ok(vec!["so-1".into()]));
            {
                let binding = NewSignoff {
                    signoff_id: "so-1".into(),
                    tenant_id: TENANT.into(),
                    project_id: PROJECT.into(),
                    task_id: "task".into(),
                    workflow_id: "workflow".into(),
                    from_state: "verifying".into(),
                    to_state: "accepted".into(),
                    acceptance_record_id: "future-record".into(),
                    criterion_id: "review".into(),
                    artifact_snapshot_id: "snapshot".into(),
                    artifact_digest: artifact.to_string(),
                    acceptance_policy_id: "m6-acceptance".into(),
                    acceptance_policy_version: "1".into(),
                    acceptance_policy_bundle_sha: policy.to_string(),
                    state_revision: 0,
                };
                let result = grants::ceremony(
                    &ledger,
                    &signer,
                    &CeremonyOp::Signoff {
                        binding: Box::new(binding),
                        parties: SignoffParties { added: vec![] },
                        requested_expires_at: None,
                    },
                    "quorum-signoff",
                );
                assert!(matches!(result, CeremonyResult::SignedOff { .. }));
            }
            if case == Case::Revoke {
                publish(
                    EvaluatorIntent::new(
                        4,
                        AdminRequest::RevokeEvaluator {
                            tenant_id: TENANT.into(),
                            project_id: PROJECT.into(),
                            evaluator_id: IDS[1].into(),
                        },
                    )
                    .unwrap(),
                );
            }
            let use_clock =
                FixedClock::new(now().plus_seconds(if case == Case::Expired { 1 } else { 0 }));
            let promotion = Broker::new(&active, &use_clock, &ledger, &effects, &permit)
                .with_quorum_policy(&selection);
            let outcome = promotion.promote(&trust, &verified, None).result;
            use writ_broker::acceptance::AcceptanceCode;
            use writ_kernel::acceptance::AcceptanceRefusal;
            let expected = match case {
                Case::Pass | Case::Legacy => Ok(PromotionOutcome::Committed {
                    revision: 1,
                    receipt_id: "promotion:workflow:1".into(),
                }),
                Case::One | Case::Failure | Case::LegacyDenied => Err(Refusal::Acceptance(
                    AcceptanceCode::Kernel(AcceptanceRefusal::Incomplete),
                )),
                Case::Omit => Err(Refusal::Acceptance(AcceptanceCode::BindingMismatch)),
                Case::Revoke => Err(Refusal::Acceptance(
                    AcceptanceCode::EvaluatorBindingMismatch,
                )),
                Case::Expired => Err(Refusal::AcceptancePolicyUnavailable),
            };
            assert_eq!(outcome, expected, "{case:?}");
            Ok(())
        })
        .unwrap();
    drop(ledger);
    let ledger = SqliteLedger::open_quorum_lab(&ledger_path).unwrap();
    assert_eq!(
        count(&ledger_path, "promotion_receipts"),
        i64::from(succeeded)
    );
    assert_eq!(
        count(&ledger_path, "consumed_records"),
        i64::from(succeeded)
    );
    assert_eq!(
        count(&ledger_path, "consumed_signoffs"),
        i64::from(succeeded)
    );
    assert_eq!(
        count(&ledger_path, "consumed_attestations"),
        if case == Case::Pass { 2 } else { 0 }
    );
    assert_eq!(
        count(&ledger_path, "quorum_promotions"),
        i64::from(case == Case::Pass)
    );
    if succeeded {
        drop(registry);
        let registry = Registry::open(&policy_path, scope(), authority).unwrap();
        registry
            .with_selection(|selection| {
                let result = Broker::new(&active, &clock, &ledger, &effects, &permit)
                    .with_quorum_policy(&selection)
                    .promote(&trust, &verified, None)
                    .result;
                assert_eq!(
                    result,
                    Ok(PromotionOutcome::Replayed {
                        revision: 1,
                        receipt_id: "promotion:workflow:1".into()
                    })
                );
                Ok(())
            })
            .unwrap();
    }
}

#[test]
fn signed_quorum_promotion_and_recovery() {
    for case in [
        Case::Pass,
        Case::One,
        Case::Failure,
        Case::Omit,
        Case::Revoke,
        Case::Legacy,
        Case::LegacyDenied,
        Case::Expired,
    ] {
        exercise(case);
    }
}
