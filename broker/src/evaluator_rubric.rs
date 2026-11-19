//! Validate pack rubric proposals against entries from the trusted evaluator
//! registry. A binding is eligibility metadata. It cannot change policy,
//! register an evaluator, provision a key, or serve as acceptance evidence.

use std::collections::BTreeSet;

use writ_kernel::contracts::StateRevision;
use writ_kernel::ids::{is_id, is_name};
use writ_kernel::json::Value;

use crate::contracts::{is_version, RegistryEntry};
use crate::workflow::SourceUnavailable;

/// Contract for one pack rubric's reference to one evaluator and its criteria.
pub const CONTRACT: &str = "pack-rubric-binding/1";

/// A structurally valid pack proposal.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RubricBinding {
    proposal: Value,
}

/// Result of checking a proposal against the current registry entry.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RubricEligibility {
    /// The proposed criteria may be evaluated subject to trusted acceptance policy
    /// and the normal evidence checks at use time.
    Eligible,
    /// Results from this evaluator are advisory.
    AdvisoryOnly,
}

/// Why a proposed rubric binding was refused.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RubricError {
    /// The proposal is not a closed, valid binding document.
    Malformed,
    /// The proposal or the registry entry names a different scope.
    ScopeMismatch,
    /// The trusted registry could not be read.
    RegistryUnavailable,
    /// The evaluator is absent in the trusted scope.
    EvaluatorUnknown,
    /// The trusted registry entry is revoked or otherwise inactive.
    EvaluatorInactive,
    /// The evaluator identity, version, class or revision does not match.
    EvaluatorMismatch,
    /// A proposed criterion is absent from the registry entry.
    CriterionMismatch,
}

const FIELDS: [&str; 12] = [
    "contract_version",
    "pack_id",
    "pack_version",
    "rubric_id",
    "rubric_version",
    "tenant_id",
    "project_id",
    "evaluator_id",
    "evaluator_version",
    "class",
    "registry_revision",
    "criteria",
];

fn pack_id(value: &str) -> bool {
    let mut bytes = value.bytes();
    (2..=64).contains(&value.len())
        && bytes
            .next()
            .is_some_and(|first| first.is_ascii_lowercase() || first.is_ascii_digit())
        && bytes.all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || b"._-".contains(&byte)
        })
}

impl RubricBinding {
    /// Check the closed wire format. The caller must separately check the
    /// binding against trusted registry state.
    pub fn validate(value: &Value) -> Result<Self, RubricError> {
        let fields = value.as_obj().ok_or(RubricError::Malformed)?;
        if fields.len() != FIELDS.len() || !FIELDS.iter().all(|key| fields.contains_key(*key)) {
            return Err(RubricError::Malformed);
        }
        let text = |name| value.get(name).and_then(Value::as_str);
        let criteria = value
            .get("criteria")
            .and_then(Value::as_arr)
            .ok_or(RubricError::Malformed)?;
        let mut unique = BTreeSet::new();
        let revision = value
            .get("registry_revision")
            .and_then(Value::as_int)
            .filter(|revision| *revision > 0)
            .and_then(StateRevision::new);
        if text("contract_version") != Some(CONTRACT)
            || !text("pack_id").is_some_and(pack_id)
            || !["pack_version", "rubric_version", "evaluator_version"]
                .iter()
                .all(|key| text(key).is_some_and(is_version))
            || !["rubric_id", "evaluator_id"]
                .iter()
                .all(|key| text(key).is_some_and(is_name))
            || !["tenant_id", "project_id"]
                .iter()
                .all(|key| text(key).is_some_and(is_id))
            || !matches!(
                text("class"),
                Some("mechanical" | "evidence" | "human" | "model_assisted")
            )
            || revision.is_none()
            || criteria.is_empty()
            || !criteria.iter().all(|value| {
                value
                    .as_str()
                    .is_some_and(|id| is_name(id) && unique.insert(id))
            })
        {
            return Err(RubricError::Malformed);
        }
        Ok(Self {
            proposal: value.clone(),
        })
    }

    /// Compare the proposal with the current trusted registry entry. The
    /// caller supplies the workflow's tenant and project and a trusted lookup.
    pub fn check(
        &self,
        tenant_id: &str,
        project_id: &str,
        lookup: impl FnOnce(&str, &str, &str) -> Result<Option<RegistryEntry>, SourceUnavailable>,
    ) -> Result<RubricEligibility, RubricError> {
        let scope_matches = |value: &Value| {
            value.get("tenant_id").and_then(Value::as_str) == Some(tenant_id)
                && value.get("project_id").and_then(Value::as_str) == Some(project_id)
        };
        if !scope_matches(&self.proposal) {
            return Err(RubricError::ScopeMismatch);
        }
        let evaluator = self
            .proposal
            .get("evaluator_id")
            .and_then(Value::as_str)
            .ok_or(RubricError::Malformed)?;
        let entry = lookup(tenant_id, project_id, evaluator)
            .map_err(|_| RubricError::RegistryUnavailable)?
            .ok_or(RubricError::EvaluatorUnknown)?;
        let entry = entry.as_value();
        if !entry.get("scope").is_some_and(scope_matches) {
            return Err(RubricError::ScopeMismatch);
        }
        if entry.get("status").and_then(Value::as_str) != Some("active") {
            return Err(RubricError::EvaluatorInactive);
        }
        if [
            "evaluator_id",
            "evaluator_version",
            "class",
            "registry_revision",
        ]
        .iter()
        .any(|key| self.proposal.get(key) != entry.get(key))
        {
            return Err(RubricError::EvaluatorMismatch);
        }
        let criteria = self.proposal.get("criteria").and_then(Value::as_arr);
        let registered = entry.get("criteria").and_then(Value::as_arr);
        if !criteria
            .zip(registered)
            .is_some_and(|(criteria, registered)| {
                criteria
                    .iter()
                    .all(|criterion| registered.contains(criterion))
            })
        {
            return Err(RubricError::CriterionMismatch);
        }
        Ok(
            if entry.get("class").and_then(Value::as_str) == Some("model_assisted") {
                RubricEligibility::AdvisoryOnly
            } else {
                RubricEligibility::Eligible
            },
        )
    }
}
