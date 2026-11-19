use std::{fs::File, io::Read, path::Path};

use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use writ_broker::evaluator_publication::EvaluatorDependencies;
use writ_kernel::{
    ids,
    json::{self, Digest, Map, Value},
};
use writ_ledger_sqlite::{EvaluatorPackSnapshot, EvaluatorRegistryReader};

use crate::{
    contract_registry::ContractRegistry,
    packs::{
        digest, pack_id, scope_value, text, version, CurrentPackPublisher, Envelope, PackAuthority,
        PackBoundary, PackOperation, PACK_ADMIN_PURPOSE,
    },
    valid_label, Error, LifecycleCall, LifecycleKind, Result, Scope,
};

const MAX_ENVELOPE: usize = 24 * 1024;

pub struct PackRegistry<'a> {
    contracts: &'a ContractRegistry<'a>,
    evaluator: &'a Path,
    evaluator_dependencies: EvaluatorDependencies<'a>,
    authority: &'a dyn PackAuthority,
}

impl<'a> PackRegistry<'a> {
    pub fn new(
        contracts: &'a ContractRegistry<'a>,
        evaluator: &'a Path,
        evaluator_dependencies: EvaluatorDependencies<'a>,
        authority: &'a dyn PackAuthority,
    ) -> Self {
        Self {
            contracts,
            evaluator,
            evaluator_dependencies,
            authority,
        }
    }

    /// Direct owner calls still require the exact human approval and every
    /// owner-side eligibility check. The host supplies its current authorization.
    pub fn execute_owner(
        &self,
        scope: &Scope,
        subject: &str,
        reauthorize: &dyn Fn() -> Result<()>,
        operation: PackOperation,
        fields: Map,
    ) -> Result<Value> {
        self.execute_as(
            &Caller {
                scope,
                subject,
                reauthorize,
                management: None,
            },
            operation,
            fields,
        )
    }

    fn execute_as(
        &self,
        caller: &Caller<'_>,
        operation: PackOperation,
        mut fields: Map,
    ) -> Result<Value> {
        if caller.scope != self.contracts.pack_scope() {
            return Err(Error::NotFound);
        }
        if !ids::is_id(caller.subject) {
            return Err(Error::Unauthorized);
        }
        (caller.reauthorize)()?;
        match operation {
            PackOperation::Inspect | PackOperation::History => {
                let id = take_string(&mut fields, "pack_id")?;
                if !pack_id(&id) {
                    return Err(Error::InvalidRequest);
                }
                let mut db = self.contracts.pack_connection(false)?;
                let tx = db.transaction().map_err(unavailable)?;
                let result = if operation == PackOperation::Inspect {
                    let version = take_string(&mut fields, "pack_version")?;
                    finish(&fields)?;
                    if crate::packs::version(&version).is_none() {
                        return Err(Error::InvalidRequest);
                    }
                    json::obj([
                        ("revision", Value::Int(revision(&tx)?)),
                        ("active_version", nullable(active(&tx, &id)?)),
                        ("envelope", stored(&tx, &id, &version)?),
                    ])
                } else {
                    finish(&fields)?;
                    history(&tx, &id)?
                };
                (caller.reauthorize)()?;
                Ok(result)
            }
            PackOperation::Verify => {
                let target = Target::parse(&mut fields)?;
                finish(&fields)?;
                self.with_evaluator(false, |snapshot| {
                    let mut db = self.contracts.pack_connection(false)?;
                    let tx = db.transaction().map_err(unavailable)?;
                    let envelope = Envelope::parse(
                        stored(&tx, &target.id, &target.version)?,
                        caller.scope,
                        &target.id,
                        &target.version,
                    )?;
                    let publisher = self.verify(&tx, snapshot, caller.scope, &target, &envelope)?;
                    (caller.reauthorize)()?;
                    if !publisher.still_valid() {
                        return Err(Error::Unauthorized);
                    }
                    Ok(json::obj([
                        ("verified", Value::Bool(true)),
                        ("revision", Value::Int(revision(&tx)?)),
                        (
                            "envelope_digest",
                            json::s(digest(&envelope.value)?.as_str()),
                        ),
                    ]))
                })
            }
            PackOperation::Recover => {
                let key = Idempotency::parse(&mut fields)?;
                finish(&fields)?;
                let management = caller.management.ok_or(Error::InvalidRequest)?;
                let correlation = management.correlation(LifecycleKind::Pack, &key.raw)?;
                let db = self.contracts.pack_connection(false)?;
                recover(&db, caller, &key, &correlation)
            }
            PackOperation::Preview => {
                let op = PackOperation::change(&take_string(&mut fields, "operation")?)?;
                let change = Change::parse(op, &mut fields)?;
                finish(&fields)?;
                self.with_dependencies(op, false, |snapshot| {
                    let mut db = self.contracts.pack_connection(false)?;
                    let tx = db.transaction().map_err(unavailable)?;
                    let prepared = self.prepare(&tx, snapshot, caller, &change)?;
                    prepared.recheck(caller)?;
                    Ok(json::obj([
                        ("intent_digest", json::s(digest(&prepared.intent)?.as_str())),
                        ("intent", prepared.intent),
                    ]))
                })
            }
            _ => self.mutate(caller, operation, fields),
        }
    }

