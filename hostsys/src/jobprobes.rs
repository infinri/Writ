//! The job-launcher probes of J-9 revisions 4 to 6 (D-046 H-2; J-11): P-JOB-IDENTITY,
//! P-JOB-DENY, P-LAUNCHER-ADMIT, P-JOB-SERIAL and the status-pipe part of P-FD (N4).
//!
//! These probes need the root launcher, the `writ-job` identity and operator actions (planting
//! canaries, test-only launcher builds and profiles), so they run only in the reference VM:
//! `m5/reference-vm/trial.sh` drives jobs through `launch.sock` and writes one `key=value`
//! report per probe into a directory passed with `--launcher-reports DIR`. This module
//! evaluates those reports; each report carries the positive observations **and** the
//! observations of its negative controls, so a probe passes only when the positive check holds
//! and every control was detected. Without a report the probe is `missing` ("requires the
//! launcher in the reference VM"), never `ok`. The drop-in scan of P-LAUNCHER-ADMIT (L2, N6) is
//! a live filesystem check.

use crate::exec::{kv, kv_get};
use crate::probes::{o, Config};
use crate::readiness::{Control, ProbeId, ProbeResult, Status};
use std::path::Path;

/// Report file names inside `--launcher-reports DIR`.
pub const REPORTS: [(ProbeId, &str); 4] = [
    (ProbeId::JobIdentity, "job-identity.txt"),
    (ProbeId::JobDeny, "job-deny.txt"),
    (ProbeId::LauncherAdmit, "launcher-admit.txt"),
    (ProbeId::JobSerial, "job-serial.txt"),
];

/// The P-FD status-pipe report (N4).
pub const FD_STATUS_REPORT: &str = "fd-status.txt";

type Kv = [(String, String)];

fn get<'a>(r: &'a Kv, k: &str) -> &'a str {
    kv_get(r, k).unwrap_or("")
}

fn all_values<'a>(r: &'a Kv, k: &str) -> Vec<&'a str> {
    r.iter()
        .filter(|(key, _)| key == k)
        .map(|(_, v)| v.as_str())
        .collect()
}

/// An evaluation: positive status and reason, and whether every control was detected.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Eval {
    /// Positive check.
    pub status: Status,
    /// Why.
    pub reason: String,
    /// Every negative control detected.
    pub controls_detected: bool,
    /// Control detail.
    pub control_detail: String,
}

fn verdict(problems: Vec<String>, ok: &str, missed: Vec<String>, ctl: &str) -> Eval {
    Eval {
        status: if problems.is_empty() {
            Status::Ok
        } else {
            Status::Missing
        },
        reason: if problems.is_empty() {
            ok.to_string()
        } else {
            problems.join("; ")
        },
        controls_detected: missed.is_empty(),
        control_detail: if missed.is_empty() {
            format!("{ctl}: every control detected")
        } else {
            format!("{ctl}: not detected: {}", missed.join(", "))
        },
    }
}

// ---------------------------------------------------------------------------------------------
// P-JOB-IDENTITY
// ---------------------------------------------------------------------------------------------

