//! Process lifecycle for the mechanical runner (M3-07; INV-35; D-035 item 6; E-066).
//!
//! **Inherited descriptors and ownership locks.** A fork duplicates every descriptor of
//! this process until the child execs, and an ownership lock (`flock` on a store's
//! `.owner` file) belongs to the open file description, so closing the owner's descriptor
//! does not release it while a child still holds a duplicate. A process that closes and
//! reopens its own store while another thread spawns a child could therefore see its own
//! lock as held (`OwnedElsewhere`, E-066). Two measures address it:
//!
//! 1. **Unlock before close.** [`OwnerLock`] explicitly unlocks before closing its
//!    descriptor. Unlocking through any descriptor of the description releases the lock
//!    for every duplicate, so a release is effective at once whatever children exist.
//!    This is what removes the transient `OwnedElsewhere`.
//! 2. **Lifecycle gate.** Every spawn holds a shared guard for the whole of
//!    [`std::process::Command::spawn`]; every acquisition and release of an ownership
//!    lock holds the exclusive guard ([`ownership_change`], [`OwnerLock`]). Spawns never
//!    overlap ownership changes (no child is forked while a lock is being taken or
//!    released) and spawns among themselves run concurrently. The gate alone is not
//!    sufficient: `Command::spawn` returns once the child is past the point of no return
//!    of exec, but on Linux the parent is released (vfork completion at the new memory
//!    map, or end of file on the close-on-exec error pipe, which closes in descriptor
//!    order) before every close-on-exec descriptor of the child is closed. With the gate
//!    and without step 1, the reopen stress test of `writ-ledger-sqlite`
//!    (`tests/process_lifecycle.rs`) failed in 3 of 5 runs.
//!
//! Close-on-exec still guarantees that the new program never runs with any of these
//! descriptors open.
//!
//! The gate is process-wide and protects no data, so a poisoned guard (a panic while it
//! was held) is recovered rather than propagated: the state it orders (descriptors and
//! OS locks) is consistent whatever the panicking thread did, and refusing every later
//! spawn or store open would turn one panic into a permanent outage without protecting
//! anything. Neither guard is reentrant: [`spawn`] and [`ownership_change`] must not be
//! called while the same thread holds either guard (no code in this workspace does).
//!
//! The only production spawn path is [`spawn`] (and [`LocalProcessSandbox`], which uses
//! the same gate); the only production ownership lock is [`OwnerLock`].
//!
//! **Local process sandbox.** [`LocalProcessSandbox`] runs a job with only the standard
//! library: cleared environment plus exactly the job environment, stdin from `/dev/null`,
//! stdout and stderr piped and read concurrently under a combined byte cap, the job work
//! directory as current directory, its own process group, and a wall-time deadline. The
//! child is killed and reaped on every exit path, including a panic (drop guard).
//!
//! It is the **unsupported-for-production profile** `local-process-uncontained/1`: it
//! establishes `bounded` and `descriptors_closed` only, never filesystem, network,
//! process-tree or resource containment (no namespaces, seccomp, cgroups or rlimits are
//! available without new dependencies or `unsafe` code; real containment is M5), so
//! [`crate::runner::RunnerRequirements::production`] refuses it. In particular:
//!
//! - Descendants of the child are not contained: only the direct child is killed (a
//!   process-group kill needs a dependency change). A descendant that keeps a pipe open
//!   delays end of output until the deadline (the job then times out) and the reader
//!   threads end when it exits.
//! - A job run by this uncontained profile has the broker's full user privileges: it can
//!   read anything the broker's user can (including key material held in files or the
//!   environment of other processes of that user) and write the ledger, store and work
//!   files directly (the `.owner` lock is advisory). A child left running by a killed
//!   broker is not stopped. This is why [`crate::runner::RunnerRequirements::production`]
//!   refuses this sandbox; the attestation key itself is held only in broker memory, but
//!   that is not a containment guarantee.
//! - Descriptors: every descriptor this workspace opens (standard library and SQLite) is
//!   close-on-exec. A descriptor inherited without close-on-exec (for example from the
//!   parent of this process) cannot be closed in the child without `unsafe` code, so the
//!   sandbox checks `/proc/self/fdinfo` instead: if any descriptor above 2 lacks
//!   close-on-exec, `descriptors_closed` is false and every job is refused
//!   ([`crate::runner::JobFailure::Unavailable`]). The check runs again under the spawn
//!   guard before every job.

