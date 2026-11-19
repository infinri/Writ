//! Public trust configuration for the disposable Capability Pack fixture.

use std::{
    path::Path,
    sync::{RwLock, RwLockReadGuard},
};

use ed25519_dalek::{Signature, VerifyingKey};
use writ_kernel::{
    json::{self, Digest, Value},
    time::Timestamp,
};
use writ_ledger_sqlite::EvaluatorRegistryReader;

use crate::{
    contract_fixture,
    contracts::CurrentContractApproval,
    fixture::{decode_signature, verify_fixture_ceremony},
    packs::{
        scope_value, CurrentPackPublisher, PackAuthority, PackOperation, PACK_ADMIN_PURPOSE,
        PACK_SIGNATURE_PURPOSE,
    },
    Error, Result, Scope,
};

pub const CEREMONY_DOMAIN: &str = "writ.management.fixture-pack-ceremony/1\0";
pub const PUBLISHER: &str = "fixture-pack-publisher";
pub const KEY: &str = "fixture-pack-signature";
pub const HUMAN: &str = "fixture-publisher";
pub use contract_fixture::FIXTURE_TIME;

const PACK_PUBLIC_KEY: [u8; 32] = [
    0xb5, 0xf9, 0x00, 0x95, 0x31, 0x8f, 0x3f, 0x04, 0x4c, 0xd8, 0xe4, 0x0b, 0x58, 0x42, 0x53, 0xd8,
    0x67, 0x77, 0xbe, 0x62, 0xbf, 0x07, 0x22, 0x13, 0x34, 0xe5, 0xfd, 0x21, 0x99, 0xcf, 0xa7, 0xf7,
];
const CEREMONY_PUBLIC_KEY: [u8; 32] = [
    0xc8, 0x53, 0xad, 0x0f, 0x0c, 0xd2, 0xb6, 0x19, 0xae, 0xa9, 0x2c, 0xee, 0xc4, 0xfd, 0x56, 0xa2,
    0x4d, 0x64, 0x99, 0xd5, 0x84, 0xce, 0x79, 0x25, 0x7e, 0x45, 0xcf, 0xd8, 0x13, 0x9b, 0x60, 0xa7,
];

pub fn initialize(path: &Path, evaluator: &Path) -> Result<()> {
    let identity =
        EvaluatorRegistryReader::for_pack(evaluator, false, |snapshot| snapshot.identity())
            .map_err(|_| Error::Unavailable)?
            .map_err(|_| Error::Unavailable)?;
    contract_fixture::initialize_pack_owner(path, &identity)
}

pub fn allowed_pack(id: &str) -> bool {
    matches!(id, "sample-pack" | "conflict-pack")
}

pub fn signable_payload(payload: &Value) -> bool {
    payload.get("purpose").and_then(Value::as_str) == Some(PACK_SIGNATURE_PURPOSE)
        && payload.get("scope") == Some(&scope_value(&contract_fixture::scope()))
        && payload.get("key_id").and_then(Value::as_str) == Some(KEY)
        && payload.get("manifest").is_some_and(|manifest| {
            manifest.get("publisher").and_then(Value::as_str) == Some(PUBLISHER)
                && manifest
                    .get("pack_id")
                    .and_then(Value::as_str)
                    .is_some_and(allowed_pack)
        })
}

pub fn authorizable_intent(intent: &Value) -> bool {
    intent.get("purpose").and_then(Value::as_str) == Some(PACK_ADMIN_PURPOSE)
        && intent.get("scope") == Some(&scope_value(&contract_fixture::scope()))
        && intent.get("subject").and_then(Value::as_str) == Some(HUMAN)
        && intent
            .get("pack_id")
            .and_then(Value::as_str)
            .is_some_and(allowed_pack)
        && intent
            .get("operation")
            .and_then(Value::as_str)
            .is_some_and(|op| PackOperation::change(op).is_ok())
}

#[derive(Default)]
pub struct FixturePackAuthority {
    revoked: RwLock<bool>,
}

impl FixturePackAuthority {
    pub fn revoke(&self) -> Result<()> {
        *self.revoked.write().map_err(|_| Error::Unavailable)? = true;
        Ok(())
    }
}

struct PublisherGuard<'a>(RwLockReadGuard<'a, bool>);
impl CurrentPackPublisher for PublisherGuard<'_> {
    fn still_valid(&self) -> bool {
        !*self.0
    }
}

struct Approval(Digest);
impl CurrentContractApproval for Approval {
    fn ceremony(&self) -> &Digest {
        &self.0
    }
    fn still_valid(&self) -> bool {
        true
    }
}

impl PackAuthority for FixturePackAuthority {
    fn now(&self) -> Result<Timestamp> {
        Timestamp::parse_strict(FIXTURE_TIME).ok_or(Error::Unavailable)
    }

    fn publisher(
        &self,
        scope: &Scope,
        pack_id: &str,
        publisher: &str,
        key_id: &str,
        message: &[u8],
        signature: &str,
    ) -> Result<Box<dyn CurrentPackPublisher + '_>> {
        let revoked = self.revoked.read().map_err(|_| Error::Unavailable)?;
        if *revoked
            || scope != &contract_fixture::scope()
            || !allowed_pack(pack_id)
            || publisher != PUBLISHER
            || key_id != KEY
        {
            return Err(Error::Unauthorized);
        }
        let signature =
            Signature::from_bytes(&decode_signature(signature).ok_or(Error::Unauthorized)?);
        VerifyingKey::from_bytes(&PACK_PUBLIC_KEY)
            .map_err(|_| Error::Unavailable)?
            .verify_strict(message, &signature)
            .map_err(|_| Error::Unauthorized)?;
        Ok(Box::new(PublisherGuard(revoked)))
    }

    fn approve(
        &self,
        intent: &Value,
        presentation: &str,
    ) -> Result<Box<dyn CurrentContractApproval + '_>> {
        if !authorizable_intent(intent) {
            return Err(Error::Unauthorized);
        }
        let digest = json::digest(intent).map_err(|_| Error::Unauthorized)?;
        let approval = verify_fixture_ceremony(
            presentation,
            CEREMONY_DOMAIN,
            &CEREMONY_PUBLIC_KEY,
            digest.as_str(),
        )
        .ok_or(Error::Unauthorized)?;
        let now = self.now()?;
        if approval.human != HUMAN
            || approval.issued_at > now
            || approval.issued_at.plus_seconds(60) <= now
        {
            return Err(Error::Unauthorized);
        }
        let identity = format!("{CEREMONY_DOMAIN}{}|{}", approval.human, approval.ceremony);
        Ok(Box::new(Approval(Digest::of_bytes(identity.as_bytes()))))
    }
}
