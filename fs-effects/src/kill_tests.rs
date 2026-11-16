//! Real process kills at every crash boundary (J-7.8; D-045 M5-D8 "actual SIGKILL recovery").
//!
//! The unit test binary re-executes itself as a child (`WRIT_FSFX_KILL`) on a disposable
//! environment: the child opens the ledger, the effect store and the adapter, arms a boundary
//! hook that announces `AT <boundary>` and blocks, and executes one `fs.write` through the
//! broker. The parent kills it with SIGKILL at that point, reopens everything (recovery runs
//! at adapter open), and checks the row reached, the files left, and the receipt after
//! reconciliation. The disposition scenarios kill between the J-11.9 steps. Only child
//! processes started here are killed. Run alone and single-threaded:
//! `cargo test -p writ-fs-effects --lib kill_tests -- --ignored --test-threads=1`.

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::io::{BufRead, BufReader, Write};
use std::os::unix::process::ExitStatusExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::{mpsc, Arc};
use std::time::Duration;

use writ_hostsys::fsfx::{FxError, ENOSPC};

use crate::dispose::{Completion, DecidedRelease, DispositionLedger, DispositionStatus};
use crate::test_support::*;
use crate::{Boundary, FsWriteAdapter, Step};

const CHILD: &str = "WRIT_FSFX_KILL";
const TARGET: &str = "sub/k.txt";

fn say(line: &str) {
    let out = std::io::stdout();
    let mut out = out.lock();
    writeln!(out, "{line}").unwrap();
    out.flush().unwrap();
}

fn hang(at: &str) -> ! {
    say(&format!("AT {at}"));
    loop {
        std::thread::sleep(Duration::from_secs(60));
    }
}

fn boundary_of(name: &str) -> Boundary {
    use Boundary::*;
    match name {
        "B0" => B0,
        "B1" => B1,
        "B2" => B2,
        "B3" => B3,
        "B4" => B4,
        "B5" => B5,
        "B6" => B6,
        "B7" => B7,
        "B8" => B8,
        "B9" => B9,
        "B10" => B10,
        "B11" => B11,
        "B12" => B12,
        "Abandoned" => Abandoned,
        "AbandonUnlinked" => AbandonUnlinked,
        other => panic!("unknown boundary {other}"),
    }
}

/// A file-backed ledger side of the dispositions (`id\tstatus\ttarget\tdigest\tidentity`).
struct FileLedger(PathBuf);

impl FileLedger {
    fn rows(&self) -> Vec<(DecidedRelease, String)> {
        std::fs::read_to_string(&self.0)
            .unwrap_or_default()
            .lines()
            .map(|l| {
                let p: Vec<&str> = l.split('\t').collect();
                (
                    DecidedRelease {
                        disposition_id: p[0].into(),
                        target: p[2].into(),
                        state_digest: p[3].into(),
                        new_last_identity: p[4].into(),
                    },
                    p[1].to_string(),
                )
            })
            .collect()
    }

    fn write(&self, rows: &[(DecidedRelease, String)]) {
        let text: String = rows
            .iter()
            .map(|(d, s)| {
                format!(
                    "{}\t{s}\t{}\t{}\t{}\n",
                    d.disposition_id, d.target, d.state_digest, d.new_last_identity
                )
            })
            .collect();
        let tmp = self.0.with_extension("tmp");
        std::fs::write(&tmp, text).unwrap();
        std::fs::File::open(&tmp).unwrap().sync_all().unwrap();
        std::fs::rename(&tmp, &self.0).unwrap();
    }
}

impl DispositionLedger for FileLedger {
    fn decided(&self, _root_id: &str) -> Vec<DecidedRelease> {
        self.rows()
            .into_iter()
            .filter(|(_, s)| s == "decided")
            .map(|(d, _)| d)
            .collect()
    }

    fn set_status(&self, id: &str, status: DispositionStatus) {
        let mut rows = self.rows();
        for (d, s) in &mut rows {
            if d.disposition_id == id {
                *s = match status {
                    DispositionStatus::Applied => "applied".into(),
                    DispositionStatus::Lapsed => "lapsed".into(),
                };
            }
        }
        self.write(&rows);
    }
}

