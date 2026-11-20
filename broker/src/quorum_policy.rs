//! Selection held by the trusted composition root for protected D-061 promotions.
use crate::refusal::Refusal;
use std::collections::BTreeMap;
use writ_kernel::contracts::AcceptanceRequirements;

/// Safety criteria derived by the selected signed policy verifier.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct QuorumRequirements {
    /// The established kernel projection, including the exact signed payload digest.
    pub(crate) acceptance: AcceptanceRequirements,
    /// Closed membership by criterion, then evaluator, with reviewed independence group.
    pub(crate) quorums: BTreeMap<String, BTreeMap<String, String>>,
}

impl QuorumRequirements {
    /// Construct the complete view at the trusted signed-policy verification boundary.
    pub fn new(
        acceptance: AcceptanceRequirements,
        quorums: BTreeMap<String, BTreeMap<String, String>>,
    ) -> Self {
        Self {
            acceptance,
            quorums,
        }
    }

    /// Ordinary consumers require a lossless projection of the selected policy.
    pub fn ordinary_requirements(&self) -> Option<&AcceptanceRequirements> {
        self.quorums.is_empty().then_some(&self.acceptance)
    }

    /// Required criterion metadata for publication preservation and deactivation checks.
    pub fn criteria(&self) -> &[writ_kernel::contracts::RequiredCriterion] {
        &self.acceptance.criteria
    }

    /// Signed membership by criterion.
    pub fn quorums(&self) -> &BTreeMap<String, BTreeMap<String, String>> {
        &self.quorums
    }
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
