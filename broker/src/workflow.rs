//! The transition service (M3-05; D-036; ADR-0008 APPROVED IN PRINCIPLE;
//! acceptance-gate-integrity sections 1 and 2; INV-32..INV-40;
//! m1/fixtures/transition/SCENARIOS.md with the production differences below).
//!
//! **One writer of workflow state (INV-32).** Workflow state and revision
//! change only through [`Broker::promote`] (protected and unprotected
//! transitions) and the authenticated administrative operations of
//! [`Broker::administer`]; the store exposes no other write.
//!
//! **Promotion** ([`Broker::promote`]), one broker operation under a shared
//! hold of the policy gate (D-036 item 2; no client-held promotion permit).
//! Exact stage order (D-038 items 2 to 5); the first stage that refuses
//! decides the answer:
//!
//! 1. Trusted time is observed (floor persisted; `TIME_UNAVAILABLE`). The
//!    request must be a `workflow.transition` (`REQUEST_SCOPE_MISMATCH`).
//! 2. **Durable receipt lookup, replay first** (D-036 item 2, M1 promote
//!    step 1): an existing promotion receipt for (tenant, idempotency key)
//!    is answered before anything is re-evaluated. The identical sealed
//!    request replays its durable receipt whatever the workflow's current
//!    revision (so a retry after an unknown commit outcome recovers from
//!    durable state); any other request with the key is
//!    `IDEMPOTENCY_CONFLICT`. This is a storage read: if it fails the answer
//!    is `LEDGER_UNAVAILABLE` (a read, so [`Promotion::failure`] is `None`),
//!    before any decision, even for a request the policy would refuse;
//!    nothing changes and nothing is consumed.
//! 3. The resource must be a well-formed workflow id inside the request's own
//!    tenant and project (`REQUEST_SCOPE_MISMATCH` otherwise, including a
//!    resource naming another tenant or project). The stored workflow is read
//!    keyed by the request's tenant and project (a storage failure is
//!    `LEDGER_UNAVAILABLE`); none is `WORKFLOW_UNKNOWN`, so another tenant's
//!    workflow is indistinguishable from a missing one and is never probed
//!    (D-038 item 3, `sc-27`).
//! 4. The request is re-decided at use time through `writ-kernel` at the
//!    trusted stored revision (protection classification, binding structure,
//!    approval requirement). A stale expected revision is `REVISION_CONFLICT`
//!    ([`ConflictStage::UseTimeDecision`]; the kernel's
//!    `STATE_REVISION_CONFLICT`, normalized here only, D-038 item 2); every
//!    other decision refusal keeps its code. `require_approval` without a
//!    grant is `APPROVAL_REQUIRED`; a grant is consumed in the promotion
//!    transaction.
//! 5. For a protected transition, outside any transaction: the record is
//!    read by id from the trusted [`AcceptanceRecordSource`] (never from the
//!    request) and verified ([`crate::acceptance::verify_record`]: MAC,
//!    projection, mode; the record must carry the requested id); each
//!    attestation the record requires is read from the [`AttestationSource`]
//!    and verified; the [`SnapshotStore`] must confirm the record's snapshot
//!    id and digest (`ARTIFACT_INTEGRITY_UNAVAILABLE`,
//!    `ARTIFACT_INTEGRITY_VIOLATION`, INV-40); only then must the request's
//!    acceptance attributes equal the record (`PERMIT_BINDING_MISMATCH`), so
//!    integrity is decided before the binding (D-038 item 4, `sc-26`). These
//!    are immutable, authenticated inputs; everything mutable is checked
//!    inside the transaction.
//! 6. The workflow lock and the per-key lock are taken (lock order: gate,
//!    workflow lock, key lock, transaction), then the fenced `promote`
//!    transaction ([`WorkflowLedger::promote`]): an existing promotion receipt
//!    for the key replays (same request fingerprint) or is
//!    `IDEMPOTENCY_CONFLICT`; the stored workflow must still be in
//!    `from_state` at the expected revision (`REVISION_CONFLICT`,
//!    [`ConflictStage::Transaction`]: a change committed between the use-time
//!    decision and this transaction); for a protected transition the
//!    project's active acceptance policy must exist
//!    (`ACCEPTANCE_POLICY_UNAVAILABLE`) and be the one the authorization
//!    bundle's protection names (`POLICY_DEPENDENCY_MISMATCH`), then
//!    [`crate::acceptance::check_use`] runs on the stored state (kernel check,
//!    replay of the record ledger-wide, registry, revocation, attestation
//!    binding, freshness and single use); a grant is checked and consumed
//!    like T-reserve. Then, atomically: state, revision + 1, last promotion,
//!    consumed record, consumed attestations, promotion receipt
//!    `promotion:<workflow_id>:<revision>` and a `promote` workflow event.
//!    If the transaction cannot run the answer is `LEDGER_UNAVAILABLE` with
//!    its [`TxFailure`]: a begin or body failure committed nothing; a failed
//!    commit has an unknown outcome (below).
//! 7. A refusal is audited after the answer (below); a failed audit write
//!    never changes the answer and never authorizes.
//!
//! The revision-conflict code is one (`REVISION_CONFLICT`) whichever check
//! detects it; [`Refusal::diagnostic`] names the stage for operators only.
//! Concurrent promotions of one workflow with distinct keys commit exactly
//! once; the others are `REVISION_CONFLICT` at either stage. Under a partial
//! storage outage in which only transactions fail (C4), stages 1 to 5 answer
//! as with storage up and an otherwise valid promotion is
//! `LEDGER_UNAVAILABLE` at stage 6 with no progress.
//!
//! An identical retry replays the durable receipt; a changed request with the
//! key is `IDEMPOTENCY_CONFLICT`; a consumed record or attestation can never
//! satisfy another promotion. A failed commit has an unknown outcome
//! ([`crate::store::TxFailure::outcome_unknown`]); the caller recovers by
//! retrying the same request, which reads the durable state (replay if it
//! committed, a fresh evaluation if it did not); nothing assumes it did not
//! commit.
//!
//! **Administration** ([`Broker::administer`], D-036 item 5): workflow
//! creation, artifact update, acceptance-policy activation, evaluator
//! registration and revocation. Each is authorized for its tenant and project
//! by a [`WorkflowAuthority`] (no production implementation: M3 interfaces
//! and test authorities are not completed production authentication), takes
//! the policy gate **exclusively** (so it waits for every in-flight
//! promotion and execution and cannot slip between a check and its use), and
//! is one fenced transaction that also records an `admin_events` row with
//! the operator's provenance. Creation never starts in a protected state or
//! the target of a protected edge, and starts at revision 0; an artifact
//! update must be confirmed by the [`SnapshotStore`] and increments the
//! revision (invalidating every record bound to the old revision or
//! artifact); no administrative operation changes a workflow's state.
//! Acceptance-policy activation replaces the project's requirement set and
//! effect prerequisites; records bound to another policy then fail the
//! kernel's policy binding.
//!
//! **Refusal audit:** every refusal is audited in its own transaction after
//! the locks are released; if storage is unavailable that write fails too
//! and the refusal is returned without a durable audit record.
//!
//! **Production differences from M1** (frozen fixtures unchanged; exact
//! M3-08 exceptions): no client-held promotion permit (`PERMIT_MISSING`,
//! `PERMIT_REQUEST_MISMATCH` and `PERMIT_POLICY_STALE` for promotions are
//! replaced by the use-time decision on the sealed request); records come
//! from the trusted source by id with an HMAC (an inline record is never
//! read); client-asserted `reconcile {observed}` is not provided (sc-43,
//! sc-44; D-036 item 3); `activate_policy`, `activate_authorization_bundle`,
//! `update_artifact` and `revoke_evaluator` are operator operations.

