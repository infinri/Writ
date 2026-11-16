use super::*;
use crate::fdplan::Access;
use crate::testutil::Scratch;
use std::fs;
use std::os::unix::fs::PermissionsExt;

fn rl() -> Rlimits {
    Rlimits {
        nofile: 64,
        fsize: 1 << 20,
        nproc: 256,
        msgqueue: 4096,
        memlock: 65536,
        sigpending: 64,
    }
}

fn run_plan() -> Plan {
    Plan {
        cgroup_root: "/sys/fs/cgroup".into(),
        unit: "writ.slice/writ-jobs.slice/writ-job-launcher@1.service".into(),
        uid: 991,
        gid: 991,
        rlimits: rl(),
        mode: HelperMode::Run {
            bwrap: "/usr/local/libexec/writ/bwrap".into(),
            args: vec!["--unshare-user".into(), "--".into(), "/work/x".into()],
        },
    }
}

#[test]
fn plan_round_trips_and_parsing_is_strict() {
    let p = run_plan();
    assert_eq!(Plan::parse(&p.to_args()).unwrap(), p);
    let c = Plan {
        mode: HelperMode::Cleanup {
            exe: "/usr/local/libexec/writ/writ-cgroup-exec".into(),
            work: "/var/lib/writ-jobs/job-1/work".into(),
        },
        ..run_plan()
    };
    assert_eq!(Plan::parse(&c.to_args()).unwrap(), c);
    let mut bad = p.to_args();
    bad.swap(1, 3);
    assert!(Plan::parse(&bad).is_err(), "flag order is fixed");
    let mut root = p.to_args();
    root[6] = "0".into(); // --uid 0
    assert!(Plan::parse(&root).is_err());
    let mut rel = p.to_args();
    rel[22] = "bwrap".into(); // --bwrap relative
    assert!(Plan::parse(&rel).is_err());
    let mut unit = p.to_args();
    unit[4] = "../x".into();
    assert!(Plan::parse(&unit).is_err());
    let mut extra = c.to_args();
    extra.push("--x".into());
    assert!(Plan::parse(&extra).is_err());
    assert!(Plan::parse(&["shell".into()]).is_err());
}

/// Records every call; fails at a named call.
struct Rec {
    calls: Vec<String>,
    fail_at: Option<&'static str>,
    cgroup: Vec<String>,
    fds: Vec<i32>,
}

impl Rec {
    fn new(fail_at: Option<&'static str>) -> Rec {
        Rec {
            calls: vec![],
            fail_at,
            cgroup: vec![
                "/writ.slice/writ-jobs.slice/writ-job-launcher@1.service/supervisor".into(),
                "/writ.slice/writ-jobs.slice/writ-job-launcher@1.service/job".into(),
            ],
            fds: vec![0, 1, 2, 3, 4],
        }
    }
    fn hit(&mut self, name: &str) -> io::Result<()> {
        self.calls.push(name.to_string());
        if self.fail_at == Some(name) {
            Err(io::Error::other("injected"))
        } else {
            Ok(())
        }
    }
}

impl Ops for Rec {
    fn threads(&mut self) -> io::Result<u32> {
        self.hit("threads").map(|_| 1)
    }
    fn fds(&mut self) -> io::Result<(Vec<i32>, Option<FdFacts>, Option<FdFacts>)> {
        self.hit("fds")?;
        Ok((
            self.fds.clone(),
            Some(FdFacts {
                fifo: true,
                access: Access::Read,
                ino: 1,
            }),
            Some(FdFacts {
                fifo: true,
                access: Access::Write,
                ino: 2,
            }),
        ))
    }
    fn own_cgroup(&mut self) -> io::Result<String> {
        self.hit("own_cgroup")?;
        Ok(self.cgroup.remove(0))
    }
    fn join_job(&mut self, _root: &Path, _unit: &str) -> io::Result<()> {
        self.hit("join_job")
    }
    fn lower_rlimit(&mut self, r: Resource, _v: u64) -> io::Result<()> {
        self.hit(&format!("rlimit {r:?}"))
    }
    fn oom_score_adj(&mut self, _v: i32) -> io::Result<()> {
        self.hit("oom_score_adj")
    }
    fn clear_groups(&mut self) -> io::Result<()> {
        self.hit("clear_groups")
    }
    fn no_new_privs(&mut self) -> io::Result<()> {
        self.hit("no_new_privs")
    }
    fn exec(&mut self, program: &Path, _args: &[OsString], uid: u32, gid: u32) -> io::Error {
        self.calls
            .push(format!("exec {} {uid} {gid}", program.display()));
        io::Error::other("test: not executed")
    }
}

const ORDER: [&str; 16] = [
    "threads",
    "fds",
    "own_cgroup",
    "join_job",
    "own_cgroup",
    "rlimit Core",
    "rlimit Nofile",
    "rlimit Fsize",
    "rlimit Nproc",
    "rlimit Msgqueue",
    "rlimit Memlock",
    "rlimit Sigpending",
    "oom_score_adj",
    "clear_groups",
    "no_new_privs",
    "exec /usr/local/libexec/writ/bwrap 991 991",
];

