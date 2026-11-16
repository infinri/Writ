//! `VerifiedDeployment` (M5a-04; J-9 "static start-time check bound to the operator-run
//! report"; J-4 trust of the snapshot store).
//!
//! The agent-uid probes can only be run by the operator; the service cannot act as the
//! agent. So the service accepts a deployment as verified only when:
//!
//! 1. the operator's `writ-readiness/1` report, read from the configured path (bounded), has
//!    exactly the D-006 digest recorded in the service configuration;
//! 2. the report says `governed_ready: true`, lists no blocking probe, and every probe in it
//!    has status `ok`;
//! 3. the report carries `static_facts` (`writ-static-facts/1`) and every fact the service
//!    can observe itself as `writ` ([`FactPlan`]: owners, modes and ACLs of layout paths,
//!    sysctls, binary digests, group memberships, its own identity) is exactly the
//!    report's, with the same set of keys.
//!
//! 4. (M5a-13 M4) the plan is the **full** static fact set ([`FactPlan::missing`]): its own
//!    identity, the J-9 sysctls, the groups `writ`, `writ-agents` and `writ-job`, at least one
//!    pinned binary, the layout paths the service requires, and the identity rules; an empty
//!    or partial plan issues nothing, so neither the `governed` label nor `immutable()` can
//!    rest on it;
//! 5. (M5a-13 M4) the **static identity check** of J-1 holds ([`identity_violations`]): the
//!    group rules over `/etc/passwd` (primary groups) and `/etc/group` (supplementary
//!    groups).
//!
//! [`VerifiedDeployment::issue`] is the only constructor. [`VerifiedDeployment::recheck`]
//! repeats every check (configured interval and before each job); any difference
//! **withdraws** the proof for the rest of the process's life (re-running the probes gives a
//! new report digest, which needs a configuration change and a restart). A withdrawn proof
//! makes the snapshot store's `immutable()` false and containment report missing
//! guarantees. On a host without an operator report (this workstation) no proof is issued.
//!
//! **Freshness (M5a-13 M5).** A proof is *current* ([`VerifiedDeployment::is_current`]) only
//! while its last passed check is no older than the configured recheck interval
//! ([`DeploymentConfig::recheck_interval`], monotonic clock); `writ-serviced` runs a timer
//! that rechecks at that interval, and the snapshot store's `immutable()` consults
//! `is_current`, so a stalled or failed timer fails closed.
//!
//! **Name-service limits (recorded).** Group membership is read from the files `/etc/passwd`
//! and `/etc/group` only (no NSS: the service links no libc name-service code). Accounts or
//! groups served by other NSS sources (LDAP, SSSD, systemd-userdb, `nss-systemd` dynamic
//! users) are invisible here; every configured uid must have exactly one `/etc/passwd`
//! entry, NIS compatibility lines (`+`, `-`) and malformed or duplicate entries refuse, and
//! the reference image (J-9) uses files only. The probes (P-AGENT-DESKTOP) observe the
//! effective membership with the kernel's view (`/proc/<pid>/status`) as the identities.

use std::collections::BTreeMap;
use std::fs;
use std::io::Read;
use std::os::unix::fs::{FileTypeExt, MetadataExt};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Instant;

use writ_kernel::json::{self, Digest, Value};

use crate::sock::process_identity;

/// The ACL extended attributes present on `p` (not following a final symlink). A copy of
/// `deny::acls`: `writ-serviced` reaches this module, and `deny` names the probe modules that
/// spawn processes (M5a-13 M3: the dependency gate refuses them in the service's reach).
fn acls(p: &Path) -> Vec<&'static str> {
    ["system.posix_acl_access", "system.posix_acl_default"]
        .into_iter()
        .filter(|n| rustix::fs::lgetxattr_query(p, *n).is_ok())
        .collect()
}

/// The static facts contract inside a readiness report.
pub const STATIC_FACTS_CONTRACT: &str = "writ-static-facts/1";
/// The readiness report contract.
pub const READINESS_CONTRACT: &str = "writ-readiness/1";
/// Largest readiness report read (bytes).
pub const MAX_REPORT_BYTES: u64 = 4 * 1024 * 1024;
/// Largest binary digested as a fact (bytes).
pub const MAX_BINARY_BYTES: u64 = 64 * 1024 * 1024;

/// What the service observes as static facts (built from its configuration).
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct FactPlan {
    /// Paths whose type, owner, group, mode and ACL presence are facts (not following a
    /// final symlink).
    pub paths: Vec<PathBuf>,
    /// Sysctl names (`kernel.yama.ptrace_scope`), read from `/proc/sys`.
    pub sysctls: Vec<String>,
    /// Files whose SHA-256 digest is a fact (`writ-cgroup-exec`, `writ-job-launcher`, unit
    /// files, the launcher configuration).
    pub binaries: Vec<PathBuf>,
    /// Groups whose gid and member list (from `/etc/group`) are facts.
    pub groups: Vec<String>,
    /// Whether the observing process's own identity is a fact.
    pub own_identity: bool,
    /// The group database read for `groups` (default `/etc/group`; tests use a copy).
    pub group_file: Option<PathBuf>,
    /// The J-1 static identity check (M5a-13 M4); `None` makes the plan incomplete.
    pub identity: Option<IdentityPlan>,
    /// Paths that must be among `paths` (the layout the service relies on: key directory,
    /// ledger and store directories, socket directory).
    pub required_paths: Vec<PathBuf>,
}

