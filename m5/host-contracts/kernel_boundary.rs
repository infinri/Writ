use super::kernel_cases::{compare_cases, Expected};
use super::*;
use writ_kernel::{commands, contracts::ActionRequest, policy};

const NOW: &str = "2026-11-17T12:00:00Z";
const REVISION: Value = Value::Int(5);
const SCOPE: &str = "file:fixture-tenant/fixture-project/fixture-root/";
const POLICY: &str = r#"{
    "contract_version": "2.0",
    "bundle_id": "host-portability",
    "version": "1",
    "publisher": "fixture",
    "known_capabilities": ["fs.write"],
    "trusted_principal_issuers": ["fixture-adapter"],
    "statements": [
        {"statement_id": "block-private", "action_kinds": ["fs.write"],
         "resource_prefixes": ["file:fixture-tenant/fixture-project/fixture-root/private/"],
         "effect": "forbid", "reason_code": "FIXTURE_PRIVATE"},
        {"statement_id": "review-write", "action_kinds": ["fs.write"],
         "resource_prefixes": ["file:fixture-tenant/fixture-project/fixture-root/review/"],
         "effect": "require_approval", "reason_code": "FIXTURE_REVIEW"},
        {"statement_id": "allow-write", "action_kinds": ["fs.write"],
         "resource_prefixes": ["file:fixture-tenant/fixture-project/fixture-root/"],
         "effect": "permit", "reason_code": "FIXTURE_ALLOWED"}
    ]
}"#;

#[derive(Debug, PartialEq, Eq)]
enum BoundaryError {
    Input(InputError),
    SnapshotMismatch,
}

fn request(proposal: CreateProposal) -> Value {
    let resource = format!("{SCOPE}{}", proposal.relative_path);
    let mut action = obj([
        ("kind", s("fs.write")),
        (
            "resource",
            obj([
                ("uri", s(&resource)),
                ("canonical_resource_id", s(&resource)),
            ]),
        ),
        ("requested_capabilities", Value::Arr(vec![])),
        (
            "attributes",
            obj([
                ("mode", s("create_only")),
                ("source_path", s(&proposal.source_path)),
                ("snapshot_id", s(&proposal.snapshot_id)),
                ("manifest_digest", s(&proposal.manifest_digest)),
                ("content_digest", s(&proposal.content_digest)),
            ]),
        ),
    ]);
    let action_digest = json::digest(&action).unwrap();
    let Value::Obj(fields) = &mut action else {
        unreachable!()
    };
    fields.insert("action_digest".into(), s(action_digest.as_str()));
    obj([
        ("contract_version", s("2.0")),
        ("request_id", s("fixture-request")),
        ("trace_id", s("fixture-trace")),
        ("tenant_id", s("fixture-tenant")),
        ("project_id", s("fixture-project")),
        (
            "principal",
            obj([
                ("id", s("fixture-principal")),
                ("verified_by", s("fixture-adapter")),
                ("authn_method", s("os_peer_credential")),
                ("roles", Value::Arr(vec![s("agent")])),
            ]),
        ),
        ("session_id", s("fixture-connection")),
        ("agent_id", Value::Null),
        ("parent_session_id", Value::Null),
        ("idempotency_key", s("fixture-operation")),
        ("expected_state_revision", REVISION),
        ("action", action),
    ])
}

fn evaluate(
    proposal: Result<CreateProposal, InputError>,
    policy: &Value,
    dispatch: impl FnOnce(),
) -> Result<(String, String), BoundaryError> {
    let proposal = proposal.map_err(BoundaryError::Input)?;
    if proposal.snapshot_id != SNAPSHOT
        || proposal.source_path != "source.txt"
        || proposal.manifest_digest != MANIFEST
        || proposal.content_digest != CONTENT
    {
        return Err(BoundaryError::SnapshotMismatch);
    }
    let request = request(proposal);
    let input = obj([
        ("bundle", policy.clone()),
        ("state_revision", REVISION),
        ("now", s(NOW)),
        ("request", request.clone()),
    ]);
    let decision = commands::decide(encode(&input).as_bytes());
    let parsed = json::parse(decision.as_bytes()).unwrap();
    if parsed.get("status").and_then(Value::as_str) == Some("allow") {
        dispatch();
    }
    Ok((encode(&request), decision))
}

fn compare(
    label: &str,
    hook: &[u8],
    cli: &[u8],
    policy: &Value,
    expect_dispatch: bool,
) -> Result<(String, String), BoundaryError> {
    let mut hook_dispatches = 0;
    let hook = evaluate(claude_protected_write(hook, ROOT), policy, || {
        hook_dispatches += 1;
    });
    let mut cli_dispatches = 0;
    let cli = evaluate(cli_protected_write(cli), policy, || {
        cli_dispatches += 1;
    });
    assert_eq!(hook, cli, "{label}");
    let expected_dispatches = usize::from(expect_dispatch);
    assert_eq!(hook_dispatches, expected_dispatches, "{label}: Claude");
    assert_eq!(cli_dispatches, expected_dispatches, "{label}: CLI");
    hook
}