// ---------------------------------------------------------------------------------------
// The child.

/// Child entry point: inert unless the parent set the environment variable.
#[test]
fn fs_kill_child() {
    let Ok(spec) = std::env::var(CHILD) else {
        return;
    };
    let parts: Vec<&str> = spec.split('|').collect();
    let env = Env::at(Path::new(parts[0]), false);
    let s = env.stores();
    let a = env.adapter(&s).unwrap();
    let ledger = FileLedger(env.dir.join("dispositions.txt"));
    match parts[1] {
        "D1" => hang("D1"),
        "D2" => {
            let d = ledger.decided("r1").pop().unwrap();
            assert_eq!(a.apply_release(&d).unwrap(), Completion::Applied);
            hang("D2");
        }
        "D3" => {
            a.complete_dispositions(&ledger).unwrap();
            hang("D3");
        }
        mode => {
            let at = boundary_of(parts[2]);
            a.set_hook(Some(Arc::new(move |b| {
                if b == at {
                    hang(parts_name(b));
                }
            })));
            if mode == "abandon" {
                a.fail_once(Step::Write, FxError::Errno(ENOSPC));
            }
            let b = broker(&s, &a);
            let req = match mode {
                "replace" => env.replace("kill", TARGET, "b.txt", &env.snaps.bytes("a.txt")),
                _ => env.create("kill", TARGET, "a.txt"),
            };
            say("READY");
            let _ = exec(&b, &req);
            say("FINISHED");
        }
    }
}

fn parts_name(b: Boundary) -> &'static str {
    use Boundary::*;
    match b {
        B0 => "B0",
        B1 => "B1",
        B2 => "B2",
        B3 => "B3",
        B4 => "B4",
        B5 => "B5",
        B6 => "B6",
        B7 => "B7",
        B8 => "B8",
        B9 => "B9",
        B10 => "B10",
        B11 => "B11",
        B12 => "B12",
        Abandoned => "Abandoned",
        AbandonUnlinked => "AbandonUnlinked",
    }
}

// ---------------------------------------------------------------------------------------
// The parent.

/// Run the child on `dir` until it announces `AT <at>`, then SIGKILL it.
fn kill_at(dir: &Path, mode: &str, at: &str) {
    let exe = std::env::current_exe().unwrap();
    let mut child = Command::new(exe)
        .args([
            "kill_tests::fs_kill_child",
            "--exact",
            "--nocapture",
            "--test-threads=1",
        ])
        .env(CHILD, format!("{}|{mode}|{at}", dir.display()))
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn()
        .unwrap();
    let stdout = child.stdout.take().unwrap();
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        for line in BufReader::new(stdout).lines() {
            let Ok(line) = line else { break };
            if tx.send(line).is_err() {
                break;
            }
        }
    });
    let want = format!("AT {at}");
    loop {
        match rx.recv_timeout(Duration::from_secs(60)) {
            Ok(line) if line.ends_with(&want) => break,
            Ok(line) if line == "FINISHED" => {
                let _ = child.kill();
                panic!("child finished without reaching {at}");
            }
            Ok(_) => {}
            Err(e) => {
                let _ = child.kill();
                panic!("child did not reach {at}: {e}");
            }
        }
    }
    child.kill().unwrap();
    let status = child.wait().unwrap();
    assert_eq!(status.signal(), Some(9), "killed with SIGKILL");
}

/// What the parent observes after a kill and a reopen (recovery ran at open).
struct After {
    row: Option<String>,
    receipt: Option<String>,
    staging: Vec<String>,
    target: Option<Vec<u8>>,
    blocked: bool,
    tainted: bool,
}

