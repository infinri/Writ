//! One `writ-job-launcher@<instance>.service` (J-11.2 to J-11.5). Root, single-threaded,
//! synchronous, no signal handling (N7). **Runs only in the designated disposable VM**: every
//! step below needs root, the delegated cgroup subtree and the private bubblewrap.
//!
//! Connection hand-over (deviation recorded for the lead): with `unsafe_code` forbidden a
//! process cannot take ownership of the inherited raw descriptor 3 that `LISTEN_FDS` passes,
//! so the socket unit's service runs with `StandardInput=socket` (the accepted connection on
//! descriptors 0 and 1). The launcher duplicates it to a close-on-exec descriptor at or above
//! 10 and marks 0 and 1 close-on-exec (replacing them needs `dup2` onto an owned 0 and 1,
//! which safe Rust can only do through rustix's `stdio` feature, outside the reviewed feature
//! set); every child is spawned with explicit standard streams, so 0 and 1 never reach one.
//! It requires its descriptor set to be exactly `{0, 1, 2, conn}` and then reserves 3 and 4
//! ([`crate::fdplan::Slots`]). **Verify in VM** that systemd passes no other descriptor with
//! `StandardInput=socket` and `Accept=yes`.
//!
//! Order (J-11.4): admission by `SO_PEERCRED` before any read; `hello`; the request within the
//! request deadline; validation; readiness (runtime-profile manifest recomputed) and residue
//! checks; job directory and source copy as root, then `fchown` of every copied entry to
//! `writ-job`; `CAP_CHOWN` dropped from the permitted and effective sets; cgroup (supervisor,
//! controllers, `job` with limits); descriptor plumbing and the helper spawn. **After the spawn
//! no `refused` is ever sent.** Start (status pipe), verification with the gate closed, release,
//! `started`, the running loop, then always: `cgroup.kill`, `populated 0`, and only then the
//! gate writer is closed; cleanup as `writ-job` through the helper's self-exec; `finished`.

use crate::bwrap;
use crate::cgroup::{unit_from_proc_cgroup, Cgroups, KillProof, UnitPath};
use crate::config::{Config, Paths};
use crate::fdplan::{self, Slots};
use crate::frame::{self, Decoder, Frame};
use crate::gate::Gate;
use crate::helper::{HelperMode, Plan, Rlimits};
use crate::identity::{self, Expect};
use crate::procfs::HostProc;
use crate::protocol::{self, Finished, Hello, LaunchCode, Reason, Request, ResultDigests};
use crate::residue;
use crate::session::{self, Bounds, Channels, Session, Start};
use crate::source::{self, Owner};
use rustix::event::{poll, PollFd, PollFlags, Timespec};
use rustix::fs::{mkdirat, openat2, unlinkat, AtFlags, Mode, OFlags, ResolveFlags};
use std::fs::File;
use std::io::{self, ErrorKind, Read, Write};
use std::os::fd::{AsFd, BorrowedFd, OwnedFd};
use std::os::unix::fs::MetadataExt;
use std::os::unix::net::UnixStream;
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};
use writ_kernel::json::{self, Digest, Value};

/// Exit status of an instance.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Exit {
    /// The connection ended normally (job finished and reported, or refused, or no request).
    Ok = 0,
    /// The instance could not serve (configuration, descriptors, admission).
    Unavailable = 1,
    /// The job cgroup did not empty: the gate stays open, nothing is cleaned; systemd stops
    /// the instance (`KillMode=control-group`).
    NotEmpty = 2,
    /// Cleanup failed: `job-<instance>/` is left as residue (the next launch refuses).
    Residue = 3,
}

fn log(msg: &str) {
    // stderr goes to the journal; never key material or request content.
    let _ = writeln!(io::stderr(), "writ-job-launcher: {msg}");
}

/// SHA-256 digest of a file's bytes (`sha256:<hex>`).
pub fn file_digest(p: &std::path::Path) -> io::Result<String> {
    Ok(Digest::of_bytes(&std::fs::read(p)?).as_str().to_string())
}

/// The `hello` facts from the configuration and the binaries on disk.
pub fn hello(
    paths: &Paths,
    cfg: &Config,
    instance: &str,
    self_exe: &std::path::Path,
) -> io::Result<Hello> {
    Ok(Hello {
        launcher_digest: file_digest(self_exe)?,
        exec_digest: file_digest(&paths.exec)?,
        bwrap_digest: file_digest(&paths.bwrap)?,
        bwrap_profile_digest: bwrap::profile_digest(),
        apparmor_profile_digest: file_digest(&paths.apparmor_profile)?,
        runtime_profiles: cfg
            .runtime_profiles
            .iter()
            .map(|p| (p.id.clone(), p.digest.clone()))
            .collect(),
        maxima_digest: cfg.maxima_digest(),
        instance: instance.to_string(),
    })
}

