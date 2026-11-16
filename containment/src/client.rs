//! The `writ` side of `launch.sock` (J-5, J-11.2, J-11.6): [`LauncherSandbox`] implements
//! [`writ_broker::runner::Sandbox`] and is the only sandbox that reports every guarantee,
//! including [`SandboxGuarantees::job_identity_separated`], and only while the service's
//! readiness ([`Readiness`]) is verified. `RunnerRequirements::production()` therefore accepts
//! jobs only through the launcher; any other sandbox, or this one without verified readiness,
//! is refused (no uncontained fallback).
//!
//! Per job: the job id is derived from the materialized work directory `run-<job_id>` under the
//! configured run root (the launcher derives the same source path, M4); the peer of the
//! connection must be the launcher's uid (root) by `SO_PEERCRED` before anything is sent; the
//! request is sent only after a `hello` whose facts equal the pinned ones; frames are bounded;
//! the `finished` frame must carry this request's digest and the pinned digests. Conclusions
//! follow [`crate::failure::conclude`]: a lost connection after the request is **uncertain**
//! ([`JobFailure::Uncertain`]: the run stays `started`), a digest mismatch, a cgroup that did not
//! empty, a differing `hello` or `LAUNCH_UNAVAILABLE` withdraw readiness.

use crate::failure::{self, Observed, RunConclusion};
use crate::frame::{self, Decoder, Frame, Stream};
use crate::protocol::{
    self, Finished, Hello, JobLimits, LaunchCode, LauncherFrame, Reason, Request, ResultDigests,
};
use std::collections::VecDeque;
use std::io::{ErrorKind, Read, Write};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};
use writ_broker::runner::{
    Job, JobFailure, JobOutput, Sandbox, SandboxGuarantees, SandboxUnavailable,
};
use writ_kernel::json::{self, s};

/// The service's containment readiness (the `VerifiedDeployment` proof, the launcher socket
/// unit's state, the pinned digests; J-5, J-9). Implemented by `writ-serviced`.
pub trait Readiness: Sync {
    /// Readiness is verified now.
    fn verified(&self) -> bool;
    /// Withdraw readiness (until the operator re-runs the probes and re-pins).
    fn withdraw(&self, why: &str);
}

/// Client configuration.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ClientConfig {
    /// `/run/writ-launcher/launch.sock`.
    pub socket: PathBuf,
    /// The launcher's uid (0).
    pub launcher_uid: u32,
    /// The run root where the runner materializes `run-<job_id>` (`/var/lib/writ/run`).
    pub run_root: PathBuf,
    /// The runtime profile jobs use.
    pub runtime_profile: String,
    /// Pinned `hello` facts.
    pub pinned: Hello,
    /// Limits sent with every request; `wall_time_ms` and `output_max_bytes` are replaced by
    /// the job's own limits.
    pub limits: JobLimits,
    /// Deadline for `hello`.
    pub hello_deadline: Duration,
    /// Slack added to the wall time for start, kill verification and cleanup.
    pub slack: Duration,
}

/// The sandbox that runs jobs only through the launcher.
pub struct LauncherSandbox<'a> {
    cfg: ClientConfig,
    readiness: &'a dyn Readiness,
    termination: Option<Arc<AtomicBool>>,
}

/// How often a running job's connection checks the termination token.
const TERMINATION_POLL: Duration = Duration::from_millis(50);

/// Contract of the environment digest.
pub const PROFILE: &str = "writ-launcher/1";

impl<'a> LauncherSandbox<'a> {
    /// A sandbox over `cfg`, guarded by `readiness`.
    pub fn new(cfg: ClientConfig, readiness: &'a dyn Readiness) -> LauncherSandbox<'a> {
        LauncherSandbox {
            cfg,
            readiness,
            termination: None,
        }
    }

    /// Cancel a running job when `token` is set (the service's graceful shutdown, J-8 step
    /// 4): the client sends `writ-launch-cancel/1`; the launcher kills the job, verifies its
    /// cgroup empty and answers `finished` with reason `cancelled`, which this sandbox
    /// reports as [`JobFailure::TerminatedShutdown`]. Without that answer the run stays
    /// uncertain ([`JobFailure::Uncertain`]).
    pub fn with_termination(mut self, token: Arc<AtomicBool>) -> LauncherSandbox<'a> {
        self.termination = Some(token);
        self
    }

