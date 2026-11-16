//! Identity, permission and secret probes (J-9): P-AGENT-USERNS, P-ACL, P-HARDLINK,
//! P-PEERCRED, P-AGENT-DENY, P-NO-SECRET, and the static layout facts of J-1.
//!
//! The agent-side probes evaluate the `writ-agent-deny/1` report that the operator produces by
//! running `writ-probe agent-deny` as the agent uid (see [`crate::agent`]); the agent uid is
//! provisional while J-1 is blocked on owner decision C-1.

use crate::probes::{errno_name, o, Config, Scratch};
use crate::readiness::{Control, ProbeId, ProbeResult, Status};
use std::fs;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};
use writ_kernel::json::Value;

/// Contract of the agent-side report.
pub const AGENT_CONTRACT: &str = "writ-agent-deny/1";

/// Agent attempts evaluated by P-AGENT-DENY: (target, absence acceptable).
pub const DENY_TARGETS: [(&str, bool); 21] = [
    ("read_state_root", false),
    ("read_ledger", false),
    ("read_stores", false),
    ("read_keys", false),
    ("read_config", false),
    ("read_run_state", false),
    ("write_work_root", false),
    ("write_target_root", false),
    ("connect_admin_sock", false),
    ("read_service_environ", false),
    ("open_service_mem", false),
    ("signal_service", false),
    ("write_service_cgroup", false),
    ("read_service_tmp", true),
    ("read_core_dumps", true),
    ("read_service_journal", false),
    ("manage_service", false),
    ("sudo_rules", true),
    ("forbidden_groups", false),
    ("docker_sock", true),
    ("lxd_sock", true),
];

/// Agent attempts evaluated by P-AGENT-USERNS.
pub const USERNS_TARGETS: [(&str, bool); 4] = [
    ("userns_unshare_map", false),
    ("userns_system_bwrap", false),
    ("userns_copied_bwrap", false),
    ("userns_private_bwrap", false),
];

/// Agent attempt evaluated by P-HARDLINK.
pub const HARDLINK_TARGETS: [(&str, bool); 1] = [("hardlink_writ_file", false)];

/// Groups an agent uid must not belong to (J-9 P-AGENT-DENY).
pub const FORBIDDEN_GROUPS: [&str; 10] = [
    "sudo",
    "admin",
    "wheel",
    "adm",
    "systemd-journal",
    "writ",
    "docker",
    "lxd",
    "disk",
    "shadow",
];

/// Check an agent-side report (strict JSON) for `targets`: produced by a uid other than
/// `service_uid`, each target present once and `refused` (or `absent` where acceptable). Any
/// attempt whose target starts with `extra_read:` or `desktop:` must also be refused or absent.
/// Returns the agent uid.
pub fn eval_agent_report(
    bytes: &[u8],
    service_uid: u32,
    targets: &[(&str, bool)],
) -> Result<i64, String> {
    let v = writ_kernel::json::parse(bytes)
        .map_err(|e| format!("agent report is not strict JSON: {}", e.code()))?;
    if v.get("contract").and_then(Value::as_str) != Some(AGENT_CONTRACT) {
        return Err("agent report contract is not writ-agent-deny/1".to_string());
    }
    let agent = v
        .get("agent_uid")
        .and_then(Value::as_int)
        .ok_or("agent_uid missing")?;
    if agent == i64::from(service_uid) || agent == 0 {
        return Err(format!(
            "agent report was produced by uid {agent} (the service uid or root)"
        ));
    }
    let attempts = v
        .get("attempts")
        .and_then(Value::as_arr)
        .ok_or("attempts missing")?;
    let outcome = |a: &Value| {
        a.get("outcome")
            .and_then(Value::as_str)
            .unwrap_or("?")
            .to_string()
    };
    for (t, absent_ok) in targets {
        let found: Vec<&Value> = attempts
            .iter()
            .filter(|a| a.get("target").and_then(Value::as_str) == Some(t))
            .collect();
        match found.as_slice() {
            [a] => {
                let out = outcome(a);
                if !(out == "refused" || (*absent_ok && out == "absent")) {
                    return Err(format!("{t}: {out}"));
                }
            }
            [] => return Err(format!("{t}: not attempted")),
            _ => return Err(format!("{t}: duplicated")),
        }
    }
    for a in attempts {
        let t = a.get("target").and_then(Value::as_str).unwrap_or("");
        if t.starts_with("extra_read:") || t.starts_with("desktop:") {
            let out = outcome(a);
            if out != "refused" && out != "absent" {
                return Err(format!("{t}: {out}"));
            }
        }
    }
    Ok(agent)
}

fn agent_report(cfg: &Config, targets: &[(&str, bool)]) -> Result<i64, String> {
    let uid = rustix::process::getuid().as_raw();
    match &cfg.agent_report {
        None => Err("agent-side report not supplied (operator runs `writ-probe agent-deny` as the agent uid)".to_string()),
        Some(p) => fs::read(p).map_err(|e| format!("agent report: {e}")).and_then(|b| eval_agent_report(&b, uid, targets)),
    }
}

