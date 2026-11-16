use super::*;
use crate::readiness::{ControlKind, ControlResult};

fn r(lines: &[String]) -> Vec<(String, String)> {
    kv(&lines.join("\n"))
}

fn without(lines: &[String], key: &str) -> Vec<String> {
    lines
        .iter()
        .filter(|l| l.split_once('=').map(|(k, _)| k) != Some(key))
        .cloned()
        .collect()
}

fn with(lines: &[String], key: &str, value: &str) -> Vec<String> {
    let mut v = without(lines, key);
    v.push(format!("{key}={value}"));
    v
}

fn identity_ok() -> Vec<String> {
    [
        "pre_verified=1",
        "result_reason=exited",
        "result_populated_zero=true",
        "fd=0:/dev/null",
        "fd=1:pipe:[1]",
        "fd=2:pipe:[2]",
        "cap_eff=0000000000000000",
        "cap_prm=0000000000000000",
        "apparmor=writ-bwrap//&writ-job-child (enforce)",
        "nnp=1",
        "oom_score_adj=500",
        "nofile=64",
        "want_nofile=64",
        "fsize=1048576",
        "want_fsize=1048576",
        "sigpending=64",
        "want_sigpending=64",
        "core=0",
        "cgroup=0::/",
        "ctl_skip_reason=start_failed",
        "ctl_skip_job_ran=0",
        "ctl_eof_job_ran=1",
        "ctl_eof_started=0",
    ]
    .iter()
    .map(|s| s.to_string())
    .collect()
}

#[test]
fn job_identity_positive_and_each_failure() {
    let ok = identity_ok();
    let e = eval_job_identity(&r(&ok));
    assert_eq!(e.status, Status::Ok, "{}", e.reason);
    assert!(e.controls_detected);
    for (k, v) in [
        ("pre_verified", "0"),
        ("result_populated_zero", "false"),
        ("cap_eff", "0000000000000400"),
        ("apparmor", "unconfined"),
        ("nnp", "0"),
        ("oom_score_adj", "0"),
        ("nofile", "1024"),
        ("core", "unlimited"),
        ("cgroup", "0::/writ.slice"),
    ] {
        assert_eq!(
            eval_job_identity(&r(&with(&ok, k, v))).status,
            Status::Missing,
            "{k}"
        );
    }
    let mut leaked = ok.clone();
    leaked.push("fd=3:pipe:[9]".into());
    assert_eq!(eval_job_identity(&r(&leaked)).status, Status::Missing);
    assert_eq!(
        eval_job_identity(&r(&without(&ok, "fd"))).status,
        Status::Missing
    );
}

#[test]
fn job_identity_controls_including_gate_eof() {
    let ok = identity_ok();
    // Gate EOF not observed: the probe would not see an early release.
    let e = eval_job_identity(&r(&with(&ok, "ctl_eof_job_ran", "0")));
    assert!(!e.controls_detected);
    assert!(e.control_detail.contains("gate-EOF"));
    let e = eval_job_identity(&r(&with(&ok, "ctl_eof_started", "1")));
    assert!(!e.controls_detected);
    let e = eval_job_identity(&r(&with(&ok, "ctl_skip_job_ran", "1")));
    assert!(!e.controls_detected);
}

fn deny_ok() -> Vec<String> {
    let mut v = Vec::new();
    for t in DENY_TARGETS {
        v.push(format!("sandbox_{t}=denied"));
        v.push(format!("escape_{t}=denied"));
    }
    for t in ESCAPE_ONLY {
        v.push(format!("escape_{t}=denied"));
    }
    for l in HOST_LOCATIONS {
        v.push(format!("host_{l}=absent"));
        v.push(format!("ctl_bind_{l}=allowed"));
    }
    v.push("ctl_canary=allowed".into());
    v.push("ctl_nolabel_userns=allowed".into());
    v
}

