//! Real process kills (M3-04; risk 12, crash at every boundary). The unit test binary
//! re-executes itself as a broker child process (`WRIT_KILL_CHILD`) on a disposable
//! ledger and effect store, which is killed with SIGKILL:
//!
//! - **at named boundaries**: the child arms a test-only hang fault and announces
//!   `AT <transaction> <stage>` when it blocks there, before `BEGIN IMMEDIATE` (`Begin`)
//!   or with the transaction open after its statements (`Body`); the parent kills it at
//!   that point and checks the exact crash state, restart and retry;
//! - **at seeded random times** while three threads run a mixed workload (allow and
//!   approval executions, failing effects, permit replays, reconciliation); the parent
//!   records whether each run was killed mid-run or had finished first.
//!
//! After every kill the parent restarts (opens the ledger and the effect store again)
//! and checks the invariants in the ledger and, independently, in the effect store: no
//! grant overuse, at most one dispatch per key, no effect without a consumed permit, at
//! most one effect per permit and per key, receipt states matching their events, and
//! every `dispatched` or `unknown_reconcile` receipt answering `RECONCILIATION_REQUIRED`
//! without a new effect. Only child processes started here are killed.

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::collections::{BTreeMap, BTreeSet};
use std::io::{BufRead, BufReader, Write};
use std::os::unix::process::ExitStatusExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{mpsc, Mutex};
use std::thread;
use std::time::Duration;

use writ_broker::execute::{Broker, Executed};
use writ_broker::keys::PermitKey;
use writ_broker::permit::Permit;
use writ_broker::policy::{ActivePolicy, PolicyState};
use writ_broker::refusal::Refusal;
use writ_broker::writ_kernel::json::Value;

use super::grant_tests::{
    approve, file_write, label, now, publish, reference, revision, verify, TestClock,
};
use super::{fault, SqliteEffectStore, SqliteLedger};

const CHILD: &str = "WRIT_KILL_CHILD";
const SEED: u64 = 20_261_112;
const RANDOM_RUNS: u64 = 16;
const CHILD_THREADS: usize = 3;
/// Operations per child thread in a long run; one run in four is short (10 per
/// thread) so that some runs finish before their kill time.
const OPS_PER_THREAD: u64 = 1000;
/// Upper bound of the seeded kill delay after the child announced `START`.
const MAX_KILL_DELAY_MS: u64 = 800;
/// The boundary tests' key and grant (a workload key, see [`request_of`]).
const BOUNDARY_KEY: &str = "p-1";
const BOUNDARY_GRANT: &str = "g-p-1";

fn permit_key() -> PermitKey {
    PermitKey::from_bytes(vec![0x44; 32]).unwrap()
}

/// xorshift64* (deterministic, seeded; no dependency).
struct Rng(u64);

impl Rng {
    fn new(seed: u64) -> Rng {
        Rng(seed.wrapping_mul(0x9E37_79B9_7F4A_7C15) | 1)
    }

    fn next(&mut self) -> u64 {
        self.0 ^= self.0 >> 12;
        self.0 ^= self.0 << 25;
        self.0 ^= self.0 >> 27;
        self.0.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }

    fn below(&mut self, n: u64) -> u64 {
        self.next() % n
    }
}

/// The request of a workload key: `a-<n>` is an allowed `file.write`, `p-<n>` a
/// `repository.publish` needing grant `g-p-<n>`.
fn request_of(key: &str) -> (Value, Option<String>) {
    if key.starts_with("p-") {
        (publish(key), Some(format!("g-{key}")))
    } else {
        (file_write(key), None)
    }
}

fn say(line: &str) {
    let out = std::io::stdout();
    let mut out = out.lock();
    writeln!(out, "{line}").unwrap();
    out.flush().unwrap();
}

// ---------------------------------------------------------------------------
// The child process.

