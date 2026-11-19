//! Separate disposable signer and simulated human publisher ceremony.

use std::{
    env, fs,
    io::{self, Read, Write},
    path::Path,
    process::ExitCode,
};

use ed25519_dalek::{Signer as _, SigningKey};
use writ_kernel::{
    ids::is_id,
    json::{self, Value},
};
use writ_policy_management_lab::{fixture, MAX_CANDIDATE, MAX_REQUEST};
use writ_policy_publication_lab::{
    sign, Error, Intent, Protected, Publication, Purpose, SignedPolicy, Signer,
};

struct FixtureSigner;

impl Signer for FixtureSigner {
    fn sign(&self, header: &Protected, bytes: &[u8]) -> Result<[u8; 64], Error> {
        let seed = match (header.purpose(), header.key_id()) {
            (Purpose::Authorization, fixture::AUTHORIZATION_KEY) => 0x61,
            (Purpose::Acceptance, fixture::ACCEPTANCE_KEY) => 0x62,
            _ => return Err(Error::Trust),
        };
        if header.scope() != &fixture::publication_scope()? {
            return Err(Error::Scope);
        }
        Ok(SigningKey::from_bytes(&[seed; 32]).sign(bytes).to_bytes())
    }
}

fn sign_policy(purpose: Purpose, key_id: &str, payload: Value) -> Result<SignedPolicy, Error> {
    let (start, end) = fixture::policy_window()?;
    let header = Protected::new(
        purpose,
        fixture::publication_scope()?,
        &payload,
        key_id,
        start,
        end,
    )?;
    sign(payload, &header, &FixtureSigner)
}

fn sign_publication(draft: &[u8], version: i64) -> Result<Value, Error> {
    let report = writ_policy_compile::evaluate(draft);
    if !report.accepted {
        return Err(Error::InvalidPolicy);
    }
    let proof = report
        .document
        .get("kernel_proof")
        .ok_or(Error::InvalidPolicy)?;
    let authorization = report
        .document
        .get("bundle")
        .ok_or(Error::InvalidPolicy)?
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
    Ok(Publication {
        authorization: sign_policy(
            Purpose::Authorization,
            fixture::AUTHORIZATION_KEY,
            authorization,
        )?,
        acceptance: sign_policy(Purpose::Acceptance, fixture::ACCEPTANCE_KEY, acceptance)?,
        prerequisites: json::obj([
            ("version", json::s("writ-policy-prerequisites/1")),
            ("draft", json::parse(draft).map_err(|_| Error::Format)?),
            (
                "proof_digest",
                json::s(
                    json::digest(proof)
                        .map_err(|_| Error::InvalidPolicy)?
                        .as_str(),
                ),
            ),
        ]),
    }
    .to_value())
}

fn authorize(preview: &[u8], credential: &[u8], ceremony: &str) -> Result<Value, Error> {
    let human = match credential {
        b"fixture-publisher-step-up" => "fixture-publisher",
        b"fixture-author-publisher-step-up" => "fixture-author-publisher",
        _ => return Err(Error::Authority),
    };
    if !is_id(ceremony) {
        return Err(Error::Format);
    }
    let preview = json::parse(preview).map_err(|_| Error::Format)?;
    if preview.get("version").and_then(Value::as_str) != Some("/manage/v1")
        || preview.get("ok").and_then(Value::as_bool) != Some(true)
    {
        return Err(Error::Format);
    }
    let result = preview.get("result").ok_or(Error::Format)?;
    let intent = result.get("intent").ok_or(Error::Format)?;
    let context = result.get("context").ok_or(Error::Format)?;
    let management = intent.get("management").ok_or(Error::Format)?;
    let context_digest = json::digest(context).map_err(|_| Error::Format)?;
    let exact = json::digest(intent).map_err(|_| Error::Format)?;
    if intent.get("purpose").and_then(Value::as_str) != Some("writ-human-policy-publisher/1")
        || intent.get("scope") != Some(&fixture::publication_scope()?.to_value())
        || !matches!(
            intent.get("operation").and_then(Value::as_str),
            Some("publish" | "rollback" | "retire")
        )
        || management.get("publisher").and_then(Value::as_str) != Some(human)
        || management.get("digest").and_then(Value::as_str) != Some(context_digest.as_str())
        || result.get("intent_digest").and_then(Value::as_str) != Some(exact.as_str())
    {
        return Err(Error::Authority);
    }
    if intent.get("operation").and_then(Value::as_str) == Some("retire") {
        let version = intent
            .get("publication_version")
            .and_then(Value::as_int)
            .ok_or(Error::Format)?;
        let confirmation = json::s(&Intent::retirement_phrase(version));
        if intent.get("confirmation") != Some(&confirmation)
            || context.get("confirmation") != Some(&confirmation)
        {
            return Err(Error::Authority);
        }
    }
    let now = fixture::fixture_time().map_err(|_| Error::Time)?;
    let claim = format!("{human}|{ceremony}|{now}|{exact}");
    let signed = format!("{}{claim}", fixture::CEREMONY_DOMAIN);
    let signature = SigningKey::from_bytes(&[0x63; 32]).sign(signed.as_bytes());
    use core::fmt::Write as _;
    let mut presentation = format!("{claim}|");
    for byte in signature.to_bytes() {
        write!(presentation, "{byte:02x}").map_err(|_| Error::Format)?;
    }
    Ok(json::obj([("presentation", json::s(&presentation))]))
}

fn read_bounded(reader: impl Read, max: usize) -> Result<Vec<u8>, Error> {
    let mut bytes = Vec::new();
    reader
        .take(max as u64 + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| Error::Format)?;
    if bytes.len() > max {
        return Err(Error::Format);
    }
    Ok(bytes)
}

fn run() -> Result<Value, Error> {
    let args: Vec<_> = env::args_os().skip(1).take(4).collect();
    match args.as_slice() {
        [command, version] if command == "sign-publication" => {
            let version = version
                .to_str()
                .and_then(|text| text.parse::<i64>().ok())
                .filter(|version| (1..=999_999_999).contains(version))
                .ok_or(Error::Format)?;
            sign_publication(&read_bounded(io::stdin().lock(), MAX_CANDIDATE)?, version)
        }
        [command, path, ceremony] if command == "authorize-publication" => {
            let path = Path::new(path);
            if !path.metadata().is_ok_and(|metadata| metadata.is_file()) {
                return Err(Error::Authority);
            }
            let credential =
                read_bounded(fs::File::open(path).map_err(|_| Error::Authority)?, 128)?;
            authorize(
                &read_bounded(io::stdin().lock(), MAX_REQUEST)?,
                &credential,
                ceremony.to_str().ok_or(Error::Format)?,
            )
        }
        _ => Err(Error::Format),
    }
}

fn main() -> ExitCode {
    let result = run().and_then(|value| json::canonical(&value).map_err(|_| Error::Format));
    match result {
        Ok(response) => {
            let mut out = io::stdout().lock();
            if writeln!(out, "{response}").is_ok() {
                ExitCode::SUCCESS
            } else {
                ExitCode::FAILURE
            }
        }
        Err(_) => {
            eprintln!("Fixture authority refused the request.");
            ExitCode::FAILURE
        }
    }
}
