//! The pure authorization decision (M2-04; D-008 extended by D-017..D-022).
//!
//! `decide` maps (request, policy, trusted state revision, trusted time) to a
//! Decision. It reads no clock, file, network or environment; identical
//! inputs give identical decisions, and JSON member order never matters.
//!
//! Evaluation order (the first failing step decides):
//! 1. request schema: `invalid` / `REQUEST_SCHEMA_INVALID`;
//! 2. `requested_capabilities` repeating `action.kind`: `invalid` / `REQUESTED_CAPABILITIES_INVALID` (D-018 A4);
//! 3. deadline at or before `now`: `invalid` / `DEADLINE_EXCEEDED`;
//! 4. no bundle: `unavailable` / `POLICY_UNAVAILABLE`; bundle rejected at load: `unavailable` / `POLICY_BUNDLE_REJECTED`;
//! 5. pinned bundle digest differs: `unavailable` / `POLICY_VERSION_MISMATCH`;
//! 6. principal issuer not trusted: `deny` / `PRINCIPAL_UNVERIFIED`;
//! 7. expected state revision differs: `invalid` / `STATE_REVISION_CONFLICT`;
//! 8. a capability the bundle does not know: `deny` / `UNKNOWN_CAPABILITY`;
//! 9. resource id missing or not canonical: `invalid` / `RESOURCE_NOT_CANONICAL`;
//! 10. resource outside the request's tenant and project: `deny` / `RESOURCE_SCOPE_MISMATCH` (INV-10, ENT-R02);
//! 11. transitions only: support (`deny` / `UNSUPPORTED_CAPABILITY`, D-020), then the
//!     structure checks of [`crate::transition`];
//! 12. statements: forbid > require_approval > permit > default deny; every requested
//!     capability must be covered by a matching approval or permit statement.

use crate::contracts::{
    short_id, ActionRequest, Decision, DecisionStatus, Effect, KernelCode, ReasonCode,
    StateRevision, Statement, NO_POLICY_SHA, WORKFLOW_TRANSITION,
};
use crate::json::{self, Value};
use crate::policy::LoadedBundle;
use crate::time::Timestamp;
use crate::transition::{check_transition, TransitionRequest};
use std::collections::BTreeSet;

/// Lifetime of an `allow` or `require_approval` decision, bounded by the request deadline.
pub const DECISION_TTL_SECONDS: i64 = 300;

/// The policy available to a decision.
#[derive(Clone, Copy, Debug)]
pub enum Policy<'a> {
    /// No bundle supplied.
    Absent,
    /// A bundle was supplied and rejected at load.
    Rejected,
    /// A loaded bundle.
    Loaded(&'a LoadedBundle),
}

/// Canonical resource ids are `<scheme>:<tenant>/<project>/<path>` with no
/// empty, `.` or `..` segments and no backslash.
pub fn is_canonical_resource_id(resource_id: &str) -> bool {
    let Some((scheme, rest)) = resource_id.split_once(':') else {
        return false;
    };
    !scheme.is_empty()
        && !rest.is_empty()
        && !rest.contains('\\')
        && rest.split('/').count() >= 3
        && rest.split('/').all(|seg| !matches!(seg, "" | "." | ".."))
}

/// Structural tenant and project scope (INV-10): the resource path must start
/// with `<tenant>/<project>/`; a tenant or project id containing `/` is never in scope.
pub fn in_scope(resource_id: &str, tenant_id: &str, project_id: &str) -> bool {
    if tenant_id.contains('/') || project_id.contains('/') {
        return false;
    }
    resource_id.split_once(':').is_some_and(|(_, rest)| {
        rest.strip_prefix(tenant_id)
            .and_then(|r| r.strip_prefix('/'))
            .and_then(|r| r.strip_prefix(project_id))
            .is_some_and(|r| r.starts_with('/'))
    })
}

