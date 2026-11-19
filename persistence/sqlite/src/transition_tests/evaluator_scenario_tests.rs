//! D-052 disposable evaluator registry scenario using the protected promotion path.

use std::sync::{RwLock, RwLockReadGuard};

use writ_broker::evaluator_publication::{
    CurrentEvaluatorPublisher, EvaluatorDependencies, EvaluatorImplementation,
    EvaluatorImplementations, EvaluatorIntent, EvaluatorPublisher, PublisherApproval,
};
use writ_broker::evaluator_rubric::{RubricBinding, RubricEligibility, RubricError};
use writ_broker::runner::{CommandRegistry, RegisteredCommand};
use writ_broker::writ_kernel::commands;
use writ_broker::writ_kernel::contracts::CriterionClass;

use super::*;

#[derive(Default)]
pub(super) struct Publishers(RwLock<BTreeMap<String, PublisherApproval>>);

struct CurrentPublishers<'a>(RwLockReadGuard<'a, BTreeMap<String, PublisherApproval>>);

fn publisher(credential: &[u8]) -> AdminPresentation<'_> {
    AdminPresentation {
        channel: "fixture-publisher",
        operator: "alice",
        credential,
    }
}

impl Publishers {
    pub(super) fn approve_fixture(&self, intent: &EvaluatorIntent) {
        self.0.write().unwrap().insert(
            intent.fingerprint().into(),
            PublisherApproval {
                operator: AuthorizedOperator {
                    id: "alice".into(),
                    issuer: "fixture-human-realm".into(),
                    channel: "fixture-publisher".into(),
                    evidence_ref: "fixture-authentication-event".into(),
                },
                ceremony: intent.fingerprint().into(),
                issued_at: ts("2026-11-07T10:59:00Z"),
                expires_at: ts("2026-11-07T11:01:00Z"),
            },
        );
    }
}

impl EvaluatorPublisher for Publishers {
    fn current(&self) -> Result<impl CurrentEvaluatorPublisher + '_, AdminRefusal> {
        Ok(CurrentPublishers(self.0.read().unwrap()))
    }
}

impl CurrentEvaluatorPublisher for CurrentPublishers<'_> {
    fn approve(
        &self,
        intent: &EvaluatorIntent,
        presentation: &AdminPresentation<'_>,
        _: Timestamp,
    ) -> Result<PublisherApproval, AdminRefusal> {
        if (
            presentation.channel,
            presentation.operator,
            presentation.credential,
        ) != ("fixture-publisher", "alice", b"publisher-credential")
        {
            return Err(AdminRefusal::Unauthenticated);
        }
        self.0
            .get(intent.fingerprint())
            .cloned()
            .ok_or(AdminRefusal::Unauthorized)
    }
}

struct FixtureDependencies;

fn command() -> RegisteredCommand {
    RegisteredCommand {
        command_id: "cargo-test".into(),
        program: b"fixture command bytes".to_vec(),
        args: vec!["test".into()],
        env: vec![("CARGO_NET_OFFLINE".into(), "true".into())],
    }
}

impl CommandRegistry for FixtureDependencies {
    fn command(
        &self,
        tenant: &str,
        project: &str,
        command_id: &str,
    ) -> Result<Option<RegisteredCommand>, SourceUnavailable> {
        Ok(((tenant, project, command_id) == (T, P, "cargo-test")).then(command))
    }
}

impl EvaluatorImplementations for FixtureDependencies {
    fn get(
        &self,
        tenant: &str,
        project: &str,
        evaluator: &str,
        version: &str,
    ) -> Result<Option<EvaluatorImplementation>, SourceUnavailable> {
        Ok(((tenant, project, version) == (T, P, "1.4.0")
            && matches!(evaluator, "ci-runner" | "doc-check" | "model-review"))
        .then(|| EvaluatorImplementation {
            class: match evaluator {
                "ci-runner" => CriterionClass::Mechanical,
                "doc-check" => CriterionClass::Evidence,
                _ => CriterionClass::ModelAssisted,
            },
            command: (evaluator == "ci-runner").then(|| {
                let command = command();
                (command.command_id.clone(), command.digest().unwrap())
            }),
            implementation_digest: sha('1'),
            environment_digest: sha('2'),
            attestation_key_id: "runner-key-1".into(),
        }))
    }
}

