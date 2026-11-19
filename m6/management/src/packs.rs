use std::collections::BTreeSet;

use writ_broker::{
    acceptance::AcceptanceCode,
    evaluator_publication::EvaluatorDependencies,
    evaluator_rubric::{RubricBinding, RubricEligibility},
    refusal::Refusal,
};
use writ_kernel::{
    ids,
    json::{self, Digest, Map, Value},
    time::Timestamp,
};
use writ_ledger_sqlite::EvaluatorPackSnapshot;

use crate::{contracts::CurrentContractApproval, Error, LifecycleCall, Result, Scope};

pub const PACK_SIGNATURE_PURPOSE: &str = "writ-capability-pack-signature/1";
pub const PACK_ADMIN_PURPOSE: &str = "writ-human-capability-pack-admin/1";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PackOperation {
    Install,
    Verify,
    Inspect,
    Enable,
    Disable,
    Upgrade,
    History,
    Preview,
    Recover,
}

impl PackOperation {
    pub(crate) fn change(text: &str) -> Result<Self> {
        match text {
            "install" => Ok(Self::Install),
            "enable" => Ok(Self::Enable),
            "disable" => Ok(Self::Disable),
            "upgrade" => Ok(Self::Upgrade),
            _ => Err(Error::InvalidRequest),
        }
    }

    pub(crate) fn name(self) -> &'static str {
        match self {
            Self::Install => "install",
            Self::Verify => "verify",
            Self::Inspect => "inspect",
            Self::Enable => "enable",
            Self::Disable => "disable",
            Self::Upgrade => "upgrade",
            Self::History => "history",
            Self::Preview => "preview",
            Self::Recover => "recover",
        }
    }

    pub(crate) fn destructive(self) -> bool {
        matches!(self, Self::Disable | Self::Upgrade)
    }

    pub(crate) fn read_only(self) -> bool {
        matches!(
            self,
            Self::Verify | Self::Inspect | Self::History | Self::Preview | Self::Recover
        )
    }
}

pub trait PackBoundary {
    fn execute(
        &self,
        call: &LifecycleCall<'_>,
        operation: PackOperation,
        fields: Map,
    ) -> Result<Value>;
}

/// Trusted host configuration. The guards must freeze revocation until dropped,
/// and recheck validity intervals using the authority's current clock.
pub trait PackAuthority {
    fn now(&self) -> Result<Timestamp>;
    fn publisher(
        &self,
        scope: &Scope,
        pack_id: &str,
        publisher: &str,
        key_id: &str,
        message: &[u8],
        signature: &str,
    ) -> Result<Box<dyn CurrentPackPublisher + '_>>;
    fn approve(
        &self,
        intent: &Value,
        presentation: &str,
    ) -> Result<Box<dyn CurrentContractApproval + '_>>;
}

pub trait CurrentPackPublisher {
    fn still_valid(&self) -> bool;
}

pub fn pack_signature_message(payload: &Value) -> Result<Vec<u8>> {
    let canonical = json::canonical(payload).map_err(|_| Error::InvalidPack)?;
    let message = format!("{PACK_SIGNATURE_PURPOSE}\0{canonical}");
    Ok(message.into_bytes())
}

pub(crate) fn scope_value(scope: &Scope) -> Value {
    json::obj([
        ("tenant", json::s(&scope.tenant)),
        ("project", json::s(&scope.project)),
    ])
}

pub(crate) fn text(value: &Value, key: &str) -> Result<String> {
    value
        .get(key)
        .and_then(Value::as_str)
        .map(str::to_owned)
        .ok_or(Error::InvalidPack)
}

pub(crate) fn pack_id(id: &str) -> bool {
    (2..=64).contains(&id.len())
        && id.bytes().enumerate().all(|(index, byte)| {
            byte.is_ascii_lowercase()
                || byte.is_ascii_digit()
                || (index > 0 && b"._-".contains(&byte))
        })
}