#[test]
fn job_deny_each_target_and_control() {
    let ok = deny_ok();
    let e = eval_job_deny(&r(&ok));
    assert_eq!(e.status, Status::Ok, "{}", e.reason);
    assert!(e.controls_detected);
    for k in [
        "sandbox_read_state",
        "escape_connect_launch_sock",
        "escape_unshare_user",
        "escape_sys_io_uring_setup",
    ] {
        assert_eq!(
            eval_job_deny(&r(&with(&ok, k, "allowed"))).status,
            Status::Missing,
            "{k}"
        );
    }
    assert_eq!(
        eval_job_deny(&r(&with(&ok, "host_dev_shm", "present"))).status,
        Status::Missing
    );
    assert_eq!(
        eval_job_deny(&r(&without(&ok, "escape_read_home"))).status,
        Status::Missing
    );
    for k in ["ctl_canary", "ctl_nolabel_userns", "ctl_bind_var_crash"] {
        assert!(
            !eval_job_deny(&r(&with(&ok, k, "denied"))).controls_detected,
            "{k}"
        );
    }
}

fn admit_ok() -> Vec<String> {
    let mut v: Vec<String> = REFUSED_PEERS
        .iter()
        .map(|w| format!("peer_{w}=closed_before_read"))
        .collect();
    v.push("peer_writ=hello".into());
    for (c, want) in REQUEST_CASES {
        v.push(format!("case_{c}={want}"));
        v.push(format!("case_{c}_job_created=0"));
    }
    v.push("dropins=none".into());
    v.push("ctl_admit_agent=detected".into());
    v
}

#[test]
fn launcher_admit_each_case_dropins_and_control() {
    let ok = admit_ok();
    let e = eval_launcher_admit(&r(&ok), &[]);
    assert_eq!(e.status, Status::Ok, "{}", e.reason);
    assert!(e.controls_detected);
    assert_eq!(
        eval_launcher_admit(&r(&with(&ok, "peer_agent", "hello")), &[]).status,
        Status::Missing
    );
    assert_eq!(
        eval_launcher_admit(
            &r(&with(&ok, "case_data_frame", "refused:LAUNCH_INVALID")),
            &[]
        )
        .status,
        Status::Missing
    );
    assert_eq!(
        eval_launcher_admit(&r(&with(&ok, "case_symlink_source_job_created", "1")), &[]).status,
        Status::Missing
    );
    assert_eq!(
        eval_launcher_admit(&r(&ok), &["/etc/systemd/system/writ.slice.d".into()]).status,
        Status::Missing
    );
    assert!(!eval_launcher_admit(&r(&without(&ok, "ctl_admit_agent")), &[]).controls_detected);
}

