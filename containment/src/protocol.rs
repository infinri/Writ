//! The `launch.sock` messages (J-11.2, J-11.3, J-11.5): `writ-launch-hello/1`, the request
//! `writ-launch/1`, `writ-launch-cancel/1`, and the launcher's `refused`, `started` and
//! `writ-launch-result/1` frames. Every control payload is one strict JSON document parsed by
//! the kernel parser (INV-41); unknown fields are refused.

use writ_kernel::json::{self, s, Map, Value};

/// Request contract.
pub const REQUEST: &str = "writ-launch/1";
/// Hello contract.
pub const HELLO: &str = "writ-launch-hello/1";
/// Cancel contract.
pub const CANCEL: &str = "writ-launch-cancel/1";
/// Refused contract.
pub const REFUSED: &str = "writ-launch-refused/1";
/// Started contract.
pub const STARTED: &str = "writ-launch-started/1";
/// Result contract.
pub const RESULT: &str = "writ-launch-result/1";

/// Largest `job_id` (J-11.3).
pub const JOB_ID_MAX: usize = 64;
/// Largest canonical relative path of the program.
pub const PROGRAM_MAX: usize = 1024;
/// Deepest program path.
pub const PROGRAM_DEPTH_MAX: usize = 16;

/// Launcher refusal codes (J-11.3). Diagnostics recorded by `writ`; never shown to the agent
/// (generic external mapping, INV-44).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LaunchCode {
    /// Malformed or unknown fields, values outside their rules.
    Invalid,
    /// The runtime profile is not on the allow-list.
    ProfileUnknown,
    /// The derived source directory violates J-11.3.
    SourceInvalid,
    /// A limit above the root-configured maximum.
    LimitExceeded,
    /// Another job is running (J-11.7).
    Busy,
    /// A readiness or residue check failed.
    Unavailable,
}

impl LaunchCode {
    /// The code string.
    pub fn as_str(self) -> &'static str {
        match self {
            LaunchCode::Invalid => "LAUNCH_INVALID",
            LaunchCode::ProfileUnknown => "LAUNCH_PROFILE_UNKNOWN",
            LaunchCode::SourceInvalid => "LAUNCH_SOURCE_INVALID",
            LaunchCode::LimitExceeded => "LAUNCH_LIMIT_EXCEEDED",
            LaunchCode::Busy => "LAUNCH_BUSY",
            LaunchCode::Unavailable => "LAUNCH_UNAVAILABLE",
        }
    }

    /// The code named `text`.
    pub fn parse(text: &str) -> Option<LaunchCode> {
        [
            LaunchCode::Invalid,
            LaunchCode::ProfileUnknown,
            LaunchCode::SourceInvalid,
            LaunchCode::LimitExceeded,
            LaunchCode::Busy,
            LaunchCode::Unavailable,
        ]
        .into_iter()
        .find(|c| c.as_str() == text)
    }
}

/// Names of the request limits, in contract order.
pub const LIMIT_NAMES: [&str; 10] = [
    "wall_time_ms",
    "memory_max_bytes",
    "pids_max",
    "cpu_max_quota_us",
    "cpu_max_period_us",
    "output_max_bytes",
    "input_max_bytes",
    "file_size_max_bytes",
    "nofile_max",
    "sigpending_max",
];

/// The limits of one job (every one required; J-11.3).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct JobLimits {
    /// Wall time.
    pub wall_time_ms: u64,
    /// `memory.max`.
    pub memory_max_bytes: u64,
    /// `pids.max`.
    pub pids_max: u64,
    /// `cpu.max` quota.
    pub cpu_max_quota_us: u64,
    /// `cpu.max` period.
    pub cpu_max_period_us: u64,
    /// Total stdout plus stderr bytes.
    pub output_max_bytes: u64,
    /// Bytes read from the source directory.
    pub input_max_bytes: u64,
    /// `RLIMIT_FSIZE`.
    pub file_size_max_bytes: u64,
    /// `RLIMIT_NOFILE`.
    pub nofile_max: u64,
    /// `RLIMIT_SIGPENDING`.
    pub sigpending_max: u64,
}