fn after(env: &Env) -> After {
    let s = env.stores();
    let a = env.adapter(&s).unwrap();
    let ops = a
        .journal()
        .read("test", |tx| tx.ops_for_target(TARGET))
        .unwrap();
    let op = ops.into_iter().find(|o| o.idempotency_key == "kill");
    let row = op.map(|op| {
        a.journal()
            .read("test", |tx| tx.outcome(&op.op_id))
            .unwrap()
            .map_or("unresolved".to_string(), |o| o.reason)
    });
    let b = broker(&s, &a);
    let first = b.reconcile().unwrap();
    let second = b.reconcile().unwrap();
    assert_eq!(
        second.changed(),
        0,
        "repeated reconciliation changes nothing"
    );
    let _ = first;
    let tr = target_row(&a, TARGET);
    // A retry never redispatches.
    let retry = exec(&b, &env.create("kill", TARGET, "a.txt"));
    assert!(retry.permit.is_none(), "no second dispatch");
    After {
        row,
        receipt: status(&s, "kill"),
        staging: staging_files(env),
        target: std::fs::read(env.tree(TARGET)).ok(),
        blocked: tr.as_ref().is_some_and(|r| r.blocked_by.is_some()),
        tainted: tr.as_ref().is_some_and(|r| r.tainted),
    }
}

fn fresh(name: &str) -> Env {
    Env::new(&format!("kill-{name}"))
}

#[test]
#[ignore = "real process kills: run alone with --ignored --test-threads=1"]
fn sigkill_at_every_create_only_boundary_then_recovery() {
    // (boundary, row reached, receipt after reconciliation, target content present)
    let cases: [(&str, Option<&str>, &str, bool); 13] = [
        ("B0", None, "unknown_reconcile", false),
        ("B1", Some("R1a"), "observed_failure", false),
        ("B2", Some("R1b"), "observed_failure", false),
        ("B3", Some("R1b"), "observed_failure", false),
        ("B4", Some("R1b"), "observed_failure", false),
        ("B5", Some("R5"), "observed_failure", false),
        ("B6", Some("R5"), "observed_failure", false),
        ("B7", Some("R5"), "observed_failure", false),
        ("B8", Some("R3"), "observed_success", true),
        ("B9", Some("R3"), "observed_success", true),
        ("B10", Some("R3"), "observed_success", true),
        ("B11", Some("applied"), "observed_success", true),
        ("B12", Some("applied"), "observed_success", true),
    ];
    for (at, row, receipt, present) in cases {
        let env = fresh(&format!("create-{at}"));
        kill_at(&env.dir, "create", at);
        let got = after(&env);
        assert_eq!(got.row.as_deref(), row, "{at}");
        assert_eq!(got.receipt.as_deref(), Some(receipt), "{at}");
        assert_eq!(got.target.is_some(), present, "{at}");
        if present {
            assert_eq!(got.target.unwrap(), env.snaps.bytes("a.txt"), "{at}");
        }
        match row {
            // The unrecorded partial or staged file is kept and the target stays blocked.
            Some("R1b") => {
                assert!(got.blocked, "{at}");
                assert_eq!(got.staging.len(), 1, "{at}");
            }
            _ => {
                assert!(!got.blocked && !got.tainted, "{at}");
                assert!(got.staging.is_empty(), "{at}: {:?}", got.staging);
            }
        }
        std::fs::remove_dir_all(&env.dir).unwrap();
    }
}

#[test]
#[ignore = "real process kills: run alone with --ignored --test-threads=1"]
fn sigkill_at_replace_if_boundaries_then_recovery_and_gc() {
    let cases = [
        ("B1", "R1a", "observed_failure", "a.txt"),
        ("B5", "R5", "observed_failure", "a.txt"),
        ("B7", "R5", "observed_failure", "a.txt"),
        ("B8", "R3x", "observed_success", "b.txt"),
        ("B10", "R3x", "observed_success", "b.txt"),
        // The displaced file left by the kill before cleanup is collected at open.
        ("B11", "applied", "observed_success", "b.txt"),
    ];
    for (at, row, receipt, content) in cases {
        let env = fresh(&format!("replace-{at}"));
        {
            let s = env.stores();
            let a = env.adapter(&s).unwrap();
            let b = broker(&s, &a);
            assert_eq!(
                exec(&b, &env.create("base", TARGET, "a.txt")).result,
                applied()
            );
        }
        kill_at(&env.dir, "replace", at);
        let got = after(&env);
        assert_eq!(got.row.as_deref(), Some(row), "{at}");
        assert_eq!(got.receipt.as_deref(), Some(receipt), "{at}");
        assert_eq!(got.target.unwrap(), env.snaps.bytes(content), "{at}");
        assert!(got.staging.is_empty(), "{at}: {:?}", got.staging);
        assert!(!got.blocked && !got.tainted, "{at}");
        std::fs::remove_dir_all(&env.dir).unwrap();
    }
}