/// P-JOB-IDENTITY (revision 5, H1, M1, M2). Keys written by the trial:
///
/// - `pre_verified=1`: the launcher logged its pre-gate verification of every pid in
///   `<unit>/job` (ids, empty groups, `NoNewPrivs`, pinned image) for this job;
/// - `result_reason`, `result_populated_zero`: the job's `finished` frame;
/// - inside the job after the gate: `fd=<n>:<target>` lines, `cap_eff`, `cap_prm` (hex),
///   `apparmor` (`/proc/self/attr/current`), `nnp`, `oom_score_adj`, `nofile`, `fsize`,
///   `sigpending`, `core` (soft limits) and `cgroup` (`/proc/self/cgroup`);
/// - `want_nofile`, `want_fsize`, `want_sigpending`: what the request asked for;
/// - control (1): `ctl_skip_reason` and `ctl_skip_job_ran` for a test build that skips
///   `setgroups` (detected when the launcher reports `start_failed` and the job never ran);
/// - control (2), gate EOF: `ctl_eof_job_ran` and `ctl_eof_started` for a test build that
///   closes the gate writer on an error path (detected when the job ran without `started`).
pub fn eval_job_identity(r: &Kv) -> Eval {
    let mut p = Vec::new();
    if get(r, "pre_verified") != "1" {
        p.push("pre-gate verification not recorded".to_string());
    }
    if get(r, "result_reason") != "exited" || get(r, "result_populated_zero") != "true" {
        p.push("job did not exit with populated 0".to_string());
    }
    let fds: Vec<&str> = all_values(r, "fd");
    // A descriptor above 2 that still names an open file is a leak (the listing's own
    // directory handle is closed before it is read, so it names nothing; as in P-FD).
    let extra: Vec<&&str> = fds
        .iter()
        .filter(|f| match f.split_once(':') {
            Some((n, target)) => n.parse::<u32>().map_or(true, |n| n > 2) && !target.is_empty(),
            None => true,
        })
        .collect();
    if fds.is_empty() || !extra.is_empty() {
        p.push(format!("job descriptors not exactly 0..2: {fds:?}"));
    }
    for k in ["cap_eff", "cap_prm"] {
        if !get(r, k).trim_start_matches('0').is_empty() || get(r, k).is_empty() {
            p.push(format!("{k} = {}", get(r, k)));
        }
    }
    if !get(r, "apparmor").starts_with("writ-bwrap//&writ-job-child") {
        p.push(format!("AppArmor label {:?}", get(r, "apparmor")));
    }
    if get(r, "nnp") != "1" {
        p.push("NoNewPrivs not set".into());
    }
    if get(r, "oom_score_adj") != "500" {
        p.push(format!("oom_score_adj {}", get(r, "oom_score_adj")));
    }
    for k in ["nofile", "fsize", "sigpending"] {
        if get(r, k).is_empty() || get(r, k) != get(r, &format!("want_{k}")) {
            p.push(format!(
                "{k} {} (want {})",
                get(r, k),
                get(r, &format!("want_{k}"))
            ));
        }
    }
    if get(r, "core") != "0" {
        p.push(format!("core limit {}", get(r, "core")));
    }
    if get(r, "cgroup") != "0::/" {
        p.push(format!(
            "cgroup {:?} (expected the job's cgroup namespace root)",
            get(r, "cgroup")
        ));
    }
    let mut missed = Vec::new();
    if !(get(r, "ctl_skip_reason") == "start_failed" && get(r, "ctl_skip_job_ran") == "0") {
        missed.push("skip-setgroups build".to_string());
    }
    if !(get(r, "ctl_eof_job_ran") == "1" && get(r, "ctl_eof_started") == "0") {
        missed.push("gate-EOF build".to_string());
    }
    verdict(
        p,
        "job identity, descriptors, capabilities, label, limits and cgroup as required",
        missed,
        "(1) skip-setgroups test build refused; (2) gate-EOF test build shows a job that ran without started",
    )
}

// ---------------------------------------------------------------------------------------------
// P-JOB-DENY
// ---------------------------------------------------------------------------------------------

/// Targets denied to `writ-job` in both views (inside the sandbox and the escape view).
pub const DENY_TARGETS: [&str; 20] = [
    "read_state",
    "write_state",
    "read_run",
    "read_etc_writ",
    "read_etc_launcher",
    "read_srv",
    "read_home",
    "read_run_user",
    "connect_host_sock",
    "connect_admin_sock",
    "connect_launch_sock",
    "connect_approval_port",
    "connect_host_abstract",
    "write_cgroup_procs",
    "write_cgroup_limits",
    "write_slice",
    "signal_service",
    "signal_launcher",
    "trace_service",
    "signal_agent",
];

/// Escape-view only: user namespaces and the H3 system calls.
pub const ESCAPE_ONLY: [&str; 7] = [
    "unshare_user",
    "exec_private_bwrap_userns",
    "sys_keyctl",
    "sys_io_uring_setup",
    "sys_bpf",
    "sys_perf_event_open",
    "sys_userfaultfd",
];

/// Host locations checked from the host after the escape run (N1).
pub const HOST_LOCATIONS: [&str; 7] = [
    "tmp",
    "var_tmp",
    "run",
    "dev_shm",
    "dev_mqueue",
    "run_user",
    "var_crash",
];

