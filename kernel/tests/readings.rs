//! Owner-approved readings (D-031, 2026-11-12) and protection reachability.
//! Positive and negative tests; frozen fixture expectations are not changed.

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod common;

use std::fs;
use writ_kernel::contracts::{ActionRequest, KernelCode};
use writ_kernel::json::{self, Value};
use writ_kernel::{commands, refusal, transition};

fn case_input(set: &str, id: &str) -> Value {
    let text = fs::read_to_string(
        common::fixtures_dir()
            .join("sets")
            .join(format!("{set}.jsonl")),
    )
    .unwrap();
    for line in text.lines().filter(|l| !l.trim().is_empty()) {
        let v = json::parse(line.as_bytes()).unwrap();
        if v.get("id").and_then(Value::as_str) == Some(id) {
            return v.get("input").cloned().unwrap();
        }
    }
    panic!("case {id} not found in {set}");
}

fn set_member(v: &mut Value, path: &[&str], value: Value) {
    let mut cur = v;
    for key in &path[..path.len() - 1] {
        cur = match cur {
            Value::Obj(m) => m.get_mut(*key).unwrap(),
            _ => panic!("not an object at {key}"),
        };
    }
    match cur {
        Value::Obj(m) => {
            m.insert(path[path.len() - 1].to_string(), value);
        }
        _ => panic!("not an object"),
    }
}

fn parse(text: &str) -> Value {
    json::parse(text.as_bytes()).unwrap()
}

fn with_raw_member(input: &Value, raw_member: &str) -> String {
    let text = input.to_json_text();
    format!("{{{raw_member},{}", &text[1..])
}

// Reading 1: revoked_evaluator_ids must hold valid evaluator names.

