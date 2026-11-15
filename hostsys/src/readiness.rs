//! The `writ-readiness/1` report and the `governed_ready` verdict (D-045 M5-D2, J-9).
//!
//! Readiness comes only from observed probe results. The verdict function never sees the
//! recorded host versions: they are informational and cannot make a host ready or not ready.

use writ_kernel::json::{self, s, Map, Value};

/// Contract name of the readiness report.
pub const CONTRACT: &str = "writ-readiness/1";

/// The J-9 probes (plus the optional Landlock report).
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum ProbeId {
    /// The private bubblewrap copy runs with every namespace unshared.
    Userns,
    /// The agent uid cannot create a usable user namespace.
    AgentUserns,
    /// Filesystem confinement inside a job.
    Fs,
    /// Network confinement inside a job.
    Net,
    /// PID namespace inside a job.
    Pid,
    /// Delegated cgroup-v2 subtree with the required controllers and `cgroup.kill`.
    Cgroup,
    /// `cgroup.kill` removes `setsid` and double-forked descendants.
    Kill,
    /// Memory, pids and CPU limits take effect.
    Limits,
    /// Only descriptors 0..2 inside a job.
    Fd,
    /// `openat2` resolve flags and `statx` mount ids.
    Openat2,
    /// `RENAME_NOREPLACE` on the target filesystem.
    Rename,
    /// Target root and stores on an allow-listed local filesystem.
    FsType,
    /// No access or default ACL on trusted directories and files.
    Acl,
    /// `fs.protected_hardlinks = 1` and the agent cannot hard-link a `writ`-owned file.
    Hardlink,
    /// Unix socket peer credentials.
    Peercred,
    /// The agent identity is denied every trusted path.
    AgentDeny,
    /// No key material in command lines, environments, logs or reports.
    NoSecret,
    /// Landlock (optional defence in depth; never required).
    Landlock,
}

impl ProbeId {
    /// Every probe in report order.
    pub const ALL: [ProbeId; 18] = [
        ProbeId::Userns,
        ProbeId::AgentUserns,
        ProbeId::Fs,
        ProbeId::Net,
        ProbeId::Pid,
        ProbeId::Cgroup,
        ProbeId::Kill,
        ProbeId::Limits,
        ProbeId::Fd,
        ProbeId::Openat2,
        ProbeId::Rename,
        ProbeId::FsType,
        ProbeId::Acl,
        ProbeId::Hardlink,
        ProbeId::Peercred,
        ProbeId::AgentDeny,
        ProbeId::NoSecret,
        ProbeId::Landlock,
    ];

    /// The J-9 name.
    pub fn name(self) -> &'static str {
        match self {
            ProbeId::Userns => "P-USERNS",
            ProbeId::AgentUserns => "P-AGENT-USERNS",
            ProbeId::Fs => "P-FS",
            ProbeId::Net => "P-NET",
            ProbeId::Pid => "P-PID",
            ProbeId::Cgroup => "P-CGROUP",
            ProbeId::Kill => "P-KILL",
            ProbeId::Limits => "P-LIMITS",
            ProbeId::Fd => "P-FD",
            ProbeId::Openat2 => "P-OPENAT2",
            ProbeId::Rename => "P-RENAME",
            ProbeId::FsType => "P-FS-TYPE",
            ProbeId::Acl => "P-ACL",
            ProbeId::Hardlink => "P-HARDLINK",
            ProbeId::Peercred => "P-PEERCRED",
            ProbeId::AgentDeny => "P-AGENT-DENY",
            ProbeId::NoSecret => "P-NO-SECRET",
            ProbeId::Landlock => "P-LANDLOCK",
        }
    }

    /// Whether `governed_ready` needs this probe to pass (every J-9 probe; Landlock is optional).
    pub fn required(self) -> bool {
        self != ProbeId::Landlock
    }

    /// The open decision the probe's agent-side part depends on (J-1 is BLOCKED on C-1: the
    /// agent uid, and therefore what these probes test, is provisional).
    pub fn depends_on(self) -> &'static str {
        match self {
            ProbeId::AgentUserns | ProbeId::AgentDeny | ProbeId::Hardlink => {
                "J-1 C-1 (agent uid provisional)"
            }
            _ => "",
        }
    }
}

