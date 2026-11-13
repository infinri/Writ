//! D-035 regression tests on the SQLite ledger and the controlled effect store:
//! reservation retention and revalidation before dispatch (item 1), effect-store
//! ownership and ledger binding (item 2), evidence-based reconciliation and
//! non-effectful permit presentation (item 3), adapter failure precedence and recorded
//! progress (item 4), approver provenance, legacy grants and the explicit upgrade from
//! `writ-ledger-3` (item 5).

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::os::unix::fs::symlink;
use std::path::{Path, PathBuf};
use std::sync::Barrier;
use std::thread;

use writ_broker::execute::{Broker, Executed, Execution, Progress, TxName};
use writ_broker::grants::{
    ApprovalAuthority, ApprovalPresentation, ApproverRefusal, AuthenticatedApprover, PrincipalKind,
    Replay, ReserveOutcome, Verdict,
};
use writ_broker::policy::PolicyState;
use writ_broker::refusal::Refusal;
use writ_broker::store::{StoreError, TxStage};
use writ_broker::writ_kernel::json::{self, Value};

use super::execute_tests::{reconciled, World};
use super::grant_tests::{
    alice, approve, approve_as, channels, file_write, label, now, publish, publish_forbidden,
    reference, reference_v2, reserve, revision, set, uses, verify,
};
use super::{fault, LedgerId, SqliteEffectStore, SqliteLedger, SCHEMA_V3};

fn exec(
    b: &Broker<'_, SqliteLedger, SqliteEffectStore>,
    req: &Value,
    g: Option<&str>,
) -> Execution {
    let g = g.map(label);
    b.execute(&verify(req), revision(), g.as_ref())
}

fn one(l: &SqliteLedger, sql: &str) -> i64 {
    l.db().conn.query_row(sql, [], |r| r.get(0)).unwrap()
}

fn text(l: &SqliteLedger, sql: &str, arg: &str) -> Option<String> {
    l.db().conn.query_row(sql, [arg], |r| r.get(0)).ok()
}

fn status(l: &SqliteLedger, idem: &str) -> Option<String> {
    text(
        l,
        "SELECT status FROM receipts WHERE idempotency_key = ?1",
        idem,
    )
}

fn effects(e: &SqliteEffectStore) -> i64 {
    e.db()
        .conn
        .query_row("SELECT count(*) FROM effects", [], |r| r.get(0))
        .unwrap()
}

fn unavailable(category: &str, code: &str) -> Refusal {
    Refusal::LedgerUnavailable(StoreError::Unavailable {
        category: category.into(),
        code: code.into(),
    })
}