/// Sysctls every complete plan records (J-9 recorded facts).
pub const REQUIRED_SYSCTLS: [&str; 9] = [
    "kernel.apparmor_restrict_unprivileged_userns",
    "kernel.apparmor_restrict_unprivileged_unconfined",
    "user.max_user_namespaces",
    "kernel.yama.ptrace_scope",
    "fs.protected_symlinks",
    "fs.protected_hardlinks",
    "fs.suid_dumpable",
    "kernel.core_pattern",
    "net.ipv4.ip_unprivileged_port_start",
];

/// The groups of the J-1 identity model.
pub const SERVICE_GROUP: &str = "writ";
/// The agent group (J-1).
pub const AGENT_GROUP: &str = "writ-agents";
/// The job group (J-1, J-11).
pub const JOB_GROUP: &str = "writ-job";

/// The configured identities the static identity check applies J-1's group rules to.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct IdentityPlan {
    /// The `writ` uid.
    pub service_uid: u32,
    /// The `writ` gid.
    pub service_gid: u32,
    /// The agent uids.
    pub agent_uids: Vec<u32>,
    /// The `writ-job` uid.
    pub job_uid: u32,
    /// The desktop (human) uids.
    pub desktop_uids: Vec<u32>,
    /// The passwd database (default `/etc/passwd`; tests use a copy).
    pub passwd_file: Option<PathBuf>,
}

impl FactPlan {
    /// What the plan lacks to be the full static fact set (module documentation, item 4);
    /// empty when complete.
    pub fn missing(&self) -> Vec<String> {
        let mut out = Vec::new();
        if !self.own_identity {
            out.push("own_identity".to_string());
        }
        if self.identity.is_none() {
            out.push("identity".to_string());
        }
        for s in REQUIRED_SYSCTLS {
            if !self.sysctls.iter().any(|x| x == s) {
                out.push(format!("sysctl:{s}"));
            }
        }
        for g in [SERVICE_GROUP, AGENT_GROUP, JOB_GROUP] {
            if !self.groups.iter().any(|x| x == g) {
                out.push(format!("group:{g}"));
            }
        }
        if self.binaries.is_empty() {
            out.push("binaries".to_string());
        }
        for p in &self.required_paths {
            if !self.paths.contains(p) {
                out.push(format!("path:{}", p.display()));
            }
        }
        out
    }
}

struct PasswdEntry {
    name: String,
    uid: u32,
    gid: u32,
}

struct GroupEntry {
    name: String,
    gid: u32,
    members: Vec<String>,
}

fn parse_passwd(db: &str) -> Result<Vec<PasswdEntry>, String> {
    let mut out: Vec<PasswdEntry> = Vec::new();
    for (n, line) in db.lines().enumerate() {
        if line.is_empty() {
            continue;
        }
        let f: Vec<&str> = line.split(':').collect();
        let entry = match f.as_slice() {
            [name, _, uid, gid, _, _, _]
                if !name.is_empty() && !name.starts_with(['+', '-', '#']) =>
            {
                uid.parse()
                    .ok()
                    .zip(gid.parse().ok())
                    .map(|(uid, gid)| PasswdEntry {
                        name: (*name).to_string(),
                        uid,
                        gid,
                    })
            }
            _ => None,
        };
        let entry = entry.ok_or_else(|| format!("passwd line {} unusable", n + 1))?;
        if out.iter().any(|e| e.name == entry.name) {
            return Err(format!("passwd name {} repeated", entry.name));
        }
        out.push(entry);
    }
    Ok(out)
}

fn parse_group(db: &str) -> Result<Vec<GroupEntry>, String> {
    let mut out: Vec<GroupEntry> = Vec::new();
    for (n, line) in db.lines().enumerate() {
        if line.is_empty() {
            continue;
        }
        let f: Vec<&str> = line.split(':').collect();
        let entry = match f.as_slice() {
            [name, _, gid, members] if !name.is_empty() && !name.starts_with(['+', '-', '#']) => {
                gid.parse().ok().map(|gid| GroupEntry {
                    name: (*name).to_string(),
                    gid,
                    members: members
                        .split(',')
                        .filter(|m| !m.is_empty())
                        .map(str::to_string)
                        .collect(),
                })
            }
            _ => None,
        };
        let entry = entry.ok_or_else(|| format!("group line {} unusable", n + 1))?;
        if out.iter().any(|e| e.name == entry.name) {
            return Err(format!("group name {} repeated", entry.name));
        }
        out.push(entry);
    }
    Ok(out)
}

