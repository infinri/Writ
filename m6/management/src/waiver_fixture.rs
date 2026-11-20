//! Host composition for the opt-in waiver fixture. The authority binary holds the signer.

use std::{
    fs::{self, File},
    io::Read,
    path::{Path, PathBuf},
    sync::Arc,
};

use ed25519_dalek::{Signature, VerifyingKey};
use writ_broker::{
    clock::{ClockError, EffectiveClock, FixedClock, Reading},
    execute::{Broker, PermitKeys},
    human::{
        ActiveCredentials, ApprovalChannel, CeremonyStart, CeremonyState, HumanVerifier,
        RegistrationBinding, RegistrationStart, RegistrationVerifier, RelyingParty, SubjectId,
        VerifiedAssertion, VerifiedRegistration, VerifierCapability, VerifierError, ES256,
        WEBAUTHN_PASSKEY,
    },
    ingress::{verify_disposable_waiver_transition, WaiverTransition},
    keys::MemoryKeySource,
    policy::{ActivePolicy, PolicyState},
    workflow::{
        AdminOperation, AdminPresentation, AdminRefusal, AdminRequest, AuthorizedOperator,
        SnapshotCheck, SnapshotStore, SourceUnavailable, WorkflowAuthority, WorkflowKey,
    },
};
use writ_kernel::{
    json::{self, Digest, Map, Value},
    policy::load_bundle,
};
use writ_ledger_sqlite::{SqliteEffectStore, SqliteLedger};
use writ_policy_publication_lab::{Registry, Selection};

use crate::{
    fixture,
    lifecycle::{LifecycleCall, WaiverBoundary, WaiverOperation},
    managed_waiver::{execute_history, ManagedWaiver, WaiverTransitions},
    Error, Result, Scope,
};

pub const HUMAN_ISSUER: &str = "fixture-waiver-human";
pub const APPROVER: &str = "fixture-waiver-approver";
pub const OPERATOR: &str = "fixture-waiver-setup-operator";
pub const ASSERTION_DOMAIN: &[u8] = b"writ.management.fixture-waiver/1\0";
pub const INGRESS_ADAPTER: &str = "m6-synthetic";
pub const REQUESTER: &str = "fixture-waiver-requester";
pub const TRANSITION_REFERENCE: &str = "fixture-transition";
pub const ARTIFACT: &[u8] = b"immutable synthetic artifact";
const SETUP_PROOF: &[u8] = b"disposable-waiver-workflow-bootstrap";
const APPROVER_KEY: [u8; 32] = [
    0xe2, 0xfe, 0x2a, 0x39, 0xb7, 0x32, 0x66, 0x42, 0x82, 0x7d, 0x6d, 0x82, 0xd4, 0x97, 0x62, 0x2c,
    0xd6, 0xc2, 0x97, 0xfe, 0x78, 0x27, 0x0e, 0xaa, 0x43, 0x6b, 0x01, 0xf0, 0x86, 0xb6, 0xf3, 0xcb,
];
const OPERATOR_KEY: [u8; 32] = [
    0x5b, 0x64, 0x89, 0xc9, 0xc7, 0xfd, 0x0d, 0xcf, 0x50, 0x54, 0x5e, 0x7c, 0x16, 0x48, 0x86, 0xef,
    0x40, 0x49, 0x1e, 0xc0, 0x6c, 0x7f, 0x1b, 0x12, 0x30, 0x41, 0x79, 0x7e, 0x81, 0x17, 0x53, 0x5e,
];

pub struct WaiverFixture {
    ledger: SqliteLedger,
    ledger_path: PathBuf,
    policy_registry_path: PathBuf,
}

impl WaiverFixture {
    pub fn open(ledger_path: &Path, policy_registry_path: &Path) -> Result<Self> {
        let ledger = SqliteLedger::open_waiver_lab(ledger_path)
            .map_err(|_| Error::Unavailable)?
            .with_approval_channel(channel()?)
            .map_err(|_| Error::Unavailable)?;
        Ok(Self {
            ledger,
            ledger_path: ledger_path.to_owned(),
            policy_registry_path: policy_registry_path.to_owned(),
        })
    }
}