pub(crate) fn version(version: &str) -> Option<[u32; 3]> {
    let parts = version
        .split('.')
        .map(|part| {
            if part.len() > 1 && part.starts_with('0') || !part.bytes().all(|b| b.is_ascii_digit())
            {
                return None;
            }
            part.parse::<u32>().ok()
        })
        .collect::<Option<Vec<_>>>()?;
    parts.try_into().ok()
}

fn closed(value: &Value, fields: &[&str]) -> Result<()> {
    if value
        .as_obj()
        .is_some_and(|obj| obj.keys().all(|key| fields.contains(&key.as_str())))
    {
        Ok(())
    } else {
        Err(Error::Unsupported)
    }
}

fn items<'a>(value: &'a Value, field: &str) -> Result<&'a [Value]> {
    value
        .get(field)
        .and_then(Value::as_arr)
        .ok_or(Error::InvalidPack)
}

pub(crate) struct ContractRef {
    pub id: String,
    pub version: String,
    pub digest: String,
}

pub(crate) struct Envelope {
    pub value: Value,
    pub manifest: Value,
    pub contracts: Vec<ContractRef>,
}

impl Envelope {
    pub(crate) fn parse(
        value: Value,
        scope: &Scope,
        id: &str,
        target_version: &str,
    ) -> Result<Self> {
        closed(&value, &["payload", "signature"])?;
        let payload = value.get("payload").ok_or(Error::InvalidPack)?;
        closed(
            payload,
            &[
                "purpose",
                "scope",
                "compatibility",
                "key_id",
                "manifest",
                "contracts",
                "rubrics",
            ],
        )?;
        if text(payload, "purpose")? != PACK_SIGNATURE_PURPOSE {
            return Err(Error::Unauthorized);
        }
        if payload.get("scope") != Some(&scope_value(scope)) {
            return Err(Error::Unauthorized);
        }
        if text(payload, "compatibility")? != "2.0" {
            return Err(Error::Unsupported);
        }
        let manifest = payload.get("manifest").ok_or(Error::InvalidPack)?.clone();
        if text(&manifest, "pack_id")? != id
            || text(&manifest, "version")? != target_version
            || text(&manifest, "signature_ref")? != text(payload, "key_id")?
        {
            return Err(Error::InvalidPack);
        }
        let mut seen = BTreeSet::new();
        let mut contract_ids = BTreeSet::new();
        let contracts = items(payload, "contracts")?
            .iter()
            .map(|contract| {
                closed(contract, &["id", "version", "digest"])?;
                let contract = ContractRef {
                    id: text(contract, "id")?,
                    version: text(contract, "version")?,
                    digest: text(contract, "digest")?,
                };
                if !ids::is_digest(&contract.digest)
                    || version(&contract.version).is_none()
                    || !contract_ids.insert(contract.id.clone())
                    || !seen.insert(format!("{}@{}", contract.id, contract.version))
                {
                    return Err(Error::InvalidPack);
                }
                Ok(contract)
            })
            .collect::<Result<Vec<_>>>()?;
        let mut artifact_refs = BTreeSet::new();
        let mut artifact_types = BTreeSet::new();
        for artifact in items(&manifest, "artifacts")? {
            closed(artifact, &["artifact_type", "schema_ref", "format"])?;
            if artifact
                .get("format")
                .is_some_and(|format| format.as_str() != Some("json"))
            {
                return Err(Error::Unsupported);
            }
            artifact_refs.insert(text(artifact, "schema_ref")?);
            if !artifact_types.insert(text(artifact, "artifact_type")?) {
                return Err(Error::InvalidPack);
            }
        }
        if seen.is_empty() || seen.len() > 16 || seen != artifact_refs {
            return Err(Error::InvalidPack);
        }
        requirements(&manifest)?;
        Ok(Self {
            value,
            manifest,
            contracts,
        })
    }

