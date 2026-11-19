use super::*;
use std::{sync::mpsc, time::Duration};
use writ_policy_management_lab::{
    contracts::CurrentContractApproval,
    packs::{CurrentPackPublisher, PackAuthority},
    Result, Scope,
};

fn recover_request(key: &str) -> Value {
    request(
        "packs.recover",
        &json::obj([("idempotency_key", json::s(key))]),
    )
}

fn assert_recovery(lab: &Lab, key: &str, commit: &Value, response: &Value) {
    assert_eq!(lab.api(PUBLISHER, &recover_request(key)), *response);
    assert_eq!(lab.cli(PUBLISHER, &recover_request(key)), *response);
    assert_eq!(lab.api(PUBLISHER, commit), *response);
    assert_eq!(lab.cli(PUBLISHER, commit), *response);
}

fn assert_history(lab: &Lab, revision: i64, active: Option<&str>, receipts: &[&Value]) {
    assert_eq!(
        lab.history(PACK),
        json::obj([
            ("revision", Value::Int(revision)),
            ("active_version", active.map_or(Value::Null, json::s)),
            (
                "history",
                Value::Arr(
                    receipts
                        .iter()
                        .map(|response| result(response).clone())
                        .collect()
                ),
            ),
        ]),
    );
}

fn install_first(lab: &Lab, contract: &str) -> Value {
    let evaluator = lab.publish_evaluator();
    lab.install(
        "1.0.0",
        0,
        lab.sign(&payload(PACK, "1.0.0", contract, &evaluator)),
        "install-first",
    )
}

fn enable_request(lab: &Lab, revision: i64) -> Value {
    lab.pack_request(
        &lab.pack_preview("enable", "1.0.0", revision, None),
        "enable",
        "enable-first",
    )
}

struct PausedApproval<'a> {
    authority: &'a FixturePackAuthority,
    reached: mpsc::Sender<()>,
    resume: mpsc::Receiver<()>,
}

impl PackAuthority for PausedApproval<'_> {
    fn now(&self) -> Result<Timestamp> {
        self.authority.now()
    }

    fn publisher(
        &self,
        scope: &Scope,
        pack_id: &str,
        publisher: &str,
        key_id: &str,
        message: &[u8],
        signature: &str,
    ) -> Result<Box<dyn CurrentPackPublisher + '_>> {
        self.authority
            .publisher(scope, pack_id, publisher, key_id, message, signature)
    }

    fn approve(
        &self,
        intent: &Value,
        presentation: &str,
    ) -> Result<Box<dyn CurrentContractApproval + '_>> {
        let approval = self.authority.approve(intent, presentation)?;
        self.reached.send(()).unwrap();
        self.resume.recv_timeout(Duration::from_secs(10)).unwrap();
        Ok(approval)
    }
}

fn api_with_pack_authority(lab: &Lab, request: &Value, trust: &dyn PackAuthority) -> Value {
    let store = Store::open_without_recovery(&lab.store).unwrap();
    let contracts = contract_fixture::open(&lab.contracts).unwrap();
    let packs = PackRegistry::new(
        &contracts,
        &lab.evaluator,
        evaluator_fixture::pack_dependencies(),
        trust,
    );
    let authority = fixture::authority();
    let service = Service::new(&store, Some(&authority)).with_packs(&packs);
    json::parse(&execute_v1_with_service(
        &service,
        PUBLISHER.as_bytes(),
        json::canonical(request).unwrap().as_bytes(),
    ))
    .unwrap()
}

fn assert_writer_reserved(path: &Path) {
    let connection = Connection::open(path).unwrap();
    connection.busy_timeout(Duration::ZERO).unwrap();
    let failure = connection.execute_batch("BEGIN IMMEDIATE").unwrap_err();
    assert_eq!(
        failure.sqlite_error_code(),
        Some(rusqlite::ErrorCode::DatabaseBusy)
    );
}

