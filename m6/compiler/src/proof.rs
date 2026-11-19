use crate::compile::{Draft, Rule};
use crate::enforcer::Enforcer;
use writ_kernel::json::{self, Digest, Value};
use writ_kernel::policy::{load_bundle, LoadedBundle};

#[derive(Clone, Copy)]
pub(crate) struct Suite {
    pub(crate) seal: &'static str,
    pub(crate) fixtures: &'static str,
}

pub(crate) const SUITES: [Suite; 3] = [
    Suite {
        seal: include_str!("../fixtures/permit.seal.json"),
        fixtures: include_str!("../fixtures/permit.cases.json"),
    },
    Suite {
        seal: include_str!("../fixtures/approval.seal.json"),
        fixtures: include_str!("../fixtures/approval.cases.json"),
    },
    Suite {
        seal: include_str!("../fixtures/deny.seal.json"),
        fixtures: include_str!("../fixtures/deny.cases.json"),
    },
];

pub(crate) fn run(
    draft: &Draft,
    registry: &[Enforcer],
    suites: &[Suite],
) -> (Vec<Value>, Value, Vec<String>) {
    let result = prove(draft, registry, suites);
    let evidence = draft
        .rules
        .iter()
        .map(|rule| rule_report(rule, result.is_ok()))
        .collect();
    match result {
        Ok(proof) => (evidence, proof, vec![]),
        Err(diagnostics) => (evidence, Value::Null, diagnostics),
    }
}

fn rule_report(rule: &Rule, verified: bool) -> Value {
    json::obj([
        ("manifest", rule.manifest.clone()),
        ("priority", Value::Int(rule.priority)),
        ("statement_id", json::s(&rule.statement.statement_id)),
        ("binding", json::s(&rule.binding)),
        (
            "offline_status",
            json::s(if verified {
                "VERIFIED_OFFLINE"
            } else {
                "UNVERIFIED"
            }),
        ),
    ])
}

fn prove(draft: &Draft, registry: &[Enforcer], suites: &[Suite]) -> Result<Value, Vec<String>> {
    let mut diagnostics = Vec::new();
    let mut enforcer: Option<&Enforcer> = None;
    for rule in &draft.rules {
        let mut entries = registry.iter().filter(|entry| entry.id == rule.binding);
        match (entries.next(), entries.next()) {
            (Some(entry), None) if enforcer.is_none_or(|current| std::ptr::eq(current, entry)) => {
                enforcer = Some(entry);
            }
            _ => diagnostics.push(format!(
                "UNVERIFIED_ENFORCER:{}",
                rule.statement.statement_id
            )),
        }
    }
    if !diagnostics.is_empty() {
        return Err(diagnostics);
    }
    let enforcer = enforcer.ok_or_else(|| vec!["UNVERIFIED_ENFORCER".into()])?;
    prove_candidate(draft, enforcer, suites).map_err(|code| vec![code.into()])
}

fn prove_candidate(
    draft: &Draft,
    enforcer: &Enforcer,
    suites: &[Suite],
) -> Result<Value, &'static str> {
    let bundle = load_bundle(&draft.bundle).map_err(|_| "UNVERIFIED_BUNDLE")?;
    let (expected, fixture_text) = evidence_for(suites, &bundle)?;
    let implementation = (enforcer.identity)().ok_or("UNVERIFIED_IMPLEMENTATION")?;
    let fixtures = json::parse(fixture_text.as_bytes()).map_err(|_| "UNVERIFIED_FIXTURE_FORMAT")?;
    let evidence = seal(draft, &bundle, enforcer, &implementation, &fixtures)?;
    if expected != evidence.as_str() {
        return Err("UNVERIFIED_STALE_EVIDENCE");
    }
    let results = run_fixtures(draft, enforcer, &bundle, &fixtures)?;
    Ok(json::obj([
        ("evidence_digest", json::s(evidence.as_str())),
        ("implementation_digest", json::s(implementation.as_str())),
        (
            "fixture_digest",
            json::s(
                json::digest(&fixtures)
                    .map_err(|_| "UNVERIFIED_FIXTURE_FORMAT")?
                    .as_str(),
            ),
        ),
        ("fixture_results", Value::Arr(results)),
    ]))
}

