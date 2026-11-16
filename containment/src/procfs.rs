//! Reading `/proc` as seen from the initial user namespace: process status (uids, gids,
//! supplementary groups, `NoNewPrivs`, thread count), executable identity, `/proc/key-users`
//! and descriptor flags. Behind [`ProcView`] so identity and residue checks are tested with
//! injected views.

use std::io;
use std::os::unix::fs::MetadataExt;
use std::path::PathBuf;

/// The parts of `/proc/<pid>/status` the checks use.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProcStatus {
    /// Real, effective, saved and filesystem uid.
    pub uid: [u32; 4],
    /// Real, effective, saved and filesystem gid.
    pub gid: [u32; 4],
    /// Supplementary groups.
    pub groups: Vec<u32>,
    /// `NoNewPrivs`.
    pub no_new_privs: bool,
    /// `Threads`.
    pub threads: u32,
}

fn four(v: &str) -> Option<[u32; 4]> {
    let mut it = v.split_whitespace().map(str::parse::<u32>);
    let out = [
        it.next()?.ok()?,
        it.next()?.ok()?,
        it.next()?.ok()?,
        it.next()?.ok()?,
    ];
    it.next().is_none().then_some(out)
}

/// Parse `/proc/<pid>/status` text; `None` when a required field is absent or malformed.
pub fn parse_status(text: &str) -> Option<ProcStatus> {
    let field = |name: &str| {
        text.lines()
            .find_map(|l| l.strip_prefix(name).and_then(|r| r.strip_prefix(':')))
            .map(str::trim)
    };
    let groups = field("Groups")?
        .split_whitespace()
        .map(str::parse::<u32>)
        .collect::<Result<Vec<_>, _>>()
        .ok()?;
    Some(ProcStatus {
        uid: four(field("Uid")?)?,
        gid: four(field("Gid")?)?,
        groups,
        no_new_privs: match field("NoNewPrivs")? {
            "0" => false,
            "1" => true,
            _ => return None,
        },
        threads: field("Threads")?.parse().ok()?,
    })
}

/// Whether `/proc/key-users` text has a line for `uid`.
pub fn key_users_has(text: &str, uid: u32) -> bool {
    text.lines().any(|l| {
        l.trim_start()
            .split_once(':')
            .and_then(|(u, _)| u.trim().parse::<u32>().ok())
            == Some(uid)
    })
}

/// The `flags:` value (octal) of `/proc/<pid>/fdinfo/<fd>` text.
pub fn fdinfo_flags(text: &str) -> Option<u32> {
    let v = text.lines().find_map(|l| l.strip_prefix("flags:"))?.trim();
    u32::from_str_radix(v, 8).ok()
}

/// A view of `/proc`.
pub trait ProcView {
    /// Every numeric entry (pid).
    fn pids(&self) -> io::Result<Vec<i32>>;
    /// `/proc/<pid>/status` text.
    fn status(&self, pid: i32) -> io::Result<String>;
    /// Device and inode of the file `/proc/<pid>/exe` resolves to.
    fn exe_identity(&self, pid: i32) -> io::Result<(u64, u64)>;
    /// `/proc/key-users` text.
    fn key_users(&self) -> io::Result<String>;
}

/// The host `/proc` (or another procfs mount).
#[derive(Clone, Debug)]
pub struct HostProc {
    /// Mount point.
    pub root: PathBuf,
}

impl ProcView for HostProc {
    fn pids(&self) -> io::Result<Vec<i32>> {
        let mut out = Vec::new();
        for e in std::fs::read_dir(&self.root)? {
            if let Some(pid) = e?.file_name().to_str().and_then(|n| n.parse::<i32>().ok()) {
                out.push(pid);
            }
        }
        out.sort_unstable();
        Ok(out)
    }

    fn status(&self, pid: i32) -> io::Result<String> {
        std::fs::read_to_string(self.root.join(pid.to_string()).join("status"))
    }

    fn exe_identity(&self, pid: i32) -> io::Result<(u64, u64)> {
        let m = std::fs::metadata(self.root.join(pid.to_string()).join("exe"))?;
        Ok((m.dev(), m.ino()))
    }

    fn key_users(&self) -> io::Result<String> {
        std::fs::read_to_string(self.root.join("key-users"))
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
pub(crate) mod tests {
    use super::*;
    use std::collections::BTreeMap;

    /// An injected `/proc`.
    #[derive(Default)]
    pub(crate) struct FakeProc {
        pub status: BTreeMap<i32, String>,
        pub exe: BTreeMap<i32, (u64, u64)>,
        pub key_users: String,
    }

    impl ProcView for FakeProc {
        fn pids(&self) -> io::Result<Vec<i32>> {
            Ok(self.status.keys().copied().collect())
        }
        fn status(&self, pid: i32) -> io::Result<String> {
            self.status
                .get(&pid)
                .cloned()
                .ok_or_else(|| io::Error::from(io::ErrorKind::NotFound))
        }
        fn exe_identity(&self, pid: i32) -> io::Result<(u64, u64)> {
            self.exe
                .get(&pid)
                .copied()
                .ok_or_else(|| io::Error::from(io::ErrorKind::NotFound))
        }
        fn key_users(&self) -> io::Result<String> {
            Ok(self.key_users.clone())
        }
    }

    pub(crate) fn status_text(uid: u32, gid: u32, groups: &str, nnp: u8) -> String {
        format!(
            "Name:\tbwrap\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\nGid:\t{gid}\t{gid}\t{gid}\t{gid}\nGroups:\t{groups}\nNoNewPrivs:\t{nnp}\nThreads:\t1\n"
        )
    }

    #[test]
    fn parses_status_fields() {
        let s = parse_status(&status_text(991, 991, "", 1)).unwrap();
        assert_eq!(s.uid, [991; 4]);
        assert!(s.groups.is_empty() && s.no_new_privs && s.threads == 1);
        let g = parse_status(&status_text(0, 0, "4 27 ", 0)).unwrap();
        assert_eq!(g.groups, vec![4, 27]);
        assert!(!g.no_new_privs);
        assert!(parse_status("Uid:\t1\t1\t1\n").is_none());
        assert!(parse_status(&status_text(1, 1, "x", 0)).is_none());
    }

    #[test]
    fn reads_own_status_from_host_proc() {
        let p = HostProc {
            root: "/proc".into(),
        };
        let pid = i32::try_from(std::process::id()).unwrap();
        let s = parse_status(&p.status(pid).unwrap()).unwrap();
        assert_eq!(s.uid[0], rustix::process::getuid().as_raw());
        assert!(p.exe_identity(pid).is_ok());
        assert!(p.pids().unwrap().contains(&pid));
    }

    #[test]
    fn key_users_and_fdinfo() {
        let ku = "    0:   101 100/100 97/1000000 2011/25000000\n  991:     1 1/1 1/200 9/20000\n";
        assert!(key_users_has(ku, 991));
        assert!(key_users_has(ku, 0));
        assert!(!key_users_has(ku, 99));
        assert_eq!(
            fdinfo_flags("pos:\t0\nflags:\t02000001\nmnt_id:\t15\n"),
            Some(0o2000001)
        );
        assert_eq!(fdinfo_flags("pos:\t0\n"), None);
    }
}
