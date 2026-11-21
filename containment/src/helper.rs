//! `writ-cgroup-exec` (J-0, J-11.4 step 5, J-11.5 cleanup; N8). Spawned by the launcher as
//! root with a cleared environment and a launcher-built argument vector; it accepts nothing
//! else. In order, exiting non-zero at the first failure and before executing anything:
//!
//! 1. single-threaded (`Threads: 1`);
//! 2. descriptor set exactly `{0, 1, 2, 3, 4}` (run) or `{0, 1, 2}` (cleanup), with 3 a
//!    read-only and 4 a write-only pipe end ([`crate::fdplan::check_helper_fds`]);
//! 3. its own cgroup is `<unit>/supervisor` (inherited from the launcher);
//! 4. writes its pid to `<unit>/job/cgroup.procs` (opened beneath its unit) and verifies
//!    `/proc/self/cgroup` reads `<unit>/job`;
//! 5. rlimits, lowering only: `RLIMIT_CORE = 0`, `NOFILE`, `FSIZE`, `NPROC`, `MSGQUEUE`,
//!    `MEMLOCK`, `SIGPENDING`; then `oom_score_adj = 500`;
//! 6. `setgroups(0)` explicitly (the standard library also clears groups when executing as root
//!    with a uid set, but ignores the error; **verify** against the pinned toolchain's `std`);
//! 7. `PR_SET_NO_NEW_PRIVS`;
//! 8. `CommandExt::exec` with `uid(writ-job)` and `gid(writ-job)`: the private bubblewrap with
//!    the fixed profile (run), or **itself** with the `cleanup-as-job` entry (cleanup, N8).
//!
//! The `cleanup-as-job` entry runs as `writ-job`: it checks its own identity and descriptors,
//! reports `ready` on stdout, waits for the launcher's go byte on stdin (EOF aborts: unlike
//! bubblewrap's `--block-fd`, this code treats EOF as "do nothing"), then removes the contents
//! of `work/` beneath a descriptor (no symlink following, no mount crossing).

use crate::fdplan::{self, FdFacts, GATE_FD, STATUS_FD};
use rustix::fs::{
    fchmod, openat2, statx, unlinkat, AtFlags, Dir, FileType, Mode, OFlags, ResolveFlags,
    StatxFlags, CWD,
};
use rustix::process::Resource;
use std::ffi::{CString, OsString};
use std::io::{self, Read, Write};
use std::os::fd::{AsFd, OwnedFd};
use std::path::{Path, PathBuf};
use std::rc::Rc;

/// `oom_score_adj` given to job processes (the launcher has -500; L5).
pub const OOM_SCORE_ADJ: i32 = 500;

/// What the helper does.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum HelperMode {
    /// Execute the private bubblewrap with `args`.
    Run {
        /// Private bubblewrap path.
        bwrap: PathBuf,
        /// bubblewrap arguments (without argv[0]).
        args: Vec<OsString>,
    },
    /// Execute itself as `writ-job` to clean `work`.
    Cleanup {
        /// The pinned `writ-cgroup-exec` path.
        exe: PathBuf,
        /// The job's `work/` directory.
        work: PathBuf,
    },
}

/// The rlimits the helper lowers (besides `RLIMIT_CORE = 0`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Rlimits {
    /// `RLIMIT_NOFILE`.
    pub nofile: u64,
    /// `RLIMIT_FSIZE`.
    pub fsize: u64,
    /// `RLIMIT_NPROC`.
    pub nproc: u64,
    /// `RLIMIT_MSGQUEUE`.
    pub msgqueue: u64,
    /// `RLIMIT_MEMLOCK`.
    pub memlock: u64,
    /// `RLIMIT_SIGPENDING`.
    pub sigpending: u64,
}

impl Rlimits {
    /// In the order they are set.
    pub fn ordered(&self) -> [(Resource, u64); 7] {
        [
            (Resource::Core, 0),
            (Resource::Nofile, self.nofile),
            (Resource::Fsize, self.fsize),
            (Resource::Nproc, self.nproc),
            (Resource::Msgqueue, self.msgqueue),
            (Resource::Memlock, self.memlock),
            (Resource::Sigpending, self.sigpending),
        ]
    }
}