fn timespec(deadline: Instant) -> Timespec {
    let left = deadline.saturating_duration_since(Instant::now());
    Timespec {
        tv_sec: i64::try_from(left.as_secs()).unwrap_or(i64::MAX),
        tv_nsec: i64::from(left.subsec_nanos()),
    }
}

/// Whether the connection has data or EOF pending right now.
pub fn readable_now(conn: &UnixStream) -> bool {
    let mut fds = [PollFd::new(conn, PollFlags::IN)];
    let zero = Timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    matches!(poll(&mut fds, Some(&zero)), Ok(n) if n > 0)
}

/// Write `bytes` to a non-blocking connection before `deadline`.
pub fn send_all(conn: &mut UnixStream, mut bytes: &[u8], deadline: Instant) -> io::Result<()> {
    while !bytes.is_empty() {
        match conn.write(bytes) {
            Ok(0) => return Err(ErrorKind::WriteZero.into()),
            Ok(n) => bytes = &bytes[n..],
            Err(e) if e.kind() == ErrorKind::Interrupted => {}
            Err(e) if e.kind() == ErrorKind::WouldBlock => {
                if Instant::now() >= deadline {
                    return Err(ErrorKind::TimedOut.into());
                }
                let mut fds = [PollFd::new(&*conn, PollFlags::OUT)];
                poll(&mut fds, Some(&timespec(deadline)))?;
            }
            Err(e) => return Err(e),
        }
    }
    Ok(())
}

/// Send one control frame.
pub fn send_control(conn: &mut UnixStream, v: &Value, deadline: Instant) -> io::Result<()> {
    let text = json::canonical(v).map_err(|_| io::Error::other("canonical"))?;
    let f =
        frame::encode(frame::CONTROL, text.as_bytes()).ok_or_else(|| io::Error::other("frame"))?;
    send_all(conn, &f, deadline)
}

/// Read the first control frame before `deadline` (a data frame, ancillary data, EOF or the
/// deadline is an error: the instance closes with nothing started).
pub fn read_request(conn: &UnixStream, deadline: Instant) -> io::Result<Vec<u8>> {
    let mut d = Decoder::new(false);
    let mut buf = [0u8; 4096];
    loop {
        if Instant::now() >= deadline {
            return Err(ErrorKind::TimedOut.into());
        }
        let mut fds = [PollFd::new(conn, PollFlags::IN)];
        match poll(&mut fds, Some(&timespec(deadline))) {
            Ok(_) | Err(rustix::io::Errno::INTR) => {}
            Err(e) => return Err(e.into()),
        }
        match writ_hostsys::sock::recv_plain(conn, &mut buf) {
            Ok(0) => return Err(ErrorKind::UnexpectedEof.into()),
            Ok(n) => {
                let frames = d
                    .feed(&buf[..n])
                    .map_err(|e| io::Error::new(ErrorKind::InvalidData, format!("{e:?}")))?;
                if let Some(Frame::Control(p)) = frames.into_iter().next() {
                    return Ok(p);
                }
            }
            Err(e) if matches!(e.kind(), ErrorKind::WouldBlock | ErrorKind::Interrupted) => {}
            Err(e) => return Err(e),
        }
    }
}

/// Owner hook that defers `fchown` until the whole copy succeeded (root can then still remove
/// a partial copy without `CAP_DAC_OVERRIDE`, which the unit does not grant).
pub struct Deferred {
    fds: Vec<OwnedFd>,
}

impl Deferred {
    /// An empty hook.
    pub fn new() -> Deferred {
        Deferred { fds: Vec::new() }
    }

    /// `fchown` every recorded entry to `uid`/`gid`.
    pub fn apply(self, uid: u32, gid: u32) -> io::Result<()> {
        for fd in &self.fds {
            rustix::fs::fchown(
                fd,
                Some(rustix::fs::Uid::from_raw(uid)),
                Some(rustix::fs::Gid::from_raw(gid)),
            )?;
        }
        Ok(())
    }

    /// Entries recorded.
    pub fn len(&self) -> usize {
        self.fds.len()
    }

    /// No entry recorded.
    pub fn is_empty(&self) -> bool {
        self.fds.is_empty()
    }
}

impl Default for Deferred {
    fn default() -> Self {
        Deferred::new()
    }
}

impl Owner for Deferred {
    fn own(&mut self, fd: BorrowedFd<'_>) -> io::Result<()> {
        self.fds.push(fd.try_clone_to_owned()?);
        Ok(())
    }
}

/// Drop `CAP_CHOWN` from the permitted and effective sets (J-11.3, M5, N3); the bounding set is
/// unchanged and `NoNewPrivileges=yes` prevents regaining it.
pub fn drop_chown() -> io::Result<()> {
    use rustix::thread::{capabilities, set_capabilities, CapabilitySet};
    let mut c = capabilities(None)?;
    c.effective.remove(CapabilitySet::CHOWN);
    c.permitted.remove(CapabilitySet::CHOWN);
    set_capabilities(None, c)?;
    let after = capabilities(None)?;
    if after.effective.contains(CapabilitySet::CHOWN)
        || after.permitted.contains(CapabilitySet::CHOWN)
    {
        return Err(io::Error::other("CAP_CHOWN still present"));
    }
    Ok(())
}

