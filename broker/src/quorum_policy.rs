//! Selection held by the trusted composition root for protected D-061 promotions.
use crate::refusal::Refusal;
use writ_kernel::contracts::AcceptanceRequirements;

/// Safety criteria derived by the selected signed policy verifier.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct QuorumRequirements {
    /// The established kernel projection, including the exact signed payload digest.
    pub acceptance: AcceptanceRequirements,
    /// Closed membership by criterion, then evaluator, with reviewed independence group.
    pub quorums: std::collections::BTreeMap<String, std::collections::BTreeMap<String, String>>,
}

/// Pins the signed selection and its verification authority through the transaction.
pub trait QuorumPolicyGuard: Sync {
    /// Recheck scope, signatures and validity at the current trusted time.
    fn current(
        &self,
        tenant: &str,
        project: &str,
        at: writ_kernel::time::Timestamp,
    ) -> Result<&QuorumRequirements, Refusal>;
}