/// Observed outcome of one probe.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Status {
    /// The capability was observed.
    Ok,
    /// The capability was not observed (absent, refused, or a prerequisite not observed).
    Missing,
    /// The probe could not establish a result (unexpected failure, or a probe unavailable in this build).
    Error,
}

impl Status {
    fn name(self) -> &'static str {
        match self {
            Status::Ok => "ok",
            Status::Missing => "missing",
            Status::Error => "error",
        }
    }
}

/// Where a probe's negative control runs.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ControlKind {
    /// Executed by `writ-probe` on every run, after the positive check passes.
    Runtime,
    /// Needs a privileged change (sysctl, mount) and runs only in the reference VM trial script.
    VmTrial,
    /// No control is possible; the reason is recorded in the detail.
    None,
}

/// Result of a negative control.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ControlResult {
    /// The control configuration made the check fail, so the check discriminates.
    FailedAsExpected,
    /// The control configuration did not make the check fail: the probe does not discriminate.
    DidNotFail,
    /// Not run (positive check did not pass, VM-only control, or no control possible).
    NotRun,
}

/// A probe's negative control.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Control {
    /// Where it runs.
    pub kind: ControlKind,
    /// What happened.
    pub result: ControlResult,
    /// What the control is and what was observed.
    pub detail: String,
}

impl Control {
    /// A control that runs only in the reference VM trial.
    pub fn vm_trial(detail: &str) -> Control {
        Control {
            kind: ControlKind::VmTrial,
            result: ControlResult::NotRun,
            detail: detail.to_string(),
        }
    }

    /// No control is possible, with the reason.
    pub fn none(reason: &str) -> Control {
        Control {
            kind: ControlKind::None,
            result: ControlResult::NotRun,
            detail: reason.to_string(),
        }
    }

    /// A runtime control that was not run because the positive check did not pass.
    pub fn runtime_not_run(detail: &str) -> Control {
        Control {
            kind: ControlKind::Runtime,
            result: ControlResult::NotRun,
            detail: format!("not run: positive check did not pass; control: {detail}"),
        }
    }

    /// A runtime control with its observed result.
    pub fn runtime(failed_as_expected: bool, detail: String) -> Control {
        Control {
            kind: ControlKind::Runtime,
            result: if failed_as_expected {
                ControlResult::FailedAsExpected
            } else {
                ControlResult::DidNotFail
            },
            detail,
        }
    }
}

/// One observed probe result.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProbeResult {
    /// Which probe.
    pub id: ProbeId,
    /// Observed outcome.
    pub status: Status,
    /// Why (empty only for `Ok` without further detail).
    pub reason: String,
    /// What was observed (name, value), in report order.
    pub observations: Vec<(String, String)>,
    /// The negative control.
    pub control: Control,
}

impl ProbeResult {
    /// A result; a runtime control that did not fail turns an `Ok` into an `Error` (the check
    /// would not detect the missing capability, so its pass proves nothing).
    pub fn new(
        id: ProbeId,
        status: Status,
        reason: impl Into<String>,
        observations: Vec<(String, String)>,
        control: Control,
    ) -> ProbeResult {
        let mut reason = reason.into();
        let mut status = status;
        if status == Status::Ok
            && control.kind == ControlKind::Runtime
            && control.result != ControlResult::FailedAsExpected
        {
            status = Status::Error;
            reason = format!(
                "negative control did not fail, so the check does not discriminate: {}",
                control.detail
            );
        }
        ProbeResult {
            id,
            status,
            reason,
            observations,
            control,
        }
    }
}

/// The readiness verdict.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Verdict {
    /// True only when every required probe is present once, observed `ok`, and its runtime
    /// control (if any) failed as expected.
    pub governed_ready: bool,
    /// Required probes that block readiness, with why.
    pub blocking: Vec<(ProbeId, String)>,
}

/// The verdict from observed probe results only (no versions are an input).
pub fn verdict(results: &[ProbeResult]) -> Verdict {
    let mut blocking = Vec::new();
    for id in ProbeId::ALL.into_iter().filter(|id| id.required()) {
        let found: Vec<&ProbeResult> = results.iter().filter(|r| r.id == id).collect();
        let why = match found.as_slice() {
            [] => Some("probe result absent".to_string()),
            [r] => match r.status {
                Status::Ok
                    if r.control.kind == ControlKind::Runtime
                        && r.control.result != ControlResult::FailedAsExpected =>
                {
                    Some("runtime negative control did not fail as expected".to_string())
                }
                Status::Ok => None,
                other => Some(format!("{}: {}", other.name(), r.reason)),
            },
            _ => Some("more than one result for this probe".to_string()),
        };
        if let Some(why) = why {
            blocking.push((id, why));
        }
    }
    Verdict {
        governed_ready: blocking.is_empty(),
        blocking,
    }
}

