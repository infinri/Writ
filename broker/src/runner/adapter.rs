//! The runner effect adapter (M3-07; INV-35, INV-40; D-033 item 4, D-035
//! items 2 to 4, D-036).
//!
//! [`RunnerAdapter`] implements [`EffectAdapter`] for requests of kind
//! [`RUNNER_EFFECT_KIND`] over its own durable [`RunnerStore`] (separate from
//! the broker ledger, bound to one ledger, like the controlled effect store).
//!
//! **Apply** ([`EffectAdapter::apply`]), at most one run per permit:
//!
//! 1. **T-start** ([`RunnerStore::start`]): begin; the broker's verification
//!    (`verify`); `PERMIT_REPLAYED` when the verified permit id already has a
//!    run; otherwise the permit id is recorded as consumed with state
//!    `started`; commit. A failure here is the outer `Err` (nothing ran).
//! 2. Outside any store transaction: the trusted inputs are read and checked,
//!    the snapshot and program are materialized into a fresh private work
//!    directory, the job runs in the [`Sandbox`], the attestation is built,
//!    authenticated and verified. The work directory is removed afterwards.
//! 3. **T-finish** ([`RunnerStore::finish`]): the `started` run becomes
//!    `applied` with its attestation, or `failed` with an operator
//!    [`RunnerDiagnostic`] (no attestation).
//!
//! A run that is `started` without an outcome (a crash or a T-finish failure
//! after the job) is never run again: the permit stays consumed, and
//! [`EffectAdapter::evidence`] and [`EffectAdapter::inspect`] report no
//! completion evidence, so the receipt stays uncertain and reconciliation
//! records nothing for it (D-035 item 3).
//!
//! **Trusted inputs.** The request names what to evaluate (attributes
//! `workflow_id`, `from_state`, `to_state`, `criterion_id`,
//! `acceptance_record_id`, `evaluator_id`, a closed set; the resource must be
//! the workflow's canonical target and no capability may be requested).
//! Everything the attestation binds is read at run time: the stored workflow
//! (task, state, revision, snapshot id and digest; it must be in
//! `from_state`), the project's active acceptance policy (id, version, bundle
//! digest; the criterion must be one of its mechanical criteria), the current
//! registry entry of the evaluator (active, in the tenant and project scope,
//! class `mechanical`, trust `M`, the criterion listed, a command whose digest
//! equals [`RegisteredCommand::digest`] of the registered command, an
//! environment digest equal to [`Sandbox::environment_digest`]), the runner
//! key named by the entry (active), and the immutable snapshot content
//! ([`SnapshotContent::immutable`] must hold). The sandbox must establish
//! every guarantee of the configured [`RunnerRequirements`].
//!
//! **Materialization.** A fresh directory `run-<permit id>` under the
//! configured work root (mode 0700; it must not exist), the snapshot files
//! (read-only, parent directories 0700) and the program at
//! [`PROGRAM_NAME`] (mode 0500). The snapshot digest is recomputed from the
//! bytes read back from the work directory and must equal the workflow's
//! artifact digest; the program read back must equal the registered bytes.
//!
//! **Attestation.** Every `runner-attestation/1` member; `attestation_id` is
//! [`attestation_id_for`] the permit id; `input_digests` is the artifact
//! digest; `evidence_digests` is empty (the evidence catalog is M7);
//! `output_digest` is [`output_digest`]; `started_at` and `finished_at` are
//! trusted time (whole seconds, rounded down) read through the configured
//! clock (the broker's floor-persisting [`LedgerClock`] in production);
//! `fresh_until` is `finished_at` plus the configured freshness; `auth` is
//! HMAC-SHA256 under the runner key over [`crate::acceptance::mac_bytes`].
//! The document must pass [`RunnerAttestation::validate`] and
//! [`verify_attestation`] before it is stored. A nonzero exit status still
//! produces an attestation (the promotion refuses it, `ATTESTATION_FAILED`).

use std::fs::{self, DirBuilder, OpenOptions};
use std::io::Write;
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::time::Duration;

use writ_kernel::contracts::{AcceptanceRequirements, CriterionClass};
use writ_kernel::ids::{is_id, is_name};
use writ_kernel::json::{self, Digest, Value};
use writ_kernel::time::Timestamp;

