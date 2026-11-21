//! The launcher instance's delegated cgroup subtree (J-5, J-11.4 step 3, J-11.5; N6).
//!
//! The unit path comes only from `/proc/self/cgroup` (never from the request) and must be
//! `/writ.slice/writ-jobs.slice/writ-job-launcher@<instance>.service`. Every file is opened
//! beneath the unit's directory descriptor (`RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS |
//! RESOLVE_NO_MAGICLINKS`). Limits are written to `<unit>/job` before any job process exists.
//! Termination writes `1` to `<unit>/job/cgroup.kill` and waits, bounded, for `populated 0` in
//! `<unit>/job/cgroup.events`; only that observation produces a [`KillProof`], which is what
//! [`crate::gate::Gate::close_after`] requires.

use crate::protocol::JobLimits;
use rustix::fs::{mkdirat, openat, unlinkat, AtFlags, Mode, OFlags, CWD};
use std::fs::File;
use std::io::{self, Read, Write};
use std::os::fd::{AsFd, OwnedFd};
use std::path::{Component, Path};
use std::time::{Duration, Instant};

/// Prefix of the launcher's instance units.
pub const UNIT_PREFIX: &str = "writ-job-launcher@";
/// Controllers enabled for `<unit>/job`.
pub const CONTROLLERS: &str = "+memory +pids +cpu";

const DIR: OFlags = OFlags::DIRECTORY
    .union(OFlags::CLOEXEC)
    .union(OFlags::NOFOLLOW);

// Only for the systemd-owned cgroup hierarchy in the launcher's fixed mount namespace.
// cgroup v2 forbids directory renames. Never use this walk on a job-controlled tree.
fn open_cgroup_path(parent: &OwnedFd, path: &Path, flags: OFlags) -> io::Result<OwnedFd> {
    let mut parts = path.components().peekable();
    let mut dir = None;
    while let Some(component) = parts.next() {
        let Component::Normal(name) = component else {
            return Err(io::Error::other("invalid cgroup path component"));
        };
        let base = dir.as_ref().unwrap_or(parent);
        if parts.peek().is_none() {
            return Ok(openat(
                base,
                name,
                flags | OFlags::NOFOLLOW | OFlags::CLOEXEC,
                Mode::empty(),
            )?);
        }
        dir = Some(openat(base, name, OFlags::PATH | DIR, Mode::empty())?);
    }
    Err(io::Error::other("empty cgroup path"))
}

/// The launcher unit's cgroup.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct UnitPath {
    /// Path relative to the cgroup mount, without a leading `/`.
    pub rel: String,
    /// The systemd instance name.
    pub instance: String,
}

/// The unit path from `/proc/self/cgroup` text, read before the launcher moves itself into
/// `<unit>/supervisor`. `slice` is the slice path relative to the mount
/// (`writ.slice/writ-jobs.slice`).
pub fn unit_from_proc_cgroup(text: &str, slice: &str) -> Result<UnitPath, String> {
    let lines: Vec<&str> = text.lines().filter(|l| !l.is_empty()).collect();
    let [line] = lines.as_slice() else {
        return Err("not exactly one cgroup line (cgroup v2 only)".into());
    };
    let path = line
        .strip_prefix("0::/")
        .ok_or_else(|| "not a cgroup v2 line".to_string())?;
    let unit = path
        .strip_prefix(slice)
        .and_then(|r| r.strip_prefix('/'))
        .ok_or_else(|| format!("not beneath {slice}"))?;
    let instance = unit
        .strip_prefix(UNIT_PREFIX)
        .and_then(|r| r.strip_suffix(".service"))
        .ok_or_else(|| "not a writ-job-launcher instance unit".to_string())?;
    let ok = !instance.is_empty()
        && instance.len() <= 128
        && instance
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_' || b == b':' || b == b'.')
        && !instance.contains("..");
    if !ok {
        return Err("instance name outside the expected characters".into());
    }
    Ok(UnitPath {
        rel: path.to_string(),
        instance: instance.to_string(),
    })
}

/// `cgroup.events` says `populated 0` (strictly).
pub fn populated_zero(events: &str) -> bool {
    events
        .lines()
        .find_map(|l| l.strip_prefix("populated "))
        .map(str::trim)
        == Some("0")
}

