//! Explicit disposable fixture CLI for the v1 management API.

use std::{
    env, fs,
    io::{self, Read, Write},
    path::Path,
    process::ExitCode,
};

use writ_kernel::json;
use writ_policy_management_lab::{
    error_response, execute_v1, execute_v1_with_publication, fixture, Error, Service, Store,
    MAX_CREDENTIAL, MAX_REQUEST,
};
use writ_policy_publication_lab::Registry;

fn read_bounded(reader: impl Read, limit: usize) -> Result<Vec<u8>, Error> {
    let mut bytes = Vec::new();
    reader
        .take(limit as u64 + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| Error::Unavailable)?;
    Ok(bytes)
}

fn run() -> Result<Vec<u8>, Error> {
    let args: Vec<_> = env::args_os().skip(1).take(5).collect();
    match args.as_slice() {
        [command, db] if command == "init" => {
            Store::create(Path::new(db))?;
            Ok(br#"{"ok":true,"result":{"initialized":true},"version":"/manage/v1"}"#.to_vec())
        }
        [command, db] if command == "init-publication" => {
            Registry::create(
                Path::new(db),
                fixture::publication_scope().map_err(|_| Error::Unavailable)?,
                fixture::FixtureTrust::default(),
            )
            .map_err(|_| Error::Unavailable)?;
            Ok(br#"{"ok":true,"result":{"initialized":true},"version":"/manage/v1"}"#.to_vec())
        }
        [command, db, credential_file, extra @ ..] if command == "v1" && extra.len() <= 1 => {
            let path = Path::new(credential_file);
            if !path.metadata().is_ok_and(|metadata| metadata.is_file()) {
                return Err(Error::Unauthorized);
            }
            let credential = fs::File::open(path).map_err(|_| Error::Unauthorized)?;
            let credential = read_bounded(credential, MAX_CREDENTIAL)?;
            let request = read_bounded(io::stdin().lock(), MAX_REQUEST)?;
            let store = Store::open(Path::new(db))?;
            let authority = fixture::authority();
            match extra {
                [registry] => {
                    let registry = Registry::open_for_administration(
                        Path::new(registry),
                        fixture::publication_scope().map_err(|_| Error::Unavailable)?,
                        fixture::FixtureTrust::default(),
                    )
                    .map_err(|_| Error::Unavailable)?
                    .with_management_source(store.clone());
                    let service = Service::new(&store, Some(&authority))
                        .with_publication(&registry)
                        .allow_self_publication();
                    Ok(execute_v1_with_publication(&service, &credential, &request))
                }
                _ => Ok(execute_v1(&store, Some(&authority), &credential, &request)),
            }
        }
        _ => Err(Error::InvalidRequest),
    }
}

fn main() -> ExitCode {
    let response = run().unwrap_or_else(error_response);
    let success = json::parse(&response)
        .ok()
        .and_then(|value| value.get("ok").and_then(json::Value::as_bool))
        .unwrap_or(false);
    let mut output = io::stdout().lock();
    if output.write_all(&response).is_err() || output.write_all(b"\n").is_err() {
        return ExitCode::FAILURE;
    }
    if success {
        ExitCode::SUCCESS
    } else {
        ExitCode::FAILURE
    }
}