use super::{
    output_digest, snapshot_digest, valid_snapshot_path, CommandRegistry, Job, JobFailure, Limits,
    RunnerRequirements, Sandbox, SnapshotContent, SnapshotFile, RUNNER_EFFECT_KIND,
};
use crate::acceptance::{mac_bytes, verify_attestation, ATTESTATION_PURPOSE, MAC_ALG};
use crate::clock::{observe, timestamp_from_parts, ClockError, EffectiveClock, TrustedClock};
use crate::contracts::{RegistryEntry, RunnerAttestation};
use crate::effects::{EffectAdapter, EffectEvidence, EffectResult, VerifiedEffect, Verify};
use crate::keys::{KeyStatus, RunnerKeyRing};
use crate::mac;
use crate::refusal::Refusal;
use crate::store::{LedgerStore, TxFailure};
use crate::workflow::{SourceUnavailable, StoredEntry, StoredPolicy, StoredWorkflow, WorkflowKey};

/// The file name of the materialized program inside the work directory (a
/// snapshot path whose first component is this name is refused).
pub const PROGRAM_NAME: &str = ".writ-runner-program";

/// The attestation id of the run of `permit_id` (`att-<permit id>`): unique
/// per ledger because permit ids are, and known to the caller from the
/// permit it was given.
pub fn attestation_id_for(permit_id: &str) -> String {
    format!("att-{permit_id}")
}

/// Trusted ledger state the runner reads at run time (never the request).
pub trait RunnerState: Sync {
    /// The stored workflow, `None` when unknown.
    fn workflow(&self, key: &WorkflowKey) -> Result<Option<StoredWorkflow>, SourceUnavailable>;

    /// The project's active acceptance policy, `None` when none is active.
    fn acceptance_policy(
        &self,
        tenant_id: &str,
        project_id: &str,
    ) -> Result<Option<StoredPolicy>, SourceUnavailable>;

    /// The current registry entry of `evaluator_id` in the tenant and
    /// project, `None` when not registered.
    fn registry_entry(
        &self,
        tenant_id: &str,
        project_id: &str,
        evaluator_id: &str,
    ) -> Result<Option<StoredEntry>, SourceUnavailable>;
}

/// The state of one run in the runner store.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RunState {
    /// The permit is consumed and the run may have started; no outcome is
    /// recorded (never run again; no completion evidence).
    Started,
    /// The run completed and its attestation is stored.
    Applied,
    /// Nothing usable was produced; the diagnostic says why.
    Failed,
}

impl RunState {
    /// `started`, `applied` or `failed`.
    pub fn as_str(self) -> &'static str {
        match self {
            RunState::Started => "started",
            RunState::Applied => "applied",
            RunState::Failed => "failed",
        }
    }

    /// The state named `text`.
    pub fn parse(text: &str) -> Option<RunState> {
        match text {
            "started" => Some(RunState::Started),
            "applied" => Some(RunState::Applied),
            "failed" => Some(RunState::Failed),
            _ => None,
        }
    }
}

/// The runner store's durable record of one consumed permit.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RunRecord {
    /// The consumed permit id.
    pub permit_id: String,
    /// Tenant of the request it was consumed for.
    pub tenant_id: String,
    /// Idempotency key of that request.
    pub idempotency_key: String,
    /// D-012 fingerprint of that request.
    pub request_fingerprint: String,
    /// The run's state.
    pub state: RunState,
    /// The stored attestation's id (`applied` only).
    pub attestation_id: Option<String>,
    /// The operator diagnostic code (`failed` only).
    pub diagnostic: Option<String>,
}

impl RunRecord {
    /// Completion evidence for reconciliation: `applied` or `failed`; a
    /// `started` run is no evidence (the receipt stays uncertain).
    pub fn evidence(&self) -> Option<EffectEvidence> {
        let result = match self.state {
            RunState::Started => return None,
            RunState::Applied => EffectResult::Applied,
            RunState::Failed => EffectResult::Failed,
        };
        Some(EffectEvidence {
            permit_id: self.permit_id.clone(),
            tenant_id: self.tenant_id.clone(),
            idempotency_key: self.idempotency_key.clone(),
            request_fingerprint: self.request_fingerprint.clone(),
            result,
        })
    }
}