use std::collections::BTreeSet;

use writ_kernel::contracts::{AcceptanceRequirements, CurrentState, StateRevision};
use writ_kernel::decision::Policy;
use writ_kernel::json::{self, Value};
use writ_kernel::time::Timestamp;
use writ_kernel::transition::{check_transition, workflow_id};

use crate::acceptance::{
    check_use, verify_attestation, verify_record, AcceptanceCode, UseContext, VerifiedAttestation,
    VerifiedRecord,
};
use crate::authorize::{authorize, Authorized};
use crate::contracts::RegistryEntry;
use crate::effects::EffectAdapter;
use crate::execute::Broker;
use crate::grants::{GrantLabel, ReserveRequest};
use crate::ingress::VerifiedRequest;
use crate::keys::{AcceptanceKeyRing, RunnerKeyRing};
use crate::refusal::{ConflictStage, Refusal};
use crate::store::{ExecuteLedger, LedgerStore, StoreError, TxFailure};

/// The action kind of a promotion.
pub const TRANSITION_KIND: &str = "workflow.transition";

/// A trusted source could not be consulted (fail closed).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct SourceUnavailable;

/// Authenticated acceptance records issued by the acceptance service, by id
/// (D-036 item 1). No production implementation (M4/M6).
pub trait AcceptanceRecordSource: Sync {
    /// The authenticated record document `record_id` of the tenant and
    /// project, `None` when there is none.
    fn record(
        &self,
        tenant_id: &str,
        project_id: &str,
        record_id: &str,
    ) -> Result<Option<Value>, SourceUnavailable>;
}

/// Runner attestations by id (D-033 item 4; the runner is M3-07).
pub trait AttestationSource: Sync {
    /// The attestation document `attestation_id`, `None` when there is none.
    fn attestation(
        &self,
        tenant_id: &str,
        project_id: &str,
        attestation_id: &str,
    ) -> Result<Option<Value>, SourceUnavailable>;
}

/// What the immutable snapshot store holds for a snapshot id (INV-40).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SnapshotCheck {
    /// Service-controlled immutable content whose digest is the given one.
    Matches,
    /// Content with another digest.
    Differs,
    /// No such snapshot.
    Unknown,
}

/// The service-controlled immutable snapshot store (INV-40; to be completed
/// with M3-07 and M5): recomputes the digest of the immutable content.
pub trait SnapshotStore: Sync {
    /// Whether `snapshot_id` of the tenant and project holds content with `digest`.
    fn check(
        &self,
        tenant_id: &str,
        project_id: &str,
        snapshot_id: &str,
        digest: &str,
    ) -> Result<SnapshotCheck, SourceUnavailable>;
}

