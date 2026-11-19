use std::{
    collections::BTreeSet,
    fs::{self, OpenOptions},
    os::unix::fs::OpenOptionsExt,
    path::{Path, PathBuf},
    time::Duration,
};

use rusqlite::{
    limits::Limit, params, Connection, OpenFlags, OptionalExtension, TransactionBehavior,
};
use writ_kernel::{
    json::{self, Digest, Map, Value},
    time::Timestamp,
};

use crate::{
    contracts::{
        core_catalog, core_contract, core_reserved, ContractAuthority, ContractBoundary,
        ContractCall, ContractDependency, ContractOperation, ContractValidator, CONTRACT_PURPOSE,
    },
    store::{recoverable, sqlite_header_matches},
    valid_label, Error, Result, Scope,
};

const APPLICATION_ID: u32 = 0x5752_4331;
const SCHEMA_VERSION: u32 = 1;
const PACK_SCHEMA_VERSION: u32 = 2;
const MAX_VERSIONS: i64 = 128;
const MAX_VERSIONS_PER_CONTRACT: i64 = 12;
const MAX_REVISION: i64 = 4 * MAX_VERSIONS;
const MAX_PRESENTATION: usize = 512;
const MAX_SCHEMA: usize = 32 * 1024;

/// The disposable owner of one exact tenant and project's custom-contract registry.
pub struct ContractRegistry<'a> {
    path: PathBuf,
    scope: Scope,
    authority: &'a dyn ContractAuthority,
    allow_self_publication: bool,
    validator: ContractValidator,
}

impl<'a> ContractRegistry<'a> {
    /// Install the owner-provided pack names and complete dependency snapshot for this scope.
    /// A missing snapshot prevents retirement for every version.
    pub fn create(
        path: &Path,
        scope: Scope,
        packs: &[String],
        dependencies: Option<&[ContractDependency]>,
        authority: &'a dyn ContractAuthority,
        allow_self_publication: bool,
        validator: ContractValidator,
    ) -> Result<Self> {
        Self::create_profile(
            path,
            scope,
            packs,
            dependencies,
            authority,
            allow_self_publication,
            validator,
            None,
        )
    }

    /// Provision the D-059 profile with the exact disposable evaluator ledger identity.
    pub fn create_pack_owner(
        path: &Path,
        scope: Scope,
        packs: &[String],
        authority: &'a dyn ContractAuthority,
        validator: ContractValidator,
        evaluator_identity: &str,
    ) -> Result<Self> {
        if writ_ledger_sqlite::LedgerId::parse(evaluator_identity).is_none() {
            return Err(Error::InvalidRequest);
        }
        Self::create_profile(
            path,
            scope,
            packs,
            Some(&[]),
            authority,
            true,
            validator,
            Some(evaluator_identity),
        )
    }

    #[allow(clippy::too_many_arguments)]
    fn create_profile(
        path: &Path,
        scope: Scope,
        packs: &[String],
        dependencies: Option<&[ContractDependency]>,
        authority: &'a dyn ContractAuthority,
        allow_self_publication: bool,
        validator: ContractValidator,
        evaluator_identity: Option<&str>,
    ) -> Result<Self> {
        if !valid_label(&scope.tenant) || !valid_label(&scope.project) {
            return Err(Error::InvalidRequest);
        }
        if packs.len() > 32 || dependencies.is_some_and(|entries| entries.len() > 128) {
            return Err(Error::LimitExceeded);
        }
        let approved_packs: BTreeSet<&str> = packs.iter().map(String::as_str).collect();
        if packs.len() != approved_packs.len() || packs.iter().any(|pack| !valid_label(pack)) {
            return Err(Error::InvalidRequest);
        }
        let mut dependency_keys = BTreeSet::new();
        for dependency in dependencies.unwrap_or_default() {
            custom_id(&scope, &dependency.contract_id, |pack| {
                Ok(approved_packs.contains(pack))
            })?;
            Version::parse(&dependency.version)?;
            if !valid_label(&dependency.owner)
                || !matches!(dependency.kind.as_str(), "pack" | "policy" | "integration")
                || !dependency_keys.insert((
                    &dependency.contract_id,
                    &dependency.version,
                    &dependency.kind,
                    &dependency.owner,
                ))
            {
                return Err(Error::InvalidRequest);
            }
        }

        OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(path)
            .map_err(unavailable)?;
        let mut db = connect(path, true)?;
        let tx = db
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(unavailable)?;
        tx.execute_batch(include_str!("contract_registry.sql"))
            .map_err(unavailable)?;
        tx.pragma_update(None, "application_id", APPLICATION_ID)
            .map_err(unavailable)?;
        let version = if let Some(identity) = evaluator_identity {
            tx.execute_batch(include_str!("pack_registry.sql"))
                .map_err(unavailable)?;
            tx.execute(
                "INSERT INTO pack_owner VALUES (1, lower(hex(randomblob(16))), ?1)",
                [identity],
            )
            .map_err(unavailable)?;
            PACK_SCHEMA_VERSION
        } else {
            SCHEMA_VERSION
        };
        tx.pragma_update(None, "user_version", version)
            .map_err(unavailable)?;
        tx.execute(
            "INSERT INTO contract_owner VALUES (1, ?1, ?2, ?3)",
            params![
                scope.tenant,
                scope.project,
                dependencies.map(|entries| entries.len() as i64)
            ],
        )
        .map_err(unavailable)?;
        for pack in packs {
            tx.execute("INSERT INTO contract_packs VALUES (?1)", [pack])
                .map_err(unavailable)?;
        }
        for dependency in dependencies.unwrap_or_default() {
            tx.execute(
                "INSERT INTO contract_dependencies VALUES (?1, ?2, ?3, ?4)",
                params![
                    dependency.contract_id,
                    dependency.version,
                    dependency.kind,
                    dependency.owner
                ],
            )
            .map_err(unavailable)?;
        }
        tx.commit().map_err(unavailable)?;
        Ok(Self {
            path: path.into(),
            scope,
            authority,
            allow_self_publication,
            validator,
        })
    }