fn parse_procs(text: &str) -> io::Result<Vec<i32>> {
    text.lines()
        .filter(|l| !l.trim().is_empty())
        .map(|l| {
            l.trim()
                .parse::<i32>()
                .map_err(|_| io::Error::from(io::ErrorKind::InvalidData))
        })
        .collect()
}

/// A counter line `name value` of a keyed cgroup file (0 when absent).
pub fn counter(text: &str, name: &str) -> u64 {
    writ_hostsys::probes::counter(text, name).unwrap_or(0)
}

/// Evidence that `<unit>/job` reported `populated 0` after `cgroup.kill` (constructible only
/// here).
#[derive(Debug)]
pub struct KillProof {
    _private: (),
}

#[cfg(test)]
impl KillProof {
    /// A proof for unit tests of other modules.
    pub(crate) fn for_tests() -> KillProof {
        KillProof { _private: () }
    }
}

/// The kill did not empty the cgroup within the deadline (or a file could not be used).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct KillFailed(pub String);

/// The outcome of [`Cgroups::terminate`].
#[derive(Debug)]
pub struct Terminated {
    /// The kill proof, or why there is none.
    pub proof: Result<KillProof, KillFailed>,
    /// The direct child's exit status when it was reaped.
    pub status: Option<std::process::ExitStatus>,
}

/// Counters reported in `finished`.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Counters {
    /// `memory.events` max, oom, oom_kill.
    pub memory_events: [u64; 3],
    /// `pids.events` max.
    pub pids_max: u64,
    /// `cpu.stat` usage_usec.
    pub cpu_usage_usec: u64,
}

/// The unit's subtree, held by descriptor.
pub struct Cgroups {
    unit: OwnedFd,
    rel: String,
}

impl Cgroups {
    /// Open the unit directory beneath the cgroup mount.
    pub fn open(cgroup_root: &Path, unit: &UnitPath) -> io::Result<Cgroups> {
        Self::open_relative(cgroup_root, &unit.rel)
    }

    pub(crate) fn open_relative(cgroup_root: &Path, rel: &str) -> io::Result<Cgroups> {
        // Mount acquisition forbids links but permits crossing mounts and using the
        // configured absolute or cwd-relative path. Only the unit lookup is beneath.
        let mut parts = cgroup_root.components().peekable();
        let mut mount: Option<OwnedFd> = None;
        while let Some(part) = parts.next() {
            let base = mount.as_ref().map_or(CWD, |fd| fd.as_fd());
            let flags = if parts.peek().is_some() {
                OFlags::PATH
            } else {
                OFlags::RDONLY
            };
            mount = Some(openat(base, part.as_os_str(), flags | DIR, Mode::empty())?);
        }
        let mount = mount.ok_or_else(|| io::Error::other("empty cgroup mount path"))?;
        let unit = open_cgroup_path(&mount, Path::new(rel), OFlags::RDONLY | DIR)?;
        Ok(Cgroups {
            unit,
            rel: rel.to_string(),
        })
    }

    pub(crate) fn join_job(&self) -> io::Result<()> {
        self.write("job/cgroup.procs", &std::process::id().to_string())
    }

    /// The unit path relative to the mount.
    pub fn rel(&self) -> &str {
        &self.rel
    }

    fn write(&self, file: &str, text: &str) -> io::Result<()> {
        let fd = open_cgroup_path(&self.unit, Path::new(file), OFlags::WRONLY)?;
        File::from(fd).write_all(text.as_bytes())
    }

    fn read(&self, file: &str) -> io::Result<String> {
        let fd = open_cgroup_path(&self.unit, Path::new(file), OFlags::RDONLY)?;
        let mut s = String::new();
        File::from(fd).take(64 * 1024).read_to_string(&mut s)?;
        Ok(s)
    }

    fn mkdir(&self, name: &str) -> io::Result<()> {
        match mkdirat(&self.unit, name, Mode::from_raw_mode(0o755)) {
            Ok(()) => Ok(()),
            Err(e) => Err(e.into()),
        }
    }

    /// Whether `<unit>/job` exists.
    pub fn job_exists(&self) -> bool {
        open_cgroup_path(&self.unit, Path::new("job"), OFlags::RDONLY | DIR).is_ok()
    }

