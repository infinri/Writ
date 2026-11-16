//! Per-launch residue checks by the launcher (J-11.7, H3, revision 6 N1), only what its view
//! contains: no process with any uid equal to `writ-job` on the host (scan of
//! `/proc/*/status`); no other populated instance in `writ-jobs.slice` and no leftover
//! `<unit>/job`; `/var/lib/writ-jobs` without any entry (a failed cleanup is residue, N8);
//! `/proc/key-users` without a line for the `writ-job` kuid. Any finding refuses
//! `LAUNCH_UNAVAILABLE`. Host locations (`/tmp`, `/var/tmp`, `/run`, `/dev/shm`, `/dev/mqueue`,
//! `/run/user`, `/var/crash`) are outside the launcher's view and are covered by the
//! operator-run P-JOB-DENY and P-JOB-SERIAL probes, never bound in to be checked here.

use crate::cgroup::populated_zero;
use crate::procfs::{key_users_has, parse_status, ProcView};
use std::path::Path;

/// One finding.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum Finding {
    /// A process with a `writ-job` uid (real, effective, saved or filesystem).
    JobProcess(i32),
    /// Another launcher instance in the slice is populated.
    PopulatedInstance(String),
    /// This instance already has a `job` cgroup.
    LeftoverJobCgroup,
    /// An entry in the jobs filesystem.
    JobsEntry(String),
    /// `/proc/key-users` has a line for the `writ-job` kuid.
    KeyUsers,
    /// A view could not be read (fail closed).
    Unreadable(String),
}

/// Processes with any uid equal to `job_uid`. A status that disappears between the listing
/// and the read is skipped (the process exited); an unparsable one is a finding.
pub fn job_processes(view: &dyn ProcView, job_uid: u32) -> Vec<Finding> {
    let pids = match view.pids() {
        Ok(p) => p,
        Err(e) => return vec![Finding::Unreadable(format!("/proc: {e}"))],
    };
    let mut out = Vec::new();
    for pid in pids {
        match view.status(pid) {
            Ok(text) => match parse_status(&text) {
                Some(st) if st.uid.contains(&job_uid) => out.push(Finding::JobProcess(pid)),
                Some(_) => {}
                None => out.push(Finding::Unreadable(format!("/proc/{pid}/status"))),
            },
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) if e.raw_os_error() == Some(3) => {} // ESRCH: exited
            Err(e) => out.push(Finding::Unreadable(format!("/proc/{pid}/status: {e}"))),
        }
    }
    out
}

/// The key-users check.
pub fn key_users(view: &dyn ProcView, job_uid: u32) -> Vec<Finding> {
    match view.key_users() {
        Ok(t) if key_users_has(&t, job_uid) => vec![Finding::KeyUsers],
        Ok(_) => vec![],
        Err(e) => vec![Finding::Unreadable(format!("/proc/key-users: {e}"))],
    }
}

/// Populated launcher instances in the slice directory other than `own_unit` (the unit
/// directory name), and a leftover `job` cgroup in `own_unit`.
pub fn slice(slice_dir: &Path, own_unit: &str) -> Vec<Finding> {
    let entries = match std::fs::read_dir(slice_dir) {
        Ok(e) => e,
        Err(e) => return vec![Finding::Unreadable(format!("slice: {e}"))],
    };
    let mut out = Vec::new();
    for e in entries {
        let Ok(e) = e else {
            out.push(Finding::Unreadable("slice entry".into()));
            continue;
        };
        let name = e.file_name().to_string_lossy().into_owned();
        let Ok(ft) = e.file_type() else {
            out.push(Finding::Unreadable(format!("slice entry {name}")));
            continue;
        };
        if !ft.is_dir() {
            continue;
        }
        if name == own_unit {
            if e.path().join("job").exists() {
                out.push(Finding::LeftoverJobCgroup);
            }
            continue;
        }
        match std::fs::read_to_string(e.path().join("cgroup.events")) {
            Ok(t) if populated_zero(&t) => {}
            Ok(_) => out.push(Finding::PopulatedInstance(name)),
            Err(err) => out.push(Finding::Unreadable(format!("{name}/cgroup.events: {err}"))),
        }
    }
    // Directory order differs between filesystems; report findings in a fixed order.
    out.sort();
    out
}

