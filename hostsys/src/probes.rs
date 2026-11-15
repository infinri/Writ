//! The J-9 capability probes. Every result is an observation made on the running host: a
//! command that ran, a system call that succeeded or was refused, a file that was read. No
//! result is inferred from a version string (D-045 M5-D2).
//!
//! Safety of the probes themselves: they only read, or make unprivileged attempts inside a
//! private scratch directory that they remove. Cgroups are created only in delegated-cgroup
//! mode ([`Config::cgroup_delegated`]), which the reference VM procedure requests explicitly
//! when the probe runs inside its own delegated systemd unit; without it nothing under
//! `/sys/fs/cgroup` is written.

use crate::exec::{self, kv, kv_get, Outcome};
use crate::readiness::{Control, ProbeId, ProbeResult, Status};
use rustix::fs::{AtFlags, Mode, OFlags, RenameFlags, ResolveFlags, StatxFlags, CWD};
use rustix::io::Errno;
use std::ffi::OsString;
use std::fs;
use std::io::Write;
use std::os::unix::fs::{DirBuilderExt, MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::{Duration, Instant};

/// Deadline of one sandboxed job.
const JOB_DEADLINE: Duration = Duration::from_secs(20);

/// Probe configuration (defaults are the J-1 layout of the reference host).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Config {
    /// Trusted state root (`/var/lib/writ`).
    pub state_root: PathBuf,
    /// Configuration directory (`/etc/writ`).
    pub config_dir: PathBuf,
    /// Socket directory (`/run/writ`).
    pub run_dir: PathBuf,
    /// `fs.write` target roots (`/var/lib/writ/targets`).
    pub target_root: PathBuf,
    /// Directory in which private scratch directories are made (`/var/lib/writ/run`, the runner
    /// work root, so filesystem probes run on the filesystem jobs and stores use).
    pub scratch: PathBuf,
    /// The pinned bubblewrap binary.
    pub bwrap: PathBuf,
    /// The probe runs inside its own delegated cgroup (systemd unit with `Delegate=yes`) and may
    /// create cgroups beneath it. Never set on a workstation.
    pub cgroup_delegated: bool,
    /// Pid of the running service (`writ-serviced`), when there is one.
    pub service_pid: Option<i32>,
    /// Report produced by `writ-probe agent-deny` run as the agent uid.
    pub agent_report: Option<PathBuf>,
    /// File holding the planted canary value (not a real key).
    pub canary_file: Option<PathBuf>,
    /// Service log files to scan for key material.
    pub log_files: Vec<PathBuf>,
}

impl Config {
    /// The J-1 reference layout.
    pub fn reference() -> Config {
        Config {
            state_root: "/var/lib/writ".into(),
            config_dir: "/etc/writ".into(),
            run_dir: "/run/writ".into(),
            target_root: "/var/lib/writ/targets".into(),
            scratch: "/var/lib/writ/run".into(),
            bwrap: "/usr/bin/bwrap".into(),
            cgroup_delegated: false,
            service_pid: None,
            agent_report: None,
            canary_file: None,
            log_files: Vec::new(),
        }
    }

    /// The configuration as report pairs.
    pub fn describe(&self) -> Vec<(String, String)> {
        let p = |x: &Path| x.display().to_string();
        let mut v = vec![
            o("state_root", p(&self.state_root)),
            o("config_dir", p(&self.config_dir)),
            o("run_dir", p(&self.run_dir)),
            o("target_root", p(&self.target_root)),
            o("scratch", p(&self.scratch)),
            o("bwrap", p(&self.bwrap)),
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
            o(
                "agent_report",
                self.agent_report.as_deref().map_or("none".to_string(), p),
            ),
            o(
                "canary_file",
                self.canary_file.as_deref().map_or("none".to_string(), p),
            ),
        ];
        for (i, f) in self.log_files.iter().enumerate() {
            v.push(o(&format!("log_file_{i}"), p(f)));
        }
        v
    }
}

fn o(k: &str, v: impl Into<String>) -> (String, String) {
    (k.to_string(), v.into())
}

fn errno_name(e: Errno) -> String {
    let io: std::io::Error = e.into();
    format!("{} ({})", io.kind(), e.raw_os_error())
}