/// The helper's validated plan.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Plan {
    /// cgroup2 mount.
    pub cgroup_root: PathBuf,
    /// Unit path relative to the mount.
    pub unit: String,
    /// `writ-job` uid.
    pub uid: u32,
    /// `writ-job` gid.
    pub gid: u32,
    /// Limits.
    pub rlimits: Rlimits,
    /// What to execute.
    pub mode: HelperMode,
}

const RUN_FLAGS: [&str; 11] = [
    "--cgroup-root",
    "--unit",
    "--uid",
    "--gid",
    "--nofile",
    "--fsize",
    "--nproc",
    "--msgqueue",
    "--memlock",
    "--sigpending",
    "--bwrap",
];

const CLEANUP_FLAGS: [&str; 12] = [
    "--cgroup-root",
    "--unit",
    "--uid",
    "--gid",
    "--nofile",
    "--fsize",
    "--nproc",
    "--msgqueue",
    "--memlock",
    "--sigpending",
    "--self",
    "--work",
];

impl Plan {
    /// The argument vector (without argv[0]) the launcher passes.
    pub fn to_args(&self) -> Vec<OsString> {
        let r = &self.rlimits;
        let common: [String; 10] = [
            self.cgroup_root.display().to_string(),
            self.unit.clone(),
            self.uid.to_string(),
            self.gid.to_string(),
            r.nofile.to_string(),
            r.fsize.to_string(),
            r.nproc.to_string(),
            r.msgqueue.to_string(),
            r.memlock.to_string(),
            r.sigpending.to_string(),
        ];
        let mut a: Vec<OsString> = Vec::new();
        match &self.mode {
            HelperMode::Run { bwrap, args } => {
                a.push("run".into());
                for (f, v) in RUN_FLAGS.iter().zip(common.iter()) {
                    a.extend([OsString::from(f), OsString::from(v)]);
                }
                a.extend([
                    "--bwrap".into(),
                    bwrap.clone().into_os_string(),
                    "--".into(),
                ]);
                a.extend(args.iter().cloned());
            }
            HelperMode::Cleanup { exe, work } => {
                a.push("cleanup".into());
                for (f, v) in CLEANUP_FLAGS.iter().zip(common.iter()) {
                    a.extend([OsString::from(f), OsString::from(v)]);
                }
                a.extend([
                    "--self".into(),
                    exe.clone().into_os_string(),
                    "--work".into(),
                    work.clone().into_os_string(),
                ]);
            }
        }
        a
    }

