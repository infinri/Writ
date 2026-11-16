//! The J-9 capability probes. Every result is an observation made on the running host: a
//! command that ran, a system call that succeeded or was refused, a file that was read. No
//! result is inferred from a version string (D-045 M5-D2).
//!
//! Safety of the probes themselves: they only read, or make unprivileged attempts inside a
//! private scratch directory that they remove. Cgroups are created only in delegated-cgroup
//! mode ([`Config::cgroup_delegated`]), which the reference VM procedure requests explicitly
//! when the probe runs inside its own delegated systemd unit; without it nothing under
//! `/sys/fs/cgroup` is written.

use crate::deny;
use crate::exec::{self, kv, kv_get, Outcome};
use crate::readiness::{Control, ProbeId, ProbeResult, Status};
use rustix::fs::{AtFlags, Mode, OFlags, RenameFlags, ResolveFlags, StatxFlags, CWD};
use rustix::io::Errno;
use std::ffi::OsString;
use std::fs;
use std::io::Write;
use std::os::unix::fs::{DirBuilderExt, MetadataExt};
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::{Duration, Instant};

/// Deadline of one sandboxed job.
const JOB_DEADLINE: Duration = Duration::from_secs(20);

/// Probe configuration (defaults are the J-1 layout and the J-9 reference image).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Config {
    /// Trusted state root (`/var/lib/writ`).
    pub state_root: PathBuf,
    /// Configuration directory (`/etc/writ`).
    pub config_dir: PathBuf,
    /// Socket directory (`/run/writ`, `writ:writ-agents` 0750).
    pub run_dir: PathBuf,
    /// `fs.write` target roots (`/srv/writ/targets`, J-1 review M-1).
    pub target_root: PathBuf,
    /// Directory in which private scratch directories are made (`/var/lib/writ/run`, the runner
    /// work root).
    pub scratch: PathBuf,
    /// The private bubblewrap copy with the AppArmor `userns` grant
    /// (`/usr/local/libexec/writ/bwrap`, `root:writ` 0750).
    pub bwrap: PathBuf,
    /// The system bubblewrap without the grant (`/usr/bin/bwrap`), used as the P-USERNS control.
    pub system_bwrap: PathBuf,
    /// Group whose members are the configured agent uids (`writ-agents`; J-1, provisional
    /// while C-1 is open).
    pub agents_group: String,
    /// A directory containing `root/` with a mount point `root/mnt` holding a file `f`, prepared
    /// by the reference VM trial; when set, P-OPENAT2 tests `RESOLVE_NO_XDEV` against that bind
    /// mount instead of the `/` to `/proc` boundary.
    pub xdev_root: Option<PathBuf>,
    /// The probe runs inside its own delegated cgroup (systemd unit with `Delegate=yes`) and may
    /// create cgroups beneath it. Never set on a workstation.
    pub cgroup_delegated: bool,
    /// Pid of the running service (`writ-serviced`), when there is one.
    pub service_pid: Option<i32>,
    /// Report produced by `writ-probe agent-deny` run as the agent uid.
    pub agent_report: Option<PathBuf>,
    /// File holding the planted canary value (not a real key).
    pub canary_file: Option<PathBuf>,
    /// Service log files (and exported journal entries) to scan for key material.
    pub log_files: Vec<PathBuf>,
    /// Directory of the launcher probe reports written by the reference VM trial
    /// ([`crate::jobprobes`]); without it the launcher probes are `missing`.
    pub launcher_reports: Option<PathBuf>,
}

impl Config {
    /// The J-1 reference layout.
    pub fn reference() -> Config {
        Config {
            state_root: "/var/lib/writ".into(),
            config_dir: "/etc/writ".into(),
            run_dir: "/run/writ".into(),
            target_root: "/srv/writ/targets".into(),
            scratch: "/var/lib/writ/run".into(),
            bwrap: "/usr/local/libexec/writ/bwrap".into(),
            system_bwrap: "/usr/bin/bwrap".into(),
            agents_group: "writ-agents".into(),
            xdev_root: None,
            cgroup_delegated: false,
            service_pid: None,
            agent_report: None,
            canary_file: None,
            log_files: Vec::new(),
            launcher_reports: None,
        }
    }

    /// The configuration as report pairs.
    pub fn describe(&self) -> Vec<(String, String)> {
        let p = |x: &Path| x.display().to_string();
        let opt = |x: Option<&Path>| x.map_or("none".to_string(), p);
        let mut v = vec![
            o("state_root", p(&self.state_root)),
            o("config_dir", p(&self.config_dir)),
            o("run_dir", p(&self.run_dir)),
            o("target_root", p(&self.target_root)),
            o("scratch", p(&self.scratch)),
            o("bwrap", p(&self.bwrap)),
            o("system_bwrap", p(&self.system_bwrap)),
            o("agents_group", self.agents_group.clone()),
            o("xdev_root", opt(self.xdev_root.as_deref())),
            o(
                "mode",
                if self.cgroup_delegated {
                    "delegated-cgroup"
                } else {
                    "read-only cgroup"
                },
            ),
            o(
                "service_pid",
                self.service_pid
                    .map_or("none".to_string(), |x| x.to_string()),
            ),
            o("agent_report", opt(self.agent_report.as_deref())),
            o("canary_file", opt(self.canary_file.as_deref())),
            o("launcher_reports", opt(self.launcher_reports.as_deref())),
        ];
        for (i, f) in self.log_files.iter().enumerate() {
            v.push(o(&format!("log_file_{i}"), p(f)));
        }
        v
    }
}

pub(crate) fn o(k: &str, v: impl Into<String>) -> (String, String) {
    (k.to_string(), v.into())
}

pub(crate) fn errno_name(e: Errno) -> String {
    let io: std::io::Error = e.into();
    format!("{} ({})", io.kind(), e.raw_os_error())
}

pub(crate) fn res_name(r: &Result<(), Errno>) -> String {
    match r {
        Ok(()) => "succeeded".to_string(),
        Err(e) => format!("refused: {}", errno_name(*e)),
    }
}

/// A private directory beneath the configured scratch root, removed on drop.
pub struct Scratch {
    path: PathBuf,
}

impl Scratch {
    /// Make `<root>/writ-probe-<pid>-<label>` with mode 0700.
    pub fn new(root: &Path, label: &str) -> std::io::Result<Scratch> {
        let path = root.join(format!("writ-probe-{}-{label}", std::process::id()));
        if path.exists() {
            fs::remove_dir_all(&path)?;
        }
        fs::DirBuilder::new().mode(0o700).create(&path)?;
        Ok(Scratch { path })
    }

    /// The directory.
    pub fn path(&self) -> &Path {
        &self.path
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.path);
    }
}

/// Single-quote a value for `/bin/sh`.
pub fn sq(text: &str) -> String {
    format!("'{}'", text.replace('\'', "'\\''"))
}

// ---------------------------------------------------------------------------------------------
// Sandboxed jobs (bubblewrap)
// ---------------------------------------------------------------------------------------------

/// Bubblewrap arguments of a probe job: every namespace unshared explicitly (the `-try`
/// variants of `--unshare-all` would continue silently without a user or cgroup namespace),
/// a read-only `/usr`, private `/proc`, `/dev` and `/tmp`, and the work directory (if any)
/// bound read-write at `/work`. `unshare_net` and `unshare_pid` false are used only by negative
/// controls (without a PID namespace the host `/proc` is bound read-only instead).
pub fn bwrap_args(
    bwrap: &Path,
    work: Option<&Path>,
    unshare_net: bool,
    unshare_pid: bool,
    extra: &[OsString],
    script: &str,
) -> Vec<OsString> {
    let mut a: Vec<OsString> = vec![bwrap.into()];
    let mut push = |xs: &[&str]| a.extend(xs.iter().map(OsString::from));
    push(&[
        "--unshare-user",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup",
    ]);
    if unshare_pid {
        push(&["--unshare-pid"]);
    }
    if unshare_net {
        push(&["--unshare-net"]);
    }
    push(&[
        "--new-session",
        "--die-with-parent",
        "--cap-drop",
        "ALL",
        "--clearenv",
        "--setenv",
        "PATH",
        "/usr/bin:/bin",
    ]);
    push(&[
        "--ro-bind",
        "/usr",
        "/usr",
        "--symlink",
        "usr/bin",
        "/bin",
        "--symlink",
        "usr/sbin",
        "/sbin",
    ]);
    push(&["--symlink", "usr/lib", "/lib"]);
    if Path::new("/usr/lib64").is_dir() {
        push(&["--symlink", "usr/lib64", "/lib64"]);
    }
    if unshare_pid {
        push(&["--proc", "/proc"]);
    } else {
        push(&["--ro-bind", "/proc", "/proc"]);
    }
    push(&["--dev", "/dev", "--tmpfs", "/tmp"]);
    match work {
        Some(w) => {
            a.extend([
                "--bind".into(),
                w.as_os_str().to_owned(),
                "/work".into(),
                "--chdir".into(),
                "/work".into(),
            ]);
        }
        None => a.extend(["--chdir".into(), "/".into()]),
    }
    a.extend(extra.iter().cloned());
    a.extend(["--".into(), "/bin/sh".into(), "-c".into(), script.into()]);
    a
}

fn job(
    cfg: &Config,
    work: Option<&Path>,
    unshare_net: bool,
    extra: &[OsString],
    script: &str,
) -> std::io::Result<Outcome> {
    exec::run_argv(
        &bwrap_args(&cfg.bwrap, work, unshare_net, true, extra, script),
        JOB_DEADLINE,
    )
}

#[cfg(test)]
fn uncontained(script: &str) -> std::io::Result<Outcome> {
    exec::run_argv(&["/bin/sh", "-c", script], JOB_DEADLINE)
}

const NAMESPACES: [&str; 7] = ["user", "pid", "net", "ipc", "uts", "cgroup", "mnt"];

/// P-USERNS evaluation: the job ran and every namespace differs from the host's.
pub fn eval_userns(
    ran: bool,
    diagnostic: &str,
    host: &[(String, String)],
    job: &[(String, String)],
) -> (Status, String) {
    if !ran {
        return (
            Status::Missing,
            format!("bubblewrap could not create the namespaces: {diagnostic}"),
        );
    }
    let shared: Vec<&str> = NAMESPACES
        .iter()
        .copied()
        .filter(|n| {
            let key = format!("ns_{n}");
            match (kv_get(host, &key), kv_get(job, &key)) {
                (Some(h), Some(j)) => h.is_empty() || j.is_empty() || h == j,
                _ => true,
            }
        })
        .collect();
    if shared.is_empty() {
        (Status::Ok, "every namespace unshared".to_string())
    } else {
        (
            Status::Missing,
            format!(
                "namespaces not unshared or not observed: {}",
                shared.join(",")
            ),
        )
    }
}