#[test]
fn steps_run_in_the_binding_order() {
    let mut r = Rec::new(None);
    let f = execute(&run_plan(), &mut r);
    assert_eq!(f.step, 8);
    assert_eq!(r.calls, ORDER);
}

#[test]
fn any_failure_stops_before_exec() {
    for (name, step) in [
        ("threads", 1),
        ("fds", 2),
        ("join_job", 4),
        ("rlimit Sigpending", 5),
        ("oom_score_adj", 5),
        ("clear_groups", 6),
        ("no_new_privs", 7),
    ] {
        let mut r = Rec::new(Some(name));
        let f = execute(&run_plan(), &mut r);
        assert_eq!(f.step, step, "{name}");
        assert_eq!(
            r.calls.last().map(String::as_str),
            Some(name),
            "stops at {name}"
        );
        assert!(!r.calls.iter().any(|c| c.starts_with("exec")));
    }
}

#[test]
fn wrong_cgroup_or_descriptors_refuse() {
    let mut r = Rec::new(None);
    r.cgroup[0] = "/system.slice/x.service".into();
    assert_eq!(execute(&run_plan(), &mut r).step, 3);
    let mut r = Rec::new(None);
    r.cgroup[1] = "/writ.slice/writ-jobs.slice/writ-job-launcher@1.service/supervisor".into();
    assert_eq!(execute(&run_plan(), &mut r).step, 4);
    let mut r = Rec::new(None);
    r.fds.push(7);
    assert_eq!(execute(&run_plan(), &mut r).step, 2);
    // Cleanup expects exactly 0..2.
    let mut r = Rec::new(None);
    let c = Plan {
        mode: HelperMode::Cleanup {
            exe: "/x/writ-cgroup-exec".into(),
            work: "/j/work".into(),
        },
        ..run_plan()
    };
    assert_eq!(execute(&c, &mut r).step, 2);
    let mut r = Rec::new(None);
    r.fds = vec![0, 1, 2];
    let f = execute(&c, &mut r);
    assert_eq!(f.step, 8);
    assert_eq!(r.calls.last().unwrap(), "exec /x/writ-cgroup-exec 991 991");
}

#[test]
fn cleanup_entry_parsing_and_identity() {
    let (u, g, w) = parse_cleanup_as_job(&[
        "cleanup-as-job".into(),
        "--uid".into(),
        "991".into(),
        "--gid".into(),
        "991".into(),
        "--work".into(),
        "/j/work".into(),
    ])
    .unwrap();
    assert_eq!((u, g, w), (991, 991, PathBuf::from("/j/work")));
    assert!(parse_cleanup_as_job(&["cleanup-as-job".into()]).is_err());
    let ok = crate::procfs::tests::status_text(991, 991, "", 1);
    assert!(check_self_identity(&ok, 991, 991).is_ok());
    let grp = crate::procfs::tests::status_text(991, 991, "990", 1);
    assert!(check_self_identity(&grp, 991, 991).is_err());
    let root = crate::procfs::tests::status_text(0, 0, "", 1);
    assert!(check_self_identity(&root, 991, 991).is_err());
    assert!(wait_go(&mut &[1u8][..]));
    assert!(!wait_go(&mut &[][..]), "EOF aborts the cleanup");
}

#[test]
fn remove_contents_stays_beneath_and_restores_permissions() {
    let s = Scratch::new("cleanup");
    let outside = s.path().join("outside");
    fs::create_dir(&outside).unwrap();
    fs::write(outside.join("keep"), b"x").unwrap();
    let work = s.path().join("work");
    fs::create_dir_all(work.join("a/b/c")).unwrap();
    fs::write(work.join("a/b/c/f"), b"1").unwrap();
    fs::write(work.join("top"), b"2").unwrap();
    std::os::unix::fs::symlink(&outside, work.join("a/escape")).unwrap();
    std::os::unix::fs::symlink(outside.join("keep"), work.join("keep-link")).unwrap();
    // The job removed its own permissions on a directory.
    fs::set_permissions(work.join("a/b"), fs::Permissions::from_mode(0o000)).unwrap();
    let fd = open_work(&work).unwrap();
    let n = remove_contents(fd, 100).unwrap();
    assert_eq!(n, 7);
    assert_eq!(fs::read_dir(&work).unwrap().count(), 0);
    assert!(
        outside.join("keep").exists(),
        "symlink targets are untouched"
    );
    // Bounded.
    fs::create_dir_all(work.join("x/y")).unwrap();
    fs::write(work.join("x/y/z"), b"").unwrap();
    let fd = open_work(&work).unwrap();
    assert!(remove_contents(fd, 1).is_err());
}
