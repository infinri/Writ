//! Cross-process single-owner coordination, ledger identity and safe initialization
//! (M3-01; M3-START-PLAN "Coordination boundary").
//!
//! The test binary re-executes itself as a probe process (`WRIT_LEDGER_PROBE`), so every
//! instance below is a separate OS process: startup races, aliases, refusals, SIGKILL and
//! restart.

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::fs::symlink;
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdout, Command, Stdio};
use std::time::{Duration, Instant};

use writ_ledger_sqlite::{LedgerStore, SqliteLedger, StoreError};

const PROBE: &str = "WRIT_LEDGER_PROBE";

/// Serializes the tests of this binary. Each test spawns child processes, and a fork briefly
/// duplicates every open descriptor of this process, including another test's ownership lock,
/// until the child execs; a parallel test that drops and reopens its ledger could then observe
/// `OwnedElsewhere` from its own transiently duplicated lock. Running these tests one at a time
/// removes the race without weakening any ownership check.
static SERIAL: std::sync::Mutex<()> = std::sync::Mutex::new(());

fn serial() -> std::sync::MutexGuard<'static, ()> {
    SERIAL
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}

/// Probe entry point: inert unless the environment variable is set by a parent test.
#[test]
fn probe() {
    let Ok(spec) = std::env::var(PROBE) else {
        return;
    };
    let (mode, rest) = spec.split_once(':').unwrap();
    let (gate, db) = rest.split_once('|').unwrap();
    if !gate.is_empty() {
        let start = Instant::now();
        while !Path::new(gate).exists() {
            assert!(
                start.elapsed() < Duration::from_secs(30),
                "start gate never opened"
            );
            std::thread::sleep(Duration::from_millis(1));
        }
    }
    let out = std::io::stdout();
    let opened = if mode.starts_with("create") {
        SqliteLedger::create(Path::new(db))
    } else {
        SqliteLedger::open(Path::new(db))
    };
    match opened {
        Ok(mut ledger) => {
            writeln!(out.lock(), "OWNER {}", ledger.owner_epoch()).unwrap();
            out.lock().flush().unwrap();
            if mode.ends_with("hold") {
                let mut sink = Vec::new();
                let _ = std::io::stdin().read_to_end(&mut sink);
                let r = ledger.record_refusal("HOLDER");
                writeln!(
                    out.lock(),
                    "WRITE {}",
                    if r.is_ok() { "ok" } else { "refused" }
                )
                .unwrap();
            }
        }
        Err(StoreError::OwnedElsewhere) => writeln!(out.lock(), "REFUSED owned_elsewhere").unwrap(),
        Err(e) => writeln!(out.lock(), "REFUSED {e}").unwrap(),
    }
    out.lock().flush().unwrap();
}

