#![allow(clippy::panic, clippy::unwrap_used)]

use std::{
    cell::Cell,
    sync::{
        atomic::{AtomicBool, AtomicUsize, Ordering},
        Barrier,
    },
};

use crate::contracts::CurrentContractApproval;

use super::*;

static DIR_ID: AtomicUsize = AtomicUsize::new(0);
const CONTRACT: &str = "tenant:lab:record-label";
const DRAFT: &str = "1.0.0";
const DAY: &str = "2026-11-01";
const AUTHOR: &str = "contract-author";
const PUBLISHER: &str = "contract-publisher";

struct Directory(PathBuf);

impl Directory {
    fn new() -> Self {
        let unique = DIR_ID.fetch_add(1, Ordering::Relaxed);
        let path = std::env::temp_dir().join(format!(
            "writ-contract-unit-{}-{unique}",
            std::process::id()
        ));
        fs::create_dir(&path).unwrap();
        Self(path)
    }

    fn path(&self) -> PathBuf {
        self.0.join("registry.sqlite")
    }
}

impl Drop for Directory {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[derive(Default)]
struct Authority {
    revoked: AtomicBool,
    revoke_on_verify: AtomicBool,
}

struct Approval<'a> {
    revoked: &'a AtomicBool,
    ceremony: Digest,
}

impl CurrentContractApproval for Approval<'_> {
    fn ceremony(&self) -> &Digest {
        &self.ceremony
    }

    fn still_valid(&self) -> bool {
        !self.revoked.load(Ordering::SeqCst)
    }
}

impl ContractAuthority for Authority {
    fn verify(
        &self,
        intent: &Value,
        presentation: &str,
    ) -> Result<Box<dyn CurrentContractApproval + '_>> {
        let (nonce, shown_digest) = presentation.split_once('|').ok_or(Error::Unauthorized)?;
        if intent.get("purpose").and_then(Value::as_str) != Some(CONTRACT_PURPOSE)
            || digest(intent)?.as_str() != shown_digest
            || self.revoked.load(Ordering::SeqCst)
        {
            return Err(Error::Unauthorized);
        }
        if self.revoke_on_verify.load(Ordering::SeqCst) {
            self.revoked.store(true, Ordering::SeqCst);
        }
        Ok(Box::new(Approval {
            revoked: &self.revoked,
            ceremony: Digest::of_bytes(nonce.as_bytes()),
        }))
    }

    fn now(&self) -> Result<Timestamp> {
        Ok(Timestamp::parse_strict("2026-11-01T09:00:00Z").unwrap())
    }
}

fn scope() -> Scope {
    Scope {
        tenant: "lab".into(),
        project: "m6".into(),
    }
}

fn owner<'a>(
    directory: &Directory,
    dependencies: Option<&[ContractDependency]>,
    authority: &'a Authority,
    allow_self: bool,
) -> ContractRegistry<'a> {
    ContractRegistry::create(
        &directory.path(),
        scope(),
        &["mapping-pack".into()],
        dependencies,
        authority,
        allow_self,
        ContractValidator::from_fixture_environment().unwrap(),
    )
    .unwrap()
}

fn map<const N: usize>(members: [(&str, Value); N]) -> Map {
    members
        .into_iter()
        .map(|(key, value)| (key.to_string(), value))
        .collect()
}

fn act(
    owner: &ContractRegistry<'_>,
    subject: &str,
    operation: ContractOperation,
    fields: Map,
) -> Result<Value> {
    owner.execute(
        &ContractCall {
            scope: &scope(),
            subject,
            operation,
            reauthorize: &|| Ok(()),
        },
        operation,
        fields,
    )
}

fn schema(property_type: &str) -> Value {
    json::obj([
        (
            "$schema",
            json::s("https://json-schema.org/draft/2020-12/schema"),
        ),
        ("type", json::s("object")),
        ("additionalProperties", Value::Bool(false)),
        (
            "properties",
            json::obj([(
                "field/label~id",
                json::obj([("type", json::s(property_type))]),
            )]),
        ),
    ])
}

fn draft_fields(id: &str, version: &str, expected: i64, key: &str, schema: Value) -> Map {
    map([
        ("contract_id", json::s(id)),
        ("version", json::s(version)),
        ("schema", schema),
        ("expected_revision", Value::Int(expected)),
        ("idempotency_key", json::s(key)),
    ])
}