fn sysctl(name: &str) -> String {
    fs::read_to_string(Path::new("/proc/sys").join(name))
        .map_or_else(|_| "absent".to_string(), |v| v.trim().to_string())
}

const NS_SCRIPT: &str = "for n in user pid net ipc uts cgroup mnt; do printf 'ns_%s=' \"$n\"; readlink /proc/self/ns/$n || echo; done";

/// P-USERNS: the service runs the private bubblewrap copy with every namespace. Control: the
/// system bubblewrap (no AppArmor `userns` grant) must fail where the restriction is set (VM
/// trial: an unprofiled copy of the private binary must fail too).
pub fn probe_userns(cfg: &Config) -> ProbeResult {
    let control_text =
        "the same job through the system bubblewrap (no AppArmor userns grant) must fail";
    let mut obs = vec![
        o("bwrap", cfg.bwrap.display().to_string()),
        o(
            "kernel.apparmor_restrict_unprivileged_userns",
            sysctl("kernel/apparmor_restrict_unprivileged_userns"),
        ),
        o(
            "user.max_user_namespaces",
            sysctl("user/max_user_namespaces"),
        ),
    ];
    let meta = match fs::metadata(&cfg.bwrap) {
        Ok(m) if m.is_file() => m,
        _ => {
            return ProbeResult::new(
                ProbeId::Userns,
                Status::Missing,
                "private bubblewrap copy not found",
                obs,
                Control::runtime_not_run(control_text),
            )
        }
    };
    obs.push(o("bwrap_setuid", (meta.mode() & 0o4000 != 0).to_string()));
    let host: Vec<(String, String)> = NAMESPACES
        .iter()
        .map(|n| {
            let link = fs::read_link(format!("/proc/self/ns/{n}"))
                .map(|p| p.display().to_string())
                .unwrap_or_default();
            (format!("ns_{n}"), link)
        })
        .collect();
    let (status, reason) = match job(cfg, None, true, &[], NS_SCRIPT) {
        Ok(out) => {
            obs.push(o(
                "job_exit",
                out.code.map_or("none".to_string(), |c| c.to_string()),
            ));
            obs.push(o("job_diagnostic", out.diagnostic()));
            eval_userns(out.success(), &out.diagnostic(), &host, &kv(&out.stdout))
        }
        Err(e) => (Status::Error, format!("could not start bubblewrap: {e}")),
    };
    let control = if status == Status::Ok {
        let argv = bwrap_args(&cfg.system_bwrap, None, true, true, &[], NS_SCRIPT);
        match exec::run_argv(&argv, JOB_DEADLINE) {
            Ok(c) => {
                let failed = eval_userns(c.success(), &c.diagnostic(), &host, &kv(&c.stdout)).0
                    != Status::Ok;
                Control::runtime(
                    failed,
                    format!(
                        "{control_text}: {}",
                        if failed {
                            c.diagnostic()
                        } else {
                            "it succeeded".into()
                        }
                    ),
                )
            }
            Err(e) => Control::runtime(false, format!("{control_text}: could not start ({e})")),
        }
    } else {
        Control::runtime_not_run(control_text)
    };
    ProbeResult::new(ProbeId::Userns, status, reason, obs, control)
}

fn prerequisite(id: ProbeId, what: &str, control: &str) -> ProbeResult {
    ProbeResult::new(
        id,
        Status::Missing,
        format!("prerequisite not observed: {what}"),
        vec![],
        Control::runtime_not_run(control),
    )
}

/// P-FS evaluation: every outside path refused in the job, the work directory writable, no
/// host-visible write outside it.
pub fn eval_fs(
    job: &[(String, String)],
    host_outside_written: bool,
    host_work_written: bool,
) -> (Status, String) {
    let must_refuse = [
        "read_srv",
        "write_usr",
        "read_state",
        "read_keys",
        "read_config",
        "reach_run",
        "reach_cgroup",
        "read_sentinel",
    ];
    let reachable: Vec<&str> = must_refuse
        .iter()
        .copied()
        .filter(|k| kv_get(job, k) != Some("refused"))
        .collect();
    if !reachable.is_empty() {
        return (
            Status::Missing,
            format!("reachable from the job: {}", reachable.join(",")),
        );
    }
    if host_outside_written {
        return (
            Status::Missing,
            "a job write outside the work directory reached the host".to_string(),
        );
    }
    if !host_work_written {
        return (
            Status::Error,
            "the job could not write its own work directory".to_string(),
        );
    }
    (
        Status::Ok,
        "outside paths refused; only the work directory is writable".to_string(),
    )
}

fn fs_script(cfg: &Config, sentinel_dir: &Path) -> String {
    let p = |x: &Path| sq(&x.display().to_string());
    let checks = [
        ("write_usr", "( : > /usr/.writ-probe )".to_string()),
        ("read_state", format!("ls -a -- {}", p(&cfg.state_root))),
        (
            "read_keys",
            format!("ls -a -- {}", p(&cfg.state_root.join("keys"))),
        ),
        ("read_config", format!("ls -a -- {}", p(&cfg.config_dir))),
        ("reach_run", format!("ls -a -- {}", p(&cfg.run_dir))),
        ("read_srv", format!("ls -a -- {}", p(&cfg.target_root))),
        (
            "reach_cgroup",
            "ls -- /sys/fs/cgroup/cgroup.procs".to_string(),
        ),
        (
            "read_sentinel",
            format!("cat -- {}", p(&sentinel_dir.join("secret"))),
        ),
    ];
    let mut s = String::new();
    for (k, c) in checks {
        s.push_str(&format!(
            "if {c} >/dev/null 2>&1; then echo {k}=reachable; else echo {k}=refused; fi\n"
        ));
    }
    s.push_str(&format!(
        "mkdir -p -- {d} 2>/dev/null && : > {d}/written 2>/dev/null\n",
        d = p(sentinel_dir)
    ));
    s.push_str(": > /work/.writ-probe\n");
    s
}

/// P-FS.
pub fn probe_fs(cfg: &Config, userns_ok: bool) -> ProbeResult {
    let control_text =
        "the same job with an extra read-only bind of the sentinel directory must reach it";
    if !userns_ok {
        return prerequisite(ProbeId::Fs, "P-USERNS", control_text);
    }
    let scratch = match Scratch::new(&cfg.scratch, "fs") {
        Ok(s) => s,
        Err(e) => {
            return ProbeResult::new(
                ProbeId::Fs,
                Status::Missing,
                format!("scratch root unusable: {e}"),
                vec![],
                Control::runtime_not_run(control_text),
            )
        }
    };
    let work = scratch.path().join("work");
    let sentinel = scratch.path().join("sentinel");
    let setup = fs::DirBuilder::new()
        .mode(0o700)
        .create(&work)
        .and_then(|()| fs::DirBuilder::new().mode(0o700).create(&sentinel));
    if let Err(e) = setup.and_then(|()| fs::write(sentinel.join("secret"), b"writ-probe-sentinel"))
    {
        return ProbeResult::new(
            ProbeId::Fs,
            Status::Error,
            format!("scratch setup failed: {e}"),
            vec![],
            Control::runtime_not_run(control_text),
        );
    }
    let script = fs_script(cfg, &sentinel);
    let out = match job(cfg, Some(&work), true, &[], &script) {
        Ok(o) => o,
        Err(e) => {
            return ProbeResult::new(
                ProbeId::Fs,
                Status::Error,
                format!("job failed to start: {e}"),
                vec![],
                Control::runtime_not_run(control_text),
            )
        }
    };
    let pairs = kv(&out.stdout);
    let mut obs = pairs.clone();
    let outside = sentinel.join("written").exists();
    let inside = work.join(".writ-probe").exists();
    obs.push(o("host_outside_written", outside.to_string()));
    obs.push(o("host_work_written", inside.to_string()));
    let (status, reason) = eval_fs(&pairs, outside, inside);
    let control = if status == Status::Ok {
        let extra: Vec<OsString> = vec![
            "--ro-bind".into(),
            sentinel.clone().into(),
            sentinel.clone().into(),
        ];
        match job(cfg, Some(&work), true, &extra, &script) {
            Ok(c) => {
                let reached = kv_get(&kv(&c.stdout), "read_sentinel") == Some("reachable");
                Control::runtime(
                    reached,
                    format!(
                        "{control_text}: read_sentinel {}",
                        if reached { "reachable" } else { "refused" }
                    ),
                )
            }
            Err(e) => Control::runtime(
                false,
                format!("{control_text}: control job failed to start: {e}"),
            ),
        }
    } else {
        Control::runtime_not_run(control_text)
    };
    ProbeResult::new(ProbeId::Fs, status, reason, obs, control)
}

/// P-NET evaluation.
pub fn eval_net(job: &[(String, String)]) -> (Status, String) {
    let ifaces = kv_get(job, "ifaces").unwrap_or("");
    let routes = kv_get(job, "routes").unwrap_or("");
    let lo = kv_get(job, "host_loopback").unwrap_or("");
    if ifaces != "lo," {
        return (
            Status::Missing,
            format!("interfaces other than loopback visible: {ifaces:?}"),
        );
    }
    if routes != "0" {
        return (Status::Missing, format!("routes present: {routes:?}"));
    }
    if lo != "refused" {
        return (Status::Missing, format!("host loopback listener: {lo:?}"));
    }
    if kv_get(job, "host_abstract") != Some("0") {
        return (
            Status::Missing,
            "host abstract socket visible from the job".to_string(),
        );
    }
    (
        Status::Ok,
        "only loopback in a new namespace; host loopback, host abstract sockets and external routes unreachable"
            .to_string(),
    )
}

fn net_script(port: u16, abstract_name: &str) -> String {
    format!(
        "echo \"host_abstract=$(grep -c ' @{abstract_name}$' /proc/net/unix)\"\n\
         echo \"ifaces=$(tail -n +3 /proc/net/dev | cut -d: -f1 | tr -d ' ' | sort | tr '\\n' ',')\"\n\
         echo \"routes=$(tail -n +2 /proc/net/route | wc -l)\"\n\
         if bash -c 'exec 3<>/dev/tcp/127.0.0.1/{port}' 2>/dev/null; then echo host_loopback=reachable; else echo host_loopback=refused; fi\n"
    )
}

