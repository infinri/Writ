//! M3-04 tests on the SQLite ledger and the separate controlled effect store: the
//! execute lifecycle (T-dispatch, T-adapter, T-observe), permits presented to the
//! adapter (replay, changed request, stale bundle, D-025 B precedence, concurrent
//! consumption), storage failures injected at every stage of every lifecycle
//! transaction with the implied crash state (D-026, S-7), ambiguous outcomes,
//! reconciliation under the exclusive gate, policy activation racing dispatch and
//! present_permit, restart persistence and sanitized diagnostics (S-5).

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{mpsc, Barrier, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use writ_broker::effects::{EffectAdapter, EffectEvidence, EffectResult, Verify};
use writ_broker::execute::{Broker, Executed, Execution, Progress, Reconciled, TxName};
use writ_broker::grants::Replay;
use writ_broker::keys::PermitKey;
use writ_broker::permit::Permit;
use writ_broker::policy::{ActivePolicy, PolicyState};
use writ_broker::refusal::Refusal;
use writ_broker::store::{LedgerStore, StoreError, TxFailure, TxStage};
use writ_broker::writ_kernel::json::{self, Value};

use super::grant_tests::{
    approve, audited, file_write, label, now, publish, reference, reference_v2, revision, set,
    uses, verify, TestClock,
};
use super::{fault, SqliteEffectStore, SqliteLedger};

const PERMIT_KEY: [u8; 32] = [0x44; 32];

fn permit_key() -> PermitKey {
    PermitKey::from_bytes(PERMIT_KEY.to_vec()).unwrap()
}

fn tmp(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("writ-execute-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

/// A fresh ledger and effect store, the reference bundle active, the clock at `now()`.
pub(crate) struct World {
    pub(crate) dir: PathBuf,
    pub(crate) ledger: SqliteLedger,
    pub(crate) effects: SqliteEffectStore,
    pub(crate) active: ActivePolicy,
    pub(crate) clock: TestClock,
    pub(crate) key: PermitKey,
}

impl World {
    pub(crate) fn new(name: &str) -> World {
        let dir = tmp(name);
        let ledger = SqliteLedger::create(&dir.join("ledger.db")).unwrap();
        World {
            effects: SqliteEffectStore::create(&dir.join("effects.db"), ledger.ledger_id())
                .unwrap(),
            ledger,
            active: ActivePolicy::new(PolicyState::loaded(reference())),
            clock: TestClock::at(now()),
            key: permit_key(),
            dir,
        }
    }

    pub(crate) fn broker(&self) -> Broker<'_, SqliteLedger, SqliteEffectStore> {
        Broker::new(
            &self.active,
            &self.clock,
            &self.ledger,
            &self.effects,
            &self.key,
        )
    }

    fn hooked<'a>(&'a self, adapter: &'a Hooked<'a>) -> Broker<'a, SqliteLedger, Hooked<'a>> {
        Broker::new(&self.active, &self.clock, &self.ledger, adapter, &self.key)
    }

    fn hooked_any<'a, A: EffectAdapter>(&'a self, adapter: &'a A) -> Broker<'a, SqliteLedger, A> {
        Broker::new(&self.active, &self.clock, &self.ledger, adapter, &self.key)
    }
}

/// The effect store with hooks around the adapter transaction.
struct Hooked<'a> {
    inner: &'a SqliteEffectStore,
    before: &'a (dyn Fn() + Sync),
    after: &'a (dyn Fn() + Sync),
}

impl EffectAdapter for Hooked<'_> {
    fn apply(&self, verify: &mut Verify<'_>) -> Result<Result<EffectResult, Refusal>, TxFailure> {
        (self.before)();
        let out = self.inner.apply(verify);
        (self.after)();
        out
    }

    fn inspect(
        &self,
        verify: &mut Verify<'_>,
    ) -> Result<Result<Option<EffectEvidence>, Refusal>, TxFailure> {
        (self.before)();
        let out = self.inner.inspect(verify);
        (self.after)();
        out
    }

    fn evidence(&self, permit_id: &str) -> Result<Option<EffectEvidence>, TxFailure> {
        self.inner.evidence(permit_id)
    }
}

/// Reconciliation result with the given counts.
pub(crate) fn reconciled(observed_success: u64, observed_failure: u64, unknown: u64) -> Reconciled {
    Reconciled {
        observed_success,
        observed_failure,
        unknown,
    }
}

fn nothing() {}

fn exec<A: EffectAdapter + ?Sized>(
    b: &Broker<'_, SqliteLedger, A>,
    req: &Value,
    grant: Option<&str>,
) -> Execution {
    let g = grant.map(label);
    b.execute(&verify(req), revision(), g.as_ref())
}

// ---------------------------------------------------------------------------
// Inspection: the ledger and, independently, the effect store.

fn ledger_i64(l: &SqliteLedger, sql: &str) -> i64 {
    l.db().conn.query_row(sql, [], |r| r.get(0)).unwrap()
}

fn effects_i64(e: &SqliteEffectStore, sql: &str) -> i64 {
    e.db().conn.query_row(sql, [], |r| r.get(0)).unwrap()
}

fn status(l: &SqliteLedger, idem: &str) -> Option<String> {
    l.db()
        .conn
        .query_row(
            "SELECT status FROM receipts WHERE idempotency_key = ?1",
            [idem],
            |r| r.get(0),
        )
        .ok()
}

fn causes(l: &SqliteLedger, idem: &str) -> Vec<String> {
    let db = l.db();
    let mut s = db
        .conn
        .prepare("SELECT cause FROM receipt_events WHERE idempotency_key = ?1 ORDER BY seq")
        .unwrap();
    s.query_map([idem], |r| r.get(0))
        .unwrap()
        .collect::<rusqlite::Result<_>>()
        .unwrap()
}

fn next_permit(l: &SqliteLedger) -> i64 {
    ledger_i64(l, "SELECT value FROM counters WHERE name = 'next_permit'")
}

fn effect_rows(e: &SqliteEffectStore) -> i64 {
    effects_i64(e, "SELECT count(*) FROM effects")
}

fn consumed_rows(e: &SqliteEffectStore) -> i64 {
    effects_i64(e, "SELECT count(*) FROM consumed_permits")
}

fn effects_for(e: &SqliteEffectStore, idem: &str) -> i64 {
    e.db()
        .conn
        .query_row(
            "SELECT count(*) FROM effects WHERE idempotency_key = ?1",
            [idem],
            |r| r.get(0),
        )
        .unwrap()
}

fn unavailable(tx: &str, code: &str) -> Refusal {
    Refusal::LedgerUnavailable(StoreError::Unavailable {
        category: format!("transaction {tx}"),
        code: code.into(),
    })
}

