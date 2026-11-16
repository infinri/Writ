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
use rustix::fs::{mkdirat, openat2, unlinkat, AtFlags, Mode, OFlags, ResolveFlags, CWD};
use std::fs::File;
use std::io::{self, Read, Write};
use std::os::fd::{AsFd, OwnedFd};
use std::path::Path;
use std::time::{Duration, Instant};

/// Prefix of the launcher's instance units.
pub const UNIT_PREFIX: &str = "writ-job-launcher@";
/// Controllers enabled for `<unit>/job`.
pub const CONTROLLERS: &str = "+memory +pids +cpu";

const RESOLVE: ResolveFlags = ResolveFlags::BENEATH
    .union(ResolveFlags::NO_SYMLINKS)
    .union(ResolveFlags::NO_MAGICLINKS);

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
        let root = openat2(
            CWD,
            cgroup_root,
            OFlags::RDONLY | OFlags::DIRECTORY | OFlags::CLOEXEC,
            Mode::empty(),
            ResolveFlags::NO_SYMLINKS | ResolveFlags::NO_MAGICLINKS,
        )?;
        let fd = openat2(
            &root,
            unit.rel.as_str(),
            OFlags::RDONLY | OFlags::DIRECTORY | OFlags::CLOEXEC,
            Mode::empty(),
            RESOLVE,
        )?;
        Ok(Cgroups {
            unit: fd,
            rel: unit.rel.clone(),
        })
    }

    /// The unit path relative to the mount.
    pub fn rel(&self) -> &str {
        &self.rel
    }

    fn write(&self, file: &str, text: &str) -> io::Result<()> {
        let fd = openat2(
            &self.unit,
            file,
            OFlags::WRONLY | OFlags::CLOEXEC,
            Mode::empty(),
            RESOLVE,
        )?;
        File::from(fd).write_all(text.as_bytes())
    }

    fn read(&self, file: &str) -> io::Result<String> {
        let fd = openat2(
            &self.unit,
            file,
            OFlags::RDONLY | OFlags::CLOEXEC,
            Mode::empty(),
            RESOLVE,
        )?;
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
        openat2(
            &self.unit,
            "job",
            OFlags::RDONLY | OFlags::DIRECTORY | OFlags::CLOEXEC,
            Mode::empty(),
            RESOLVE,
        )
        .is_ok()
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
        self.read("job/cgroup.procs")?
            .lines()
            .filter(|l| !l.trim().is_empty())
            .map(|l| {
                l.trim()
                    .parse::<i32>()
                    .map_err(|_| io::Error::from(io::ErrorKind::InvalidData))
            })
            .collect()
    }

    /// `populated 0` currently reported for `<unit>/job`.
    pub fn job_populated_zero(&self) -> io::Result<bool> {
        Ok(populated_zero(&self.read("job/cgroup.events")?))
    }

    /// Write `cgroup.kill` and wait (bounded) for `populated 0`.
    pub fn kill_and_verify(&self, deadline: Duration) -> Result<KillProof, KillFailed> {
        self.write("job/cgroup.kill", "1")
            .map_err(|e| KillFailed(format!("cgroup.kill: {e}")))?;
        let start = Instant::now();
        loop {
            match self.job_populated_zero() {
                Ok(true) => return Ok(KillProof { _private: () }),
                Ok(false) => {}
                Err(e) => return Err(KillFailed(format!("cgroup.events: {e}"))),
            }
            if start.elapsed() >= deadline {
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
