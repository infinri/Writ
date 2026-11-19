#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use super::*;
use writ_kernel::json::Map;
use writ_kernel::policy::load_bundle;

const PERMIT: &str = include_str!("../fixtures/permit.input.json");
const APPROVAL: &str = include_str!("../fixtures/approval.input.json");
const DENY: &str = include_str!("../fixtures/deny.input.json");

fn object(value: &mut Value) -> &mut Map {
    let Value::Obj(fields) = value else {
        panic!("fixture must be an object");
    };
    fields
}

fn array(value: &mut Value) -> &mut Vec<Value> {
    let Value::Arr(items) = value else {
        panic!("fixture must be an array");
    };
    items
}

fn diagnostics(report: &Report) -> Vec<&str> {
    report
        .document
        .get("diagnostics")
        .unwrap()
        .as_arr()
        .unwrap()
        .iter()
        .map(|entry| entry.as_str().unwrap())
        .collect()
}

fn assert_accepted(report: &Report) {
    assert!(report.accepted, "{:?}", diagnostics(report));
    assert_eq!(
        report.document.get("deployed_enforcement"),
        Some(&json::s("UNVERIFIED"))
    );
    assert_eq!(
        report.document.get("runtime_authority"),
        Some(&Value::Bool(false))
    );
}

#[test]
fn three_candidates_execute_every_fixed_decision_and_load_the_canonical_bundle() {
    for (input, suite, status, reason, winner, overlaps) in [
        (
            PERMIT,
            proof::SUITES[0],
            "allow",
            "M6_SYNTHETIC_PERMIT",
            "m6-d19c8fc0747ed5cbc72a44d9c976826d",
            0,
        ),
        (
            APPROVAL,
            proof::SUITES[1],
            "require_approval",
            "M6_SYNTHETIC_APPROVAL",
            "m6-ad0eecf1b3acf7fdcec81e9c93b75f93",
            1,
        ),
        (
            DENY,
            proof::SUITES[2],
            "deny",
            "M6_SYNTHETIC_FORBID",
            "m6-78a02294180060e1fd64b1962f300b17",
            3,
        ),
    ] {
        let report = evaluate(input.as_bytes());
        assert_accepted(&report);
        let wire = json::canonical(&report.document).unwrap();
        let parsed = json::parse(wire.as_bytes()).unwrap();
        let bundle = load_bundle(parsed.get("bundle").unwrap()).unwrap();
        assert_eq!(
            parsed.get("bundle_digest"),
            Some(&json::s(bundle.sha.as_str()))
        );
        assert_eq!(
            parsed.get("overlaps").unwrap().as_arr().unwrap().len(),
            overlaps
        );
        let results = parsed
            .get("kernel_proof")
            .unwrap()
            .get("fixture_results")
            .unwrap()
            .as_arr()
            .unwrap();
        let cases = json::parse(suite.fixtures.as_bytes()).unwrap();
        for fixture in cases.as_arr().unwrap() {
            let id = fixture.get("id").unwrap();
            let result = results
                .iter()
                .find(|result| result.get("id") == Some(id))
                .unwrap();
            let decision = result.get("decision").unwrap();
            assert_eq!(Some(decision), fixture.get("expected"));
            if id.as_str() == Some("winner") {
                assert_eq!(decision.get("status"), Some(&json::s(status)));
                assert_eq!(decision.get("reason_code"), Some(&json::s(reason)));
                assert_eq!(
                    decision.get("matched_rule_ids"),
                    Some(&Value::Arr(vec![json::s(winner)]))
                );
            }
        }
    }
}

#[test]
fn repeat_and_equivalent_input_order_produce_identical_results() {
    let original = evaluate(DENY.as_bytes());
    assert_accepted(&original);
    let canonical = json::canonical(&original.document).unwrap();
    let mut reordered = json::parse(DENY.as_bytes()).unwrap();
    let input = object(&mut reordered);
    let manifests = array(input.get_mut("manifests").unwrap());
    for entry in &mut *manifests {
        array(object(entry).get_mut("test_vectors").unwrap()).reverse();
    }
    manifests.reverse();
    array(input.get_mut("companions").unwrap()).reverse();

    for input in [DENY.to_owned(), json::canonical(&reordered).unwrap()] {
        assert_eq!(
            json::canonical(&evaluate(input.as_bytes()).document).unwrap(),
            canonical
        );
    }
}

#[test]
fn changing_priority_does_not_change_statements_and_refuses_stale_evidence() {
    let original = compile::compile(DENY.as_bytes()).unwrap();
    let mut changed = json::parse(DENY.as_bytes()).unwrap();
    let manifests = array(object(&mut changed).get_mut("manifests").unwrap());
    object(&mut manifests[0]).insert("priority".into(), Value::Int(-999));
    let wire = json::canonical(&changed).unwrap();
    let revised = compile::compile(wire.as_bytes()).unwrap();
    assert_eq!(revised.bundle, original.bundle);
    let report = evaluate(wire.as_bytes());
    assert!(!report.accepted);
    assert_eq!(diagnostics(&report), ["UNVERIFIED_STALE_EVIDENCE"]);
}