#[test]
#[ignore = "real process kills: run alone with --ignored --test-threads=1"]
fn sigkill_during_inline_staging_failure_resolution_reaches_r1c() {
    for at in ["Abandoned", "AbandonUnlinked"] {
        let env = fresh(&format!("abandon-{at}"));
        kill_at(&env.dir, "abandon", at);
        let got = after(&env);
        assert_eq!(got.row.as_deref(), Some("STAGE_FAILED"), "{at}");
        assert_eq!(got.receipt.as_deref(), Some("observed_failure"), "{at}");
        assert!(got.staging.is_empty(), "{at}: {:?}", got.staging);
        assert!(got.target.is_none() && !got.blocked && !got.tainted, "{at}");
        std::fs::remove_dir_all(&env.dir).unwrap();
    }
}

#[test]
#[ignore = "real process kills: run alone with --ignored --test-threads=1"]
fn sigkill_then_external_changes_reach_r2_r4_r6_r8_and_r_other() {
    use std::io::{Seek, SeekFrom};
    // R2: intent durable, then another writer creates the target.
    let env = fresh("r2");
    kill_at(&env.dir, "create", "B1");
    std::fs::write(env.tree(TARGET), env.snaps.bytes("a.txt")).unwrap();
    let got = after(&env);
    assert_eq!(got.row.as_deref(), Some("R2"));
    assert_eq!(got.receipt.as_deref(), Some("unknown_reconcile"));
    assert!(got.tainted);
    drop(env);
    // R4: renamed, then rewritten in place (same inode, other bytes).
    let env = fresh("r4");
    kill_at(&env.dir, "create", "B10");
    let mut f = std::fs::OpenOptions::new()
        .write(true)
        .open(env.tree(TARGET))
        .unwrap();
    f.seek(SeekFrom::Start(0)).unwrap();
    f.write_all(b"XXXX").unwrap();
    f.sync_all().unwrap();
    drop(f);
    let got = after(&env);
    assert_eq!(got.row.as_deref(), Some("R4"));
    assert_eq!(got.receipt.as_deref(), Some("unknown_reconcile"));
    drop(env);
    // R6: staged, then a foreign target with the expected bytes; the staged file is kept.
    let env = fresh("r6");
    kill_at(&env.dir, "create", "B7");
    std::fs::write(env.tree(TARGET), env.snaps.bytes("a.txt")).unwrap();
    let got = after(&env);
    assert_eq!(got.row.as_deref(), Some("R6"));
    assert_eq!(got.staging.len(), 1);
    drop(env);
    // R8: staged, then a hard link to the staged file.
    let env = fresh("r8");
    kill_at(&env.dir, "create", "B7");
    let staged = env.staging().join(&staging_files(&env)[0]);
    std::fs::hard_link(&staged, env.dir.join("link")).unwrap();
    let got = after(&env);
    assert_eq!(got.row.as_deref(), Some("R8"));
    drop(env);
    // R-other: replace_if staged, then the staged file vanishes while the target is prior.
    let env = fresh("rother");
    {
        let s = env.stores();
        let a = env.adapter(&s).unwrap();
        let b = broker(&s, &a);
        assert_eq!(
            exec(&b, &env.create("base", TARGET, "a.txt")).result,
            applied()
        );
    }
    kill_at(&env.dir, "replace", "B7");
    for f in staging_files(&env) {
        std::fs::remove_file(env.staging().join(f)).unwrap();
    }
    let got = after(&env);
    assert_eq!(got.row.as_deref(), Some("R-other"));
    assert_eq!(got.receipt.as_deref(), Some("unknown_reconcile"));
    assert!(got.tainted);
}