fn evidence_for<'a>(
    suites: &'a [Suite],
    bundle: &LoadedBundle,
) -> Result<(String, &'a str), &'static str> {
    let mut evidence = None;
    for suite in suites {
        let document = json::parse(suite.seal.as_bytes()).map_err(|_| "UNVERIFIED_SEAL_FORMAT")?;
        let Value::Obj(fields) = document else {
            return Err("UNVERIFIED_SEAL_FORMAT");
        };
        let (Some(bundle_digest), Some(evidence_digest)) = (
            fields.get("bundle_digest").and_then(Value::as_str),
            fields.get("evidence_digest").and_then(Value::as_str),
        ) else {
            return Err("UNVERIFIED_SEAL_FORMAT");
        };
        if fields.len() != 2
            || !writ_kernel::ids::is_digest(bundle_digest)
            || !writ_kernel::ids::is_digest(evidence_digest)
        {
            return Err("UNVERIFIED_SEAL_FORMAT");
        }
        if bundle_digest == bundle.sha.as_str()
            && evidence
                .replace((evidence_digest.to_owned(), suite.fixtures))
                .is_some()
        {
            return Err("UNVERIFIED_DUPLICATE_EVIDENCE");
        }
    }
    evidence.ok_or("UNVERIFIED_MISSING_EVIDENCE")
}

fn seal(
    draft: &Draft,
    bundle: &LoadedBundle,
    enforcer: &Enforcer,
    implementation: &Digest,
    fixtures: &Value,
) -> Result<Digest, &'static str> {
    let binding = json::obj([
        ("proof_version", json::s("m6-kernel-proof/1")),
        ("bundle_digest", json::s(bundle.sha.as_str())),
        ("implementation", json::s(enforcer.id)),
        ("implementation_digest", json::s(implementation.as_str())),
        (
            "manifests",
            Value::Arr(
                draft
                    .rules
                    .iter()
                    .map(|rule| rule.manifest.clone())
                    .collect(),
            ),
        ),
        ("fixtures", fixtures.clone()),
    ]);
    json::digest(&binding).map_err(|_| "UNVERIFIED_SEAL_FORMAT")
}

fn run_fixtures(
    draft: &Draft,
    enforcer: &Enforcer,
    bundle: &LoadedBundle,
    fixtures: &Value,
) -> Result<Vec<Value>, &'static str> {
    let Value::Arr(cases) = fixtures else {
        return Err("UNVERIFIED_FIXTURE_FORMAT");
    };
    let mut results = std::collections::BTreeMap::new();
    let mut matched_ids = std::collections::BTreeSet::new();
    for case in cases {
        let Value::Obj(fields) = case else {
            return Err("UNVERIFIED_FIXTURE_FORMAT");
        };
        let (Some(id), Some(request), Some(expected)) = (
            fields.get("id").and_then(Value::as_str),
            fields.get("request"),
            fields.get("expected"),
        ) else {
            return Err("UNVERIFIED_FIXTURE_FORMAT");
        };
        if fields.len() != 3 || results.contains_key(id) {
            return Err("UNVERIFIED_FIXTURE_FORMAT");
        }
        let actual = (enforcer.run)(request, bundle).ok_or("UNVERIFIED_ENFORCER_EXECUTION")?;
        if &actual != expected {
            return Err("UNVERIFIED_FIXTURE_FAILED");
        }
        let expected_reason = match id {
            "resource-scope" => Some("RESOURCE_SCOPE_MISMATCH"),
            "scope" => Some("DEFAULT_DENY"),
            "unknown" => Some("UNKNOWN_CAPABILITY"),
            _ => None,
        };
        let matched = actual
            .get("matched_rule_ids")
            .and_then(Value::as_arr)
            .ok_or("UNVERIFIED_FIXTURE_EXECUTION")?;
        if let Some(reason) = expected_reason {
            if !matched.is_empty()
                || actual.get("status").and_then(Value::as_str) != Some("deny")
                || actual.get("reason_code").and_then(Value::as_str) != Some(reason)
            {
                return Err("UNVERIFIED_FIXTURE_COVERAGE");
            }
        } else {
            if matched.is_empty() {
                return Err("UNVERIFIED_FIXTURE_COVERAGE");
            }
            matched_ids.extend(matched.iter().filter_map(Value::as_str).map(str::to_owned));
        }
        results.insert(id, json::obj([("id", json::s(id)), ("decision", actual)]));
    }
    let ids: Vec<_> = results.keys().copied().collect();
    if ids
        != [
            "approval",
            "permit",
            "resource-scope",
            "scope",
            "unknown",
            "winner",
        ]
        || !draft
            .rules
            .iter()
            .all(|rule| matched_ids.contains(&rule.statement.statement_id))
        || draft.rules.iter().any(|rule| rule.vectors != ids)
    {
        return Err("UNVERIFIED_FIXTURE_COVERAGE");
    }
    Ok(results.into_values().collect())
}
