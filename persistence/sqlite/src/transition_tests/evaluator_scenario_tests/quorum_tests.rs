//! Disposable quorum ledger transaction and exact-recovery scenarios.
use super::*;
use writ_broker::quorum_policy::{QuorumPolicyGuard, QuorumRequirements};
use writ_broker::workflow::{AttemptScope, CurrentAttempt};
use writ_broker::writ_kernel::contracts::AcceptanceRequirements;

const SECOND_KEY: [u8; 32] = [0x6c; 32];

struct Policy(QuorumRequirements);
impl QuorumPolicyGuard for Policy {
    fn current(
        &self,
        tenant: &str,
        project: &str,
        at: Timestamp,
    ) -> Result<&QuorumRequirements, Refusal> {
        assert_eq!((tenant, project), (T, P));
        assert!(at >= now());
        Ok(&self.0)
    }
}

#[derive(Clone)]
struct Attempt {
    digest: Digest,
    members: BTreeMap<String, Digest>,
}

impl CurrentAttempt for Attempt {
    fn record_id(&self) -> &str {
        "acc-1"
    }
    fn record_digest(&self) -> &Digest {
        &self.digest
    }
    fn attempt_id(&self) -> &str {
        "attempt-1"
    }
    fn members(&self) -> &BTreeMap<String, Digest> {
        &self.members
    }
}

struct Records<'a> {
    docs: &'a Docs,
    attempt: &'a Attempt,
}

impl AcceptanceRecordSource for Records<'_> {
    fn record(&self, t: &str, p: &str, id: &str) -> Result<Option<Value>, SourceUnavailable> {
        self.docs.record(t, p, id)
    }
    fn current_attempt(
        &self,
        scope: &AttemptScope<'_>,
    ) -> Result<Option<Box<dyn CurrentAttempt + '_>>, SourceUnavailable> {
        assert_eq!(scope.workflow, &wf());
        assert_eq!(
            (
                scope.task_id,
                scope.revision,
                scope.artifact_snapshot_id,
                scope.artifact_digest
            ),
            ("task-1", 1, "snap-1", sha('b').as_str())
        );
        assert_eq!(
            (scope.from_state, scope.to_state, scope.policy_sha),
            ("verifying", "accepted", sha('c').as_str())
        );
        Ok(Some(Box::new(self.attempt.clone())))
    }
}

