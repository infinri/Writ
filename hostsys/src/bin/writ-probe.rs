//! `writ-probe`: host readiness report (`writ-readiness/1`) and agent-side denial attempts
//! (`writ-agent-deny/1`), written as canonical JSON to stdout (M5a-03, J-9).
//!
//! Exit status: 0 governed-ready, 1 not ready, 2 usage or internal error.

use std::path::PathBuf;
use std::process::ExitCode;
use std::time::{SystemTime, UNIX_EPOCH};
use writ_hostsys::readiness::{report, verdict, ReportContext};
use writ_hostsys::{agent, facts, probes, Config};
use writ_kernel::json;

const USAGE: &str = "usage:
  writ-probe [report] [options]          readiness report (writ-readiness/1)
  writ-probe agent-deny --service-pid N  as the agent uid: denial attempts (writ-agent-deny/1)
options:
  --state-root DIR   (default /var/lib/writ)     --config-dir DIR (default /etc/writ)
  --run-dir DIR      (default /run/writ)         --target-root DIR (default <state-root>/targets)
  --scratch DIR      (default <state-root>/run)  --bwrap PATH (default /usr/bin/bwrap)
  --cgroup-delegated (reference VM only: the probe runs in its own delegated systemd unit)
  --service-pid N    --agent-report FILE  --canary-file FILE  --log-file FILE (repeatable)";

fn parse(args: &[String]) -> Result<(String, Config), String> {
    let mut cfg = Config::reference();
    let mut mode = "report".to_string();
    let (mut target, mut scratch) = (None, None);
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
            "--target-root" => target = Some(PathBuf::from(val()?)),
            "--scratch" => scratch = Some(PathBuf::from(val()?)),
            "--bwrap" => cfg.bwrap = PathBuf::from(val()?),
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
            "-h" | "--help" => return Err(String::new()),
            other => return Err(format!("unknown argument {other}")),
        }
    }
    cfg.target_root = target.unwrap_or_else(|| cfg.state_root.join("targets"));
    cfg.scratch = scratch.unwrap_or_else(|| cfg.state_root.join("run"));
    Ok((mode, cfg))
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let (mode, cfg) = match parse(&args) {
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
        return match json::canonical(&agent::report(&cfg, pid)) {
            Ok(text) => {
                println!("{text}");
                ExitCode::SUCCESS
            }
            Err(e) => {
                eprintln!("writ-probe: {}", e.code());
                ExitCode::from(2)
            }
        };
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
    match json::canonical(&report(&ctx, &results)) {
        Ok(text) => {
            println!("{text}");
            if verdict(&results).governed_ready {
                ExitCode::SUCCESS
            } else {
                ExitCode::from(1)
            }
        }
        Err(e) => {
            eprintln!("writ-probe: {}", e.code());
            ExitCode::from(2)
        }
    }
}