fn submit(
    w: &W,
    authorities: &impl EvaluatorPublisher,
    intent: &EvaluatorIntent,
) -> Result<(), Refusal> {
    submit_with_credential(w, authorities, intent, b"publisher-credential")
}

pub(super) fn submit_with_credential(
    w: &W,
    authorities: &impl EvaluatorPublisher,
    intent: &EvaluatorIntent,
    credential: &[u8],
) -> Result<(), Refusal> {
    w.broker()
        .publish_evaluator(
            authorities,
            &publisher(credential),
            &EvaluatorDependencies {
                runner_keys: &w.runner_keys,
                commands: &FixtureDependencies,
                implementations: &FixtureDependencies,
            },
            intent,
        )
        .unwrap()
}

fn entry(id: &str, expected: i64) -> Value {
    let mut entry = registry_entry(id, expected + 1);
    entry = set(&entry, &["published_at"], json::s(&now().to_string()));
    if id == "ci-runner" {
        entry = set(
            &entry,
            &["command", "command_digest"],
            json::s(&command().digest().unwrap()),
        );
    } else if id == "model-review" {
        entry = set(&entry, &["evaluator_id"], json::s(id));
        entry = set(&entry, &["class"], json::s("model_assisted"));
        entry = set(&entry, &["trust"], json::s("MA"));
        entry = entry.without("command");
    }
    entry
}

pub(super) fn publication(id: &str, expected: i64) -> EvaluatorIntent {
    EvaluatorIntent::new(
        expected,
        AdminRequest::RegisterEvaluator {
            entry: entry(id, expected),
        },
    )
    .unwrap()
}

fn revocation() -> EvaluatorIntent {
    EvaluatorIntent::new(
        2,
        AdminRequest::RevokeEvaluator {
            tenant_id: T.into(),
            project_id: P.into(),
            evaluator_id: "ci-runner".into(),
        },
    )
    .unwrap()
}

fn rubric() -> Value {
    let fixture = parse(&fixture(
        "docs/contracts/proposed/examples/pack-rubric-binding.valid.json",
    ));
    set(
        &set(&fixture, &["tenant_id"], json::s(T)),
        &["project_id"],
        json::s(P),
    )
}

fn fixture(path: &str) -> String {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    std::fs::read_to_string(root.join(path)).unwrap()
}

fn check(w: &W, proposal: &Value) -> Result<RubricEligibility, RubricError> {
    RubricBinding::validate(proposal)?.check(T, P, |tenant, project, evaluator| {
        let (_, entry) = w
            .ledger
            .evaluator_registry_view(tenant, project, evaluator)
            .map_err(|_| SourceUnavailable)?;
        Ok(entry)
    })
}

fn evidence(w: &W) {
    let mut record = record("acc-1", 1, &sha('b'), "att-1");
    let mut evaluations = record
        .get("evaluations")
        .unwrap()
        .as_arr()
        .unwrap()
        .to_vec();
    for (index, evaluation) in evaluations.iter_mut().enumerate() {
        *evaluation = set(
            evaluation,
            &["registry_revision"],
            Value::Int(i64::try_from(index + 1).unwrap()),
        );
    }
    record = set(&record, &["evaluations"], Value::Arr(evaluations));
    w.docs.put("acc-1", sign(&record, &RECORD_KEY));
    let mut attestation = attestation("att-1", "acc-1", 1);
    attestation = set(&attestation, &["registry_revision"], Value::Int(1));
    attestation = set(
        &attestation,
        &["command_digest"],
        json::s(&command().digest().unwrap()),
    );
    w.docs.put("att-1", sign(&attestation, &RUNNER_KEY));
}

fn scenario(name: &str) -> (W, Publishers) {
    let w = W::new(name);
    let publishers = Publishers::default();
    for (id, revision) in [("ci-runner", 0), ("doc-check", 1)] {
        let intent = publication(id, revision);
        publishers.approve_fixture(&intent);
        submit(&w, &publishers, &intent).unwrap();
        assert_eq!(
            w.ledger.evaluator_registry_view(T, P, id).unwrap().0,
            revision + 1
        );
    }
    assert_eq!(check(&w, &rubric()), Ok(RubricEligibility::Eligible));
    w.admin(AdminRequest::CreateWorkflow {
        key: wf(),
        task_id: "task-1".into(),
        initial_state: "planning".into(),
        artifact_snapshot_id: "snap-1".into(),
        artifact_digest: sha('b'),
    })
    .unwrap();
    w.admin(AdminRequest::ActivateAcceptancePolicy {
        tenant_id: T.into(),
        project_id: P.into(),
        requirements: requirements("3"),
        effect_prerequisites: Vec::new(),
    })
    .unwrap();
    assert_eq!(
        w.promote(&transition("k0", "planning", "verifying", 0, None)),
        Ok(committed(1))
    );
    evidence(&w);
    (w, publishers)
}