/// P-JOB-DENY (revision 4 to 6, H2, H3, N1). Keys: `sandbox_<target>` and `escape_<target>`
/// = `denied|allowed` for [`DENY_TARGETS`], `escape_<x>` for [`ESCAPE_ONLY`], and
/// `host_<location>=absent|present` (a `writ-job`-created file visible in the host view).
/// Controls: `ctl_canary=allowed` (a loosely moded canary in the view is reported readable),
/// `ctl_nolabel_userns=allowed` (the escape run without the child label obtains a namespace),
/// and `ctl_bind_<location>=allowed` for each host location bound writable by a test profile.
pub fn eval_job_deny(r: &Kv) -> Eval {
    let mut p = Vec::new();
    for t in DENY_TARGETS {
        for view in ["sandbox", "escape"] {
            let k = format!("{view}_{t}");
            if get(r, &k) != "denied" {
                p.push(format!("{k}={}", get(r, &k)));
            }
        }
    }
    for t in ESCAPE_ONLY {
        let k = format!("escape_{t}");
        if get(r, &k) != "denied" {
            p.push(format!("{k}={}", get(r, &k)));
        }
    }
    for l in HOST_LOCATIONS {
        let k = format!("host_{l}");
        if get(r, &k) != "absent" {
            p.push(format!("{k}={}", get(r, &k)));
        }
    }
    let mut missed = Vec::new();
    if get(r, "ctl_canary") != "allowed" {
        missed.push("loose canary".to_string());
    }
    if get(r, "ctl_nolabel_userns") != "allowed" {
        missed.push("escape without the child label".to_string());
    }
    for l in HOST_LOCATIONS {
        if get(r, &format!("ctl_bind_{l}")) != "allowed" {
            missed.push(format!("writable bind of {l}"));
        }
    }
    verdict(
        p,
        "writ-job denied every target in the sandbox and the escape view; nothing in host locations",
        missed,
        "(1) loose canary readable; (2) escape without the child label gets a namespace; (3) each host location bound writable is reported",
    )
}

// ---------------------------------------------------------------------------------------------
// P-LAUNCHER-ADMIT
// ---------------------------------------------------------------------------------------------

/// Peers that must be closed before any byte is read.
pub const REFUSED_PEERS: [&str; 5] = ["agent", "job", "desktop", "root", "overflow"];

/// Request cases and the outcome each must have (`refused:<CODE>` or `closed`), with no job
/// process created.
pub const REQUEST_CASES: [(&str, &str); 14] = [
    ("before_hello", "closed"),
    ("oversized", "closed"),
    ("unknown_field", "refused:LAUNCH_INVALID"),
    ("scm_rights", "closed"),
    ("data_frame", "closed"),
    ("no_request", "closed"),
    ("limit_above", "refused:LAUNCH_LIMIT_EXCEEDED"),
    ("unknown_profile", "refused:LAUNCH_PROFILE_UNKNOWN"),
    ("bad_job_id", "refused:LAUNCH_INVALID"),
    ("symlink_source", "refused:LAUNCH_SOURCE_INVALID"),
    ("foreign_source", "refused:LAUNCH_SOURCE_INVALID"),
    ("acl_source", "refused:LAUNCH_SOURCE_INVALID"),
    ("hardlink_source", "refused:LAUNCH_SOURCE_INVALID"),
    ("failed_socket_unit", "unavailable"),
];