// ---------------------------------------------------------------------------------------------
// Layout (J-1)
// ---------------------------------------------------------------------------------------------

/// What a trusted path must look like.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Expect {
    /// Directory (else a regular file or socket).
    pub dir: bool,
    /// Acceptable exact permission bits.
    pub modes: &'static [u32],
    /// Required owner uid.
    pub uid: u32,
    /// Required group, if any.
    pub gid: Option<u32>,
}

/// Problems of one path's `lstat` result against its expectation (`None` when it conforms).
pub fn mode_problem(
    is_symlink: bool,
    is_dir: bool,
    mode: u32,
    uid: u32,
    gid: u32,
    e: Expect,
) -> Option<String> {
    if is_symlink {
        return Some("is a symlink".to_string());
    }
    if is_dir != e.dir {
        return Some(
            if e.dir {
                "not a directory"
            } else {
                "is a directory"
            }
            .to_string(),
        );
    }
    if !e.modes.contains(&(mode & 0o7777)) {
        return Some(format!(
            "mode {:04o}, required one of {}",
            mode & 0o7777,
            e.modes
                .iter()
                .map(|m| format!("{m:04o}"))
                .collect::<Vec<_>>()
                .join("|")
        ));
    }
    if uid != e.uid {
        return Some(format!("owner {uid}, required {}", e.uid));
    }
    if let Some(g) = e.gid {
        if gid != g {
            return Some(format!("group {gid}, required {g}"));
        }
    }
    None
}

/// The ACL extended attributes present on `p` (not following a final symlink).
pub fn acls(p: &Path) -> Vec<&'static str> {
    ["system.posix_acl_access", "system.posix_acl_default"]
        .into_iter()
        .filter(|n| rustix::fs::lgetxattr_query(p, *n).is_ok())
        .collect()
}

fn check_path(p: &Path, e: Expect) -> Option<String> {
    let m = match fs::symlink_metadata(p) {
        Ok(m) => m,
        Err(err) => return Some(format!("{}: {err}", p.display())),
    };
    if let Some(why) = mode_problem(
        m.file_type().is_symlink(),
        m.is_dir(),
        m.mode(),
        m.uid(),
        m.gid(),
        e,
    ) {
        return Some(format!("{}: {why}", p.display()));
    }
    let a = acls(p);
    (!a.is_empty()).then(|| format!("{}: has {}", p.display(), a.join(",")))
}

fn ancestor_problems(p: &Path, uid: u32) -> Vec<String> {
    p.ancestors()
        .skip(1)
        .filter_map(|a| {
            let m = fs::symlink_metadata(a).ok()?;
            let bad = m.file_type().is_symlink()
                || (m.uid() != 0 && m.uid() != uid)
                || m.mode() & 0o022 != 0;
            bad.then(|| {
                format!(
                    "{}: ancestor owner {} mode {:04o}",
                    a.display(),
                    m.uid(),
                    m.mode() & 0o7777
                )
            })
        })
        .collect()
}

/// The gid of group `name` from `/etc/group` text.
pub fn group_gid(etc_group: &str, name: &str) -> Option<u32> {
    etc_group.lines().find_map(|l| {
        let mut f = l.split(':');
        (f.next()? == name)
            .then(|| f.nth(1)?.parse().ok())
            .flatten()
    })
}

/// The job identity's group (D-046 H-2): the private bubblewrap copy is `root:writ-job` 0750,
/// executable by the job uid only (J-1, J-11.10).
pub const JOB_GROUP: &str = "writ-job";

/// J-1 layout problems as seen by the service uid.
pub fn layout_problems(cfg: &Config) -> Vec<String> {
    let uid = rustix::process::getuid().as_raw();
    let gid = rustix::process::getgid().as_raw();
    let etc_group = fs::read_to_string("/etc/group").unwrap_or_default();
    let (layout, mut problems) = expected_layout(cfg, uid, gid, &etc_group);
    problems.extend(layout.iter().filter_map(|(p, e)| check_path(p, *e)));
    for p in [&cfg.state_root, &cfg.target_root, &cfg.bwrap] {
        problems.extend(ancestor_problems(p, uid));
    }
    problems
}