fn publish_fields(id: &str, version: &str, expected: i64) -> Map {
    map([
        ("contract_id", json::s(id)),
        ("version", json::s(version)),
        ("expected_revision", Value::Int(expected)),
    ])
}

fn preview(
    owner: &ContractRegistry<'_>,
    subject: &str,
    change: Change,
    fields: &Map,
) -> Result<Value> {
    let mut fields = fields.clone();
    fields.insert("change".into(), json::s(change.name()));
    act(owner, subject, ContractOperation::Preview, fields)
}

fn approved_fields(mut fields: Map, preview: &Value, key: &str, nonce: &str) -> Map {
    let digest = preview.get("intent_digest").unwrap().as_str().unwrap();
    fields.insert("idempotency_key".into(), json::s(key));
    fields.insert("intent_digest".into(), json::s(digest));
    fields.insert("presentation".into(), json::s(&format!("{nonce}|{digest}")));
    if let Some(confirmation) = preview
        .get("intent")
        .unwrap()
        .get("confirmation")
        .unwrap()
        .as_str()
    {
        fields.insert("confirmation".into(), json::s(confirmation));
    }
    fields
}

fn create(owner: &ContractRegistry<'_>, version: &str, expected: i64, schema: Value) -> Value {
    act(
        owner,
        AUTHOR,
        ContractOperation::Create,
        draft_fields(
            CONTRACT,
            version,
            expected,
            &format!("draft-{version}"),
            schema,
        ),
    )
    .unwrap()
}

fn commit(
    owner: &ContractRegistry<'_>,
    change: Change,
    operation: ContractOperation,
    fields: Map,
    key: &str,
) -> Value {
    let intent = preview(owner, PUBLISHER, change, &fields).unwrap();
    let fields = approved_fields(fields, &intent, key, key);
    act(owner, PUBLISHER, operation, fields).unwrap()
}

fn publish(owner: &ContractRegistry<'_>, version: &str, expected: i64) -> Value {
    commit(
        owner,
        Change::Publish,
        ContractOperation::Publish,
        publish_fields(CONTRACT, version, expected),
        &format!("publish-{version}"),
    )
}

fn deprecate(owner: &ContractRegistry<'_>) -> Value {
    let mut request = publish_fields(CONTRACT, DRAFT, 4);
    request.insert("replacement_version".into(), json::s("1.1.0"));
    request.insert("end_date".into(), json::s(DAY));
    commit(
        owner,
        Change::Deprecate,
        ContractOperation::Deprecate,
        request,
        "deprecate",
    )
}

fn inspect(owner: &ContractRegistry<'_>, target: &str) -> Value {
    act(
        owner,
        AUTHOR,
        ContractOperation::Inspect,
        map([("contract_id", json::s(target))]),
    )
    .unwrap()
}

fn prepare_retirement(owner: &ContractRegistry<'_>) {
    create(owner, DRAFT, 0, schema("string"));
    publish(owner, DRAFT, 1);
    create(owner, "1.1.0", 2, schema("string"));
    publish(owner, "1.1.0", 3);
    deprecate(owner);
}

