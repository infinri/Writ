use std::{
    collections::BTreeSet,
    time::{Duration, Instant},
};

use writ_kernel::{
    contracts::CONTRACT_VERSION,
    decision::{in_scope, is_canonical_resource_id},
    json::{self, Value},
};
use writ_policy_compile::{prepare_simulation, Diagnostic, SimulationDraft};

use crate::{Error, Result, Scope};

const MAX_RULES: usize = 16;
const MAX_CASES: usize = 16;
const MAX_RESOURCE_BYTES: usize = 256;
const BUDGET: Duration = Duration::from_secs(5);

pub(crate) fn validate(scope: &Scope, candidate: &Value) -> Result<Value> {
    let started = Instant::now();
    let (compilable, issues) = match prepare(scope, candidate)? {
        Ok(draft) => (true, draft.verify_offline_evidence()),
        Err(issues) => (false, issues),
    };
    check_budget(started)?;
    let offline_proven = compilable && issues.is_empty();
    Ok(json::obj([
        ("compilable", Value::Bool(compilable)),
        ("offline_proven", Value::Bool(offline_proven)),
        ("deployed_enforcement", json::s("UNVERIFIED")),
        ("runtime_authority", Value::Bool(false)),
        (
            "diagnostics",
            Value::Arr(
                issues
                    .iter()
                    .map(|issue| {
                        json::obj([
                            ("code", json::s(&issue.code)),
                            ("pointer", json::s(&issue.pointer)),
                        ])
                    })
                    .collect(),
            ),
        ),
    ]))
}

pub(crate) fn simulate(scope: &Scope, candidate: &Value, corpus: &Value) -> Result<Value> {
    let started = Instant::now();
    let requests = requests(scope, corpus)?;
    let draft = prepare(scope, candidate)?.map_err(|_| Error::InvalidPolicy)?;
    let decisions = decisions(&draft, &requests, started)?;
    Ok(json::obj([
        ("runtime_authority", Value::Bool(false)),
        (
            "cases",
            Value::Arr(
                decisions
                    .into_iter()
                    .enumerate()
                    .map(|(index, decision)| {
                        json::obj([("case", Value::Int(index as i64)), ("decision", decision)])
                    })
                    .collect(),
            ),
        ),
    ]))
}

pub(crate) fn compare(
    scope: &Scope,
    before: &Value,
    after: &Value,
    corpus: &Value,
) -> Result<Value> {
    let started = Instant::now();
    let requests = requests(scope, corpus)?;
    let left = prepare(scope, before)?.map_err(|_| Error::InvalidPolicy)?;
    check_budget(started)?;
    let right = prepare(scope, after)?.map_err(|_| Error::InvalidPolicy)?;
    let left = decisions(&left, &requests, started)?;
    let right = decisions(&right, &requests, started)?;
    let cases = left
        .into_iter()
        .zip(right)
        .enumerate()
        .map(|(index, (before, after))| {
            json::obj([
                ("case", Value::Int(index as i64)),
                ("changed", difference(&before, &after)),
                ("before", before),
                ("after", after),
            ])
        })
        .collect();
    let structure = difference(before, after);
    check_budget(started)?;
    Ok(json::obj([
        ("runtime_authority", Value::Bool(false)),
        ("structure", structure),
        ("cases", Value::Arr(cases)),
    ]))
}

pub(crate) fn prepare(
    scope: &Scope,
    candidate: &Value,
) -> Result<std::result::Result<SimulationDraft, Vec<Diagnostic>>> {
    let candidate = if let Some(authorization) = candidate.get("authorization") {
        let valid = candidate.as_obj().is_some_and(|fields| fields.len() == 2)
            && candidate.get("acceptance").is_some_and(|acceptance| {
                writ_policy_publication_lab::simulation_requirements(acceptance).is_ok()
            });
        if !valid {
            return Ok(Err(vec![Diagnostic {
                code: "INVALID_ACCEPTANCE_POLICY".into(),
                pointer: "/acceptance".into(),
            }]));
        }
        authorization
    } else {
        candidate
    };
    for group in ["manifests", "companions"] {
        if candidate
            .get(group)
            .and_then(Value::as_arr)
            .is_some_and(|rules| rules.len() > MAX_RULES)
        {
            return Err(Error::LimitExceeded);
        }
    }
    let input = json::canonical(candidate).map_err(|_| Error::InvalidRequest)?;
    if input.len() > crate::MAX_CANDIDATE {
        return Err(Error::LimitExceeded);
    }
    let draft = match prepare_simulation(input.as_bytes()) {
        Ok(draft) => draft,
        Err(issues) => return Ok(Err(issues)),
    };
    let mut issues = Vec::new();
    for (group, field) in [
        ("manifests", "tenant_scope"),
        ("companions", "resource_prefix"),
    ] {
        if let Some(rows) = candidate.get(group).and_then(Value::as_arr) {
            for (index, row) in rows.iter().enumerate() {
                let value = row.get(field).and_then(Value::as_str).unwrap_or_default();
                let matches_scope = if group == "manifests" {
                    value == scope.tenant
                } else {
                    in_scope(value, &scope.tenant, &scope.project)
                };
                if !matches_scope {
                    issues.push(Diagnostic {
                        code: "DRAFT_SCOPE_MISMATCH".into(),
                        pointer: format!("/{group}/{index}/{field}"),
                    });
                }
            }
        }
    }
    if issues.is_empty() {
        Ok(Ok(draft))
    } else {
        Ok(Err(issues))
    }
}