/// Why a run produced no attestation (operator diagnostic stored with the
/// `failed` outcome; never an external refusal code).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RunnerDiagnostic {
    /// The request is not a well-formed runner request (kind, closed
    /// attributes, resource, capabilities).
    RequestInvalid,
    /// The sandbox could not report its guarantees.
    SandboxUnavailable,
    /// The sandbox lacks a guarantee the configured requirements need.
    SandboxInsufficient,
    /// The snapshot content store does not guarantee immutability, or could
    /// not be read.
    ArtifactIntegrityUnavailable,
    /// The snapshot is unknown, or its content does not have the workflow's
    /// digest after materialization.
    ArtifactIntegrityViolation,
    /// The snapshot content has an invalid, duplicate or reserved path.
    SnapshotInvalid,
    /// Trusted ledger state could not be read.
    StateUnavailable,
    /// The workflow does not exist.
    WorkflowUnknown,
    /// The workflow is not in the request's `from_state`.
    WorkflowState,
    /// The project has no usable active acceptance policy.
    AcceptancePolicyUnavailable,
    /// The criterion is not a mechanical criterion of the active policy.
    CriterionNotRequired,
    /// The evaluator has no registry entry.
    EvaluatorUnregistered,
    /// The registry entry is revoked.
    EvaluatorRevoked,
    /// The stored registry entry is not a valid entry.
    RegistryInvalid,
    /// The registry entry's scope, evaluator, class, trust, criteria,
    /// command or attestation algorithm do not fit this evaluation.
    EvaluatorBindingMismatch,
    /// The entry's environment digest is not the sandbox's.
    EnvironmentMismatch,
    /// The entry's command is not registered.
    CommandUnregistered,
    /// The command registry could not be consulted.
    CommandRegistryUnavailable,
    /// The registered command's digest differs from the entry's.
    CommandDigestMismatch,
    /// The materialized program differs from the registered bytes.
    ProgramIntegrity,
    /// The entry's runner key is unknown or revoked.
    KeyUnauthenticated,
    /// The runner key ring could not be consulted.
    KeyUnavailable,
    /// Trusted time could not be read.
    TimeUnavailable,
    /// The work directory could not be created, written or read.
    WorkdirUnavailable,
    /// The sandbox could not run jobs.
    JobUnavailable,
    /// The program could not be started.
    JobSpawn,
    /// The wall-time limit was reached.
    JobTimedOut,
    /// The output limit was exceeded.
    JobOutputExceeded,
    /// The child ended by a signal.
    JobSignaled,
    /// Waiting for or reading from the child failed.
    JobIo,
    /// The produced attestation could not be built, authenticated or did not
    /// verify.
    AttestationInvalid,
}

impl RunnerDiagnostic {
    /// The stored diagnostic code (existing internal codes where one fits).
    pub fn code(self) -> &'static str {
        use RunnerDiagnostic::*;
        match self {
            RequestInvalid => "RUNNER_REQUEST_INVALID",
            SandboxUnavailable => "RUNNER_SANDBOX_UNAVAILABLE",
            SandboxInsufficient => "RUNNER_SANDBOX_INSUFFICIENT",
            ArtifactIntegrityUnavailable => "ARTIFACT_INTEGRITY_UNAVAILABLE",
            ArtifactIntegrityViolation => "ARTIFACT_INTEGRITY_VIOLATION",
            SnapshotInvalid => "RUNNER_SNAPSHOT_INVALID",
            StateUnavailable => "RUNNER_STATE_UNAVAILABLE",
            WorkflowUnknown => "WORKFLOW_UNKNOWN",
            WorkflowState => "RUNNER_WORKFLOW_STATE",
            AcceptancePolicyUnavailable => "ACCEPTANCE_POLICY_UNAVAILABLE",
            CriterionNotRequired => "RUNNER_CRITERION_NOT_REQUIRED",
            EvaluatorUnregistered => "EVALUATOR_UNREGISTERED",
            EvaluatorRevoked => "ACCEPTANCE_EVALUATOR_REVOKED",
            RegistryInvalid => "EVALUATOR_REGISTRY_UNAVAILABLE",
            EvaluatorBindingMismatch => "EVALUATOR_BINDING_MISMATCH",
            EnvironmentMismatch => "RUNNER_ENVIRONMENT_MISMATCH",
            CommandUnregistered => "RUNNER_COMMAND_UNREGISTERED",
            CommandRegistryUnavailable => "RUNNER_COMMAND_REGISTRY_UNAVAILABLE",
            CommandDigestMismatch => "RUNNER_COMMAND_DIGEST_MISMATCH",
            ProgramIntegrity => "RUNNER_PROGRAM_INTEGRITY",
            KeyUnauthenticated => "ATTESTATION_UNAUTHENTICATED",
            KeyUnavailable => "ATTESTATION_KEY_UNAVAILABLE",
            TimeUnavailable => "TIME_UNAVAILABLE",
            WorkdirUnavailable => "RUNNER_WORKDIR_UNAVAILABLE",
            JobUnavailable => "RUNNER_JOB_UNAVAILABLE",
            JobSpawn => "RUNNER_JOB_SPAWN",
            JobTimedOut => "RUNNER_JOB_TIMED_OUT",
            JobOutputExceeded => "RUNNER_JOB_OUTPUT_EXCEEDED",
            JobSignaled => "RUNNER_JOB_SIGNALED",
            JobIo => "RUNNER_JOB_IO",
            AttestationInvalid => "RUNNER_ATTESTATION_INVALID",
        }
    }

    fn of_job(failure: JobFailure) -> RunnerDiagnostic {
        match failure {
            JobFailure::Unavailable => RunnerDiagnostic::JobUnavailable,
            JobFailure::Spawn => RunnerDiagnostic::JobSpawn,
            JobFailure::TimedOut => RunnerDiagnostic::JobTimedOut,
            JobFailure::OutputExceeded => RunnerDiagnostic::JobOutputExceeded,
            JobFailure::Signaled => RunnerDiagnostic::JobSignaled,
            JobFailure::Io => RunnerDiagnostic::JobIo,
        }
    }
}

