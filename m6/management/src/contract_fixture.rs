//! Disposable host configuration and verification authority for custom contracts.

use std::path::Path;

use writ_kernel::{
    json::{self, Digest, Value},
    time::Timestamp,
};

use crate::{
    contract_registry::ContractRegistry,
    contracts::{
        ContractAuthority, ContractDependency, ContractValidator, CurrentContractApproval,
        CONTRACT_PURPOSE,
    },
    fixture::{self, verify_fixture_ceremony},
    Error, Result, Scope,
};

pub const CEREMONY_DOMAIN: &str = "writ.management.fixture-custom-contract-ceremony/1\0";
pub const FIXTURE_TIME: &str = "2026-11-19T12:00:00Z";
pub const DEPENDENT_CONTRACT: &str = "tenant:m6-lab:dependent-contract";
const PUBLIC_KEY: [u8; 32] = [
    0x2b, 0xc2, 0x80, 0x0b, 0x33, 0x16, 0xe0, 0x09, 0x20, 0x9f, 0xfd, 0x75, 0x7d, 0xab, 0x19, 0xcc,
    0xf0, 0xae, 0x84, 0xbc, 0x7a, 0xe9, 0x06, 0x54, 0xe1, 0xe8, 0x17, 0x12, 0xd2, 0x70, 0xf6, 0x53,
];

struct FixtureContractAuthority;
static AUTHORITY: FixtureContractAuthority = FixtureContractAuthority;

pub fn scope() -> Scope {
    Scope {
        tenant: fixture::TENANT.into(),
        project: fixture::PROJECT.into(),
    }
}

pub fn initialize(path: &Path) -> Result<()> {
    let dependencies = [
        ("pack", "sample-pack"),
        ("policy", "sample-policy"),
        ("integration", "sample-integration"),
    ]
    .map(|(kind, owner)| ContractDependency {
        contract_id: DEPENDENT_CONTRACT.into(),
        version: "1.0.0".into(),
        kind: kind.into(),
        owner: owner.into(),
    });
    ContractRegistry::create(
        path,
        scope(),
        &["sample-pack".into()],
        Some(&dependencies),
        &AUTHORITY,
        true,
        ContractValidator::from_fixture_environment()?,
    )?;
    Ok(())
}

pub fn initialize_pack_owner(path: &Path, ledger_identity: &str) -> Result<()> {
    ContractRegistry::create_pack_owner(
        path,
        scope(),
        &["sample-pack".into(), "conflict-pack".into()],
        &AUTHORITY,
        ContractValidator::from_fixture_environment()?,
        ledger_identity,
    )?;
    Ok(())
}

pub fn open(path: &Path) -> Result<ContractRegistry<'static>> {
    ContractRegistry::open(
        path,
        scope(),
        &AUTHORITY,
        true,
        ContractValidator::from_fixture_environment()?,
    )
}

pub fn authorizable_intent(intent: &Value, subject: &str) -> bool {
    intent.get("purpose").and_then(Value::as_str) == Some(CONTRACT_PURPOSE)
        && intent.get("subject").and_then(Value::as_str) == Some(subject)
        && intent
            .get("scope")
            .and_then(|scope| scope.get("tenant"))
            .and_then(Value::as_str)
            == Some(fixture::TENANT)
        && intent
            .get("scope")
            .and_then(|scope| scope.get("project"))
            .and_then(Value::as_str)
            == Some(fixture::PROJECT)
        && matches!(
            intent.get("operation").and_then(Value::as_str),
            Some("publish" | "deprecate" | "retire")
        )
}

struct FixtureContractApproval(Digest);

impl CurrentContractApproval for FixtureContractApproval {
    fn ceremony(&self) -> &Digest {
        &self.0
    }

    fn still_valid(&self) -> bool {
        true
    }
}

impl ContractAuthority for FixtureContractAuthority {
    fn verify(
        &self,
        intent: &Value,
        presentation: &str,
    ) -> Result<Box<dyn CurrentContractApproval + '_>> {
        let digest = json::digest(intent).map_err(|_| Error::Unauthorized)?;
        let approval =
            verify_fixture_ceremony(presentation, CEREMONY_DOMAIN, &PUBLIC_KEY, digest.as_str())
                .ok_or(Error::Unauthorized)?;
        let now = self.now()?;
        if !matches!(
            approval.human,
            "fixture-publisher" | "fixture-author-publisher"
        ) || !authorizable_intent(intent, approval.human)
            || approval.issued_at > now
            || approval.issued_at.plus_seconds(60) <= now
        {
            return Err(Error::Unauthorized);
        }
        let identity = format!("{CEREMONY_DOMAIN}{}|{}", approval.human, approval.ceremony);
        Ok(Box::new(FixtureContractApproval(Digest::of_bytes(
            identity.as_bytes(),
        ))))
    }

    fn now(&self) -> Result<Timestamp> {
        Timestamp::parse_strict(FIXTURE_TIME).ok_or(Error::Unavailable)
    }
}