/// P-LAUNCHER-ADMIT (revision 4 and 5, L2, L3, L7). Keys: `peer_<who>=closed_before_read`
/// for [`REFUSED_PEERS`], `peer_writ=hello`; `case_<name>=<outcome>` and
/// `case_<name>_job_created=0` for [`REQUEST_CASES`]; `dropins=none` (from
/// `systemctl show -p DropInPaths`, combined with the live scan). `static_found` holds the live
/// findings: drop-in directories ([`dropin_scan`]) and AppArmor profile includes or `local/`
/// overrides ([`apparmor_problems`], M5a-13 M7). Control: `ctl_admit_agent=detected` (a
/// configuration admitting the agent uid is reported).
pub fn eval_launcher_admit(r: &Kv, static_found: &[String]) -> Eval {
    let mut p = Vec::new();
    for who in REFUSED_PEERS {
        let k = format!("peer_{who}");
        if get(r, &k) != "closed_before_read" {
            p.push(format!("{k}={}", get(r, &k)));
        }
    }
    if get(r, "peer_writ") != "hello" {
        p.push("writ did not receive hello".into());
    }
    for (case, want) in REQUEST_CASES {
        let k = format!("case_{case}");
        if get(r, &k) != want {
            p.push(format!("{k}={} (want {want})", get(r, &k)));
        }
        if get(r, &format!("{k}_job_created")) != "0" {
            p.push(format!("{k}: a job process was created"));
        }
    }
    if get(r, "dropins") != "none" {
        p.push(format!("systemd reports drop-ins: {}", get(r, "dropins")));
    }
    if !static_found.is_empty() {
        p.push(format!(
            "drop-ins or AppArmor includes present: {}",
            static_found.join(",")
        ));
    }
    let missed = if get(r, "ctl_admit_agent") == "detected" {
        vec![]
    } else {
        vec!["configuration admitting the agent uid".to_string()]
    };
    verdict(
        p,
        "only writ admitted, before any read; every malformed or out-of-bounds request refused with no job",
        missed,
        "launcher configuration admitting the agent uid is detected",
    )
}

/// Drop-in directories that can apply to the launcher units (L2, N6), relative to each unit
/// directory.
pub const DROPIN_NAMES: [&str; 11] = [
    "writ-job-launcher@.service.d",
    "writ-job-launcher-.service.d",
    "writ-job-.service.d",
    "writ-.service.d",
    "writ-.socket.d",
    "writ-job-launcher.socket.d",
    "writ.slice.d",
    "writ-jobs.slice.d",
    "service.d",
    "socket.d",
    "slice.d",
];

/// Unit directories searched for drop-ins: the persistent, runtime and vendor directories,
/// and (M5a-13 L8) the `systemctl set-property` control directories, transient units, attached
/// portable units, generator output and `/lib` (when it is not the `/usr/lib` symlink).
pub const UNIT_DIRS: [&str; 12] = [
    "/etc/systemd/system.control",
    "/run/systemd/system.control",
    "/run/systemd/transient",
    "/etc/systemd/system",
    "/run/systemd/system",
    "/run/systemd/generator.early",
    "/run/systemd/generator",
    "/run/systemd/generator.late",
    "/etc/systemd/system.attached",
    "/run/systemd/system.attached",
    "/usr/local/lib/systemd/system",
    "/usr/lib/systemd/system",
];

/// Additional unit directory on systems where `/lib` is not a symlink to `/usr/lib`.
pub const LIB_UNIT_DIR: &str = "/lib/systemd/system";

/// The installed two-profile AppArmor file (J-11.5a) and the `local/` overrides it must not
/// read (M5a-13 M7).
pub const APPARMOR_PROFILE: &str = "/etc/apparmor.d/writ-bwrap";
/// `local/` override files that must not exist.
pub const APPARMOR_LOCAL: [&str; 2] = [
    "/etc/apparmor.d/local/writ-bwrap",
    "/etc/apparmor.d/local/writ-job-child",
];

/// Problems of the pinned AppArmor profile (M5a-13 M7): its digest covers only its own bytes,
/// so any `include` (or legacy `#include`) other than `<tunables/global>` is a problem, as is
/// any existing `local/` override file in `local_present`.
pub fn apparmor_problems(profile: &str, local_present: &[String]) -> Vec<String> {
    let mut out: Vec<String> = profile
        .lines()
        .map(str::trim)
        .filter(|t| {
            let d = t.strip_prefix('#').unwrap_or(t);
            d.starts_with("include") && d["include".len()..].trim() != "<tunables/global>"
        })
        .map(|t| format!("apparmor include: {t}"))
        .collect();
    out.extend(
        local_present
            .iter()
            .map(|p| format!("apparmor override: {p}")),
    );
    out
}