fn setup(name: &str) -> (W, Policy, Attempt) {
    let dir = std::env::temp_dir().join(format!("writ-quorum-{}-{name}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let ledger = SqliteLedger::create_quorum_lab(&dir.join("ledger.db")).unwrap();
    let effects = SqliteEffectStore::create(&dir.join("effects.db"), ledger.ledger_id()).unwrap();
    let mut w = W::with_stores(dir, ledger, effects);
    w.runner_keys.register(
        "runner-key-2",
        RunnerAttestationKey::from_bytes(SECOND_KEY.to_vec()).unwrap(),
    );
    let publishers = Publishers::default();
    for (i, e) in ["ci-runner", "doc-check", "ci-second"]
        .into_iter()
        .enumerate()
    {
        let intent = publication(e, i as i64);
        publishers.approve_fixture(&intent);
        submit(&w, &publishers, &intent).unwrap();
    }
    w.admin(AdminRequest::CreateWorkflow {
        key: wf(),
        task_id: "task-1".into(),
        initial_state: "planning".into(),
        artifact_snapshot_id: "snap-1".into(),
        artifact_digest: sha('b'),
    })
    .unwrap();
    let req = requirements("3");
    let mut quorums = BTreeMap::new();
    quorums.insert(
        "build".into(),
        BTreeMap::from([
            ("ci-runner".into(), "team-a".into()),
            ("ci-second".into(), "team-b".into()),
        ]),
    );
    let policy = Policy(QuorumRequirements::new(
        AcceptanceRequirements::from_value(&req).unwrap(),
        quorums,
    ));
    w.admin(AdminRequest::ActivateAcceptancePolicy {
        tenant_id: T.into(),
        project_id: P.into(),
        requirements: req,
        effect_prerequisites: Vec::new(),
    })
    .unwrap();
    assert_eq!(
        w.promote(&transition("k0", "planning", "verifying", 0, None)),
        Ok(committed(1))
    );
    evidence(&w);
    let mut doc = w.docs.get("acc-1").unwrap().unwrap();
    let evals = doc
        .get("evaluations")
        .unwrap()
        .as_arr()
        .unwrap()
        .iter()
        .map(|e| set(e, &["signoff_id"], Value::Null))
        .collect();
    doc = set(&doc, &["evaluations"], Value::Arr(evals));
    doc = set(
        &doc,
        &["contract_version"],
        json::s("authenticated-acceptance-record/3"),
    );
    doc = set(&doc, &["attempt_id"], json::s("attempt-1"));
    doc = set(
        &doc,
        &["contributions"],
        parse(
            r#"[{"criterion_id":"build","evaluator_id":"ci-runner","attestation_id":"att-1"},{"criterion_id":"build","evaluator_id":"ci-second","attestation_id":"att-2"}]"#,
        ),
    );
    let doc = sign(&doc, &RECORD_KEY);
    let mut attempt = Attempt {
        digest: json::digest(&doc).unwrap(),
        members: BTreeMap::new(),
    };
    w.docs.put("acc-1", doc);
    let base = w.docs.get("att-1").unwrap().unwrap();
    for (id, evaluator, rev, key_id, key) in [
        ("att-1", "ci-runner", 1, "runner-key-1", &RUNNER_KEY),
        ("att-2", "ci-second", 3, "runner-key-2", &SECOND_KEY),
    ] {
        let mut att = base.clone();
        for (name, v) in [
            ("contract_version", json::s("runner-attestation/2")),
            ("attestation_id", json::s(id)),
            ("evaluator_id", json::s(evaluator)),
            ("registry_revision", Value::Int(rev)),
            ("attempt_id", json::s("attempt-1")),
            ("verdict", json::s("pass")),
        ] {
            att = set(&att, &[name], v);
        }
        att = set(&att, &["auth", "key_id"], json::s(key_id));
        let att = sign(&att, key);
        attempt
            .members
            .insert(id.into(), json::digest(&att).unwrap());
        w.docs.put(id, att);
    }
    (w, policy, attempt)
}

fn promote(
    w: &W,
    policy: &Policy,
    attempt: &Attempt,
    request: &Value,
) -> Result<PromotionOutcome, Refusal> {
    let records = Records {
        docs: &w.docs,
        attempt,
    };
    let trust = Trust {
        records: &records,
        ..w.trust()
    };
    w.broker()
        .with_quorum_policy(policy)
        .promote(&trust, &verify(request), None)
        .result
}

fn untouched(w: &W) {
    unchanged(w);
    assert_eq!(w.one("SELECT count(*) FROM quorum_promotions"), 0);
}

#[test]
fn quorum_binds_complete_signed_bytes_and_recovers_exact_receipt_after_reopen() {
    let (w, policy, attempt) = setup("receipt");
    let req = accept("k1", 1);
    for bad in [0, 1, 2] {
        let mut changed = attempt.clone();
        match bad {
            0 => {
                changed
                    .members
                    .insert("omitted".into(), changed.digest.clone());
            }
            1 => {
                changed
                    .members
                    .insert("att-2".into(), changed.digest.clone());
            }
            _ => {
                changed.digest = Digest::of_bytes(b"other signed record");
            }
        }
        assert!(promote(&w, &policy, &changed, &req).is_err());
        untouched(&w);
    }
    assert_eq!(promote(&w, &policy, &attempt, &req), Ok(committed(2)));
    assert_eq!(w.one("SELECT count(*) FROM consumed_attestations"), 2);
    assert_eq!(
        w.text("SELECT member_ids FROM quorum_promotions"),
        r#"["att-1","att-2"]"#
    );
    assert_eq!(
        w.text("SELECT record_sha FROM quorum_promotions"),
        attempt.digest.as_str()
    );
    let digests = Value::Obj(
        attempt
            .members
            .iter()
            .map(|(id, digest)| (id.clone(), json::s(digest.as_str())))
            .collect(),
    );
    assert_eq!(
        w.text("SELECT member_digests FROM quorum_promotions"),
        json::canonical(&digests).unwrap()
    );
    let dir = w.dir.clone();
    drop(w);
    let ledger = SqliteLedger::open_quorum_lab(&dir.join("ledger.db")).unwrap();
    let effects = SqliteEffectStore::open(&dir.join("effects.db"), ledger.ledger_id()).unwrap();
    let reopened = W::with_stores(dir, ledger, effects);
    assert_eq!(
        reopened.promote(&req),
        Ok(PromotionOutcome::Replayed {
            revision: 2,
            receipt_id: "promotion:wf-1:2".into()
        })
    );
    assert_eq!(
        reopened.promote(&accept("k1", 2)),
        Err(Refusal::IdempotencyConflict)
    );
    assert_eq!(reopened.one("SELECT count(*) FROM quorum_promotions"), 1);
}

#[test]
fn quorum_rolls_back_partial_writes_and_rechecks_durable_revocation() {
    let (w, policy, attempt) = setup("rollback");
    let req = accept("k1", 1);
    for (stage, code, _) in STAGES {
        w.ledger.inject("promote", stage, code);
        assert!(matches!(
            promote(&w, &policy, &attempt, &req),
            Err(Refusal::LedgerUnavailable(_))
        ));
        untouched(&w);
    }
    w.ledger.db().conn.execute_batch(
        "CREATE TRIGGER stop_second_member BEFORE INSERT ON consumed_attestations
         WHEN EXISTS (SELECT 1 FROM consumed_attestations WHERE acceptance_record_id=NEW.acceptance_record_id)
         BEGIN SELECT RAISE(ABORT,'second consumption unavailable'); END;"
    ).unwrap();
    assert!(matches!(
        promote(&w, &policy, &attempt, &req),
        Err(Refusal::LedgerUnavailable(_))
    ));
    untouched(&w);
    w.ledger
        .db()
        .conn
        .execute_batch("DROP TRIGGER stop_second_member")
        .unwrap();
    w.ledger.db().conn.execute_batch("CREATE TRIGGER stop_quorum_audit BEFORE INSERT ON quorum_promotions BEGIN SELECT RAISE(ABORT,'audit unavailable'); END;").unwrap();
    assert!(matches!(
        promote(&w, &policy, &attempt, &req),
        Err(Refusal::LedgerUnavailable(_))
    ));
    untouched(&w);
    w.ledger
        .db()
        .conn
        .execute_batch("DROP TRIGGER stop_quorum_audit")
        .unwrap();
    revoke_material(
        &w,
        writ_broker::keys::KeyPurpose::RunnerAttestation,
        &SECOND_KEY,
    );
    assert_eq!(
        promote(&w, &policy, &attempt, &req),
        Err(acceptance(AcceptanceCode::AttestationUnauthenticated))
    );
    untouched(&w);
}

struct SecondKeyGate<'a> {
    keys: &'a MemoryKeys<RunnerAttestationKey>,
    gate: &'a OperationGate,
    reads: AtomicUsize,
    pause_at: usize,
}

impl RunnerKeyRing for SecondKeyGate<'_> {
    fn runner_key(&self, id: &str) -> Result<KeyStatus<'_, RunnerAttestationKey>, KeysUnavailable> {
        if id == "runner-key-2" && self.reads.fetch_add(1, Ordering::SeqCst) == self.pause_at {
            self.gate.open();
        }
        self.keys.runner_key(id)
    }
}