/// An authenticated, verified attestation to store with an `applied` run.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProducedAttestation {
    attestation_id: String,
    tenant_id: String,
    project_id: String,
    document: String,
}

impl ProducedAttestation {
    /// Its `attestation_id`.
    pub fn attestation_id(&self) -> &str {
        &self.attestation_id
    }

    /// Its tenant.
    pub fn tenant_id(&self) -> &str {
        &self.tenant_id
    }

    /// Its project.
    pub fn project_id(&self) -> &str {
        &self.project_id
    }

    /// The D-006 canonical JSON text of the document.
    pub fn document(&self) -> &str {
        &self.document
    }
}

/// The outcome of a started run, to record in T-finish. Built only by the
/// runner adapter.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RunOutcome {
    permit_id: String,
    result: Result<ProducedAttestation, RunnerDiagnostic>,
}

impl RunOutcome {
    /// The permit id of the run (its `started` row).
    pub fn permit_id(&self) -> &str {
        &self.permit_id
    }

    /// `Applied` (with an attestation) or `Failed` (with a diagnostic).
    pub fn effect_result(&self) -> EffectResult {
        match self.result {
            Ok(_) => EffectResult::Applied,
            Err(_) => EffectResult::Failed,
        }
    }

    /// The attestation of an applied run.
    pub fn attestation(&self) -> Option<&ProducedAttestation> {
        self.result.as_ref().ok()
    }

    /// The diagnostic of a failed run.
    pub fn diagnostic(&self) -> Option<RunnerDiagnostic> {
        self.result.as_ref().err().copied()
    }
}

/// The runner's own durable store of consumed permits, runs and
/// attestations (separate from the broker ledger, bound to one ledger).
pub trait RunnerStore: Sync {
    /// T-start: begin (a failure is the outer `Err` before any permit check,
    /// D-025 B); `verify`; `PERMIT_REPLAYED` (rolled back) when the verified
    /// permit id already has a run; otherwise record it `started` for the
    /// verified request and commit. Returns the verified effect. A refusal
    /// rolls back and records nothing.
    fn start(&self, verify: &mut Verify<'_>) -> Result<Result<VerifiedEffect, Refusal>, TxFailure>;

    /// T-finish: the `started` run of `outcome.permit_id()` becomes
    /// `applied` with its attestation (unique id) or `failed` with its
    /// diagnostic, in one transaction. A run in any other state, or none, is
    /// a failure (never overwritten).
    fn finish(&self, outcome: &RunOutcome) -> Result<(), TxFailure>;

    /// Read-only: begin; `verify`; the record of the verified permit id
    /// (`None`: not consumed); roll back. A storage failure is the outer
    /// `Err` and takes precedence over every permit check.
    fn inspect(
        &self,
        verify: &mut Verify<'_>,
    ) -> Result<Result<Option<RunRecord>, Refusal>, TxFailure>;

    /// The record of `permit_id`, if any (read-only).
    fn record(&self, permit_id: &str) -> Result<Option<RunRecord>, TxFailure>;
}

/// A [`TrustedClock`] that persists every reading as the ledger's floor
/// before returning it ([`observe`]), as the broker's own time reads do.
pub struct LedgerClock<'a, L: ?Sized> {
    clock: &'a (dyn EffectiveClock + Sync),
    ledger: &'a L,
}

impl<'a, L: ?Sized> LedgerClock<'a, L> {
    /// Time from `clock`, floor persisted in `ledger`.
    pub fn new(clock: &'a (dyn EffectiveClock + Sync), ledger: &'a L) -> LedgerClock<'a, L> {
        LedgerClock { clock, ledger }
    }
}