/// The trusted inputs of a promotion.
#[derive(Clone, Copy)]
pub struct Trust<'a> {
    /// Acceptance records by id.
    pub records: &'a dyn AcceptanceRecordSource,
    /// Runner attestations by id.
    pub attestations: &'a dyn AttestationSource,
    /// Acceptance-record keys.
    pub record_keys: &'a dyn AcceptanceKeyRing,
    /// Runner attestation keys.
    pub runner_keys: &'a dyn RunnerKeyRing,
    /// Immutable snapshots.
    pub snapshots: &'a dyn SnapshotStore,
}

/// One workflow's identity.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct WorkflowKey {
    /// Tenant.
    pub tenant_id: String,
    /// Project.
    pub project_id: String,
    /// Workflow id.
    pub workflow_id: String,
}

/// A workflow as stored.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StoredWorkflow {
    /// Task the workflow belongs to.
    pub task_id: String,
    /// Current state.
    pub state: String,
    /// Current revision.
    pub revision: i64,
    /// Current artifact snapshot id.
    pub artifact_snapshot_id: String,
    /// Current artifact digest.
    pub artifact_digest: String,
    /// The latest protected promotion, if any.
    pub last_promotion: Option<LastPromotion>,
}

/// The promotion receipt of the latest protected commit (D-019 N-3).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LastPromotion {
    /// `promotion:<workflow_id>:<revision>`.
    pub receipt_id: String,
    /// The consumed record.
    pub record_id: String,
    /// Promoted snapshot id.
    pub artifact_snapshot_id: String,
    /// Promoted digest.
    pub artifact_digest: String,
    /// Acceptance-policy bundle digest it was accepted under.
    pub acceptance_policy_bundle_sha: String,
    /// Authorization bundle digest it was decided under.
    pub authorization_bundle_sha: String,
}

/// The project's active acceptance policy as stored.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StoredPolicy {
    /// The trusted requirement set (kernel contract, canonical JSON).
    pub requirements: String,
    /// Effect prerequisites: effect kind to the workflow states it requires.
    pub effect_prerequisites: Vec<(String, Vec<String>)>,
}

/// One registry entry as stored.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StoredEntry {
    /// The entry document (canonical JSON).
    pub entry: String,
    /// `active` or `revoked`.
    pub revoked: bool,
}

/// What the promotion transaction read, handed to
/// [`PromotionIntent::check`].
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PromotionView {
    /// The workflow (`None`: unknown).
    pub workflow: Option<StoredWorkflow>,
    /// The project's active acceptance policy.
    pub policy: Option<StoredPolicy>,
    /// Registry entries of the evaluators the record names, by evaluator id.
    pub registry: Vec<(String, StoredEntry)>,
    /// Whether the record id was already consumed (ledger-wide).
    pub record_consumed: bool,
    /// Which of the record's attestations were already consumed.
    pub consumed_attestations: BTreeSet<String>,
}

/// A promotion to commit: built only by the broker from a guard-bound
/// authorization and verified trusted inputs.
pub struct PromotionIntent<'a> {
    request: &'a ReserveRequest<'a>,
    key: WorkflowKey,
    from_state: String,
    to_state: String,
    expected_revision: Option<i64>,
    protected: Option<ProtectedInputs<'a>>,
    authorization_bundle_sha: &'a str,
    protection_policy_sha: Option<String>,
    now: Timestamp,
}

struct ProtectedInputs<'a> {
    record: &'a VerifiedRecord,
    attestations: &'a [VerifiedAttestation],
}

/// What the transaction must write after a successful check.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PromotionWrite {
    /// The new revision.
    pub revision: i64,
    /// `promotion:<workflow_id>:<revision>`.
    pub receipt_id: String,
    /// The new last promotion (protected transitions only).
    pub last_promotion: Option<LastPromotion>,
    /// The record to consume (protected only).
    pub record_id: Option<String>,
    /// Attestations to consume.
    pub attestation_ids: Vec<String>,
}