/// Deterministic approval request id: binds tenant, principal, kind, resource,
/// action digest, capability set and bundle digest; for transitions also
/// `from_state` and `to_state`.
pub fn approval_request_id(
    request: &ActionRequest,
    resource_id: &str,
    bundle_sha: &str,
    transition: Option<&TransitionRequest>,
) -> String {
    let caps = Value::Arr(
        request
            .capability_set()
            .iter()
            .map(|c| json::s(c))
            .collect(),
    );
    let mut binding = json::obj([
        ("tenant_id", json::s(&request.tenant_id)),
        ("principal_id", json::s(&request.principal.id)),
        ("action_kind", json::s(&request.action.kind)),
        ("canonical_resource_id", json::s(resource_id)),
        ("action_digest", json::s(&request.action.action_digest)),
        ("requested_capabilities", caps),
        ("policy_bundle_sha", json::s(bundle_sha)),
    ]);
    if let (Some(t), Value::Obj(m)) = (transition, &mut binding) {
        m.insert("from_state".into(), json::s(&t.from_state));
        m.insert("to_state".into(), json::s(&t.to_state));
    }
    short_id("apr", &binding)
}

fn applies(
    st: &Statement,
    request: &ActionRequest,
    resource_id: &str,
    kinds: &BTreeSet<&str>,
    transition: Option<&TransitionRequest>,
) -> bool {
    if st
        .tenants
        .as_ref()
        .is_some_and(|ts| !ts.contains(&request.tenant_id))
    {
        return false;
    }
    if st
        .principal_roles
        .as_ref()
        .is_some_and(|rs| !rs.iter().any(|r| request.principal.roles.contains(r)))
    {
        return false;
    }
    if !st.action_kinds.iter().any(|k| kinds.contains(k.as_str())) {
        return false;
    }
    if st
        .resource_prefixes
        .as_ref()
        .is_some_and(|ps| !ps.iter().any(|p| resource_id.starts_with(p.as_str())))
    {
        return false;
    }
    if st.from_states.is_some() || st.to_states.is_some() {
        let Some(t) = transition else { return false };
        if st
            .from_states
            .as_ref()
            .is_some_and(|l| !l.contains(&t.from_state))
            || st
                .to_states
                .as_ref()
                .is_some_and(|l| !l.contains(&t.to_state))
        {
            return false;
        }
    }
    true
}

struct Outcome {
    status: DecisionStatus,
    reason: ReasonCode,
    matched: Vec<String>,
    approval: Option<String>,
}

fn kernel(status: DecisionStatus, code: KernelCode) -> Outcome {
    Outcome {
        status,
        reason: ReasonCode::Kernel(code),
        matched: Vec::new(),
        approval: None,
    }
}

fn ids(statements: &[&Statement]) -> Vec<String> {
    statements
        .iter()
        .map(|st| st.statement_id.clone())
        .collect()
}