    fn mutate(
        &self,
        caller: &Caller<'_>,
        operation: PackOperation,
        mut fields: Map,
    ) -> Result<Value> {
        let binding = json::obj([
            ("operation", json::s(operation.name())),
            ("fields", Value::Obj(fields.clone())),
            ("managed", Value::Bool(caller.management.is_some())),
        ]);
        let request = digest(&binding)?;
        let change = Change::parse(operation, &mut fields)?;
        let key = Idempotency::parse(&mut fields)?;
        let shown = take_string(&mut fields, "intent_digest")?;
        let presentation = take_string(&mut fields, "presentation")?;
        let confirmation = if operation.destructive() {
            Some(take_string(&mut fields, "confirmation")?)
        } else {
            None
        };
        finish(&fields)?;
        if !ids::is_digest(&shown) || presentation.len() > 512 || presentation.is_empty() {
            return Err(Error::InvalidRequest);
        }

        let db = self.contracts.pack_connection(false)?;
        if let Some(receipt) = known_receipt(&db, caller, &key, request.as_str())? {
            return Ok(receipt);
        }
        drop(db);

        self.with_dependencies(operation, true, |snapshot| {
            let mut db = self.contracts.pack_connection(true)?;
            let tx = db
                .transaction_with_behavior(TransactionBehavior::Immediate)
                .map_err(unavailable)?;
            if let Some(receipt) = known_receipt(&tx, caller, &key, request.as_str())? {
                return Ok(receipt);
            }
            let prepared = self.prepare(&tx, snapshot, caller, &change)?;
            let intent = digest(&prepared.intent)?;
            if shown != intent.as_str() {
                return Err(Error::RevisionConflict);
            }
            if confirmation.as_deref()
                != prepared.intent.get("confirmation").and_then(Value::as_str)
            {
                return Err(Error::InvalidRequest);
            }
            let approval = self.authority.approve(&prepared.intent, &presentation)?;
            let ceremony = approval.ceremony();
            let consumed = tx
                .query_row(
                    "SELECT EXISTS(SELECT 1 FROM pack_audit WHERE ceremony = ?1)",
                    [ceremony.as_str()],
                    |row| row.get::<_, bool>(0),
                )
                .map_err(unavailable)?;
            if consumed {
                return Err(Error::Unauthorized);
            }
            prepared.recheck(caller)?;
            if !approval.still_valid() {
                return Err(Error::Unauthorized);
            }

            let registry_identity = registry_identity(&tx)?;
            let dispatch = caller
                .management
                .map(|management| {
                    let mut nonce = [0u8; 32];
                    File::open("/dev/urandom")
                        .and_then(|mut source| source.read_exact(&mut nonce))
                        .map_err(unavailable)?;
                    let dispatch_id = Digest::of_bytes(&nonce);
                    let correlation = json::obj([
                        ("request_digest", json::s(request.as_str())),
                        ("registry_identity", json::s(&registry_identity)),
                        (
                            "management_identity",
                            prepared
                                .intent
                                .get("management_identity")
                                .cloned()
                                .ok_or(Error::Unavailable)?,
                        ),
                        ("dispatch", json::s(dispatch_id.as_str())),
                        ("ceremony", json::s(ceremony.as_str())),
                    ]);
                    if !management.reserve(LifecycleKind::Pack, &key.raw, &binding, &correlation)? {
                        return Err(Error::Unavailable);
                    }
                    Ok((dispatch_id, correlation))
                })
                .transpose()?;
            let revision = change.expected + 1;
            let target = &change.target;
            let at = self.authority.now()?;
            let envelope_digest = digest(&prepared.envelope.value)?;
            let receipt = json::obj([
                ("scope", scope_value(caller.scope)),
                ("registry_identity", json::s(&registry_identity)),
                ("subject", json::s(caller.subject)),
                ("revision", Value::Int(revision)),
                ("operation", json::s(operation.name())),
                ("pack_id", json::s(&target.id)),
                ("pack_version", json::s(&target.version)),
                (
                    "previous_active_version",
                    prepared
                        .intent
                        .get("active_version")
                        .cloned()
                        .ok_or(Error::Unavailable)?,
                ),
                ("envelope_digest", json::s(envelope_digest.as_str())),
                ("intent_digest", json::s(intent.as_str())),
                ("ceremony", json::s(ceremony.as_str())),
                ("at", json::s(&at.to_string())),
            ]);
            match operation {
                PackOperation::Install => {
                    let document =
                        json::canonical(&prepared.envelope.value).map_err(unavailable)?;
                    tx.execute(
                        "INSERT INTO pack_versions VALUES (?1, ?2, ?3, ?4, ?5)",
                        params![
                            target.id,
                            target.version,
                            document,
                            envelope_digest.as_str(),
                            revision
                        ],
                    )
                    .map_err(unavailable)?;
                }
                PackOperation::Enable => {
                    tx.execute(
                        "INSERT INTO pack_selection VALUES (?1, ?2)",
                        params![target.id, target.version],
                    )
                    .map_err(unavailable)?;
                }
                PackOperation::Upgrade => {
                    tx.execute(
                        "UPDATE pack_selection SET pack_version = ?2 WHERE pack_id = ?1",
                        params![target.id, target.version],
                    )
                    .map_err(unavailable)?;
                }
                PackOperation::Disable => {
                    tx.execute(
                        "DELETE FROM pack_selection WHERE pack_id = ?1",
                        [&target.id],
                    )
                    .map_err(unavailable)?;
                }
                _ => return Err(Error::InvalidRequest),
            }
            tx.execute(
                "INSERT INTO pack_audit VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12)",
                params![
                    revision,
                    target.id,
                    target.version,
                    operation.name(),
                    caller.subject,
                    key.digest.as_str(),
                    request.as_str(),
                    intent.as_str(),
                    dispatch.as_ref().map(|(id, _)| id.as_str()),
                    ceremony.as_str(),
                    json::canonical(&receipt).map_err(unavailable)?,
                    digest(&receipt)?.as_str()
                ],
            )
            .map_err(unavailable)?;
            if let Some(snapshot) = snapshot {
                prepared.envelope.verify_rubrics(
                    snapshot,
                    caller.scope,
                    &self.evaluator_dependencies,
                )?;
            }
            if let (Some(management), Some((_, correlation))) = (caller.management, &dispatch) {
                if management.correlation(LifecycleKind::Pack, &key.raw)? != *correlation {
                    return Err(Error::Unavailable);
                }
            }
            prepared.recheck(caller)?;
            if !approval.still_valid() {
                return Err(Error::Unauthorized);
            }
            tx.commit().map_err(unavailable)?;
            Ok(receipt)
        })
    }

