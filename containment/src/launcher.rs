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
use crate::cgroup::{unit_from_proc_cgroup, Cgroups, KillProof, Terminated, UnitPath};
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
use crate::source;
use rustix::event::{poll, PollFd, PollFlags, Timespec};
use rustix::fs::{mkdirat, openat2, unlinkat, AtFlags, Mode, OFlags, ResolveFlags};
use std::fs::File;
use std::io::{self, ErrorKind, Read, Write};
use std::os::fd::{AsFd, OwnedFd};
use std::os::unix::fs::MetadataExt;
use std::os::unix::net::UnixStream;
use std::process::{Child, Command, ExitStatus, Stdio};
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
    let aa = std::fs::read(&paths.apparmor_profile)?;
    apparmor_includes_ok(&String::from_utf8_lossy(&aa)).map_err(io::Error::other)?;
    Ok(Hello {
        launcher_digest: file_digest(self_exe)?,
        exec_digest: file_digest(&paths.exec)?,
        bwrap_digest: file_digest(&paths.bwrap)?,
        bwrap_profile_digest: bwrap::profile_digest(),
        apparmor_profile_digest: Digest::of_bytes(&aa).as_str().to_string(),
        runtime_profiles: cfg
            .runtime_profiles
            .iter()
            .map(|p| (p.id.clone(), p.digest.clone()))
            .collect(),
        maxima_digest: cfg.maxima_digest(),
        instance: instance.to_string(),
        build: protocol::build_mode().to_string(),
    })
}

/// The AppArmor profile file must be self-contained (M7): its digest is what `hello` pins, so
/// no `include` (nor the legacy `#include`) may pull in another file, except the system
/// `<tunables/global>` the profile syntax needs. In particular `local/writ-bwrap` and
/// `local/writ-job-child` overrides are refused.
pub fn apparmor_includes_ok(text: &str) -> Result<(), String> {
    for line in text.lines() {
        let t = line.trim();
        // `#include` (no space) is the legacy include directive, not a comment.
        let directive = t.strip_prefix('#').unwrap_or(t);
        if !directive.starts_with("include") {
            continue;
        }
        let rest = directive["include".len()..].trim();
        if rest != "<tunables/global>" {
            return Err(format!("AppArmor profile includes another file: {t}"));
        }
    }
    Ok(())
}

/// The bounding set of the unit (J-11.2, M5): `CAP_CHOWN` (0), `CAP_SETGID` (6),
/// `CAP_SETUID` (7).
pub const BOUNDING_SET: u64 = (1 << 0) | (1 << 6) | (1 << 7);

/// Descriptors the launcher needs besides the source walk (connection, pipes, cgroup and job
/// directories, reads); the walk needs `2 * (max_depth + 1) + 2` more (M6).
pub const BASE_DESCRIPTORS: u64 = 64;

/// What the launcher observes about itself before serving (L7).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SelfFacts {
    /// `/proc/self/status` text.
    pub status: String,
    /// Group of the source root (`/var/lib/writ/run`, read by group; N9).
    pub source_gid: u32,
    /// The unit's actual `memory.max` (`None`: `max` or unreadable).
    pub unit_memory_max: Option<u64>,
    /// `RLIMIT_NOFILE` soft limit (`None`: unlimited).
    pub nofile: Option<u64>,
    /// `WRIT_TEST_FAULT` is set in the environment.
    pub test_fault_env: bool,
}

fn status_hex(status: &str, key: &str) -> Option<u64> {
    let v = status
        .lines()
        .find_map(|l| l.strip_prefix(key).and_then(|r| r.strip_prefix(':')))?;
    u64::from_str_radix(v.trim(), 16).ok()
}

