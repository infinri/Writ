//! Separate signer for synthetic custom-contract human authorization.

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
    contract_fixture::{authorizable_intent, CEREMONY_DOMAIN, FIXTURE_TIME},
    MAX_REQUEST,
};

fn run() -> Option<String> {
    let mut args = env::args_os().skip(1);
    let path = args.next()?;
    if !fs::metadata(&path).ok()?.is_file() {
        return None;
    }
    let mut credential = Vec::new();
    fs::File::open(path)
        .ok()?
        .take(128)
        .read_to_end(&mut credential)
        .ok()?;
    let subject = match credential.as_slice() {
        b"fixture-contract-publisher-step-up" => "fixture-publisher",
        b"fixture-contract-author-publisher-step-up" => "fixture-author-publisher",
        _ => return None,
    };
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
    if response.get("version").and_then(Value::as_str) != Some("/manage/v1")
        || response.get("ok").and_then(Value::as_bool) != Some(true)
    {
        return None;
    }
    let result = response.get("result")?;
    let intent = result.get("intent")?;
    let digest = json::digest(intent).ok()?;
    if result.get("intent_digest").and_then(Value::as_str) != Some(digest.as_str())
        || !authorizable_intent(intent, subject)
    {
        return None;
    }
    let claim = format!("{subject}|{ceremony}|{issued_at}|{digest}");
    let signed = format!("{CEREMONY_DOMAIN}{claim}");
    let signature = SigningKey::from_bytes(&[0x64; 32]).sign(signed.as_bytes());
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