#[test]
fn dropin_scan_finds_every_applicable_directory() {
    let base = std::env::temp_dir().join(format!("writ-dropins-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&base);
    std::fs::create_dir_all(&base).unwrap();
    assert!(dropin_scan(&[base.as_path()]).is_empty());
    for d in [
        "writ.slice.d",
        "writ-job-launcher@3-1-990.service.d",
        "service.d",
    ] {
        std::fs::create_dir(base.join(d)).unwrap();
    }
    std::fs::create_dir(base.join("unrelated.service.d")).unwrap();
    let found = dropin_scan(&[base.as_path()]);
    assert_eq!(found.len(), 3, "{found:?}");
    std::fs::remove_dir_all(&base).unwrap();
}

fn serial_ok() -> Vec<String> {
    let mut v: Vec<String> = [
        "second_connection=refused",
        "after_job_procs=0",
        "after_slice_populated=0",
        "after_jobs_entries=0",
        "key_users_line=absent",
        "carryover_sysv=absent",
        "carryover_mqueue=absent",
        "carryover_keyring=absent",
        "carryover_tmp=absent",
        "apport=disabled",
        "core_pattern_pipe=0",
        "after_writ_job_anywhere=0",
        "ctl_lingering_proc=LAUNCH_UNAVAILABLE",
        "ctl_key_users=LAUNCH_UNAVAILABLE",
        "ctl_jobs_entry=LAUNCH_UNAVAILABLE",
        "ctl_core_pipe=reported",
    ]
    .iter()
    .map(|s| s.to_string())
    .collect();
    for l in HOST_LOCATIONS {
        v.push(format!("find_{l}=none"));
        v.push(format!("ctl_find_{l}=found"));
    }
    v
}

#[test]
fn job_serial_each_check_and_control() {
    let ok = serial_ok();
    let e = eval_job_serial(&r(&ok));
    assert_eq!(e.status, Status::Ok, "{}", e.reason);
    assert!(e.controls_detected);
    for (k, v) in [
        ("second_connection", "served"),
        ("key_users_line", "present"),
        ("carryover_tmp", "present"),
        ("find_run_user", "/run/user/991/x"),
        ("core_pattern_pipe", "1"),
    ] {
        assert_eq!(
            eval_job_serial(&r(&with(&ok, k, v))).status,
            Status::Missing,
            "{k}"
        );
    }
    for k in ["ctl_jobs_entry", "ctl_find_dev_mqueue", "ctl_core_pipe"] {
        assert!(
            !eval_job_serial(&r(&with(&ok, k, "missed"))).controls_detected,
            "{k}"
        );
    }
}

#[test]
fn fd_status_pipe_is_never_trusted() {
    let ok: Vec<String> = [
        "status_write=unreachable",
        "host_forged_writes=1",
        "job_exit=7",
        "result_reason=exited",
        "result_exit_status=7",
        "result_populated_zero=true",
        "ctl_forged_writes=1",
        "ctl_result_exit_status=99",
        "ctl_trusting_build=detected",
    ]
    .iter()
    .map(|s| s.to_string())
    .collect();
    let e = eval_fd_status(&r(&ok));
    assert_eq!(e.status, Status::Ok, "{}", e.reason);
    assert!(e.controls_detected);
    // The escaped job's write changed the reported exit status.
    assert_eq!(
        eval_fd_status(&r(&with(&ok, "result_exit_status", "99"))).status,
        Status::Missing
    );
    assert!(!eval_fd_status(&r(&without(&ok, "ctl_trusting_build"))).controls_detected);
    // M5a-11: the positive run needs the forged write to the monitor's descriptor 4 to have
    // happened (otherwise it shows nothing), and the control must report the forged value.
    let k = "host_forged_writes";
    assert_eq!(
        eval_fd_status(&r(&with(&ok, k, "0"))).status,
        Status::Missing
    );
    assert_eq!(eval_fd_status(&r(&without(&ok, k))).status, Status::Missing);
    for (k, v) in [
        ("ctl_forged_writes", "0"),
        ("ctl_result_exit_status", "7"),
        ("ctl_trusting_build", "missed"),
    ] {
        assert!(
            !eval_fd_status(&r(&with(&ok, k, v))).controls_detected,
            "{k}={v}"
        );
    }
}

#[test]
fn launcher_probes_are_missing_without_reports_and_need_their_controls() {
    let cfg = Config::reference();
    for (id, _) in REPORTS {
        let p = probe(&cfg, id);
        assert_eq!(p.status, Status::Missing);
        assert_eq!(p.control.kind, ControlKind::VmTrial);
    }
    assert!(fd_status(&cfg).is_none());
    let dir = std::env::temp_dir().join(format!("writ-launcher-reports-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(dir.join("job-serial.txt"), serial_ok().join("\n")).unwrap();
    let mut no_ctl = identity_ok();
    no_ctl = with(&no_ctl, "ctl_eof_job_ran", "0");
    std::fs::write(dir.join("job-identity.txt"), no_ctl.join("\n")).unwrap();
    let cfg = Config {
        launcher_reports: Some(dir.clone()),
        ..Config::reference()
    };
    let s = probe(&cfg, ProbeId::JobSerial);
    assert_eq!(s.status, Status::Ok, "{}", s.reason);
    assert_eq!(s.control.result, ControlResult::FailedAsExpected);
    // A control that did not fail turns the pass into an error (readiness.rs).
    let i = probe(&cfg, ProbeId::JobIdentity);
    assert_eq!(i.status, Status::Error);
    std::fs::remove_dir_all(&dir).unwrap();
}
