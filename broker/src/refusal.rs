//! Broker refusals of the decision-free core (M3-02), of approvals and grant
//! consumption (M3-03) and of the execute lifecycle (M3-04).
//!
//! A refusal carries the internal code that goes to the access-controlled
//! audit record. Agents and hosts receive only [`Refusal::external_code`], the
//! kernel's agent-facing mapping (D-018 A7, D-020).

use std::fmt;
use writ_kernel::contracts::{DecisionStatus, ReasonCode};
use writ_kernel::refusal::external_code;

use crate::acceptance::AcceptanceCode;
use crate::grants::ApproverRefusal;
use crate::store::StoreError;

/// Which check detected a promotion's revision conflict (D-038 item 2).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ConflictStage {
    /// The use-time kernel decision before the promotion transaction
    /// (kernel `STATE_REVISION_CONFLICT`, normalized at the broker boundary).
    UseTimeDecision,
    /// The compare-and-set inside the fenced promotion transaction.
    Transaction,
    /// The stored revision cannot be represented or incremented.
    StoredRevision,
}

/// Why the broker refused a request or a permit.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Refusal {
    /// The seal is absent, malformed, forged, made by an unregistered adapter
    /// or another adapter than `principal.verified_by`, or the request was
    /// altered after sealing (D-010).
    IngressUnauthenticated,
    /// The use-time kernel decision is neither `allow` nor `require_approval`;
    /// the code is the decision's `reason_code`.
    Decision {
        /// Decision status.
        status: DecisionStatus,
        /// Decision reason (kernel code or the deciding statement's code).
        reason: ReasonCode,
    },
    /// The permit's request fingerprint differs from the presented request (D-012).
    PermitRequestMismatch,
    /// The permit is malformed or its MAC does not verify under the permit key.
    PermitInvalid,
    /// The trusted time is at or after the permit's expiry.
    PermitExpired,
    /// The permit is bound to a policy bundle digest other than the currently
    /// active authorization bundle, or no verified bundle is active (D-018 A3,
    /// owner decision A of 2026-11-12). Carries no digest: the refusal never
    /// reveals the active bundle.
    PermitStale,
    /// The permit id was already consumed by the effect adapter (M3-04): a
    /// permit authorizes at most one effect.
    PermitReplayed,
    /// Trusted time could not be established: a clock-source failure, an
    /// overflow, an unacknowledged startup rollback or a persisted-floor
    /// failure ([`crate::clock::ClockError`]). Fail closed; externally the
    /// kernel's generic mapping.
    TimeUnavailable,
    /// The use-time decision is `require_approval` and no grant was presented.
    ApprovalRequired,
    /// The approval was not presented by an authenticated human on a trusted
    /// approval channel ([`ApproverRefusal`] says why; operator diagnostic only).
    HumanUnauthenticated(ApproverRefusal),
    /// The use-time decision for the request to approve is not `require_approval`.
    NotApprovable,
    /// The approver is the request's principal or agent.
    SelfApprovalForbidden,
    /// The grant label already names a grant; that grant is never reset.
    GrantRefInUse,
    /// No grant has the presented label.
    GrantUnknown,
    /// The grant's binding differs from the request and its use-time decision.
    GrantMismatch,
    /// The trusted time is at or after the grant's expiry.
    GrantExpired,
    /// The grant has no use left.
    GrantExhausted,
    /// A receipt for the (tenant, idempotency key) binds another request
    /// fingerprint or another effective grant (D-012).
    IdempotencyConflict,
    /// The receipt's effect may have started (`dispatched` or
    /// `unknown_reconcile`); only reconciliation may proceed.
    ReconciliationRequired,
    /// The ledger could not complete the operation (fail closed, D-024). The
    /// [`StoreError`] is the sanitized operator diagnostic.
    LedgerUnavailable(StoreError),
    /// A kernel structure code reported outside a decision (M3-05).
    Kernel(&'static str),
    /// Acceptance-record, attestation, registry or snapshot refusal (M3-05, D-036).
    Acceptance(AcceptanceCode),
    /// The request is not this operation's kind or resource (M3-05).
    RequestScopeMismatch,
    /// No such workflow (M3-05).
    WorkflowUnknown,
    /// The workflow already exists (administration, M3-05).
    WorkflowExists,
    /// Expected revision or source state differ from the stored workflow
    /// (D-038 item 2: one broker code for the stale expected revision and the
    /// transactional compare-and-set). The [`ConflictStage`] records which
    /// check detected it (operator diagnostic only; never externally visible).
    RevisionConflict(ConflictStage),
    /// The project has no usable active acceptance policy (fail closed).
    AcceptancePolicyUnavailable,
    /// The authorization bundle's protection names another acceptance policy.
    PolicyDependencyMismatch,
    /// A workflow cannot be created in a protected state (D-036 item 5).
    WorkflowInitialStateProtected,
    /// An administrative request is malformed.
    AdminRequestInvalid,
    /// The operator was not authenticated (operator diagnostic).
    OperatorUnauthenticated,
    /// The operator is not authorized for the operation, tenant or project.
    OperatorUnauthorized,
    /// A registered evaluator entry must have a higher registry revision.
    RegistryRevisionStale,
    /// Effect prerequisite: required promotion attributes absent, or the
    /// workflow has no matching promotion receipt (D-019 N-3).
    PromotionReceiptInvalid,
    /// Effect prerequisite: the workflow is not in a required state.
    EffectPrerequisiteUnmet,
    /// Effect prerequisite: the requested artifact is not the promoted one.
    EffectArtifactMismatch,
    /// Effect prerequisite: the promotion's artifact or policies are no
    /// longer current.
    PromotionStale,
}

