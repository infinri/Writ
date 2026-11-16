//! The J-11.6 failure table as code. [`row`] states, for every failure mode, what the launcher
//! does and what `writ` concludes; [`conclude`] is the classification the writ-side client
//! ([`crate::client`]) applies to what it observed on the connection, and [`launcher_action`]
//! is the rule the launcher's own code follows after a failure. Each row has a test.

use crate::protocol::{Finished, LaunchCode, Reason};

/// Every failure mode of J-11.6.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Failure {
    /// Socket missing, connect refused, socket unit failed, EOF before `hello`.
    NoHello,
    /// `hello` facts differ from the pinned values.
    HelloDiffers,
    /// No request within the deadline after `hello`.
    NoRequest,
    /// A `refused` frame (only before the helper is spawned).
    Refused,
    /// Failure after the helper was spawned (helper exit, status timeout, verification
    /// mismatch, gate write error).
    AfterSpawn,
    /// EOF or timeout after the request and before `refused` or `finished`.
    EofAfterRequest,
    /// Launcher crash, abort or kill after the helper was spawned.
    LauncherCrash,
    /// Launcher OOM-killed.
    LauncherOom,
    /// `finished` digests or `request_digest` differ from what `writ` sent and pinned.
    DigestMismatch,
    /// `writ` crashes or stops.
    WritStops,
    /// `writ` stops reading output.
    WritStopsReading,
    /// The cgroup does not empty.
    CgroupNotEmpty,
    /// Memory, pids, CPU, wall time, output, file size or signal-queue limit.
    LimitExceeded,
    /// Job filesystem full.
    JobFsFull,
    /// bubblewrap escape.
    Escape,
    /// Configuration, unit, drop-in or binary changed.
    ConfigChanged,
    /// Kernel escape to root.
    KernelEscape,
}

/// Every row, in table order.
pub const ALL: [Failure; 17] = [
    Failure::NoHello,
    Failure::HelloDiffers,
    Failure::NoRequest,
    Failure::Refused,
    Failure::AfterSpawn,
    Failure::EofAfterRequest,
    Failure::LauncherCrash,
    Failure::LauncherOom,
    Failure::DigestMismatch,
    Failure::WritStops,
    Failure::WritStopsReading,
    Failure::CgroupNotEmpty,
    Failure::LimitExceeded,
    Failure::JobFsFull,
    Failure::Escape,
    Failure::ConfigChanged,
    Failure::KernelEscape,
];

/// What the launcher does.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LauncherAction {
    /// Nothing was started (no helper spawned).
    NothingStarted,
    /// Kill with the gate closed, verify `populated 0`, then report.
    KillVerifyThenReport,
    /// Kill, verify and clean up when it sees the EOF.
    KillVerifyCleanup,
    /// The gate closes by process exit; systemd kills the instance cgroup.
    ProcessExitSystemdKills,
    /// Gate left open, no cleanup, exit non-zero; systemd stops the instance.
    LeaveForSystemd,
    /// A kernel limit or a launcher kill ends the job; reported with counters.
    LimitThenReport,
    /// The job sees the error; reported as is.
    ReportAsIs,
    /// Out of scope.
    OutOfScope,
    /// Not the launcher's decision (a `writ`-side check).
    NotApplicable,
}

/// What `writ` concludes about the run.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RunConclusion {
    /// The job was not run (the request was never sent, or nothing was started).
    NotRun,
    /// The run is `failed` with a diagnostic.
    Failed,
    /// **Uncertain**: the run stays `started` and is never re-run.
    Uncertain,
    /// The outcome as reported by `finished`.
    AsReported,
    /// Not applicable (`writ` did not send a request).
    NotApplicable,
    /// Out of scope (recorded limitation).
    OutOfScope,
}

/// One row: launcher action, run conclusion, and whether containment readiness is withdrawn.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Row {
    /// Launcher side.
    pub launcher: LauncherAction,
    /// `writ` side.
    pub run: RunConclusion,
    /// Containment readiness withdrawn.
    pub withdraw_readiness: bool,
}

