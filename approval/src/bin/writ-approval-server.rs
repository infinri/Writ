//! `writ-approval-server`: the production entry point of the local approval channel (Y3,
//! D-043; D-040 O-1, O-2, O-11, O-12; approval/README.md).
//!
//! Order, each step failing closed with a non-zero exit before the next:
//! 1. parse and validate the required configuration ([`writ_approval::server`]; exit 2);
//! 2. build the reviewed `webauthn-rs` [`PasskeyVerifier`] for RP id `localhost` and origin
//!    `http://localhost:<port>` and the [`ApprovalChannel`] over it (the only verifier this
//!    binary can build: there is no test verifier, switch or fallback);
//! 3. open the existing ledger through the normal ownership path ([`SqliteLedger::open`]:
//!    canonical identity, sidecar ownership lock, verification, owner epoch) and attach the
//!    channel once;
//! 4. start the trusted host clock against the ledger's persisted floor (a blocked startup
//!    rollback is refused);
//! 5. bind the loopback listener on `127.0.0.1:<port>`, check that its fixed origin and RP
//!    id are the configured ones, announce readiness on standard output and serve until the
//!    process is terminated (ownership is released by the operating system at exit).

use std::io::Write;
use std::process::ExitCode;
use std::sync::atomic::AtomicBool;
use std::sync::Arc;

use writ_approval::backend::LedgerBackend;
use writ_approval::listener::{ApprovalListener, Limits, ListenerConfig};
use writ_approval::server::{ServerConfig, USAGE};
use writ_approval::verifier::PasskeyVerifier;
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
    match run(&config) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("writ-approval-server: not serving: {e}");
            ExitCode::from(1)
        }
    }
}

fn run(c: &ServerConfig) -> Result<(), String> {
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
    listener
        .serve(Arc::new(AtomicBool::new(false)))
        .map_err(|e| format!("serve: {}", e.kind()))
}