/// P-NET.
pub fn probe_net(cfg: &Config, userns_ok: bool) -> ProbeResult {
    let control_text = "the same job without --unshare-net must reach the host loopback listener";
    if !userns_ok {
        return prerequisite(ProbeId::Net, "P-USERNS", control_text);
    }
    let listener = match std::net::TcpListener::bind("127.0.0.1:0") {
        Ok(l) => l,
        Err(e) => {
            return ProbeResult::new(
                ProbeId::Net,
                Status::Error,
                format!("host loopback listener: {e}"),
                vec![],
                Control::runtime_not_run(control_text),
            )
        }
    };
    let port = listener.local_addr().map(|a| a.port()).unwrap_or(0);
    let abstract_name = format!("writ-probe-net-{}", std::process::id());
    let abstract_listener = {
        use std::os::linux::net::SocketAddrExt;
        std::os::unix::net::SocketAddr::from_abstract_name(abstract_name.as_bytes())
            .and_then(|a| std::os::unix::net::UnixListener::bind_addr(&a))
    };
    let script = net_script(port, &abstract_name);
    let out = match job(cfg, None, true, &[], &script) {
        Ok(o) => o,
        Err(e) => {
            return ProbeResult::new(
                ProbeId::Net,
                Status::Error,
                format!("job failed to start: {e}"),
                vec![],
                Control::runtime_not_run(control_text),
            )
        }
    };
    let pairs = kv(&out.stdout);
    let (status, reason) = eval_net(&pairs);
    let control = if status == Status::Ok {
        match job(cfg, None, false, &[], &script) {
            Ok(c) => {
                let reached = kv_get(&kv(&c.stdout), "host_loopback") == Some("reachable");
                Control::runtime(
                    reached,
                    format!(
                        "{control_text}: host_loopback {}",
                        if reached { "reachable" } else { "refused" }
                    ),
                )
            }
            Err(e) => Control::runtime(
                false,
                format!("{control_text}: control job failed to start: {e}"),
            ),
        }
    } else {
        Control::runtime_not_run(control_text)
    };
    drop(listener);
    let mut pairs = pairs;
    pairs.push(o(
        "host_abstract_listener",
        if abstract_listener.is_ok() {
            "bound"
        } else {
            "not bound"
        },
    ));
    let (status, reason) = match (&abstract_listener, status) {
        (Err(e), Status::Ok) => (
            Status::Error,
            format!("host abstract listener not bound, so its invisibility proves nothing: {e}"),
        ),
        _ => (status, reason),
    };
    drop(abstract_listener);
    ProbeResult::new(ProbeId::Net, status, reason, pairs, control)
}

/// P-PID evaluation.
pub fn eval_pid(job: &[(String, String)]) -> (Status, String) {
    let procs: Option<u32> = kv_get(job, "procs").and_then(|v| v.parse().ok());
    if kv_get(job, "signal") != Some("refused") {
        return (
            Status::Missing,
            "the target process could be signalled from the job".to_string(),
        );
    }
    if kv_get(job, "proc_visible") != Some("no") {
        return (
            Status::Missing,
            "the target process is visible in the job's /proc".to_string(),
        );
    }
    match procs {
        Some(n) if n <= 8 => (
            Status::Ok,
            format!("{n} processes visible; host processes invisible and unsignalable"),
        ),
        Some(n) => (Status::Missing, format!("{n} processes visible in the job")),
        None => (Status::Error, "process count not observed".to_string()),
    }
}

fn pid_script(pid: i32) -> String {
    format!(
        "echo \"procs=$(ls /proc | grep -c '^[0-9][0-9]*$')\"\n\
         if kill -0 {pid} 2>/dev/null; then echo signal=delivered; else echo signal=refused; fi\n\
         if [ -e /proc/{pid}/cmdline ]; then echo proc_visible=yes; else echo proc_visible=no; fi\n"
    )
}

/// P-PID (target: the service pid if configured, else the probe itself).
pub fn probe_pid(cfg: &Config, userns_ok: bool) -> ProbeResult {
    let control_text =
        "the same job without PID unsharing (host /proc bound) must see and signal the target";
    if !userns_ok {
        return prerequisite(ProbeId::Pid, "P-USERNS", control_text);
    }
    let target = cfg
        .service_pid
        .unwrap_or_else(|| i32::try_from(std::process::id()).unwrap_or(1));
    let script = pid_script(target);
    let out = match job(cfg, None, true, &[], &script) {
        Ok(o) => o,
        Err(e) => {
            return ProbeResult::new(
                ProbeId::Pid,
                Status::Error,
                format!("job failed to start: {e}"),
                vec![],
                Control::runtime_not_run(control_text),
            )
        }
    };
    let mut pairs = kv(&out.stdout);
    let (status, reason) = eval_pid(&pairs);
    pairs.push(o("target_pid", target.to_string()));
    let control = if status == Status::Ok {
        let argv = bwrap_args(&cfg.bwrap, None, true, false, &[], &script);
        match exec::run_argv(&argv, JOB_DEADLINE) {
            Ok(c) => {
                let p = kv(&c.stdout);
                let failed = eval_pid(&p).0 == Status::Missing;
                Control::runtime(
                    failed,
                    format!(
                        "{control_text}: signal {}",
                        kv_get(&p, "signal").unwrap_or("?")
                    ),
                )
            }
            Err(e) => Control::runtime(false, format!("{control_text}: {e}")),
        }
    } else {
        Control::runtime_not_run(control_text)
    };
    ProbeResult::new(ProbeId::Pid, status, reason, pairs, control)
}

/// P-FD evaluation: descriptors above 2 that still name an open file are leaks (the directory
/// handle used to list the descriptors is closed before it is read, so it names nothing).
pub fn eval_fd(job: &[(String, String)]) -> (Status, String) {
    let fds: Vec<(&str, &str)> = job
        .iter()
        .filter(|(k, _)| k == "fd")
        .filter_map(|(_, v)| v.split_once(':'))
        .collect();
    if fds.is_empty() {
        return (Status::Error, "no descriptors observed".to_string());
    }
    let leaked: Vec<String> = fds
        .iter()
        .filter(|(n, t)| n.parse::<u32>().map_or(true, |n| n > 2) && !t.is_empty())
        .map(|(n, t)| format!("{n}->{t}"))
        .collect();
    if leaked.is_empty() {
        (
            Status::Ok,
            "only descriptors 0..2 open in the job".to_string(),
        )
    } else {
        (
            Status::Missing,
            format!("extra descriptors open: {}", leaked.join(",")),
        )
    }
}

const FD_SCRIPT: &str =
    "for f in /proc/$$/fd/*; do printf 'fd=%s:' \"${f##*/}\"; readlink \"$f\" || echo; done";

/// P-FD.
pub fn probe_fd(cfg: &Config, userns_ok: bool) -> ProbeResult {
    let control_text = "the same job spawned with an inherited extra descriptor 7 must report it";
    if !userns_ok {
        return prerequisite(ProbeId::Fd, "P-USERNS", control_text);
    }
    let out = match job(cfg, None, true, &[], FD_SCRIPT) {
        Ok(o) => o,
        Err(e) => {
            return ProbeResult::new(
                ProbeId::Fd,
                Status::Error,
                format!("job failed to start: {e}"),
                vec![],
                Control::runtime_not_run(control_text),
            )
        }
    };
    let mut pairs = kv(&out.stdout);
    let (mut status, mut reason) = eval_fd(&pairs);
    // Revision 6 (N4): the status-pipe part runs through the launcher in the reference VM.
    let status_pipe = crate::jobprobes::fd_status(cfg);
    match &status_pipe {
        Some(e) => {
            pairs.push(o("status_pipe", e.reason.clone()));
            if status == Status::Ok && e.status != Status::Ok {
                status = e.status;
                reason = format!("status pipe (N4): {}", e.reason);
            }
        }
        None => {
            pairs.push(o("status_pipe", "not run (launcher report not supplied)"));
            if status == Status::Ok {
                status = Status::Missing;
                reason = "status-pipe check (N4) requires the launcher in the reference VM".into();
            }
        }
    }
    let status_control = status_pipe.as_ref().is_none_or(|e| e.controls_detected);
    let control = if status == Status::Ok {
        let mut argv: Vec<OsString> = vec![
            "/bin/sh".into(),
            "-c".into(),
            "exec 7</dev/null; exec \"$@\"".into(),
            "sh".into(),
        ];
        argv.extend(bwrap_args(&cfg.bwrap, None, true, true, &[], FD_SCRIPT));
        match exec::run_argv(&argv, JOB_DEADLINE) {
            Ok(c) => {
                let failed = eval_fd(&kv(&c.stdout)).0 == Status::Missing;
                Control::runtime(
                    failed && status_control,
                    format!(
                        "{control_text}: {}; status-pipe trusting build {}",
                        if failed { "reported" } else { "not reported" },
                        if status_control {
                            "detected"
                        } else {
                            "not detected"
                        }
                    ),
                )
            }
            Err(e) => Control::runtime(false, format!("{control_text}: {e}")),
        }
    } else {
        Control::runtime_not_run(control_text)
    };
    ProbeResult::new(ProbeId::Fd, status, reason, pairs, control)
}

// ---------------------------------------------------------------------------------------------
// Cgroups
// ---------------------------------------------------------------------------------------------

const CGROUP2_MAGIC: i64 = 0x6367_7270;
const CONTROLLERS: [&str; 3] = ["memory", "pids", "cpu"];
const JOB_FILES: [&str; 7] = [
    "cgroup.procs",
    "cgroup.kill",
    "cgroup.events",
    "memory.max",
    "memory.swap.max",
    "pids.max",
    "cpu.max",
];

/// The process's cgroup-v2 path from `/proc/self/cgroup` text (the `0::` line).
pub fn own_cgroup(text: &str) -> Option<String> {
    text.lines()
        .find_map(|l| l.strip_prefix("0::"))
        .map(str::to_string)
}

/// Controllers among memory, pids and cpu that a `cgroup.controllers` text lacks.
pub fn missing_controllers(text: &str) -> Vec<&'static str> {
    let have: Vec<&str> = text.split_whitespace().collect();
    CONTROLLERS
        .iter()
        .copied()
        .filter(|c| !have.contains(c))
        .collect()
}

/// Interface files a job cgroup must have that `present` lacks (for example `cgroup.kill` on
/// kernels before 5.14).
pub fn missing_job_files(present: impl Fn(&str) -> bool) -> Vec<&'static str> {
    JOB_FILES.iter().copied().filter(|f| !present(f)).collect()
}

/// `populated` value of a `cgroup.events` text.
pub fn populated(events: &str) -> Option<bool> {
    events
        .lines()
        .find_map(|l| l.strip_prefix("populated "))
        .map(|v| v.trim() == "1")
}

/// A counter (`name value` line) of a cgroup keyed file such as `memory.events` or `cpu.stat`.
pub fn counter(text: &str, name: &str) -> Option<u64> {
    text.lines().find_map(|l| {
        let (k, v) = l.split_once(' ')?;
        (k == name).then(|| v.trim().parse().ok()).flatten()
    })
}

// `f_type` is i64 on x86_64 and i32 on some other targets; the conversion keeps both.
#[allow(clippy::useless_conversion)]
fn statfs_magic(p: &Path) -> Result<i64, Errno> {
    rustix::fs::statfs(p).map(|s| i64::from(s.f_type))
}

/// The delegated subtree prepared by P-CGROUP for the kill and limit probes.
pub struct Delegated {
    jobs: PathBuf,
}

