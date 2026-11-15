//! `writ-probe agent-deny`: actual attempts made as the agent uid (provisional while J-1 is
//! blocked on C-1) against the trusted layout, the service, privilege channels and user
//! namespaces (J-9 P-AGENT-DENY, P-AGENT-USERNS, P-HARDLINK). Every attempt is unprivileged; a
//! write or link that unexpectedly succeeds is removed again and reported as `allowed`.

use crate::deny::{AGENT_CONTRACT, FORBIDDEN_GROUPS};
use crate::exec;
use crate::probes::Config;
use std::fs;
use std::io::{ErrorKind, Read};
use std::os::unix::fs::MetadataExt;
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::time::Duration;
use writ_kernel::json::{self, s, Value};

/// Options of the agent-side run besides [`Config`].
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct AgentOptions {
    /// Pid of the service process.
    pub service_pid: i32,
    /// systemd unit of the service (`writ.service`).
    pub service_unit: String,
    /// A `writ`-owned file in a directory the agent can reach, prepared by the operator.
    pub hardlink_source: Option<PathBuf>,
    /// Extra paths that must not be readable (the VM trial passes a loosely moded canary here
    /// to show that a readable file is reported).
    pub extra_read: Vec<PathBuf>,
    /// The human desktop uid whose display and session-bus sockets must be unreachable (only
    /// when C-1 option (a) is chosen).
    pub desktop_uid: Option<u32>,
}

type Attempt = (&'static str, String);

fn io_outcome(r: std::io::Result<()>) -> Attempt {
    match r {
        Ok(()) => ("allowed", String::new()),
        Err(e) if e.kind() == ErrorKind::PermissionDenied || e.raw_os_error() == Some(1) => {
            ("refused", e.to_string())
        }
        Err(e) if e.kind() == ErrorKind::NotFound => ("absent", e.to_string()),
        Err(e) => ("error", e.to_string()),
    }
}

/// A command attempt: exit 0 is `allowed`, a non-zero exit or a refused exec is `refused`, a
/// missing program `absent`.
fn cmd_outcome(argv: &[&str]) -> Attempt {
    match exec::run_argv(argv, Duration::from_secs(20)) {
        Ok(o) if o.success() => ("allowed", o.stdout.lines().next().unwrap_or("").to_string()),
        Ok(o) => ("refused", o.diagnostic()),
        Err(e) => io_outcome(Err(e)),
    }
}

fn read_dir(p: &Path) -> std::io::Result<()> {
    fs::read_dir(p).map(drop)
}

fn create_and_remove(dir: &Path) -> std::io::Result<()> {
    let f = dir.join(format!(".writ-agent-probe-{}", std::process::id()));
    fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&f)?;
    let _ = fs::remove_file(&f);
    Ok(())
}

fn read_some(p: &Path) -> std::io::Result<()> {
    let mut buf = [0u8; 1];
    fs::File::open(p)?.read(&mut buf).map(drop)
}

fn signal(pid: i32) -> std::io::Result<()> {
    let p = rustix::process::Pid::from_raw(pid)
        .ok_or_else(|| std::io::Error::from(ErrorKind::InvalidInput))?;
    rustix::process::test_kill_process(p).map_err(std::io::Error::from)
}

fn write_service_cgroup(pid: i32) -> std::io::Result<()> {
    let text = fs::read_to_string(format!("/proc/{pid}/cgroup"))?;
    let rel = crate::probes::own_cgroup(&text)
        .ok_or_else(|| std::io::Error::from(ErrorKind::NotFound))?;
    let path = Path::new("/sys/fs/cgroup")
        .join(rel.trim_start_matches('/'))
        .join("cgroup.procs");
    fs::OpenOptions::new().write(true).open(path).map(drop)
}

/// Read attempts on every entry owned by `uid` in the shared temporary directories.
fn service_tmp(uid: Option<u32>) -> Attempt {
    let Some(uid) = uid else {
        return ("error", "service uid unknown".to_string());
    };
    let mut tried = 0;
    for dir in ["/tmp", "/var/tmp", "/dev/shm"] {
        for e in fs::read_dir(dir).into_iter().flatten().flatten() {
            if e.metadata().ok().map(|m| m.uid()) != Some(uid) {
                continue;
            }
            tried += 1;
            let p = e.path();
            let r = if p.is_dir() {
                read_dir(&p)
            } else {
                read_some(&p)
            };
            if r.is_ok() {
                return ("allowed", p.display().to_string());
            }
        }
    }
    if tried == 0 {
        ("absent", "no entry owned by the service uid".to_string())
    } else {
        ("refused", format!("{tried} entries refused"))
    }
}