impl JobLimits {
    /// The values in [`LIMIT_NAMES`] order.
    pub fn values(&self) -> [u64; 10] {
        [
            self.wall_time_ms,
            self.memory_max_bytes,
            self.pids_max,
            self.cpu_max_quota_us,
            self.cpu_max_period_us,
            self.output_max_bytes,
            self.input_max_bytes,
            self.file_size_max_bytes,
            self.nofile_max,
            self.sigpending_max,
        ]
    }

    fn from_values(v: [u64; 10]) -> JobLimits {
        JobLimits {
            wall_time_ms: v[0],
            memory_max_bytes: v[1],
            pids_max: v[2],
            cpu_max_quota_us: v[3],
            cpu_max_period_us: v[4],
            output_max_bytes: v[5],
            input_max_bytes: v[6],
            file_size_max_bytes: v[7],
            nofile_max: v[8],
            sigpending_max: v[9],
        }
    }

    /// Parse an object with exactly the ten limits, each a positive integer.
    pub fn from_value(v: &Value) -> Option<JobLimits> {
        let m = v.as_obj()?;
        if m.len() != LIMIT_NAMES.len() {
            return None;
        }
        let mut out = [0u64; 10];
        for (slot, name) in out.iter_mut().zip(LIMIT_NAMES) {
            let n = m.get(name)?.as_int()?;
            if n < 1 {
                return None;
            }
            *slot = u64::try_from(n).ok()?;
        }
        Some(JobLimits::from_values(out))
    }

    /// The limits as a JSON object.
    pub fn to_value(&self) -> Value {
        Value::Obj(
            LIMIT_NAMES
                .iter()
                .zip(self.values())
                .map(|(k, v)| {
                    (
                        k.to_string(),
                        Value::Int(i64::try_from(v).unwrap_or(i64::MAX)),
                    )
                })
                .collect(),
        )
    }

    /// The first limit above `max`, if any.
    pub fn first_exceeding(&self, max: &JobLimits) -> Option<&'static str> {
        LIMIT_NAMES
            .iter()
            .zip(self.values().into_iter().zip(max.values()))
            .find(|(_, (v, m))| v > m)
            .map(|(n, _)| *n)
    }

    /// Values the kernel accepts (`cpu.max` period 1000..=1000000 us, quota at least 1000 us)
    /// and that leave the job its standard descriptors.
    pub fn kernel_valid(&self) -> bool {
        (1000..=1_000_000).contains(&self.cpu_max_period_us)
            && self.cpu_max_quota_us >= 1000
            && self.nofile_max >= 8
    }
}

/// Bounds on the request's variable parts (from the launcher configuration).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct RequestBounds {
    /// Most arguments.
    pub max_args: usize,
    /// Largest argument.
    pub max_arg_bytes: usize,
    /// Most environment entries.
    pub max_env: usize,
    /// Largest environment value.
    pub max_env_bytes: usize,
}

/// A validated `writ-launch/1` request.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Request {
    /// The run id; the source is `run-<job_id>` (J-11.3, M4).
    pub job_id: String,
    /// An allow-listed runtime profile id.
    pub runtime_profile: String,
    /// Canonical relative path of the program inside the source.
    pub program: String,
    /// Arguments (reach only the unprivileged job).
    pub args: Vec<String>,
    /// Environment of the job.
    pub env: Vec<(String, String)>,
    /// Limits.
    pub limits: JobLimits,
    /// D-006 digest of the request document (`request_digest`).
    pub digest: String,
}

/// `job_id` rule: 1..=64 bytes of `[a-z0-9-]`.
pub fn valid_job_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= JOB_ID_MAX
        && id
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
}