    /// Open and verify an existing owner database through a read-only connection.
    pub fn open(
        path: &Path,
        scope: Scope,
        authority: &'a dyn ContractAuthority,
        allow_self_publication: bool,
        validator: ContractValidator,
    ) -> Result<Self> {
        let registry = Self {
            path: path.into(),
            scope,
            authority,
            allow_self_publication,
            validator,
        };
        check_open(registry.connection(false)?)?;
        Ok(registry)
    }

    /// Recover a recognized disposable owner database after an interrupted SQLite transaction.
    pub fn recover_storage(path: &Path, scope: &Scope) -> Result<()> {
        let must_recover = recoverable(path, APPLICATION_ID, profile(path)?)?;
        check_open(scoped_connection(path, scope, must_recover)?)
    }

    fn connection(&self, write: bool) -> Result<Connection> {
        scoped_connection(&self.path, &self.scope, write)
    }

    pub(crate) fn pack_scope(&self) -> &Scope {
        &self.scope
    }

    pub(crate) fn pack_connection(&self, write: bool) -> Result<Connection> {
        if profile(&self.path)? != PACK_SCHEMA_VERSION {
            return Err(Error::Unavailable);
        }
        self.connection(write)
    }

    pub(crate) fn pack_manifest(&self, manifest: &Value) -> Result<()> {
        let schema = core_contract("capability-pack").ok_or(Error::Unavailable)?;
        let diagnostics = self.validator.validate_example(
            schema.get("schema").ok_or(Error::Unavailable)?,
            manifest,
            true,
        )?;
        if diagnostics.as_arr().is_some_and(|errors| errors.is_empty()) {
            Ok(())
        } else {
            Err(Error::InvalidPack)
        }
    }

    pub(crate) fn pack_contract(
        &self,
        db: &Connection,
        pack: &str,
        id: &str,
        version: &str,
    ) -> Result<String> {
        if let Some(core) = core_contract(id) {
            if version != "1.0.0" {
                return Err(Error::NotFound);
            }
            return Ok(digest(core.get("schema").ok_or(Error::Unavailable)?)?.to_string());
        }
        self.custom_id(db, id)?;
        if id.starts_with("pack:") && id.split(':').nth(1) != Some(pack) {
            return Err(Error::NotFound);
        }
        Version::parse(version)?;
        let version = record(
            db,
            &Target {
                id: id.into(),
                version: version.into(),
            },
        )?;
        if !matches!(version.change, Change::Publish | Change::Deprecate) {
            return Err(Error::InvalidPack);
        }
        Ok(version.digest)
    }

    fn custom_id(&self, db: &Connection, id: &str) -> Result<()> {
        custom_id(&self.scope, id, |pack| {
            db.query_row(
                "SELECT EXISTS (SELECT 1 FROM contract_packs WHERE name = ?1)",
                [pack],
                |row| row.get(0),
            )
            .map_err(unavailable)
        })
    }

    fn read(
        &self,
        call: &ContractCall<'_>,
        operation: ContractOperation,
        mut fields: Map,
    ) -> Result<Value> {
        use ContractOperation::*;
        let mut db = self.connection(false)?;
        let tx = db.transaction().map_err(unavailable)?;
        match operation {
            Browse => {
                empty(&fields)?;
                let mut catalog: Vec<Value> = core_catalog()
                    .iter()
                    .map(|entry| entry.without("schema"))
                    .collect();
                let mut query = tx
                    .prepare(
                        "SELECT v.contract_id, v.version, v.contract_digest, a.operation
                         FROM contract_versions v JOIN contract_audit a ON a.revision = (
                             SELECT max(prior.revision) FROM contract_audit prior
                             WHERE prior.contract_id = v.contract_id AND prior.version = v.version
                         )
                         ORDER BY v.contract_id, v.major, v.minor, v.patch",
                    )
                    .map_err(unavailable)?;
                let mut rows = query.query([]).map_err(unavailable)?;
                while let Some(row) = rows.next().map_err(unavailable)? {
                    let id: String = row.get(0).map_err(unavailable)?;
                    self.custom_id(&tx, &id).map_err(unavailable)?;
                    let version: String = row.get(1).map_err(unavailable)?;
                    let digest: String = row.get(2).map_err(unavailable)?;
                    let change: String = row.get(3).map_err(unavailable)?;
                    catalog.push(json::obj([
                        ("contract_id", json::s(&id)),
                        ("version", json::s(&version)),
                        ("contract_digest", json::s(&digest)),
                        ("status", json::s(Change::stored(&change)?.status())),
                    ]));
                }
                Ok(json::obj([
                    ("revision", Value::Int(revision(&tx)?)),
                    ("contracts", Value::Arr(catalog)),
                ]))
            }
            Inspect => {
                let id = string(&mut fields, "contract_id")?;
                empty(&fields)?;
                self.inspect(&tx, &id)
            }
            ValidateSchema => {
                let schema = take(&mut fields, "schema")?;
                empty(&fields)?;
                Ok(validation_report(
                    self.validator.validate_schema(&schema)?.diagnostics,
                ))
            }
            ValidateExample => {
                let target = Target::parse(&mut fields)?;
                let example = take(&mut fields, "example")?;
                empty(&fields)?;
                let (schema, core) = if let Some(entry) = core_contract(&target.id) {
                    if entry.get("version").and_then(Value::as_str) != Some(&target.version) {
                        return Err(Error::NotFound);
                    }
                    (entry.get("schema").ok_or(Error::Unavailable)?.clone(), true)
                } else {
                    self.custom_id(&tx, &target.id)?;
                    (record(&tx, &target)?.schema, false)
                };
                Ok(validation_report(
                    self.validator.validate_example(&schema, &example, core)?,
                ))
            }
            Preview => {
                let change = Change::from_name(&string(&mut fields, "change")?)?;
                if change == Change::Create {
                    return Err(Error::InvalidRequest);
                }
                let request = ChangeRequest::parse(&mut fields, change)?;
                empty(&fields)?;
                self.custom_id(&tx, &request.target.id)?;
                let at = self.authority.now()?;
                let preview = self.prepare(&tx, call.subject, change, &request, at)?;
                Ok(json::obj([
                    ("intent_digest", json::s(digest(&preview.intent)?.as_str())),
                    ("intent", preview.intent),
                    ("compatibility", preview.compatibility),
                ]))
            }
            Recover => {
                let key = key(&mut fields)?;
                empty(&fields)?;
                previous_receipt(&tx, call.subject, &key)?
                    .map(|(_, receipt)| receipt)
                    .ok_or(Error::NotFound)
            }
            _ => Err(Error::InvalidRequest),
        }
    }

