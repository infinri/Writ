use std::{
    collections::BTreeMap,
    fs::{self, DirBuilder},
    os::unix::fs::DirBuilderExt,
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, AtomicI64, AtomicU64, Ordering},
        Arc, RwLock, RwLockReadGuard,
    },
};

use ed25519_dalek::{Signer as _, SigningKey};
use writ_broker::{
    clock::{ClockError, Drift, EffectiveClock, Reading, TrustedClock},
    effects::EffectAdapter,
    execute::{Broker, Executed},
    grants::GrantLabel,
    ingress::{verify_ingress, Seal, VerifiedRequest},
    keys::{IngressKey, MemoryKeyRing, PermitKey},
    permit::Permit,
    policy::{ActivePolicy, PolicyState},
    refusal::Refusal,
};
use writ_kernel::{
    contracts::{ActionRequest, StateRevision},
    json::{self, Digest, Value},
    time::Timestamp,
};
use writ_ledger_sqlite::{SqliteEffectStore, SqliteLedger};
use writ_policy_publication_lab::{
    sign, Approval, CurrentTrust, Error, Intent, Operation, Protected, Publication, Purpose,
    Registry, Scope, Selection, Signer, TrustAuthority, VerificationKey, VerificationSource,
};

pub const EPOCH: &str = "2026-11-19T12:00:00Z";
pub const KEY_AUTHORIZATION: &str = "lab-authorization";
pub const KEY_ACCEPTANCE: &str = "lab-acceptance";
const ADAPTER: &str = "m6-synthetic";

static NEXT_DIR: AtomicU64 = AtomicU64::new(1);

pub struct Disposable(PathBuf);

impl Disposable {
    pub fn create() -> std::io::Result<Self> {
        loop {
            let n = NEXT_DIR.fetch_add(1, Ordering::Relaxed);
            let path = std::env::temp_dir().join(format!("writ-m6-lab-{}-{n}", std::process::id()));
            match DirBuilder::new().mode(0o700).create(&path) {
                Ok(()) => return Ok(Self(path)),
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
                Err(error) => return Err(error),
            }
        }
    }

    pub fn path(&self) -> &Path {
        &self.0
    }
}

impl Drop for Disposable {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

pub fn scope() -> Scope {
    Scope::new("m6-lab", "offline-proof").unwrap()
}

pub fn now() -> Timestamp {
    Timestamp::parse_strict(EPOCH).unwrap()
}

#[derive(Clone)]
pub struct FixtureAuthority(Arc<AuthorityState>);

struct AuthorityState {
    data: RwLock<TrustData>,
    elapsed: AtomicI64,
    time_available: AtomicBool,
}

struct TrustData {
    keys: BTreeMap<String, VerificationKey>,
    presentations: BTreeMap<Vec<u8>, (Digest, Approval)>,
    trust_available: bool,
    publisher_enabled: bool,
}

pub struct FixtureView<'a> {
    state: &'a AuthorityState,
    data: RwLockReadGuard<'a, TrustData>,
}

impl FixtureAuthority {
    fn new(keys: BTreeMap<String, VerificationKey>) -> Self {
        Self(Arc::new(AuthorityState {
            data: RwLock::new(TrustData {
                keys,
                presentations: BTreeMap::new(),
                trust_available: true,
                publisher_enabled: true,
            }),
            elapsed: AtomicI64::new(0),
            time_available: AtomicBool::new(true),
        }))
    }

    pub fn authorize(&self, intent: &Intent, ceremony: &str) -> Vec<u8> {
        let issued_at = self.current().unwrap().now().unwrap();
        self.authorize_dates(intent, ceremony, issued_at, issued_at.plus_seconds(60))
    }

