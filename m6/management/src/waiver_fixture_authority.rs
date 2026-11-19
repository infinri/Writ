//! Disposable authority process for signed acceptance, workflow setup and human assertions.

use std::{
    env, fs,
    io::{self, Read, Write},
    path::Path,
    process::ExitCode,
};

use ed25519_dalek::{Signer as _, SigningKey};
use writ_broker::{
    approval_store::{
        ApprovalLedger, BootstrapEnrollment, CeremonyBegin, CeremonyFinish, CeremonyOp,
        CeremonyResult, NewEnrollment, NewRole, OperatorSession, RegistrationBegin,
        RegistrationFinish, RegistrationLedger,
    },
    clock::{observe, FixedClock, ObservedTime},
    human::{Permission, PrincipalKind, SubjectId, VerifiedRegistration},
    waiver::WaiverBinding,
};
use writ_kernel::json::{self, Digest, Value};
use writ_ledger_sqlite::{BootstrapSession, SqliteLedger};
use writ_policy_management_lab::{fixture, waiver_fixture as waiver, MAX_REQUEST};
use writ_policy_publication_lab::{
    sign, Error as PolicyError, Intent, Operation, Protected, Publication, Purpose, Registry,
    SignedPolicy, Signer,
};

type Result<T> = std::result::Result<T, ()>;

const SESSION: &str = "waiver-setup-operator-session";
const OPERATOR_SEED: [u8; 32] = [0x72; 32];
const APPROVER_SEED: [u8; 32] = [0x71; 32];

struct FixturePolicySigner;

impl Signer for FixturePolicySigner {
    fn sign(
        &self,
        protected: &Protected,
        bytes: &[u8],
    ) -> std::result::Result<[u8; 64], PolicyError> {
        if protected.scope() != &fixture::publication_scope()? {
            return Err(PolicyError::Scope);
        }
        let seed = match (protected.purpose(), protected.key_id()) {
            (Purpose::Authorization, fixture::AUTHORIZATION_KEY) => 0x61,
            (Purpose::Acceptance, fixture::ACCEPTANCE_KEY) => 0x62,
            _ => return Err(PolicyError::Trust),
        };
        Ok(SigningKey::from_bytes(&[seed; 32]).sign(bytes).to_bytes())
    }
}

fn sign_policy(purpose: Purpose, key: &str, payload: Value) -> Result<SignedPolicy> {
    let (start, end) = fixture::policy_window().map_err(|_| ())?;
    let scope = fixture::publication_scope().map_err(|_| ())?;
    let header = Protected::new(purpose, scope, &payload, key, start, end).map_err(|_| ())?;
    sign(payload, &header, &FixturePolicySigner).map_err(|_| ())
}

fn seed_policy(path: &Path) -> Result<Value> {
    let draft = include_bytes!("../../compiler/fixtures/permit.input.json");
    let report = writ_policy_compile::evaluate(draft);
    if !report.accepted {
        return Err(());
    }
    let authorization = report
        .document
        .get("bundle")
        .ok_or(())?
        .without("publisher_signature");
    let proof = report.document.get("kernel_proof").ok_or(())?;
    let rule = |criterion, class, origin, max_seconds| {
        json::obj([
            ("criterion_id", json::s(criterion)),
            ("required_class", json::s(class)),
            ("origin", json::s(origin)),
            ("max_waiver_seconds", Value::Int(max_seconds)),
        ])
    };
    let acceptance = json::obj([
        ("version", json::s("writ-acceptance-policy/2")),
        ("policy_id", json::s("m6-acceptance")),
        ("policy_version", json::s("1")),
        (
            "criteria",
            Value::Arr(vec![
                rule("review", "human", "invariant", 0),
                rule("waiver-review", "mechanical", "project", 300),
            ]),
        ),
    ]);
    let publication = Publication {
        authorization: sign_policy(
            Purpose::Authorization,
            fixture::AUTHORIZATION_KEY,
            authorization,
        )?,
        acceptance: sign_policy(Purpose::Acceptance, fixture::ACCEPTANCE_KEY, acceptance)?,
        prerequisites: json::obj([
            ("version", json::s("writ-policy-prerequisites/1")),
            ("draft", json::parse(draft).map_err(|_| ())?),
            (
                "proof_digest",
                json::s(json::digest(proof).map_err(|_| ())?.as_str()),
            ),
        ]),
    };
    let scope = fixture::publication_scope().map_err(|_| ())?;
    let intent =
        Intent::new(scope.clone(), Operation::Publish, 0, 1, &publication).map_err(|_| ())?;
    let digest = intent.fingerprint().map_err(|_| ())?;
    let now = fixture::fixture_time().map_err(|_| ())?;
    let claim = format!("fixture-publisher|fixture-waiver-policy|{now}|{digest}");
    let signing_bytes = format!("{}{claim}", fixture::CEREMONY_DOMAIN);
    let key = SigningKey::from_bytes(&[0x63; 32]);
    let presentation = format!(
        "{claim}|{}",
        hex(&key.sign(signing_bytes.as_bytes()).to_bytes())
    );
    let registry =
        Registry::create(path, scope, fixture::FixtureTrust::default()).map_err(|_| ())?;
    registry
        .publish(&intent, &publication, presentation.as_bytes())
        .map_err(|_| ())?;
    Ok(json::obj([("initialized", Value::Bool(true))]))
}

