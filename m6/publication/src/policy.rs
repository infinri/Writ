use std::collections::BTreeMap;

use writ_kernel::{
    contracts::{AcceptanceRequirements, CriterionClass},
    json::{self, Value},
    policy::{self, LoadedBundle},
};

use writ_broker::waiver::WAIVER_MAX_SECONDS;

use crate::{signature::ACCEPTANCE_V2, wire, Error, Publication, Scope};

const ACCEPTANCE_ID: &str = "m6-acceptance";
const ACTION: &str = "synthetic.inspect";

pub(crate) struct Policies {
    pub(crate) authorization: LoadedBundle,
    pub(crate) acceptance: AcceptanceRequirements,
    pub(crate) waivers: BTreeMap<String, u32>,
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
    let (acceptance, waivers) = acceptance(&publication.acceptance.payload, version)?;
    Ok(Policies {
        authorization,
        acceptance,
        waivers,
    })
}

pub(crate) fn preserve_required_criteria(
    candidate: &Policies,
    selected: Option<(i64, &Publication)>,
) -> Result<(), Error> {
    let Some((version, publication)) = selected else {
        return Ok(());
    };
    let (required, _) = acceptance(&publication.acceptance.payload, version)?;
    let mut candidate = candidate.acceptance.criteria.iter();
    if required
        .criteria
        .iter()
        .all(|required| candidate.any(|criterion| criterion == required))
    {
        Ok(())
    } else {
        Err(Error::InvalidPolicy)
    }
}

pub(crate) fn refuse_deactivation(
    publication: &Publication,
    acceptance: &AcceptanceRequirements,
    policy_id: &str,
) -> Result<(), Error> {
    if policy_id == ACCEPTANCE_ID
        || acceptance
            .criteria
            .iter()
            .any(|criterion| criterion.criterion_id == policy_id)
    {
        return Err(Error::InvalidPolicy);
    }
    let manifests = publication
        .prerequisites
        .get("draft")
        .and_then(|draft| draft.get("manifests"))
        .and_then(Value::as_arr)
        .ok_or(Error::InvalidPolicy)?;
    if manifests
        .iter()
        .any(|manifest| manifest.get("policy_id").and_then(Value::as_str) == Some(policy_id))
    {
        return Err(Error::Unsupported);
    }
    Err(Error::InvalidPolicy)
}

fn acceptance(
    payload: &Value,
    version: i64,
) -> Result<(AcceptanceRequirements, BTreeMap<String, u32>), Error> {
    let map = wire::exact(
        payload,
        &["version", "policy_id", "policy_version", "criteria"],
    )?;
    if wire::text(map, "policy_id")? != ACCEPTANCE_ID
        || wire::text(map, "policy_version")? != version.to_string()
    {
        return Err(Error::InvalidPolicy);
    }
    let mut waivers = BTreeMap::new();
    let criteria = match wire::text(map, "version")? {
        "writ-acceptance-policy/1" => map["criteria"].clone(),
        ACCEPTANCE_V2 => {
            let declared = map["criteria"].as_arr().ok_or(Error::InvalidPolicy)?;
            let projected = declared
                .iter()
                .map(|criterion| project_criterion(criterion, &mut waivers))
                .collect::<Result<Vec<_>, _>>()?;
            Value::Arr(projected)
        }
        _ => return Err(Error::InvalidPolicy),
    };
    let digest = wire::digest(payload)?;
    let requirements = AcceptanceRequirements::from_value(&json::obj([
        ("acceptance_policy_id", map["policy_id"].clone()),
        ("acceptance_policy_version", map["policy_version"].clone()),
        ("acceptance_policy_bundle_sha", json::s(digest.as_str())),
        ("criteria", criteria),
    ]))
    .map_err(|_| Error::InvalidPolicy)?;
    if !requirements.criteria.iter().any(|criterion| {
        criterion.criterion_id == "review" && criterion.required_class == CriterionClass::Human
    }) {
        return Err(Error::InvalidPolicy);
    }
    Ok((requirements, waivers))
}

fn project_criterion(
    criterion: &Value,
    waivers: &mut BTreeMap<String, u32>,
) -> Result<Value, Error> {
    let fields = wire::exact(
        criterion,
        &[
            "criterion_id",
            "required_class",
            "origin",
            "max_waiver_seconds",
        ],
    )?;
    let id = wire::text(fields, "criterion_id")?;
    let origin = wire::text(fields, "origin")?;
    let seconds = fields["max_waiver_seconds"]
        .as_int()
        .and_then(|seconds| u32::try_from(seconds).ok())
        .filter(|seconds| *seconds <= WAIVER_MAX_SECONDS)
        .ok_or(Error::InvalidPolicy)?;
    let expected_origin = if id == "waiver-review" {
        "project"
    } else {
        "invariant"
    };
    if origin != expected_origin || (origin == "invariant" && seconds != 0) {
        return Err(Error::InvalidPolicy);
    }
    if seconds > 0 && waivers.insert(id.to_owned(), seconds).is_some() {
        return Err(Error::InvalidPolicy);
    }
    Ok(json::obj([
        ("criterion_id", fields["criterion_id"].clone()),
        ("required_class", fields["required_class"].clone()),
    ]))
}