impl<'a> PromotionIntent<'a> {
    /// The receipt key, fingerprint and effective grant (as for T-reserve).
    pub fn request(&self) -> &ReserveRequest<'a> {
        self.request
    }

    /// The workflow.
    pub fn key(&self) -> &WorkflowKey {
        &self.key
    }

    /// Source state.
    pub fn from_state(&self) -> &str {
        &self.from_state
    }

    /// Target state.
    pub fn to_state(&self) -> &str {
        &self.to_state
    }

    /// The record id of a protected transition.
    pub fn record_id(&self) -> Option<&str> {
        self.protected
            .as_ref()
            .map(|p| p.record.record().acceptance_record_id.as_str())
    }

    /// The evaluator ids the record names.
    pub fn evaluator_ids(&self) -> Vec<&str> {
        self.protected.as_ref().map_or_else(Vec::new, |p| {
            p.record
                .evaluations()
                .iter()
                .map(|e| e.evaluator_id.as_str())
                .collect()
        })
    }

    /// The attestation ids the record requires.
    pub fn attestation_ids(&self) -> Vec<&str> {
        self.protected
            .as_ref()
            .map_or_else(Vec::new, |p| p.record.attestation_ids())
    }

    /// The checks on the stored state (module documentation), in order:
    /// workflow, compare-and-set, then for a protected transition the policy
    /// and its dependency, then [`check_use`]. Returns what to write.
    pub fn check(&self, view: &PromotionView) -> Result<PromotionWrite, Refusal> {
        let workflow = view.workflow.as_ref().ok_or(Refusal::WorkflowUnknown)?;
        if self.expected_revision != Some(workflow.revision) || workflow.state != self.from_state {
            return Err(Refusal::RevisionConflict(ConflictStage::Transaction));
        }
        let revision = workflow
            .revision
            .checked_add(1)
            .ok_or(Refusal::RevisionConflict(ConflictStage::StoredRevision))?;
        let receipt_id = format!("promotion:{}:{revision}", self.key.workflow_id);
        let Some(protected) = &self.protected else {
            return Ok(PromotionWrite {
                revision,
                receipt_id,
                last_promotion: None,
                record_id: None,
                attestation_ids: Vec::new(),
            });
        };
        let policy = view
            .policy
            .as_ref()
            .ok_or(Refusal::AcceptancePolicyUnavailable)?;
        let requirements = json::parse(policy.requirements.as_bytes())
            .ok()
            .and_then(|v| AcceptanceRequirements::from_value(&v).ok())
            .ok_or(Refusal::AcceptancePolicyUnavailable)?;
        if self.protection_policy_sha.as_deref()
            != Some(requirements.policy.acceptance_policy_bundle_sha.as_str())
        {
            return Err(Refusal::PolicyDependencyMismatch);
        }
        let record = protected.record.record();
        let current = CurrentState {
            tenant_id: self.key.tenant_id.clone(),
            project_id: self.key.project_id.clone(),
            task_id: workflow.task_id.clone(),
            workflow_id: self.key.workflow_id.clone(),
            transition: writ_kernel::contracts::Transition {
                from_state: self.from_state.clone(),
                to_state: self.to_state.clone(),
            },
            artifact_snapshot_id: workflow.artifact_snapshot_id.clone(),
            artifact_digest: workflow.artifact_digest.clone(),
            policy: requirements.policy.clone(),
            state_revision: StateRevision::new(workflow.revision)
                .ok_or(Refusal::RevisionConflict(ConflictStage::StoredRevision))?,
            now: self.now,
            consumed_record_ids: if view.record_consumed {
                vec![record.acceptance_record_id.clone()]
            } else {
                Vec::new()
            },
            revoked_evaluator_ids: view
                .registry
                .iter()
                .filter(|(_, e)| e.revoked)
                .map(|(id, _)| id.clone())
                .collect(),
        };
        let mut entries = Vec::with_capacity(view.registry.len());
        for (id, stored) in &view.registry {
            let entry = json::parse(stored.entry.as_bytes())
                .ok()
                .and_then(|v| RegistryEntry::validate(&v).ok())
                .ok_or(Refusal::Acceptance(AcceptanceCode::RegistryUnavailable))?;
            entries.push((id.clone(), entry));
        }
        let registry = |id: &str| {
            entries
                .iter()
                .find(|(e, _)| e == id)
                .map(|(_, entry)| entry.clone())
        };
        let attestation_ids = check_use(
            protected.record,
            &UseContext {
                current: &current,
                requirements: &requirements,
                registry: &registry,
                attestations: protected.attestations,
                consumed_attestations: &view.consumed_attestations,
            },
        )
        .map_err(Refusal::Acceptance)?;
        Ok(PromotionWrite {
            revision,
            last_promotion: Some(LastPromotion {
                receipt_id: receipt_id.clone(),
                record_id: record.acceptance_record_id.clone(),
                artifact_snapshot_id: record.artifact_snapshot_id.clone(),
                artifact_digest: record.artifact_digest.clone(),
                acceptance_policy_bundle_sha: requirements.policy.acceptance_policy_bundle_sha,
                authorization_bundle_sha: self.authorization_bundle_sha.to_string(),
            }),
            receipt_id,
            record_id: Some(record.acceptance_record_id.clone()),
            attestation_ids,
        })
    }
}

/// What the promotion transaction committed or replayed.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum PromotionOutcome {
    /// Committed now.
    Committed {
        /// The new revision.
        revision: i64,
        /// The promotion receipt id.
        receipt_id: String,
    },
    /// An identical earlier promotion: its durable receipt.
    Replayed {
        /// Its revision.
        revision: i64,
        /// Its receipt id.
        receipt_id: String,
    },
}

/// The answer of [`Broker::promote`].
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Promotion {
    /// The outcome or the (audited) refusal.
    pub result: Result<PromotionOutcome, Refusal>,
    /// The failed promotion transaction, if it failed (its stage and whether
    /// an issued commit has an unknown outcome).
    pub failure: Option<TxFailure>,
}

/// An operator or service as presented on an administrative channel.
#[derive(Clone, Copy, Debug)]
pub struct AdminPresentation<'a> {
    /// Channel.
    pub channel: &'a str,
    /// Claimed operator or service id.
    pub operator: &'a str,
    /// Authority-specific proof.
    pub credential: &'a [u8],
}

