//! End-to-end D-051 tests over disposable test-authority stores.

#![allow(clippy::unwrap_used, clippy::panic)]

#[path = "../examples/support/mod.rs"]
#[allow(dead_code)]
mod support;

use rusqlite::{Connection, OpenFlags};
use std::{
    path::Path,
    sync::{mpsc, Barrier, Mutex},
    thread,
    time::Duration,
};
use support::*;
use writ_broker::{
    effects::{EffectAdapter, EffectEvidence, EffectResult, Verify},
    execute::Executed,
    refusal::Refusal,
    store::TxFailure,
};
use writ_kernel::json::{self, Value};
use writ_ledger_sqlite::{SqliteEffectStore, SqliteLedger};
use writ_policy_publication_lab::{Error, Operation, Publication, Purpose, Registry, Scope};

fn count(path: &Path, table: &str) -> i64 {
    let database = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY).unwrap();
    database
        .query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |row| {
            row.get(0)
        })
        .unwrap()
}

fn selected(registry: &Registry<FixtureAuthority>) -> (i64, i64) {
    registry
        .with_selection(|view| Ok((view.revision(), view.publication_version())))
        .unwrap()
}

fn publish(
    registry: &Registry<FixtureAuthority>,
    auth: &FixtureAuthority,
    publication: &Publication,
    expected: i64,
    version: i64,
    ceremony: &str,
) {
    let intent = intent(Operation::Publish, expected, version, publication);
    let approval = auth.authorize(&intent, ceremony);
    let commit = registry.publish(&intent, publication, &approval).unwrap();
    assert_eq!(
        (commit.revision, commit.publication_version),
        (expected + 1, version)
    );
}

fn broker<'r>(
    base: &Path,
    registry: &'r Registry<FixtureAuthority>,
) -> LabBroker<'r, FixtureAuthority> {
    let ledger = SqliteLedger::create(&base.join("ledger.sqlite")).unwrap();
    let effects =
        SqliteEffectStore::create(&base.join("effects.sqlite"), ledger.ledger_id()).unwrap();
    LabBroker::new(registry, ledger, effects)
}

fn edit(document: &Value, name: &str, value: Value) -> Value {
    let mut edited = document.as_obj().unwrap().clone();
    edited.insert(name.into(), value);
    Value::Obj(edited)
}

#[test]
fn verified_restart_loads_the_exact_pair_and_replays_effect_once() {
    let dir = Disposable::create().unwrap();
    let path = dir.path().join("policy.sqlite");
    let (authority, signer) = FixtureSigner::pair();
    let allow = signer.publication(ALLOW, 1);
    let first = intent(Operation::Publish, 0, 1, &allow);
    let registry = Registry::create(&path, scope(), authority.clone()).unwrap();
    assert_eq!(registry.with_selection(|_| Ok(())), Err(Error::Recovery));
    publish(&registry, &authority, &allow, 0, 1, "first");
    drop(registry);
    drop(signer);

    let registry = Registry::open(&path, scope(), authority).unwrap();
    registry
        .with_selection(|view| {
            assert_eq!(view.publication(), &allow);
            assert_eq!((view.revision(), view.publication_version()), (1, 1));
            assert_eq!(
                view.acceptance().policy.acceptance_policy_bundle_sha,
                json::digest(&allow.acceptance.payload).unwrap().as_str()
            );
            assert_eq!(view.acceptance().criteria[0].criterion_id, "review");
            Ok(())
        })
        .unwrap();
    let audit = registry.audit().unwrap();
    assert_eq!(audit.len(), 1);
    assert_eq!(
        audit[0].intent_digest,
        first.fingerprint().unwrap().as_str()
    );
    assert_eq!(
        audit[0].publication_digest,
        allow.fingerprint().unwrap().as_str()
    );
    assert_eq!(audit[0].human, "lab-human-publisher");

    let broker = broker(dir.path(), &registry);
    let request = request("valid-one");
    let (result, permit) = broker.execute(&request, None).unwrap();
    assert_eq!(result, Ok(Executed::EffectCompleted));
    let permit = permit.unwrap();
    assert!(broker.effects.evidence(permit.id()).unwrap().is_some());
    assert_eq!(
        broker.present(&permit, request.request()),
        Ok(Refusal::PermitReplayed)
    );
    assert!(matches!(
        broker.execute(&request, None).unwrap().0,
        Ok(Executed::Replayed(_))
    ));
    assert_eq!(count(&dir.path().join("effects.sqlite"), "effects"), 1);
    assert_eq!(
        count(&dir.path().join("effects.sqlite"), "consumed_permits"),
        1
    );
    assert_eq!(count(&dir.path().join("ledger.sqlite"), "receipts"), 1);
}

#[test]
fn publication_requires_current_scoped_human_and_exact_approved_pair() {
    let dir = Disposable::create().unwrap();
    let (authority, signer) = FixtureSigner::pair();
    let registry = Registry::create(
        &dir.path().join("policy.sqlite"),
        scope(),
        authority.clone(),
    )
    .unwrap();
    let candidate = signer.publication(ALLOW, 1);
    let intended = intent(Operation::Publish, 0, 1, &candidate);
    assert_eq!(
        registry.publish(&intended, &candidate, b"os-operator"),
        Err(Error::Authority)
    );
    let proof = authority.authorize(&intended, "exact-approval");
    authority.publisher_enabled(false);
    assert_eq!(
        registry.publish(&intended, &candidate, &proof),
        Err(Error::Authority)
    );
    authority.publisher_enabled(true);

    let mut mixed = candidate.clone();
    mixed.acceptance = signer.sign_policy(
        Purpose::Acceptance,
        edit(
            &candidate.acceptance.payload,
            "criteria",
            Value::Arr(vec![
                json::obj([
                    ("criterion_id", json::s("review")),
                    ("required_class", json::s("human")),
                ]),
                json::obj([
                    ("criterion_id", json::s("build")),
                    ("required_class", json::s("mechanical")),
                ]),
            ]),
        ),
    );
    let mixed_intent = intent(Operation::Publish, 0, 1, &mixed);
    assert_eq!(
        registry.publish(&intended, &mixed, &proof),
        Err(Error::Authority)
    );
    assert_eq!(
        registry.publish(&mixed_intent, &mixed, &proof),
        Err(Error::Authority)
    );
    let wrong_scope = writ_policy_publication_lab::Intent::new(
        Scope::new("m6-lab", "offline").unwrap(),
        Operation::Publish,
        0,
        1,
        &candidate,
    )
    .unwrap();
    assert_eq!(
        registry.publish(&wrong_scope, &candidate, &proof),
        Err(Error::Scope)
    );
    let stale = authority.authorize_dates(
        &intended,
        "expired-approval",
        now().plus_seconds(-60),
        now(),
    );
    assert_eq!(
        registry.publish(&intended, &candidate, &stale),
        Err(Error::Authority)
    );
    assert!(registry.audit().unwrap().is_empty());
    assert_eq!(count(&dir.path().join("policy.sqlite"), "publications"), 0);

    registry.publish(&intended, &candidate, &proof).unwrap();
    let next = signer.publication(DENY, 2);
    let next_intent = intent(Operation::Publish, 1, 2, &next);
    let reused = authority.authorize(&next_intent, "exact-approval");
    assert_eq!(
        registry.publish(&next_intent, &next, &reused),
        Err(Error::Replay)
    );
    assert_eq!(selected(&registry), (1, 1));
    assert_eq!(registry.audit().unwrap().len(), 1);
}