fn revoked() -> Refusal {
    acceptance(AcceptanceCode::Kernel(
        writ_broker::writ_kernel::acceptance::AcceptanceRefusal::EvaluatorRevoked,
    ))
}

#[test]
fn publication_requires_an_exact_authorized_intent_and_scoped_dependencies() {
    let w = W::new("d052-publication-boundaries");
    let authorities = Publishers::default();
    let valid = publication("ci-runner", 0);
    assert_eq!(
        submit(&w, &authorities, &valid),
        Err(Refusal::OperatorUnauthorized)
    );
    authorities.approve_fixture(&valid);
    let dependencies = EvaluatorDependencies {
        runner_keys: &w.runner_keys,
        commands: &FixtureDependencies,
        implementations: &FixtureDependencies,
    };
    for bad in [b"agent".as_slice(), b"pack", b"model"] {
        assert_eq!(
            w.broker()
                .publish_evaluator(&authorities, &publisher(bad), &dependencies, &valid)
                .unwrap(),
            Err(Refusal::OperatorUnauthenticated)
        );
    }
    for path in [
        &["implementation_digest"][..],
        &["environment_digest"],
        &["command", "command_digest"],
        &["attestation", "key_id"],
        &["scope", "project_id"],
    ] {
        let wrong = if path[0].ends_with("_digest") || path[0] == "command" {
            sha('f')
        } else {
            "wrong".into()
        };
        let document = set(&entry("ci-runner", 0), path, json::s(&wrong));
        let intent =
            EvaluatorIntent::new(0, AdminRequest::RegisterEvaluator { entry: document }).unwrap();
        assert_eq!(
            submit(&w, &authorities, &intent),
            Err(Refusal::OperatorUnauthorized)
        );
        authorities.approve_fixture(&intent);
        assert_eq!(
            submit(&w, &authorities, &intent),
            Err(acceptance(AcceptanceCode::EvaluatorBindingMismatch)),
            "{path:?}",
        );
    }
    let upgraded_model = set(
        &entry("ci-runner", 0),
        &["evaluator_id"],
        json::s("model-review"),
    );
    let intent = EvaluatorIntent::new(
        0,
        AdminRequest::RegisterEvaluator {
            entry: upgraded_model,
        },
    )
    .unwrap();
    authorities.approve_fixture(&intent);
    assert_eq!(
        submit(&w, &authorities, &intent),
        Err(acceptance(AcceptanceCode::EvaluatorBindingMismatch)),
    );
    assert_eq!(w.one("SELECT count(*) FROM evaluator_registry"), 0);
    assert_eq!(
        w.ledger
            .evaluator_registry_view(T, P, "ci-runner")
            .unwrap()
            .0,
        0
    );
    submit(&w, &authorities, &valid).unwrap();
    for generic in [
        AdminRequest::RegisterEvaluator {
            entry: entry("ci-runner", 1),
        },
        AdminRequest::RevokeEvaluator {
            tenant_id: T.into(),
            project_id: P.into(),
            evaluator_id: "ci-runner".into(),
        },
    ] {
        assert_eq!(w.admin(generic), Err(Refusal::OperatorUnauthorized));
    }
    assert_eq!(
        w.ledger
            .evaluator_registry_view(T, P, "ci-runner")
            .unwrap()
            .0,
        1
    );
    assert_eq!(check(&w, &rubric()), Ok(RubricEligibility::Eligible));
}