impl Refusal {
    /// Internal code (audit record and test harnesses only).
    pub fn code(&self) -> &str {
        match self {
            Refusal::IngressUnauthenticated => "INGRESS_UNAUTHENTICATED",
            Refusal::Decision { reason, .. } => reason.as_str(),
            Refusal::PermitRequestMismatch => "PERMIT_REQUEST_MISMATCH",
            Refusal::PermitInvalid => "PERMIT_INVALID",
            Refusal::PermitExpired => "PERMIT_EXPIRED",
            Refusal::PermitStale => "PERMIT_STALE",
            Refusal::PermitReplayed => "PERMIT_REPLAYED",
            Refusal::TimeUnavailable => "TIME_UNAVAILABLE",
            Refusal::ApprovalRequired => "APPROVAL_REQUIRED",
            Refusal::HumanUnauthenticated(_) => "HUMAN_UNAUTHENTICATED",
            Refusal::NotApprovable => "NOT_APPROVABLE",
            Refusal::SelfApprovalForbidden => "SELF_APPROVAL_FORBIDDEN",
            Refusal::GrantRefInUse => "GRANT_REF_IN_USE",
            Refusal::GrantUnknown => "GRANT_UNKNOWN",
            Refusal::GrantMismatch => "GRANT_MISMATCH",
            Refusal::GrantExpired => "GRANT_EXPIRED",
            Refusal::GrantExhausted => "GRANT_EXHAUSTED",
            Refusal::IdempotencyConflict => "IDEMPOTENCY_CONFLICT",
            Refusal::ReconciliationRequired => "RECONCILIATION_REQUIRED",
            Refusal::LedgerUnavailable(_) => StoreError::EXTERNAL_CODE,
            Refusal::Kernel(code) => code,
            Refusal::Acceptance(code) => code.code(),
            Refusal::RequestScopeMismatch => "REQUEST_SCOPE_MISMATCH",
            Refusal::WorkflowUnknown => "WORKFLOW_UNKNOWN",
            Refusal::WorkflowExists => "WORKFLOW_EXISTS",
            Refusal::RevisionConflict(_) => "REVISION_CONFLICT",
            Refusal::AcceptancePolicyUnavailable => "ACCEPTANCE_POLICY_UNAVAILABLE",
            Refusal::PolicyDependencyMismatch => "POLICY_DEPENDENCY_MISMATCH",
            Refusal::WorkflowInitialStateProtected => "WORKFLOW_INITIAL_STATE_PROTECTED",
            Refusal::AdminRequestInvalid => "ADMIN_REQUEST_INVALID",
            Refusal::OperatorUnauthenticated => "OPERATOR_UNAUTHENTICATED",
            Refusal::OperatorUnauthorized => "OPERATOR_UNAUTHORIZED",
            Refusal::RegistryRevisionStale => "REGISTRY_REVISION_STALE",
            Refusal::PromotionReceiptInvalid => "PROMOTION_RECEIPT_INVALID",
            Refusal::EffectPrerequisiteUnmet => "EFFECT_PREREQUISITE_UNMET",
            Refusal::EffectArtifactMismatch => "EFFECT_ARTIFACT_MISMATCH",
            Refusal::PromotionStale => "PROMOTION_STALE",
        }
    }

