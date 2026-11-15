//! Writ V2 host readiness (M5a-03, D-045 M5-D2, J-0, J-9 revision 2): OS capability probes
//! and the `writ-readiness/1` report produced by the `writ-probe` operator binary.
//!
//! Readiness is established only by executed probes on the running host; recorded versions
//! are informational and never an input to the verdict. A host missing any required
//! capability is reported not ready, and governed jobs and writes must be refused there.
//! Landlock is reported but optional.
//!
//! - [`readiness`]: probe ids, results, negative controls, the verdict and the report.
//! - [`probes`]: the sandbox, cgroup and filesystem probes and their pure evaluation functions.
//! - [`deny`]: identity, permission and secret probes (agent user namespaces, ACLs, hard links,
//!   peer credentials, agent denial, secrets) and the J-1 layout facts.
//! - [`facts`]: recorded host facts and versions.
//! - [`agent`]: the agent-side attempts (`writ-probe agent-deny`).
//! - [`exec`]: bounded child-process execution.
//!
//! J-0 lists `writ-containment` as a dependency of `writ-probe`; that crate does not exist yet
//! (M5a-07), so the probe binary lives in this crate for now. This crate opens no trusted state
//! for writing; it contains no business logic, SQL or network listener (the peer-credential
//! probe binds a private abstract socket and connects to itself).

pub mod agent;
pub mod deny;
pub mod exec;
pub mod facts;
pub mod probes;
pub mod readiness;

pub use probes::Config;
pub use readiness::{report, verdict, ProbeId, ProbeResult, Status, Verdict};
