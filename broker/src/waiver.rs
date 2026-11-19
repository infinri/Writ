//! Exact D-053 waiver approval context and trusted signed-policy interface.

use std::collections::BTreeMap;

use writ_kernel::{
    contracts::AcceptanceRequirements,
    ids::{is_digest, is_id, is_name},
    json::{self, CanonicalError, Digest, Value, MAX_SAFE_INTEGER},
    time::Timestamp,
};

use crate::{
    approval::SignoffExclusions, clock::ObservedTime, human::SubjectId, refusal::Refusal,
    workflow::WorkflowKey,
};

/// Closed approval purpose, separate from grants, signoffs and publication.
pub const WAIVER_PURPOSE: &str = "writ-human-criterion-waiver/1";
/// Maximum waiver lifetime in seconds from the trusted request sample.
pub const WAIVER_MAX_SECONDS: u32 = 600;

/// Scope and mutation requested through trusted ingress.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct WaiverRequest {
    /// Exact workflow in its tenant and project.
    pub workflow: WorkflowKey,
    /// Expected source state.
    pub from_state: String,
    /// Exact destination state.
    pub to_state: String,
    /// Required stored workflow revision.
    pub expected_revision: i64,
    /// Criterion the policy must explicitly permit.
    pub criterion_id: String,
    /// Human-readable reason shown to the approver.
    pub reason: String,
}

impl WaiverRequest {
    /// Validate the closed request's field domains before resolution.
    pub fn well_formed(&self) -> bool {
        is_id(&self.workflow.tenant_id)
            && is_id(&self.workflow.project_id)
            && is_name(&self.workflow.workflow_id)
            && is_name(&self.from_state)
            && is_name(&self.to_state)
            && self.from_state != self.to_state
            && (0..=MAX_SAFE_INTEGER).contains(&self.expected_revision)
            && is_name(&self.criterion_id)
            && !self.reason.trim().is_empty()
            && self.reason.chars().count() <= 500
            && !self.reason.chars().any(char::is_control)
    }
}

/// Exact signed D-051 selection identity.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct WaiverPolicyIdentity {
    /// Selection tenant.
    pub tenant_id: String,
    /// Selection project.
    pub project_id: String,
    /// Logical digest of the signed authorization bundle.
    pub authorization_digest: String,
    /// Digest of the signed acceptance policy including waiver rules.
    pub acceptance_digest: String,
    /// Durable monotonic activation revision.
    pub activation_revision: i64,
    /// Digest of both envelopes, their payloads and prerequisites.
    pub publication_digest: String,
}

impl WaiverPolicyIdentity {
    fn well_formed(&self) -> bool {
        is_id(&self.tenant_id)
            && is_id(&self.project_id)
            && is_digest(&self.authorization_digest)
            && is_digest(&self.acceptance_digest)
            && is_digest(&self.publication_digest)
            && (1..=MAX_SAFE_INTEGER).contains(&self.activation_revision)
    }

    fn value(&self) -> Value {
        json::obj([
            ("tenant_id", json::s(&self.tenant_id)),
            ("project_id", json::s(&self.project_id)),
            ("authorization_digest", json::s(&self.authorization_digest)),
            ("acceptance_digest", json::s(&self.acceptance_digest)),
            ("activation_revision", Value::Int(self.activation_revision)),
            ("publication_digest", json::s(&self.publication_digest)),
        ])
    }

    fn read(v: &Value) -> Option<Self> {
        let identity = Self {
            tenant_id: text(v, "tenant_id")?,
            project_id: text(v, "project_id")?,
            authorization_digest: text(v, "authorization_digest")?,
            acceptance_digest: text(v, "acceptance_digest")?,
            activation_revision: v.get("activation_revision")?.as_int()?,
            publication_digest: text(v, "publication_digest")?,
        };
        (identity.well_formed() && identity.value() == *v).then_some(identity)
    }
}

/// A trust boundary supplied by the configured signed registry, never ingress.
/// Its implementation pins selection and authority for the entire ledger operation.
pub trait WaiverPolicyGuard: Sync {
    /// Reverify signatures, current keys and time under the pinned selection.
    fn current(&self, now: ObservedTime) -> Result<WaiverPolicyView, Refusal>;
}

/// A verified observation from the pinned authority.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct WaiverPolicyView {
    identity: WaiverPolicyIdentity,
    permits: BTreeMap<String, u32>,
    publishers: Vec<SubjectId>,
    checked_at: Timestamp,
    expires_at: Timestamp,
    requirements: AcceptanceRequirements,
}

impl WaiverPolicyView {
    /// Build the observation from configured policy authority. Ingress cannot
    /// construct or replace the configured authority through a request.
    pub fn new(
        identity: WaiverPolicyIdentity,
        permits: BTreeMap<String, u32>,
        mut publishers: Vec<SubjectId>,
        checked_at: Timestamp,
        expires_at: Timestamp,
        requirements: AcceptanceRequirements,
    ) -> Result<Self, Refusal> {
        if !identity.well_formed()
            || requirements.policy.acceptance_policy_bundle_sha != identity.acceptance_digest
            || checked_at >= expires_at
            || permits.iter().any(|(criterion, seconds)| {
                !is_name(criterion)
                    || !(1..=WAIVER_MAX_SECONDS).contains(seconds)
                    || !requirements
                        .criteria
                        .iter()
                        .any(|required| required.criterion_id == *criterion)
            })
            || publishers.is_empty()
            || !publishers.iter().all(SubjectId::is_well_formed)
        {
            return Err(Refusal::AcceptancePolicyUnavailable);
        }
        publishers.sort();
        publishers.dedup();
        Ok(Self {
            identity,
            permits,
            publishers,
            checked_at,
            expires_at,
            requirements,
        })
    }