/// Canonical relative path (J-7.1): UTF-8, single `/` separators, no leading or trailing `/`,
/// no empty, `.` or `..` component, no NUL or control character, bounded length and depth.
pub fn valid_relative_path(p: &str) -> bool {
    !p.is_empty()
        && p.len() <= PROGRAM_MAX
        && !p.chars().any(char::is_control)
        && p.split('/').count() <= PROGRAM_DEPTH_MAX
        && p.split('/').all(|c| !c.is_empty() && c != "." && c != "..")
}

/// Environment name rule `[A-Za-z_][A-Za-z0-9_]*`.
pub fn valid_env_name(n: &str) -> bool {
    let mut b = n.bytes();
    matches!(b.next(), Some(c) if c.is_ascii_alphabetic() || c == b'_')
        && b.all(|c| c.is_ascii_alphanumeric() || c == b'_')
}

fn exact_keys(m: &Map, keys: &[&str]) -> bool {
    m.len() == keys.len() && keys.iter().all(|k| m.contains_key(*k))
}

fn no_nul(t: &str) -> bool {
    !t.contains('\0')
}

impl Request {
    /// The request document.
    pub fn to_value(&self) -> Value {
        json::obj([
            ("contract", s(REQUEST)),
            ("job_id", s(&self.job_id)),
            ("runtime_profile", s(&self.runtime_profile)),
            ("program", s(&self.program)),
            ("args", Value::Arr(self.args.iter().map(|a| s(a)).collect())),
            (
                "env",
                Value::Arr(
                    self.env
                        .iter()
                        .map(|(k, v)| Value::Arr(vec![s(k), s(v)]))
                        .collect(),
                ),
            ),
            ("limits", self.limits.to_value()),
        ])
    }

    /// A request built by `writ` (the digest computed here); `None` when a value is outside
    /// the D-006 domain.
    pub fn new(
        job_id: &str,
        runtime_profile: &str,
        program: &str,
        args: Vec<String>,
        env: Vec<(String, String)>,
        limits: JobLimits,
    ) -> Option<Request> {
        let mut r = Request {
            job_id: job_id.to_string(),
            runtime_profile: runtime_profile.to_string(),
            program: program.to_string(),
            args,
            env,
            limits,
            digest: String::new(),
        };
        r.digest = json::digest(&r.to_value()).ok()?.as_str().to_string();
        Some(r)
    }
}