    pub fn authorize_dates(
        &self,
        intent: &Intent,
        ceremony: &str,
        issued_at: Timestamp,
        expires_at: Timestamp,
    ) -> Vec<u8> {
        let presentation =
            format!("fixture-proof/{ceremony}/{}", intent.fingerprint().unwrap()).into_bytes();
        self.0.data.write().unwrap().presentations.insert(
            presentation.clone(),
            (
                intent.fingerprint().unwrap(),
                Approval {
                    human: "lab-human-publisher".into(),
                    ceremony: ceremony.into(),
                    issued_at,
                    expires_at,
                },
            ),
        );
        presentation
    }

    pub fn revoke(&self, key: &str) {
        self.0
            .data
            .write()
            .unwrap()
            .keys
            .get_mut(key)
            .unwrap()
            .revoked = true;
    }

    pub fn set_key_expiry(&self, key: &str, expires_at: Timestamp) {
        self.0
            .data
            .write()
            .unwrap()
            .keys
            .get_mut(key)
            .unwrap()
            .expires_at = expires_at;
    }

    pub fn trust_available(&self, available: bool) {
        self.0.data.write().unwrap().trust_available = available;
    }

    pub fn time_available(&self, available: bool) {
        self.0.time_available.store(available, Ordering::SeqCst);
    }

    pub fn publisher_enabled(&self, enabled: bool) {
        self.0.data.write().unwrap().publisher_enabled = enabled;
    }

    pub fn advance_to(&self, elapsed: i64) {
        self.0.elapsed.fetch_max(elapsed, Ordering::SeqCst);
    }
}

impl TrustAuthority for FixtureAuthority {
    fn current(&self) -> Result<impl CurrentTrust + '_, Error> {
        let data = self.0.data.read().map_err(|_| Error::Trust)?;
        if !data.trust_available {
            return Err(Error::Trust);
        }
        Ok(FixtureView {
            state: &self.0,
            data,
        })
    }
}

impl VerificationSource for FixtureView<'_> {
    fn resolve(&self, key_id: &str) -> Result<VerificationKey, Error> {
        self.data.keys.get(key_id).cloned().ok_or(Error::Trust)
    }
}

impl TrustedClock for FixtureView<'_> {
    fn now(&self) -> Result<Timestamp, ClockError> {
        if !self.state.time_available.load(Ordering::SeqCst) {
            return Err(ClockError::Source);
        }
        Ok(now().plus_seconds(self.state.elapsed.load(Ordering::SeqCst)))
    }
}

impl CurrentTrust for FixtureView<'_> {
    fn approve(
        &self,
        intent: &Intent,
        presentation: &[u8],
        _now: Timestamp,
    ) -> Result<Approval, Error> {
        if !self.data.publisher_enabled || intent.scope() != &scope() {
            return Err(Error::Authority);
        }
        let (digest, approval) = self
            .data
            .presentations
            .get(presentation)
            .ok_or(Error::Authority)?;
        if digest != &intent.fingerprint()? {
            return Err(Error::Authority);
        }
        Ok(approval.clone())
    }
}

pub struct FixtureSigner(BTreeMap<String, (Purpose, SigningKey)>);

impl FixtureSigner {
    pub fn pair() -> (FixtureAuthority, Self) {
        let signers = [
            (KEY_AUTHORIZATION, Purpose::Authorization, 0x5a),
            (KEY_ACCEPTANCE, Purpose::Acceptance, 0x69),
        ]
        .into_iter()
        .map(|(key, purpose, seed)| (key.into(), (purpose, SigningKey::from_bytes(&[seed; 32]))))
        .collect::<BTreeMap<String, _>>();
        let keys = signers
            .iter()
            .map(|(key, (purpose, signer))| {
                (
                    key.clone(),
                    VerificationKey {
                        key_id: key.clone(),
                        purpose: *purpose,
                        scope: scope(),
                        public_key: signer.verifying_key().to_bytes(),
                        not_before: now(),
                        expires_at: now().plus_seconds(3600),
                        revoked: false,
                    },
                )
            })
            .collect();
        (FixtureAuthority::new(keys), Self(signers))
    }