#[test]
fn append_only_lifecycle_rolls_back_failed_storage_and_recovers_exact_receipt() {
    let directory = Directory::new();
    let authority = Authority::default();
    let registry = owner(&directory, Some(&[]), &authority, false);
    let db = Connection::open(directory.path()).unwrap();
    db.execute_batch(
        "CREATE TRIGGER reject_audit BEFORE INSERT ON contract_audit
         BEGIN SELECT RAISE(ABORT, 'injected audit failure'); END;",
    )
    .unwrap();
    let draft = draft_fields(CONTRACT, DRAFT, 0, "draft-1.0.0", schema("string"));
    assert_eq!(
        act(&registry, AUTHOR, ContractOperation::Create, draft),
        Err(Error::Unavailable)
    );
    assert_eq!(
        db.query_row("SELECT count(*) FROM contract_versions", [], |row| row
            .get::<_, i64>(0))
            .unwrap(),
        0
    );
    db.execute_batch("DROP TRIGGER reject_audit").unwrap();
    drop(db);
    prepare_retirement(&registry);

    let fields = publish_fields(CONTRACT, DRAFT, 5);
    let preview = preview(&registry, PUBLISHER, Change::Retire, &fields).unwrap();
    let fields = approved_fields(fields, &preview, "retire", "retire-nonce");
    let before = inspect(&registry, CONTRACT);
    assert_eq!(*before.get("revision").unwrap(), Value::Int(5));

    let mut bad_confirmation = fields.clone();
    bad_confirmation.insert("confirmation".into(), json::s("retire anyone"));
    assert_eq!(
        act(
            &registry,
            PUBLISHER,
            ContractOperation::Retire,
            bad_confirmation
        ),
        Err(Error::InvalidRequest)
    );

    let db = Connection::open(directory.path()).unwrap();
    assert!(db
        .execute("UPDATE contract_versions SET schema = '{}'", [])
        .is_err());
    db.execute_batch(
        "CREATE TRIGGER reject_audit BEFORE INSERT ON contract_audit
         BEGIN SELECT RAISE(ABORT, 'injected audit failure'); END;",
    )
    .unwrap();
    assert_eq!(
        act(
            &registry,
            PUBLISHER,
            ContractOperation::Retire,
            fields.clone()
        ),
        Err(Error::Unavailable)
    );
    assert_eq!(inspect(&registry, CONTRACT), before);
    db.execute_batch("DROP TRIGGER reject_audit").unwrap();
    drop(db);

    let committed = act(
        &registry,
        PUBLISHER,
        ContractOperation::Retire,
        fields.clone(),
    )
    .unwrap();
    assert_eq!(*committed.get("revision").unwrap(), Value::Int(6));
    drop(registry);

    let path = directory.path();
    let relocated = directory.0.join("unavailable.sqlite");
    fs::rename(&path, &relocated).unwrap();
    assert!(matches!(
        ContractRegistry::open(
            &path,
            scope(),
            &authority,
            false,
            ContractValidator::from_fixture_environment().unwrap()
        ),
        Err(Error::Unavailable)
    ));
    fs::rename(&relocated, &path).unwrap();

    let reopened = ContractRegistry::open(
        &path,
        scope(),
        &authority,
        false,
        ContractValidator::from_fixture_environment().unwrap(),
    )
    .unwrap();
    let after = inspect(&reopened, CONTRACT);
    let versions = after.get("versions").unwrap().as_arr().unwrap();
    assert_eq!(*versions[0].get("status").unwrap(), json::s("retired"));
    assert_eq!(*versions[0].get("end_date").unwrap(), json::s(DAY));
    assert_eq!(
        *versions[0].get("replacement_version").unwrap(),
        json::s("1.1.0")
    );
    assert_eq!(
        after.get("history").unwrap().as_arr().unwrap().last(),
        Some(&committed)
    );
    authority.revoked.store(true, Ordering::SeqCst);
    assert_eq!(
        act(
            &reopened,
            PUBLISHER,
            ContractOperation::Retire,
            fields.clone()
        ),
        Ok(committed.clone())
    );
    let recover = map([("idempotency_key", json::s("retire"))]);
    assert_eq!(
        act(
            &reopened,
            PUBLISHER,
            ContractOperation::Recover,
            recover.clone()
        ),
        Ok(committed)
    );
    assert_eq!(
        act(&reopened, AUTHOR, ContractOperation::Recover, recover),
        Err(Error::NotFound)
    );
    let mut changed = fields;
    changed.insert("presentation".into(), json::s("different"));
    assert_eq!(
        act(&reopened, PUBLISHER, ContractOperation::Retire, changed),
        Err(Error::IdempotencyConflict)
    );
}