    /// Parse the launcher-built vector strictly (fixed flag order, absolute paths, positive
    /// limits, non-root ids).
    pub fn parse(args: &[OsString]) -> Result<Plan, String> {
        let text: Vec<&str> = args
            .iter()
            .map(|a| a.to_str().ok_or("non-UTF-8 argument"))
            .collect::<Result<_, _>>()?;
        let (mode, rest) = text.split_first().ok_or("no mode")?;
        let flags: &[&str] = match *mode {
            "run" => &RUN_FLAGS,
            "cleanup" => &CLEANUP_FLAGS,
            _ => return Err("unknown mode".into()),
        };
        if rest.len() < flags.len() * 2 {
            return Err("missing arguments".into());
        }
        let mut vals = Vec::new();
        for (i, f) in flags.iter().enumerate() {
            if rest[2 * i] != *f {
                return Err(format!("expected {f}"));
            }
            vals.push(rest[2 * i + 1]);
        }
        let tail = &rest[flags.len() * 2..];
        let num = |i: usize| -> Result<u64, String> {
            vals[i]
                .parse::<u64>()
                .ok()
                .filter(|n| *n > 0)
                .ok_or_else(|| format!("{}: positive integer", flags[i]))
        };
        let id = |i: usize| -> Result<u32, String> {
            let n = u32::try_from(num(i)?).map_err(|_| format!("{}: range", flags[i]))?;
            if n == crate::config::OVERFLOW_ID {
                return Err(format!("{}: overflow id", flags[i]));
            }
            Ok(n)
        };
        let abs = |i: usize| -> Result<PathBuf, String> {
            let p = Path::new(vals[i]);
            if p.is_absolute() && !vals[i].contains("/../") && !vals[i].ends_with("/..") {
                Ok(p.to_path_buf())
            } else {
                Err(format!("{}: absolute path required", flags[i]))
            }
        };
        let unit = vals[1];
        if unit.is_empty()
            || unit.starts_with('/')
            || unit
                .split('/')
                .any(|c| c.is_empty() || c == "." || c == "..")
        {
            return Err("--unit: relative cgroup path".into());
        }
        let mode = match *mode {
            "run" => {
                let (sep, bargs) = tail.split_first().ok_or("missing --")?;
                if *sep != "--" {
                    return Err("missing --".into());
                }
                HelperMode::Run {
                    bwrap: abs(10)?,
                    args: bargs.iter().map(OsString::from).collect(),
                }
            }
            _ => {
                if !tail.is_empty() {
                    return Err("unexpected arguments".into());
                }
                HelperMode::Cleanup {
                    exe: abs(10)?,
                    work: abs(11)?,
                }
            }
        };
        Ok(Plan {
            cgroup_root: abs(0)?,
            unit: unit.to_string(),
            uid: id(2)?,
            gid: id(3)?,
            rlimits: Rlimits {
                nofile: num(4)?,
                fsize: num(5)?,
                nproc: num(6)?,
                msgqueue: num(7)?,
                memlock: num(8)?,
                sigpending: num(9)?,
            },
            mode,
        })
    }
}

/// The helper's privileged operations (real ones in [`RealOps`]; tests record the order).
pub trait Ops {
    /// `Threads` of this process.
    fn threads(&mut self) -> io::Result<u32>;
    /// Open descriptors (without the listing's own) and facts of 3 and 4 when open.
    fn fds(&mut self) -> io::Result<(Vec<i32>, Option<FdFacts>, Option<FdFacts>)>;
    /// This process's cgroup-v2 path (`/proc/self/cgroup`, `0::` line, leading `/`).
    fn own_cgroup(&mut self) -> io::Result<String>;
    /// Write this pid to `<root>/<unit>/job/cgroup.procs`, opened beneath the unit.
    fn join_job(&mut self, root: &Path, unit: &str) -> io::Result<()>;
    /// Lower `r` to `v` (never raise).
    fn lower_rlimit(&mut self, r: Resource, v: u64) -> io::Result<()>;
    /// Write `oom_score_adj`.
    fn oom_score_adj(&mut self, v: i32) -> io::Result<()>;
    /// `setgroups(0)`.
    fn clear_groups(&mut self) -> io::Result<()>;
    /// `PR_SET_NO_NEW_PRIVS`.
    fn no_new_privs(&mut self) -> io::Result<()>;
    /// Execute `program` with `args` as `uid`/`gid` with a cleared environment; returns only
    /// on failure.
    fn exec(&mut self, program: &Path, args: &[OsString], uid: u32, gid: u32) -> io::Error;
}

/// A helper failure: the step and why (the helper exits with `70 + step`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Failed {
    /// Step number (1-based, as in the module documentation).
    pub step: u8,
    /// Why.
    pub why: String,
}

fn fail<T>(step: u8, why: impl std::fmt::Display) -> Result<T, Failed> {
    Err(Failed {
        step,
        why: why.to_string(),
    })
}

/// Run the plan; returns only on failure (success executes the next image).
pub fn execute(plan: &Plan, ops: &mut dyn Ops) -> Failed {
    match steps(plan, ops) {
        Ok(()) => Failed {
            step: 0,
            why: "exec returned".into(),
        },
        Err(f) => f,
    }
}

