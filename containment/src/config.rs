//! Launcher configuration `/etc/writ-launcher/launcher.json` (`root:writ` 0640; J-1, J-11.2,
//! J-11.3) and the fixed J-1 paths. The file holds policy only (ids, maxima, bounds, deadlines,
//! the runtime-profile allow-list); every path is fixed by J-1 and never configurable by
//! `writ`. A configuration that violates the memory budget (L5), names uid 0, the overflow uid
//! or equal service and job uids, or has an unknown field, refuses to load.

use crate::protocol::{JobLimits, RequestBounds};
use std::path::PathBuf;
use std::time::Duration;
use writ_kernel::json::{self, s, Value};

/// Configuration contract.
pub const CONTRACT: &str = "writ-launcher-config/1";

/// The overflow id (peers in a namespace with unmapped ids).
pub const OVERFLOW_ID: u32 = 65534;

/// Fixed paths (J-1). Production uses [`Paths::reference`]; tests point them at scratch
/// directories.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Paths {
    /// The configuration file.
    pub config: PathBuf,
    /// Source root (`/var/lib/writ/run`, bound read-only into the launcher's view by PID 1).
    pub source_root: PathBuf,
    /// Jobs filesystem (`/var/lib/writ-jobs`).
    pub jobs_root: PathBuf,
    /// cgroup2 mount.
    pub cgroup_root: PathBuf,
    /// `writ-jobs.slice` relative to the cgroup mount (N6).
    pub slice: String,
    /// Private bubblewrap.
    pub bwrap: PathBuf,
    /// `writ-cgroup-exec`.
    pub exec: PathBuf,
    /// AppArmor profile file of J-11.5a.
    pub apparmor_profile: PathBuf,
    /// Root of the pinned runtime profiles.
    pub runtime_root: PathBuf,
    /// procfs.
    pub proc_root: PathBuf,
}

impl Paths {
    /// The J-1 reference layout.
    pub fn reference() -> Paths {
        Paths {
            config: "/etc/writ-launcher/launcher.json".into(),
            source_root: "/var/lib/writ/run".into(),
            jobs_root: "/var/lib/writ-jobs".into(),
            cgroup_root: "/sys/fs/cgroup".into(),
            slice: "writ.slice/writ-jobs.slice".into(),
            bwrap: "/usr/local/libexec/writ/bwrap".into(),
            exec: "/usr/local/libexec/writ/writ-cgroup-exec".into(),
            apparmor_profile: "/etc/apparmor.d/writ-bwrap".into(),
            runtime_root: "/usr/lib/writ-runtime".into(),
            proc_root: "/proc".into(),
        }
    }
}

/// One allow-listed runtime profile.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RuntimeProfile {
    /// Id (`[a-z0-9.-]`, at most 64 bytes); its root is `<runtime_root>/<id>`.
    pub id: String,
    /// Pinned manifest digest (recomputed before each launch, J-11.4 step 1).
    pub digest: String,
}

/// Deadlines from the launcher's monotonic clock (never from `writ`'s behaviour; M3).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Deadlines {
    /// Request after `hello`.
    pub request: Duration,
    /// From the helper spawn to `started`.
    pub start: Duration,
    /// Kill verification (`populated 0`), default 5 s.
    pub kill: Duration,
    /// Cleanup as `writ-job`.
    pub cleanup: Duration,
}

/// Source walk bounds (the byte bound is the request's `input_max_bytes`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct SourceBounds {
    /// Deepest directory nesting.
    pub max_depth: usize,
    /// Most entries.
    pub max_entries: usize,
}

/// Helper rlimits not taken from the request.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FixedRlimits {
    /// `RLIMIT_NPROC`.
    pub nproc: u64,
    /// `RLIMIT_MSGQUEUE`.
    pub msgqueue: u64,
    /// `RLIMIT_MEMLOCK`.
    pub memlock: u64,
}