/// The decision steps after schema validation.
fn evaluate(
    request: &ActionRequest,
    policy: Policy<'_>,
    state_revision: StateRevision,
    now: Timestamp,
) -> Outcome {
    use DecisionStatus::{Allow, Deny, Invalid, RequireApproval, Unavailable};
    let action = &request.action;
    if action.requested_capabilities.contains(&action.kind) {
        return kernel(Invalid, KernelCode::RequestedCapabilitiesInvalid);
    }
    if request.deadline_at.is_some_and(|d| d <= now) {
        return kernel(Invalid, KernelCode::DeadlineExceeded);
    }
    let bundle = match policy {
        Policy::Absent => return kernel(Unavailable, KernelCode::PolicyUnavailable),
        Policy::Rejected => return kernel(Unavailable, KernelCode::PolicyBundleRejected),
        Policy::Loaded(b) => b,
    };
    if request
        .policy_bundle_sha
        .as_ref()
        .is_some_and(|pin| pin != bundle.sha.as_str())
    {
        return kernel(Unavailable, KernelCode::PolicyVersionMismatch);
    }
    if !bundle
        .bundle
        .trusted_principal_issuers
        .contains(&request.principal.verified_by)
    {
        return kernel(Deny, KernelCode::PrincipalUnverified);
    }
    if request
        .expected_state_revision
        .is_some_and(|r| r != state_revision)
    {
        return kernel(Invalid, KernelCode::StateRevisionConflict);
    }
    let mut kinds: BTreeSet<&str> = BTreeSet::from([action.kind.as_str()]);
    kinds.extend(action.requested_capabilities.iter().map(String::as_str));
    if !kinds.iter().all(|k| {
        bundle
            .bundle
            .known_capabilities
            .iter()
            .any(|known| known == k)
    }) {
        return kernel(Deny, KernelCode::UnknownCapability);
    }
    let resource_id = action
        .resource
        .canonical_resource_id
        .as_deref()
        .unwrap_or("");
    if !is_canonical_resource_id(resource_id) {
        return kernel(Invalid, KernelCode::ResourceNotCanonical);
    }
    if !in_scope(resource_id, &request.tenant_id, &request.project_id) {
        return kernel(Deny, KernelCode::ResourceScopeMismatch);
    }
    let transition = if action.kind == WORKFLOW_TRANSITION {
        if !bundle.supports_transitions() {
            return kernel(Deny, KernelCode::UnsupportedCapability);
        }
        match check_transition(request, resource_id, bundle.protection()) {
            Ok(t) => Some(t),
            Err(code @ KernelCode::ProtectionUnavailable) => return kernel(Unavailable, code),
            Err(code) => return kernel(Invalid, code),
        }
    } else {
        None
    };

    let mut matching: Vec<&Statement> = bundle
        .bundle
        .statements
        .iter()
        .filter(|st| applies(st, request, resource_id, &kinds, transition.as_ref()))
        .collect();
    matching.sort_by(|a, b| a.statement_id.as_bytes().cmp(b.statement_id.as_bytes()));
    let with = |effect: Effect| {
        matching
            .iter()
            .copied()
            .filter(|st| st.effect == effect)
            .collect::<Vec<_>>()
    };
    let (forbids, approvals, permits) = (
        with(Effect::Forbid),
        with(Effect::RequireApproval),
        with(Effect::Permit),
    );
    if let Some(first) = forbids.first() {
        return Outcome {
            status: Deny,
            reason: ReasonCode::Policy(first.reason_code.clone()),
            matched: ids(&forbids),
            approval: None,
        };
    }
    if approvals.is_empty() && permits.is_empty() {
        return kernel(Deny, KernelCode::DefaultDeny);
    }
    let both: Vec<&Statement> = approvals.iter().chain(permits.iter()).copied().collect();
    let covered: BTreeSet<&str> = both
        .iter()
        .flat_map(|st| st.action_kinds.iter().map(String::as_str))
        .collect();
    if !kinds.is_subset(&covered) {
        return Outcome {
            status: Deny,
            reason: ReasonCode::Kernel(KernelCode::CapabilityNotPermitted),
            matched: ids(&both),
            approval: None,
        };
    }
    if let Some(first) = approvals.first() {
        let apr = approval_request_id(
            request,
            resource_id,
            bundle.sha.as_str(),
            transition.as_ref(),
        );
        return Outcome {
            status: RequireApproval,
            reason: ReasonCode::Policy(first.reason_code.clone()),
            matched: ids(&approvals),
            approval: Some(apr),
        };
    }
    match permits.first() {
        Some(first) => Outcome {
            status: Allow,
            reason: ReasonCode::Policy(first.reason_code.clone()),
            matched: ids(&permits),
            approval: None,
        },
        None => kernel(Deny, KernelCode::DefaultDeny),
    }
}