/// Read attempts on stored core dumps of the service uid (systemd-coredump naming).
fn core_dumps(uid: Option<u32>) -> Attempt {
    let Some(uid) = uid else {
        return ("error", "service uid unknown".to_string());
    };
    let mut tried = 0;
    let rd = match fs::read_dir("/var/lib/systemd/coredump") {
        Ok(rd) => rd,
        Err(e) => return io_outcome(Err(e)),
    };
    for e in rd.flatten() {
        let name = e.file_name().to_string_lossy().into_owned();
        if name.split('.').nth(2) != Some(uid.to_string().as_str()) {
            continue;
        }
        tried += 1;
        if read_some(&e.path()).is_ok() {
            return ("allowed", name);
        }
    }
    if tried == 0 {
        ("absent", "no core dump of the service uid".to_string())
    } else {
        ("refused", format!("{tried} refused"))
    }
}

fn journal(unit: &str) -> Attempt {
    match exec::run_argv(
        &["journalctl", "-q", "--no-pager", "-n", "5", "-u", unit],
        Duration::from_secs(20),
    ) {
        Ok(o) if o.stdout.lines().any(|l| !l.trim().is_empty()) => {
            ("allowed", "service journal entries visible".to_string())
        }
        Ok(o) => ("refused", o.diagnostic()),
        Err(e) => io_outcome(Err(e)),
    }
}

fn sudo() -> Attempt {
    match exec::run_argv(&["sudo", "-n", "-l"], Duration::from_secs(20)) {
        Ok(o) if o.success() => ("allowed", "sudo -n -l lists rules".to_string()),
        Ok(o) if format!("{}{}", o.stdout, o.stderr).contains("may not run sudo") => {
            ("refused", o.diagnostic())
        }
        Ok(o) => ("error", o.diagnostic()),
        Err(e) => io_outcome(Err(e)),
    }
}

/// Supplementary group names of this process that are on the forbidden list.
pub fn forbidden_memberships(status: &str, etc_group: &str) -> Vec<String> {
    let gids: Vec<&str> = status
        .lines()
        .find_map(|l| l.strip_prefix("Groups:"))
        .map(|g| g.split_whitespace().collect())
        .unwrap_or_default();
    etc_group
        .lines()
        .filter_map(|l| {
            let f: Vec<&str> = l.split(':').collect();
            let (name, gid) = (*f.first()?, *f.get(2)?);
            (gids.contains(&gid) && FORBIDDEN_GROUPS.contains(&name)).then(|| name.to_string())
        })
        .collect()
}

fn groups() -> Attempt {
    let status = fs::read_to_string("/proc/self/status").unwrap_or_default();
    let bad = forbidden_memberships(
        &status,
        &fs::read_to_string("/etc/group").unwrap_or_default(),
    );
    if bad.is_empty() {
        ("refused", "no forbidden group".to_string())
    } else {
        ("allowed", bad.join(","))
    }
}

fn connect(p: &Path) -> Attempt {
    io_outcome(UnixStream::connect(p).map(drop))
}

fn bwrap_userns(bwrap: &str) -> Attempt {
    cmd_outcome(&[bwrap, "--unshare-user", "--ro-bind", "/", "/", "true"])
}

fn copied_bwrap(system: &Path) -> Attempt {
    let dir = std::env::temp_dir().join(format!("writ-agent-probe-{}", std::process::id()));
    let copy = dir.join("bwrap");
    let made = fs::create_dir_all(&dir).and_then(|()| fs::copy(system, &copy).map(drop));
    let out = match made {
        Ok(()) => bwrap_userns(&copy.display().to_string()),
        Err(e) => ("error", format!("copy failed: {e}")),
    };
    let _ = fs::remove_dir_all(&dir);
    out
}

fn hardlink(src: Option<&Path>) -> Attempt {
    let Some(src) = src else {
        return ("error", "no --hardlink-source given".to_string());
    };
    let dir = std::env::temp_dir().join(format!("writ-agent-link-{}", std::process::id()));
    let _ = fs::create_dir_all(&dir);
    let dst = dir.join("link");
    let r = fs::hard_link(src, &dst);
    let _ = fs::remove_dir_all(&dir);
    io_outcome(r)
}