    /// Pinned selection identity.
    pub fn identity(&self) -> &WaiverPolicyIdentity {
        &self.identity
    }

    /// Signed validity limit for an explicitly waivable project criterion.
    pub fn permits(&self, criterion_id: &str) -> Option<u32> {
        self.permits.get(criterion_id).copied()
    }

    /// Exact mandatory requirement set projected from the signed payload.
    pub fn requirements(&self) -> &AcceptanceRequirements {
        &self.requirements
    }

    /// Qualified publishers recovered from the pinned registry audit.
    pub fn publishers(&self) -> &[SubjectId] {
        &self.publishers
    }

    /// Trusted time of the last signature and key check.
    pub fn checked_at(&self) -> Timestamp {
        self.checked_at
    }

    /// Earliest exclusive expiry across the two signatures and verification keys.
    pub fn expires_at(&self) -> Timestamp {
        self.expires_at
    }
}

/// Closed context shown before the ceremony and stored unchanged on recording.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct WaiverBinding {
    /// Exact ingress request.
    pub request: WaiverRequest,
    /// Trusted task of the stored workflow.
    pub task_id: String,
    /// Immutable stored artifact snapshot identity.
    pub artifact_snapshot_id: String,
    /// Verified digest of the stored snapshot.
    pub artifact_digest: String,
    /// Exact signed D-051 policy selection.
    pub policy: WaiverPolicyIdentity,
    /// Independently fenced authorization digest for this transition.
    pub workflow_authorization_digest: String,
    /// Qualified requester from authenticated ingress.
    pub requester: SubjectId,
    /// Exclusion parties derived from trusted ingress and ledger state.
    pub exclusions: SignoffExclusions,
    /// Exclusive, server-computed expiry displayed to the approver.
    pub expires_at: Timestamp,
}

fn text(v: &Value, name: &str) -> Option<String> {
    Some(v.get(name)?.as_str()?.to_owned())
}

impl WaiverBinding {
    /// The exact displayed D-006 document.
    pub fn value(&self) -> Value {
        let request = &self.request;
        json::obj([
            ("purpose", json::s(WAIVER_PURPOSE)),
            ("tenant_id", json::s(&request.workflow.tenant_id)),
            ("project_id", json::s(&request.workflow.project_id)),
            ("task_id", json::s(&self.task_id)),
            ("workflow_id", json::s(&request.workflow.workflow_id)),
            ("from_state", json::s(&request.from_state)),
            ("to_state", json::s(&request.to_state)),
            ("expected_revision", Value::Int(request.expected_revision)),
            ("criterion_id", json::s(&request.criterion_id)),
            ("artifact_snapshot_id", json::s(&self.artifact_snapshot_id)),
            ("artifact_digest", json::s(&self.artifact_digest)),
            ("policy", self.policy.value()),
            (
                "workflow_authorization_digest",
                json::s(&self.workflow_authorization_digest),
            ),
            (
                "requester",
                json::obj([
                    ("issuer", json::s(&self.requester.issuer)),
                    ("subject", json::s(&self.requester.subject)),
                ]),
            ),
            ("exclusions", self.exclusions.context()),
            ("reason", json::s(&request.reason)),
            ("expires_at", json::s(&self.expires_at.to_string())),
        ])
    }

    /// Read the exact closed, canonicalizable binding from durable state.
    pub fn from_value(v: &Value) -> Option<Self> {
        let requester = v.get("requester")?;
        let binding = Self {
            request: WaiverRequest {
                workflow: WorkflowKey {
                    tenant_id: text(v, "tenant_id")?,
                    project_id: text(v, "project_id")?,
                    workflow_id: text(v, "workflow_id")?,
                },
                from_state: text(v, "from_state")?,
                to_state: text(v, "to_state")?,
                expected_revision: v.get("expected_revision")?.as_int()?,
                criterion_id: text(v, "criterion_id")?,
                reason: text(v, "reason")?,
            },
            task_id: text(v, "task_id")?,
            artifact_snapshot_id: text(v, "artifact_snapshot_id")?,
            artifact_digest: text(v, "artifact_digest")?,
            policy: WaiverPolicyIdentity::read(v.get("policy")?)?,
            workflow_authorization_digest: text(v, "workflow_authorization_digest")?,
            requester: SubjectId::new(&text(requester, "issuer")?, &text(requester, "subject")?),
            exclusions: SignoffExclusions::from_context(v.get("exclusions")?)?,
            expires_at: Timestamp::parse_rfc3339(v.get("expires_at")?.as_str()?)?,
        };
        (binding.request.well_formed()
            && is_id(&binding.task_id)
            && is_id(&binding.artifact_snapshot_id)
            && is_digest(&binding.artifact_digest)
            && is_digest(&binding.workflow_authorization_digest)
            && binding.policy.tenant_id == binding.request.workflow.tenant_id
            && binding.policy.project_id == binding.request.workflow.project_id
            && binding.requester.is_well_formed()
            && binding.exclusions.requesters == [binding.requester.clone()]
            && binding.exclusions.added.is_empty()
            && binding.value() == *v)
            .then_some(binding)
    }

    /// Fingerprint of the approval context as displayed.
    pub fn fingerprint(&self) -> Result<Digest, CanonicalError> {
        json::digest(&self.value())
    }
}