#[test]
fn fixtures_must_prove_the_exact_bundle_and_keep_mandatory_requirements() {
    let dir = Disposable::create().unwrap();
    let (authority, signer) = FixtureSigner::pair();
    let registry = Registry::create(
        &dir.path().join("policy.sqlite"),
        scope(),
        authority.clone(),
    )
    .unwrap();
    let base = signer.publication(ALLOW, 1);

    let mut invalid = base.clone();
    invalid.prerequisites = edit(
        &base.prerequisites,
        "proof_digest",
        json::s(&format!("sha256:{}", "0".repeat(64))),
    );
    let mut weakened = base.clone();
    weakened.acceptance = signer.sign_policy(
        Purpose::Acceptance,
        edit(
            &base.acceptance.payload,
            "criteria",
            Value::Arr(vec![json::obj([
                ("criterion_id", json::s("review")),
                ("required_class", json::s("mechanical")),
            ])]),
        ),
    );
    let mut wrong_draft = base.clone();
    wrong_draft.prerequisites = signer.publication(DENY, 1).prerequisites;
    for (n, publication) in [invalid, weakened, wrong_draft].iter().enumerate() {
        let attempt = intent(Operation::Publish, 0, 1, publication);
        let proof = authority.authorize(&attempt, &format!("invalid-{n}"));
        assert_eq!(
            registry.publish(&attempt, publication, &proof),
            Err(Error::InvalidPolicy)
        );
    }
    assert!(registry.audit().unwrap().is_empty());
}

#[test]
fn unavailable_clock_keys_and_revocation_block_use_and_reopen() {
    let dir = Disposable::create().unwrap();
    let path = dir.path().join("policy.sqlite");
    let (authority, signer) = FixtureSigner::pair();
    let registry = Registry::create(&path, scope(), authority.clone()).unwrap();
    let candidate = signer.publication(ALLOW, 1);
    let attempt = intent(Operation::Publish, 0, 1, &candidate);
    let proof = authority.authorize(&attempt, "publish-one");
    authority.trust_available(false);
    assert_eq!(
        registry.publish(&attempt, &candidate, &proof),
        Err(Error::Trust)
    );
    authority.trust_available(true);
    authority.time_available(false);
    assert_eq!(
        registry.publish(&attempt, &candidate, &proof),
        Err(Error::Time)
    );
    authority.time_available(true);
    registry.publish(&attempt, &candidate, &proof).unwrap();
    let broker = broker(dir.path(), &registry);
    authority.trust_available(false);
    assert!(matches!(
        broker.execute(&request("trust-down"), None),
        Err(Error::Trust)
    ));
    authority.trust_available(true);
    authority.set_key_expiry(KEY_ACCEPTANCE, now().plus_seconds(2));
    authority.advance_to(2);
    assert!(matches!(
        broker.execute(&request("trust-expired"), None),
        Err(Error::Time)
    ));
    drop(broker);
    drop(registry);
    assert!(matches!(
        Registry::open(&path, scope(), authority.clone()),
        Err(Error::Time)
    ));
    authority.revoke(KEY_ACCEPTANCE);
    assert!(matches!(
        Registry::open(&path, scope(), authority.clone()),
        Err(Error::Trust)
    ));
    assert_eq!(count(&path, "publication_audit"), 1);
    assert_eq!(
        count(&dir.path().join("effects.sqlite"), "consumed_permits"),
        0
    );
    assert_eq!(count(&dir.path().join("ledger.sqlite"), "receipts"), 0);
}

#[test]
fn competing_publications_commit_one_exact_view() {
    let dir = Disposable::create().unwrap();
    let (authority, signer) = FixtureSigner::pair();
    let registry = Registry::create(
        &dir.path().join("policy.sqlite"),
        scope(),
        authority.clone(),
    )
    .unwrap();
    let allow = signer.publication(ALLOW, 1);
    let deny = signer.publication(DENY, 1);
    let barrier = Barrier::new(2);
    let selected_digest = thread::scope(|threads| {
        let run = |publication: &Publication, ceremony| {
            let candidate = intent(Operation::Publish, 0, 1, publication);
            let approval = authority.authorize(&candidate, ceremony);
            barrier.wait();
            registry.publish(&candidate, publication, &approval)
        };
        let a_candidate = &allow;
        let b_candidate = &deny;
        let a = threads.spawn(move || run(a_candidate, "competition-one"));
        let b = threads.spawn(move || run(b_candidate, "competition-two"));
        match (a.join().unwrap(), b.join().unwrap()) {
            (Ok(_), Err(Error::Conflict)) => allow.fingerprint().unwrap(),
            (Err(Error::Conflict), Ok(_)) => deny.fingerprint().unwrap(),
            other => panic!("one winner required: {other:?}"),
        }
    });
    assert_eq!(
        registry.audit().unwrap()[0].publication_digest,
        selected_digest.as_str()
    );
    registry
        .with_selection(|view| {
            assert_eq!(view.publication().fingerprint()?, selected_digest);
            assert_eq!(view.revision(), 1);
            Ok(())
        })
        .unwrap();
}

#[test]
fn audit_or_head_write_failure_preserves_previous_durable_state() {
    let dir = Disposable::create().unwrap();
    let path = dir.path().join("policy.sqlite");
    let (authority, signer) = FixtureSigner::pair();
    let registry = Registry::create(&path, scope(), authority.clone()).unwrap();
    let first = signer.publication(ALLOW, 1);
    publish(&registry, &authority, &first, 0, 1, "first");
    let second = signer.publication(DENY, 2);
    let attempt = intent(Operation::Publish, 1, 2, &second);
    let proof = authority.authorize(&attempt, "second");
    let connection = Connection::open(&path).unwrap();
    for action in ["INSERT ON publication_audit", "UPDATE ON registry_head"] {
        connection
            .execute_batch(&format!(
                "CREATE TRIGGER fault BEFORE {action}
                BEGIN SELECT RAISE(ABORT, 'injected failure'); END;"
            ))
            .unwrap();
        assert_eq!(
            registry.publish(&attempt, &second, &proof),
            Err(Error::Storage)
        );
        assert_eq!(selected(&registry), (1, 1));
        assert_eq!(registry.audit().unwrap().len(), 1);
        assert_eq!(count(&path, "publications"), 1);
        connection.execute_batch("DROP TRIGGER fault").unwrap();
    }
    drop(connection);
    drop(registry);
    let registry = Registry::open(&path, scope(), authority.clone()).unwrap();
    assert_eq!(selected(&registry), (1, 1));
    registry.publish(&attempt, &second, &proof).unwrap();
    drop(registry);
    let recovered = Registry::open(&path, scope(), authority).unwrap();
    assert_eq!(selected(&recovered), (2, 2));
    assert_eq!(recovered.audit().unwrap().len(), 2);
}