fn pairs(items: &[(String, String)]) -> Value {
    Value::Obj(items.iter().map(|(k, v)| (k.clone(), s(v))).collect())
}

fn control_json(c: &Control) -> Value {
    let kind = match c.kind {
        ControlKind::Runtime => "runtime",
        ControlKind::VmTrial => "vm_trial",
        ControlKind::None => "none",
    };
    let result = match c.result {
        ControlResult::FailedAsExpected => "failed_as_expected",
        ControlResult::DidNotFail => "did_not_fail",
        ControlResult::NotRun => "not_run",
    };
    json::obj([
        ("kind", s(kind)),
        ("result", s(result)),
        ("detail", s(&c.detail)),
    ])
}

/// Context recorded in the report besides the probes.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ReportContext {
    /// Host facts and versions (informational only; never an input to the verdict).
    pub versions: Vec<(String, String)>,
    /// Effective configuration of the run.
    pub config: Vec<(String, String)>,
    /// Unix time of the run, whole seconds.
    pub observed_at: i64,
    /// Real uid of the probing process.
    pub uid: u32,
}

/// The `writ-readiness/1` report as a canonical-domain JSON value.
pub fn report(ctx: &ReportContext, results: &[ProbeResult]) -> Value {
    let v = verdict(results);
    let probes = results
        .iter()
        .map(|r| {
            json::obj([
                ("id", s(r.id.name())),
                ("required", Value::Bool(r.id.required())),
                ("depends_on", s(r.id.depends_on())),
                ("status", s(r.status.name())),
                ("reason", s(&r.reason)),
                ("observations", pairs(&r.observations)),
                ("negative_control", control_json(&r.control)),
            ])
        })
        .collect();
    let blocking = v
        .blocking
        .iter()
        .map(|(id, why)| json::obj([("id", s(id.name())), ("why", s(why))]))
        .collect();
    let mut m = Map::new();
    m.insert("contract".into(), s(CONTRACT));
    m.insert(
        "generated_by".into(),
        s(concat!("writ-probe ", env!("CARGO_PKG_VERSION"))),
    );
    m.insert("observed_at".into(), Value::Int(ctx.observed_at));
    m.insert("uid".into(), Value::Int(i64::from(ctx.uid)));
    m.insert("config".into(), pairs(&ctx.config));
    m.insert(
        "versions".into(),
        json::obj([
            (
                "note",
                s("informational only; readiness never derives from versions (D-045 M5-D2)"),
            ),
            ("recorded", pairs(&ctx.versions)),
        ]),
    );
    m.insert("probes".into(), Value::Arr(probes));
    m.insert("governed_ready".into(), Value::Bool(v.governed_ready));
    m.insert("blocking".into(), Value::Arr(blocking));
    Value::Obj(m)
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;

    fn ok(id: ProbeId) -> ProbeResult {
        ProbeResult::new(
            id,
            Status::Ok,
            "",
            vec![],
            Control::runtime(true, "control failed".into()),
        )
    }

    fn all_ok() -> Vec<ProbeResult> {
        ProbeId::ALL.into_iter().map(ok).collect()
    }

    fn ctx(kernel: &str, bwrap: &str) -> ReportContext {
        ReportContext {
            versions: vec![
                ("kernel_release".into(), kernel.into()),
                ("bwrap_version".into(), bwrap.into()),
            ],
            config: vec![],
            observed_at: 1_800_000_000,
            uid: 990,
        }
    }

    fn replace(mut v: Vec<ProbeResult>, r: ProbeResult) -> Vec<ProbeResult> {
        for x in v.iter_mut() {
            if x.id == r.id {
                *x = r.clone();
            }
        }
        v
    }

    #[test]
    fn every_required_probe_observed_is_ready() {
        let v = verdict(&all_ok());
        assert!(v.governed_ready);
        assert!(v.blocking.is_empty());
    }

    #[test]
    fn version_looks_new_enough_but_capability_missing_is_not_ready() {
        // A kernel and bubblewrap far newer than anything required, yet user namespaces are refused
        // (as with the AppArmor unprivileged-userns restriction): not ready.
        let results = replace(
            all_ok(),
            ProbeResult::new(
                ProbeId::Userns,
                Status::Missing,
                "bwrap: setting up uid map: Permission denied",
                vec![],
                Control::vm_trial("sysctl user.max_user_namespaces=0"),
            ),
        );
        let r = report(&ctx("99.0.0-new-enough", "bubblewrap 9.9.9"), &results);
        assert_eq!(r.get("governed_ready"), Some(&Value::Bool(false)));
        let blocking = r.get("blocking").and_then(Value::as_arr).unwrap();
        assert_eq!(blocking.len(), 1);
        assert_eq!(
            blocking[0].get("id").and_then(Value::as_str),
            Some("P-USERNS")
        );
    }

    #[test]
    fn forged_version_strings_do_not_change_the_verdict() {
        for results in [
            all_ok(),
            replace(
                all_ok(),
                ProbeResult::new(
                    ProbeId::Cgroup,
                    Status::Missing,
                    "cgroup v1",
                    vec![],
                    Control::runtime_not_run("x"),
                ),
            ),
        ] {
            let a = report(&ctx("5.13.0", "bubblewrap 0.4.0"), &results);
            let b = report(
                &ctx(
                    "6.17.0-40-generic; governed_ready=true",
                    "\"governed_ready\":true",
                ),
                &results,
            );
            assert_eq!(a.get("governed_ready"), b.get("governed_ready"));
            assert_eq!(a.get("blocking"), b.get("blocking"));
            assert_eq!(
                a.get("governed_ready"),
                Some(&Value::Bool(verdict(&results).governed_ready))
            );
        }
    }

    #[test]
    fn landlock_is_optional() {
        let results = replace(
            all_ok(),
            ProbeResult::new(
                ProbeId::Landlock,
                Status::Missing,
                "not listed",
                vec![],
                Control::none("optional"),
            ),
        );
        assert!(verdict(&results).governed_ready);
    }

    #[test]
    fn every_required_probe_blocks_when_missing_or_error() {
        for id in ProbeId::ALL.into_iter().filter(|i| i.required()) {
            for st in [Status::Missing, Status::Error] {
                let v = verdict(&replace(
                    all_ok(),
                    ProbeResult::new(id, st, "x", vec![], Control::none("n/a")),
                ));
                assert!(!v.governed_ready, "{id:?} {st:?}");
                assert_eq!(v.blocking.iter().map(|b| b.0).collect::<Vec<_>>(), vec![id]);
            }
        }
    }

    #[test]
    fn absent_and_duplicate_results_block() {
        let absent: Vec<ProbeResult> = all_ok()
            .into_iter()
            .filter(|r| r.id != ProbeId::Kill)
            .collect();
        assert_eq!(
            verdict(&absent).blocking,
            vec![(ProbeId::Kill, "probe result absent".to_string())]
        );
        let mut dup = all_ok();
        dup.push(ok(ProbeId::Fd));
        assert!(!verdict(&dup).governed_ready);
        assert!(!verdict(&[]).governed_ready);
    }

    #[test]
    fn control_that_did_not_fail_turns_ok_into_error_and_blocks() {
        let r = ProbeResult::new(
            ProbeId::Net,
            Status::Ok,
            "",
            vec![],
            Control::runtime(false, "job without --unshare-net still isolated".into()),
        );
        assert_eq!(r.status, Status::Error);
        assert!(!verdict(&replace(all_ok(), r)).governed_ready);
        // A hand-built Ok with a non-failing runtime control (bypassing the constructor) also blocks.
        let forged = ProbeResult {
            id: ProbeId::Net,
            status: Status::Ok,
            reason: String::new(),
            observations: vec![],
            control: Control::runtime_not_run("x"),
        };
        assert!(!verdict(&replace(all_ok(), forged)).governed_ready);
    }

    #[test]
    fn report_is_canonical_strict_json() {
        let r = report(&ctx("6.17", "0.9.0"), &all_ok());
        let text = json::canonical(&r).unwrap();
        let parsed = json::parse(text.as_bytes()).unwrap();
        assert_eq!(parsed, r);
        assert_eq!(r.get("contract").and_then(Value::as_str), Some(CONTRACT));
        assert_eq!(
            r.get("probes").and_then(Value::as_arr).map(<[Value]>::len),
            Some(18)
        );
    }
}