/// The J-1 layout the service uid `uid` (primary group `gid`) expects, and the problems found
/// while building it (an absent group). `run/` is `writ:writ` 0750 (N9: the launcher reads
/// sources by group through PID 1's bind); the private bubblewrap is `root:writ-job` 0750
/// (D-046 H-2).
pub fn expected_layout(
    cfg: &Config,
    uid: u32,
    gid: u32,
    etc_group: &str,
) -> (Vec<(PathBuf, Expect)>, Vec<String>) {
    let agents = group_gid(etc_group, &cfg.agents_group);
    let job_group = group_gid(etc_group, JOB_GROUP);
    let d = |modes: &'static [u32]| Expect {
        dir: true,
        modes,
        uid,
        gid: Some(gid),
    };
    let sr = &cfg.state_root;
    let mut layout: Vec<(PathBuf, Expect)> = vec![
        (sr.clone(), d(&[0o700])),
        (sr.join("ledger"), d(&[0o700])),
        (sr.join("stores"), d(&[0o700])),
        (sr.join("keys"), d(&[0o700])),
        (sr.join("run"), d(&[0o750])),
        (cfg.target_root.clone(), d(&[0o755, 0o750])),
        (
            cfg.config_dir.clone(),
            Expect {
                dir: true,
                modes: &[0o750],
                uid: 0,
                gid: Some(gid),
            },
        ),
        (cfg.run_dir.join("state"), d(&[0o700])),
    ];
    let mut problems = Vec::new();
    match job_group {
        Some(j) => layout.push((
            cfg.bwrap.clone(),
            Expect {
                dir: false,
                modes: &[0o750],
                uid: 0,
                gid: Some(j),
            },
        )),
        None => problems.push(format!("group {JOB_GROUP} absent")),
    }
    match agents {
        Some(a) => layout.push((
            cfg.run_dir.clone(),
            Expect {
                dir: true,
                modes: &[0o750],
                uid,
                gid: Some(a),
            },
        )),
        None => problems.push(format!("group {} absent", cfg.agents_group)),
    }
    let admin = cfg.run_dir.join("admin.sock");
    if admin.exists() {
        layout.push((
            admin,
            Expect {
                dir: false,
                modes: &[0o600],
                uid,
                gid: Some(gid),
            },
        ));
    }
    (layout, problems)
}

/// Abstract socket names held by process `pid` (from its descriptors and `/proc/net/unix`).
pub fn abstract_sockets(pid: i32) -> Result<Vec<String>, String> {
    let mut inodes = Vec::new();
    for e in fs::read_dir(format!("/proc/{pid}/fd"))
        .map_err(|e| e.to_string())?
        .flatten()
    {
        if let Ok(t) = fs::read_link(e.path()) {
            if let Some(n) = t
                .to_str()
                .and_then(|s| s.strip_prefix("socket:["))
                .and_then(|s| s.strip_suffix(']'))
            {
                inodes.push(n.to_string());
            }
        }
    }
    let table = fs::read_to_string("/proc/net/unix").map_err(|e| e.to_string())?;
    Ok(abstract_in_table(&table, &inodes))
}

/// Abstract names (`@...`) in `/proc/net/unix` text whose inode is in `inodes`.
pub fn abstract_in_table(table: &str, inodes: &[String]) -> Vec<String> {
    table
        .lines()
        .skip(1)
        .filter_map(|l| {
            let f: Vec<&str> = l.split_whitespace().collect();
            let (inode, path) = (f.get(6)?, f.get(7)?);
            (path.starts_with('@') && inodes.iter().any(|i| i == inode)).then(|| path.to_string())
        })
        .collect()
}

/// The `Max core file size` soft limit of a process from its `/proc/<pid>/limits` text.
pub fn core_limit(limits: &str) -> Option<String> {
    limits.lines().find_map(|l| {
        l.strip_prefix("Max core file size")
            .map(|r| r.split_whitespace().next().unwrap_or("").to_string())
    })
}

fn sysctl(name: &str) -> String {
    fs::read_to_string(Path::new("/proc/sys").join(name))
        .map_or_else(|_| "absent".to_string(), |v| v.trim().to_string())
}

