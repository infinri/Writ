//! The mechanical runner (M3-07; INV-35, INV-40; D-033 item 4, D-034, D-036;
//! [M3-ACCEPTANCE-CONTRACTS](../../docs/implementation/M3-ACCEPTANCE-CONTRACTS.md)).
//!
//! The runner is a broker effect of kind [`RUNNER_EFFECT_KIND`]: an
//! evaluation runs only through the execute lifecycle (reservation, dispatch,
//! permit, adapter transaction, observation), and its result is a
//! `runner-attestation/1` authenticated with a [`RunnerAttestationKey`]. The
//! agent names what to evaluate; everything the attestation binds is read from
//! trusted state at run time (stored workflow, active acceptance policy,
//! current registry entry, registered command, immutable snapshot).
//!
//! This module defines the shared interfaces (owned centrally; implementations
//! live in their own modules):
//!
//! - [`CommandRegistry`]: operator-registered commands with a content digest
//!   ([`RegisteredCommand::digest`]) compared with the registry entry's
//!   `command.command_digest`.
//! - [`SnapshotContent`]: the bytes of an immutable snapshot, whose digest is
//!   recomputed by the runner with [`snapshot_digest`] after materialization.
//! - [`Sandbox`]: bounded execution with declared [`SandboxGuarantees`]. A
//!   sandbox that cannot establish every guarantee the runner requires makes
//!   the runner refuse (fail closed); see [`RunnerRequirements`].
//!
//! **Formats (implementation choices within D-033/D-036, documented in
//! M3-ACCEPTANCE-CONTRACTS):**
//!
//! - Command digest: D-006 digest of `{"contract":"writ-command/1",
//!   "command_id", "program_digest", "args", "env"}` where `program_digest`
//!   is the SHA-256 of the executable bytes, `args` the argument strings and
//!   `env` the sorted `[name, value]` pairs.
//! - Snapshot digest: D-006 digest of `{"contract":"writ-snapshot-manifest/1",
//!   "files":[{"path", "digest"}...]}` with files sorted by path; paths are
//!   relative, `/`-separated, without empty, `.` or `..` components.
//! - Output digest: D-006 digest of `{"contract":"writ-runner-output/1",
//!   "exit_status", "stdout", "stderr"}` where `stdout` and `stderr` are the
//!   SHA-256 digests of the captured bytes.

use crate::workflow::SourceUnavailable;
use std::path::Path;
use std::time::Duration;
use writ_kernel::json::{self, Digest, Value};

/// The action kind of a runner evaluation.
pub const RUNNER_EFFECT_KIND: &str = "runner.evaluate";

/// A command registered by an authorized operator (never authored by an
/// agent request).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RegisteredCommand {
    /// The registry's `command.command_id`.
    pub command_id: String,
    /// The executable's bytes, materialized into the private work directory
    /// and executed from there.
    pub program: Vec<u8>,
    /// Arguments after the program.
    pub args: Vec<String>,
    /// The complete child environment (nothing is inherited).
    pub env: Vec<(String, String)>,
}

impl RegisteredCommand {
    /// The `writ-command/1` digest (module documentation); `None` if a value
    /// is outside the D-006 domain or an environment name is empty, contains
    /// `=` or NUL, or occurs twice (a duplicate would make two different
    /// environments share one digest). A `#!` script's interpreter is not
    /// covered by this digest; with the uncontained local profile it is the
    /// host's.
    pub fn digest(&self) -> Option<String> {
        let mut names: Vec<&str> = self.env.iter().map(|(k, _)| k.as_str()).collect();
        names.sort_unstable();
        if names.windows(2).any(|w| w[0] == w[1])
            || names
                .iter()
                .any(|n| n.is_empty() || n.contains('=') || n.contains('\0'))
        {
            return None;
        }
        let mut env = self.env.clone();
        env.sort();
        let doc = json::obj([
            ("contract", json::s("writ-command/1")),
            ("command_id", json::s(&self.command_id)),
            (
                "program_digest",
                json::s(Digest::of_bytes(&self.program).as_str()),
            ),
            (
                "args",
                Value::Arr(self.args.iter().map(|a| json::s(a)).collect()),
            ),
            (
                "env",
                Value::Arr(
                    env.iter()
                        .map(|(k, v)| Value::Arr(vec![json::s(k), json::s(v)]))
                        .collect(),
                ),
            ),
        ]);
        json::digest(&doc).ok().map(|d| d.as_str().to_string())
    }
}

