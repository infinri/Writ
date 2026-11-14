//! Fail-closed policy bundle loading (D-008, D-017, D-019 N-4, D-020).
//!
//! A bundle loads only if it is schema-valid and internally consistent. Any
//! rejection makes every decision under it `unavailable` /
//! `POLICY_BUNDLE_REJECTED`; malformed configuration is never reported as an
//! ordinary denial.

use crate::contracts::{Edge, Effect, PolicyBundle, SchemaError, Statement, WorkflowProtection};
use crate::json::{self, Digest, Value};
use std::collections::{BTreeSet, VecDeque};
use std::fmt;

/// Why a bundle was rejected at load.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum BundleRejection {
    /// Schema violation.
    Schema(SchemaError),
    /// Two statements share a `statement_id`.
    DuplicateStatementId,
    /// A statement covers a capability missing from `known_capabilities`.
    UnknownStatementCapability,
    /// A `permit` or `require_approval` statement covers `workflow.transition` without `to_states` (D-017).
    UnscopedTransitionStatement,
    /// A statement covers `workflow.transition` but the bundle has no `workflow_protection` (D-020).
    ProtectionMissing,
    /// A protected state is listed twice.
    DuplicateProtectedState,
    /// A protected edge is listed twice or is a self edge.
    InvalidProtectedEdge,
    /// A protected edge is reachable by an unprotected permitted route (D-019 N-4).
    BypassableProtectedEdge,
    /// The bundle content is outside the D-006 domain (not reachable from strictly parsed input).
    NotCanonical,
}

impl fmt::Display for BundleRejection {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            BundleRejection::Schema(e) => write!(f, "bundle {e}"),
            other => write!(f, "bundle rejected: {other:?}"),
        }
    }
}

impl std::error::Error for BundleRejection {}

/// A loaded, consistent bundle and its digest.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LoadedBundle {
    /// D-006 digest of the bundle without `publisher_signature`.
    pub sha: Digest,
    /// The bundle.
    pub bundle: PolicyBundle,
}

impl LoadedBundle {
    /// The transition protection classification, when present.
    pub fn protection(&self) -> Option<&WorkflowProtection> {
        self.bundle.workflow_protection.as_ref()
    }

    /// D-020: support is declared by a statement covering transitions or by
    /// `workflow_protection`; listing `workflow.transition` only as a known
    /// capability does not declare support.
    pub fn supports_transitions(&self) -> bool {
        self.bundle.workflow_protection.is_some()
            || self
                .bundle
                .statements
                .iter()
                .any(Statement::covers_transition)
    }
}

/// D-006 digest of the bundle content; the publisher signature is excluded.
pub fn bundle_sha(document: &Value) -> Result<Digest, BundleRejection> {
    json::digest(&document.without("publisher_signature"))
        .map_err(|_| BundleRejection::NotCanonical)
}

/// Load a bundle document, failing closed on any inconsistency.
pub fn load_bundle(document: &Value) -> Result<LoadedBundle, BundleRejection> {
    let bundle = PolicyBundle::from_value(document).map_err(BundleRejection::Schema)?;
    let sha = bundle_sha(document)?;
    let mut ids = BTreeSet::new();
    if !bundle
        .statements
        .iter()
        .all(|st| ids.insert(st.statement_id.as_str()))
    {
        return Err(BundleRejection::DuplicateStatementId);
    }
    if bundle
        .statements
        .iter()
        .flat_map(|st| st.action_kinds.iter())
        .any(|k| !bundle.known_capabilities.contains(k))
    {
        return Err(BundleRejection::UnknownStatementCapability);
    }
    if bundle
        .statements
        .iter()
        .any(|st| st.effect != Effect::Forbid && st.covers_transition() && st.to_states.is_none())
    {
        return Err(BundleRejection::UnscopedTransitionStatement);
    }
    if let Some(p) = &bundle.workflow_protection {
        let mut states = BTreeSet::new();
        if !p
            .protected_to_states
            .iter()
            .all(|s| states.insert(s.as_str()))
        {
            return Err(BundleRejection::DuplicateProtectedState);
        }
        let mut edges = BTreeSet::new();
        if !p
            .protected_transitions
            .iter()
            .all(|e| e.from_state != e.to_state && edges.insert(e))
        {
            return Err(BundleRejection::InvalidProtectedEdge);
        }
    }
    if bundle.statements.iter().any(Statement::covers_transition) {
        match &bundle.workflow_protection {
            None => return Err(BundleRejection::ProtectionMissing),
            Some(p) if edge_bypassable(&bundle.statements, p) => {
                return Err(BundleRejection::BypassableProtectedEdge)
            }
            Some(_) => {}
        }
    }
    Ok(LoadedBundle { sha, bundle })
}