    pub fn publication(&self, draft: &[u8], version: i64) -> Publication {
        let report = writ_policy_compile::evaluate(draft);
        assert!(report.accepted);
        let proof = report.document.get("kernel_proof").unwrap();
        let authorization = report
            .document
            .get("bundle")
            .unwrap()
            .without("publisher_signature");
        let acceptance = json::obj([
            ("version", json::s("writ-acceptance-policy/1")),
            ("policy_id", json::s("m6-acceptance")),
            ("policy_version", json::s(&version.to_string())),
            (
                "criteria",
                Value::Arr(vec![json::obj([
                    ("criterion_id", json::s("review")),
                    ("required_class", json::s("human")),
                ])]),
            ),
        ]);
        Publication {
            authorization: self.sign_policy(Purpose::Authorization, authorization),
            acceptance: self.sign_policy(Purpose::Acceptance, acceptance),
            prerequisites: json::obj([
                ("version", json::s("writ-policy-prerequisites/1")),
                ("draft", json::parse(draft).unwrap()),
                (
                    "proof_digest",
                    json::s(json::digest(proof).unwrap().as_str()),
                ),
            ]),
        }
    }

    pub fn sign_policy(
        &self,
        purpose: Purpose,
        payload: Value,
    ) -> writ_policy_publication_lab::SignedPolicy {
        self.sign_until(purpose, payload, now().plus_seconds(600))
    }

    pub fn sign_until(
        &self,
        purpose: Purpose,
        payload: Value,
        expires_at: Timestamp,
    ) -> writ_policy_publication_lab::SignedPolicy {
        let key = match purpose {
            Purpose::Authorization => KEY_AUTHORIZATION,
            Purpose::Acceptance => KEY_ACCEPTANCE,
        };
        let header = Protected::new(purpose, scope(), &payload, key, now(), expires_at).unwrap();
        sign(payload, &header, self).unwrap()
    }
}

impl Signer for FixtureSigner {
    fn sign(&self, protected: &Protected, bytes: &[u8]) -> Result<[u8; 64], Error> {
        let (purpose, signer) = self.0.get(protected.key_id()).ok_or(Error::Trust)?;
        if protected.purpose() != *purpose || protected.scope() != &scope() {
            return Err(Error::Trust);
        }
        Ok(signer.sign(bytes).to_bytes())
    }
}

pub const ALLOW: &[u8] = include_bytes!("../../../compiler/fixtures/permit.input.json");
pub const APPROVAL: &[u8] = include_bytes!("../../../compiler/fixtures/approval.input.json");
pub const DENY: &[u8] = include_bytes!("../../../compiler/fixtures/deny.input.json");

pub fn intent(
    operation: Operation,
    revision: i64,
    version: i64,
    publication: &Publication,
) -> Intent {
    Intent::new(scope(), operation, revision, version, publication).unwrap()
}

pub fn request(id: &str) -> VerifiedRequest {
    let requests =
        json::parse(include_bytes!("../../../compiler/fixtures/deny.cases.json")).unwrap();
    let mut doc = requests
        .as_arr()
        .unwrap()
        .iter()
        .find(|case| case.get("id").and_then(Value::as_str) == Some("winner"))
        .unwrap()
        .get("request")
        .unwrap()
        .as_obj()
        .unwrap()
        .clone();
    for name in ["idempotency_key", "request_id", "trace_id"] {
        doc.insert(name.into(), json::s(id));
    }
    doc.remove("expected_state_revision");
    seal_request(&Value::Obj(doc))
}

pub fn seal_request(request: &Value) -> VerifiedRequest {
    let key = || IngressKey::from_bytes(vec![0x31; 32]).unwrap();
    let mut keys = MemoryKeyRing::new();
    keys.register(ADAPTER, key());
    let seal = Seal::create(request, ADAPTER, &key()).unwrap().to_value();
    verify_ingress(request, Some(&seal), &keys).unwrap()
}

