//! Trusted dependencies and public verification material for the disposable evaluator owner.

use std::path::{Path, PathBuf};

use ed25519_dalek::{Signature, VerifyingKey};
use writ_broker::{
    clock::FixedClock,
    effects::{EffectAdapter, EffectEvidence, EffectResult, Verify},
    evaluator_publication::{
        CurrentEvaluatorPublisher, EvaluatorDependencies, EvaluatorImplementation,
        EvaluatorImplementations, EvaluatorIntent, EvaluatorPublisher, PublisherApproval,
    },
    execute::{Broker, PermitKeys},
    keys::{
        KeyStatus, KeysUnavailable, MaterialDigest, MemoryKeySource, RunnerAttestationKey,
        RunnerKeyRing,
    },
    policy::{ActivePolicy, PolicyState},
    refusal::Refusal,
    runner::{CommandRegistry, RegisteredCommand},
    store::{StoreError, TxFailure, TxStage},
    workflow::{AdminPresentation, AdminRefusal, AuthorizedOperator, SourceUnavailable},
};
use writ_kernel::{
    contracts::CriterionClass,
    ids,
    json::{Map, Value},
    time::Timestamp,
};
use writ_ledger_sqlite::{EvaluatorRegistryReader, SqliteLedger};

use crate::{
    fixture::{PROJECT, TENANT},
    lifecycle::{EvaluatorBoundary, EvaluatorOperation, LifecycleCall},
    managed_evaluator::{read_operation, ManagedEvaluator, RegistryRead},
    Error, Result, Scope,
};

pub const CEREMONY_DOMAIN: &str = "writ.management.fixture-evaluator-ceremony/1\0";
pub const FIXTURE_TIME: &str = "2026-11-01T09:00:00Z";
pub const PUBLISHER: &str = "fixture-publisher";
pub const EVALUATOR: &str = "doc-check";
const CHANNEL: &str = "fixture-evaluator-publisher";
const RUNNER_KEY: &str = "runner-key-1";
const RUNNER_MATERIAL_DIGEST: &str =
    "sha256:669078fce74f422e5be66b6df44c17a33b34d7a28ac6e60a6a7968bc059951c9";
const PUBLIC_KEY: [u8; 32] = [
    0xd7, 0x5a, 0x98, 0x01, 0x82, 0xb1, 0x0a, 0xb7, 0xd5, 0x4b, 0xfe, 0xd3, 0xc9, 0x64, 0x07, 0x3a,
    0x0e, 0xe1, 0x72, 0xf3, 0xda, 0xa6, 0x23, 0x25, 0xaf, 0x02, 0x1a, 0x68, 0xf7, 0x07, 0x51, 0x1a,
];

pub fn initialize(path: &Path) -> Result<()> {
    SqliteLedger::create_evaluator_lab(path).map_err(|_| Error::Unavailable)?;
    Ok(())
}

pub fn pack_dependencies() -> EvaluatorDependencies<'static> {
    EvaluatorDependencies {
        implementations: &Dependencies,
        commands: &Dependencies,
        runner_keys: &RunnerIdentity,
    }
}

pub fn open(path: &Path) -> Result<impl EvaluatorBoundary> {
    EvaluatorFixture::open(path)
}

struct EvaluatorFixture {
    path: PathBuf,
    scope: Scope,
    clock: FixedClock,
    active: ActivePolicy,
    authority: Authority,
    permit_keys: MemoryKeySource,
}

impl EvaluatorFixture {
    fn open(path: &Path) -> Result<Self> {
        Ok(Self {
            path: path.into(),
            scope: Scope {
                tenant: TENANT.into(),
                project: PROJECT.into(),
            },
            clock: FixedClock::new(
                Timestamp::parse_strict(FIXTURE_TIME).ok_or(Error::Unavailable)?,
            ),
            active: ActivePolicy::new(PolicyState::Absent),
            authority: Authority,
            permit_keys: MemoryKeySource::default(),
        })
    }
}

impl EvaluatorBoundary for EvaluatorFixture {
    fn execute(
        &self,
        call: &LifecycleCall<'_>,
        operation: EvaluatorOperation,
        fields: Map,
    ) -> Result<Value> {
        if call.scope != &self.scope {
            return Err(Error::NotFound);
        }
        let dependencies = pack_dependencies();
        if !matches!(
            operation,
            EvaluatorOperation::Publish | EvaluatorOperation::Revoke
        ) {
            let registry =
                EvaluatorRegistryReader::open(&self.path).map_err(|_| Error::Unavailable)?;
            return read_operation(
                RegistryRead::ReadOnly(&registry),
                &dependencies,
                call,
                operation,
                fields,
            );
        }
        let ledger =
            SqliteLedger::open_evaluator_lab(&self.path).map_err(|_| Error::Unavailable)?;
        let broker = Broker::with_permit_keys(
            &self.active,
            &self.clock,
            &ledger,
            &ClosedEffects,
            PermitKeys::Keyed {
                source: &self.permit_keys,
                revocations: &ledger,
            },
        );
        let service = ManagedEvaluator::new(
            &self.scope,
            &ledger,
            &broker,
            &self.authority,
            dependencies,
            CHANNEL,
        )?;
        service.execute(call, operation, fields)
    }
}

struct Authority;

impl EvaluatorPublisher for Authority {
    fn current(&self) -> std::result::Result<impl CurrentEvaluatorPublisher + '_, AdminRefusal> {
        Ok(self)
    }
}

