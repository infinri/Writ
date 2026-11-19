//! Offline policy compilation and executable proof for D-050.

mod compile;
mod conflict;
mod enforcer;
mod proof;

use writ_kernel::json::{self, Value};
use writ_kernel::policy::{load_bundle, LoadedBundle};

/// Stable compiler diagnostic with a location in the unsigned input.
#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub struct Diagnostic {
    /// Machine code without statement identifiers or input values.
    pub code: String,
    /// JSON Pointer to the input region associated with the code.
    pub pointer: String,
}

impl Diagnostic {
    fn new(code: &str, pointer: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            pointer: pointer.into(),
        }
    }
}

/// A candidate in the compiler's supported subset for isolated simulation.
/// It cannot produce a signed or publishable policy.
pub struct SimulationDraft {
    draft: compile::Draft,
    bundle: LoadedBundle,
    enforcer: &'static enforcer::Enforcer,
}

/// Compile and analyze the existing supported subset without requiring pinned evidence.
/// The existing publication-facing evaluator still requires full pinned evidence.
pub fn prepare_simulation(input: &[u8]) -> Result<SimulationDraft, Vec<Diagnostic>> {
    let draft = compile::compile_detailed(input)?;
    analyze(&draft).map_err(|codes| safe_diagnostics(&codes, "/companions"))?;
    let enforcer = proof::registered_enforcer(&draft, &enforcer::REGISTRY)
        .map_err(|codes| safe_diagnostics(&codes, "/manifests"))?;
    let bundle =
        load_bundle(&draft.bundle).map_err(|_| vec![Diagnostic::new("UNVERIFIED_BUNDLE", "")])?;
    Ok(SimulationDraft {
        draft,
        bundle,
        enforcer,
    })
}

impl SimulationDraft {
    /// Recheck the same pinned offline evidence used by the full evaluator.
    pub fn verify_offline_evidence(&self) -> Vec<Diagnostic> {
        let (_, _, codes) = proof::run(&self.draft, &enforcer::REGISTRY, &proof::SUITES);
        safe_diagnostics(&codes, "")
    }

    /// Run the registered pure decision enforcer on a supplied fixture Request.
    pub fn decide_fixture(&self, request: &Value) -> Option<Value> {
        (self.enforcer.run)(request, &self.bundle)
    }
}

fn safe_diagnostics(codes: &[String], pointer: &str) -> Vec<Diagnostic> {
    let mut diagnostics: Vec<_> = codes
        .iter()
        .map(|code| Diagnostic::new(code.split(':').next().unwrap_or(code), pointer))
        .collect();
    diagnostics.sort_unstable();
    diagnostics.dedup();
    diagnostics
}

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
    let overlaps = match analyze(&draft) {
        Ok(overlaps) => overlaps,
        Err(diagnostics) => return report(None, vec![], vec![], Value::Null, diagnostics),
    };
    let (evidence, proof, diagnostics) = proof::run(&draft, registry, suites);
    let bundle = diagnostics.is_empty().then_some(draft.bundle);
    report(bundle, overlaps, evidence, proof, diagnostics)
}

fn analyze(draft: &compile::Draft) -> Result<Vec<Value>, Vec<String>> {
    let statements: Vec<_> = draft
        .rules
        .iter()
        .map(|rule| rule.statement.clone())
        .collect();
    conflict::analyze(&statements)
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
