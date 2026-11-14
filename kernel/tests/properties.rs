//! Property-style tests with a fixed-seed generator (no external crates).
//!
//! Seeds are constants, so every run checks the same examples; a failure
//! message names the iteration. Properties: D-006 round trip and canonical
//! stability; strict-parser rejection of single injected defects; decide and
//! check-acceptance invariance under member order, whitespace and escape
//! permutations; deny precedence; statement order independence; tenant and
//! project isolation; approval ids binding the target state; single-field
//! mutations of acceptance bindings; malformed acceptance inputs.

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod common;

use common::{case_input, random_value, render, render_string, set_cases, Rng};
use writ_kernel::commands;
use writ_kernel::contracts::ActionRequest;
use writ_kernel::decision::approval_request_id;
use writ_kernel::json::{self, Map, Value};
use writ_kernel::policy::bundle_sha;
use writ_kernel::transition::TransitionRequest;

const SEED: u64 = 20_261_112;

fn obj_mut(v: &mut Value) -> &mut Map {
    match v {
        Value::Obj(m) => m,
        other => panic!("not an object: {other:?}"),
    }
}

fn member_mut<'a>(v: &'a mut Value, key: &str) -> &'a mut Value {
    obj_mut(v)
        .get_mut(key)
        .unwrap_or_else(|| panic!("member {key}"))
}

fn output(text: &str) -> Value {
    json::parse(text.as_bytes()).unwrap()
}

fn str_at<'a>(v: &'a Value, key: &str) -> &'a str {
    v.get(key).and_then(Value::as_str).unwrap_or("")
}

// ===========================================================================
// M2-02: strict JSON and canonical form.

#[test]
fn canonical_round_trip_and_rendering_invariance() {
    let mut rng = Rng::new(SEED);
    for i in 0..3000 {
        let v = random_value(&mut rng, 4);
        let c = json::canonical(&v).unwrap();
        let back = json::parse(c.as_bytes())
            .unwrap_or_else(|e| panic!("#{i}: canonical text does not parse ({e}): {c}"));
        assert_eq!(back, v, "#{i}");
        assert_eq!(
            json::canonical(&back).unwrap(),
            c,
            "#{i}: canonical form is a fixed point"
        );
        let rendered = render(&mut rng, &v, true, true);
        let reparsed = json::parse(rendered.as_bytes())
            .unwrap_or_else(|e| panic!("#{i}: rendering does not parse ({e}): {rendered:?}"));
        assert_eq!(
            json::canonical(&reparsed).unwrap(),
            c,
            "#{i}: member order, whitespace and escapes are insignificant"
        );
        assert_eq!(
            json::digest(&reparsed).unwrap(),
            json::digest(&v).unwrap(),
            "#{i}"
        );
        assert_eq!(
            commands::canonical(rendered.as_bytes()),
            commands::canonical(c.as_bytes()),
            "#{i}"
        );
    }
}

#[test]
fn single_injected_defects_are_rejected_with_their_code() {
    let defects: [(&str, &str); 20] = [
        ("1.0", "NUMBER_NOT_INTEGER"),
        ("-2.5", "NUMBER_NOT_INTEGER"),
        ("1e3", "NUMBER_NOT_INTEGER"),
        ("7E-1", "NUMBER_NOT_INTEGER"),
        ("0.0", "NUMBER_NOT_INTEGER"),
        ("-0", "NUMBER_NEGATIVE_ZERO"),
        ("9007199254740992", "NUMBER_OUT_OF_RANGE"),
        ("-9007199254740993", "NUMBER_OUT_OF_RANGE"),
        ("123456789012345678901", "NUMBER_OUT_OF_RANGE"),
        ("NaN", "INVALID_JSON"),
        ("Infinity", "INVALID_JSON"),
        ("-Infinity", "INVALID_JSON"),
        ("01", "INVALID_JSON"),
        ("+1", "INVALID_JSON"),
        ("tru", "INVALID_JSON"),
        ("'a'", "INVALID_JSON"),
        ("\"\\ud800\"", "LONE_SURROGATE"),
        ("\"\\udc00x\"", "LONE_SURROGATE"),
        ("\"\\ud83d\\u0041\"", "LONE_SURROGATE"),
        ("[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[0]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]", "DEPTH_EXCEEDED"),
    ];
    let mut rng = Rng::new(SEED + 1);
    let mut examples = 0;
    for i in 0..2000 {
        let v = random_value(&mut rng, 3);
        let valid = render(&mut rng, &v, true, true);
        let (token, code) = *rng.pick(&defects);
        // The defect sits at depth 1 (64 nested arrays then put its scalar at depth 65).
        let text = match rng.below(3) {
            0 => format!("[{valid},{token}]"),
            1 => format!("[{token},{valid}]"),
            _ => format!("{{\"v\":{valid},\"d\":{token}}}"),
        };
        assert_eq!(
            json::parse(text.as_bytes()).map_err(|e| e.code()),
            Err(code),
            "#{i}: {text:?}"
        );
        examples += 1;
    }
    for i in 0..1000 {
        // Duplicate member names, including names equal only after escape decoding.
        let name = common::random_string(&mut rng, 5);
        let (a, b) = (random_value(&mut rng, 2), random_value(&mut rng, 2));
        let first = render(&mut rng, &a, true, true);
        let second = render(&mut rng, &b, true, true);
        let text = format!(
            "{{{}:{first},{}:{second}}}",
            render_string(&mut rng, &name, true),
            render_string(&mut rng, &name, true)
        );
        let wrapped = if rng.chance(50) {
            format!("[{{\"ok\":1}},{text}]")
        } else {
            text
        };
        assert_eq!(
            json::parse(wrapped.as_bytes()).map_err(|e| e.code()),
            Err("DUPLICATE_KEY"),
            "#{i}: {wrapped:?}"
        );
        examples += 1;
    }
    assert!(examples >= 3000);
}