    fn with_dependencies<T>(
        &self,
        operation: PackOperation,
        commit: bool,
        callback: impl FnOnce(Option<&EvaluatorPackSnapshot<'_>>) -> Result<T>,
    ) -> Result<T> {
        if operation == PackOperation::Disable {
            callback(None)
        } else {
            self.with_evaluator(commit, |snapshot| callback(Some(snapshot)))
        }
    }

    fn with_evaluator<T>(
        &self,
        commit: bool,
        callback: impl FnOnce(&EvaluatorPackSnapshot<'_>) -> Result<T>,
    ) -> Result<T> {
        // Lock order: evaluator writer reservation, then the shared contract/pack
        // owner. Contract retirement takes only the latter. Evaluator
        // publication and revocation take only the former.
        EvaluatorRegistryReader::for_pack(self.evaluator, commit, callback).map_err(unavailable)?
    }

    fn prepare<'p>(
        &'p self,
        tx: &Connection,
        snapshot: Option<&EvaluatorPackSnapshot<'_>>,
        caller: &Caller<'_>,
        change: &Change,
    ) -> Result<Prepared<'p>> {
        if revision(tx)? != change.expected {
            return Err(Error::RevisionConflict);
        }
        if change.expected >= 512 {
            return Err(Error::LimitExceeded);
        }
        let target = &change.target;
        let active = active(tx, &target.id)?;
        match change.operation {
            PackOperation::Install => {
                if stored_exists(tx, &target.id, &target.version)? {
                    return Err(Error::RevisionConflict);
                }
            }
            PackOperation::Enable if active.is_none() => {}
            PackOperation::Disable if active.as_deref() == Some(target.version.as_str()) => {}
            PackOperation::Upgrade
                if active
                    .as_deref()
                    .and_then(version)
                    .zip(version(&target.version))
                    .is_some_and(|(old, next)| old < next) => {}
            _ => return Err(Error::RevisionConflict),
        }
        let value = change
            .envelope
            .clone()
            .map_or_else(|| stored(tx, &target.id, &target.version), Ok)?;
        if json::canonical(&value).map_err(unavailable)?.len() > MAX_ENVELOPE {
            return Err(Error::LimitExceeded);
        }
        let envelope = Envelope::parse(value, caller.scope, &target.id, &target.version)?;
        let publisher = if let Some(snapshot) = snapshot {
            Some(self.verify(tx, snapshot, caller.scope, target, &envelope)?)
        } else {
            None
        };

        if matches!(
            change.operation,
            PackOperation::Enable | PackOperation::Upgrade
        ) {
            if let Some(previous) = &active {
                let old = Envelope::parse(
                    stored(tx, &target.id, previous)?,
                    caller.scope,
                    &target.id,
                    previous,
                )?;
                if !envelope.preserves_gates(&old)? {
                    return Err(Error::InvalidPack);
                }
            }
            for (id, other) in active_entries(tx, caller.scope)? {
                if id != target.id && envelope.conflicts(&other)? {
                    return Err(Error::PackConflict);
                }
            }
        }

        let confirmation = if change.operation.destructive() {
            json::s(&format!(
                "{} {}@{} from {}",
                change.operation.name(),
                target.id,
                target.version,
                active.as_deref().ok_or(Error::Unavailable)?
            ))
        } else {
            Value::Null
        };
        let intent = json::obj([
            ("purpose", json::s(PACK_ADMIN_PURPOSE)),
            ("registry_identity", json::s(&registry_identity(tx)?)),
            (
                "management_identity",
                nullable(
                    caller
                        .management
                        .map(|call| call.store.pack_dispatch_identity())
                        .transpose()?,
                ),
            ),
            ("operation", json::s(change.operation.name())),
            ("scope", scope_value(caller.scope)),
            ("subject", json::s(caller.subject)),
            ("pack_id", json::s(&target.id)),
            ("pack_version", json::s(&target.version)),
            (
                "envelope_digest",
                json::s(digest(&envelope.value)?.as_str()),
            ),
            ("expected_revision", Value::Int(change.expected)),
            ("active_version", nullable(active)),
            ("confirmation", confirmation),
        ]);
        Ok(Prepared {
            envelope,
            intent,
            publisher,
        })
    }