/// The validated configuration.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Config {
    /// The `writ` service uid (the only admitted peer).
    pub writ_uid: u32,
    /// `writ-job` uid.
    pub job_uid: u32,
    /// `writ-job` gid.
    pub job_gid: u32,
    /// Root-configured maxima.
    pub maxima: JobLimits,
    /// Request bounds.
    pub request: RequestBounds,
    /// Source bounds.
    pub source: SourceBounds,
    /// Runtime profiles.
    pub runtime_profiles: Vec<RuntimeProfile>,
    /// Deadlines.
    pub deadlines: Deadlines,
    /// Bounded outbound buffer towards `writ` (M3).
    pub outbound_buffer_bytes: usize,
    /// Bytes of the status pipe drained after `started` (N4).
    pub status_drain_bytes: usize,
    /// Expected processes in `<unit>/job` before the gate (verify in VM, N5).
    pub pre_gate_processes: usize,
    /// Fixed rlimits.
    pub rlimits: FixedRlimits,
    /// The launcher's own memory bound.
    pub launcher_memory_bytes: u64,
    /// The unit's `MemoryMax=`.
    pub unit_memory_max_bytes: u64,
}

/// Why a configuration does not load.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ConfigError(pub String);

fn err<T>(t: &str) -> Result<T, ConfigError> {
    Err(ConfigError(t.to_string()))
}

fn exact(m: &json::Map, keys: &[&str], what: &str) -> Result<(), ConfigError> {
    if m.len() == keys.len() && keys.iter().all(|k| m.contains_key(*k)) {
        Ok(())
    } else {
        err(&format!("{what}: fields differ from {keys:?}"))
    }
}

fn pos(m: &json::Map, k: &str) -> Result<u64, ConfigError> {
    m.get(k)
        .and_then(Value::as_int)
        .and_then(|n| u64::try_from(n).ok())
        .filter(|n| *n > 0)
        .ok_or_else(|| ConfigError(format!("{k}: positive integer required")))
}

fn sub<'a>(m: &'a json::Map, k: &str, keys: &[&str]) -> Result<&'a json::Map, ConfigError> {
    let o = m
        .get(k)
        .and_then(Value::as_obj)
        .ok_or_else(|| ConfigError(format!("{k}: object required")))?;
    exact(o, keys, k)?;
    Ok(o)
}

fn usize_of(n: u64) -> Result<usize, ConfigError> {
    usize::try_from(n).map_err(|_| ConfigError("value too large".into()))
}

/// Runtime profile id rule.
pub fn valid_profile_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 64
        && !id.starts_with('.')
        && id
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-' || b == b'.')
}