/// D-019 N-4: a protected edge A to B (B not itself a protected state) must
/// not be reachable from A through transitions permitted or approvable by
/// statements, using no protected edge and entering no protected state. A
/// statement without `from_states` permits any source; the states are all
/// names in the bundle. Tenant, role and resource conditions are ignored
/// (conservative: a route that some request could take counts).
fn edge_bypassable(statements: &[Statement], p: &WorkflowProtection) -> bool {
    let mut states: BTreeSet<&str> = BTreeSet::new();
    for st in statements {
        for list in [&st.from_states, &st.to_states].into_iter().flatten() {
            states.extend(list.iter().map(String::as_str));
        }
    }
    states.extend(p.protected_to_states.iter().map(String::as_str));
    for e in &p.protected_transitions {
        states.insert(e.from_state.as_str());
        states.insert(e.to_state.as_str());
    }
    let is_protected_edge = |f: &str, t: &str| {
        p.protected_transitions
            .iter()
            .any(|e| e.from_state == f && e.to_state == t)
    };
    let is_protected_state = |t: &str| p.protected_to_states.iter().any(|s| s == t);
    let mut graph: BTreeSet<(&str, &str)> = BTreeSet::new();
    for st in statements
        .iter()
        .filter(|st| st.effect != Effect::Forbid && st.covers_transition())
    {
        let sources: Vec<&str> = match &st.from_states {
            Some(list) => list.iter().map(String::as_str).collect(),
            None => states.iter().copied().collect(),
        };
        for f in &sources {
            for t in st.to_states.iter().flatten() {
                if *f != t.as_str() && !is_protected_edge(f, t) && !is_protected_state(t) {
                    graph.insert((f, t.as_str()));
                }
            }
        }
    }
    p.protected_transitions
        .iter()
        .filter(|e| !is_protected_state(&e.to_state))
        .any(
            |Edge {
                 from_state,
                 to_state,
             }| {
                let mut seen: BTreeSet<&str> = BTreeSet::from([from_state.as_str()]);
                let mut queue: VecDeque<&str> = VecDeque::from([from_state.as_str()]);
                while let Some(x) = queue.pop_front() {
                    for (_, t) in graph.iter().filter(|(f, _)| *f == x) {
                        if *t == to_state.as_str() {
                            return true;
                        }
                        if seen.insert(t) {
                            queue.push_back(t);
                        }
                    }
                }
                false
            },
        )
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;
    use crate::json::parse;

    fn bundle(statements: &str, protection: Option<&str>) -> Value {
        let protection = protection
            .map(|p| format!(r#","workflow_protection":{p}"#))
            .unwrap_or_default();
        let text = format!(
            r#"{{"contract_version":"2.0","bundle_id":"b","version":"1","publisher":"p","known_capabilities":["file.write","workflow.transition"],"trusted_principal_issuers":["i"],"statements":{statements},"publisher_signature":null{protection}}}"#
        );
        parse(text.as_bytes()).unwrap()
    }

    fn protection(states: &str, edges: &str) -> String {
        format!(
            r#"{{"acceptance_policy_bundle_sha":"sha256:{}","protected_to_states":{states},"protected_transitions":{edges}}}"#,
            "c".repeat(64)
        )
    }

    fn permit(id: &str, from: Option<&str>, to: &str) -> String {
        let from = from
            .map(|f| format!(r#","from_states":{f}"#))
            .unwrap_or_default();
        format!(
            r#"{{"statement_id":"{id}","effect":"permit","action_kinds":["workflow.transition"],"reason_code":"OK","to_states":{to}{from}}}"#
        )
    }

    #[test]
    fn loads_and_digests_without_signature() {
        let a = bundle("[]", None);
        let loaded = load_bundle(&a).unwrap();
        let mut signed = a.clone();
        if let Value::Obj(m) = &mut signed {
            m.insert("publisher_signature".into(), json::s("sig"));
        }
        assert_eq!(
            load_bundle(&signed).unwrap().sha,
            loaded.sha,
            "signature is not part of the digest"
        );
        assert!(
            !loaded.supports_transitions(),
            "known capability alone is not support (D-020)"
        );
    }

    #[test]
    fn rejections() {
        let p = protection(r#"["accepted"]"#, "[]");
        let cases = [
            (
                bundle(
                    r#"[{"statement_id":"a","effect":"permit","action_kinds":["file.write"],"reason_code":"OK"},{"statement_id":"a","effect":"forbid","action_kinds":["file.write"],"reason_code":"NO"}]"#,
                    None,
                ),
                BundleRejection::DuplicateStatementId,
            ),
            (
                bundle(
                    r#"[{"statement_id":"a","effect":"permit","action_kinds":["net.egress"],"reason_code":"OK"}]"#,
                    None,
                ),
                BundleRejection::UnknownStatementCapability,
            ),
            (
                bundle(
                    r#"[{"statement_id":"a","effect":"permit","action_kinds":["workflow.transition"],"reason_code":"OK"}]"#,
                    Some(&p),
                ),
                BundleRejection::UnscopedTransitionStatement,
            ),
            (
                bundle(&format!("[{}]", permit("a", None, r#"["accepted"]"#)), None),
                BundleRejection::ProtectionMissing,
            ),
            (
                bundle("[]", Some(&protection(r#"["accepted","accepted"]"#, "[]"))),
                BundleRejection::DuplicateProtectedState,
            ),
            (
                bundle(
                    "[]",
                    Some(&protection("[]", r#"[{"from_state":"a","to_state":"a"}]"#)),
                ),
                BundleRejection::InvalidProtectedEdge,
            ),
            (
                bundle(
                    "[]",
                    Some(&protection(
                        "[]",
                        r#"[{"from_state":"a","to_state":"b"},{"from_state":"a","to_state":"b"}]"#,
                    )),
                ),
                BundleRejection::InvalidProtectedEdge,
            ),
        ];
        for (doc, want) in cases {
            assert_eq!(load_bundle(&doc).unwrap_err(), want);
        }
        assert!(matches!(
            load_bundle(&Value::Null),
            Err(BundleRejection::Schema(_))
        ));
    }

    #[test]
    fn forbid_transition_statement_needs_no_target_states() {
        let p = protection("[]", "[]");
        let doc = bundle(
            r#"[{"statement_id":"f","effect":"forbid","action_kinds":["workflow.transition"],"reason_code":"NO"}]"#,
            Some(&p),
        );
        assert!(load_bundle(&doc).unwrap().supports_transitions());
    }

    #[test]
    fn bypassable_edges_are_rejected() {
        let p = protection("[]", r#"[{"from_state":"draft","to_state":"verifying"}]"#);
        // draft -> planning -> verifying routes around the protected edge.
        let route = format!(
            "[{},{}]",
            permit("a", Some(r#"["draft"]"#), r#"["planning"]"#),
            permit("b", Some(r#"["planning"]"#), r#"["verifying"]"#)
        );
        assert_eq!(
            load_bundle(&bundle(&route, Some(&p))).unwrap_err(),
            BundleRejection::BypassableProtectedEdge
        );
        // A statement without from_states permits any source, including draft itself via another state.
        let any_source = format!(
            "[{},{}]",
            permit("a", Some(r#"["draft"]"#), r#"["planning"]"#),
            permit("b", None, r#"["verifying"]"#)
        );
        assert_eq!(
            load_bundle(&bundle(&any_source, Some(&p))).unwrap_err(),
            BundleRejection::BypassableProtectedEdge
        );
        // Without the second hop the edge cannot be bypassed.
        let no_route = format!(
            "[{},{}]",
            permit("a", Some(r#"["draft"]"#), r#"["planning"]"#),
            permit("b", Some(r#"["draft"]"#), r#"["verifying"]"#)
        );
        assert!(load_bundle(&bundle(&no_route, Some(&p))).is_ok());
        // A route through a protected state is not a bypass.
        let p2 = protection(
            r#"["planning"]"#,
            r#"[{"from_state":"draft","to_state":"verifying"}]"#,
        );
        assert!(load_bundle(&bundle(&route, Some(&p2))).is_ok());
        // A forbid statement contributes no route.
        let forbid_route = format!(
            r#"[{},{{"statement_id":"b","effect":"forbid","action_kinds":["workflow.transition"],"reason_code":"NO","from_states":["planning"],"to_states":["verifying"]}}]"#,
            permit("a", Some(r#"["draft"]"#), r#"["planning"]"#)
        );
        assert!(load_bundle(&bundle(&forbid_route, Some(&p))).is_ok());
    }
}