    pub(crate) fn verify_publisher<'a>(
        &self,
        authority: &'a dyn PackAuthority,
        scope: &Scope,
        id: &str,
    ) -> Result<Box<dyn CurrentPackPublisher + 'a>> {
        let payload = self.value.get("payload").ok_or(Error::InvalidPack)?;
        authority.publisher(
            scope,
            id,
            &text(&self.manifest, "publisher")?,
            &text(payload, "key_id")?,
            &pack_signature_message(payload)?,
            &text(&self.value, "signature")?,
        )
    }

    pub(crate) fn verify_rubrics(
        &self,
        snapshot: &EvaluatorPackSnapshot<'_>,
        scope: &Scope,
        dependencies: &EvaluatorDependencies<'_>,
    ) -> Result<()> {
        let payload = self.value.get("payload").ok_or(Error::InvalidPack)?;
        let rubrics = items(payload, "rubrics")?;
        let evaluators = items(&self.manifest, "evaluators")?;
        if rubrics.len() != evaluators.len() || rubrics.len() > 16 {
            return Err(Error::InvalidPack);
        }
        let mut seen = BTreeSet::new();
        let mut rubric_ids = BTreeSet::new();
        for rubric in rubrics {
            closed(rubric, &["binding", "entry_digest"])?;
            let binding = rubric.get("binding").ok_or(Error::InvalidPack)?;
            let validator = RubricBinding::validate(binding).map_err(|_| Error::InvalidPack)?;
            let evaluator_id = text(binding, "evaluator_id")?;
            let rubric_version = text(binding, "rubric_version")?;
            if !seen.insert((evaluator_id.clone(), rubric_version.clone()))
                || !rubric_ids.insert(text(binding, "rubric_id")?)
                || binding.get("pack_id") != self.manifest.get("pack_id")
                || binding.get("pack_version") != self.manifest.get("version")
            {
                return Err(Error::InvalidPack);
            }
            let mut matched = evaluators.iter().filter(|entry| {
                entry.get("evaluator_id") == binding.get("evaluator_id")
                    && entry.get("rubric_version") == binding.get("rubric_version")
                    && entry.get("kind") == binding.get("class")
            });
            let declaration = matched.next().ok_or(Error::InvalidPack)?;
            if matched.next().is_some() {
                return Err(Error::InvalidPack);
            }
            let current = snapshot
                .entry(&scope.tenant, &scope.project, &evaluator_id)
                .map_err(|_| Error::Unavailable)?
                .ok_or(Error::InvalidPack)?;
            if current.key_revoked
                || json::digest(current.entry.as_value())
                    .map_err(|_| Error::Unavailable)?
                    .as_str()
                    != text(rubric, "entry_digest")?
            {
                return Err(Error::InvalidPack);
            }
            let key = dependencies
                .check_entry(&current.entry)
                .map_err(|error| match error {
                    Refusal::Acceptance(
                        AcceptanceCode::RegistryUnavailable
                        | AcceptanceCode::AttestationKeyUnavailable,
                    ) => Error::Unavailable,
                    _ => Error::InvalidPack,
                })?;
            if key != (current.key_id, current.key_digest) {
                return Err(Error::InvalidPack);
            }
            let eligibility = validator
                .check(&scope.tenant, &scope.project, |_, _, _| {
                    Ok(Some(current.entry))
                })
                .map_err(|_| Error::InvalidPack)?;
            if eligibility == RubricEligibility::AdvisoryOnly
                && declaration.get("blocking").and_then(Value::as_bool) == Some(true)
            {
                return Err(Error::InvalidPack);
            }
        }
        Ok(())
    }

    pub(crate) fn conflicts(&self, other: &Self) -> Result<bool> {
        for (field, nested) in [
            ("task_types", None),
            ("artifacts", Some("artifact_type")),
            ("workflows", Some("workflow_id")),
        ] {
            let right = items(&other.manifest, field)?;
            if items(&self.manifest, field)?.iter().any(|left| {
                right.iter().any(|right| {
                    nested.map_or(left, |key| left.get(key).unwrap_or(left))
                        == nested.map_or(right, |key| right.get(key).unwrap_or(right))
                })
            }) {
                return Ok(true);
            }
        }
        Ok(self.contracts.iter().any(|left| {
            other.contracts.iter().any(|right| {
                left.id == right.id
                    && (left.version != right.version || left.digest != right.digest)
            })
        }))
    }

    pub(crate) fn preserves_gates(&self, previous: &Self) -> Result<bool> {
        let next = items(&self.manifest, "workflows")?;
        for workflow in items(&previous.manifest, "workflows")? {
            let Some(gates) = workflow.get("human_gates").and_then(Value::as_arr) else {
                continue;
            };
            if !gates.is_empty()
                && !next.iter().any(|candidate| {
                    ["workflow_id", "task_type"]
                        .iter()
                        .all(|field| candidate.get(field) == workflow.get(field))
                        && candidate
                            .get("human_gates")
                            .and_then(Value::as_arr)
                            .is_some_and(|new| gates.iter().all(|gate| new.contains(gate)))
                })
            {
                return Ok(false);
            }
        }
        // This profile has no mandatory evaluator evidence semantics.
        Ok(true)
    }
}