/// Administrative operations.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AdminOperation {
    /// Create a workflow.
    CreateWorkflow,
    /// Update a workflow's artifact.
    UpdateArtifact,
    /// Activate a project's acceptance policy.
    ActivateAcceptancePolicy,
    /// Register an evaluator.
    RegisterEvaluator,
    /// Revoke an evaluator.
    RevokeEvaluator,
}

impl AdminOperation {
    /// Stored operation name.
    pub fn as_str(self) -> &'static str {
        match self {
            AdminOperation::CreateWorkflow => "create_workflow",
            AdminOperation::UpdateArtifact => "update_artifact",
            AdminOperation::ActivateAcceptancePolicy => "activate_acceptance_policy",
            AdminOperation::RegisterEvaluator => "register_evaluator",
            AdminOperation::RevokeEvaluator => "revoke_evaluator",
        }
    }
}

/// The authenticated, authorized operator and its provenance.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AuthorizedOperator {
    /// Operator or service id.
    pub id: String,
    /// Issuer or realm.
    pub issuer: String,
    /// Channel it was authenticated on.
    pub channel: String,
    /// The authority's authentication-evidence reference (never a credential).
    pub evidence_ref: String,
}

/// Why an administrative request was refused (operator diagnostics).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AdminRefusal {
    /// Not authenticated.
    Unauthenticated,
    /// Authenticated, not authorized for this operation, tenant or project.
    Unauthorized,
}

/// Authenticates and authorizes operators for administrative operations
/// (D-036 item 5). No production implementation.
pub trait WorkflowAuthority {
    /// The operator `presentation` authenticates, if authorized for
    /// `operation` on the tenant and project.
    fn authorize(
        &self,
        presentation: &AdminPresentation<'_>,
        operation: AdminOperation,
        tenant_id: &str,
        project_id: &str,
    ) -> Result<AuthorizedOperator, AdminRefusal>;
}

/// An administrative request.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum AdminRequest {
    /// Create `key` in `initial_state` at revision 0 with the artifact.
    CreateWorkflow {
        /// Workflow.
        key: WorkflowKey,
        /// Task.
        task_id: String,
        /// Initial state (never protected).
        initial_state: String,
        /// Snapshot id.
        artifact_snapshot_id: String,
        /// Digest.
        artifact_digest: String,
    },
    /// Replace the artifact (revision + 1).
    UpdateArtifact {
        /// Workflow.
        key: WorkflowKey,
        /// New snapshot id.
        artifact_snapshot_id: String,
        /// New digest.
        artifact_digest: String,
    },
    /// Activate the project's acceptance policy.
    ActivateAcceptancePolicy {
        /// Tenant.
        tenant_id: String,
        /// Project.
        project_id: String,
        /// The requirement set (kernel `AcceptanceRequirements` contract).
        requirements: Value,
        /// Effect kind to required workflow states.
        effect_prerequisites: Vec<(String, Vec<String>)>,
    },
    /// Register (or re-register at a higher revision) an evaluator.
    RegisterEvaluator {
        /// The entry (validated `evaluator-registry-entry/1`, status active).
        entry: Value,
    },
    /// Revoke an evaluator of the tenant and project.
    RevokeEvaluator {
        /// Tenant.
        tenant_id: String,
        /// Project.
        project_id: String,
        /// Evaluator.
        evaluator_id: String,
    },
}

/// A validated administrative change: built only by the broker.
pub struct AdminChange {
    request: AdminRequest,
    operator: AuthorizedOperator,
    at: Timestamp,
    requirements_text: Option<String>,
    registry_revision: Option<i64>,
}

impl AdminChange {
    /// The request.
    pub fn request(&self) -> &AdminRequest {
        &self.request
    }

    /// The operator and provenance.
    pub fn operator(&self) -> &AuthorizedOperator {
        &self.operator
    }

    /// Trusted time of the change.
    pub fn at(&self) -> Timestamp {
        self.at
    }

    /// Canonical text of an activated requirement set.
    pub fn requirements_text(&self) -> Option<&str> {
        self.requirements_text.as_deref()
    }

    /// The registry revision of a registered entry.
    pub fn registry_revision(&self) -> Option<i64> {
        self.registry_revision
    }
}

/// The workflow operations of a ledger (M3-05).
pub trait WorkflowLedger: Sync {
    /// The stored workflow (read; `None`: unknown).
    fn workflow(&self, key: &WorkflowKey) -> Result<Option<StoredWorkflow>, StoreError>;

    /// The promotion receipt of (tenant, idempotency key) as (fingerprint,
    /// outcome) (read; `None`: none).
    fn promotion_receipt(
        &self,
        tenant_id: &str,
        idempotency_key: &str,
    ) -> Result<Option<(String, PromotionOutcome)>, StoreError>;

    /// One fenced administrative transaction plus its `admin_events` row.
    /// Store-level refusals: `WORKFLOW_EXISTS`, `WORKFLOW_UNKNOWN`,
    /// `REGISTRY_REVISION_STALE`, `EVALUATOR_UNREGISTERED`.
    fn administer(&self, change: &AdminChange) -> Result<Result<(), Refusal>, TxFailure>;

    /// The fenced `promote` transaction (module documentation).
    fn promote(
        &self,
        intent: &PromotionIntent<'_>,
    ) -> Result<Result<PromotionOutcome, Refusal>, TxFailure>;
}

