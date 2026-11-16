//! Verification while the gate is closed (J-11.4 step 6; M2, N5) and the status pipe.
//!
//! The launcher reads bubblewrap's `--json-status-fd` output (bounded, deadline) until the first
//! complete JSON object reports `child-pid` (**verify in VM**: bubblewrap 0.9.0 emits it before
//! blocking on `--block-fd`). It then reads `<unit>/job/cgroup.procs` and requires, for every
//! listed pid, as seen from the initial user namespace: real, effective, saved and filesystem
//! uid and gid equal to `writ-job`'s kernel ids, no supplementary groups, `NoNewPrivs: 1`, and
//! `/proc/<pid>/exe` resolving to the pinned image (same device and inode). The listed set must
//! be exactly the direct child and the reported sandbox child when
//! `pre_gate_processes == 2` (the pre-gate count is on the verify-in-VM list, N5). After
//! `started` the status pipe is only drained, never trusted (N4).

use crate::procfs::{parse_status, ProcView};
use writ_kernel::json;

/// Accumulates status-pipe bytes until the first complete JSON object.
#[derive(Debug)]
pub struct StatusReader {
    buf: Vec<u8>,
    max: usize,
}

impl StatusReader {
    /// A reader that refuses more than `max` bytes before the object completes.
    pub fn new(max: usize) -> StatusReader {
        StatusReader {
            buf: Vec::new(),
            max,
        }
    }

    /// Feed bytes; `Ok(Some(pid))` once the first object is complete and names a positive
    /// `child-pid`.
    pub fn feed(&mut self, bytes: &[u8]) -> Result<Option<i32>, String> {
        self.buf.extend_from_slice(bytes);
        let mut depth = 0usize;
        let mut start = None;
        for (i, &b) in self.buf.iter().enumerate() {
            match b {
                b'{' => {
                    if depth == 0 {
                        start = Some(i);
                    }
                    depth += 1;
                }
                b'}' => {
                    depth = depth
                        .checked_sub(1)
                        .ok_or_else(|| "unbalanced status output".to_string())?;
                    if depth == 0 {
                        let s = start.ok_or_else(|| "unbalanced status output".to_string())?;
                        return child_pid(&self.buf[s..=i]).map(Some);
                    }
                }
                b'"' | b' ' | b'\n' | b'\t' | b':' | b',' | b'-' | b'_' => {}
                b if b.is_ascii_alphanumeric() => {}
                other => return Err(format!("unexpected status byte {other:#x}")),
            }
            if depth == 0 && start.is_none() && !b.is_ascii_whitespace() {
                return Err("status output does not start with an object".into());
            }
        }
        if self.buf.len() > self.max {
            return Err("status output exceeds its bound".into());
        }
        Ok(None)
    }
}

fn child_pid(object: &[u8]) -> Result<i32, String> {
    let v = json::parse(object).map_err(|e| format!("status json: {}", e.code()))?;
    let n = v
        .get("child-pid")
        .and_then(json::Value::as_int)
        .ok_or_else(|| "status object without child-pid".to_string())?;
    i32::try_from(n)
        .ok()
        .filter(|p| *p > 0)
        .ok_or_else(|| "child-pid out of range".to_string())
}

/// What every process in `<unit>/job` must be before the gate opens.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Expect {
    /// `writ-job` kernel uid.
    pub uid: u32,
    /// `writ-job` kernel gid.
    pub gid: u32,
    /// Device and inode of the pinned image (`bwrap` or `writ-cgroup-exec` for cleanup).
    pub image: (u64, u64),
    /// Exact number of processes expected.
    pub count: usize,
}

/// Check one pid's identity.
pub fn check_pid(view: &dyn ProcView, pid: i32, e: &Expect) -> Result<(), String> {
    let text = view
        .status(pid)
        .map_err(|err| format!("pid {pid}: status {err}"))?;
    let st = parse_status(&text).ok_or_else(|| format!("pid {pid}: status unparsable"))?;
    if st.uid != [e.uid; 4] {
        return Err(format!("pid {pid}: uids {:?}", st.uid));
    }
    if st.gid != [e.gid; 4] {
        return Err(format!("pid {pid}: gids {:?}", st.gid));
    }
    if !st.groups.is_empty() {
        return Err(format!("pid {pid}: supplementary groups {:?}", st.groups));
    }
    if !st.no_new_privs {
        return Err(format!("pid {pid}: NoNewPrivs 0"));
    }
    let exe = view
        .exe_identity(pid)
        .map_err(|err| format!("pid {pid}: exe {err}"))?;
    if exe != e.image {
        return Err(format!("pid {pid}: not the pinned image"));
    }
    Ok(())
}