fn cg_write(dir: &Path, file: &str, value: &str) -> std::io::Result<()> {
    fs::OpenOptions::new()
        .write(true)
        .open(dir.join(file))?
        .write_all(value.as_bytes())
}

fn cg_read(dir: &Path, file: &str) -> String {
    fs::read_to_string(dir.join(file)).unwrap_or_default()
}

/// Kill every process of a cgroup and wait (bounded) for it to empty; true when emptied.
fn cg_kill_and_wait(dir: &Path) -> bool {
    let _ = cg_write(dir, "cgroup.kill", "1");
    let start = Instant::now();
    while start.elapsed() < Duration::from_secs(5) {
        if populated(&cg_read(dir, "cgroup.events")) == Some(false) {
            return true;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    false
}

fn cg_remove(dir: &Path) -> bool {
    cg_kill_and_wait(dir) && fs::remove_dir(dir).is_ok()
}

/// P-CGROUP. Read-only unless `cfg.cgroup_delegated`.
pub fn probe_cgroup(cfg: &Config) -> (ProbeResult, Option<Delegated>) {
    let control_text =
        "creating a cgroup outside the delegated subtree (the hierarchy root) must be refused";
    let mut obs = Vec::new();
    let magic = statfs_magic(Path::new("/sys/fs/cgroup"));
    obs.push(o(
        "sys_fs_cgroup_magic",
        magic.map_or_else(errno_name, |m| format!("{m:#x}")),
    ));
    if magic != Ok(CGROUP2_MAGIC) {
        let r = ProbeResult::new(
            ProbeId::Cgroup,
            Status::Missing,
            "cgroup v2 unified hierarchy not mounted at /sys/fs/cgroup",
            obs,
            Control::runtime_not_run(control_text),
        );
        return (r, None);
    }
    let own = own_cgroup(&fs::read_to_string("/proc/self/cgroup").unwrap_or_default())
        .unwrap_or_default();
    let own_dir = Path::new("/sys/fs/cgroup").join(own.trim_start_matches('/'));
    let uid = rustix::process::getuid().as_raw();
    let owner = fs::metadata(own_dir.join("cgroup.procs")).map(|m| m.uid());
    let controllers = cg_read(&own_dir, "cgroup.controllers");
    obs.push(o("own_cgroup", own.clone()));
    obs.push(o(
        "own_cgroup_procs_owner",
        owner
            .as_ref()
            .map_or_else(|e| e.to_string(), |u| u.to_string()),
    ));
    obs.push(o("own_controllers", controllers.trim().to_string()));
    obs.push(o(
        "cgroup_kill_present",
        own_dir.join("cgroup.kill").exists().to_string(),
    ));
    let (status, reason, delegated) = if !cfg.cgroup_delegated {
        (
            Status::Missing,
            "delegated-cgroup mode not requested; the probe writes no cgroup unless run in a delegated unit with --cgroup-delegated (reference VM procedure)".to_string(),
            None,
        )
    } else if owner.ok() != Some(uid) {
        (
            Status::Missing,
            format!("own cgroup {own} is not delegated to uid {uid}"),
            None,
        )
    } else if !missing_controllers(&controllers).is_empty() {
        (
            Status::Missing,
            format!(
                "controllers not available: {}",
                missing_controllers(&controllers).join(",")
            ),
            None,
        )
    } else {
        match prepare_delegated(&own_dir) {
            Ok((jobs, notes)) => {
                obs.extend(notes);
                (
                    Status::Ok,
                    "delegated subtree writable; memory, pids, cpu enabled; cgroup.kill present"
                        .to_string(),
                    Some(Delegated { jobs }),
                )
            }
            Err(why) => (Status::Missing, why, None),
        }
    };
    let control = if status == Status::Ok {
        let p =
            Path::new("/sys/fs/cgroup").join(format!("writ-probe-control-{}", std::process::id()));
        match fs::create_dir(&p) {
            Ok(()) => {
                let _ = fs::remove_dir(&p);
                Control::runtime(
                    false,
                    format!("{control_text}: creation succeeded (running with privileges?)"),
                )
            }
            Err(e) => Control::runtime(true, format!("{control_text}: refused ({e})")),
        }
    } else {
        Control::runtime_not_run(control_text)
    };
    (
        ProbeResult::new(ProbeId::Cgroup, status, reason, obs, control),
        delegated,
    )
}

fn prepare_delegated(own_dir: &Path) -> Result<(PathBuf, Vec<(String, String)>), String> {
    let leaf = own_dir.join("probe");
    let jobs = own_dir.join("jobs");
    let enable = "+memory +pids +cpu";
    let step = |what: &str, r: std::io::Result<()>| r.map_err(|e| format!("{what}: {e}"));
    if !leaf.exists() {
        step("create leaf cgroup", fs::create_dir(&leaf))?;
    }
    step(
        "move the probe into the leaf",
        cg_write(&leaf, "cgroup.procs", &std::process::id().to_string()),
    )?;
    step(
        "enable controllers in the delegated root",
        cg_write(own_dir, "cgroup.subtree_control", enable),
    )?;
    if !jobs.exists() {
        step("create jobs cgroup", fs::create_dir(&jobs))?;
    }
    step(
        "enable controllers for jobs",
        cg_write(&jobs, "cgroup.subtree_control", enable),
    )?;
    let job = jobs.join("job-probe-cgroup");
    step("create a job cgroup", fs::create_dir(&job))?;
    let missing = missing_job_files(|f| job.join(f).exists());
    let mut notes = vec![o("job_files_missing", missing.join(","))];
    let limits = [
        ("memory.max", "67108864"),
        ("memory.swap.max", "0"),
        ("pids.max", "64"),
        ("cpu.max", "50000 100000"),
    ];
    let mut write_err = None;
    for (f, v) in limits {
        if missing.contains(&f) {
            continue;
        }
        if let Err(e) = cg_write(&job, f, v) {
            write_err = Some(format!("write {f}: {e}"));
        }
        notes.push(o(f, cg_read(&job, f).trim().to_string()));
    }
    let removed = cg_remove(&job);
    if !missing.is_empty() {
        return Err(format!("job cgroup lacks {}", missing.join(",")));
    }
    if let Some(e) = write_err {
        return Err(e);
    }
    if !removed {
        return Err("job cgroup could not be removed".to_string());
    }
    Ok((jobs, notes))
}

/// A process placed into a cgroup before it runs its script (it waits for one input line).
fn spawn_in_cgroup(cg: &Path, script: &str) -> std::io::Result<std::process::Child> {
    let full = format!("read go; {script}");
    let Some(mut cmd) = exec::command(&["/bin/sh", "-c", full.as_str()]) else {
        return Err(std::io::Error::other("empty argv"));
    };
    cmd.stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = cmd.spawn()?;
    if let Err(e) = cg_write(cg, "cgroup.procs", &child.id().to_string()) {
        exec::kill_group(&mut child);
        let _ = child.wait();
        return Err(e);
    }
    if let Some(mut stdin) = child.stdin.take() {
        stdin.write_all(b"go\n")?;
    }
    Ok(child)
}

fn procs(cg: &Path) -> usize {
    cg_read(cg, "cgroup.procs")
        .lines()
        .filter(|l| !l.trim().is_empty())
        .count()
}

/// P-KILL evaluation.
pub fn eval_kill(
    survivors: usize,
    after_group_kill: usize,
    emptied: bool,
) -> (Status, String, bool) {
    if survivors < 2 {
        return (
            Status::Error,
            format!("the job left {survivors} descendants; the probe needs 2 to discriminate"),
            false,
        );
    }
    let control_failed = after_group_kill > 0;
    if emptied {
        (Status::Ok, format!("{survivors} setsid and double-forked descendants removed by cgroup.kill; populated 0 observed"), control_failed)
    } else {
        (
            Status::Missing,
            "cgroup did not empty after cgroup.kill".to_string(),
            control_failed,
        )
    }
}

/// P-KILL.
pub fn probe_kill(d: Option<&Delegated>) -> ProbeResult {
    let control_text = "process-group SIGKILL alone must leave a survivor";
    let Some(d) = d else {
        return prerequisite(ProbeId::Kill, "P-CGROUP (delegated subtree)", control_text);
    };
    let cg = d.jobs.join("job-probe-kill");
    if let Err(e) = fs::create_dir(&cg) {
        return ProbeResult::new(
            ProbeId::Kill,
            Status::Error,
            format!("create job cgroup: {e}"),
            vec![],
            Control::runtime_not_run(control_text),
        );
    }
    let script = "setsid -f sleep 300 >/dev/null 2>&1 </dev/null; sh -c 'sleep 300 >/dev/null 2>&1 </dev/null &'; echo started=1";
    let result = (|| -> std::io::Result<(usize, usize, bool, i32)> {
        let child = spawn_in_cgroup(&cg, script)?;
        let pid = i32::try_from(child.id()).unwrap_or(0);
        exec::wait(child, Duration::from_secs(10))?;
        std::thread::sleep(Duration::from_millis(300));
        let survivors = procs(&cg);
        if let Some(p) = rustix::process::Pid::from_raw(pid) {
            let _ = rustix::process::kill_process_group(p, rustix::process::Signal::KILL);
        }
        std::thread::sleep(Duration::from_millis(300));
        let after = procs(&cg);
        let emptied = cg_kill_and_wait(&cg);
        Ok((survivors, after, emptied, pid))
    })();
    let (status, reason, control, obs) = match result {
        Ok((survivors, after, emptied, pid)) => {
            let (st, why, control_failed) = eval_kill(survivors, after, emptied);
            let obs = vec![
                o("job_pid", pid.to_string()),
                o("descendants_after_job_exit", survivors.to_string()),
                o("after_process_group_kill", after.to_string()),
                o("emptied_by_cgroup_kill", emptied.to_string()),
            ];
            (
                st,
                why,
                Control::runtime(
                    control_failed,
                    format!("{control_text}: {after} left after the group kill"),
                ),
                obs,
            )
        }
        Err(e) => (
            Status::Error,
            format!("job failed: {e}"),
            Control::runtime_not_run(control_text),
            vec![],
        ),
    };
    let removed = cg_remove(&cg);
    let mut obs = obs;
    obs.push(o("job_cgroup_removed", removed.to_string()));
    if !removed {
        return ProbeResult::new(
            ProbeId::Kill,
            Status::Error,
            "job cgroup not removed (quarantined)",
            obs,
            control,
        );
    }
    ProbeResult::new(ProbeId::Kill, status, reason, obs, control)
}

fn limited_run(
    d: &Delegated,
    name: &str,
    limits: &[(&str, &str)],
    script: &str,
) -> Result<(Outcome, String, String, String), String> {
    let cg = d.jobs.join(name);
    fs::create_dir(&cg).map_err(|e| format!("create {name}: {e}"))?;
    let run = (|| -> Result<Outcome, String> {
        for (f, v) in limits {
            cg_write(&cg, f, v).map_err(|e| format!("{name} {f}: {e}"))?;
        }
        let child = spawn_in_cgroup(&cg, script).map_err(|e| format!("{name}: {e}"))?;
        exec::wait(child, Duration::from_secs(15)).map_err(|e| format!("{name}: {e}"))
    })();
    let events = (
        cg_read(&cg, "memory.events"),
        cg_read(&cg, "pids.events"),
        cg_read(&cg, "cpu.stat"),
    );
    let removed = cg_remove(&cg);
    let out = run?;
    if !removed {
        return Err(format!("{name}: cgroup not removed (quarantined)"));
    }
    Ok((out, events.0, events.1, events.2))
}

/// P-LIMITS evaluation.
pub fn eval_limits(
    oom_kill: Option<u64>,
    pids_max: Option<u64>,
    throttled: Option<u64>,
) -> (Status, String) {
    let mut missing = Vec::new();
    if oom_kill.unwrap_or(0) == 0 {
        missing.push("memory.max (no oom_kill)");
    }
    if pids_max.unwrap_or(0) == 0 {
        missing.push("pids.max (no fork refused)");
    }
    if throttled.unwrap_or(0) == 0 {
        missing.push("cpu.max (no throttling)");
    }
    if missing.is_empty() {
        (
            Status::Ok,
            "memory, pids and CPU limits observed".to_string(),
        )
    } else {
        (
            Status::Missing,
            format!("limits not observed: {}", missing.join(", ")),
        )
    }
}

/// Fork workload of P-LIMITS: 16 children, then the process count of its own cgroup.
const FORK_SCRIPT: &str = "i=0; while [ $i -lt 16 ]; do sleep 2 & i=$((i+1)); done; echo \"count=$(wc -l < /sys/fs/cgroup$(sed -n 's/^0:://p' /proc/self/cgroup)/cgroup.procs)\"; wait";

/// P-LIMITS.
pub fn probe_limits(d: Option<&Delegated>) -> ProbeResult {
    let control_text =
        "the fork workload in a cgroup without pids.max must exceed the count (all 16 children alive, no pids.events max)";
    let Some(d) = d else {
        return prerequisite(
            ProbeId::Limits,
            "P-CGROUP (delegated subtree)",
            control_text,
        );
    };
    let mem_script = "exec dd if=/dev/zero of=/dev/null bs=64M count=1";
    let mem = limited_run(
        d,
        "job-probe-mem",
        &[("memory.max", "33554432"), ("memory.swap.max", "0")],
        mem_script,
    );
    let pids = limited_run(d, "job-probe-pids", &[("pids.max", "4")], FORK_SCRIPT);
    let cpu = limited_run(
        d,
        "job-probe-cpu",
        &[("cpu.max", "10000 100000")],
        "timeout 2 sh -c 'while :; do :; done'",
    );
    let mut obs = Vec::new();
    let oom = mem.as_ref().ok().and_then(|r| counter(&r.1, "oom_kill"));
    let pmax = pids.as_ref().ok().and_then(|r| counter(&r.2, "max"));
    let thr = cpu
        .as_ref()
        .ok()
        .and_then(|r| counter(&r.3, "nr_throttled"));
    for (k, r) in [("memory", &mem), ("pids", &pids), ("cpu", &cpu)] {
        if let Err(e) = r {
            obs.push(o(&format!("{k}_error"), e.clone()));
        }
    }
    obs.push(o(
        "memory_oom_kill",
        oom.map_or("unobserved".into(), |v| v.to_string()),
    ));
    obs.push(o(
        "pids_max_events",
        pmax.map_or("unobserved".into(), |v| v.to_string()),
    ));
    obs.push(o(
        "cpu_nr_throttled",
        thr.map_or("unobserved".into(), |v| v.to_string()),
    ));
    let (status, reason) = eval_limits(oom, pmax, thr);
    let control = if status == Status::Ok {
        match limited_run(d, "job-probe-pids-control", &[], FORK_SCRIPT) {
            Ok((out, _, events, _)) => {
                let count: u32 = kv_get(&kv(&out.stdout), "count")
                    .and_then(|v| v.parse().ok())
                    .unwrap_or(0);
                let exceeded = count > 16 && counter(&events, "max").unwrap_or(0) == 0;
                Control::runtime(
                    exceeded,
                    format!(
                        "{control_text}: {count} processes, pids.events max {:?}",
                        counter(&events, "max")
                    ),
                )
            }
            Err(e) => Control::runtime(false, format!("{control_text}: {e}")),
        }
    } else {
        Control::runtime_not_run(control_text)
    };
    ProbeResult::new(ProbeId::Limits, status, reason, obs, control)
}

// ---------------------------------------------------------------------------------------------
// Filesystem semantics
// ---------------------------------------------------------------------------------------------

/// Observations of P-OPENAT2.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Openat2Obs {
    /// A plain path beneath the test root with every flag.
    pub inside: Result<(), Errno>,
    /// `../outside` with `RESOLVE_BENEATH`.
    pub beneath_escape: Result<(), Errno>,
    /// A symlink component with `RESOLVE_NO_SYMLINKS`.
    pub symlink: Result<(), Errno>,
    /// A mount crossing with `RESOLVE_NO_XDEV` (a bind mount inside a test root prepared by the
    /// VM trial, else the `/` to `/proc` boundary).
    pub mount_crossing: Result<(), Errno>,
    /// `statx` reported a mount id, and the crossed mount has a different one.
    pub mount_id: bool,
    /// `statx` reported a birth time (`STATX_BTIME`).
    pub btime: bool,
}

/// P-OPENAT2 evaluation.
pub fn eval_openat2(x: &Openat2Obs) -> (Status, String) {
    match x.inside {
        Err(e) if e == Errno::NOSYS => {
            return (Status::Missing, "openat2 not supported".to_string())
        }
        Err(e) if e == Errno::INVAL || e == Errno::TOOBIG => {
            return (
                Status::Missing,
                format!("resolve flags not supported: {}", errno_name(e)),
            )
        }
        Err(e) => {
            return (
                Status::Error,
                format!("open beneath the test root failed: {}", errno_name(e)),
            )
        }
        Ok(()) => {}
    }
    let checks = [
        ("RESOLVE_BENEATH", x.beneath_escape, Errno::XDEV),
        ("RESOLVE_NO_SYMLINKS", x.symlink, Errno::LOOP),
        ("RESOLVE_NO_XDEV", x.mount_crossing, Errno::XDEV),
    ];
    for (flag, r, want) in checks {
        if r != Err(want) {
            return (
                Status::Missing,
                format!("{flag} not honoured: {}", res_name(&r)),
            );
        }
    }
    if !x.mount_id {
        return (
            Status::Missing,
            "statx did not report distinguishing mount ids".to_string(),
        );
    }
    if !x.btime {
        return (
            Status::Missing,
            "statx did not report a birth time".to_string(),
        );
    }
    (
        Status::Ok,
        "RESOLVE_BENEATH, RESOLVE_NO_SYMLINKS, RESOLVE_NO_XDEV honoured; statx mount id and birth time reported".to_string(),
    )
}

fn open2(dir: &impl rustix::fd::AsFd, path: &str, resolve: ResolveFlags) -> Result<(), Errno> {
    rustix::fs::openat2(
        dir,
        path,
        OFlags::RDONLY | OFlags::CLOEXEC,
        Mode::empty(),
        resolve,
    )
    .map(drop)
}

fn statx_of(p: &Path) -> Option<rustix::fs::Statx> {
    rustix::fs::statx(
        CWD,
        p,
        AtFlags::SYMLINK_NOFOLLOW,
        StatxFlags::MNT_ID | StatxFlags::BTIME,
    )
    .ok()
}

fn has_mask(st: &rustix::fs::Statx, f: StatxFlags) -> bool {
    StatxFlags::from_bits_retain(st.stx_mask) & f == f
}

fn mnt_id(p: &Path) -> Option<u64> {
    statx_of(p)
        .filter(|st| has_mask(st, StatxFlags::MNT_ID))
        .map(|st| st.stx_mnt_id)
}

fn dir_fd(p: &Path) -> Result<rustix::fd::OwnedFd, Errno> {
    rustix::fs::open(
        p,
        OFlags::PATH | OFlags::DIRECTORY | OFlags::CLOEXEC,
        Mode::empty(),
    )
}

/// P-OPENAT2.
pub fn probe_openat2(cfg: &Config) -> ProbeResult {
    let control_text =
        "the same opens without resolve flags must succeed (including across the mount)";
    let fail = |st: Status, why: String| {
        ProbeResult::new(
            ProbeId::Openat2,
            st,
            why,
            vec![],
            Control::runtime_not_run(control_text),
        )
    };
    let scratch = match Scratch::new(&cfg.scratch, "openat2") {
        Ok(s) => s,
        Err(e) => return fail(Status::Missing, format!("scratch root unusable: {e}")),
    };
    let root = scratch.path().join("root");
    let setup = (|| -> std::io::Result<rustix::fd::OwnedFd> {
        fs::create_dir_all(root.join("inner"))?;
        fs::write(root.join("inner/file"), b"x")?;
        fs::write(scratch.path().join("outside"), b"x")?;
        std::os::unix::fs::symlink("inner", root.join("link"))?;
        Ok(dir_fd(&root)?)
    })();
    let rfd = match setup {
        Ok(f) => f,
        Err(e) => return fail(Status::Error, format!("scratch setup failed: {e}")),
    };
    // The mount crossing: the trial's bind mount `<xdev_root>/root/mnt` (file `f`), else `/proc`.
    let (cross_dir, cross_path, crossed) = match &cfg.xdev_root {
        Some(x) => (x.join("root"), "mnt/f", x.join("root/mnt")),
        None => (
            PathBuf::from("/"),
            "proc/self/status",
            PathBuf::from("/proc"),
        ),
    };
    let cfd = match dir_fd(&cross_dir) {
        Ok(f) => f,
        Err(e) => {
            return fail(
                Status::Error,
                format!("open {}: {}", cross_dir.display(), errno_name(e)),
            )
        }
    };
    let all = ResolveFlags::BENEATH | ResolveFlags::NO_SYMLINKS | ResolveFlags::NO_XDEV;
    let st = statx_of(scratch.path());
    let base_mnt = mnt_id(&cross_dir);
    let crossed_mnt = mnt_id(&crossed);
    let x = Openat2Obs {
        inside: open2(&rfd, "inner/file", all),
        beneath_escape: open2(&rfd, "../outside", ResolveFlags::BENEATH),
        symlink: open2(&rfd, "link/file", ResolveFlags::NO_SYMLINKS),
        mount_crossing: open2(&cfd, cross_path, ResolveFlags::NO_XDEV),
        mount_id: matches!((base_mnt, crossed_mnt), (Some(a), Some(b)) if a != b),
        btime: st.as_ref().is_some_and(|s| has_mask(s, StatxFlags::BTIME)),
    };
    let obs = vec![
        o("inside", res_name(&x.inside)),
        o("beneath_escape", res_name(&x.beneath_escape)),
        o("symlink_component", res_name(&x.symlink)),
        o(
            "mount_crossing",
            format!("{} ({})", res_name(&x.mount_crossing), crossed.display()),
        ),
        o(
            "base_mount_id",
            base_mnt.map_or("unreported".into(), |v| v.to_string()),
        ),
        o(
            "crossed_mount_id",
            crossed_mnt.map_or("unreported".into(), |v| v.to_string()),
        ),
        o("btime", x.btime.to_string()),
    ];
    let (status, reason) = eval_openat2(&x);
    let control = if status == Status::Ok {
        let none = ResolveFlags::empty();
        let c = [
            open2(&rfd, "../outside", none),
            open2(&rfd, "link/file", none),
            open2(&cfd, cross_path, none),
        ];
        let all_ok = c.iter().all(Result::is_ok);
        Control::runtime(
            all_ok,
            format!(
                "{control_text}: {}",
                c.iter().map(res_name).collect::<Vec<_>>().join("; ")
            ),
        )
    } else {
        Control::runtime_not_run(control_text)
    };
    ProbeResult::new(ProbeId::Openat2, status, reason, obs, control)
}

/// Observations of P-RENAME.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct RenameObs {
    /// `RENAME_NOREPLACE` onto an existing file.
    pub over_existing: Result<(), Errno>,
    /// The existing destination kept its content.
    pub existing_intact: bool,
    /// `RENAME_NOREPLACE` onto a new name.
    pub to_new: Result<(), Errno>,
    /// `RENAME_EXCHANGE` of two files.
    pub exchange: Result<(), Errno>,
    /// Their contents were swapped.
    pub swapped: bool,
    /// The test root filesystem is on the allow-list.
    pub fs_allowed: bool,
}