fn enable_before_dependency(
    lab: &Lab,
    enable: &Value,
    dependency_path: &Path,
    dependency: &Value,
) -> (Value, Value) {
    let start = Barrier::new(2);
    thread::scope(|scope| {
        let (reached, paused) = mpsc::channel();
        let (resume, wait_for_resume) = mpsc::channel();
        let pack_worker = scope.spawn(move || {
            let authority = PausedApproval {
                authority: &lab.trust,
                reached,
                resume: wait_for_resume,
            };
            api_with_pack_authority(lab, enable, &authority)
        });
        paused.recv_timeout(Duration::from_secs(10)).unwrap();
        assert_writer_reserved(dependency_path);
        let (finished, pending) = mpsc::channel();
        let start_dependency = &start;
        let dependency_worker = scope.spawn(move || {
            start_dependency.wait();
            let response = lab.api(PUBLISHER, dependency);
            finished.send(()).unwrap();
            response
        });
        start.wait();
        assert_eq!(
            pending.recv_timeout(Duration::from_millis(100)),
            Err(mpsc::RecvTimeoutError::Timeout),
        );
        resume.send(()).unwrap();
        (
            pack_worker.join().unwrap(),
            dependency_worker.join().unwrap(),
        )
    })
}

#[test]
fn contract_retirement_and_pack_enable_follow_owner_order() {
    for enable_first in [true, false] {
        let lab = Lab::new();
        let contract = lab.publish_contract("1.0.0", 0);
        lab.publish_contract("2.0.0", 2);
        lab.deprecate_contract();
        let install = install_first(&lab, &contract);
        let retirement = lab.contract_request(
            "retire",
            &Lab::contract_fields("1.0.0", 5),
            "retire-contract",
        );
        let enable = enable_request(&lab, 1);
        if enable_first {
            let (enabled, refused) =
                enable_before_dependency(&lab, &enable, &lab.contracts, &retirement);
            assert_eq!(code(&refused), Some("ACTIVE_DEPENDENTS"), "{refused:?}");
            assert_history(&lab, 2, Some("1.0.0"), &[&install, &enabled]);
            assert_recovery(&lab, "enable-first", &enable, &enabled);
        } else {
            result(&lab.api(PUBLISHER, &retirement));
            let refused = lab.cli(PUBLISHER, &enable);
            assert_eq!(code(&refused), Some("INVALID_PACK"), "{refused:?}");
            assert_eq!(
                code(&lab.api(PUBLISHER, &recover_request("enable-first"))),
                Some("NOT_FOUND"),
            );
            assert_history(&lab, 1, None, &[&install]);
        }
    }
}

#[test]
fn evaluator_revocation_and_pack_enable_follow_owner_order() {
    for enable_first in [true, false] {
        let lab = Lab::new();
        let contract = lab.publish_contract("1.0.0", 0);
        let install = install_first(&lab, &contract);
        let revoke = lab.revoke_evaluator();
        let enable = enable_request(&lab, 1);
        if enable_first {
            let (enabled, revoked) =
                enable_before_dependency(&lab, &enable, &lab.evaluator, &revoke);
            result(&revoked);
            let verify = request("packs.verify", &target("1.0.0"));
            assert_eq!(code(&lab.cli(VIEWER, &verify)), Some("INVALID_PACK"));
            assert_history(&lab, 2, Some("1.0.0"), &[&install, &enabled]);
            assert_recovery(&lab, "enable-first", &enable, &enabled);
        } else {
            result(&lab.api(PUBLISHER, &revoke));
            let refused = lab.cli(PUBLISHER, &enable);
            assert_eq!(code(&refused), Some("INVALID_PACK"), "{refused:?}");
            assert_eq!(
                code(&lab.api(PUBLISHER, &recover_request("enable-first"))),
                Some("NOT_FOUND"),
            );
            assert_history(&lab, 1, None, &[&install]);
        }
    }
}

