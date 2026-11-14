//! Workflow transition structure (M2-05; D-017, D-018 A2, A4, A8, D-019 N-1).
//!
//! For `workflow.transition` the kernel checks, in this order, after the
//! general D-008 checks and the transition-support check: the resource is
//! exactly `workflow:<tenant>/<project>/<workflow_id>`
//! (`WORKFLOW_RESOURCE_INVALID`); the attributes are a closed set with typed,
//! different `from_state` and `to_state` and an acceptance binding that is
//! either complete or absent (`TRANSITION_MALFORMED`); no other capability is
//! requested (`COMPOSITE_NOT_SUPPORTED`); and the binding agrees with the
//! bundle's protection classification (`ACCEPTANCE_BINDING_REQUIRED`,
//! `ACCEPTANCE_BINDING_UNEXPECTED`). The kernel checks structure only: it
//! validates no record, evidence or artifact and performs no I/O; the broker
//! validates the acceptance record at use time.

use crate::contracts::{ActionRequest, KernelCode, WorkflowProtection};
use crate::ids::is_name;
use crate::json::{Map, Value};

/// Attributes of the acceptance binding of a protected transition.
pub const ACCEPTANCE_ATTRIBUTES: [&str; 3] = [
    "acceptance_record_id",
    "artifact_snapshot_id",
    "artifact_digest",
];

/// The acceptance binding carried by a transition request.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AcceptanceBinding {
    /// Presented acceptance record id.
    pub acceptance_record_id: String,
    /// Artifact snapshot id.
    pub artifact_snapshot_id: String,
    /// Artifact digest.
    pub artifact_digest: String,
}

/// A structurally valid transition request.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TransitionRequest {
    /// Workflow named by the resource.
    pub workflow_id: String,
    /// Source state.
    pub from_state: String,
    /// Target state.
    pub to_state: String,
    /// Acceptance binding, when present.
    pub binding: Option<AcceptanceBinding>,
}

/// The workflow id of `workflow:<tenant>/<project>/<workflow_id>` for exactly
/// this tenant and project; `None` for any other shape.
pub fn workflow_id(resource_id: &str, tenant_id: &str, project_id: &str) -> Option<String> {
    let rest = resource_id.strip_prefix("workflow:")?;
    let mut segments = rest.split('/');
    match (
        segments.next(),
        segments.next(),
        segments.next(),
        segments.next(),
    ) {
        (Some(t), Some(p), Some(w), None) if t == tenant_id && p == project_id && is_name(w) => {
            Some(w.to_string())
        }
        _ => None,
    }
}

/// `from_state`, `to_state` and the binding from a closed attribute set.
pub fn transition_attributes(
    attributes: Option<&Map>,
) -> Option<(String, String, Option<AcceptanceBinding>)> {
    let attrs = attributes?;
    if !attrs.keys().all(|k| {
        k == "from_state" || k == "to_state" || ACCEPTANCE_ATTRIBUTES.contains(&k.as_str())
    }) {
        return None;
    }
    let state = |k: &str| {
        attrs
            .get(k)
            .and_then(Value::as_str)
            .filter(|s| is_name(s))
            .map(str::to_string)
    };
    let (from, to) = (state("from_state")?, state("to_state")?);
    if from == to {
        return None;
    }
    let present = ACCEPTANCE_ATTRIBUTES
        .iter()
        .filter(|k| attrs.contains_key(**k))
        .count();
    let binding = match present {
        0 => None,
        3 => {
            let text = |k: &str| attrs.get(k).and_then(Value::as_str).map(str::to_string);
            Some(AcceptanceBinding {
                acceptance_record_id: text("acceptance_record_id")?,
                artifact_snapshot_id: text("artifact_snapshot_id")?,
                artifact_digest: text("artifact_digest")?,
            })
        }
        _ => return None,
    };
    Some((from, to, binding))
}

/// Structural checks of a transition request against the bundle's protection
/// classification, in contract order. `protection` is `None` only when the
/// classification is unavailable, which fails closed.
pub fn check_transition(
    request: &ActionRequest,
    resource_id: &str,
    protection: Option<&WorkflowProtection>,
) -> Result<TransitionRequest, KernelCode> {
    let workflow_id = workflow_id(resource_id, &request.tenant_id, &request.project_id)
        .ok_or(KernelCode::WorkflowResourceInvalid)?;
    let (from_state, to_state, binding) = transition_attributes(request.action.attributes.as_ref())
        .ok_or(KernelCode::TransitionMalformed)?;
    if !request.action.requested_capabilities.is_empty() {
        return Err(KernelCode::CompositeNotSupported);
    }
    let protection = protection.ok_or(KernelCode::ProtectionUnavailable)?;
    match (
        protection.is_protected(&from_state, &to_state),
        binding.is_some(),
    ) {
        (true, false) => Err(KernelCode::AcceptanceBindingRequired),
        (false, true) => Err(KernelCode::AcceptanceBindingUnexpected),
        _ => Ok(TransitionRequest {
            workflow_id,
            from_state,
            to_state,
            binding,
        }),
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;
    use crate::json::parse;

    fn attrs(text: &str) -> Map {
        match parse(text.as_bytes()).unwrap() {
            Value::Obj(m) => m,
            other => panic!("{other:?}"),
        }
    }

    #[test]
    fn workflow_resource_shape() {
        assert_eq!(
            workflow_id("workflow:t/p/wf-1", "t", "p").as_deref(),
            Some("wf-1")
        );
        for bad in [
            "workflow:t/p/wf-1/accepted",
            "workflow:t/p",
            "workflow:t/q/wf-1",
            "workflow:u/p/wf-1",
            "workflow:t/p/WF",
            "workspace:t/p/wf-1",
            "workflow:t/p/",
            "Workflow:t/p/wf",
        ] {
            assert_eq!(workflow_id(bad, "t", "p"), None, "{bad}");
        }
    }

    #[test]
    fn closed_attribute_set() {
        let (f, t, b) = transition_attributes(Some(&attrs(
            r#"{"from_state":"verifying","to_state":"accepted"}"#,
        )))
        .unwrap();
        assert_eq!((f.as_str(), t.as_str(), b), ("verifying", "accepted", None));
        let bound = transition_attributes(Some(&attrs(r#"{"from_state":"a","to_state":"b","acceptance_record_id":"r","artifact_snapshot_id":"s","artifact_digest":"d"}"#))).unwrap();
        assert!(bound.2.is_some());
        for bad in [
            r#"{"from_state":"a","to_state":"a"}"#,
            r#"{"from_state":"a"}"#,
            r#"{"from_state":"a","to_state":"B"}"#,
            r#"{"from_state":"a","to_state":1}"#,
            r#"{"from_state":"a","to_state":"b","extra":1}"#,
            r#"{"from_state":"a","to_state":"b","acceptance_record_id":"r"}"#,
            r#"{"from_state":"a","to_state":"b","acceptance_record_id":"r","artifact_snapshot_id":"s","artifact_digest":7}"#,
        ] {
            assert!(transition_attributes(Some(&attrs(bad))).is_none(), "{bad}");
        }
        assert!(transition_attributes(None).is_none());
    }
}