#[test]
fn disposable_publication_rubric_promotion_revocation_and_reopen() {
    // These disposable stores have identical workflow, evaluator and evidence
    // inputs. The second exercises revocation without reusing consumed evidence.
    let (positive, _) = scenario("d052-accepted");
    assert_eq!(positive.promote(&accept("k1", 1)), Ok(committed(2)));
    assert_eq!(positive.state(), ("accepted".into(), 2));
    assert_eq!(positive.one("SELECT count(*) FROM consumed_records"), 1);
    assert_eq!(
        positive.one("SELECT count(*) FROM consumed_attestations"),
        1
    );

    let (w, publishers) = scenario("d052-revoked");
    let intent = revocation();
    publishers.approve_fixture(&intent);
    submit(&w, &publishers, &intent).unwrap();
    let committed = w
        .ledger
        .evaluator_publication_commit(T, P, intent.fingerprint())
        .unwrap()
        .unwrap();
    assert_eq!(committed.revision, 3);
    assert_eq!(check(&w, &rubric()), Err(RubricError::EvaluatorInactive));
    let request = accept("k1", 1);
    assert_eq!(w.promote(&request), Err(revoked()));
    unchanged(&w);
    let dir = w.dir.clone();
    drop(w);

    let reopened = W::at(dir, true);
    evidence(&reopened);
    assert_eq!(
        reopened
            .ledger
            .evaluator_registry_view(T, P, "ci-runner")
            .unwrap()
            .0,
        3
    );
    assert_eq!(
        reopened
            .ledger
            .evaluator_publication_commit(T, P, intent.fingerprint())
            .unwrap()
            .unwrap()
            .sequence,
        committed.sequence
    );
    assert_eq!(
        check(&reopened, &rubric()),
        Err(RubricError::EvaluatorInactive)
    );
    assert_eq!(reopened.promote(&request), Err(revoked()));
    unchanged(&reopened);
}

#[test]
fn rubric_refuses_unknown_wrong_scope_stale_class_and_criteria_bindings() {
    let (w, publishers) = scenario("d052-bindings");
    let valid = rubric();
    for (field, invalid, refusal) in [
        (
            "evaluator_id",
            json::s("unknown"),
            RubricError::EvaluatorUnknown,
        ),
        (
            "evaluator_id",
            json::s("doc-check"),
            RubricError::EvaluatorMismatch,
        ),
        ("tenant_id", json::s("wrong"), RubricError::ScopeMismatch),
        ("project_id", json::s("wrong"), RubricError::ScopeMismatch),
        (
            "evaluator_version",
            json::s("2.0.0"),
            RubricError::EvaluatorMismatch,
        ),
        (
            "registry_revision",
            Value::Int(2),
            RubricError::EvaluatorMismatch,
        ),
        ("class", json::s("evidence"), RubricError::EvaluatorMismatch),
        (
            "criteria",
            Value::Arr(vec![json::s("docs")]),
            RubricError::CriterionMismatch,
        ),
        (
            "criteria",
            Value::Arr(vec![json::s("build"), json::s("build")]),
            RubricError::Malformed,
        ),
        ("trust", json::s("M"), RubricError::Malformed),
        (
            "attestation_key_id",
            json::s("new-key"),
            RubricError::Malformed,
        ),
    ] {
        assert_eq!(
            check(&w, &set(&valid, &[field], invalid)),
            Err(refusal),
            "{field}"
        );
    }
    let model = publication("model-review", 2);
    publishers.approve_fixture(&model);
    submit(&w, &publishers, &model).unwrap();
    let proposal = set(&valid, &["evaluator_id"], json::s("model-review"));
    let proposal = set(&proposal, &["registry_revision"], Value::Int(3));
    let proposal = set(&proposal, &["class"], json::s("model_assisted"));
    assert_eq!(check(&w, &proposal), Ok(RubricEligibility::AdvisoryOnly));

    // Even an authenticated fixture document cannot substitute the model for
    // the evaluator in the trusted mandatory mechanical criterion.
    let document = w.docs.get("acc-1").unwrap().unwrap();
    let mut payload = document.get("record").unwrap().clone();
    let mut criteria = payload.get("criteria").unwrap().as_arr().unwrap().to_vec();
    criteria[0] = set(&criteria[0], &["evaluator_id"], json::s("model-review"));
    payload = set(&payload, &["criteria"], Value::Arr(criteria));
    let digest = json::digest(&payload.without("record_digest")).unwrap();
    payload = set(&payload, &["record_digest"], json::s(digest.as_str()));
    let mut evaluations = document
        .get("evaluations")
        .unwrap()
        .as_arr()
        .unwrap()
        .to_vec();
    evaluations[0] = set(&evaluations[0], &["evaluator_id"], json::s("model-review"));
    evaluations[0] = set(&evaluations[0], &["registry_revision"], Value::Int(3));
    let document = set(&document, &["record"], payload);
    let document = set(&document, &["evaluations"], Value::Arr(evaluations));
    w.docs.put("acc-1", sign(&document, &RECORD_KEY));
    assert!(matches!(
        w.promote(&accept("k1", 1)),
        Err(Refusal::Acceptance(_))
    ));
    unchanged(&w);
}