    fn terminating(&self) -> bool {
        self.termination
            .as_ref()
            .is_some_and(|t| t.load(Ordering::SeqCst))
    }

    fn expected(&self, req: &Request) -> Finished {
        let p = &self.cfg.pinned;
        Finished {
            job_id: req.job_id.clone(),
            request_digest: req.digest.clone(),
            reason: Reason::Exited,
            exit_status: None,
            signal: None,
            kill_used: true,
            populated_zero: true,
            memory_events: [0; 3],
            pids_max_events: 0,
            cpu_usage_usec: 0,
            output_bytes: [0, 0],
            digests: ResultDigests {
                bwrap: p.bwrap_digest.clone(),
                bwrap_profile: p.bwrap_profile_digest.clone(),
                apparmor_profile: p.apparmor_profile_digest.clone(),
                runtime_profile: p
                    .runtime_profile(&self.cfg.runtime_profile)
                    .unwrap_or_default()
                    .to_string(),
                exec: p.exec_digest.clone(),
            },
        }
    }

    /// The request for `job`, or `None` when the job is not a materialized run directory under
    /// the run root.
    pub fn request(&self, job: &Job<'_>) -> Option<Request> {
        let dir = job.workdir;
        if dir.parent() != Some(self.cfg.run_root.as_path()) {
            return None;
        }
        let job_id = dir.file_name()?.to_str()?.strip_prefix("run-")?;
        if !protocol::valid_job_id(job_id) {
            return None;
        }
        let program = job.program.strip_prefix(dir).ok()?.to_str()?;
        if !protocol::valid_relative_path(program) {
            return None;
        }
        let limits = JobLimits {
            wall_time_ms: u64::try_from(job.limits.wall_time.as_millis()).ok()?,
            output_max_bytes: u64::try_from(job.limits.max_output_bytes).ok()?,
            ..self.cfg.limits
        };
        Request::new(
            job_id,
            &self.cfg.runtime_profile,
            program,
            job.args.to_vec(),
            job.env.to_vec(),
            limits,
        )
    }

    /// Run the exchange for `req`: what was observed, and whether this side sent `cancel`.
    fn observe_cancellable(&self, req: &Request, out: &mut Collected) -> (Observed, bool) {
        let mut cancelled = false;
        let observed = self.observe_inner(req, out, &mut cancelled);
        (observed, cancelled)
    }