/// The connection taken from standard input (`StandardInput=socket`), moved high; 0 and 1
/// marked close-on-exec; then the descriptor set must be exactly `{0, 1, 2, conn}`.
pub fn take_connection(proc_root: &std::path::Path) -> io::Result<UnixStream> {
    let conn = io::stdin().as_fd().try_clone_to_owned()?;
    let conn = fdplan::high(conn)?;
    let st = rustix::fs::fstat(&conn)?;
    if rustix::fs::FileType::from_raw_mode(st.st_mode) != rustix::fs::FileType::Socket {
        return Err(io::Error::other("standard input is not a socket"));
    }
    for fd in [io::stdin().as_fd(), io::stdout().as_fd()] {
        rustix::io::fcntl_setfd(fd, rustix::io::FdFlags::CLOEXEC)?;
    }
    let own = std::os::fd::AsRawFd::as_raw_fd(&conn);
    let listed = fdplan::list_own_fds(proc_root)?;
    let mut want = vec![0, 1, 2, own];
    want.sort_unstable();
    if listed != want {
        return Err(io::Error::other(format!(
            "descriptor set {listed:?}, expected {want:?}"
        )));
    }
    Ok(UnixStream::from(conn))
}

struct Job<'a> {
    paths: &'a Paths,
    cfg: &'a Config,
    unit: UnitPath,
    req: Request,
    digests: ResultDigests,
    job_dir: Option<OwnedFd>,
    job_dir_name: String,
}

fn refuse(conn: &mut UnixStream, job_id: &str, code: LaunchCode) -> Exit {
    log(&format!("refused {}", code.as_str()));
    let _ = send_control(
        conn,
        &protocol::refused(job_id, code),
        Instant::now() + Duration::from_secs(2),
    );
    Exit::Ok
}

/// Serve one connection; returns the instance's exit status.
pub fn serve(paths: &Paths) -> Exit {
    let cfg = match std::fs::read(&paths.config)
        .map_err(|e| e.to_string())
        .and_then(|b| Config::parse(&b).map_err(|e| e.0))
    {
        Ok(c) => c,
        Err(e) => {
            log(&format!("configuration: {e}"));
            return Exit::Unavailable;
        }
    };
    if !rustix::process::geteuid().is_root() {
        log("not running as root");
        return Exit::Unavailable;
    }
    let mut conn = match take_connection(&paths.proc_root) {
        Ok(c) => c,
        Err(e) => {
            log(&format!("connection: {e}"));
            return Exit::Unavailable;
        }
    };
    // Admission before any read (J-11.2).
    match writ_hostsys::sock::peer_credentials(&conn) {
        Ok(p) if p.uid == cfg.writ_uid && p.uid != 0 && p.uid != crate::config::OVERFLOW_ID => {}
        Ok(p) => {
            log(&format!("peer uid {} refused", p.uid));
            return Exit::Ok;
        }
        Err(e) => {
            log(&format!("peer credentials: {e}"));
            return Exit::Unavailable;
        }
    }
    let slots = match Slots::reserve() {
        Ok(s) => s,
        Err(e) => {
            log(&format!("descriptor slots: {e}"));
            return Exit::Unavailable;
        }
    };
    if conn.set_nonblocking(true).is_err() {
        return Exit::Unavailable;
    }
    let cg_text = std::fs::read_to_string(paths.proc_root.join("self/cgroup")).unwrap_or_default();
    let unit = match unit_from_proc_cgroup(&cg_text, &paths.slice) {
        Ok(u) => u,
        Err(e) => {
            log(&format!("unit cgroup: {e}"));
            return Exit::Unavailable;
        }
    };
    let self_exe = paths.proc_root.join("self/exe");
    let h = match hello(paths, &cfg, &unit.instance, &self_exe) {
        Ok(h) => h,
        Err(e) => {
            log(&format!("hello facts: {e}"));
            return Exit::Unavailable;
        }
    };
    // Nothing may arrive before `hello` (P-LAUNCHER-ADMIT "request before hello").
    if readable_now(&conn) {
        log("bytes before hello: closed");
        return Exit::Ok;
    }
    let now = Instant::now();
    if send_control(&mut conn, &h.to_value(), now + cfg.deadlines.request).is_err() {
        return Exit::Ok;
    }
    let payload = match read_request(&conn, Instant::now() + cfg.deadlines.request) {
        Ok(p) => p,
        Err(e) => {
            log(&format!("no request: {e}"));
            return Exit::Ok;
        }
    };
    let req = match protocol::parse_request(&payload, &cfg.request, &cfg.maxima, &cfg.profile_ids())
    {
        Ok(r) => r,
        Err(code) => return refuse(&mut conn, "", code),
    };
    let digests = ResultDigests {
        bwrap: h.bwrap_digest.clone(),
        bwrap_profile: h.bwrap_profile_digest.clone(),
        apparmor_profile: h.apparmor_profile_digest.clone(),
        runtime_profile: h
            .runtime_profile(&req.runtime_profile)
            .unwrap_or_default()
            .to_string(),
        exec: h.exec_digest.clone(),
    };
    let job_dir_name = format!("job-{}", unit.instance);
    let mut job = Job {
        paths,
        cfg: &cfg,
        unit,
        req,
        digests,
        job_dir: None,
        job_dir_name,
    };
    job.launch(&mut conn, slots)
}