/// Child entry point: inert unless the parent set the environment variable.
#[test]
fn kill_child() {
    let Ok(spec) = std::env::var(CHILD) else {
        return;
    };
    let parts: Vec<&str> = spec.split('|').collect();
    let dir = PathBuf::from(parts[1]);
    let ledger = SqliteLedger::open(&dir.join("ledger.db")).unwrap();
    let effects = SqliteEffectStore::open(&dir.join("effects.db")).unwrap();
    let active = ActivePolicy::new(PolicyState::loaded(reference()));
    let clock = TestClock::at(now());
    let key = permit_key();
    let b = Broker::new(&active, &clock, &ledger, &effects, &key);
    match parts[0] {
        "boundary" => boundary_child(&b, &ledger, &effects, parts[2], parts[3]),
        "random" => random_child(
            &b,
            &effects,
            parts[2].parse().unwrap(),
            parts[3].parse().unwrap(),
        ),
        other => panic!("unknown child mode {other}"),
    }
}

fn stage_of(name: &str) -> fault::Stage {
    match name {
        "Begin" => fault::Stage::Begin,
        "Body" => fault::Stage::Body,
        other => panic!("unknown stage {other}"),
    }
}

fn boundary_child(
    b: &Broker<'_, SqliteLedger, SqliteEffectStore>,
    ledger: &SqliteLedger,
    effects: &SqliteEffectStore,
    tx: &str,
    stage: &str,
) {
    approve(b.grants(), &publish(BOUNDARY_KEY), BOUNDARY_GRANT).unwrap();
    if tx == "reconcile" {
        // A receipt left `dispatched` by an operation that returned.
        ledger.inject("observe", fault::Stage::Commit, rusqlite::ffi::SQLITE_IOERR);
        assert!(b
            .execute(
                &verify(&publish(BOUNDARY_KEY)),
                revision(),
                Some(&label(BOUNDARY_GRANT))
            )
            .result
            .is_err());
        ledger.inject_hang("reconcile", stage_of(stage));
        say("READY");
        let _ = b.reconcile();
    } else {
        if tx == "adapter" {
            effects.inject_hang(stage_of(stage));
        } else {
            ledger.inject_hang(tx, stage_of(stage));
        }
        say("READY");
        let _ = b.execute(
            &verify(&publish(BOUNDARY_KEY)),
            revision(),
            Some(&label(BOUNDARY_GRANT)),
        );
    }
    say("FINISHED");
}

fn random_child(
    b: &Broker<'_, SqliteLedger, SqliteEffectStore>,
    effects: &SqliteEffectStore,
    seed: u64,
    ops_per_thread: u64,
) {
    say("START");
    let last: Mutex<Option<(Permit, Value)>> = Mutex::new(None);
    let done = AtomicUsize::new(0);
    thread::scope(|s| {
        for t in 0..CHILD_THREADS {
            let (last, done) = (&last, &done);
            s.spawn(move || {
                let mut rng = Rng::new(seed.wrapping_add(t as u64 * 7919));
                for op in 0..ops_per_thread {
                    let outcome = match rng.below(10) {
                        0 => format!("reconcile {:?}", b.reconcile()),
                        1 => {
                            let held = last.lock().unwrap().clone();
                            match held {
                                Some((permit, req)) => {
                                    format!("present {:?}", b.present_permit(&permit, &req))
                                }
                                None => "present none".to_string(),
                            }
                        }
                        r => {
                            // Keys shared by every run (retries across restarts) and
                            // keys of this run (new dispatches throughout the run).
                            let space = if rng.below(10) < 3 {
                                "x".to_string()
                            } else {
                                seed.to_string()
                            };
                            let key = if r <= 3 {
                                format!("p-{space}-{}", rng.below(40))
                            } else {
                                format!("a-{space}-{}", rng.below(200))
                            };
                            let (req, grant) = request_of(&key);
                            if let Some(g) = &grant {
                                let _ = approve(b.grants(), &req, g);
                            }
                            effects.set_effects_fail(rng.below(6) == 0);
                            let g = grant.as_deref().map(label);
                            let e = b.execute(&verify(&req), revision(), g.as_ref());
                            if let Some(p) = e.permit {
                                *last.lock().unwrap() = Some((p, req));
                            }
                            format!(
                                "execute {key} {:?}",
                                e.result.map_err(|r| r.code().to_string())
                            )
                        }
                    };
                    say(&format!("OP {t} {op} {outcome}"));
                    done.fetch_add(1, Ordering::SeqCst);
                }
            });
        }
    });
    say(&format!("FINISHED {}", done.load(Ordering::SeqCst)));
}

