use writ_kernel::{
    json::{self, Digest, Map, Value},
    time::Timestamp,
};

use crate::{Result, Scope};

pub use crate::contract_validation::ContractValidator;

pub const CONTRACT_PURPOSE: &str = "writ-human-custom-contract-admin/1";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ContractOperation {
    Browse,
    Inspect,
    ValidateSchema,
    ValidateExample,
    Create,
    Preview,
    Publish,
    Deprecate,
    Retire,
    Recover,
}

pub trait ContractBoundary {
    fn execute(
        &self,
        call: &ContractCall<'_>,
        operation: ContractOperation,
        fields: Map,
    ) -> Result<Value>;
}

#[non_exhaustive]
pub struct ContractCall<'a> {
    pub(crate) operation: ContractOperation,
    pub scope: &'a Scope,
    pub subject: &'a str,
    pub reauthorize: &'a dyn Fn() -> Result<()>,
}

pub trait ContractAuthority {
    /// Verify the exact purpose-bound intent and hold current authority through the commit.
    fn verify(
        &self,
        intent: &Value,
        presentation: &str,
    ) -> Result<Box<dyn CurrentContractApproval + '_>>;
    fn now(&self) -> Result<Timestamp>;
}

pub trait CurrentContractApproval {
    fn ceremony(&self) -> &Digest;
    fn still_valid(&self) -> bool;
}

pub struct ContractDependency {
    pub contract_id: String,
    pub version: String,
    pub kind: String,
    pub owner: String,
}

macro_rules! core_schemas {
    ($($id:literal),+ $(,)?) => {
        &[$(($id, include_bytes!(concat!(
            "../../../docs/contracts/", $id, ".schema.json"
        )))),+]
    };
}

const CORE: &[(&str, &[u8])] = core_schemas![
    "action-request",
    "approval-grant",
    "artifact",
    "audit-event",
    "broker-permit",
    "capability-pack",
    "decision",
    "effect-receipt",
    "evaluation-record",
    "evidence",
    "model-capabilities",
    "model-event",
    "model-request",
    "policy-bundle",
    "policy-manifest",
    "principal-proof",
    "task-spec",
];

pub(crate) fn core_reserved(slug: &str) -> bool {
    fn normalized(id: &str) -> impl Iterator<Item = u8> + '_ {
        id.bytes()
            .filter(u8::is_ascii_alphanumeric)
            .map(|byte| byte.to_ascii_lowercase())
    }
    CORE.iter()
        .any(|(id, _)| normalized(slug).eq(normalized(id)))
}

pub(crate) fn core_contract(id: &str) -> Option<Value> {
    let (id, schema) = CORE.iter().find(|(stored, _)| *stored == id)?;
    Some(json::obj([
        ("contract_id", json::s(id)),
        ("version", json::s("1.0.0")),
        ("status", json::s("core")),
        ("schema", json::parse(schema).ok()?),
    ]))
}

pub(crate) fn core_catalog() -> Vec<Value> {
    CORE.iter()
        .filter_map(|(id, _)| core_contract(id))
        .collect()
}