#[test]
fn quorum_key_revocation_orders_with_concurrent_promotion() {
    for pause_at in [0, 1] {
        let (w, policy, attempt) = setup(&format!("key-race-{pause_at}"));
        let (entered, waiting) = mpsc::channel();
        let (release, released) = mpsc::channel();
        let gate = OperationGate {
            entered,
            release: Mutex::new(released),
        };
        let keys = SecondKeyGate {
            keys: &w.runner_keys,
            gate: &gate,
            reads: AtomicUsize::new(0),
            pause_at,
        };
        let (started, contending) = mpsc::channel();
        let (finished, completed) = mpsc::channel();
        let result = thread::scope(|scope| {
            let promotion = scope.spawn(|| {
                let records = Records {
                    docs: &w.docs,
                    attempt: &attempt,
                };
                let trust = Trust {
                    records: &records,
                    runner_keys: &keys,
                    ..w.trust()
                };
                w.broker()
                    .with_quorum_policy(&policy)
                    .promote(&trust, &verify(&accept("k1", 1)), None)
                    .result
            });
            waiting.recv_timeout(Duration::from_secs(10)).unwrap();
            let revocation = scope.spawn(|| {
                started.send(()).unwrap();
                revoke_material(
                    &w,
                    writ_broker::keys::KeyPurpose::RunnerAttestation,
                    &SECOND_KEY,
                );
                finished.send(()).unwrap();
            });
            contending.recv_timeout(Duration::from_secs(10)).unwrap();
            if pause_at == 0 {
                completed.recv_timeout(Duration::from_secs(10)).unwrap();
            } else {
                assert_eq!(
                    completed.recv_timeout(Duration::from_millis(50)),
                    Err(mpsc::RecvTimeoutError::Timeout)
                );
            }
            release.send(()).unwrap();
            revocation.join().unwrap();
            promotion.join().unwrap()
        });
        if pause_at == 0 {
            assert_eq!(
                result,
                Err(acceptance(AcceptanceCode::AttestationUnauthenticated))
            );
            untouched(&w);
        } else {
            assert_eq!(result, Ok(committed(2)));
            assert!(w.one("SELECT seq FROM ledger_sequence WHERE kind='promotion' AND reference='promotion:wf-1:2'") < w.one("SELECT ledger_seq FROM key_revocations"));
            assert_eq!(w.one("SELECT count(*) FROM consumed_attestations"), 2);
        }
    }
}