impl<L: ?Sized> TrustedClock for LedgerClock<'_, L>
where
    for<'b> &'b L: LedgerStore,
{
    fn now(&self) -> Result<Timestamp, ClockError> {
        let mut ledger: &L = self.ledger;
        observe(self.clock, &mut ledger).map(|o| o.at())
    }
}

/// The runner's configuration.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RunnerConfig {
    /// The sandbox guarantees required before anything runs.
    pub requirements: RunnerRequirements,
    /// Bounds of every job.
    pub limits: Limits,
    /// `fresh_until` is `finished_at` plus this (whole seconds).
    pub freshness: Duration,
    /// The existing directory under which private work directories are
    /// created (must not be writable by untrusted parties).
    pub work_root: PathBuf,
}

/// The trusted sources of a runner.
#[derive(Clone, Copy)]
pub struct RunnerSources<'a> {
    /// Ledger state (workflow, active acceptance policy, registry).
    pub state: &'a dyn RunnerState,
    /// Registered commands.
    pub commands: &'a dyn CommandRegistry,
    /// Immutable snapshot content.
    pub snapshots: &'a dyn SnapshotContent,
    /// Runner attestation keys.
    pub keys: &'a (dyn RunnerKeyRing + Sync),
    /// Bounded execution.
    pub sandbox: &'a dyn Sandbox,
    /// Trusted time ([`LedgerClock`] in production).
    pub clock: &'a (dyn TrustedClock + Sync),
}

/// The runner effect adapter (module documentation).
pub struct RunnerAdapter<'a, S: ?Sized> {
    store: &'a S,
    sources: RunnerSources<'a>,
    config: RunnerConfig,
}

/// What the request names (validated names only).
struct Target<'r> {
    tenant: &'r str,
    project: &'r str,
    workflow: &'r str,
    from_state: &'r str,
    to_state: &'r str,
    criterion: &'r str,
    record: &'r str,
    evaluator: &'r str,
}

const ATTRIBUTES: [&str; 6] = [
    "acceptance_record_id",
    "criterion_id",
    "evaluator_id",
    "from_state",
    "to_state",
    "workflow_id",
];

fn target(effect: &VerifiedEffect) -> Option<Target<'_>> {
    let request = effect.request();
    let action = request.get("action")?;
    if action.get("kind").and_then(Value::as_str) != Some(RUNNER_EFFECT_KIND) {
        return None;
    }
    if !action
        .get("requested_capabilities")
        .and_then(Value::as_arr)
        .is_some_and(<[Value]>::is_empty)
    {
        return None;
    }
    let attrs = action.get("attributes")?.as_obj()?;
    if attrs.len() != ATTRIBUTES.len() || !ATTRIBUTES.iter().all(|k| attrs.contains_key(*k)) {
        return None;
    }
    let attr = |k: &str, ok: fn(&str) -> bool| attrs.get(k)?.as_str().filter(|s| ok(s));
    let t = Target {
        tenant: effect.tenant_id(),
        project: request
            .get("project_id")
            .and_then(Value::as_str)
            .filter(|s| is_id(s))?,
        workflow: attr("workflow_id", is_name)?,
        from_state: attr("from_state", is_name)?,
        to_state: attr("to_state", is_name)?,
        criterion: attr("criterion_id", is_name)?,
        record: attr("acceptance_record_id", is_id)?,
        evaluator: attr("evaluator_id", is_name)?,
    };
    let resource = action
        .get("resource")
        .and_then(|r| r.get("canonical_resource_id"))
        .and_then(Value::as_str);
    (is_id(t.tenant) && t.from_state != t.to_state && resource == Some(&t.canonical_target()))
        .then_some(t)
}

impl Target<'_> {
    fn canonical_target(&self) -> String {
        format!(
            "workflow:{}/{}/{}",
            self.tenant, self.project, self.workflow
        )
    }
}

fn entry_str<'e>(entry: &'e Value, path: &[&str]) -> Option<&'e str> {
    let mut v = entry;
    for k in path {
        v = v.get(k)?;
    }
    v.as_str()
}

/// Whole seconds, rounded down.
fn whole_seconds(t: Timestamp) -> Option<Timestamp> {
    timestamp_from_parts(t.unix_seconds(), 0)
}

/// A private work directory, removed (best effort) when dropped.
struct WorkDir(PathBuf);

impl Drop for WorkDir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

