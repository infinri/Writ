//! Explicit disposable fixture CLI for the v1 management API.

use std::{
    env, fs,
    io::{self, Read, Write},
    path::Path,
    process::ExitCode,
};

use writ_kernel::json;
use writ_policy_management_lab::{
    contract_fixture, contract_registry::ContractRegistry, error_response, evaluator_fixture,
    execute_v1_with_service, fixture, waiver_fixture::WaiverFixture, Error, Service, Store,
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
        [command, registry] if command == "init-contracts" => {
            contract_fixture::initialize(Path::new(registry))?;
            Ok(br#"{"ok":true,"result":{"initialized":true},"version":"/manage/v1"}"#.to_vec())
        }
        [command, registry] if command == "recover-contract-storage" => {
            ContractRegistry::recover_storage(Path::new(registry), &contract_fixture::scope())?;
            Ok(br#"{"ok":true,"result":{"recovered":true},"version":"/manage/v1"}"#.to_vec())
        }
        [command, ledger] if command == "init-evaluator" => {
            evaluator_fixture::initialize(Path::new(ledger))?;
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
        [command, db, credential_file, extra @ ..]
            if (command == "v1" && extra.len() <= 1)
                || (command == "v1-contracts" && extra.len() == 1)
                || (command == "v1-evaluator" && extra.len() == 1)
                || (command == "v1-waiver" && extra.len() == 2) =>
        {
            let path = Path::new(credential_file);
            if !path.metadata().is_ok_and(|metadata| metadata.is_file()) {
                return Err(Error::Unauthorized);
            }
            let credential = fs::File::open(path).map_err(|_| Error::Unauthorized)?;
            let credential = read_bounded(credential, MAX_CREDENTIAL)?;
            let request = read_bounded(io::stdin().lock(), MAX_REQUEST)?;
            let store = if command == "v1-contracts" {
                Store::open_without_recovery(Path::new(db))?
            } else {
                Store::open(Path::new(db))?
            };
            let authority = fixture::authority();
            let service = Service::new(&store, Some(&authority));
            match extra {
                [registry] if command == "v1-contracts" => {
                    let registry = contract_fixture::open(Path::new(registry))?;
                    let service = service.with_contracts(&registry).allow_self_publication();
                    Ok(execute_v1_with_service(&service, &credential, &request))
                }
                [ledger] if command == "v1-evaluator" => {
                    let evaluator = evaluator_fixture::open(Path::new(ledger))?;
                    let service = service.with_evaluator(&evaluator);
                    Ok(execute_v1_with_service(&service, &credential, &request))
                }
                [ledger, policy] if command == "v1-waiver" => {
                    let waiver = WaiverFixture::open(Path::new(ledger), Path::new(policy))?;
                    let service = service.with_waiver(&waiver);
                    Ok(execute_v1_with_service(&service, &credential, &request))
                }
                [registry] => {
                    let registry = Registry::open_for_administration(
                        Path::new(registry),
                        fixture::publication_scope().map_err(|_| Error::Unavailable)?,
                        fixture::FixtureTrust::default(),
                    )
                    .map_err(|_| Error::Unavailable)?
                    .with_management_source(store.clone());
                    let service = service.with_publication(&registry).allow_self_publication();
                    Ok(execute_v1_with_service(&service, &credential, &request))
                }
                _ => Ok(execute_v1_with_service(&service, &credential, &request)),
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