/// Decide `request` (any JSON value; a schema-invalid request still receives
/// a Decision) under `policy` at the trusted `state_revision` and `now`.
pub fn decide(
    request: &Value,
    policy: Policy<'_>,
    state_revision: StateRevision,
    now: Timestamp,
) -> Decision {
    let request_id = request
        .get("request_id")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .unwrap_or("unknown")
        .to_string();
    let policy_bundle_sha = match policy {
        Policy::Loaded(b) => b.sha.as_str().to_string(),
        Policy::Absent | Policy::Rejected => NO_POLICY_SHA.to_string(),
    };
    let (outcome, deadline) = match ActionRequest::from_value(request) {
        Ok(typed) => (
            evaluate(&typed, policy, state_revision, now),
            typed.deadline_at,
        ),
        Err(_) => (
            kernel(DecisionStatus::Invalid, KernelCode::RequestSchemaInvalid),
            None,
        ),
    };
    let mut matched_rule_ids = outcome.matched;
    matched_rule_ids.sort_by(|a, b| a.as_bytes().cmp(b.as_bytes()));
    matched_rule_ids.dedup();
    let expires_at = match outcome.status {
        DecisionStatus::Allow | DecisionStatus::RequireApproval => {
            let ttl = now.plus_seconds(DECISION_TTL_SECONDS);
            Some(deadline.map_or(ttl, |d| d.min(ttl)))
        }
        _ => None,
    };
    Decision {
        request_id,
        status: outcome.status,
        policy_bundle_sha,
        reason_code: outcome.reason,
        matched_rule_ids,
        state_revision,
        decided_at: now,
        approval_request_reference: outcome.approval,
        expires_at,
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;
    use crate::json::parse;
    use crate::policy::load_bundle;

    const BUNDLE: &str = r#"{"contract_version":"2.0","bundle_id":"b","version":"1","publisher":"p","known_capabilities":["file.write","file.read","repository.publish"],"trusted_principal_issuers":["adapter"],"statements":[
        {"statement_id":"permit-write","effect":"permit","action_kinds":["file.write","file.read"],"reason_code":"WRITE_OK","resource_prefixes":["workspace:"]},
        {"statement_id":"approve-publish","effect":"require_approval","action_kinds":["repository.publish"],"reason_code":"PUBLISH_NEEDS_APPROVAL"},
        {"statement_id":"forbid-secrets","effect":"forbid","action_kinds":["file.read","file.write"],"reason_code":"SECRETS_FORBIDDEN","resource_prefixes":["workspace:acme/web/secrets/"]}]}"#;

    fn request(tenant: &str, project: &str, kind: &str, rid: &str, extra: &str) -> Value {
        let text = format!(
            r#"{{"contract_version":"2.0","request_id":"r1","trace_id":"t1","tenant_id":"{tenant}","principal":{{"id":"agent:1","verified_by":"adapter","authn_method":"peer","roles":["agent"]}},"project_id":"{project}","session_id":"s1","idempotency_key":"k1","action":{{"kind":"{kind}","resource":{{"uri":"x","canonical_resource_id":"{rid}"}},"action_digest":"sha256:0123456789abcdef"{extra}}}}}"#
        );
        parse(text.as_bytes()).unwrap()
    }

    fn run(req: &Value) -> Decision {
        let b = load_bundle(&parse(BUNDLE.as_bytes()).unwrap()).unwrap();
        decide(
            req,
            Policy::Loaded(&b),
            StateRevision::new(5).unwrap(),
            Timestamp::parse_strict("2026-11-07T11:00:00Z").unwrap(),
        )
    }

    fn outcome(d: &Decision) -> (&'static str, String, Vec<String>) {
        (
            d.status.as_str(),
            d.reason_code.as_str().to_string(),
            d.matched_rule_ids.clone(),
        )
    }

    #[test]
    fn allow_with_provenance_and_expiry() {
        let d = run(&request(
            "acme",
            "web",
            "file.write",
            "workspace:acme/web/src/a.rs",
            "",
        ));
        assert_eq!(
            outcome(&d),
            ("allow", "WRITE_OK".into(), vec!["permit-write".into()])
        );
        assert_eq!(
            d.expires_at.map(|t| t.to_string()).as_deref(),
            Some("2026-11-07T11:05:00Z")
        );
    }

    #[test]
    fn forbid_beats_permit() {
        let d = run(&request(
            "acme",
            "web",
            "file.write",
            "workspace:acme/web/secrets/key",
            "",
        ));
        assert_eq!(
            outcome(&d),
            (
                "deny",
                "SECRETS_FORBIDDEN".into(),
                vec!["forbid-secrets".into()]
            )
        );
        assert_eq!(d.expires_at, None);
    }

    #[test]
    fn approval_and_composite_coverage() {
        let d = run(&request(
            "acme",
            "web",
            "repository.publish",
            "repo:acme/web/main",
            "",
        ));
        assert_eq!(
            outcome(&d),
            (
                "require_approval",
                "PUBLISH_NEEDS_APPROVAL".into(),
                vec!["approve-publish".into()]
            )
        );
        assert!(d
            .approval_request_reference
            .as_deref()
            .is_some_and(|a| a.starts_with("apr:") && a.len() == 36));
        // permit-write only matches workspace: resources, so file.write rides along uncovered.
        let d = run(&request(
            "acme",
            "web",
            "repository.publish",
            "repo:acme/web/main",
            r#","requested_capabilities":["file.write"]"#,
        ));
        assert_eq!(
            outcome(&d),
            (
                "deny",
                "CAPABILITY_NOT_PERMITTED".into(),
                vec!["approve-publish".into()]
            )
        );
        assert_eq!(d.approval_request_reference, None);
    }

    #[test]
    fn cross_tenant_and_cross_project_are_denied() {
        for (tenant, project, rid) in [
            ("acme", "web", "workspace:other/web/a"),
            ("acme", "web", "workspace:acme/api/a"),
            ("acme", "web", "workspace:acmex/web/a"),
            ("acme", "web", "workspace:acme/webx/a"),
            ("acme/web", "x", "workspace:acme/web/x/a"),
            ("acme", "web/x", "workspace:acme/web/x/a"),
        ] {
            let d = run(&request(tenant, project, "file.write", rid, ""));
            assert_eq!(
                outcome(&d),
                ("deny", "RESOURCE_SCOPE_MISMATCH".into(), vec![]),
                "{tenant} {project} {rid}"
            );
        }
    }

    #[test]
    fn non_canonical_resources_are_invalid() {
        for rid in [
            "",
            "workspace:acme/web",
            "workspace:acme//a",
            "workspace:acme/web/../x",
            "workspace:acme/web/./x",
            "acme/web/x",
            ":acme/web/x",
            "workspace:acme\\\\web\\\\x",
        ] {
            let d = run(&request("acme", "web", "file.write", rid, ""));
            assert_eq!(d.reason_code.as_str(), "RESOURCE_NOT_CANONICAL", "{rid}");
        }
    }

    #[test]
    fn early_outcomes() {
        let req = request("acme", "web", "file.write", "workspace:acme/web/a", "");
        let now = Timestamp::parse_strict("2026-11-07T11:00:00Z").unwrap();
        let rev = StateRevision::new(5).unwrap();
        assert_eq!(
            decide(&req, Policy::Absent, rev, now).reason_code.as_str(),
            "POLICY_UNAVAILABLE"
        );
        assert_eq!(
            decide(&req, Policy::Rejected, rev, now).policy_bundle_sha,
            NO_POLICY_SHA
        );
        let bad = decide(&Value::Null, Policy::Absent, rev, now);
        assert_eq!(
            (bad.status, bad.request_id.as_str()),
            (DecisionStatus::Invalid, "unknown")
        );
        let redundant = run(&request(
            "acme",
            "web",
            "file.write",
            "workspace:acme/web/a",
            r#","requested_capabilities":["file.write"]"#,
        ));
        assert_eq!(
            redundant.reason_code.as_str(),
            "REQUESTED_CAPABILITIES_INVALID"
        );
    }

    #[test]
    fn deadline_bounds_expiry_and_expired_deadline_is_invalid() {
        let mut req = request("acme", "web", "file.write", "workspace:acme/web/a", "");
        if let Value::Obj(m) = &mut req {
            m.insert("deadline_at".into(), json::s("2026-11-07T11:01:00.5Z"));
        }
        assert_eq!(
            run(&req).expires_at.map(|t| t.to_string()).as_deref(),
            Some("2026-11-07T11:01:00.5Z")
        );
        if let Value::Obj(m) = &mut req {
            m.insert("deadline_at".into(), json::s("2026-11-07T11:00:00Z"));
        }
        assert_eq!(
            outcome(&run(&req)),
            ("invalid", "DEADLINE_EXCEEDED".into(), vec![])
        );
    }

    #[test]
    fn approval_ids_bind_the_operation() {
        let a = run(&request(
            "acme",
            "web",
            "repository.publish",
            "repo:acme/web/main",
            "",
        ))
        .approval_request_reference;
        let b = run(&request(
            "acme",
            "web",
            "repository.publish",
            "repo:acme/web/dev",
            "",
        ))
        .approval_request_reference;
        assert!(a.is_some() && a != b);
    }
}