pub struct LabBroker<'r, T, A = SqliteEffectStore> {
    registry: &'r Registry<T>,
    pub ledger: SqliteLedger,
    pub effects: A,
    active: ActivePolicy,
    permit_key: PermitKey,
}

pub struct BoundPermit {
    scope: Scope,
    ledger: String,
    activation: i64,
    publication: Digest,
    permit: Permit,
}

impl BoundPermit {
    pub fn id(&self) -> &str {
        self.permit.permit_id()
    }
}

struct UseClock<'a>(&'a Selection<'a>);

impl EffectiveClock for UseClock<'_> {
    fn read(&self) -> Result<Reading, ClockError> {
        Ok(Reading {
            effective: self.0.recheck().map_err(|_| ClockError::Source)?,
            wall: None,
            drift: Drift::Normal,
            drift_changed: false,
        })
    }
}

impl<'r, T: TrustAuthority, A: EffectAdapter> LabBroker<'r, T, A> {
    pub fn new(registry: &'r Registry<T>, ledger: SqliteLedger, effects: A) -> Self {
        Self {
            registry,
            ledger,
            effects,
            active: ActivePolicy::new(PolicyState::Absent),
            permit_key: PermitKey::from_bytes(vec![0x42; 32]).unwrap(),
        }
    }

    pub fn into_stores(self) -> (SqliteLedger, A) {
        (self.ledger, self.effects)
    }

    pub fn execute(
        &self,
        request: &VerifiedRequest,
        grant: Option<&GrantLabel>,
    ) -> Result<(Result<Executed, Refusal>, Option<BoundPermit>), Error> {
        self.with_broker(request.request(), |selection, broker, state_revision| {
            let result = broker.execute(request, state_revision, grant);
            let publication = selection.publication().fingerprint()?;
            let permit = result.permit.map(|permit| BoundPermit {
                scope: self.registry.scope().clone(),
                ledger: self.ledger.ledger_id().as_str().to_string(),
                activation: selection.revision(),
                publication,
                permit,
            });
            Ok((result.result, permit))
        })
    }

    pub fn present(&self, permit: &BoundPermit, request: &Value) -> Result<Refusal, Error> {
        self.with_broker(request, |selection, broker, _| {
            if &permit.scope != self.registry.scope()
                || permit.ledger != self.ledger.ledger_id().as_str()
                || permit.activation != selection.revision()
                || permit.publication != selection.publication().fingerprint()?
            {
                return Err(Error::StalePermit);
            }
            Ok(broker.present_permit(&permit.permit, request))
        })
    }

    fn with_broker<R>(
        &self,
        request: &Value,
        use_broker: impl FnOnce(
            &Selection<'_>,
            &Broker<'_, SqliteLedger, A>,
            StateRevision,
        ) -> Result<R, Error>,
    ) -> Result<R, Error> {
        let request = ActionRequest::from_value(request).map_err(|_| Error::Format)?;
        let scope = self.registry.scope();
        let prefix = format!("synthetic:{}/{}/", scope.tenant(), scope.project());
        if request.tenant_id != scope.tenant()
            || request.project_id != scope.project()
            || request.action.kind != "synthetic.inspect"
            || !request
                .action
                .resource
                .canonical_resource_id
                .is_some_and(|id| id.starts_with(&prefix))
        {
            return Err(Error::Scope);
        }
        self.registry.with_selection(|selection| {
            self.active
                .activate(PolicyState::loaded(selection.authorization().clone()));
            let clock = UseClock(&selection);
            let broker = Broker::new(
                &self.active,
                &clock,
                &self.ledger,
                &self.effects,
                &self.permit_key,
            );
            let revision = StateRevision::new(selection.revision()).ok_or(Error::Recovery)?;
            use_broker(&selection, &broker, revision)
        })
    }
}
