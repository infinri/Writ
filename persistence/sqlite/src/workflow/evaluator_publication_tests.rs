#![allow(clippy::unwrap_used, clippy::panic)]

use std::cell::Cell;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicI64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use writ_broker::clock::{timestamp_from_parts, ClockError, EffectiveClock, FixedClock, Reading};
use writ_broker::evaluator_publication::{
    CurrentEvaluatorPublisher, EvaluatorDependencies, EvaluatorImplementation,
    EvaluatorImplementations, EvaluatorIntent, EvaluatorPublisher, ManagedEvaluatorAuthorization,
    PublisherApproval,
};
use writ_broker::execute::Broker;
use writ_broker::keys::{MemoryKeys, PermitKey, RunnerAttestationKey};
use writ_broker::policy::{ActivePolicy, PolicyState};
use writ_broker::runner::{CommandRegistry, RegisteredCommand};
use writ_broker::store::TxStage;
use writ_broker::workflow::{
    AdminPresentation, AdminRefusal, AuthorizedOperator, SourceUnavailable,
};
use writ_broker::writ_kernel::contracts::CriterionClass;
use writ_broker::writ_kernel::time::Timestamp;

use crate::{fault, SqliteEffectStore};

use super::*;

const TENANT: &str = "t1";
const PROJECT: &str = "p1";
const EVALUATOR: &str = "doc-check";

fn now() -> Timestamp {
    Timestamp::parse_strict("2026-11-01T09:00:00Z").unwrap()
}

fn publish(revision: i64) -> EvaluatorIntent {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../docs/contracts/proposed/examples/evaluator-registry-entry.valid.json");
    let original = json::parse(&std::fs::read(path).unwrap()).unwrap();
    let Value::Obj(mut fields) = original else {
        panic!("fixture must be an object");
    };
    fields.remove("command");
    fields.insert("evaluator_id".into(), json::s(EVALUATOR));
    fields.insert("class".into(), json::s("evidence"));
    fields.insert("trust".into(), json::s("E"));
    fields.insert("registry_revision".into(), Value::Int(revision + 1));
    EvaluatorIntent::new(
        revision,
        AdminRequest::RegisterEvaluator {
            entry: Value::Obj(fields),
        },
    )
    .unwrap()
}

fn revoke(revision: i64) -> EvaluatorIntent {
    EvaluatorIntent::new(
        revision,
        AdminRequest::RevokeEvaluator {
            tenant_id: TENANT.into(),
            project_id: PROJECT.into(),
            evaluator_id: EVALUATOR.into(),
        },
    )
    .unwrap()
}

struct Authority<'a> {
    intent: &'a str,
    ceremony: &'a str,
    calls: Cell<usize>,
    commit: CommitControl,
    clock: &'a TestClock,
}

#[derive(Clone, Copy)]
enum CommitControl {
    Allowed,
    Denied,
    Expired,
}

#[derive(Default)]
struct TestClock(AtomicI64);

impl EffectiveClock for TestClock {
    fn read(&self) -> Result<Reading, ClockError> {
        let seconds = now().unix_seconds() + self.0.load(Ordering::Relaxed);
        FixedClock::new(timestamp_from_parts(seconds, 250_000_000).unwrap()).read()
    }
}

impl EvaluatorPublisher for Authority<'_> {
    fn current(&self) -> Result<impl CurrentEvaluatorPublisher + '_, AdminRefusal> {
        Ok(self)
    }
}

impl CurrentEvaluatorPublisher for &Authority<'_> {
    fn approve(
        &self,
        intent: &EvaluatorIntent,
        presentation: &AdminPresentation<'_>,
        _: Timestamp,
    ) -> Result<PublisherApproval, AdminRefusal> {
        if presentation.credential != b"fixture-proof" || intent.fingerprint() != self.intent {
            return Err(AdminRefusal::Unauthenticated);
        }
        let call = self.calls.replace(self.calls.get() + 1);
        if call != 0 {
            match self.commit {
                CommitControl::Allowed => (),
                CommitControl::Denied => return Err(AdminRefusal::Unauthorized),
                CommitControl::Expired => self.clock.0.store(10, Ordering::Relaxed),
            }
        }
        Ok(PublisherApproval {
            operator: AuthorizedOperator {
                id: "alice".into(),
                issuer: "fixture-issuer".into(),
                channel: "fixture-publisher".into(),
                evidence_ref: "fixture-event".into(),
            },
            ceremony: self.ceremony.into(),
            issued_at: now(),
            expires_at: now().plus_seconds(10),
        })
    }
}

struct Dependencies;

impl CommandRegistry for Dependencies {
    fn command(
        &self,
        _: &str,
        _: &str,
        _: &str,
    ) -> Result<Option<RegisteredCommand>, SourceUnavailable> {
        Ok(None)
    }
}