    fn verify<'p>(
        &'p self,
        tx: &Connection,
        snapshot: &EvaluatorPackSnapshot<'_>,
        scope: &Scope,
        target: &Target,
        envelope: &Envelope,
    ) -> Result<Box<dyn CurrentPackPublisher + 'p>> {
        let identity = tx
            .query_row(
                "SELECT evaluator_identity FROM pack_owner WHERE singleton = 1",
                [],
                |row| row.get::<_, String>(0),
            )
            .map_err(unavailable)?;
        if snapshot.identity().map_err(unavailable)? != identity {
            return Err(Error::Unavailable);
        }
        let publisher = envelope.verify_publisher(self.authority, scope, &target.id)?;
        self.contracts.pack_manifest(&envelope.manifest)?;
        for contract in &envelope.contracts {
            if self
                .contracts
                .pack_contract(tx, &target.id, &contract.id, &contract.version)?
                != contract.digest
            {
                return Err(Error::InvalidPack);
            }
        }
        envelope.verify_rubrics(snapshot, scope, &self.evaluator_dependencies)?;
        Ok(publisher)
    }
}

impl PackBoundary for PackRegistry<'_> {
    fn execute(
        &self,
        call: &LifecycleCall<'_>,
        operation: PackOperation,
        fields: Map,
    ) -> Result<Value> {
        self.execute_as(
            &Caller {
                scope: call.scope,
                subject: call.subject,
                reauthorize: call.reauthorize,
                management: Some(call),
            },
            operation,
            fields,
        )
    }
}