fn tmp(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("writ-ledger-proc-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn spawn(mode: &str, gate: &str, db: &Path, cwd: &Path) -> Child {
    Command::new(std::env::current_exe().unwrap())
        .args([
            "probe",
            "--exact",
            "--nocapture",
            "--test-threads=1",
            "--quiet",
        ])
        .env(PROBE, format!("{mode}:{gate}|{}", db.display()))
        .current_dir(cwd)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .unwrap()
}

fn first_status(out: &mut BufReader<ChildStdout>) -> String {
    let mut line = String::new();
    loop {
        line.clear();
        assert!(
            out.read_line(&mut line).unwrap() > 0,
            "probe produced no status"
        );
        let l = line.trim();
        if l.starts_with("OWNER") || l.starts_with("REFUSED") {
            return l.to_string();
        }
    }
}

fn writes(out: &mut BufReader<ChildStdout>) -> Vec<String> {
    let mut s = String::new();
    out.read_to_string(&mut s).unwrap();
    s.lines()
        .filter(|l| l.starts_with("WRITE"))
        .map(str::to_string)
        .collect()
}

/// Start all probes behind one gate, return their first statuses, then release them.
fn race(specs: &[(&str, PathBuf)], d: &Path) -> (Vec<String>, Vec<String>) {
    let gate = d.join(format!("go-{}", specs.len()));
    let _ = std::fs::remove_file(&gate);
    let mut children: Vec<Child> = specs
        .iter()
        .map(|(m, p)| spawn(m, gate.to_str().unwrap(), p, d))
        .collect();
    std::fs::write(&gate, b"").unwrap();
    let mut outs: Vec<BufReader<ChildStdout>> = children
        .iter_mut()
        .map(|c| BufReader::new(c.stdout.take().unwrap()))
        .collect();
    let statuses: Vec<String> = outs.iter_mut().map(first_status).collect();
    for c in &mut children {
        drop(c.stdin.take());
    }
    let w: Vec<String> = outs.iter_mut().flat_map(writes).collect();
    for c in &mut children {
        assert!(c.wait().unwrap().success());
    }
    (statuses, w)
}

fn new_ledger(d: &Path) -> PathBuf {
    let db = d.join("ledger.db");
    drop(SqliteLedger::create(&db).unwrap());
    db
}

fn next_epoch(db: &Path) -> u64 {
    SqliteLedger::open(db).unwrap().owner_epoch()
}

#[test]
fn concurrent_startup_has_exactly_one_owner_and_the_rest_fail_closed() {
    let _serial = serial();
    for round in 0..5 {
        let d = tmp(&format!("race{round}"));
        let db = new_ledger(&d);
        let specs: Vec<(&str, PathBuf)> = (0..8).map(|_| ("open-hold", db.clone())).collect();
        let (statuses, w) = race(&specs, &d);
        assert_eq!(
            statuses.iter().filter(|s| *s == "OWNER 2").count(),
            1,
            "round {round}: {statuses:?}"
        );
        assert_eq!(
            statuses
                .iter()
                .filter(|s| *s == "REFUSED owned_elsewhere")
                .count(),
            7,
            "{statuses:?}"
        );
        assert_eq!(w, vec!["WRITE ok".to_string()], "only the owner writes");
        assert_eq!(
            next_epoch(&db),
            3,
            "refused instances must not change the epoch"
        );
    }
}

#[test]
fn concurrent_creation_has_exactly_one_creator() {
    let _serial = serial();
    let d = tmp("create-race");
    let db = d.join("ledger.db");
    let specs: Vec<(&str, PathBuf)> = (0..8).map(|_| ("create-hold", db.clone())).collect();
    let (statuses, w) = race(&specs, &d);
    assert_eq!(
        statuses.iter().filter(|s| *s == "OWNER 1").count(),
        1,
        "{statuses:?}"
    );
    assert!(
        statuses
            .iter()
            .filter(|s| !s.starts_with("OWNER"))
            .all(|s| s == "REFUSED owned_elsewhere" || s == "REFUSED open EXISTS"),
        "{statuses:?}"
    );
    assert_eq!(w, vec!["WRITE ok".to_string()]);
}

#[test]
fn every_supported_alias_shares_one_ownership() {
    let _serial = serial();
    let d = tmp("alias");
    std::fs::create_dir_all(d.join("real/sub")).unwrap();
    let db = new_ledger(&d.join("real"));
    symlink(&db, d.join("file-link.db")).unwrap();
    symlink(d.join("real"), d.join("dir-link")).unwrap();
    symlink(d.join("file-link.db"), d.join("link-to-link.db")).unwrap();
    let aliases: Vec<PathBuf> = vec![
        db.clone(),
        PathBuf::from("real/ledger.db"), // relative to the probes' working directory
        PathBuf::from("./real/sub/../ledger.db"), // dot and dot-dot components
        d.join("file-link.db"),          // symbolic link to the file
        d.join("dir-link/ledger.db"),    // symbolic link to the directory
        d.join("link-to-link.db"),       // chain of symbolic links
    ];
    for round in 0..3 {
        let specs: Vec<(&str, PathBuf)> =
            aliases.iter().map(|p| ("open-hold", p.clone())).collect();
        let (statuses, w) = race(&specs, &d);
        assert_eq!(
            statuses.iter().filter(|s| s.starts_with("OWNER")).count(),
            1,
            "round {round}: {statuses:?}"
        );
        assert_eq!(
            statuses
                .iter()
                .filter(|s| *s == "REFUSED owned_elsewhere")
                .count(),
            aliases.len() - 1,
            "{statuses:?}"
        );
        assert_eq!(w, vec!["WRITE ok".to_string()]);
    }
}

#[test]
fn hard_links_are_refused_as_ambiguous() {
    let _serial = serial();
    let d = tmp("hardlink");
    let db = new_ledger(&d);
    std::fs::hard_link(&db, d.join("other-name.db")).unwrap();
    for p in [db.clone(), d.join("other-name.db")] {
        let mut c = spawn("open", "", &p, &d);
        let mut out = BufReader::new(c.stdout.take().unwrap());
        assert_eq!(
            first_status(&mut out),
            "REFUSED ownership AMBIGUOUS_IDENTITY",
            "{}",
            p.display()
        );
        c.wait().unwrap();
    }
    std::fs::remove_file(d.join("other-name.db")).unwrap();
    assert!(
        SqliteLedger::open(&db).is_ok(),
        "a single link is accepted again"
    );
}

#[test]
fn killed_owner_releases_ownership_and_a_restart_takes_a_higher_epoch() {
    let _serial = serial();
    let d = tmp("kill");
    let db = new_ledger(&d);
    let mut owner = spawn("open-hold", "", &db, &d);
    let mut out = BufReader::new(owner.stdout.take().unwrap());
    assert_eq!(first_status(&mut out), "OWNER 2");
    owner.kill().unwrap(); // SIGKILL on Unix: no destructor, no clean shutdown
    owner.wait().unwrap();
    let mut restart = spawn("open", "", &db, &d);
    let mut rout = BufReader::new(restart.stdout.take().unwrap());
    assert_eq!(first_status(&mut rout), "OWNER 3");
    restart.wait().unwrap();
}

#[test]
fn reconciler_process_is_refused_while_a_broker_owns_the_ledger() {
    let _serial = serial();
    let d = tmp("reconciler");
    let db = new_ledger(&d);
    let mut broker = spawn("open-hold", "", &db, &d);
    let mut out = BufReader::new(broker.stdout.take().unwrap());
    assert_eq!(first_status(&mut out), "OWNER 2");
    let started = Instant::now();
    let mut reconciler = spawn("open", "", &db, &d);
    let mut rout = BufReader::new(reconciler.stdout.take().unwrap());
    assert_eq!(first_status(&mut rout), "REFUSED owned_elsewhere");
    reconciler.wait().unwrap();
    assert!(
        started.elapsed() < Duration::from_secs(5),
        "refusal must not wait for the lock"
    );
    drop(broker.stdin.take());
    assert_eq!(
        writes(&mut out),
        vec!["WRITE ok".to_string()],
        "the owner is unaffected"
    );
    assert!(broker.wait().unwrap().success());
}