fn steps(plan: &Plan, ops: &mut dyn Ops) -> Result<(), Failed> {
    match ops.threads() {
        Ok(1) => {}
        Ok(n) => return fail(1, format!("{n} threads")),
        Err(e) => return fail(1, e),
    }
    let (listed, g, st) = ops.fds().or_else(|e| fail(2, e))?;
    let expected: &[i32] = match plan.mode {
        HelperMode::Run { .. } => &[0, 1, 2, GATE_FD, STATUS_FD],
        HelperMode::Cleanup { .. } => &[0, 1, 2],
    };
    fdplan::check_helper_fds(&listed, expected, g.as_ref(), st.as_ref()).or_else(|e| fail(2, e))?;
    let supervisor = format!("/{}/supervisor", plan.unit);
    match ops.own_cgroup() {
        Ok(c) if c == supervisor => {}
        Ok(c) => return fail(3, format!("started in {c}, not {supervisor}")),
        Err(e) => return fail(3, e),
    }
    ops.join_job(&plan.cgroup_root, &plan.unit)
        .or_else(|e| fail(4, e))?;
    let job = format!("/{}/job", plan.unit);
    match ops.own_cgroup() {
        Ok(c) if c == job => {}
        Ok(c) => return fail(4, format!("in {c} after joining, not {job}")),
        Err(e) => return fail(4, e),
    }
    for (r, v) in plan.rlimits.ordered() {
        ops.lower_rlimit(r, v)
            .or_else(|e| fail(5, format!("{r:?}: {e}")))?;
    }
    ops.oom_score_adj(OOM_SCORE_ADJ).or_else(|e| fail(5, e))?;
    ops.clear_groups().or_else(|e| fail(6, e))?;
    ops.no_new_privs().or_else(|e| fail(7, e))?;
    let (program, args): (&Path, Vec<OsString>) = match &plan.mode {
        HelperMode::Run { bwrap, args } => (bwrap, args.clone()),
        HelperMode::Cleanup { exe, work } => (
            exe,
            vec![
                "cleanup-as-job".into(),
                "--uid".into(),
                plan.uid.to_string().into(),
                "--gid".into(),
                plan.gid.to_string().into(),
                "--work".into(),
                work.clone().into_os_string(),
            ],
        ),
    };
    let e = ops.exec(program, &args, plan.uid, plan.gid);
    fail(8, e)
}

/// The real operations.
pub struct RealOps {
    /// procfs mount.
    pub proc_root: PathBuf,
}

/// The `0::` path of a `/proc/<pid>/cgroup` text.
pub fn cgroup_line(text: &str) -> Option<String> {
    text.lines()
        .find_map(|l| l.strip_prefix("0::"))
        .map(str::to_string)
}

impl Ops for RealOps {
    fn threads(&mut self) -> io::Result<u32> {
        let t = std::fs::read_to_string(self.proc_root.join("self/status"))?;
        crate::procfs::parse_status(&t)
            .map(|s| s.threads)
            .ok_or_else(|| io::Error::other("status"))
    }

    fn fds(&mut self) -> io::Result<(Vec<i32>, Option<FdFacts>, Option<FdFacts>)> {
        let listed = fdplan::list_own_fds(&self.proc_root)?;
        let g = listed
            .contains(&GATE_FD)
            .then(|| fdplan::observe_fd(&self.proc_root, GATE_FD))
            .transpose()?;
        let s = listed
            .contains(&STATUS_FD)
            .then(|| fdplan::observe_fd(&self.proc_root, STATUS_FD))
            .transpose()?;
        Ok((listed, g, s))
    }

    fn own_cgroup(&mut self) -> io::Result<String> {
        let t = std::fs::read_to_string(self.proc_root.join("self/cgroup"))?;
        cgroup_line(&t).ok_or_else(|| io::Error::other("no cgroup v2 line"))
    }

    fn join_job(&mut self, root: &Path, unit: &str) -> io::Result<()> {
        crate::cgroup::Cgroups::open_relative(root, unit)?.join_job()
    }

    fn lower_rlimit(&mut self, r: Resource, v: u64) -> io::Result<()> {
        let cur = rustix::process::getrlimit(r);
        let lower = |x: Option<u64>| Some(x.map_or(v, |c| c.min(v)));
        let new = rustix::process::Rlimit {
            current: lower(cur.current),
            maximum: lower(cur.maximum),
        };
        let new = rustix::process::Rlimit {
            current: match (new.current, new.maximum) {
                (Some(c), Some(m)) => Some(c.min(m)),
                (c, _) => c,
            },
            maximum: new.maximum,
        };
        rustix::process::setrlimit(r, new)?;
        Ok(())
    }