fn live_static_findings() -> Vec<String> {
    let mut roots: Vec<&Path> = UNIT_DIRS.iter().map(Path::new).collect();
    let lib = Path::new(LIB_UNIT_DIR);
    let usr = Path::new("/usr/lib/systemd/system");
    if lib.exists() && std::fs::canonicalize(lib).ok() != std::fs::canonicalize(usr).ok() {
        roots.push(lib);
    }
    let mut found = dropin_scan(&roots);
    let local: Vec<String> = APPARMOR_LOCAL
        .iter()
        .filter(|p| Path::new(p).symlink_metadata().is_ok())
        .map(|p| (*p).to_string())
        .collect();
    match std::fs::read_to_string(APPARMOR_PROFILE) {
        Ok(text) => found.extend(apparmor_problems(&text, &local)),
        Err(e) => found.push(format!("{APPARMOR_PROFILE}: {e}")),
    }
    found
}

/// Existing drop-in directories under `roots` (including per-instance
/// `writ-job-launcher@<instance>.service.d`).
pub fn dropin_scan(roots: &[&Path]) -> Vec<String> {
    let mut out = Vec::new();
    for root in roots {
        for n in DROPIN_NAMES {
            let p = root.join(n);
            if p.symlink_metadata().is_ok() {
                out.push(p.display().to_string());
            }
        }
        if let Ok(rd) = std::fs::read_dir(root) {
            for e in rd.flatten() {
                let name = e.file_name().to_string_lossy().into_owned();
                if name.starts_with("writ-job-launcher@")
                    && name.ends_with(".service.d")
                    && name != "writ-job-launcher@.service.d"
                {
                    out.push(root.join(name).display().to_string());
                }
            }
        }
    }
    out.sort();
    out.dedup();
    out
}

// ---------------------------------------------------------------------------------------------
// P-JOB-SERIAL
// ---------------------------------------------------------------------------------------------

/// P-JOB-SERIAL (revision 4 and 6, J-11.7, H3, N1). Keys: `second_connection=refused`;
/// after a job: `after_job_procs=0`, `after_slice_populated=0`, `after_jobs_entries=0`,
/// `key_users_line=absent`; `carryover_sysv`, `carryover_mqueue`, `carryover_keyring`,
/// `carryover_tmp` = `absent` (objects of job 1 seen by job 2); `find_<location>=none` for
/// every host location; `apport=disabled`; `core_pattern_pipe=0`. Controls (each makes the
/// launcher refuse or the probe report): `ctl_lingering_proc`, `ctl_key_users` and
/// `ctl_jobs_entry` = `LAUNCH_UNAVAILABLE`; `ctl_find_<location>=found`;
/// `ctl_core_pipe=reported`.
pub fn eval_job_serial(r: &Kv) -> Eval {
    let mut p = Vec::new();
    let want: [(&str, &str); 12] = [
        ("second_connection", "refused"),
        ("after_job_procs", "0"),
        ("after_slice_populated", "0"),
        ("after_jobs_entries", "0"),
        ("key_users_line", "absent"),
        ("carryover_sysv", "absent"),
        ("carryover_mqueue", "absent"),
        ("carryover_keyring", "absent"),
        ("carryover_tmp", "absent"),
        ("apport", "disabled"),
        ("core_pattern_pipe", "0"),
        ("after_writ_job_anywhere", "0"),
    ];
    for (k, v) in want {
        if get(r, k) != v {
            p.push(format!("{k}={} (want {v})", get(r, k)));
        }
    }
    for l in HOST_LOCATIONS {
        let k = format!("find_{l}");
        if get(r, &k) != "none" {
            p.push(format!("{k}={}", get(r, &k)));
        }
    }
    let mut missed = Vec::new();
    for k in ["ctl_lingering_proc", "ctl_key_users", "ctl_jobs_entry"] {
        if get(r, k) != "LAUNCH_UNAVAILABLE" {
            missed.push(k.to_string());
        }
    }
    for l in HOST_LOCATIONS {
        if get(r, &format!("ctl_find_{l}")) != "found" {
            missed.push(format!("planted file in {l}"));
        }
    }
    if get(r, "ctl_core_pipe") != "reported" {
        missed.push("pipe core_pattern".into());
    }
    verdict(
        p,
        "one job at a time; nothing of a job survives it; core dumps not piped",
        missed,
        "lingering process, key-users entry and jobs-filesystem entry refuse; planted files and a pipe core_pattern are reported",
    )
}

// ---------------------------------------------------------------------------------------------
// P-FD status pipe (revision 6, N4)
// ---------------------------------------------------------------------------------------------

