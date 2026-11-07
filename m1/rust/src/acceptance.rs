//! `check_acceptance(record, current, requirements)`: pure validation of an
//! untrusted acceptance record against trusted broker state and a trusted,
//! version-bound requirement set (m1/fixtures/acceptance/SEMANTICS.md).
//!
//! Not covered (boundary 2): `record_digest` is an unkeyed consistency check,
//! not proof of issuance; trust labels are claims, not evaluator identity.

use crate::ids::{is_digest, is_id, is_name, is_policy_version};
use crate::json::{self, Value};
use crate::time::parse_strict_z;

pub const CONTRACT_VERSION: &str = "m1-draft-3";

fn exact_keys(v: &Value, keys: &[&str]) -> bool {
    match v.as_obj() {
        Some(m) => m.len() == keys.len() && keys.iter().all(|k| v.get(k).is_some()),
        None => false,
    }
}

fn s_ok(v: &Value, key: &str, f: fn(&str) -> bool) -> bool {
    v.str_at(key).is_some_and(f)
}

fn ts_ok(v: &Value, key: &str) -> bool {
    v.str_at(key).and_then(parse_strict_z).is_some()
}

fn revision_ok(v: &Value) -> bool {
    matches!(v.get("state_revision"), Some(Value::Int(n)) if (0..=json::MAX_SAFE).contains(n))
}

fn transition_ok(v: &Value) -> bool {
    v.get("transition")
        .is_some_and(|t| exact_keys(t, &["from_state", "to_state"]) && s_ok(t, "from_state", is_name) && s_ok(t, "to_state", is_name))
}

fn id_list_ok(v: &Value, key: &str, f: fn(&str) -> bool) -> bool {
    v.get(key).and_then(Value::as_arr).is_some_and(|a| a.iter().all(|x| x.as_str().is_some_and(f)))
}

fn policy_binding_ok(v: &Value) -> bool {
    s_ok(v, "acceptance_policy_id", is_id)
        && s_ok(v, "acceptance_policy_version", is_policy_version)
        && s_ok(v, "acceptance_policy_bundle_sha", is_digest)
}

fn unique_ids(items: &[Value]) -> bool {
    let ids: Vec<&str> = items.iter().filter_map(|c| c.str_at("criterion_id")).collect();
    ids.iter().enumerate().all(|(i, a)| !ids[..i].contains(a))
}

pub fn current_valid(c: &Value) -> bool {
    exact_keys(
        c,
        &[
            "tenant_id", "project_id", "task_id", "workflow_id", "transition", "artifact_snapshot_id", "artifact_digest",
            "acceptance_policy_id", "acceptance_policy_version", "acceptance_policy_bundle_sha", "state_revision", "now",
            "consumed_record_ids", "revoked_evaluator_ids",
        ],
    ) && s_ok(c, "tenant_id", is_id)
        && s_ok(c, "project_id", is_id)
        && s_ok(c, "task_id", is_id)
        && s_ok(c, "workflow_id", is_name)
        && transition_ok(c)
        && s_ok(c, "artifact_snapshot_id", is_id)
        && s_ok(c, "artifact_digest", is_digest)
        && policy_binding_ok(c)
        && revision_ok(c)
        && ts_ok(c, "now")
        && id_list_ok(c, "consumed_record_ids", is_id)
        && id_list_ok(c, "revoked_evaluator_ids", is_name)
}

pub fn requirements_valid(r: &Value) -> bool {
    if !exact_keys(r, &["acceptance_policy_id", "acceptance_policy_version", "acceptance_policy_bundle_sha", "criteria"]) || !policy_binding_ok(r) {
        return false;
    }
    let Some(criteria) = r.get("criteria").and_then(Value::as_arr) else { return false };
    !criteria.is_empty()
        && criteria.iter().all(|c| {
            exact_keys(c, &["criterion_id", "required_class"])
                && s_ok(c, "criterion_id", is_name)
                && matches!(c.str_at("required_class"), Some("mechanical" | "evidence" | "human"))
        })
        && unique_ids(criteria)
}

fn criterion_valid(c: &Value) -> bool {
    exact_keys(c, &["criterion_id", "mandatory", "required_class", "status", "evaluator_id", "evaluator_trust", "fresh_until"])
        && s_ok(c, "criterion_id", is_name)
        && c.get("mandatory").and_then(Value::as_bool).is_some()
        && matches!(c.str_at("required_class"), Some("mechanical" | "evidence" | "human" | "model_assisted"))
        && matches!(c.str_at("status"), Some("pass" | "fail" | "unknown" | "stale" | "waived"))
        && s_ok(c, "evaluator_id", is_name)
        && matches!(c.str_at("evaluator_trust"), Some("U" | "MA" | "M" | "E" | "H"))
        && ts_ok(c, "fresh_until")
        // A model-assisted criterion is advisory only (D-017).
        && !(c.str_at("required_class") == Some("model_assisted") && c.get("mandatory") == Some(&Value::Bool(true)))
}

pub fn record_valid(r: &Value) -> bool {
    if !exact_keys(
        r,
        &[
            "contract_version", "acceptance_record_id", "tenant_id", "project_id", "task_id", "workflow_id", "transition",
            "artifact_snapshot_id", "artifact_digest", "acceptance_policy_id", "acceptance_policy_version",
            "acceptance_policy_bundle_sha", "state_revision", "status", "issued_at", "expires_at", "criteria", "record_digest",
        ],
    ) {
        return false;
    }
    let Some(criteria) = r.get("criteria").and_then(Value::as_arr) else { return false };
    r.str_at("contract_version") == Some(CONTRACT_VERSION)
        && s_ok(r, "acceptance_record_id", is_id)
        && s_ok(r, "tenant_id", is_id)
        && s_ok(r, "project_id", is_id)
        && s_ok(r, "task_id", is_id)
        && s_ok(r, "workflow_id", is_name)
        && transition_ok(r)
        && s_ok(r, "artifact_snapshot_id", is_id)
        && s_ok(r, "artifact_digest", is_digest)
        && policy_binding_ok(r)
        && revision_ok(r)
        && matches!(r.str_at("status"), Some("accepted" | "rejected" | "incomplete" | "needs_human_review"))
        && ts_ok(r, "issued_at")
        && ts_ok(r, "expires_at")
        && !criteria.is_empty()
        && criteria.iter().all(criterion_valid)
        && unique_ids(criteria)
        && s_ok(r, "record_digest", is_digest)
}

