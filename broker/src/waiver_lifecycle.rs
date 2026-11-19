//! Scoped waiver ceremony and durable receipt interface for D-053.

use crate::approval::{ApprovalCode, ChallengeBinding, ChallengeOperation, ChallengeRefusal};
use crate::approval_store::{CeremonyBegin, CeremonyFinish, CeremonyOutcome, IssuedChallenge};
use crate::human::{
    check_actor, signoff_separation, AcceptedAssertion, Actor, Permission, SubjectId,
};
use crate::refusal::Refusal;
use crate::store::{StoreError, TxFailure};
use crate::waiver::WaiverBinding;
use crate::workflow::WaiverIntent;
use writ_kernel::json::CanonicalError;
use writ_kernel::time::Timestamp;

/// Durable receipt of one recorded `waived` result. A receipt is historical evidence.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct WaiverReceipt {
    /// Complete approved context, including the displayed expiry.
    pub binding: WaiverBinding,
    /// Human subject authenticated by the configured channel.
    pub approver: SubjectId,
    /// Credential used by the approver in the configured channel.
    pub credential_id: String,
    /// Consumed human ceremony, unique across the ledger.
    pub challenge_id: String,
    /// Trusted record time.
    pub recorded_at: Timestamp,
    /// Durable ledger-wide commit order.
    pub ledger_seq: i64,
}

/// A ceremony and the exact context the approver reviews, including the waiver expiry.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct WaiverChallenge {
    /// The existing human channel's client options and ceremony expiry.
    pub issued: IssuedChallenge,
    /// The exact context fingerprinted by the stored challenge.
    pub context: WaiverBinding,
}

/// SQLite waiver operations on the same ledger as workflow state and human authority.
pub trait WaiverLedger: Sync {
    /// Persist the exact server-derived context for display and review.
    fn request_waiver(
        &self,
        intent: &WaiverIntent<'_>,
    ) -> Result<Result<WaiverBinding, Refusal>, TxFailure>;

    /// Begin one purpose-bound ceremony for the previously displayed context.
    fn begin_waiver(
        &self,
        begin: &CeremonyBegin<'_>,
        intent: &WaiverIntent<'_>,
    ) -> Result<Result<WaiverChallenge, Refusal>, TxFailure>;

    /// Consume the human approval and record the waived result in one transaction.
    fn finish_waiver(
        &self,
        finish: &CeremonyFinish<'_>,
        intent: &WaiverIntent<'_>,
    ) -> Result<Result<CeremonyOutcome, Refusal>, TxFailure>;

    /// Resolve current eligibility of a reference against trusted state and signed policy.
    fn validate_waiver(
        &self,
        waiver_id: &str,
        intent: &WaiverIntent<'_>,
    ) -> Result<Result<WaiverBinding, Refusal>, TxFailure>;

    /// Look up the immutable receipt, including after a previously unknown commit.
    fn waiver_receipt(&self, waiver_id: &str) -> Result<Option<WaiverReceipt>, StoreError>;
}

/// Bind the existing human ceremony to the exact waiver context.
pub fn ceremony_binding(binding: &WaiverBinding) -> Result<ChallengeBinding, CanonicalError> {
    let fingerprint = binding.fingerprint()?;
    Ok(ChallengeBinding {
        operation: ChallengeOperation::Waive,
        tenant_id: binding.request.workflow.tenant_id.clone(),
        project_id: binding.request.workflow.project_id.clone(),
        target_id: fingerprint.as_str().to_string(),
        context_fingerprint: fingerprint,
        policy_bundle_sha: binding.policy.acceptance_digest.clone(),
        revision: binding.request.expected_revision,
    })
}

/// Check the accepted assertion and the ledger-read human role and exclusions.
pub fn check_waiver_approval(
    binding: &WaiverBinding,
    accepted: &AcceptedAssertion,
    actor: &Actor<'_>,
) -> Result<(), ApprovalCode> {
    let expected = ceremony_binding(binding)
        .map_err(|_| ApprovalCode::ChallengeInvalid(ChallengeRefusal::Malformed))?;
    if accepted.binding() != &expected {
        return Err(ApprovalCode::ChallengeInvalid(
            ChallengeRefusal::BindingMismatch,
        ));
    }
    if accepted.accepted_at() >= binding.expires_at {
        return Err(ApprovalCode::WaiverExpired);
    }
    check_actor(actor, accepted, Permission::WaiverAuthority)?;
    signoff_separation(
        accepted.subject(),
        actor.enrollment,
        &binding.exclusions.all(),
    )
}
