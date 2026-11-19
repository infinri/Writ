//! Separate signer for synthetic evaluator publisher ceremonies.

use std::{
    env,
    fmt::Write as _,
    fs,
    io::{self, Read, Write},
    process::ExitCode,
};

use ed25519_dalek::{Signer as _, SigningKey};
use writ_kernel::{
    ids,
    json::{self, Value},
    time::Timestamp,
};
use writ_policy_management_lab::{
    evaluator_fixture::{CEREMONY_DOMAIN, FIXTURE_TIME, PUBLISHER},
    MAX_REQUEST,
};

const TEST_SEED: [u8; 32] = [
    0x9d, 0x61, 0xb1, 0x9d, 0xef, 0xfd, 0x5a, 0x60, 0xba, 0x84, 0x4a, 0xf4, 0x92, 0xec, 0x2c, 0xc4,
    0x44, 0x49, 0xc5, 0x69, 0x7b, 0x32, 0x69, 0x19, 0x70, 0x3b, 0xac, 0x03, 0x1c, 0xae, 0x7f, 0x60,
];

fn run() -> Option<String> {
    let mut args = env::args_os().skip(1);
    let credential_path = args.next()?;
    if !fs::metadata(&credential_path).ok()?.is_file() {
        return None;
    }
    let credential = fs::File::open(credential_path).ok()?;
    let mut bytes = Vec::new();
    credential.take(64).read_to_end(&mut bytes).ok()?;
    if bytes != b"fixture-evaluator-human" {
        return None;
    }
    let ceremony = args.next()?.into_string().ok()?;
    let issued_at = match args.next() {
        Some(time) => time.into_string().ok()?,
        None => FIXTURE_TIME.into(),
    };
    if !ids::is_id(&ceremony)
        || Timestamp::parse_strict(&issued_at).is_none()
        || args.next().is_some()
    {
        return None;
    }
    let mut input = Vec::new();
    io::stdin()
        .lock()
        .take(MAX_REQUEST as u64 + 1)
        .read_to_end(&mut input)
        .ok()?;
    if input.len() > MAX_REQUEST {
        return None;
    }
    let response = json::parse(&input).ok()?;
    if response.get("ok").and_then(Value::as_bool) != Some(true) {
        return None;
    }
    let response = response.get("result")?;
    let digest = response.get("intent_digest")?.as_str()?;
    let intent = response.get("intent")?;
    if !ids::is_digest(digest)
        || json::digest(intent).ok()?.as_str() != digest
        || intent.get("purpose").and_then(Value::as_str) != Some("writ-human-evaluator-publisher/1")
    {
        return None;
    }
    let claim = format!("{PUBLISHER}|{ceremony}|{issued_at}|{digest}");
    let signed = format!("{CEREMONY_DOMAIN}{claim}");
    let signature = SigningKey::from_bytes(&TEST_SEED).sign(signed.as_bytes());
    let mut presentation = format!("{claim}|");
    for byte in signature.to_bytes() {
        write!(presentation, "{byte:02x}").ok()?;
    }
    json::canonical(&json::obj([("presentation", json::s(&presentation))])).ok()
}

fn main() -> ExitCode {
    match run() {
        Some(body) if writeln!(io::stdout().lock(), "{body}").is_ok() => ExitCode::SUCCESS,
        _ => ExitCode::FAILURE,
    }
}