/// The J-1 static identity check over the passwd and group databases (module
/// documentation): every violation, empty when the rules hold. Primary groups come from
/// passwd, supplementary groups from group member lists; a user whose primary gid is a
/// group's gid is a member of that group.
pub fn identity_violations(passwd: &str, group: &str, plan: &IdentityPlan) -> Vec<String> {
    let users = match parse_passwd(passwd) {
        Ok(u) => u,
        Err(e) => return vec![e],
    };
    let groups = match parse_group(group) {
        Ok(g) => g,
        Err(e) => return vec![e],
    };
    let mut out = Vec::new();
    let user = |uid: u32, out: &mut Vec<String>| -> Option<&PasswdEntry> {
        let found: Vec<&PasswdEntry> = users.iter().filter(|u| u.uid == uid).collect();
        match found.as_slice() {
            [one] => Some(*one),
            [] => {
                out.push(format!(
                    "uid {uid} has no /etc/passwd entry (only files are read)"
                ));
                None
            }
            _ => {
                out.push(format!("uid {uid} has several /etc/passwd entries"));
                None
            }
        }
    };
    let named = |name: &str, out: &mut Vec<String>| -> Option<&GroupEntry> {
        let g = groups.iter().find(|g| g.name == name);
        if g.is_none() {
            out.push(format!("group {name} absent"));
        } else if groups
            .iter()
            .filter(|x| Some(x.gid) == g.map(|g| g.gid))
            .count()
            > 1
        {
            out.push(format!("group {name} shares its gid with another group"));
        }
        g
    };
    // Every gid a user is in: its primary gid and each group listing its name.
    let gids_of = |u: &PasswdEntry| -> Vec<u32> {
        let mut g = vec![u.gid];
        for e in &groups {
            if e.members.contains(&u.name) && !g.contains(&e.gid) {
                g.push(e.gid);
            }
        }
        g
    };
    // Every account name in a gid: users with it as primary and listed members.
    let members_of = |gid: u32| -> Vec<String> {
        let mut m: Vec<String> = users
            .iter()
            .filter(|u| u.gid == gid)
            .map(|u| u.name.clone())
            .collect();
        for e in groups.iter().filter(|e| e.gid == gid) {
            for n in &e.members {
                if !m.contains(n) {
                    m.push(n.clone());
                }
            }
        }
        m.sort();
        m
    };
    let service = user(plan.service_uid, &mut out);
    let job = user(plan.job_uid, &mut out);
    let agents: Vec<&PasswdEntry> = plan
        .agent_uids
        .iter()
        .filter_map(|u| user(*u, &mut out))
        .collect();
    let desktops: Vec<&PasswdEntry> = plan
        .desktop_uids
        .iter()
        .filter_map(|u| user(*u, &mut out))
        .collect();
    let writ = named(SERVICE_GROUP, &mut out);
    let agent_group = named(AGENT_GROUP, &mut out);
    let job_group = named(JOB_GROUP, &mut out);
    if !out.is_empty() {
        return out;
    }
    let (Some(service), Some(job), Some(writ), Some(agent_group), Some(job_group)) =
        (service, job, writ, agent_group, job_group)
    else {
        return vec!["identity databases incomplete".to_string()];
    };
    // `writ`: the service's primary group, with no member account but the service itself.
    if writ.gid != plan.service_gid || service.gid != plan.service_gid {
        out.push("group writ is not the service's primary group".to_string());
    }
    if members_of(writ.gid) != vec![service.name.clone()] || !writ.members.is_empty() {
        out.push("group writ has member accounts".to_string());
    }
    // `writ` is in no supplementary group.
    if gids_of(service).len() != 1 {
        out.push("writ is in a supplementary group".to_string());
    }
    // `writ-agents` contains exactly the agent accounts (and none has it as primary).
    let mut want: Vec<String> = agents.iter().map(|a| a.name.clone()).collect();
    want.sort();
    if members_of(agent_group.gid) != want || users.iter().any(|u| u.gid == agent_group.gid) {
        out.push("group writ-agents is not exactly the agent uids".to_string());
    }
    // `writ-job`: the job uid's own primary group, no other member, no supplementary group.
    if job.gid != job_group.gid {
        out.push("writ-job's primary group is not group writ-job".to_string());
    }
    if members_of(job_group.gid) != vec![job.name.clone()] || !job_group.members.is_empty() {
        out.push("group writ-job has other members".to_string());
    }
    if gids_of(job).len() != 1 {
        out.push("writ-job is in a supplementary group".to_string());
    }
    // Each agent: exactly {its own primary group, writ-agents}.
    for a in &agents {
        let mut g = gids_of(a);
        g.sort_unstable();
        let mut expect = vec![a.gid, agent_group.gid];
        expect.sort_unstable();
        let own = a.gid != writ.gid
            && a.gid != agent_group.gid
            && a.gid != job_group.gid
            && members_of(a.gid) == vec![a.name.clone()];
        if g != expect || !own {
            out.push(format!(
                "agent uid {}: groups are not exactly its own primary group and writ-agents",
                a.uid
            ));
        }
    }
    // Desktop uids: in none of writ, writ-agents, writ-job; their primary group holds no
    // agent uid and not writ-job.
    for d in &desktops {
        let g = gids_of(d);
        if [writ.gid, agent_group.gid, job_group.gid]
            .iter()
            .any(|x| g.contains(x))
        {
            out.push(format!(
                "desktop uid {} is in writ, writ-agents or writ-job",
                d.uid
            ));
        }
        let primary = members_of(d.gid);
        if agents.iter().any(|a| primary.contains(&a.name)) || primary.contains(&job.name) {
            out.push(format!(
                "an agent uid or writ-job is in desktop uid {}'s primary group",
                d.uid
            ));
        }
    }
    out
}