#[test]
fn secure_promotion_requires_the_registry_audit_and_the_published_key() {
    let (w, _) = scenario("d052-registry-unavailable");
    w.ledger
        .db()
        .conn
        .execute(
            "DELETE FROM admin_events WHERE target LIKE 'evaluator-publication:%/ci-runner/%'",
            [],
        )
        .unwrap();
    assert_eq!(check(&w, &rubric()), Err(RubricError::RegistryUnavailable));
    assert!(matches!(
        w.promote(&accept("k1", 1)),
        Err(Refusal::LedgerUnavailable(_))
    ));
    unchanged(&w);

    let (mut w, _) = scenario("d052-registered-key-replaced");
    let changed_key = [0x7c; 32];
    w.runner_keys.register(
        "runner-key-1",
        RunnerAttestationKey::from_bytes(changed_key.to_vec()).unwrap(),
    );
    let old = w.docs.get("att-1").unwrap().unwrap();
    w.docs.put("att-1", sign(&old, &changed_key));
    assert_eq!(
        w.promote(&accept("k1", 1)),
        Err(acceptance(AcceptanceCode::AttestationUnauthenticated)),
    );
    unchanged(&w);

    let (w, _) = scenario("d052-registered-key-revoked");
    revoke_material(
        &w,
        writ_broker::keys::KeyPurpose::RunnerAttestation,
        &RUNNER_KEY,
    );
    assert_eq!(
        w.promote(&accept("k1", 1)),
        Err(acceptance(AcceptanceCode::AttestationUnauthenticated)),
    );
    unchanged(&w);
}

#[test]
fn enabling_disabling_and_upgrading_a_rubric_preserves_every_golden_decision() {
    let (w, _) = scenario("d052-pack-invariance");
    let corpus: Vec<Value> = fixture("m1/fixtures/sets/golden-decisions.jsonl")
        .lines()
        .map(parse)
        .collect();
    assert_eq!(corpus.len(), 26);
    let decide = |pack: Option<(&Value, &Value)>| {
        let eligibility = pack.map(|(manifest, proposal)| {
            assert_eq!(manifest.get("pack_id"), proposal.get("pack_id"));
            assert_eq!(manifest.get("version"), proposal.get("pack_version"));
            check(&w, proposal)
        });
        let decisions: Vec<String> = corpus
            .iter()
            .map(|fixture| {
                let input = fixture.get("input").unwrap().to_json_text();
                commands::decide(input.as_bytes())
            })
            .collect();
        (eligibility, decisions)
    };

    let (none, baseline) = decide(None);
    assert_eq!(none, None);
    let v1 = rubric();
    let template = parse(&fixture("docs/packs/examples/software-engineering.json"));
    let pack = set(&template, &["pack_id"], v1.get("pack_id").unwrap().clone());
    let pack = set(&pack, &["version"], json::s("1"));
    let (enabled, enabled_decisions) = decide(Some((&pack, &v1)));
    assert_eq!(enabled, Some(Ok(RubricEligibility::Eligible)));
    assert_eq!(enabled_decisions, baseline);

    let v2 = set(&v1, &["pack_version"], json::s("2"));
    let v2 = set(&v2, &["rubric_version"], json::s("2"));
    let upgraded_pack = set(&pack, &["version"], json::s("2"));
    let upgraded_pack = set(
        &upgraded_pack,
        &["tool_capabilities"],
        Value::Arr(vec![
            json::s("network.egress"),
            json::s("repository.publish"),
        ]),
    );
    let (upgraded, upgraded_decisions) = decide(Some((&upgraded_pack, &v2)));
    assert_eq!(upgraded, Some(Ok(RubricEligibility::Eligible)));
    assert_eq!(upgraded_decisions, baseline);

    let (disabled, disabled_decisions) = decide(None);
    assert_eq!(disabled, None);
    assert_eq!(disabled_decisions, baseline);
}