/// The launcher's self-checks (L7): no test fault requested of a release build; `NoNewPrivs`;
/// the bounding set exactly [`BOUNDING_SET`] and no ambient capability; supplementary groups
/// exactly the source root's group (root's own group 0 tolerated; verify in VM what systemd
/// 255 sets for `User=root` with `SupplementaryGroups=writ`); the unit's actual `MemoryMax`
/// equal to the configured one (the budget was checked against it at load, L5); and a
/// descriptor limit that fits the configured walk depth (a launcher-level fault, M6).
pub fn self_check(f: &SelfFacts, cfg: &Config, test_faults: bool) -> Result<(), String> {
    if f.test_fault_env && !test_faults {
        return Err("WRIT_TEST_FAULT set for a build without test-faults".into());
    }
    let st = crate::procfs::parse_status(&f.status).ok_or("status unreadable")?;
    if !st.no_new_privs {
        return Err("NoNewPrivs is not set".into());
    }
    match status_hex(&f.status, "CapBnd") {
        Some(b) if b == BOUNDING_SET => {}
        other => {
            return Err(format!(
                "bounding set {other:x?}, expected {BOUNDING_SET:x}"
            ))
        }
    }
    match status_hex(&f.status, "CapAmb") {
        Some(0) => {}
        other => return Err(format!("ambient set {other:x?}")),
    }
    let mut groups = st.groups.clone();
    groups.retain(|g| *g != 0);
    if groups != [f.source_gid] || f.source_gid == 0 || f.source_gid == cfg.job_gid {
        return Err(format!(
            "supplementary groups {:?}, expected only {}",
            st.groups, f.source_gid
        ));
    }
    if f.unit_memory_max != Some(cfg.unit_memory_max_bytes) {
        return Err(format!(
            "unit memory.max {:?} differs from the configured {}",
            f.unit_memory_max, cfg.unit_memory_max_bytes
        ));
    }
    let need = 2 * (cfg.source.max_depth as u64 + 1) + 2 + BASE_DESCRIPTORS;
    if f.nofile.is_some_and(|n| n < need) {
        return Err(format!(
            "RLIMIT_NOFILE {:?} below {need} for max_depth {}",
            f.nofile, cfg.source.max_depth
        ));
    }
    Ok(())
}