/// Operator diagnostics carry the transaction, stage, category and code only.
fn assert_sanitized(lines: &[String], dir: &Path) {
    let key_hex: String = PERMIT_KEY.iter().map(|b| format!("{b:02x}")).collect();
    for line in lines {
        assert!(
            line.chars()
                .all(|c| c.is_ascii_alphanumeric() || c == ' ' || c == '_'),
            "{line}"
        );
        assert!(!line.contains(dir.to_str().unwrap()), "{line}");
        for word in [
            "SELECT", "INSERT", "UPDATE", "receipts", "ledger", "effects", "db",
        ] {
            assert!(!line.contains(word), "{line} contains {word}");
        }
        assert!(!line.contains(&key_hex[..16]), "{line}");
    }
}

/// A permit issued by an execution whose adapter transaction could not begin: the
/// receipt is `dispatched`, the permit unconsumed and held by the caller.
fn unconsumed_permit<A: EffectAdapter + ?Sized>(
    w: &World,
    b: &Broker<'_, SqliteLedger, A>,
    req: &Value,
) -> Permit {
    w.effects
        .inject(fault::Stage::Begin, rusqlite::ffi::SQLITE_IOERR);
    let e = exec(b, req, None);
    assert_eq!(e.result, Err(unavailable("adapter", "SQLITE_IOERR")));
    e.permit.unwrap()
}

fn tampered(permit: &Permit, member: &str, value: &str) -> Permit {
    let mut v = permit.to_value();
    if let Value::Obj(m) = &mut v {
        m.insert(member.into(), json::s(value));
    }
    Permit::from_value(&v).unwrap()
}

// ---------------------------------------------------------------------------
// The lifecycle.

#[test]
fn an_allowed_execution_dispatches_once_applies_one_effect_and_observes_success() {
    let w = World::new("allow");
    let b = w.broker();
    let req = file_write("k1");
    let e = exec(&b, &req, None);
    assert_eq!(e.result, Ok(Executed::EffectCompleted));
    let names: Vec<_> = e
        .evidence
        .transactions
        .iter()
        .map(|t| (t.name, t.committed, t.failure.is_none()))
        .collect();
    assert_eq!(
        names,
        vec![
            (TxName::Reserve, true, true),
            (TxName::Dispatch, true, true),
            (TxName::Adapter, true, true),
            (TxName::Observe, true, true),
        ]
    );
    assert_eq!(e.evidence.progress(), Progress::Observed);
    let permit = e.permit.unwrap();
    assert_eq!(permit.permit_id(), "permit-1");
    assert_eq!(permit.expires_at(), "2026-11-05T12:00:30Z");
    assert_eq!(permit.policy_bundle_sha(), reference().sha.as_str());
    // The ledger: one receipt with its full history.
    assert_eq!(status(&w.ledger, "k1").as_deref(), Some("observed_success"));
    assert_eq!(causes(&w.ledger, "k1"), ["reserve", "dispatch", "observe"]);
    assert_eq!(next_permit(&w.ledger), 1);
    // The effect store, read directly: one consumed permit, one effect for this request.
    let row: (String, String, String, String) = w
        .effects
        .db()
        .conn
        .query_row(
            "SELECT e.permit_id, e.tenant_id, e.idempotency_key, c.result \
             FROM effects e JOIN consumed_permits c USING (permit_id)",
            [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
        )
        .unwrap();
    assert_eq!(
        row,
        (
            "permit-1".into(),
            "fixture-tenant".into(),
            "k1".into(),
            "applied".into()
        )
    );
    let fingerprint: String = w
        .effects
        .db()
        .conn
        .query_row("SELECT request_fingerprint FROM effects", [], |r| r.get(0))
        .unwrap();
    assert_eq!(fingerprint, json::digest(&req).unwrap().as_str());
    // An identical retry replays without a second dispatch or effect.
    let again = exec(&b, &req, None);
    assert_eq!(
        again.result,
        Ok(Executed::Replayed(Replay::EffectCompleted))
    );
    assert_eq!(again.permit, None);
    assert_eq!(again.evidence.progress(), Progress::NoChange);
    assert_eq!(effect_rows(&w.effects), 1);
    assert_eq!(next_permit(&w.ledger), 1);
    assert_eq!(b.grants().key_locks().held(), 0);
}

#[test]
fn an_approved_execution_consumes_one_grant_use_and_applies_one_effect() {
    let w = World::new("approved");
    let b = w.broker();
    approve(b.grants(), &publish("k1"), "g1").unwrap();
    assert_eq!(
        exec(&b, &publish("k1"), None).result,
        Err(Refusal::ApprovalRequired)
    );
    let e = exec(&b, &publish("k1"), Some("g1"));
    assert_eq!(e.result, Ok(Executed::EffectCompleted));
    assert_eq!(uses(&w.ledger, "g1"), Some(1));
    assert_eq!(effects_for(&w.effects, "k1"), 1);
    assert_eq!(
        exec(&b, &publish("k1"), Some("g1")).result,
        Ok(Executed::Replayed(Replay::EffectCompleted))
    );
    assert_eq!(uses(&w.ledger, "g1"), Some(1));
    // Another key cannot reuse the single-use grant.
    assert_eq!(
        exec(&b, &publish("k2"), Some("g1")).result.unwrap_err(),
        Refusal::GrantExhausted
    );
    assert_eq!(effect_rows(&w.effects), 1);
}

#[test]
fn a_failed_effect_consumes_the_permit_and_observes_failure() {
    let w = World::new("effect-fails");
    let b = w.broker();
    w.effects.set_effects_fail(true);
    let e = exec(&b, &file_write("k1"), None);
    assert_eq!(e.result, Ok(Executed::EffectFailed));
    assert_eq!(status(&w.ledger, "k1").as_deref(), Some("observed_failure"));
    assert_eq!(effect_rows(&w.effects), 0, "nothing applied");
    assert_eq!(
        effects_i64(
            &w.effects,
            "SELECT count(*) FROM consumed_permits WHERE result = 'failed'"
        ),
        1
    );
    w.effects.set_effects_fail(false);
    assert_eq!(
        exec(&b, &file_write("k1"), None).result,
        Ok(Executed::Replayed(Replay::EffectFailed))
    );
    // The consumed permit cannot be presented again for a second try.
    assert_eq!(
        b.present_permit(&e.permit.unwrap(), &file_write("k1")),
        Refusal::PermitReplayed
    );
    assert_eq!(effect_rows(&w.effects), 0);
}

// ---------------------------------------------------------------------------
// Permits presented to the adapter.

#[test]
fn a_replayed_permit_is_refused_and_applies_nothing() {
    let w = World::new("replay");
    let b = w.broker();
    let req = file_write("k1");
    let permit = exec(&b, &req, None).permit.unwrap();
    for _ in 0..3 {
        let refusal = b.present_permit(&permit, &req);
        assert_eq!(refusal, Refusal::PermitReplayed);
        assert_eq!(refusal.external_code(), "PROMOTION_REFUSED");
    }
    assert_eq!(effect_rows(&w.effects), 1);
    assert_eq!(consumed_rows(&w.effects), 1);
    assert_eq!(audited(&w.ledger, "PERMIT_REPLAYED"), 3);
    // The adapter itself refuses a second consumption of one permit id: an adapter
    // called twice with the same verified permit applies once.
    let twice = |verify: &mut Verify<'_>| {
        let first = w.effects.apply(verify);
        let second = w.effects.apply(verify);
        (first, second)
    };
    let doubled = Doubled(&w.effects, &twice);
    let b2 = w.hooked_any(&doubled);
    let e = exec(&b2, &file_write("k2"), None);
    assert_eq!(e.result, Ok(Executed::EffectCompleted));
    assert_eq!(effects_for(&w.effects, "k2"), 1);
}

