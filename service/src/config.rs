//! `writ-service-config/1`: the service configuration (J-1, J-2, J-6, J-11.8). One strict
//! JSON object (kernel parser), read from a file given on the command line; no environment
//! variable, no default file and no secret (keys live in the key directory, J-3).
//!
//! Load-time rules (any violation refuses the whole configuration):
//! - identities: the service uid and gid are not 0; the agent, job and desktop uids are
//!   pairwise distinct, none is 0, 65534 or the service uid (J-1 static identity check);
//! - `host_bindings`: each uid is a configured agent uid and appears once; each binding is
//!   exactly one (tenant, project) with the principal configuration and the adapter's ingress
//!   key id (J-2 scope binding, J-6);
//! - `admin_uids` (default `[0]`): never an agent, job or desktop uid (J-11.8);
//! - connection caps and deadlines are positive and bounded.

use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};

use writ_broker::keys::{KeyId, KeyPurpose};
use writ_broker::writ_kernel::json::{self, Map, Value};
use writ_hostsys::deployment::{DeploymentConfig, FactPlan, IdentityPlan};
use writ_rpc::admission::OVERFLOW_ID;

/// The configuration contract.
pub const CONFIG_CONTRACT: &str = "writ-service-config/1";
/// Largest configuration file in bytes.
pub const MAX_CONFIG_BYTES: u64 = 256 * 1024;
/// Default deployment recheck interval in seconds (M5a-13 M5; `deployment.recheck_seconds`).
pub const DEFAULT_RECHECK_SECONDS: u64 = 300;
/// Largest configurable deployment recheck interval in seconds.
pub const MAX_RECHECK_SECONDS: i64 = 3600;
/// Longest tenant, project, principal, role or adapter id in bytes.
pub const MAX_ID_BYTES: usize = 128;

/// One `host.sock` binding: the only scope and principal a connection from `uid` has.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct HostBinding {
    /// The agent uid admitted on `host.sock`.
    pub uid: u32,
    /// Tenant of every request of the connection.
    pub tenant_id: String,
    /// Project of every request of the connection.
    pub project_id: String,
    /// `principal.id` of every sealed request (J-6: from configuration only).
    pub principal_id: String,
    /// `principal.roles`.
    pub roles: Vec<String>,
    /// The sealing adapter id (`principal.verified_by`).
    pub adapter_id: String,
    /// The ingress key the adapter seals with (`seal/2`).
    pub ingress_key_id: KeyId,
}

/// Connection caps and deadlines (J-2).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Limits {
    /// Open connections in all.
    pub max_connections: usize,
    /// Open connections per uid.
    pub max_connections_per_uid: usize,
    /// Idle deadline between frames, seconds.
    pub idle_seconds: u64,
    /// Deadline for one request's frames and its answer, seconds.
    pub request_seconds: u64,
}

impl Limits {
    /// Defaults: 64 connections, 8 per uid, 30 s idle, 10 s per request.
    pub const DEFAULT: Limits = Limits {
        max_connections: 64,
        max_connections_per_uid: 8,
        idle_seconds: 30,
        request_seconds: 10,
    };
}

/// The start-time sweep of leftover job state (J-11.6 "`writ` crashes or stops"; M5a-11).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SweepConfig {
    /// Where the runner materializes `run-<id>` (`/var/lib/writ/run`).
    pub run_root: PathBuf,
    /// `cgroup.events` of `writ-jobs.slice` (read only; J-11.5).
    pub jobs_cgroup_events: PathBuf,
    /// How long the sweep waits for `populated 0` (default 10 s, at most 60 s).
    pub wait: std::time::Duration,
}

/// The approval listener (loopback; WebAuthn authenticates the human).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ApprovalConfig {
    /// Loopback port (the origin is `http://localhost:<port>`).
    pub port: u16,
    /// Channel identity recorded on decisions.
    pub channel: String,
    /// Issuer of page subjects.
    pub subject_issuer: String,
}