#[test]
fn downgrade_replay_rollback_and_retirement_are_ordered() {
    let dir = Disposable::create().unwrap();
    let path = dir.path().join("policy.sqlite");
    let (authority, signer) = FixtureSigner::pair();
    let registry = Registry::create(&path, scope(), authority.clone()).unwrap();
    let allow = signer.publication(ALLOW, 1);
    let deny = signer.publication(DENY, 2);
    publish(&registry, &authority, &allow, 0, 1, "publish-one");
    let broker = broker(dir.path(), &registry);
    let initial = request("prior-permit");
    let old_permit = broker.execute(&initial, None).unwrap().1.unwrap();
    publish(&registry, &authority, &deny, 1, 2, "publish-two");
    assert_eq!(
        broker.present(&old_permit, initial.request()),
        Err(Error::StalePermit)
    );
    let older = intent(Operation::Publish, 2, 1, &allow);
    let proof = authority.authorize(&older, "downgrade");
    assert_eq!(registry.publish(&older, &allow, &proof), Err(Error::Replay));
    let rewrapped = signer.publication(ALLOW, 3);
    let replay = intent(Operation::Publish, 2, 3, &rewrapped);
    let proof = authority.authorize(&replay, "rewrapped");
    assert_eq!(
        registry.publish(&replay, &rewrapped, &proof),
        Err(Error::Replay)
    );

    let rollback = intent(Operation::Rollback, 2, 1, &allow);
    assert_eq!(
        registry.change(&rollback, None, b"operator"),
        Err(Error::Authority)
    );
    let rollback_proof = authority.authorize(&rollback, "rollback-one");
    registry.change(&rollback, None, &rollback_proof).unwrap();
    assert_eq!(
        registry.change(&rollback, None, &rollback_proof),
        Err(Error::Conflict)
    );
    assert_eq!(selected(&registry), (3, 1));
    assert_eq!(
        broker.present(&old_permit, initial.request()),
        Err(Error::StalePermit)
    );
    let approval = signer.publication(APPROVAL, 3);
    publish(&registry, &authority, &approval, 3, 3, "publish-three");
    let retire = intent(Operation::Retire, 4, 1, &allow);
    registry
        .change(&retire, None, &authority.authorize(&retire, "retire"))
        .unwrap();
    assert_eq!(selected(&registry), (4, 3));
    let retired = intent(Operation::Rollback, 4, 1, &allow);
    assert_eq!(
        registry.change(
            &retired,
            None,
            &authority.authorize(&retired, "retired-rollback")
        ),
        Err(Error::Retired)
    );
    let revoked = intent(Operation::Rollback, 4, 2, &deny);
    let revoked_proof = authority.authorize(&revoked, "revoked-rollback");
    authority.revoke(KEY_AUTHORIZATION);
    assert_eq!(
        registry.change(&revoked, None, &revoked_proof),
        Err(Error::Trust)
    );
    assert_eq!(registry.audit().unwrap().len(), 5);
    assert_eq!(count(&path, "publications"), 3);
    assert_eq!(
        count(&dir.path().join("effects.sqlite"), "consumed_permits"),
        1
    );
}

#[test]
fn retirement_checks_exact_confirmation_current_authority_and_complete_dependencies() {
    let dir = Disposable::create().unwrap();
    let path = dir.path().join("policy.sqlite");
    let (authority, signer) = FixtureSigner::pair();
    let registry = Registry::create(&path, scope(), authority.clone()).unwrap();
    let first = signer.publication(ALLOW, 1);
    let second = signer.publication(DENY, 2);
    publish(&registry, &authority, &first, 0, 1, "first");

    let current = intent(Operation::Retire, 1, 1, &first);
    assert_eq!(
        registry.preview_retire(1, 1, "retire 1"),
        Err(Error::Replay)
    );
    assert_eq!(
        registry.change(&current, None, &authority.authorize(&current, "active")),
        Err(Error::Replay)
    );
    publish(&registry, &authority, &second, 1, 2, "replacement");
    let unchanged = registry.audit().unwrap();
    let retire = intent(Operation::Retire, 2, 1, &first);
    let approval = authority.authorize(&retire, "retirement");
    assert_eq!(
        registry.preview_retire(2, 1, "retire 2"),
        Err(Error::Format)
    );
    assert_eq!(
        registry.preview_retire(1, 1, "retire 1"),
        Err(Error::Conflict)
    );
    assert_eq!(
        registry.change(&current, None, &authority.authorize(&current, "stale")),
        Err(Error::Conflict)
    );
    let unconfirmed = Intent::new(scope(), Operation::Retire, 2, 1, &first).unwrap();
    let unconfirmed_proof = authority.authorize(&unconfirmed, "no-confirmation");
    assert_eq!(
        registry.change(&unconfirmed, None, &unconfirmed_proof),
        Err(Error::Format)
    );
    assert_eq!(
        registry.change(&retire, None, &unconfirmed_proof),
        Err(Error::Authority)
    );

    let first_digest = first.fingerprint().unwrap().to_string();
    for (dependents, expected) in [
        (Some([(1, first_digest)].into()), Error::ActiveDependents),
        (None, Error::DependencyUnavailable),
    ] {
        authority.active_dependents(dependents);
        assert_eq!(registry.preview_retire(2, 1, "retire 1"), Err(expected));
        assert_eq!(registry.change(&retire, None, &approval), Err(expected));
        assert_eq!(registry.audit().unwrap(), unchanged);
        assert_eq!(selected(&registry), (2, 2));
    }
    authority.active_dependents(Some(Default::default()));
    assert_eq!(registry.preview_retire(2, 1, "retire 1"), Ok(first.clone()));
    authority.publisher_enabled(false);
    assert_eq!(
        registry.change(&retire, None, &approval),
        Err(Error::Authority)
    );
    authority.publisher_enabled(true);
    assert_eq!(registry.audit().unwrap(), unchanged);
    assert_eq!(selected(&registry), (2, 2));

    let connection = Connection::open(&path).unwrap();
    connection
        .execute_batch(
            "CREATE TRIGGER fault BEFORE INSERT ON publication_audit
             BEGIN SELECT RAISE(ABORT, 'retirement audit failure'); END;",
        )
        .unwrap();
    assert_eq!(
        registry.change(&retire, None, &approval),
        Err(Error::Storage)
    );
    assert_eq!(registry.audit().unwrap(), unchanged);
    assert_eq!(selected(&registry), (2, 2));
    assert_eq!(count(&path, "publications"), 2);
    connection.execute_batch("DROP TRIGGER fault").unwrap();
    drop(connection);
    drop(registry);

    let registry = Registry::open(&path, scope(), authority.clone()).unwrap();
    assert_eq!(registry.audit().unwrap(), unchanged);
    let receipt = registry.change(&retire, None, &approval).unwrap();
    assert_eq!(
        (
            receipt.revision,
            receipt.publication_version,
            receipt.audit_sequence
        ),
        (2, 1, 3)
    );
    drop(registry);

    let registry = Registry::open(&path, scope(), authority.clone()).unwrap();
    let history = registry.audit().unwrap();
    assert_eq!(&history[..2], &unchanged);
    assert_eq!(history[2].operation, Operation::Retire);
    assert_eq!(
        history[2].intent_digest,
        retire.fingerprint().unwrap().as_str()
    );
    assert_eq!(history[2].publication_digest, history[0].publication_digest);
    assert_eq!(selected(&registry), (2, 2));
    assert_eq!(count(&path, "publications"), 2);
    assert_eq!(
        registry.change(&retire, None, &approval),
        Err(Error::Retired)
    );
    let rollback = intent(Operation::Rollback, 2, 1, &first);
    assert_eq!(
        registry.change(&rollback, None, &authority.authorize(&rollback, "restore")),
        Err(Error::Retired)
    );
    assert_eq!(registry.audit().unwrap(), history);
}