/// Make every attempt and build the `writ-agent-deny/1` report.
pub fn report(cfg: &Config, opt: &AgentOptions) -> Value {
    let sr = &cfg.state_root;
    let pid = opt.service_pid;
    let proc_dir = Path::new("/proc").join(pid.to_string());
    let service_uid = fs::metadata(&proc_dir).map(|m| m.uid()).ok();
    let mut attempts: Vec<(String, Attempt)> = vec![
        ("read_state_root".into(), io_outcome(read_dir(sr))),
        (
            "read_ledger".into(),
            io_outcome(read_dir(&sr.join("ledger"))),
        ),
        (
            "read_stores".into(),
            io_outcome(read_dir(&sr.join("stores"))),
        ),
        ("read_keys".into(), io_outcome(read_dir(&sr.join("keys")))),
        ("read_config".into(), io_outcome(read_dir(&cfg.config_dir))),
        (
            "read_run_state".into(),
            io_outcome(read_dir(&cfg.run_dir.join("state"))),
        ),
        (
            "write_work_root".into(),
            io_outcome(create_and_remove(&sr.join("run"))),
        ),
        (
            "write_target_root".into(),
            io_outcome(create_and_remove(&cfg.target_root)),
        ),
        (
            "connect_admin_sock".into(),
            connect(&cfg.run_dir.join("admin.sock")),
        ),
        (
            "read_service_environ".into(),
            io_outcome(read_some(&proc_dir.join("environ"))),
        ),
        (
            "open_service_mem".into(),
            io_outcome(read_some(&proc_dir.join("mem"))),
        ),
        ("signal_service".into(), io_outcome(signal(pid))),
        (
            "write_service_cgroup".into(),
            io_outcome(write_service_cgroup(pid)),
        ),
        ("read_service_tmp".into(), service_tmp(service_uid)),
        ("read_core_dumps".into(), core_dumps(service_uid)),
        ("read_service_journal".into(), journal(&opt.service_unit)),
        (
            "manage_service".into(),
            cmd_outcome(&[
                "systemctl",
                "--no-ask-password",
                "kill",
                "--signal=0",
                opt.service_unit.as_str(),
            ]),
        ),
        ("sudo_rules".into(), sudo()),
        ("forbidden_groups".into(), groups()),
        (
            "docker_sock".into(),
            connect(Path::new("/var/run/docker.sock")),
        ),
        (
            "lxd_sock".into(),
            connect(Path::new("/var/snap/lxd/common/lxd/unix.socket")),
        ),
        (
            "userns_unshare_map".into(),
            cmd_outcome(&["unshare", "-Ur", "true"]),
        ),
        (
            "userns_system_bwrap".into(),
            bwrap_userns(&cfg.system_bwrap.display().to_string()),
        ),
        (
            "userns_copied_bwrap".into(),
            copied_bwrap(&cfg.system_bwrap),
        ),
        (
            "userns_private_bwrap".into(),
            bwrap_userns(&cfg.bwrap.display().to_string()),
        ),
        (
            "hardlink_writ_file".into(),
            hardlink(opt.hardlink_source.as_deref()),
        ),
    ];
    for p in &opt.extra_read {
        attempts.push((
            format!("extra_read:{}", p.display()),
            io_outcome(read_some(p)),
        ));
    }
    if let Some(uid) = opt.desktop_uid {
        let mut sockets: Vec<PathBuf> = fs::read_dir("/tmp/.X11-unix")
            .into_iter()
            .flatten()
            .flatten()
            .map(|e| e.path())
            .collect();
        let run = PathBuf::from(format!("/run/user/{uid}"));
        sockets.push(run.join("bus"));
        sockets.extend(
            fs::read_dir(&run)
                .into_iter()
                .flatten()
                .flatten()
                .map(|e| e.path())
                .filter(|p| {
                    p.file_name()
                        .is_some_and(|n| n.to_string_lossy().starts_with("wayland-"))
                }),
        );
        for p in sockets {
            attempts.push((format!("desktop:{}", p.display()), connect(&p)));
        }
    }
    let items = attempts
        .into_iter()
        .map(|(t, (out, detail))| {
            json::obj([
                ("target", s(&t)),
                ("outcome", s(out)),
                ("detail", s(&detail)),
            ])
        })
        .collect();
    json::obj([
        ("contract", s(AGENT_CONTRACT)),
        (
            "agent_uid",
            Value::Int(i64::from(rustix::process::getuid().as_raw())),
        ),
        ("service_pid", Value::Int(i64::from(pid))),
        ("service_unit", s(&opt.service_unit)),
        ("attempts", Value::Arr(items)),
    ])
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;
    use crate::deny::{eval_agent_report, DENY_TARGETS};

    #[test]
    fn same_uid_attempts_are_not_a_passing_report() {
        let cfg = Config {
            state_root: "/nonexistent/writ-probe-state".into(),
            ..Config::reference()
        };
        let me = i32::try_from(std::process::id()).unwrap();
        let opt = AgentOptions {
            service_pid: me,
            service_unit: "writ-probe-nonexistent.service".into(),
            ..AgentOptions::default()
        };
        let r = report(&cfg, &opt);
        let text = json::canonical(&r).unwrap();
        let uid = rustix::process::getuid().as_raw();
        assert!(eval_agent_report(text.as_bytes(), uid, &DENY_TARGETS).is_err());
        assert!(
            eval_agent_report(text.as_bytes(), uid.wrapping_add(1), &DENY_TARGETS).is_err(),
            "allowed and absent outcomes are not refusals"
        );
        let attempts = r.get("attempts").and_then(Value::as_arr).unwrap();
        let sig = attempts
            .iter()
            .find(|a| a.get("target").and_then(Value::as_str) == Some("signal_service"))
            .unwrap();
        assert_eq!(sig.get("outcome").and_then(Value::as_str), Some("allowed"));
    }

    #[test]
    fn forbidden_group_membership() {
        let status = "Name:\tx\nGroups:\t4 27 1001 \n";
        let group = "adm:x:4:x\nsudo:x:27:x\nx:x:1001:\n";
        assert_eq!(
            forbidden_memberships(status, group),
            vec!["adm".to_string(), "sudo".to_string()]
        );
        assert!(forbidden_memberships("Groups:\t1001\n", group).is_empty());
    }
}