/// P-RENAME evaluation.
pub fn eval_rename(x: &RenameObs) -> (Status, String) {
    if !x.fs_allowed {
        return (
            Status::Missing,
            "test root filesystem is not on the allow-list (ext4 with journal or xfs)".to_string(),
        );
    }
    match x.over_existing {
        Err(e) if e == Errno::INVAL || e == Errno::NOSYS => {
            return (
                Status::Missing,
                format!("RENAME_NOREPLACE unsupported: {}", errno_name(e)),
            )
        }
        Err(e) if e == Errno::EXIST && x.existing_intact && x.to_new.is_ok() => {}
        Err(e) if e == Errno::EXIST => {
            return (
                Status::Error,
                format!(
                    "destination changed or rename to a new name failed: {}",
                    res_name(&x.to_new)
                ),
            )
        }
        Err(e) => return (Status::Error, format!("unexpected: {}", errno_name(e))),
        Ok(()) => {
            return (
                Status::Missing,
                "RENAME_NOREPLACE replaced an existing destination".to_string(),
            )
        }
    }
    match x.exchange {
        Ok(()) if x.swapped => (
            Status::Ok,
            "RENAME_NOREPLACE refuses an existing destination; RENAME_EXCHANGE swaps".to_string(),
        ),
        Ok(()) => (
            Status::Error,
            "RENAME_EXCHANGE reported success without swapping".to_string(),
        ),
        Err(e) => (
            Status::Missing,
            format!("RENAME_EXCHANGE unsupported: {}", errno_name(e)),
        ),
    }
}