struct ExpiresAtCommit<'a> {
    policy: &'a Policy,
    reads: AtomicUsize,
}

impl QuorumPolicyGuard for ExpiresAtCommit<'_> {
    fn current(
        &self,
        tenant: &str,
        project: &str,
        at: Timestamp,
    ) -> Result<&QuorumRequirements, Refusal> {
        if self.reads.fetch_add(1, Ordering::SeqCst) >= 2 {
            return Err(Refusal::AcceptancePolicyUnavailable);
        }
        self.policy.current(tenant, project, at)
    }
}

#[test]
fn quorum_refuses_unavailable_policy_at_commit_and_substituted_attestation_attempt() {
    let (w, policy, mut attempt) = setup("policy");
    let records = Records {
        docs: &w.docs,
        attempt: &attempt,
    };
    let trust = Trust {
        records: &records,
        ..w.trust()
    };
    let guard = ExpiresAtCommit {
        policy: &policy,
        reads: AtomicUsize::new(0),
    };
    assert_eq!(
        w.broker()
            .with_quorum_policy(&guard)
            .promote(&trust, &verify(&accept("k1", 1)), None)
            .result,
        Err(Refusal::AcceptancePolicyUnavailable)
    );
    untouched(&w);
    let original = w.docs.get("att-2").unwrap().unwrap();
    let changed = sign(
        &set(&original, &["attempt_id"], json::s("old-attempt")),
        &SECOND_KEY,
    );
    attempt
        .members
        .insert("att-2".into(), json::digest(&changed).unwrap());
    w.docs.put("att-2", changed);
    assert_eq!(
        promote(&w, &policy, &attempt, &accept("k1", 1)),
        Err(acceptance(AcceptanceCode::AttestationBindingMismatch))
    );
    untouched(&w);
}

struct FinalRecordKey<'a> {
    original: &'a MemoryKeys<AcceptanceRecordKey>,
    alternate: Option<AcceptanceRecordKey>,
    reads: AtomicUsize,
}

impl writ_broker::keys::AcceptanceKeyRing for FinalRecordKey<'_> {
    fn record_key(&self, id: &str) -> Result<KeyStatus<'_, AcceptanceRecordKey>, KeysUnavailable> {
        if self.reads.fetch_add(1, Ordering::SeqCst) == 0 {
            return self.original.record_key(id);
        }
        self.alternate
            .as_ref()
            .map(KeyStatus::Active)
            .ok_or(KeysUnavailable)
    }
}

#[test]
fn quorum_record_key_and_evidence_time_must_remain_current_at_commit() {
    let (w, policy, attempt) = setup("key-time");
    let records = Records {
        docs: &w.docs,
        attempt: &attempt,
    };
    for alternate in [
        None,
        Some(AcceptanceRecordKey::from_bytes(vec![0x78; 32]).unwrap()),
    ] {
        let expected = if alternate.is_some() {
            AcceptanceCode::RecordUnauthenticated
        } else {
            AcceptanceCode::KeyUnavailable
        };
        let keys = FinalRecordKey {
            original: &w.record_keys,
            alternate,
            reads: AtomicUsize::new(0),
        };
        let trust = Trust {
            records: &records,
            record_keys: &keys,
            ..w.trust()
        };
        assert_eq!(
            w.broker()
                .with_quorum_policy(&policy)
                .promote(&trust, &verify(&accept("k1", 1)), None)
                .result,
            Err(acceptance(expected))
        );
        untouched(&w);
    }
    struct AdvanceAtCheck<'a> {
        policy: &'a Policy,
        clock: &'a TestClock,
    }
    impl QuorumPolicyGuard for AdvanceAtCheck<'_> {
        fn current(&self, t: &str, p: &str, at: Timestamp) -> Result<&QuorumRequirements, Refusal> {
            self.clock.set(ts("2026-11-07T11:31:00Z"));
            self.policy.current(t, p, at)
        }
    }
    let guard = AdvanceAtCheck {
        policy: &policy,
        clock: &w.clock,
    };
    let trust = Trust {
        records: &records,
        ..w.trust()
    };
    assert!(w
        .broker()
        .with_quorum_policy(&guard)
        .promote(&trust, &verify(&accept("k1", 1)), None)
        .result
        .is_err());
    untouched(&w);
}