struct OperationGate {
    entered: mpsc::Sender<()>,
    release: Mutex<mpsc::Receiver<()>>,
}

impl OperationGate {
    fn open(&self) {
        self.entered.send(()).unwrap();
        self.release
            .lock()
            .unwrap()
            .recv_timeout(Duration::from_secs(10))
            .unwrap();
    }
}

struct GatedRecords<'a> {
    docs: &'a Docs,
    gate: &'a OperationGate,
}

impl AcceptanceRecordSource for GatedRecords<'_> {
    fn record(
        &self,
        tenant: &str,
        project: &str,
        id: &str,
    ) -> Result<Option<Value>, SourceUnavailable> {
        self.gate.open();
        self.docs.record(tenant, project, id)
    }
}

struct GatedPublisher<'a> {
    publishers: &'a Publishers,
    gate: &'a OperationGate,
}

impl EvaluatorPublisher for GatedPublisher<'_> {
    fn current(&self) -> Result<impl CurrentEvaluatorPublisher + '_, AdminRefusal> {
        self.gate.open();
        self.publishers.current()
    }
}

#[test]
fn promotion_racing_revocation_follows_the_durable_commit_sequence() {
    for promotion_first in [true, false] {
        let (w, publishers) = scenario(&format!("d052-race-{promotion_first}"));
        let intent = revocation();
        publishers.approve_fixture(&intent);
        let (entered, waiting) = mpsc::channel();
        let (release, released) = mpsc::channel();
        let gate = OperationGate {
            entered,
            release: Mutex::new(released),
        };
        let (contending, started) = mpsc::channel();
        let (finished, result_while_held) = mpsc::channel();
        let records = GatedRecords {
            docs: &w.docs,
            gate: &gate,
        };
        let publisher = GatedPublisher {
            publishers: &publishers,
            gate: &gate,
        };
        let (result, revoke) = thread::scope(|scope| {
            let promote = || {
                let mut trust = w.trust();
                if promotion_first {
                    trust.records = &records;
                } else {
                    contending.send(()).unwrap();
                }
                let result = w
                    .broker()
                    .promote(&trust, &verify(&accept("k1", 1)), None)
                    .result;
                if !promotion_first {
                    finished.send(()).unwrap();
                }
                result
            };
            let revoke = || {
                if promotion_first {
                    contending.send(()).unwrap();
                    let result = submit(&w, &publishers, &intent);
                    finished.send(()).unwrap();
                    result
                } else {
                    submit(&w, &publisher, &intent)
                }
            };
            // Pause the first operation with its fence held before starting
            // the contender. Read the ledger's order after both operations finish.
            let (promotion, revocation) = if promotion_first {
                let promotion = scope.spawn(promote);
                waiting.recv_timeout(Duration::from_secs(10)).unwrap();
                (promotion, scope.spawn(revoke))
            } else {
                let revocation = scope.spawn(revoke);
                waiting.recv_timeout(Duration::from_secs(10)).unwrap();
                (scope.spawn(promote), revocation)
            };
            started.recv_timeout(Duration::from_secs(10)).unwrap();
            let early_result = result_while_held.recv_timeout(Duration::from_millis(50));
            release.send(()).unwrap();
            assert_eq!(early_result, Err(mpsc::RecvTimeoutError::Timeout));
            (promotion.join().unwrap(), revocation.join().unwrap())
        });
        assert_eq!(revoke, Ok(()));
        let revocation = w
            .ledger
            .evaluator_publication_commit(T, P, intent.fingerprint())
            .unwrap()
            .unwrap();
        let promotion = w.one(
            "SELECT coalesce(max(seq), 0) FROM ledger_sequence \
             WHERE kind = 'promotion' AND reference = 'promotion:wf-1:2'",
        );
        if promotion_first {
            assert!(promotion > 0 && promotion < revocation.sequence);
            assert_eq!(result, Ok(committed(2)));
            assert_eq!(w.state(), ("accepted".into(), 2));
            assert_eq!(w.one("SELECT count(*) FROM consumed_records"), 1);
            assert_eq!(w.one("SELECT count(*) FROM consumed_attestations"), 1);
        } else {
            assert_eq!(promotion, 0);
            assert_eq!(result, Err(revoked()));
            unchanged(&w);
        }
    }
}
