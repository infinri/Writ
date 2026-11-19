use super::*;
use writ_policy_management_lab::packs::PACK_ADMIN_PURPOSE;

fn refused(lab: &Lab, credential: &str, input: &Value, expected: &str) {
    let api = lab.api(credential, input);
    assert_eq!(api, lab.cli(credential, input), "{input:?}");
    assert_eq!(code(&api), Some(expected), "{api:?}");
}

fn pack_payload(lab: &Lab) -> Value {
    let contract = lab.publish_contract("1.0.0", 0);
    let evaluator = lab.publish_evaluator();
    payload(PACK, "1.0.0", &contract, &evaluator)
}

#[test]
fn pack_lifecycle_requires_a_scoped_human_publisher() {
    let lab = Lab::new();
    let envelope = lab.sign(&pack_payload(&lab));
    let preview = lab.pack_preview("install", "1.0.0", 0, Some(envelope));
    let commit = lab.pack_request(&preview, "install", "install-pack");
    for call in [&preview, &commit] {
        refused(&lab, "fixture-agent", call, "UNAUTHORIZED");
        for credential in ["fixture-service", AUTHOR, VIEWER] {
            refused(&lab, credential, call, "NOT_FOUND");
        }
        for field in ["tenant", "project"] {
            let mut outside_scope = call.clone();
            let mut scope = call.get("scope").unwrap().clone();
            put(&mut scope, field, json::s("other"));
            put(&mut outside_scope, "scope", scope);
            refused(&lab, PUBLISHER, &outside_scope, "NOT_FOUND");
        }
    }
    assert_eq!(lab.history(PACK).get("revision"), Some(&Value::Int(0)));
    result(&lab.cli(PUBLISHER, &commit));
}

#[test]
fn pack_signature_and_approval_bind_the_purpose_and_bytes() {
    let lab = Lab::new();
    let payload = pack_payload(&lab);
    let envelope = lab.sign(&payload);
    let preview = lab.pack_preview("install", "1.0.0", 0, Some(envelope.clone()));
    let commit = lab.pack_request(&preview, "install", "install-signed");

    let mut bad_signature = envelope.clone();
    put(&mut bad_signature, "signature", json::s(&"0".repeat(128)));
    let mut wrong_purpose = payload.clone();
    put(&mut wrong_purpose, "purpose", json::s(PACK_ADMIN_PURPOSE));
    let mut wrong_scope = payload.clone();
    let mut scope = payload.get("scope").unwrap().clone();
    put(&mut scope, "project", json::s("other"));
    put(&mut wrong_scope, "scope", scope);
    let mut changed = payload.clone();
    let mut manifest = payload.get("manifest").unwrap().clone();
    put(
        &mut manifest,
        "description",
        json::s("Changed after preview"),
    );
    put(&mut changed, "manifest", manifest);

    let mut attempts = vec![bad_signature];
    for altered in [wrong_purpose, wrong_scope, changed.clone()] {
        let mut signed = envelope.clone();
        put(&mut signed, "payload", altered);
        attempts.push(signed);
    }
    for invalid in attempts {
        let preview = lab.pack_preview("install", "1.0.0", 0, Some(invalid.clone()));
        refused(&lab, PUBLISHER, &preview, "UNAUTHORIZED");
        let mut invalid_commit = commit.clone();
        put(&mut invalid_commit, "envelope", invalid);
        refused(&lab, PUBLISHER, &invalid_commit, "UNAUTHORIZED");
    }

    let mut signed_again = commit.clone();
    put(&mut signed_again, "envelope", lab.sign(&changed));
    refused(&lab, PUBLISHER, &signed_again, "REVISION_CONFLICT");

    let mut bad_approval_signature = commit.clone();
    let presentation = commit.get("presentation").unwrap().as_str().unwrap();
    let (claim, _) = presentation.rsplit_once('|').unwrap();
    put(
        &mut bad_approval_signature,
        "presentation",
        json::s(&format!("{claim}|{}", "0".repeat(128))),
    );
    refused(&lab, PUBLISHER, &bad_approval_signature, "UNAUTHORIZED");

    let approved_preview = lab.api(PUBLISHER, &preview);
    let mut expired_signer = Command::new(PACK_SIGNER);
    expired_signer
        .arg("approve")
        .arg(lab.dir.join("fixture-pack-publisher-step-up"))
        .arg("expired-pack-approval")
        .arg("2026-11-19T11:59:00Z");
    let expired = invoke(expired_signer, &approved_preview);
    let mut expired_commit = commit.clone();
    put(
        &mut expired_commit,
        "presentation",
        expired.get("presentation").unwrap().clone(),
    );
    refused(&lab, PUBLISHER, &expired_commit, "UNAUTHORIZED");

    let mut wrong_approval = commit.clone();
    let mut contract = Lab::contract_fields("2.0.0", 2);
    put(
        &mut contract,
        "idempotency_key",
        json::s("create-other-contract"),
    );
    put(
        &mut contract,
        "schema",
        json::obj([
            (
                "$schema",
                json::s("https://json-schema.org/draft/2020-12/schema"),
            ),
            ("type", json::s("integer")),
        ]),
    );
    result(&lab.api(AUTHOR, &request("contracts.create", &contract)));
    let contract_approval = lab.contract_request(
        "publish",
        &Lab::contract_fields("2.0.0", 3),
        "approve-other-contract",
    );
    put(
        &mut wrong_approval,
        "presentation",
        contract_approval.get("presentation").unwrap().clone(),
    );
    refused(&lab, PUBLISHER, &wrong_approval, "UNAUTHORIZED");

    assert_eq!(lab.history(PACK).get("revision"), Some(&Value::Int(0)));
    result(&lab.cli(PUBLISHER, &commit));
}