    fn oom_score_adj(&mut self, v: i32) -> io::Result<()> {
        std::fs::OpenOptions::new()
            .write(true)
            .open(self.proc_root.join("self/oom_score_adj"))?
            .write_all(v.to_string().as_bytes())
    }

    fn clear_groups(&mut self) -> io::Result<()> {
        #[cfg(feature = "test-faults")]
        if fault("skip-setgroups") {
            return Ok(());
        }
        rustix::thread::set_thread_groups(&[])?;
        Ok(())
    }

    fn no_new_privs(&mut self) -> io::Result<()> {
        #[cfg(feature = "test-faults")]
        if fault("skip-no-new-privs") {
            return Ok(());
        }
        rustix::thread::set_no_new_privs(true)?;
        Ok(())
    }

    fn exec(&mut self, program: &Path, args: &[OsString], uid: u32, gid: u32) -> io::Error {
        use std::os::unix::process::CommandExt;
        let mut c = std::process::Command::new(program);
        c.args(args).env_clear();
        #[cfg(feature = "test-faults")]
        if fault("skip-setgroups") {
            // Change ids without the standard library's setgroups(0), keeping root's groups.
            if let Err(e) = rustix::thread::set_thread_res_gid(
                rustix::fs::Gid::from_raw(gid),
                rustix::fs::Gid::from_raw(gid),
                rustix::fs::Gid::from_raw(gid),
            ) {
                return e.into();
            }
            if let Err(e) = rustix::thread::set_thread_res_uid(
                rustix::fs::Uid::from_raw(uid),
                rustix::fs::Uid::from_raw(uid),
                rustix::fs::Uid::from_raw(uid),
            ) {
                return e.into();
            }
            return c.exec();
        }
        c.uid(uid).gid(gid).exec()
    }
}

/// Whether the test-only fault `name` is requested (feature `test-faults` only).
#[cfg(feature = "test-faults")]
pub fn fault(name: &str) -> bool {
    std::env::var("WRIT_TEST_FAULT").is_ok_and(|v| v == name)
}

// ---------------------------------------------------------------------------------------------
// cleanup-as-job
// ---------------------------------------------------------------------------------------------

/// Parse `cleanup-as-job --uid N --gid N --work <abs>`.
pub fn parse_cleanup_as_job(args: &[OsString]) -> Result<(u32, u32, PathBuf), String> {
    let t: Vec<&str> = args
        .iter()
        .map(|a| a.to_str().ok_or("non-UTF-8 argument"))
        .collect::<Result<_, _>>()?;
    match t.as_slice() {
        ["cleanup-as-job", "--uid", u, "--gid", g, "--work", w] => {
            let u: u32 = u.parse().map_err(|_| "uid")?;
            let g: u32 = g.parse().map_err(|_| "gid")?;
            if u == 0 || g == 0 || !w.starts_with('/') || w.contains("/../") {
                return Err("ids or path".into());
            }
            Ok((u, g, PathBuf::from(w)))
        }
        _ => Err("usage".into()),
    }
}

/// The cleanup entry's own identity check: every uid `uid`, every gid `gid`, no groups,
/// `NoNewPrivs`.
pub fn check_self_identity(status: &str, uid: u32, gid: u32) -> Result<(), String> {
    let s = crate::procfs::parse_status(status).ok_or("status")?;
    if s.uid != [uid; 4] || s.gid != [gid; 4] || !s.groups.is_empty() || !s.no_new_privs {
        return Err(format!(
            "identity {:?} {:?} {:?} nnp={}",
            s.uid, s.gid, s.groups, s.no_new_privs
        ));
    }
    Ok(())
}