// ===========================================================================
// M2-04: decide determinism, order independence and deny precedence.

const DECIDE_SETS: [&str; 3] = [
    "golden-decisions",
    "transition-decisions",
    "protocol-decide",
];

#[test]
fn decide_is_invariant_under_member_order_whitespace_and_escapes() {
    let mut rng = Rng::new(SEED + 2);
    let mut checked = 0;
    for set in DECIDE_SETS {
        for case in set_cases(set) {
            let Some(input) = case_input(&case) else {
                continue;
            };
            let base = commands::decide(&case.input);
            assert_eq!(
                commands::decide(input.to_json_text().as_bytes()),
                base,
                "{}",
                case.id
            );
            for k in 0..10 {
                let text = render(&mut rng, &input, true, k % 2 == 0);
                assert_eq!(
                    commands::decide(text.as_bytes()),
                    base,
                    "{} permutation {k}",
                    case.id
                );
                checked += 1;
            }
        }
    }
    assert!(checked >= 1000, "{checked}");
}

/// Decisions that reached statement evaluation.
fn reached_statements(decision: &Value) -> bool {
    let status = str_at(decision, "status");
    let matched = decision
        .get("matched_rule_ids")
        .and_then(Value::as_arr)
        .is_some_and(|a| !a.is_empty());
    status == "allow"
        || status == "require_approval"
        || matched
        || str_at(decision, "reason_code") == "DEFAULT_DENY"
}

/// A request pinned to the original bundle's digest is re-pinned to the
/// mutated bundle (an unchanged pin correctly yields POLICY_VERSION_MISMATCH);
/// a deliberately wrong pin stays wrong.
fn repin(original: &Value, input: &mut Value) {
    let old = bundle_sha(original.get("bundle").unwrap()).unwrap();
    let new = bundle_sha(input.get("bundle").unwrap()).unwrap();
    let Some(Value::Obj(request)) = obj_mut(input).get_mut("request") else {
        return;
    };
    if request.get("policy_bundle_sha") == Some(&json::s(old.as_str())) {
        request.insert("policy_bundle_sha".into(), json::s(new.as_str()));
    }
}

fn statements_mut(input: &mut Value) -> &mut Vec<Value> {
    match member_mut(member_mut(input, "bundle"), "statements") {
        Value::Arr(a) => a,
        other => panic!("statements {other:?}"),
    }
}

