//! `writ-serviced` (M5a-04; J-1, J-2): the single-owner trusted service.
//!
//! Usage: `writ-serviced --config <path>` serves; `writ-serviced --init --config <path>`
//! creates the ledger and the snapshot store; `writ-serviced --print-static-facts --config
//! <path>` prints the `writ-static-facts/1` object for the configured fact plan, observed as
//! this process (the operator runs it as the service user and records it in the readiness
//! report, J-9). No other argument, no environment variable, no key on the command line.
//!
//! Order when serving, each step failing closed with a non-zero exit before the next:
//! 1. umask 0077 (J-2);
//! 2. configuration (`writ-service-config/1`; exit 2 when refused);
//! 3. the J-1 startup identity check: real and effective uid and gid equal the configured
//!    service ids, neither is 0, no supplementary group (exit 3);
//! 4. open and own the ledger, stores and keys ([`writ_service::Service::open`]);
//! 5. bind and serve `host.sock`, `admin.sock` and the approval listener; announce on
//!    standard output and serve until terminated (signals are M5a-11).

use std::io::Write;
use std::path::PathBuf;
use std::process::ExitCode;

use writ_hostsys::deployment::{observe, static_facts_value};
use writ_hostsys::sock::{check_service_identity, process_identity, restrict_umask};
use writ_service::config::ServiceConfig;
use writ_service::Service;

const USAGE: &str = "usage: writ-serviced [--init | --print-static-facts] --config <path>";

enum Mode {
    Serve,
    Init,
    PrintFacts,
}

fn args() -> Option<(Mode, PathBuf)> {
    let mut mode = Mode::Serve;
    let mut config = None;
    let mut it = std::env::args_os().skip(1);
    let mut mode_set = false;
    while let Some(a) = it.next() {
        match a.to_str()? {
            "--config" if config.is_none() => config = Some(PathBuf::from(it.next()?)),
            "--init" if !mode_set => {
                mode = Mode::Init;
                mode_set = true;
            }
            "--print-static-facts" if !mode_set => {
                mode = Mode::PrintFacts;
                mode_set = true;
            }
            _ => return None,
        }
    }
    Some((mode, config?))
}

fn main() -> ExitCode {
    restrict_umask();
    let Some((mode, path)) = args() else {
        eprintln!("writ-serviced: {USAGE}");
        return ExitCode::from(2);
    };
    let config = match ServiceConfig::load(&path) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("writ-serviced: {e}");
            return ExitCode::from(2);
        }
    };
    if let Mode::PrintFacts = mode {
        let Some(d) = &config.deployment else {
            eprintln!("writ-serviced: no deployment fact plan configured");
            return ExitCode::from(2);
        };
        let text =
            writ_broker::writ_kernel::json::canonical(&static_facts_value(&observe(&d.plan)));
        return match text {
            Ok(t) => {
                println!("{t}");
                ExitCode::SUCCESS
            }
            Err(_) => ExitCode::from(1),
        };
    }
    let identity = match process_identity() {
        Ok(i) => i,
        Err(e) => {
            eprintln!("writ-serviced: identity unavailable: {}", e.kind());
            return ExitCode::from(3);
        }
    };
    if let Err(why) = check_service_identity(&identity, config.service_uid, config.service_gid) {
        eprintln!("writ-serviced: refusing to run: startup identity check failed ({why:?})");
        return ExitCode::from(3);
    }
    if let Mode::Init = mode {
        return match Service::initialize(&config) {
            Ok(()) => ExitCode::SUCCESS,
            Err(e) => {
                eprintln!("writ-serviced: init refused: {e}");
                ExitCode::from(1)
            }
        };
    }
    let service = match Service::open(config) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("writ-serviced: not serving: {e}");
            return ExitCode::from(1);
        }
    };
    let label = service.config().label(service.deployment_verified());
    let running = match service.serve() {
        Ok(r) => r,
        Err(e) => {
            eprintln!("writ-serviced: not serving: {e}");
            return ExitCode::from(1);
        }
    };
    let mut out = std::io::stdout().lock();
    let _ = writeln!(out, "writ-serviced: serving ({label})").and_then(|()| out.flush());
    drop(out);
    loop {
        std::thread::park();
        let _ = &running;
    }
}