/// Parse and validate one request payload against the configured bounds, maxima and runtime
/// profile allow-list. Order of checks: syntax and fields (`LAUNCH_INVALID`), profile
/// (`LAUNCH_PROFILE_UNKNOWN`), limits (`LAUNCH_LIMIT_EXCEEDED`).
pub fn parse_request(
    payload: &[u8],
    bounds: &RequestBounds,
    maxima: &JobLimits,
    profiles: &[&str],
) -> Result<Request, LaunchCode> {
    use LaunchCode::Invalid;
    let v = json::parse(payload).map_err(|_| Invalid)?;
    let m = v.as_obj().ok_or(Invalid)?;
    if !exact_keys(
        m,
        &[
            "contract",
            "job_id",
            "runtime_profile",
            "program",
            "args",
            "env",
            "limits",
        ],
    ) {
        return Err(Invalid);
    }
    let text = |k: &str| m.get(k).and_then(Value::as_str).ok_or(Invalid);
    if text("contract")? != REQUEST {
        return Err(Invalid);
    }
    let job_id = text("job_id")?;
    let profile = text("runtime_profile")?;
    let program = text("program")?;
    if !valid_job_id(job_id) || !valid_relative_path(program) || profile.is_empty() {
        return Err(Invalid);
    }
    let args_v = m.get("args").and_then(Value::as_arr).ok_or(Invalid)?;
    if args_v.len() > bounds.max_args {
        return Err(Invalid);
    }
    let mut args = Vec::with_capacity(args_v.len());
    for a in args_v {
        let a = a.as_str().ok_or(Invalid)?;
        if a.len() > bounds.max_arg_bytes || !no_nul(a) {
            return Err(Invalid);
        }
        args.push(a.to_string());
    }
    let env_v = m.get("env").and_then(Value::as_arr).ok_or(Invalid)?;
    if env_v.len() > bounds.max_env {
        return Err(Invalid);
    }
    let mut env: Vec<(String, String)> = Vec::with_capacity(env_v.len());
    for e in env_v {
        let pair = e.as_arr().ok_or(Invalid)?;
        let [k, val] = pair else {
            return Err(Invalid);
        };
        let (k, val) = (k.as_str().ok_or(Invalid)?, val.as_str().ok_or(Invalid)?);
        if !valid_env_name(k)
            || k.len() > bounds.max_env_bytes
            || val.len() > bounds.max_env_bytes
            || !no_nul(val)
            || env.iter().any(|(n, _)| n == k)
        {
            return Err(Invalid);
        }
        env.push((k.to_string(), val.to_string()));
    }
    let limits = m
        .get("limits")
        .and_then(JobLimits::from_value)
        .ok_or(Invalid)?;
    if !limits.kernel_valid() {
        return Err(Invalid);
    }
    if !profiles.contains(&profile) {
        return Err(LaunchCode::ProfileUnknown);
    }
    if limits.first_exceeding(maxima).is_some() {
        return Err(LaunchCode::LimitExceeded);
    }
    let digest = json::digest(&v).map_err(|_| Invalid)?.as_str().to_string();
    Ok(Request {
        job_id: job_id.to_string(),
        runtime_profile: profile.to_string(),
        program: program.to_string(),
        args,
        env,
        limits,
        digest,
    })
}

/// The launcher's `hello` facts (J-11.2).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Hello {
    /// SHA-256 digest of the launcher executable.
    pub launcher_digest: String,
    /// SHA-256 digest of `writ-cgroup-exec`.
    pub exec_digest: String,
    /// SHA-256 digest of the private bubblewrap.
    pub bwrap_digest: String,
    /// D-006 digest of the fixed bubblewrap profile `writ-bwrap/1`.
    pub bwrap_profile_digest: String,
    /// SHA-256 digest of `/etc/apparmor.d/writ-bwrap`.
    pub apparmor_profile_digest: String,
    /// Allow-listed runtime profiles with their manifest digests.
    pub runtime_profiles: Vec<(String, String)>,
    /// D-006 digest of the configured maxima.
    pub maxima_digest: String,
    /// The systemd instance name.
    pub instance: String,
    /// The build mode (L7): [`BUILD_RELEASE`] or [`BUILD_TEST_FAULTS`]; pinned, so a
    /// test-faults launcher never matches an installed pin.
    pub build: String,
}

/// `build` of an installable launcher.
pub const BUILD_RELEASE: &str = "release";
/// `build` of a test-only launcher (feature `test-faults`).
pub const BUILD_TEST_FAULTS: &str = "test-faults";

/// The build mode of this binary.
pub fn build_mode() -> &'static str {
    if crate::TEST_FAULTS {
        BUILD_TEST_FAULTS
    } else {
        BUILD_RELEASE
    }
}

const HELLO_KEYS: [&str; 10] = [
    "build",
    "contract",
    "launcher_digest",
    "exec_digest",
    "bwrap_digest",
    "bwrap_profile_digest",
    "apparmor_profile_digest",
    "runtime_profiles",
    "maxima_digest",
    "instance",
];