/// The status-pipe part of P-FD. Keys: `status_write=written|unreachable` (the job's attempt to
/// write to `/proc/<monitor>/fd/4` after `started`), `host_forged_writes=<n>` (forged
/// `exit-code` lines written to the monitor's descriptor 4 from the host while the job ran,
/// standing in for an escaped job that reaches it; at least one is required),
/// `job_exit=<n>` (what the job itself exited with), `result_reason`, `result_exit_status`,
/// `result_populated_zero`. Control (M5a-11): the test-only build with fault
/// `trust-status-after-started`, under the same forged write (`ctl_forged_writes` at least
/// one), reports the forged value (`ctl_result_exit_status=99`), recorded by the trial as
/// `ctl_trusting_build=detected`.
pub fn eval_fd_status(r: &Kv) -> Eval {
    let mut p = Vec::new();
    if !matches!(get(r, "status_write"), "written" | "unreachable") {
        p.push("status-pipe write attempt not recorded".into());
    }
    if get(r, "host_forged_writes").parse::<u32>().unwrap_or(0) == 0 {
        p.push("no forged write reached the monitor's status descriptor".into());
    }
    if get(r, "result_reason") != "exited"
        || get(r, "result_exit_status") != get(r, "job_exit")
        || get(r, "job_exit").is_empty()
        || get(r, "result_populated_zero") != "true"
    {
        p.push(format!(
            "finished differs from the job's own end: reason {} status {} (job {}) populated_zero {}",
            get(r, "result_reason"),
            get(r, "result_exit_status"),
            get(r, "job_exit"),
            get(r, "result_populated_zero")
        ));
    }
    let missed = if get(r, "ctl_trusting_build") == "detected"
        && get(r, "ctl_forged_writes").parse::<u32>().unwrap_or(0) > 0
        && get(r, "ctl_result_exit_status") == "99"
    {
        vec![]
    } else {
        vec!["a build that trusts the status pipe after started".to_string()]
    };
    verdict(
        p,
        "a job writing to the status pipe after started changes neither finished nor the kill evidence",
        missed,
        "a test build that parses the status pipe after started is detected",
    )
}

fn read_report(cfg: &Config, name: &str) -> Option<Vec<(String, String)>> {
    let dir = cfg.launcher_reports.as_ref()?;
    std::fs::read_to_string(dir.join(name)).ok().map(|t| kv(&t))
}

fn result(id: ProbeId, e: Eval, control_text: &str) -> ProbeResult {
    let control = if e.status == Status::Ok {
        Control::runtime(e.controls_detected, e.control_detail.clone())
    } else {
        Control::runtime_not_run(control_text)
    };
    ProbeResult::new(id, e.status, e.reason, vec![], control)
}

/// One launcher probe from its report (missing without one).
pub fn probe(cfg: &Config, id: ProbeId) -> ProbeResult {
    let Some(name) = REPORTS.iter().find(|(i, _)| *i == id).map(|(_, n)| *n) else {
        return ProbeResult::new(
            id,
            Status::Error,
            "not a launcher probe",
            vec![],
            Control::none("n/a"),
        );
    };
    let Some(r) = read_report(cfg, name) else {
        return ProbeResult::new(
            id,
            Status::Missing,
            "requires the launcher in the reference VM (report not supplied; --launcher-reports)",
            vec![o("report", name)],
            Control::vm_trial("controls run by m5/reference-vm/trial.sh through test-only launcher builds and profiles"),
        );
    };
    let (e, text) = match id {
        ProbeId::JobIdentity => (
            eval_job_identity(&r),
            "skip-setgroups and gate-EOF test builds",
        ),
        ProbeId::JobDeny => (
            eval_job_deny(&r),
            "canary, unlabelled escape, writable binds",
        ),
        ProbeId::LauncherAdmit => (
            eval_launcher_admit(&r, &live_static_findings()),
            "configuration admitting the agent uid",
        ),
        _ => (
            eval_job_serial(&r),
            "lingering process, key-users, jobs entry, planted files, pipe core_pattern",
        ),
    };
    result(id, e, text)
}

/// The status-pipe evaluation for P-FD, `None` without a report.
pub fn fd_status(cfg: &Config) -> Option<Eval> {
    read_report(cfg, FD_STATUS_REPORT).map(|r| eval_fd_status(&r))
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests;
