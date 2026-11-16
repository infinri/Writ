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
//! [`VerifiedDeployment::issue`] is the only constructor. [`VerifiedDeployment::recheck`]
//! repeats every check (configured interval and before each job); any difference
//! **withdraws** the proof for the rest of the process's life (re-running the probes gives a
//! new report digest, which needs a configuration change and a restart). A withdrawn proof
//! makes the snapshot store's `immutable()` false and containment report missing
//! guarantees. On a host without an operator report (this workstation) no proof is issued.

use std::collections::BTreeMap;
use std::fs;
use std::io::Read;
use std::os::unix::fs::{FileTypeExt, MetadataExt};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use writ_kernel::json::{self, Digest, Value};

use crate::deny::acls;
use crate::sock::process_identity;

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
        let db = fs::read_to_string(file).unwrap_or_default();
        for g in &plan.groups {
            facts.insert(format!("group:{g}"), group_fact(&db, g));
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
}

fn check(config: &DeploymentConfig) -> Result<(), DeploymentRefused> {
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
    Ok(())
}

/// Proof that the deployment is verified (module documentation). Only
/// [`VerifiedDeployment::issue`] constructs one.
#[derive(Debug)]
pub struct VerifiedDeployment {
    config: DeploymentConfig,
    verified: AtomicBool,
    withdrawn: std::sync::Mutex<Option<DeploymentRefused>>,
}

impl VerifiedDeployment {
    /// Issue the proof when every check passes.
    pub fn issue(config: DeploymentConfig) -> Result<Arc<VerifiedDeployment>, DeploymentRefused> {
        check(&config)?;
        Ok(Arc::new(VerifiedDeployment {
            config,
            verified: AtomicBool::new(true),
            withdrawn: std::sync::Mutex::new(None),
        }))
    }

    /// `true` until a recheck withdraws the proof.
    pub fn is_verified(&self) -> bool {
        self.verified.load(Ordering::SeqCst)
    }

    /// Repeat every check; on any difference withdraw the proof for good. Returns
    /// [`VerifiedDeployment::is_verified`].
    pub fn recheck(&self) -> bool {
        if !self.is_verified() {
            return false;
        }
        if let Err(why) = check(&self.config) {
            self.verified.store(false, Ordering::SeqCst);
            if let Ok(mut w) = self.withdrawn.lock() {
                *w = Some(why);
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
        fs::write(&groups, "writ:x:990:\nwrit-agents:x:991:writ-agent\n").unwrap();
        FactPlan {
            paths: vec![state],
            sysctls: vec!["kernel.yama.ptrace_scope".into()],
            binaries: vec![bin],
            groups: vec!["writ".into(), "writ-agents".into()],
            own_identity: true,
            group_file: Some(groups),
        }
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