/// The validated service configuration.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ServiceConfig {
    /// The `writ` uid.
    pub service_uid: u32,
    /// The `writ` gid.
    pub service_gid: u32,
    /// Configured agent uids.
    pub agent_uids: Vec<u32>,
    /// Configured desktop (human) uids; empty means advisory only (J-1).
    pub desktop_uids: Vec<u32>,
    /// The job uid (`writ-job`).
    pub job_uid: u32,
    /// The ledger file (must exist; [`crate::Service::initialize`] creates it).
    pub ledger: PathBuf,
    /// The snapshot store file.
    pub snapshots: PathBuf,
    /// The key directory.
    pub key_dir: PathBuf,
    /// The issuing margin in seconds (J-3).
    pub key_margin_seconds: i64,
    /// `host.sock` path.
    pub host_socket: PathBuf,
    /// `admin.sock` path.
    pub admin_socket: PathBuf,
    /// `host.sock` bindings.
    pub host_bindings: Vec<HostBinding>,
    /// uids admitted on `admin.sock`.
    pub admin_uids: Vec<u32>,
    /// Caps and deadlines.
    pub limits: Limits,
    /// The approval listener, if served by this process.
    pub approval: Option<ApprovalConfig>,
    /// The operator report and fact plan for `VerifiedDeployment` (J-9); `None`: no proof.
    pub deployment: Option<DeploymentConfig>,
    /// `sockets.activation` = `"systemd"`: every socket is received from systemd (J-8) and
    /// none is ever bound by the service; `"bind"` (default, development): the service binds
    /// its Unix sockets and the approval port itself.
    pub socket_activation: bool,
    /// `sockets.host_gid`: the group (`writ-agents`) the received `host.sock` must have
    /// (M5a-13 L10); required with `activation = "systemd"`.
    pub host_socket_gid: Option<u32>,
    /// The drain period of the graceful shutdown (J-8; `shutdown.drain_seconds`, default
    /// 10, at most 30).
    pub drain: std::time::Duration,
    /// The start-time sweep (`containment`), when configured.
    pub sweep: Option<SweepConfig>,
}

/// Why a configuration was refused.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ConfigError(pub String);

impl std::fmt::Display for ConfigError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "configuration refused: {}", self.0)
    }
}

fn err<T>(what: &str) -> Result<T, ConfigError> {
    Err(ConfigError(what.to_string()))
}

/// The members of `v` when it is an object with exactly `required` plus any of `optional`.
fn members<'a>(
    v: &'a Value,
    required: &[&str],
    optional: &[&str],
    what: &str,
) -> Result<&'a Map, ConfigError> {
    let Some(m) = v.as_obj() else {
        return err(&format!("{what}: not an object"));
    };
    if let Some(k) = m
        .keys()
        .find(|k| !required.contains(&k.as_str()) && !optional.contains(&k.as_str()))
    {
        return err(&format!("{what}: unknown member {k:?}"));
    }
    if let Some(k) = required.iter().find(|k| !m.contains_key(**k)) {
        return err(&format!("{what}: missing member {k:?}"));
    }
    Ok(m)
}

fn uid(v: Option<&Value>, what: &str) -> Result<u32, ConfigError> {
    match v {
        Some(Value::Int(n)) => u32::try_from(*n).or_else(|_| err(what)),
        _ => err(what),
    }
}

fn uids(v: Option<&Value>, what: &str) -> Result<Vec<u32>, ConfigError> {
    match v {
        Some(Value::Arr(items)) => items.iter().map(|i| uid(Some(i), what)).collect(),
        _ => err(what),
    }
}

fn id_text(v: Option<&Value>, what: &str) -> Result<String, ConfigError> {
    match v.and_then(Value::as_str) {
        Some(t)
            if !t.is_empty()
                && t.len() <= MAX_ID_BYTES
                && t.bytes().all(|b| b.is_ascii_graphic()) =>
        {
            Ok(t.to_string())
        }
        _ => err(what),
    }
}

fn abs_path(v: Option<&Value>, what: &str) -> Result<PathBuf, ConfigError> {
    match v.and_then(Value::as_str) {
        Some(t) if t.starts_with('/') && !t.contains('\0') && !t.split('/').any(|c| c == "..") => {
            Ok(PathBuf::from(t))
        }
        _ => err(what),
    }
}

fn positive(v: Option<&Value>, max: i64, what: &str) -> Result<i64, ConfigError> {
    match v {
        Some(Value::Int(n)) if *n > 0 && *n <= max => Ok(*n),
        _ => err(what),
    }
}

fn strings(v: Option<&Value>, what: &str) -> Result<Vec<String>, ConfigError> {
    match v {
        Some(Value::Arr(items)) => items.iter().map(|i| id_text(Some(i), what)).collect(),
        _ => err(what),
    }
}

