//! The start-time sweep of job state left by a previous `writ-serviced` (M5a-11; lead
//! follow-up after M5a-07; M5-INTERFACES J-11.6 row "`writ` crashes or stops", J-8 step 4).
//!
//! Everything goes through the launcher contract; the service never touches a job cgroup,
//! the jobs filesystem or a job process (it has no permission to, J-1):
//!
//! 1. **Wait for `populated 0`** of `writ-jobs.slice` (read-only `cgroup.events`, allowed
//!    under `ProtectControlGroups=yes`, J-11.5), bounded by the configured wait. The launcher
//!    kills and verifies a job when its `writ` connection reaches EOF, and systemd kills the
//!    instance cgroup when the launcher dies, so a job of the previous service ends without
//!    any action here. Until `populated 0` is observed, containment is not ready
//!    ([`SweepReport::containment_ready`] false) and nothing is removed (a launcher may still
//!    be reading a source directory).
//! 2. **Remove leftover sources** `run-<id>` under the run root: only directories with a
//!    valid job id that the service uid owns (the runner materializes them; a crash can leave
//!    one behind). `std::fs::remove_dir_all` does not follow symbolic links. Anything else is
//!    kept and reported.
//! 3. **Runs stay uncertain:** the runner store is not read or changed; a run left `started`
//!    stays `started` (never re-run, no completion evidence, J-11.6); only an operator
//!    disposition (`run_abandon`, J-11.9) ends it.
//!
//! Leftover per-job directories in `/var/lib/writ-jobs` are launcher residue: the service
//! cannot list them (0711 root), and the launcher's per-launch residue check refuses every
//! launch (`LAUNCH_UNAVAILABLE`) until the operator clears them (J-11.7).

use std::fs;
use std::os::unix::fs::MetadataExt;
use std::path::Path;
use std::thread;
use std::time::{Duration, Instant};

use writ_broker::writ_kernel::json::{self, Value};

use crate::config::SweepConfig;

/// What the sweep observed and did.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SweepReport {
    /// `Some(false)` when `populated 0` was observed, `Some(true)` when the slice was still
    /// populated at the end of the wait, `None` when `cgroup.events` could not be read.
    pub jobs_populated: Option<bool>,
    /// Source directories removed.
    pub removed: Vec<String>,
    /// Entries of the run root left in place (not a valid `run-<id>` owned by the service, or
    /// the removal failed).
    pub kept: Vec<String>,
}

impl SweepReport {
    /// `true` only when the slice was observed empty (J-11.6: wait for `populated 0`).
    pub fn containment_ready(&self) -> bool {
        self.jobs_populated == Some(false)
    }

    /// The readiness member (names only; no path outside the run root's entries).
    pub fn to_value(&self) -> Value {
        let names = |v: &[String]| Value::Arr(v.iter().map(|n| json::s(n)).collect());
        json::obj([
            (
                "jobs_populated",
                self.jobs_populated.map_or(Value::Null, Value::Bool),
            ),
            ("containment_ready", Value::Bool(self.containment_ready())),
            ("removed", names(&self.removed)),
            ("kept", names(&self.kept)),
        ])
    }
}

/// `populated` of a `cgroup.events` file; `None` when unreadable or malformed.
pub fn populated(events: &Path) -> Option<bool> {
    let text = fs::read_to_string(events).ok()?;
    text.lines()
        .find_map(|l| l.strip_prefix("populated "))
        .and_then(|v| match v.trim() {
            "0" => Some(false),
            "1" => Some(true),
            _ => None,
        })
}

fn valid_run_name(name: &str) -> bool {
    name.strip_prefix("run-").is_some_and(|id| {
        !id.is_empty()
            && id.len() <= 64
            && id
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
    })
}