fn init_waiver(ledger_path: &Path, policy: &Path) -> Result<Value> {
    drop(SqliteLedger::create_waiver_lab(ledger_path).map_err(|_| ())?);
    let setup = BootstrapSession::open_waiver_lab(ledger_path)
        .map_err(|_| ())?
        .map_err(|_| ())?
        .with_approval_channel(waiver::channel().map_err(|_| ())?)
        .map_err(|_| ())?;
    let operator = SubjectId::new(waiver::HUMAN_ISSUER, waiver::OPERATOR);
    let reviewer = SubjectId::new(waiver::HUMAN_ISSUER, waiver::APPROVER);
    let clock = FixedClock::new(fixture::fixture_time().map_err(|_| ())?);
    let now = setup.observe(&clock).map_err(|_| ())?;
    let registration = setup
        .begin_registration(&RegistrationBegin {
            subject: &operator,
            operator: None,
            now,
        })
        .map_err(|_| ())?
        .map_err(|_| ())?;
    let credential = register(&setup, &registration.registration_id, &operator, None, now)?;
    setup
        .bootstrap(
            &BootstrapEnrollment {
                subject: operator.clone(),
                credential,
                linked_identities: vec![],
            },
            now,
        )
        .map_err(|_| ())?
        .map_err(|_| ())?;

    let ledger = SqliteLedger::open_waiver_lab(ledger_path)
        .map_err(|_| ())?
        .with_approval_channel(waiver::channel().map_err(|_| ())?)
        .map_err(|_| ())?;
    let now = observe(&clock, &mut &ledger).map_err(|_| ())?;
    let session = OperatorSession {
        operator: operator.clone(),
        session_id: SESSION.into(),
    };
    let start = ceremony(
        &ledger,
        &operator,
        &CeremonyOp::RegistrationStart {
            subject: reviewer.clone(),
            operator: operator.clone(),
            operator_session: SESSION.into(),
        },
        "waiver-setup-registration",
        now,
    )?;
    let CeremonyResult::RegistrationStarted { registration } = start else {
        return Err(());
    };
    let credential = register(
        &ledger,
        &registration.registration_id,
        &reviewer,
        Some(&session),
        now,
    )?;
    let enrollment = NewEnrollment {
        subject: reviewer.clone(),
        kind: PrincipalKind::Human,
        credential: Some(credential),
        linked_identities: vec![],
        roles: vec![NewRole {
            permission: Permission::WaiverAuthority,
            tenant_id: fixture::TENANT.into(),
            project_id: fixture::PROJECT.into(),
        }],
    };
    if ceremony(
        &ledger,
        &operator,
        &CeremonyOp::Enroll {
            enrollment,
            operator_session: SESSION.into(),
        },
        "waiver-setup-enrollment",
        now,
    )? != (CeremonyResult::Enrolled { subject: reviewer })
    {
        return Err(());
    }
    drop(ledger);

    waiver::initialize_workflow(ledger_path, policy).map_err(|_| ())?;
    let mut transition = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(waiver::ingress_path(ledger_path))
        .map_err(|_| ())?;
    transition
        .write_all(include_bytes!("../fixtures/waiver-transition.json"))
        .map_err(|_| ())?;
    Ok(json::obj([("initialized", Value::Bool(true))]))
}

fn register(
    ledger: &dyn RegistrationLedger,
    registration: &str,
    subject: &SubjectId,
    operator: Option<&OperatorSession>,
    now: ObservedTime,
) -> Result<VerifiedRegistration> {
    ledger
        .finish_registration(&RegistrationFinish {
            registration_id: registration,
            response: subject.subject.as_bytes(),
            operator,
            now,
        })
        .map_err(|_| ())?
        .map_err(|_| ())
}