impl Config {
    /// Parse and validate the configuration file bytes.
    pub fn parse(bytes: &[u8]) -> Result<Config, ConfigError> {
        let v = json::parse(bytes).map_err(|e| ConfigError(format!("json: {}", e.code())))?;
        let m = v
            .as_obj()
            .ok_or_else(|| ConfigError("object required".into()))?;
        exact(
            m,
            &[
                "contract",
                "writ_uid",
                "job_uid",
                "job_gid",
                "maxima",
                "request",
                "source",
                "runtime_profiles",
                "deadlines_ms",
                "outbound_buffer_bytes",
                "status_drain_bytes",
                "pre_gate_processes",
                "rlimits",
                "memory",
            ],
            "configuration",
        )?;
        if m.get("contract").and_then(Value::as_str) != Some(CONTRACT) {
            return err("contract");
        }
        let uid = |k: &str| -> Result<u32, ConfigError> {
            let n = pos(m, k)?;
            let n = u32::try_from(n).map_err(|_| ConfigError(format!("{k}: range")))?;
            if n == OVERFLOW_ID {
                return err(&format!("{k}: overflow id"));
            }
            Ok(n)
        };
        let (writ_uid, job_uid, job_gid) = (uid("writ_uid")?, uid("job_uid")?, uid("job_gid")?);
        if writ_uid == job_uid {
            return err("writ_uid equals job_uid");
        }
        let maxima = m
            .get("maxima")
            .and_then(JobLimits::from_value)
            .ok_or_else(|| ConfigError("maxima: the ten limits, positive".into()))?;
        if !maxima.kernel_valid() {
            return err("maxima: cpu.max or nofile outside kernel range");
        }
        let r = sub(
            m,
            "request",
            &["max_args", "max_arg_bytes", "max_env", "max_env_bytes"],
        )?;
        let request = RequestBounds {
            max_args: usize_of(pos(r, "max_args")?)?,
            max_arg_bytes: usize_of(pos(r, "max_arg_bytes")?)?,
            max_env: usize_of(pos(r, "max_env")?)?,
            max_env_bytes: usize_of(pos(r, "max_env_bytes")?)?,
        };
        let sb = sub(m, "source", &["max_depth", "max_entries"])?;
        let source = SourceBounds {
            max_depth: usize_of(pos(sb, "max_depth")?)?,
            max_entries: usize_of(pos(sb, "max_entries")?)?,
        };
        let mut runtime_profiles: Vec<RuntimeProfile> = Vec::new();
        for p in m
            .get("runtime_profiles")
            .and_then(Value::as_arr)
            .ok_or_else(|| ConfigError("runtime_profiles: array".into()))?
        {
            let pm = p
                .as_obj()
                .ok_or_else(|| ConfigError("runtime profile: object".into()))?;
            exact(pm, &["id", "digest"], "runtime profile")?;
            let id = pm.get("id").and_then(Value::as_str).unwrap_or_default();
            let digest = pm.get("digest").and_then(Value::as_str).unwrap_or_default();
            if !valid_profile_id(id) || !is_digest(digest) {
                return err("runtime profile: id or digest invalid");
            }
            if runtime_profiles.iter().any(|x| x.id == id) {
                return err("runtime profile: duplicate id");
            }
            runtime_profiles.push(RuntimeProfile {
                id: id.to_string(),
                digest: digest.to_string(),
            });
        }
        let d = sub(m, "deadlines_ms", &["request", "start", "kill", "cleanup"])?;
        let ms = |k: &str| pos(d, k).map(Duration::from_millis);
        let deadlines = Deadlines {
            request: ms("request")?,
            start: ms("start")?,
            kill: ms("kill")?,
            cleanup: ms("cleanup")?,
        };
        let rl = sub(m, "rlimits", &["nproc", "msgqueue", "memlock"])?;
        let rlimits = FixedRlimits {
            nproc: pos(rl, "nproc")?,
            msgqueue: pos(rl, "msgqueue")?,
            memlock: pos(rl, "memlock")?,
        };
        let mem = sub(m, "memory", &["launcher_bytes", "unit_max_bytes"])?;
        let launcher_memory_bytes = pos(mem, "launcher_bytes")?;
        let unit_memory_max_bytes = pos(mem, "unit_max_bytes")?;
        // Memory budget (L5): the launcher's own bound plus the largest permitted job memory
        // is at most the unit's MemoryMax.
        if launcher_memory_bytes.saturating_add(maxima.memory_max_bytes) > unit_memory_max_bytes {
            return err("memory budget: launcher_bytes + maxima.memory_max_bytes > unit_max_bytes");
        }
        let outbound_buffer_bytes = usize_of(pos(m, "outbound_buffer_bytes")?)?;
        if outbound_buffer_bytes < 2 * crate::frame::HEADER + 2 + 64 {
            return err("outbound_buffer_bytes too small");
        }
        Ok(Config {
            writ_uid,
            job_uid,
            job_gid,
            maxima,
            request,
            source,
            runtime_profiles,
            deadlines,
            outbound_buffer_bytes,
            status_drain_bytes: usize_of(pos(m, "status_drain_bytes")?)?,
            pre_gate_processes: usize_of(pos(m, "pre_gate_processes")?)?,
            rlimits,
            launcher_memory_bytes,
            unit_memory_max_bytes,
        })
    }

    /// D-006 digest of the maxima object (`maxima_digest`).
    pub fn maxima_digest(&self) -> String {
        json::digest(&json::obj([
            ("contract", s("writ-launcher-maxima/1")),
            ("maxima", self.maxima.to_value()),
        ]))
        .map(|d| d.as_str().to_string())
        .unwrap_or_default()
    }

    /// The allow-listed profile ids.
    pub fn profile_ids(&self) -> Vec<&str> {
        self.runtime_profiles
            .iter()
            .map(|p| p.id.as_str())
            .collect()
    }