impl Job<'_> {
    fn view(&self) -> HostProc {
        HostProc {
            root: self.paths.proc_root.clone(),
        }
    }

    fn unit_dir_name(&self) -> String {
        self.unit
            .rel
            .rsplit('/')
            .next()
            .unwrap_or_default()
            .to_string()
    }

    fn remove_partial(&mut self) {
        let Some(jd) = self.job_dir.take() else {
            return;
        };
        if let Ok(work) = openat2(
            &jd,
            "work",
            OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
            Mode::empty(),
            ResolveFlags::BENEATH | ResolveFlags::NO_SYMLINKS,
        ) {
            let _ = crate::helper::remove_contents(work, self.cfg.source.max_entries + 1);
            let _ = unlinkat(&jd, "work", AtFlags::REMOVEDIR);
        }
        drop(jd);
        if let Ok(root) = File::open(&self.paths.jobs_root) {
            let _ = unlinkat(&root, self.job_dir_name.as_str(), AtFlags::REMOVEDIR);
        }
    }

    /// Steps 1 and 2: readiness, residue, job directory, copy, chown, drop `CAP_CHOWN`.
    fn prepare(&mut self) -> Result<(), LaunchCode> {
        let profile = self
            .cfg
            .profile(&self.req.runtime_profile)
            .ok_or(LaunchCode::ProfileUnknown)?;
        match bwrap::manifest(&self.paths.runtime_root.join(&profile.id), 0) {
            Ok((_, d)) if d == profile.digest => {}
            Ok(_) => {
                log("runtime profile manifest differs from the pinned digest");
                return Err(LaunchCode::Unavailable);
            }
            Err(e) => {
                log(&format!("runtime profile: {e}"));
                return Err(LaunchCode::Unavailable);
            }
        }
        let slice_dir = self.paths.cgroup_root.join(&self.paths.slice);
        let findings = residue::check_all(
            &self.view(),
            self.cfg.job_uid,
            &slice_dir,
            &self.unit_dir_name(),
            &self.paths.jobs_root,
        );
        if !findings.is_empty() {
            log(&format!("residue: {findings:?}"));
            return Err(LaunchCode::Unavailable);
        }
        let root = File::open(&self.paths.jobs_root).map_err(|_| LaunchCode::Unavailable)?;
        mkdirat(
            &root,
            self.job_dir_name.as_str(),
            Mode::from_raw_mode(0o711),
        )
        .map_err(|_| LaunchCode::Unavailable)?;
        let jd = openat2(
            &root,
            self.job_dir_name.as_str(),
            OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
            Mode::empty(),
            ResolveFlags::BENEATH | ResolveFlags::NO_SYMLINKS,
        )
        .map_err(|_| LaunchCode::Unavailable)?;
        rustix::fs::fchmod(&jd, Mode::from_raw_mode(0o711)).map_err(|_| LaunchCode::Unavailable)?;
        let mut owner = Deferred::new();
        let rules = source::Rules {
            owner_uid: self.cfg.writ_uid,
            max_depth: self.cfg.source.max_depth,
            max_entries: self.cfg.source.max_entries,
            max_bytes: self.req.limits.input_max_bytes,
        };
        let copied = source::copy(
            &self.paths.source_root,
            &self.req.job_id,
            jd.as_fd(),
            &self.req.program,
            &rules,
            &mut owner,
        );
        self.job_dir = Some(jd);
        match copied {
            Ok(_) => {}
            Err(source::CopyError::Source(why)) => {
                log(&format!("source: {why}"));
                self.remove_partial();
                return Err(LaunchCode::SourceInvalid);
            }
            Err(source::CopyError::Destination(why)) => {
                log(&format!("destination: {why}"));
                self.remove_partial();
                return Err(LaunchCode::Unavailable);
            }
        }
        if owner.apply(self.cfg.job_uid, self.cfg.job_gid).is_err() {
            // Partly chowned entries can no longer be removed by root: residue.
            log("chown of the copy failed");
            return Err(LaunchCode::Unavailable);
        }
        drop_chown().map_err(|_| LaunchCode::Unavailable)?;
        Ok(())
    }

    fn rlimits(&self) -> Rlimits {
        Rlimits {
            nofile: self.req.limits.nofile_max,
            fsize: self.req.limits.file_size_max_bytes,
            nproc: self.cfg.rlimits.nproc,
            msgqueue: self.cfg.rlimits.msgqueue,
            memlock: self.cfg.rlimits.memlock,
            sigpending: self.req.limits.sigpending_max,
        }
    }

    fn work_path(&self) -> std::path::PathBuf {
        self.paths.jobs_root.join(&self.job_dir_name).join("work")
    }

    fn image(&self, p: &std::path::Path) -> io::Result<(u64, u64)> {
        let m = std::fs::metadata(p)?;
        Ok((m.dev(), m.ino()))
    }

    fn launch(&mut self, conn: &mut UnixStream, mut slots: Slots) -> Exit {
        let job_id = self.req.job_id.clone();
        if let Err(code) = self.prepare() {
            return refuse(conn, &job_id, code);
        }
        let cg = match Cgroups::open(&self.paths.cgroup_root, &self.unit) {
            Ok(c) => c,
            Err(e) => {
                log(&format!("cgroup: {e}"));
                return refuse(conn, &job_id, LaunchCode::Unavailable);
            }
        };
        if let Err(e) = cg
            .enter_supervisor(std::process::id())
            .and_then(|()| cg.create_job(&self.req.limits))
        {
            log(&format!("cgroup setup: {e}"));
            return refuse(conn, &job_id, LaunchCode::Unavailable);
        }
        let (top, runtime) = match self.cfg.profile(&self.req.runtime_profile) {
            Some(p) => {
                let root = self.paths.runtime_root.join(&p.id);
                match bwrap::manifest(&root, 0) {
                    Ok((top, _)) => (top, root),
                    Err(_) => return refuse(conn, &job_id, LaunchCode::Unavailable),
                }
            }
            None => return refuse(conn, &job_id, LaunchCode::ProfileUnknown),
        };
        let bwrap_image = match self.image(&self.paths.bwrap) {
            Ok(i) => i,
            Err(_) => return refuse(conn, &job_id, LaunchCode::Unavailable),
        };
        let pipes = match fdplan::pipes() {
            Ok(p) => p,
            Err(_) => return refuse(conn, &job_id, LaunchCode::Unavailable),
        };
        let plan = Plan {
            cgroup_root: self.paths.cgroup_root.clone(),
            unit: self.unit.rel.clone(),
            uid: self.cfg.job_uid,
            gid: self.cfg.job_gid,
            rlimits: self.rlimits(),
            mode: HelperMode::Run {
                bwrap: self.paths.bwrap.clone(),
                args: bwrap::args(&runtime, &top, &self.work_path(), &self.req),
            },
        };
        let mut gate = Gate::new(pipes.gate_write);
        let mut cmd = Command::new(&self.paths.exec);
        cmd.args(plan.to_args())
            .env_clear()
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        #[cfg(feature = "test-faults")]
        if let Ok(v) = std::env::var("WRIT_TEST_FAULT") {
            cmd.env("WRIT_TEST_FAULT", v);
        }
        // Step 4: dup2 onto 3 and 4 immediately before the spawn (single-threaded).
        if slots
            .install(&pipes.gate_read, &pipes.status_write)
            .is_err()
        {
            return refuse(conn, &job_id, LaunchCode::Unavailable);
        }
        let spawned = cmd.spawn();
        // Close 3, 4 and the originals of those ends right after the spawn returns (this
        // instance serves one connection; later spawns use only 0..2).
        drop(slots);
        drop(pipes.gate_read);
        drop(pipes.status_write);
        let mut child = match spawned {
            Ok(c) => c,
            Err(e) => {
                // Nothing was spawned: still before the helper, a refusal is definitive.
                log(&format!("spawn: {e}"));
                gate_close_unspawned(gate, &cg, self.cfg.deadlines.kill);
                return refuse(conn, &job_id, LaunchCode::Unavailable);
            }
        };
        // From here on: no refused frame (H1).
        let (Some(out), Some(err)) = (child.stdout.take(), child.stderr.take()) else {
            return self.after_spawn_failure(conn, &cg, gate, Some(child), "pipes");
        };
        let pidfd = match i32::try_from(child.id())
            .ok()
            .and_then(rustix::process::Pid::from_raw)
            .map(|p| rustix::process::pidfd_open(p, rustix::process::PidfdFlags::empty()))
        {
            Some(Ok(fd)) => fd,
            _ => return self.after_spawn_failure(conn, &cg, gate, Some(child), "pidfd"),
        };
        let conn_clone = match conn.try_clone() {
            Ok(c) => c,
            Err(_) => return self.after_spawn_failure(conn, &cg, gate, Some(child), "conn"),
        };
        let channels = Channels {
            conn: conn_clone,
            stdout: Some(File::from(OwnedFd::from(out))),
            stderr: Some(File::from(OwnedFd::from(err))),
            status: Some(File::from(pipes.status_read)),
            child: pidfd,
        };
        let bounds = Bounds {
            output_max: self.req.limits.output_max_bytes,
            outbound_max: self.cfg.outbound_buffer_bytes,
            status_max: 4096,
            status_drain: self.cfg.status_drain_bytes,
        };
        let mut s = match Session::new(channels, bounds, &job_id) {
            Ok(s) => s,
            Err(_) => return self.after_spawn_failure(conn, &cg, gate, Some(child), "session"),
        };
        let start = s.await_start(Instant::now() + self.cfg.deadlines.start);
        let mut conn_lost = false;
        let reason = match start {
            Start::ChildPid(pid) => {
                let expect = Expect {
                    uid: self.cfg.job_uid,
                    gid: self.cfg.job_gid,
                    image: bwrap_image,
                    count: self.cfg.pre_gate_processes,
                };
                let direct = i32::try_from(child.id()).unwrap_or(-1);
                let verified = cg.job_procs().map_err(|e| e.to_string()).and_then(|procs| {
                    identity::verify_job_set(&self.view(), &procs, direct, Some(pid), &expect)
                });
                if verified.is_ok() {
                    // The trial records this line as P-JOB-IDENTITY `pre_verified=1`.
                    log(&format!(
                        "pre-gate verified: job {job_id} direct {direct} sandbox child {pid}"
                    ));
                }
                match verified {
                    Err(why) => {
                        log(&format!("verification: {why}"));
                        Some(Reason::StartFailed)
                    }
                    Ok(()) => match gate.release() {
                        Err(_) => Some(Reason::StartFailed),
                        Ok(()) => {
                            s.send_control(&protocol::started(&job_id));
                            s.mark_started();
                            match s.run(
                                Instant::now()
                                    + Duration::from_millis(self.req.limits.wall_time_ms),
                            ) {
                                session::Run::ChildExited => None,
                                session::Run::Kill(r) => Some(r),
                                session::Run::ConnLost => {
                                    conn_lost = true;
                                    Some(Reason::Cancelled)
                                }
                            }
                        }
                    },
                }
            }
            Start::Failed(why) => {
                log(&format!("start: {why}"));
                Some(Reason::StartFailed)
            }
            Start::Cancelled => Some(Reason::Cancelled),
            Start::ConnLost => {
                conn_lost = true;
                Some(Reason::Cancelled)
            }
        };
        #[cfg(feature = "test-faults")]
        if reason == Some(Reason::StartFailed) && crate::helper::fault("gate-eof") {
            // P-JOB-IDENTITY control (2): an error path that closes the gate without killing.
            gate.close_unverified_for_fault();
            std::thread::sleep(Duration::from_millis(500));
            return Exit::Ok;
        }
        // Always: kill, verify populated 0, and only then close the gate writer.
        let proof = cg.kill_and_verify(self.cfg.deadlines.kill);
        let status = child.wait().ok();
        let counters = cg.counters();
        s.drain_output();
        let (reason, exit_status, signal) = match (reason, status) {
            (Some(r), st) => (
                if s.output_exceeded() {
                    Reason::OutputExceeded
                } else {
                    r
                },
                st.and_then(|x| x.code()).map(i64::from),
                st.and_then(|x| std::os::unix::process::ExitStatusExt::signal(&x))
                    .map(i64::from),
            ),
            (None, Some(st)) => match (
                st.code(),
                std::os::unix::process::ExitStatusExt::signal(&st),
            ) {
                (Some(c), _) => (Reason::Exited, Some(i64::from(c)), None),
                (None, sig) => (Reason::Signaled, None, sig.map(i64::from)),
            },
            (None, None) => (Reason::StartFailed, None, None),
        };
        // P-FD negative control (test-only, never installed): a build that trusts the status
        // pipe after `started` reports what a writer of the monitor's descriptor 4 claimed.
        #[cfg(feature = "test-faults")]
        let (reason, exit_status) = match s.trusted_exit() {
            Some(x) if crate::helper::fault("trust-status-after-started") => {
                (Reason::Exited, Some(x))
            }
            _ => (reason, exit_status),
        };
        let request_digest = self.req.digest.clone();
        let result_digests = self.digests.clone();
        let fin = |populated_zero: bool, s: &Session| Finished {
            job_id: job_id.clone(),
            request_digest: request_digest.clone(),
            reason,
            exit_status,
            signal,
            kill_used: true,
            populated_zero,
            memory_events: counters.memory_events,
            pids_max_events: counters.pids_max,
            cpu_usage_usec: counters.cpu_usage_usec,
            output_bytes: s.output_bytes(),
            digests: result_digests.clone(),
        };
        let proof = match proof {
            Ok(p) => p,
            Err(e) => {
                // J-11.5: gate stays open (leaked by Gate's Drop), nothing cleaned, exit non-zero.
                log(&format!("kill not verified: {}", e.0));
                if !conn_lost {
                    s.send_control(&fin(false, &s).to_value());
                    s.flush(Instant::now() + Duration::from_secs(2));
                }
                return Exit::NotEmpty;
            }
        };
        gate.close_after(&proof);
        let cleaned = self.cleanup(&cg, &proof);
        if !conn_lost {
            s.send_control(&fin(true, &s).to_value());
            s.flush(Instant::now() + Duration::from_secs(2));
        }
        if cleaned {
            Exit::Ok
        } else {
            Exit::Residue
        }
    }

    /// A failure after the helper was spawned (or might have been): kill with the gate closed,
    /// verify, then report `start_failed` (never `refused`).
    fn after_spawn_failure(
        &mut self,
        conn: &mut UnixStream,
        cg: &Cgroups,
        gate: Gate,
        child: Option<Child>,
        why: &str,
    ) -> Exit {
        log(&format!("after spawn: {why}"));
        let proof = cg.kill_and_verify(self.cfg.deadlines.kill);
        if let Some(mut c) = child {
            let _ = c.wait();
        }
        let populated_zero = proof.is_ok();
        let fin = Finished {
            job_id: self.req.job_id.clone(),
            request_digest: self.req.digest.clone(),
            reason: Reason::StartFailed,
            exit_status: None,
            signal: None,
            kill_used: true,
            populated_zero,
            memory_events: [0; 3],
            pids_max_events: 0,
            cpu_usage_usec: 0,
            output_bytes: [0, 0],
            digests: self.digests.clone(),
        };
        let _ = send_control(
            conn,
            &fin.to_value(),
            Instant::now() + Duration::from_secs(2),
        );
        match proof {
            Ok(p) => {
                gate.close_after(&p);
                if self.cleanup(cg, &p) {
                    Exit::Ok
                } else {
                    Exit::Residue
                }
            }
            Err(_) => Exit::NotEmpty,
        }
    }

    /// Cleanup as `writ-job` (J-11.5, N8): the helper joins `<unit>/job`, sets `no_new_privs`
    /// and executes itself as `writ-job`; the launcher verifies that process (ids, groups,
    /// `NoNewPrivs`, the pinned `writ-cgroup-exec` image) before sending the go byte; then kill
    /// and verify again, remove `<unit>/job`, `work/` and `job-<instance>/`. Any failure leaves
    /// `job-<instance>/` as residue.
    fn cleanup(&mut self, cg: &Cgroups, _after: &KillProof) -> bool {
        let deadline = Instant::now() + self.cfg.deadlines.cleanup;
        let ok = self.cleanup_as_job(cg, deadline);
        let proof = match cg.kill_and_verify(self.cfg.deadlines.kill) {
            Ok(p) => p,
            Err(_) => return false,
        };
        if !ok {
            return false;
        }
        if cg.remove_job(&proof).is_err() {
            return false;
        }
        let Some(jd) = self.job_dir.take() else {
            return false;
        };
        if unlinkat(&jd, "work", AtFlags::REMOVEDIR).is_err() {
            return false;
        }
        drop(jd);
        match File::open(&self.paths.jobs_root) {
            Ok(root) => unlinkat(&root, self.job_dir_name.as_str(), AtFlags::REMOVEDIR).is_ok(),
            Err(_) => false,
        }
    }

    fn cleanup_as_job(&self, cg: &Cgroups, deadline: Instant) -> bool {
        let plan = Plan {
            cgroup_root: self.paths.cgroup_root.clone(),
            unit: self.unit.rel.clone(),
            uid: self.cfg.job_uid,
            gid: self.cfg.job_gid,
            rlimits: self.rlimits(),
            mode: HelperMode::Cleanup {
                exe: self.paths.exec.clone(),
                work: self.work_path(),
            },
        };
        let Ok(image) = self.image(&self.paths.exec) else {
            return false;
        };
        let child = Command::new(&self.paths.exec)
            .args(plan.to_args())
            .env_clear()
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn();
        let Ok(mut child) = child else {
            return false;
        };
        let ok = (|| -> Option<()> {
            let mut go = child.stdin.take()?;
            let mut ready = child.stdout.take()?;
            // Wait for "ready" (bounded by the deadline through the read timeout of a poll).
            let fd = ready.as_fd().try_clone_to_owned().ok()?;
            let mut fds = [PollFd::new(&fd, PollFlags::IN)];
            poll(&mut fds, Some(&timespec(deadline))).ok()?;
            let mut buf = [0u8; 6];
            ready.read_exact(&mut buf).ok()?;
            if &buf != b"ready\n" {
                return None;
            }
            let procs = cg.job_procs().ok()?;
            let direct = i32::try_from(child.id()).ok()?;
            let expect = Expect {
                uid: self.cfg.job_uid,
                gid: self.cfg.job_gid,
                image,
                count: 1,
            };
            identity::verify_job_set(&self.view(), &procs, direct, None, &expect).ok()?;
            go.write_all(&[1]).ok()?;
            drop(go);
            // Wait for the exit (bounded).
            loop {
                if let Ok(Some(st)) = child.try_wait() {
                    return st.success().then_some(());
                }
                if Instant::now() >= deadline {
                    return None;
                }
                std::thread::sleep(Duration::from_millis(10));
            }
        })();
        if ok.is_none() {
            let _ = child.kill();
        }
        let _ = child.wait();
        ok.is_some()
    }
}