impl Hello {
    /// The hello document.
    pub fn to_value(&self) -> Value {
        json::obj([
            ("contract", s(HELLO)),
            ("build", s(&self.build)),
            ("launcher_digest", s(&self.launcher_digest)),
            ("exec_digest", s(&self.exec_digest)),
            ("bwrap_digest", s(&self.bwrap_digest)),
            ("bwrap_profile_digest", s(&self.bwrap_profile_digest)),
            ("apparmor_profile_digest", s(&self.apparmor_profile_digest)),
            (
                "runtime_profiles",
                Value::Arr(
                    self.runtime_profiles
                        .iter()
                        .map(|(id, d)| json::obj([("id", s(id)), ("digest", s(d))]))
                        .collect(),
                ),
            ),
            ("maxima_digest", s(&self.maxima_digest)),
            ("instance", s(&self.instance)),
        ])
    }

    /// Parse a hello payload (strict, exact fields).
    pub fn parse(payload: &[u8]) -> Option<Hello> {
        let v = json::parse(payload).ok()?;
        let m = v.as_obj()?;
        if !exact_keys(m, &HELLO_KEYS) || m.get("contract")?.as_str()? != HELLO {
            return None;
        }
        let t = |k: &str| m.get(k).and_then(Value::as_str).map(str::to_string);
        let mut runtime_profiles = Vec::new();
        for p in m.get("runtime_profiles")?.as_arr()? {
            let pm = p.as_obj()?;
            if !exact_keys(pm, &["id", "digest"]) {
                return None;
            }
            runtime_profiles.push((
                pm.get("id")?.as_str()?.to_string(),
                pm.get("digest")?.as_str()?.to_string(),
            ));
        }
        Some(Hello {
            launcher_digest: t("launcher_digest")?,
            exec_digest: t("exec_digest")?,
            bwrap_digest: t("bwrap_digest")?,
            bwrap_profile_digest: t("bwrap_profile_digest")?,
            apparmor_profile_digest: t("apparmor_profile_digest")?,
            runtime_profiles,
            maxima_digest: t("maxima_digest")?,
            instance: t("instance")?,
            build: t("build")?,
        })
    }

    /// Whether the pinned facts equal these (the instance name is not pinned).
    pub fn matches(&self, pinned: &Hello) -> bool {
        Hello {
            instance: String::new(),
            ..self.clone()
        } == Hello {
            instance: String::new(),
            ..pinned.clone()
        }
    }

    /// The manifest digest of runtime profile `id`.
    pub fn runtime_profile(&self, id: &str) -> Option<&str> {
        self.runtime_profiles
            .iter()
            .find(|(i, _)| i == id)
            .map(|(_, d)| d.as_str())
    }
}

/// A `refused` frame.
pub fn refused(job_id: &str, code: LaunchCode) -> Value {
    json::obj([
        ("contract", s(REFUSED)),
        ("job_id", s(job_id)),
        ("code", s(code.as_str())),
    ])
}

/// A `started` frame.
pub fn started(job_id: &str) -> Value {
    json::obj([("contract", s(STARTED)), ("job_id", s(job_id))])
}

/// A `cancel` frame from `writ`.
pub fn cancel(job_id: &str) -> Value {
    json::obj([("contract", s(CANCEL)), ("job_id", s(job_id))])
}

/// Parse a control frame from `writ` after the request: only `cancel` for this job.
pub fn is_cancel(payload: &[u8], job_id: &str) -> bool {
    let Ok(v) = json::parse(payload) else {
        return false;
    };
    let Some(m) = v.as_obj() else {
        return false;
    };
    exact_keys(m, &["contract", "job_id"])
        && m.get("contract").and_then(Value::as_str) == Some(CANCEL)
        && m.get("job_id").and_then(Value::as_str) == Some(job_id)
}

/// Why a job ended (J-11.5).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Reason {
    /// The direct child exited.
    Exited,
    /// The direct child was ended by a signal not sent by the launcher.
    Signaled,
    /// Wall time reached.
    TimedOut,
    /// Output limit or bounded outbound buffer exceeded.
    OutputExceeded,
    /// `cancel` from `writ`.
    Cancelled,
    /// Failure after the helper was spawned and before `started`.
    StartFailed,
}