    fn inspect(&self, db: &Connection, id: &str) -> Result<Value> {
        let mut versions = Vec::new();
        let mut history = Vec::new();
        if let Some(core) = core_contract(id) {
            versions.push(core);
        } else {
            self.custom_id(db, id)?;
            let mut query = db
                .prepare(
                    "SELECT version FROM contract_versions WHERE contract_id = ?1
                     ORDER BY major, minor, patch",
                )
                .map_err(unavailable)?;
            let mut rows = query.query([id]).map_err(unavailable)?;
            while let Some(row) = rows.next().map_err(unavailable)? {
                let target = Target {
                    id: id.into(),
                    version: row.get(0).map_err(unavailable)?,
                };
                let version = record(db, &target)?;
                let replacement = deprecation(db, &target)?;
                let dependents = active_dependents(db, &target)?.map_or(Value::Null, Value::Arr);
                versions.push(json::obj([
                    ("contract_id", json::s(id)),
                    ("version", json::s(&target.version)),
                    ("schema", version.schema),
                    ("contract_digest", json::s(&version.digest)),
                    ("status", json::s(version.change.status())),
                    (
                        "replacement_version",
                        replacement
                            .as_ref()
                            .map_or(Value::Null, |r| json::s(&r.version)),
                    ),
                    (
                        "end_date",
                        replacement
                            .as_ref()
                            .map_or(Value::Null, |r| json::s(&r.end_date)),
                    ),
                    ("dependencies", dependents),
                ]));
            }
            if versions.is_empty() {
                return Err(Error::NotFound);
            }
            let mut query = db
                .prepare(
                    "SELECT receipt, receipt_digest FROM contract_audit
                     WHERE contract_id = ?1 ORDER BY revision",
                )
                .map_err(unavailable)?;
            let mut rows = query.query([id]).map_err(unavailable)?;
            while let Some(row) = rows.next().map_err(unavailable)? {
                history.push(read_document(
                    row.get(0).map_err(unavailable)?,
                    row.get(1).map_err(unavailable)?,
                )?);
            }
        }
        Ok(json::obj([
            ("revision", Value::Int(revision(db)?)),
            ("contract_id", json::s(id)),
            ("versions", Value::Arr(versions)),
            ("history", Value::Arr(history)),
        ]))
    }
}

impl ContractBoundary for ContractRegistry<'_> {
    fn execute(
        &self,
        call: &ContractCall<'_>,
        operation: ContractOperation,
        fields: Map,
    ) -> Result<Value> {
        if call.operation != operation {
            return Err(Error::Unauthorized);
        }
        if call.scope != &self.scope {
            return Err(Error::NotFound);
        }
        if !writ_kernel::ids::is_id(call.subject) {
            return Err(Error::Unauthorized);
        }
        (call.reauthorize)()?;
        match operation {
            ContractOperation::Create => self.create_draft(call, fields),
            ContractOperation::Publish => self.mutate(call, Change::Publish, fields),
            ContractOperation::Deprecate => self.mutate(call, Change::Deprecate, fields),
            ContractOperation::Retire => self.mutate(call, Change::Retire, fields),
            _ => self.read(call, operation, fields),
        }
    }
}

