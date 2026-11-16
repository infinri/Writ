//! The serving path of `writ-serviced` after its startup identity check (M5a-04, M5a-11;
//! M5-INTERFACES J-1, J-8). `main` checks the process identity and then calls [`run`]; the
//! signal tests call [`run`] in a child process (on a workstation account the identity check
//! refuses, J-1), so the real serving and shutdown code runs under real signals.
//!
//! Order, each step failing closed (exit 1) before the next:
//! 1. with socket activation configured, take over and verify the inherited sockets
//!    ([`Inherited::take`]) before anything is opened;
//! 2. open and own the ledger, stores and keys ([`Service::open`]);
//! 3. the start-time sweep, when configured ([`crate::sweep`]);
//! 4. serve; announce `writ-serviced: serving (<label>)` on standard output;
//! 5. at the first `SIGTERM` or `SIGINT`: arm the watchdog ([`shutdown::arm`]: a second
//!    signal exits 4, the hard deadline exits 5), then [`crate::Running::stop_gracefully`]
//!    and drop the service (stores, then the ledger, lock last); exit 0.

use std::io::Write;
use std::sync::mpsc::{Receiver, RecvTimeoutError};
use std::time::Duration;

use writ_hostsys::sock::ListenEnv;

use crate::config::ServiceConfig;
use crate::shutdown::{self, Stop};
use crate::{Inherited, Running, Service, Sockets};

/// Exit status of a start refused at or after opening the stores.
pub const EXIT_NOT_SERVING: u8 = 1;

/// Serve `config` until the first signal on `signals`, then shut down (module
/// documentation). `listen` is the socket-activation environment (read once by `main`).
pub fn run(config: ServiceConfig, signals: Receiver<Stop>, listen: &ListenEnv) -> u8 {
    run_with(config, signals, listen, |_| {})
}

/// [`run`] with `serving` called once the service serves, before the announcement (the
/// signal tests use it to hold a request open or a store handle).
pub fn run_with(
    config: ServiceConfig,
    signals: Receiver<Stop>,
    listen: &ListenEnv,
    serving: impl FnOnce(&Running),
) -> u8 {
    let sockets = if config.socket_activation {
        match Inherited::take(&config, listen) {
            Ok(i) => Sockets::Inherited(i),
            Err(e) => {
                eprintln!("writ-serviced: not serving: {e}");
                return EXIT_NOT_SERVING;
            }
        }
    } else {
        Sockets::Bind
    };
    let drain = config.drain;
    let mut service = match Service::open(config) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("writ-serviced: not serving: {e}");
            return EXIT_NOT_SERVING;
        }
    };
    if let Some(r) = service.sweep() {
        eprintln!(
            "writ-serviced: start sweep: jobs populated {:?}, removed {:?}, kept {:?}",
            r.jobs_populated, r.removed, r.kept
        );
    }
    let label = service.config().label(service.deployment_verified());
    let running = match service.serve_with(sockets) {
        Ok(r) => r,
        Err(e) => {
            eprintln!("writ-serviced: not serving: {e}");
            return EXIT_NOT_SERVING;
        }
    };
    serving(&running);
    let mut out = std::io::stdout().lock();
    let _ = writeln!(out, "writ-serviced: serving ({label})").and_then(|()| out.flush());
    drop(out);
    let first = loop {
        match signals.recv_timeout(Duration::from_secs(1)) {
            Ok(s) => break s,
            Err(RecvTimeoutError::Timeout) => {}
            Err(RecvTimeoutError::Disconnected) => {
                eprintln!("writ-serviced: signal handling stopped; shutting down");
                break Stop::Terminate;
            }
        }
    };
    eprintln!(
        "writ-serviced: {first:?}: stopping (drain {} s)",
        drain.as_secs()
    );
    let hard = match shutdown::arm(signals, drain) {
        Ok(h) => h,
        Err(e) => {
            eprintln!(
                "writ-serviced: watchdog unavailable ({:?}); exiting",
                e.kind()
            );
            return shutdown::EXIT_HARD_DEADLINE;
        }
    };
    let stopped = running.stop_gracefully(drain, hard);
    let Some(service) = stopped.service else {
        eprintln!("writ-serviced: a task still holds a store handle at the hard deadline");
        return shutdown::EXIT_HARD_DEADLINE;
    };
    drop(service);
    eprintln!(
        "writ-serviced: stopped (drained {}, jobs ended {}); ownership released",
        stopped.drained, stopped.jobs_ended
    );
    0
}