use std::fs::{File, OpenOptions, TryLockError};
use std::io::{self, Read};
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::path::Path;
use std::process::{Child, ChildStderr, ChildStdout, Command, Stdio};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender};
use std::sync::{PoisonError, RwLock};
use std::time::{Duration, Instant};

use crate::runner::{Job, JobFailure, JobOutput, Sandbox, SandboxGuarantees, SandboxUnavailable};
use writ_kernel::json::{self, Value};

/// The process-wide lifecycle gate (module documentation).
static GATE: RwLock<()> = RwLock::new(());

/// Run `f` under the shared spawn guard.
fn spawning<T>(f: impl FnOnce() -> T) -> T {
    let _guard = GATE.read().unwrap_or_else(PoisonError::into_inner);
    f()
}

/// Spawn `command` under the shared guard of the lifecycle gate: no ownership lock of
/// this process is acquired or released while the child can still hold duplicates of
/// this process's descriptors.
pub fn spawn(command: &mut Command) -> io::Result<Child> {
    spawning(|| command.spawn())
}

/// Run `change` (acquiring or releasing an ownership lock) under the exclusive guard of
/// the lifecycle gate, so that it never overlaps a spawn. `change` must not spawn or call
/// this function.
pub fn ownership_change<T>(change: impl FnOnce() -> T) -> T {
    let _guard = GATE.write().unwrap_or_else(PoisonError::into_inner);
    change()
}

/// Why an ownership lock was not acquired.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum OwnerLockError {
    /// Another open file description holds the lock (another owner).
    Held,
    /// The lock file could not be opened or locked.
    Io,
}

/// An exclusive OS lock (`File::try_lock`) on an ownership lock file, acquired and
/// released under the exclusive guard of the lifecycle gate. Held until dropped; a store
/// keeps it as its last field so that its connection closes first.
#[derive(Debug)]
pub struct OwnerLock {
    file: Option<File>,
}

impl OwnerLock {
    /// Open (creating it if needed, never truncating) and lock `path` without waiting.
    pub fn acquire(path: &Path) -> Result<OwnerLock, OwnerLockError> {
        ownership_change(|| {
            let file = OpenOptions::new()
                .read(true)
                .write(true)
                .create(true)
                .truncate(false)
                .open(path)
                .map_err(|_| OwnerLockError::Io)?;
            match file.try_lock() {
                Ok(()) => Ok(OwnerLock { file: Some(file) }),
                Err(TryLockError::WouldBlock) => Err(OwnerLockError::Held),
                Err(TryLockError::Error(_)) => Err(OwnerLockError::Io),
            }
        })
    }
}

impl Drop for OwnerLock {
    fn drop(&mut self) {
        if let Some(file) = self.file.take() {
            ownership_change(|| {
                // Unlock before closing: a `flock` lock belongs to the open file
                // description, so this releases it even while a child that has not yet
                // finished exec still holds a duplicate (module documentation). If it
                // fails, closing still releases it once every duplicate is gone.
                let _ = file.unlock();
                drop(file);
            });
        }
    }
}

/// The profile name bound by [`LocalProcessSandbox::environment_digest`].
pub const LOCAL_PROCESS_PROFILE: &str = "local-process-uncontained/1";

/// How often the run loop checks the child while no output arrives.
const POLL: Duration = Duration::from_millis(5);

/// `O_CLOEXEC` as reported in `/proc/<pid>/fdinfo` `flags` on the supported targets.
#[cfg(all(
    target_os = "linux",
    any(
        target_arch = "x86_64",
        target_arch = "x86",
        target_arch = "aarch64",
        target_arch = "arm",
        target_arch = "riscv64"
    )
))]
const O_CLOEXEC: Option<u32> = Some(0o2_000_000);
#[cfg(not(all(
    target_os = "linux",
    any(
        target_arch = "x86_64",
        target_arch = "x86",
        target_arch = "aarch64",
        target_arch = "arm",
        target_arch = "riscv64"
    )
)))]
const O_CLOEXEC: Option<u32> = None;