fn binding(v: &Value) -> Result<HostBinding, ConfigError> {
    let m = members(
        v,
        &[
            "uid",
            "tenant_id",
            "project_id",
            "principal_id",
            "roles",
            "adapter_id",
            "ingress_key_id",
        ],
        &[],
        "host_bindings[]",
    )?;
    let ingress_key_id = m
        .get("ingress_key_id")
        .and_then(Value::as_str)
        .and_then(KeyId::parse)
        .filter(|k| k.purpose() == KeyPurpose::Ingress);
    let Some(ingress_key_id) = ingress_key_id else {
        return err("host_bindings[].ingress_key_id: not an ingress key id");
    };
    Ok(HostBinding {
        uid: uid(m.get("uid"), "host_bindings[].uid")?,
        tenant_id: id_text(m.get("tenant_id"), "host_bindings[].tenant_id")?,
        project_id: id_text(m.get("project_id"), "host_bindings[].project_id")?,
        principal_id: id_text(m.get("principal_id"), "host_bindings[].principal_id")?,
        roles: strings(m.get("roles"), "host_bindings[].roles")?,
        adapter_id: id_text(m.get("adapter_id"), "host_bindings[].adapter_id")?,
        ingress_key_id,
    })
}

fn plan(v: &Value) -> Result<FactPlan, ConfigError> {
    let m = members(
        v,
        &[],
        &["paths", "sysctls", "binaries", "groups", "own_identity"],
        "deployment.plan",
    )?;
    let paths = |k: &str| -> Result<Vec<PathBuf>, ConfigError> {
        match m.get(k) {
            None => Ok(vec![]),
            Some(Value::Arr(items)) => items
                .iter()
                .map(|i| abs_path(Some(i), "deployment.plan path"))
                .collect(),
            Some(_) => err("deployment.plan paths"),
        }
    };
    let names = |k: &str| -> Result<Vec<String>, ConfigError> {
        match m.get(k) {
            None => Ok(vec![]),
            some => strings(some, "deployment.plan names"),
        }
    };
    Ok(FactPlan {
        paths: paths("paths")?,
        sysctls: names("sysctls")?,
        binaries: paths("binaries")?,
        groups: names("groups")?,
        own_identity: matches!(m.get("own_identity"), Some(Value::Bool(true))),
        group_file: None,
        identity: None,
        required_paths: Vec::new(),
    })
}