#[test]
fn owning_service_refuses_core_ids_and_wrong_namespaces() {
    let directory = Directory::new();
    let authority = Authority::default();
    let registry = owner(&directory, Some(&[]), &authority, false);

    for (id, expected) in [
        ("decision", Error::ProtectedContract),
        ("tenant:lab:DeCision", Error::ProtectedContract),
        ("pack:mapping-pack:approvalgrant", Error::ProtectedContract),
        ("tenant:other:record-label", Error::NotFound),
        ("pack:other:record-label", Error::NotFound),
        ("tenant:lab:bad/path", Error::InvalidRequest),
    ] {
        assert_eq!(
            act(
                &registry,
                AUTHOR,
                ContractOperation::Create,
                draft_fields(id, DRAFT, 0, "create", schema("string"))
            ),
            Err(expected),
            "{id}"
        );
    }
    for (change, operation) in [
        (Change::Publish, ContractOperation::Publish),
        (Change::Deprecate, ContractOperation::Deprecate),
        (Change::Retire, ContractOperation::Retire),
    ] {
        let mut fields = publish_fields("decision", DRAFT, 0);
        if change == Change::Deprecate {
            fields.insert("replacement_version".into(), json::s("2.0.0"));
            fields.insert("end_date".into(), json::s(DAY));
        }
        assert_eq!(
            preview(&registry, PUBLISHER, change, &fields),
            Err(Error::ProtectedContract)
        );
        fields.insert(
            "intent_digest".into(),
            json::s(Digest::of_bytes(b"irrelevant").as_str()),
        );
        fields.insert("presentation".into(), json::s("irrelevant"));
        fields.insert("idempotency_key".into(), json::s("mutate"));
        if change.destructive() {
            fields.insert("confirmation".into(), json::s("irrelevant"));
        }
        assert_eq!(
            act(&registry, PUBLISHER, operation, fields),
            Err(Error::ProtectedContract)
        );
    }

    let pack = "pack:mapping-pack:record-label";
    let draft = draft_fields(pack, DRAFT, 0, "create", schema("string"));
    let first = act(&registry, AUTHOR, ContractOperation::Create, draft.clone()).unwrap();
    assert_eq!(
        act(&registry, AUTHOR, ContractOperation::Create, draft),
        Ok(first)
    );
    assert_eq!(
        *inspect(&registry, "decision")
            .get("versions")
            .unwrap()
            .as_arr()
            .unwrap()[0]
            .get("status")
            .unwrap(),
        json::s("core")
    );

    let mut wrong = scope();
    wrong.project = "other".into();
    let call = ContractCall {
        scope: &wrong,
        subject: AUTHOR,
        operation: ContractOperation::Browse,
        reauthorize: &|| Ok(()),
    };
    assert_eq!(
        registry.execute(&call, ContractOperation::Browse, Map::new()),
        Err(Error::NotFound)
    );
    assert_eq!(
        registry.execute(&call, ContractOperation::Create, Map::new()),
        Err(Error::Unauthorized)
    );
    assert!(matches!(
        ContractRegistry::open(
            &directory.path(),
            wrong,
            &authority,
            false,
            ContractValidator::from_fixture_environment().unwrap()
        ),
        Err(Error::NotFound)
    ));
}

#[test]
fn retirement_requires_complete_dependency_evidence_and_no_active_dependents() {
    for (dependencies, error) in [
        (None, Error::Unavailable),
        (
            Some(vec![ContractDependency {
                contract_id: CONTRACT.into(),
                version: DRAFT.into(),
                kind: "integration".into(),
                owner: "fixture-integration".into(),
            }]),
            Error::ActiveDependents,
        ),
    ] {
        let directory = Directory::new();
        let authority = Authority::default();
        let registry = owner(&directory, dependencies.as_deref(), &authority, false);
        prepare_retirement(&registry);

        let fields = publish_fields(CONTRACT, DRAFT, 5);
        assert_eq!(
            preview(&registry, PUBLISHER, Change::Retire, &fields),
            Err(error)
        );
        let mut forged = fields;
        forged.insert("idempotency_key".into(), json::s("retire"));
        forged.insert(
            "intent_digest".into(),
            json::s(Digest::of_bytes(b"test").as_str()),
        );
        forged.insert("presentation".into(), json::s("test"));
        forged.insert(
            "confirmation".into(),
            json::s(&format!("retire {CONTRACT}@{DRAFT}")),
        );
        assert_eq!(
            act(&registry, PUBLISHER, ContractOperation::Retire, forged),
            Err(error)
        );

        let inspection = inspect(&registry, CONTRACT);
        let versions = inspection.get("versions").unwrap().as_arr().unwrap();
        let dependents = versions[0].get("dependencies").unwrap();
        if dependencies.is_none() {
            assert_eq!(*dependents, Value::Null);
        } else {
            assert_eq!(
                dependents.as_arr().unwrap(),
                &[json::obj([
                    ("kind", json::s("integration")),
                    ("owner", json::s("fixture-integration")),
                ])]
            );
        }
        assert_eq!(versions[0].get("status"), Some(&json::s("deprecated")));
        assert_eq!(inspection.get("revision"), Some(&Value::Int(5)));
    }
}