#[test]
fn retirement_and_rollback_race_has_only_one_winner() {
    let dir = Disposable::create().unwrap();
    let path = dir.path().join("policy.sqlite");
    let (authority, signer) = FixtureSigner::pair();
    let registry = Registry::create(&path, scope(), authority.clone()).unwrap();
    let first = signer.publication(ALLOW, 1);
    let second = signer.publication(DENY, 2);
    publish(&registry, &authority, &first, 0, 1, "first");
    publish(&registry, &authority, &second, 1, 2, "replacement");

    let retire = intent(Operation::Retire, 2, 1, &first);
    let retirement_proof = authority.authorize(&retire, "retire");
    let rollback = intent(Operation::Rollback, 2, 1, &first);
    let rollback_proof = authority.authorize(&rollback, "rollback");
    let start = Barrier::new(2);
    let (retired, rolled_back) = thread::scope(|group| {
        let retired = group.spawn(|| {
            start.wait();
            registry.change(&retire, None, &retirement_proof)
        });
        let rolled_back = group.spawn(|| {
            start.wait();
            registry.change(&rollback, None, &rollback_proof)
        });
        (retired.join().unwrap(), rolled_back.join().unwrap())
    });
    let (selection, operation, receipt) = match (retired, rolled_back) {
        (Ok(receipt), Err(Error::Retired)) => ((2, 2), Operation::Retire, receipt),
        (Err(Error::Conflict), Ok(receipt)) => ((3, 1), Operation::Rollback, receipt),
        other => panic!("one winner required: {other:?}"),
    };
    drop(registry);
    let registry = Registry::open(&path, scope(), authority).unwrap();
    assert_eq!(selected(&registry), selection);
    let history = registry.audit().unwrap();
    assert_eq!(history.len(), 3);
    assert_eq!(history[2].operation, operation);
    assert_eq!(history[2].sequence, receipt.audit_sequence);
    assert_eq!(history[2].revision, receipt.revision);
}

#[test]
fn direct_policy_changes_preserve_mandatory_requirements() {
    let dir = Disposable::create().unwrap();
    let path = dir.path().join("policy.sqlite");
    let (authority, signer) = FixtureSigner::pair();
    let registry = Registry::create(&path, scope(), authority.clone()).unwrap();
    let first = signer.publication(ALLOW, 1);
    publish(&registry, &authority, &first, 0, 1, "first");
    let criterion = |id, class| {
        json::obj([
            ("criterion_id", json::s(id)),
            ("required_class", json::s(class)),
        ])
    };
    let with_criteria = |version, criteria| {
        let mut pair = signer.publication(ALLOW, version);
        let payload = edit(&pair.acceptance.payload, "criteria", Value::Arr(criteria));
        pair.acceptance = signer.sign_policy(Purpose::Acceptance, payload);
        pair
    };
    let review = criterion("review", "human");
    let evidence = criterion("evidence", "mechanical");
    let second = with_criteria(2, vec![review.clone(), evidence.clone()]);
    publish(&registry, &authority, &second, 1, 2, "replacement");
    let unchanged = registry.audit().unwrap();

    for id in ["m6-acceptance", "review", "evidence"] {
        assert_eq!(
            registry.refuse_deactivation(2, 2, id, &Intent::deactivation_phrase(id)),
            Err(Error::InvalidPolicy)
        );
    }
    assert_eq!(
        registry.refuse_deactivation(
            2,
            2,
            "m6.synthetic.permit",
            "deactivate m6.synthetic.permit"
        ),
        Err(Error::Unsupported)
    );
    assert_eq!(
        registry.refuse_deactivation(2, 2, "review", "deactivate evidence"),
        Err(Error::Format)
    );

    for (index, criteria) in [
        vec![review.clone()],
        vec![review.clone(), criterion("evidence", "human")],
        vec![evidence, review],
    ]
    .into_iter()
    .enumerate()
    {
        let pair = with_criteria(3, criteria);
        assert_eq!(
            registry.preview(2, 3, Some(&pair)),
            Err(Error::InvalidPolicy)
        );
        let attempt = intent(Operation::Publish, 2, 3, &pair);
        let proof = authority.authorize(&attempt, &format!("changed-{index}"));
        assert_eq!(
            registry.publish(&attempt, &pair, &proof),
            Err(Error::InvalidPolicy)
        );
    }
    let rollback = intent(Operation::Rollback, 2, 1, &first);
    let proof = authority.authorize(&rollback, "omitting-rollback");
    assert_eq!(registry.preview(2, 1, None), Err(Error::InvalidPolicy));
    assert_eq!(
        registry.change(&rollback, None, &proof),
        Err(Error::InvalidPolicy)
    );
    assert_eq!(registry.audit().unwrap(), unchanged);
    assert_eq!(selected(&registry), (2, 2));

    let retire = intent(Operation::Retire, 2, 1, &first);
    let approval = authority.authorize(&retire, "retire");
    registry.change(&retire, None, &approval).unwrap();
    let mut rewrap = signer.publication(ALLOW, 3);
    rewrap.acceptance = first.acceptance;
    let attempt = intent(Operation::Publish, 2, 3, &rewrap);
    let proof = authority.authorize(&attempt, "rewrap");
    assert_eq!(
        registry.publish(&attempt, &rewrap, &proof),
        Err(Error::Replay)
    );
    assert_eq!(registry.audit().unwrap().len(), 3);
    assert_eq!(selected(&registry), (2, 2));
}

struct Paused<'a> {
    inner: SqliteEffectStore,
    pause: &'a (dyn Fn() + Sync),
}

impl EffectAdapter for Paused<'_> {
    fn apply(&self, verify: &mut Verify<'_>) -> Result<Result<EffectResult, Refusal>, TxFailure> {
        (self.pause)();
        self.inner.apply(verify)
    }

    fn inspect(
        &self,
        verify: &mut Verify<'_>,
    ) -> Result<Result<Option<EffectEvidence>, Refusal>, TxFailure> {
        self.inner.inspect(verify)
    }

    fn evidence(&self, permit_id: &str) -> Result<Option<EffectEvidence>, TxFailure> {
        self.inner.evidence(permit_id)
    }
}

#[test]
fn effect_rechecks_time_before_consuming_the_permit() {
    let dir = Disposable::create().unwrap();
    let (authority, signer) = FixtureSigner::pair();
    let registry = Registry::create(
        &dir.path().join("policy.sqlite"),
        scope(),
        authority.clone(),
    )
    .unwrap();
    let allow = signer.publication(ALLOW, 1);
    publish(&registry, &authority, &allow, 0, 1, "publish-one");
    let ledger = SqliteLedger::create(&dir.path().join("ledger.sqlite")).unwrap();
    let effects_path = dir.path().join("effects.sqlite");
    let effects = SqliteEffectStore::create(&effects_path, ledger.ledger_id()).unwrap();
    let advance = || authority.advance_to(600);
    let broker = LabBroker::new(
        &registry,
        ledger,
        Paused {
            inner: effects,
            pause: &advance,
        },
    );
    let (result, permit) = broker.execute(&request("expired-at-effect"), None).unwrap();
    assert_eq!(result, Err(Refusal::TimeUnavailable));
    assert!(
        permit.is_some(),
        "dispatch committed before the clock advanced"
    );
    assert_eq!(count(&effects_path, "consumed_permits"), 0);
    assert_eq!(count(&effects_path, "effects"), 0);
    assert_eq!(registry.audit().unwrap().len(), 1);
}

#[path = "support/grants.rs"]
mod grants;