/// P-RENAME (on the scratch root, which must share the target root's filesystem).
pub fn probe_rename(cfg: &Config) -> ProbeResult {
    let control_text = "a plain rename onto the existing destination must replace it (VM trial: a tmpfs-backed root is reported unsupported)";
    let fail = |st: Status, why: String| {
        ProbeResult::new(
            ProbeId::Rename,
            st,
            why,
            vec![],
            Control::runtime_not_run(control_text),
        )
    };
    let scratch = match Scratch::new(&cfg.scratch, "rename") {
        Ok(s) => s,
        Err(e) => return fail(Status::Missing, format!("scratch root unusable: {e}")),
    };
    let f = |n: &str| scratch.path().join(n);
    if let Err(e) = fs::write(f("a"), b"A")
        .and_then(|()| fs::write(f("b"), b"B"))
        .and_then(|()| fs::write(f("d"), b"D"))
    {
        return fail(Status::Error, format!("scratch setup failed: {e}"));
    }
    let read = |n: &str| fs::read(f(n)).unwrap_or_default();
    let over = rustix::fs::renameat_with(CWD, f("a"), CWD, f("b"), RenameFlags::NOREPLACE);
    let intact = read("b") == b"B";
    let to_new = rustix::fs::renameat_with(CWD, f("a"), CWD, f("c"), RenameFlags::NOREPLACE);
    let exchange = rustix::fs::renameat_with(CWD, f("c"), CWD, f("d"), RenameFlags::EXCHANGE);
    let swapped = read("c") == b"D" && read("d") == b"A";
    let class = classify(scratch.path());
    let x = RenameObs {
        over_existing: over,
        existing_intact: intact,
        to_new,
        exchange,
        swapped,
        fs_allowed: class.as_ref().is_ok_and(|c| c.allowed().is_ok()),
    };
    let mut obs = vec![
        o("onto_existing", res_name(&over)),
        o("existing_intact", intact.to_string()),
        o("onto_new_name", res_name(&to_new)),
        o("exchange", res_name(&exchange)),
        o("swapped", swapped.to_string()),
        o(
            "test_root_fs",
            class.as_ref().map_or_else(Clone::clone, FsFacts::describe),
        ),
    ];
    let scratch_dev = fs::metadata(scratch.path()).map(|m| m.dev()).ok();
    let target_dev = fs::metadata(&cfg.target_root).map(|m| m.dev()).ok();
    obs.push(o(
        "same_device_as_target_root",
        format!("{:?}", scratch_dev.zip(target_dev).map(|(a, b)| a == b)),
    ));
    let (status, reason) = match (scratch_dev, target_dev) {
        (Some(a), Some(b)) if a != b => (
            Status::Missing,
            "scratch root is not on the target root's filesystem; observe with --scratch on it"
                .to_string(),
        ),
        _ => eval_rename(&x),
    };
    let control = if status == Status::Ok {
        let plain = rustix::fs::renameat(CWD, f("c"), CWD, f("b"));
        let replaced = plain.is_ok() && read("b") == b"D";
        Control::runtime(replaced, format!("{control_text}: {}", res_name(&plain)))
    } else {
        Control::runtime_not_run(control_text)
    };
    ProbeResult::new(ProbeId::Rename, status, reason, obs, control)
}

/// The filesystem type of the mount with id `mnt_id` in `/proc/self/mountinfo` text.
pub fn mount_type(mountinfo: &str, mnt_id: u64) -> Option<String> {
    mountinfo.lines().find_map(|l| {
        let first = l.split_whitespace().next()?.parse::<u64>().ok()?;
        if first != mnt_id {
            return None;
        }
        let (_, tail) = l.split_once(" - ")?;
        tail.split_whitespace().next().map(str::to_string)
    })
}

/// Observed facts of the filesystem holding a path.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FsFacts {
    /// statfs magic.
    pub magic: i64,
    /// Type in the mount table.
    pub mount_type: String,
    /// Block device name (`/sys/dev/block/<major>:<minor>`).
    pub device: Option<String>,
    /// ext4 journal observed (`/proc/fs/jbd2/<device>-8`); `None` for other types.
    pub has_journal: Option<bool>,
    /// Filesystem UUID observed through `/dev/disk/by-uuid`.
    pub uuid: Option<String>,
}

impl FsFacts {
    /// Allow-list (J-7.2): ext4 with an observed journal, or xfs; never tmpfs, overlay, network
    /// or FUSE. The ext2/3/4 magic is shared, so the mount table type must agree.
    pub fn allowed(&self) -> Result<(), String> {
        match (self.magic, self.mount_type.as_str()) {
            (0xEF53, "ext4") if self.has_journal == Some(true) => Ok(()),
            (0xEF53, "ext4") => Err("ext4 without an observed journal".to_string()),
            (0x5846_5342, "xfs") => Ok(()),
            (m, t) => Err(format!("{t} ({m:#x}) is not on the allow-list")),
        }
    }

    fn describe(&self) -> String {
        format!(
            "{} ({:#x}) device {} journal {:?} uuid {}",
            self.mount_type,
            self.magic,
            self.device.as_deref().unwrap_or("unknown"),
            self.has_journal,
            self.uuid.as_deref().unwrap_or("unobserved")
        )
    }
}

/// Major and minor numbers of a Linux `dev_t`.
pub fn dev_major_minor(dev: u64) -> (u64, u64) {
    (
        ((dev >> 8) & 0xfff) | ((dev >> 32) & !0xfff),
        (dev & 0xff) | ((dev >> 12) & !0xff),
    )
}

fn classify(p: &Path) -> Result<FsFacts, String> {
    let magic = statfs_magic(p).map_err(errno_name)?;
    let id = mnt_id(p).ok_or_else(|| "no mount id".to_string())?;
    let info = fs::read_to_string("/proc/self/mountinfo").map_err(|e| e.to_string())?;
    let mount_type = mount_type(&info, id).unwrap_or_else(|| "unknown".into());
    let (maj, min) = dev_major_minor(fs::metadata(p).map_err(|e| e.to_string())?.dev());
    let device = fs::read_link(format!("/sys/dev/block/{maj}:{min}"))
        .ok()
        .and_then(|t| t.file_name().map(|n| n.to_string_lossy().into_owned()));
    let has_journal = (mount_type == "ext4").then(|| {
        device
            .as_ref()
            .is_some_and(|d| Path::new(&format!("/proc/fs/jbd2/{d}-8")).exists())
    });
    let uuid = device.as_ref().and_then(|d| {
        fs::read_dir("/dev/disk/by-uuid")
            .ok()?
            .flatten()
            .find_map(|e| {
                let target = fs::canonicalize(e.path()).ok()?;
                (target.file_name()?.to_string_lossy() == d.as_str())
                    .then(|| e.file_name().to_string_lossy().into_owned())
            })
    });
    Ok(FsFacts {
        magic,
        mount_type,
        device,
        has_journal,
        uuid,
    })
}

/// The operator's filesystem record (`<config>/filesystems`, written by setup from `dumpe2fs -h`
/// or `xfs_info`): lines `path=<path> uuid=<uuid> features=<comma list>`.
pub fn fs_record(text: &str, path: &Path) -> Option<(String, Vec<String>)> {
    let want = path.display().to_string();
    text.lines().find_map(|l| {
        let (mut p, mut u, mut f) = (None, None, Vec::new());
        for field in l.split_whitespace() {
            if let Some(v) = field.strip_prefix("path=") {
                p = Some(v);
            } else if let Some(v) = field.strip_prefix("uuid=") {
                u = Some(v.to_string());
            } else if let Some(v) = field.strip_prefix("features=") {
                f = v
                    .split(',')
                    .filter(|x| !x.is_empty())
                    .map(str::to_string)
                    .collect();
            }
        }
        (p == Some(want.as_str()))
            .then_some(u)
            .flatten()
            .map(|u| (u, f))
    })
}