/// D-038 item 2: the use-time kernel decision's stale expected revision is
/// the same condition as the transaction's compare-and-set and is answered
/// `REVISION_CONFLICT` at the broker boundary; every other refusal passes
/// unchanged (the kernel itself is not changed).
fn use_time_conflict(refusal: Refusal) -> Refusal {
    match &refusal {
        Refusal::Decision { reason, .. } if reason.as_str() == "STATE_REVISION_CONFLICT" => {
            Refusal::RevisionConflict(ConflictStage::UseTimeDecision)
        }
        _ => refusal,
    }
}

fn acceptance(code: AcceptanceCode) -> Refusal {
    Refusal::Acceptance(code)
}

impl<'a, L, A> Broker<'a, L, A>
where
    L: ExecuteLedger + WorkflowLedger + ?Sized,
    for<'b> &'b L: LedgerStore,
    A: EffectAdapter + ?Sized,
{
    /// Promote a workflow (module documentation). Refusals are audited.
    pub fn promote(
        &self,
        trust: &Trust<'_>,
        verified: &VerifiedRequest,
        grant: Option<&GrantLabel>,
    ) -> Promotion {
        let mut failure = None;
        let result = self.promote_held(trust, verified, grant, &mut failure);
        Promotion {
            result: self.grants().audited(result),
            failure,
        }
    }

    fn promote_held(
        &self,
        trust: &Trust<'_>,
        verified: &VerifiedRequest,
        grant: Option<&GrantLabel>,
        failure: &mut Option<TxFailure>,
    ) -> Result<PromotionOutcome, Refusal> {
        let shared = self.grants().active.shared();
        let observed = self.grants().observe()?;
        let request = verified.request();
        let tenant = request
            .get("tenant_id")
            .and_then(Value::as_str)
            .unwrap_or("");
        let project = request
            .get("project_id")
            .and_then(Value::as_str)
            .unwrap_or("");
        let kind = request
            .get("action")
            .and_then(|a| a.get("kind"))
            .and_then(Value::as_str);
        let resource = request
            .get("action")
            .and_then(|a| a.get("resource"))
            .and_then(|r| r.get("canonical_resource_id"))
            .and_then(Value::as_str)
            .unwrap_or("");
        if kind != Some(TRANSITION_KIND) {
            return Err(Refusal::RequestScopeMismatch);
        }
        // An existing receipt for the key is answered first (M1 promote step 1): an
        // identical sealed request replays its durable receipt whatever the workflow's
        // current revision, so a retry after an unknown commit outcome recovers from
        // durable state; any other request with the key is a conflict.
        let idempotency_key = request
            .get("idempotency_key")
            .and_then(Value::as_str)
            .unwrap_or("");
        if let Some((fingerprint, outcome)) = self
            .grants()
            .ledger
            .promotion_receipt(tenant, idempotency_key)
            .map_err(Refusal::LedgerUnavailable)?
        {
            return if fingerprint == verified.fingerprint().as_str() {
                Ok(outcome)
            } else {
                Err(Refusal::IdempotencyConflict)
            };
        }
        let key = WorkflowKey {
            tenant_id: tenant.to_string(),
            project_id: project.to_string(),
            workflow_id: workflow_id(resource, tenant, project)
                .ok_or(Refusal::RequestScopeMismatch)?,
        };
        let ledger = self.grants().ledger;
        let stored = ledger
            .workflow(&key)
            .map_err(Refusal::LedgerUnavailable)?
            .ok_or(Refusal::WorkflowUnknown)?;
        let revision = StateRevision::new(stored.revision)
            .ok_or(Refusal::RevisionConflict(ConflictStage::StoredRevision))?;
        let inputs = shared.trusted_inputs(revision);
        let authorized = authorize(verified, &inputs, &observed).map_err(use_time_conflict)?;
        let protection = match authorized.hold().policy() {
            Policy::Loaded(bundle) => bundle.protection().cloned(),
            Policy::Absent | Policy::Rejected => None,
        };
        let transition = check_transition(authorized.request(), resource, protection.as_ref())
            .map_err(|code| Refusal::Kernel(code.as_str()))?;
        let reserve = ReserveRequest::new(&authorized, observed, grant)?;
        let (record, attestations) = match &transition.binding {
            None => (None, Vec::new()),
            Some(binding) => {
                let (record, attestations) = self.protected_inputs(trust, &key, &authorized)?;
                let r = record.record();
                if (
                    &binding.acceptance_record_id,
                    &binding.artifact_snapshot_id,
                    &binding.artifact_digest,
                ) != (
                    &r.acceptance_record_id,
                    &r.artifact_snapshot_id,
                    &r.artifact_digest,
                ) {
                    return Err(acceptance(AcceptanceCode::BindingMismatch));
                }
                (Some(record), attestations)
            }
        };
        let intent = PromotionIntent {
            request: &reserve,
            key: key.clone(),
            from_state: transition.from_state.clone(),
            to_state: transition.to_state.clone(),
            expected_revision: authorized
                .request()
                .expected_state_revision
                .map(StateRevision::get),
            protected: record.as_ref().map(|record| ProtectedInputs {
                record,
                attestations: &attestations,
            }),
            authorization_bundle_sha: authorized.policy_bundle_sha(),
            protection_policy_sha: protection.map(|p| p.acceptance_policy_bundle_sha),
            now: observed.at(),
        };
        let _workflow = self
            .workflow_locks
            .lock(&key.tenant_id, &workflow_lock_name(&key));
        let _key = self
            .grants()
            .key_locks
            .lock(reserve.tenant_id(), reserve.idempotency_key());
        match ledger.promote(&intent) {
            Ok(answer) => answer,
            Err(f) => {
                let refusal = Refusal::LedgerUnavailable(f.error.clone());
                *failure = Some(f);
                Err(refusal)
            }
        }
    }

    /// Read and verify the record a protected transition names, its
    /// attestations and its snapshot (immutable, authenticated inputs).
    fn protected_inputs(
        &self,
        trust: &Trust<'_>,
        key: &WorkflowKey,
        authorized: &Authorized<'_>,
    ) -> Result<(VerifiedRecord, Vec<VerifiedAttestation>), Refusal> {
        use AcceptanceCode::*;
        let attrs = authorized.request().action.attributes.as_ref();
        let record_id = attrs
            .and_then(|a| a.get("acceptance_record_id"))
            .and_then(Value::as_str)
            .ok_or(acceptance(RecordUnknown))?;
        let doc = trust
            .records
            .record(&key.tenant_id, &key.project_id, record_id)
            .map_err(|_| acceptance(SourceUnavailable))?
            .ok_or(acceptance(RecordUnknown))?;
        let record = verify_record(&doc, trust.record_keys).map_err(acceptance)?;
        if record.record().acceptance_record_id != record_id {
            return Err(acceptance(BindingMismatch));
        }
        let now = authorized.decision().decided_at;
        let mut attestations = Vec::new();
        for id in record.attestation_ids() {
            let doc = trust
                .attestations
                .attestation(&key.tenant_id, &key.project_id, id)
                .map_err(|_| acceptance(AttestationSourceUnavailable))?
                .ok_or(acceptance(AttestationUnknown))?;
            attestations
                .push(verify_attestation(&doc, trust.runner_keys, now).map_err(acceptance)?);
        }
        let r = record.record();
        match trust
            .snapshots
            .check(
                &key.tenant_id,
                &key.project_id,
                &r.artifact_snapshot_id,
                &r.artifact_digest,
            )
            .map_err(|_| acceptance(ArtifactIntegrityUnavailable))?
        {
            SnapshotCheck::Matches => {}
            SnapshotCheck::Differs | SnapshotCheck::Unknown => {
                return Err(acceptance(ArtifactIntegrityViolation))
            }
        }
        Ok((record, attestations))
    }

    /// Perform an administrative operation (module documentation): authorize,
    /// validate, then one fenced transaction under the exclusive gate.
    /// Refusals are audited.
    pub fn administer(
        &self,
        authority: &dyn WorkflowAuthority,
        presentation: &AdminPresentation<'_>,
        snapshots: &dyn SnapshotStore,
        request: AdminRequest,
    ) -> Result<(), Refusal> {
        let result = self.administer_held(authority, presentation, snapshots, request);
        self.grants().audited(result)
    }

    fn administer_held(
        &self,
        authority: &dyn WorkflowAuthority,
        presentation: &AdminPresentation<'_>,
        snapshots: &dyn SnapshotStore,
        request: AdminRequest,
    ) -> Result<(), Refusal> {
        let (operation, tenant, project) =
            admin_scope(&request).ok_or(Refusal::AdminRequestInvalid)?;
        self.administer_held_scoped(
            authority,
            presentation,
            snapshots,
            operation,
            tenant,
            project,
            request,
        )
    }

    #[allow(clippy::too_many_arguments)]
    fn administer_held_scoped(
        &self,
        authority: &dyn WorkflowAuthority,
        presentation: &AdminPresentation<'_>,
        snapshots: &dyn SnapshotStore,
        operation: AdminOperation,
        tenant: String,
        project: String,
        request: AdminRequest,
    ) -> Result<(), Refusal> {
        let operator = authority
            .authorize(presentation, operation, &tenant, &project)
            .map_err(|r| match r {
                AdminRefusal::Unauthenticated => Refusal::OperatorUnauthenticated,
                AdminRefusal::Unauthorized => Refusal::OperatorUnauthorized,
            })?;
        let ok = |s: &str| !s.is_empty() && s.chars().count() <= 256;
        if operator.id != presentation.operator
            || operator.channel != presentation.channel
            || ![
                &operator.id,
                &operator.issuer,
                &operator.channel,
                &operator.evidence_ref,
            ]
            .iter()
            .all(|f| ok(f))
        {
            return Err(Refusal::OperatorUnauthenticated);
        }
        let hold = self.grants().active.exclusive();
        let observed = self.grants().observe()?;
        let mut requirements_text = None;
        let mut registry_revision = None;
        match &request {
            AdminRequest::CreateWorkflow {
                key,
                task_id,
                initial_state,
                artifact_snapshot_id,
                artifact_digest,
            } => {
                let valid = writ_kernel::ids::is_id(&key.tenant_id)
                    && writ_kernel::ids::is_id(&key.project_id)
                    && writ_kernel::ids::is_name(&key.workflow_id)
                    && writ_kernel::ids::is_id(task_id)
                    && writ_kernel::ids::is_name(initial_state)
                    && writ_kernel::ids::is_id(artifact_snapshot_id)
                    && writ_kernel::ids::is_digest(artifact_digest);
                if !valid {
                    return Err(Refusal::AdminRequestInvalid);
                }
                // Never start in a protected state or the target of a protected edge.
                let protection = match hold.policy() {
                    Policy::Loaded(bundle) => bundle.protection().cloned(),
                    Policy::Absent | Policy::Rejected => None,
                };
                let protection = protection.ok_or(Refusal::Kernel("PROTECTION_UNAVAILABLE"))?;
                if protection
                    .protected_to_states
                    .iter()
                    .any(|s| s == initial_state)
                    || protection
                        .protected_transitions
                        .iter()
                        .any(|e| &e.to_state == initial_state)
                {
                    return Err(Refusal::WorkflowInitialStateProtected);
                }
                snapshot(snapshots, key, artifact_snapshot_id, artifact_digest)?;
            }
            AdminRequest::UpdateArtifact {
                key,
                artifact_snapshot_id,
                artifact_digest,
            } => {
                if !writ_kernel::ids::is_id(artifact_snapshot_id)
                    || !writ_kernel::ids::is_digest(artifact_digest)
                {
                    return Err(Refusal::AdminRequestInvalid);
                }
                snapshot(snapshots, key, artifact_snapshot_id, artifact_digest)?;
            }
            AdminRequest::ActivateAcceptancePolicy {
                requirements,
                effect_prerequisites,
                ..
            } => {
                AcceptanceRequirements::from_value(requirements)
                    .map_err(|_| Refusal::AdminRequestInvalid)?;
                let states_ok = effect_prerequisites.iter().all(|(kind, states)| {
                    writ_kernel::ids::is_kind(kind)
                        && !states.is_empty()
                        && states.iter().all(|s| writ_kernel::ids::is_name(s))
                });
                if !states_ok {
                    return Err(Refusal::AdminRequestInvalid);
                }
                requirements_text =
                    Some(json::canonical(requirements).map_err(|_| Refusal::AdminRequestInvalid)?);
            }
            AdminRequest::RegisterEvaluator { entry } => {
                RegistryEntry::validate(entry).map_err(|_| Refusal::AdminRequestInvalid)?;
                if entry.get("status").and_then(Value::as_str) != Some("active") {
                    return Err(Refusal::AdminRequestInvalid);
                }
                registry_revision = entry.get("registry_revision").and_then(Value::as_int);
            }
            AdminRequest::RevokeEvaluator { evaluator_id, .. } => {
                if !writ_kernel::ids::is_name(evaluator_id) {
                    return Err(Refusal::AdminRequestInvalid);
                }
            }
        }
        let change = AdminChange {
            request,
            operator,
            at: observed.at(),
            requirements_text,
            registry_revision,
        };
        let answer = self.grants().ledger.administer(&change);
        drop(hold);
        answer.map_err(|f| Refusal::LedgerUnavailable(f.error))?
    }
}