fn requirements(manifest: &Value) -> Result<()> {
    let tasks = items(manifest, "task_types")?;
    let mut workflow_ids = BTreeSet::new();
    let mut task_coverage = BTreeSet::new();
    for workflow in items(manifest, "workflows")? {
        closed(
            workflow,
            &["workflow_id", "task_type", "steps", "human_gates"],
        )?;
        let id = text(workflow, "workflow_id")?;
        let task = text(workflow, "task_type")?;
        if !workflow_ids.insert(id)
            || !tasks.contains(&json::s(&task))
            || !items(workflow, "steps")?
                .iter()
                .all(|step| matches!(step.as_str(), Some("review" | "record" | "request-signoff")))
            || workflow
                .get("human_gates")
                .and_then(Value::as_arr)
                .is_some_and(|gates| {
                    gates
                        .iter()
                        .any(|gate| gate.as_str() != Some("review-signoff"))
                })
        {
            return Err(Error::Unsupported);
        }
        task_coverage.insert(task);
    }
    let memory = manifest.get("memory").ok_or(Error::InvalidPack)?;
    let limits = manifest.get("limits").ok_or(Error::InvalidPack)?;
    closed(memory, &["read_types", "write_types", "retention_class"])?;
    closed(
        limits,
        &[
            "max_tool_calls",
            "max_duration_seconds",
            "max_model_calls",
            "max_cost_usd",
        ],
    )?;
    for evaluator in items(manifest, "evaluators")? {
        closed(
            evaluator,
            &["evaluator_id", "kind", "rubric_version", "blocking"],
        )?;
        if evaluator.get("blocking").and_then(Value::as_bool) == Some(true) {
            return Err(Error::Unsupported);
        }
    }
    if tasks.len() != task_coverage.len()
        || !items(manifest, "tool_capabilities")?
            .iter()
            .all(|tool| tool.as_str() == Some("fs.read"))
        || !items(memory, "read_types")?.is_empty()
        || !items(memory, "write_types")?.is_empty()
        || text(memory, "retention_class")? != "ephemeral"
        || manifest
            .get("supported_modes")
            .and_then(Value::as_arr)
            .is_some_and(|modes| {
                modes
                    .iter()
                    .any(|mode| mode.as_str() != Some("external_host"))
            })
        || manifest
            .get("required_model_features")
            .and_then(Value::as_arr)
            .is_some_and(|features| !features.is_empty())
        || limits
            .get("max_model_calls")
            .is_some_and(|calls| calls.as_int() != Some(0))
        || limits
            .get("max_cost_usd")
            .is_some_and(|cost| *cost != Value::Null && cost.as_int() != Some(0))
    {
        return Err(Error::Unsupported);
    }
    Ok(())
}

pub(crate) fn digest(value: &Value) -> Result<Digest> {
    json::digest(value).map_err(|_| Error::InvalidRequest)
}