/// P-FS-TYPE evaluation of one root: observed type allowed, observed UUID equal to the recorded
/// one, recorded features without `casefold` (and with `has_journal` for ext4).
pub fn eval_fs_entry(f: &FsFacts, record: Option<&(String, Vec<String>)>) -> Result<(), String> {
    f.allowed()?;
    let uuid = f
        .uuid
        .as_deref()
        .ok_or("filesystem UUID not resolvable through /dev/disk/by-uuid")?;
    let (ru, rf) = record.ok_or("no operator filesystem record")?;
    if ru != uuid {
        return Err(format!("UUID {uuid} differs from the recorded {ru}"));
    }
    if rf.iter().any(|x| x == "casefold") {
        return Err("recorded features include casefold".to_string());
    }
    if f.mount_type == "ext4" && !rf.iter().any(|x| x == "has_journal") {
        return Err("recorded ext4 features lack has_journal".to_string());
    }
    Ok(())
}

/// P-FS-TYPE.
pub fn probe_fs_type(cfg: &Config) -> ProbeResult {
    let control_text = "the same classification of a tmpfs (/dev/shm) must be refused";
    let record = fs::read_to_string(cfg.config_dir.join("filesystems")).unwrap_or_default();
    let (mut obs, mut bad) = (Vec::new(), Vec::new());
    for (name, p) in [
        ("state_root", &cfg.state_root),
        ("target_root", &cfg.target_root),
    ] {
        match classify(p) {
            Ok(f) => {
                obs.push(o(name, f.describe()));
                if let Err(e) = eval_fs_entry(&f, fs_record(&record, p).as_ref()) {
                    bad.push(format!("{name}: {e}"));
                }
            }
            Err(e) => {
                obs.push(o(name, format!("unobserved: {e}")));
                bad.push(format!("{name} {} not observable ({e})", p.display()));
            }
        }
    }
    let (status, reason) = if bad.is_empty() {
        (
            Status::Ok,
            "state and target roots on allow-listed local filesystems matching the operator record"
                .to_string(),
        )
    } else {
        (Status::Missing, bad.join("; "))
    };
    let control = if status == Status::Ok {
        match classify(Path::new("/dev/shm")) {
            Ok(f) => Control::runtime(
                f.allowed().is_err(),
                format!("{control_text}: {}", f.describe()),
            ),
            Err(e) => Control::runtime(false, format!("{control_text}: {e}")),
        }
    } else {
        Control::runtime_not_run(control_text)
    };
    ProbeResult::new(ProbeId::FsType, status, reason, obs, control)
}

/// P-LANDLOCK (optional): listed among the active LSMs.
pub fn probe_landlock() -> ProbeResult {
    let lsm = fs::read_to_string("/sys/kernel/security/lsm").unwrap_or_default();
    let listed = lsm.split(',').any(|x| x.trim() == "landlock");
    ProbeResult::new(
        ProbeId::Landlock,
        if listed { Status::Ok } else { Status::Missing },
        if listed {
            "listed in the active LSMs (ABI not probed: no safe syscall wrapper in the reviewed features)"
        } else {
            "not listed in the active LSMs"
        },
        vec![o("lsm", lsm.trim().to_string())],
        Control::none("optional defence in depth (D-045 M5-D2); never part of the verdict"),
    )
}