/// Run the sweep (module documentation) as `service_uid`.
pub fn sweep(cfg: &SweepConfig, service_uid: u32) -> SweepReport {
    let until = Instant::now() + cfg.wait;
    let mut state = populated(&cfg.jobs_cgroup_events);
    while state != Some(false) && Instant::now() < until {
        thread::sleep(Duration::from_millis(50));
        state = populated(&cfg.jobs_cgroup_events);
    }
    let mut report = SweepReport {
        jobs_populated: state,
        removed: Vec::new(),
        kept: Vec::new(),
    };
    if !report.containment_ready() {
        return report;
    }
    let Ok(entries) = fs::read_dir(&cfg.run_root) else {
        return report;
    };
    let mut names: Vec<(String, std::path::PathBuf)> = entries
        .filter_map(Result::ok)
        .map(|e| (e.file_name().to_string_lossy().into_owned(), e.path()))
        .collect();
    names.sort();
    for (name, path) in names {
        let ours = fs::symlink_metadata(&path)
            .is_ok_and(|m| m.is_dir() && m.uid() == service_uid && valid_run_name(&name));
        if ours && fs::remove_dir_all(&path).is_ok() {
            report.removed.push(name);
        } else {
            report.kept.push(name);
        }
    }
    report
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;
    use std::os::unix::fs::symlink;
    use std::path::PathBuf;

    fn me() -> u32 {
        fs::metadata("/proc/self").unwrap().uid()
    }

    fn setup(name: &str) -> (PathBuf, SweepConfig) {
        let dir = std::env::temp_dir().join(format!("writ-sweep-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(dir.join("run")).unwrap();
        let cfg = SweepConfig {
            run_root: dir.join("run"),
            jobs_cgroup_events: dir.join("cgroup.events"),
            wait: Duration::from_millis(300),
        };
        (dir, cfg)
    }

    #[test]
    fn leftover_sources_are_removed_only_after_populated_0() {
        let (dir, cfg) = setup("ok");
        let run = &cfg.run_root;
        fs::create_dir_all(run.join("run-permit-1/src")).unwrap();
        fs::write(run.join("run-permit-1/src/lib.rs"), b"x").unwrap();
        fs::create_dir(run.join("run-BAD")).unwrap();
        fs::write(run.join("run-file"), b"not a directory").unwrap();
        // A symbolic link named like a run is kept and its target untouched.
        fs::create_dir(dir.join("outside")).unwrap();
        fs::write(dir.join("outside/keep"), b"k").unwrap();
        symlink(dir.join("outside"), run.join("run-link")).unwrap();
        // Still populated: nothing removed, containment not ready.
        fs::write(&cfg.jobs_cgroup_events, "populated 1\nfrozen 0\n").unwrap();
        let r = sweep(&cfg, me());
        assert_eq!(r.jobs_populated, Some(true));
        assert!(!r.containment_ready());
        assert!(r.removed.is_empty() && r.kept.is_empty());
        assert!(run.join("run-permit-1").exists());
        // Empty: the valid, service-owned source is removed; everything else is kept.
        fs::write(&cfg.jobs_cgroup_events, "populated 0\nfrozen 0\n").unwrap();
        let r = sweep(&cfg, me());
        assert!(r.containment_ready());
        assert_eq!(r.removed, vec!["run-permit-1".to_string()]);
        assert_eq!(
            r.kept,
            vec![
                "run-BAD".to_string(),
                "run-file".to_string(),
                "run-link".to_string()
            ]
        );
        assert!(dir.join("outside/keep").exists());
        // Owned by another uid: kept.
        fs::create_dir(run.join("run-permit-2")).unwrap();
        let r = sweep(&cfg, me() + 1);
        assert!(r.removed.is_empty() && r.kept.contains(&"run-permit-2".to_string()));
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn an_unreadable_slice_is_not_ready() {
        let (dir, cfg) = setup("missing");
        fs::create_dir(cfg.run_root.join("run-permit-1")).unwrap();
        let started = Instant::now();
        let r = sweep(&cfg, me());
        assert!(started.elapsed() >= Duration::from_millis(300), "waited");
        assert_eq!(r.jobs_populated, None);
        assert!(!r.containment_ready());
        assert!(cfg.run_root.join("run-permit-1").exists());
        fs::write(&cfg.jobs_cgroup_events, "populated x\n").unwrap();
        assert_eq!(populated(&cfg.jobs_cgroup_events), None);
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn the_slice_emptying_during_the_wait_is_observed() {
        let (dir, mut cfg) = setup("wait");
        cfg.wait = Duration::from_secs(5);
        fs::write(&cfg.jobs_cgroup_events, "populated 1\n").unwrap();
        let events = cfg.jobs_cgroup_events.clone();
        let t = thread::spawn(move || {
            thread::sleep(Duration::from_millis(200));
            fs::write(events, "populated 0\n").unwrap();
        });
        let r = sweep(&cfg, me());
        t.join().unwrap();
        assert!(r.containment_ready());
        fs::remove_dir_all(&dir).unwrap();
    }
}