    /// Operator diagnostic for the check that originated a normalized refusal
    /// (D-038 item 2), `None` when the code says everything.
    pub fn diagnostic(&self) -> Option<&'static str> {
        match self {
            Refusal::RevisionConflict(ConflictStage::UseTimeDecision) => {
                Some("revision conflict: use-time decision (kernel STATE_REVISION_CONFLICT)")
            }
            Refusal::RevisionConflict(ConflictStage::Transaction) => {
                Some("revision conflict: promotion transaction compare-and-set")
            }
            Refusal::RevisionConflict(ConflictStage::StoredRevision) => {
                Some("revision conflict: stored revision not representable")
            }
            _ => None,
        }
    }

    /// Agent-facing code (the kernel's external refusal mapping).
    pub fn external_code(&self) -> &'static str {
        external_code(self.code())
    }
}

impl fmt::Display for Refusal {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.code())
    }
}

impl std::error::Error for Refusal {}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;
    use writ_kernel::contracts::KernelCode;

    #[test]
    fn codes_and_external_mapping() {
        let cases = [
            (
                Refusal::IngressUnauthenticated,
                "INGRESS_UNAUTHENTICATED",
                "PROMOTION_REFUSED",
            ),
            (
                Refusal::PermitRequestMismatch,
                "PERMIT_REQUEST_MISMATCH",
                "PROMOTION_REFUSED",
            ),
            (
                Refusal::PermitInvalid,
                "PERMIT_INVALID",
                "PROMOTION_REFUSED",
            ),
            (
                Refusal::PermitExpired,
                "PERMIT_EXPIRED",
                "PROMOTION_REFUSED",
            ),
            (Refusal::PermitStale, "PERMIT_STALE", "PROMOTION_REFUSED"),
            (
                Refusal::PermitReplayed,
                "PERMIT_REPLAYED",
                "PROMOTION_REFUSED",
            ),
            (
                Refusal::TimeUnavailable,
                "TIME_UNAVAILABLE",
                "PROMOTION_REFUSED",
            ),
            (
                Refusal::Decision {
                    status: DecisionStatus::Invalid,
                    reason: ReasonCode::Kernel(KernelCode::CompositeNotSupported),
                },
                "COMPOSITE_NOT_SUPPORTED",
                "COMPOSITE_NOT_SUPPORTED",
            ),
            (
                Refusal::Decision {
                    status: DecisionStatus::Deny,
                    reason: ReasonCode::Policy("SECRETS_PATH_FORBIDDEN".into()),
                },
                "SECRETS_PATH_FORBIDDEN",
                "PROMOTION_REFUSED",
            ),
        ];
        for (refusal, internal, external) in cases {
            assert_eq!(refusal.code(), internal);
            assert_eq!(refusal.to_string(), internal);
            assert_eq!(refusal.external_code(), external, "{internal}");
        }
    }

    #[test]
    fn permit_stale_uses_the_kernel_generic_mapping() {
        // Not a pass-through code of the kernel: the generic external refusal.
        assert!(!writ_kernel::refusal::PASS_THROUGH.contains(&"PERMIT_STALE"));
        assert_eq!(external_code("PERMIT_STALE"), "PROMOTION_REFUSED");
        assert_eq!(
            Refusal::PermitStale.external_code(),
            writ_kernel::refusal::PROMOTION_REFUSED
        );
    }

    #[test]
    fn grant_and_idempotency_codes_and_their_external_mapping() {
        let cases = [
            (
                Refusal::ApprovalRequired,
                "APPROVAL_REQUIRED",
                "APPROVAL_REQUIRED",
            ),
            (
                Refusal::HumanUnauthenticated(ApproverRefusal::NotHuman),
                "HUMAN_UNAUTHENTICATED",
                "PROMOTION_REFUSED",
            ),
            (
                Refusal::NotApprovable,
                "NOT_APPROVABLE",
                "PROMOTION_REFUSED",
            ),
            (
                Refusal::SelfApprovalForbidden,
                "SELF_APPROVAL_FORBIDDEN",
                "PROMOTION_REFUSED",
            ),
            (
                Refusal::GrantRefInUse,
                "GRANT_REF_IN_USE",
                "PROMOTION_REFUSED",
            ),
            (Refusal::GrantUnknown, "GRANT_UNKNOWN", "PROMOTION_REFUSED"),
            (
                Refusal::GrantMismatch,
                "GRANT_MISMATCH",
                "PROMOTION_REFUSED",
            ),
            (Refusal::GrantExpired, "GRANT_EXPIRED", "PROMOTION_REFUSED"),
            (
                Refusal::GrantExhausted,
                "GRANT_EXHAUSTED",
                "PROMOTION_REFUSED",
            ),
            (
                Refusal::IdempotencyConflict,
                "IDEMPOTENCY_CONFLICT",
                "IDEMPOTENCY_CONFLICT",
            ),
            (
                Refusal::ReconciliationRequired,
                "RECONCILIATION_REQUIRED",
                "RECONCILIATION_REQUIRED",
            ),
            (
                Refusal::LedgerUnavailable(StoreError::OwnedElsewhere),
                "LEDGER_UNAVAILABLE",
                "PROMOTION_REFUSED",
            ),
        ];
        for (refusal, internal, external) in cases {
            assert_eq!(refusal.code(), internal);
            assert_eq!(refusal.external_code(), external, "{internal}");
        }
    }

    #[test]
    fn every_revision_conflict_stage_has_one_code_and_a_distinct_operator_diagnostic() {
        let stages = [
            ConflictStage::UseTimeDecision,
            ConflictStage::Transaction,
            ConflictStage::StoredRevision,
        ];
        let mut diagnostics = Vec::new();
        for stage in stages {
            let refusal = Refusal::RevisionConflict(stage);
            assert_eq!(refusal.code(), "REVISION_CONFLICT");
            assert_eq!(refusal.to_string(), "REVISION_CONFLICT");
            // The stage is never externally visible: the generic mapping.
            assert_eq!(
                refusal.external_code(),
                writ_kernel::refusal::PROMOTION_REFUSED
            );
            let diagnostic = refusal.diagnostic().unwrap();
            assert!(diagnostic.starts_with("revision conflict: "));
            assert!(!diagnostics.contains(&diagnostic));
            diagnostics.push(diagnostic);
        }
        // Unrelated refusals, including the kernel's own code, are not remapped.
        let kernel = Refusal::Decision {
            status: DecisionStatus::Invalid,
            reason: ReasonCode::Kernel(KernelCode::StateRevisionConflict),
        };
        assert_eq!(kernel.code(), "STATE_REVISION_CONFLICT");
        assert_eq!(kernel.diagnostic(), None);
        assert_eq!(Refusal::WorkflowUnknown.diagnostic(), None);
    }

    #[test]
    fn time_unavailable_uses_the_kernel_generic_mapping() {
        assert!(!writ_kernel::refusal::PASS_THROUGH.contains(&"TIME_UNAVAILABLE"));
        assert_eq!(
            Refusal::TimeUnavailable.external_code(),
            writ_kernel::refusal::PROMOTION_REFUSED
        );
    }
}