struct Caller<'a> {
    scope: &'a Scope,
    subject: &'a str,
    reauthorize: &'a dyn Fn() -> Result<()>,
    management: Option<&'a LifecycleCall<'a>>,
}

struct Prepared<'p> {
    envelope: Envelope,
    intent: Value,
    publisher: Option<Box<dyn CurrentPackPublisher + 'p>>,
}

impl Prepared<'_> {
    fn recheck(&self, caller: &Caller<'_>) -> Result<()> {
        (caller.reauthorize)()?;
        if self
            .publisher
            .as_ref()
            .is_some_and(|publisher| !publisher.still_valid())
        {
            return Err(Error::Unauthorized);
        }
        Ok(())
    }
}

struct Target {
    id: String,
    version: String,
}

impl Target {
    fn parse(fields: &mut Map) -> Result<Self> {
        let id = take_string(fields, "pack_id")?;
        let requested = take_string(fields, "pack_version")?;
        if !pack_id(&id) || version(&requested).is_none() {
            return Err(Error::InvalidRequest);
        }
        Ok(Self {
            id,
            version: requested,
        })
    }
}

struct Change {
    operation: PackOperation,
    target: Target,
    expected: i64,
    envelope: Option<Value>,
}

impl Change {
    fn parse(operation: PackOperation, fields: &mut Map) -> Result<Self> {
        let target = Target::parse(fields)?;
        let expected = fields
            .remove("expected_revision")
            .and_then(|value| value.as_int())
            .filter(|value| (0..=512).contains(value))
            .ok_or(Error::InvalidRequest)?;
        let envelope = if operation == PackOperation::Install {
            Some(fields.remove("envelope").ok_or(Error::InvalidRequest)?)
        } else {
            None
        };
        Ok(Self {
            operation,
            target,
            expected,
            envelope,
        })
    }
}

struct Idempotency {
    raw: String,
    digest: Digest,
}