#[test]
fn revoked_evaluator_ids_valid_names_are_accepted() {
    let base = case_input("acceptance-binding", "pos-01-valid");
    for ids in [
        r#"[]"#,
        r#"["unrelated-evaluator"]"#,
        r#"["a","b_c","z-9"]"#,
    ] {
        let mut v = base.clone();
        set_member(&mut v, &["current", "revoked_evaluator_ids"], parse(ids));
        assert_eq!(
            commands::check_acceptance(v.to_json_text().as_bytes()),
            r#"{"ok":true}"#,
            "{ids}"
        );
    }
    let mut v = base.clone();
    set_member(
        &mut v,
        &["current", "revoked_evaluator_ids"],
        parse(r#"["ci-runner"]"#),
    );
    assert_eq!(
        commands::check_acceptance(v.to_json_text().as_bytes()),
        r#"{"refusal":"ACCEPTANCE_EVALUATOR_REVOKED"}"#
    );
}

#[test]
fn revoked_evaluator_ids_malformed_entries_are_current_state_malformed() {
    let base = case_input("acceptance-binding", "pos-01-valid");
    let long = format!(r#"["{}"]"#, "a".repeat(65));
    for bad in [
        r#"[""]"#,
        r#"["Ci-runner"]"#,
        r#"["1runner"]"#,
        r#"["ci runner"]"#,
        r#"[7]"#,
        r#"[null]"#,
        r#"[["ci-runner"]]"#,
        r#""ci-runner""#,
        r#"null"#,
        r#"{}"#,
        long.as_str(),
    ] {
        let mut v = base.clone();
        set_member(&mut v, &["current", "revoked_evaluator_ids"], parse(bad));
        assert_eq!(
            commands::check_acceptance(v.to_json_text().as_bytes()),
            r#"{"refusal":"CURRENT_STATE_MALFORMED"}"#,
            "{bad}"
        );
    }
}

// Reading 2: external-refusal keeps the documented parse-error response and maps
// missing, non-string and unknown codes to PROMOTION_REFUSED.

#[test]
fn external_refusal_parse_errors_keep_the_documented_response() {
    for text in [
        "{",
        r#"{"internal_code":"A","internal_code":"B"}"#,
        r#"{"internal_code":1.5}"#,
    ] {
        let out = commands::external_refusal(text.as_bytes());
        let v = parse(&out);
        let Value::Obj(m) = &v else { panic!("{out}") };
        assert_eq!(m.len(), 2, "{out}");
        assert_eq!(
            v.get("input_error").and_then(Value::as_str),
            Some("INPUT_JSON_INVALID"),
            "{text}"
        );
        assert!(
            v.get("json_error").and_then(Value::as_str).is_some(),
            "{text}: {out}"
        );
    }
}

#[test]
fn external_refusal_missing_non_string_and_unknown_codes_are_promotion_refused() {
    for text in [
        r#"{}"#,
        r#"{"internal_code":7}"#,
        r#"{"internal_code":null}"#,
        r#"{"internal_code":[]}"#,
        r#"{"internal_code":{}}"#,
        r#"{"internal_code":true}"#,
        r#"{"internal_code":"NOT_A_KNOWN_CODE"}"#,
        r#"{"internal_code":""}"#,
        r#"[]"#,
        r#""INPUT_JSON_INVALID""#,
    ] {
        assert_eq!(
            commands::external_refusal(text.as_bytes()),
            r#"{"external":"PROMOTION_REFUSED"}"#,
            "{text}"
        );
    }
    for code in refusal::PASS_THROUGH {
        let out = commands::external_refusal(format!(r#"{{"internal_code":"{code}"}}"#).as_bytes());
        assert_eq!(out, format!(r#"{{"external":"{code}"}}"#));
    }
}

// Reading 3: extra members are ignored only at the decide and check-acceptance
// command wrappers; ignored values must still be strict JSON; nested contracts stay strict.

#[test]
fn decide_wrapper_ignores_extra_members_but_not_nested_ones() {
    let base = case_input("golden-decisions", "workspace-file-write");
    let expected = commands::decide(base.to_json_text().as_bytes());
    assert!(expected.contains(r#""status":"allow""#), "{expected}");
    let mut extra = base.clone();
    set_member(
        &mut extra,
        &["zz_extra"],
        parse(r#"{"a":[1,2,{"b":null}]}"#),
    );
    assert_eq!(commands::decide(extra.to_json_text().as_bytes()), expected);

    let dup = with_raw_member(&base, r#""zz_extra":{"a":1,"a":2}"#);
    let out = parse(&commands::decide(dup.as_bytes()));
    assert_eq!(
        out.get("reason_code").and_then(Value::as_str),
        Some("REQUEST_JSON_INVALID")
    );
    let float = with_raw_member(&base, r#""zz_extra":1.5"#);
    let out = parse(&commands::decide(float.as_bytes()));
    assert_eq!(
        out.get("reason_code").and_then(Value::as_str),
        Some("REQUEST_JSON_INVALID")
    );

    let mut nested = base.clone();
    set_member(&mut nested, &["request", "zz_extra"], Value::Bool(true));
    let out = parse(&commands::decide(nested.to_json_text().as_bytes()));
    assert_eq!(
        out.get("status").and_then(Value::as_str),
        Some("invalid"),
        "nested extra member must stay strict"
    );
}

#[test]
fn check_acceptance_wrapper_ignores_extra_members_but_not_nested_ones() {
    let base = case_input("acceptance-binding", "pos-01-valid");
    let mut extra = base.clone();
    set_member(&mut extra, &["zz_extra"], parse(r#"[1,{"x":"y"}]"#));
    assert_eq!(
        commands::check_acceptance(extra.to_json_text().as_bytes()),
        r#"{"ok":true}"#
    );

    let dup = with_raw_member(&base, r#""zz_extra":{"a":1,"a":2}"#);
    let out = parse(&commands::check_acceptance(dup.as_bytes()));
    assert_eq!(
        out.get("refusal").and_then(Value::as_str),
        Some("INPUT_JSON_INVALID")
    );

    for path in [
        ["record", "zz_extra"],
        ["current", "zz_extra"],
        ["requirements", "zz_extra"],
    ] {
        let mut nested = base.clone();
        set_member(&mut nested, &path, Value::Bool(true));
        let out = commands::check_acceptance(nested.to_json_text().as_bytes());
        assert_ne!(
            out, r#"{"ok":true}"#,
            "nested extra member in {} must stay strict",
            path[0]
        );
    }
}

// Protection reachability: the public library API versus validated command inputs.

#[test]
fn check_transition_without_protection_is_protection_unavailable() {
    let input = case_input("transition-decisions", "tx-01-accept-permitted");
    let request = ActionRequest::from_value(input.get("request").unwrap()).unwrap();
    let resource = request
        .action
        .resource
        .canonical_resource_id
        .clone()
        .unwrap();
    assert_eq!(
        transition::check_transition(&request, &resource, None),
        Err(KernelCode::ProtectionUnavailable)
    );
}

#[test]
fn command_inputs_never_reach_protection_unavailable() {
    // Through `decide`, a bundle whose transition statements lack workflow_protection is
    // rejected at load time, so check_transition is only reached with a classification.
    let mut input = case_input("transition-decisions", "tx-01-accept-permitted");
    match &mut input {
        Value::Obj(m) => match m.get_mut("bundle").unwrap() {
            Value::Obj(b) => {
                b.remove("workflow_protection");
            }
            _ => panic!("bundle"),
        },
        _ => panic!("input"),
    }
    let out = commands::decide(input.to_json_text().as_bytes());
    assert!(!out.contains("PROTECTION_UNAVAILABLE"), "{out}");
    assert!(!out.contains(r#""status":"allow""#), "{out}");
}
