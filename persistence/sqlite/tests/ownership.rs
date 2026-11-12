//! Cross-process single-owner coordination (M3-01; M3-START-PLAN "Coordination boundary").
//!
//! The test binary re-executes itself as a probe process (`WRIT_LEDGER_PROBE`), so every
//! instance below is a separate OS process: startup races, refusals, SIGKILL and restart.

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdout, Command, Stdio};
use std::time::{Duration, Instant};

use writ_ledger_sqlite::{LedgerStore, SqliteLedger, StoreError};

const PROBE: &str = "WRIT_LEDGER_PROBE";

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
    match SqliteLedger::open(Path::new(db)) {
        Ok(mut ledger) => {
            writeln!(out.lock(), "OWNER {}", ledger.owner_epoch()).unwrap();
            out.lock().flush().unwrap();
            if mode == "hold" {
                // Hold ownership until the parent closes stdin, then prove we can still write.
                let mut sink = Vec::new();
                let _ = std::io::stdin().read_to_end(&mut sink);
                let r = ledger.write("audit", |tx| {
                    tx.execute("INSERT INTO audit_refusals (code) VALUES ('HOLDER')", [])
                });
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

fn spawn(mode: &str, gate: &str, db: &Path) -> Child {
    Command::new(std::env::current_exe().unwrap())
        .args([
            "probe",
            "--exact",
            "--nocapture",
            "--test-threads=1",
            "--quiet",
        ])
        .env(PROBE, format!("{mode}:{gate}|{}", db.display()))
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

fn rest_status(out: &mut BufReader<ChildStdout>) -> Vec<String> {
    let mut s = String::new();
    out.read_to_string(&mut s).unwrap();
    s.lines()
        .filter(|l| l.starts_with("WRITE"))
        .map(str::to_string)
        .collect()
}

/// The epoch the next owner gets: one more than the highest epoch any earlier owner took.
fn next_epoch(db: &Path) -> String {
    SqliteLedger::open(db).unwrap().owner_epoch().to_string()
}

#[test]
fn concurrent_startup_has_exactly_one_owner_and_the_rest_fail_closed() {
    for round in 0..5 {
        let d = tmp(&format!("race{round}"));
        let db = d.join("ledger.db");
        let gate = d.join("go");
        let mut children: Vec<Child> = (0..8)
            .map(|_| spawn("hold", gate.to_str().unwrap(), &db))
            .collect();
        std::fs::write(&gate, b"").unwrap();
        let mut outs: Vec<BufReader<ChildStdout>> = children
            .iter_mut()
            .map(|c| BufReader::new(c.stdout.take().unwrap()))
            .collect();
        let statuses: Vec<String> = outs.iter_mut().map(first_status).collect();
        let owners: Vec<&String> = statuses.iter().filter(|s| s.starts_with("OWNER")).collect();
        assert_eq!(owners.len(), 1, "round {round}: {statuses:?}");
        assert_eq!(owners[0], "OWNER 1");
        assert_eq!(
            statuses
                .iter()
                .filter(|s| *s == "REFUSED owned_elsewhere")
                .count(),
            7,
            "{statuses:?}"
        );
        for c in &mut children {
            drop(c.stdin.take());
        }
        let writes: Vec<String> = outs.iter_mut().flat_map(rest_status).collect();
        assert_eq!(
            writes,
            vec!["WRITE ok".to_string()],
            "only the owner writes"
        );
        for c in &mut children {
            assert!(c.wait().unwrap().success());
        }
        assert_eq!(
            next_epoch(&db),
            "2",
            "refused instances must not change the epoch"
        );
    }
}

#[test]
fn killed_owner_releases_ownership_and_a_restart_takes_a_higher_epoch() {
    let d = tmp("kill");
    let db = d.join("ledger.db");
    let mut owner = spawn("hold", "", &db);
    let mut out = BufReader::new(owner.stdout.take().unwrap());
    assert_eq!(first_status(&mut out), "OWNER 1");
    let mut blocked = spawn("try", "", &db);
    let mut bout = BufReader::new(blocked.stdout.take().unwrap());
    assert_eq!(first_status(&mut bout), "REFUSED owned_elsewhere");
    blocked.wait().unwrap();
    owner.kill().unwrap(); // SIGKILL on Unix: no destructor, no clean shutdown
    owner.wait().unwrap();
    let mut restart = spawn("try", "", &db);
    let mut rout = BufReader::new(restart.stdout.take().unwrap());
    assert_eq!(first_status(&mut rout), "OWNER 2");
    restart.wait().unwrap();
    let integrity: String = {
        let l = SqliteLedger::open(&db).unwrap();
        l.read(|c| c.query_row("PRAGMA integrity_check", [], |r| r.get(0)))
            .unwrap()
    };
    assert_eq!(integrity, "ok");
}

#[test]
fn reconciler_process_is_refused_while_a_broker_owns_the_ledger() {
    let d = tmp("reconciler");
    let db = d.join("ledger.db");
    let mut broker = spawn("hold", "", &db);
    let mut out = BufReader::new(broker.stdout.take().unwrap());
    assert_eq!(first_status(&mut out), "OWNER 1");
    // A separate reconciler uses the same ownership path and must fail closed, not wait.
    let started = Instant::now();
    let mut reconciler = spawn("try", "", &db);
    let mut rout = BufReader::new(reconciler.stdout.take().unwrap());
    assert_eq!(first_status(&mut rout), "REFUSED owned_elsewhere");
    reconciler.wait().unwrap();
    assert!(
        started.elapsed() < Duration::from_secs(5),
        "refusal must not wait for the lock"
    );
    drop(broker.stdin.take());
    assert_eq!(
        rest_status(&mut out),
        vec!["WRITE ok".to_string()],
        "the owner is unaffected"
    );
    assert!(broker.wait().unwrap().success());
}