/// Entries in the jobs filesystem (`lost+found` of an ext4 root is ignored when root-owned
/// and empty is not required; it is never writable by `writ-job`).
pub fn jobs_fs(jobs_root: &Path) -> Vec<Finding> {
    let entries = match std::fs::read_dir(jobs_root) {
        Ok(e) => e,
        Err(e) => return vec![Finding::Unreadable(format!("jobs filesystem: {e}"))],
    };
    let mut out = Vec::new();
    for e in entries {
        match e {
            Ok(e) => {
                let name = e.file_name().to_string_lossy().into_owned();
                if name != "lost+found" {
                    out.push(Finding::JobsEntry(name));
                }
            }
            Err(err) => out.push(Finding::Unreadable(format!("jobs filesystem entry: {err}"))),
        }
    }
    out.sort();
    out
}

/// Every per-launch check.
pub fn check_all(
    view: &dyn ProcView,
    job_uid: u32,
    slice_dir: &Path,
    own_unit: &str,
    jobs_root: &Path,
) -> Vec<Finding> {
    let mut f = job_processes(view, job_uid);
    f.extend(slice(slice_dir, own_unit));
    f.extend(jobs_fs(jobs_root));
    f.extend(key_users(view, job_uid));
    f
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;
    use crate::procfs::tests::{status_text, FakeProc};
    use crate::testutil::Scratch;
    use std::fs;

    #[test]
    fn job_uid_process_found_by_any_uid_field() {
        let mut v = FakeProc::default();
        v.status.insert(1, status_text(0, 0, "", 0));
        assert!(job_processes(&v, 991).is_empty());
        // A lingering writ-job process (started outside the launcher).
        v.status.insert(77, status_text(991, 991, "", 0));
        // A process that only kept writ-job as its saved uid.
        v.status.insert(
            78,
            "Uid:\t0\t0\t991\t0\nGid:\t0\t0\t0\t0\nGroups:\t\nNoNewPrivs:\t0\nThreads:\t1\n".into(),
        );
        assert_eq!(
            job_processes(&v, 991),
            vec![Finding::JobProcess(77), Finding::JobProcess(78)]
        );
        v.status.insert(79, "garbage".into());
        assert!(job_processes(&v, 991).contains(&Finding::Unreadable("/proc/79/status".into())));
    }

    #[test]
    fn key_users_line_is_a_finding() {
        let mut v = FakeProc {
            key_users: "    0:     5 5/5 3/1000000 50/25000000\n".into(),
            ..FakeProc::default()
        };
        assert!(key_users(&v, 991).is_empty());
        v.key_users.push_str("  991:     1 1/1 1/200 9/20000\n");
        assert_eq!(key_users(&v, 991), vec![Finding::KeyUsers]);
    }

    #[test]
    fn slice_and_jobs_fs_findings() {
        let s = Scratch::new("residue");
        let slice_dir = s.path().join("slice");
        let own = "writ-job-launcher@2.service";
        fs::create_dir_all(slice_dir.join(own)).unwrap();
        fs::create_dir_all(slice_dir.join("writ-job-launcher@1.service")).unwrap();
        fs::write(
            slice_dir.join("writ-job-launcher@1.service/cgroup.events"),
            "populated 0\n",
        )
        .unwrap();
        fs::write(slice_dir.join("cgroup.procs"), "").unwrap();
        assert!(slice(&slice_dir, own).is_empty());
        fs::write(
            slice_dir.join("writ-job-launcher@1.service/cgroup.events"),
            "populated 1\n",
        )
        .unwrap();
        fs::create_dir(slice_dir.join(own).join("job")).unwrap();
        let mut expected = vec![
            Finding::PopulatedInstance("writ-job-launcher@1.service".into()),
            Finding::LeftoverJobCgroup,
        ];
        expected.sort();
        assert_eq!(
            slice(&slice_dir, own),
            expected
                .into_iter()
                .filter(|f| slice(&slice_dir, own).contains(f))
                .collect::<Vec<_>>()
        );
        assert_eq!(slice(&slice_dir, own).len(), 2);

        let jobs = s.path().join("jobs");
        fs::create_dir_all(jobs.join("lost+found")).unwrap();
        assert!(jobs_fs(&jobs).is_empty());
        // A planted writ-job-owned entry or a failed cleanup's job directory.
        fs::create_dir(jobs.join("job-1")).unwrap();
        assert_eq!(jobs_fs(&jobs), vec![Finding::JobsEntry("job-1".into())]);
        assert!(!jobs_fs(&s.path().join("absent")).is_empty());
        let v = FakeProc::default();
        assert_eq!(check_all(&v, 991, &slice_dir, own, &jobs).len(), 3);
    }
}