fn tmp(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("writ-d035-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

// ---------------------------------------------------------------------------
// Item 1: reservation retention, revalidation before dispatch, permit expiry.

/// Reserve `k1` under grant `g1` (approved with `ttl` seconds) without dispatching.
fn reserved(w: &World, ttl: Option<u32>) {
    let b = w.broker();
    approve_as(
        b.grants(),
        &publish("k1"),
        "g1",
        Verdict::Approved,
        ttl,
        &alice(),
        &channels(),
    )
    .unwrap();
    assert_eq!(
        reserve(b.grants(), &publish("k1"), Some("g1")),
        Ok(ReserveOutcome::Reserved)
    );
    assert_eq!(status(&w.ledger, "k1").as_deref(), Some("reserved"));
}

/// Nothing was dispatched for `k1` and the reservation kept its key and grant use.
fn reservation_kept(w: &World) {
    assert_eq!(status(&w.ledger, "k1").as_deref(), Some("reserved"));
    assert_eq!(uses(&w.ledger, "g1"), Some(1), "grant use kept, no refund");
    assert_eq!(one(&w.ledger, "SELECT count(*) FROM grant_uses"), 1);
    assert_eq!(
        one(
            &w.ledger,
            "SELECT value FROM counters WHERE name = 'next_permit'"
        ),
        0
    );
    assert_eq!(one(&w.ledger, "SELECT count(*) FROM receipt_permits"), 0);
    assert_eq!(effects(&w.effects), 0);
}

#[test]
fn an_expired_grant_never_dispatches_a_retained_reservation() {
    let w = World::new("d035-expired");
    reserved(&w, Some(10));
    let b = w.broker();
    // The grant expires while the reservation waits; retries refuse at T-dispatch.
    w.clock.set(now().plus_seconds(10));
    for _ in 0..3 {
        let e = exec(&b, &publish("k1"), Some("g1"));
        assert_eq!(e.result, Err(Refusal::GrantExpired));
        assert_eq!(e.permit, None);
        let ran: Vec<_> = e
            .evidence
            .transactions
            .iter()
            .map(|t| (t.name, t.committed))
            .collect();
        assert_eq!(
            ran,
            [(TxName::Reserve, true), (TxName::Dispatch, false)],
            "resumed, then refused without dispatch"
        );
        assert_eq!(e.evidence.progress(), Progress::Reserved);
        reservation_kept(&w);
    }
    // The key is never recycled and the grant never substituted or replenished.
    approve(b.grants(), &publish("k1"), "g2").unwrap();
    assert_eq!(
        exec(&b, &publish("k1"), Some("g2")).result,
        Err(Refusal::IdempotencyConflict)
    );
    assert_eq!(uses(&w.ledger, "g2"), Some(0));
    let other = set(&publish("k1"), &["request_id"], json::s("req-other"));
    assert_eq!(
        exec(&b, &other, Some("g1")).result,
        Err(Refusal::IdempotencyConflict)
    );
    reservation_kept(&w);
    // Time never moves back: a later clock reading cannot revive it either.
    w.clock.set(now().plus_seconds(3600));
    assert_eq!(
        exec(&b, &publish("k1"), Some("g1")).result,
        Err(Refusal::GrantExpired)
    );
    assert_eq!(
        one(
            &w.ledger,
            "SELECT count(*) FROM audit_refusals WHERE code = 'GRANT_EXPIRED'"
        ),
        4
    );
}

#[test]
fn a_policy_change_after_reservation_is_revalidated_before_dispatch() {
    let w = World::new("d035-policy");
    reserved(&w, None);
    let b = w.broker();
    // Another bundle: the use-time binding (bundle digest) no longer matches the grant.
    w.active.activate(PolicyState::loaded(reference_v2()));
    assert_eq!(
        exec(&b, &publish("k1"), Some("g1")).result,
        Err(Refusal::GrantMismatch)
    );
    reservation_kept(&w);
    // Publishing forbidden: the use-time decision refuses before T-reserve.
    w.active.activate(PolicyState::loaded(publish_forbidden()));
    let e = exec(&b, &publish("k1"), Some("g1"));
    assert!(
        matches!(e.result, Err(Refusal::Decision { .. })),
        "{:?}",
        e.result
    );
    assert!(e.evidence.transactions.is_empty());
    reservation_kept(&w);
    // Back under the approval's policy the same reservation resumes without a second use.
    w.active.activate(PolicyState::loaded(reference()));
    let e = exec(&b, &publish("k1"), Some("g1"));
    assert_eq!(e.result, Ok(Executed::EffectCompleted));
    assert_eq!(uses(&w.ledger, "g1"), Some(1));
    assert_eq!(effects(&w.effects), 1);
}

#[test]
fn a_reservation_whose_grant_use_is_missing_is_an_integrity_failure() {
    let w = World::new("d035-use-missing");
    reserved(&w, None);
    w.ledger
        .db()
        .conn
        .execute("DELETE FROM grant_uses", [])
        .unwrap();
    let e = exec(&w.broker(), &publish("k1"), Some("g1"));
    assert_eq!(
        e.result,
        Err(unavailable("transaction dispatch", "integrity"))
    );
    assert_eq!(status(&w.ledger, "k1").as_deref(), Some("reserved"));
    assert_eq!(effects(&w.effects), 0);
}

#[test]
fn a_grant_backed_permit_expires_no_later_than_its_grant() {
    // (grant lifetime, request deadline offset, expected permit expiry offset)
    for (ttl, deadline, want) in [
        (10, None, 10),      // grant expiry
        (600, None, 30),     // permit TTL
        (600, Some(20), 20), // request deadline
        (15, Some(20), 15),  // grant expiry before the deadline
        (25, Some(12), 12),  // deadline before the grant expiry
    ] {
        let w = World::new(&format!("d035-cap-{ttl}"));
        let b = w.broker();
        let mut req = publish("k1");
        if let Some(d) = deadline {
            req = set(
                &req,
                &["deadline_at"],
                json::s(&now().plus_seconds(d).to_string()),
            );
        }
        approve_as(
            b.grants(),
            &req,
            "g1",
            Verdict::Approved,
            Some(ttl),
            &alice(),
            &channels(),
        )
        .unwrap();
        let e = exec(&b, &req, Some("g1"));
        assert_eq!(
            e.result,
            Ok(Executed::EffectCompleted),
            "{ttl} {deadline:?}"
        );
        assert_eq!(
            e.permit.unwrap().expires_at(),
            now().plus_seconds(want).to_string(),
            "{ttl} {deadline:?}"
        );
    }
    // An allowed request has no grant: TTL or deadline only.
    let w = World::new("d035-cap-allow");
    let e = exec(&w.broker(), &file_write("k1"), None);
    assert_eq!(
        e.permit.unwrap().expires_at(),
        now().plus_seconds(30).to_string()
    );
}

// ---------------------------------------------------------------------------
// Item 2: effect-store ownership and ledger binding.

fn effects_error(code: &str) -> StoreError {
    StoreError::Unavailable {
        category: "effects".into(),
        code: code.into(),
    }
}

#[test]
fn the_effect_store_has_one_owner_across_every_alias() {
    let d = tmp("own");
    std::fs::create_dir_all(d.join("real")).unwrap();
    let ledger = SqliteLedger::create(&d.join("ledger.db")).unwrap();
    let id = ledger.ledger_id().clone();
    let path = d.join("real/effects.db");
    let owner = SqliteEffectStore::create(&path, &id).unwrap();
    symlink(&path, d.join("file-link.db")).unwrap();
    symlink(d.join("real"), d.join("dir-link")).unwrap();
    let aliases = [
        path.clone(),
        d.join("real/../real/effects.db"),
        d.join("file-link.db"),
        d.join("dir-link/effects.db"),
    ];
    for alias in &aliases {
        assert_eq!(
            SqliteEffectStore::open(alias, &id).err(),
            Some(effects_error("OWNED_ELSEWHERE")),
            "{}",
            alias.display()
        );
        // `create` never touches an existing store: refused as owned, or (through a link
        // to the file, whose own name is new) as existing.
        let created = SqliteEffectStore::create(alias, &id).err();
        assert!(
            created == Some(effects_error("OWNED_ELSEWHERE"))
                || created == Some(effects_error("EXISTS")),
            "{}: {created:?}",
            alias.display()
        );
    }
    // Restart: released when the owner drops, then exactly one owner again.
    drop(owner);
    for alias in &aliases {
        let reopened = SqliteEffectStore::open(alias, &id).unwrap();
        assert_eq!(
            SqliteEffectStore::open(&path, &id).err(),
            Some(effects_error("OWNED_ELSEWHERE"))
        );
        drop(reopened);
    }
    // Concurrent startup in one process: exactly one owner, the rest fail closed.
    for _ in 0..5 {
        let start = Barrier::new(8);
        let results: Vec<_> = thread::scope(|s| {
            let handles: Vec<_> = aliases
                .iter()
                .cycle()
                .take(8)
                .map(|p| {
                    let (start, id) = (&start, &id);
                    s.spawn(move || {
                        start.wait();
                        SqliteEffectStore::open(p, id)
                    })
                })
                .collect();
            handles.into_iter().map(|h| h.join().unwrap()).collect()
        });
        assert_eq!(results.iter().filter(|r| r.is_ok()).count(), 1);
        for r in results.iter().filter(|r| r.is_err()) {
            assert_eq!(r.as_ref().err(), Some(&effects_error("OWNED_ELSEWHERE")));
        }
    }
}

#[test]
fn a_hard_linked_effect_store_is_refused_as_ambiguous() {
    let d = tmp("hard");
    let ledger = SqliteLedger::create(&d.join("ledger.db")).unwrap();
    let id = ledger.ledger_id().clone();
    drop(SqliteEffectStore::create(&d.join("effects.db"), &id).unwrap());
    std::fs::hard_link(d.join("effects.db"), d.join("other.db")).unwrap();
    for p in ["effects.db", "other.db"] {
        assert_eq!(
            SqliteEffectStore::open(&d.join(p), &id).err(),
            Some(effects_error("AMBIGUOUS_IDENTITY")),
            "{p}"
        );
    }
    std::fs::remove_file(d.join("other.db")).unwrap();
    assert!(SqliteEffectStore::open(&d.join("effects.db"), &id).is_ok());
}

#[test]
fn an_effect_store_never_serves_another_ledger() {
    let d = tmp("bound");
    let a = SqliteLedger::create(&d.join("a.db")).unwrap();
    let b = SqliteLedger::create(&d.join("b.db")).unwrap();
    let store = SqliteEffectStore::create(&d.join("effects.db"), a.ledger_id()).unwrap();
    // Not concurrently (the owner holds it) ...
    assert!(SqliteEffectStore::open(&d.join("effects.db"), b.ledger_id()).is_err());
    drop(store);
    // ... and not later: the binding is checked at every open.
    for _ in 0..2 {
        assert_eq!(
            SqliteEffectStore::open(&d.join("effects.db"), b.ledger_id()).err(),
            Some(effects_error("LEDGER_MISMATCH"))
        );
    }
    let fake = LedgerId::parse("00000000000000000000000000000000").unwrap();
    assert_eq!(
        SqliteEffectStore::open(&d.join("effects.db"), &fake).err(),
        Some(effects_error("LEDGER_MISMATCH"))
    );
    assert!(SqliteEffectStore::open(&d.join("effects.db"), a.ledger_id()).is_ok());
    assert_eq!(LedgerId::parse("0123"), None);
    assert_eq!(LedgerId::parse(&"G".repeat(32)), None);
}

// ---------------------------------------------------------------------------
// Items 3 and 4: evidence-based reconciliation, failure precedence and progress.

#[test]
fn concurrent_reconciliations_record_each_outcome_once_from_evidence_only() {
    let w = World::new("d035-reconcile");
    let b = w.broker();
    // k1: effect applied, observation lost. k2: effect failed (permit consumed),
    // observation lost. k3: adapter transaction failed before commit (no evidence).
    // k4: completed normally.
    w.ledger
        .inject("observe", fault::Stage::Begin, rusqlite::ffi::SQLITE_IOERR);
    assert!(exec(&b, &file_write("k1"), None).result.is_err());
    w.effects.set_effects_fail(true);
    w.ledger
        .inject("observe", fault::Stage::Body, rusqlite::ffi::SQLITE_IOERR);
    assert!(exec(&b, &file_write("k2"), None).result.is_err());
    w.effects.set_effects_fail(false);
    w.effects
        .inject(fault::Stage::Body, rusqlite::ffi::SQLITE_IOERR);
    assert!(exec(&b, &file_write("k3"), None).result.is_err());
    assert_eq!(
        exec(&b, &file_write("k4"), None).result,
        Ok(Executed::EffectCompleted)
    );
    let start = Barrier::new(8);
    let results: Vec<_> = thread::scope(|s| {
        let handles: Vec<_> = (0..8)
            .map(|_| {
                let (b, start) = (&b, &start);
                s.spawn(move || {
                    start.wait();
                    b.reconcile().unwrap()
                })
            })
            .collect();
        handles.into_iter().map(|h| h.join().unwrap()).collect()
    });
    let total = results.iter().fold(reconciled(0, 0, 0), |mut t, r| {
        t.observed_success += r.observed_success;
        t.observed_failure += r.observed_failure;
        t.unknown += r.unknown;
        t
    });
    assert_eq!(total, reconciled(1, 1, 1), "{results:?}");
    for (k, want) in [
        ("k1", "observed_success"),
        ("k2", "observed_failure"),
        ("k3", "unknown_reconcile"),
        ("k4", "observed_success"),
    ] {
        assert_eq!(status(&w.ledger, k).as_deref(), Some(want), "{k}");
    }
    assert_eq!(
        one(
            &w.ledger,
            "SELECT count(*) FROM receipt_events WHERE cause = 'reconcile'"
        ),
        3
    );
    assert_eq!(one(&w.ledger, "SELECT count(*) FROM reconciliations"), 3);
    // Repeated recovery changes nothing; the evidence-free receipt stays uncertain.
    for _ in 0..3 {
        assert_eq!(b.reconcile(), Ok(reconciled(0, 0, 0)));
    }
    assert_eq!(
        exec(&b, &file_write("k1"), None).result,
        Ok(Executed::Replayed(Replay::EffectCompleted))
    );
    assert_eq!(
        exec(&b, &file_write("k2"), None).result,
        Ok(Executed::Replayed(Replay::EffectFailed))
    );
    assert_eq!(
        exec(&b, &file_write("k3"), None).result,
        Err(Refusal::ReconciliationRequired)
    );
    assert_eq!(effects(&w.effects), 2, "nothing redispatched");
}

#[test]
fn evidence_bound_to_another_receipt_is_an_integrity_failure() {
    let w = World::new("d035-evidence-mismatch");
    let b = w.broker();
    w.ledger
        .inject("observe", fault::Stage::Begin, rusqlite::ffi::SQLITE_IOERR);
    assert!(exec(&b, &file_write("k1"), None).result.is_err());
    // The effect store's record for permit-1 is altered to name another key.
    w.effects
        .db()
        .conn
        .execute_batch(
            "UPDATE effects SET idempotency_key = 'other'; \
             UPDATE consumed_permits SET idempotency_key = 'other';",
        )
        .unwrap();
    assert_eq!(
        b.reconcile(),
        Err(unavailable("reconcile evidence", "integrity"))
    );
    assert_eq!(status(&w.ledger, "k1").as_deref(), Some("dispatched"));
    // An inconsistent record (applied without its effect row) is refused by the store.
    w.effects
        .db()
        .conn
        .execute_batch("UPDATE consumed_permits SET idempotency_key = 'k1'; DELETE FROM effects;")
        .unwrap();
    assert_eq!(
        b.reconcile(),
        Err(unavailable("transaction evidence", "integrity"))
    );
    assert_eq!(status(&w.ledger, "k1").as_deref(), Some("dispatched"));
    assert_eq!(one(&w.ledger, "SELECT count(*) FROM reconciliations"), 0);
}

#[test]
fn an_ambiguous_adapter_commit_is_recorded_and_its_uncertainty_retained() {
    let w = World::new("d035-ambiguous");
    let b = w.broker();
    w.effects
        .inject(fault::Stage::Commit, rusqlite::ffi::SQLITE_IOERR);
    let e = exec(&b, &file_write("k1"), None);
    assert_eq!(
        e.result,
        Err(unavailable("transaction adapter", "SQLITE_IOERR"))
    );
    let (name, failure) = e.evidence.failure().unwrap();
    assert_eq!(
        (name, failure.stage, failure.outcome_unknown),
        (TxName::Adapter, TxStage::Commit, true)
    );
    assert_eq!(e.evidence.progress(), Progress::DispatchedWithoutEffect);
    assert_eq!(
        e.evidence.possible_progress(),
        Progress::DispatchedWithEffect
    );
    assert!(e.evidence.reconciliation_required());
    let record: (String, String, String, String, i64) = w
        .ledger
        .db()
        .conn
        .query_row(
            "SELECT transaction_name, stage, progress, possible_progress, outcome_unknown \
             FROM failure_records",
            [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)),
        )
        .unwrap();
    assert_eq!(
        record,
        (
            "adapter".into(),
            "commit".into(),
            "dispatched_without_effect".into(),
            "dispatched_with_effect".into(),
            1
        )
    );
    // The receipt is never assumed effect-free: a retry needs reconciliation, and
    // reconciliation without evidence keeps it uncertain.
    assert_eq!(
        exec(&b, &file_write("k1"), None).result,
        Err(Refusal::ReconciliationRequired)
    );
    assert_eq!(b.reconcile(), Ok(reconciled(0, 0, 1)));
    assert_eq!(
        exec(&b, &file_write("k1"), None).result,
        Err(Refusal::ReconciliationRequired)
    );
    assert_eq!(effects(&w.effects), 0);
}

// ---------------------------------------------------------------------------
// Item 5: approver provenance, legacy grants and the explicit upgrade.

#[test]
fn approval_persists_provenance_and_never_the_credential() {
    let w = World::new("d035-provenance");
    let b = w.broker();
    let issued = approve(b.grants(), &publish("k1"), "g1").unwrap();
    let row: Vec<String> = w
        .ledger
        .db()
        .conn
        .query_row(
            "SELECT version, approver_id, approver_issuer, channel, approved_at, verdict, \
             auth_evidence_ref, request_fingerprint, policy_bundle_sha \
             FROM grant_provenance WHERE label = 'g1'",
            [],
            |r| (0..9).map(|i| r.get::<_, String>(i)).collect(),
        )
        .unwrap();
    let fingerprint = json::digest(&publish("k1")).unwrap();
    assert_eq!(
        row,
        [
            "writ-provenance-1",
            "human:alice",
            "realm:fixture",
            "approval-ui",
            &now().to_string(),
            "approved",
            "authn-event:approval-ui:human:alice",
            fingerprint.as_str(),
            reference().sha.as_str(),
        ]
    );
    assert!(issued.binding.contains(reference().sha.as_str()));
    // No byte of the presented credential reaches the ledger files.
    drop(b);
    let dir = w.dir.clone();
    drop(w);
    for f in ["ledger.db", "ledger.db-wal"] {
        if let Ok(bytes) = std::fs::read(dir.join(f)) {
            assert!(
                !bytes.windows(10).any(|x| x == b"cred-alice"),
                "{f} contains the credential"
            );
        }
    }
}

/// An authority that authenticates the presented human but cannot establish provenance
/// or echoes the credential as its evidence reference.
struct Partial(&'static str, &'static str);

impl ApprovalAuthority for Partial {
    fn authenticate(
        &self,
        p: &ApprovalPresentation<'_>,
    ) -> Result<AuthenticatedApprover, ApproverRefusal> {
        Ok(AuthenticatedApprover {
            id: p.approver.into(),
            kind: PrincipalKind::Human,
            channel: p.channel.into(),
            issuer: self.0.into(),
            evidence_ref: self.1.into(),
        })
    }
}

#[test]
fn an_approval_without_provenance_is_refused_and_stores_nothing() {
    let w = World::new("d035-no-provenance");
    let b = w.broker();
    for (authority, why) in [
        (
            Partial("", "event-1"),
            ApproverRefusal::IncompleteProvenance,
        ),
        (Partial("realm", ""), ApproverRefusal::IncompleteProvenance),
        (
            Partial("realm", "bearer cred-alice"),
            ApproverRefusal::CredentialInEvidence,
        ),
    ] {
        assert_eq!(
            approve_as(
                b.grants(),
                &publish("k1"),
                "g1",
                Verdict::Approved,
                None,
                &alice(),
                &authority
            ),
            Err(Refusal::HumanUnauthenticated(why))
        );
    }
    assert_eq!(one(&w.ledger, "SELECT count(*) FROM grants"), 0);
    assert_eq!(one(&w.ledger, "SELECT count(*) FROM grant_provenance"), 0);
}

#[test]
fn a_grant_with_damaged_provenance_is_not_executable() {
    let w = World::new("d035-damaged");
    let b = w.broker();
    approve(b.grants(), &publish("k1"), "g1").unwrap();
    w.ledger
        .db()
        .conn
        .execute("UPDATE grant_provenance SET verdict = 'denied'", [])
        .unwrap();
    assert_eq!(
        exec(&b, &publish("k1"), Some("g1")).result,
        Err(unavailable("transaction reserve", "provenance"))
    );
    assert_eq!(uses(&w.ledger, "g1"), Some(0));
    assert_eq!(one(&w.ledger, "SELECT count(*) FROM receipts"), 0);
}

/// A `writ-ledger-3` ledger as an M3-04 broker left it: the exact v3 schema with a grant
/// `g1` for `k1` and a reservation of `k2` under grant `g2` (bindings copied from grants
/// a v4 broker issued for the same requests, so only provenance is missing).
fn legacy_ledger(dir: &Path) -> PathBuf {
    let w = World::new("d035-legacy-source");
    let b = w.broker();
    approve(b.grants(), &publish("k1"), "g1").unwrap();
    approve(b.grants(), &publish("k2"), "g2").unwrap();
    let grants: Vec<(String, String, i64, i64)> = {
        let db = w.ledger.db();
        let mut s = db
            .conn
            .prepare("SELECT label, binding, expires_s, expires_ns FROM grants ORDER BY label")
            .unwrap();
        s.query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)))
            .unwrap()
            .collect::<rusqlite::Result<_>>()
            .unwrap()
    };
    let fingerprint = json::digest(&publish("k2")).unwrap();
    let path = dir.join("legacy.db");
    let c = rusqlite::Connection::open(&path).unwrap();
    c.execute_batch(SCHEMA_V3).unwrap();
    for (label, binding, s, ns) in &grants {
        let used = i64::from(label == "g2");
        c.execute(
            "INSERT INTO grants (label, binding, expires_s, expires_ns, max_uses, uses) \
             VALUES (?1, ?2, ?3, ?4, 1, ?5)",
            (label, binding, s, ns, used),
        )
        .unwrap();
    }
    c.execute_batch(&format!(
        "INSERT INTO grant_uses (label, tenant_id, idempotency_key) VALUES ('g2', 'fixture-tenant', 'k2');
         INSERT INTO receipts (tenant_id, idempotency_key, fingerprint, grant_label, status)
           VALUES ('fixture-tenant', 'k2', '{}', 'g2', 'reserved');
         INSERT INTO receipt_events (tenant_id, idempotency_key, status, cause)
           VALUES ('fixture-tenant', 'k2', 'reserved', 'reserve');
         INSERT INTO audit_refusals (code) VALUES ('LEGACY');",
        fingerprint.as_str()
    ))
    .unwrap();
    let mode: String = c
        .query_row("PRAGMA journal_mode = WAL", [], |r| r.get(0))
        .unwrap();
    assert_eq!(mode, "wal");
    drop(c);
    path
}

