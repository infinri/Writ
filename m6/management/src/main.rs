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
    fixture, pack_fixture, pack_registry::PackRegistry, preflight_v1,
    waiver_fixture::WaiverFixture, Error, Service, Store, MAX_CREDENTIAL, MAX_REQUEST,
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
    let args: Vec<_> = env::args_os().skip(1).take(8).collect();
    let authenticated = args.first().is_some_and(|value| value == "authenticated");
    let args = if authenticated { &args[1..] } else { &args[..] };
    if authenticated
        && !args.first().is_some_and(|value| {
            value
                .to_str()
                .is_some_and(|s| s == "v1" || s.starts_with("v1-"))
        })
    {
        return Err(Error::InvalidRequest);
    }
    match args {
        [command, db] if command == "init" => {
            Store::create(Path::new(db))?;
            Ok(br#"{"ok":true,"result":{"initialized":true},"version":"/manage/v1"}"#.to_vec())
        }
        [command, registry] if command == "init-contracts" => {
            contract_fixture::initialize(Path::new(registry))?;
            Ok(br#"{"ok":true,"result":{"initialized":true},"version":"/manage/v1"}"#.to_vec())
        }
        [command, registry, evaluator] if command == "init-packs" => {
            pack_fixture::initialize(Path::new(registry), Path::new(evaluator))?;
            Ok(br#"{"ok":true,"result":{"initialized":true},"version":"/manage/v1"}"#.to_vec())
        }
        [command, management] if command == "recover-management-storage" => {
            Store::open(Path::new(management))?;
            Ok(br#"{"ok":true,"result":{"recovered":true},"version":"/manage/v1"}"#.to_vec())
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
                || (command == "v1-waiver" && extra.len() == 2)
                || (command == "v1-packs" && extra.len() == 2)
                || (command == "v1-policy" && extra.len() == 2) =>
        {
            let path = Path::new(credential_file);
            if !path.metadata().is_ok_and(|metadata| metadata.is_file()) {
                return Err(Error::Unauthorized);
            }
            let credential = fs::File::open(path).map_err(|_| Error::Unauthorized)?;
            let credential = read_bounded(credential, MAX_CREDENTIAL)?;
            let request = read_bounded(io::stdin().lock(), MAX_REQUEST)?;
            let fixture_identity = fixture::authority();
            let catalog = env::var_os("WRIT_MANAGEMENT_IDENTITY_DB")
                .map(|path| writ_policy_management_lab::identity::CatalogAuthority(path.into()));
            let authority: &dyn writ_policy_management_lab::Authenticator = match &catalog {
                Some(catalog) => catalog,
                None if authenticated => return Err(Error::Unauthorized),
                None => &fixture_identity,
            };
            let allow_self_publication = command == "v1-contracts"
                || command == "v1-packs"
                || (command == "v1" && !extra.is_empty());
            let request = match preflight_v1(
                authority,
                &credential,
                &request,
                allow_self_publication,
                command == "v1-policy",
            ) {
                Ok(request) => request,
                Err(response) => return Ok(response),
            };
            let read_lifecycle = request.reads_policy_lifecycle() || request.compares_publication();
            let records = if request.compares_recorded() {
                let path = env::var_os("WRIT_MANAGEMENT_TRANSITIONS").ok_or(Error::Unavailable)?;
                Some(
                    writ_policy_management_lab::comparison::RecordedTransitions::open(Path::new(
                        &path,
                    ))?,
                )
            } else {
                None
            };
            let read_draft = command == "v1" && request.reads_draft_metadata();
            let execute = |service: &Service<'_>| {
                service
                    .execute(&credential, request)
                    .map(|response| response.encode())
                    .unwrap_or_else(error_response)
            };
            let store = if read_draft
                || read_lifecycle
                || command == "v1-contracts"
                || command == "v1-packs"
            {
                Store::open_without_recovery(Path::new(db))?
            } else {
                Store::open(Path::new(db))?
            };
            let audit = match (
                env::var_os("WRIT_MANAGEMENT_AUDIT_SOURCE"),
                env::var_os("WRIT_MANAGEMENT_AUDIT_DB"),
            ) {
                (None, None) => None,
                (None, Some(path)) => Some(writ_policy_management_lab::audit::AuditReader::new(
                    Path::new(&path),
                )),
                (Some(mode), None) if mode == "draft-owner" => Some(
                    writ_policy_management_lab::audit::AuditReader::draft_owner(&store),
                ),
                _ => return Err(Error::Unavailable),
            };
            let mut service = Service::new(&store, Some(authority));
            if let Some(source) = &records {
                service = service.with_recorded_transitions(source);
            }
            if let Some(reader) = &audit {
                service = service.with_audit(reader);
            }
            let service = if allow_self_publication {
                service.allow_self_publication()
            } else {
                service
            };
            match extra {
                [registry] if command == "v1-contracts" => {
                    let registry = contract_fixture::open(Path::new(registry))?;
                    let service = service.with_contracts(&registry);
                    Ok(execute(&service))
                }
                [ledger] if command == "v1-evaluator" => {
                    let evaluator = evaluator_fixture::open(Path::new(ledger))?;
                    let service = service.with_evaluator(&evaluator);
                    Ok(execute(&service))
                }
                [contracts, evaluator] if command == "v1-packs" => {
                    let contracts = contract_fixture::open(Path::new(contracts))?;
                    let authority = pack_fixture::FixturePackAuthority::default();
                    let packs = PackRegistry::new(
                        &contracts,
                        Path::new(evaluator),
                        evaluator_fixture::pack_dependencies(),
                        &authority,
                    );
                    let service = service.with_packs(&packs).with_contracts(&contracts);
                    Ok(execute(&service))
                }
                [ledger, policy] if command == "v1-waiver" => {
                    let waiver = WaiverFixture::open(Path::new(ledger), Path::new(policy))?;
                    let service = service.with_waiver(&waiver);
                    Ok(execute(&service))
                }
                [registry, scenario] if command == "v1-policy" => {
                    let trust = fixture::FixtureTrust::default();
                    trust
                        .set_enforcement_scenario(scenario.to_str().ok_or(Error::InvalidRequest)?)
                        .map_err(|_| Error::InvalidRequest)?;
                    let registry = Registry::open_for_inspection(
                        Path::new(registry),
                        fixture::publication_scope().map_err(|_| Error::Unavailable)?,
                        trust,
                    )
                    .map_err(|_| Error::Unavailable)?;
                    let service = service.with_publication(&registry);
                    Ok(execute(&service))
                }
                [registry] => {
                    let open = if read_lifecycle {
                        Registry::open_for_inspection
                    } else {
                        Registry::open_for_administration
                    };
                    let registry = open(
                        Path::new(registry),
                        fixture::publication_scope().map_err(|_| Error::Unavailable)?,
                        fixture::FixtureTrust::default(),
                    )
                    .map_err(|_| Error::Unavailable)?
                    .with_management_source(store.clone());
                    let service = service.with_publication(&registry);
                    Ok(execute(&service))
                }
                _ => Ok(execute(&service)),
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