#[test]
fn quorum_evaluator_revocation_order_controls_protected_promotion() {
    for revoke_first in [true, false] {
        let (w, policy, attempt) = setup(&format!("evaluator-order-{revoke_first}"));
        let publishers = Publishers::default();
        let revoke = EvaluatorIntent::new(
            3,
            AdminRequest::RevokeEvaluator {
                tenant_id: T.into(),
                project_id: P.into(),
                evaluator_id: "ci-second".into(),
            },
        )
        .unwrap();
        publishers.approve_fixture(&revoke);
        if revoke_first {
            submit(&w, &publishers, &revoke).unwrap();
            assert!(promote(&w, &policy, &attempt, &accept("k1", 1)).is_err());
            untouched(&w);
        } else {
            assert_eq!(
                promote(&w, &policy, &attempt, &accept("k1", 1)),
                Ok(committed(2))
            );
            submit(&w, &publishers, &revoke).unwrap();
            let last = w.text("SELECT kind FROM ledger_sequence ORDER BY seq DESC LIMIT 1");
            assert_eq!(last, "administration");
            assert_eq!(w.one("SELECT count(*) FROM quorum_promotions"), 1);
        }
    }
}

#[test]
fn quorum_rejects_alternative_signed_bytes_for_same_id_and_inconsistent_manifest() {
    let (w, policy, attempt) = setup("manifest");
    let original = w.docs.get("att-2").unwrap().unwrap();
    let oversized = set(
        &original,
        &["contract_version"],
        json::s("runner-attestation/1"),
    );
    let oversized = set(
        &oversized,
        &["output_digest"],
        Value::Str("x".repeat(64 * 1024)),
    );
    w.docs.put("att-2", oversized);
    assert_eq!(
        promote(&w, &policy, &attempt, &accept("k1", 1)),
        Err(acceptance(AcceptanceCode::AttestationMalformed))
    );
    untouched(&w);
    let changed = sign(
        &set(&original, &["output_digest"], json::s(&sha('f'))),
        &SECOND_KEY,
    );
    w.docs.put("att-2", changed);
    assert_eq!(
        promote(&w, &policy, &attempt, &accept("k1", 1)),
        Err(acceptance(AcceptanceCode::AttestationBindingMismatch))
    );
    untouched(&w);
    w.docs.put("att-2", original);

    struct Varying<'a> {
        attempt: &'a Attempt,
        wrong: BTreeMap<String, Digest>,
        reads: AtomicUsize,
    }
    impl CurrentAttempt for &Varying<'_> {
        fn record_id(&self) -> &str {
            self.attempt.record_id()
        }
        fn attempt_id(&self) -> &str {
            self.attempt.attempt_id()
        }
        fn record_digest(&self) -> &Digest {
            &self.attempt.digest
        }
        fn members(&self) -> &BTreeMap<String, Digest> {
            if self.reads.fetch_add(1, Ordering::SeqCst) == 0 {
                &self.wrong
            } else {
                &self.attempt.members
            }
        }
    }
    struct Source<'a> {
        docs: &'a Docs,
        manifest: Varying<'a>,
    }
    impl AcceptanceRecordSource for Source<'_> {
        fn record(&self, t: &str, p: &str, id: &str) -> Result<Option<Value>, SourceUnavailable> {
            self.docs.record(t, p, id)
        }
        fn current_attempt(
            &self,
            _: &AttemptScope<'_>,
        ) -> Result<Option<Box<dyn CurrentAttempt + '_>>, SourceUnavailable> {
            Ok(Some(Box::new(&self.manifest)))
        }
    }
    let mut wrong = attempt.members.clone();
    wrong.insert(
        "att-2".into(),
        Digest::of_bytes(b"different authenticated result"),
    );
    let source = Source {
        docs: &w.docs,
        manifest: Varying {
            attempt: &attempt,
            wrong,
            reads: AtomicUsize::new(0),
        },
    };
    let trust = Trust {
        records: &source,
        ..w.trust()
    };
    assert_eq!(
        w.broker()
            .with_quorum_policy(&policy)
            .promote(&trust, &verify(&accept("k1", 1)), None)
            .result,
        Err(acceptance(AcceptanceCode::AttestationBindingMismatch))
    );
    assert_eq!(source.manifest.reads.load(Ordering::SeqCst), 1);
    untouched(&w);
}