    /// Move `pid` (the launcher) into `<unit>/supervisor` (created if needed), so the unit's
    /// own cgroup holds no process and controllers can be enabled for children.
    pub fn enter_supervisor(&self, pid: u32) -> io::Result<()> {
        match self.mkdir("supervisor") {
            Err(e) if e.kind() != io::ErrorKind::AlreadyExists => return Err(e),
            _ => {}
        }
        self.write("supervisor/cgroup.procs", &pid.to_string())
    }

    /// Enable the job controllers in `<unit>/cgroup.subtree_control`, create `<unit>/job` and
    /// write its limits (before any job process exists).
    pub fn create_job(&self, l: &JobLimits) -> io::Result<()> {
        self.write("cgroup.subtree_control", CONTROLLERS)?;
        self.mkdir("job")?;
        self.write("job/memory.max", &l.memory_max_bytes.to_string())?;
        self.write("job/memory.swap.max", "0")?;
        self.write("job/memory.oom.group", "1")?;
        self.write("job/pids.max", &l.pids_max.to_string())?;
        self.write(
            "job/cpu.max",
            &format!("{} {}", l.cpu_max_quota_us, l.cpu_max_period_us),
        )
    }

    /// Pids in `<unit>/job/cgroup.procs`.
    pub fn job_procs(&self) -> io::Result<Vec<i32>> {
        parse_procs(&self.read("job/cgroup.procs")?)
    }

    /// `populated 0` currently reported for `<unit>/job`.
    pub fn job_populated_zero(&self) -> io::Result<bool> {
        Ok(populated_zero(&self.read("job/cgroup.events")?))
    }

    /// Write `cgroup.kill` and wait (bounded) for `populated 0`.
    pub fn kill_and_verify(&self, deadline: Duration) -> Result<KillProof, KillFailed> {
        let end = Instant::now() + deadline;
        self.write("job/cgroup.kill", "1")
            .map_err(|e| KillFailed(format!("cgroup.kill: {e}")))?;
        self.wait_populated_zero(end)
    }

    /// The unit's own `memory.max` (systemd's `MemoryMax=`), for the memory budget self-check
    /// (L7); `None` for `max` or an unreadable value.
    pub fn unit_memory_max(&self) -> Option<u64> {
        self.read("memory.max").ok()?.trim().parse().ok()
    }

    /// Freeze `<unit>/job` (`cgroup.freeze`) and wait, bounded, for `frozen 1`. Used only when
    /// the kill could not be verified (L5): the launcher then exits non-zero, which closes the
    /// gate writer; a frozen job cannot act on that EOF before systemd's `SIGKILL` of the
    /// instance cgroup (cgroup v2 delivers `SIGKILL` to frozen tasks; verify in VM).
    pub fn freeze_job(&self, deadline: Duration) -> Result<(), KillFailed> {
        let end = Instant::now() + deadline;
        self.write("job/cgroup.freeze", "1")
            .map_err(|e| KillFailed(format!("cgroup.freeze: {e}")))?;
        loop {
            let ev = self
                .read("job/cgroup.events")
                .map_err(|e| KillFailed(format!("cgroup.events: {e}")))?;
            let frozen = ev
                .lines()
                .find_map(|l| l.strip_prefix("frozen "))
                .map(str::trim)
                == Some("1");
            if frozen {
                return Ok(());
            }
            if Instant::now() >= end {
                return Err(KillFailed("frozen 0 at the deadline".into()));
            }
            std::thread::sleep(Duration::from_millis(5));
        }
    }

    /// Replace the job's own limits on `<unit>/job` by the fixed launcher-owned cleanup limits
    /// (L4) after a verified kill, before the cleanup process joins it.
    pub fn set_cleanup_limits(
        &self,
        _proof: &KillProof,
        memory_max: u64,
        pids_max: u64,
        cpu_period_us: u64,
    ) -> io::Result<()> {
        self.write("job/memory.max", &memory_max.to_string())?;
        self.write("job/pids.max", &pids_max.to_string())?;
        self.write("job/cpu.max", &format!("max {cpu_period_us}"))
    }

    /// Pids in `<unit>/supervisor/cgroup.procs`.
    pub fn supervisor_procs(&self) -> io::Result<Vec<i32>> {
        parse_procs(&self.read("supervisor/cgroup.procs")?)
    }