fn snapshot(
    snapshots: &dyn SnapshotStore,
    key: &WorkflowKey,
    id: &str,
    digest: &str,
) -> Result<(), Refusal> {
    match snapshots
        .check(&key.tenant_id, &key.project_id, id, digest)
        .map_err(|_| acceptance(AcceptanceCode::ArtifactIntegrityUnavailable))?
    {
        SnapshotCheck::Matches => Ok(()),
        SnapshotCheck::Differs | SnapshotCheck::Unknown => {
            Err(acceptance(AcceptanceCode::ArtifactIntegrityViolation))
        }
    }
}

/// The operation and the tenant and project an administrative request is
/// scoped to (an evaluator entry's own scope).
fn admin_scope(request: &AdminRequest) -> Option<(AdminOperation, String, String)> {
    let owned = |op, t: &str, p: &str| Some((op, t.to_string(), p.to_string()));
    match request {
        AdminRequest::CreateWorkflow { key, .. } => owned(
            AdminOperation::CreateWorkflow,
            &key.tenant_id,
            &key.project_id,
        ),
        AdminRequest::UpdateArtifact { key, .. } => owned(
            AdminOperation::UpdateArtifact,
            &key.tenant_id,
            &key.project_id,
        ),
        AdminRequest::ActivateAcceptancePolicy {
            tenant_id,
            project_id,
            ..
        } => owned(
            AdminOperation::ActivateAcceptancePolicy,
            tenant_id,
            project_id,
        ),
        AdminRequest::RevokeEvaluator {
            tenant_id,
            project_id,
            ..
        } => owned(AdminOperation::RevokeEvaluator, tenant_id, project_id),
        AdminRequest::RegisterEvaluator { entry } => {
            let scope = |k: &str| entry.get("scope")?.get(k)?.as_str();
            owned(
                AdminOperation::RegisterEvaluator,
                scope("tenant_id")?,
                scope("project_id")?,
            )
        }
    }
}

/// The workflow lock name of `key` within its tenant.
pub(crate) fn workflow_lock_name(key: &WorkflowKey) -> String {
    format!("{}/{}", key.project_id, key.workflow_id)
}