    fn observe_inner(&self, req: &Request, out: &mut Collected, cancelled: &mut bool) -> Observed {
        let Ok(conn) = UnixStream::connect(&self.cfg.socket) else {
            return Observed::NoHello;
        };
        match writ_hostsys::sock::peer_credentials(&conn) {
            Ok(p) if p.uid == self.cfg.launcher_uid => {}
            _ => return Observed::NoHello,
        }
        let hello_by = Instant::now() + self.cfg.hello_deadline;
        let mut r = Reader {
            conn,
            d: Decoder::new(true),
            queue: VecDeque::new(),
        };
        let hello = match r.next(hello_by) {
            Next::Frame(Frame::Control(p)) => Hello::parse(&p),
            _ => return Observed::NoHello,
        };
        match hello {
            Some(h) if h.matches(&self.cfg.pinned) => {}
            _ => return Observed::HelloDiffers,
        }
        let Ok(text) = json::canonical(&req.to_value()) else {
            return Observed::NoHello;
        };
        let Some(f) = frame::encode(frame::CONTROL, text.as_bytes()) else {
            return Observed::NoHello;
        };
        if r.conn.write_all(&f).is_err() {
            // The request may or may not have arrived.
            return Observed::Lost;
        }
        let wall = Duration::from_millis(req.limits.wall_time_ms);
        let end = Instant::now() + wall + self.cfg.slack;
        loop {
            if !*cancelled && self.terminating() {
                let sent = json::canonical(&protocol::cancel(&req.job_id))
                    .ok()
                    .and_then(|t| frame::encode(frame::CONTROL, t.as_bytes()))
                    .is_some_and(|f| r.conn.write_all(&f).is_ok());
                if !sent {
                    return Observed::Lost;
                }
                *cancelled = true;
            }
            let slice = if self.termination.is_some() && !*cancelled {
                end.min(Instant::now() + TERMINATION_POLL)
            } else {
                end
            };
            let got = match r.next(slice) {
                Next::TimedOut if slice < end => continue,
                Next::TimedOut | Next::Gone => return Observed::Lost,
                Next::Frame(f) => f,
            };
            match Some(got) {
                None => return Observed::Lost,
                Some(Frame::Data(p)) => match frame::parse_output(&p) {
                    Some((stream, id, bytes)) if id == req.job_id => {
                        if !out.push(stream, bytes, req.limits.output_max_bytes) {
                            return Observed::Lost;
                        }
                    }
                    _ => return Observed::Lost,
                },
                Some(Frame::Control(p)) => match protocol::parse_launcher_frame(&p) {
                    Some(LauncherFrame::Refused(id, code)) if id == req.job_id || id.is_empty() => {
                        return Observed::Refused(code)
                    }
                    Some(LauncherFrame::Started(id)) if id == req.job_id => out.started = true,
                    Some(LauncherFrame::Finished(fin)) => return Observed::Finished(fin),
                    _ => return Observed::Lost,
                },
            }
        }
    }
}

/// Output collected from data frames.
#[derive(Default)]
struct Collected {
    stdout: Vec<u8>,
    stderr: Vec<u8>,
    started: bool,
}

impl Collected {
    fn push(&mut self, s: Stream, bytes: &[u8], max: u64) -> bool {
        let total = (self.stdout.len() + self.stderr.len() + bytes.len()) as u64;
        if total > max {
            return false;
        }
        match s {
            Stream::Stdout => self.stdout.extend_from_slice(bytes),
            Stream::Stderr => self.stderr.extend_from_slice(bytes),
        }
        true
    }
}

/// What the next read from the launcher produced.
enum Next {
    /// A complete frame.
    Frame(Frame),
    /// The deadline passed (the connection is intact).
    TimedOut,
    /// EOF, an error or a framing violation.
    Gone,
}

/// Frames from the launcher, bounded by the decoder's per-type maxima.
struct Reader {
    conn: UnixStream,
    d: Decoder,
    queue: VecDeque<Frame>,
}

impl Reader {
    /// The next frame before `deadline`.
    fn next(&mut self, deadline: Instant) -> Next {
        let mut buf = [0u8; 16 * 1024];
        loop {
            if let Some(f) = self.queue.pop_front() {
                return Next::Frame(f);
            }
            let left = deadline.saturating_duration_since(Instant::now());
            if left.is_zero() {
                return Next::TimedOut;
            }
            if self.conn.set_read_timeout(Some(left)).is_err() {
                return Next::Gone;
            }
            let n = match self.conn.read(&mut buf) {
                Ok(0) => return Next::Gone,
                Ok(n) => n,
                Err(e) if e.kind() == ErrorKind::Interrupted => continue,
                Err(e) if matches!(e.kind(), ErrorKind::WouldBlock | ErrorKind::TimedOut) => {
                    return Next::TimedOut
                }
                Err(_) => return Next::Gone,
            };
            match self.d.feed(&buf[..n]) {
                Ok(frames) => self.queue.extend(frames),
                Err(_) => return Next::Gone,
            }
        }
    }
}

