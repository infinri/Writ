//! Separate, deterministic test-only publisher and human ceremony signer.

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
    pack_fixture::{authorizable_intent, signable_payload, CEREMONY_DOMAIN, FIXTURE_TIME, HUMAN},
    packs::pack_signature_message,
    MAX_REQUEST,
};

fn sign(seed: u8, message: &[u8]) -> Option<String> {
    SigningKey::from_bytes(&[seed; 32])
        .sign(message)
        .to_bytes()
        .iter()
        .try_fold(String::new(), |mut text, byte| {
            write!(text, "{byte:02x}").ok()?;
            Some(text)
        })
}

fn run() -> Option<String> {
    let mut args = env::args_os().skip(1);
    let operation = args.next()?.into_string().ok()?;
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
    let mut input = Vec::new();
    io::stdin()
        .lock()
        .take(MAX_REQUEST as u64 + 1)
        .read_to_end(&mut input)
        .ok()?;
    if input.len() > MAX_REQUEST {
        return None;
    }
    let value = json::parse(&input).ok()?;
    let body = match (operation.as_str(), credential.as_slice()) {
        ("sign", b"fixture-pack-build-signer") if signable_payload(&value) => {
            let signature = sign(0x76, &pack_signature_message(&value).ok()?)?;
            json::obj([("payload", value), ("signature", json::s(&signature))])
        }
        ("approve", b"fixture-pack-publisher-step-up") => {
            let ceremony = args.next()?.into_string().ok()?;
            let issued_at = match args.next() {
                Some(time) => time.into_string().ok()?,
                None => FIXTURE_TIME.into(),
            };
            if !ids::is_id(&ceremony)
                || Timestamp::parse_strict(&issued_at).is_none()
                || value.get("version").and_then(Value::as_str) != Some("/manage/v1")
                || value.get("ok").and_then(Value::as_bool) != Some(true)
            {
                return None;
            }
            let result = value.get("result")?;
            let intent = result.get("intent")?;
            let digest = json::digest(intent).ok()?;
            if !authorizable_intent(intent)
                || result.get("intent_digest").and_then(Value::as_str) != Some(digest.as_str())
            {
                return None;
            }
            let claim = format!("{HUMAN}|{ceremony}|{issued_at}|{digest}");
            let signature = sign(0x77, format!("{CEREMONY_DOMAIN}{claim}").as_bytes())?;
            json::obj([("presentation", json::s(&format!("{claim}|{signature}")))])
        }
        _ => return None,
    };
    if args.next().is_some() {
        return None;
    }
    json::canonical(&body).ok()
}

fn main() -> ExitCode {
    match run() {
        Some(body) if writeln!(io::stdout().lock(), "{body}").is_ok() => ExitCode::SUCCESS,
        _ => ExitCode::FAILURE,
    }
}