#[test]
fn a_matching_forbid_always_wins() {
    let mut rng = Rng::new(SEED + 3);
    let mut checked = 0;
    for set in DECIDE_SETS {
        for case in set_cases(set) {
            let Some(input) = case_input(&case) else {
                continue;
            };
            if !matches!(input.get("bundle"), Some(Value::Obj(_))) {
                continue;
            }
            let base = output(&commands::decide(&case.input));
            if !reached_statements(&base) {
                continue;
            }
            let request = input.get("request").unwrap();
            let kind = request
                .get("action")
                .and_then(|a| a.get("kind"))
                .and_then(Value::as_str)
                .unwrap()
                .to_string();
            let tenant = str_at(request, "tenant_id").to_string();
            for k in 0..12 {
                let mut mutated = input.clone();
                let statements = statements_mut(&mut mutated);
                let mut forbid = json::obj([
                    ("statement_id", json::s("!prop-forbid")),
                    ("effect", json::s("forbid")),
                    ("action_kinds", Value::Arr(vec![json::s(&kind)])),
                    ("reason_code", json::s("PROP_FORBID")),
                ]);
                if rng.chance(50) {
                    obj_mut(&mut forbid)
                        .insert("tenants".into(), Value::Arr(vec![json::s(&tenant)]));
                }
                let at = rng.below(statements.len() + 1);
                statements.insert(at, forbid);
                if kind != "workflow.transition" && rng.chance(50) {
                    // A broad permit and approval for the same kind cannot outrank the forbid.
                    for (id, effect) in [
                        ("!prop-permit", "permit"),
                        ("!prop-approve", "require_approval"),
                    ] {
                        let at = rng.below(statements.len() + 1);
                        statements.insert(
                            at,
                            json::obj([
                                ("statement_id", json::s(id)),
                                ("effect", json::s(effect)),
                                ("action_kinds", Value::Arr(vec![json::s(&kind)])),
                                ("reason_code", json::s("PROP_GRANT")),
                            ]),
                        );
                    }
                }
                repin(&input, &mut mutated);
                let text = render(&mut rng, &mutated, true, false);
                let d = output(&commands::decide(text.as_bytes()));
                assert_eq!(
                    (str_at(&d, "status"), str_at(&d, "reason_code")),
                    ("deny", "PROP_FORBID"),
                    "{} variant {k}",
                    case.id
                );
                let matched = d.get("matched_rule_ids").and_then(Value::as_arr).unwrap();
                assert_eq!(
                    matched.first(),
                    Some(&json::s("!prop-forbid")),
                    "{} variant {k}",
                    case.id
                );
                assert!(
                    !matched.contains(&json::s("!prop-permit"))
                        && !matched.contains(&json::s("!prop-approve")),
                    "only forbids are reported"
                );
                assert_eq!(d.get("expires_at"), Some(&Value::Null));
                assert_eq!(d.get("approval_request_reference"), Some(&Value::Null));
                checked += 1;
            }
        }
    }
    assert!(checked >= 300, "{checked}");
}

#[test]
fn statement_order_never_changes_the_outcome() {
    let mut rng = Rng::new(SEED + 4);
    let fields = [
        "status",
        "reason_code",
        "matched_rule_ids",
        "approval_request_reference",
    ];
    for set in DECIDE_SETS {
        for case in set_cases(set) {
            let Some(input) = case_input(&case) else {
                continue;
            };
            if !matches!(input.get("bundle"), Some(Value::Obj(_))) {
                continue;
            }
            let base = output(&commands::decide(&case.input));
            for k in 0..5 {
                let mut mutated = input.clone();
                rng.shuffle(statements_mut(&mut mutated));
                repin(&input, &mut mutated);
                let d = output(&commands::decide(mutated.to_json_text().as_bytes()));
                for f in fields {
                    if f == "approval_request_reference" {
                        // The approval id binds the bundle digest, which covers statement order.
                        assert_eq!(
                            d.get(f).is_some_and(|v| v != &Value::Null),
                            base.get(f).is_some_and(|v| v != &Value::Null),
                            "{} {f} {k}",
                            case.id
                        );
                    } else {
                        assert_eq!(d.get(f), base.get(f), "{} {f} {k}", case.id);
                    }
                }
            }
        }
    }
}

fn golden_input(id: &str) -> Value {
    let case = set_cases("golden-decisions")
        .into_iter()
        .find(|c| c.id == id)
        .unwrap();
    case_input(&case).unwrap()
}