impl Sandbox for LauncherSandbox<'_> {
    fn guarantees(&self) -> Result<SandboxGuarantees, SandboxUnavailable> {
        if self.readiness.verified() {
            Ok(SandboxGuarantees::all())
        } else {
            Err(SandboxUnavailable)
        }
    }

    /// D-006 digest of `{"contract":"writ-sandbox-profile/1","profile":"writ-launcher/1",
    /// "bwrap","bwrap_profile","apparmor_profile","runtime_profile","exec","launcher",
    /// "maxima"}` from the pinned facts.
    fn environment_digest(&self) -> String {
        let p = &self.cfg.pinned;
        json::digest(&json::obj([
            ("contract", s("writ-sandbox-profile/1")),
            ("profile", s(PROFILE)),
            ("bwrap", s(&p.bwrap_digest)),
            ("bwrap_profile", s(&p.bwrap_profile_digest)),
            ("apparmor_profile", s(&p.apparmor_profile_digest)),
            (
                "runtime_profile",
                s(p.runtime_profile(&self.cfg.runtime_profile)
                    .unwrap_or_default()),
            ),
            ("exec", s(&p.exec_digest)),
            ("launcher", s(&p.launcher_digest)),
            ("maxima", s(&p.maxima_digest)),
        ]))
        .map(|d| d.as_str().to_string())
        .unwrap_or_default()
    }

    fn run(&self, job: &Job<'_>) -> Result<JobOutput, JobFailure> {
        if !self.readiness.verified() {
            return Err(JobFailure::Unavailable);
        }
        let req = self.request(job).ok_or(JobFailure::Unavailable)?;
        let mut out = Collected::default();
        let (observed, cancelled) = self.observe_cancellable(&req, &mut out);
        let expected = self.expected(&req);
        let c = failure::conclude(&observed, &expected);
        if c.withdraw_readiness {
            self.readiness.withdraw(match c.failure {
                Some(f) => match f {
                    failure::Failure::HelloDiffers => {
                        "launcher hello differs from the pinned facts"
                    }
                    failure::Failure::DigestMismatch => "launcher result digests differ",
                    failure::Failure::CgroupNotEmpty => "job cgroup did not empty",
                    _ => "launcher reported LAUNCH_UNAVAILABLE",
                },
                None => "launcher",
            });
        }
        match (c.run, &observed) {
            (RunConclusion::Uncertain, _) => Err(JobFailure::Uncertain),
            (RunConclusion::NotRun, _) => Err(JobFailure::Unavailable),
            (
                RunConclusion::Failed,
                Observed::Refused(LaunchCode::Unavailable | LaunchCode::Busy),
            ) => Err(JobFailure::Unavailable),
            (RunConclusion::Failed, Observed::Refused(_)) => Err(JobFailure::Spawn),
            (RunConclusion::Failed, Observed::Finished(f))
                if f.reason == Reason::StartFailed && f.populated_zero =>
            {
                Err(JobFailure::Spawn)
            }
            (RunConclusion::Failed, _) => Err(JobFailure::Unavailable),
            (RunConclusion::AsReported, Observed::Finished(f)) => {
                if f.output_bytes != [out.stdout.len() as u64, out.stderr.len() as u64]
                    || !out.started
                {
                    return Err(JobFailure::Io);
                }
                match f.reason {
                    Reason::Exited => match f.exit_status.and_then(|x| u8::try_from(x).ok()) {
                        Some(exit_status) => Ok(JobOutput {
                            exit_status,
                            stdout: out.stdout,
                            stderr: out.stderr,
                        }),
                        None => Err(JobFailure::Io),
                    },
                    Reason::Signaled => Err(JobFailure::Signaled),
                    Reason::TimedOut => Err(JobFailure::TimedOut),
                    Reason::OutputExceeded => Err(JobFailure::OutputExceeded),
                    // J-8 step 4: killed and verified empty after this side's cancel.
                    Reason::Cancelled if cancelled => Err(JobFailure::TerminatedShutdown),
                    Reason::Cancelled | Reason::StartFailed => Err(JobFailure::Io),
                }
            }
            _ => Err(JobFailure::Io),
        }
    }
}

/// The run directory `run-<job_id>` of a job id under `run_root`.
pub fn run_dir(run_root: &Path, job_id: &str) -> Option<PathBuf> {
    protocol::valid_job_id(job_id).then(|| run_root.join(format!("run-{job_id}")))
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests;