fn ceremony(
    ledger: &SqliteLedger,
    operator: &SubjectId,
    operation: &CeremonyOp,
    key: &str,
    now: ObservedTime,
) -> Result<CeremonyResult> {
    let issued = ledger
        .begin_ceremony(&CeremonyBegin {
            op: operation,
            subject: operator,
            idempotency_key: key,
            now,
        })
        .map_err(|_| ())?
        .map_err(|_| ())?;
    let response = assertion(&OPERATOR_SEED, &issued.client_options);
    let outcome = ledger
        .finish_ceremony(&CeremonyFinish {
            op: operation,
            challenge_id: &issued.challenge_id,
            response: response.as_bytes(),
            idempotency_key: key,
            now,
        })
        .map_err(|_| ())?
        .map_err(|_| ())?;
    Ok(outcome.result)
}

fn assertion(seed: &[u8; 32], options: &[u8]) -> String {
    let message = [waiver::ASSERTION_DOMAIN, options].concat();
    hex(&SigningKey::from_bytes(seed).sign(&message).to_bytes())
}

fn hex(bytes: &[u8]) -> String {
    const DIGITS: &[u8] = b"0123456789abcdef";
    let mut output = String::with_capacity(bytes.len() * 2);
    for &byte in bytes {
        output.push(char::from(DIGITS[usize::from(byte >> 4)]));
        output.push(char::from(DIGITS[usize::from(byte & 15)]));
    }
    output
}

fn read_bounded(input: impl Read, max: usize) -> Result<Vec<u8>> {
    let mut bytes = Vec::new();
    input
        .take(max as u64 + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| ())?;
    if bytes.len() > max {
        return Err(());
    }
    Ok(bytes)
}

fn assert_waiver(ledger_path: &Path, path: &Path) -> Result<Value> {
    if !path.metadata().is_ok_and(|data| data.is_file()) {
        return Err(());
    }
    let credential = read_bounded(fs::File::open(path).map_err(|_| ())?, 128)?;
    if credential != b"fixture-waiver-approver-step-up" {
        return Err(());
    }
    let response = json::parse(&read_bounded(io::stdin().lock(), MAX_REQUEST)?).map_err(|_| ())?;
    if response.get("ok") != Some(&Value::Bool(true))
        || response.get("version").and_then(Value::as_str) != Some("/manage/v1")
    {
        return Err(());
    }
    let result = response.get("result").ok_or(())?;
    let context = result.get("context").ok_or(())?;
    let binding = WaiverBinding::from_value(context).ok_or(())?;
    let digest = binding.fingerprint().map_err(|_| ())?;
    if binding.request.workflow.tenant_id != fixture::TENANT
        || binding.request.workflow.project_id != fixture::PROJECT
        || result.get("waiver_id").and_then(Value::as_str) != Some(digest.as_str())
    {
        return Err(());
    }
    let options = result
        .get("client_options_hex")
        .and_then(Value::as_str)
        .and_then(waiver::decode_hex::<32>)
        .ok_or(())?;
    let challenge = result
        .get("challenge_id")
        .and_then(Value::as_str)
        .ok_or(())?;
    let ledger = SqliteLedger::open_waiver_lab(ledger_path).map_err(|_| ())?;
    let approver = SubjectId::new(waiver::HUMAN_ISSUER, waiver::APPROVER);
    let now = fixture::fixture_time().map_err(|_| ())?;
    if !ledger
        .waiver_challenge_matches(
            &binding,
            challenge,
            &approver,
            &Digest::of_bytes(&options),
            now,
        )
        .map_err(|_| ())?
    {
        return Err(());
    }
    Ok(json::obj([(
        "presentation",
        json::s(&assertion(&APPROVER_SEED, &options)),
    )]))
}

fn run() -> Result<Value> {
    let args: Vec<_> = env::args_os().skip(1).take(4).collect();
    match args.as_slice() {
        [command, policy] if command == "seed-policy" => seed_policy(Path::new(policy)),
        [command, ledger, policy] if command == "init-waiver" => {
            init_waiver(Path::new(ledger), Path::new(policy))
        }
        [command, ledger, credential] if command == "assert-waiver" => {
            assert_waiver(Path::new(ledger), Path::new(credential))
        }
        _ => Err(()),
    }
}

fn main() -> ExitCode {
    match run().and_then(|value| json::canonical(&value).map_err(|_| ())) {
        Ok(response) if writeln!(io::stdout().lock(), "{response}").is_ok() => ExitCode::SUCCESS,
        _ => {
            eprintln!("Fixture waiver authority refused the request.");
            ExitCode::FAILURE
        }
    }
}