/// An adapter that runs the real adapter transaction twice per call and returns the
/// first answer after asserting the second is a replay.
type AdapterAnswer = Result<Result<EffectResult, Refusal>, TxFailure>;
type Twice<'a> = &'a (dyn Fn(&mut Verify<'_>) -> (AdapterAnswer, AdapterAnswer) + Sync);

struct Doubled<'a>(&'a SqliteEffectStore, Twice<'a>);

impl EffectAdapter for Doubled<'_> {
    fn apply(&self, verify: &mut Verify<'_>) -> Result<Result<EffectResult, Refusal>, TxFailure> {
        let (first, second) = (self.1)(verify);
        assert_eq!(second, Ok(Err(Refusal::PermitReplayed)));
        first
    }

    fn inspect(
        &self,
        verify: &mut Verify<'_>,
    ) -> Result<Result<Option<EffectEvidence>, Refusal>, TxFailure> {
        self.0.inspect(verify)
    }

    fn evidence(&self, permit_id: &str) -> Result<Option<EffectEvidence>, TxFailure> {
        self.0.evidence(permit_id)
    }
}

#[test]
fn a_presented_permit_is_verified_but_never_applies_an_effect() {
    let w = World::new("mismatch");
    let b = w.broker();
    let req = file_write("k1");
    let permit = unconsumed_permit(&w, &b, &req);
    let changed = set(
        &req,
        &["action", "resource", "canonical_resource_id"],
        json::s("workspace:fixture-tenant/fixture-project/src/other.py"),
    );
    let other_key = set(&req, &["idempotency_key"], json::s("k2"));
    for presented in [&changed, &other_key] {
        assert_eq!(
            b.present_permit(&permit, presented),
            Refusal::PermitRequestMismatch
        );
    }
    assert_eq!(consumed_rows(&w.effects), 0, "a mismatch consumes nothing");
    // D-035 item 3: the exact request with a valid, unconsumed permit of a `dispatched`
    // receipt starts no effect; recovery goes through the broker.
    for _ in 0..3 {
        assert_eq!(
            b.present_permit(&permit, &req),
            Refusal::ReconciliationRequired
        );
    }
    assert_eq!(consumed_rows(&w.effects), 0);
    assert_eq!(effect_rows(&w.effects), 0);
    assert_eq!(status(&w.ledger, "k1").as_deref(), Some("dispatched"));
    assert_eq!(
        exec(&b, &req, None).result,
        Err(Refusal::ReconciliationRequired)
    );
    // Also after reconciliation marked it uncertain.
    assert_eq!(b.reconcile(), Ok(reconciled(0, 0, 1)));
    assert_eq!(
        b.present_permit(&permit, &req),
        Refusal::ReconciliationRequired
    );
    assert_eq!(effect_rows(&w.effects), 0);
    assert_eq!(audited(&w.ledger, "RECONCILIATION_REQUIRED"), 5);
}

#[test]
fn a_permit_for_another_bundle_after_activation_is_stale() {
    let w = World::new("stale");
    let b = w.broker();
    let req = file_write("k1");
    let permit = unconsumed_permit(&w, &b, &req);
    w.active.activate(PolicyState::loaded(reference_v2()));
    let refusal = b.present_permit(&permit, &req);
    assert_eq!(refusal, Refusal::PermitStale);
    assert_eq!(refusal.external_code(), "PROMOTION_REFUSED");
    for state in [PolicyState::Absent, PolicyState::Rejected] {
        w.active.activate(state);
        assert_eq!(b.present_permit(&permit, &req), Refusal::PermitStale);
    }
    assert_eq!(consumed_rows(&w.effects), 0);
    // A permit issued under the new bundle is current under it.
    w.active.activate(PolicyState::loaded(reference_v2()));
    let e = exec(&b, &file_write("k2"), None);
    assert_eq!(e.result, Ok(Executed::EffectCompleted));
    assert_eq!(
        e.permit.unwrap().policy_bundle_sha(),
        reference_v2().sha.as_str()
    );
    assert_eq!(effect_rows(&w.effects), 1);
}

#[test]
fn adapter_verification_follows_the_d025_b_precedence() {
    let w = World::new("precedence");
    let b = w.broker();
    let req = file_write("k1");
    let permit = unconsumed_permit(&w, &b, &req);
    let first = if permit
        .to_value()
        .get("mac")
        .unwrap()
        .as_str()
        .unwrap()
        .starts_with('0')
    {
        "1"
    } else {
        "0"
    };
    let mac = permit
        .to_value()
        .get("mac")
        .unwrap()
        .as_str()
        .unwrap()
        .to_string();
    let forged = tampered(&permit, "mac", &format!("{first}{}", &mac[1..]));
    let changed = set(&req, &["request_id"], json::s("req-other"));
    // A storage failure of the adapter transaction at any stage is LEDGER_UNAVAILABLE,
    // whatever is wrong with the permit; at begin, no permit check has even run
    // (D-025 B, D-035 item 4). Also with an expired, stale permit.
    w.active.activate(PolicyState::loaded(reference_v2()));
    w.clock.set(now().plus_seconds(30));
    for presented in [(&forged, &changed), (&permit, &req)] {
        for (stage, code, name, _) in STAGES {
            w.effects.inject(stage, code);
            assert_eq!(
                b.present_permit(presented.0, presented.1),
                unavailable("adapter", name),
                "{stage:?}"
            );
        }
    }
    assert_eq!(consumed_rows(&w.effects), 0);
    // Inside the transaction: mismatch, then MAC, then expiry, then stale, then replay.
    assert_eq!(
        b.present_permit(&forged, &changed),
        Refusal::PermitRequestMismatch
    );
    assert_eq!(b.present_permit(&forged, &req), Refusal::PermitInvalid);
    assert_eq!(b.present_permit(&permit, &req), Refusal::PermitExpired);
    // The expiry decision's time was persisted as the floor first.
    assert_eq!(w.ledger.time_floor().unwrap(), Some(now().plus_seconds(30)));
    let w2 = World::new("precedence-2");
    let b2 = w2.broker();
    let p2 = unconsumed_permit(&w2, &b2, &req);
    w2.active.activate(PolicyState::loaded(reference_v2()));
    assert_eq!(b2.present_permit(&p2, &req), Refusal::PermitStale);
    w2.active.activate(PolicyState::loaded(reference()));
    assert_eq!(
        b2.present_permit(&p2, &req),
        Refusal::ReconciliationRequired
    );
    // A consumed permit is a replay (after every other check passed).
    let consumed = exec(&b2, &file_write("k9"), None).permit.unwrap();
    let k9 = file_write("k9");
    assert_eq!(b2.present_permit(&consumed, &k9), Refusal::PermitReplayed);
    w2.effects
        .inject(fault::Stage::Body, rusqlite::ffi::SQLITE_IOERR);
    assert_eq!(
        b2.present_permit(&consumed, &k9),
        unavailable("adapter", "SQLITE_IOERR"),
        "storage first, even for a replay"
    );
    // A valid permit inspected under a failing (read-only) adapter commit: refused, and
    // a rollback never has an unknown outcome.
    let w3 = World::new("precedence-3");
    let b3 = w3.broker();
    let p3 = unconsumed_permit(&w3, &b3, &req);
    w3.effects
        .inject(fault::Stage::Commit, rusqlite::ffi::SQLITE_FULL);
    assert_eq!(
        b3.present_permit(&p3, &req),
        unavailable("adapter", "SQLITE_FULL")
    );
    assert_eq!(consumed_rows(&w3.effects), 0);
    assert_eq!(effect_rows(&w3.effects), 0);
    assert_eq!(
        b3.present_permit(&p3, &req),
        Refusal::ReconciliationRequired
    );
    assert_eq!(
        consumed_rows(&w.effects),
        0,
        "nothing consumed in the first world"
    );
}

#[test]
fn many_threads_presenting_an_unconsumed_permit_apply_nothing() {
    const WORKERS: usize = 64;
    let w = World::new("permit-race");
    let b = w.broker();
    let req = file_write("k1");
    let permit = unconsumed_permit(&w, &b, &req);
    let start = Barrier::new(WORKERS);
    let outcomes: Vec<_> = thread::scope(|s| {
        let handles: Vec<_> = (0..WORKERS)
            .map(|_| {
                let (b, start, permit, req) = (&b, &start, &permit, &req);
                s.spawn(move || {
                    start.wait();
                    b.present_permit(permit, req)
                })
            })
            .collect();
        handles.into_iter().map(|h| h.join().unwrap()).collect()
    });
    assert!(
        outcomes
            .iter()
            .all(|o| *o == Refusal::ReconciliationRequired),
        "{outcomes:?}"
    );
    assert_eq!(effect_rows(&w.effects), 0);
    assert_eq!(consumed_rows(&w.effects), 0);
    assert_eq!(status(&w.ledger, "k1").as_deref(), Some("dispatched"));
}

#[test]
fn sixty_four_threads_executing_one_request_dispatch_it_once() {
    const WORKERS: usize = 64;
    let w = World::new("same-key");
    let b = w.broker();
    let req = file_write("k1");
    let verified = verify(&req);
    let start = Barrier::new(WORKERS);
    let outcomes: Vec<_> = thread::scope(|s| {
        let handles: Vec<_> = (0..WORKERS)
            .map(|_| {
                let (b, start, verified) = (&b, &start, &verified);
                s.spawn(move || {
                    start.wait();
                    b.execute(verified, revision(), None).result
                })
            })
            .collect();
        handles
            .into_iter()
            .map(|h| h.join().unwrap())
            .collect::<Vec<_>>()
    });
    let completed = outcomes
        .iter()
        .filter(|o| **o == Ok(Executed::EffectCompleted))
        .count();
    let replays = outcomes
        .iter()
        .filter(|o| **o == Ok(Executed::Replayed(Replay::EffectCompleted)))
        .count();
    assert_eq!((completed, replays), (1, WORKERS - 1), "{outcomes:?}");
    assert_eq!(causes(&w.ledger, "k1"), ["reserve", "dispatch", "observe"]);
    assert_eq!(effect_rows(&w.effects), 1);
    assert_eq!(next_permit(&w.ledger), 1);
}

// ---------------------------------------------------------------------------
// Storage failures (S-7) at every stage, with the implied crash state (D-026).

const STAGES: [(fault::Stage, std::ffi::c_int, &str, TxStage); 3] = [
    (
        fault::Stage::Begin,
        rusqlite::ffi::SQLITE_BUSY,
        "SQLITE_BUSY",
        TxStage::Begin,
    ),
    (
        fault::Stage::Body,
        rusqlite::ffi::SQLITE_IOERR,
        "SQLITE_IOERR",
        TxStage::Body,
    ),
    (
        fault::Stage::Commit,
        rusqlite::ffi::SQLITE_FULL,
        "SQLITE_FULL",
        TxStage::Commit,
    ),
];

#[test]
fn storage_failure_at_every_lifecycle_stage_fails_closed_with_the_implied_crash_state() {
    for tx in ["reserve", "dispatch", "adapter", "observe"] {
        for (i, (stage, code, name, at)) in STAGES.into_iter().enumerate() {
            let case = format!("{tx} {at:?}");
            let w = World::new(&format!("stage-{tx}-{i}"));
            let b = w.broker();
            approve(b.grants(), &publish("k1"), "g1").unwrap();
            if tx == "adapter" {
                w.effects.inject(stage, code);
            } else {
                w.ledger.inject(tx, stage, code);
            }
            let e = exec(&b, &publish("k1"), Some("g1"));
            let refusal = e.result.clone().unwrap_err();
            assert_eq!(refusal, unavailable(tx, name), "{case}");
            assert_eq!(refusal.external_code(), "PROMOTION_REFUSED", "{case}");
            let (failed, failure) = e.evidence.failure().unwrap();
            assert_eq!(failed.as_str(), tx, "{case}");
            assert_eq!(failure.stage, at, "{case}");
            assert_eq!(failure.is_busy(), name == "SQLITE_BUSY", "{case}");
            // A failed COMMIT of a transaction that meant to commit has an unknown outcome
            // (D-035 item 4); begin and body failures committed nothing.
            let unknown = at == TxStage::Commit;
            assert_eq!(failure.outcome_unknown, unknown, "{case}");
            let suffix = if unknown { " outcome_unknown" } else { "" };
            assert_eq!(
                e.evidence.diagnostics(),
                vec![format!(
                    "{tx} {} transaction {tx} {name}{suffix}",
                    at.as_str()
                )],
                "{case}"
            );
            // The failure is audited with its stage and progress.
            let possible = if unknown {
                match tx {
                    "reserve" => Progress::Reserved,
                    "dispatch" => Progress::DispatchedWithoutEffect,
                    "adapter" => Progress::DispatchedWithEffect,
                    _ => Progress::Observed,
                }
            } else {
                e.evidence.progress()
            };
            assert_eq!(e.evidence.possible_progress(), possible, "{case}");
            let record: (String, String, String, String, String, i64) = w
                .ledger
                .db()
                .conn
                .query_row(
                    "SELECT f.transaction_name, f.stage, f.code, f.progress, f.possible_progress, \
                     f.outcome_unknown FROM failure_records f \
                     JOIN audit_refusals a ON a.seq = f.audit_seq \
                     WHERE a.code = 'LEDGER_UNAVAILABLE'",
                    [],
                    |r| {
                        Ok((
                            r.get(0)?,
                            r.get(1)?,
                            r.get(2)?,
                            r.get(3)?,
                            r.get(4)?,
                            r.get(5)?,
                        ))
                    },
                )
                .unwrap();
            assert_eq!(
                record,
                (
                    tx.to_string(),
                    at.as_str().to_string(),
                    name.to_string(),
                    e.evidence.progress().as_str().to_string(),
                    possible.as_str().to_string(),
                    i64::from(unknown)
                ),
                "{case}"
            );
            assert_sanitized(&e.evidence.diagnostics(), &w.dir);
            assert_sanitized(&[refusal.to_string()], &w.dir);
            // The crash state the committed transactions imply, checked in both stores.
            let (progress, receipt, permits, consumed, effects) = match tx {
                "reserve" => (Progress::NoChange, None, 0, 0, 0),
                "dispatch" => (Progress::Reserved, Some("reserved"), 0, 0, 0),
                "adapter" => (
                    Progress::DispatchedWithoutEffect,
                    Some("dispatched"),
                    1,
                    0,
                    0,
                ),
                _ => (Progress::DispatchedWithEffect, Some("dispatched"), 1, 1, 1),
            };
            assert_eq!(e.evidence.progress(), progress, "{case}");
            assert_eq!(status(&w.ledger, "k1").as_deref(), receipt, "{case}");
            assert_eq!(next_permit(&w.ledger), permits, "{case}");
            assert_eq!(e.permit.is_some(), permits == 1, "{case}");
            assert_eq!(consumed_rows(&w.effects), consumed, "{case}");
            assert_eq!(effect_rows(&w.effects), effects, "{case}");
            let grant_uses = if tx == "reserve" { 0 } else { 1 };
            assert_eq!(uses(&w.ledger, "g1"), Some(grant_uses), "{case}");
            assert_eq!(audited(&w.ledger, "LEDGER_UNAVAILABLE"), 1, "{case}");
            assert!(w.ledger.db().conn.is_autocommit(), "{case}");
            assert!(w.effects.db().conn.is_autocommit(), "{case}");
            assert_eq!(b.grants().key_locks().held(), 0, "{case}");
            // The retry: nothing committed or only reserved: it completes (resuming
            // without a second grant use); dispatched: never dispatched again.
            let retry = exec(&b, &publish("k1"), Some("g1")).result;
            match tx {
                "reserve" | "dispatch" => {
                    assert_eq!(retry, Ok(Executed::EffectCompleted), "{case}");
                    assert_eq!(effect_rows(&w.effects), 1, "{case}");
                }
                _ => {
                    assert_eq!(retry, Err(Refusal::ReconciliationRequired), "{case}");
                    assert_eq!(effect_rows(&w.effects), effects, "{case}");
                    assert_eq!(consumed_rows(&w.effects), consumed, "{case}");
                }
            }
            assert_eq!(uses(&w.ledger, "g1"), Some(1), "{case}");
            assert_eq!(
                causes(&w.ledger, "k1")
                    .iter()
                    .filter(|c| *c == "dispatch")
                    .count(),
                1,
                "{case}: exactly one dispatch"
            );
        }
    }
}

#[test]
fn storage_failure_in_reconcile_rolls_back_at_every_stage() {
    for (i, (stage, code, name, _)) in STAGES.into_iter().enumerate() {
        let w = World::new(&format!("reconcile-fault-{i}"));
        let b = w.broker();
        w.ledger
            .inject("observe", fault::Stage::Commit, rusqlite::ffi::SQLITE_IOERR);
        assert!(exec(&b, &file_write("k1"), None).result.is_err());
        assert_eq!(status(&w.ledger, "k1").as_deref(), Some("dispatched"));
        // The scan, the adapter's evidence read and the write each fail closed.
        for target in ["reconcile_scan", "evidence", "reconcile"] {
            if target == "evidence" {
                w.effects.inject_named("evidence", stage, code);
            } else {
                w.ledger.inject(target, stage, code);
            }
            let refusal = b.reconcile().unwrap_err();
            assert_eq!(refusal, unavailable(target, name), "{target} {stage:?}");
            assert_sanitized(&[refusal.to_string()], &w.dir);
            assert_eq!(status(&w.ledger, "k1").as_deref(), Some("dispatched"));
            assert_eq!(causes(&w.ledger, "k1"), ["reserve", "dispatch"]);
            assert!(w.ledger.db().conn.is_autocommit());
            assert!(w.effects.db().conn.is_autocommit());
        }
        // The effect was applied: the adapter's evidence records the outcome (D-035 item 3).
        assert_eq!(b.reconcile(), Ok(reconciled(1, 0, 0)), "{stage:?}");
        assert_eq!(status(&w.ledger, "k1").as_deref(), Some("observed_success"));
        assert_eq!(
            causes(&w.ledger, "k1"),
            ["reserve", "dispatch", "reconcile"]
        );
        assert_eq!(
            effect_rows(&w.effects),
            1,
            "reconcile never touches effects"
        );
    }
}

#[test]
fn a_floor_failure_inside_the_adapter_transaction_refuses_without_an_effect() {
    for (i, (stage, code, _, _)) in STAGES.into_iter().enumerate() {
        let w = World::new(&format!("adapter-floor-{i}"));
        let arm = || w.ledger.inject("time_floor", stage, code);
        let adapter = Hooked {
            inner: &w.effects,
            before: &arm,
            after: &nothing,
        };
        let b = w.hooked(&adapter);
        let e = exec(&b, &file_write("k1"), None);
        assert_eq!(e.result, Err(Refusal::TimeUnavailable), "{stage:?}");
        assert_eq!(e.evidence.progress(), Progress::DispatchedWithoutEffect);
        assert_eq!(status(&w.ledger, "k1").as_deref(), Some("dispatched"));
        assert_eq!(consumed_rows(&w.effects), 0);
        assert_eq!(effect_rows(&w.effects), 0);
    }
}

#[test]
fn an_ambiguous_outcome_is_never_redispatched_and_reconciles_from_adapter_evidence() {
    let w = World::new("ambiguous");
    let b = w.broker();
    approve(b.grants(), &publish("k1"), "g1").unwrap();
    // The effect was applied, but the observation was lost: the caller cannot know.
    w.ledger
        .inject("observe", fault::Stage::Commit, rusqlite::ffi::SQLITE_IOERR);
    let e = exec(&b, &publish("k1"), Some("g1"));
    assert_eq!(e.result, Err(unavailable("observe", "SQLITE_IOERR")));
    assert_eq!(effect_rows(&w.effects), 1);
    for _ in 0..3 {
        assert_eq!(
            exec(&b, &publish("k1"), Some("g1")).result,
            Err(Refusal::ReconciliationRequired)
        );
    }
    // The crash between adapter commit and receipt observation: the adapter holds durable
    // evidence for exactly this receipt's permit, so reconciliation records the outcome by
    // a checked transition (D-035 item 3); repeating it changes nothing.
    assert_eq!(b.reconcile(), Ok(reconciled(1, 0, 0)));
    assert_eq!(b.reconcile(), Ok(reconciled(0, 0, 0)));
    assert_eq!(status(&w.ledger, "k1").as_deref(), Some("observed_success"));
    assert_eq!(
        causes(&w.ledger, "k1"),
        ["reserve", "dispatch", "reconcile"]
    );
    assert_eq!(
        ledger_i64(
            &w.ledger,
            "SELECT count(*) FROM reconciliations WHERE outcome = 'observed_success' \
             AND evidence = 'adapter_record' AND permit_number = 1"
        ),
        1
    );
    assert_eq!(
        exec(&b, &publish("k1"), Some("g1")).result,
        Ok(Executed::Replayed(Replay::EffectCompleted))
    );
    // The client-held permit cannot apply a second effect either.
    assert_eq!(
        b.present_permit(&e.permit.unwrap(), &publish("k1")),
        Refusal::PermitReplayed
    );
    assert_eq!(effect_rows(&w.effects), 1);
    assert_eq!(ledger_i64(&w.ledger, "SELECT count(*) FROM receipts"), 1);
    assert_eq!(uses(&w.ledger, "g1"), Some(1));
}

#[test]
fn a_receipt_changed_behind_the_broker_is_a_state_conflict_not_corruption() {
    let w = World::new("state-conflict");
    // Another party (an unsupported second process) reconciles the receipt while the
    // effect is applied: T-observe matches no row.
    let tamper = || {
        w.ledger
            .db()
            .conn
            .execute(
                "UPDATE receipts SET status = 'unknown_reconcile' WHERE idempotency_key = 'k1'",
                [],
            )
            .unwrap();
    };
    let adapter = Hooked {
        inner: &w.effects,
        before: &nothing,
        after: &tamper,
    };
    let b = w.hooked(&adapter);
    let e = exec(&b, &file_write("k1"), None);
    assert_eq!(e.result, Err(unavailable("observe", "STATE_CONFLICT")));
    let (name, failure) = e.evidence.failure().unwrap();
    assert_eq!((name, failure.stage), (TxName::Observe, TxStage::Body));
    assert_eq!(e.evidence.progress(), Progress::DispatchedWithEffect);
    assert_sanitized(&e.evidence.diagnostics(), &w.dir);
    assert!(!e.evidence.diagnostics()[0].contains("CORRUPT"));
    let check: String = w
        .ledger
        .db()
        .conn
        .query_row("PRAGMA integrity_check", [], |r| r.get(0))
        .unwrap();
    assert_eq!(check, "ok");
    assert_eq!(effect_rows(&w.effects), 1);
}

// ---------------------------------------------------------------------------
// Reconciliation and policy activation racing in-flight operations.

/// Block the first adapter call until `go` is set, then hold it a little longer.
fn pause_first(entered: &Mutex<Option<mpsc::Sender<()>>>, go: &AtomicBool) {
    if let Some(tx) = entered.lock().unwrap().take() {
        tx.send(()).unwrap();
        let start = Instant::now();
        while !go.load(Ordering::SeqCst) {
            assert!(start.elapsed() < Duration::from_secs(10));
            thread::sleep(Duration::from_millis(1));
        }
        thread::sleep(Duration::from_millis(200));
    }
}

#[test]
fn reconcile_waits_for_an_in_flight_execution_and_never_changes_it() {
    let w = World::new("reconcile-race");
    {
        // A receipt left `dispatched` by an operation that already returned.
        let b = w.broker();
        w.ledger
            .inject("observe", fault::Stage::Commit, rusqlite::ffi::SQLITE_IOERR);
        assert!(exec(&b, &file_write("k0"), None).result.is_err());
    }
    let (entered_tx, entered_rx) = mpsc::channel();
    let entered = Mutex::new(Some(entered_tx));
    let attempting = AtomicBool::new(false);
    let reconciled = AtomicBool::new(false);
    let seen = AtomicBool::new(false);
    let before = || {
        pause_first(&entered, &attempting);
        if reconciled.load(Ordering::SeqCst) {
            seen.store(true, Ordering::SeqCst);
        }
    };
    let after = || {
        if reconciled.load(Ordering::SeqCst) {
            seen.store(true, Ordering::SeqCst);
        }
    };
    let adapter = Hooked {
        inner: &w.effects,
        before: &before,
        after: &after,
    };
    let b = w.hooked(&adapter);
    let (executed, count) = thread::scope(|s| {
        let execution = s.spawn(|| exec(&b, &file_write("k1"), None).result);
        entered_rx.recv_timeout(Duration::from_secs(10)).unwrap();
        // k1 is now `dispatched` and in flight.
        assert_eq!(status(&w.ledger, "k1").as_deref(), Some("dispatched"));
        let reconcile = s.spawn(|| {
            attempting.store(true, Ordering::SeqCst);
            let n = b.reconcile();
            reconciled.store(true, Ordering::SeqCst);
            n
        });
        (execution.join().unwrap(), reconcile.join().unwrap())
    });
    assert_eq!(executed, Ok(Executed::EffectCompleted));
    assert!(
        !seen.load(Ordering::SeqCst),
        "reconcile ran inside an execution"
    );
    assert_eq!(
        count,
        Ok(super::execute_tests::reconciled(1, 0, 0)),
        "only the receipt whose operation had returned"
    );
    assert_eq!(status(&w.ledger, "k0").as_deref(), Some("observed_success"));
    assert_eq!(status(&w.ledger, "k1").as_deref(), Some("observed_success"));
}

#[test]
fn concurrent_executions_and_reconciles_never_interfere() {
    const EXECUTIONS: usize = 48;
    const RECONCILES: usize = 16;
    let w = World::new("reconcile-stress");
    let b = w.broker();
    // Some receipts left dispatched before the group (returned operations).
    for k in ["c1", "c2", "c3"] {
        w.ledger
            .inject("observe", fault::Stage::Body, rusqlite::ffi::SQLITE_IOERR);
        assert!(exec(&b, &file_write(k), None).result.is_err());
    }
    let requests: Vec<_> = (0..EXECUTIONS)
        .map(|i| verify(&file_write(&format!("k{i}"))))
        .collect();
    let start = Barrier::new(EXECUTIONS + RECONCILES);
    let total = AtomicUsize::new(0);
    let results: Vec<_> = thread::scope(|s| {
        let mut handles = Vec::new();
        for v in &requests {
            let (b, start) = (&b, &start);
            handles.push(s.spawn(move || {
                start.wait();
                b.execute(v, revision(), None).result
            }));
        }
        for _ in 0..RECONCILES {
            let (b, start, total) = (&b, &start, &total);
            s.spawn(move || {
                start.wait();
                let n = b.reconcile().unwrap().changed();
                total.fetch_add(usize::try_from(n).unwrap(), Ordering::SeqCst);
            });
        }
        handles.into_iter().map(|h| h.join().unwrap()).collect()
    });
    for r in &results {
        assert_eq!(*r, Ok(Executed::EffectCompleted));
    }
    // c1..c3 applied their effects before observe failed: each is recorded once from
    // the adapter's evidence, by exactly one of the concurrent reconciliations.
    assert_eq!(total.load(Ordering::SeqCst), 3);
    assert_eq!(
        ledger_i64(&w.ledger, "SELECT count(*) FROM reconciliations"),
        3
    );
    assert_eq!(
        ledger_i64(
            &w.ledger,
            "SELECT count(*) FROM receipts WHERE status = 'observed_success'"
        ),
        i64::try_from(EXECUTIONS).unwrap() + 3
    );
    assert_eq!(audited(&w.ledger, "LEDGER_UNAVAILABLE"), 3);
    assert_eq!(
        effect_rows(&w.effects),
        i64::try_from(EXECUTIONS).unwrap() + 3
    );
}

#[test]
fn activation_waits_for_dispatch_and_effect_authorization_under_one_hold() {
    let w = World::new("activation-dispatch");
    let (entered_tx, entered_rx) = mpsc::channel();
    let entered = Mutex::new(Some(entered_tx));
    let attempting = AtomicBool::new(false);
    let activated = AtomicBool::new(false);
    let seen = AtomicBool::new(false);
    let before = || {
        pause_first(&entered, &attempting);
        if activated.load(Ordering::SeqCst) {
            seen.store(true, Ordering::SeqCst);
        }
    };
    let after = || {
        if activated.load(Ordering::SeqCst) {
            seen.store(true, Ordering::SeqCst);
        }
    };
    let adapter = Hooked {
        inner: &w.effects,
        before: &before,
        after: &after,
    };
    let b = w.hooked(&adapter);
    let e = thread::scope(|s| {
        let execution = s.spawn(|| exec(&b, &file_write("k1"), None));
        entered_rx.recv_timeout(Duration::from_secs(10)).unwrap();
        // Dispatched under the old policy; the effect is not yet authorized.
        assert_eq!(status(&w.ledger, "k1").as_deref(), Some("dispatched"));
        s.spawn(|| {
            attempting.store(true, Ordering::SeqCst);
            w.active.activate(PolicyState::loaded(reference_v2()));
            activated.store(true, Ordering::SeqCst);
        });
        execution.join().unwrap()
    });
    assert_eq!(e.result, Ok(Executed::EffectCompleted));
    assert!(
        !seen.load(Ordering::SeqCst),
        "the activation completed between dispatch and effect authorization"
    );
    assert!(activated.load(Ordering::SeqCst));
    let permit = e.permit.unwrap();
    assert_eq!(permit.policy_bundle_sha(), reference().sha.as_str());
    // After the activation the same request is replayed (never redispatched), and a
    // permit of the old bundle is stale.
    assert_eq!(
        exec(&b, &file_write("k1"), None).result,
        Ok(Executed::Replayed(Replay::EffectCompleted))
    );
    assert_eq!(
        b.present_permit(&permit, &file_write("k1")),
        Refusal::PermitStale
    );
    assert_eq!(effect_rows(&w.effects), 1);
}

#[test]
fn activation_waits_for_present_permit_and_a_later_presentation_is_stale() {
    let w = World::new("activation-present");
    let req1 = file_write("k1");
    let req2 = file_write("k2");
    let (p1, p2) = {
        let b = w.broker();
        (
            unconsumed_permit(&w, &b, &req1),
            unconsumed_permit(&w, &b, &req2),
        )
    };
    let (entered_tx, entered_rx) = mpsc::channel();
    let entered = Mutex::new(Some(entered_tx));
    let attempting = AtomicBool::new(false);
    let activated = AtomicBool::new(false);
    let seen = AtomicBool::new(false);
    let before = || pause_first(&entered, &attempting);
    let after = || {
        if activated.load(Ordering::SeqCst) {
            seen.store(true, Ordering::SeqCst);
        }
    };
    let adapter = Hooked {
        inner: &w.effects,
        before: &before,
        after: &after,
    };
    let b = w.hooked(&adapter);
    let presented = thread::scope(|s| {
        let presentation = s.spawn(|| b.present_permit(&p1, &req1));
        entered_rx.recv_timeout(Duration::from_secs(10)).unwrap();
        s.spawn(|| {
            attempting.store(true, Ordering::SeqCst);
            w.active.activate(PolicyState::loaded(reference_v2()));
            activated.store(true, Ordering::SeqCst);
        });
        presentation.join().unwrap()
    });
    // Verified under the old policy while the activation waited; it never applies.
    assert_eq!(presented, Refusal::ReconciliationRequired);
    assert!(!seen.load(Ordering::SeqCst));
    assert_eq!(b.present_permit(&p2, &req2), Refusal::PermitStale);
    assert_eq!(b.present_permit(&p1, &req1), Refusal::PermitStale);
    assert_eq!(effect_rows(&w.effects), 0);
}

// ---------------------------------------------------------------------------
// Restart persistence.

#[test]
fn restart_resumes_reserved_and_never_redispatches_dispatched_or_reconciled() {
    let w = World::new("restart");
    let dir = w.dir.clone();
    let (k3_permit, k2_permit) = {
        let b = w.broker();
        for k in ["k1", "k2", "k3", "k4"] {
            approve(b.grants(), &publish(k), &format!("g-{k}")).unwrap();
        }
        // k1: dispatch failed: reserved, grant use kept.
        w.ledger.inject(
            "dispatch",
            fault::Stage::Commit,
            rusqlite::ffi::SQLITE_IOERR,
        );
        assert!(exec(&b, &publish("k1"), Some("g-k1")).result.is_err());
        // k2: observe failed: dispatched with the effect.
        w.ledger
            .inject("observe", fault::Stage::Begin, rusqlite::ffi::SQLITE_BUSY);
        let k2 = exec(&b, &publish("k2"), Some("g-k2"));
        assert!(k2.result.is_err());
        // k3: completed.
        let k3 = exec(&b, &publish("k3"), Some("g-k3"));
        assert_eq!(k3.result, Ok(Executed::EffectCompleted));
        // k4: adapter failed, then reconciled.
        w.effects
            .inject(fault::Stage::Body, rusqlite::ffi::SQLITE_IOERR);
        assert!(exec(&b, &publish("k4"), Some("g-k4")).result.is_err());
        // k2: the adapter's evidence records its effect; k4: no evidence, uncertain.
        assert_eq!(b.reconcile(), Ok(reconciled(1, 0, 1)), "k2 and k4");
        (k3.permit.unwrap(), k2.permit.unwrap())
    };
    let before_permits = next_permit(&w.ledger);
    assert_eq!(before_permits, 3);
    let World {
        ledger,
        effects,
        active,
        clock,
        key,
        ..
    } = w;
    drop(ledger);
    drop(effects);
    clock.set(now().plus_seconds(5));
    let ledger = SqliteLedger::open(&dir.join("ledger.db")).unwrap();
    let effects = SqliteEffectStore::open(&dir.join("effects.db"), ledger.ledger_id()).unwrap();
    assert_eq!(LedgerStore::owner_epoch(&ledger), 2);
    let b = Broker::new(&active, &clock, &ledger, &effects, &key);
    // k1 resumes without a second grant use and gets a new, unused permit id.
    let k1 = exec(&b, &publish("k1"), Some("g-k1"));
    assert_eq!(k1.result, Ok(Executed::EffectCompleted));
    assert_eq!(k1.permit.unwrap().permit_id(), "permit-4");
    assert_eq!(uses(&ledger, "g-k1"), Some(1));
    assert_eq!(causes(&ledger, "k1"), ["reserve", "dispatch", "observe"]);
    // k2 replays its reconciled outcome; k4 stays uncertain; neither is dispatched again.
    for (k, g, want) in [
        (
            "k2",
            "g-k2",
            Ok(Executed::Replayed(Replay::EffectCompleted)),
        ),
        ("k4", "g-k4", Err(Refusal::ReconciliationRequired)),
    ] {
        assert_eq!(exec(&b, &publish(k), Some(g)).result, want, "{k}");
        assert_eq!(
            causes(&ledger, k)
                .iter()
                .filter(|c| *c == "dispatch")
                .count(),
            1
        );
    }
    assert_eq!(effects_for(&effects, "k4"), 0);
    // k3 replays; its consumed permit and k2's survive the restart.
    assert_eq!(
        exec(&b, &publish("k3"), Some("g-k3")).result,
        Ok(Executed::Replayed(Replay::EffectCompleted))
    );
    assert_eq!(
        b.present_permit(&k3_permit, &publish("k3")),
        Refusal::PermitReplayed
    );
    assert_eq!(
        b.present_permit(&k2_permit, &publish("k2")),
        Refusal::PermitReplayed
    );
    // Recovery after the restart changes nothing further.
    assert_eq!(b.reconcile(), Ok(reconciled(0, 0, 0)));
    assert_eq!(effect_rows(&effects), 3, "k2, k3, k1");
    assert_eq!(next_permit(&ledger), 4);
    for k in ["k1", "k2", "k3"] {
        assert_eq!(effects_for(&effects, k), 1, "{k}");
    }
}

// ---------------------------------------------------------------------------
// The effect store is separate and verified.

#[test]
fn the_effect_store_is_separate_from_the_ledger_and_verified_on_open() {
    let w = World::new("separate");
    let tables = |sql: &str| -> Vec<String> {
        let db = w.ledger.db();
        let mut s = db.conn.prepare(sql).unwrap();
        s.query_map([], |r| r.get(0))
            .unwrap()
            .collect::<rusqlite::Result<_>>()
            .unwrap()
    };
    let ledger_tables = tables("SELECT name FROM sqlite_master WHERE type = 'table'");
    assert!(
        !ledger_tables
            .iter()
            .any(|t| t.contains("adapter") || t == "effects" || t == "consumed_permits"),
        "{ledger_tables:?}"
    );
    let dir = w.dir.clone();
    let id = w.ledger.ledger_id().clone();
    drop(w);
    // Neither file opens as the other.
    assert!(SqliteEffectStore::open(&dir.join("ledger.db"), &id).is_err());
    assert!(SqliteLedger::open(&dir.join("effects.db")).is_err());
    assert!(SqliteEffectStore::open(&dir.join("missing.db"), &id).is_err());
    assert!(SqliteEffectStore::create(&dir.join("effects.db"), &id).is_err());
    assert!(SqliteEffectStore::open(&dir.join("effects.db"), &id).is_ok());
    // A tampered schema (an extra trigger) is refused.
    {
        let c = rusqlite::Connection::open(dir.join("effects.db")).unwrap();
        c.execute_batch("CREATE TRIGGER t AFTER INSERT ON effects BEGIN DELETE FROM effects; END;")
            .unwrap();
    }
    assert!(SqliteEffectStore::open(&dir.join("effects.db"), &id).is_err());
}

#[test]
fn every_refusal_is_audited_after_the_locks_are_released() {
    let w = World::new("audit");
    let b = w.broker();
    w.ledger
        .inject("observe", fault::Stage::Commit, rusqlite::ffi::SQLITE_IOERR);
    assert!(exec(&b, &file_write("k1"), None).result.is_err());
    assert_eq!(
        exec(&b, &file_write("k1"), None).result,
        Err(Refusal::ReconciliationRequired)
    );
    assert_eq!(audited(&w.ledger, "LEDGER_UNAVAILABLE"), 1);
    assert_eq!(audited(&w.ledger, "RECONCILIATION_REQUIRED"), 1);
    // An audit failure does not change the answer.
    w.ledger
        .inject("audit", fault::Stage::Commit, rusqlite::ffi::SQLITE_IOERR);
    assert_eq!(
        exec(&b, &file_write("k1"), None).result,
        Err(Refusal::ReconciliationRequired)
    );
    assert_eq!(audited(&w.ledger, "RECONCILIATION_REQUIRED"), 1);
    // The gate is free: an activation completes immediately.
    w.active.activate(PolicyState::loaded(reference()));
    assert_eq!(b.grants().key_locks().held(), 0);
}