#[test]
fn requests_never_reach_another_tenant_or_project() {
    let mut rng = Rng::new(SEED + 5);
    let base = golden_input("workspace-file-write");
    assert_eq!(
        str_at(
            &output(&commands::decide(base.to_json_text().as_bytes())),
            "status"
        ),
        "allow"
    );
    let segments = [
        "fixture-tenant",
        "fixture-project",
        "tenant-b",
        "other",
        "fixture-tenant-x",
        "fixture",
        "a",
        "fixture-project-archive",
    ];
    for i in 0..1000 {
        let mut input = base.clone();
        let request = member_mut(&mut input, "request");
        let (tenant, project) = (*rng.pick(&segments), *rng.pick(&segments));
        let (rt, rp) = (*rng.pick(&segments), *rng.pick(&segments));
        obj_mut(request).insert("tenant_id".into(), json::s(tenant));
        obj_mut(request).insert("project_id".into(), json::s(project));
        let rid = format!("workspace:{rt}/{rp}/src/app.py");
        obj_mut(member_mut(member_mut(request, "action"), "resource"))
            .insert("canonical_resource_id".into(), json::s(&rid));
        let d = output(&commands::decide(input.to_json_text().as_bytes()));
        if (tenant, project) != (rt, rp) {
            assert_eq!(
                (str_at(&d, "status"), str_at(&d, "reason_code")),
                ("deny", "RESOURCE_SCOPE_MISMATCH"),
                "#{i}: {tenant}/{project} -> {rid}"
            );
        } else {
            assert_ne!(str_at(&d, "reason_code"), "RESOURCE_SCOPE_MISMATCH", "#{i}");
        }
    }
}

#[test]
fn approval_ids_bind_the_exact_transition() {
    let case = set_cases("transition-decisions")
        .into_iter()
        .find(|c| c.id == "tx-02-deploy-needs-approval")
        .unwrap();
    let input = case_input(&case).unwrap();
    let request = ActionRequest::from_value(input.get("request").unwrap()).unwrap();
    let rid = request
        .action
        .resource
        .canonical_resource_id
        .clone()
        .unwrap();
    let mut rng = Rng::new(SEED + 6);
    let states = [
        "verifying",
        "accepted",
        "deployed",
        "released",
        "planning",
        "draft",
        "x1",
        "x2",
    ];
    for i in 0..1000 {
        let (f, t1, t2) = (*rng.pick(&states), *rng.pick(&states), *rng.pick(&states));
        let tr = |to: &str| TransitionRequest {
            workflow_id: "wf-1".into(),
            from_state: f.into(),
            to_state: to.into(),
            binding: None,
        };
        let (a, b) = (
            approval_request_id(&request, &rid, "sha256:x", Some(&tr(t1))),
            approval_request_id(&request, &rid, "sha256:x", Some(&tr(t2))),
        );
        assert_eq!(a == b, t1 == t2, "#{i}");
        assert_ne!(
            approval_request_id(&request, &rid, "sha256:y", Some(&tr(t1))),
            a,
            "#{i}: binds the bundle digest"
        );
    }
    let d = output(&commands::decide(&case.input));
    assert!(str_at(&d, "approval_request_reference").starts_with("apr:"));
}

// ===========================================================================
// M2-05: acceptance bindings.

fn acceptance_base() -> Value {
    let case = set_cases("acceptance-binding")
        .into_iter()
        .find(|c| c.id == "pos-01-valid")
        .unwrap();
    let input = case_input(&case).unwrap();
    assert_eq!(
        commands::check_acceptance(input.to_json_text().as_bytes()),
        r#"{"ok":true}"#
    );
    input
}

fn reseal(input: &mut Value) {
    let record = member_mut(input, "record");
    let digest = json::digest(&record.without("record_digest")).unwrap();
    obj_mut(record).insert("record_digest".into(), json::s(digest.as_str()));
}

fn refusal(input: &Value) -> String {
    let out = output(&commands::check_acceptance(input.to_json_text().as_bytes()));
    out.get("refusal")
        .and_then(Value::as_str)
        .unwrap_or("ok")
        .to_string()
}

fn fresh_value(rng: &mut Rng, field: &str, old: &Value) -> Value {
    loop {
        let n = rng.next();
        let v = match field {
            "artifact_digest" | "acceptance_policy_bundle_sha" => {
                json::s(&format!("sha256:{:064x}", u128::from(n) * 0x1_0000_0001))
            }
            "acceptance_policy_version" => json::s(&(n % 1_000_000_000).to_string()),
            "state_revision" => Value::Int((n % 10_000) as i64),
            "workflow_id" => json::s(&format!("wf-{}", n % 1000)),
            "transition" => {
                let states = ["draft", "planning", "verifying", "accepted", "deployed"];
                let (f, t) = (states[(n % 5) as usize], states[((n / 5) % 5) as usize]);
                if f == t {
                    continue;
                }
                json::obj([("from_state", json::s(f)), ("to_state", json::s(t))])
            }
            _ => json::s(&format!(
                "{}{}",
                ["x", "1", "t", "p"][(n % 4) as usize],
                n % 100_000
            )),
        };
        if &v != old {
            return v;
        }
    }
}

