//! Signals and the hard deadline of the bounded graceful shutdown (M5-INTERFACES J-8; D-045
//! M5-D16; M5a-11).
//!
//! [`signals`] registers `SIGTERM` and `SIGINT` through Tokio `signal` on a current-thread
//! runtime owned by a dedicated thread, and reports each delivery on a channel. The entry
//! point registers them before it opens the ledger, so a signal at any later point is seen.
//! [`arm`] is called at the first signal: it starts the watchdog that ends the process at
//! once, non-zero, on a second signal ([`EXIT_SECOND_SIGNAL`]) or when the hard deadline
//! passes ([`EXIT_HARD_DEADLINE`]). The operating system then releases the ownership lock;
//! every committed state is durable and uncertain outcomes stay uncertain (nothing is
//! rewritten on that path).
//!
//! `writ-approval` never handles signals (its source never uses `tokio::signal`; dependency
//! gate source rule).

use std::io;
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::task::Poll;
use std::thread;
use std::time::{Duration, Instant};

use tokio::signal::unix::{signal, SignalKind};

/// Exit status when a second signal arrives during the shutdown.
pub const EXIT_SECOND_SIGNAL: u8 = 4;
/// Exit status when the hard deadline (drain plus [`GRACE`]) passes.
pub const EXIT_HARD_DEADLINE: u8 = 5;
/// Default drain period (J-8: default 10 s).
pub const DEFAULT_DRAIN: Duration = Duration::from_secs(10);
/// Longest drain period (J-8: maximum 30 s).
pub const MAX_DRAIN: Duration = Duration::from_secs(30);
/// Time after the drain period before the hard deadline (J-8: drain plus 5 s).
pub const GRACE: Duration = Duration::from_secs(5);

/// The signal that started a delivery.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Stop {
    /// `SIGTERM`.
    Terminate,
    /// `SIGINT`.
    Interrupt,
}

/// Register `SIGTERM` and `SIGINT` now and report every delivery on the returned channel.
pub fn signals() -> io::Result<Receiver<Stop>> {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_io()
        .build()?;
    let (mut term, mut int) = runtime.block_on(async {
        Ok::<_, io::Error>((
            signal(SignalKind::terminate())?,
            signal(SignalKind::interrupt())?,
        ))
    })?;
    let (tx, rx) = mpsc::channel();
    thread::Builder::new()
        .name("writ-signals".into())
        .spawn(move || {
            runtime.block_on(async move {
                loop {
                    let got = std::future::poll_fn(|cx| {
                        if let Poll::Ready(r) = term.poll_recv(cx) {
                            return Poll::Ready(r.map(|()| Stop::Terminate));
                        }
                        if let Poll::Ready(r) = int.poll_recv(cx) {
                            return Poll::Ready(r.map(|()| Stop::Interrupt));
                        }
                        Poll::Pending
                    })
                    .await;
                    match got {
                        Some(s) if tx.send(s).is_ok() => {}
                        _ => return,
                    }
                }
            });
        })?;
    Ok(rx)
}

/// Start the watchdog for a shutdown that began now with drain period `drain`: a second
/// signal on `rx` ends the process with [`EXIT_SECOND_SIGNAL`]; reaching `drain` plus
/// [`GRACE`] ends it with [`EXIT_HARD_DEADLINE`].
pub fn arm(rx: Receiver<Stop>, drain: Duration) -> io::Result<Instant> {
    let hard = Instant::now() + drain + GRACE;
    thread::Builder::new()
        .name("writ-shutdown-watchdog".into())
        .spawn(move || {
            let left = hard.saturating_duration_since(Instant::now());
            let code = match rx.recv_timeout(left) {
                Ok(s) => {
                    eprintln!("shutdown: second signal ({s:?}); exiting now");
                    EXIT_SECOND_SIGNAL
                }
                Err(RecvTimeoutError::Timeout) | Err(RecvTimeoutError::Disconnected) => {
                    // A closed channel is waited out to the deadline (never an early exit).
                    thread::sleep(hard.saturating_duration_since(Instant::now()));
                    eprintln!("shutdown: hard deadline passed; exiting now");
                    EXIT_HARD_DEADLINE
                }
            };
            std::process::exit(i32::from(code));
        })?;
    Ok(hard)
}