impl WaiverBoundary for WaiverFixture {
    fn execute(
        &self,
        call: &LifecycleCall<'_>,
        operation: WaiverOperation,
        fields: Map,
    ) -> Result<Value> {
        if let Some(history) = execute_history(&self.ledger, call, operation, &fields) {
            return history;
        }
        let effects =
            SqliteEffectStore::open(&effects_path(&self.ledger_path), self.ledger.ledger_id())
                .map_err(|_| Error::Unavailable)?;
        let ingress = FixtureIngress::open(&self.ledger_path)?;
        open_registry(&self.policy_registry_path)?
            .with_selection(|selected| {
                let result = (|| {
                    let clock = FixtureClock;
                    let active = local_policy(&selected)?;
                    let keys = MemoryKeySource::default();
                    let broker = Broker::with_permit_keys(
                        &active,
                        &clock,
                        &self.ledger,
                        &effects,
                        PermitKeys::Keyed {
                            source: &keys,
                            revocations: &self.ledger,
                        },
                    )
                    .with_waiver_authority(&selected, &Snapshot);
                    ManagedWaiver::new(&self.ledger, &broker, &ingress)?
                        .execute(call, operation, fields)
                })();
                Ok(result)
            })
            .map_err(|_| Error::Unavailable)?
    }
}

/// Provision the synthetic workflow on the already bootstrapped ledger.
pub fn initialize_workflow(ledger_path: &Path, policy_registry_path: &Path) -> Result<()> {
    let ledger = SqliteLedger::open_waiver_lab(ledger_path).map_err(|_| Error::Unavailable)?;
    let effects = SqliteEffectStore::create(&effects_path(ledger_path), ledger.ledger_id())
        .map_err(|_| Error::Unavailable)?;
    open_registry(policy_registry_path)?
        .with_selection(|selected| {
            let result = (|| {
                let active = local_policy(&selected)?;
                let clock = FixtureClock;
                let keys = MemoryKeySource::default();
                let broker = Broker::with_permit_keys(
                    &active,
                    &clock,
                    &ledger,
                    &effects,
                    PermitKeys::Keyed {
                        source: &keys,
                        revocations: &ledger,
                    },
                );
                let upload = selected
                    .publication()
                    .acceptance
                    .payload
                    .get("criteria")
                    .and_then(Value::as_arr)
                    .ok_or(Error::Unavailable)?;
                let requirements = selected.acceptance().map_err(|_| Error::Unavailable)?;
                let requirements = json::obj([
                    (
                        "acceptance_policy_id",
                        json::s(&requirements.policy.acceptance_policy_id),
                    ),
                    (
                        "acceptance_policy_version",
                        json::s(&requirements.policy.acceptance_policy_version),
                    ),
                    (
                        "acceptance_policy_bundle_sha",
                        json::s(&requirements.policy.acceptance_policy_bundle_sha),
                    ),
                    (
                        "criteria",
                        Value::Arr(
                            upload
                                .iter()
                                .map(|rule| rule.without("origin").without("max_waiver_seconds"))
                                .collect(),
                        ),
                    ),
                ]);
                let presentation = AdminPresentation {
                    operator: OPERATOR,
                    credential: SETUP_PROOF,
                    channel: "disposable-setup",
                };
                let activate = AdminRequest::ActivateAcceptancePolicy {
                    tenant_id: fixture::TENANT.into(),
                    project_id: fixture::PROJECT.into(),
                    requirements,
                    effect_prerequisites: vec![],
                };
                let create = AdminRequest::CreateWorkflow {
                    key: WorkflowKey {
                        tenant_id: fixture::TENANT.into(),
                        project_id: fixture::PROJECT.into(),
                        workflow_id: "workflow".into(),
                    },
                    task_id: "task".into(),
                    initial_state: "verifying".into(),
                    artifact_snapshot_id: "snapshot".into(),
                    artifact_digest: Digest::of_bytes(ARTIFACT).to_string(),
                };
                for request in [activate, create] {
                    broker
                        .administer(&SetupOperator, &presentation, &Snapshot, request)
                        .map_err(|_| Error::Unavailable)?;
                }
                Ok(())
            })();
            Ok(result)
        })
        .map_err(|_| Error::Unavailable)?
}

struct FixtureClock;

impl EffectiveClock for FixtureClock {
    fn read(&self) -> std::result::Result<Reading, ClockError> {
        FixedClock::new(fixture::fixture_time()?).read()
    }
}

