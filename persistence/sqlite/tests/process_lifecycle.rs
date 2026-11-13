//! Process lifecycle with open stores (M3-07; D-035 item 6; E-066): children spawned
//! through the lifecycle gate inherit no store descriptor, and a process that closes and
//! reopens its own ledger and effect store while other threads spawn children never sees
//! its own ownership lock as held elsewhere.
//!
//! Real processes, disposable temporary directories only. The tests of this binary are
//! serialized (as in `ownership.rs`) so that each one alone owns the process's children
//! and descriptors while it asserts on them.

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use writ_broker::process::{self, LocalProcessSandbox};
use writ_broker::runner::{Job, Limits, Sandbox};
use writ_ledger_sqlite::{LedgerStore, SqliteEffectStore, SqliteLedger, StoreError};

static SERIAL: std::sync::Mutex<()> = std::sync::Mutex::new(());

fn serial() -> std::sync::MutexGuard<'static, ()> {
    SERIAL
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}

fn tmp(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!(
        "writ-ledger-lifecycle-{}-{name}",
        std::process::id()
    ));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

/// `ls -l` of `/proc/self/fd` as seen by a child: descriptor to link target.
fn child_descriptors(workdir: &Path) -> BTreeMap<String, String> {
    let args = ["-l".to_string(), "/proc/self/fd".to_string()];
    let out = LocalProcessSandbox::new()
        .run(&Job {
            program: Path::new("/bin/ls"),
            args: &args,
            env: &[],
            workdir,
            limits: Limits {
                wall_time: Duration::from_secs(10),
                max_output_bytes: 64 * 1024,
            },
        })
        .unwrap();
    assert_eq!(
        out.exit_status,
        0,
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8(out.stdout)
        .unwrap()
        .lines()
        .filter_map(|l| l.split_once(" -> "))
        .map(|(left, target)| {
            (
                left.split_whitespace().last().unwrap().to_string(),
                target.to_string(),
            )
        })
        .collect()
}

#[test]
fn a_child_inherits_no_store_descriptor() {
    let _serial = serial();
    let d = tmp("fds");
    let ledger = SqliteLedger::create(&d.join("ledger.db")).unwrap();
    let effects = SqliteEffectStore::create(&d.join("effects.db"), ledger.ledger_id()).unwrap();
    // Both stores hold their connection and `.owner` lock descriptors now.
    let g = LocalProcessSandbox::new().guarantees().unwrap();
    assert!(g.descriptors_closed && g.bounded);
    let fds = child_descriptors(&d);
    // Exactly the standard streams (stdin from /dev/null, stdout and stderr pipes to the
    // sandbox) plus descriptor 3, which `ls` itself opens on the directory it lists
    // (`/proc/<its pid>/fd`). No ledger, effect store, WAL, `.owner` or test-harness
    // descriptor reaches the child.
    assert_eq!(
        fds.keys().map(String::as_str).collect::<Vec<_>>(),
        ["0", "1", "2", "3"],
        "{fds:?}"
    );
    assert_eq!(fds["0"], "/dev/null");
    assert!(fds["1"].starts_with("pipe:[") && fds["2"].starts_with("pipe:["));
    assert_ne!(fds["1"], fds["2"]);
    let listed = fds["3"].strip_prefix("/proc/").unwrap();
    let (pid, rest) = listed.split_once('/').unwrap();
    assert!(pid.parse::<u32>().is_ok() && rest == "fd", "{fds:?}");
    drop(effects);
    drop(ledger);
    let _ = std::fs::remove_dir_all(&d);
}

// Regression test for E-066. With the gate but without the explicit unlock of
// `OwnerLock` this test failed in 3 of 5 runs (a child that `spawn` already returned for
// still held a duplicate of a released lock); a deterministic demonstration of the
// mechanism is `writ_broker` `process::tests::
// a_released_owner_lock_is_free_while_a_duplicate_descriptor_lives`. A demonstration with
// ungated spawns is omitted: whether a fork lands in the window is timing dependent.
const REOPENS: u64 = 300;
const SPAWNERS: usize = 4;

#[test]
fn reopening_own_stores_while_spawning_never_sees_owned_elsewhere() {
    let _serial = serial();
    let start = Instant::now();
    let d = tmp("stress");
    let (ledger_path, effects_path) = (d.join("ledger.db"), d.join("effects.db"));
    let mut ledger = SqliteLedger::create(&ledger_path).unwrap();
    let id = ledger.ledger_id().clone();
    let mut effects = Some(SqliteEffectStore::create(&effects_path, &id).unwrap());
    let first_epoch = ledger.owner_epoch();

    let stop = Arc::new(AtomicBool::new(false));
    let spawned = Arc::new(AtomicUsize::new(0));
    let mut spawners = Vec::new();
    for i in 0..SPAWNERS {
        let (stop, spawned, dir) = (Arc::clone(&stop), Arc::clone(&spawned), d.clone());
        spawners.push(std::thread::spawn(move || {
            while !stop.load(Ordering::SeqCst) {
                if i == 0 {
                    // The production sandbox path.
                    let out = LocalProcessSandbox::new()
                        .run(&Job {
                            program: Path::new("/bin/true"),
                            args: &[],
                            env: &[],
                            workdir: &dir,
                            limits: Limits {
                                wall_time: Duration::from_secs(10),
                                max_output_bytes: 0,
                            },
                        })
                        .unwrap();
                    assert_eq!(out.exit_status, 0);
                } else {
                    let mut child = process::spawn(
                        Command::new("/bin/true")
                            .env_clear()
                            .stdin(Stdio::null())
                            .stdout(Stdio::null())
                            .stderr(Stdio::null()),
                    )
                    .unwrap();
                    assert!(child.wait().unwrap().success());
                }
                spawned.fetch_add(1, Ordering::SeqCst);
            }
        }));
    }

    for i in 1..=REOPENS {
        drop(effects.take());
        drop(ledger);
        ledger = match SqliteLedger::open(&ledger_path) {
            Ok(l) => l,
            Err(e) => panic!("reopen {i} of the ledger refused: {e:?}"),
        };
        // Same owner semantics on every restart: one more epoch, same identity.
        assert_eq!(ledger.owner_epoch(), first_epoch + i);
        assert_eq!(ledger.ledger_id(), &id);
        effects = match SqliteEffectStore::open(&effects_path, &id) {
            Ok(s) => Some(s),
            Err(e) => panic!("reopen {i} of the effect store refused: {e:?}"),
        };
        // And a second owner in this process is still refused while they are held.
        if i % 50 == 0 {
            assert!(matches!(
                SqliteLedger::open(&ledger_path),
                Err(StoreError::OwnedElsewhere)
            ));
            assert!(SqliteEffectStore::open(&effects_path, &id).is_err());
        }
    }
    stop.store(true, Ordering::SeqCst);
    for s in spawners {
        s.join().unwrap();
    }
    let children = spawned.load(Ordering::SeqCst);
    // The spawns overlapped the reopen loop (not a vacuous run).
    assert!(children >= 100, "only {children} children spawned");
    assert!(start.elapsed() < Duration::from_secs(30));
    drop(effects);
    drop(ledger);
    let _ = std::fs::remove_dir_all(&d);
}
