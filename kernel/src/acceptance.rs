//! Pure acceptance-record check (M2-05; ADR-0008 design approved in
//! principle; semantics `m1/fixtures/acceptance/SEMANTICS.md`, m1-draft-3).
//!
//! Answers: does this untrusted record, as given, satisfy the trusted,
//! version-bound requirement set for this exact transition in this exact
//! broker state? Trusted inputs are validated first so that broken registry
//! or broker state fails closed before any record is examined.
//!
//! Not covered (boundary 2): `record_digest` is an unkeyed consistency check,
//! not proof of issuance; trust labels are claims, never proof of evaluator
//! identity or registration. Artifact bytes are not examined (boundary 4).

use crate::contracts::{
    AcceptanceRecord, AcceptanceRequirements, CriterionStatus, CurrentState, RecordStatus,
};
use crate::json::Value;
use std::fmt;

/// Internal refusal codes of the acceptance check, in check order.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AcceptanceRefusal {
    /// Trusted broker state malformed.
    CurrentStateMalformed,
    /// Trusted requirement set malformed.
    RequirementsMalformed,
    /// Requirement set belongs to another acceptance policy than the active one.
    RequirementsPolicyMismatch,
    /// Record malformed.
    RecordMalformed,
    /// Digest mismatch, reversed validity or future issuance; or not accepted.
    RecordInvalid,
    /// Record already consumed.
    Replayed,
    /// Tenant, project, task or workflow differ.
    ScopeMismatch,
    /// Transition differs.
    TransitionMismatch,
    /// Artifact snapshot or digest differ.
    ArtifactMismatch,
    /// Acceptance policy differs.
    PolicyMismatch,
    /// Workflow revision differs.
    RevisionMismatch,
    /// Record expired.
    Expired,
    /// A required criterion is absent.
    CriteriaMissing,
    /// A required criterion is downgraded or re-classed, or an extra mandatory one is present.
    CriteriaMismatch,
    /// Evaluator trust label does not match the class.
    UntrustedEvaluator,
    /// Evaluator revoked.
    EvaluatorRevoked,
    /// Result stale.
    Stale,
    /// Result fail, unknown or waived.
    Incomplete,
}

impl AcceptanceRefusal {
    /// Internal contract code.
    pub fn code(self) -> &'static str {
        match self {
            AcceptanceRefusal::CurrentStateMalformed => "CURRENT_STATE_MALFORMED",
            AcceptanceRefusal::RequirementsMalformed => "REQUIREMENTS_MALFORMED",
            AcceptanceRefusal::RequirementsPolicyMismatch => "REQUIREMENTS_POLICY_MISMATCH",
            AcceptanceRefusal::RecordMalformed => "ACCEPTANCE_RECORD_MALFORMED",
            AcceptanceRefusal::RecordInvalid => "ACCEPTANCE_RECORD_INVALID",
            AcceptanceRefusal::Replayed => "ACCEPTANCE_REPLAYED",
            AcceptanceRefusal::ScopeMismatch => "ACCEPTANCE_SCOPE_MISMATCH",
            AcceptanceRefusal::TransitionMismatch => "ACCEPTANCE_TRANSITION_MISMATCH",
            AcceptanceRefusal::ArtifactMismatch => "ACCEPTANCE_ARTIFACT_MISMATCH",
            AcceptanceRefusal::PolicyMismatch => "ACCEPTANCE_POLICY_MISMATCH",
            AcceptanceRefusal::RevisionMismatch => "ACCEPTANCE_REVISION_MISMATCH",
            AcceptanceRefusal::Expired => "ACCEPTANCE_EXPIRED",
            AcceptanceRefusal::CriteriaMissing => "ACCEPTANCE_CRITERIA_MISSING",
            AcceptanceRefusal::CriteriaMismatch => "ACCEPTANCE_CRITERIA_MISMATCH",
            AcceptanceRefusal::UntrustedEvaluator => "ACCEPTANCE_UNTRUSTED_EVALUATOR",
            AcceptanceRefusal::EvaluatorRevoked => "ACCEPTANCE_EVALUATOR_REVOKED",
            AcceptanceRefusal::Stale => "ACCEPTANCE_STALE",
            AcceptanceRefusal::Incomplete => "ACCEPTANCE_INCOMPLETE",
        }
    }
}