impl ContractRegistry<'_> {
    fn create_draft(&self, call: &ContractCall<'_>, mut fields: Map) -> Result<Value> {
        let binding = request_digest(call, Change::Create, &fields)?;
        let target = Target::parse(&mut fields)?;
        let schema = take(&mut fields, "schema")?;
        let expected = expected_revision(&mut fields)?;
        let key = key(&mut fields)?;
        empty(&fields)?;

        let mut db = self.connection(true)?;
        let tx = db
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(unavailable)?;
        self.custom_id(&tx, &target.id)?;
        (call.reauthorize)()?;
        if let Some(receipt) = replay(&tx, call.subject, &key, &binding)? {
            return Ok(receipt);
        }
        let revision = next_revision(&tx, expected)?;
        let (total, same_contract): (i64, i64) = tx
            .query_row(
                "SELECT count(*), count(*) FILTER (WHERE contract_id = ?1) FROM contract_versions",
                [&target.id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .map_err(unavailable)?;
        if load_record(&tx, &target)?.is_some() {
            return Err(Error::RevisionConflict);
        }
        if total >= MAX_VERSIONS || same_contract >= MAX_VERSIONS_PER_CONTRACT {
            return Err(Error::LimitExceeded);
        }
        let document = json::canonical(&schema).map_err(|_| Error::InvalidContract)?;
        if document.len() > MAX_SCHEMA {
            return Err(Error::LimitExceeded);
        }
        if self
            .validator
            .validate_schema(&schema)?
            .normalized
            .is_none()
        {
            return Err(Error::InvalidContract);
        }
        let schema_digest = Digest::of_bytes(document.as_bytes());
        let Version([major, minor, patch]) = Version::parse(&target.version)?;
        tx.execute(
            "INSERT INTO contract_versions VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
            params![
                target.id,
                target.version,
                major,
                minor,
                patch,
                document,
                schema_digest.as_str(),
                call.subject,
                revision
            ],
        )
        .map_err(unavailable)?;
        let receipt = Audit {
            target: &target,
            scope: &self.scope,
            acknowledge_self_publication: false,
            revision,
            change: Change::Create,
            subject: call.subject,
            key: &key,
            binding: &binding,
            schema_digest: schema_digest.as_str(),
            intent_digest: None,
            ceremony: None,
            at: self.authority.now()?,
            replacement: None,
            compatibility: Value::Null,
        }
        .append(&tx)?;
        (call.reauthorize)()?;
        tx.commit().map_err(unavailable)?;
        Ok(receipt)
    }

    fn mutate(&self, call: &ContractCall<'_>, change: Change, mut fields: Map) -> Result<Value> {
        let binding = request_digest(call, change, &fields)?;
        let request = ChangeRequest::parse(&mut fields, change)?;
        let shown_intent = string(&mut fields, "intent_digest")?;
        let presentation = string(&mut fields, "presentation")?;
        let key = key(&mut fields)?;
        let confirmation = if change.destructive() {
            Some(string(&mut fields, "confirmation")?)
        } else {
            None
        };
        empty(&fields)?;
        if !writ_kernel::ids::is_digest(&shown_intent)
            || presentation.is_empty()
            || presentation.len() > MAX_PRESENTATION
        {
            return Err(Error::InvalidRequest);
        }

        let mut db = self.connection(true)?;
        let tx = db
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(unavailable)?;
        self.custom_id(&tx, &request.target.id)?;
        (call.reauthorize)()?;
        if let Some(receipt) = replay(&tx, call.subject, &key, &binding)? {
            return Ok(receipt);
        }
        let revision = next_revision(&tx, request.expected_revision)?;
        let at = self.authority.now()?;
        let prepared = self.prepare(&tx, call.subject, change, &request, at)?;
        let intent_digest = digest(&prepared.intent)?;
        if shown_intent != intent_digest.as_str() {
            return Err(Error::RevisionConflict);
        }
        if confirmation.as_deref() != prepared.intent.get("confirmation").and_then(Value::as_str) {
            return Err(Error::InvalidRequest);
        }
        let committed_at = self.authority.now()?;
        if let Some(replacement) = &prepared.replacement {
            check_date(change, &replacement.end_date, committed_at)?;
        }
        let approval = self.authority.verify(&prepared.intent, &presentation)?;
        let ceremony = approval.ceremony();
        let consumed: bool = tx
            .query_row(
                "SELECT EXISTS (SELECT 1 FROM contract_audit WHERE ceremony = ?1)",
                [ceremony.as_str()],
                |row| row.get(0),
            )
            .map_err(unavailable)?;
        if consumed {
            return Err(Error::Unauthorized);
        }
        let receipt = Audit {
            target: &request.target,
            scope: &self.scope,
            acknowledge_self_publication: request.acknowledge_self_publication,
            revision,
            change,
            subject: call.subject,
            key: &key,
            binding: &binding,
            schema_digest: &prepared.contract_digest,
            intent_digest: Some(&intent_digest),
            ceremony: Some(ceremony),
            at: committed_at,
            replacement: prepared.replacement.as_ref(),
            compatibility: prepared.compatibility,
        }
        .append(&tx)?;
        (call.reauthorize)()?;
        if !approval.still_valid() {
            return Err(Error::Unauthorized);
        }
        tx.commit().map_err(unavailable)?;
        drop(approval);
        Ok(receipt)
    }

    fn prepare(
        &self,
        db: &Connection,
        subject: &str,
        change: Change,
        request: &ChangeRequest,
        at: Timestamp,
    ) -> Result<Prepared> {
        next_revision(db, request.expected_revision)?;
        let target = &request.target;
        let stored = record(db, target)?;
        if change.predecessor() != Some(stored.change) {
            return Err(Error::RevisionConflict);
        }
        let (replacement, compatibility) = match change {
            Change::Publish => {
                if stored.creator == subject {
                    if !self.allow_self_publication || !request.acknowledge_self_publication {
                        return Err(Error::Unauthorized);
                    }
                } else if request.acknowledge_self_publication {
                    return Err(Error::InvalidRequest);
                }
                (None, self.compatibility(db, target, &stored.schema)?)
            }
            Change::Deprecate => {
                let replacement = request.replacement.as_ref().ok_or(Error::InvalidRequest)?;
                check_date(change, &replacement.end_date, at)?;
                let candidate = Target {
                    id: target.id.clone(),
                    version: replacement.version.clone(),
                };
                if Version::parse(&candidate.version)? <= Version::parse(&target.version)?
                    || record(db, &candidate)?.change != Change::Publish
                {
                    return Err(Error::InvalidContract);
                }
                (Some(replacement.clone()), Value::Null)
            }
            Change::Retire => {
                let replacement = deprecation(db, target)?.ok_or(Error::Unavailable)?;
                check_date(change, &replacement.end_date, at)?;
                let dependents = active_dependents(db, target)?.ok_or(Error::Unavailable)?;
                if !dependents.is_empty() {
                    return Err(Error::ActiveDependents);
                }
                let candidate = Target {
                    id: target.id.clone(),
                    version: replacement.version.clone(),
                };
                if !matches!(
                    record(db, &candidate)?.change,
                    Change::Publish | Change::Deprecate
                ) {
                    return Err(Error::InvalidContract);
                }
                (Some(replacement), Value::Null)
            }
            Change::Create => return Err(Error::InvalidRequest),
        };
        let intent = json::obj([
            ("purpose", json::s(CONTRACT_PURPOSE)),
            ("operation", json::s(change.name())),
            ("scope", scope_value(&self.scope)),
            ("subject", json::s(subject)),
            ("contract_id", json::s(&target.id)),
            ("version", json::s(&target.version)),
            ("contract_digest", json::s(&stored.digest)),
            ("expected_revision", Value::Int(request.expected_revision)),
            (
                "replacement_version",
                replacement
                    .as_ref()
                    .map_or(Value::Null, |r| json::s(&r.version)),
            ),
            (
                "end_date",
                replacement
                    .as_ref()
                    .map_or(Value::Null, |r| json::s(&r.end_date)),
            ),
            (
                "confirmation",
                if change.destructive() {
                    json::s(&format!(
                        "{} {}@{}",
                        change.name(),
                        target.id,
                        target.version
                    ))
                } else {
                    Value::Null
                },
            ),
            (
                "acknowledge_self_publication",
                Value::Bool(request.acknowledge_self_publication),
            ),
        ]);
        Ok(Prepared {
            intent,
            contract_digest: stored.digest,
            compatibility,
            replacement,
        })
    }

    fn compatibility(&self, db: &Connection, target: &Target, schema: &Value) -> Result<Value> {
        let prior = db
            .query_row(
                "SELECT v.version FROM contract_versions v JOIN contract_audit a USING (contract_id, version)
                 WHERE v.contract_id = ?1 AND a.operation = 'publish'
                 ORDER BY v.major DESC, v.minor DESC, v.patch DESC LIMIT 1",
                [&target.id],
                |row| row.get::<_, String>(0),
            )
            .optional()
            .map_err(unavailable)?;
        let Some(prior) = prior else {
            return Ok(compatibility_result("initial", Value::Null, Vec::new()));
        };
        let old_version = Version::parse(&prior).map_err(unavailable)?;
        let new_version = Version::parse(&target.version)?;
        if new_version <= old_version {
            return Err(Error::IncompatibleContract);
        }
        let previous = record(
            db,
            &Target {
                id: target.id.clone(),
                version: prior.clone(),
            },
        )?;
        let old = self
            .validator
            .validate_schema(&previous.schema)?
            .normalized
            .ok_or(Error::Unavailable)?;
        let new = self
            .validator
            .validate_schema(schema)?
            .normalized
            .ok_or(Error::InvalidContract)?;
        let mut comparison = Compatibility::default();
        compare_schema(&old, &new, "", &mut comparison);
        if comparison.unknown
            || (!comparison.changes.is_empty() && new_version.0[0] <= old_version.0[0])
        {
            return Err(Error::IncompatibleContract);
        }
        let status = if comparison.changes.is_empty() {
            "compatible"
        } else {
            "breaking"
        };
        let result = compatibility_result(status, json::s(&prior), comparison.changes);
        if json::canonical(&result).map_err(unavailable)?.len() > 16 * 1024 {
            return Err(Error::LimitExceeded);
        }
        Ok(result)
    }
}

struct Prepared {
    intent: Value,
    contract_digest: String,
    compatibility: Value,
    replacement: Option<Replacement>,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Change {
    Create,
    Publish,
    Deprecate,
    Retire,
}

impl Change {
    fn from_name(name: &str) -> Result<Self> {
        match name {
            "create" => Ok(Self::Create),
            "publish" => Ok(Self::Publish),
            "deprecate" => Ok(Self::Deprecate),
            "retire" => Ok(Self::Retire),
            _ => Err(Error::InvalidRequest),
        }
    }

    fn stored(name: &str) -> Result<Self> {
        Self::from_name(name).map_err(unavailable)
    }

    fn name(self) -> &'static str {
        match self {
            Self::Create => "create",
            Self::Publish => "publish",
            Self::Deprecate => "deprecate",
            Self::Retire => "retire",
        }
    }

    fn status(self) -> &'static str {
        match self {
            Self::Create => "draft",
            Self::Publish => "published",
            Self::Deprecate => "deprecated",
            Self::Retire => "retired",
        }
    }

    fn predecessor(self) -> Option<Self> {
        match self {
            Self::Create => None,
            Self::Publish => Some(Self::Create),
            Self::Deprecate => Some(Self::Publish),
            Self::Retire => Some(Self::Deprecate),
        }
    }

    fn destructive(self) -> bool {
        matches!(self, Self::Deprecate | Self::Retire)
    }
}