/// Observed facts, keyed by `kind:subject`.
pub type Facts = BTreeMap<String, String>;

fn path_fact(p: &Path) -> String {
    match fs::symlink_metadata(p) {
        Err(e) => format!("absent:{}", e.raw_os_error().unwrap_or(0)),
        Ok(m) => {
            let ft = m.file_type();
            let kind = if ft.is_symlink() {
                "symlink"
            } else if ft.is_dir() {
                "dir"
            } else if ft.is_file() {
                "file"
            } else if ft.is_socket() {
                "socket"
            } else {
                "other"
            };
            let acl = if acls(p).is_empty() { 0 } else { 1 };
            format!(
                "type={kind} uid={} gid={} mode={:o} nlink={} acl={acl}",
                m.uid(),
                m.gid(),
                m.mode() & 0o7777,
                m.nlink()
            )
        }
    }
}

fn sysctl_fact(name: &str) -> String {
    if name.is_empty()
        || !name
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'.' || b == b'_')
    {
        return "invalid".to_string();
    }
    let path = format!("/proc/sys/{}", name.replace('.', "/"));
    fs::read_to_string(path).map_or_else(
        |e| format!("absent:{}", e.raw_os_error().unwrap_or(0)),
        |t| t.trim().to_string(),
    )
}

fn digest_fact(p: &Path) -> String {
    let file = match fs::File::open(p) {
        Ok(f) => f,
        Err(e) => return format!("absent:{}", e.raw_os_error().unwrap_or(0)),
    };
    let mut bytes = Vec::new();
    match file.take(MAX_BINARY_BYTES + 1).read_to_end(&mut bytes) {
        Ok(n) if (n as u64) <= MAX_BINARY_BYTES => Digest::of_bytes(&bytes).to_string(),
        Ok(_) => "too-large".to_string(),
        Err(e) => format!("unreadable:{}", e.raw_os_error().unwrap_or(0)),
    }
}

fn group_fact(db: &str, name: &str) -> String {
    for line in db.lines() {
        let mut parts = line.split(':');
        if parts.next() != Some(name) {
            continue;
        }
        let _password = parts.next();
        let gid = parts.next().unwrap_or("");
        let mut members: Vec<&str> = parts
            .next()
            .unwrap_or("")
            .split(',')
            .filter(|m| !m.is_empty())
            .collect();
        members.sort_unstable();
        return format!("gid={gid} members={}", members.join(","));
    }
    "absent".to_string()
}

/// Observe the facts of `plan` as the running process.
pub fn observe(plan: &FactPlan) -> Facts {
    let mut facts = Facts::new();
    for p in &plan.paths {
        facts.insert(format!("path:{}", p.display()), path_fact(p));
    }
    for s in &plan.sysctls {
        facts.insert(format!("sysctl:{s}"), sysctl_fact(s));
    }
    for b in &plan.binaries {
        facts.insert(format!("digest:{}", b.display()), digest_fact(b));
    }
    if !plan.groups.is_empty() {
        let file = plan
            .group_file
            .clone()
            .unwrap_or_else(|| PathBuf::from("/etc/group"));
        let db = bounded_text(&file);
        for g in &plan.groups {
            let fact = db
                .as_deref()
                .map_or_else(|| "unreadable".to_string(), |db| group_fact(db, g));
            facts.insert(format!("group:{g}"), fact);
        }
    }
    if plan.own_identity {
        let text = process_identity().map_or_else(
            |e| format!("unavailable:{}", e.raw_os_error().unwrap_or(0)),
            |id| {
                format!(
                    "ruid={} euid={} rgid={} egid={} groups={:?}",
                    id.ruid, id.euid, id.rgid, id.egid, id.groups
                )
            },
        );
        facts.insert("self:identity".to_string(), text);
    }
    facts
}

/// The `static_facts` member a readiness report carries for `facts`.
pub fn static_facts_value(facts: &Facts) -> Value {
    json::obj([
        ("contract", json::s(STATIC_FACTS_CONTRACT)),
        (
            "facts",
            Value::Obj(facts.iter().map(|(k, v)| (k.clone(), json::s(v))).collect()),
        ),
    ])
}

/// Where the service finds and pins the operator report.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DeploymentConfig {
    /// The operator-run `writ-readiness/1` report.
    pub report: PathBuf,
    /// Its D-006 digest (`sha256:<hex>`) recorded in the service configuration.
    pub report_digest: String,
    /// The facts the service observes.
    pub plan: FactPlan,
    /// How often `writ-serviced` rechecks, and the age after which a proof is no longer
    /// current (M5a-13 M5).
    pub recheck_interval: std::time::Duration,
}

/// Why no proof was issued, or why it was withdrawn.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum DeploymentRefused {
    /// The report could not be read, or is larger than [`MAX_REPORT_BYTES`].
    ReportUnreadable,
    /// The report's digest is not the configured one.
    DigestMismatch,
    /// Not a strict JSON `writ-readiness/1` report.
    ReportMalformed,
    /// Not governed-ready, a blocking probe, or a probe whose status is not `ok`.
    NotReady,
    /// No `writ-static-facts/1` member.
    NoStaticFacts,
    /// An observed fact differs from the report's (the key), or the key sets differ.
    FactDiffers(String),
    /// The plan is not the full static fact set (what it lacks; M5a-13 M4).
    PlanIncomplete(Vec<String>),
    /// The J-1 static identity check failed (every violation; M5a-13 M4).
    IdentityViolation(Vec<String>),
}