/// Create `dir` with mode 0700 exactly (it must not exist).
fn private_dir(dir: &Path) -> std::io::Result<()> {
    DirBuilder::new().mode(0o700).create(dir)?;
    fs::set_permissions(dir, fs::Permissions::from_mode(0o700))
}

/// Write `bytes` to the new file `path` with exactly `mode`.
fn new_file(path: &Path, bytes: &[u8], mode: u32) -> std::io::Result<()> {
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(mode)
        .open(path)?;
    file.write_all(bytes)?;
    file.sync_all()?;
    drop(file);
    fs::set_permissions(path, fs::Permissions::from_mode(mode))
}

/// Materialize `files` under `root` (creating parent directories 0700,
/// files read-only), then read every file back.
fn materialize(root: &Path, files: &[SnapshotFile]) -> Result<Vec<SnapshotFile>, RunnerDiagnostic> {
    let io = |_| RunnerDiagnostic::WorkdirUnavailable;
    for f in files {
        let parts: Vec<&str> = f.path.split('/').collect();
        let mut dir = root.to_path_buf();
        for part in &parts[..parts.len().saturating_sub(1)] {
            dir.push(part);
            match fs::symlink_metadata(&dir) {
                Ok(m) if m.is_dir() => {}
                Ok(_) => return Err(RunnerDiagnostic::SnapshotInvalid),
                Err(_) => private_dir(&dir).map_err(io)?,
            }
        }
        new_file(&root.join(&f.path), &f.bytes, 0o400).map_err(io)?;
    }
    files
        .iter()
        .map(|f| {
            fs::read(root.join(&f.path))
                .map(|bytes| SnapshotFile {
                    path: f.path.clone(),
                    bytes,
                })
                .map_err(io)
        })
        .collect()
}