struct Target {
    id: String,
    version: String,
}

impl Target {
    fn parse(fields: &mut Map) -> Result<Self> {
        let id = string(fields, "contract_id")?;
        let version = string(fields, "version")?;
        Version::parse(&version)?;
        Ok(Self { id, version })
    }
}

#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
struct Version([i64; 3]);

impl Version {
    fn parse(text: &str) -> Result<Self> {
        let mut parts = text.split('.');
        let mut version = [0; 3];
        for part in &mut version {
            let field = parts.next().ok_or(Error::InvalidRequest)?;
            if field.is_empty()
                || field.len() > 6
                || (field.len() > 1 && field.starts_with('0'))
                || !field.bytes().all(|byte| byte.is_ascii_digit())
            {
                return Err(Error::InvalidRequest);
            }
            *part = field.parse().map_err(|_| Error::InvalidRequest)?;
        }
        if parts.next().is_some() {
            return Err(Error::InvalidRequest);
        }
        Ok(Self(version))
    }
}

struct ChangeRequest {
    target: Target,
    expected_revision: i64,
    replacement: Option<Replacement>,
    acknowledge_self_publication: bool,
}

impl ChangeRequest {
    fn parse(fields: &mut Map, change: Change) -> Result<Self> {
        let target = Target::parse(fields)?;
        let expected_revision = expected_revision(fields)?;
        let replacement = if change == Change::Deprecate {
            let version = string(fields, "replacement_version")?;
            Version::parse(&version)?;
            let end_date = string(fields, "end_date")?;
            if end_date.len() != 10
                || Timestamp::parse_strict(&format!("{end_date}T00:00:00Z")).is_none()
            {
                return Err(Error::InvalidRequest);
            }
            Some(Replacement { version, end_date })
        } else {
            None
        };
        let acknowledge_self_publication = if change == Change::Publish {
            fields
                .remove("acknowledge_self_publication")
                .map_or(Ok(false), |value| {
                    value.as_bool().ok_or(Error::InvalidRequest)
                })?
        } else {
            false
        };
        Ok(Self {
            target,
            expected_revision,
            replacement,
            acknowledge_self_publication,
        })
    }
}

#[derive(Clone)]
struct Replacement {
    version: String,
    end_date: String,
}

struct Record {
    schema: Value,
    digest: String,
    creator: String,
    change: Change,
}

fn load_record(db: &Connection, target: &Target) -> Result<Option<Record>> {
    let stored = db
        .query_row(
            "SELECT v.schema, v.contract_digest, v.creator, a.operation
             FROM contract_versions v JOIN contract_audit a USING (contract_id, version)
             WHERE v.contract_id = ?1 AND v.version = ?2 ORDER BY a.revision DESC LIMIT 1",
            params![target.id, target.version],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                ))
            },
        )
        .optional()
        .map_err(unavailable)?;
    stored
        .map(|(schema, digest, creator, change)| {
            Ok(Record {
                schema: read_document(schema, digest.clone())?,
                digest,
                creator,
                change: Change::stored(&change)?,
            })
        })
        .transpose()
}