/// Run every probe in J-9 order.
pub fn run_all(cfg: &Config, versions: &[(String, String)]) -> Vec<ProbeResult> {
    let userns = probe_userns(cfg);
    let userns_ok = userns.status == Status::Ok;
    let (cgroup, delegated) = probe_cgroup(cfg);
    let mut v = vec![
        userns,
        deny::probe_agent_userns(cfg),
        probe_fs(cfg, userns_ok),
        probe_net(cfg, userns_ok),
        probe_pid(cfg, userns_ok),
        cgroup,
        probe_kill(delegated.as_ref()),
        probe_limits(delegated.as_ref()),
        probe_fd(cfg, userns_ok),
        probe_openat2(cfg),
        probe_rename(cfg),
        probe_fs_type(cfg),
        deny::probe_acl(cfg),
        deny::probe_hardlink(cfg),
        deny::probe_peercred(),
        deny::probe_agent_deny(cfg),
    ];
    for id in [
        ProbeId::JobIdentity,
        ProbeId::JobDeny,
        ProbeId::LauncherAdmit,
        ProbeId::JobSerial,
    ] {
        v.push(crate::jobprobes::probe(cfg, id));
    }
    let secret = deny::probe_no_secret(cfg, &v, versions);
    v.push(secret);
    v.push(probe_landlock());
    v
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;

    fn p(items: &[(&str, &str)]) -> Vec<(String, String)> {
        items.iter().map(|(k, v)| o(k, *v)).collect()
    }

    #[test]
    fn userns_requires_every_namespace_distinct() {
        let host = p(&[
            ("ns_user", "user:[1]"),
            ("ns_pid", "pid:[2]"),
            ("ns_net", "net:[3]"),
            ("ns_ipc", "ipc:[4]"),
            ("ns_uts", "uts:[5]"),
            ("ns_cgroup", "cgroup:[6]"),
            ("ns_mnt", "mnt:[7]"),
        ]);
        let job = p(&[
            ("ns_user", "user:[11]"),
            ("ns_pid", "pid:[12]"),
            ("ns_net", "net:[13]"),
            ("ns_ipc", "ipc:[14]"),
            ("ns_uts", "uts:[15]"),
            ("ns_cgroup", "cgroup:[16]"),
            ("ns_mnt", "mnt:[17]"),
        ]);
        assert_eq!(eval_userns(true, "", &host, &job).0, Status::Ok);
        let mut shared = job.clone();
        shared[2].1 = "net:[3]".into();
        let (st, why) = eval_userns(true, "", &host, &shared);
        assert_eq!(st, Status::Missing);
        assert!(why.contains("net"));
        assert_eq!(
            eval_userns(
                false,
                "bwrap: setting up uid map: Permission denied",
                &host,
                &[]
            )
            .0,
            Status::Missing
        );
        assert_eq!(eval_userns(true, "", &host, &job[..6]).0, Status::Missing);
    }

    #[test]
    fn userns_missing_binary_is_missing() {
        let cfg = Config {
            bwrap: "/nonexistent/writ-probe-bwrap".into(),
            ..Config::reference()
        };
        let r = probe_userns(&cfg);
        assert_eq!(r.status, Status::Missing);
        assert_eq!(r.reason, "private bubblewrap copy not found");
    }

    #[test]
    fn fs_evaluation() {
        let all_refused = p(&[
            ("read_srv", "refused"),
            ("write_usr", "refused"),
            ("read_state", "refused"),
            ("read_keys", "refused"),
            ("read_config", "refused"),
            ("reach_run", "refused"),
            ("reach_cgroup", "refused"),
            ("read_sentinel", "refused"),
        ]);
        assert_eq!(eval_fs(&all_refused, false, true).0, Status::Ok);
        let mut extra_bind = all_refused.clone();
        extra_bind.push(o("read_sentinel", "reachable"));
        assert_eq!(eval_fs(&extra_bind, false, true).0, Status::Missing);
        assert_eq!(eval_fs(&all_refused, true, true).0, Status::Missing);
        assert_eq!(eval_fs(&all_refused, false, false).0, Status::Error);
        assert_eq!(
            eval_fs(&all_refused[1..], false, true).0,
            Status::Missing,
            "/srv/writ check absent"
        );
    }

    #[test]
    fn net_evaluation() {
        let ok = [
            ("ifaces", "lo,"),
            ("routes", "0"),
            ("host_loopback", "refused"),
            ("host_abstract", "0"),
        ];
        assert_eq!(eval_net(&p(&ok)).0, Status::Ok);
        assert_eq!(
            eval_net(&p(&[
                ("ifaces", "eth0,lo,"),
                ("routes", "2"),
                ("host_loopback", "reachable"),
                ("host_abstract", "1")
            ]))
            .0,
            Status::Missing
        );
        let mut lo = ok;
        lo[2].1 = "reachable";
        assert_eq!(eval_net(&p(&lo)).0, Status::Missing);
        let mut abs = ok;
        abs[3].1 = "1";
        assert_eq!(
            eval_net(&p(&abs)).0,
            Status::Missing,
            "host abstract socket visible"
        );
    }

    #[test]
    fn pid_evaluation_and_uncontained_check_discriminates() {
        assert_eq!(
            eval_pid(&p(&[
                ("procs", "3"),
                ("signal", "refused"),
                ("proc_visible", "no")
            ]))
            .0,
            Status::Ok
        );
        assert_eq!(
            eval_pid(&p(&[
                ("procs", "300"),
                ("signal", "refused"),
                ("proc_visible", "no")
            ]))
            .0,
            Status::Missing
        );
        let me = i32::try_from(std::process::id()).unwrap();
        let out = uncontained(&pid_script(me)).unwrap();
        assert_eq!(
            eval_pid(&kv(&out.stdout)).0,
            Status::Missing,
            "{}",
            out.stdout
        );
    }

    #[test]
    fn fd_evaluation_and_inherited_descriptor_is_reported() {
        assert_eq!(
            eval_fd(&p(&[
                ("fd", "0:/dev/null"),
                ("fd", "1:pipe:[1]"),
                ("fd", "2:pipe:[2]"),
                ("fd", "3:")
            ]))
            .0,
            Status::Ok
        );
        assert_eq!(
            eval_fd(&p(&[("fd", "0:/dev/null"), ("fd", "7:/dev/null")])).0,
            Status::Missing
        );
        assert_eq!(eval_fd(&[]).0, Status::Error);
        // The control's wrapper shape: an exec'd program inherits descriptor 7.
        let argv = [
            "/bin/sh",
            "-c",
            "exec 7</dev/null; exec \"$@\"",
            "sh",
            "/bin/sh",
            "-c",
            FD_SCRIPT,
        ];
        let out = exec::run_argv(&argv, JOB_DEADLINE).unwrap();
        let (st, why) = eval_fd(&kv(&out.stdout));
        assert_eq!(st, Status::Missing, "{}", out.stdout);
        assert!(why.contains("7->/dev/null"));
    }

    #[test]
    fn cgroup_parsers_and_missing_cgroup_kill() {
        assert_eq!(
            own_cgroup("0::/system.slice/writ.service\n").as_deref(),
            Some("/system.slice/writ.service")
        );
        assert_eq!(own_cgroup("12:memory:/x\n"), None, "cgroup v1 only");
        assert!(missing_controllers("cpuset cpu io memory hugetlb pids rdma misc").is_empty());
        assert_eq!(missing_controllers("cpu io memory"), vec!["pids"]);
        assert_eq!(
            missing_job_files(|f| f != "cgroup.kill"),
            vec!["cgroup.kill"]
        );
        assert!(missing_job_files(|_| true).is_empty());
        assert_eq!(populated("populated 0\nfrozen 0\n"), Some(false));
        assert_eq!(populated("populated 1\n"), Some(true));
        assert_eq!(
            counter("low 0\nhigh 0\nmax 3\noom 1\noom_kill 1\n", "oom_kill"),
            Some(1)
        );
        assert_eq!(counter("max 0\n", "oom_kill"), None);
    }

    #[test]
    fn kill_and_limits_evaluation() {
        assert_eq!(
            eval_kill(2, 1, true),
            (Status::Ok, eval_kill(2, 1, true).1, true)
        );
        assert_eq!(eval_kill(2, 1, false).0, Status::Missing);
        assert_eq!(eval_kill(1, 0, true).0, Status::Error);
        assert!(
            !eval_kill(2, 0, true).2,
            "a group kill that removed everything means the control did not fail"
        );
        assert_eq!(eval_limits(Some(1), Some(4), Some(9)).0, Status::Ok);
        assert_eq!(eval_limits(Some(0), Some(4), Some(9)).0, Status::Missing);
        assert_eq!(eval_limits(Some(1), None, Some(9)).0, Status::Missing);
    }

    #[test]
    fn openat2_evaluation_including_missing_flags() {
        let good = Openat2Obs {
            inside: Ok(()),
            beneath_escape: Err(Errno::XDEV),
            symlink: Err(Errno::LOOP),
            mount_crossing: Err(Errno::XDEV),
            mount_id: true,
            btime: true,
        };
        assert_eq!(eval_openat2(&good).0, Status::Ok);
        for bad in [
            Openat2Obs {
                inside: Err(Errno::NOSYS),
                ..good
            },
            Openat2Obs {
                inside: Err(Errno::INVAL),
                ..good
            },
            Openat2Obs {
                beneath_escape: Ok(()),
                ..good
            },
            Openat2Obs {
                symlink: Ok(()),
                ..good
            },
            Openat2Obs {
                mount_crossing: Ok(()),
                ..good
            },
            Openat2Obs {
                mount_id: false,
                ..good
            },
            Openat2Obs {
                btime: false,
                ..good
            },
        ] {
            assert_eq!(eval_openat2(&bad).0, Status::Missing, "{bad:?}");
        }
        assert!(eval_openat2(&Openat2Obs {
            mount_crossing: Ok(()),
            ..good
        })
        .1
        .contains("NO_XDEV"));
    }

    #[test]
    fn rename_evaluation() {
        let good = RenameObs {
            over_existing: Err(Errno::EXIST),
            existing_intact: true,
            to_new: Ok(()),
            exchange: Ok(()),
            swapped: true,
            fs_allowed: true,
        };
        assert_eq!(eval_rename(&good).0, Status::Ok);
        assert_eq!(
            eval_rename(&RenameObs {
                over_existing: Ok(()),
                existing_intact: false,
                ..good
            })
            .0,
            Status::Missing
        );
        assert_eq!(
            eval_rename(&RenameObs {
                over_existing: Err(Errno::INVAL),
                ..good
            })
            .0,
            Status::Missing
        );
        assert_eq!(
            eval_rename(&RenameObs {
                existing_intact: false,
                ..good
            })
            .0,
            Status::Error
        );
        assert_eq!(
            eval_rename(&RenameObs {
                exchange: Err(Errno::INVAL),
                ..good
            })
            .0,
            Status::Missing
        );
        assert_eq!(
            eval_rename(&RenameObs {
                swapped: false,
                ..good
            })
            .0,
            Status::Error
        );
        assert_eq!(
            eval_rename(&RenameObs {
                fs_allowed: false,
                ..good
            })
            .0,
            Status::Missing,
            "tmpfs-backed root unsupported"
        );
    }

    fn facts(magic: i64, t: &str, journal: Option<bool>, uuid: Option<&str>) -> FsFacts {
        FsFacts {
            magic,
            mount_type: t.into(),
            device: Some("vda1".into()),
            has_journal: journal,
            uuid: uuid.map(str::to_string),
        }
    }

    #[test]
    fn filesystem_allow_list_and_record() {
        assert!(facts(0xEF53, "ext4", Some(true), None).allowed().is_ok());
        assert!(
            facts(0xEF53, "ext4", Some(false), None).allowed().is_err(),
            "ext4 without a journal"
        );
        assert!(facts(0x5846_5342, "xfs", None, None).allowed().is_ok());
        assert!(facts(0xEF53, "ext3", Some(true), None).allowed().is_err());
        assert!(facts(0x0102_1994, "tmpfs", None, None).allowed().is_err());
        assert!(facts(0x794C_7630, "overlay", None, None).allowed().is_err());
        assert!(facts(0x9123_683E, "btrfs", None, None).allowed().is_err());
        let info = "22 1 259:2 / / rw,relatime shared:1 - ext4 /dev/nvme0n1p2 rw\n30 22 0:5 / /dev/shm rw - tmpfs tmpfs rw\n";
        assert_eq!(mount_type(info, 22).as_deref(), Some("ext4"));
        assert_eq!(mount_type(info, 30).as_deref(), Some("tmpfs"));
        assert_eq!(mount_type(info, 99), None);
        let rec = "path=/var/lib/writ uuid=u-1 features=has_journal,extent\npath=/srv/writ/targets uuid=u-1 features=has_journal,casefold\n";
        let ok = facts(0xEF53, "ext4", Some(true), Some("u-1"));
        assert_eq!(
            eval_fs_entry(&ok, fs_record(rec, Path::new("/var/lib/writ")).as_ref()),
            Ok(())
        );
        assert!(
            eval_fs_entry(&ok, fs_record(rec, Path::new("/srv/writ/targets")).as_ref()).is_err(),
            "casefold"
        );
        assert!(eval_fs_entry(&ok, None).is_err(), "no record");
        let other = facts(0xEF53, "ext4", Some(true), Some("u-2"));
        assert!(
            eval_fs_entry(&other, fs_record(rec, Path::new("/var/lib/writ")).as_ref()).is_err(),
            "UUID mismatch"
        );
        assert!(eval_fs_entry(
            &facts(0xEF53, "ext4", Some(true), None),
            fs_record(rec, Path::new("/var/lib/writ")).as_ref()
        )
        .is_err());
        assert_eq!(dev_major_minor(0x10302), (259, 2));
    }

    #[test]
    fn shell_quoting() {
        assert_eq!(sq("/var/lib/writ"), "'/var/lib/writ'");
        assert_eq!(sq("a'b"), "'a'\\''b'");
    }

    #[test]
    fn job_profile_unshares_every_namespace_explicitly() {
        let a: Vec<String> = bwrap_args(Path::new("/b"), None, true, true, &[], "true")
            .iter()
            .map(|x| x.to_string_lossy().into_owned())
            .collect();
        for flag in [
            "--unshare-user",
            "--unshare-pid",
            "--unshare-net",
            "--unshare-ipc",
            "--unshare-uts",
            "--unshare-cgroup",
            "--new-session",
            "--clearenv",
        ] {
            assert!(a.iter().any(|x| x == flag), "{flag}");
        }
        assert!(
            !a.iter()
                .any(|x| x.ends_with("-try") || x == "--unshare-all"),
            "no silently optional namespaces"
        );
        let control = bwrap_args(
            Path::new("/b"),
            Some(Path::new("/w")),
            false,
            false,
            &[],
            "true",
        );
        assert!(!control
            .iter()
            .any(|x| x == "--unshare-net" || x == "--unshare-pid"));
        assert!(control
            .windows(3)
            .any(|w| w[0] == "--ro-bind" && w[1] == "/proc" && w[2] == "/proc"));
        assert!(control
            .windows(3)
            .any(|w| w[0] == "--bind" && w[1] == "/w" && w[2] == "/work"));
    }

    // The in-job scripts run uncontained must report the capability as absent: this checks
    // their syntax and output format here.
    #[test]
    fn job_scripts_uncontained_report_absence() {
        let host: Vec<(String, String)> = NAMESPACES
            .iter()
            .map(|n| {
                (
                    format!("ns_{n}"),
                    fs::read_link(format!("/proc/self/ns/{n}"))
                        .map(|p| p.display().to_string())
                        .unwrap_or_default(),
                )
            })
            .collect();
        let out = uncontained(NS_SCRIPT).unwrap();
        let (st, why) = eval_userns(true, "", &host, &kv(&out.stdout));
        assert_eq!(st, Status::Missing);
        assert!(why.contains("user"), "{why}");

        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../target");
        fs::create_dir_all(&root).unwrap();
        let scratch = Scratch::new(&root, "unit-fs").unwrap();
        let sentinel = scratch.path().join("sentinel");
        fs::create_dir(&sentinel).unwrap();
        fs::write(sentinel.join("secret"), b"s").unwrap();
        let cfg = Config::reference();
        // Redirect only the job's own work-directory write to the scratch directory. A blanket
        // replacement of "/work/" would also rewrite host paths that contain it (the CI checkout
        // lives under /home/runner/work/, run 43668827472), so the sentinel would not be found.
        let job_write = ": > /work/.writ-probe\n";
        let script = fs_script(&cfg, &sentinel);
        assert_eq!(script.matches(job_write).count(), 1, "{script}");
        let script = script.replace(
            job_write,
            &format!(
                ": > {}\n",
                sq(&scratch.path().join(".writ-probe").display().to_string())
            ),
        );
        assert!(
            script.contains(&sq(&sentinel.join("secret").display().to_string())),
            "{script}"
        );
        let out = uncontained(&script).unwrap();
        let pairs = kv(&out.stdout);
        assert_eq!(
            kv_get(&pairs, "read_sentinel"),
            Some("reachable"),
            "{}",
            out.stdout
        );
        assert_eq!(
            eval_fs(&pairs, sentinel.join("written").exists(), true).0,
            Status::Missing
        );

        use std::os::linux::net::SocketAddrExt;
        let name = format!("writ-probe-unit-{}", std::process::id());
        let addr = std::os::unix::net::SocketAddr::from_abstract_name(name.as_bytes()).unwrap();
        let _abs = std::os::unix::net::UnixListener::bind_addr(&addr).unwrap();
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let out = uncontained(&net_script(listener.local_addr().unwrap().port(), &name)).unwrap();
        let pairs = kv(&out.stdout);
        assert_eq!(
            kv_get(&pairs, "host_loopback"),
            Some("reachable"),
            "{}",
            out.stdout
        );
        assert_eq!(kv_get(&pairs, "host_abstract"), Some("1"), "{}", out.stdout);
        assert_eq!(eval_net(&pairs).0, Status::Missing);
    }
}