/// Whether every descriptor of this process above 2 is close-on-exec; `Err` when the
/// host cannot tell (unsupported target, `/proc` unreadable).
fn descriptors_close_on_exec() -> Result<bool, SandboxUnavailable> {
    let cloexec = O_CLOEXEC.ok_or(SandboxUnavailable)?;
    let entries = std::fs::read_dir("/proc/self/fdinfo").map_err(|_| SandboxUnavailable)?;
    let mut seen = 0usize;
    for entry in entries {
        let entry = entry.map_err(|_| SandboxUnavailable)?;
        let name = entry.file_name();
        let fd: u32 = name
            .to_str()
            .and_then(|n| n.parse().ok())
            .ok_or(SandboxUnavailable)?;
        seen += 1;
        if fd <= 2 {
            continue;
        }
        let text = match std::fs::read_to_string(entry.path()) {
            Ok(t) => t,
            // Closed by another thread since the listing: not inherited by a later spawn.
            Err(e) if e.kind() == io::ErrorKind::NotFound => continue,
            Err(_) => return Err(SandboxUnavailable),
        };
        let flags = text
            .lines()
            .find_map(|l| l.strip_prefix("flags:"))
            .and_then(|v| u32::from_str_radix(v.trim(), 8).ok())
            .ok_or(SandboxUnavailable)?;
        if flags & cloexec == 0 {
            return Ok(false);
        }
    }
    // The listing itself holds a descriptor, so an empty listing means it was not real.
    if seen == 0 {
        return Err(SandboxUnavailable);
    }
    Ok(true)
}

/// A local process sandbox without containment (module documentation): the
/// unsupported-for-production profile [`LOCAL_PROCESS_PROFILE`].
#[derive(Clone, Copy, Debug, Default)]
pub struct LocalProcessSandbox;

impl LocalProcessSandbox {
    /// The sandbox.
    pub const fn new() -> LocalProcessSandbox {
        LocalProcessSandbox
    }
}

impl Sandbox for LocalProcessSandbox {
    fn guarantees(&self) -> Result<SandboxGuarantees, SandboxUnavailable> {
        let closed = descriptors_close_on_exec()?;
        Ok(SandboxGuarantees {
            filesystem_isolated: false,
            network_isolated: false,
            process_tree_contained: false,
            resource_limited: false,
            descriptors_closed: closed,
            bounded: true,
        })
    }

    /// D-006 digest of `{"contract":"writ-sandbox-profile/1","profile":
    /// "local-process-uncontained/1","guarantees":{...}}` with the guarantees reported
    /// now (all false when the host cannot report them).
    fn environment_digest(&self) -> String {
        let g = self.guarantees().unwrap_or_default();
        let doc = json::obj([
            ("contract", json::s("writ-sandbox-profile/1")),
            ("profile", json::s(LOCAL_PROCESS_PROFILE)),
            (
                "guarantees",
                json::obj([
                    ("filesystem_isolated", Value::Bool(g.filesystem_isolated)),
                    ("network_isolated", Value::Bool(g.network_isolated)),
                    (
                        "process_tree_contained",
                        Value::Bool(g.process_tree_contained),
                    ),
                    ("resource_limited", Value::Bool(g.resource_limited)),
                    ("descriptors_closed", Value::Bool(g.descriptors_closed)),
                    ("bounded", Value::Bool(g.bounded)),
                ]),
            ),
        ]);
        // Every member is a fixed string or a boolean, inside the D-006 domain.
        json::digest(&doc)
            .map(|d| d.as_str().to_string())
            .unwrap_or_default()
    }

    fn run(&self, job: &Job<'_>) -> Result<JobOutput, JobFailure> {
        let deadline = Instant::now()
            .checked_add(job.limits.wall_time)
            .ok_or(JobFailure::Unavailable)?;
        let mut command = Command::new(job.program);
        command
            .args(job.args)
            .env_clear()
            .envs(job.env.iter().map(|(k, v)| (k, v)))
            .current_dir(job.workdir)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .process_group(0);
        let child = spawning(|| match descriptors_close_on_exec() {
            Ok(true) => command.spawn().map_err(|_| JobFailure::Spawn),
            Ok(false) | Err(SandboxUnavailable) => Err(JobFailure::Unavailable),
        })?;
        let mut child = Reaper::new(child);
        let (tx, rx) = mpsc::channel();
        let stdout = child.child.stdout.take().ok_or(JobFailure::Io)?;
        let stderr = child.child.stderr.take().ok_or(JobFailure::Io)?;
        pump(Pipe::Out(stdout), tx.clone())?;
        pump(Pipe::Err(stderr), tx)?;
        collect(&mut child, &rx, deadline, job.limits.max_output_bytes)
    }
}

