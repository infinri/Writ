use writ_kernel::{
    contracts::{AcceptanceRequirements, CriterionClass},
    json::{self, Value},
    policy::{self, LoadedBundle},
};

use crate::{wire, Error, Publication, Scope};

const ACCEPTANCE_ID: &str = "m6-acceptance";
const ACTION: &str = "synthetic.inspect";

pub(crate) struct Policies {
    pub(crate) authorization: LoadedBundle,
    pub(crate) acceptance: AcceptanceRequirements,
}

fn resource_prefix(scope: &Scope) -> String {
    format!("synthetic:{}/{}/", scope.tenant(), scope.project())
}

pub(crate) fn load(
    publication: &Publication,
    scope: &Scope,
    version: i64,
) -> Result<Policies, Error> {
    let prereq = wire::exact(
        &publication.prerequisites,
        &["version", "draft", "proof_digest"],
    )?;
    if wire::text(prereq, "version")? != "writ-policy-prerequisites/1" {
        return Err(Error::Format);
    }
    let expected = wire::digest_text(wire::text(prereq, "proof_digest")?)?;
    let draft = wire::canonical(&prereq["draft"])?;
    let report = writ_policy_compile::evaluate(draft.as_bytes());
    if !report.accepted {
        return Err(Error::InvalidPolicy);
    }
    let proof = report
        .document
        .get("kernel_proof")
        .ok_or(Error::InvalidPolicy)?;
    if wire::digest(proof)?.as_str() != expected {
        return Err(Error::InvalidPolicy);
    }
    let generated = report.document.get("bundle").ok_or(Error::InvalidPolicy)?;
    let payload = &publication.authorization.payload;
    if generated.get("publisher_signature") != Some(&Value::Null)
        || generated.without("publisher_signature") != *payload
    {
        return Err(Error::InvalidPolicy);
    }
    let authorization = policy::load_bundle(payload).map_err(|_| Error::InvalidPolicy)?;
    let prefix = resource_prefix(scope);
    if authorization.bundle.statements.iter().any(|statement| {
        !matches!(statement.tenants.as_deref(), Some([tenant]) if tenant == scope.tenant())
            || !matches!(statement.resource_prefixes.as_deref(), Some([value]) if value.starts_with(&prefix))
            || statement.action_kinds != [ACTION]
    }) {
        return Err(Error::Scope);
    }
    let acceptance = acceptance(&publication.acceptance.payload, version)?;
    Ok(Policies {
        authorization,
        acceptance,
    })
}

fn acceptance(payload: &Value, version: i64) -> Result<AcceptanceRequirements, Error> {
    let map = wire::exact(
        payload,
        &["version", "policy_id", "policy_version", "criteria"],
    )?;
    if wire::text(map, "version")? != "writ-acceptance-policy/1"
        || wire::text(map, "policy_id")? != ACCEPTANCE_ID
        || wire::text(map, "policy_version")? != version.to_string()
    {
        return Err(Error::InvalidPolicy);
    }
    let digest = wire::digest(payload)?;
    let requirements = AcceptanceRequirements::from_value(&json::obj([
        ("acceptance_policy_id", map["policy_id"].clone()),
        ("acceptance_policy_version", map["policy_version"].clone()),
        ("acceptance_policy_bundle_sha", json::s(digest.as_str())),
        ("criteria", map["criteria"].clone()),
    ]))
    .map_err(|_| Error::InvalidPolicy)?;
    if !requirements.criteria.iter().any(|criterion| {
        criterion.criterion_id == "review" && criterion.required_class == CriterionClass::Human
    }) {
        return Err(Error::InvalidPolicy);
    }
    Ok(requirements)
}