/// D-006 digest of the record without its `record_digest` member.
pub fn record_digest(record: &Value) -> Option<String> {
    json::digest(&record.without("record_digest")).ok()
}

fn ts(v: &Value, key: &str) -> i64 {
    v.str_at(key).and_then(parse_strict_z).expect("validated timestamp")
}

fn same(a: &Value, b: &Value, keys: &[&str]) -> bool {
    keys.iter().all(|k| a.get(k) == b.get(k))
}

fn expected_trust(class: &str) -> &'static str {
    match class {
        "mechanical" => "M",
        "evidence" => "E",
        _ => "H",
    }
}

/// Returns `Ok(())` or the internal refusal code.
pub fn check(record: Option<&Value>, current: Option<&Value>, requirements: Option<&Value>) -> Result<(), &'static str> {
    let current = current.filter(|c| current_valid(c)).ok_or("CURRENT_STATE_MALFORMED")?;
    let reqs = requirements.filter(|r| requirements_valid(r)).ok_or("REQUIREMENTS_MALFORMED")?;
    let policy_keys = ["acceptance_policy_id", "acceptance_policy_version", "acceptance_policy_bundle_sha"];
    if !same(reqs, current, &policy_keys) {
        return Err("REQUIREMENTS_POLICY_MISMATCH");
    }
    let record = record.filter(|r| record_valid(r)).ok_or("ACCEPTANCE_RECORD_MALFORMED")?;

    let now = ts(current, "now");
    let (issued, expires) = (ts(record, "issued_at"), ts(record, "expires_at"));
    if record_digest(record).as_deref() != record.str_at("record_digest") || expires <= issued || issued > now {
        return Err("ACCEPTANCE_RECORD_INVALID");
    }
    let rid = record.get("acceptance_record_id");
    if current.get("consumed_record_ids").and_then(Value::as_arr).is_some_and(|ids| ids.iter().any(|x| Some(x) == rid)) {
        return Err("ACCEPTANCE_REPLAYED");
    }
    if !same(record, current, &["tenant_id", "project_id", "task_id", "workflow_id"]) {
        return Err("ACCEPTANCE_SCOPE_MISMATCH");
    }
    if !same(record, current, &["transition"]) {
        return Err("ACCEPTANCE_TRANSITION_MISMATCH");
    }
    if !same(record, current, &["artifact_snapshot_id", "artifact_digest"]) {
        return Err("ACCEPTANCE_ARTIFACT_MISMATCH");
    }
    if !same(record, current, &policy_keys) {
        return Err("ACCEPTANCE_POLICY_MISMATCH");
    }
    if !same(record, current, &["state_revision"]) {
        return Err("ACCEPTANCE_REVISION_MISMATCH");
    }
    if now >= expires {
        return Err("ACCEPTANCE_EXPIRED");
    }

    let crits = record.get("criteria").and_then(Value::as_arr).unwrap();
    let find = |id: &str| crits.iter().find(|c| c.str_at("criterion_id") == Some(id));
    let mut required: Vec<(&str, &str)> = reqs
        .get("criteria")
        .and_then(Value::as_arr)
        .unwrap()
        .iter()
        .map(|c| (c.str_at("criterion_id").unwrap(), c.str_at("required_class").unwrap()))
        .collect();
    required.sort_by(|a, b| a.0.as_bytes().cmp(b.0.as_bytes()));

    for (id, class) in &required {
        match find(id) {
            None => return Err("ACCEPTANCE_CRITERIA_MISSING"),
            Some(c) => {
                if c.get("mandatory") != Some(&Value::Bool(true)) || c.str_at("required_class") != Some(class) {
                    return Err("ACCEPTANCE_CRITERIA_MISMATCH");
                }
            }
        }
    }
    if crits.iter().any(|c| {
        c.get("mandatory") == Some(&Value::Bool(true)) && !required.iter().any(|(id, _)| c.str_at("criterion_id") == Some(id))
    }) {
        return Err("ACCEPTANCE_CRITERIA_MISMATCH");
    }

    let revoked = current.get("revoked_evaluator_ids").and_then(Value::as_arr).unwrap();
    for (id, class) in &required {
        let c = find(id).unwrap();
        if c.str_at("evaluator_trust") != Some(expected_trust(class)) {
            return Err("ACCEPTANCE_UNTRUSTED_EVALUATOR");
        }
        if revoked.iter().any(|x| Some(x) == c.get("evaluator_id")) {
            return Err("ACCEPTANCE_EVALUATOR_REVOKED");
        }
        let status = c.str_at("status").unwrap();
        if status == "stale" || (status == "pass" && now >= ts(c, "fresh_until")) {
            return Err("ACCEPTANCE_STALE");
        }
        if matches!(status, "fail" | "unknown" | "waived") {
            return Err("ACCEPTANCE_INCOMPLETE");
        }
    }
    if record.str_at("status") != Some("accepted") {
        return Err("ACCEPTANCE_RECORD_INVALID");
    }
    Ok(())
}