#[test]
fn the_explicit_upgrade_keeps_legacy_data_and_legacy_grants_stay_non_executable() {
    let d = tmp("upgrade");
    let path = legacy_ledger(&d);
    let schema = || StoreError::Unavailable {
        category: "initialize".into(),
        code: "schema".into(),
    };
    // open never upgrades.
    let before = std::fs::read(&path).unwrap();
    assert_eq!(SqliteLedger::open(&path).err(), Some(schema()));
    assert_eq!(std::fs::read(&path).unwrap(), before);
    let ledger = SqliteLedger::upgrade_from_v3(&path).unwrap();
    // Everything kept, nothing invented.
    assert_eq!(one(&ledger, "SELECT count(*) FROM grants"), 2);
    assert_eq!(one(&ledger, "SELECT count(*) FROM grant_uses"), 1);
    assert_eq!(one(&ledger, "SELECT count(*) FROM receipts"), 1);
    assert_eq!(one(&ledger, "SELECT count(*) FROM audit_refusals"), 1);
    assert_eq!(one(&ledger, "SELECT count(*) FROM grant_provenance"), 0);
    assert_eq!(one(&ledger, "SELECT count(*) FROM receipt_permits"), 0);
    assert_eq!(
        text(
            &ledger,
            "SELECT value FROM meta WHERE key = ?1",
            "schema_version"
        )
        .as_deref(),
        Some("writ-ledger-4")
    );
    let effects = SqliteEffectStore::create(&d.join("effects.db"), ledger.ledger_id()).unwrap();
    let w = World::new("d035-upgrade-policy");
    let b = Broker::new(&w.active, &w.clock, &ledger, &effects, &w.key);
    // A legacy grant is never consumed ...
    let e = b.execute(&verify(&publish("k1")), revision(), Some(&label("g1")));
    assert_eq!(
        e.result,
        Err(unavailable("transaction reserve", "provenance"))
    );
    assert_eq!(uses(&ledger, "g1"), Some(0));
    // ... and a legacy reservation is never dispatched under it; it keeps its use.
    let e = b.execute(&verify(&publish("k2")), revision(), Some(&label("g2")));
    assert_eq!(
        e.result,
        Err(unavailable("transaction dispatch", "provenance"))
    );
    assert_eq!(status(&ledger, "k2").as_deref(), Some("reserved"));
    assert_eq!(uses(&ledger, "g2"), Some(1));
    assert_eq!(
        effects
            .db()
            .conn
            .query_row("SELECT count(*) FROM consumed_permits", [], |r| r
                .get::<_, i64>(0))
            .unwrap(),
        0
    );
    // A new approval in the upgraded ledger is executable.
    approve(b.grants(), &publish("k3"), "g3").unwrap();
    assert_eq!(
        b.execute(&verify(&publish("k3")), revision(), Some(&label("g3")))
            .result,
        Ok(Executed::EffectCompleted)
    );
    let id = ledger.ledger_id().clone();
    drop(b);
    drop(effects);
    drop(ledger);
    // The upgrade is one-way and explicit: an upgraded ledger opens normally (same
    // identity) and is refused by a second upgrade.
    assert_eq!(SqliteLedger::upgrade_from_v3(&path).err(), Some(schema()));
    let reopened = SqliteLedger::open(&path).unwrap();
    assert_eq!(reopened.ledger_id(), &id);
    drop(reopened);
    // Anything but an exact writ-ledger-3 ledger is refused unchanged.
    let other = d.join("other.db");
    let c = rusqlite::Connection::open(&other).unwrap();
    c.execute_batch(&format!("{SCHEMA_V3}CREATE TABLE extra (x INTEGER);"))
        .unwrap();
    let _: String = c
        .query_row("PRAGMA journal_mode = WAL", [], |r| r.get(0))
        .unwrap();
    drop(c);
    let before = std::fs::read(&other).unwrap();
    assert_eq!(SqliteLedger::upgrade_from_v3(&other).err(), Some(schema()));
    assert_eq!(std::fs::read(&other).unwrap(), before);
}
