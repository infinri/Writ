//! `writ-cgroup-exec`: the launcher's fixed helper (J-0, J-11.4 step 5, J-11.5 cleanup).
//! Spawned as root by `writ-job-launcher` with a launcher-built argument vector:
//!
//! - `run ...`: join `<unit>/job`, lower rlimits, `oom_score_adj`, clear groups,
//!   `no_new_privs`, then execute the private bubblewrap as `writ-job`;
//! - `cleanup ...`: the same preparation, then execute **itself** as `writ-job` with the
//!   `cleanup-as-job` entry, which removes the job's `work/` contents after the launcher's go.
//!
//! Exits non-zero at the first failure before executing anything (`70 + step`). Refuses to run
//! unless built with `panic = "abort"`.

use std::ffi::OsString;
use std::io::Write;
use std::path::Path;
use std::process::ExitCode;
use writ_containment::helper::{self, Plan, RealOps};
use writ_containment::{build_info, fdplan, PANIC_ABORT};

/// Most entries the cleanup removes.
const CLEANUP_MAX: usize = 1_000_000;

fn err(msg: &str) {
    let _ = writeln!(std::io::stderr(), "writ-cgroup-exec: {msg}");
}

fn cleanup_as_job(args: &[OsString]) -> ExitCode {
    let (uid, gid, work) = match helper::parse_cleanup_as_job(args) {
        Ok(x) => x,
        Err(e) => {
            err(&e);
            return ExitCode::from(64);
        }
    };
    let status = std::fs::read_to_string("/proc/self/status").unwrap_or_default();
    if let Err(e) = helper::check_self_identity(&status, uid, gid) {
        err(&e);
        return ExitCode::from(71);
    }
    match fdplan::list_own_fds(Path::new("/proc")) {
        Ok(l) if l == [0, 1, 2] => {}
        other => {
            err(&format!("descriptors {other:?}"));
            return ExitCode::from(72);
        }
    }
    let mut out = std::io::stdout();
    if out
        .write_all(b"ready\n")
        .and_then(|()| out.flush())
        .is_err()
    {
        return ExitCode::from(73);
    }
    if !helper::wait_go(&mut std::io::stdin()) {
        // EOF or error: the launcher did not verify this process; do nothing.
        return ExitCode::from(74);
    }
    match helper::open_work(&work).and_then(|fd| helper::remove_contents(fd, CLEANUP_MAX)) {
        Ok(_) => ExitCode::SUCCESS,
        Err(e) => {
            err(&format!("cleanup: {e}"));
            ExitCode::from(75)
        }
    }
}

fn main() -> ExitCode {
    let args: Vec<OsString> = std::env::args_os().skip(1).collect();
    if args.len() == 1 && args[0] == "--build-info" {
        let _ = std::io::stdout().write_all(build_info().as_bytes());
        return ExitCode::SUCCESS;
    }
    if !PANIC_ABORT {
        err("refusing to run: not built with panic = \"abort\" (profile launcher)");
        return ExitCode::from(70);
    }
    if !writ_containment::TEST_FAULTS && std::env::var_os("WRIT_TEST_FAULT").is_some() {
        err("refusing to run: WRIT_TEST_FAULT set for a build without test-faults");
        return ExitCode::from(70);
    }
    if args.first().is_some_and(|a| a == "cleanup-as-job") {
        return cleanup_as_job(&args);
    }
    let plan = match Plan::parse(&args) {
        Ok(p) => p,
        Err(e) => {
            err(&e);
            return ExitCode::from(64);
        }
    };
    let failed = helper::execute(
        &plan,
        &mut RealOps {
            proc_root: "/proc".into(),
        },
    );
    err(&format!("step {}: {}", failed.step, failed.why));
    ExitCode::from(70 + failed.step)
}
