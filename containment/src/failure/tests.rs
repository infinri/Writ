use super::*;
use crate::protocol::ResultDigests;
use LauncherAction as L;
use RunConclusion as R;

fn expected() -> Finished {
    Finished {
        job_id: "run-1".into(),
        request_digest: "sha256:r".into(),
        reason: Reason::Exited,
        exit_status: Some(0),
        signal: None,
        kill_used: true,
        populated_zero: true,
        memory_events: [0; 3],
        pids_max_events: 0,
        cpu_usage_usec: 0,
        output_bytes: [0, 0],
        digests: ResultDigests {
            bwrap: "sha256:b".into(),
            bwrap_profile: "sha256:p".into(),
            apparmor_profile: "sha256:a".into(),
            runtime_profile: "sha256:rt".into(),
            exec: "sha256:e".into(),
        },
    }
}

fn fin(edit: impl FnOnce(&mut Finished)) -> Observed {
    let mut f = expected();
    edit(&mut f);
    Observed::Finished(Box::new(f))
}

fn check(f: Failure, launcher: LauncherAction, run: RunConclusion, withdraw: bool) {
    assert_eq!(
        row(f),
        Row {
            launcher,
            run,
            withdraw_readiness: withdraw
        },
        "{f:?}"
    );
}

#[test]
fn every_row_is_covered_once() {
    assert_eq!(ALL.len(), 17);
    for (i, a) in ALL.iter().enumerate() {
        assert!(!ALL[i + 1..].contains(a));
    }
}

#[test]
fn row_no_hello() {
    check(Failure::NoHello, L::NothingStarted, R::NotRun, false);
    assert_eq!(
        conclude(&Observed::NoHello, &expected()).run,
        R::NotRun,
        "EOF before hello (including MaxConnections) means the request was never sent"
    );
    assert!(!conclude(&Observed::NoHello, &expected()).withdraw_readiness);
}

#[test]
fn row_hello_differs() {
    check(Failure::HelloDiffers, L::NothingStarted, R::NotRun, true);
    assert!(conclude(&Observed::HelloDiffers, &expected()).withdraw_readiness);
}

#[test]
fn row_no_request() {
    check(
        Failure::NoRequest,
        L::NothingStarted,
        R::NotApplicable,
        false,
    );
}

#[test]
fn row_refused_only_before_spawn() {
    check(Failure::Refused, L::NothingStarted, R::Failed, false);
    assert_eq!(launcher_action(false, false), L::NothingStarted);
    let c = conclude(&Observed::Refused(LaunchCode::LimitExceeded), &expected());
    assert_eq!((c.failure, c.run), (Some(Failure::Refused), R::Failed));
    assert!(!c.withdraw_readiness);
    assert!(conclude(&Observed::Refused(LaunchCode::Unavailable), &expected()).withdraw_readiness);
}

#[test]
fn row_after_spawn() {
    check(
        Failure::AfterSpawn,
        L::KillVerifyThenReport,
        R::Failed,
        false,
    );
    assert_eq!(launcher_action(true, false), L::KillVerifyThenReport);
    let c = conclude(&fin(|f| f.reason = Reason::StartFailed), &expected());
    assert_eq!((c.failure, c.run), (Some(Failure::AfterSpawn), R::Failed));
}

#[test]
fn row_eof_after_request_is_uncertain() {
    check(
        Failure::EofAfterRequest,
        L::KillVerifyCleanup,
        R::Uncertain,
        false,
    );
    assert_eq!(launcher_action(true, true), L::KillVerifyCleanup);
    assert_eq!(conclude(&Observed::Lost, &expected()).run, R::Uncertain);
}

#[test]
fn row_launcher_crash_and_oom_are_uncertain() {
    check(
        Failure::LauncherCrash,
        L::ProcessExitSystemdKills,
        R::Uncertain,
        false,
    );
    check(
        Failure::LauncherOom,
        L::ProcessExitSystemdKills,
        R::Uncertain,
        false,
    );
    // writ observes either as a lost connection.
    assert_eq!(conclude(&Observed::Lost, &expected()).run, R::Uncertain);
}

#[test]
fn row_digest_mismatch() {
    check(Failure::DigestMismatch, L::NotApplicable, R::Failed, true);
    for o in [
        fin(|f| f.request_digest = "sha256:x".into()),
        fin(|f| f.digests.bwrap = "sha256:x".into()),
        fin(|f| f.digests.runtime_profile = "sha256:x".into()),
        fin(|f| f.job_id = "run-2".into()),
    ] {
        let c = conclude(&o, &expected());
        assert_eq!(c.failure, Some(Failure::DigestMismatch));
        assert!(c.withdraw_readiness);
    }
}

#[test]
fn row_writ_stops() {
    check(
        Failure::WritStops,
        L::KillVerifyCleanup,
        R::Uncertain,
        false,
    );
}

#[test]
fn row_writ_stops_reading() {
    check(
        Failure::WritStopsReading,
        L::LimitThenReport,
        R::AsReported,
        false,
    );
    let c = conclude(&fin(|f| f.reason = Reason::OutputExceeded), &expected());
    assert_eq!(c.run, R::AsReported);
}

#[test]
fn row_cgroup_not_empty() {
    check(Failure::CgroupNotEmpty, L::LeaveForSystemd, R::Failed, true);
    let c = conclude(&fin(|f| f.populated_zero = false), &expected());
    assert_eq!(c.failure, Some(Failure::CgroupNotEmpty));
    assert!(c.withdraw_readiness);
    let c = conclude(&fin(|f| f.kill_used = false), &expected());
    assert_eq!(c.failure, Some(Failure::CgroupNotEmpty));
}

#[test]
fn row_limits() {
    check(
        Failure::LimitExceeded,
        L::LimitThenReport,
        R::AsReported,
        false,
    );
    let c = conclude(&fin(|f| f.reason = Reason::TimedOut), &expected());
    assert_eq!(c.failure, Some(Failure::LimitExceeded));
    assert_eq!(c.run, R::AsReported);
}

#[test]
fn row_job_fs_full() {
    check(Failure::JobFsFull, L::ReportAsIs, R::AsReported, false);
}

#[test]
fn row_escape() {
    check(
        Failure::Escape,
        L::KillVerifyThenReport,
        R::AsReported,
        false,
    );
}

#[test]
fn row_config_changed() {
    check(Failure::ConfigChanged, L::NothingStarted, R::NotRun, true);
}

#[test]
fn row_kernel_escape() {
    check(Failure::KernelEscape, L::OutOfScope, R::OutOfScope, false);
}

#[test]
fn normal_end_is_as_reported() {
    let c = conclude(&fin(|_| {}), &expected());
    assert_eq!(c.failure, None);
    assert_eq!(c.run, R::AsReported);
}