#[test]
fn concurrent_upgrades_leave_one_exact_receipt() {
    let lab = Lab::new();
    let contract = lab.publish_contract("1.0.0", 0);
    let evaluator = lab.publish_evaluator();
    let mut receipts = Vec::new();
    let versions = ["1.0.0", "2.0.0", "3.0.0"];
    for (revision, version) in (0..).zip(versions) {
        let envelope = lab.sign(&payload(PACK, version, &contract, &evaluator));
        receipts.push(lab.install(version, revision, envelope, &format!("install-{revision}")));
    }
    let enable = enable_request(&lab, 3);
    receipts.push(lab.api(PUBLISHER, &enable));
    let keys = ["upgrade-second", "upgrade-third"];
    let upgrades = [versions[1], versions[2]]
        .into_iter()
        .zip(keys)
        .map(|(version, key)| {
            lab.pack_request(
                &lab.pack_preview("upgrade", version, 4, None),
                "upgrade",
                key,
            )
        })
        .collect::<Vec<_>>();

    let start = Barrier::new(3);
    let responses = thread::scope(|scope| {
        let first = scope.spawn(|| {
            start.wait();
            lab.api(PUBLISHER, &upgrades[0])
        });
        let second = scope.spawn(|| {
            start.wait();
            lab.cli(PUBLISHER, &upgrades[1])
        });
        start.wait();
        [first.join().unwrap(), second.join().unwrap()]
    });
    let winner = usize::from(code(&responses[0]).is_some());
    let loser = 1 - winner;
    let receipt = result(&responses[winner]);
    let active = versions[winner + 1];
    assert_eq!(receipt.get("pack_version"), Some(&json::s(active)));
    assert_eq!(
        receipt.get("previous_active_version"),
        Some(&json::s("1.0.0"))
    );
    assert_eq!(receipt.get("revision"), Some(&Value::Int(5)));
    assert_eq!(code(&responses[loser]), Some("REVISION_CONFLICT"));
    assert_eq!(
        code(&lab.api(PUBLISHER, &recover_request(keys[loser]))),
        Some("NOT_FOUND"),
    );
    assert_recovery(&lab, keys[winner], &upgrades[winner], &responses[winner]);
    receipts.push(responses[winner].clone());
    assert_history(&lab, 5, Some(active), &receipts.iter().collect::<Vec<_>>());
}

#[test]
fn pack_storage_and_audit_failures_leave_no_authoritative_receipt() {
    let lab = Lab::new();
    let contract = lab.publish_contract("1.0.0", 0);
    let install = install_first(&lab, &contract);
    let preview = lab.pack_preview("enable", "1.0.0", 1, None);
    let db = Connection::open(&lab.contracts).unwrap();
    let faults = [
        ("pack_selection", "enable-storage-fault"),
        ("pack_audit", "enable-audit-fault"),
    ];
    for (table, key) in faults {
        let commit = lab.pack_request(&preview, "enable", key);
        db.execute_batch(&format!(
            "CREATE TRIGGER injected_failure BEFORE INSERT ON {table}
             BEGIN SELECT RAISE(ABORT, 'fixture write failure'); END;",
        ))
        .unwrap();
        assert_eq!(code(&lab.cli(PUBLISHER, &commit)), Some("UNAVAILABLE"));
        assert_history(&lab, 1, None, &[&install]);
        db.execute_batch("DROP TRIGGER injected_failure").unwrap();

        // The management reservation survived. With no owning receipt, a retry cannot dispatch.
        assert_eq!(code(&lab.api(PUBLISHER, &commit)), Some("UNAVAILABLE"));
        let mut rekeyed = commit.clone();
        put(
            &mut rekeyed,
            "idempotency_key",
            json::s("rekeyed-uncertain"),
        );
        assert_eq!(code(&lab.api(PUBLISHER, &rekeyed)), Some("UNAUTHORIZED"));
        assert_eq!(
            code(&lab.cli(PUBLISHER, &recover_request(key))),
            Some("UNAVAILABLE"),
        );
        assert_history(&lab, 1, None, &[&install]);
    }
    let key = "enable-reapproved";
    let commit = lab.pack_request(&preview, "enable", key);
    let management = Connection::open(&lab.store).unwrap();
    management
        .execute_batch(
            "CREATE TRIGGER injected_failure BEFORE INSERT ON pack_lifecycle_intents
             BEGIN SELECT RAISE(ABORT, 'fixture reservation failure'); END;",
        )
        .unwrap();
    assert_eq!(code(&lab.cli(PUBLISHER, &commit)), Some("UNAVAILABLE"));
    assert_eq!(
        code(&lab.api(PUBLISHER, &recover_request(key))),
        Some("NOT_FOUND"),
    );
    assert_history(&lab, 1, None, &[&install]);
    management
        .execute_batch("DROP TRIGGER injected_failure")
        .unwrap();
    let response = lab.api(PUBLISHER, &commit);
    let receipt = result(&response);
    let (stored, digest): (String, String) = db
        .query_row(
            "SELECT receipt, receipt_digest FROM pack_audit WHERE revision = 2",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .unwrap();
    assert_eq!(stored, json::canonical(receipt).unwrap());
    assert_eq!(digest, json::digest(receipt).unwrap().as_str());
    assert_recovery(&lab, key, &commit, &response);
    for (_, key) in faults {
        assert_eq!(
            code(&lab.api(PUBLISHER, &recover_request(key))),
            Some("UNAVAILABLE"),
        );
    }
    assert_history(&lab, 2, Some("1.0.0"), &[&install, &response]);
}