    /// Terminate a job whose direct child (the helper, later bubblewrap) is `child` (H1, M1).
    ///
    /// A cancel or EOF can arrive while the helper is still in `<unit>/supervisor` and has not
    /// joined `<unit>/job`; a `cgroup.kill` of the then empty `job` would prove nothing about
    /// it. So, in order, every step bounded by one `deadline` and every write result checked
    /// before anything waits:
    ///
    /// 1. `cgroup.kill` of `job` (stops a released job at once);
    /// 2. `SIGKILL` to the direct child through its pidfd (or `Child::kill` without one): the
    ///    helper can then never join `job` or execute bubblewrap;
    /// 3. the direct child reaped (polled, never a blocking `wait`);
    /// 4. `cgroup.kill` of `job` again (descendants forked into `job` before step 2);
    /// 5. `populated 0` of `job`;
    /// 6. `supervisor` holds only this launcher (`self_pid`).
    ///
    /// Only then is a [`KillProof`] returned. Any failure returns at once with the reason and
    /// whatever status was reaped; the caller reports `populated_zero: false` (J-11.5).
    pub fn terminate(
        &self,
        child: &mut std::process::Child,
        pidfd: Option<std::os::fd::BorrowedFd<'_>>,
        self_pid: u32,
        deadline: Duration,
    ) -> Terminated {
        let end = Instant::now() + deadline;
        let mut status = None;
        let proof = self.terminate_steps(child, pidfd, self_pid, end, &mut status);
        if proof.is_err() && status.is_none() {
            // Never block: reap only if the child is already gone.
            status = child.try_wait().ok().flatten();
        }
        Terminated { proof, status }
    }

    fn terminate_steps(
        &self,
        child: &mut std::process::Child,
        pidfd: Option<std::os::fd::BorrowedFd<'_>>,
        self_pid: u32,
        end: Instant,
        status: &mut Option<std::process::ExitStatus>,
    ) -> Result<KillProof, KillFailed> {
        let first = self.write("job/cgroup.kill", "1");
        // The direct child is signalled even when the kill write failed (never harmful).
        let signalled = match pidfd {
            Some(fd) => {
                match rustix::process::pidfd_send_signal(fd, rustix::process::Signal::KILL) {
                    Ok(()) | Err(rustix::io::Errno::SRCH) => Ok(()),
                    Err(e) => Err(io::Error::from(e)),
                }
            }
            None => child.kill(),
        };
        first.map_err(|e| KillFailed(format!("cgroup.kill: {e}")))?;
        signalled.map_err(|e| KillFailed(format!("SIGKILL of the direct child: {e}")))?;
        loop {
            match child.try_wait() {
                Ok(Some(st)) => {
                    *status = Some(st);
                    break;
                }
                Ok(None) => {}
                Err(e) => return Err(KillFailed(format!("reaping the direct child: {e}"))),
            }
            if Instant::now() >= end {
                return Err(KillFailed("direct child alive at the kill deadline".into()));
            }
            std::thread::sleep(Duration::from_millis(2));
        }
        self.write("job/cgroup.kill", "1")
            .map_err(|e| KillFailed(format!("cgroup.kill: {e}")))?;
        let proof = self.wait_populated_zero(end)?;
        let own = i32::try_from(self_pid).unwrap_or(-1);
        match self.supervisor_procs() {
            Ok(p) if p == [own] => Ok(proof),
            Ok(p) => Err(KillFailed(format!(
                "supervisor holds {p:?}, not only {own}"
            ))),
            Err(e) => Err(KillFailed(format!("supervisor/cgroup.procs: {e}"))),
        }
    }

    fn wait_populated_zero(&self, end: Instant) -> Result<KillProof, KillFailed> {
        loop {
            match self.job_populated_zero() {
                Ok(true) => return Ok(KillProof { _private: () }),
                Ok(false) => {}
                Err(e) => return Err(KillFailed(format!("cgroup.events: {e}"))),
            }
            if Instant::now() >= end {
                return Err(KillFailed("populated 1 at the kill deadline".into()));
            }
            std::thread::sleep(Duration::from_millis(5));
        }
    }

    /// The counters of `<unit>/job` (zero for an unreadable file).
    pub fn counters(&self) -> Counters {
        let r = |f: &str| self.read(f).unwrap_or_default();
        let mem = r("job/memory.events");
        Counters {
            memory_events: [
                counter(&mem, "max"),
                counter(&mem, "oom"),
                counter(&mem, "oom_kill"),
            ],
            pids_max: counter(&r("job/pids.events"), "max"),
            cpu_usage_usec: counter(&r("job/cpu.stat"), "usage_usec"),
        }
    }