// ---------------------------------------------------------------------------
// The parent.

fn disposable(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("writ-kill-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    drop(SqliteLedger::create(&d.join("ledger.db")).unwrap());
    drop(SqliteEffectStore::create(&d.join("effects.db")).unwrap());
    d
}

fn spawn_child(spec: &str) -> Child {
    Command::new(std::env::current_exe().unwrap())
        .args([
            "kill_tests::kill_child",
            "--exact",
            "--nocapture",
            "--test-threads=1",
            "--quiet",
        ])
        .env(CHILD, spec)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .unwrap()
}

/// Drain the child's stdout on a thread; lines arrive on the returned channel.
fn lines(child: &mut Child) -> mpsc::Receiver<String> {
    let out = BufReader::new(child.stdout.take().unwrap());
    let (tx, rx) = mpsc::channel();
    thread::spawn(move || {
        for line in out.lines() {
            let Ok(line) = line else { break };
            if tx.send(line).is_err() {
                break;
            }
        }
    });
    rx
}

fn wait_for(rx: &mpsc::Receiver<String>, prefix: &str) -> String {
    loop {
        let line = rx
            .recv_timeout(Duration::from_secs(30))
            .unwrap_or_else(|e| panic!("no {prefix} line from the child: {e}"));
        if line.starts_with(prefix) {
            return line;
        }
    }
}

/// SIGKILL the child (only processes started by these tests) and confirm the signal.
fn kill(mut child: Child) {
    child.kill().unwrap();
    let status = child.wait().unwrap();
    assert_eq!(status.signal(), Some(9), "{status:?}");
}

fn rows<T: rusqlite::types::FromSql>(conn: &rusqlite::Connection, sql: &str) -> Vec<Vec<T>> {
    let mut s = conn.prepare(sql).unwrap();
    let n = s.column_count();
    s.query_map([], |r| (0..n).map(|i| r.get(i)).collect())
        .unwrap()
        .collect::<rusqlite::Result<_>>()
        .unwrap()
}

/// Facts read after a restart, from the ledger and independently from the effect store.
#[derive(Debug)]
struct Facts {
    /// (key) -> receipt status.
    receipts: BTreeMap<String, String>,
    /// (key) -> number of applied effects.
    effects: BTreeMap<String, i64>,
    consumed: i64,
}

/// Check every invariant on a restarted ledger and effect store; returns the facts.
fn check_invariants(ledger: &SqliteLedger, effects: &SqliteEffectStore) -> Facts {
    let l = ledger.db();
    let e = effects.db();
    for conn in [&l.conn, &e.conn] {
        let ok: String = conn
            .query_row("PRAGMA integrity_check", [], |r| r.get(0))
            .unwrap();
        assert_eq!(ok, "ok");
    }
    // No grant overuse: uses within bounds, one grant_uses row per use, one key per grant.
    for g in rows::<Value2>(&l.conn, "SELECT label, uses, max_uses FROM grants") {
        let (label, used, max) = (g[0].text(), g[1].int(), g[2].int());
        assert!(used <= max && max == 1, "{label}: {used}/{max}");
        let use_rows: i64 = l
            .conn
            .query_row(
                "SELECT count(*) FROM grant_uses WHERE label = ?1",
                [&label],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(use_rows, used, "{label}");
    }
    let next_permit: i64 = l
        .conn
        .query_row(
            "SELECT value FROM counters WHERE name = 'next_permit'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    // Receipt states match their events; at most one dispatch per key.
    let mut receipts = BTreeMap::new();
    let mut dispatches: i64 = 0;
    for r in rows::<Value2>(&l.conn, "SELECT idempotency_key, status FROM receipts") {
        let (key, status) = (r[0].text(), r[1].text());
        let history: Vec<(String, String)> = {
            let mut s = l
                .conn
                .prepare(
                    "SELECT status, cause FROM receipt_events WHERE idempotency_key = ?1 ORDER BY seq",
                )
                .unwrap();
            s.query_map([&key], |r| Ok((r.get(0)?, r.get(1)?)))
                .unwrap()
                .collect::<rusqlite::Result<_>>()
                .unwrap()
        };
        let mut state = None::<String>;
        for (to, cause) in &history {
            let allowed = matches!(
                (state.as_deref(), to.as_str(), cause.as_str()),
                (None, "reserved", "reserve")
                    | (Some("reserved"), "dispatched", "dispatch")
                    | (
                        Some("dispatched"),
                        "observed_success" | "observed_failure",
                        "observe"
                    )
                    | (Some("dispatched"), "unknown_reconcile", "reconcile")
            );
            assert!(allowed, "{key}: {history:?}");
            state = Some(to.clone());
        }
        assert_eq!(
            state.as_deref(),
            Some(status.as_str()),
            "{key}: {history:?}"
        );
        if history.iter().any(|(_, c)| c == "dispatch") {
            dispatches += 1;
        }
        receipts.insert(key, status);
    }
    let receipt_events: i64 = l
        .conn
        .query_row(
            "SELECT count(*) FROM receipt_events WHERE idempotency_key NOT IN (SELECT idempotency_key FROM receipts)",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(receipt_events, 0);
    assert_eq!(
        dispatches, next_permit,
        "one permit number per committed dispatch"
    );
    // The effect store: no effect without a consumed permit; one effect per permit
    // and per key; every consumed permit was issued by a committed dispatch.
    let orphans: i64 = e
        .conn
        .query_row(
            "SELECT count(*) FROM effects WHERE permit_id NOT IN (SELECT permit_id FROM consumed_permits)",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(orphans, 0, "effect without a consumed permit");
    let consumed_ids: Vec<String> =
        rows::<Value2>(&e.conn, "SELECT permit_id FROM consumed_permits")
            .into_iter()
            .map(|r| r[0].text())
            .collect();
    for id in &consumed_ids {
        let n: i64 = id.strip_prefix("permit-").unwrap().parse().unwrap();
        assert!(n >= 1 && n <= next_permit, "{id} was never dispatched");
    }
    let per_permit = rows::<Value2>(
        &e.conn,
        "SELECT permit_id, count(*) FROM effects GROUP BY permit_id HAVING count(*) > 1",
    );
    assert!(per_permit.is_empty(), "{per_permit:?}");
    let mut per_key = BTreeMap::new();
    for r in rows::<Value2>(
        &e.conn,
        "SELECT idempotency_key, count(*) FROM effects GROUP BY idempotency_key",
    ) {
        let (key, n) = (r[0].text(), r[1].int());
        assert_eq!(n, 1, "{key}: duplicate effect");
        let status = receipts.get(&key).map(String::as_str);
        assert!(
            matches!(
                status,
                Some("dispatched" | "unknown_reconcile" | "observed_success")
            ),
            "{key}: effect with receipt {status:?}"
        );
        per_key.insert(key, n);
    }
    for (key, status) in &receipts {
        let n = per_key.get(key).copied().unwrap_or(0);
        match status.as_str() {
            "observed_success" => assert_eq!(n, 1, "{key}"),
            "observed_failure" | "reserved" => assert_eq!(n, 0, "{key}"),
            _ => {}
        }
    }
    Facts {
        receipts,
        effects: per_key,
        consumed: i64::try_from(consumed_ids.len()).unwrap(),
    }
}

/// A loosely typed SQLite value for the inspection queries.
#[derive(Debug)]
enum Value2 {
    Int(i64),
    Text(String),
}

impl Value2 {
    fn text(&self) -> String {
        match self {
            Value2::Text(t) => t.clone(),
            Value2::Int(i) => i.to_string(),
        }
    }

    fn int(&self) -> i64 {
        match self {
            Value2::Int(i) => *i,
            Value2::Text(t) => t.parse().unwrap(),
        }
    }
}

impl rusqlite::types::FromSql for Value2 {
    fn column_result(v: rusqlite::types::ValueRef<'_>) -> rusqlite::types::FromSqlResult<Self> {
        match v {
            rusqlite::types::ValueRef::Integer(i) => Ok(Value2::Int(i)),
            other => Ok(Value2::Text(String::column_result(other)?)),
        }
    }
}

/// After a restart every `dispatched` or `unknown_reconcile` receipt answers
/// `RECONCILIATION_REQUIRED` and applies no new effect.
fn uncertain_receipts_are_never_redispatched(dir: &Path, facts: &Facts) {
    let ledger = SqliteLedger::open(&dir.join("ledger.db")).unwrap();
    let effects = SqliteEffectStore::open(&dir.join("effects.db")).unwrap();
    let active = ActivePolicy::new(PolicyState::loaded(reference()));
    let clock = TestClock::at(now());
    let key = permit_key();
    let b = Broker::new(&active, &clock, &ledger, &effects, &key);
    let before = effects
        .db()
        .conn
        .query_row("SELECT count(*) FROM effects", [], |r| r.get::<_, i64>(0))
        .unwrap();
    for (k, status) in &facts.receipts {
        if status == "dispatched" || status == "unknown_reconcile" {
            let (req, grant) = request_of(k);
            let g = grant.as_deref().map(label);
            assert_eq!(
                b.execute(&verify(&req), revision(), g.as_ref()).result,
                Err(Refusal::ReconciliationRequired),
                "{k}"
            );
        }
    }
    let after = effects
        .db()
        .conn
        .query_row("SELECT count(*) FROM effects", [], |r| r.get::<_, i64>(0))
        .unwrap();
    assert_eq!(before, after);
}

fn restart(dir: &Path) -> Facts {
    let ledger = SqliteLedger::open(&dir.join("ledger.db")).unwrap();
    let effects = SqliteEffectStore::open(&dir.join("effects.db")).unwrap();
    let facts = check_invariants(&ledger, &effects);
    drop(ledger);
    drop(effects);
    uncertain_receipts_are_never_redispatched(dir, &facts);
    facts
}

#[test]
fn sigkill_at_every_lifecycle_boundary_leaves_the_implied_crash_state() {
    let cases = [
        ("reserve", "Begin", None),
        ("reserve", "Body", None),
        ("dispatch", "Begin", Some("reserved")),
        ("dispatch", "Body", Some("reserved")),
        ("adapter", "Begin", Some("dispatched")),
        ("adapter", "Body", Some("dispatched")),
        ("observe", "Begin", Some("dispatched")),
        ("observe", "Body", Some("dispatched")),
        ("reconcile", "Begin", Some("dispatched")),
        ("reconcile", "Body", Some("dispatched")),
    ];
    for (tx, stage, want) in cases {
        let case = format!("{tx} {stage}");
        let dir = disposable(&format!("boundary-{tx}-{stage}"));
        let mut child = spawn_child(&format!("boundary|{}|{tx}|{stage}", dir.display()));
        let rx = lines(&mut child);
        wait_for(&rx, "READY");
        assert_eq!(wait_for(&rx, "AT "), format!("AT {tx} {stage}"), "{case}");
        assert!(child.try_wait().unwrap().is_none(), "{case}: still running");
        kill(child);
        let facts = restart(&dir);
        assert_eq!(
            facts.receipts.get(BOUNDARY_KEY).map(String::as_str),
            want,
            "{case}"
        );
        // The effect exists exactly when the adapter transaction committed.
        let effect = i64::from(matches!(tx, "observe" | "reconcile"));
        assert_eq!(
            facts.effects.get(BOUNDARY_KEY).copied().unwrap_or(0),
            effect,
            "{case}"
        );
        assert_eq!(facts.consumed, effect, "{case}");
        // Retry after the restart: never a second dispatch or effect.
        let ledger = SqliteLedger::open(&dir.join("ledger.db")).unwrap();
        let effects = SqliteEffectStore::open(&dir.join("effects.db")).unwrap();
        let active = ActivePolicy::new(PolicyState::loaded(reference()));
        let clock = TestClock::at(now());
        let key = permit_key();
        let b = Broker::new(&active, &clock, &ledger, &effects, &key);
        let retry = b
            .execute(
                &verify(&publish(BOUNDARY_KEY)),
                revision(),
                Some(&label(BOUNDARY_GRANT)),
            )
            .result;
        match tx {
            "reserve" | "dispatch" => assert_eq!(retry, Ok(Executed::EffectCompleted), "{case}"),
            _ => assert_eq!(retry, Err(Refusal::ReconciliationRequired), "{case}"),
        }
        if tx == "reconcile" {
            assert_eq!(
                b.reconcile(),
                Ok(1),
                "{case}: the killed reconcile left nothing"
            );
        }
        let facts = check_invariants(&ledger, &effects);
        // Completed by the retry, or applied before the kill; never applied after a
        // kill that left the adapter transaction uncommitted (uncertain, reconcile).
        let final_effects = i64::from(tx != "adapter");
        assert_eq!(
            facts.effects.get(BOUNDARY_KEY).copied().unwrap_or(0),
            final_effects,
            "{case}"
        );
        let uses: i64 = ledger
            .db()
            .conn
            .query_row(
                "SELECT uses FROM grants WHERE label = ?1",
                [BOUNDARY_GRANT],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(uses, 1, "{case}: one grant use");
        let _ = std::fs::remove_dir_all(&dir);
    }
}

#[test]
fn sigkill_at_seeded_random_points_never_breaks_the_invariants() {
    let dir = disposable("random");
    let mut rng = Rng::new(SEED);
    let (mut killed, mut finished) = (0, 0);
    let mut killed_after_ops = BTreeSet::new();
    for run in 0..RANDOM_RUNS {
        let delay = Duration::from_millis(rng.below(MAX_KILL_DELAY_MS));
        let ops = if rng.below(4) == 0 {
            10
        } else {
            OPS_PER_THREAD
        };
        let mut child = spawn_child(&format!("random|{}|{}|{ops}", dir.display(), SEED + run));
        let rx = lines(&mut child);
        wait_for(&rx, "START");
        thread::sleep(delay);
        let ops_seen = rx.try_iter().filter(|l| l.starts_with("OP ")).count();
        // A run is a mid-run kill only if SIGKILL ended it; a child that exited on its
        // own before the kill (including in the window before the signal) finished.
        let outcome = if child.try_wait().unwrap().is_some() {
            None
        } else {
            child.kill().unwrap();
            Some(())
        };
        let status = child.wait().unwrap();
        let was_killed = outcome.is_some() && status.signal() == Some(9);
        if was_killed {
            killed += 1;
            killed_after_ops.insert(ops_seen);
        } else {
            assert!(status.success(), "run {run}: {status:?}");
            finished += 1;
        }
        let facts = restart(&dir);
        let uncertain = facts
            .receipts
            .values()
            .filter(|s| *s == "dispatched" || *s == "unknown_reconcile")
            .count();
        let reserved = facts.receipts.values().filter(|s| *s == "reserved").count();
        say(&format!(
            "run {run}: {}, ops per thread {ops}, delay {} ms, ops seen {ops_seen}, receipts {} \
             (reserved {reserved}, dispatched or unknown_reconcile {uncertain}), keys with an \
             effect {}, consumed permits {}",
            if was_killed {
                "killed mid-run"
            } else {
                "finished first"
            },
            delay.as_millis(),
            facts.receipts.len(),
            facts.effects.len(),
            facts.consumed
        ));
    }
    say(&format!(
        "seed {SEED}: {killed} runs killed mid-run, {finished} finished before the kill"
    ));
    assert_eq!(killed + finished, RANDOM_RUNS);
    assert!(
        killed >= RANDOM_RUNS / 4,
        "too few mid-run kills: {killed} of {RANDOM_RUNS}"
    );
    assert!(
        killed_after_ops.len() > 1,
        "kills at distinct progress points"
    );
    let _ = std::fs::remove_dir_all(&dir);
}