/// Operator-registered commands by id, per tenant and project.
pub trait CommandRegistry: Sync {
    /// The command `command_id`, `None` when not registered.
    fn command(
        &self,
        tenant_id: &str,
        project_id: &str,
        command_id: &str,
    ) -> Result<Option<RegisteredCommand>, SourceUnavailable>;
}

/// One file of a snapshot.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SnapshotFile {
    /// Relative `/`-separated path (module documentation).
    pub path: String,
    /// Content.
    pub bytes: Vec<u8>,
}

/// `true` when `path` is a valid snapshot path: non-empty, relative,
/// `/`-separated components that are neither empty, `.` nor `..`, and no NUL.
pub fn valid_snapshot_path(path: &str) -> bool {
    !path.is_empty()
        && !path.contains('\0')
        && path
            .split('/')
            .all(|c| !c.is_empty() && c != "." && c != "..")
}

/// The `writ-snapshot-manifest/1` digest of `files`; `None` for an invalid or
/// duplicate path or a value outside the D-006 domain.
pub fn snapshot_digest(files: &[SnapshotFile]) -> Option<String> {
    let mut entries: Vec<(&str, String)> = Vec::with_capacity(files.len());
    for f in files {
        if !valid_snapshot_path(&f.path) {
            return None;
        }
        entries.push((&f.path, Digest::of_bytes(&f.bytes).as_str().to_string()));
    }
    entries.sort();
    if entries.windows(2).any(|w| w[0].0 == w[1].0) {
        return None;
    }
    let doc = json::obj([
        ("contract", json::s("writ-snapshot-manifest/1")),
        (
            "files",
            Value::Arr(
                entries
                    .iter()
                    .map(|(p, d)| json::obj([("path", json::s(p)), ("digest", json::s(d))]))
                    .collect(),
            ),
        ),
    ]);
    json::digest(&doc).ok().map(|d| d.as_str().to_string())
}

/// The bytes of service-controlled immutable snapshots (INV-40).
pub trait SnapshotContent: Sync {
    /// The files of `snapshot_id`, `None` when there is no such snapshot.
    fn content(
        &self,
        tenant_id: &str,
        project_id: &str,
        snapshot_id: &str,
    ) -> Result<Option<Vec<SnapshotFile>>, SourceUnavailable>;

    /// Whether this store guarantees that stored content cannot change and
    /// cannot be written by the agent. A store that cannot establish this
    /// answers `false` and the runner refuses (`ARTIFACT_INTEGRITY_UNAVAILABLE`).
    fn immutable(&self) -> bool;
}

/// What a sandbox establishes for every job it runs.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct SandboxGuarantees {
    /// The child cannot read or write outside its work directory.
    pub filesystem_isolated: bool,
    /// The child has no network access.
    pub network_isolated: bool,
    /// Every descendant of the child is terminated when the job ends.
    pub process_tree_contained: bool,
    /// CPU, memory and process-count limits are enforced.
    pub resource_limited: bool,
    /// The child inherits no descriptor other than its standard streams, and
    /// no spawn overlaps a ledger or store ownership change in this process.
    pub descriptors_closed: bool,
    /// Wall-time and output limits are enforced (the child is killed at the
    /// deadline; output beyond the limit fails the job).
    pub bounded: bool,
}

impl SandboxGuarantees {
    /// `true` when every guarantee in `required` is present here.
    pub fn satisfies(&self, required: &SandboxGuarantees) -> bool {
        let have = [
            self.filesystem_isolated,
            self.network_isolated,
            self.process_tree_contained,
            self.resource_limited,
            self.descriptors_closed,
            self.bounded,
        ];
        let need = [
            required.filesystem_isolated,
            required.network_isolated,
            required.process_tree_contained,
            required.resource_limited,
            required.descriptors_closed,
            required.bounded,
        ];
        have.iter().zip(need).all(|(h, n)| *h || !n)
    }

    /// Every guarantee.
    pub const fn all() -> SandboxGuarantees {
        SandboxGuarantees {
            filesystem_isolated: true,
            network_isolated: true,
            process_tree_contained: true,
            resource_limited: true,
            descriptors_closed: true,
            bounded: true,
        }
    }
}

/// The guarantees the runner requires before it runs anything.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct RunnerRequirements {
    /// Required sandbox guarantees (private: the only constructors are
    /// [`RunnerRequirements::production`] and, in test builds, the uncontained
    /// test profile).
    sandbox: SandboxGuarantees,
}