    /// Remove `<unit>/job` after a verified kill.
    pub fn remove_job(&self, _proof: &KillProof) -> io::Result<()> {
        unlinkat(&self.unit, "job", AtFlags::REMOVEDIR)?;
        Ok(())
    }

    /// The unit directory descriptor (the helper re-opens `job` beneath its own unit).
    pub fn unit_fd(&self) -> std::os::fd::BorrowedFd<'_> {
        self.unit.as_fd()
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;
    use crate::testutil::Scratch;
    use std::fs;

    const SLICE: &str = "writ.slice/writ-jobs.slice";

    #[test]
    fn unit_path_only_from_the_expected_shape() {
        let u = unit_from_proc_cgroup(
            "0::/writ.slice/writ-jobs.slice/writ-job-launcher@3-1234-990.service\n",
            SLICE,
        )
        .unwrap();
        assert_eq!(u.instance, "3-1234-990");
        assert_eq!(
            u.rel,
            "writ.slice/writ-jobs.slice/writ-job-launcher@3-1234-990.service"
        );
        for bad in [
            "0::/system.slice/writ-job-launcher@1.service\n",
            "0::/writ.slice/writ-jobs.slice/other@1.service\n",
            "0::/writ.slice/writ-jobs.slice/writ-job-launcher@1.service/supervisor\n",
            "0::/writ.slice/writ-jobs.slice/writ-job-launcher@.service\n",
            "0::/writ.slice/writ-jobs.slice/writ-job-launcher@a..b.service\n",
            "1:name=systemd:/x\n0::/writ.slice/writ-jobs.slice/writ-job-launcher@1.service\n",
            "",
        ] {
            assert!(unit_from_proc_cgroup(bad, SLICE).is_err(), "{bad:?}");
        }
    }

    #[test]
    fn populated_is_strict() {
        assert!(populated_zero("populated 0\nfrozen 0\n"));
        assert!(!populated_zero("populated 1\n"));
        assert!(!populated_zero("populated x\n"));
        assert!(!populated_zero("frozen 0\n"));
    }

    /// A fake cgroup mount with the unit directory and pre-created files.
    fn fake() -> (Scratch, UnitPath) {
        let s = Scratch::new("cgroup");
        let rel = format!("{SLICE}/writ-job-launcher@7.service");
        let unit = s.path().join(&rel);
        fs::create_dir_all(unit.join("supervisor")).unwrap();
        fs::create_dir_all(unit.join("job")).unwrap();
        for f in ["cgroup.subtree_control", "supervisor/cgroup.procs"] {
            fs::write(unit.join(f), "").unwrap();
        }
        for f in [
            "memory.max",
            "memory.swap.max",
            "memory.oom.group",
            "pids.max",
            "cpu.max",
            "cgroup.kill",
            "cgroup.procs",
        ] {
            fs::write(unit.join("job").join(f), "").unwrap();
        }
        fs::write(unit.join("job/cgroup.events"), "populated 1\nfrozen 0\n").unwrap();
        (
            s,
            UnitPath {
                rel,
                instance: "7".into(),
            },
        )
    }

    #[test]
    fn limits_written_before_any_process_and_kill_needs_populated_zero() {
        let (s, u) = fake();
        let c = Cgroups::open(s.path(), &u).unwrap();
        c.enter_supervisor(42).unwrap();
        // The job directory pre-exists in the fake; create_job must not hide an existing one.
        assert!(c.create_job(&crate::protocol::tests::limits()).is_err());
        fs::remove_dir_all(s.path().join(&u.rel).join("job")).unwrap();
        // Real cgroupfs creates the files on mkdir; the fake cannot, so writes fail cleanly.
        assert!(c.create_job(&crate::protocol::tests::limits()).is_err());
        let unit = s.path().join(&u.rel);
        assert_eq!(
            fs::read_to_string(unit.join("cgroup.subtree_control")).unwrap(),
            CONTROLLERS
        );
        assert_eq!(
            fs::read_to_string(unit.join("supervisor/cgroup.procs")).unwrap(),
            "42"
        );
    }