/// Kills and reaps its child when dropped unless it was already reaped, so no exit path
/// (including a panic) leaves a running or zombie child.
struct Reaper {
    child: Child,
    reaped: bool,
}

impl Reaper {
    fn new(child: Child) -> Reaper {
        Reaper {
            child,
            reaped: false,
        }
    }

    /// Kill (if still running) and reap the child, then report `failure`.
    fn fail<T>(&mut self, failure: JobFailure) -> Result<T, JobFailure> {
        self.kill_and_reap();
        Err(failure)
    }

    fn kill_and_reap(&mut self) {
        if !self.reaped {
            let _ = self.child.kill();
            let _ = self.child.wait();
            self.reaped = true;
        }
    }
}

impl Drop for Reaper {
    fn drop(&mut self) {
        self.kill_and_reap();
    }
}

enum Pipe {
    Out(ChildStdout),
    Err(ChildStderr),
}

enum Chunk {
    Out(Vec<u8>),
    Err(Vec<u8>),
    Eof,
    Failed,
}

/// Read `pipe` to its end on a detached thread (a descendant of the child may hold it
/// open after the child is gone), sending chunks to `tx`.
fn pump(pipe: Pipe, tx: Sender<Chunk>) -> Result<(), JobFailure> {
    std::thread::Builder::new()
        .name("writ-sandbox-pipe".to_string())
        .spawn(move || {
            let (mut reader, is_out): (Box<dyn Read + Send>, bool) = match pipe {
                Pipe::Out(p) => (Box::new(p), true),
                Pipe::Err(p) => (Box::new(p), false),
            };
            loop {
                let mut buf = vec![0u8; 8192];
                let msg = match reader.read(&mut buf) {
                    Ok(0) => Chunk::Eof,
                    Ok(n) => {
                        buf.truncate(n);
                        if is_out {
                            Chunk::Out(buf)
                        } else {
                            Chunk::Err(buf)
                        }
                    }
                    Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
                    Err(_) => Chunk::Failed,
                };
                let last = matches!(msg, Chunk::Eof | Chunk::Failed);
                if tx.send(msg).is_err() || last {
                    return;
                }
            }
        })
        .map(drop)
        .map_err(|_| JobFailure::Io)
}

/// Collect output and the exit status until both pipes ended and the child was reaped,
/// enforcing the deadline and the combined output cap.
fn collect(
    child: &mut Reaper,
    rx: &Receiver<Chunk>,
    deadline: Instant,
    max_output_bytes: usize,
) -> Result<JobOutput, JobFailure> {
    let (mut stdout, mut stderr) = (Vec::new(), Vec::new());
    let mut open = 2u8;
    let mut status = None;
    loop {
        if open == 0 {
            if let Some(status) = status {
                return finished(status, stdout, stderr);
            }
        }
        let now = Instant::now();
        if now >= deadline {
            return child.fail(JobFailure::TimedOut);
        }
        let wait = (deadline - now).min(POLL);
        if open > 0 {
            match rx.recv_timeout(wait) {
                Ok(Chunk::Out(b)) => stdout.extend_from_slice(&b),
                Ok(Chunk::Err(b)) => stderr.extend_from_slice(&b),
                Ok(Chunk::Eof) => open -= 1,
                Ok(Chunk::Failed) | Err(RecvTimeoutError::Disconnected) => {
                    return child.fail(JobFailure::Io)
                }
                Err(RecvTimeoutError::Timeout) => {}
            }
            if stdout.len().saturating_add(stderr.len()) > max_output_bytes {
                return child.fail(JobFailure::OutputExceeded);
            }
        } else {
            std::thread::sleep(wait);
        }
        if status.is_none() {
            match child.child.try_wait() {
                Ok(Some(s)) => {
                    child.reaped = true;
                    status = Some(s);
                }
                Ok(None) => {}
                Err(_) => return child.fail(JobFailure::Io),
            }
        }
    }
}

fn finished(
    status: std::process::ExitStatus,
    stdout: Vec<u8>,
    stderr: Vec<u8>,
) -> Result<JobOutput, JobFailure> {
    if status.signal().is_some() {
        return Err(JobFailure::Signaled);
    }
    let code = status.code().ok_or(JobFailure::Io)?;
    let exit_status = u8::try_from(code).map_err(|_| JobFailure::Io)?;
    Ok(JobOutput {
        exit_status,
        stdout,
        stderr,
    })
}

#[cfg(test)]
mod tests;