fn record(db: &Connection, target: &Target) -> Result<Record> {
    load_record(db, target)?.ok_or(Error::NotFound)
}

fn deprecation(db: &Connection, target: &Target) -> Result<Option<Replacement>> {
    db.query_row(
        "SELECT replacement_version, end_date FROM contract_audit
         WHERE contract_id = ?1 AND version = ?2 AND operation = 'deprecate'",
        params![target.id, target.version],
        |row| {
            Ok(Replacement {
                version: row.get(0)?,
                end_date: row.get(1)?,
            })
        },
    )
    .optional()
    .map_err(unavailable)
}

fn active_dependents(db: &Connection, target: &Target) -> Result<Option<Vec<Value>>> {
    let expected: Option<i64> = db
        .query_row(
            "SELECT dependency_count FROM contract_owner WHERE singleton = 1",
            [],
            |row| row.get(0),
        )
        .map_err(unavailable)?;
    let actual = db
        .query_row("SELECT count(*) FROM contract_dependencies", [], |row| {
            row.get(0)
        })
        .map_err(unavailable)?;
    if expected != Some(actual) {
        return Ok(None);
    }
    let mut query = db
        .prepare(
            "SELECT kind, owner FROM contract_dependencies WHERE contract_id = ?1 AND version = ?2
             UNION ALL
             SELECT 'replacement', a.version FROM contract_audit a
             WHERE a.contract_id = ?1 AND a.replacement_version = ?2 AND a.operation = 'deprecate'
               AND NOT EXISTS (
                 SELECT 1 FROM contract_audit b
                 WHERE b.contract_id = a.contract_id AND b.version = a.version AND b.operation = 'retire'
               )
             ORDER BY kind, owner",
        )
        .map_err(unavailable)?;
    let mut rows = query
        .query(params![target.id, target.version])
        .map_err(unavailable)?;
    let mut dependents = Vec::new();
    while let Some(row) = rows.next().map_err(unavailable)? {
        dependents.push(json::obj([
            (
                "kind",
                json::s(
                    row.get_ref(0)
                        .map_err(unavailable)?
                        .as_str()
                        .map_err(unavailable)?,
                ),
            ),
            (
                "owner",
                json::s(
                    row.get_ref(1)
                        .map_err(unavailable)?
                        .as_str()
                        .map_err(unavailable)?,
                ),
            ),
        ]));
    }
    if db
        .pragma_query_value(None, "user_version", |row| row.get::<_, u32>(0))
        .map_err(unavailable)?
        == PACK_SCHEMA_VERSION
    {
        let scope = db
            .query_row(
                "SELECT tenant, project FROM contract_owner WHERE singleton = 1",
                [],
                |row| {
                    Ok(Scope {
                        tenant: row.get(0)?,
                        project: row.get(1)?,
                    })
                },
            )
            .map_err(unavailable)?;
        for owner in crate::pack_registry::active_contract_dependents(
            db,
            &scope,
            &target.id,
            &target.version,
        )? {
            dependents.push(json::obj([
                ("kind", json::s("pack")),
                ("owner", json::s(&owner)),
            ]));
        }
    }
    Ok(Some(dependents))
}

fn check_date(change: Change, end_date: &str, at: Timestamp) -> Result<()> {
    let now = at.to_string();
    let today = now.get(..10).ok_or(Error::Unavailable)?;
    if change == Change::Deprecate && end_date < today {
        Err(Error::InvalidContract)
    } else if change == Change::Retire && end_date > today {
        Err(Error::RevisionConflict)
    } else {
        Ok(())
    }
}

struct Audit<'a> {
    target: &'a Target,
    scope: &'a Scope,
    revision: i64,
    change: Change,
    subject: &'a str,
    key: &'a Digest,
    binding: &'a Digest,
    schema_digest: &'a str,
    intent_digest: Option<&'a Digest>,
    ceremony: Option<&'a Digest>,
    at: Timestamp,
    replacement: Option<&'a Replacement>,
    compatibility: Value,
    acknowledge_self_publication: bool,
}

impl Audit<'_> {
    fn append(self, db: &Connection) -> Result<Value> {
        let receipt = json::obj([
            ("scope", scope_value(self.scope)),
            ("revision", Value::Int(self.revision)),
            ("contract_id", json::s(&self.target.id)),
            ("version", json::s(&self.target.version)),
            ("contract_digest", json::s(self.schema_digest)),
            ("operation", json::s(self.change.name())),
            ("status", json::s(self.change.status())),
            ("subject", json::s(self.subject)),
            (
                "intent_digest",
                self.intent_digest
                    .map_or(Value::Null, |value| json::s(value.as_str())),
            ),
            (
                "ceremony",
                self.ceremony
                    .map_or(Value::Null, |value| json::s(value.as_str())),
            ),
            ("at", json::s(&self.at.to_string())),
            (
                "replacement_version",
                self.replacement
                    .map_or(Value::Null, |r| json::s(&r.version)),
            ),
            (
                "end_date",
                self.replacement
                    .map_or(Value::Null, |r| json::s(&r.end_date)),
            ),
            ("compatibility", self.compatibility),
            (
                "acknowledge_self_publication",
                Value::Bool(self.acknowledge_self_publication),
            ),
        ]);
        let document = json::canonical(&receipt).map_err(unavailable)?;
        let receipt_digest = Digest::of_bytes(document.as_bytes());
        let deprecation = (self.change == Change::Deprecate)
            .then_some(self.replacement)
            .flatten();
        db.execute(
            "INSERT INTO contract_audit VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13)",
            params![
                self.revision,
                self.target.id,
                self.target.version,
                self.change.name(),
                self.subject,
                self.key.as_str(),
                self.binding.as_str(),
                self.intent_digest.map(Digest::as_str),
                self.ceremony.map(Digest::as_str),
                deprecation.map(|replacement| &replacement.version),
                deprecation.map(|replacement| &replacement.end_date),
                document,
                receipt_digest.as_str()
            ],
        )
        .map_err(unavailable)?;
        Ok(receipt)
    }
}