fn requests(scope: &Scope, corpus: &Value) -> Result<Vec<Value>> {
    let rows = corpus
        .as_arr()
        .filter(|rows| !rows.is_empty())
        .ok_or(Error::InvalidRequest)?;
    if rows.len() > MAX_CASES {
        return Err(Error::LimitExceeded);
    }
    rows.iter()
        .enumerate()
        .map(|(index, row)| request(scope, index, row))
        .collect()
}

fn request(scope: &Scope, index: usize, row: &Value) -> Result<Value> {
    let fields = row.as_obj().ok_or(Error::InvalidRequest)?;
    let kind = row
        .get("kind")
        .and_then(Value::as_str)
        .filter(|kind| matches!(*kind, "synthetic.inspect" | "synthetic.unknown"))
        .ok_or(Error::InvalidRequest)?;
    let resource = row
        .get("resource")
        .and_then(Value::as_str)
        .ok_or(Error::InvalidRequest)?;
    if resource.len() > MAX_RESOURCE_BYTES {
        return Err(Error::LimitExceeded);
    }
    if fields.len() != 2
        || !resource.starts_with("synthetic:")
        || !is_canonical_resource_id(resource)
        || !in_scope(resource, &scope.tenant, &scope.project)
    {
        return Err(Error::InvalidRequest);
    }
    let resource = json::obj([
        ("uri", json::s(resource)),
        ("canonical_resource_id", json::s(resource)),
    ]);
    let action = json::obj([("kind", json::s(kind)), ("resource", resource.clone())]);
    let digest = json::digest(&action).map_err(|_| Error::InvalidRequest)?;
    let id = format!("m6-case-{index}");
    Ok(json::obj([
        ("contract_version", json::s(CONTRACT_VERSION)),
        ("request_id", json::s(&id)),
        ("trace_id", json::s(&id)),
        ("tenant_id", json::s(&scope.tenant)),
        ("project_id", json::s(&scope.project)),
        ("session_id", json::s("m6-fixture-session")),
        ("idempotency_key", json::s(&id)),
        ("expected_state_revision", Value::Int(1)),
        (
            "principal",
            json::obj([
                ("id", json::s("agent:m6-fixture")),
                ("verified_by", json::s("m6-synthetic")),
                ("authn_method", json::s("fixture")),
                ("roles", Value::Arr(vec![json::s("agent")])),
            ]),
        ),
        (
            "action",
            json::obj([
                ("kind", json::s(kind)),
                ("resource", resource),
                ("action_digest", json::s(digest.as_str())),
            ]),
        ),
    ]))
}

fn decisions(draft: &SimulationDraft, requests: &[Value], started: Instant) -> Result<Vec<Value>> {
    let mut decisions = Vec::with_capacity(requests.len());
    for request in requests {
        check_budget(started)?;
        decisions.push(draft.decide_fixture(request).ok_or(Error::Unavailable)?);
    }
    check_budget(started)?;
    Ok(decisions)
}

fn check_budget(started: Instant) -> Result<()> {
    if started.elapsed() > BUDGET {
        return Err(Error::LimitExceeded);
    }
    Ok(())
}

pub(crate) fn difference(before: &Value, after: &Value) -> Value {
    let mut changed = Vec::new();
    collect_difference("", Some(before), Some(after), &mut changed);
    Value::Arr(changed)
}

fn collect_difference(
    pointer: &str,
    before: Option<&Value>,
    after: Option<&Value>,
    changed: &mut Vec<Value>,
) {
    if before == after {
        return;
    }
    match (before, after) {
        (Some(Value::Obj(before)), Some(Value::Obj(after))) => {
            let keys: BTreeSet<_> = before.keys().chain(after.keys()).collect();
            for key in keys {
                let token = key.replace('~', "~0").replace('/', "~1");
                collect_difference(
                    &format!("{pointer}/{token}"),
                    before.get(key),
                    after.get(key),
                    changed,
                );
            }
        }
        (Some(Value::Arr(before)), Some(Value::Arr(after))) => {
            for index in 0..before.len().max(after.len()) {
                collect_difference(
                    &format!("{pointer}/{index}"),
                    before.get(index),
                    after.get(index),
                    changed,
                );
            }
        }
        _ => changed.push(json::s(pointer)),
    }
}