/// P-AGENT-DENY: the J-1 layout as the service sees it, the service's static exposure (abstract
/// sockets, core limit), and the agent-side report of refused attempts.
pub fn probe_agent_deny(cfg: &Config) -> ProbeResult {
    let control_text = "the mode check on a deliberately mis-moded (0644) test key file must report it (VM trial: a loosely moded canary key passed with --extra-read must be reported readable)";
    let mut problems = layout_problems(cfg);
    let mut obs = vec![
        o("fs.suid_dumpable", sysctl("fs/suid_dumpable")),
        o("kernel.core_pattern", sysctl("kernel/core_pattern")),
        o(
            "kernel.yama.ptrace_scope",
            sysctl("kernel/yama/ptrace_scope"),
        ),
    ];
    match cfg.service_pid {
        Some(pid) => {
            match abstract_sockets(pid) {
                Ok(a) if a.is_empty() => obs.push(o("service_abstract_sockets", "none")),
                Ok(a) => problems.push(format!("service holds abstract sockets: {}", a.join(","))),
                Err(e) => problems.push(format!("service sockets unobservable: {e}")),
            }
            let lim =
                core_limit(&fs::read_to_string(format!("/proc/{pid}/limits")).unwrap_or_default());
            obs.push(o(
                "service_core_limit",
                lim.clone().unwrap_or_else(|| "unobserved".into()),
            ));
            if lim.as_deref() != Some("0") {
                problems.push("service core file size limit is not 0 (LimitCORE=0)".to_string());
            }
        }
        None => problems.push(
            "no service process (--service-pid) to check for abstract sockets and core limit"
                .to_string(),
        ),
    }
    let agent = agent_report(cfg, &DENY_TARGETS);
    match &agent {
        Ok(a) => obs.push(o("agent_uid", a.to_string())),
        Err(e) => obs.push(o("agent_report", e.clone())),
    }
    obs.push(o("layout_problems", problems.len().to_string()));
    for (i, p) in problems.iter().enumerate() {
        obs.push(o(&format!("problem_{i}"), p.clone()));
    }
    let (status, reason) = match (problems.first(), &agent) {
        (Some(first), _) => (Status::Missing, format!("not as required: {first}")),
        (None, Err(e)) => (Status::Missing, e.clone()),
        (None, Ok(_)) => (
            Status::Ok,
            "layout as required; every agent attempt refused".to_string(),
        ),
    };
    let control = if status == Status::Ok {
        let uid = rustix::process::getuid().as_raw();
        match Scratch::new(&cfg.scratch, "agentdeny") {
            Ok(s) => {
                let key = s.path().join("test.key");
                let made = fs::write(&key, b"not a key")
                    .and_then(|()| fs::set_permissions(&key, fs::Permissions::from_mode(0o644)));
                let flagged = made.is_ok()
                    && check_path(
                        &key,
                        Expect {
                            dir: false,
                            modes: &[0o600, 0o400],
                            uid,
                            gid: None,
                        },
                    )
                    .is_some();
                Control::runtime(
                    flagged,
                    format!(
                        "{control_text}: {}",
                        if flagged { "reported" } else { "not reported" }
                    ),
                )
            }
            Err(e) => Control::runtime(false, format!("{control_text}: {e}")),
        }
    } else {
        Control::runtime_not_run(control_text)
    };
    ProbeResult::new(ProbeId::AgentDeny, status, reason, obs, control)
}

/// P-AGENT-USERNS: the agent uid could not obtain a usable user namespace by any route.
pub fn probe_agent_userns(cfg: &Config) -> ProbeResult {
    let control = Control::vm_trial(
        "VM trial: the same attempts as a test uid that may execute the private copy (in group writ) must succeed, so the probe reports missing",
    );
    let obs = vec![o(
        "kernel.apparmor_restrict_unprivileged_userns",
        sysctl("kernel/apparmor_restrict_unprivileged_userns"),
    )];
    match agent_report(cfg, &USERNS_TARGETS) {
        Ok(a) => ProbeResult::new(ProbeId::AgentUserns, Status::Ok, format!("agent uid {a} obtained no user namespace (unshare, system, copied and private bubblewrap)"), obs, control),
        Err(e) => ProbeResult::new(ProbeId::AgentUserns, Status::Missing, e, obs, control),
    }
}

/// P-HARDLINK: `fs.protected_hardlinks = 1` observed and the agent's hard link to a
/// `writ`-owned file refused.
pub fn probe_hardlink(cfg: &Config) -> ProbeResult {
    let control = Control::vm_trial(
        "VM trial: with `sysctl fs.protected_hardlinks=0` the probe must report missing",
    );
    let value = sysctl("fs/protected_hardlinks");
    let obs = vec![o("fs.protected_hardlinks", value.clone())];
    if value != "1" {
        return ProbeResult::new(
            ProbeId::Hardlink,
            Status::Missing,
            format!("fs.protected_hardlinks is {value}"),
            obs,
            control,
        );
    }
    match agent_report(cfg, &HARDLINK_TARGETS) {
        Ok(_) => ProbeResult::new(
            ProbeId::Hardlink,
            Status::Ok,
            "protected_hardlinks set; agent hard link refused",
            obs,
            control,
        ),
        Err(e) => ProbeResult::new(ProbeId::Hardlink, Status::Missing, e, obs, control),
    }
}

// ---------------------------------------------------------------------------------------------
// ACLs
// ---------------------------------------------------------------------------------------------

/// A minimal POSIX ACL xattr value (version 2; user::rwx, group::r-x, other::r-x).
pub fn minimal_acl() -> Vec<u8> {
    let mut v = 2u32.to_le_bytes().to_vec();
    for (tag, perm) in [(0x01u16, 7u16), (0x04, 5), (0x20, 5)] {
        v.extend_from_slice(&tag.to_le_bytes());
        v.extend_from_slice(&perm.to_le_bytes());
        v.extend_from_slice(&u32::MAX.to_le_bytes());
    }
    v
}

const ACL_WALK_LIMIT: usize = 20_000;