impl Reason {
    /// The contract string.
    pub fn as_str(self) -> &'static str {
        match self {
            Reason::Exited => "exited",
            Reason::Signaled => "signaled",
            Reason::TimedOut => "timed_out",
            Reason::OutputExceeded => "output_exceeded",
            Reason::Cancelled => "cancelled",
            Reason::StartFailed => "start_failed",
        }
    }

    /// The reason named `t`.
    pub fn parse(t: &str) -> Option<Reason> {
        [
            Reason::Exited,
            Reason::Signaled,
            Reason::TimedOut,
            Reason::OutputExceeded,
            Reason::Cancelled,
            Reason::StartFailed,
        ]
        .into_iter()
        .find(|r| r.as_str() == t)
    }
}

/// The digests reported with a result.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ResultDigests {
    /// Private bubblewrap.
    pub bwrap: String,
    /// `writ-bwrap/1` profile.
    pub bwrap_profile: String,
    /// AppArmor profile file.
    pub apparmor_profile: String,
    /// The runtime profile used.
    pub runtime_profile: String,
    /// `writ-cgroup-exec`.
    pub exec: String,
}

/// The `finished` frame `writ-launch-result/1`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Finished {
    /// The request's job id.
    pub job_id: String,
    /// The request's digest.
    pub request_digest: String,
    /// Why it ended.
    pub reason: Reason,
    /// Exit status when it exited.
    pub exit_status: Option<i64>,
    /// Signal number when signaled.
    pub signal: Option<i64>,
    /// `cgroup.kill` written.
    pub kill_used: bool,
    /// `populated 0` observed for the job cgroup.
    pub populated_zero: bool,
    /// `memory.events` max, oom, oom_kill.
    pub memory_events: [u64; 3],
    /// `pids.events` max.
    pub pids_max_events: u64,
    /// `cpu.stat` usage_usec.
    pub cpu_usage_usec: u64,
    /// Bytes of stdout and stderr read.
    pub output_bytes: [u64; 2],
    /// Reported digests.
    pub digests: ResultDigests,
}

fn int(n: u64) -> Value {
    Value::Int(
        i64::try_from(n)
            .unwrap_or(json::MAX_SAFE_INTEGER)
            .min(json::MAX_SAFE_INTEGER),
    )
}

fn opt_int(n: Option<i64>) -> Value {
    n.map_or(Value::Null, Value::Int)
}

impl Finished {
    /// The result document.
    pub fn to_value(&self) -> Value {
        let d = &self.digests;
        json::obj([
            ("contract", s(RESULT)),
            ("job_id", s(&self.job_id)),
            ("request_digest", s(&self.request_digest)),
            ("reason", s(self.reason.as_str())),
            ("exit_status", opt_int(self.exit_status)),
            ("signal", opt_int(self.signal)),
            (
                "kill",
                json::obj([
                    ("used", Value::Bool(self.kill_used)),
                    ("populated_zero", Value::Bool(self.populated_zero)),
                ]),
            ),
            (
                "memory_events",
                json::obj([
                    ("max", int(self.memory_events[0])),
                    ("oom", int(self.memory_events[1])),
                    ("oom_kill", int(self.memory_events[2])),
                ]),
            ),
            (
                "pids_events",
                json::obj([("max", int(self.pids_max_events))]),
            ),
            ("cpu_usage_usec", int(self.cpu_usage_usec)),
            (
                "output_bytes",
                json::obj([
                    ("stdout", int(self.output_bytes[0])),
                    ("stderr", int(self.output_bytes[1])),
                ]),
            ),
            (
                "digests",
                json::obj([
                    ("bwrap", s(&d.bwrap)),
                    ("bwrap_profile", s(&d.bwrap_profile)),
                    ("apparmor_profile", s(&d.apparmor_profile)),
                    ("runtime_profile", s(&d.runtime_profile)),
                    ("exec", s(&d.exec)),
                ]),
            ),
        ])
    }