    /// The profile `id`.
    pub fn profile(&self, id: &str) -> Option<&RuntimeProfile> {
        self.runtime_profiles.iter().find(|p| p.id == id)
    }
}

/// `sha256:<64 lowercase hex>`.
pub fn is_digest(t: &str) -> bool {
    t.strip_prefix("sha256:").is_some_and(|h| {
        h.len() == 64
            && h.bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    })
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
pub(crate) mod tests {
    use super::*;

    pub(crate) fn sample() -> Value {
        let lim = crate::protocol::tests::limits().to_value();
        json::obj([
            ("contract", s(CONTRACT)),
            ("writ_uid", Value::Int(990)),
            ("job_uid", Value::Int(991)),
            ("job_gid", Value::Int(991)),
            ("maxima", lim),
            (
                "request",
                json::obj([
                    ("max_args", Value::Int(16)),
                    ("max_arg_bytes", Value::Int(4096)),
                    ("max_env", Value::Int(16)),
                    ("max_env_bytes", Value::Int(4096)),
                ]),
            ),
            (
                "source",
                json::obj([
                    ("max_depth", Value::Int(8)),
                    ("max_entries", Value::Int(256)),
                ]),
            ),
            (
                "runtime_profiles",
                Value::Arr(vec![json::obj([
                    ("id", s("py312")),
                    ("digest", s(&format!("sha256:{}", "a".repeat(64)))),
                ])]),
            ),
            (
                "deadlines_ms",
                json::obj([
                    ("request", Value::Int(2000)),
                    ("start", Value::Int(5000)),
                    ("kill", Value::Int(5000)),
                    ("cleanup", Value::Int(5000)),
                ]),
            ),
            ("outbound_buffer_bytes", Value::Int(1 << 20)),
            ("status_drain_bytes", Value::Int(65536)),
            ("pre_gate_processes", Value::Int(2)),
            (
                "rlimits",
                json::obj([
                    ("nproc", Value::Int(256)),
                    ("msgqueue", Value::Int(0x1000)),
                    ("memlock", Value::Int(0x10000)),
                ]),
            ),
            (
                "memory",
                json::obj([
                    ("launcher_bytes", Value::Int(64 << 20)),
                    ("unit_max_bytes", Value::Int(512 << 20)),
                ]),
            ),
        ])
    }

    fn with(edit: impl FnOnce(&mut json::Map)) -> Result<Config, ConfigError> {
        let Value::Obj(mut m) = sample() else {
            panic!()
        };
        edit(&mut m);
        Config::parse(json::canonical(&Value::Obj(m)).unwrap().as_bytes())
    }

    #[test]
    fn sample_loads() {
        let c = with(|_| {}).unwrap();
        assert_eq!(c.writ_uid, 990);
        assert_eq!(c.profile_ids(), vec!["py312"]);
        assert!(is_digest(&c.maxima_digest()));
    }

    #[test]
    fn memory_budget_violation_refuses_to_load() {
        let e = with(|m| {
            m.insert(
                "memory".into(),
                json::obj([
                    ("launcher_bytes", Value::Int(64 << 20)),
                    ("unit_max_bytes", Value::Int(300 << 20)),
                ]),
            );
        });
        assert!(e.unwrap_err().0.contains("memory budget"));
    }

    #[test]
    fn identities_and_fields_are_checked() {
        assert!(with(|m| {
            m.insert("job_uid".into(), Value::Int(990));
        })
        .is_err());
        assert!(with(|m| {
            m.insert("writ_uid".into(), Value::Int(0));
        })
        .is_err());
        assert!(with(|m| {
            m.insert("job_uid".into(), Value::Int(65534));
        })
        .is_err());
        assert!(with(|m| {
            m.insert("extra".into(), Value::Int(1));
        })
        .is_err());
        assert!(with(|m| {
            m.insert(
                "runtime_profiles".into(),
                Value::Arr(vec![json::obj([
                    ("id", s("../x")),
                    ("digest", s("sha256:00")),
                ])]),
            );
        })
        .is_err());
    }
}