/// Paths under `roots` (inclusive, not following symlinks) that carry an access or default
/// ACL, plus paths that could not be read.
pub fn acl_scan(roots: &[&Path]) -> (Vec<String>, Vec<String>, usize) {
    let (mut found, mut unreadable, mut seen) = (Vec::new(), Vec::new(), 0usize);
    let mut stack: Vec<PathBuf> = roots.iter().map(|p| p.to_path_buf()).collect();
    while let Some(p) = stack.pop() {
        if seen >= ACL_WALK_LIMIT {
            unreadable.push(format!("walk limit {ACL_WALK_LIMIT} reached"));
            break;
        }
        seen += 1;
        let m = match fs::symlink_metadata(&p) {
            Ok(m) => m,
            Err(e) => {
                unreadable.push(format!("{}: {e}", p.display()));
                continue;
            }
        };
        let a = acls(&p);
        if !a.is_empty() {
            found.push(format!("{}: {}", p.display(), a.join(",")));
        }
        if m.is_dir() {
            match fs::read_dir(&p) {
                Ok(rd) => stack.extend(rd.flatten().map(|e| e.path())),
                Err(e) => unreadable.push(format!("{}: {e}", p.display())),
            }
        }
    }
    (found, unreadable, seen)
}

/// P-ACL: no access or default ACL on any trusted directory or file.
pub fn probe_acl(cfg: &Config) -> ProbeResult {
    let control_text = "a default ACL planted on a scratch directory must be detected";
    let roots: Vec<&Path> = vec![
        &cfg.state_root,
        &cfg.config_dir,
        &cfg.run_dir,
        &cfg.target_root,
        &cfg.bwrap,
    ];
    let (found, unreadable, seen) = acl_scan(&roots);
    let mut obs = vec![
        o("paths_checked", seen.to_string()),
        o("with_acl", found.len().to_string()),
    ];
    obs.extend(
        found
            .iter()
            .take(20)
            .enumerate()
            .map(|(i, f)| o(&format!("acl_{i}"), f.clone())),
    );
    obs.extend(
        unreadable
            .iter()
            .take(20)
            .enumerate()
            .map(|(i, f)| o(&format!("unreadable_{i}"), f.clone())),
    );
    let (status, reason) = if !found.is_empty() {
        (Status::Missing, format!("ACL present: {}", found[0]))
    } else if !unreadable.is_empty() {
        (
            Status::Missing,
            format!("not observable: {}", unreadable[0]),
        )
    } else {
        (
            Status::Ok,
            format!("no access or default ACL on {seen} trusted paths"),
        )
    };
    let control = if status == Status::Ok {
        match Scratch::new(&cfg.scratch, "acl") {
            Ok(s) => {
                let planted = rustix::fs::setxattr(
                    s.path(),
                    "system.posix_acl_default",
                    &minimal_acl(),
                    rustix::fs::XattrFlags::empty(),
                );
                let detected = planted.is_ok() && !acl_scan(&[s.path()]).0.is_empty();
                Control::runtime(
                    detected,
                    format!(
                        "{control_text}: plant {}, {}",
                        planted.map_or_else(errno_name, |()| "ok".into()),
                        if detected { "detected" } else { "not detected" }
                    ),
                )
            }
            Err(e) => Control::runtime(false, format!("{control_text}: {e}")),
        }
    } else {
        Control::runtime_not_run(control_text)
    };
    ProbeResult::new(ProbeId::Acl, status, reason, obs, control)
}

// ---------------------------------------------------------------------------------------------
// Peer credentials
// ---------------------------------------------------------------------------------------------

/// The overflow uid reported for peers whose ids are unmapped in the reader's namespace.
pub const OVERFLOW_ID: u32 = 65534;

/// J-2 admission of a `host.sock` peer: refused for uid 0, the overflow uid or gid, and any uid
/// not in `allowed`.
pub fn admit(uid: u32, gid: u32, allowed: &[u32]) -> Result<(), &'static str> {
    if uid == 0 {
        return Err("uid 0 refused on host.sock");
    }
    if uid == OVERFLOW_ID || gid == OVERFLOW_ID {
        return Err("overflow id (peer in a namespace with unmapped ids) refused");
    }
    if !allowed.contains(&uid) {
        return Err("uid not on the allow-list");
    }
    Ok(())
}

fn observe_peercred() -> Result<(u32, u32, i32), String> {
    use std::os::linux::net::SocketAddrExt;
    use std::os::unix::net::{SocketAddr, UnixListener, UnixStream};
    let name = format!("writ-probe-peercred-{}", std::process::id());
    let addr = SocketAddr::from_abstract_name(name.as_bytes()).map_err(|e| e.to_string())?;
    let listener = UnixListener::bind_addr(&addr).map_err(|e| format!("bind: {e}"))?;
    let client = UnixStream::connect_addr(&addr).map_err(|e| format!("connect: {e}"))?;
    let (server, _) = listener.accept().map_err(|e| format!("accept: {e}"))?;
    let cred = rustix::net::sockopt::socket_peercred(&server).map_err(errno_name)?;
    drop(client);
    Ok((
        cred.uid.as_raw(),
        cred.gid.as_raw(),
        cred.pid.as_raw_nonzero().get(),
    ))
}

