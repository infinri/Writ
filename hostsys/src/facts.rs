//! Host facts and versions recorded in every readiness report (J-9). Informational only: none
//! of these values is an input to the verdict.

use crate::exec;
use crate::probes::Config;
use std::fs;
use std::os::unix::fs::MetadataExt;
use std::path::Path;
use std::time::Duration;
use writ_kernel::json::Digest;

/// Largest bubblewrap binary that is digested.
const MAX_BINARY: u64 = 64 * 1024 * 1024;

fn first_line(text: &str) -> String {
    text.lines()
        .map(str::trim)
        .find(|l| !l.is_empty())
        .unwrap_or("")
        .to_string()
}

fn read(p: &str) -> String {
    fs::read_to_string(p).map_or_else(|_| "absent".to_string(), |t| t.trim().to_string())
}

fn cmd(argv: &[&str]) -> String {
    match exec::run_argv(argv, Duration::from_secs(10)) {
        Ok(o) if o.success() => first_line(&o.stdout),
        Ok(o) => format!("unavailable: {}", o.diagnostic()),
        Err(e) => format!("unavailable: {e}"),
    }
}

fn os_release() -> String {
    fs::read_to_string("/etc/os-release")
        .ok()
        .and_then(|t| {
            t.lines()
                .find_map(|l| l.strip_prefix("PRETTY_NAME="))
                .map(|v| v.trim_matches('"').to_string())
        })
        .unwrap_or_else(|| "absent".to_string())
}

fn toolchain() -> String {
    include_str!("../../rust-toolchain.toml")
        .lines()
        .find_map(|l| l.trim().strip_prefix("channel = "))
        .map_or_else(
            || "unknown".to_string(),
            |v| v.trim_matches('"').to_string(),
        )
}

fn binary_digest(p: &Path) -> String {
    match fs::metadata(p) {
        Ok(m) if m.is_file() && m.len() <= MAX_BINARY => fs::read(p).map_or_else(
            |e| format!("unreadable: {e}"),
            |b| Digest::of_bytes(&b).to_string(),
        ),
        Ok(_) => "not a regular file of bounded size".to_string(),
        Err(e) => format!("absent: {e}"),
    }
}

/// Collect the recorded facts for `cfg`.
pub fn collect(cfg: &Config) -> Vec<(String, String)> {
    let release = read("/proc/sys/kernel/osrelease");
    let bwrap = cfg.bwrap.display().to_string();
    let image = fs::read_to_string(cfg.config_dir.join("reference-image"))
        .map(|t| {
            t.lines()
                .take(4)
                .map(str::trim)
                .collect::<Vec<_>>()
                .join("; ")
        })
        .unwrap_or_else(|_| {
            "not recorded (no reference-image file in the configuration directory)".to_string()
        });
    let pkgs = exec::run_argv(
        &[
            "dpkg-query",
            "-W",
            "-f",
            "${Package}=${Version} ",
            "bubblewrap",
            "systemd",
            "util-linux",
            "coreutils",
            "libc6",
            &format!("linux-image-{release}"),
        ],
        Duration::from_secs(10),
    )
    .map_or_else(
        |e| format!("unavailable: {e}"),
        |o| o.stdout.trim().to_string(),
    );
    let cgroup = |f: &str| read(&format!("/sys/fs/cgroup/{f}"));
    let mut v = vec![
        ("os_release".to_string(), os_release()),
        ("reference_image".to_string(), image),
        ("kernel_release".to_string(), release),
        (
            "kernel_version".to_string(),
            read("/proc/sys/kernel/version"),
        ),
        ("packages".to_string(), pkgs),
        ("systemd".to_string(), cmd(&["systemctl", "--version"])),
        (
            "bwrap_version".to_string(),
            cmd(&[bwrap.as_str(), "--version"]),
        ),
        ("bwrap_sha256".to_string(), binary_digest(&cfg.bwrap)),
        (
            "bwrap_mode".to_string(),
            fs::metadata(&cfg.bwrap).map_or("absent".into(), |m| {
                format!("{:04o} uid {}", m.mode() & 0o7777, m.uid())
            }),
        ),
        ("util_linux".to_string(), cmd(&["setsid", "--version"])),
        ("coreutils".to_string(), cmd(&["true", "--version"])),
        ("glibc".to_string(), cmd(&["getconf", "GNU_LIBC_VERSION"])),
        ("sqlite".to_string(), "not linked by writ-probe".to_string()),
        ("rust_toolchain".to_string(), toolchain()),
        (
            "cgroup_root_controllers".to_string(),
            cgroup("cgroup.controllers"),
        ),
        ("cgroup_self".to_string(), read("/proc/self/cgroup")),
    ];
    for s in [
        "kernel/unprivileged_userns_clone",
        "user/max_user_namespaces",
        "kernel/yama/ptrace_scope",
        "fs/protected_symlinks",
        "fs/protected_hardlinks",
        "kernel/apparmor_restrict_unprivileged_userns",
        "kernel/apparmor_restrict_unprivileged_unconfined",
    ] {
        v.push((
            format!("sysctl.{}", s.replace('/', ".")),
            read(&format!("/proc/sys/{s}")),
        ));
    }
    v.push((
        "apparmor_enabled".to_string(),
        read("/sys/module/apparmor/parameters/enabled"),
    ));
    v.push((
        "selinux_enforce".to_string(),
        read("/sys/fs/selinux/enforce"),
    ));
    v.push(("lsm".to_string(), read("/sys/kernel/security/lsm")));
    v.push(("claude_code".to_string(), "not recorded (M5b)".to_string()));
    v
}