#[test]
fn activation_waits_for_a_granted_effect_and_then_hard_deny_prevents_more_use() {
    let dir = Disposable::create().unwrap();
    let (authority, signer) = FixtureSigner::pair();
    let registry = Registry::create(
        &dir.path().join("policy.sqlite"),
        scope(),
        authority.clone(),
    )
    .unwrap();
    let approval = signer.publication(APPROVAL, 1);
    publish(&registry, &authority, &approval, 0, 1, "approval-active");
    let deny = signer.publication(DENY, 2);
    let activate = intent(Operation::Publish, 1, 2, &deny);
    let activate_proof = authority.authorize(&activate, "deny-active");

    let ledger_path = dir.path().join("ledger.sqlite");
    let effects_path = dir.path().join("effects.sqlite");
    let ledger = grants::fixture_ledger(&ledger_path);
    let effects = SqliteEffectStore::create(&effects_path, ledger.ledger_id()).unwrap();
    let (entered_tx, entered_rx) = mpsc::channel();
    let (release_tx, release_rx) = mpsc::channel();
    let release_rx = Mutex::new(release_rx);
    let pause = || {
        entered_tx.send(()).unwrap();
        release_rx
            .lock()
            .unwrap()
            .recv_timeout(Duration::from_secs(5))
            .unwrap();
    };
    let broker = LabBroker::new(
        &registry,
        ledger,
        Paused {
            inner: effects,
            pause: &pause,
        },
    );
    let request = request("effect-race");
    assert_eq!(
        broker.execute(&request, None).unwrap().0,
        Err(Refusal::ApprovalRequired)
    );
    let inspection =
        Connection::open_with_flags(&ledger_path, OpenFlags::SQLITE_OPEN_READ_ONLY).unwrap();
    let approval_request: String = inspection
        .query_row("SELECT request_id FROM approval_requests", [], |row| {
            row.get(0)
        })
        .unwrap();
    let grant = grants::approve(&broker.ledger, &approval_request);
    let different = request_with_other_digest(request.request());
    assert_eq!(
        broker.execute(&different, Some(&grant)).unwrap().0,
        Err(Refusal::GrantMismatch)
    );
    assert_eq!(count(&ledger_path, "grant_uses"), 0);
    let permit = thread::scope(|threads| {
        let effect = threads.spawn(|| broker.execute(&request, Some(&grant)).unwrap());
        entered_rx.recv_timeout(Duration::from_secs(5)).unwrap();
        let (started_tx, started_rx) = mpsc::channel();
        let (done_tx, done_rx) = mpsc::channel();
        let registry_ref = &registry;
        threads.spawn(move || {
            started_tx.send(()).unwrap();
            let result = registry_ref.publish(&activate, &deny, &activate_proof);
            done_tx.send(result).unwrap();
        });
        let started = started_rx.recv_timeout(Duration::from_secs(5));
        let pending = done_rx.recv_timeout(Duration::from_millis(75));
        release_tx.send(()).unwrap();
        started.unwrap();
        assert!(matches!(pending, Err(mpsc::RecvTimeoutError::Timeout)));
        let (result, permit) = effect.join().unwrap();
        assert_eq!(result, Ok(Executed::EffectCompleted));
        assert_eq!(
            done_rx
                .recv_timeout(Duration::from_secs(5))
                .unwrap()
                .unwrap()
                .revision,
            2
        );
        permit.unwrap()
    });
    assert!(broker.effects.evidence(permit.id()).unwrap().is_some());
    assert_eq!(
        broker.present(&permit, request.request()),
        Err(Error::StalePermit)
    );
    let (result, permit) = broker.execute(&request, Some(&grant)).unwrap();
    assert_eq!(result.unwrap_err().code(), "M6_SYNTHETIC_FORBID");
    assert!(permit.is_none());
    assert_eq!(count(&ledger_path, "grant_uses"), 1);
    assert_eq!(count(&ledger_path, "receipts"), 1);
    assert_eq!(count(&effects_path, "consumed_permits"), 1);
    assert_eq!(count(&effects_path, "effects"), 1);
    assert_eq!(registry.audit().unwrap().len(), 2);
}

fn request_with_other_digest(request: &Value) -> writ_broker::ingress::VerifiedRequest {
    let action = request.get("action").unwrap();
    let action = edit(
        action,
        "action_digest",
        json::s(&format!("sha256:{}", "5".repeat(64))),
    );
    seal_request(&edit(request, "action", action))
}

#[test]
fn explicit_recovery_rolls_back_from_an_expired_selection() {
    let dir = Disposable::create().unwrap();
    let path = dir.path().join("policy.sqlite");
    let (authority, signer) = FixtureSigner::pair();
    let registry = Registry::create(&path, scope(), authority.clone()).unwrap();
    let allow = signer.publication(ALLOW, 1);
    let mut short = signer.publication(DENY, 2);
    short.acceptance = signer.sign_until(
        Purpose::Acceptance,
        short.acceptance.payload,
        now().plus_seconds(5),
    );
    publish(&registry, &authority, &allow, 0, 1, "first");
    publish(&registry, &authority, &short, 1, 2, "second");
    drop(registry);
    authority.advance_to(5);
    assert!(matches!(
        Registry::open(&path, scope(), authority.clone()),
        Err(Error::Time)
    ));
    let rollback = intent(Operation::Rollback, 2, 1, &allow);
    let proof = authority.authorize(&rollback, "fresh-recovery");
    assert_eq!(
        Registry::recover(&path, scope(), authority.clone(), &rollback, &proof)
            .unwrap()
            .revision,
        3
    );
    let registry = Registry::open(&path, scope(), authority).unwrap();
    assert_eq!(selected(&registry), (3, 1));
    assert_eq!(registry.audit().unwrap()[2].operation, Operation::Rollback);
    assert_eq!(count(&path, "publications"), 2);
}

#[test]
fn acceptance_records_remain_bound_to_the_exact_selected_policy() {
    use writ_kernel::{
        acceptance::{check, AcceptanceRefusal},
        contracts::{AcceptanceRecord, CurrentState},
    };

    let dir = Disposable::create().unwrap();
    let (authority, signer) = FixtureSigner::pair();
    let registry = Registry::create(
        &dir.path().join("policy.sqlite"),
        scope(),
        authority.clone(),
    )
    .unwrap();
    let first = signer.publication(ALLOW, 1);
    let second = signer.publication(ALLOW, 2);
    publish(&registry, &authority, &first, 0, 1, "first-pair");
    let fixtures = json::parse(include_bytes!(
        "../../../m1/fixtures/acceptance/acceptance-binding.cases.json"
    ))
    .unwrap();
    let mut old_record = fixtures.get("base_record").unwrap().clone();
    let mut current = fixtures.get("base_current").unwrap().clone();
    for value in [&mut old_record, &mut current] {
        *value = edit(value, "tenant_id", json::s(scope().tenant()));
        *value = edit(value, "project_id", json::s(scope().project()));
    }
    old_record = edit(
        &old_record,
        "issued_at",
        json::s(&now().plus_seconds(-10).to_string()),
    );
    old_record = edit(
        &old_record,
        "expires_at",
        json::s(&now().plus_seconds(60).to_string()),
    );
    current = edit(&current, "now", json::s(&now().to_string()));
    let criterion = &old_record.get("criteria").unwrap().as_arr().unwrap()[2];
    let criterion = edit(criterion, "criterion_id", json::s("review"));
    let criterion = edit(
        &criterion,
        "fresh_until",
        json::s(&now().plus_seconds(60).to_string()),
    );
    old_record = edit(&old_record, "criteria", Value::Arr(vec![criterion]));
    registry
        .with_selection(|view| {
            let policy = &view.acceptance().policy;
            for value in [&mut old_record, &mut current] {
                *value = edit(
                    value,
                    "acceptance_policy_id",
                    json::s(&policy.acceptance_policy_id),
                );
                *value = edit(
                    value,
                    "acceptance_policy_version",
                    json::s(&policy.acceptance_policy_version),
                );
                *value = edit(
                    value,
                    "acceptance_policy_bundle_sha",
                    json::s(&policy.acceptance_policy_bundle_sha),
                );
            }
            let digest = json::digest(&old_record.without("record_digest")).unwrap();
            old_record = edit(&old_record, "record_digest", json::s(digest.as_str()));
            assert_eq!(
                check(
                    &AcceptanceRecord::from_value(&old_record).unwrap(),
                    &CurrentState::from_value(&current).unwrap(),
                    view.acceptance(),
                ),
                Ok(())
            );
            Ok(())
        })
        .unwrap();
    publish(&registry, &authority, &second, 1, 2, "second-pair");
    registry
        .with_selection(|view| {
            let old = AcceptanceRecord::from_value(&old_record).unwrap();
            let mut current = CurrentState::from_value(&current).unwrap();
            current.policy = view.acceptance().policy.clone();
            assert_eq!(
                check(&old, &current, view.acceptance()),
                Err(AcceptanceRefusal::PolicyMismatch)
            );
            Ok(())
        })
        .unwrap();
}
use std::sync::atomic::{AtomicI64, Ordering};
use writ_broker::clock::{ClockError, TrustedClock};
use writ_kernel::time::Timestamp;
use writ_policy_publication_lab::{
    Approval, CurrentTrust, Intent, TrustAuthority, VerificationKey, VerificationSource,
};

