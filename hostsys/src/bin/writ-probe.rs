//! `writ-probe`: host readiness report (`writ-readiness/1`) and agent-side attempts
//! (`writ-agent-deny/1`), written as canonical JSON to stdout (M5a-03, J-9 revision 2).
//!
//! Exit status: 0 governed-ready, 1 not ready, 2 usage or internal error.

use std::path::PathBuf;
use std::process::ExitCode;
use std::time::{SystemTime, UNIX_EPOCH};
use writ_hostsys::agent::{self, AgentOptions};
use writ_hostsys::readiness::{report, verdict, ReportContext};
use writ_hostsys::{facts, probes, Config};
use writ_kernel::json;

const USAGE: &str = "usage:
  writ-probe [report] [options]    as the service uid: readiness report (writ-readiness/1)
  writ-probe agent-deny [options]  as the agent uid: attempts (writ-agent-deny/1); needs --service-pid
options:
  --state-root DIR   (default /var/lib/writ)          --config-dir DIR (default /etc/writ)
  --run-dir DIR      (default /run/writ)              --target-root DIR (default /srv/writ/targets)
  --scratch DIR      (default <state-root>/run)       --bwrap PATH (default /usr/local/libexec/writ/bwrap)
  --system-bwrap PATH (default /usr/bin/bwrap)        --agents-group NAME (default writ-agents)
  --xdev-root DIR    (VM trial: DIR/root/mnt is a bind mount holding file f)
  --cgroup-delegated (reference VM only: the probe runs in its own delegated systemd unit)
  --service-pid N    --agent-report FILE  --canary-file FILE  --log-file FILE (repeatable)
agent-deny only:
  --service-unit NAME (default writ.service)  --hardlink-source FILE  --extra-read FILE (repeatable)
  --desktop-uid N (C-1 option (a) only)";

fn parse(args: &[String]) -> Result<(String, Config, AgentOptions), String> {
    let mut cfg = Config::reference();
    let mut opt = AgentOptions {
        service_unit: "writ.service".into(),
        ..AgentOptions::default()
    };
    let mut mode = "report".to_string();
    let mut scratch = None;
    let mut it = args.iter();
    while let Some(a) = it.next() {
        let mut val = || {
            it.next()
                .cloned()
                .ok_or_else(|| format!("{a} needs a value"))
        };
        match a.as_str() {
            "report" | "agent-deny" => mode = a.clone(),
            "--state-root" => cfg.state_root = PathBuf::from(val()?),
            "--config-dir" => cfg.config_dir = PathBuf::from(val()?),
            "--run-dir" => cfg.run_dir = PathBuf::from(val()?),
            "--target-root" => cfg.target_root = PathBuf::from(val()?),
            "--scratch" => scratch = Some(PathBuf::from(val()?)),
            "--bwrap" => cfg.bwrap = PathBuf::from(val()?),
            "--system-bwrap" => cfg.system_bwrap = PathBuf::from(val()?),
            "--agents-group" => cfg.agents_group = val()?,
            "--xdev-root" => cfg.xdev_root = Some(PathBuf::from(val()?)),
            "--cgroup-delegated" => cfg.cgroup_delegated = true,
            "--service-pid" => {
                cfg.service_pid = Some(
                    val()?
                        .parse()
                        .map_err(|_| "--service-pid needs a pid".to_string())?,
                )
            }
            "--agent-report" => cfg.agent_report = Some(PathBuf::from(val()?)),
            "--canary-file" => cfg.canary_file = Some(PathBuf::from(val()?)),
            "--log-file" => cfg.log_files.push(PathBuf::from(val()?)),
            "--service-unit" => opt.service_unit = val()?,
            "--hardlink-source" => opt.hardlink_source = Some(PathBuf::from(val()?)),
            "--extra-read" => opt.extra_read.push(PathBuf::from(val()?)),
            "--desktop-uid" => {
                opt.desktop_uid = Some(
                    val()?
                        .parse()
                        .map_err(|_| "--desktop-uid needs a uid".to_string())?,
                )
            }
            "-h" | "--help" => return Err(String::new()),
            other => return Err(format!("unknown argument {other}")),
        }
    }
    cfg.scratch = scratch.unwrap_or_else(|| cfg.state_root.join("run"));
    Ok((mode, cfg, opt))
}

fn emit(v: &json::Value) -> Result<(), ExitCode> {
    match json::canonical(v) {
        Ok(text) => {
            println!("{text}");
            Ok(())
        }
        Err(e) => {
            eprintln!("writ-probe: {}", e.code());
            Err(ExitCode::from(2))
        }
    }
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let (mode, cfg, mut opt) = match parse(&args) {
        Ok(x) => x,
        Err(e) => {
            if !e.is_empty() {
                eprintln!("writ-probe: {e}");
            }
            eprintln!("{USAGE}");
            return ExitCode::from(2);
        }
    };
    if mode == "agent-deny" {
        let Some(pid) = cfg.service_pid else {
            eprintln!("writ-probe: agent-deny needs --service-pid\n{USAGE}");
            return ExitCode::from(2);
        };
        opt.service_pid = pid;
        return emit(&agent::report(&cfg, &opt)).map_or_else(|c| c, |()| ExitCode::SUCCESS);
    }
    let versions = facts::collect(&cfg);
    let results = probes::run_all(&cfg, &versions);
    let ctx = ReportContext {
        versions,
        config: cfg.describe(),
        observed_at: SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(0, |d| i64::try_from(d.as_secs()).unwrap_or(0)),
        uid: rustix::process::getuid().as_raw(),
    };
    match emit(&report(&ctx, &results)) {
        Ok(()) if verdict(&results).governed_ready => ExitCode::SUCCESS,
        Ok(()) => ExitCode::from(1),
        Err(c) => c,
    }
}