impl EvaluatorImplementations for Dependencies {
    fn get(
        &self,
        tenant: &str,
        project: &str,
        evaluator: &str,
        version: &str,
    ) -> Result<Option<EvaluatorImplementation>, SourceUnavailable> {
        Ok(
            ((tenant, project, evaluator, version) == (TENANT, PROJECT, EVALUATOR, "1.4.0")).then(
                || EvaluatorImplementation {
                    class: CriterionClass::Evidence,
                    command: None,
                    implementation_digest: format!("sha256:{}", "1".repeat(64)),
                    environment_digest: format!("sha256:{}", "2".repeat(64)),
                    attestation_key_id: "runner-key-1".into(),
                },
            ),
        )
    }
}

struct Lab {
    dir: PathBuf,
}

impl Lab {
    fn new() -> (Self, SqliteLedger) {
        let dir = std::env::temp_dir().join(format!(
            "writ-m6-evaluator-storage-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir(&dir).unwrap();
        let lab = Self { dir };
        let ledger = SqliteLedger::create(&lab.dir.join("ledger.db")).unwrap();
        (lab, ledger)
    }

    fn reopen(&self) -> SqliteLedger {
        SqliteLedger::open(&self.dir.join("ledger.db")).unwrap()
    }

    fn mutate(
        &self,
        ledger: &SqliteLedger,
        intent: &EvaluatorIntent,
        ceremony: &str,
        commit: CommitControl,
    ) -> Result<Result<(), Refusal>, TxFailure> {
        let clock = TestClock::default();
        let authority = Authority {
            intent: intent.fingerprint(),
            ceremony,
            calls: Cell::new(0),
            commit,
            clock: &clock,
        };
        let presentation = AdminPresentation {
            channel: "fixture-publisher",
            operator: "alice",
            credential: b"fixture-proof",
        };
        let active = ActivePolicy::new(PolicyState::Absent);
        let key = PermitKey::from_bytes(vec![0x13; 32]).unwrap();
        let effects = self.dir.join("effects.db");
        let effects = if effects.exists() {
            SqliteEffectStore::open(&effects, ledger.ledger_id())
        } else {
            SqliteEffectStore::create(&effects, ledger.ledger_id())
        }
        .unwrap();
        let mut runner_keys = MemoryKeys::new();
        runner_keys.register(
            "runner-key-1",
            RunnerAttestationKey::from_bytes(vec![0x2d; 32]).unwrap(),
        );
        let correlation = attempt(ceremony);
        Broker::new(&active, &clock, ledger, &effects, &key).publish_evaluator_managed(
            &authority,
            &presentation,
            &EvaluatorDependencies {
                implementations: &Dependencies,
                commands: &Dependencies,
                runner_keys: &runner_keys,
            },
            intent,
            &ManagedEvaluatorAuthorization {
                correlation: &correlation,
                reauthorize: &|| Ok(()),
            },
        )
    }
}

impl Drop for Lab {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

fn attempt(ceremony: &str) -> Digest {
    Digest::of_bytes(format!("managed-attempt:{ceremony}").as_bytes())
}

fn view(ledger: &SqliteLedger) -> (i64, Option<RegistryEntry>) {
    ledger
        .evaluator_registry_view(TENANT, PROJECT, EVALUATOR)
        .unwrap()
}

#[test]
fn publisher_recheck_and_required_audit_fail_atomically() {
    let (lab, ledger) = Lab::new();
    let intent = publish(0);
    assert_eq!(
        lab.mutate(&ledger, &intent, "refused", CommitControl::Denied),
        Ok(Err(Refusal::OperatorUnauthorized))
    );
    assert_eq!(view(&ledger), (0, None));
    assert_eq!(
        lab.mutate(&ledger, &intent, "expired", CommitControl::Expired),
        Ok(Err(Refusal::OperatorUnauthorized))
    );
    assert_eq!(view(&ledger), (0, None));

    // Readable audit metadata with a failed write, on the disposable test connection only.
    ledger
        .db()
        .conn
        .execute_batch("CREATE TEMP VIEW admin_events AS SELECT * FROM main.admin_events")
        .unwrap();
    let failed = lab
        .mutate(&ledger, &intent, "audit", CommitControl::Allowed)
        .unwrap_err();
    assert_eq!(failed.stage, TxStage::Body);
    assert!(!failed.outcome_unknown);
    ledger
        .db()
        .conn
        .execute_batch("DROP VIEW temp.admin_events")
        .unwrap();
    assert_eq!(view(&ledger), (0, None));
    assert_eq!(
        ledger.evaluator_publication_commit(TENANT, PROJECT, intent.fingerprint()),
        Ok(None)
    );
    assert_eq!(
        lab.mutate(&ledger, &intent, "audit", CommitControl::Allowed),
        Ok(Ok(()))
    );
    assert_eq!(view(&ledger).0, 1);
}

#[test]
fn recovery_reconciles_both_possible_outcomes_of_an_uncertain_commit() {
    let (lab, ledger) = Lab::new();
    let publication = publish(0);
    assert_eq!(
        lab.mutate(&ledger, &publication, "publish", CommitControl::Allowed),
        Ok(Ok(()))
    );
    let intent = revoke(1);
    ledger.inject("admin", fault::Stage::Body, rusqlite::ffi::SQLITE_IOERR);
    let body = lab
        .mutate(&ledger, &intent, "revoke", CommitControl::Allowed)
        .unwrap_err();
    assert_eq!(body.stage, TxStage::Body);
    assert!(!body.outcome_unknown);
    assert_eq!(view(&ledger).0, 1);
    ledger.inject("admin", fault::Stage::Commit, rusqlite::ffi::SQLITE_IOERR);
    let uncertain = lab
        .mutate(&ledger, &intent, "revoke", CommitControl::Allowed)
        .unwrap_err();
    assert_eq!(uncertain.stage, TxStage::Commit);
    assert!(uncertain.outcome_unknown);
    drop(ledger);

    let ledger = lab.reopen();
    assert_eq!(view(&ledger).0, 1);
    assert_eq!(
        ledger.evaluator_publication_commit(TENANT, PROJECT, intent.fingerprint()),
        Ok(None)
    );
    assert_eq!(
        lab.mutate(&ledger, &intent, "publish", CommitControl::Allowed),
        Ok(Err(Refusal::OperatorUnauthorized)),
        "a committed ceremony cannot be reused for another intent"
    );
    // Discard a successful result to model loss of the acknowledgment after commit.
    let _acknowledgment = lab.mutate(&ledger, &intent, "revoke", CommitControl::Allowed);
    drop(ledger);
    let ledger = lab.reopen();
    let outcome = ledger
        .evaluator_publication_commit(TENANT, PROJECT, intent.fingerprint())
        .unwrap()
        .unwrap();
    let original = ledger
        .evaluator_publication_commit(TENANT, PROJECT, publication.fingerprint())
        .unwrap()
        .unwrap();
    assert_eq!(outcome.revision, 2);
    assert!(outcome.sequence > original.sequence);
    let managed = ledger
        .evaluator_management_receipt(
            TENANT,
            PROJECT,
            intent.fingerprint(),
            attempt("revoke").as_str(),
        )
        .unwrap()
        .unwrap();
    assert_eq!(managed.sequence, outcome.sequence);
    assert_eq!(
        ledger.evaluator_management_receipt(
            TENANT,
            PROJECT,
            intent.fingerprint(),
            attempt("another-attempt").as_str(),
        ),
        Ok(None)
    );
    let (revision, entry) = view(&ledger);
    assert_eq!(revision, 2);
    let entry = entry.unwrap();
    assert_eq!(entry.as_value().get("status"), Some(&json::s("revoked")));
    assert_eq!(
        entry.as_value().get("registry_revision"),
        Some(&Value::Int(2))
    );
    assert_eq!(
        entry.as_value().get("revoked_at"),
        Some(&json::s(&now().to_string()))
    );
    assert_eq!(
        lab.mutate(&ledger, &intent, "fresh", CommitControl::Allowed),
        Ok(Err(Refusal::RegistryRevisionStale))
    );
}

#[test]
fn audit_loss_cannot_fall_back_to_legacy_registry() {
    let (lab, ledger) = Lab::new();
    let intent = publish(0);
    assert_eq!(
        lab.mutate(&ledger, &intent, "publication", CommitControl::Allowed),
        Ok(Ok(()))
    );
    ledger
        .db()
        .conn
        .execute_batch("DELETE FROM ledger_sequence; DELETE FROM admin_events;")
        .unwrap();
    assert!(ledger
        .evaluator_registry_view(TENANT, PROJECT, EVALUATOR)
        .is_err());
    assert!(ledger
        .registry_entry_for_runner(TENANT, PROJECT, EVALUATOR)
        .is_err());
    assert!(ledger
        .evaluator_publication_commit(TENANT, PROJECT, intent.fingerprint())
        .is_err());
    assert!(lab
        .mutate(&ledger, &revoke(1), "revocation", CommitControl::Allowed)
        .is_err());
}

#[test]
fn evaluator_receipt_rejects_a_copied_or_relocated_sequence_reference() {
    let (lab, ledger) = Lab::new();
    ledger
        .db()
        .conn
        .execute(
            "INSERT INTO ledger_sequence(kind, reference) VALUES ('administration', 'other-event')",
            [],
        )
        .unwrap();
    let intent = publish(0);
    assert_eq!(
        lab.mutate(&ledger, &intent, "publisher", CommitControl::Allowed),
        Ok(Ok(()))
    );
    let correlation = attempt("publisher");
    let receive = || {
        ledger.evaluator_management_receipt(
            TENANT,
            PROJECT,
            intent.fingerprint(),
            correlation.as_str(),
        )
    };
    let committed = receive().unwrap().unwrap();
    assert_eq!(committed.sequence, 2);
    ledger
        .db()
        .conn
        .execute(
            "UPDATE ledger_sequence SET reference =
            (SELECT reference FROM ledger_sequence WHERE seq = ?1) WHERE seq = 1",
            [committed.sequence],
        )
        .unwrap();
    assert!(receive().is_err());
    ledger
        .db()
        .conn
        .execute(
            "UPDATE ledger_sequence SET reference = 'other-event' WHERE seq = ?1",
            [committed.sequence],
        )
        .unwrap();
    assert!(receive().is_err());
}