struct AdvanceDuringApproval {
    authority: FixtureAuthority,
    offset: AtomicI64,
    approvals: AtomicI64,
    advance_seconds: i64,
}

struct View<'a, T> {
    trust: T,
    offset: &'a AtomicI64,
    approvals: &'a AtomicI64,
    advance_seconds: i64,
}

impl TrustAuthority for AdvanceDuringApproval {
    fn current(&self) -> Result<impl CurrentTrust + '_, Error> {
        Ok(View {
            trust: self.authority.current()?,
            offset: &self.offset,
            approvals: &self.approvals,
            advance_seconds: self.advance_seconds,
        })
    }
}

impl<T: CurrentTrust> TrustedClock for View<'_, T> {
    fn now(&self) -> Result<Timestamp, ClockError> {
        Ok(self
            .trust
            .now()?
            .plus_seconds(self.offset.load(Ordering::SeqCst)))
    }
}

impl<T: CurrentTrust> VerificationSource for View<'_, T> {
    fn resolve(&self, id: &str) -> Result<VerificationKey, Error> {
        self.trust.resolve(id)
    }
}

impl<T: CurrentTrust> CurrentTrust for View<'_, T> {
    fn approve(
        &self,
        intent: &Intent,
        presentation: &[u8],
        time: Timestamp,
    ) -> Result<Approval, Error> {
        let approval = self.trust.approve(intent, presentation, time)?;
        if self.approvals.fetch_add(1, Ordering::SeqCst) == 1 {
            self.offset.store(self.advance_seconds, Ordering::SeqCst);
        }
        Ok(approval)
    }
}

#[test]
fn signature_and_human_expiry_inside_second_approval_refuses_commit() {
    let (authority, signer) = FixtureSigner::pair();
    let disposable = Disposable::create().unwrap();
    let p = signer.publication(ALLOW, 1);
    let intent = intent(Operation::Publish, 0, 1, &p);
    let approved = authority.authorize(&intent, "expires-during-final-approval");
    let registry = Registry::create(
        &disposable.path().join("policies.sqlite"),
        scope(),
        AdvanceDuringApproval {
            authority,
            offset: AtomicI64::new(0),
            approvals: AtomicI64::new(0),
            advance_seconds: 600,
        },
    )
    .unwrap();
    assert_eq!(registry.publish(&intent, &p, &approved), Err(Error::Time));
    assert!(registry.audit().unwrap().is_empty());
}

mod waivers;

#[test]
#[ignore = "required CI supplies the explicitly built fixture CLI"]
fn management_drafts_leave_signed_selection_grants_and_effects_unchanged() {
    use std::{
        env, fs,
        io::Write,
        process::{Command, Stdio},
    };

    let dir = Disposable::create().unwrap();
    let cli = env::var_os("WRIT_MANAGEMENT_FIXTURE_BIN").unwrap();
    let drafts = dir.path().join("drafts.sqlite");
    let credential = dir.path().join("fixture-credential");
    fs::write(&credential, b"fixture-author").unwrap();
    assert!(Command::new(&cli)
        .arg("init")
        .arg(&drafts)
        .output()
        .unwrap()
        .status
        .success());

    let (authority, signer) = FixtureSigner::pair();
    let registry = Registry::create(
        &dir.path().join("policy.sqlite"),
        scope(),
        authority.clone(),
    )
    .unwrap();
    let approval = signer.publication(APPROVAL, 1);
    publish(
        &registry,
        &authority,
        &approval,
        0,
        1,
        "approval-stays-active",
    );
    let ledger_path = dir.path().join("ledger.sqlite");
    let effects_path = dir.path().join("effects.sqlite");
    let ledger = grants::fixture_ledger(&ledger_path);
    let effects = SqliteEffectStore::create(&effects_path, ledger.ledger_id()).unwrap();
    let broker = LabBroker::new(&registry, ledger, effects);
    let protected = request("real-fixture-grant");
    let decide = || {
        use writ_kernel::{
            contracts::StateRevision,
            decision::{decide, Policy},
        };
        registry
            .with_selection(|view| {
                Ok(decide(
                    protected.request(),
                    Policy::Loaded(view.authorization()),
                    StateRevision::new(0).unwrap(),
                    now(),
                )
                .to_value())
            })
            .unwrap()
    };
    let original_decision = decide();
    assert_eq!(
        broker.execute(&protected, None).unwrap().0,
        Err(Refusal::ApprovalRequired)
    );
    let approval_request: String =
        Connection::open_with_flags(&ledger_path, OpenFlags::SQLITE_OPEN_READ_ONLY)
            .unwrap()
            .query_row("SELECT request_id FROM approval_requests", [], |row| {
                row.get(0)
            })
            .unwrap();
    let grant = grants::approve(&broker.ledger, &approval_request);

    let invoke = |operation, fields: &[(&str, Value)]| {
        let mut query = json::obj([
            ("version", json::s("/manage/v1")),
            ("operation", json::s(operation)),
            (
                "scope",
                json::obj([
                    ("tenant", json::s("m6-lab")),
                    ("project", json::s("offline-proof")),
                ]),
            ),
        ])
        .as_obj()
        .unwrap()
        .clone();
        query.extend(
            fields
                .iter()
                .map(|(key, value)| ((*key).to_string(), value.clone())),
        );
        let mut child = Command::new(&cli)
            .arg("v1")
            .arg(&drafts)
            .arg(&credential)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        let input = json::canonical(&Value::Obj(query)).unwrap();
        child
            .stdin
            .take()
            .unwrap()
            .write_all(input.as_bytes())
            .unwrap();
        let output = child.wait_with_output().unwrap();
        assert!(output.status.success(), "{:?}", json::parse(&output.stdout));
        json::parse(&output.stdout)
            .unwrap()
            .get("result")
            .unwrap()
            .clone()
    };
    for (operation, policy, revision) in [("create", APPROVAL, 0), ("edit", DENY, 1)] {
        let fields = [
            ("id", json::s("draft")),
            ("expected_revision", Value::Int(revision)),
            ("idempotency_key", json::s(operation)),
            ("preview", Value::Bool(false)),
            ("candidate", json::parse(policy).unwrap()),
        ];
        assert_eq!(
            invoke(operation, &fields).get("revision"),
            Some(&Value::Int(revision + 1))
        );
    }
    let comparison = invoke(
        "compare",
        &[
            (
                "before",
                json::obj([("id", json::s("draft")), ("revision", Value::Int(1))]),
            ),
            (
                "after",
                json::obj([("id", json::s("draft")), ("revision", Value::Int(2))]),
            ),
            (
                "corpus",
                Value::Arr(vec![json::obj([
                    ("kind", json::s("synthetic.inspect")),
                    (
                        "resource",
                        json::s(
                            "synthetic:m6-lab/offline-proof/inventory/restricted/sealed/report",
                        ),
                    ),
                ])]),
            ),
        ],
    );
    assert_eq!(
        comparison.get("runtime_authority"),
        Some(&Value::Bool(false))
    );
    let paired = &comparison.get("cases").unwrap().as_arr().unwrap()[0];
    assert_eq!(
        paired.get("before").unwrap().get("status"),
        Some(&json::s("require_approval"))
    );
    assert_eq!(
        paired.get("after").unwrap().get("status"),
        Some(&json::s("deny"))
    );
    for query in [
        br#"{"version":"/manage/v1","operation":"evaluator_inspect","scope":{"tenant":"m6-lab","project":"offline-proof"},"evaluator_id":"doc-check","after":0,"limit":1}"#.as_slice(),
        br#"{"version":"/manage/v1","operation":"waiver_inspect","scope":{"tenant":"m6-lab","project":"offline-proof"},"waiver_id":"sha256:1111111111111111111111111111111111111111111111111111111111111111"}"#.as_slice(),
    ] {
        let mut child = Command::new(&cli)
            .arg("v1")
            .arg(&drafts)
            .arg(&credential)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        child.stdin.take().unwrap().write_all(query).unwrap();
        let output = child.wait_with_output().unwrap();
        assert!(!output.status.success());
        assert_eq!(
            json::parse(&output.stdout)
                .unwrap()
                .get("error")
                .and_then(|error| error.get("code")),
            Some(&json::s("UNAVAILABLE"))
        );
        assert_eq!(decide(), original_decision);
    }
    let outage = Command::new(&cli)
        .arg("v1")
        .arg(dir.path().join("unavailable-management.sqlite"))
        .arg(&credential)
        .stdin(Stdio::null())
        .output()
        .unwrap();
    assert!(!outage.status.success());
    assert_eq!(
        json::parse(&outage.stdout)
            .unwrap()
            .get("error")
            .unwrap()
            .get("code"),
        Some(&json::s("UNAVAILABLE"))
    );
    assert_eq!(decide(), original_decision);
    assert_eq!(selected(&registry), (1, 1));
    assert_eq!(registry.audit().unwrap().len(), 1);
    assert_eq!(count(&ledger_path, "approval_requests"), 1);
    assert_eq!(count(&ledger_path, "grant_uses"), 0);
    assert_eq!(count(&ledger_path, "receipts"), 0);
    assert_eq!(count(&effects_path, "effects"), 0);
    assert_eq!(count(&effects_path, "consumed_permits"), 0);
    assert_eq!(
        broker.execute(&protected, Some(&grant)).unwrap().0,
        Ok(Executed::EffectCompleted)
    );
    assert_eq!(count(&ledger_path, "grant_uses"), 1);
    assert_eq!(count(&effects_path, "effects"), 1);
}