#[test]
fn publication_is_immutable_and_breaking_changes_require_a_major_bump() {
    let directory = Directory::new();
    let authority = Authority::default();
    let registry = owner(&directory, Some(&[]), &authority, false);
    create(&registry, DRAFT, 0, schema("number"));
    publish(&registry, DRAFT, 1);
    create(&registry, "1.1.0", 2, schema("integer"));
    let minor = publish_fields(CONTRACT, "1.1.0", 3);
    assert_eq!(
        preview(&registry, PUBLISHER, Change::Publish, &minor),
        Err(Error::IncompatibleContract)
    );
    create(&registry, "2.0.0", 3, schema("integer"));

    let major = publish_fields(CONTRACT, "2.0.0", 4);
    let shown = preview(&registry, PUBLISHER, Change::Publish, &major).unwrap();
    let compatibility = shown.get("compatibility").unwrap();
    assert_eq!(compatibility.get("status"), Some(&json::s("breaking")));
    assert_eq!(compatibility.get("previous_version"), Some(&json::s(DRAFT)));
    assert_eq!(
        compatibility.get("changes").unwrap().as_arr().unwrap(),
        &[json::obj([
            ("code", json::s("TYPE_NARROWED")),
            ("pointer", json::s("/properties/field~1label~0id/type")),
        ])]
    );
    let receipt = act(
        &registry,
        PUBLISHER,
        ContractOperation::Publish,
        approved_fields(major, &shown, "major", "major-ceremony"),
    )
    .unwrap();
    assert_eq!(receipt.get("compatibility"), Some(compatibility));
    assert_eq!(
        act(
            &registry,
            AUTHOR,
            ContractOperation::Create,
            draft_fields(CONTRACT, "2.0.0", 5, "overwrite", schema("boolean")),
        ),
        Err(Error::RevisionConflict)
    );
    assert_eq!(
        preview(
            &registry,
            PUBLISHER,
            Change::Publish,
            &publish_fields(CONTRACT, "2.0.0", 5)
        ),
        Err(Error::RevisionConflict)
    );
    let mut schema_with_unknown_change = schema("integer");
    if let Value::Obj(fields) = &mut schema_with_unknown_change {
        fields.insert("const".into(), json::obj([]));
    }
    create(&registry, "3.0.0", 5, schema_with_unknown_change);
    assert_eq!(
        preview(
            &registry,
            PUBLISHER,
            Change::Publish,
            &publish_fields(CONTRACT, "3.0.0", 6)
        ),
        Err(Error::IncompatibleContract)
    );
}

#[test]
fn exact_human_intent_is_single_use_and_authorization_is_rechecked_at_commit() {
    let directory = Directory::new();
    let authority = Authority::default();
    let registry = owner(&directory, Some(&[]), &authority, true);
    create(&registry, DRAFT, 0, schema("string"));
    let mut fields = publish_fields(CONTRACT, DRAFT, 1);
    assert_eq!(
        preview(&registry, AUTHOR, Change::Publish, &fields),
        Err(Error::Unauthorized)
    );
    fields.insert("acknowledge_self_publication".into(), Value::Bool(true));
    let shown = preview(&registry, AUTHOR, Change::Publish, &fields).unwrap();
    let fields = approved_fields(fields, &shown, "publish-first", "ceremony-first");
    let mut substituted = fields.clone();
    substituted.insert(
        "presentation".into(),
        json::s("ceremony-first|other-purpose"),
    );
    assert_eq!(
        act(&registry, AUTHOR, ContractOperation::Publish, substituted),
        Err(Error::Unauthorized)
    );

    authority.revoke_on_verify.store(true, Ordering::SeqCst);
    assert_eq!(
        act(
            &registry,
            AUTHOR,
            ContractOperation::Publish,
            fields.clone()
        ),
        Err(Error::Unauthorized)
    );
    assert_eq!(
        inspect(&registry, CONTRACT).get("revision"),
        Some(&Value::Int(1))
    );
    authority.revoke_on_verify.store(false, Ordering::SeqCst);
    authority.revoked.store(false, Ordering::SeqCst);

    let calls = Cell::new(0);
    let call = ContractCall {
        scope: &scope(),
        subject: AUTHOR,
        operation: ContractOperation::Publish,
        reauthorize: &|| {
            calls.set(calls.get() + 1);
            if calls.get() > 2 {
                Err(Error::Unauthorized)
            } else {
                Ok(())
            }
        },
    };
    assert_eq!(
        registry.execute(&call, ContractOperation::Publish, fields.clone()),
        Err(Error::Unauthorized)
    );
    assert_eq!(
        inspect(&registry, CONTRACT).get("revision"),
        Some(&Value::Int(1))
    );
    let receipt = act(&registry, AUTHOR, ContractOperation::Publish, fields).unwrap();
    assert_eq!(
        receipt.get("acknowledge_self_publication"),
        Some(&Value::Bool(true))
    );

    create(&registry, "1.1.0", 2, schema("string"));
    let second = publish_fields(CONTRACT, "1.1.0", 3);
    let shown = preview(&registry, PUBLISHER, Change::Publish, &second).unwrap();
    let second = approved_fields(second, &shown, "publish-second", "ceremony-first");
    assert_eq!(
        act(&registry, PUBLISHER, ContractOperation::Publish, second),
        Err(Error::Unauthorized)
    );
    assert_eq!(
        inspect(&registry, CONTRACT).get("revision"),
        Some(&Value::Int(3))
    );
}