    /// Parse a result payload (strict, exact fields).
    pub fn parse(payload: &[u8]) -> Option<Finished> {
        let v = json::parse(payload).ok()?;
        let m = v.as_obj()?;
        if !exact_keys(
            m,
            &[
                "contract",
                "job_id",
                "request_digest",
                "reason",
                "exit_status",
                "signal",
                "kill",
                "memory_events",
                "pids_events",
                "cpu_usage_usec",
                "output_bytes",
                "digests",
            ],
        ) || m.get("contract")?.as_str()? != RESULT
        {
            return None;
        }
        let u = |v: &Value| v.as_int().and_then(|n| u64::try_from(n).ok());
        let opt = |k: &str| match m.get(k)? {
            Value::Null => Some(None),
            Value::Int(n) => Some(Some(*n)),
            _ => None,
        };
        let sub = |k: &str, keys: &[&str]| -> Option<Map> {
            let o = m.get(k)?.as_obj()?;
            exact_keys(o, keys).then(|| o.clone())
        };
        let kill = sub("kill", &["used", "populated_zero"])?;
        let mem = sub("memory_events", &["max", "oom", "oom_kill"])?;
        let pids = sub("pids_events", &["max"])?;
        let out = sub("output_bytes", &["stdout", "stderr"])?;
        let dg = sub(
            "digests",
            &[
                "bwrap",
                "bwrap_profile",
                "apparmor_profile",
                "runtime_profile",
                "exec",
            ],
        )?;
        let ds = |k: &str| dg.get(k).and_then(Value::as_str).map(str::to_string);
        Some(Finished {
            job_id: m.get("job_id")?.as_str()?.to_string(),
            request_digest: m.get("request_digest")?.as_str()?.to_string(),
            reason: Reason::parse(m.get("reason")?.as_str()?)?,
            exit_status: opt("exit_status")?,
            signal: opt("signal")?,
            kill_used: kill.get("used")?.as_bool()?,
            populated_zero: kill.get("populated_zero")?.as_bool()?,
            memory_events: [
                u(mem.get("max")?)?,
                u(mem.get("oom")?)?,
                u(mem.get("oom_kill")?)?,
            ],
            pids_max_events: u(pids.get("max")?)?,
            cpu_usage_usec: u(m.get("cpu_usage_usec")?)?,
            output_bytes: [u(out.get("stdout")?)?, u(out.get("stderr")?)?],
            digests: ResultDigests {
                bwrap: ds("bwrap")?,
                bwrap_profile: ds("bwrap_profile")?,
                apparmor_profile: ds("apparmor_profile")?,
                runtime_profile: ds("runtime_profile")?,
                exec: ds("exec")?,
            },
        })
    }
}

/// A launcher control frame as seen by `writ`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum LauncherFrame {
    /// `refused` with its code.
    Refused(String, LaunchCode),
    /// `started`.
    Started(String),
    /// `finished`.
    Finished(Box<Finished>),
}

/// Parse a launcher control frame after `hello`.
pub fn parse_launcher_frame(payload: &[u8]) -> Option<LauncherFrame> {
    let v = json::parse(payload).ok()?;
    let m = v.as_obj()?;
    match m.get("contract")?.as_str()? {
        REFUSED if exact_keys(m, &["contract", "job_id", "code"]) => Some(LauncherFrame::Refused(
            m.get("job_id")?.as_str()?.to_string(),
            LaunchCode::parse(m.get("code")?.as_str()?)?,
        )),
        STARTED if exact_keys(m, &["contract", "job_id"]) => Some(LauncherFrame::Started(
            m.get("job_id")?.as_str()?.to_string(),
        )),
        RESULT => Finished::parse(payload).map(|f| LauncherFrame::Finished(Box::new(f))),
        _ => None,
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
pub(crate) mod tests;