/// The spawn itself failed (no helper exists): kill and verify anyway before the gate closes.
fn gate_close_unspawned(gate: Gate, cg: &Cgroups, deadline: Duration) {
    if let Ok(p) = cg.kill_and_verify(deadline) {
        gate.close_after(&p);
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;
    use crate::testutil::Scratch;
    use std::os::unix::fs::PermissionsExt;

    #[test]
    fn deferred_chown_records_and_applies_to_own_ids() {
        let s = Scratch::new("deferred");
        let f = File::create(s.path().join("x")).unwrap();
        let mut d = Deferred::new();
        d.own(f.as_fd()).unwrap();
        assert_eq!(d.len(), 1);
        // Chown to our own ids is permitted unprivileged.
        let uid = rustix::process::getuid().as_raw();
        let gid = rustix::process::getgid().as_raw();
        d.apply(uid, gid).unwrap();
    }

    #[test]
    fn dropping_chown_succeeds_without_privilege() {
        // Unprivileged: CAP_CHOWN is absent already; removing it is a no-op that must succeed.
        drop_chown().unwrap();
    }

    #[test]
    fn request_reading_is_bounded_and_control_only() {
        let (a, mut b) = UnixStream::pair().unwrap();
        a.set_nonblocking(true).unwrap();
        b.write_all(&frame::encode(frame::CONTROL, b"{}").unwrap())
            .unwrap();
        assert_eq!(
            read_request(&a, Instant::now() + Duration::from_secs(2)).unwrap(),
            b"{}"
        );
        b.write_all(&frame::encode(frame::DATA, b"x").unwrap())
            .unwrap();
        assert!(read_request(&a, Instant::now() + Duration::from_secs(2)).is_err());
        let (a, _b) = UnixStream::pair().unwrap();
        a.set_nonblocking(true).unwrap();
        let t = Instant::now();
        assert_eq!(
            read_request(&a, Instant::now() + Duration::from_millis(100))
                .unwrap_err()
                .kind(),
            ErrorKind::TimedOut
        );
        assert!(t.elapsed() < Duration::from_secs(2));
        let (a, b) = UnixStream::pair().unwrap();
        drop(b);
        assert!(read_request(&a, Instant::now() + Duration::from_secs(2)).is_err());
    }

    #[test]
    fn bytes_before_hello_are_detected() {
        let (a, mut b) = UnixStream::pair().unwrap();
        assert!(!readable_now(&a));
        b.write_all(b"x").unwrap();
        assert!(readable_now(&a));
        let (a, b) = UnixStream::pair().unwrap();
        drop(b);
        assert!(readable_now(&a), "EOF before hello counts");
    }

    #[test]
    fn hello_reports_file_digests_and_configured_profiles() {
        let s = Scratch::new("hello");
        let mut paths = Paths::reference();
        for (name, field) in [("exec", 0), ("bwrap", 1), ("aa", 2), ("self", 3)] {
            std::fs::write(s.path().join(name), name.as_bytes()).unwrap();
            std::fs::set_permissions(s.path().join(name), std::fs::Permissions::from_mode(0o644))
                .unwrap();
            match field {
                0 => paths.exec = s.path().join(name),
                1 => paths.bwrap = s.path().join(name),
                2 => paths.apparmor_profile = s.path().join(name),
                _ => {}
            }
        }
        let cfg = Config::parse(
            json::canonical(&crate::config::tests::sample())
                .unwrap()
                .as_bytes(),
        )
        .unwrap();
        let h = hello(&paths, &cfg, "4", &s.path().join("self")).unwrap();
        assert_eq!(h.exec_digest, Digest::of_bytes(b"exec").as_str());
        assert_eq!(h.bwrap_digest, Digest::of_bytes(b"bwrap").as_str());
        assert_eq!(h.launcher_digest, Digest::of_bytes(b"self").as_str());
        assert_eq!(h.runtime_profiles.len(), 1);
        assert_eq!(h.maxima_digest, cfg.maxima_digest());
        assert_eq!(h.instance, "4");
        paths.bwrap = s.path().join("absent");
        assert!(hello(&paths, &cfg, "4", &s.path().join("self")).is_err());
    }
}