#[test]
fn concurrent_mutations_at_one_revision_can_commit_only_once() {
    let directory = Directory::new();
    let authority = Authority::default();
    let registry = owner(&directory, Some(&[]), &authority, false);
    let rendezvous = Barrier::new(2);
    std::thread::scope(|thread| {
        let contenders: Vec<_> = ["tenant:lab:racing-one", "tenant:lab:racing-two"]
            .into_iter()
            .map(|id| {
                let path = directory.path();
                let rendezvous = &rendezvous;
                let authority = &authority;
                thread.spawn(move || {
                    let contender = ContractRegistry::open(
                        &path,
                        scope(),
                        authority,
                        false,
                        ContractValidator::from_fixture_environment().unwrap(),
                    )
                    .unwrap();
                    rendezvous.wait();
                    act(
                        &contender,
                        AUTHOR,
                        ContractOperation::Create,
                        draft_fields(
                            id,
                            DRAFT,
                            0,
                            id.rsplit(':').next().unwrap(),
                            schema("string"),
                        ),
                    )
                })
            })
            .collect();
        let outcomes: Vec<_> = contenders
            .into_iter()
            .map(|join| join.join().unwrap())
            .collect();
        assert_eq!(outcomes.iter().filter(|result| result.is_ok()).count(), 1);
        assert_eq!(
            outcomes
                .iter()
                .filter(|result| **result == Err(Error::RevisionConflict))
                .count(),
            1
        );
    });
    let visible = act(&registry, AUTHOR, ContractOperation::Browse, Map::new()).unwrap();
    assert_eq!(visible.get("revision"), Some(&Value::Int(1)));
    assert_eq!(
        visible
            .get("contracts")
            .unwrap()
            .as_arr()
            .unwrap()
            .iter()
            .filter(|contract| contract.get("status") == Some(&json::s("draft")))
            .count(),
        1
    );
}

fn leave_hot_journal(path: &Path) -> PathBuf {
    let executable =
        std::env::var_os("WRIT_CONTRACT_PYTHON").unwrap_or_else(|| "/usr/bin/python3".into());
    let result = std::process::Command::new(executable)
        .args([
            "-I",
            "-c",
            "import os, sqlite3, sys
db = sqlite3.connect(sys.argv[1])
db.execute('CREATE TABLE sentinel (content BLOB)')
db.executemany('INSERT INTO sentinel VALUES (?)', [(b'A' * 8192,)] * 32)
db.commit()
db.execute('PRAGMA cache_size = 1')
db.execute('BEGIN IMMEDIATE')
db.execute('UPDATE sentinel SET content = ?', (b'B' * 8192,))
os._exit(0)",
        ])
        .arg(path)
        .status()
        .unwrap();
    assert!(result.success());
    let mut journal = path.as_os_str().to_os_string();
    journal.push("-journal");
    let journal = PathBuf::from(journal);
    assert_eq!(
        &fs::read(&journal).unwrap()[..8],
        &[0xd9, 0xd5, 0x05, 0xf9, 0x20, 0xa1, 0x63, 0xd7]
    );
    journal
}

