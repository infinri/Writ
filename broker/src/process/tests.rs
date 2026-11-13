//! Real-process tests of the lifecycle gate and the local process sandbox (disposable
//! temporary directories only).

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use super::*;
use crate::runner::{Limits, RunnerRequirements};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

const SH: &str = "/bin/sh";

fn tmp(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("writ-process-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn limits(wall_ms: u64, max_output_bytes: usize) -> Limits {
    Limits {
        wall_time: Duration::from_millis(wall_ms),
        max_output_bytes,
    }
}

fn run_sh(
    dir: &Path,
    script: &str,
    env: &[(String, String)],
    limits: Limits,
) -> Result<JobOutput, JobFailure> {
    let args = ["-c".to_string(), script.to_string()];
    LocalProcessSandbox::new().run(&Job {
        program: Path::new(SH),
        args: &args,
        env,
        workdir: dir,
        limits,
    })
}

fn wait_gone(pid: &str) -> bool {
    !Path::new("/proc").join(pid).exists()
}

#[test]
fn guarantees_are_honest_and_the_profile_digest_binds_them() {
    let s = LocalProcessSandbox::new();
    let g = s.guarantees().unwrap();
    assert!(g.bounded && g.descriptors_closed);
    assert!(!g.filesystem_isolated && !g.network_isolated);
    assert!(!g.process_tree_contained && !g.resource_limited);
    assert!(g.satisfies(&RunnerRequirements::uncontained_for_disposable_tests().sandbox));
    assert!(!g.satisfies(&RunnerRequirements::production().sandbox));
    let expected = json::digest(&json::obj([
        ("contract", json::s("writ-sandbox-profile/1")),
        ("profile", json::s("local-process-uncontained/1")),
        (
            "guarantees",
            json::obj([
                ("filesystem_isolated", Value::Bool(false)),
                ("network_isolated", Value::Bool(false)),
                ("process_tree_contained", Value::Bool(false)),
                ("resource_limited", Value::Bool(false)),
                ("descriptors_closed", Value::Bool(true)),
                ("bounded", Value::Bool(true)),
            ]),
        ),
    ]))
    .unwrap();
    assert_eq!(s.environment_digest(), expected.as_str());
}

#[test]
fn the_child_sees_exactly_the_job_environment() {
    let dir = tmp("env");
    let env = vec![
        ("WRIT_A".to_string(), "1".to_string()),
        ("WRIT_B".to_string(), "two words".to_string()),
    ];
    let out = LocalProcessSandbox::new()
        .run(&Job {
            program: Path::new("/usr/bin/env"),
            args: &[],
            env: &env,
            workdir: &dir,
            limits: limits(10_000, 4096),
        })
        .unwrap();
    let mut lines: Vec<&str> = std::str::from_utf8(&out.stdout).unwrap().lines().collect();
    lines.sort_unstable();
    assert_eq!(lines, ["WRIT_A=1", "WRIT_B=two words"]);
    assert_eq!(out.exit_status, 0);
    assert!(out.stderr.is_empty());
    // And nothing at all when the job environment is empty.
    let out = LocalProcessSandbox::new()
        .run(&Job {
            program: Path::new("/usr/bin/env"),
            args: &[],
            env: &[],
            workdir: &dir,
            limits: limits(10_000, 4096),
        })
        .unwrap();
    assert!(out.stdout.is_empty());
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn stdin_is_null_cwd_is_the_workdir_and_the_exit_status_is_reported() {
    let dir = tmp("cwd");
    let out = run_sh(
        &dir,
        "/bin/readlink /proc/self/fd/0; /bin/readlink /proc/self/cwd; read x; echo eof=$?; echo err >&2; exit 7",
        &[],
        limits(10_000, 4096),
    )
    .unwrap();
    let canonical = std::fs::canonicalize(&dir).unwrap();
    assert_eq!(
        String::from_utf8(out.stdout).unwrap(),
        format!("/dev/null\n{}\neof=1\n", canonical.display())
    );
    assert_eq!(out.stderr, b"err\n");
    assert_eq!(out.exit_status, 7);
    let out = run_sh(&dir, "exit 255", &[], limits(10_000, 0)).unwrap();
    assert_eq!(out.exit_status, 255);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn the_child_runs_in_its_own_process_group() {
    let dir = tmp("pgrp");
    // Field 5 of /proc/<pid>/stat is the process group: the shell leads its own group.
    let out = run_sh(
        &dir,
        "read -r a b c d pgrp rest < /proc/$$/stat; echo $$ $pgrp",
        &[],
        limits(10_000, 4096),
    )
    .unwrap();
    let text = String::from_utf8(out.stdout).unwrap();
    let mut parts = text.split_whitespace();
    let (pid, pgrp) = (parts.next().unwrap(), parts.next().unwrap());
    assert_eq!(pid, pgrp);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn the_deadline_kills_and_reaps_the_child() {
    let dir = tmp("timeout");
    let start = Instant::now();
    let r = run_sh(
        &dir,
        "echo $$ > pid; echo partial; exec /bin/sleep 30",
        &[],
        limits(500, 4096),
    );
    assert_eq!(r, Err(JobFailure::TimedOut));
    assert!(start.elapsed() < Duration::from_secs(10));
    let pid = std::fs::read_to_string(dir.join("pid")).unwrap();
    // Reaped before run returned: no process and no zombie entry remains.
    assert!(wait_gone(pid.trim()), "child {pid} still exists");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn output_beyond_the_combined_cap_kills_the_child() {
    let dir = tmp("cap");
    // Exactly at the cap (stdout plus stderr) completes.
    let out = run_sh(
        &dir,
        "printf 12345; printf 67890 >&2",
        &[],
        limits(10_000, 10),
    )
    .unwrap();
    assert_eq!((out.stdout.len(), out.stderr.len()), (5, 5));
    // One byte more fails.
    assert_eq!(
        run_sh(
            &dir,
            "printf 12345; printf 678901 >&2",
            &[],
            limits(10_000, 10)
        ),
        Err(JobFailure::OutputExceeded)
    );
    // An endless writer is killed and reaped long before its deadline.
    let start = Instant::now();
    assert_eq!(
        run_sh(
            &dir,
            "echo $$ > pid; while :; do echo xxxxxxxxxxxxxxxx; done",
            &[],
            limits(30_000, 100_000)
        ),
        Err(JobFailure::OutputExceeded)
    );
    assert!(start.elapsed() < Duration::from_secs(20));
    let pid = std::fs::read_to_string(dir.join("pid")).unwrap();
    assert!(wait_gone(pid.trim()));
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_signaled_child_is_reported_as_signaled() {
    let dir = tmp("signal");
    assert_eq!(
        run_sh(&dir, "kill -KILL $$", &[], limits(10_000, 4096)),
        Err(JobFailure::Signaled)
    );
    assert_eq!(
        run_sh(&dir, "kill -TERM $$", &[], limits(10_000, 4096)),
        Err(JobFailure::Signaled)
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_missing_program_is_a_spawn_failure() {
    let dir = tmp("missing");
    let r = LocalProcessSandbox::new().run(&Job {
        program: &dir.join("absent"),
        args: &[],
        env: &[],
        workdir: &dir,
        limits: limits(10_000, 4096),
    });
    assert_eq!(r, Err(JobFailure::Spawn));
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_panic_while_the_child_runs_still_kills_and_reaps_it() {
    let child = spawn(
        Command::new("/bin/sleep")
            .arg("30")
            .env_clear()
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null()),
    )
    .unwrap();
    let pid = child.id().to_string();
    assert!(!wait_gone(&pid));
    let r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(move || {
        let _guard = Reaper::new(child);
        panic!("job panicked");
    }));
    assert!(r.is_err());
    assert!(wait_gone(&pid), "child {pid} survived the panic");
}

#[test]
fn a_spawn_waits_for_an_ownership_change_in_progress() {
    let released = Arc::new(AtomicBool::new(false));
    let (held_tx, held_rx) = mpsc::channel();
    let holder = {
        let released = Arc::clone(&released);
        std::thread::spawn(move || {
            ownership_change(|| {
                held_tx.send(()).unwrap();
                std::thread::sleep(Duration::from_millis(200));
                released.store(true, Ordering::SeqCst);
            });
        })
    };
    held_rx.recv().unwrap();
    let mut child = spawn(Command::new("/bin/true").env_clear()).unwrap();
    assert!(
        released.load(Ordering::SeqCst),
        "spawned during an ownership change"
    );
    assert!(child.wait().unwrap().success());
    holder.join().unwrap();
}

#[test]
fn a_poisoned_gate_still_orders_spawns_and_ownership_changes() {
    let r = std::panic::catch_unwind(|| ownership_change(|| panic!("poison the gate")));
    assert!(r.is_err());
    let dir = tmp("poison");
    let lock = OwnerLock::acquire(&dir.join("x.owner")).unwrap();
    assert_eq!(
        OwnerLock::acquire(&dir.join("x.owner")).err(),
        Some(OwnerLockError::Held)
    );
    drop(lock);
    let again = OwnerLock::acquire(&dir.join("x.owner")).unwrap();
    let mut child = spawn(Command::new("/bin/true").env_clear()).unwrap();
    assert!(child.wait().unwrap().success());
    drop(again);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_released_owner_lock_is_free_while_a_duplicate_descriptor_lives() {
    let dir = tmp("dup");
    let path = dir.join("x.owner");
    // The hazard (E-066): a plain lock outlives the close of its descriptor while a
    // duplicate exists (as in a child between fork and exec; `try_clone` duplicates the
    // descriptor within this process deterministically).
    let plain = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(&path)
        .unwrap();
    plain.try_lock().unwrap();
    let duplicate = plain.try_clone().unwrap();
    drop(plain);
    assert_eq!(OwnerLock::acquire(&path).err(), Some(OwnerLockError::Held));
    drop(duplicate);
    // The fix: an OwnerLock unlocks before closing, so its release is effective at once.
    let lock = OwnerLock::acquire(&path).unwrap();
    let duplicate = lock.file.as_ref().unwrap().try_clone().unwrap();
    drop(lock);
    let again = OwnerLock::acquire(&path).unwrap();
    drop(duplicate);
    assert_eq!(OwnerLock::acquire(&path).err(), Some(OwnerLockError::Held));
    drop(again);
    let _ = std::fs::remove_dir_all(&dir);
}