fn res_name(r: &Result<(), Errno>) -> String {
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
/// bound read-write at `/work`.
pub fn bwrap_args(
    bwrap: &Path,
    work: Option<&Path>,
    unshare_net: bool,
    extra: &[OsString],
    script: &str,
) -> Vec<OsString> {
    let mut a: Vec<OsString> = vec![bwrap.into()];
    let mut push = |xs: &[&str]| a.extend(xs.iter().map(OsString::from));
    push(&[
        "--unshare-user",
        "--unshare-ipc",
        "--unshare-pid",
        "--unshare-uts",
        "--unshare-cgroup",
    ]);
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
    push(&["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]);
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
        &bwrap_args(&cfg.bwrap, work, unshare_net, extra, script),
        JOB_DEADLINE,
    )
}

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

/// P-USERNS.
pub fn probe_userns(cfg: &Config) -> ProbeResult {
    let control = Control::vm_trial(
        "VM trial: `sysctl -w user.max_user_namespaces=0`, rerun writ-probe, P-USERNS must be missing; restore the value",
    );
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
        o(
            "kernel.unprivileged_userns_clone",
            sysctl("kernel/unprivileged_userns_clone"),
        ),
    ];
    let meta = match fs::metadata(&cfg.bwrap) {
        Ok(m) if m.is_file() => m,
        _ => {
            return ProbeResult::new(
                ProbeId::Userns,
                Status::Missing,
                "bubblewrap binary not found",
                obs,
                control,
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
    let script = "for n in user pid net ipc uts cgroup mnt; do printf 'ns_%s=' \"$n\"; readlink /proc/self/ns/$n || echo; done";
    let (status, reason) = match job(cfg, None, true, &[], script) {
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
    (
        Status::Ok,
        "only loopback in a new namespace; host loopback and external routes unreachable"
            .to_string(),
    )
}

fn net_script(port: u16) -> String {
    format!(
        "echo \"ifaces=$(tail -n +3 /proc/net/dev | cut -d: -f1 | tr -d ' ' | sort | tr '\\n' ',')\"\n\
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
    let script = net_script(port);
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
    let control_text = "the same check run uncontained must see and signal the target";
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
        match uncontained(&script) {
            Ok(c) => {
                let p = kv(&c.stdout);
                let failed = eval_pid(&p).0 != Status::Ok;
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
    let control_text =
        "the same check run uncontained with an inherited descriptor 7 must report it";
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
    let pairs = kv(&out.stdout);
    let (status, reason) = eval_fd(&pairs);
    let control = if status == Status::Ok {
        match uncontained(&format!("exec 7</dev/null; {FD_SCRIPT}")) {
            Ok(c) => {
                let failed = eval_fd(&kv(&c.stdout)).0 == Status::Missing;
                Control::runtime(
                    failed,
                    format!(
                        "{control_text}: {}",
                        if failed { "reported" } else { "not reported" }
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

/// P-LIMITS.
pub fn probe_limits(d: Option<&Delegated>) -> ProbeResult {
    let control_text =
        "the memory workload in a cgroup without memory.max must complete without an OOM kill";
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
    let pids = limited_run(
        d,
        "job-probe-pids",
        &[("pids.max", "4")],
        "i=0; while [ $i -lt 16 ]; do sleep 2 & i=$((i+1)); done; wait",
    );
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
        match limited_run(d, "job-probe-mem-control", &[], mem_script) {
            Ok((out, events, _, _)) => {
                let unlimited_ok = out.success() && counter(&events, "oom_kill").unwrap_or(0) == 0;
                Control::runtime(
                    unlimited_ok,
                    format!(
                        "{control_text}: exit {:?}, oom_kill {:?}",
                        out.code,
                        counter(&events, "oom_kill")
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
    /// A mount crossing (`/` to `proc`) with `RESOLVE_NO_XDEV`.
    pub mount_crossing: Result<(), Errno>,
    /// `statx` reported a mount id, and `/proc` has a different one.
    pub mount_id: bool,
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
    (
        Status::Ok,
        "RESOLVE_BENEATH, RESOLVE_NO_SYMLINKS, RESOLVE_NO_XDEV honoured; statx mount id reported"
            .to_string(),
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

fn mnt_id(p: &Path) -> Option<u64> {
    let st = rustix::fs::statx(CWD, p, AtFlags::SYMLINK_NOFOLLOW, StatxFlags::MNT_ID).ok()?;
    (StatxFlags::from_bits_retain(st.stx_mask) & StatxFlags::MNT_ID == StatxFlags::MNT_ID)
        .then_some(st.stx_mnt_id)
}

/// P-OPENAT2.
pub fn probe_openat2(cfg: &Config) -> ProbeResult {
    let control_text =
        "the same opens without resolve flags must succeed (the mount crossing is the real / to /proc boundary)";
    let scratch = match Scratch::new(&cfg.scratch, "openat2") {
        Ok(s) => s,
        Err(e) => {
            return ProbeResult::new(
                ProbeId::Openat2,
                Status::Missing,
                format!("scratch root unusable: {e}"),
                vec![],
                Control::runtime_not_run(control_text),
            )
        }
    };
    let root = scratch.path().join("root");
    let setup = (|| -> std::io::Result<rustix::fd::OwnedFd> {
        fs::create_dir_all(root.join("inner"))?;
        fs::write(root.join("inner/file"), b"x")?;
        fs::write(scratch.path().join("outside"), b"x")?;
        std::os::unix::fs::symlink("inner", root.join("link"))?;
        Ok(rustix::fs::open(
            &root,
            OFlags::PATH | OFlags::DIRECTORY | OFlags::CLOEXEC,
            Mode::empty(),
        )?)
    })();
    let rfd = match setup {
        Ok(f) => f,
        Err(e) => {
            return ProbeResult::new(
                ProbeId::Openat2,
                Status::Error,
                format!("scratch setup failed: {e}"),
                vec![],
                Control::runtime_not_run(control_text),
            )
        }
    };
    let slash = match rustix::fs::open(
        "/",
        OFlags::PATH | OFlags::DIRECTORY | OFlags::CLOEXEC,
        Mode::empty(),
    ) {
        Ok(f) => f,
        Err(e) => {
            return ProbeResult::new(
                ProbeId::Openat2,
                Status::Error,
                format!("open /: {}", errno_name(e)),
                vec![],
                Control::runtime_not_run(control_text),
            )
        }
    };
    let all = ResolveFlags::BENEATH | ResolveFlags::NO_SYMLINKS | ResolveFlags::NO_XDEV;
    let scratch_mnt = mnt_id(scratch.path());
    let proc_mnt = mnt_id(Path::new("/proc"));
    let x = Openat2Obs {
        inside: open2(&rfd, "inner/file", all),
        beneath_escape: open2(&rfd, "../outside", ResolveFlags::BENEATH),
        symlink: open2(&rfd, "link/file", ResolveFlags::NO_SYMLINKS),
        mount_crossing: open2(&slash, "proc/self/status", ResolveFlags::NO_XDEV),
        mount_id: matches!((scratch_mnt, proc_mnt), (Some(a), Some(b)) if a != b),
    };
    let obs = vec![
        o("inside", res_name(&x.inside)),
        o("beneath_escape", res_name(&x.beneath_escape)),
        o("symlink_component", res_name(&x.symlink)),
        o("mount_crossing", res_name(&x.mount_crossing)),
        o(
            "scratch_mount_id",
            scratch_mnt.map_or("unreported".into(), |v| v.to_string()),
        ),
        o(
            "proc_mount_id",
            proc_mnt.map_or("unreported".into(), |v| v.to_string()),
        ),
    ];
    let (status, reason) = eval_openat2(&x);
    let control = if status == Status::Ok {
        let none = ResolveFlags::empty();
        let c = [
            open2(&rfd, "../outside", none),
            open2(&rfd, "link/file", none),
            open2(&slash, "proc/self/status", none),
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

/// P-RENAME evaluation.
pub fn eval_rename(
    over_existing: Result<(), Errno>,
    existing_intact: bool,
    to_new: Result<(), Errno>,
) -> (Status, String) {
    match over_existing {
        Err(e) if e == Errno::INVAL || e == Errno::NOSYS => (
            Status::Missing,
            format!("RENAME_NOREPLACE unsupported: {}", errno_name(e)),
        ),
        Err(e) if e == Errno::EXIST && existing_intact && to_new.is_ok() => (
            Status::Ok,
            "RENAME_NOREPLACE refuses an existing destination".to_string(),
        ),
        Err(e) if e == Errno::EXIST => (
            Status::Error,
            format!(
                "destination changed or rename to a new name failed: {}",
                res_name(&to_new)
            ),
        ),
        Err(e) => (Status::Error, format!("unexpected: {}", errno_name(e))),
        Ok(()) => (
            Status::Missing,
            "RENAME_NOREPLACE replaced an existing destination".to_string(),
        ),
    }
}

/// P-RENAME.
pub fn probe_rename(cfg: &Config) -> ProbeResult {
    let control_text = "a plain rename onto the existing destination must replace it";
    let scratch = match Scratch::new(&cfg.scratch, "rename") {
        Ok(s) => s,
        Err(e) => {
            return ProbeResult::new(
                ProbeId::Rename,
                Status::Missing,
                format!("scratch root unusable: {e}"),
                vec![],
                Control::runtime_not_run(control_text),
            )
        }
    };
    let (a, b, c) = (
        scratch.path().join("a"),
        scratch.path().join("b"),
        scratch.path().join("c"),
    );
    if let Err(e) = fs::write(&a, b"A").and_then(|()| fs::write(&b, b"B")) {
        return ProbeResult::new(
            ProbeId::Rename,
            Status::Error,
            format!("scratch setup failed: {e}"),
            vec![],
            Control::runtime_not_run(control_text),
        );
    }
    let over = rustix::fs::renameat_with(CWD, &a, CWD, &b, RenameFlags::NOREPLACE);
    let intact = fs::read(&b).ok().as_deref() == Some(b"B".as_slice());
    let to_new = rustix::fs::renameat_with(CWD, &a, CWD, &c, RenameFlags::NOREPLACE);
    let mut obs = vec![
        o("onto_existing", res_name(&over)),
        o("existing_intact", intact.to_string()),
        o("onto_new_name", res_name(&to_new)),
        o(
            "scratch_dev",
            fs::metadata(scratch.path()).map_or("?".into(), |m| m.dev().to_string()),
        ),
    ];
    let scratch_dev = fs::metadata(scratch.path()).map(|m| m.dev()).ok();
    let target_dev = fs::metadata(&cfg.target_root).map(|m| m.dev()).ok();
    if let Some(d) = target_dev {
        obs.push(o("target_root_dev", d.to_string()));
    }
    let (status, reason) = match (scratch_dev, target_dev) {
        (Some(a), Some(b)) if a != b => (
            Status::Missing,
            "scratch root is not on the target root's filesystem; observe with --scratch on it"
                .to_string(),
        ),
        _ => eval_rename(over, intact, to_new),
    };
    let control = if status == Status::Ok {
        let plain = rustix::fs::renameat(CWD, &c, CWD, &b);
        let replaced = plain.is_ok() && fs::read(&b).ok().as_deref() == Some(b"A".as_slice());
        Control::runtime(replaced, format!("{control_text}: {}", res_name(&plain)))
    } else {
        Control::runtime_not_run(control_text)
    };
    ProbeResult::new(ProbeId::Rename, status, reason, obs, control)
}

/// Filesystem allow-list (J-7): ext4 and xfs, by both the statfs magic and the mount table type
/// (the ext2/3/4 magic is shared).
pub fn fs_allowed(magic: i64, mount_type: &str) -> bool {
    matches!((magic, mount_type), (0xEF53, "ext4") | (0x5846_5342, "xfs"))
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

fn classify(p: &Path) -> Result<(i64, String), String> {
    let magic = statfs_magic(p).map_err(errno_name)?;
    let id = mnt_id(p).ok_or_else(|| "no mount id".to_string())?;
    let info = fs::read_to_string("/proc/self/mountinfo").map_err(|e| e.to_string())?;
    Ok((
        magic,
        mount_type(&info, id).unwrap_or_else(|| "unknown".into()),
    ))
}

/// P-FS-TYPE.
pub fn probe_fs_type(cfg: &Config) -> ProbeResult {
    let control_text = "the same classification of a tmpfs (/dev/shm) must be refused";
    let mut obs = Vec::new();
    let mut bad = Vec::new();
    for (name, p) in [
        ("state_root", &cfg.state_root),
        ("target_root", &cfg.target_root),
    ] {
        match classify(p) {
            Ok((m, t)) => {
                obs.push(o(name, format!("{t} ({m:#x})")));
                if !fs_allowed(m, &t) {
                    bad.push(format!("{name} on {t}"));
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
            "state and target roots on allow-listed local filesystems".to_string(),
        )
    } else {
        (Status::Missing, bad.join("; "))
    };
    let control = if status == Status::Ok {
        match classify(Path::new("/dev/shm")) {
            Ok((m, t)) => Control::runtime(
                !fs_allowed(m, &t),
                format!("{control_text}: /dev/shm is {t} ({m:#x})"),
            ),
            Err(e) => Control::runtime(false, format!("{control_text}: {e}")),
        }
    } else {
        Control::runtime_not_run(control_text)
    };
    ProbeResult::new(ProbeId::FsType, status, reason, obs, control)
}

/// P-PEERCRED: not observable in this crate's reviewed feature set.
pub fn probe_peercred() -> ProbeResult {
    ProbeResult::new(
        ProbeId::Peercred,
        Status::Error,
        "not observable in this build: SO_PEERCRED needs rustix `net` (not in writ-hostsys's reviewed features) or Tokio's peer_cred (writ-rpc, M5a-04); std's peer_cred is unstable on Rust 1.99.1",
        vec![],
        Control::none("no probe implemented; the control (a connection from another uid is reported with that uid) belongs with the implementation"),
    )
}

// ---------------------------------------------------------------------------------------------
// Agent denial and secrets
// ---------------------------------------------------------------------------------------------

/// What a trusted path must look like.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Expect {
    /// Directory (else a socket or regular file as noted by the caller).
    pub dir: bool,
    /// Exact permission bits.
    pub mode: u32,
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
    if mode & 0o7777 != e.mode {
        return Some(format!(
            "mode {:04o}, required {:04o}",
            mode & 0o7777,
            e.mode
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
    match rustix::fs::lgetxattr_query(p, "system.posix_acl_access") {
        Ok(_) => Some(format!("{}: has a POSIX ACL", p.display())),
        Err(_) => None,
    }
}

/// Ancestors of `p` (excluding `p`) that are not owned by root or `uid`, or are group- or
/// other-writable.
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

/// Contract of the agent-side report.
pub const AGENT_CONTRACT: &str = "writ-agent-deny/1";

/// Targets every agent-side report must contain.
pub const AGENT_TARGETS: [&str; 10] = [
    "read_state_root",
    "read_ledger",
    "read_stores",
    "read_keys",
    "write_work_root",
    "write_target_root",
    "connect_admin_sock",
    "read_service_environ",
    "open_service_mem",
    "signal_service",
];

/// Check an agent-side report (strict JSON): produced by a different uid than `service_uid`,
/// every required target present once and refused. Returns the agent uid.
pub fn eval_agent_report(bytes: &[u8], service_uid: u32) -> Result<i64, String> {
    use writ_kernel::json::Value;
    let v = writ_kernel::json::parse(bytes)
        .map_err(|e| format!("agent report is not strict JSON: {}", e.code()))?;
    if v.get("contract").and_then(Value::as_str) != Some(AGENT_CONTRACT) {
        return Err("agent report contract is not writ-agent-deny/1".to_string());
    }
    let agent = v
        .get("agent_uid")
        .and_then(Value::as_int)
        .ok_or("agent_uid missing")?;
    if agent == i64::from(service_uid) {
        return Err("agent report was produced by the service uid".to_string());
    }
    let attempts = v
        .get("attempts")
        .and_then(Value::as_arr)
        .ok_or("attempts missing")?;
    for t in AGENT_TARGETS {
        let found: Vec<&Value> = attempts
            .iter()
            .filter(|a| a.get("target").and_then(Value::as_str) == Some(t))
            .collect();
        match found.as_slice() {
            [a] if a.get("outcome").and_then(Value::as_str) == Some("refused") => {}
            [a] => {
                return Err(format!(
                    "{t}: {}",
                    a.get("outcome").and_then(Value::as_str).unwrap_or("?")
                ))
            }
            [] => return Err(format!("{t}: not attempted")),
            _ => return Err(format!("{t}: duplicated")),
        }
    }
    Ok(agent)
}

/// P-AGENT-DENY: the J-1 layout as the service sees it (owner, exact mode, no ACL, safe
/// ancestors) plus the agent-side report of actual refused attempts.
pub fn probe_agent_deny(cfg: &Config) -> ProbeResult {
    let control_text = "the same mode check on a deliberately mis-moded (0644) test key file must report it (VM trial: in the keys directory)";
    let uid = rustix::process::getuid().as_raw();
    let gid = rustix::process::getgid().as_raw();
    let d = |mode| Expect {
        dir: true,
        mode,
        uid,
        gid: None,
    };
    let sr = &cfg.state_root;
    let layout = [
        (sr.clone(), d(0o700)),
        (sr.join("ledger"), d(0o700)),
        (sr.join("stores"), d(0o700)),
        (sr.join("keys"), d(0o700)),
        (sr.join("run"), d(0o700)),
        (
            cfg.config_dir.clone(),
            Expect {
                dir: true,
                mode: 0o750,
                uid: 0,
                gid: Some(gid),
            },
        ),
        (cfg.run_dir.clone(), d(0o755)),
    ];
    let mut problems: Vec<String> = layout
        .iter()
        .filter_map(|(p, e)| check_path(p, *e))
        .collect();
    problems.extend(ancestor_problems(sr, uid));
    let admin = cfg.run_dir.join("admin.sock");
    let mut obs = vec![o("layout_problems", problems.len().to_string())];
    obs.push(o(
        "admin_sock",
        if admin.exists() { "present" } else { "absent" },
    ));
    let agent = match &cfg.agent_report {
        None => Err(
            "agent-side report not supplied (run `writ-probe agent-deny` as the agent uid)"
                .to_string(),
        ),
        Some(p) => fs::read(p)
            .map_err(|e| format!("agent report: {e}"))
            .and_then(|b| eval_agent_report(&b, uid)),
    };
    match &agent {
        Ok(a) => obs.push(o("agent_uid", a.to_string())),
        Err(e) => obs.push(o("agent_report", e.clone())),
    }
    for (i, p) in problems.iter().enumerate() {
        obs.push(o(&format!("problem_{i}"), p.clone()));
    }
    let (status, reason) = match (problems.first(), &agent) {
        (Some(first), _) => (
            Status::Missing,
            format!("trusted layout not as required: {first}"),
        ),
        (None, Err(e)) => (Status::Missing, e.clone()),
        (None, Ok(_)) => (
            Status::Ok,
            "layout owned and moded as required; every agent attempt refused".to_string(),
        ),
    };
    let control = if status == Status::Ok {
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
                            mode: 0o600,
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

/// P-NO-SECRET: scan the service's command line and environment, the configured logs and the
/// other probe results for the planted canary (and long hex runs, outside the report).
pub fn probe_no_secret(
    cfg: &Config,
    others: &[ProbeResult],
    versions: &[(String, String)],
) -> ProbeResult {
    let control_text = "the scanner applied to a buffer with the planted canary must detect it";
    let canary = match &cfg.canary_file {
        None => {
            return ProbeResult::new(
                ProbeId::NoSecret,
                Status::Missing,
                "no canary planted (--canary-file)",
                vec![],
                Control::runtime_not_run(control_text),
            )
        }
        Some(p) => match read_bounded(p) {
            Ok(b) => String::from_utf8_lossy(&b).trim().as_bytes().to_vec(),
            Err(e) => {
                return ProbeResult::new(
                    ProbeId::NoSecret,
                    Status::Error,
                    format!("canary file: {e}"),
                    vec![],
                    Control::runtime_not_run(control_text),
                )
            }
        },
    };
    if canary.len() < 16 {
        return ProbeResult::new(
            ProbeId::NoSecret,
            Status::Error,
            "canary shorter than 16 bytes",
            vec![],
            Control::runtime_not_run(control_text),
        );
    }
    let Some(pid) = cfg.service_pid else {
        return ProbeResult::new(
            ProbeId::NoSecret,
            Status::Missing,
            "no service process to scan (--service-pid)",
            vec![],
            Control::runtime_not_run(control_text),
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
    let mut obs = Vec::new();
    let mut found = Vec::new();
    let mut unreadable = Vec::new();
    for (name, p) in &targets {
        match read_bounded(p) {
            Ok(b) => {
                let hit = find_secret(&b, &canary);
                obs.push(o(name, hit.unwrap_or("clean").to_string()));
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
    let mut report_text = String::new();
    for r in others {
        report_text.push_str(&r.reason);
        for (k, v) in &r.observations {
            report_text.push_str(k);
            report_text.push_str(v);
        }
    }
    for (k, v) in versions {
        report_text.push_str(k);
        report_text.push_str(v);
    }
    let in_report = !canary.is_empty()
        && report_text
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
        (
            Status::Ok,
            "no canary or key-shaped material in command line, environment, logs or report"
                .to_string(),
        )
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

/// Run every probe in order.
pub fn run_all(cfg: &Config, versions: &[(String, String)]) -> Vec<ProbeResult> {
    let userns = probe_userns(cfg);
    let userns_ok = userns.status == Status::Ok;
    let (cgroup, delegated) = probe_cgroup(cfg);
    let mut v = vec![
        userns,
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
        probe_peercred(),
        probe_agent_deny(cfg),
    ];
    let secret = probe_no_secret(cfg, &v, versions);
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
        assert_eq!(r.reason, "bubblewrap binary not found");
    }

    #[test]
    fn fs_evaluation() {
        let all_refused = p(&[
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
        assert_eq!(eval_fs(&all_refused[1..], false, true).0, Status::Missing);
    }

    #[test]
    fn net_evaluation() {
        assert_eq!(
            eval_net(&p(&[
                ("ifaces", "lo,"),
                ("routes", "0"),
                ("host_loopback", "refused")
            ]))
            .0,
            Status::Ok
        );
        assert_eq!(
            eval_net(&p(&[
                ("ifaces", "eth0,lo,"),
                ("routes", "2"),
                ("host_loopback", "reachable")
            ]))
            .0,
            Status::Missing
        );
        assert_eq!(
            eval_net(&p(&[
                ("ifaces", "lo,"),
                ("routes", "0"),
                ("host_loopback", "reachable")
            ]))
            .0,
            Status::Missing
        );
    }

    #[test]
    fn pid_evaluation_and_uncontained_control_discriminates() {
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
    fn fd_evaluation_and_uncontained_control_discriminates() {
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
        let out = uncontained(&format!("exec 7</dev/null; {FD_SCRIPT}")).unwrap();
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
        let without_kill = missing_job_files(|f| f != "cgroup.kill");
        assert_eq!(without_kill, vec!["cgroup.kill"]);
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
        };
        assert_eq!(eval_openat2(&good).0, Status::Ok);
        assert_eq!(
            eval_openat2(&Openat2Obs {
                inside: Err(Errno::NOSYS),
                ..good
            })
            .0,
            Status::Missing
        );
        assert_eq!(
            eval_openat2(&Openat2Obs {
                inside: Err(Errno::INVAL),
                ..good
            })
            .0,
            Status::Missing
        );
        assert_eq!(
            eval_openat2(&Openat2Obs {
                beneath_escape: Ok(()),
                ..good
            })
            .0,
            Status::Missing
        );
        assert_eq!(
            eval_openat2(&Openat2Obs {
                symlink: Ok(()),
                ..good
            })
            .0,
            Status::Missing
        );
        let (st, why) = eval_openat2(&Openat2Obs {
            mount_crossing: Ok(()),
            ..good
        });
        assert_eq!(st, Status::Missing);
        assert!(why.contains("NO_XDEV"));
        assert_eq!(
            eval_openat2(&Openat2Obs {
                mount_id: false,
                ..good
            })
            .0,
            Status::Missing
        );
    }

    #[test]
    fn rename_evaluation() {
        assert_eq!(eval_rename(Err(Errno::EXIST), true, Ok(())).0, Status::Ok);
        assert_eq!(eval_rename(Ok(()), false, Ok(())).0, Status::Missing);
        assert_eq!(
            eval_rename(Err(Errno::INVAL), true, Ok(())).0,
            Status::Missing
        );
        assert_eq!(
            eval_rename(Err(Errno::EXIST), false, Ok(())).0,
            Status::Error
        );
    }

    #[test]
    fn filesystem_allow_list() {
        assert!(fs_allowed(0xEF53, "ext4"));
        assert!(fs_allowed(0x5846_5342, "xfs"));
        assert!(!fs_allowed(0xEF53, "ext3"));
        assert!(!fs_allowed(0x0102_1994, "tmpfs"));
        assert!(!fs_allowed(0x794C_7630, "overlay"));
        assert!(!fs_allowed(0x9123_683E, "btrfs"));
        let info = "22 1 259:2 / / rw,relatime shared:1 - ext4 /dev/nvme0n1p2 rw\n30 22 0:5 / /dev/shm rw - tmpfs tmpfs rw\n";
        assert_eq!(mount_type(info, 22).as_deref(), Some("ext4"));
        assert_eq!(mount_type(info, 30).as_deref(), Some("tmpfs"));
        assert_eq!(mount_type(info, 99), None);
    }

    #[test]
    fn mode_checks() {
        let e = Expect {
            dir: true,
            mode: 0o700,
            uid: 990,
            gid: None,
        };
        assert_eq!(mode_problem(false, true, 0o40700, 990, 990, e), None);
        assert!(mode_problem(false, true, 0o40750, 990, 990, e).is_some());
        assert!(mode_problem(false, true, 0o40700, 1000, 990, e).is_some());
        assert!(mode_problem(true, false, 0o120777, 990, 990, e).is_some());
        let key = Expect {
            dir: false,
            mode: 0o600,
            uid: 990,
            gid: None,
        };
        assert!(
            mode_problem(false, false, 0o100644, 990, 990, key).is_some(),
            "mis-moded key reported"
        );
        let etc = Expect {
            dir: true,
            mode: 0o750,
            uid: 0,
            gid: Some(990),
        };
        assert!(mode_problem(false, true, 0o40750, 0, 0, etc).is_some());
    }

    fn agent_report(uid: i64, outcomes: &[(&str, &str)]) -> Vec<u8> {
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
        let all: Vec<(&str, &str)> = AGENT_TARGETS.iter().map(|t| (*t, "refused")).collect();
        assert_eq!(eval_agent_report(&agent_report(1000, &all), 990), Ok(1000));
        assert!(
            eval_agent_report(&agent_report(990, &all), 990).is_err(),
            "same uid as the service"
        );
        let mut allowed = all.clone();
        allowed[3].1 = "allowed";
        assert!(eval_agent_report(&agent_report(1000, &allowed), 990)
            .unwrap_err()
            .starts_with("read_keys"));
        assert!(eval_agent_report(&agent_report(1000, &all[1..]), 990).is_err());
        let mut dup = all.clone();
        dup.push(all[0]);
        assert!(eval_agent_report(&agent_report(1000, &dup), 990).is_err());
        assert!(
            eval_agent_report(
                b"{\"contract\":\"writ-agent-deny/1\",\"contract\":\"x\"}",
                990
            )
            .is_err(),
            "strict parse"
        );
    }

    #[test]
    fn secret_scanner() {
        let canary = b"WRIT-CANARY-0123456789abcdef";
        assert_eq!(find_secret(b"PATH=/usr/bin", canary), None);
        assert_eq!(
            find_secret(b"x WRIT-CANARY-0123456789abcdef y", canary),
            Some("canary")
        );
        let hex = "a".repeat(64);
        assert_eq!(
            find_secret(hex.as_bytes(), canary),
            Some("hex run of 64 or more digits")
        );
        assert_eq!(find_secret("a".repeat(63).as_bytes(), canary), None);
    }

    #[test]
    fn shell_quoting() {
        assert_eq!(sq("/var/lib/writ"), "'/var/lib/writ'");
        assert_eq!(sq("a'b"), "'a'\\''b'");
    }

    #[test]
    fn job_profile_unshares_every_namespace_explicitly() {
        let a: Vec<String> = bwrap_args(Path::new("/usr/bin/bwrap"), None, true, &[], "true")
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
        let no_net = bwrap_args(
            Path::new("/usr/bin/bwrap"),
            Some(Path::new("/w")),
            false,
            &[],
            "true",
        );
        assert!(!no_net.iter().any(|x| x == "--unshare-net"));
        assert!(no_net
            .windows(3)
            .any(|w| w[0] == "--bind" && w[1] == "/w" && w[2] == "/work"));
    }

    // The in-job scripts run uncontained must report the capability as absent: this checks
    // their syntax and output format here, and is the shape of each runtime control.
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
        let script = "for n in user pid net ipc uts cgroup mnt; do printf 'ns_%s=' \"$n\"; readlink /proc/self/ns/$n || echo; done";
        let out = uncontained(script).unwrap();
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
        let out = uncontained(
            &fs_script(&cfg, &sentinel)
                .replace("/work/", &format!("{}/", scratch.path().display())),
        )
        .unwrap();
        let pairs = kv(&out.stdout);
        assert_eq!(kv_get(&pairs, "read_sentinel"), Some("reachable"));
        assert_eq!(
            eval_fs(&pairs, sentinel.join("written").exists(), true).0,
            Status::Missing
        );

        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let out = uncontained(&net_script(listener.local_addr().unwrap().port())).unwrap();
        let pairs = kv(&out.stdout);
        assert_eq!(
            kv_get(&pairs, "host_loopback"),
            Some("reachable"),
            "{}",
            out.stdout
        );
        assert_eq!(eval_net(&pairs).0, Status::Missing);
    }
}