/// Wait for the launcher's go byte on `gate`: a byte proceeds, EOF or an error aborts.
pub fn wait_go(gate: &mut dyn Read) -> bool {
    let mut b = [0u8; 1];
    matches!(gate.read(&mut b), Ok(1))
}

const CLEAN_RESOLVE: ResolveFlags = ResolveFlags::BENEATH
    .union(ResolveFlags::NO_SYMLINKS)
    .union(ResolveFlags::NO_MAGICLINKS)
    .union(ResolveFlags::NO_XDEV);

fn open_dir_beneath(parent: impl AsFd, name: &CString) -> io::Result<OwnedFd> {
    Ok(openat2(
        parent,
        name.as_c_str(),
        OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
        CLEAN_RESOLVE,
    )?)
}

/// Remove everything beneath the directory `top` (kept itself), iteratively, by descriptor,
/// without following symbolic links or crossing mounts. Directories are made owner-accessible
/// first (a job may have removed its own permissions). Bounded by `max_entries`.
pub fn remove_contents(top: OwnedFd, max_entries: usize) -> io::Result<usize> {
    // Pending directories hold their parent by a shared descriptor, so open descriptors grow
    // with depth, not with width.
    enum Item {
        Enter(Rc<OwnedFd>, CString),
        Leave(Rc<OwnedFd>, CString),
    }
    struct Count {
        removed: usize,
        seen: usize,
        max: usize,
    }
    fn expand(dir: &Rc<OwnedFd>, stack: &mut Vec<Item>, c: &mut Count) -> io::Result<()> {
        let mut names = Vec::new();
        let mut d = Dir::read_from(dir.as_fd())?;
        while let Some(e) = d.read() {
            let e = e?;
            let n = e.file_name();
            if n.to_bytes() != b"." && n.to_bytes() != b".." {
                names.push(n.to_owned());
            }
        }
        for n in names {
            c.seen += 1;
            if c.seen > c.max {
                return Err(io::Error::other("too many entries to clean"));
            }
            let sx = statx(
                dir.as_fd(),
                n.as_c_str(),
                AtFlags::SYMLINK_NOFOLLOW,
                StatxFlags::TYPE,
            )?;
            if FileType::from_raw_mode(u32::from(sx.stx_mode)) == FileType::Directory {
                stack.push(Item::Enter(Rc::clone(dir), n));
            } else {
                unlinkat(dir.as_fd(), n.as_c_str(), AtFlags::empty())?;
                c.removed += 1;
            }
        }
        Ok(())
    }
    let mut c = Count {
        removed: 0,
        seen: 0,
        max: max_entries,
    };
    let mut stack = Vec::new();
    expand(&Rc::new(top), &mut stack, &mut c)?;
    while let Some(item) = stack.pop() {
        match item {
            Item::Leave(parent, name) => {
                unlinkat(parent.as_fd(), name.as_c_str(), AtFlags::REMOVEDIR)?;
                c.removed += 1;
            }
            Item::Enter(parent, name) => {
                let sub = match open_dir_beneath(parent.as_fd(), &name) {
                    Ok(fd) => fd,
                    Err(e) if e.raw_os_error() == Some(13) => {
                        // EACCES: the job removed its own permissions; restore them, reopen.
                        rustix::fs::chmodat(
                            parent.as_fd(),
                            name.as_c_str(),
                            Mode::from_raw_mode(0o700),
                            AtFlags::empty(),
                        )?;
                        open_dir_beneath(parent.as_fd(), &name)?
                    }
                    Err(e) => return Err(e),
                };
                fchmod(&sub, Mode::from_raw_mode(0o700))?;
                stack.push(Item::Leave(parent, name));
                expand(&Rc::new(sub), &mut stack, &mut c)?;
            }
        }
    }
    Ok(c.removed)
}

/// Open the `work` directory for cleaning.
pub fn open_work(work: &Path) -> io::Result<OwnedFd> {
    let fd = openat2(
        CWD,
        work,
        OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
        ResolveFlags::NO_SYMLINKS | ResolveFlags::NO_MAGICLINKS,
    )?;
    fchmod(&fd, Mode::from_raw_mode(0o700))?;
    Ok(fd)
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests;
