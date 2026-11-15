//! `writ-probe agent-deny`: actual access attempts made as the agent uid against the trusted
//! layout (J-1, P-AGENT-DENY). Every attempt is unprivileged; a write that unexpectedly
//! succeeds is removed again and reported as `allowed`.

use crate::probes::{Config, AGENT_CONTRACT};
use std::fs;
use std::io::{ErrorKind, Read};
use std::os::unix::net::UnixStream;
use std::path::Path;
use writ_kernel::json::{self, s, Value};

fn outcome(r: std::io::Result<()>) -> (&'static str, String) {
    match r {
        Ok(()) => ("allowed", String::new()),
        Err(e) if e.kind() == ErrorKind::PermissionDenied => ("refused", e.to_string()),
        Err(e) if e.raw_os_error() == Some(1) => ("refused", e.to_string()),
        Err(e) if e.kind() == ErrorKind::NotFound => ("absent", e.to_string()),
        Err(e) => ("error", e.to_string()),
    }
}

fn read_dir(p: &Path) -> std::io::Result<()> {
    fs::read_dir(p).map(drop)
}

fn create_and_remove(dir: &Path) -> std::io::Result<()> {
    let f = dir.join(format!(".writ-agent-probe-{}", std::process::id()));
    fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&f)?;
    let _ = fs::remove_file(&f);
    Ok(())
}

fn read_some(p: &Path) -> std::io::Result<()> {
    let mut buf = [0u8; 1];
    fs::File::open(p)?.read(&mut buf).map(drop)
}

fn signal(pid: i32) -> std::io::Result<()> {
    let p = rustix::process::Pid::from_raw(pid)
        .ok_or_else(|| std::io::Error::from(ErrorKind::InvalidInput))?;
    rustix::process::test_kill_process(p).map_err(std::io::Error::from)
}

/// Make every attempt and build the `writ-agent-deny/1` report.
pub fn report(cfg: &Config, service_pid: i32) -> Value {
    let sr = &cfg.state_root;
    let proc_dir = Path::new("/proc").join(service_pid.to_string());
    let attempts: Vec<(&str, std::io::Result<()>)> = vec![
        ("read_state_root", read_dir(sr)),
        ("read_ledger", read_dir(&sr.join("ledger"))),
        ("read_stores", read_dir(&sr.join("stores"))),
        ("read_keys", read_dir(&sr.join("keys"))),
        ("write_work_root", create_and_remove(&sr.join("run"))),
        ("write_target_root", create_and_remove(&cfg.target_root)),
        (
            "connect_admin_sock",
            UnixStream::connect(cfg.run_dir.join("admin.sock")).map(drop),
        ),
        ("read_service_environ", read_some(&proc_dir.join("environ"))),
        ("open_service_mem", read_some(&proc_dir.join("mem"))),
        ("signal_service", signal(service_pid)),
    ];
    let items = attempts
        .into_iter()
        .map(|(t, r)| {
            let (out, detail) = outcome(r);
            json::obj([
                ("target", s(t)),
                ("outcome", s(out)),
                ("detail", s(&detail)),
            ])
        })
        .collect();
    json::obj([
        ("contract", s(AGENT_CONTRACT)),
        (
            "agent_uid",
            Value::Int(i64::from(rustix::process::getuid().as_raw())),
        ),
        ("service_pid", Value::Int(i64::from(service_pid))),
        ("attempts", Value::Arr(items)),
    ])
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;
    use crate::probes::eval_agent_report;

    #[test]
    fn same_uid_attempts_are_not_a_passing_report() {
        // Run as the current user against its own process and an absent layout: nothing is
        // refused, and the report is rejected for the service uid regardless.
        let cfg = Config {
            state_root: "/nonexistent/writ-probe-state".into(),
            ..Config::reference()
        };
        let me = i32::try_from(std::process::id()).unwrap();
        let r = report(&cfg, me);
        let text = json::canonical(&r).unwrap();
        let uid = rustix::process::getuid().as_raw();
        assert!(eval_agent_report(text.as_bytes(), uid).is_err());
        assert!(
            eval_agent_report(text.as_bytes(), uid.wrapping_add(1)).is_err(),
            "absent and allowed outcomes are not refusals"
        );
        let attempts = r.get("attempts").and_then(Value::as_arr).unwrap();
        let sig = attempts
            .iter()
            .find(|a| a.get("target").and_then(Value::as_str) == Some("signal_service"))
            .unwrap();
        assert_eq!(sig.get("outcome").and_then(Value::as_str), Some("allowed"));
    }
}