    #[test]
    fn limits_values() {
        let (s, u) = fake();
        let c = Cgroups::open(s.path(), &u).unwrap();
        let unit = s.path().join(&u.rel);
        // Write the limit files directly through create_job's writer on an existing job dir.
        let l = crate::protocol::tests::limits();
        c.write("job/memory.max", &l.memory_max_bytes.to_string())
            .unwrap();
        c.write(
            "job/cpu.max",
            &format!("{} {}", l.cpu_max_quota_us, l.cpu_max_period_us),
        )
        .unwrap();
        assert_eq!(
            fs::read_to_string(unit.join("job/cpu.max")).unwrap(),
            "50000 100000"
        );
        fs::write(unit.join("job/cgroup.procs"), "12\n13\n").unwrap();
        assert_eq!(c.job_procs().unwrap(), vec![12, 13]);
    }

    #[test]
    fn kill_without_populated_zero_yields_no_proof() {
        let (s, u) = fake();
        let c = Cgroups::open(s.path(), &u).unwrap();
        let r = c.kill_and_verify(Duration::from_millis(30));
        assert!(r.is_err());
        let unit = s.path().join(&u.rel);
        assert_eq!(
            fs::read_to_string(unit.join("job/cgroup.kill")).unwrap(),
            "1"
        );
        fs::write(unit.join("job/cgroup.events"), "populated 0\n").unwrap();
        let proof = c.kill_and_verify(Duration::from_millis(30)).unwrap();
        fs::write(
            unit.join("job/memory.events"),
            "low 0\nmax 3\noom 1\noom_kill 1\n",
        )
        .unwrap();
        fs::write(unit.join("job/cpu.stat"), "usage_usec 77\n").unwrap();
        let k = c.counters();
        assert_eq!(k.memory_events, [3, 1, 1]);
        assert_eq!(k.cpu_usage_usec, 77);
        assert_eq!(k.pids_max, 0);
        // remove_job needs the proof; the fake directory still has files, so rmdir fails.
        assert!(c.remove_job(&proof).is_err());
    }

    /// A fake unit whose `job` is empty and whose `supervisor` holds only this process.
    fn fake_empty_job() -> (Scratch, Cgroups) {
        let (s, u) = fake();
        let unit = s.path().join(&u.rel);
        fs::write(unit.join("job/cgroup.events"), "populated 0\nfrozen 0\n").unwrap();
        fs::write(
            unit.join("supervisor/cgroup.procs"),
            format!("{}\n", std::process::id()),
        )
        .unwrap();
        let c = Cgroups::open(s.path(), &u).unwrap();
        (s, c)
    }

    fn blocked_child() -> std::process::Child {
        // Stands for a helper that joins `job` only after the kill and then blocks on the
        // gate (--block-fd 3) forever.
        std::process::Command::new("/bin/sh")
            .args(["-c", "exec sleep 600"])
            .stdin(std::process::Stdio::null())
            .spawn()
            .unwrap()
    }