fn previous_receipt(
    db: &Connection,
    subject: &str,
    key: &Digest,
) -> Result<Option<(String, Value)>> {
    let stored = db
        .query_row(
            "SELECT request_digest, receipt, receipt_digest FROM contract_audit
             WHERE subject = ?1 AND key_digest = ?2",
            params![subject, key.as_str()],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                ))
            },
        )
        .optional()
        .map_err(unavailable)?;
    stored
        .map(|(request, receipt, checksum)| Ok((request, read_document(receipt, checksum)?)))
        .transpose()
}

fn replay(db: &Connection, subject: &str, key: &Digest, binding: &Digest) -> Result<Option<Value>> {
    match previous_receipt(db, subject, key)? {
        Some((previous, receipt)) if previous == binding.as_str() => Ok(Some(receipt)),
        Some(_) => Err(Error::IdempotencyConflict),
        None => Ok(None),
    }
}

fn read_document(document: String, expected_digest: String) -> Result<Value> {
    let value = json::parse(document.as_bytes()).map_err(unavailable)?;
    if Digest::of_bytes(document.as_bytes()).as_str() != expected_digest
        || json::canonical(&value).map_err(unavailable)? != document
    {
        return Err(Error::Unavailable);
    }
    Ok(value)
}

fn request_digest(call: &ContractCall<'_>, change: Change, fields: &Map) -> Result<Digest> {
    digest(&json::obj([
        ("operation", json::s(change.name())),
        ("scope", scope_value(call.scope)),
        ("subject", json::s(call.subject)),
        ("fields", Value::Obj(fields.clone())),
    ]))
}

fn revision(db: &Connection) -> Result<i64> {
    db.query_row(
        "SELECT COALESCE(max(revision), 0) FROM contract_audit",
        [],
        |row| row.get(0),
    )
    .map_err(unavailable)
}

fn next_revision(db: &Connection, expected: i64) -> Result<i64> {
    let current = revision(db)?;
    if current != expected {
        return Err(Error::RevisionConflict);
    }
    if current >= MAX_REVISION {
        return Err(Error::LimitExceeded);
    }
    Ok(current + 1)
}

fn scope_value(scope: &Scope) -> Value {
    json::obj([
        ("tenant", json::s(&scope.tenant)),
        ("project", json::s(&scope.project)),
    ])
}

fn custom_id(
    scope: &Scope,
    id: &str,
    approved_pack: impl FnOnce(&str) -> Result<bool>,
) -> Result<()> {
    if core_reserved(id) {
        return Err(Error::ProtectedContract);
    }
    let Some((namespace, tail)) = id.split_once(':') else {
        return Err(Error::InvalidRequest);
    };
    let Some((owner, slug)) = tail.split_once(':') else {
        return Err(Error::InvalidRequest);
    };
    if core_reserved(slug) {
        return Err(Error::ProtectedContract);
    }
    if !valid_label(owner)
        || slug.is_empty()
        || slug.len() > 64
        || !slug.as_bytes()[0].is_ascii_lowercase()
        || !slug
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-')
    {
        return Err(Error::InvalidRequest);
    }
    match namespace {
        "tenant" if owner == scope.tenant => Ok(()),
        "pack" if approved_pack(owner)? => Ok(()),
        "tenant" | "pack" => Err(Error::NotFound),
        _ => Err(Error::InvalidRequest),
    }
}

fn take(fields: &mut Map, name: &str) -> Result<Value> {
    fields.remove(name).ok_or(Error::InvalidRequest)
}

fn string(fields: &mut Map, name: &str) -> Result<String> {
    match take(fields, name)? {
        Value::Str(value) if !value.is_empty() => Ok(value),
        _ => Err(Error::InvalidRequest),
    }
}

fn expected_revision(fields: &mut Map) -> Result<i64> {
    take(fields, "expected_revision")?
        .as_int()
        .filter(|&revision| (0..=json::MAX_SAFE_INTEGER).contains(&revision))
        .ok_or(Error::InvalidRequest)
}

fn key(fields: &mut Map) -> Result<Digest> {
    let key = string(fields, "idempotency_key")?;
    if !valid_label(&key) {
        return Err(Error::InvalidRequest);
    }
    Ok(Digest::of_bytes(key.as_bytes()))
}

fn empty(fields: &Map) -> Result<()> {
    if fields.is_empty() {
        Ok(())
    } else {
        Err(Error::InvalidRequest)
    }
}

fn validation_report(diagnostics: Value) -> Value {
    json::obj([
        (
            "valid",
            Value::Bool(diagnostics.as_arr().is_some_and(<[Value]>::is_empty)),
        ),
        ("diagnostics", diagnostics),
    ])
}

fn digest(value: &Value) -> Result<Digest> {
    json::digest(value).map_err(|_| Error::InvalidRequest)
}

fn profile(path: &Path) -> Result<u32> {
    for version in [SCHEMA_VERSION, PACK_SCHEMA_VERSION] {
        if sqlite_header_matches(path, APPLICATION_ID, version)? {
            return Ok(version);
        }
    }
    Err(Error::Unavailable)
}

fn scoped_connection(path: &Path, scope: &Scope, write: bool) -> Result<Connection> {
    let profile = profile(path)?;
    let db = connect(path, write)?;
    let marker: u32 = db
        .pragma_query_value(None, "application_id", |row| row.get(0))
        .map_err(unavailable)?;
    let version: u32 = db
        .pragma_query_value(None, "user_version", |row| row.get(0))
        .map_err(unavailable)?;
    if marker != APPLICATION_ID || version != profile {
        return Err(Error::Unavailable);
    }
    let stored = db
        .query_row(
            "SELECT tenant, project FROM contract_owner WHERE singleton = 1",
            [],
            |row| {
                Ok(Scope {
                    tenant: row.get(0)?,
                    project: row.get(1)?,
                })
            },
        )
        .map_err(unavailable)?;
    if stored != *scope {
        return Err(Error::NotFound);
    }
    Ok(db)
}