#[test]
fn optional_removal_owner_fences_authority_faults_competition_and_recovery() {
    let dir = Disposable::create().unwrap();
    let path = dir.path().join("policy.sqlite");
    let (authority, signer) = FixtureSigner::pair();
    let registry = Registry::create(&path, scope(), authority.clone()).unwrap();
    let source = signer.publication(
        include_bytes!("../../compiler/fixtures/optional-inspect.input.json"),
        1,
    );
    publish(&registry, &authority, &source, 0, 1, "optional-source");
    let ordinary = signer.publication(DENY, 2);
    let replacement = ordinary.clone().bind_optional_removal(&source).unwrap();
    let exact = intent(Operation::Publish, 1, 2, &replacement);
    let presentation = authority.authorize(&exact, "optional-removal");
    let before = registry.audit().unwrap();
    let difference = registry
        .preview_optional_removal(1, 2, &replacement)
        .unwrap();
    assert_eq!(difference.get("after").unwrap().as_arr().unwrap().len(), 6);

    let unrelated = signer.publication(ALLOW, 1);
    let mut stale = replacement.clone();
    stale.prerequisites = edit(
        &stale.prerequisites,
        "proof_digest",
        json::s(&format!("sha256:{}", "0".repeat(64))),
    );
    let misbound = ordinary.clone().bind_optional_removal(&unrelated).unwrap();
    let weakening = signer
        .publication(APPROVAL, 2)
        .bind_optional_removal(&source)
        .unwrap();
    for (n, candidate) in [ordinary, stale, misbound, weakening].iter().enumerate() {
        let command = intent(Operation::Publish, 1, 2, candidate);
        let approved = authority.authorize(&command, &format!("invalid-removal-{n}"));
        assert_eq!(
            registry.publish(&command, candidate, &approved),
            Err(Error::InvalidPolicy)
        );
    }
    let wrong_scope = Intent::new(
        Scope::new("m6-lab", "other-project").unwrap(),
        Operation::Publish,
        1,
        2,
        &replacement,
    )
    .unwrap();
    assert_eq!(
        registry.publish(&wrong_scope, &replacement, &presentation),
        Err(Error::Scope)
    );
    let mut wrong_purpose = replacement.clone();
    wrong_purpose.acceptance = replacement.authorization.clone();
    let purpose_intent = intent(Operation::Publish, 1, 2, &wrong_purpose);
    let purpose_presentation = authority.authorize(&purpose_intent, "wrong-policy-purpose");
    assert!(registry
        .publish(&purpose_intent, &wrong_purpose, &purpose_presentation)
        .is_err());
    authority.publisher_enabled(false);
    assert_eq!(
        registry.publish(&exact, &replacement, &presentation),
        Err(Error::Authority)
    );
    authority.publisher_enabled(true);

    let connection = Connection::open(&path).unwrap();
    for action in ["INSERT ON publication_audit", "UPDATE ON registry_head"] {
        connection.execute_batch(&format!(
            "CREATE TRIGGER fault BEFORE {action} BEGIN SELECT RAISE(ABORT, 'fixture failure'); END;"
        )).unwrap();
        assert_eq!(
            registry.publish(&exact, &replacement, &presentation),
            Err(Error::Storage)
        );
        assert_eq!(registry.audit().unwrap(), before);
        assert_eq!(count(&path, "publications"), 1);
        connection.execute_batch("DROP TRIGGER fault").unwrap();
    }
    assert_eq!(selected(&registry), (1, 1));
    drop(connection);
    let lab = broker(dir.path(), &registry);
    let cases = json::parse(include_bytes!(
        "../../compiler/fixtures/optional-inspect.cases.json"
    ))
    .unwrap();
    let case = cases
        .as_arr()
        .unwrap()
        .iter()
        .find(|case| case.get("id") == Some(&json::s("scope")))
        .unwrap();
    let request = seal_request(
        &case
            .get("request")
            .unwrap()
            .without("expected_state_revision"),
    );
    let (result, permit) = lab.execute(&request, None).unwrap();
    assert!(result.is_ok(), "{result:?}");
    let permit = permit.unwrap();
    let barrier = Barrier::new(2);
    let (first, second) = thread::scope(|threads| {
        let select = |ceremony| {
            let approval = authority.authorize(&exact, ceremony);
            barrier.wait();
            registry.publish(&exact, &replacement, &approval)
        };
        let first = threads.spawn(move || select("remove-first"));
        let second = threads.spawn(move || select("remove-second"));
        (first.join().unwrap(), second.join().unwrap())
    });
    let commit = match (first, second) {
        (Ok(commit), Err(Error::Conflict)) | (Err(Error::Conflict), Ok(commit)) => commit,
        other => panic!("one exact removal must commit: {other:?}"),
    };
    assert_eq!(
        registry.publish(&exact, &replacement, &presentation),
        Err(Error::Conflict)
    );
    assert_eq!(
        lab.present(&permit, request.request()),
        Err(Error::StalePermit)
    );
    assert_eq!(
        count(&dir.path().join("effects.sqlite"), "consumed_permits"),
        1
    );
    drop(lab);
    drop(registry);
    let reopened = Registry::open(&path, scope(), authority.clone()).unwrap();
    assert_eq!(reopened.current_selection().unwrap(), Some(commit));
    let audit = reopened.audit().unwrap();
    assert_eq!(audit[0], before[0]);
    assert_eq!(
        audit[1].intent_digest,
        exact.fingerprint().unwrap().as_str()
    );
    reopened
        .with_selection(|view| {
            assert_eq!(view.publication(), &replacement);
            Ok(())
        })
        .unwrap();

    let restore = intent(Operation::Rollback, 2, 1, &source);
    reopened
        .change(
            &restore,
            None,
            &authority.authorize(&restore, "restore-optional"),
        )
        .unwrap();
    let stale_removal = intent(Operation::Rollback, 3, 2, &replacement);
    let rollback = authority.authorize(&stale_removal, "old-removal-rollback");
    assert_eq!(
        reopened.change(&stale_removal, None, &rollback),
        Err(Error::Replay)
    );
    assert_eq!(selected(&reopened), (3, 1));
    assert_eq!(reopened.audit().unwrap().len(), 3);
}