fn local_policy(selected: &Selection<'_>) -> Result<ActivePolicy> {
    let policy = include_str!("../../../m1/fixtures/transition/m1-transition.bundle.json")
        .replace("fixture-tenant", fixture::TENANT)
        .replace("fixture-project", fixture::PROJECT)
        .replace("writ-host-adapter:fixture", INGRESS_ADAPTER);
    let Value::Obj(mut bundle) = json::parse(policy.as_bytes()).map_err(|_| Error::Unavailable)?
    else {
        return Err(Error::Unavailable);
    };
    let Some(Value::Obj(protection)) = bundle.get_mut("workflow_protection") else {
        return Err(Error::Unavailable);
    };
    protection.insert(
        "acceptance_policy_bundle_sha".into(),
        json::s(
            &selected
                .acceptance()
                .map_err(|_| Error::Unavailable)?
                .policy
                .acceptance_policy_bundle_sha,
        ),
    );
    protection.insert("protected_transitions".into(), Value::Arr(vec![]));
    let loaded = load_bundle(&Value::Obj(bundle)).map_err(|_| Error::Unavailable)?;
    Ok(ActivePolicy::new(PolicyState::loaded(loaded)))
}

fn open_registry(path: &Path) -> Result<Registry<fixture::FixtureTrust>> {
    Registry::open(
        path,
        fixture::publication_scope().map_err(|_| Error::Unavailable)?,
        fixture::FixtureTrust::default(),
    )
    .map_err(|_| Error::Unavailable)
}

fn effects_path(path: &Path) -> PathBuf {
    path.with_extension("waiver-effects.sqlite")
}

pub fn ingress_path(path: &Path) -> PathBuf {
    path.with_extension("waiver-ingress.json")
}

struct FixtureIngress(Value);

impl FixtureIngress {
    fn open(ledger_path: &Path) -> Result<Self> {
        let data = fs::read(ingress_path(ledger_path)).map_err(|_| Error::Unavailable)?;
        let request = json::parse(&data).map_err(|_| Error::Unavailable)?;
        Ok(Self(request))
    }
}

impl WaiverTransitions for FixtureIngress {
    fn resolve(
        &self,
        scope: &Scope,
        transition_ref: &str,
        management_requester: Option<&str>,
    ) -> Result<WaiverTransition> {
        if scope.tenant != fixture::TENANT
            || scope.project != fixture::PROJECT
            || transition_ref != TRANSITION_REFERENCE
        {
            return Err(Error::NotFound);
        }
        if management_requester.is_some_and(|subject| subject != REQUESTER) {
            return Err(Error::Unauthorized);
        }
        verify_disposable_waiver_transition(&self.0).map_err(|_| Error::Unauthorized)
    }
}

struct Snapshot;

impl SnapshotStore for Snapshot {
    fn check(
        &self,
        tenant: &str,
        project: &str,
        snapshot: &str,
        digest: &str,
    ) -> std::result::Result<SnapshotCheck, SourceUnavailable> {
        Ok(
            if tenant != fixture::TENANT || project != fixture::PROJECT || snapshot != "snapshot" {
                SnapshotCheck::Unknown
            } else if digest == Digest::of_bytes(ARTIFACT).as_str() {
                SnapshotCheck::Matches
            } else {
                SnapshotCheck::Differs
            },
        )
    }
}

struct SetupOperator;

impl WorkflowAuthority for SetupOperator {
    fn authorize(
        &self,
        presentation: &AdminPresentation<'_>,
        operation: AdminOperation,
        tenant: &str,
        project: &str,
    ) -> std::result::Result<AuthorizedOperator, AdminRefusal> {
        if tenant != fixture::TENANT
            || project != fixture::PROJECT
            || presentation.operator != OPERATOR
            || presentation.credential != SETUP_PROOF
            || !matches!(
                operation,
                AdminOperation::ActivateAcceptancePolicy | AdminOperation::CreateWorkflow
            )
        {
            return Err(AdminRefusal::Unauthorized);
        }
        Ok(AuthorizedOperator {
            issuer: "fixture-waiver-workflow-admin".into(),
            id: OPERATOR.into(),
            channel: presentation.channel.into(),
            evidence_ref: "disposable-waiver-bootstrap".into(),
        })
    }
}