impl<'a, S: RunnerStore + ?Sized> RunnerAdapter<'a, S> {
    /// A runner over `store` with its trusted `sources` and `config`.
    pub fn new(
        store: &'a S,
        sources: RunnerSources<'a>,
        config: RunnerConfig,
    ) -> RunnerAdapter<'a, S> {
        RunnerAdapter {
            store,
            sources,
            config,
        }
    }

    /// The store's record of `permit_id` (operators and tests).
    pub fn record(&self, permit_id: &str) -> Result<Option<RunRecord>, TxFailure> {
        self.store.record(permit_id)
    }

    /// Evaluate the started run of `effect` (module documentation).
    fn evaluate(&self, effect: &VerifiedEffect) -> Result<ProducedAttestation, RunnerDiagnostic> {
        use RunnerDiagnostic as D;
        let src = &self.sources;
        let t = target(effect).ok_or(D::RequestInvalid)?;
        let attestation_id = attestation_id_for(effect.permit_id());
        if !is_id(&attestation_id) {
            return Err(D::RequestInvalid);
        }
        // The sandbox and the snapshot store must establish their guarantees
        // before anything else is consulted (fail closed).
        let guarantees = src
            .sandbox
            .guarantees()
            .map_err(|_| D::SandboxUnavailable)?;
        if !guarantees.satisfies(&self.config.requirements.sandbox) {
            return Err(D::SandboxInsufficient);
        }
        if !src.snapshots.immutable() {
            return Err(D::ArtifactIntegrityUnavailable);
        }
        // Trusted ledger state.
        let key = WorkflowKey {
            tenant_id: t.tenant.to_string(),
            project_id: t.project.to_string(),
            workflow_id: t.workflow.to_string(),
        };
        let workflow = src
            .state
            .workflow(&key)
            .map_err(|_| D::StateUnavailable)?
            .ok_or(D::WorkflowUnknown)?;
        if workflow.state != t.from_state {
            return Err(D::WorkflowState);
        }
        let policy = src
            .state
            .acceptance_policy(t.tenant, t.project)
            .map_err(|_| D::StateUnavailable)?
            .ok_or(D::AcceptancePolicyUnavailable)?;
        let requirements = json::parse(policy.requirements.as_bytes())
            .ok()
            .and_then(|v| AcceptanceRequirements::from_value(&v).ok())
            .ok_or(D::AcceptancePolicyUnavailable)?;
        if !requirements.criteria.iter().any(|c| {
            c.criterion_id == t.criterion && c.required_class == CriterionClass::Mechanical
        }) {
            return Err(D::CriterionNotRequired);
        }
        let stored = src
            .state
            .registry_entry(t.tenant, t.project, t.evaluator)
            .map_err(|_| D::StateUnavailable)?
            .ok_or(D::EvaluatorUnregistered)?;
        let entry = json::parse(stored.entry.as_bytes())
            .ok()
            .and_then(|v| RegistryEntry::validate(&v).ok())
            .ok_or(D::RegistryInvalid)?;
        let e = entry.as_value();
        if stored.revoked || entry_str(e, &["status"]) != Some("active") {
            return Err(D::EvaluatorRevoked);
        }
        let bound = entry_str(e, &["evaluator_id"]) == Some(t.evaluator)
            && entry_str(e, &["scope", "tenant_id"]) == Some(t.tenant)
            && entry_str(e, &["scope", "project_id"]) == Some(t.project)
            && entry_str(e, &["class"]) == Some("mechanical")
            && entry_str(e, &["trust"]) == Some("M")
            && entry_str(e, &["attestation", "alg"]) == Some(MAC_ALG)
            && e.get("criteria")
                .and_then(Value::as_arr)
                .is_some_and(|c| c.iter().any(|x| x.as_str() == Some(t.criterion)));
        let (Some(command_id), Some(command_digest), Some(key_id), true) = (
            entry_str(e, &["command", "command_id"]),
            entry_str(e, &["command", "command_digest"]),
            entry_str(e, &["attestation", "key_id"]),
            bound,
        ) else {
            return Err(D::EvaluatorBindingMismatch);
        };
        let environment_digest = src.sandbox.environment_digest();
        if entry_str(e, &["environment_digest"]) != Some(&environment_digest) {
            return Err(D::EnvironmentMismatch);
        }
        let command = src
            .commands
            .command(t.tenant, t.project, command_id)
            .map_err(|_| D::CommandRegistryUnavailable)?
            .ok_or(D::CommandUnregistered)?;
        if command.command_id != command_id || command.digest().as_deref() != Some(command_digest) {
            return Err(D::CommandDigestMismatch);
        }
        let runner_key = match src.keys.runner_key(key_id).map_err(|_| D::KeyUnavailable)? {
            KeyStatus::Active(k) => k,
            KeyStatus::Revoked | KeyStatus::Unknown => return Err(D::KeyUnauthenticated),
        };
        let content = src
            .snapshots
            .content(t.tenant, t.project, &workflow.artifact_snapshot_id)
            .map_err(|_| D::ArtifactIntegrityUnavailable)?
            .ok_or(D::ArtifactIntegrityViolation)?;
        if content.iter().any(|f| {
            !valid_snapshot_path(&f.path) || f.path.split('/').next() == Some(PROGRAM_NAME)
        }) || snapshot_digest(&content).is_none()
        {
            return Err(D::SnapshotInvalid);
        }
        // Run.
        let now = || {
            src.clock
                .now()
                .ok()
                .and_then(whole_seconds)
                .ok_or(D::TimeUnavailable)
        };
        let started_at = now()?;
        let output = {
            let dir = self
                .config
                .work_root
                .join(format!("run-{}", effect.permit_id()));
            match fs::symlink_metadata(&self.config.work_root) {
                Ok(m) if m.is_dir() => {}
                _ => return Err(D::WorkdirUnavailable),
            }
            private_dir(&dir).map_err(|_| D::WorkdirUnavailable)?;
            let work = WorkDir(dir);
            let program = work.0.join(PROGRAM_NAME);
            new_file(&program, &command.program, 0o500).map_err(|_| D::WorkdirUnavailable)?;
            let readback = materialize(&work.0, &content)?;
            if snapshot_digest(&readback).as_deref() != Some(workflow.artifact_digest.as_str()) {
                return Err(D::ArtifactIntegrityViolation);
            }
            let program_bytes = fs::read(&program).map_err(|_| D::WorkdirUnavailable)?;
            if Digest::of_bytes(&program_bytes) != Digest::of_bytes(&command.program) {
                return Err(D::ProgramIntegrity);
            }
            let job = Job {
                program: &program,
                args: &command.args,
                env: &command.env,
                workdir: &work.0,
                limits: self.config.limits,
            };
            let output = src.sandbox.run(&job).map_err(RunnerDiagnostic::of_job)?;
            drop(work);
            output
        };
        let finished_at = now()?;
        let freshness =
            i64::try_from(self.config.freshness.as_secs()).map_err(|_| D::AttestationInvalid)?;
        let fresh_until = finished_at.plus_seconds(freshness);
        let output_digest = output_digest(&output).ok_or(D::AttestationInvalid)?;
        let text = |s: &str| json::s(s);
        let mut doc = json::obj([
            ("contract_version", text("runner-attestation/1")),
            ("purpose", text(ATTESTATION_PURPOSE)),
            ("attestation_id", text(&attestation_id)),
            ("tenant_id", text(t.tenant)),
            ("project_id", text(t.project)),
            ("task_id", text(&workflow.task_id)),
            ("workflow_id", text(t.workflow)),
            (
                "transition",
                json::obj([
                    ("from_state", text(t.from_state)),
                    ("to_state", text(t.to_state)),
                ]),
            ),
            ("artifact_snapshot_id", text(&workflow.artifact_snapshot_id)),
            ("artifact_digest", text(&workflow.artifact_digest)),
            ("canonical_target", text(&t.canonical_target())),
            (
                "acceptance_policy_id",
                text(&requirements.policy.acceptance_policy_id),
            ),
            (
                "acceptance_policy_version",
                text(&requirements.policy.acceptance_policy_version),
            ),
            (
                "acceptance_policy_bundle_sha",
                text(&requirements.policy.acceptance_policy_bundle_sha),
            ),
            ("state_revision", Value::Int(workflow.revision)),
            ("acceptance_record_id", text(t.record)),
            ("criterion_id", text(t.criterion)),
            ("evaluator_id", text(t.evaluator)),
            (
                "evaluator_version",
                e.get("evaluator_version").cloned().unwrap_or(Value::Null),
            ),
            (
                "implementation_digest",
                e.get("implementation_digest")
                    .cloned()
                    .unwrap_or(Value::Null),
            ),
            ("environment_digest", text(&environment_digest)),
            (
                "registry_revision",
                e.get("registry_revision").cloned().unwrap_or(Value::Null),
            ),
            ("command_id", text(command_id)),
            ("command_digest", text(command_digest)),
            (
                "input_digests",
                Value::Arr(vec![text(&workflow.artifact_digest)]),
            ),
            ("evidence_digests", Value::Arr(Vec::new())),
            ("exit_status", Value::Int(i64::from(output.exit_status))),
            ("output_digest", text(&output_digest)),
            ("started_at", text(&started_at.to_string())),
            ("finished_at", text(&finished_at.to_string())),
            ("fresh_until", text(&fresh_until.to_string())),
            (
                "auth",
                json::obj([("alg", text(MAC_ALG)), ("key_id", text(key_id))]),
            ),
        ]);
        let bytes = mac_bytes(&doc).ok_or(D::AttestationInvalid)?;
        let tag =
            mac::sign_hex(runner_key.bytes(), bytes.as_bytes()).ok_or(D::AttestationInvalid)?;
        if let Value::Obj(m) = &mut doc {
            if let Some(Value::Obj(auth)) = m.get_mut("auth") {
                auth.insert("mac".to_string(), json::s(&tag));
            }
        }
        RunnerAttestation::validate(&doc, finished_at).map_err(|_| D::AttestationInvalid)?;
        verify_attestation(&doc, src.keys, finished_at).map_err(|_| D::AttestationInvalid)?;
        Ok(ProducedAttestation {
            attestation_id,
            tenant_id: t.tenant.to_string(),
            project_id: t.project.to_string(),
            document: json::canonical(&doc).map_err(|_| D::AttestationInvalid)?,
        })
    }
}

