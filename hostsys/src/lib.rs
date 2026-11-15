//! Writ V2 host readiness (M5a-03, D-045 M5-D2, J-0, J-9): OS capability probes and the
//! `writ-readiness/1` report produced by the `writ-probe` operator binary.
//!
//! Readiness is established only by executed probes on the running host; recorded versions
//! are informational and never an input to the verdict. A host missing any required
//! capability is reported not ready, and governed jobs and writes must be refused there.
//! Landlock is reported but optional.
//!
//! - [`readiness`]: probe ids, results, negative controls, the verdict and the report.
//! - [`probes`]: the J-9 probes (each an observation with a negative control or a documented
//!   reason why none is possible) and their pure evaluation functions.
//! - [`facts`]: recorded host facts and versions.
//! - [`agent`]: the agent-side denial attempts (`writ-probe agent-deny`).
//! - [`exec`]: bounded child-process execution.
//!
//! This crate opens no trusted state (ledger, stores, keys); it contains no business logic,
//! SQL or network listener (J-0).

pub mod agent;
pub mod exec;
pub mod facts;
pub mod probes;
pub mod readiness;

pub use probes::Config;
pub use readiness::{report, verdict, ProbeId, ProbeResult, Status, Verdict};