pub fn channel() -> Result<ApprovalChannel> {
    let verifier = Arc::new(Verifier);
    ApprovalChannel::new(verifier.clone(), verifier, "fixture-waiver-assertion")
        .ok_or(Error::Unavailable)
}

struct Verifier;

fn relying_party() -> RelyingParty {
    RelyingParty {
        rp_id: "localhost".into(),
        origin: "http://localhost:8443".into(),
    }
}

fn nonce() -> std::result::Result<Vec<u8>, VerifierError> {
    let mut bytes = vec![0u8; 32];
    File::open("/dev/urandom")
        .and_then(|mut file| file.read_exact(&mut bytes))
        .map_err(|_| VerifierError::Unavailable)?;
    Ok(bytes)
}

impl RegistrationVerifier for Verifier {
    fn relying_party(&self) -> RelyingParty {
        relying_party()
    }

    fn begin_registration(
        &self,
        subject: &SubjectId,
    ) -> std::result::Result<RegistrationStart, VerifierError> {
        if subject.issuer != HUMAN_ISSUER {
            return Err(VerifierError::CredentialUnknown);
        }
        Ok(RegistrationStart {
            client_options: vec![],
            state: CeremonyState::from_bytes(nonce()?),
        })
    }

    fn finish_registration(
        &self,
        cap: &VerifierCapability,
        binding: &RegistrationBinding,
        _: &CeremonyState,
        response: &[u8],
    ) -> std::result::Result<VerifiedRegistration, VerifierError> {
        let credential = std::str::from_utf8(response).map_err(|_| VerifierError::Malformed)?;
        if credential != binding.subject.subject || binding.subject.issuer != HUMAN_ISSUER {
            return Err(VerifierError::CredentialUnknown);
        }
        let key = match credential {
            APPROVER => APPROVER_KEY,
            OPERATOR => OPERATOR_KEY,
            _ => return Err(VerifierError::CredentialUnknown),
        };
        Ok(VerifiedRegistration::new(
            cap,
            binding,
            credential,
            key.to_vec(),
            ES256,
            true,
        ))
    }
}

impl HumanVerifier for Verifier {
    fn relying_party(&self) -> RelyingParty {
        relying_party()
    }

    fn begin(
        &self,
        _: &SubjectId,
        _: &ActiveCredentials,
    ) -> std::result::Result<CeremonyStart, VerifierError> {
        let options = nonce()?;
        Ok(CeremonyStart {
            challenge_digest: Digest::of_bytes(&options),
            state: CeremonyState::from_bytes(options.clone()),
            client_options: options,
        })
    }

    fn finish(
        &self,
        cap: &VerifierCapability,
        state: &CeremonyState,
        response: &[u8],
        credentials: &ActiveCredentials,
    ) -> std::result::Result<VerifiedAssertion, VerifierError> {
        let text = std::str::from_utf8(response).map_err(|_| VerifierError::Malformed)?;
        let signature =
            Signature::from_bytes(&decode_hex::<64>(text).ok_or(VerifierError::Malformed)?);
        let message = [ASSERTION_DOMAIN, state.as_bytes()].concat();
        for credential in credentials.as_slice() {
            let public: [u8; 32] = credential
                .public_material
                .as_slice()
                .try_into()
                .map_err(|_| VerifierError::Malformed)?;
            let key = VerifyingKey::from_bytes(&public).map_err(|_| VerifierError::Malformed)?;
            if key.verify_strict(&message, &signature).is_ok() {
                return Ok(VerifiedAssertion::new(
                    cap,
                    credentials.subject().clone(),
                    &credential.credential_id,
                    true,
                    WEBAUTHN_PASSKEY,
                    Digest::of_bytes(state.as_bytes()),
                    None,
                ));
            }
        }
        Err(VerifierError::CredentialUnknown)
    }
}

pub fn decode_hex<const N: usize>(text: &str) -> Option<[u8; N]> {
    if text.len() != 2 * N || !text.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return None;
    }
    let mut bytes = [0; N];
    for (index, byte) in bytes.iter_mut().enumerate() {
        *byte = u8::from_str_radix(&text[index * 2..index * 2 + 2], 16).ok()?;
    }
    Some(bytes)
}
