//! `writ-approval-server`: the production entry point of the local approval channel (Y3,
//! D-043; D-040 O-1, O-2, O-11, O-12; approval-server/README.md).
//!
//! Order, each step failing closed with a non-zero exit before the next:
//! 1. parse and validate the required configuration ([`writ_approval_server`]; exit 2);
//! 2. build the reviewed `webauthn-rs` [`PasskeyVerifier`] for RP id `localhost` and origin
//!    `http://localhost:<port>` and the [`ApprovalChannel`] over it (the only verifier this
//!    binary can build: there is no test verifier, switch or fallback);
//! 3. open the existing ledger through the normal ownership path ([`SqliteLedger::open`]:
//!    canonical identity, sidecar ownership lock, verification, owner epoch) and attach the
//!    channel once;
//! 4. start the trusted host clock against the ledger's persisted floor (a blocked startup
//!    rollback is refused);
//! 5. bind the loopback listener on `127.0.0.1:<port>`, check that its fixed origin and RP
//!    id are the configured ones, announce readiness on standard output and serve.
//!
//! `SIGTERM` and `SIGINT` are registered before step 3 ([`shutdown::signals`]). The bounded
//! graceful shutdown (M5-INTERFACES J-8; M5a-11), at the first signal:
//! 1. **stop accepting work:** the listening socket stays open and every new request is
//!    answered `503` `stopping`; no ceremony, login or decision begins;
//! 2. **drain** (`--drain-seconds`, default 10, at most 30): requests in progress finish;
//!    ceremonies not finished are left (their challenges stay issued and expire);
//! 3. **release:** the accept loop ends, the listener's runtime (and with it every
//!    remaining connection) is dropped, the serving thread is joined, then the backend, the
//!    clock and the ledger are dropped (the ownership lock is unlocked before close under
//!    the lifecycle gate); exit 0;
//! 4. a **second signal** exits at once with status 4, the **hard deadline** (drain plus
//!    5 s) with status 5; the operating system releases the lock and nothing committed is
//!    rewritten.

use std::io::Write;
use std::process::ExitCode;
use std::sync::atomic::AtomicBool;
use std::sync::mpsc::{Receiver, RecvTimeoutError};
use std::sync::Arc;
use std::time::{Duration, Instant};

use writ_approval::backend::LedgerBackend;
use writ_approval::listener::{ApprovalListener, Limits, ListenerConfig};
use writ_approval::verifier::PasskeyVerifier;
use writ_approval_server::shutdown::{self, Stop};
use writ_approval_server::{ServerConfig, USAGE};
use writ_broker::clock::{Boottime, HostClock, SystemWall};
use writ_broker::human::ApprovalChannel;
use writ_ledger_sqlite::SqliteLedger;

fn main() -> ExitCode {
    let config = match ServerConfig::from_args(std::env::args_os().skip(1)) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("writ-approval-server: configuration refused: {e}\n{USAGE}");
            return ExitCode::from(2);
        }
    };
    let signals = match shutdown::signals() {
        Ok(rx) => rx,
        Err(e) => {
            eprintln!(
                "writ-approval-server: not serving: signal handling: {}",
                e.kind()
            );
            return ExitCode::from(1);
        }
    };
    match run(&config, signals) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("writ-approval-server: not serving: {e}");
            ExitCode::from(1)
        }
    }
}

fn run(c: &ServerConfig, signals: Receiver<Stop>) -> Result<(), String> {
    let verifier = Arc::new(
        PasskeyVerifier::new(&c.rp_id, &c.origin)
            .map_err(|e| format!("passkey verifier: {e:?}"))?,
    );
    let channel = ApprovalChannel::new(verifier.clone(), verifier, &c.channel)
        .ok_or_else(|| "approval channel: refused".to_string())?;
    let ledger = SqliteLedger::open(&c.ledger)
        .and_then(|l| l.with_approval_channel(channel))
        .map_err(|e| format!("ledger: {e:?}"))?;
    let clock = {
        let mut store: &SqliteLedger = &ledger;
        HostClock::start(SystemWall, Boottime, &mut store)
            .map_err(|e| format!("trusted clock: {e:?}"))?
    };
    if clock.is_blocked() {
        return Err("trusted clock: startup rollback unacknowledged".into());
    }
    let backend = Arc::new(LedgerBackend::new(Arc::new(ledger), Arc::new(clock)));
    let listener = ApprovalListener::bind(
        ListenerConfig {
            port: c.port,
            subject_issuer: c.subject_issuer.clone(),
            limits: Limits::default(),
        },
        backend,
    )
    .map_err(|e| format!("bind 127.0.0.1:{}: {}", c.port, e.kind()))?;
    if listener.origin() != c.origin || listener.rp_id() != c.rp_id {
        return Err("listener origin or RP id differs from the configuration".into());
    }
    let mut out = std::io::stdout().lock();
    writeln!(
        out,
        "writ-approval-server: serving {} (rp id {}, channel {}, verifier webauthn-rs passkey)",
        c.origin, c.rp_id, c.channel
    )
    .and_then(|()| out.flush())
    .map_err(|e| format!("stdout: {}", e.kind()))?;
    drop(out);
    let control = listener.control();
    let server = std::thread::spawn(move || listener.serve(Arc::new(AtomicBool::new(false))));
    // Serve until the first signal (or until the listener fails).
    let first = loop {
        match signals.recv_timeout(Duration::from_millis(100)) {
            Ok(s) => break s,
            Err(RecvTimeoutError::Timeout) if !server.is_finished() => {}
            Err(_) => {
                return match server.join() {
                    Ok(Err(e)) => Err(format!("serve: {}", e.kind())),
                    _ => Err("serve: the listener stopped".into()),
                };
            }
        }
    };
    // J-8 step 1: stop accepting work (the socket stays open, new requests answer 503).
    control.stop_accepting_work();
    eprintln!(
        "writ-approval-server: {first:?}: stopping (drain {} s)",
        c.drain.as_secs()
    );
    shutdown::arm(signals, c.drain).map_err(|e| format!("watchdog: {}", e.kind()))?;
    // Step 2: drain the requests in progress.
    let drained = control.wait_idle(Instant::now() + c.drain);
    // Step 3: release; the ledger is dropped with the listener's backend when the serving
    // thread returns (the hard deadline bounds this join).
    control.exit();
    let served = server.join();
    eprintln!("writ-approval-server: stopped (drained {drained}); ownership released");
    match served {
        Ok(Ok(())) => Ok(()),
        Ok(Err(e)) => Err(format!("serve: {}", e.kind())),
        Err(_) => Err("serve: the listener panicked".into()),
    }
}