/// A tainted target (R7) and one decided release recorded in the file ledger (step 1).
fn decided_release(env: &Env) -> DecidedRelease {
    let s = env.stores();
    let a = env.adapter(&s).unwrap();
    let b = broker(&s, &a);
    let t = env.tree(TARGET);
    a.set_hook(Some(Arc::new(move |b| {
        if b == Boundary::B8 {
            std::fs::remove_file(&t).unwrap();
        }
    })));
    assert!(exec(&b, &env.create("taint", TARGET, "a.txt"))
        .result
        .is_err());
    a.set_hook(None);
    let ctx = a.release_context(TARGET).unwrap();
    let d = DecidedRelease {
        disposition_id: "disp-kill".into(),
        target: TARGET.into(),
        state_digest: ctx.state_digest,
        new_last_identity: ctx.observed,
    };
    FileLedger(env.dir.join("dispositions.txt")).write(&[(d.clone(), "decided".into())]);
    d
}

fn applied_once(a: &FsWriteAdapter<'_>, id: &str) -> bool {
    a.journal()
        .read("test", |tx| tx.disposition(id))
        .unwrap()
        .is_some_and(|r| r.prev_tainted)
}

#[test]
#[ignore = "real process kills: run alone with --ignored --test-threads=1"]
fn sigkill_between_disposition_steps_completes_exactly_once_or_lapses() {
    // Between (1) and (2): completion applies; status applied.
    let env = fresh("disp-1");
    let d = decided_release(&env);
    kill_at(&env.dir, "D1", "D1");
    {
        let s = env.stores();
        let a = env.adapter(&s).unwrap();
        let ledger = FileLedger(env.dir.join("dispositions.txt"));
        let done = a.complete_dispositions(&ledger).unwrap();
        assert_eq!(done, vec![(d.disposition_id.clone(), Completion::Applied)]);
        assert!(applied_once(&a, &d.disposition_id));
        assert_eq!(ledger.rows()[0].1, "applied");
        assert!(!target_row(&a, TARGET).unwrap().tainted);
    }
    std::fs::remove_dir_all(&env.dir).unwrap();
    // Between (2) and (3): completion finds the id and only marks applied.
    let env = fresh("disp-2");
    let d = decided_release(&env);
    kill_at(&env.dir, "D2", "D2");
    {
        let s = env.stores();
        let a = env.adapter(&s).unwrap();
        let ledger = FileLedger(env.dir.join("dispositions.txt"));
        assert_eq!(ledger.rows()[0].1, "decided");
        let done = a.complete_dispositions(&ledger).unwrap();
        assert_eq!(
            done,
            vec![(d.disposition_id.clone(), Completion::AlreadyApplied)]
        );
        assert!(
            applied_once(&a, &d.disposition_id),
            "prev state is the tainted one"
        );
        assert_eq!(ledger.rows()[0].1, "applied");
    }
    std::fs::remove_dir_all(&env.dir).unwrap();
    // After (3): nothing left to complete.
    let env = fresh("disp-3");
    decided_release(&env);
    kill_at(&env.dir, "D3", "D3");
    {
        let s = env.stores();
        let a = env.adapter(&s).unwrap();
        let ledger = FileLedger(env.dir.join("dispositions.txt"));
        assert!(a.complete_dispositions(&ledger).unwrap().is_empty());
    }
    std::fs::remove_dir_all(&env.dir).unwrap();
    // A state change between (1) and completion: lapsed, never applied.
    let env = fresh("disp-lapse");
    let d = decided_release(&env);
    kill_at(&env.dir, "D1", "D1");
    std::fs::write(env.tree(TARGET), b"changed").unwrap();
    {
        let s = env.stores();
        let a = env.adapter(&s).unwrap();
        let ledger = FileLedger(env.dir.join("dispositions.txt"));
        let done = a.complete_dispositions(&ledger).unwrap();
        assert_eq!(done, vec![(d.disposition_id.clone(), Completion::Lapsed)]);
        assert_eq!(ledger.rows()[0].1, "lapsed");
        assert!(target_row(&a, TARGET).unwrap().tainted);
    }
    std::fs::remove_dir_all(&env.dir).unwrap();
}