/// P-PEERCRED: `SO_PEERCRED` of an accepted connection reports the connecting process's uid,
/// gid and pid, and admission accepts it only when listed.
pub fn probe_peercred() -> ProbeResult {
    let control_text = "the same peer against an allow-list without its uid, and a peer reported with the overflow uid, must both be refused";
    let (uid, gid, pid) = (
        rustix::process::getuid().as_raw(),
        rustix::process::getgid().as_raw(),
        i32::try_from(std::process::id()).unwrap_or(0),
    );
    let observed = match observe_peercred() {
        Ok(x) => x,
        Err(e) => {
            return ProbeResult::new(
                ProbeId::Peercred,
                Status::Error,
                format!("SO_PEERCRED not observed: {e}"),
                vec![],
                Control::runtime_not_run(control_text),
            )
        }
    };
    let obs = vec![
        o("peer_uid", observed.0.to_string()),
        o("peer_gid", observed.1.to_string()),
        o("peer_pid", observed.2.to_string()),
        o("expected", format!("{uid}/{gid}/{pid}")),
    ];
    let (status, reason) = if observed != (uid, gid, pid) {
        (
            Status::Missing,
            "peer credentials differ from the connecting process".to_string(),
        )
    } else if let Err(e) = admit(observed.0, observed.1, &[uid]) {
        (Status::Missing, format!("listed peer not admitted: {e}"))
    } else {
        (
            Status::Ok,
            "peer credentials match the connecting process; listed uid admitted".to_string(),
        )
    };
    let control = if status == Status::Ok {
        let unlisted = admit(observed.0, observed.1, &[observed.0.wrapping_add(1)]);
        let overflow = admit(OVERFLOW_ID, OVERFLOW_ID, &[OVERFLOW_ID]);
        Control::runtime(
            unlisted.is_err() && overflow.is_err(),
            format!("{control_text}: unlisted {unlisted:?}, overflow {overflow:?}"),
        )
    } else {
        Control::runtime_not_run(control_text)
    };
    ProbeResult::new(ProbeId::Peercred, status, reason, obs, control)
}

// ---------------------------------------------------------------------------------------------
// Secrets
// ---------------------------------------------------------------------------------------------

/// What looks like key material in `hay`: the canary, or a run of at least 64 hex digits.
pub fn find_secret(hay: &[u8], canary: &[u8]) -> Option<&'static str> {
    if !canary.is_empty() && hay.windows(canary.len()).any(|w| w == canary) {
        return Some("canary");
    }
    let mut run = 0usize;
    for b in hay {
        run = if b.is_ascii_hexdigit() { run + 1 } else { 0 };
        if run >= 64 {
            return Some("hex run of 64 or more digits");
        }
    }
    None
}

fn read_bounded(p: &Path) -> std::io::Result<Vec<u8>> {
    use std::io::Read;
    let mut v = Vec::new();
    fs::File::open(p)?
        .take(16 * 1024 * 1024)
        .read_to_end(&mut v)?;
    Ok(v)
}