#[test]
fn any_single_bound_field_mutation_refuses() {
    let base = acceptance_base();
    let bound: [(&str, &str); 11] = [
        ("tenant_id", "ACCEPTANCE_SCOPE_MISMATCH"),
        ("project_id", "ACCEPTANCE_SCOPE_MISMATCH"),
        ("task_id", "ACCEPTANCE_SCOPE_MISMATCH"),
        ("workflow_id", "ACCEPTANCE_SCOPE_MISMATCH"),
        ("transition", "ACCEPTANCE_TRANSITION_MISMATCH"),
        ("artifact_snapshot_id", "ACCEPTANCE_ARTIFACT_MISMATCH"),
        ("artifact_digest", "ACCEPTANCE_ARTIFACT_MISMATCH"),
        ("acceptance_policy_id", "ACCEPTANCE_POLICY_MISMATCH"),
        ("acceptance_policy_version", "ACCEPTANCE_POLICY_MISMATCH"),
        ("acceptance_policy_bundle_sha", "ACCEPTANCE_POLICY_MISMATCH"),
        ("state_revision", "ACCEPTANCE_REVISION_MISMATCH"),
    ];
    let mut rng = Rng::new(SEED + 7);
    for i in 0..3000 {
        let (field, record_code) = *rng.pick(&bound);
        let mut input = base.clone();
        let side = rng.below(3);
        let target = ["record", "current", "record"][side];
        let old = input
            .get(target)
            .and_then(|t| t.get(field))
            .cloned()
            .unwrap();
        let new = fresh_value(&mut rng, field, &old);
        obj_mut(member_mut(&mut input, target)).insert(field.into(), new);
        if side != 2 {
            reseal(&mut input);
        }
        let got = refusal(&input);
        let want = match side {
            0 => record_code,
            1 if field.starts_with("acceptance_policy") => "REQUIREMENTS_POLICY_MISMATCH",
            1 => record_code,
            _ => "ACCEPTANCE_RECORD_INVALID",
        };
        assert_eq!(got, want, "#{i}: {target}.{field}");
    }
}

#[test]
fn malformed_acceptance_inputs_refuse_in_trust_order() {
    let base = acceptance_base();
    let mut rng = Rng::new(SEED + 8);
    let codes = [
        ("current", "CURRENT_STATE_MALFORMED"),
        ("requirements", "REQUIREMENTS_MALFORMED"),
        ("record", "ACCEPTANCE_RECORD_MALFORMED"),
    ];
    for i in 0..1500 {
        let (target, code) = *rng.pick(&codes);
        let mut input = base.clone();
        let m = obj_mut(member_mut(&mut input, target));
        let keys: Vec<String> = m.keys().cloned().collect();
        let key = rng.pick(&keys).clone();
        let op = rng.below(3);
        let digest_touched = key == "record_digest" && op != 2;
        match op {
            0 => {
                m.remove(&key);
            }
            1 => {
                m.insert(key, Value::Bool(true));
            }
            _ => {
                m.insert(format!("extra_{}", rng.below(100)), Value::Null);
            }
        }
        if target == "record" && !digest_touched && rng.chance(50) {
            // Resealing cannot rescue a malformed record.
            let record = member_mut(&mut input, "record");
            let d = json::digest(&record.without("record_digest")).unwrap();
            obj_mut(record).insert("record_digest".into(), json::s(d.as_str()));
        }
        assert_eq!(refusal(&input), code, "#{i}: {target}");
    }
}

#[test]
fn check_acceptance_is_invariant_under_member_order_whitespace_and_escapes() {
    let mut rng = Rng::new(SEED + 9);
    let mut checked = 0;
    for set in ["acceptance-binding", "protocol-acceptance"] {
        for case in set_cases(set) {
            let Some(input) = case_input(&case) else {
                continue;
            };
            let base = commands::check_acceptance(&case.input);
            for k in 0..10 {
                let text = render(&mut rng, &input, true, k % 2 == 0);
                assert_eq!(
                    commands::check_acceptance(text.as_bytes()),
                    base,
                    "{} permutation {k}",
                    case.id
                );
                checked += 1;
            }
        }
    }
    assert!(checked >= 800, "{checked}");
}