impl fmt::Display for AcceptanceRefusal {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.code())
    }
}

impl std::error::Error for AcceptanceRefusal {}

/// Check order steps 1 to 4 (shape and requirement binding), then
/// [`check`]. Absent inputs are malformed.
pub fn check_values(
    record: Option<&Value>,
    current: Option<&Value>,
    requirements: Option<&Value>,
) -> Result<(), AcceptanceRefusal> {
    let current = current
        .and_then(|v| CurrentState::from_value(v).ok())
        .ok_or(AcceptanceRefusal::CurrentStateMalformed)?;
    let requirements = requirements
        .and_then(|v| AcceptanceRequirements::from_value(v).ok())
        .ok_or(AcceptanceRefusal::RequirementsMalformed)?;
    if requirements.policy != current.policy {
        return Err(AcceptanceRefusal::RequirementsPolicyMismatch);
    }
    let record = record
        .and_then(|v| AcceptanceRecord::from_value(v).ok())
        .ok_or(AcceptanceRefusal::RecordMalformed)?;
    check(&record, &current, &requirements)
}

/// The typed check (steps 3 and 5 to 16 of the contract); the first failure is the result.
pub fn check(
    record: &AcceptanceRecord,
    current: &CurrentState,
    requirements: &AcceptanceRequirements,
) -> Result<(), AcceptanceRefusal> {
    use AcceptanceRefusal as R;
    if requirements.policy != current.policy {
        return Err(R::RequirementsPolicyMismatch);
    }
    let now = current.now;
    if record.computed_digest.as_str() != record.record_digest
        || record.expires_at <= record.issued_at
        || record.issued_at > now
    {
        return Err(R::RecordInvalid);
    }
    if current
        .consumed_record_ids
        .contains(&record.acceptance_record_id)
    {
        return Err(R::Replayed);
    }
    if (
        &record.tenant_id,
        &record.project_id,
        &record.task_id,
        &record.workflow_id,
    ) != (
        &current.tenant_id,
        &current.project_id,
        &current.task_id,
        &current.workflow_id,
    ) {
        return Err(R::ScopeMismatch);
    }
    if record.transition != current.transition {
        return Err(R::TransitionMismatch);
    }
    if (&record.artifact_snapshot_id, &record.artifact_digest)
        != (&current.artifact_snapshot_id, &current.artifact_digest)
    {
        return Err(R::ArtifactMismatch);
    }
    if record.policy != current.policy {
        return Err(R::PolicyMismatch);
    }
    if record.state_revision != current.state_revision {
        return Err(R::RevisionMismatch);
    }
    if now >= record.expires_at {
        return Err(R::Expired);
    }

    let mut required: Vec<_> = requirements.criteria.iter().collect();
    required.sort_by(|a, b| a.criterion_id.as_bytes().cmp(b.criterion_id.as_bytes()));
    let mut matched = Vec::with_capacity(required.len());
    for req in &required {
        let Some(c) = record
            .criteria
            .iter()
            .find(|c| c.criterion_id == req.criterion_id)
        else {
            return Err(R::CriteriaMissing);
        };
        if !c.mandatory || c.required_class != req.required_class {
            return Err(R::CriteriaMismatch);
        }
        matched.push((req, c));
    }
    if record
        .criteria
        .iter()
        .any(|c| c.mandatory && !required.iter().any(|r| r.criterion_id == c.criterion_id))
    {
        return Err(R::CriteriaMismatch);
    }
    for (req, c) in matched {
        if req.required_class.required_trust() != Some(c.evaluator_trust) {
            return Err(R::UntrustedEvaluator);
        }
        if current.revoked_evaluator_ids.contains(&c.evaluator_id) {
            return Err(R::EvaluatorRevoked);
        }
        match c.status {
            CriterionStatus::Stale => return Err(R::Stale),
            CriterionStatus::Pass if now >= c.fresh_until => return Err(R::Stale),
            CriterionStatus::Pass => {}
            CriterionStatus::Fail | CriterionStatus::Unknown | CriterionStatus::Waived => {
                return Err(R::Incomplete)
            }
        }
    }
    if record.status != RecordStatus::Accepted {
        return Err(R::RecordInvalid);
    }
    Ok(())
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;
    use crate::json::{self, parse, Map};

    type Edit = Box<dyn FnOnce(&mut Map)>;

    fn sha(c: char) -> String {
        format!("sha256:{}", c.to_string().repeat(64))
    }

    fn current() -> Value {
        let text = format!(
            r#"{{"tenant_id":"t1","project_id":"p1","task_id":"task-1","workflow_id":"wf-1","transition":{{"from_state":"verifying","to_state":"accepted"}},"artifact_snapshot_id":"snap-1","artifact_digest":"{}","acceptance_policy_id":"accept","acceptance_policy_version":"3","acceptance_policy_bundle_sha":"{}","state_revision":7,"now":"2026-11-07T11:00:00Z","consumed_record_ids":[],"revoked_evaluator_ids":[]}}"#,
            sha('b'),
            sha('c')
        );
        parse(text.as_bytes()).unwrap()
    }

    fn requirements() -> Value {
        let text = format!(
            r#"{{"acceptance_policy_id":"accept","acceptance_policy_version":"3","acceptance_policy_bundle_sha":"{}","criteria":[{{"criterion_id":"tests","required_class":"mechanical"}},{{"criterion_id":"review","required_class":"human"}}]}}"#,
            sha('c')
        );
        parse(text.as_bytes()).unwrap()
    }

    fn criterion(id: &str, class: &str, trust: &str, status: &str, mandatory: bool) -> String {
        format!(
            r#"{{"criterion_id":"{id}","mandatory":{mandatory},"required_class":"{class}","status":"{status}","evaluator_id":"eval-{id}","evaluator_trust":"{trust}","fresh_until":"2026-11-08T00:00:00Z"}}"#
        )
    }

    fn sealed(mut record: Value) -> Value {
        let d = json::digest(&record.without("record_digest")).unwrap();
        if let Value::Obj(m) = &mut record {
            m.insert("record_digest".into(), json::s(d.as_str()));
        }
        record
    }

    fn record_with(criteria: &[String], edit: impl FnOnce(&mut Map)) -> Value {
        let text = format!(
            r#"{{"contract_version":"m1-draft-3","acceptance_record_id":"acc-1","tenant_id":"t1","project_id":"p1","task_id":"task-1","workflow_id":"wf-1","transition":{{"from_state":"verifying","to_state":"accepted"}},"artifact_snapshot_id":"snap-1","artifact_digest":"{}","acceptance_policy_id":"accept","acceptance_policy_version":"3","acceptance_policy_bundle_sha":"{}","state_revision":7,"status":"accepted","issued_at":"2026-11-07T10:00:00Z","expires_at":"2026-11-07T12:00:00Z","criteria":[{}],"record_digest":"{}"}}"#,
            sha('b'),
            sha('c'),
            criteria.join(","),
            sha('0')
        );
        let mut v = parse(text.as_bytes()).unwrap();
        if let Value::Obj(m) = &mut v {
            edit(m);
        }
        sealed(v)
    }

    fn good() -> Vec<String> {
        vec![
            criterion("tests", "mechanical", "M", "pass", true),
            criterion("review", "human", "H", "pass", true),
        ]
    }

    fn check_record(record: &Value) -> Result<(), AcceptanceRefusal> {
        check_values(Some(record), Some(&current()), Some(&requirements()))
    }

    #[test]
    fn valid_record_is_accepted() {
        assert_eq!(check_record(&record_with(&good(), |_| {})), Ok(()));
        let mut extra = good();
        extra.push(criterion("lint", "model_assisted", "MA", "fail", false));
        assert_eq!(
            check_record(&record_with(&extra, |_| {})),
            Ok(()),
            "advisory criteria never affect the result"
        );
    }

    #[test]
    fn trusted_inputs_are_validated_first() {
        let r = record_with(&good(), |_| {});
        assert_eq!(
            check_values(Some(&r), None, Some(&requirements())),
            Err(AcceptanceRefusal::CurrentStateMalformed)
        );
        assert_eq!(
            check_values(Some(&r), Some(&current()), None),
            Err(AcceptanceRefusal::RequirementsMalformed)
        );
        assert_eq!(
            check_values(None, Some(&current()), Some(&requirements())),
            Err(AcceptanceRefusal::RecordMalformed)
        );
        let other_policy = requirements().without("acceptance_policy_version");
        assert_eq!(
            check_values(Some(&r), Some(&current()), Some(&other_policy)),
            Err(AcceptanceRefusal::RequirementsMalformed)
        );
        let mut mismatched = requirements();
        if let Value::Obj(m) = &mut mismatched {
            m.insert("acceptance_policy_version".into(), json::s("4"));
        }
        assert_eq!(
            check_values(Some(&Value::Null), Some(&current()), Some(&mismatched)),
            Err(AcceptanceRefusal::RequirementsPolicyMismatch)
        );
    }

    #[test]
    fn integrity_and_binding_refusals_in_order() {
        let mut tampered = record_with(&good(), |_| {});
        if let Value::Obj(m) = &mut tampered {
            m.insert("task_id".into(), json::s("task-2"));
        }
        assert_eq!(
            check_record(&tampered),
            Err(AcceptanceRefusal::RecordInvalid),
            "digest no longer matches"
        );
        let cases: Vec<(Edit, AcceptanceRefusal)> = vec![
            (
                Box::new(|m: &mut Map| {
                    m.insert("issued_at".into(), json::s("2026-11-07T11:00:01Z"));
                }),
                AcceptanceRefusal::RecordInvalid,
            ),
            (
                Box::new(|m: &mut Map| {
                    m.insert("expires_at".into(), json::s("2026-11-07T10:00:00Z"));
                }),
                AcceptanceRefusal::RecordInvalid,
            ),
            (
                Box::new(|m: &mut Map| {
                    m.insert("tenant_id".into(), json::s("t2"));
                }),
                AcceptanceRefusal::ScopeMismatch,
            ),
            (
                Box::new(|m: &mut Map| {
                    m.insert("workflow_id".into(), json::s("wf-2"));
                }),
                AcceptanceRefusal::ScopeMismatch,
            ),
            (
                Box::new(|m: &mut Map| {
                    m.insert(
                        "transition".into(),
                        parse(br#"{"from_state":"draft","to_state":"accepted"}"#).unwrap(),
                    );
                }),
                AcceptanceRefusal::TransitionMismatch,
            ),
            (
                Box::new(|m: &mut Map| {
                    m.insert("artifact_snapshot_id".into(), json::s("snap-2"));
                }),
                AcceptanceRefusal::ArtifactMismatch,
            ),
            (
                Box::new(|m: &mut Map| {
                    m.insert("acceptance_policy_version".into(), json::s("2"));
                }),
                AcceptanceRefusal::PolicyMismatch,
            ),
            (
                Box::new(|m: &mut Map| {
                    m.insert("state_revision".into(), Value::Int(6));
                }),
                AcceptanceRefusal::RevisionMismatch,
            ),
            (
                Box::new(|m: &mut Map| {
                    m.insert("expires_at".into(), json::s("2026-11-07T11:00:00Z"));
                }),
                AcceptanceRefusal::Expired,
            ),
            (
                Box::new(|m: &mut Map| {
                    m.insert("status".into(), json::s("rejected"));
                }),
                AcceptanceRefusal::RecordInvalid,
            ),
        ];
        for (edit, want) in cases {
            assert_eq!(check_record(&record_with(&good(), edit)), Err(want));
        }
        let mut consumed = current();
        if let Value::Obj(m) = &mut consumed {
            m.insert(
                "consumed_record_ids".into(),
                parse(br#"["acc-1"]"#).unwrap(),
            );
        }
        assert_eq!(
            check_values(
                Some(&record_with(&good(), |_| {})),
                Some(&consumed),
                Some(&requirements())
            ),
            Err(AcceptanceRefusal::Replayed)
        );
    }

    #[test]
    fn completeness_trust_and_status() {
        let cases = [
            (
                vec![criterion("tests", "mechanical", "M", "pass", true)],
                AcceptanceRefusal::CriteriaMissing,
            ),
            (
                vec![
                    criterion("tests", "mechanical", "M", "pass", true),
                    criterion("review", "human", "H", "pass", false),
                ],
                AcceptanceRefusal::CriteriaMismatch,
            ),
            (
                vec![
                    criterion("tests", "evidence", "E", "pass", true),
                    criterion("review", "human", "H", "pass", true),
                ],
                AcceptanceRefusal::CriteriaMismatch,
            ),
            (
                [good(), vec![criterion("extra", "human", "H", "pass", true)]].concat(),
                AcceptanceRefusal::CriteriaMismatch,
            ),
            (
                vec![
                    criterion("tests", "mechanical", "MA", "pass", true),
                    criterion("review", "human", "H", "pass", true),
                ],
                AcceptanceRefusal::UntrustedEvaluator,
            ),
            (
                vec![
                    criterion("tests", "mechanical", "M", "stale", true),
                    criterion("review", "human", "H", "pass", true),
                ],
                AcceptanceRefusal::Stale,
            ),
            (
                vec![
                    criterion("tests", "mechanical", "M", "pass", true),
                    criterion("review", "human", "H", "waived", true),
                ],
                AcceptanceRefusal::Incomplete,
            ),
            (
                vec![
                    criterion("tests", "mechanical", "M", "fail", true),
                    criterion("review", "human", "U", "pass", true),
                ],
                AcceptanceRefusal::UntrustedEvaluator,
            ),
        ];
        for (criteria, want) in cases {
            assert_eq!(
                check_record(&record_with(&criteria, |_| {})),
                Err(want),
                "{criteria:?}"
            );
        }
        let mut revoked = current();
        if let Value::Obj(m) = &mut revoked {
            m.insert(
                "revoked_evaluator_ids".into(),
                parse(br#"["eval-tests"]"#).unwrap(),
            );
        }
        assert_eq!(
            check_values(
                Some(&record_with(&good(), |_| {})),
                Some(&revoked),
                Some(&requirements())
            ),
            Err(AcceptanceRefusal::EvaluatorRevoked)
        );
    }

    #[test]
    fn malformed_records() {
        assert_eq!(
            check_record(&record_with(
                &[criterion("x", "model_assisted", "MA", "pass", true)],
                |_| {}
            )),
            Err(AcceptanceRefusal::RecordMalformed)
        );
        assert_eq!(
            check_record(&record_with(&[], |_| {})),
            Err(AcceptanceRefusal::RecordMalformed)
        );
        assert_eq!(
            check_record(&record_with(
                &[
                    criterion("tests", "mechanical", "M", "pass", true),
                    criterion("tests", "mechanical", "M", "pass", true)
                ],
                |_| {}
            )),
            Err(AcceptanceRefusal::RecordMalformed)
        );
        assert_eq!(
            check_record(&record_with(&good(), |m| {
                m.insert("extra".into(), Value::Null);
            })),
            Err(AcceptanceRefusal::RecordMalformed)
        );
        assert_eq!(
            check_record(&record_with(&good(), |m| {
                m.insert("contract_version".into(), json::s("m1-draft-2"));
            })),
            Err(AcceptanceRefusal::RecordMalformed)
        );
    }
}