fn check(config: &DeploymentConfig) -> Result<(), DeploymentRefused> {
    let missing = config.plan.missing();
    if !missing.is_empty() || config.recheck_interval.is_zero() {
        return Err(DeploymentRefused::PlanIncomplete(missing));
    }
    let file = fs::File::open(&config.report).map_err(|_| DeploymentRefused::ReportUnreadable)?;
    let mut bytes = Vec::new();
    file.take(MAX_REPORT_BYTES + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| DeploymentRefused::ReportUnreadable)?;
    if bytes.len() as u64 > MAX_REPORT_BYTES {
        return Err(DeploymentRefused::ReportUnreadable);
    }
    if Digest::of_bytes(&bytes).to_string() != config.report_digest {
        return Err(DeploymentRefused::DigestMismatch);
    }
    let report = json::parse(&bytes).map_err(|_| DeploymentRefused::ReportMalformed)?;
    let m = report.as_obj().ok_or(DeploymentRefused::ReportMalformed)?;
    if m.get("contract").and_then(Value::as_str) != Some(READINESS_CONTRACT) {
        return Err(DeploymentRefused::ReportMalformed);
    }
    let ready = m.get("governed_ready") == Some(&Value::Bool(true));
    let unblocked = matches!(m.get("blocking"), Some(Value::Arr(b)) if b.is_empty());
    let probes_ok = match m.get("probes") {
        Some(Value::Arr(ps)) if !ps.is_empty() => ps.iter().all(|p| {
            p.as_obj()
                .and_then(|o| o.get("status"))
                .and_then(Value::as_str)
                == Some("ok")
        }),
        _ => false,
    };
    if !(ready && unblocked && probes_ok) {
        return Err(DeploymentRefused::NotReady);
    }
    let sf = m
        .get("static_facts")
        .and_then(Value::as_obj)
        .ok_or(DeploymentRefused::NoStaticFacts)?;
    if sf.get("contract").and_then(Value::as_str) != Some(STATIC_FACTS_CONTRACT) {
        return Err(DeploymentRefused::NoStaticFacts);
    }
    let recorded = sf
        .get("facts")
        .and_then(Value::as_obj)
        .ok_or(DeploymentRefused::NoStaticFacts)?;
    let observed = observe(&config.plan);
    for (k, v) in &observed {
        if recorded.get(k).and_then(Value::as_str) != Some(v.as_str()) {
            return Err(DeploymentRefused::FactDiffers(k.clone()));
        }
    }
    if let Some(extra) = recorded.keys().find(|k| !observed.contains_key(*k)) {
        return Err(DeploymentRefused::FactDiffers(extra.clone()));
    }
    if let Some(identity) = &config.plan.identity {
        let passwd = identity
            .passwd_file
            .clone()
            .unwrap_or_else(|| PathBuf::from("/etc/passwd"));
        let group = config
            .plan
            .group_file
            .clone()
            .unwrap_or_else(|| PathBuf::from("/etc/group"));
        let read = |p: &Path| bounded_text(p).ok_or_else(|| format!("{} unreadable", p.display()));
        let violations = match (read(&passwd), read(&group)) {
            (Ok(pw), Ok(gr)) => identity_violations(&pw, &gr, identity),
            (Err(e), _) | (_, Err(e)) => vec![e],
        };
        if !violations.is_empty() {
            return Err(DeploymentRefused::IdentityViolation(violations));
        }
    }
    Ok(())
}

/// Largest identity database read (bytes).
const MAX_DB_BYTES: u64 = 4 * 1024 * 1024;

fn bounded_text(p: &Path) -> Option<String> {
    let mut bytes = Vec::new();
    fs::File::open(p)
        .ok()?
        .take(MAX_DB_BYTES + 1)
        .read_to_end(&mut bytes)
        .ok()?;
    if bytes.len() as u64 > MAX_DB_BYTES {
        return None;
    }
    String::from_utf8(bytes).ok()
}

/// Proof that the deployment is verified (module documentation). Only
/// [`VerifiedDeployment::issue`] constructs one.
#[derive(Debug)]
pub struct VerifiedDeployment {
    config: DeploymentConfig,
    verified: AtomicBool,
    withdrawn: std::sync::Mutex<Option<DeploymentRefused>>,
    last_passed: std::sync::Mutex<Instant>,
}

impl VerifiedDeployment {
    /// Issue the proof when every check passes.
    pub fn issue(config: DeploymentConfig) -> Result<Arc<VerifiedDeployment>, DeploymentRefused> {
        check(&config)?;
        Ok(Arc::new(VerifiedDeployment {
            config,
            verified: AtomicBool::new(true),
            withdrawn: std::sync::Mutex::new(None),
            last_passed: std::sync::Mutex::new(Instant::now()),
        }))
    }

    /// `true` until a recheck withdraws the proof.
    pub fn is_verified(&self) -> bool {
        self.verified.load(Ordering::SeqCst)
    }

