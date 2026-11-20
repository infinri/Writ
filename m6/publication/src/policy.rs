use std::collections::BTreeMap;

use writ_kernel::{
    contracts::{AcceptanceRequirements, CriterionClass},
    json::{self, Value},
    policy::{self, LoadedBundle},
};

use writ_broker::{quorum_policy::QuorumRequirements, waiver::WAIVER_MAX_SECONDS};

use crate::{
    signature::{ACCEPTANCE_V2, ACCEPTANCE_V3},
    wire, Error, Operation, Publication, Scope,
};

const ACCEPTANCE_ID: &str = "m6-acceptance";
const ACTION: &str = "synthetic.inspect";

pub(crate) struct Policies {
    pub(crate) authorization: LoadedBundle,
    pub(crate) acceptance: QuorumRequirements,
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
    let document = &publication.prerequisites;
    let prereq = if let Some(removal) = publication.removal() {
        let prereq = wire::exact(document, &["version", "draft", "proof_digest", "removal"])?;
        let binding = wire::exact(removal, &["policy_id", "source_publication_digest"])?;
        if wire::text(prereq, "version")? != "writ-policy-prerequisites/2"
            || wire::text(binding, "policy_id")? != writ_policy_compile::OPTIONAL_INSPECT_POLICY
        {
            return Err(Error::InvalidPolicy);
        }
        wire::digest_text(wire::text(binding, "source_publication_digest")?)?;
        prereq
    } else {
        let prereq = wire::exact(document, &["version", "draft", "proof_digest"])?;
        if wire::text(prereq, "version")? != "writ-policy-prerequisites/1" {
            return Err(Error::Format);
        }
        prereq
    };
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
    if required
        .quorums()
        .iter()
        .any(|(id, eligible)| candidate.acceptance.quorums().get(id) != Some(eligible))
    {
        return Err(Error::InvalidPolicy);
    }
    let mut candidate = candidate.acceptance.criteria().iter();
    if required
        .criteria()
        .iter()
        .all(|required| candidate.any(|criterion| criterion == required))
    {
        Ok(())
    } else {
        Err(Error::InvalidPolicy)
    }
}

pub(crate) fn check_selection(
    operation: Operation,
    publication: &Publication,
    selected: Option<(i64, &Publication)>,
) -> Result<Option<Value>, Error> {
    let removal = publication.removal();
    if operation == Operation::Rollback && removal.is_some() {
        return Err(Error::InvalidPolicy);
    }
    let Some((_, current)) = selected else {
        return if removal.is_some() {
            Err(Error::InvalidPolicy)
        } else {
            Ok(None)
        };
    };
    let draft = current
        .prerequisites
        .get("draft")
        .ok_or(Error::InvalidPolicy)?;
    if let Some(binding) = removal.filter(|_| operation == Operation::Publish) {
        let (replacement, decisions) =
            writ_policy_compile::optional_inspect_removal(draft).ok_or(Error::InvalidPolicy)?;
        if publication.prerequisites.get("draft") != Some(&replacement)
            || binding
                .get("source_publication_digest")
                .and_then(Value::as_str)
                != Some(current.fingerprint()?.as_str())
            || current.acceptance.payload.without("policy_version")
                != publication.acceptance.payload.without("policy_version")
        {
            return Err(Error::InvalidPolicy);
        }
        return Ok(Some(decisions));
    }
    let has_optional = draft
        .get("manifests")
        .and_then(Value::as_arr)
        .ok_or(Error::InvalidPolicy)?
        .iter()
        .any(|manifest| {
            manifest.get("policy_id").and_then(Value::as_str)
                == Some(writ_policy_compile::OPTIONAL_INSPECT_POLICY)
        });
    if has_optional || current.removal().is_some() {
        let next = publication
            .prerequisites
            .get("draft")
            .ok_or(Error::InvalidPolicy)?;
        if (has_optional && next != draft)
            || (current.removal().is_some()
                && !writ_policy_compile::optional_inspect_removal(next)
                    .is_some_and(|(base, _)| &base == draft))
        {
            return Err(Error::InvalidPolicy);
        }
    }
    Ok(None)
}

pub(crate) fn refuse_deactivation(
    publication: &Publication,
    acceptance: &QuorumRequirements,
    policy_id: &str,
) -> Result<(), Error> {
    if policy_id == ACCEPTANCE_ID
        || acceptance
            .criteria()
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
) -> Result<(QuorumRequirements, BTreeMap<String, u32>), Error> {
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
    let mut quorums = BTreeMap::new();
    let criteria = match wire::text(map, "version")? {
        "writ-acceptance-policy/1" => map["criteria"].clone(),
        ACCEPTANCE_V3 => {
            let declared = map["criteria"]
                .as_arr()
                .filter(|c| !c.is_empty() && c.len() <= 64)
                .ok_or(Error::InvalidPolicy)?;
            let mut projected = Vec::new();
            for criterion in declared {
                let rule = criterion.get("quorum").ok_or(Error::InvalidPolicy)?;
                projected.push(project_criterion(
                    &criterion.without("quorum"),
                    &mut waivers,
                )?);
                if rule == &Value::Null {
                    continue;
                }
                if !matches!(
                    criterion.get("required_class").and_then(Value::as_str),
                    Some("mechanical" | "evidence")
                ) || criterion.get("max_waiver_seconds").and_then(Value::as_int) != Some(0)
                {
                    return Err(Error::InvalidPolicy);
                }
                let members = wire::exact(rule, &["eligible"])?["eligible"]
                    .as_arr()
                    .filter(|m| (2..=16).contains(&m.len()))
                    .ok_or(Error::InvalidPolicy)?;
                let mut eligible = BTreeMap::new();
                for member in members {
                    let fields = wire::exact(member, &["evaluator_id", "independence_group"])?;
                    let id = wire::text(fields, "evaluator_id")?;
                    let group = wire::text(fields, "independence_group")?;
                    if !writ_kernel::ids::is_name(id)
                        || !writ_kernel::ids::is_name(group)
                        || eligible.insert(id.to_owned(), group.to_owned()).is_some()
                    {
                        return Err(Error::InvalidPolicy);
                    }
                }
                if eligible
                    .values()
                    .collect::<std::collections::BTreeSet<_>>()
                    .len()
                    < 2
                {
                    return Err(Error::InvalidPolicy);
                }
                let id = criterion
                    .get("criterion_id")
                    .and_then(Value::as_str)
                    .ok_or(Error::InvalidPolicy)?;
                quorums.insert(id.to_owned(), eligible);
            }
            if quorums.len() > 16 {
                return Err(Error::InvalidPolicy);
            }
            Value::Arr(projected)
        }
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
    Ok((QuorumRequirements::new(requirements, quorums), waivers))
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::SignedPolicy;

    #[test]
    fn historical_removal_is_not_a_rollback_target() {
        let policy = SignedPolicy {
            payload: Value::Null,
            envelope: Value::Null,
        };
        let source = Publication {
            authorization: policy.clone(),
            acceptance: policy,
            prerequisites: json::obj([
                ("version", json::s("writ-policy-prerequisites/1")),
                ("draft", json::obj([("manifests", Value::Arr(vec![]))])),
                ("proof_digest", Value::Null),
            ]),
        };
        let removal = source.clone().bind_optional_removal(&source);
        assert!(matches!(
            removal.and_then(|removal| check_selection(
                Operation::Rollback,
                &removal,
                Some((3, &source))
            )),
            Err(Error::InvalidPolicy)
        ));
    }
}