#[test]
fn same_tier_reason_conflict_refuses_even_with_different_priorities() {
    let mut input = json::parse(APPROVAL.as_bytes()).unwrap();
    let companion = array(object(&mut input).get_mut("companions").unwrap());
    for entry in &mut *companion {
        object(entry).insert("effect".into(), json::s("permit"));
    }
    let original = evaluate(json::canonical(&input).unwrap().as_bytes());
    assert!(!original.accepted);
    assert!(diagnostics(&original)[0].starts_with("CONFLICT_SAME_TIER_REASON"));
    for field in ["manifests", "companions"] {
        array(object(&mut input).get_mut(field).unwrap()).reverse();
    }
    let reversed = evaluate(json::canonical(&input).unwrap().as_bytes());
    assert_eq!(original.document, reversed.document);
}

#[test]
fn removing_registry_binding_marks_rules_unverified_and_refuses() {
    let report = evaluate_with(DENY.as_bytes(), &[], &proof::SUITES);
    assert!(!report.accepted);
    assert_eq!(report.document.get("bundle"), Some(&Value::Null));
    assert_eq!(diagnostics(&report).len(), 3);
    assert!(diagnostics(&report)
        .iter()
        .all(|code| code.starts_with("UNVERIFIED_ENFORCER")));
    for evidence in report
        .document
        .get("rule_evidence")
        .unwrap()
        .as_arr()
        .unwrap()
    {
        assert_eq!(evidence.get("offline_status"), Some(&json::s("UNVERIFIED")));
    }
}

#[test]
fn missing_fixture_suite_refuses() {
    let report = evaluate_with(PERMIT.as_bytes(), &enforcer::REGISTRY, &[]);
    assert!(!report.accepted);
    assert_eq!(diagnostics(&report), ["UNVERIFIED_MISSING_EVIDENCE"]);
}

#[test]
fn stale_fixture_contents_refuse() {
    let mut suites = proof::SUITES;
    suites[0].fixtures = suites[1].fixtures;
    let report = evaluate_with(PERMIT.as_bytes(), &enforcer::REGISTRY, &suites);
    assert!(!report.accepted);
    assert_eq!(diagnostics(&report), ["UNVERIFIED_STALE_EVIDENCE"]);
}

#[test]
fn a_different_enforcer_implementation_identity_refuses() {
    let mut registry = enforcer::REGISTRY;
    registry[0].identity = || json::digest(&json::s("different implementation")).ok();
    let report = evaluate_with(PERMIT.as_bytes(), &registry, &proof::SUITES);
    assert!(!report.accepted);
    assert_eq!(diagnostics(&report), ["UNVERIFIED_STALE_EVIDENCE"]);
}

#[test]
fn a_failing_negative_execution_refuses_despite_current_evidence() {
    let mut registry = enforcer::REGISTRY;
    registry[0].run = |request, bundle| {
        let mut actual = (enforcer::REGISTRY[0].run)(request, bundle)?;
        if request.get("request_id").and_then(Value::as_str) == Some("m6-scope") {
            object(&mut actual).insert("expires_at".into(), json::s("unexpected expiry"));
        }
        Some(actual)
    };
    let report = evaluate_with(PERMIT.as_bytes(), &registry, &proof::SUITES);
    assert!(!report.accepted);
    assert_eq!(diagnostics(&report), ["UNVERIFIED_FIXTURE_FAILED"]);
}

#[test]
fn simulation_prepares_supported_edits_without_accepting_unpinned_evidence() {
    let edited = PERMIT.replace("M6_SYNTHETIC_PERMIT", "M6_EDITED_PERMIT");
    let draft = prepare_simulation(edited.as_bytes()).unwrap();
    assert_eq!(
        draft.verify_offline_evidence(),
        vec![Diagnostic::new("UNVERIFIED_MISSING_EVIDENCE", "")]
    );
    let request = json::parse(proof::SUITES[0].fixtures.as_bytes()).unwrap();
    let request = request.as_arr().unwrap()[1].get("request").unwrap();
    let decision = draft.decide_fixture(request).unwrap();
    assert_eq!(
        decision.get("reason_code"),
        Some(&json::s("M6_EDITED_PERMIT"))
    );
    assert!(!evaluate(edited.as_bytes()).accepted);
}

#[test]
fn simulation_refuses_unsupported_rules_with_value_free_json_pointers() {
    let unsupported = PERMIT.replace(r#""effect": "permit""#, r#""effect": "private-value""#);
    let diagnostics = prepare_simulation(unsupported.as_bytes()).err().unwrap();
    assert_eq!(
        diagnostics,
        vec![Diagnostic::new(
            "COMPANION_EFFECT_UNSUPPORTED",
            "/companions/0/effect"
        )]
    );
}