/// P-NO-SECRET: scan the service's command line and environment, the configured logs and
/// exported journal entries, and the other probe results for the planted canary (and long hex
/// runs, outside the report).
pub fn probe_no_secret(
    cfg: &Config,
    others: &[ProbeResult],
    versions: &[(String, String)],
) -> ProbeResult {
    let control_text = "the scanner applied to a buffer with the planted canary must detect it";
    let fail = |st: Status, why: &str| {
        ProbeResult::new(
            ProbeId::NoSecret,
            st,
            why,
            vec![],
            Control::runtime_not_run(control_text),
        )
    };
    let canary = match &cfg.canary_file {
        None => return fail(Status::Missing, "no canary planted (--canary-file)"),
        Some(p) => match read_bounded(p) {
            Ok(b) => String::from_utf8_lossy(&b).trim().as_bytes().to_vec(),
            Err(e) => return fail(Status::Error, &format!("canary file: {e}")),
        },
    };
    if canary.len() < 16 {
        return fail(Status::Error, "canary shorter than 16 bytes");
    }
    let Some(pid) = cfg.service_pid else {
        return fail(
            Status::Missing,
            "no service process to scan (--service-pid)",
        );
    };
    let mut targets: Vec<(String, PathBuf)> = vec![
        (
            "service_cmdline".into(),
            PathBuf::from(format!("/proc/{pid}/cmdline")),
        ),
        (
            "service_environ".into(),
            PathBuf::from(format!("/proc/{pid}/environ")),
        ),
    ];
    targets.extend(
        cfg.log_files
            .iter()
            .enumerate()
            .map(|(i, p)| (format!("log_file_{i}"), p.clone())),
    );
    let (mut obs, mut found, mut unreadable) = (Vec::new(), Vec::new(), Vec::new());
    for (name, p) in &targets {
        match read_bounded(p) {
            Ok(b) => {
                let hit = find_secret(&b, &canary);
                obs.push(o(name, hit.unwrap_or("clean")));
                if let Some(h) = hit {
                    found.push(format!("{name}: {h}"));
                }
            }
            Err(e) => {
                obs.push(o(name, format!("unreadable: {e}")));
                unreadable.push(name.clone());
            }
        }
    }
    let mut text = String::new();
    for r in others {
        text.push_str(&r.reason);
        for (k, v) in &r.observations {
            text.push_str(k);
            text.push_str(v);
        }
    }
    for (k, v) in versions {
        text.push_str(k);
        text.push_str(v);
    }
    let in_report = text
        .as_bytes()
        .windows(canary.len())
        .any(|w| w == canary.as_slice());
    obs.push(o(
        "readiness_report",
        if in_report { "canary" } else { "clean" },
    ));
    if in_report {
        found.push("readiness_report: canary".to_string());
    }
    let (status, reason) = if !found.is_empty() {
        (
            Status::Missing,
            format!("key material found: {}", found.join("; ")),
        )
    } else if !unreadable.is_empty() {
        (
            Status::Error,
            format!("not scanned: {}", unreadable.join(",")),
        )
    } else {
        (Status::Ok, "no canary or key-shaped material in command line, environment, logs, journal or report".to_string())
    };
    let control = if status == Status::Ok {
        let mut planted = b"prefix ".to_vec();
        planted.extend_from_slice(&canary);
        planted.extend_from_slice(b" suffix");
        let detected = find_secret(&planted, &canary) == Some("canary");
        Control::runtime(
            detected,
            format!(
                "{control_text}: {}",
                if detected { "detected" } else { "not detected" }
            ),
        )
    } else {
        Control::runtime_not_run(control_text)
    };
    ProbeResult::new(ProbeId::NoSecret, status, reason, obs, control)
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;

    #[test]
    fn mode_checks() {
        let e = Expect {
            dir: true,
            modes: &[0o700],
            uid: 990,
            gid: None,
        };
        assert_eq!(mode_problem(false, true, 0o40700, 990, 990, e), None);
        assert!(mode_problem(false, true, 0o40750, 990, 990, e).is_some());
        assert!(mode_problem(false, true, 0o40700, 1000, 990, e).is_some());
        assert!(mode_problem(true, false, 0o120777, 990, 990, e).is_some());
        let key = Expect {
            dir: false,
            modes: &[0o600, 0o400],
            uid: 990,
            gid: None,
        };
        assert!(
            mode_problem(false, false, 0o100644, 990, 990, key).is_some(),
            "mis-moded key reported"
        );
        assert_eq!(mode_problem(false, false, 0o100400, 990, 990, key), None);
        let bwrap = Expect {
            dir: false,
            modes: &[0o750],
            uid: 0,
            gid: Some(990),
        };
        assert!(
            mode_problem(false, false, 0o100755, 0, 990, bwrap).is_some(),
            "agent-executable private copy reported"
        );
        assert!(mode_problem(false, false, 0o100750, 0, 0, bwrap).is_some());
    }

    #[test]
    fn layout_expects_run_0750_and_bwrap_group_writ_job() {
        // M5a-13: N9 (`run/` read by group writ through PID 1's bind) and D-046 H-2 (the
        // private bubblewrap executable by writ-job only).
        let cfg = Config::reference();
        let groups = "writ:x:990:\nwrit-agents:x:991:writ-agent\nwrit-job:x:992:\n";
        let (layout, problems) = expected_layout(&cfg, 990, 990, groups);
        assert!(problems.is_empty(), "{problems:?}");
        let find = |p: &std::path::Path| layout.iter().find(|(q, _)| q == p).map(|(_, e)| *e);
        let run = find(&cfg.state_root.join("run")).unwrap();
        assert_eq!(run.modes, &[0o750]);
        assert_eq!(run.gid, Some(990));
        let bwrap = find(&cfg.bwrap).unwrap();
        assert_eq!(
            (bwrap.uid, bwrap.gid, bwrap.modes),
            (0, Some(992), &[0o750][..])
        );
        assert!(mode_problem(false, false, 0o100750, 0, 990, bwrap).is_some());
        assert_eq!(mode_problem(false, false, 0o100750, 0, 992, bwrap), None);
        let (_, problems) = expected_layout(&cfg, 990, 990, "writ-agents:x:991:\n");
        assert!(
            problems.iter().any(|p| p.contains("writ-job")),
            "{problems:?}"
        );
    }

    fn report(uid: i64, outcomes: &[(&str, &str)]) -> Vec<u8> {
        let attempts: Vec<String> = outcomes
            .iter()
            .map(|(t, o)| format!("{{\"outcome\":\"{o}\",\"target\":\"{t}\"}}"))
            .collect();
        format!(
            "{{\"agent_uid\":{uid},\"attempts\":[{}],\"contract\":\"writ-agent-deny/1\"}}",
            attempts.join(",")
        )
        .into_bytes()
    }

    #[test]
    fn agent_report_evaluation() {
        let all: Vec<(&str, &str)> = DENY_TARGETS.iter().map(|(t, _)| (*t, "refused")).collect();
        assert_eq!(
            eval_agent_report(&report(1000, &all), 990, &DENY_TARGETS),
            Ok(1000)
        );
        assert!(
            eval_agent_report(&report(990, &all), 990, &DENY_TARGETS).is_err(),
            "service uid"
        );
        assert!(
            eval_agent_report(&report(0, &all), 990, &DENY_TARGETS).is_err(),
            "root"
        );
        let mut allowed = all.clone();
        allowed[3].1 = "allowed";
        assert!(
            eval_agent_report(&report(1000, &allowed), 990, &DENY_TARGETS)
                .unwrap_err()
                .starts_with("read_keys")
        );
        let mut absent = all.clone();
        absent[19].1 = "absent";
        assert!(
            eval_agent_report(&report(1000, &absent), 990, &DENY_TARGETS).is_ok(),
            "no docker socket is fine"
        );
        absent[0].1 = "absent";
        assert!(
            eval_agent_report(&report(1000, &absent), 990, &DENY_TARGETS).is_err(),
            "absent state root is not a refusal"
        );
        assert!(eval_agent_report(&report(1000, &all[1..]), 990, &DENY_TARGETS).is_err());
        let mut dup = all.clone();
        dup.push(all[0]);
        assert!(eval_agent_report(&report(1000, &dup), 990, &DENY_TARGETS).is_err());
        let mut canary = all.clone();
        canary.push(("extra_read:/var/tmp/x/canary.key", "allowed"));
        assert!(
            eval_agent_report(&report(1000, &canary), 990, &DENY_TARGETS).is_err(),
            "loose canary reported"
        );
        assert!(eval_agent_report(
            b"{\"contract\":\"writ-agent-deny/1\",\"contract\":\"x\"}",
            990,
            &DENY_TARGETS
        )
        .is_err());
        let userns: Vec<(&str, &str)> = USERNS_TARGETS
            .iter()
            .map(|(t, _)| (*t, "refused"))
            .collect();
        assert!(eval_agent_report(&report(1000, &userns), 990, &USERNS_TARGETS).is_ok());
        let mut granted = userns.clone();
        granted[3].1 = "allowed";
        assert!(
            eval_agent_report(&report(1000, &granted), 990, &USERNS_TARGETS).is_err(),
            "granted uid detected"
        );
    }

    #[test]
    fn peercred_admission() {
        assert_eq!(admit(1000, 1000, &[1000]), Ok(()));
        assert!(admit(1001, 1000, &[1000]).is_err());
        assert!(admit(0, 0, &[0]).is_err());
        assert!(admit(OVERFLOW_ID, 1000, &[OVERFLOW_ID]).is_err());
        assert!(admit(1000, OVERFLOW_ID, &[1000]).is_err());
        let r = probe_peercred();
        assert_eq!(r.status, Status::Ok, "{r:?}");
        assert_eq!(
            r.control.result,
            crate::readiness::ControlResult::FailedAsExpected
        );
    }

    #[test]
    fn parsers() {
        assert_eq!(
            group_gid("root:x:0:\nwrit-agents:x:991:alice\n", "writ-agents"),
            Some(991)
        );
        assert_eq!(group_gid("root:x:0:\n", "writ-agents"), None);
        let table = "Num RefCount Protocol Flags Type St Inode Path\n0000: 00000002 00000000 00010000 0001 01 12345 @writ-x\n0000: 00000002 00000000 00010000 0001 01 777 /run/writ/admin.sock\n0000: 00000002 00000000 00010000 0001 01 888 @other\n";
        assert_eq!(
            abstract_in_table(table, &["12345".into(), "777".into()]),
            vec!["@writ-x".to_string()]
        );
        assert_eq!(core_limit("Limit Soft Hard Units\nMax core file size        0                    0                    bytes\n").as_deref(), Some("0"));
        assert_eq!(minimal_acl().len(), 4 + 3 * 8);
    }

    #[test]
    fn acl_planting_is_detected() {
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../target");
        fs::create_dir_all(&root).unwrap();
        let s = Scratch::new(&root, "acl-unit").unwrap();
        assert!(acl_scan(&[s.path()]).0.is_empty());
        if rustix::fs::setxattr(
            s.path(),
            "system.posix_acl_default",
            &minimal_acl(),
            rustix::fs::XattrFlags::empty(),
        )
        .is_ok()
        {
            assert_eq!(acl_scan(&[s.path()]).0.len(), 1);
        }
    }

    #[test]
    fn secret_scanner() {
        let canary = b"WRIT-CANARY-0123456789abcdef";
        assert_eq!(find_secret(b"PATH=/usr/bin", canary), None);
        assert_eq!(
            find_secret(b"x WRIT-CANARY-0123456789abcdef y", canary),
            Some("canary")
        );
        assert_eq!(
            find_secret("a".repeat(64).as_bytes(), canary),
            Some("hex run of 64 or more digits")
        );
        assert_eq!(find_secret("a".repeat(63).as_bytes(), canary), None);
    }
}