/// The row of `f`.
pub fn row(f: Failure) -> Row {
    use LauncherAction as L;
    use RunConclusion as R;
    let r = |launcher, run, withdraw_readiness| Row {
        launcher,
        run,
        withdraw_readiness,
    };
    match f {
        Failure::NoHello => r(L::NothingStarted, R::NotRun, false),
        Failure::HelloDiffers => r(L::NothingStarted, R::NotRun, true),
        Failure::NoRequest => r(L::NothingStarted, R::NotApplicable, false),
        Failure::Refused => r(L::NothingStarted, R::Failed, false),
        Failure::AfterSpawn => r(L::KillVerifyThenReport, R::Failed, false),
        Failure::EofAfterRequest => r(L::KillVerifyCleanup, R::Uncertain, false),
        Failure::LauncherCrash => r(L::ProcessExitSystemdKills, R::Uncertain, false),
        Failure::LauncherOom => r(L::ProcessExitSystemdKills, R::Uncertain, false),
        Failure::DigestMismatch => r(L::NotApplicable, R::Failed, true),
        Failure::WritStops => r(L::KillVerifyCleanup, R::Uncertain, false),
        Failure::WritStopsReading => r(L::LimitThenReport, R::AsReported, false),
        Failure::CgroupNotEmpty => r(L::LeaveForSystemd, R::Failed, true),
        Failure::LimitExceeded => r(L::LimitThenReport, R::AsReported, false),
        Failure::JobFsFull => r(L::ReportAsIs, R::AsReported, false),
        Failure::Escape => r(L::KillVerifyThenReport, R::AsReported, false),
        Failure::ConfigChanged => r(L::NothingStarted, R::NotRun, true),
        Failure::KernelEscape => r(L::OutOfScope, R::OutOfScope, false),
    }
}

/// The launcher's rule after a failure: `spawned` says whether the helper was spawned.
/// Before the spawn only `refused` is possible; after it, never `refused`.
pub fn launcher_action(spawned: bool, conn_lost: bool) -> LauncherAction {
    match (spawned, conn_lost) {
        (false, _) => LauncherAction::NothingStarted,
        (true, true) => LauncherAction::KillVerifyCleanup,
        (true, false) => LauncherAction::KillVerifyThenReport,
    }
}

/// What the writ-side client observed on one launch connection.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Observed {
    /// No `hello` (socket missing, refused, EOF before `hello`, `MaxConnections` refusal).
    NoHello,
    /// `hello` facts differ from the pinned values.
    HelloDiffers,
    /// `refused` with a code.
    Refused(LaunchCode),
    /// EOF, error or deadline after the request without `refused` or `finished` (also a
    /// launcher crash or OOM kill, which `writ` cannot tell apart).
    Lost,
    /// `finished` received.
    Finished(Box<Finished>),
}

/// The client's conclusion.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Conclusion {
    /// The failure row that applies (`None`: the job ended normally as reported).
    pub failure: Option<Failure>,
    /// The run conclusion.
    pub run: RunConclusion,
    /// Containment readiness withdrawn.
    pub withdraw_readiness: bool,
}

/// The client's conclusion for an observation; `expected` carries the job id, the request
/// digest and the pinned result digests the `finished` frame must carry. A `refused`
/// `LAUNCH_UNAVAILABLE` (a readiness or residue check failed) also withdraws readiness.
pub fn conclude(o: &Observed, expected: &Finished) -> Conclusion {
    let from = |f: Failure| {
        let r = row(f);
        Conclusion {
            failure: Some(f),
            run: r.run,
            withdraw_readiness: r.withdraw_readiness,
        }
    };
    match o {
        Observed::NoHello => from(Failure::NoHello),
        Observed::HelloDiffers => from(Failure::HelloDiffers),
        Observed::Refused(code) => Conclusion {
            withdraw_readiness: *code == LaunchCode::Unavailable,
            ..from(Failure::Refused)
        },
        Observed::Lost => from(Failure::EofAfterRequest),
        Observed::Finished(fin) => {
            if fin.job_id != expected.job_id
                || fin.request_digest != expected.request_digest
                || fin.digests != expected.digests
            {
                from(Failure::DigestMismatch)
            } else if !fin.populated_zero || !fin.kill_used {
                from(Failure::CgroupNotEmpty)
            } else {
                match fin.reason {
                    Reason::StartFailed => from(Failure::AfterSpawn),
                    Reason::TimedOut | Reason::OutputExceeded => from(Failure::LimitExceeded),
                    Reason::Exited | Reason::Signaled | Reason::Cancelled => Conclusion {
                        failure: None,
                        run: RunConclusion::AsReported,
                        withdraw_readiness: false,
                    },
                }
            }
        }
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests;