fn check_decision(
    result: (String, String),
    path: &str,
    status: &str,
    reason: &str,
    rule: Option<&str>,
    bundle: &Value,
) {
    let (request, decision) = result;
    let request = ActionRequest::from_value(&json::parse(request.as_bytes()).unwrap()).unwrap();
    assert_eq!(
        request.action.resource.canonical_resource_id.as_deref(),
        Some(format!("{SCOPE}{path}").as_str()),
    );
    assert_eq!(request.action.kind, "fs.write");
    assert_eq!(request.principal.id, "fixture-principal");
    assert_eq!(request.session_id, "fixture-connection");
    assert_eq!(request.agent_id, None);
    assert_eq!(request.parent_session_id, None);
    let decision = json::parse(decision.as_bytes()).unwrap();
    assert_eq!(decision.as_obj().unwrap().len(), 13);
    let sha = if bundle == &Value::Null {
        "none".to_owned()
    } else {
        policy::bundle_sha(bundle).unwrap().as_str().to_owned()
    };
    assert_eq!(decision.get("contract_version"), Some(&s("2.0")));
    assert!(decision
        .get("decision_id")
        .and_then(Value::as_str)
        .unwrap()
        .starts_with("dec:"));
    assert_eq!(decision.get("policy_bundle_sha"), Some(&s(&sha)));
    assert_eq!(
        decision.get("matched_rule_ids"),
        Some(&Value::Arr(rule.into_iter().map(s).collect()))
    );
    assert_eq!(decision.get("safe_message"), Some(&Value::Null));
    let expires = match status {
        "allow" | "require_approval" => s("2026-11-17T12:05:00Z"),
        _ => Value::Null,
    };
    assert_eq!(decision.get("expires_at"), Some(&expires));
    assert_eq!(decision.get("request_id"), Some(&s(&request.request_id)));
    assert_eq!(decision.get("status"), Some(&s(status)));
    assert_eq!(decision.get("reason_code"), Some(&s(reason)));
    assert_eq!(decision.get("decided_at"), Some(&s(NOW)));
    assert_eq!(decision.get("state_revision"), Some(&REVISION));
    assert_eq!(decision.get("permit_reference"), Some(&Value::Null));
    let approval = decision.get("approval_request_reference").unwrap();
    if status == "require_approval" {
        assert!(!approval.as_str().unwrap().is_empty());
    } else {
        assert_eq!(approval, &Value::Null);
    }
}

#[test]
fn paired_intake_matches_canonical_requests_and_complete_decisions() {
    let policy = json::parse(POLICY.as_bytes()).unwrap();
    compare_cases(|label, hook, cli, expected| match expected {
        Expected::Refused(error) => assert_eq!(
            compare(label, hook, cli, &policy, false),
            Err(BoundaryError::Input(error)),
            "{label}"
        ),
        Expected::Decision(path) => check_decision(
            compare(label, hook, cli, &policy, true).unwrap(),
            path,
            "allow",
            "FIXTURE_ALLOWED",
            Some("allow-write"),
            &policy,
        ),
    });
}

#[test]
fn fixed_policy_and_snapshot_refusals_cannot_dispatch() {
    let policy = json::parse(POLICY.as_bytes()).unwrap();
    for (path, bundle, status, reason, rule) in [
        (
            "private/report.txt",
            &policy,
            "deny",
            "FIXTURE_PRIVATE",
            Some("block-private"),
        ),
        (
            "review/report.txt",
            &policy,
            "require_approval",
            "FIXTURE_REVIEW",
            Some("review-write"),
        ),
        (
            "report.txt",
            &Value::Null,
            "unavailable",
            "POLICY_UNAVAILABLE",
            None,
        ),
    ] {
        let hook = encode(&claude_request(path));
        let cli = encode(&cli_request(path));
        check_decision(
            compare(path, hook.as_bytes(), cli.as_bytes(), bundle, false).unwrap(),
            path,
            status,
            reason,
            rule,
            bundle,
        );
    }
    let hook = encode(&claude_request("report.txt"));
    let cli = encode(&cli_request("report.txt"));
    for (known, unbound) in [
        (
            SNAPSHOT,
            "snap-dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
        ),
        ("source.txt", "unlisted.txt"),
        (MANIFEST, CONTENT),
        (CONTENT, MANIFEST),
    ] {
        let hook = hook.replace(known, unbound);
        let cli = cli.replace(known, unbound);
        assert_eq!(
            compare(known, hook.as_bytes(), cli.as_bytes(), &policy, false),
            Err(BoundaryError::SnapshotMismatch),
            "{known}"
        );
    }
}