impl Idempotency {
    fn parse(fields: &mut Map) -> Result<Self> {
        let raw = take_string(fields, "idempotency_key")?;
        if !valid_label(&raw) {
            return Err(Error::InvalidRequest);
        }
        Ok(Self {
            digest: Digest::of_bytes(raw.as_bytes()),
            raw,
        })
    }
}

fn registry_identity(db: &Connection) -> Result<String> {
    let identity: String = db
        .query_row(
            "SELECT registry_identity FROM pack_owner WHERE singleton = 1",
            [],
            |row| row.get(0),
        )
        .map_err(unavailable)?;
    writ_ledger_sqlite::LedgerId::parse(&identity).ok_or(Error::Unavailable)?;
    Ok(identity)
}

fn known_receipt(
    db: &Connection,
    caller: &Caller<'_>,
    key: &Idempotency,
    request: &str,
) -> Result<Option<Value>> {
    if let Some(management) = caller.management {
        match management.correlation(LifecycleKind::Pack, &key.raw) {
            Ok(correlation) => {
                if text(&correlation, "request_digest")? != request {
                    return Err(Error::IdempotencyConflict);
                }
                return recover(db, caller, key, &correlation).map(Some);
            }
            Err(Error::NotFound) => {}
            Err(error) => return Err(error),
        }
    }
    let previous = previous_receipt(db, caller.subject, key, request, None)?;
    (caller.reauthorize)()?;
    Ok(previous)
}

fn recover(
    db: &Connection,
    caller: &Caller<'_>,
    key: &Idempotency,
    correlation: &Value,
) -> Result<Value> {
    let management = caller.management.ok_or(Error::InvalidRequest)?;
    if text(correlation, "registry_identity")? != registry_identity(db)?
        || text(correlation, "management_identity")? != management.store.pack_dispatch_identity()?
    {
        return Err(Error::Unavailable);
    }
    let request = text(correlation, "request_digest")?;
    let dispatch = text(correlation, "dispatch")?;
    let receipt = previous_receipt(db, caller.subject, key, &request, Some(&dispatch))?
        .ok_or(Error::Unavailable)?;
    (caller.reauthorize)()?;
    Ok(receipt)
}

fn previous_receipt(
    db: &Connection,
    subject: &str,
    key: &Idempotency,
    request: &str,
    dispatch: Option<&str>,
) -> Result<Option<Value>> {
    let row = db
        .query_row(
            "SELECT request_digest, receipt, receipt_digest, management_dispatch
         FROM pack_audit WHERE subject=?1 AND key_digest=?2",
            params![subject, key.digest.as_str()],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, Option<String>>(3)?,
                ))
            },
        )
        .optional()
        .map_err(unavailable)?;
    row.map(|(binding, body, digest, recorded_dispatch)| {
        if binding != request || recorded_dispatch.as_deref() != dispatch {
            return Err(Error::IdempotencyConflict);
        }
        read_document(&body, &digest)
    })
    .transpose()
}

fn read_document(document: &str, digest: &str) -> Result<Value> {
    if Digest::of_bytes(document.as_bytes()).as_str() != digest {
        return Err(Error::Unavailable);
    }
    json::parse(document.as_bytes()).map_err(unavailable)
}