fn observe_self(paths: &Paths, cg: &Cgroups) -> io::Result<SelfFacts> {
    Ok(SelfFacts {
        status: std::fs::read_to_string(paths.proc_root.join("self/status"))?,
        source_gid: std::fs::metadata(&paths.source_root)?.gid(),
        unit_memory_max: cg.unit_memory_max(),
        nofile: rustix::process::getrlimit(rustix::process::Resource::Nofile).current,
        test_fault_env: std::env::var_os("WRIT_TEST_FAULT").is_some(),
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
    if !crate::TEST_FAULTS && std::env::var_os("WRIT_TEST_FAULT").is_some() {
        log("WRIT_TEST_FAULT set for a build without test-faults");
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
    let checked = Cgroups::open(&paths.cgroup_root, &unit)
        .and_then(|cg| observe_self(paths, &cg))
        .map_err(|e| e.to_string())
        .and_then(|f| self_check(&f, &cfg, crate::TEST_FAULTS));
    if let Err(e) = checked {
        // No hello: writ sees EOF before hello (containment unavailable).
        log(&format!("self-check: {e}"));
        return Exit::Unavailable;
    }
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
        );
        match copied {
            Ok(_) => {}
            Err(source::CopyError::Source(why)) => {
                log(&format!("source: {why}"));
                self.job_dir = Some(jd);
                self.remove_partial();
                return Err(LaunchCode::SourceInvalid);
            }
            Err(source::CopyError::Descriptors(why)) => {
                // Request-level (M6): the launcher's own bound was checked at start.
                log(&format!("descriptors: {why}"));
                self.job_dir = Some(jd);
                self.remove_partial();
                return Err(LaunchCode::SourceInvalid);
            }
            Err(source::CopyError::Destination(why)) => {
                log(&format!("destination: {why}"));
                self.job_dir = Some(jd);
                self.remove_partial();
                return Err(LaunchCode::Unavailable);
            }
        }
        let chowned = source::chown_tree(
            jd.as_fd(),
            self.cfg.job_uid,
            self.cfg.job_gid,
            self.cfg.source.max_entries,
        );
        self.job_dir = Some(jd);
        if let Err(e) = chowned {
            // Partly given away (children first): root can no longer remove those entries
            // without CAP_DAC_OVERRIDE, and no cgroup exists yet for a cleanup as writ-job:
            // `job-<instance>/` is residue (the next launch refuses until the operator clears
            // it; L3).
            log(&format!("chown of the copy failed: {e}"));
            return Err(LaunchCode::Unavailable);
        }
        // A failure here leaves the copy, now owned by writ-job, as residue (L3; no cgroup yet).
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
        // From here on the job directory holds a copy owned by writ-job: every refusal goes
        // through `refuse_prepared`, which cleans it up as writ-job when it can (L3).
        let cg = match Cgroups::open(&self.paths.cgroup_root, &self.unit) {
            Ok(c) => c,
            Err(e) => {
                log(&format!("cgroup: {e}"));
                return self.refuse_prepared(conn, &job_id, LaunchCode::Unavailable, None);
            }
        };
        if let Err(e) = cg
            .enter_supervisor(std::process::id())
            .and_then(|()| cg.create_job(&self.req.limits))
        {
            log(&format!("cgroup setup: {e}"));
            return self.refuse_prepared(conn, &job_id, LaunchCode::Unavailable, Some(&cg));
        }
        let (top, runtime) = match self.cfg.profile(&self.req.runtime_profile) {
            Some(p) => {
                let root = self.paths.runtime_root.join(&p.id);
                match bwrap::manifest(&root, 0) {
                    Ok((top, _)) => (top, root),
                    Err(_) => {
                        return self.refuse_prepared(
                            conn,
                            &job_id,
                            LaunchCode::Unavailable,
                            Some(&cg),
                        )
                    }
                }
            }
            None => {
                return self.refuse_prepared(conn, &job_id, LaunchCode::ProfileUnknown, Some(&cg))
            }
        };
        let bwrap_image = match self.image(&self.paths.bwrap) {
            Ok(i) => i,
            Err(_) => {
                return self.refuse_prepared(conn, &job_id, LaunchCode::Unavailable, Some(&cg))
            }
        };
        let pipes = match fdplan::pipes() {
            Ok(p) => p,
            Err(_) => {
                return self.refuse_prepared(conn, &job_id, LaunchCode::Unavailable, Some(&cg))
            }
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
            // No helper exists; the gate writer is leaked by Gate's Drop (nothing reads it).
            return self.refuse_prepared(conn, &job_id, LaunchCode::Unavailable, Some(&cg));
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
                return self.refuse_prepared(conn, &job_id, LaunchCode::Unavailable, Some(&cg));
            }
        };
        // From here on: no refused frame (H1).
        let pidfd = i32::try_from(child.id())
            .ok()
            .and_then(rustix::process::Pid::from_raw)
            .and_then(|p| {
                rustix::process::pidfd_open(p, rustix::process::PidfdFlags::empty()).ok()
            });
        let Some(pidfd) = pidfd else {
            return self.after_spawn_failure(conn, &cg, gate, child, None, "pidfd");
        };
        let Ok(kill_fd) = pidfd.try_clone() else {
            return self.after_spawn_failure(conn, &cg, gate, child, Some(pidfd), "pidfd");
        };
        let (Some(out), Some(err)) = (child.stdout.take(), child.stderr.take()) else {
            return self.after_spawn_failure(conn, &cg, gate, child, Some(kill_fd), "pipes");
        };
        let conn_clone = match conn.try_clone() {
            Ok(c) => c,
            Err(_) => {
                return self.after_spawn_failure(conn, &cg, gate, child, Some(kill_fd), "conn")
            }
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
            Err(_) => {
                return self.after_spawn_failure(conn, &cg, gate, child, Some(kill_fd), "session")
            }
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
        // Always: kill the direct child and the job, verify (bounded, H1, M1), and only then
        // close the gate writer.
        let Terminated { proof, status } = cg.terminate(
            &mut child,
            Some(kill_fd.as_fd()),
            std::process::id(),
            self.cfg.deadlines.kill,
        );
        let counters = cg.counters();
        s.drain_output();
        let (reason, exit_status, signal) = outcome(reason, status, s.output_exceeded());
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
                // J-11.5: nothing cleaned, finished with populated_zero false, exit non-zero.
                log(&format!("kill not verified: {}", e.0));
                if !conn_lost {
                    s.send_control(&fin(false, &s).to_value());
                    s.flush(Instant::now() + Duration::from_secs(2));
                }
                return self.not_empty(&cg, gate);
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

    /// A refusal after [`Job::prepare`] (no helper spawned, so the refusal is definitive): send
    /// `refused`, then remove the writ-job-owned copy through the cleanup as writ-job (L3).
    /// That needs `<unit>/job` (the cleanup process joins it) and a kill proof for it; without
    /// a usable `job` cgroup (the unit directory could not be opened, or `job` was never
    /// created) the copy stays as residue and the next launch refuses `LAUNCH_UNAVAILABLE`
    /// until the operator clears `/var/lib/writ-jobs/job-<instance>/`.
    fn refuse_prepared(
        &mut self,
        conn: &mut UnixStream,
        job_id: &str,
        code: LaunchCode,
        cg: Option<&Cgroups>,
    ) -> Exit {
        refuse(conn, job_id, code);
        let Some(cg) = cg.filter(|c| c.job_exists()) else {
            log("refused after prepare without a job cgroup: job directory left as residue");
            return Exit::Residue;
        };
        match cg.kill_and_verify(self.cfg.deadlines.kill) {
            Ok(p) if self.cleanup(cg, &p) => Exit::Ok,
            Ok(_) => Exit::Residue,
            Err(e) => {
                log(&format!(
                    "refused after prepare: kill not verified: {}",
                    e.0
                ));
                Exit::Residue
            }
        }
    }

    /// The kill could not be verified (J-11.5, L5). The gate writer is never closed by this
    /// code, but exiting closes it; so the job cgroup is frozen first (bounded), which keeps a
    /// still-gated bubblewrap from acting on that EOF until systemd's `SIGKILL` of the
    /// instance cgroup (`KillMode=control-group`) after this exit. Nothing is cleaned.
    fn not_empty(&self, cg: &Cgroups, gate: Gate) -> Exit {
        if let Err(e) = cg.freeze_job(self.cfg.deadlines.kill) {
            log(&format!("freeze after an unverified kill: {}", e.0));
        }
        // Gate's Drop leaks the writer; process exit closes it.
        drop(gate);
        Exit::NotEmpty
    }

    /// A failure after the helper was spawned (or might have been): kill with the gate closed,
    /// verify, then report `start_failed` (never `refused`).
    fn after_spawn_failure(
        &mut self,
        conn: &mut UnixStream,
        cg: &Cgroups,
        gate: Gate,
        mut child: Child,
        pidfd: Option<OwnedFd>,
        why: &str,
    ) -> Exit {
        log(&format!("after spawn: {why}"));
        let Terminated { proof, .. } = cg.terminate(
            &mut child,
            pidfd.as_ref().map(AsFd::as_fd),
            std::process::id(),
            self.cfg.deadlines.kill,
        );
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
            Err(e) => {
                log(&format!("kill not verified: {}", e.0));
                self.not_empty(cg, gate)
            }
        }
    }

    /// Cleanup as `writ-job` (J-11.5, N8): the helper joins `<unit>/job`, sets `no_new_privs`
    /// and executes itself as `writ-job`; the launcher verifies that process (ids, groups,
    /// `NoNewPrivs`, the pinned `writ-cgroup-exec` image) before sending the go byte; then kill
    /// and verify again, remove `<unit>/job`, `work/` and `job-<instance>/`. Any failure leaves
    /// `job-<instance>/` as residue. The cleanup runs under fixed launcher-owned limits, never
    /// the job's (L4), and one deadline covers it from the spawn to the exit.
    fn cleanup(&mut self, cg: &Cgroups, after: &KillProof) -> bool {
        let deadline = Instant::now() + self.cfg.deadlines.cleanup;
        let ok = cg
            .set_cleanup_limits(
                after,
                CLEANUP_MEMORY_BYTES.min(self.cfg.maxima.memory_max_bytes),
                CLEANUP_PIDS,
                self.req.limits.cpu_max_period_us,
            )
            .map_err(|e| log(&format!("cleanup limits: {e}")))
            .is_ok()
            && self.cleanup_as_job(cg, deadline);
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
            rlimits: cleanup_rlimits(self.cfg),
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
            let ready = child.stdout.take()?;
            if !read_ready(OwnedFd::from(ready), deadline) {
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
        // Bounded: the child was killed (or exited); reaping a SIGKILLed child returns.
        let _ = child.wait();
        ok.is_some()
    }
}

/// Memory of `<unit>/job` during the cleanup (L4; at most the configured job maximum).
pub const CLEANUP_MEMORY_BYTES: u64 = 64 << 20;
/// `pids.max` of `<unit>/job` during the cleanup (the helper, then itself as writ-job).
pub const CLEANUP_PIDS: u64 = 4;
/// `RLIMIT_NOFILE` of the cleanup (its walk holds one descriptor per directory level).
pub const CLEANUP_NOFILE: u64 = 4096;

/// The cleanup's rlimits (L4): fixed by the launcher, independent of the job's request (a job
/// asking for `nofile_max` 3 or `file_size_max_bytes` 1 must not break its own cleanup).
pub fn cleanup_rlimits(cfg: &Config) -> Rlimits {
    Rlimits {
        nofile: CLEANUP_NOFILE,
        fsize: 1 << 20,
        nproc: cfg.rlimits.nproc,
        msgqueue: cfg.rlimits.msgqueue,
        memlock: cfg.rlimits.memlock,
        sigpending: 64,
    }
}

/// Wait for the cleanup process's `ready\n` until `deadline` (L4): non-blocking reads under
/// `poll`, so a process that never writes (or writes part of it) cannot hold the launcher.
pub fn read_ready(fd: OwnedFd, deadline: Instant) -> bool {
    if rustix::io::ioctl_fionbio(&fd, true).is_err() {
        return false;
    }
    let mut f = File::from(fd);
    let mut got = Vec::with_capacity(6);
    let mut buf = [0u8; 6];
    while got.len() < 6 {
        if Instant::now() >= deadline {
            return false;
        }
        let mut fds = [PollFd::new(&f, PollFlags::IN)];
        match poll(&mut fds, Some(&timespec(deadline))) {
            Ok(_) | Err(rustix::io::Errno::INTR) => {}
            Err(_) => return false,
        }
        match f.read(&mut buf[..6 - got.len()]) {
            Ok(0) => return false,
            Ok(n) => got.extend_from_slice(&buf[..n]),
            Err(e) if matches!(e.kind(), ErrorKind::WouldBlock | ErrorKind::Interrupted) => {}
            Err(_) => return false,
        }
    }
    got == b"ready\n"
}

/// The result's reason, exit status and signal from the session's reason and the direct
/// child's reaped status.
fn outcome(
    reason: Option<Reason>,
    status: Option<ExitStatus>,
    output_exceeded: bool,
) -> (Reason, Option<i64>, Option<i64>) {
    use std::os::unix::process::ExitStatusExt;
    match (reason, status) {
        (Some(r), st) => (
            if output_exceeded {
                Reason::OutputExceeded
            } else {
                r
            },
            st.and_then(|x| x.code()).map(i64::from),
            st.and_then(|x| x.signal()).map(i64::from),
        ),
        (None, Some(st)) => match (st.code(), st.signal()) {
            (Some(c), _) => (Reason::Exited, Some(i64::from(c)), None),
            (None, sig) => (Reason::Signaled, None, sig.map(i64::from)),
        },
        (None, None) => (Reason::StartFailed, None, None),
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
        assert_eq!(h.apparmor_profile_digest, Digest::of_bytes(b"aa").as_str());
        assert_eq!(h.build, protocol::build_mode());
        // M7: a profile pulling in a local override is refused (no hello).
        std::fs::write(
            &paths.apparmor_profile,
            "profile writ-bwrap {\n  include if exists <local/writ-bwrap>\n}\n",
        )
        .unwrap();
        assert!(hello(&paths, &cfg, "4", &s.path().join("self")).is_err());
        std::fs::write(&paths.apparmor_profile, b"aa").unwrap();
        paths.bwrap = s.path().join("absent");
        assert!(hello(&paths, &cfg, "4", &s.path().join("self")).is_err());
    }

    #[test]
    fn apparmor_profile_must_be_self_contained() {
        let shipped = include_str!("../../m5/reference-vm/launcher/apparmor-writ-bwrap");
        apparmor_includes_ok(shipped).unwrap();
        for bad in [
            "  include if exists <local/writ-bwrap>\n",
            "include <local/writ-job-child>\n",
            "#include <local/writ-bwrap>\n",
            "  include \"/etc/apparmor.d/other\"\n",
        ] {
            assert!(apparmor_includes_ok(bad).is_err(), "{bad:?}");
        }
        apparmor_includes_ok("# a comment that mentions include\ninclude <tunables/global>\n")
            .unwrap();
    }

    fn cfg() -> Config {
        Config::parse(
            json::canonical(&crate::config::tests::sample())
                .unwrap()
                .as_bytes(),
        )
        .unwrap()
    }

    fn facts() -> SelfFacts {
        SelfFacts {
            status: "Uid:\t0\t0\t0\t0\nGid:\t0\t0\t0\t0\nGroups:\t0 990 \nNoNewPrivs:\t1\n\
                     Threads:\t1\nCapBnd:\t00000000000000c1\nCapAmb:\t0000000000000000\n"
                .into(),
            source_gid: 990,
            unit_memory_max: Some(512 << 20),
            nofile: Some(4096),
            test_fault_env: false,
        }
    }

    #[test]
    fn self_checks_cover_privileges_groups_budget_and_build() {
        let c = cfg();
        self_check(&facts(), &c, false).unwrap();
        let bad = |edit: &dyn Fn(&mut SelfFacts)| {
            let mut f = facts();
            edit(&mut f);
            self_check(&f, &c, false).unwrap_err()
        };
        assert!(
            bad(&|f| f.status = f.status.replace("NoNewPrivs:\t1", "NoNewPrivs:\t0"))
                .contains("NoNewPrivs")
        );
        assert!(bad(&|f| f.status = f.status.replace("c1\n", "c3\n")).contains("bounding"));
        assert!(
            bad(&|f| f.status = f.status.replace("CapAmb:\t0000000000000000", "CapAmb:\t1"))
                .contains("ambient")
        );
        assert!(bad(&|f| f.status = f.status.replace("0 990", "0 990 991")).contains("groups"));
        assert!(bad(&|f| f.status = f.status.replace("0 990", "0")).contains("groups"));
        assert!(bad(&|f| f.unit_memory_max = Some(256 << 20)).contains("memory.max"));
        assert!(bad(&|f| f.unit_memory_max = None).contains("memory.max"));
        assert!(bad(&|f| f.nofile = Some(64)).contains("RLIMIT_NOFILE"));
        assert!(bad(&|f| f.test_fault_env = true).contains("WRIT_TEST_FAULT"));
        // A test-faults build may be given a fault.
        let mut f = facts();
        f.test_fault_env = true;
        self_check(&f, &c, true).unwrap();
        // Groups without root's own group 0 are fine too.
        f.status = f.status.replace("0 990", "990");
        self_check(&f, &c, true).unwrap();
    }

    #[test]
    fn cleanup_limits_ignore_the_jobs_request() {
        let c = cfg();
        let r = cleanup_rlimits(&c);
        assert_eq!(r.nofile, CLEANUP_NOFILE);
        assert_eq!(r.fsize, 1 << 20);
        assert_eq!(r.nproc, c.rlimits.nproc);
        const { assert!(CLEANUP_PIDS >= 2) };
    }

    #[test]
    fn waiting_for_ready_is_bounded() {
        use rustix::pipe::{pipe_with, PipeFlags};
        // L4: a cleanup process that never writes `ready` (or writes part of it) cannot hold
        // the launcher past the deadline.
        let (r, w) = pipe_with(PipeFlags::CLOEXEC).unwrap();
        let t = Instant::now();
        assert!(!read_ready(r, Instant::now() + Duration::from_millis(100)));
        assert!(t.elapsed() < Duration::from_secs(2));
        let (r, w2) = pipe_with(PipeFlags::CLOEXEC).unwrap();
        File::from(w2.try_clone().unwrap())
            .write_all(b"rea")
            .unwrap();
        assert!(!read_ready(r, Instant::now() + Duration::from_millis(100)));
        let (r, w3) = pipe_with(PipeFlags::CLOEXEC).unwrap();
        File::from(w3.try_clone().unwrap())
            .write_all(b"ready\n")
            .unwrap();
        assert!(read_ready(r, Instant::now() + Duration::from_secs(2)));
        drop((w, w2, w3));
    }

    #[test]
    fn outcome_prefers_the_session_reason_and_reaped_status() {
        use std::os::unix::process::ExitStatusExt;
        let killed = ExitStatus::from_raw(9);
        assert_eq!(
            outcome(Some(Reason::Cancelled), Some(killed), false),
            (Reason::Cancelled, None, Some(9))
        );
        assert_eq!(
            outcome(Some(Reason::TimedOut), None, true).0,
            Reason::OutputExceeded
        );
        assert_eq!(
            outcome(None, Some(ExitStatus::from_raw(3 << 8)), false),
            (Reason::Exited, Some(3), None)
        );
        assert_eq!(outcome(None, None, false).0, Reason::StartFailed);
    }
}