impl ServiceConfig {
    /// Parse and validate a configuration document.
    pub fn parse(bytes: &[u8]) -> Result<ServiceConfig, ConfigError> {
        let v = json::parse(bytes).or_else(|_| err("not strict JSON"))?;
        let m = members(
            &v,
            &[
                "contract",
                "service",
                "identities",
                "ledger",
                "snapshots",
                "keys",
                "sockets",
                "host_bindings",
            ],
            &[
                "admin_uids",
                "limits",
                "approval",
                "deployment",
                "shutdown",
                "containment",
            ],
            "configuration",
        )?;
        if m.get("contract").and_then(Value::as_str) != Some(CONFIG_CONTRACT) {
            return err("contract");
        }
        let service = members(
            m.get("service").unwrap_or(&Value::Null),
            &["uid", "gid"],
            &[],
            "service",
        )?;
        let service_uid = uid(service.get("uid"), "service.uid")?;
        let service_gid = uid(service.get("gid"), "service.gid")?;
        let ids = members(
            m.get("identities").unwrap_or(&Value::Null),
            &["agent_uids", "desktop_uids", "job_uid"],
            &[],
            "identities",
        )?;
        let agent_uids = uids(ids.get("agent_uids"), "identities.agent_uids")?;
        let desktop_uids = uids(ids.get("desktop_uids"), "identities.desktop_uids")?;
        let job_uid = uid(ids.get("job_uid"), "identities.job_uid")?;
        let keys = members(
            m.get("keys").unwrap_or(&Value::Null),
            &["dir"],
            &["margin_seconds"],
            "keys",
        )?;
        let sockets = members(
            m.get("sockets").unwrap_or(&Value::Null),
            &["host", "admin"],
            &["activation", "host_gid"],
            "sockets",
        )?;
        let host_socket_gid = match sockets.get("host_gid") {
            None => None,
            some => Some(uid(some, "sockets.host_gid")?),
        };
        let socket_activation = match sockets.get("activation") {
            None => false,
            Some(Value::Str(t)) if t == "bind" => false,
            Some(Value::Str(t)) if t == "systemd" => true,
            Some(_) => return err("sockets.activation: \"systemd\" or \"bind\""),
        };
        let drain = match m.get("shutdown") {
            None => std::time::Duration::from_secs(10),
            Some(v) => {
                let d = members(v, &["drain_seconds"], &[], "shutdown")?;
                std::time::Duration::from_secs(positive(
                    d.get("drain_seconds"),
                    30,
                    "shutdown.drain_seconds",
                )? as u64)
            }
        };
        let sweep = match m.get("containment") {
            None => None,
            Some(v) => {
                let c = members(
                    v,
                    &["run_root", "jobs_cgroup_events"],
                    &["sweep_wait_seconds"],
                    "containment",
                )?;
                Some(SweepConfig {
                    run_root: abs_path(c.get("run_root"), "containment.run_root")?,
                    jobs_cgroup_events: abs_path(
                        c.get("jobs_cgroup_events"),
                        "containment.jobs_cgroup_events",
                    )?,
                    wait: std::time::Duration::from_secs(match c.get("sweep_wait_seconds") {
                        None => 10,
                        some => positive(some, 60, "containment.sweep_wait_seconds")? as u64,
                    }),
                })
            }
        };
        let host_bindings = match m.get("host_bindings") {
            Some(Value::Arr(items)) if !items.is_empty() => {
                items.iter().map(binding).collect::<Result<Vec<_>, _>>()?
            }
            _ => return err("host_bindings: a non-empty array"),
        };
        let admin_uids = match m.get("admin_uids") {
            None => vec![0],
            some => uids(some, "admin_uids")?,
        };
        let limits = match m.get("limits") {
            None => Limits::DEFAULT,
            Some(l) => {
                let l = members(
                    l,
                    &[
                        "max_connections",
                        "max_connections_per_uid",
                        "idle_seconds",
                        "request_seconds",
                    ],
                    &[],
                    "limits",
                )?;
                Limits {
                    max_connections: positive(
                        l.get("max_connections"),
                        4096,
                        "limits.max_connections",
                    )? as usize,
                    max_connections_per_uid: positive(
                        l.get("max_connections_per_uid"),
                        4096,
                        "limits.max_connections_per_uid",
                    )? as usize,
                    idle_seconds: positive(l.get("idle_seconds"), 3600, "limits.idle_seconds")?
                        as u64,
                    request_seconds: positive(
                        l.get("request_seconds"),
                        600,
                        "limits.request_seconds",
                    )? as u64,
                }
            }
        };
        let approval = match m.get("approval") {
            None => None,
            Some(a) => {
                let a = members(a, &["port", "channel", "subject_issuer"], &[], "approval")?;
                Some(ApprovalConfig {
                    port: u16::try_from(positive(a.get("port"), 65535, "approval.port")?)
                        .or_else(|_| err("approval.port"))?,
                    channel: id_text(a.get("channel"), "approval.channel")?,
                    subject_issuer: id_text(a.get("subject_issuer"), "approval.subject_issuer")?,
                })
            }
        };
        let deployment = match m.get("deployment") {
            None => None,
            Some(d) => {
                let d = members(
                    d,
                    &["report", "report_digest", "plan"],
                    &["recheck_seconds"],
                    "deployment",
                )?;
                let digest = match d.get("report_digest").and_then(Value::as_str) {
                    Some(t)
                        if t.len() == 71
                            && t.starts_with("sha256:")
                            && t[7..]
                                .bytes()
                                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)) =>
                    {
                        t.to_string()
                    }
                    _ => return err("deployment.report_digest"),
                };
                Some(DeploymentConfig {
                    report: abs_path(d.get("report"), "deployment.report")?,
                    report_digest: digest,
                    plan: plan(d.get("plan").unwrap_or(&Value::Null))?,
                    recheck_interval: std::time::Duration::from_secs(
                        match d.get("recheck_seconds") {
                            None => DEFAULT_RECHECK_SECONDS,
                            some => {
                                positive(some, MAX_RECHECK_SECONDS, "deployment.recheck_seconds")?
                                    as u64
                            }
                        },
                    ),
                })
            }
        };
        let mut config = ServiceConfig {
            service_uid,
            service_gid,
            agent_uids,
            desktop_uids,
            job_uid,
            ledger: abs_path(m.get("ledger"), "ledger")?,
            snapshots: abs_path(m.get("snapshots"), "snapshots")?,
            key_dir: abs_path(keys.get("dir"), "keys.dir")?,
            key_margin_seconds: match keys.get("margin_seconds") {
                None => writ_keys::DEFAULT_MARGIN_SECONDS,
                some => positive(some, 3600, "keys.margin_seconds")?,
            },
            host_socket: abs_path(sockets.get("host"), "sockets.host")?,
            admin_socket: abs_path(sockets.get("admin"), "sockets.admin")?,
            host_bindings,
            admin_uids,
            limits,
            approval,
            deployment,
            socket_activation,
            host_socket_gid,
            drain,
            sweep,
        };
        config.validate()?;
        config.complete_plan()?;
        Ok(config)
    }

    /// M5a-13 M4: the deployment plan carries the J-1 identity rules from `identities` and
    /// requires the layout paths this configuration names (key directory, ledger and store
    /// directories, socket directories); a plan that is not the full static fact set
    /// ([`FactPlan::missing`]) refuses the configuration, so `"plan":{}` never loads.
    fn complete_plan(&mut self) -> Result<(), ConfigError> {
        let Some(d) = &mut self.deployment else {
            return Ok(());
        };
        d.plan.identity = Some(IdentityPlan {
            service_uid: self.service_uid,
            service_gid: self.service_gid,
            agent_uids: self.agent_uids.clone(),
            job_uid: self.job_uid,
            desktop_uids: self.desktop_uids.clone(),
            passwd_file: None,
        });
        let mut required = vec![self.key_dir.clone()];
        for p in [
            &self.ledger,
            &self.snapshots,
            &self.host_socket,
            &self.admin_socket,
        ] {
            if let Some(parent) = p.parent() {
                if !required.iter().any(|r| r == parent) {
                    required.push(parent.to_path_buf());
                }
            }
        }
        d.plan.required_paths = required;
        let missing = d.plan.missing();
        if missing.is_empty() {
            Ok(())
        } else {
            Err(ConfigError(format!(
                "deployment.plan is not the full static fact set; missing {}",
                missing.join(", ")
            )))
        }
    }

    /// Read and parse the configuration file at `path` (bounded).
    pub fn load(path: &Path) -> Result<ServiceConfig, ConfigError> {
        let file = fs::File::open(path).or_else(|_| err("unreadable"))?;
        let mut bytes = Vec::new();
        file.take(MAX_CONFIG_BYTES + 1)
            .read_to_end(&mut bytes)
            .or_else(|_| err("unreadable"))?;
        if bytes.len() as u64 > MAX_CONFIG_BYTES {
            return err("too large");
        }
        ServiceConfig::parse(&bytes)
    }

    fn validate(&self) -> Result<(), ConfigError> {
        if self.service_uid == 0 || self.service_gid == 0 {
            return err("service uid or gid 0");
        }
        if self.agent_uids.is_empty() {
            return err("no agent uid");
        }
        let mut identities: Vec<u32> = self.agent_uids.clone();
        identities.extend(&self.desktop_uids);
        identities.push(self.job_uid);
        for u in &identities {
            if *u == 0 || *u == OVERFLOW_ID || *u == self.service_uid {
                return err("agent, job or desktop uid is 0, 65534 or the service uid");
            }
        }
        // Distinct within each list, and the job uid distinct from every other identity. An
        // agent uid equal to a desktop uid is accepted but labelled advisory-shared-user.
        for list in [&self.agent_uids, &self.desktop_uids] {
            for (i, u) in list.iter().enumerate() {
                if list[..i].contains(u) || *u == self.job_uid {
                    return err("agent, job and desktop uids are not distinct");
                }
            }
        }
        let mut seen = Vec::new();
        for b in &self.host_bindings {
            if !self.agent_uids.contains(&b.uid) {
                return err("host binding for a uid that is not an agent uid");
            }
            if seen.contains(&b.uid) {
                return err("two host bindings for one uid");
            }
            seen.push(b.uid);
        }
        if self.admin_uids.is_empty() {
            return err("admin_uids empty");
        }
        for u in &self.admin_uids {
            if identities.contains(u) || *u == OVERFLOW_ID {
                return err("admin uid is an agent, job or desktop uid");
            }
        }
        if self.limits.max_connections_per_uid > self.limits.max_connections {
            return err("per-uid cap above the global cap");
        }
        if self.host_socket == self.admin_socket {
            return err("host and admin socket paths equal");
        }
        match self.host_socket_gid {
            None if self.socket_activation => {
                return err("sockets.host_gid is required with socket activation")
            }
            // That it is `writ-agents` (not `writ`) is the static identity check's (J-1).
            Some(g) if g == 0 || g == OVERFLOW_ID => return err("sockets.host_gid is 0 or 65534"),
            _ => {}
        }
        Ok(())
    }

    /// The installation label (J-1): `governed` only with a desktop uid configured, no agent
    /// uid equal to a desktop uid, socket activation (M5a-13 L10: the development `bind` mode,
    /// where the service creates its own sockets, never carries the governed label), and a
    /// verified deployment (which includes a passed P-AGENT-DESKTOP in the operator report);
    /// otherwise `advisory-shared-user` (no desktop uid, or an agent uid that is a desktop
    /// uid) or `unverified`. Governed jobs and writes refuse unless `governed` (M5-D2).
    pub fn label(&self, deployment_verified: bool) -> &'static str {
        if self.desktop_uids.is_empty()
            || self
                .agent_uids
                .iter()
                .any(|a| self.desktop_uids.contains(a))
        {
            "advisory-shared-user"
        } else if deployment_verified && self.socket_activation {
            "governed"
        } else {
            "unverified"
        }
    }

    /// The binding of `uid` on `host.sock`.
    pub fn binding(&self, uid: u32) -> Option<&HostBinding> {
        self.host_bindings.iter().find(|b| b.uid == uid)
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;

    pub(crate) fn doc(over: &[(&str, &str)]) -> String {
        let mut parts = vec![
            ("contract", r#""writ-service-config/1""#.to_string()),
            ("service", r#"{"uid":990,"gid":990}"#.to_string()),
            ("identities", r#"{"agent_uids":[1001],"desktop_uids":[1000],"job_uid":992}"#.to_string()),
            ("ledger", r#""/var/lib/writ/ledger/ledger.db""#.to_string()),
            ("snapshots", r#""/var/lib/writ/stores/snapshots.db""#.to_string()),
            ("keys", r#"{"dir":"/var/lib/writ/keys"}"#.to_string()),
            ("sockets", r#"{"host":"/run/writ/host.sock","admin":"/run/writ/admin.sock"}"#.to_string()),
            (
                "host_bindings",
                r#"[{"uid":1001,"tenant_id":"acme","project_id":"web","principal_id":"agent:writ-agent","roles":["agent"],"adapter_id":"writ-host-adapter:cli","ingress_key_id":"ingress/k1"}]"#
                    .to_string(),
            ),
        ];
        for (k, v) in over {
            match parts.iter_mut().find(|(n, _)| n == k) {
                Some(p) => p.1 = v.to_string(),
                None => parts.push((k, v.to_string())),
            }
        }
        let body: Vec<String> = parts
            .into_iter()
            .filter(|(_, v)| !v.is_empty())
            .map(|(k, v)| format!("\"{k}\":{v}"))
            .collect();
        format!("{{{}}}", body.join(","))
    }

    fn parse(over: &[(&str, &str)]) -> Result<ServiceConfig, ConfigError> {
        ServiceConfig::parse(doc(over).as_bytes())
    }

    #[test]
    fn a_complete_configuration_parses_with_defaults() {
        let c = parse(&[]).unwrap();
        assert_eq!(c.admin_uids, vec![0]);
        assert_eq!(c.limits, Limits::DEFAULT);
        assert_eq!(c.binding(1001).unwrap().tenant_id, "acme");
        assert!(c.binding(1002).is_none());
        assert_eq!(c.label(false), "unverified");
        // M5a-13 L10: bind mode (the default) never carries the governed label.
        assert_eq!(c.label(true), "unverified");
        let c = parse(&[(
            "sockets",
            r#"{"host":"/run/writ/host.sock","admin":"/run/writ/admin.sock","activation":"systemd","host_gid":991}"#,
        )])
        .unwrap();
        assert_eq!(c.host_socket_gid, Some(991));
        assert_eq!(c.label(false), "unverified");
        assert_eq!(c.label(true), "governed");
    }

    /// M5a-13 M4: a deployment plan that is not the full static fact set never loads;
    /// the full plan receives the identity rules and the layout paths from the
    /// configuration. M5: `recheck_seconds` defaults to 300 and is bounded.
    #[test]
    fn the_deployment_plan_must_be_the_full_static_fact_set() {
        let digest = format!("sha256:{}", "a".repeat(64));
        let sysctls: Vec<String> = writ_hostsys::deployment::REQUIRED_SYSCTLS
            .iter()
            .map(|s| format!("\"{s}\""))
            .collect();
        let full_plan = format!(
            r#"{{"paths":["/var/lib/writ/keys","/var/lib/writ/ledger","/var/lib/writ/stores","/run/writ"],"sysctls":[{}],"binaries":["/usr/local/libexec/writ/writ-cgroup-exec"],"groups":["writ","writ-agents","writ-job"],"own_identity":true}}"#,
            sysctls.join(",")
        );
        let deployment = |plan: &str, extra: &str| {
            format!(
                r#"{{"report":"/etc/writ/readiness.json","report_digest":"{digest}","plan":{plan}{extra}}}"#
            )
        };
        let refused = parse(&[("deployment", &deployment("{}", ""))]).unwrap_err();
        assert!(
            refused.0.contains("not the full static fact set"),
            "{refused}"
        );
        for drop in [
            r#""/run/writ""#,
            r#""writ-job""#,
            r#""own_identity":true"#,
            r#""/usr/local/libexec/writ/writ-cgroup-exec""#,
            r#""fs.suid_dumpable""#,
        ] {
            let partial = full_plan
                .replace(&format!("{drop},"), "")
                .replace(&format!(",{drop}"), "")
                .replace(drop, "");
            assert_ne!(partial, full_plan, "{drop}");
            assert!(
                parse(&[("deployment", &deployment(&partial, ""))]).is_err(),
                "{drop}"
            );
        }
        let c = parse(&[("deployment", &deployment(&full_plan, ""))]).unwrap();
        let d = c.deployment.unwrap();
        assert_eq!(d.recheck_interval, std::time::Duration::from_secs(300));
        let identity = d.plan.identity.unwrap();
        assert_eq!(
            (
                identity.service_uid,
                identity.agent_uids,
                identity.job_uid,
                identity.desktop_uids
            ),
            (990, vec![1001], 992, vec![1000])
        );
        assert_eq!(d.plan.required_paths.len(), 4);
        let c = parse(&[(
            "deployment",
            &deployment(&full_plan, r#","recheck_seconds":60"#),
        )])
        .unwrap();
        assert_eq!(
            c.deployment.unwrap().recheck_interval,
            std::time::Duration::from_secs(60)
        );
        for bad in [r#","recheck_seconds":0"#, r#","recheck_seconds":3601"#] {
            assert!(parse(&[("deployment", &deployment(&full_plan, bad))]).is_err());
        }
    }

    #[test]
    fn identity_rules_are_enforced_at_load() {
        for ids in [
            r#"{"agent_uids":[1001,1001],"desktop_uids":[1000],"job_uid":992}"#,
            r#"{"agent_uids":[1001],"desktop_uids":[1000],"job_uid":1001}"#,
            r#"{"agent_uids":[990],"desktop_uids":[1000],"job_uid":992}"#,
            r#"{"agent_uids":[0],"desktop_uids":[1000],"job_uid":992}"#,
            r#"{"agent_uids":[65534],"desktop_uids":[1000],"job_uid":992}"#,
            r#"{"agent_uids":[],"desktop_uids":[1000],"job_uid":992}"#,
        ] {
            assert!(parse(&[("identities", ids)]).is_err(), "{ids}");
        }
        assert!(parse(&[("service", r#"{"uid":0,"gid":990}"#)]).is_err());
        // Agent, job or desktop uid on admin.sock (J-11.8).
        for admin in ["[1001]", "[992]", "[1000]", "[]", "[65534]"] {
            assert!(parse(&[("admin_uids", admin)]).is_err(), "{admin}");
        }
        assert!(parse(&[("admin_uids", "[0,1500]")]).is_ok());
        // Without desktop uids: advisory only.
        let c = parse(&[(
            "identities",
            r#"{"agent_uids":[1001],"desktop_uids":[],"job_uid":992}"#,
        )])
        .unwrap();
        assert_eq!(c.label(true), "advisory-shared-user");
        // An agent uid that is also the desktop uid: accepted, advisory only.
        let c = parse(&[
            ("identities", r#"{"agent_uids":[1000],"desktop_uids":[1000],"job_uid":992}"#),
            (
                "host_bindings",
                r#"[{"uid":1000,"tenant_id":"acme","project_id":"web","principal_id":"agent:a","roles":["agent"],"adapter_id":"ad","ingress_key_id":"ingress/k1"}]"#,
            ),
        ])
        .unwrap();
        assert_eq!(c.label(true), "advisory-shared-user");
    }

    #[test]
    fn host_bindings_are_one_per_agent_uid_with_an_ingress_key() {
        let b = |uid: u32, key: &str| {
            format!(
                r#"{{"uid":{uid},"tenant_id":"acme","project_id":"web","principal_id":"agent:a","roles":["agent"],"adapter_id":"ad","ingress_key_id":"{key}"}}"#
            )
        };
        let two = format!("[{},{}]", b(1001, "ingress/k1"), b(1001, "ingress/k2"));
        assert!(
            parse(&[("host_bindings", &two)]).is_err(),
            "two bindings for one uid"
        );
        let other = format!("[{}]", b(1002, "ingress/k1"));
        assert!(
            parse(&[("host_bindings", &other)]).is_err(),
            "not an agent uid"
        );
        let permit = format!("[{}]", b(1001, "permit/k1"));
        assert!(
            parse(&[("host_bindings", &permit)]).is_err(),
            "not an ingress key"
        );
        assert!(parse(&[("host_bindings", "[]")]).is_err());
        let extra = r#"[{"uid":1001,"tenant_id":"acme","project_id":"web","principal_id":"agent:a","roles":["agent"],"adapter_id":"ad","ingress_key_id":"ingress/k1","project_ids":["x"]}]"#;
        assert!(
            parse(&[("host_bindings", extra)]).is_err(),
            "unknown member"
        );
    }

    #[test]
    fn activation_shutdown_and_sweep_settings() {
        let c = parse(&[]).unwrap();
        assert!(!c.socket_activation);
        assert_eq!(c.drain, std::time::Duration::from_secs(10));
        assert!(c.sweep.is_none());
        let c = parse(&[
            (
                "sockets",
                r#"{"host":"/run/writ/host.sock","admin":"/run/writ/admin.sock","activation":"systemd","host_gid":991}"#,
            ),
            ("shutdown", r#"{"drain_seconds":3}"#),
            (
                "containment",
                r#"{"run_root":"/var/lib/writ/run","jobs_cgroup_events":"/sys/fs/cgroup/writ.slice/writ-jobs.slice/cgroup.events","sweep_wait_seconds":2}"#,
            ),
        ])
        .unwrap();
        assert!(c.socket_activation);
        assert_eq!(c.drain, std::time::Duration::from_secs(3));
        let sweep = c.sweep.unwrap();
        assert_eq!(sweep.run_root, PathBuf::from("/var/lib/writ/run"));
        assert_eq!(sweep.wait, std::time::Duration::from_secs(2));
        for (k, v) in [
            (
                "sockets",
                r#"{"host":"/run/writ/host.sock","admin":"/run/writ/admin.sock","activation":"inetd"}"#,
            ),
            // M5a-13 L10: activation without the host.sock group, or a bad group.
            (
                "sockets",
                r#"{"host":"/run/writ/host.sock","admin":"/run/writ/admin.sock","activation":"systemd"}"#,
            ),
            (
                "sockets",
                r#"{"host":"/run/writ/host.sock","admin":"/run/writ/admin.sock","activation":"systemd","host_gid":0}"#,
            ),
            (
                "sockets",
                r#"{"host":"/run/writ/host.sock","admin":"/run/writ/admin.sock","activation":"systemd","host_gid":65534}"#,
            ),
            ("shutdown", r#"{"drain_seconds":0}"#),
            ("shutdown", r#"{"drain_seconds":31}"#),
            ("shutdown", r#"{"drain":3}"#),
            ("containment", r#"{"run_root":"/var/lib/writ/run"}"#),
            (
                "containment",
                r#"{"run_root":"run","jobs_cgroup_events":"/sys/fs/cgroup/x/cgroup.events"}"#,
            ),
            (
                "containment",
                r#"{"run_root":"/var/lib/writ/run","jobs_cgroup_events":"/sys/fs/cgroup/x/cgroup.events","sweep_wait_seconds":61}"#,
            ),
        ] {
            assert!(parse(&[(k, v)]).is_err(), "{k} {v}");
        }
    }

    #[test]
    fn documents_are_strict() {
        assert!(ServiceConfig::parse(
            b"{\"contract\":\"writ-service-config/1\",\"contract\":\"x\"}"
        )
        .is_err());
        assert!(parse(&[("unknown", "1")]).is_err());
        assert!(parse(&[("ledger", r#""relative/ledger.db""#)]).is_err());
        assert!(parse(&[("ledger", r#""/var/lib/writ/../x""#)]).is_err());
        assert!(parse(&[("contract", r#""writ-service-config/2""#)]).is_err());
        assert!(parse(&[("limits", r#"{"max_connections":2,"max_connections_per_uid":3,"idle_seconds":1,"request_seconds":1}"#)]).is_err());
        assert!(parse(&[(
            "sockets",
            r#"{"host":"/run/writ/x.sock","admin":"/run/writ/x.sock"}"#
        )])
        .is_err());
    }
}