fn revision(db: &Connection) -> Result<i64> {
    let (count, head): (i64, i64) = db
        .query_row(
            "SELECT count(*), COALESCE(max(revision), 0) FROM pack_audit",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .map_err(unavailable)?;
    if count != head {
        return Err(Error::Unavailable);
    }
    Ok(head)
}

fn active(db: &Connection, id: &str) -> Result<Option<String>> {
    let selected = db
        .query_row(
            "SELECT pack_version FROM pack_selection WHERE pack_id = ?1",
            [id],
            |row| row.get(0),
        )
        .optional()
        .map_err(unavailable)?;
    let last: Option<(String, String)> = db.query_row(
        "SELECT operation, pack_version FROM pack_audit WHERE pack_id = ?1 AND operation <> 'install' ORDER BY revision DESC LIMIT 1", [id],
        |row| Ok((row.get(0)?, row.get(1)?)),
    ).optional().map_err(unavailable)?;
    let audited = last
        .filter(|(op, _)| op != "disable")
        .map(|(_, version)| version);
    if selected != audited {
        return Err(Error::Unavailable);
    }
    Ok(selected)
}

fn active_entries(db: &Connection, scope: &Scope) -> Result<Vec<(String, Envelope)>> {
    revision(db)?;
    let mut query = db.prepare("SELECT pack_id FROM pack_audit UNION SELECT pack_id FROM pack_selection ORDER BY pack_id").map_err(unavailable)?;
    let rows = query
        .query_map([], |row| row.get::<_, String>(0))
        .map_err(unavailable)?;
    let mut entries = Vec::new();
    for id in rows {
        let id = id.map_err(unavailable)?;
        if let Some(version) = active(db, &id)? {
            let value = stored(db, &id, &version)?;
            let envelope = Envelope::parse(value, scope, &id, &version)?;
            entries.push((id, envelope));
        }
    }
    Ok(entries)
}

pub(crate) fn active_contract_dependents(
    db: &Connection,
    scope: &Scope,
    contract: &str,
    version: &str,
) -> Result<Vec<String>> {
    Ok(active_entries(db, scope)?
        .into_iter()
        .filter_map(|(id, envelope)| {
            envelope
                .contracts
                .iter()
                .any(|dep| dep.id == contract && dep.version == version)
                .then_some(id)
        })
        .collect())
}

fn stored_exists(db: &Connection, id: &str, version: &str) -> Result<bool> {
    db.query_row(
        "SELECT EXISTS (SELECT 1 FROM pack_versions WHERE pack_id = ?1 AND pack_version = ?2)",
        params![id, version],
        |row| row.get(0),
    )
    .map_err(unavailable)
}

fn stored(db: &Connection, id: &str, version: &str) -> Result<Value> {
    let (value, digest, receipt, receipt_digest): (String, String, String, String) = db
        .query_row(
            "SELECT v.envelope, v.envelope_digest, a.receipt, a.receipt_digest
         FROM pack_versions v JOIN pack_audit a
           ON a.revision = v.installed_revision AND a.pack_id = v.pack_id
           AND a.pack_version = v.pack_version AND a.operation = 'install'
         WHERE v.pack_id = ?1 AND v.pack_version = ?2",
            params![id, version],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        )
        .optional()
        .map_err(unavailable)?
        .ok_or(Error::NotFound)?;
    if read_document(&receipt, &receipt_digest)?
        .get("envelope_digest")
        .and_then(Value::as_str)
        != Some(digest.as_str())
    {
        return Err(Error::Unavailable);
    }
    read_document(&value, &digest)
}

fn history(db: &Connection, id: &str) -> Result<Value> {
    let mut query = db
        .prepare(
            "SELECT receipt, receipt_digest FROM pack_audit WHERE pack_id = ?1 ORDER BY revision",
        )
        .map_err(unavailable)?;
    let rows = query
        .query_map([id], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        })
        .map_err(unavailable)?;
    let records = rows
        .map(|row| {
            let (body, digest) = row.map_err(unavailable)?;
            read_document(&body, &digest)
        })
        .collect::<Result<Vec<_>>>()?;
    Ok(json::obj([
        ("revision", Value::Int(revision(db)?)),
        ("active_version", nullable(active(db, id)?)),
        ("history", Value::Arr(records)),
    ]))
}

fn nullable(value: Option<String>) -> Value {
    value.map_or(Value::Null, |value| json::s(&value))
}
fn unavailable<E>(_: E) -> Error {
    Error::Unavailable
}

fn take_string(fields: &mut Map, field: &str) -> Result<String> {
    fields
        .remove(field)
        .and_then(|value| value.as_str().map(str::to_owned))
        .ok_or(Error::InvalidRequest)
}

fn finish(fields: &Map) -> Result<()> {
    if fields.is_empty() {
        Ok(())
    } else {
        Err(Error::InvalidRequest)
    }
}