impl<S: RunnerStore + ?Sized> EffectAdapter for RunnerAdapter<'_, S> {
    fn apply(&self, verify: &mut Verify<'_>) -> Result<Result<EffectResult, Refusal>, TxFailure> {
        let effect = match self.store.start(verify)? {
            Ok(effect) => effect,
            Err(refusal) => return Ok(Err(refusal)),
        };
        // The permit is consumed: from here on this permit never runs again.
        let outcome = RunOutcome {
            permit_id: effect.permit_id().to_string(),
            result: self.evaluate(&effect),
        };
        // A failure to record the outcome happens after the consuming commit:
        // the permit is consumed and the job may have run, so the adapter
        // transaction's outcome is reported as unknown (D-035 item 4), which
        // makes the possible progress `dispatched_with_effect`.
        self.store.finish(&outcome).map_err(|f| TxFailure {
            outcome_unknown: true,
            ..f
        })?;
        Ok(Ok(outcome.effect_result()))
    }

    fn inspect(
        &self,
        verify: &mut Verify<'_>,
    ) -> Result<Result<Option<EffectEvidence>, Refusal>, TxFailure> {
        Ok(self
            .store
            .inspect(verify)?
            .map(|record| record.and_then(|r| r.evidence())))
    }

    fn evidence(&self, permit_id: &str) -> Result<Option<EffectEvidence>, TxFailure> {
        Ok(self.store.record(permit_id)?.and_then(|r| r.evidence()))
    }
}