/// Verify the pre-gate set: exactly `e.count` processes, including the direct child and (when
/// given) the sandbox child, every one passing [`check_pid`].
pub fn verify_job_set(
    view: &dyn ProcView,
    procs: &[i32],
    direct_child: i32,
    sandbox_child: Option<i32>,
    e: &Expect,
) -> Result<(), String> {
    let mut set = procs.to_vec();
    set.sort_unstable();
    set.dedup();
    if set.len() != procs.len() {
        return Err("duplicate pids in cgroup.procs".into());
    }
    if set.len() != e.count {
        return Err(format!(
            "{} processes in the job cgroup, expected {}",
            set.len(),
            e.count
        ));
    }
    if !set.contains(&direct_child) {
        return Err("the direct child is not in the job cgroup".into());
    }
    if let Some(c) = sandbox_child {
        if !set.contains(&c) {
            return Err("the reported sandbox child is not in the job cgroup".into());
        }
    }
    for pid in set {
        check_pid(view, pid, e)?;
    }
    Ok(())
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;
    use crate::procfs::tests::{status_text, FakeProc};

    const BWRAP_STATUS: &[u8] = b"{ \"child-pid\": 4242,\n    \"cgroup-namespace\": 4026532300,\n    \"ipc-namespace\": 4026532298,\n    \"mnt-namespace\": 4026532296,\n    \"net-namespace\": 4026532301,\n    \"pid-namespace\": 4026532299,\n    \"uts-namespace\": 4026532297\n}\n";

    #[test]
    fn status_reader_gets_child_pid_across_chunks() {
        let mut r = StatusReader::new(4096);
        let (a, b) = BWRAP_STATUS.split_at(20);
        assert_eq!(r.feed(a).unwrap(), None);
        assert_eq!(r.feed(b).unwrap(), Some(4242));
    }

    #[test]
    fn status_reader_refuses_garbage_and_overflow() {
        assert!(StatusReader::new(4096).feed(b"x{}").is_err());
        assert!(StatusReader::new(4096)
            .feed(b"{ \"exit-code\": 1 }\n")
            .is_err());
        assert!(StatusReader::new(4096)
            .feed(b"{ \"child-pid\": 0 }")
            .is_err());
        assert!(StatusReader::new(4096).feed(b"}").is_err());
        assert!(StatusReader::new(4096)
            .feed(b"{ \"a\": \"\\u0000\" }")
            .is_err());
        let mut r = StatusReader::new(8);
        assert!(r.feed(b"{ \"child-pid\"").is_err());
    }

    fn view(groups: &str, nnp: u8, uid: u32) -> FakeProc {
        let mut v = FakeProc::default();
        for pid in [100, 101] {
            v.status.insert(pid, status_text(uid, 991, groups, nnp));
            v.exe.insert(pid, (8, 77));
        }
        v
    }

    const E: Expect = Expect {
        uid: 991,
        gid: 991,
        image: (8, 77),
        count: 2,
    };

    #[test]
    fn verified_set_passes() {
        let v = view("", 1, 991);
        assert!(verify_job_set(&v, &[100, 101], 100, Some(101), &E).is_ok());
    }

    #[test]
    fn identity_failures_are_detected() {
        // setgroups skipped (P-JOB-IDENTITY control 1).
        let v = view("990", 1, 991);
        assert!(verify_job_set(&v, &[100, 101], 100, Some(101), &E)
            .unwrap_err()
            .contains("supplementary"));
        // no_new_privs skipped.
        let v = view("", 0, 991);
        assert!(verify_job_set(&v, &[100, 101], 100, Some(101), &E)
            .unwrap_err()
            .contains("NoNewPrivs"));
        // Still root.
        let v = view("", 1, 0);
        assert!(verify_job_set(&v, &[100, 101], 100, Some(101), &E)
            .unwrap_err()
            .contains("uids"));
        // Saved uid differs.
        let mut v = view("", 1, 991);
        v.status.insert(
            101,
            "Uid:\t991\t991\t0\t991\nGid:\t991\t991\t991\t991\nGroups:\t\nNoNewPrivs:\t1\nThreads:\t1\n"
                .into(),
        );
        assert!(verify_job_set(&v, &[100, 101], 100, Some(101), &E).is_err());
        // Another image (a copy of bubblewrap or another binary).
        let mut v = view("", 1, 991);
        v.exe.insert(101, (8, 78));
        assert!(verify_job_set(&v, &[100, 101], 100, Some(101), &E)
            .unwrap_err()
            .contains("pinned image"));
    }

    #[test]
    fn set_shape_is_exact() {
        let mut v = view("", 1, 991);
        assert!(verify_job_set(&v, &[100], 100, Some(101), &E).is_err());
        v.status.insert(102, status_text(991, 991, "", 1));
        v.exe.insert(102, (8, 77));
        assert!(verify_job_set(&v, &[100, 101, 102], 100, Some(101), &E).is_err());
        assert!(verify_job_set(&v, &[100, 102], 100, Some(101), &E).is_err());
        assert!(verify_job_set(&v, &[101, 102], 100, Some(101), &E).is_err());
        assert!(verify_job_set(&v, &[100, 100], 100, Some(101), &E).is_err());
        // A pid that vanished cannot be verified.
        assert!(verify_job_set(&v, &[100, 103], 100, None, &E).is_err());
    }
}