    /// Run `terminate` on another thread; `None` when it did not return within `limit`.
    fn bounded(
        s: Scratch,
        c: Cgroups,
        mut child: std::process::Child,
        deadline: Duration,
        limit: Duration,
    ) -> Option<(Terminated, std::process::Child)> {
        let pid = child.id();
        let (tx, rx) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            let pidfd = rustix::process::pidfd_open(
                rustix::process::Pid::from_raw(i32::try_from(child.id()).unwrap()).unwrap(),
                rustix::process::PidfdFlags::empty(),
            )
            .unwrap();
            let t = c.terminate(
                &mut child,
                Some(pidfd.as_fd()),
                std::process::id(),
                deadline,
            );
            let _ = tx.send((t, child));
            drop(s);
        });
        let got = rx.recv_timeout(limit).ok();
        if got.is_none() {
            // Unblock the stuck thread so the test process does not keep a sleeper.
            let _ = std::process::Command::new("kill")
                .args(["-9", &pid.to_string()])
                .status();
        }
        got
    }

    #[test]
    fn termination_before_the_helper_joined_kills_the_direct_child_and_is_bounded() {
        // H1: a cancel or EOF during start finds `job` still empty; the direct child must be
        // killed through its pidfd and every wait bounded, never a blocking wait.
        let (s, c) = fake_empty_job();
        let t0 = Instant::now();
        let (t, _child) = bounded(
            s,
            c,
            blocked_child(),
            Duration::from_secs(2),
            Duration::from_secs(5),
        )
        .expect("terminate blocked on the direct child");
        assert!(t0.elapsed() < Duration::from_secs(5));
        assert!(t.proof.is_ok(), "{:?}", t.proof);
        let st = t.status.expect("direct child reaped");
        assert_eq!(std::os::unix::process::ExitStatusExt::signal(&st), Some(9));
    }

    #[test]
    fn a_failed_kill_write_reports_at_once_without_waiting() {
        // M1: the cgroup.kill write result is checked first; a failure never blocks.
        let (s, c) = fake_empty_job();
        fs::remove_file(s.path().join(&c.rel).join("job/cgroup.kill")).unwrap();
        let (t, _child) = bounded(
            s,
            c,
            blocked_child(),
            Duration::from_secs(2),
            Duration::from_secs(5),
        )
        .expect("terminate blocked after a failed kill write");
        assert!(t.proof.is_err());
    }

    #[test]
    fn a_foreign_process_in_supervisor_yields_no_proof() {
        // Every cgroup the helper could reach must be empty of it: `supervisor` holds only the
        // launcher.
        let (s, c) = fake_empty_job();
        fs::write(
            s.path().join(&c.rel).join("supervisor/cgroup.procs"),
            format!("{}\n1\n", std::process::id()),
        )
        .unwrap();
        let (t, _child) = bounded(
            s,
            c,
            blocked_child(),
            Duration::from_secs(2),
            Duration::from_secs(5),
        )
        .expect("terminate blocked");
        assert!(t.proof.is_err());
    }

    #[test]
    fn freeze_waits_bounded_for_frozen() {
        // L5: on an unverified kill the job is frozen before the launcher exits.
        let (s, c) = fake_empty_job();
        let job = s.path().join(&c.rel).join("job");
        fs::write(job.join("cgroup.freeze"), "").unwrap();
        fs::write(job.join("cgroup.events"), "populated 1\nfrozen 0\n").unwrap();
        let t = Instant::now();
        assert!(c.freeze_job(Duration::from_millis(50)).is_err());
        assert!(t.elapsed() < Duration::from_secs(2));
        assert_eq!(fs::read_to_string(job.join("cgroup.freeze")).unwrap(), "1");
        fs::write(job.join("cgroup.events"), "populated 1\nfrozen 1\n").unwrap();
        c.freeze_job(Duration::from_millis(50)).unwrap();
    }

    #[test]
    fn cleanup_limits_replace_the_jobs_own() {
        // L4: launcher-owned limits on `job` for the cleanup, after a verified kill.
        let (s, c) = fake_empty_job();
        let job = s.path().join(&c.rel).join("job");
        fs::write(job.join("memory.max"), "1").unwrap();
        c.set_cleanup_limits(&KillProof::for_tests(), 64 << 20, 4, 100_000)
            .unwrap();
        assert_eq!(
            fs::read_to_string(job.join("memory.max")).unwrap(),
            (64u64 << 20).to_string()
        );
        assert_eq!(fs::read_to_string(job.join("pids.max")).unwrap(), "4");
        assert_eq!(
            fs::read_to_string(job.join("cpu.max")).unwrap(),
            "max 100000"
        );
        // The unit's own memory.max (systemd MemoryMax=) for the L7 budget check.
        fs::write(s.path().join(&c.rel).join("memory.max"), "603979776\n").unwrap();
        assert_eq!(c.unit_memory_max(), Some(603_979_776));
        fs::write(s.path().join(&c.rel).join("memory.max"), "max\n").unwrap();
        assert_eq!(c.unit_memory_max(), None);
    }

    #[test]
    fn symlinked_unit_component_is_refused() {
        let s = Scratch::new("cgroup-link");
        fs::create_dir_all(s.path().join("elsewhere")).unwrap();
        fs::create_dir_all(s.path().join(SLICE)).unwrap();
        std::os::unix::fs::symlink(
            s.path().join("elsewhere"),
            s.path().join(SLICE).join("writ-job-launcher@7.service"),
        )
        .unwrap();
        let u = UnitPath {
            rel: format!("{SLICE}/writ-job-launcher@7.service"),
            instance: "7".into(),
        };
        assert!(Cgroups::open(s.path(), &u).is_err());
    }
}
