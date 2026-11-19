//! Offline policy compilation and executable proof for D-050.

mod compile;
mod conflict;
mod enforcer;
mod proof;

use writ_kernel::json::{self, Value};

/// Result of offline evaluation.
pub struct Report {
    /// Whether the candidate passed compilation, conflict analysis and kernel proof.
    pub accepted: bool,
    /// Canonicalizable report. A bundle is supplied only after successful offline proof.
    pub document: Value,
}

/// Compile and prove a candidate using the built-in enforcer registry and evidence.
pub fn evaluate(input: &[u8]) -> Report {
    evaluate_with(input, &enforcer::REGISTRY, &proof::SUITES)
}

fn evaluate_with(input: &[u8], registry: &[enforcer::Enforcer], suites: &[proof::Suite]) -> Report {
    let draft = match compile::compile(input) {
        Ok(draft) => draft,
        Err(diagnostics) => return report(None, vec![], vec![], Value::Null, diagnostics),
    };
    let statements: Vec<_> = draft
        .rules
        .iter()
        .map(|rule| rule.statement.clone())
        .collect();
    let overlaps = match conflict::analyze(&statements) {
        Ok(overlaps) => overlaps,
        Err(diagnostics) => return report(None, vec![], vec![], Value::Null, diagnostics),
    };
    let (evidence, proof, diagnostics) = proof::run(&draft, registry, suites);
    let bundle = diagnostics.is_empty().then_some(draft.bundle);
    report(bundle, overlaps, evidence, proof, diagnostics)
}

fn report(
    bundle: Option<Value>,
    overlaps: Vec<Value>,
    evidence: Vec<Value>,
    proof: Value,
    mut diagnostics: Vec<String>,
) -> Report {
    diagnostics.sort_unstable();
    diagnostics.dedup();
    let accepted = bundle.is_some();
    let bundle_sha = bundle
        .as_ref()
        .and_then(|bundle| writ_kernel::policy::bundle_sha(bundle).ok());
    Report {
        accepted,
        document: json::obj([
            ("report_version", json::s("m6-offline-policy-report/1")),
            (
                "status",
                json::s(if accepted {
                    "accepted_offline"
                } else {
                    "refused"
                }),
            ),
            ("deployed_enforcement", json::s("UNVERIFIED")),
            ("runtime_authority", Value::Bool(false)),
            ("bundle", bundle.unwrap_or(Value::Null)),
            (
                "bundle_digest",
                bundle_sha.map_or(Value::Null, |sha| json::s(sha.as_str())),
            ),
            ("overlaps", Value::Arr(overlaps)),
            ("rule_evidence", Value::Arr(evidence)),
            ("kernel_proof", proof),
            (
                "diagnostics",
                Value::Arr(diagnostics.iter().map(|message| json::s(message)).collect()),
            ),
        ]),
    }
}

#[cfg(test)]
mod tests;