impl CurrentEvaluatorPublisher for &Authority {
    fn approve(
        &self,
        intent: &EvaluatorIntent,
        presentation: &AdminPresentation<'_>,
        _now: Timestamp,
    ) -> std::result::Result<PublisherApproval, AdminRefusal> {
        let refused = AdminRefusal::Unauthenticated;
        if presentation.channel != CHANNEL || presentation.operator != PUBLISHER {
            return Err(AdminRefusal::Unauthorized);
        }
        let document = intent.as_value();
        let mutation = document.get("mutation").ok_or(refused)?;
        let scope = if document.get("operation").and_then(Value::as_str) == Some("publish") {
            mutation.get("scope").ok_or(refused)?
        } else {
            mutation
        };
        if scope.get("tenant_id").and_then(Value::as_str) != Some(TENANT)
            || scope.get("project_id").and_then(Value::as_str) != Some(PROJECT)
        {
            return Err(AdminRefusal::Unauthorized);
        }
        let raw = std::str::from_utf8(presentation.credential).map_err(|_| refused)?;
        let (claim, signature) = raw.rsplit_once('|').ok_or(refused)?;
        let mut fields = claim.split('|');
        let human = fields.next().ok_or(refused)?;
        let ceremony = fields
            .next()
            .filter(|value| ids::is_id(value))
            .ok_or(refused)?;
        let issued_at = fields
            .next()
            .and_then(Timestamp::parse_strict)
            .ok_or(refused)?;
        let digest = fields.next().ok_or(refused)?;
        if fields.next().is_some() || human != PUBLISHER || digest != intent.fingerprint() {
            return Err(refused);
        }
        let verifier = VerifyingKey::from_bytes(&PUBLIC_KEY).map_err(|_| refused)?;
        let signed = format!("{CEREMONY_DOMAIN}{claim}");
        verifier
            .verify_strict(
                signed.as_bytes(),
                &signature_bytes(signature).ok_or(refused)?,
            )
            .map_err(|_| refused)?;
        Ok(PublisherApproval {
            operator: AuthorizedOperator {
                id: human.into(),
                issuer: "fixture-evaluator-authority".into(),
                channel: CHANNEL.into(),
                evidence_ref: ceremony.into(),
            },
            ceremony: ceremony.into(),
            issued_at,
            expires_at: issued_at.plus_seconds(60),
        })
    }
}

fn signature_bytes(text: &str) -> Option<Signature> {
    let (pairs, remaining) = text.as_bytes().as_chunks::<2>();
    if pairs.len() != 64 || !remaining.is_empty() {
        return None;
    }
    let mut output = [0; 64];
    for (byte, pair) in output.iter_mut().zip(pairs) {
        *byte = u8::from_str_radix(std::str::from_utf8(pair).ok()?, 16).ok()?;
    }
    Some(Signature::from_bytes(&output))
}

struct RunnerIdentity;

impl RunnerKeyRing for RunnerIdentity {
    fn runner_key(
        &self,
        _: &str,
    ) -> std::result::Result<KeyStatus<'_, RunnerAttestationKey>, KeysUnavailable> {
        Ok(KeyStatus::Unknown)
    }

    fn runner_key_material_digest(
        &self,
        key_id: &str,
    ) -> std::result::Result<Option<MaterialDigest>, KeysUnavailable> {
        if key_id == RUNNER_KEY {
            MaterialDigest::parse(RUNNER_MATERIAL_DIGEST)
                .map(Some)
                .ok_or(KeysUnavailable)
        } else {
            Ok(None)
        }
    }
}

struct Dependencies;

impl EvaluatorImplementations for Dependencies {
    fn get(
        &self,
        tenant: &str,
        project: &str,
        evaluator: &str,
        version: &str,
    ) -> std::result::Result<Option<EvaluatorImplementation>, SourceUnavailable> {
        Ok(
            ((tenant, project, evaluator, version) == (TENANT, PROJECT, EVALUATOR, "1.4.0")).then(
                || EvaluatorImplementation {
                    class: CriterionClass::Evidence,
                    command: None,
                    implementation_digest: format!("sha256:{}", "1".repeat(64)),
                    environment_digest: format!("sha256:{}", "2".repeat(64)),
                    attestation_key_id: RUNNER_KEY.into(),
                },
            ),
        )
    }
}

impl CommandRegistry for Dependencies {
    fn command(
        &self,
        _: &str,
        _: &str,
        _: &str,
    ) -> std::result::Result<Option<RegisteredCommand>, SourceUnavailable> {
        Ok(None)
    }
}

struct ClosedEffects;

impl EffectAdapter for ClosedEffects {
    fn apply(&self, _: &mut Verify<'_>) -> std::result::Result<ResultEffect, TxFailure> {
        Err(effects_unavailable())
    }

    fn inspect(&self, _: &mut Verify<'_>) -> std::result::Result<ResultEvidence, TxFailure> {
        Err(effects_unavailable())
    }

    fn evidence(&self, _: &str) -> std::result::Result<Option<EffectEvidence>, TxFailure> {
        Err(effects_unavailable())
    }
}

type ResultEffect = std::result::Result<EffectResult, Refusal>;
type ResultEvidence = std::result::Result<Option<EffectEvidence>, Refusal>;

fn effects_unavailable() -> TxFailure {
    TxFailure::new(
        TxStage::Begin,
        StoreError::Unavailable {
            category: "evaluator fixture effects".into(),
            code: "DISABLED".into(),
        },
    )
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    #[test]
    fn fixture_runner_registry_has_identity_without_attestation_material() {
        assert!(RunnerIdentity
            .runner_key_material_digest(RUNNER_KEY)
            .unwrap()
            .is_some());
        assert!(matches!(
            RunnerIdentity.runner_key(RUNNER_KEY).unwrap(),
            KeyStatus::Unknown
        ));
    }
}