#[test]
fn optional_removal_preserves_independently_signed_v2_waiver_bounds() {
    let dir = Disposable::create().unwrap();
    let (authority, signer) = FixtureSigner::pair();
    let registry = Registry::create(
        &dir.path().join("policy.sqlite"),
        scope(),
        authority.clone(),
    )
    .unwrap();
    let signed = |draft: &[u8], version, seconds| {
        let mut pair = signer.publication(draft, version);
        let criterion = |id, origin, limit| {
            json::obj([
                ("criterion_id", json::s(id)),
                ("required_class", json::s("human")),
                ("origin", json::s(origin)),
                ("max_waiver_seconds", Value::Int(limit)),
            ])
        };
        let payload = edit(
            &edit(
                &pair.acceptance.payload,
                "version",
                json::s("writ-acceptance-policy/2"),
            ),
            "criteria",
            Value::Arr(vec![
                criterion("review", "invariant", 0),
                criterion("waiver-review", "project", seconds),
            ]),
        );
        pair.acceptance = signer.sign_policy(Purpose::Acceptance, payload);
        pair
    };
    let source = signed(
        include_bytes!("../../compiler/fixtures/optional-inspect.input.json"),
        1,
        60,
    );
    publish(&registry, &authority, &source, 0, 1, "v2-source");
    let changed = signed(DENY, 2, 120).bind_optional_removal(&source).unwrap();
    let requested = intent(Operation::Publish, 1, 2, &changed);
    let approval = authority.authorize(&requested, "v2-changed-limit");
    assert_eq!(
        registry.publish(&requested, &changed, &approval),
        Err(Error::InvalidPolicy)
    );
    assert_eq!(selected(&registry), (1, 1));
    assert_eq!(registry.audit().unwrap().len(), 1);
    let preserved = signed(DENY, 2, 60).bind_optional_removal(&source).unwrap();
    publish(&registry, &authority, &preserved, 1, 2, "v2-preserved");
}

#[test]
fn optional_removal_refuses_source_expiry_inside_second_approval() {
    let dir = Disposable::create().unwrap();
    let path = dir.path().join("policy.sqlite");
    let (authority, signer) = FixtureSigner::pair();
    let mut source = signer.publication(
        include_bytes!("../../compiler/fixtures/optional-inspect.input.json"),
        1,
    );
    source.acceptance = signer.sign_until(
        Purpose::Acceptance,
        source.acceptance.payload,
        now().plus_seconds(1),
    );
    let registry = Registry::create(&path, scope(), authority.clone()).unwrap();
    publish(&registry, &authority, &source, 0, 1, "short-source");
    let before = registry.audit().unwrap();
    drop(registry);
    let replacement = signer
        .publication(DENY, 2)
        .bind_optional_removal(&source)
        .unwrap();
    let request = intent(Operation::Publish, 1, 2, &replacement);
    let approval = authority.authorize(&request, "source-expires-during-removal");
    let registry = Registry::open(
        &path,
        scope(),
        AdvanceDuringApproval {
            authority: authority.clone(),
            offset: AtomicI64::new(0),
            approvals: AtomicI64::new(0),
            advance_seconds: 2,
        },
    )
    .unwrap();
    assert_eq!(
        registry.publish(&request, &replacement, &approval),
        Err(Error::Time)
    );
    drop(registry);
    let reopened = Registry::open(&path, scope(), authority).unwrap();
    assert_eq!(selected(&reopened), (1, 1));
    assert_eq!(reopened.audit().unwrap(), before);
}

mod quorum;

#[test]
fn signed_quorum_cannot_be_downgraded_by_publish_or_historical_rollback() {
    let dir = Disposable::create().unwrap();
    let (authority, signer) = FixtureSigner::pair();
    let registry = Registry::create(
        &dir.path().join("policy.sqlite"),
        scope(),
        authority.clone(),
    )
    .unwrap();
    let criterion = |id, class| {
        json::obj([
            ("criterion_id", json::s(id)),
            ("required_class", json::s(class)),
        ])
    };
    let pair = |version, quorum: Option<Value>| {
        let mut p = signer.publication(ALLOW, version);
        let mut review = criterion("review", "human");
        let mut build = criterion("build", "mechanical");
        let mut acceptance = p.acceptance.payload;
        if let Some(quorum) = quorum {
            for c in [&mut review, &mut build] {
                *c = edit(
                    &edit(c, "origin", json::s("invariant")),
                    "max_waiver_seconds",
                    Value::Int(0),
                );
            }
            review = edit(&review, "quorum", Value::Null);
            build = edit(&build, "quorum", quorum);
            acceptance = edit(&acceptance, "version", json::s("writ-acceptance-policy/3"));
        }
        p.acceptance = signer.sign_policy(
            Purpose::Acceptance,
            edit(&acceptance, "criteria", Value::Arr(vec![review, build])),
        );
        p
    };
    let member = |id, group| {
        json::obj([
            ("evaluator_id", json::s(id)),
            ("independence_group", json::s(group)),
        ])
    };
    let eligible = |members| json::obj([("eligible", Value::Arr(members))]);
    let legacy = pair(1, None);
    publish(&registry, &authority, &legacy, 0, 1, "pre-quorum");
    let v3 = pair(
        2,
        Some(eligible(vec![member("first", "a"), member("second", "b")])),
    );
    publish(&registry, &authority, &v3, 1, 2, "quorum-enabled");
    registry
        .with_selection(|selection| {
            assert_eq!(
                writ_broker::quorum_policy::QuorumPolicyGuard::current(
                    &selection,
                    scope().tenant(),
                    scope().project(),
                    now()
                )
                .unwrap()
                .quorums["build"]["second"],
                "b"
            );
            Ok(())
        })
        .unwrap();
    let before = registry.audit().unwrap();
    for (n, target) in [
        pair(3, None),
        pair(3, Some(Value::Null)),
        pair(
            3,
            Some(eligible(vec![member("first", "a"), member("first", "b")])),
        ),
        pair(
            3,
            Some(eligible(vec![member("first", "a"), member("second", "a")])),
        ),
    ]
    .into_iter()
    .enumerate()
    {
        let change = intent(Operation::Publish, 2, 3, &target);
        let approved = authority.authorize(&change, &format!("downgrade-{n}"));
        assert_eq!(
            registry.publish(&change, &target, &approved),
            Err(Error::InvalidPolicy)
        );
    }
    let rollback = intent(Operation::Rollback, 2, 1, &legacy);
    let approved = authority.authorize(&rollback, "historical-quorum-downgrade");
    assert_eq!(
        registry.change(&rollback, None, &approved),
        Err(Error::InvalidPolicy)
    );
    assert_eq!(selected(&registry), (2, 2));
    assert_eq!(registry.audit().unwrap(), before);
}