#[test]
fn revoked_publisher_cannot_verify_or_enable_an_installed_pack() {
    let lab = Lab::new();
    let envelope = lab.sign(&pack_payload(&lab));
    lab.install("1.0.0", 0, envelope, "install-before-revocation");
    let verify = request("packs.verify", &target("1.0.0"));
    let pre_revocation = lab.api(VIEWER, &verify);
    result(&pre_revocation);
    assert_eq!(pre_revocation, lab.cli(VIEWER, &verify));

    let preview = lab.pack_preview("enable", "1.0.0", 1, None);
    let commit = lab.pack_request(&preview, "enable", "enable-after-revocation");
    lab.trust.revoke().unwrap();
    for (subject, call) in [
        (VIEWER, &verify),
        (PUBLISHER, &preview),
        (PUBLISHER, &commit),
    ] {
        let response = lab.api(subject, call);
        assert_eq!(code(&response), Some("UNAUTHORIZED"), "{response:?}");
    }
    let history = lab.history(PACK);
    assert_eq!(history.get("revision"), Some(&Value::Int(1)));
    assert_eq!(history.get("active_version"), Some(&Value::Null));
}

#[test]
fn unsupported_requirements_are_refused_even_when_signed() {
    let lab = Lab::new();
    let original = pack_payload(&lab);
    for (field, value) in [
        (
            "supported_modes",
            Value::Arr(vec![json::s("managed_agent")]),
        ),
        (
            "required_model_features",
            Value::Arr(vec![json::s("model-tools")]),
        ),
        ("tool_capabilities", Value::Arr(vec![json::s("fs.write")])),
    ] {
        let mut payload = original.clone();
        let mut manifest = payload.get("manifest").unwrap().clone();
        put(&mut manifest, field, value);
        put(&mut payload, "manifest", manifest);
        let preview = lab.pack_preview("install", "1.0.0", 0, Some(lab.sign(&payload)));
        refused(&lab, PUBLISHER, &preview, "UNSUPPORTED");
    }
    let mut incompatible = original;
    put(&mut incompatible, "compatibility", json::s("3.0"));
    let preview = lab.pack_preview("install", "1.0.0", 0, Some(lab.sign(&incompatible)));
    refused(&lab, PUBLISHER, &preview, "UNSUPPORTED");
    assert_eq!(lab.history(PACK).get("revision"), Some(&Value::Int(0)));
}

#[test]
fn conflicting_packs_cannot_be_enabled_together() {
    let lab = Lab::new();
    let envelope = lab.sign(&pack_payload(&lab));
    lab.install("1.0.0", 0, envelope.clone(), "install-original");
    let mut conflict = envelope.get("payload").unwrap().clone();
    let mut manifest = conflict.get("manifest").unwrap().clone();
    put(&mut manifest, "pack_id", json::s("conflict-pack"));
    put(&mut conflict, "manifest", manifest);
    let rubric = &conflict.get("rubrics").unwrap().as_arr().unwrap()[0];
    let mut rubric = rubric.clone();
    let mut binding = rubric.get("binding").unwrap().clone();
    put(&mut binding, "pack_id", json::s("conflict-pack"));
    put(&mut rubric, "binding", binding);
    put(&mut conflict, "rubrics", Value::Arr(vec![rubric]));

    let mut install = lab.pack_preview("install", "1.0.0", 1, Some(lab.sign(&conflict)));
    put(&mut install, "pack_id", json::s("conflict-pack"));
    let install = lab.pack_request(&install, "install", "install-conflict");
    result(&lab.cli(PUBLISHER, &install));
    let enable = lab.pack_request(
        &lab.pack_preview("enable", "1.0.0", 2, None),
        "enable",
        "enable-original",
    );
    result(&lab.api(PUBLISHER, &enable));

    let mut conflict_enable = lab.pack_preview("enable", "1.0.0", 3, None);
    put(&mut conflict_enable, "pack_id", json::s("conflict-pack"));
    refused(&lab, PUBLISHER, &conflict_enable, "PACK_CONFLICT");
    assert_eq!(
        lab.history(PACK).get("active_version"),
        Some(&json::s("1.0.0"))
    );
    let history = lab.history("conflict-pack");
    assert_eq!(history.get("revision"), Some(&Value::Int(3)));
    assert_eq!(history.get("active_version"), Some(&Value::Null));
}

#[test]
fn exact_retry_returns_receipt_and_reused_approval_is_refused() {
    let lab = Lab::new();
    let envelope = lab.sign(&pack_payload(&lab));
    let preview = lab.pack_preview("install", "1.0.0", 0, Some(envelope));
    let commit = lab.pack_request(&preview, "install", "first-ceremony");
    let receipt = lab.api(PUBLISHER, &commit);
    result(&receipt);
    assert_eq!(receipt, lab.cli(PUBLISHER, &commit));
    assert_eq!(receipt, lab.api(PUBLISHER, &commit));

    let mut changed_retry = commit.clone();
    put(&mut changed_retry, "expected_revision", Value::Int(1));
    refused(&lab, PUBLISHER, &changed_retry, "IDEMPOTENCY_CONFLICT");

    let mut replay = lab.pack_request(
        &lab.pack_preview("enable", "1.0.0", 1, None),
        "enable",
        "first-ceremony",
    );
    put(&mut replay, "idempotency_key", json::s("new-key"));
    refused(&lab, PUBLISHER, &replay, "UNAUTHORIZED");

    let history = lab.history(PACK);
    assert_eq!(history.get("revision"), Some(&Value::Int(1)));
    assert_eq!(history.get("active_version"), Some(&Value::Null));
    assert_eq!(
        history.get("history"),
        Some(&Value::Arr(vec![result(&receipt).clone()]))
    );
}