    /// `true` while the proof holds **and** its last passed check is no older than the
    /// recheck interval (module documentation, "Freshness"). Fails closed: a poisoned lock
    /// or a stalled recheck timer makes it `false`.
    pub fn is_current(&self) -> bool {
        self.is_verified()
            && self
                .last_passed
                .lock()
                .is_ok_and(|t| t.elapsed() <= self.config.recheck_interval)
    }

    /// The configured recheck interval.
    pub fn recheck_interval(&self) -> std::time::Duration {
        self.config.recheck_interval
    }

    /// Repeat every check; on any difference withdraw the proof for good. Returns
    /// [`VerifiedDeployment::is_verified`].
    pub fn recheck(&self) -> bool {
        if !self.is_verified() {
            return false;
        }
        match check(&self.config) {
            Ok(()) => {
                if let Ok(mut t) = self.last_passed.lock() {
                    *t = Instant::now();
                }
            }
            Err(why) => {
                self.verified.store(false, Ordering::SeqCst);
                if let Ok(mut w) = self.withdrawn.lock() {
                    *w = Some(why);
                }
            }
        }
        self.is_verified()
    }

    /// Why the proof was withdrawn, if it was.
    pub fn withdrawn_because(&self) -> Option<DeploymentRefused> {
        self.withdrawn.lock().ok().and_then(|w| w.clone())
    }