impl RunnerRequirements {
    /// The supported configuration: every guarantee. No M3 sandbox provides
    /// all of them, so a runner with these requirements refuses every job
    /// (fail closed) until a contained sandbox exists (M5).
    pub const fn production() -> RunnerRequirements {
        RunnerRequirements {
            sandbox: SandboxGuarantees::all(),
        }
    }

    /// **Unsupported configuration, for disposable test resources only:**
    /// requires bounded execution and closed descriptors but accepts a
    /// sandbox without filesystem, network, process-tree or resource
    /// containment. Attestations produced under it bind the sandbox's
    /// environment digest, so a registry entry published for a contained
    /// environment never accepts them. A job run under it has the broker's
    /// full user privileges, including over the ledger, store and key
    /// material, so it is compiled only for the broker's own tests and for
    /// crates that enable the `uncontained-test-runner` feature, which only
    /// `writ-ledger-sqlite` does, as a dev-dependency (never in a normal
    /// build).
    #[cfg(any(test, feature = "uncontained-test-runner"))]
    pub const fn uncontained_for_disposable_tests() -> RunnerRequirements {
        RunnerRequirements {
            sandbox: SandboxGuarantees {
                filesystem_isolated: false,
                network_isolated: false,
                process_tree_contained: false,
                resource_limited: false,
                descriptors_closed: true,
                bounded: true,
            },
        }
    }

    /// The required sandbox guarantees.
    pub const fn sandbox(&self) -> SandboxGuarantees {
        self.sandbox
    }
}

/// Bounds of one job.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Limits {
    /// Wall-time limit; the child is killed when it is reached.
    pub wall_time: Duration,
    /// Limit on stdout plus stderr bytes.
    pub max_output_bytes: usize,
}

/// One job: a materialized program run in a private work directory.
#[derive(Clone, Copy, Debug)]
pub struct Job<'a> {
    /// The materialized executable (inside `workdir`).
    pub program: &'a Path,
    /// Arguments.
    pub args: &'a [String],
    /// The complete environment.
    pub env: &'a [(String, String)],
    /// The private work directory (the materialized snapshot).
    pub workdir: &'a Path,
    /// Bounds.
    pub limits: Limits,
}

/// A job that ran to completion within its bounds.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct JobOutput {
    /// Exit status 0..=255.
    pub exit_status: u8,
    /// Captured stdout.
    pub stdout: Vec<u8>,
    /// Captured stderr.
    pub stderr: Vec<u8>,
}

/// Why a job did not complete (no attestation is produced for any of these).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum JobFailure {
    /// The sandbox cannot run jobs now.
    Unavailable,
    /// The program could not be started.
    Spawn,
    /// The wall-time limit was reached; the child was killed.
    TimedOut,
    /// The output limit was exceeded; the child was killed.
    OutputExceeded,
    /// The child ended by a signal.
    Signaled,
    /// Waiting for or reading from the child failed.
    Io,
}

/// The sandbox cannot report its guarantees (fail closed).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct SandboxUnavailable;

/// Bounded execution of jobs.
pub trait Sandbox: Sync {
    /// The guarantees this sandbox establishes on this host now.
    fn guarantees(&self) -> Result<SandboxGuarantees, SandboxUnavailable>;

    /// The `environment_digest` of jobs run by this sandbox: identifies its
    /// profile and guarantees, compared with the registry entry.
    fn environment_digest(&self) -> String;

    /// Run `job` to completion within its limits.
    fn run(&self, job: &Job<'_>) -> Result<JobOutput, JobFailure>;
}

/// The `writ-runner-output/1` digest of a completed job (module documentation).
pub fn output_digest(output: &JobOutput) -> Option<String> {
    let doc = json::obj([
        ("contract", json::s("writ-runner-output/1")),
        ("exit_status", Value::Int(i64::from(output.exit_status))),
        ("stdout", json::s(Digest::of_bytes(&output.stdout).as_str())),
        ("stderr", json::s(Digest::of_bytes(&output.stderr).as_str())),
    ]);
    json::digest(&doc).ok().map(|d| d.as_str().to_string())
}

mod adapter;
pub use adapter::{
    attestation_id_for, LedgerClock, ProducedAttestation, RunOutcome, RunRecord, RunState,
    RunnerAdapter, RunnerConfig, RunnerDiagnostic, RunnerSources, RunnerState, RunnerStore,
    PROGRAM_NAME,
};

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests;