#[test]
fn ordinary_open_never_recovers_a_hot_journal_and_only_owns_recovery_for_its_format() {
    let foreign = Directory::new();
    let foreign_path = foreign.path();
    let foreign_journal = leave_hot_journal(&foreign_path);
    let foreign_before = fs::read(&foreign_path).unwrap();
    let foreign_journal_before = fs::read(&foreign_journal).unwrap();
    let authority = Authority::default();

    assert!(matches!(
        ContractRegistry::open(
            &foreign_path,
            scope(),
            &authority,
            false,
            ContractValidator::from_fixture_environment().unwrap()
        ),
        Err(Error::Unavailable)
    ));
    assert_eq!(
        ContractRegistry::recover_storage(&foreign_path, &scope()),
        Err(Error::Unavailable)
    );
    assert_eq!(fs::read(&foreign_path).unwrap(), foreign_before);
    assert_eq!(fs::read(&foreign_journal).unwrap(), foreign_journal_before);

    let directory = Directory::new();
    let registry = owner(&directory, Some(&[]), &authority, false);
    let committed = create(&registry, DRAFT, 0, schema("string"));
    let own_path = directory.path();
    let own_journal = leave_hot_journal(&own_path);
    let owner_before = fs::read(&own_path).unwrap();
    let owner_journal_before = fs::read(&own_journal).unwrap();
    assert_eq!(
        preview(
            &registry,
            PUBLISHER,
            Change::Publish,
            &publish_fields(CONTRACT, DRAFT, 1)
        ),
        Err(Error::Unavailable)
    );
    assert!(matches!(
        ContractRegistry::open(
            &own_path,
            scope(),
            &authority,
            false,
            ContractValidator::from_fixture_environment().unwrap()
        ),
        Err(Error::Unavailable)
    ));
    assert_eq!(fs::read(&own_path).unwrap(), owner_before);
    assert_eq!(fs::read(&own_journal).unwrap(), owner_journal_before);

    ContractRegistry::recover_storage(&own_path, &scope()).unwrap();
    assert!(!own_journal.exists());
    let recovered = ContractRegistry::open(
        &own_path,
        scope(),
        &authority,
        false,
        ContractValidator::from_fixture_environment().unwrap(),
    )
    .unwrap();
    assert_eq!(
        act(
            &recovered,
            AUTHOR,
            ContractOperation::Recover,
            map([("idempotency_key", json::s("draft-1.0.0"))])
        ),
        Ok(committed)
    );
    let clean = Connection::open(&own_path).unwrap();
    let value: Vec<u8> = clean
        .query_row("SELECT content FROM sentinel LIMIT 1", [], |row| row.get(0))
        .unwrap();
    assert_eq!(value, vec![b'A'; 8192]);
}

#[test]
fn deprecated_versions_keep_their_replacements_available_until_retirement() {
    let directory = Directory::new();
    let authority = Authority::default();
    let registry = owner(&directory, Some(&[]), &authority, false);
    prepare_retirement(&registry);
    create(&registry, "1.2.0", 5, schema("string"));
    publish(&registry, "1.2.0", 6);
    let mut next = publish_fields(CONTRACT, "1.1.0", 7);
    next.insert("replacement_version".into(), json::s("1.2.0"));
    next.insert("end_date".into(), json::s(DAY));
    commit(
        &registry,
        Change::Deprecate,
        ContractOperation::Deprecate,
        next,
        "deprecate-next",
    );

    assert_eq!(
        preview(
            &registry,
            PUBLISHER,
            Change::Retire,
            &publish_fields(CONTRACT, "1.1.0", 8)
        ),
        Err(Error::ActiveDependents)
    );
    commit(
        &registry,
        Change::Retire,
        ContractOperation::Retire,
        publish_fields(CONTRACT, DRAFT, 8),
        "retire-original",
    );
    commit(
        &registry,
        Change::Retire,
        ContractOperation::Retire,
        publish_fields(CONTRACT, "1.1.0", 9),
        "retire-next",
    );
    assert_eq!(
        inspect(&registry, CONTRACT).get("revision"),
        Some(&Value::Int(10))
    );
}