    /// The pinned report digest.
    pub fn report_digest(&self) -> &str {
        &self.config.report_digest
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    fn dir(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("writ-deploy-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&d);
        fs::create_dir_all(&d).unwrap();
        d
    }

    fn plan(d: &Path) -> FactPlan {
        let state = d.join("state");
        fs::create_dir_all(&state).unwrap();
        fs::set_permissions(&state, fs::Permissions::from_mode(0o700)).unwrap();
        let bin = d.join("helper");
        fs::write(&bin, b"#!/bin/true\n").unwrap();
        let groups = d.join("group");
        fs::write(&groups, GROUP).unwrap();
        let passwd = d.join("passwd");
        fs::write(&passwd, PASSWD).unwrap();
        FactPlan {
            paths: vec![state.clone()],
            sysctls: REQUIRED_SYSCTLS.iter().map(|s| s.to_string()).collect(),
            binaries: vec![bin],
            groups: vec!["writ".into(), "writ-agents".into(), "writ-job".into()],
            own_identity: true,
            group_file: Some(groups),
            identity: Some(IdentityPlan {
                passwd_file: Some(passwd),
                ..identities()
            }),
            required_paths: vec![state],
        }
    }

    /// A reference-shaped account database (J-1): writ 990, agent 1001 in writ-agents,
    /// writ-job 992, the human 1000.
    const PASSWD: &str = "root:x:0:0:root:/root:/bin/bash\n\
        human:x:1000:1000::/home/human:/bin/bash\n\
        writ:x:990:990::/var/lib/writ:/usr/sbin/nologin\n\
        writ-agent:x:1001:1001::/home/writ-agent:/bin/bash\n\
        writ-job:x:992:992::/nonexistent:/usr/sbin/nologin\n";
    const GROUP: &str = "root:x:0:\nhuman:x:1000:\nsudo:x:27:human\nwrit:x:990:\n\
        writ-agent:x:1001:\nwrit-agents:x:991:writ-agent\nwrit-job:x:992:\n";

    fn identities() -> IdentityPlan {
        IdentityPlan {
            service_uid: 990,
            service_gid: 990,
            agent_uids: vec![1001],
            job_uid: 992,
            desktop_uids: vec![1000],
            passwd_file: None,
        }
    }

    #[test]
    fn the_reference_identity_model_passes() {
        assert_eq!(
            identity_violations(PASSWD, GROUP, &identities()),
            Vec::<String>::new()
        );
    }

    /// M5a-13 M4: every J-1 / N10 group rule refuses on its own.
    #[test]
    fn every_identity_rule_refuses_on_its_own() {
        let cases: &[(&str, &str, &str, &str)] = &[
            // (what, passwd edit from -> to, group edit from -> to)
            ("writ has a member", "", "", "writ:x:990:\n=>writ:x:990:human\n"),
            (
                "another account has writ as primary group",
                "human:x:1000:1000:=>human:x:1000:990:",
                "",
                "",
            ),
            ("writ in a supplementary group", "", "", "sudo:x:27:human=>sudo:x:27:human,writ"),
            (
                "writ-agents lacks the agent",
                "",
                "",
                "writ-agents:x:991:writ-agent=>writ-agents:x:991:",
            ),
            (
                "writ-agents has another member",
                "",
                "",
                "writ-agents:x:991:writ-agent=>writ-agents:x:991:writ-agent,root",
            ),
            (
                "an account has writ-agents as primary group",
                "root:x:0:0:=>root:x:0:991:",
                "",
                "",
            ),
            (
                "the agent in a third group",
                "",
                "",
                "sudo:x:27:human=>sudo:x:27:human,writ-agent",
            ),
            (
                "the agent's primary group shared",
                "writ-job:x:992:992:=>writ-job:x:992:1001:",
                "",
                "",
            ),
            (
                "the agent's primary group is writ-agents",
                "writ-agent:x:1001:1001:=>writ-agent:x:1001:991:",
                "",
                "",
            ),
            ("writ-job in a supplementary group", "", "", "sudo:x:27:human=>sudo:x:27:writ-job"),
            ("writ-job's group has a member", "", "", "writ-job:x:992:\n=>writ-job:x:992:root\n"),
            (
                "writ-job's primary group is not writ-job",
                "writ-job:x:992:992:=>writ-job:x:992:0:",
                "",
                "",
            ),
            ("a desktop uid in writ-agents", "", "", "writ-agents:x:991:writ-agent=>writ-agents:x:991:writ-agent,human"),
            ("a desktop uid in writ-job", "", "", "writ-job:x:992:\n=>writ-job:x:992:human\n"),
            ("an agent in the desktop's primary group", "", "", "human:x:1000:=>human:x:1000:writ-agent"),
            ("writ-job in the desktop's primary group", "", "", "human:x:1000:=>human:x:1000:writ-job"),
            ("a configured uid missing from passwd", "human:x:1000=>other:x:1003", "", ""),
            ("a group missing", "", "", "writ-job:x:992:\n=>"),
            ("a NIS compatibility line", "", "", "root:x:0:\n=>root:x:0:\n+::::\n"),
            ("a duplicate passwd uid", "root:x:0:0:root:/root:/bin/bash=>root:x:0:0:root:/root:/bin/bash\nshadow:x:1000:5::/:/bin/sh", "", ""),
            ("a malformed passwd line", "root:x:0:0:root:/root:/bin/bash=>root:x:zero:0:root:/root:/bin/bash", "", ""),
            ("two groups share writ's gid", "", "", "writ:x:990:\n=>writ:x:990:\nwrit2:x:990:\n"),
        ];
        for (what, pw, _, gr) in cases {
            let edit = |db: &str, e: &str| {
                if e.is_empty() {
                    return db.to_string();
                }
                let (from, to) = e.split_once("=>").unwrap();
                assert!(db.contains(from), "{what}: {from}");
                db.replacen(from, to, 1)
            };
            let v = identity_violations(&edit(PASSWD, pw), &edit(GROUP, gr), &identities());
            assert!(!v.is_empty(), "{what} was not refused");
        }
    }

    /// M5a-13 M4: an empty or partial plan issues nothing (a governed label and
    /// `immutable()` need the full static fact set).
    #[test]
    fn an_incomplete_plan_issues_nothing() {
        let d = dir("incomplete");
        let full = plan(&d);
        assert_eq!(full.missing(), Vec::<String>::new());
        let empty = FactPlan::default();
        let facts = observe(&empty);
        let refused =
            VerifiedDeployment::issue(config(&d, &report(Some(&facts), true, "ok"), empty)).err();
        assert!(
            matches!(&refused, Some(DeploymentRefused::PlanIncomplete(m)) if m.len() > 5),
            "{refused:?}"
        );
        type Strip = fn(&mut FactPlan);
        let strips: [Strip; 6] = [
            |p| p.own_identity = false,
            |p| p.identity = None,
            |p| p.sysctls.pop().map(drop).unwrap_or(()),
            |p| p.groups.retain(|g| g != "writ-job"),
            |p| p.binaries.clear(),
            |p| p.paths.clear(),
        ];
        for strip in strips {
            let mut partial = full.clone();
            strip(&mut partial);
            let facts = observe(&partial);
            assert!(matches!(
                VerifiedDeployment::issue(config(&d, &report(Some(&facts), true, "ok"), partial))
                    .err(),
                Some(DeploymentRefused::PlanIncomplete(_))
            ));
        }
        // An identity violation refuses at issue and withdraws at recheck.
        let p = plan(&d);
        let facts = observe(&p);
        let passwd = p.identity.as_ref().unwrap().passwd_file.clone().unwrap();
        let proof =
            VerifiedDeployment::issue(config(&d, &report(Some(&facts), true, "ok"), p.clone()))
                .unwrap();
        fs::write(
            &passwd,
            PASSWD.replace("writ-agent:x:1001:1001:", "writ-agent:x:1001:1000:"),
        )
        .unwrap();
        assert!(!proof.recheck());
        assert!(matches!(
            proof.withdrawn_because(),
            Some(DeploymentRefused::IdentityViolation(_))
        ));
        assert!(matches!(
            VerifiedDeployment::issue(config(&d, &report(Some(&facts), true, "ok"), p)).err(),
            Some(DeploymentRefused::IdentityViolation(_))
        ));
    }

    /// M5a-13 M5: a proof is current only while its last passed check is no older than
    /// the recheck interval.
    #[test]
    fn a_proof_without_a_recent_recheck_is_not_current() {
        let d = dir("fresh");
        let p = plan(&d);
        let facts = observe(&p);
        let mut c = config(&d, &report(Some(&facts), true, "ok"), p);
        c.recheck_interval = std::time::Duration::from_millis(150);
        let proof = VerifiedDeployment::issue(c.clone()).unwrap();
        assert!(proof.is_current());
        std::thread::sleep(std::time::Duration::from_millis(250));
        assert!(proof.is_verified(), "not withdrawn");
        assert!(!proof.is_current(), "stale: fails closed");
        assert!(proof.recheck());
        assert!(
            proof.is_current(),
            "a passed recheck makes it current again"
        );
        c.recheck_interval = std::time::Duration::ZERO;
        assert!(matches!(
            VerifiedDeployment::issue(c).err(),
            Some(DeploymentRefused::PlanIncomplete(_))
        ));
    }

    fn report(facts: Option<&Facts>, ready: bool, probe_status: &str) -> Vec<u8> {
        let mut m = json::Map::new();
        m.insert("contract".into(), json::s(READINESS_CONTRACT));
        m.insert("governed_ready".into(), Value::Bool(ready));
        m.insert("blocking".into(), Value::Arr(vec![]));
        m.insert(
            "probes".into(),
            Value::Arr(vec![json::obj([
                ("id", json::s("P-USERNS")),
                ("status", json::s(probe_status)),
            ])]),
        );
        if let Some(f) = facts {
            m.insert("static_facts".into(), static_facts_value(f));
        }
        json::canonical(&Value::Obj(m)).unwrap().into_bytes()
    }

    fn config(d: &Path, bytes: &[u8], plan: FactPlan) -> DeploymentConfig {
        let path = d.join("readiness.json");
        fs::write(&path, bytes).unwrap();
        DeploymentConfig {
            report: path,
            report_digest: Digest::of_bytes(bytes).to_string(),
            plan,
            recheck_interval: std::time::Duration::from_secs(300),
        }
    }

    #[test]
    fn a_matching_operator_report_issues_the_proof_and_a_changed_fact_withdraws_it() {
        let d = dir("issue");
        let p = plan(&d);
        let facts = observe(&p);
        let state_key = format!("path:{}", d.join("state").display());
        assert!(facts[&state_key].contains("type=dir") && facts[&state_key].contains("mode=700"));
        let bytes = report(Some(&facts), true, "ok");
        let proof = VerifiedDeployment::issue(config(&d, &bytes, p)).unwrap();
        assert!(proof.is_verified());
        assert!(proof.recheck());
        // The trusted directory's mode changes after start: withdrawn for good.
        fs::set_permissions(d.join("state"), fs::Permissions::from_mode(0o750)).unwrap();
        assert!(!proof.recheck());
        assert_eq!(
            proof.withdrawn_because(),
            Some(DeploymentRefused::FactDiffers(state_key))
        );
        fs::set_permissions(d.join("state"), fs::Permissions::from_mode(0o700)).unwrap();
        assert!(!proof.recheck(), "withdrawal is permanent");
    }

    #[test]
    fn every_check_refuses_on_its_own() {
        let d = dir("refuse");
        let p = plan(&d);
        let facts = observe(&p);
        // Missing report.
        let mut c = config(&d, &report(Some(&facts), true, "ok"), p.clone());
        c.report = d.join("absent.json");
        assert_eq!(
            VerifiedDeployment::issue(c).err(),
            Some(DeploymentRefused::ReportUnreadable)
        );
        // Digest not the configured one.
        let mut c = config(&d, &report(Some(&facts), true, "ok"), p.clone());
        c.report_digest = Digest::of_bytes(b"other").to_string();
        assert_eq!(
            VerifiedDeployment::issue(c).err(),
            Some(DeploymentRefused::DigestMismatch)
        );
        // Not ready, or a probe not ok.
        for bytes in [
            report(Some(&facts), false, "ok"),
            report(Some(&facts), true, "missing"),
        ] {
            assert_eq!(
                VerifiedDeployment::issue(config(&d, &bytes, p.clone())).err(),
                Some(DeploymentRefused::NotReady)
            );
        }
        // No static facts (a report produced before this check existed).
        assert_eq!(
            VerifiedDeployment::issue(config(&d, &report(None, true, "ok"), p.clone())).err(),
            Some(DeploymentRefused::NoStaticFacts)
        );
        // A fact that differs, and an extra recorded fact.
        let mut changed = facts.clone();
        changed.insert("sysctl:kernel.yama.ptrace_scope".into(), "0-planted".into());
        assert!(matches!(
            VerifiedDeployment::issue(config(&d, &report(Some(&changed), true, "ok"), p.clone()))
                .err(),
            Some(DeploymentRefused::FactDiffers(_))
        ));
        let mut extra = facts.clone();
        extra.insert("path:/planted".into(), "x".into());
        assert_eq!(
            VerifiedDeployment::issue(config(&d, &report(Some(&extra), true, "ok"), p.clone()))
                .err(),
            Some(DeploymentRefused::FactDiffers("path:/planted".into()))
        );
        // Malformed.
        assert_eq!(
            VerifiedDeployment::issue(config(&d, b"{\"contract\":\"x\"}", p)).err(),
            Some(DeploymentRefused::ReportMalformed)
        );
    }

    #[test]
    fn group_and_path_facts_are_precise() {
        let db = "a:x:1:\nb:x:2:z,y\n";
        assert_eq!(group_fact(db, "b"), "gid=2 members=y,z");
        assert_eq!(group_fact(db, "a"), "gid=1 members=");
        assert_eq!(group_fact(db, "c"), "absent");
        assert!(path_fact(Path::new("/nonexistent-writ")).starts_with("absent:"));
        assert_eq!(sysctl_fact("../../etc/passwd"), "invalid");
    }
}
