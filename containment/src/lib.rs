//! Writ V2 job containment (M5a-07; D-045 M5-D4, D-046 H-2; M5-INTERFACES J-5, J-11).
//!
//! Jobs run as `writ-job`, never as `writ`, established by a constrained root launcher
//! (`writ-job-launcher`, one systemd instance per connection on `launch.sock`) through the
//! fixed helper `writ-cgroup-exec` and the private bubblewrap with the `writ-bwrap/1` profile.
//! Everything here is safe Rust (`unsafe_code` forbidden); the launcher is single-threaded and
//! synchronous (a `poll` loop), built with `panic = "abort"` (profile `launcher`).
//!
//! Launcher side (uses only the kernel's JSON parser, `writ-hostsys` and these modules):
//!
//! - [`frame`]: J-2 framing; data frames from `writ` refused.
//! - [`protocol`]: `hello`, `writ-launch/1`, `refused`, `started`, `writ-launch-result/1`.
//! - [`config`]: the root-owned launcher configuration and fixed J-1 paths.
//! - [`source`]: the iterative source copy from `run-<job_id>` (statx before open, dev/ino).
//! - [`cgroup`]: the delegated subtree from `/proc/self/cgroup`, limits, kill and `populated 0`.
//! - [`gate`]: the gate writer that is never closed before a verified kill (H1).
//! - [`fdplan`]: descriptor plumbing (`F_DUPFD_CLOEXEC >= 10`, `dup2` to 3 and 4) and the
//!   helper's descriptor checks.
//! - [`identity`]: `--json-status-fd` parsing and pid verification by kuid and pinned image.
//! - [`residue`]: the per-launch residue checks (J-11.7, N1).
//! - [`bwrap`]: the `writ-bwrap/1` argument vector.
//! - [`helper`]: `writ-cgroup-exec` (ordering; exit at the first failure; cleanup self-exec).
//! - [`session`]: the poll loop (non-blocking, independent deadlines, bounded output buffer).
//! - [`failure`]: the J-11.6 failure table as code.
//! - [`launcher`]: the orchestration of one connection (root, VM only).
//!
//! `writ` side: `client` (next step), the `writ_broker::runner::Sandbox` that runs jobs only through
//! the launcher and reports guarantees only while readiness is verified.

pub mod bwrap;
pub mod cgroup;
pub mod config;
pub mod failure;
pub mod fdplan;
pub mod frame;
pub mod gate;
pub mod helper;
pub mod identity;
pub mod launcher;
pub mod procfs;
pub mod protocol;
pub mod residue;
pub mod session;
pub mod source;

/// Whether this build unwinds on panic (the installed binaries must not).
pub const PANIC_ABORT: bool = cfg!(panic = "abort");

/// Whether test-only fault injection is compiled in (never for installation).
pub const TEST_FAULTS: bool = cfg!(feature = "test-faults");

/// `--build-info` text of both binaries.
pub fn build_info() -> String {
    format!(
        "panic_abort={PANIC_ABORT}\ntest_faults={TEST_FAULTS}\nversion={}\n",
        env!("CARGO_PKG_VERSION")
    )
}

#[cfg(test)]
mod testutil;