fn check_open(mut db: Connection) -> Result<()> {
    let read = db.transaction().map_err(unavailable)?;
    let broken_link = read
        .query_row("PRAGMA foreign_key_check", [], |_| Ok(()))
        .optional()
        .map_err(unavailable)?
        .is_some();
    let (entries, revision): (i64, i64) = read
        .query_row(
            "SELECT count(*), COALESCE(max(revision), 0) FROM contract_audit",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .map_err(unavailable)?;
    if broken_link || entries != revision {
        return Err(Error::Unavailable);
    }
    Ok(())
}

fn connect(path: &Path, write: bool) -> Result<Connection> {
    if !fs::symlink_metadata(path).map_err(unavailable)?.is_file() {
        return Err(Error::Unavailable);
    }
    let flags = if write {
        OpenFlags::SQLITE_OPEN_READ_WRITE
    } else {
        OpenFlags::SQLITE_OPEN_READ_ONLY
    };
    let db = Connection::open_with_flags(path, flags | OpenFlags::SQLITE_OPEN_NOFOLLOW)
        .map_err(unavailable)?;
    db.busy_timeout(Duration::from_secs(3))
        .map_err(unavailable)?;
    db.set_limit(Limit::SQLITE_LIMIT_LENGTH, 64 * 1024)
        .map_err(unavailable)?;
    db.set_limit(Limit::SQLITE_LIMIT_ATTACHED, 0)
        .map_err(unavailable)?;
    db.execute_batch(
        "PRAGMA foreign_keys = ON;
         PRAGMA trusted_schema = OFF;
         PRAGMA synchronous = FULL;
         PRAGMA mmap_size = 0;
         PRAGMA cache_size = -256;",
    )
    .map_err(unavailable)?;
    Ok(db)
}

fn unavailable<E>(_: E) -> Error {
    Error::Unavailable
}

fn compatibility_result(status: &str, prior: Value, changes: Vec<Value>) -> Value {
    json::obj([
        ("status", json::s(status)),
        ("previous_version", prior),
        ("changes", Value::Arr(changes)),
    ])
}

#[derive(Default)]
struct Compatibility {
    unknown: bool,
    changes: Vec<Value>,
}

impl Compatibility {
    fn breaking(&mut self, code: &str, pointer: &str) {
        self.changes.push(json::obj([
            ("code", json::s(code)),
            ("pointer", json::s(pointer)),
        ]));
    }
}

fn pointer(parent: &str, member: &str) -> String {
    let escaped = member.replace('~', "~0").replace('/', "~1");
    format!("{parent}/{escaped}")
}

fn compare_schema(old: &Value, new: &Value, at: &str, comparison: &mut Compatibility) {
    if old == new {
        return;
    }
    let (Some(old), Some(new)) = (old.as_obj(), new.as_obj()) else {
        comparison.unknown = true;
        return;
    };
    let understood = [
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "minItems",
        "maxItems",
    ];
    if old
        .keys()
        .chain(new.keys())
        .any(|key| !understood.contains(&key.as_str()) && old.get(key) != new.get(key))
    {
        comparison.unknown = true;
    }
    let previous_types = types(old.get("type"));
    let next_types = types(new.get("type"));
    if !previous_types.is_subset(&next_types) {
        comparison.breaking("TYPE_NARROWED", &pointer(at, "type"));
    }
    let previous_required = strings(old.get("required"));
    for property in strings(new.get("required")).difference(&previous_required) {
        comparison.breaking(
            "REQUIRED_FIELD_ADDED",
            &pointer(&pointer(at, "properties"), property),
        );
    }
    let old_open = old.get("additionalProperties") != Some(&Value::Bool(false));
    let new_open = new.get("additionalProperties") != Some(&Value::Bool(false));
    if old_open && !new_open {
        comparison.breaking(
            "ADDITIONAL_PROPERTIES_CLOSED",
            &pointer(at, "additionalProperties"),
        );
    }
    compare_properties(old, new, &pointer(at, "properties"), old_open, comparison);
    match (old.get("items"), new.get("items")) {
        (Some(before), Some(after)) => {
            compare_schema(before, after, &pointer(at, "items"), comparison)
        }
        (None, Some(_)) => comparison.breaking("ITEMS_CONSTRAINED", &pointer(at, "items")),
        _ => {}
    }
    for label in [
        "minLength",
        "minimum",
        "minItems",
        "maxLength",
        "maximum",
        "maxItems",
    ] {
        let before = old.get(label).and_then(Value::as_int);
        let after = new.get(label).and_then(Value::as_int);
        let tighter = if label.starts_with("min") {
            after > before
        } else {
            after.is_some_and(|after| before.is_none_or(|before| after < before))
        };
        if tighter {
            comparison.breaking("BOUND_TIGHTENED", &pointer(at, label));
        }
    }
}

fn compare_properties(
    old: &Map,
    new: &Map,
    at: &str,
    old_open: bool,
    comparison: &mut Compatibility,
) {
    let previous = old.get("properties").and_then(Value::as_obj);
    let next = new.get("properties").and_then(Value::as_obj);
    for (property, before) in previous.into_iter().flatten() {
        let location = pointer(at, property);
        if let Some(after) = next.and_then(|properties| properties.get(property)) {
            compare_schema(before, after, &location, comparison);
        } else {
            comparison.breaking("FIELD_REMOVED", &location);
        }
    }
    if old_open {
        for (property, _) in next.into_iter().flatten() {
            if previous.is_none_or(|properties| !properties.contains_key(property)) {
                comparison.breaking("OPEN_FIELD_CONSTRAINED", &pointer(at, property));
            }
        }
    }
}

fn types(value: Option<&Value>) -> BTreeSet<&str> {
    let mut types = match value {
        Some(Value::Str(name)) => BTreeSet::from([name.as_str()]),
        None => BTreeSet::from([
            "array", "boolean", "integer", "null", "number", "object", "string",
        ]),
        _ => strings(value),
    };
    if types.contains("number") {
        types.insert("integer");
    }
    types
}

fn strings(value: Option<&Value>) -> BTreeSet<&str> {
    value
        .and_then(Value::as_arr)
        .unwrap_or_default()
        .iter()
        .filter_map(Value::as_str)
        .collect()
}

#[cfg(test)]
#[path = "../tests/contract_registry/unit.rs"]
mod tests;
