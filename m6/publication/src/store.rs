use std::{
    collections::{BTreeMap, BTreeSet},
    fs::{self, OpenOptions},
    os::unix::fs::MetadataExt,
    path::{Path, PathBuf},
    sync::Mutex,
};

use rusqlite::{Connection, OpenFlags, Row, TransactionBehavior};
use writ_broker::lifecycle::OwnerLock;
use writ_kernel::{
    json::{self, Digest, Value},
    time::Timestamp,
};

use crate::{
    wire::{canonical, digest, number},
    Approval, Error, Intent, Operation, Publication, Scope,
};

const SCHEMA: &str = include_str!("store.sql");
const MANAGEMENT_SCHEMA: &str = include_str!("management.sql");
const SCHEMA_VERSION: &str = "writ-policy-registry/1";

/// A durable change in the disposable registry.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Commit {
    /// Activation revision after the change.
    pub revision: i64,
    /// Immutable publication version targeted by the change.
    pub publication_version: i64,
    /// Sequence of the committed audit event.
    pub audit_sequence: i64,
}

/// Durable provenance for a registry operation.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Audit {
    /// Monotonic sequence across activation and retirement events.
    pub sequence: i64,
    /// Activation revision after the event.
    pub revision: i64,
    /// Authorized operation.
    pub operation: Operation,
    /// Immutable publication version targeted by the operation.
    pub publication_version: i64,
    /// Digest of the complete immutable publication.
    pub publication_digest: String,
    /// Digest of the exact human authorization intent.
    pub intent_digest: String,
    /// Digest of the bound management context, when the intent came through management.
    pub management_digest: Option<String>,
    /// Authenticated scoped publisher.
    pub human: String,
    /// Consumed single-use ceremony.
    pub ceremony: String,
    /// Trusted authorization time recorded by the transaction.
    pub at: Timestamp,
}

pub(crate) struct Store {
    scope: Scope,
    connection: Mutex<Connection>,
    data_version: i64,
    selection_event: Mutex<Option<Audit>>,
    _owner: OwnerLock,
}

#[derive(Debug, Default, PartialEq, Eq)]
struct Head {
    revision: i64,
    publication_version: Option<i64>,
    audit_sequence: i64,
    audit_digest: Option<String>,
}

impl Head {
    fn after(&self, event: &Event) -> Result<Self, Error> {
        let audit = &event.audit;
        let publication_version = match audit.operation {
            Operation::Retire => self.publication_version,
            _ => Some(audit.publication_version),
        };
        let audit_digest = event.chain_digest(self.audit_digest.as_deref())?;
        Ok(Self {
            revision: audit.revision,
            publication_version,
            audit_sequence: audit.sequence,
            audit_digest: Some(audit_digest),
        })
    }

    fn next_revision(&self, operation: Operation) -> Result<i64, Error> {
        if operation == Operation::Retire {
            Ok(self.revision)
        } else {
            number(self.revision + 1, 1).map_err(|_| Error::Storage)
        }
    }

    fn next_sequence(&self) -> Result<i64, Error> {
        number(self.audit_sequence + 1, 1).map_err(|_| Error::Storage)
    }
}

struct Entry {
    publication: Publication,
    document: String,
    publication_digest: String,
    pair_digest: Digest,
    authorization_digest: Digest,
    retired: bool,
}

impl Entry {
    fn new(publication: Publication) -> Result<Self, Error> {
        Ok(Self {
            document: canonical(&publication.to_value())?,
            publication_digest: publication.fingerprint()?.to_string(),
            pair_digest: publication.pair_digest()?,
            authorization_digest: digest(&publication.authorization.payload)?,
            retired: false,
            publication,
        })
    }

    fn from_storage(document: &str, publication_digest: &str) -> Result<Self, Error> {
        let value = json::parse(document.as_bytes()).map_err(|_| Error::Recovery)?;
        let publication = Publication::from_value(&value).map_err(|_| Error::Recovery)?;
        let parsed = Self::new(publication).map_err(|_| Error::Recovery)?;
        if parsed.document != document || parsed.publication_digest != publication_digest {
            return Err(Error::Recovery);
        }
        Ok(parsed)
    }
}

struct Event {
    audit: Audit,
    issued_at: Timestamp,
    expires_at: Timestamp,
}

impl Event {
    fn from_row(row: &Row<'_>) -> Result<Self, Error> {
        let read = |_| Error::Recovery;
        Ok(Self {
            audit: Audit {
                sequence: row.get(0).map_err(read)?,
                revision: row.get(1).map_err(read)?,
                operation: Operation::parse(&row.get::<_, String>(2).map_err(read)?)
                    .map_err(incoherent)?,
                publication_version: row.get(3).map_err(read)?,
                publication_digest: row.get(4).map_err(read)?,
                intent_digest: row.get(5).map_err(read)?,
                management_digest: row.get(11).map_err(read)?,
                human: row.get(6).map_err(read)?,
                ceremony: row.get(7).map_err(read)?,
                at: stored_time(&row.get::<_, String>(8).map_err(read)?)?,
            },
            issued_at: stored_time(&row.get::<_, String>(9).map_err(read)?)?,
            expires_at: stored_time(&row.get::<_, String>(10).map_err(read)?)?,
        })
    }

    fn approval(&self) -> Approval {
        Approval {
            human: self.audit.human.clone(),
            ceremony: self.audit.ceremony.clone(),
            issued_at: self.issued_at,
            expires_at: self.expires_at,
        }
    }

    fn chain_digest(&self, previous: Option<&str>) -> Result<String, Error> {
        let audit = &self.audit;
        Ok(digest(&json::obj([
            ("version", json::s("writ-policy-audit/1")),
            ("previous", previous.map_or(Value::Null, json::s)),
            ("sequence", Value::Int(audit.sequence)),
            ("revision", Value::Int(audit.revision)),
            ("operation", json::s(audit.operation.as_str())),
            ("publication_version", Value::Int(audit.publication_version)),
            ("publication_digest", json::s(&audit.publication_digest)),
            ("intent_digest", json::s(&audit.intent_digest)),
            ("human", json::s(&audit.human)),
            ("ceremony", json::s(&audit.ceremony)),
            ("issued_at", json::s(&self.issued_at.to_string())),
            ("expires_at", json::s(&self.expires_at.to_string())),
            ("at", json::s(&audit.at.to_string())),
        ]))?
        .to_string())
    }
}

fn incoherent(_: Error) -> Error {
    Error::Recovery
}

fn stored_time(value: &str) -> Result<Timestamp, Error> {
    Timestamp::parse_rfc3339(value)
        .filter(|at| at.to_string() == value)
        .ok_or(Error::Recovery)
}

#[derive(Default)]
struct History {
    head: Head,
    publications: BTreeMap<i64, Entry>,
    audit: Vec<Audit>,
    ceremonies: BTreeSet<String>,
}

impl History {
    fn selected(&self) -> Result<Option<(i64, &Publication)>, Error> {
        self.head
            .publication_version
            .map(|version| {
                self.publications
                    .get(&version)
                    .map(|entry| (version, &entry.publication))
                    .ok_or(Error::Recovery)
            })
            .transpose()
    }

    fn check_publish(&self, version: i64, entry: &Entry) -> Result<(), Error> {
        let previous = self.publications.last_key_value().map_or(0, |(v, _)| *v);
        if version != previous + 1 {
            return Err(Error::Replay);
        }
        let same_as_active = self
            .head
            .publication_version
            .and_then(|v| self.publications.get(&v))
            .is_some_and(|current| current.authorization_digest == entry.authorization_digest);
        if self.publications.values().any(|prior| {
            prior.pair_digest == entry.pair_digest
                || (!same_as_active && prior.authorization_digest == entry.authorization_digest)
        }) {
            return Err(Error::Replay);
        }
        Ok(())
    }

    fn historical_target(&self, operation: Operation, version: i64) -> Result<&Entry, Error> {
        let entry = self.publications.get(&version).ok_or(Error::Recovery)?;
        if entry.retired {
            return Err(Error::Retired);
        }
        let current = self.head.publication_version.ok_or(Error::Recovery)?;
        match operation {
            Operation::Rollback if version < current => Ok(entry),
            Operation::Retire if version != current => Ok(entry),
            _ => Err(Error::Replay),
        }
    }

    fn apply_stored(
        &mut self,
        scope: &Scope,
        event: Event,
        pending: &mut BTreeMap<i64, Entry>,
    ) -> Result<(), Error> {
        let audit = &event.audit;
        let version = audit.publication_version;
        let publishing = if audit.operation == Operation::Publish {
            let entry = pending.remove(&version).ok_or(Error::Recovery)?;
            self.check_publish(version, &entry).map_err(incoherent)?;
            Some(entry)
        } else {
            None
        };
        let entry = match &publishing {
            Some(entry) => entry,
            None => self
                .historical_target(audit.operation, version)
                .map_err(incoherent)?,
        };
        let expected_intent = Intent::new(
            scope.clone(),
            audit.operation,
            self.head.revision,
            version,
            &entry.publication,
        )
        .map_err(incoherent)?;
        // Earlier direct retirements were recorded without typed confirmation.
        let legacy_retirement = audit.operation == Operation::Retire
            && audit.management_digest.is_none()
            && expected_intent.fingerprint().map_err(incoherent)?.as_str() == audit.intent_digest;
        let expected_intent = if audit.operation == Operation::Retire && !legacy_retirement {
            expected_intent
                .with_retirement_confirmation(&Intent::retirement_phrase(version))
                .map_err(incoherent)?
        } else {
            expected_intent
        };
        let expected_intent = match &audit.management_digest {
            Some(digest) => expected_intent
                .bind_management(digest, &audit.human)
                .map_err(incoherent)?,
            None => expected_intent,
        };
        let expected_intent = expected_intent.fingerprint().map_err(incoherent)?;
        if audit.sequence != self.head.next_sequence().map_err(incoherent)?
            || audit.revision
                != self
                    .head
                    .next_revision(audit.operation)
                    .map_err(incoherent)?
            || audit.publication_digest != entry.publication_digest
            || audit.intent_digest != expected_intent.as_str()
            || self.audit.last().is_some_and(|last| audit.at < last.at)
            || event.approval().check(audit.at).is_err()
            || !self.ceremonies.insert(audit.ceremony.clone())
        {
            return Err(Error::Recovery);
        }
        self.head = self.head.after(&event).map_err(incoherent)?;
        if let Some(entry) = publishing {
            self.publications.insert(version, entry);
        }
        if audit.operation == Operation::Retire {
            self.publications
                .get_mut(&version)
                .ok_or(Error::Recovery)?
                .retired = true;
        }
        self.audit.push(event.audit);
        Ok(())
    }
}

fn suffix(path: &Path, value: &str) -> PathBuf {
    let mut name = path.as_os_str().to_owned();
    name.push(value);
    name.into()
}

fn connect(path: &Path) -> rusqlite::Result<Connection> {
    connection_with_mode(path, OpenFlags::SQLITE_OPEN_READ_WRITE)
}

fn connection_with_mode(path: &Path, mode: OpenFlags) -> rusqlite::Result<Connection> {
    let connection = Connection::open_with_flags(path, mode | OpenFlags::SQLITE_OPEN_NOFOLLOW)?;
    connection.execute_batch(
        "PRAGMA foreign_keys = ON; PRAGMA trusted_schema = OFF; PRAGMA synchronous = FULL;",
    )?;
    Ok(connection)
}

type Ddl = BTreeMap<String, String>;

fn table_schema(connection: &Connection) -> rusqlite::Result<Ddl> {
    let mut query = connection.prepare(
        "SELECT name, sql FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'",
    )?;
    let tables = query
        .query_map([], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        })?
        .collect();
    tables
}

fn inspection_budget(connection: &Connection, max_rows: i64) -> Result<(), Error> {
    let pages: i64 = connection
        .query_row("PRAGMA page_count", [], |row| row.get(0))
        .map_err(|_| Error::Recovery)?;
    let size: i64 = connection
        .query_row("PRAGMA page_size", [], |row| row.get(0))
        .map_err(|_| Error::Recovery)?;
    if !pages
        .checked_mul(size)
        .is_some_and(|bytes| (0..=32 * 1024 * 1024).contains(&bytes))
    {
        return Err(Error::DependencyUnavailable);
    }
    for (table, key) in [
        ("publications", "version"),
        ("publication_audit", "sequence"),
        ("management_intents", "sequence"),
    ] {
        if table == "management_intents" && !has_management_table(connection)? {
            continue;
        }
        let query =
            format!("SELECT EXISTS(SELECT 1 FROM {table} ORDER BY {key} LIMIT 1 OFFSET ?1)");
        let exceeds: bool = connection
            .query_row(&query, [max_rows], |row| row.get(0))
            .map_err(|_| Error::Recovery)?;
        if exceeds {
            return Err(Error::DependencyUnavailable);
        }
    }
    Ok(())
}

fn verify_schema(connection: &Connection) -> Result<(), Error> {
    let schema = Connection::open_in_memory().map_err(|_| Error::Storage)?;
    schema.execute_batch(SCHEMA).map_err(|_| Error::Storage)?;
    let journal: String = connection
        .query_row("PRAGMA journal_mode", [], |row| row.get(0))
        .map_err(|_| Error::Recovery)?;
    let check: String = connection
        .query_row("PRAGMA quick_check", [], |row| row.get(0))
        .map_err(|_| Error::Recovery)?;
    let actual = table_schema(connection).map_err(|_| Error::Recovery)?;
    let base = table_schema(&schema).map_err(|_| Error::Storage)?;
    schema
        .execute_batch(MANAGEMENT_SCHEMA)
        .map_err(|_| Error::Storage)?;
    let extended = table_schema(&schema).map_err(|_| Error::Storage)?;
    if journal != "wal" || check != "ok" || (actual != base && actual != extended) {
        return Err(Error::Recovery);
    }
    Ok(())
}

fn durable_head(connection: &Connection, scope: &Scope) -> Result<Head, Error> {
    let mut query = connection
        .prepare("SELECT singleton, schema_version, tenant, project, revision, publication_version, audit_sequence, audit_digest FROM registry_head")
        .map_err(|_| Error::Recovery)?;
    let mut rows = query.query([]).map_err(|_| Error::Recovery)?;
    let row = rows
        .next()
        .map_err(|_| Error::Recovery)?
        .ok_or(Error::Recovery)?;
    let read = |_| Error::Recovery;
    let singleton: i64 = row.get(0).map_err(read)?;
    let schema: String = row.get(1).map_err(read)?;
    let tenant: String = row.get(2).map_err(read)?;
    let project: String = row.get(3).map_err(read)?;
    let head = Head {
        revision: row.get(4).map_err(read)?,
        publication_version: row.get(5).map_err(read)?,
        audit_sequence: row.get(6).map_err(read)?,
        audit_digest: row.get(7).map_err(read)?,
    };
    if singleton != 1 || schema != SCHEMA_VERSION || rows.next().map_err(read)?.is_some() {
        return Err(Error::Recovery);
    }
    if tenant != scope.tenant() || project != scope.project() {
        return Err(Error::Scope);
    }
    Ok(head)
}

fn has_management_table(connection: &Connection) -> Result<bool, Error> {
    connection
        .query_row(
            "SELECT EXISTS (SELECT 1 FROM sqlite_schema WHERE name = 'management_intents' AND type = 'table')",
            [],
            |row| row.get(0),
        )
        .map_err(|_| Error::Recovery)
}

fn recover(connection: &Connection, scope: &Scope) -> Result<History, Error> {
    let head = durable_head(connection, scope)?;
    let read = |_| Error::Recovery;
    let mut pending = BTreeMap::new();
    let mut query = connection
        .prepare("SELECT version, document, publication_digest FROM publications ORDER BY version")
        .map_err(read)?;
    let mut rows = query.query([]).map_err(read)?;
    while let Some(row) = rows.next().map_err(read)? {
        let version = number(row.get(0).map_err(read)?, 1).map_err(incoherent)?;
        let document = row.get::<_, String>(1).map_err(read)?;
        let digest = row.get::<_, String>(2).map_err(read)?;
        let entry = Entry::from_storage(&document, &digest)?;
        if pending.insert(version, entry).is_some() {
            return Err(Error::Recovery);
        }
    }
    let mut history = History::default();
    let extended = has_management_table(connection)?;
    let audit_query = if extended {
        "SELECT a.sequence, revision, operation, publication_version, publication_digest,
                intent_digest, human, ceremony, at, issued_at, expires_at, m.digest
         FROM publication_audit a LEFT JOIN management_intents m USING (sequence)
         ORDER BY a.sequence"
    } else {
        "SELECT sequence, revision, operation, publication_version, publication_digest,
                intent_digest, human, ceremony, at, issued_at, expires_at, NULL
         FROM publication_audit ORDER BY sequence"
    };
    let mut query = connection.prepare(audit_query).map_err(read)?;
    let mut rows = query.query([]).map_err(read)?;
    let mut managed = 0;
    while let Some(row) = rows.next().map_err(read)? {
        let event = Event::from_row(row)?;
        managed += i64::from(event.audit.management_digest.is_some());
        history.apply_stored(scope, event, &mut pending)?;
    }
    if extended {
        let count: i64 = connection
            .query_row("SELECT count(*) FROM management_intents", [], |row| {
                row.get(0)
            })
            .map_err(read)?;
        if count != managed {
            return Err(Error::Recovery);
        }
    }
    if history.head != head || !pending.is_empty() {
        return Err(Error::Recovery);
    }
    Ok(history)
}

fn data_version(connection: &Connection) -> Result<i64, Error> {
    connection
        .query_row("PRAGMA data_version", [], |row| row.get(0))
        .map_err(|_| Error::Recovery)
}

impl Store {
    pub(crate) fn create(path: &Path, scope: Scope) -> Result<Self, Error> {
        let name = path.file_name().ok_or(Error::Storage)?;
        let parent = path
            .parent()
            .filter(|path| !path.as_os_str().is_empty())
            .unwrap_or_else(|| Path::new("."));
        let path = fs::canonicalize(parent)
            .map_err(|_| Error::Storage)?
            .join(name);
        let owner = OwnerLock::acquire(&suffix(&path, ".owner")).map_err(|_| Error::Storage)?;
        let sidecars = ["-journal", "-wal", "-shm"].map(|extension| suffix(&path, extension));
        if sidecars
            .iter()
            .any(|path| fs::symlink_metadata(path).is_ok())
        {
            return Err(Error::Storage);
        }
        OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)
            .map_err(|_| Error::Storage)?;
        let initialize = || -> rusqlite::Result<Connection> {
            let mut connection = connect(&path)?;
            let tx = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
            tx.execute_batch(SCHEMA)?;
            tx.execute(
                "INSERT INTO registry_head VALUES (1, ?1, ?2, ?3, 0, NULL, 0, NULL)",
                (SCHEMA_VERSION, scope.tenant(), scope.project()),
            )?;
            tx.commit()?;
            let mode: String =
                connection.query_row("PRAGMA journal_mode = WAL", [], |row| row.get(0))?;
            if mode != "wal" {
                return Err(rusqlite::Error::InvalidQuery);
            }
            Ok(connection)
        };
        match initialize() {
            Ok(connection) => Ok(Self {
                scope,
                data_version: data_version(&connection)?,
                selection_event: Mutex::new(None),
                connection: Mutex::new(connection),
                _owner: owner,
            }),
            Err(_) => {
                let _ = fs::remove_file(path);
                for path in sidecars {
                    let _ = fs::remove_file(path);
                }
                Err(Error::Storage)
            }
        }
    }

    pub(crate) fn open(path: &Path, scope: Scope) -> Result<Self, Error> {
        Self::open_with_budget(path, scope, None)
    }

    pub(crate) fn open_for_inspection(path: &Path, scope: Scope) -> Result<Self, Error> {
        Self::open_with_budget(path, scope, Some(256))
    }

    fn open_with_budget(path: &Path, scope: Scope, max_rows: Option<i64>) -> Result<Self, Error> {
        let path = fs::canonicalize(path).map_err(|_| Error::Recovery)?;
        let metadata = fs::metadata(&path).map_err(|_| Error::Recovery)?;
        if !metadata.is_file() || metadata.nlink() != 1 {
            return Err(Error::Recovery);
        }
        let owner = OwnerLock::acquire(&suffix(&path, ".owner")).map_err(|_| Error::Storage)?;
        let mode = if max_rows.is_some() {
            OpenFlags::SQLITE_OPEN_READ_ONLY
        } else {
            OpenFlags::SQLITE_OPEN_READ_WRITE
        };
        let mut connection = connection_with_mode(&path, mode).map_err(|_| Error::Recovery)?;
        let snapshot = connection.transaction().map_err(|_| Error::Recovery)?;
        let data_version = data_version(&snapshot)?;
        if let Some(limit) = max_rows {
            inspection_budget(&snapshot, limit)?;
        }
        verify_schema(&snapshot)?;
        let selection_event = recover(&snapshot, &scope)?
            .audit
            .into_iter()
            .rev()
            .find(|event| event.operation != Operation::Retire);
        snapshot.commit().map_err(|_| Error::Recovery)?;
        Ok(Self {
            scope,
            connection: Mutex::new(connection),
            data_version,
            selection_event: Mutex::new(selection_event),
            _owner: owner,
        })
    }

    pub(crate) fn scope(&self) -> &Scope {
        &self.scope
    }

    pub(crate) fn change(
        &self,
        intent: &Intent,
        candidate: Option<&Publication>,
        validate: impl Fn(
            &Publication,
            Option<(i64, &Publication)>,
        ) -> Result<(Approval, Timestamp), Error>,
    ) -> Result<Commit, Error> {
        if intent.scope() != &self.scope {
            return Err(Error::Scope);
        }
        let operation = intent.operation();
        let version = intent.publication_version();
        if (operation == Operation::Publish) != candidate.is_some() {
            return Err(Error::Format);
        }
        let mut connection = self.connection.lock().map_err(|_| Error::Storage)?;
        let tx = connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|_| Error::Storage)?;
        let history = recover(&tx, &self.scope)?;
        if intent.expected_revision() != history.head.revision {
            return Err(Error::Conflict);
        }
        let candidate = candidate.cloned().map(Entry::new).transpose()?;
        let entry = match &candidate {
            Some(entry) => {
                history.check_publish(version, entry)?;
                entry
            }
            None => history.historical_target(operation, version)?,
        };
        let exact = Intent::new(
            self.scope.clone(),
            operation,
            history.head.revision,
            version,
            &entry.publication,
        )?;
        let exact = if operation == Operation::Retire {
            exact.with_retirement_confirmation(
                intent.retirement_confirmation().ok_or(Error::Format)?,
            )?
        } else {
            exact
        };
        let exact = match intent.management() {
            Some(context) => exact.bind_management(&context.digest, &context.publisher)?,
            None => exact,
        };
        if &exact != intent {
            return Err(Error::Authority);
        }
        let selected = history.selected()?;
        let (approval, at) = validate(&entry.publication, selected)?;
        approval.check(at)?;
        if !intent.matches_approval(&approval) {
            return Err(Error::Authority);
        }
        if history.audit.last().is_some_and(|audit| at < audit.at) {
            return Err(Error::Time);
        }
        if history.ceremonies.contains(&approval.ceremony) {
            return Err(Error::Replay);
        }
        let sequence = history.head.next_sequence()?;
        let revision = history.head.next_revision(operation)?;
        let event = Event {
            audit: Audit {
                sequence,
                revision,
                operation,
                publication_version: version,
                publication_digest: entry.publication_digest.clone(),
                intent_digest: intent.fingerprint()?.to_string(),
                management_digest: intent.management().map(|context| context.digest.clone()),
                human: approval.human.clone(),
                ceremony: approval.ceremony.clone(),
                at,
            },
            issued_at: approval.issued_at,
            expires_at: approval.expires_at,
        };
        let head = history.head.after(&event)?;
        let storage = |_| Error::Storage;
        if candidate.is_some() {
            tx.execute(
                "INSERT INTO publications VALUES (?1, ?2, ?3)",
                (version, &entry.document, &entry.publication_digest),
            )
            .map_err(storage)?;
        }
        tx.execute(
            "INSERT INTO publication_audit VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)",
            (
                sequence,
                revision,
                operation.as_str(),
                version,
                &event.audit.publication_digest,
                &event.audit.intent_digest,
                &event.audit.human,
                &event.audit.ceremony,
                event.issued_at.to_string(),
                event.expires_at.to_string(),
                at.to_string(),
            ),
        )
        .map_err(storage)?;
        if let Some(digest) = &event.audit.management_digest {
            if !has_management_table(&tx)? {
                tx.execute_batch(MANAGEMENT_SCHEMA).map_err(storage)?;
            }
            tx.execute(
                "INSERT INTO management_intents VALUES (?1, ?2)",
                (sequence, digest),
            )
            .map_err(storage)?;
        }
        tx.execute(
            "UPDATE registry_head SET revision = ?1, publication_version = ?2, audit_sequence = ?3, audit_digest = ?4 WHERE singleton = 1",
            (revision, head.publication_version, sequence, &head.audit_digest),
        )
        .map_err(storage)?;
        if recover(&tx, &self.scope).map_err(|_| Error::Storage)?.head != head {
            return Err(Error::Storage);
        }
        let (final_approval, final_time) = validate(&entry.publication, selected)?;
        if final_approval != approval {
            return Err(Error::Authority);
        }
        if final_time < at {
            return Err(Error::Time);
        }
        final_approval.check(final_time)?;
        let mut selection_event = self.selection_event.lock().map_err(|_| Error::Storage)?;
        tx.commit().map_err(storage)?;
        if operation != Operation::Retire {
            *selection_event = Some(event.audit);
        }
        Ok(Commit {
            revision,
            publication_version: version,
            audit_sequence: sequence,
        })
    }

    pub(crate) fn preview(
        &self,
        operation: Operation,
        expected_revision: i64,
        version: i64,
        candidate: Option<&Publication>,
        validate: impl FnOnce(&Publication, Option<(i64, &Publication)>) -> Result<(), Error>,
    ) -> Result<Publication, Error> {
        if (operation == Operation::Publish) != candidate.is_some() {
            return Err(Error::Format);
        }
        let connection = self.connection.lock().map_err(|_| Error::Storage)?;
        let history = recover(&connection, &self.scope)?;
        if history.head.revision != expected_revision {
            return Err(Error::Conflict);
        }
        let candidate = candidate.cloned().map(Entry::new).transpose()?;
        let entry = match &candidate {
            Some(entry) => {
                history.check_publish(version, entry)?;
                entry
            }
            None => history.historical_target(operation, version)?,
        };
        history.head.next_revision(operation)?;
        history.head.next_sequence()?;
        validate(&entry.publication, history.selected()?)?;
        Ok(entry.publication.clone())
    }

    pub(crate) fn with_selected<R>(
        &self,
        use_selection: impl FnOnce(i64, i64, &Publication, &[Audit]) -> Result<R, Error>,
    ) -> Result<Option<R>, Error> {
        let connection = self.connection.lock().map_err(|_| Error::Storage)?;
        let history = recover(&connection, &self.scope)?;
        let Some((version, publication)) = history.selected()? else {
            return Ok(None);
        };
        let result = use_selection(history.head.revision, version, publication, &history.audit);
        drop(connection);
        result.map(Some)
    }

    /// The owner verified the chain at open and before each commit. Reject any writes by
    /// another connection and read the selected document and keyset page in one snapshot.
    pub(crate) fn lifecycle_page<R>(
        &self,
        after: i64,
        limit: usize,
        project: impl FnOnce(
            Option<(&Audit, &Publication)>,
            Vec<Audit>,
            Option<i64>,
        ) -> Result<R, Error>,
    ) -> Result<R, Error> {
        number(after, 0)?;
        if !(1..=50).contains(&limit) {
            return Err(Error::Format);
        }
        let mut db = self.connection.lock().map_err(|_| Error::Storage)?;
        let tx = db.transaction().map_err(|_| Error::Storage)?;
        let head = durable_head(&tx, &self.scope)?;
        if data_version(&tx)? != self.data_version {
            return Err(Error::Recovery);
        }
        let activation = self.selection_event.lock().map_err(|_| Error::Recovery)?;
        let publication = if let Some(audit) = activation.as_ref() {
            if head.publication_version != Some(audit.publication_version)
                || head.revision != audit.revision
            {
                return Err(Error::Recovery);
            }
            let document: String = tx
                .query_row(
                    "SELECT document FROM publications WHERE version=?1",
                    [audit.publication_version],
                    |row| row.get(0),
                )
                .map_err(|_| Error::Recovery)?;
            Some(Entry::from_storage(&document, &audit.publication_digest)?.publication)
        } else if head == Head::default() {
            None
        } else {
            return Err(Error::Recovery);
        };
        let mut query = tx
            .prepare(
                "SELECT sequence, revision, operation, publication_version, publication_digest,
            intent_digest, human, ceremony, at, issued_at, expires_at, NULL
            FROM publication_audit WHERE sequence > ?1 ORDER BY sequence LIMIT ?2",
            )
            .map_err(|_| Error::Storage)?;
        let mut rows = query
            .query((after, limit as i64 + 1))
            .map_err(|_| Error::Storage)?;
        let mut events = Vec::new();
        while let Some(row) = rows.next().map_err(|_| Error::Storage)? {
            events.push(Event::from_row(row)?.audit);
        }
        let next = if events.len() > limit {
            events.truncate(limit);
            events.last().map(|event| event.sequence)
        } else {
            None
        };
        project(activation.as_ref().zip(publication.as_ref()), events, next)
    }

    pub(crate) fn audit(&self) -> Result<Vec<Audit>, Error> {
        let connection = self.connection.lock().map_err(|_| Error::Storage)?;
        Ok(recover(&connection, &self.scope)?.audit)
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;
    use crate::SignedPolicy;
    use std::{
        cell::Cell,
        process::Command,
        sync::atomic::{AtomicUsize, Ordering},
    };

    struct Scratch(PathBuf);

    impl Scratch {
        fn new() -> Self {
            static NEXT_ID: AtomicUsize = AtomicUsize::new(0);
            let id = NEXT_ID.fetch_add(1, Ordering::Relaxed);
            let directory = std::env::temp_dir().join(format!(
                "writ-publication-store-{}-{id}",
                std::process::id()
            ));
            fs::create_dir(&directory).unwrap();
            Self(directory)
        }

        fn database(&self) -> PathBuf {
            self.0.join("registry.sqlite3")
        }
    }

    impl Drop for Scratch {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn scope() -> Scope {
        Scope::new("m6-lab", "offline-proof").unwrap()
    }

    fn now() -> Timestamp {
        Timestamp::parse_strict("2026-11-19T12:30:00Z").unwrap()
    }

    fn approval(ceremony: &str) -> (Approval, Timestamp) {
        let at = now();
        (
            Approval {
                human: "fixture-publisher".into(),
                ceremony: ceremony.into(),
                issued_at: at.plus_seconds(-1),
                expires_at: at.plus_seconds(10),
            },
            at,
        )
    }

    fn publication(authorization: &str, acceptance: &str, prerequisites: &str) -> Publication {
        let signed = |name| SignedPolicy {
            payload: json::s(name),
            envelope: Value::Null,
        };
        Publication {
            authorization: signed(authorization),
            acceptance: signed(acceptance),
            prerequisites: json::s(prerequisites),
        }
    }

    fn change(
        store: &Store,
        operation: Operation,
        revision: i64,
        version: i64,
        target: &Publication,
        ceremony: &str,
    ) -> Result<Commit, Error> {
        let intent = Intent::new(scope(), operation, revision, version, target).unwrap();
        let candidate = (operation == Operation::Publish).then_some(target);
        let intent = if operation == Operation::Retire {
            intent
                .with_retirement_confirmation(&Intent::retirement_phrase(version))
                .unwrap()
        } else {
            intent
        };
        store.change(&intent, candidate, |_, _| Ok(approval(ceremony)))
    }

    fn selected(store: &Store) -> (i64, i64, Publication) {
        store
            .with_selected(|revision, version, publication, _| {
                Ok((revision, version, publication.clone()))
            })
            .unwrap()
            .unwrap()
    }

    #[test]
    fn inspection_budget_bounds_recovery_without_limiting_publication() {
        let scratch = Scratch::new();
        let path = scratch.database();
        let store = Store::create(&path, scope()).unwrap();
        let first = publication("auth-1", "accept-1", "proof-1");
        let second = publication("auth-2", "accept-2", "proof-2");
        change(&store, Operation::Publish, 0, 1, &first, "first").unwrap();
        change(&store, Operation::Publish, 1, 2, &second, "second").unwrap();
        drop(store);
        assert!(matches!(
            Store::open_with_budget(&path, scope(), Some(1)),
            Err(Error::DependencyUnavailable)
        ));
        let bounded = Store::open_with_budget(&path, scope(), Some(2)).unwrap();
        assert_eq!(bounded.audit().unwrap().len(), 2);
        drop(bounded);
        let ordinary = Store::open(&path, scope()).unwrap();
        change(&ordinary, Operation::Rollback, 2, 1, &first, "third").unwrap();
        drop(ordinary);
        assert!(matches!(
            Store::open_with_budget(&path, scope(), Some(2)),
            Err(Error::DependencyUnavailable)
        ));
        assert_eq!(
            Store::open(&path, scope()).unwrap().audit().unwrap().len(),
            3
        );
    }

    #[test]
    fn managed_intent_commits_and_recovers_only_with_its_exact_human_context() {
        let scratch = Scratch::new();
        let path = scratch.database();
        let store = Store::create(&path, scope()).unwrap();
        let first = publication("auth-1", "accept-1", "proof-1");
        let binding = Digest::of_bytes(b"management-context");
        let managed = Intent::new(scope(), Operation::Publish, 0, 1, &first)
            .unwrap()
            .with_management(binding.clone(), &approval("c1").0.human)
            .unwrap();
        let wrong = managed
            .clone()
            .with_management(binding.clone(), "different-human")
            .unwrap();
        assert_eq!(
            store.change(&wrong, Some(&first), |_, _| Ok(approval("c1"))),
            Err(Error::Authority)
        );
        assert!(store.audit().unwrap().is_empty());
        let commit = store
            .change(&managed, Some(&first), |_, _| Ok(approval("c1")))
            .unwrap();
        drop(store);
        let reopened = Store::open(&path, scope()).unwrap();
        let audit = reopened.audit().unwrap();
        assert_eq!(audit[0].sequence, commit.audit_sequence);
        assert_eq!(
            audit[0].intent_digest,
            managed.fingerprint().unwrap().as_str()
        );
        assert_eq!(
            audit[0].management_digest.as_deref(),
            Some(binding.as_str())
        );
        let db = reopened.connection.lock().unwrap();
        db.execute("DELETE FROM management_intents", []).unwrap();
        drop(db);
        assert!(matches!(reopened.audit(), Err(Error::Recovery)));
    }

    #[test]
    fn immutable_history_replay_and_retirement() {
        let scratch = Scratch::new();
        let path = scratch.database();
        let store = Store::create(&path, scope()).unwrap();
        assert_eq!(store.scope(), &scope());
        let first = publication("auth-1", "accept-1", "proof-1");
        let second = publication("auth-2", "accept-2", "proof-2");
        let replay_pair = publication("auth-1", "accept-1", "proof-reordered");
        let replay_authorization = publication("auth-1", "accept-3", "proof-1");
        let third = publication("auth-3", "accept-3", "proof-3");

        assert_eq!(store.with_selected(|_, _, _, _| Ok(())), Ok(None));
        change(&store, Operation::Publish, 0, 1, &first, "c1").unwrap();
        assert_eq!(
            change(&store, Operation::Publish, 0, 2, &second, "c2"),
            Err(Error::Conflict)
        );
        assert_eq!(
            change(&store, Operation::Publish, 1, 2, &replay_pair, "c2"),
            Err(Error::Replay)
        );
        assert_eq!(
            change(&store, Operation::Publish, 1, 2, &second, "c1"),
            Err(Error::Replay)
        );
        change(&store, Operation::Publish, 1, 2, &second, "c2").unwrap();
        assert_eq!(
            change(&store, Operation::Publish, 2, 1, &first, "c3"),
            Err(Error::Replay)
        );
        assert_eq!(
            change(
                &store,
                Operation::Publish,
                2,
                3,
                &replay_authorization,
                "c3"
            ),
            Err(Error::Replay)
        );
        assert_eq!(
            change(&store, Operation::Retire, 2, 2, &second, "c3"),
            Err(Error::Replay)
        );
        assert_eq!(
            change(&store, Operation::Rollback, 2, 1, &replay_pair, "c3"),
            Err(Error::Authority)
        );
        let rollback = change(&store, Operation::Rollback, 2, 1, &first, "c3").unwrap();
        assert_eq!((rollback.revision, rollback.publication_version), (3, 1));
        assert_eq!(selected(&store), (3, 1, first.clone()));
        let retire = change(&store, Operation::Retire, 3, 2, &second, "c4").unwrap();
        assert_eq!((retire.revision, retire.audit_sequence), (3, 4));
        change(&store, Operation::Publish, 3, 3, &third, "c5").unwrap();
        assert_eq!(
            change(&store, Operation::Rollback, 4, 2, &second, "c6"),
            Err(Error::Retired)
        );
        assert_eq!(selected(&store), (4, 3, third.clone()));
        let audit = store.audit().unwrap();
        assert_eq!(
            audit.iter().map(|event| event.revision).collect::<Vec<_>>(),
            [1, 2, 3, 3, 4]
        );
        assert_eq!(audit[2].operation, Operation::Rollback);
        assert_eq!(
            audit[2].publication_digest,
            first.fingerprint().unwrap().as_str()
        );
        drop(store);

        let recovered = Store::open(&path, scope()).unwrap();
        assert_eq!(recovered.audit().unwrap(), audit);
        assert_eq!(selected(&recovered), (4, 3, third));
        assert_eq!(
            change(&recovered, Operation::Rollback, 4, 2, &second, "c6"),
            Err(Error::Retired)
        );
    }

    #[test]
    fn legacy_unmanaged_retirement_history_stays_readable() {
        let scratch = Scratch::new();
        let path = scratch.database();
        let store = Store::create(&path, scope()).unwrap();
        let first = publication("auth-1", "accept-1", "proof-1");
        let second = publication("auth-2", "accept-2", "proof-2");
        change(&store, Operation::Publish, 0, 1, &first, "c1").unwrap();
        change(&store, Operation::Publish, 1, 2, &second, "c2").unwrap();

        let legacy = Intent::new(scope(), Operation::Retire, 2, 1, &first).unwrap();
        assert_eq!(
            store.change(&legacy, None, |_, _| Ok(approval("c3"))),
            Err(Error::Format)
        );

        let mut connection = store.connection.lock().unwrap();
        let tx = connection.transaction().unwrap();
        let history = recover(&tx, &scope()).unwrap();
        let (approval, at) = approval("c3");
        let event = Event {
            audit: Audit {
                sequence: 3,
                revision: 2,
                operation: Operation::Retire,
                publication_version: 1,
                publication_digest: first.fingerprint().unwrap().to_string(),
                intent_digest: legacy.fingerprint().unwrap().to_string(),
                management_digest: None,
                human: approval.human,
                ceremony: approval.ceremony,
                at,
            },
            issued_at: approval.issued_at,
            expires_at: approval.expires_at,
        };
        let head = history.head.after(&event).unwrap();
        tx.execute(
            "INSERT INTO publication_audit VALUES (3, 2, 'retire', 1, ?1, ?2, ?3, ?4, ?5, ?6, ?7)",
            (
                &event.audit.publication_digest,
                &event.audit.intent_digest,
                &event.audit.human,
                &event.audit.ceremony,
                event.issued_at.to_string(),
                event.expires_at.to_string(),
                at.to_string(),
            ),
        )
        .unwrap();
        tx.execute(
            "UPDATE registry_head SET audit_sequence = 3, audit_digest = ?1 WHERE singleton = 1",
            (&head.audit_digest,),
        )
        .unwrap();
        tx.commit().unwrap();
        drop(connection);
        drop(store);

        let reopened = Store::open(&path, scope()).unwrap();
        assert_eq!(selected(&reopened), (2, 2, second));
        assert_eq!(reopened.audit().unwrap()[2], event.audit);
        assert_eq!(
            change(&reopened, Operation::Rollback, 2, 1, &first, "c4"),
            Err(Error::Retired)
        );
    }

    #[test]
    fn staged_changes_roll_back_on_audit_storage_or_second_check_failure() {
        let scratch = Scratch::new();
        let path = scratch.database();
        let first = publication("auth-1", "accept-1", "proof-1");
        let second = publication("auth-2", "accept-2", "proof-2");
        let intent = Intent::new(scope(), Operation::Publish, 1, 2, &second).unwrap();
        let store = Store::create(&path, scope()).unwrap();
        change(&store, Operation::Publish, 0, 1, &first, "c1").unwrap();
        for (timing, table) in [
            ("BEFORE INSERT", "publications"),
            ("BEFORE INSERT", "publication_audit"),
            ("BEFORE UPDATE", "registry_head"),
        ] {
            store
                .connection
                .lock()
                .unwrap()
                .execute_batch(&format!(
                    "CREATE TEMP TRIGGER refusal {timing} ON {table}
                 BEGIN SELECT RAISE(ABORT, 'fixture'); END"
                ))
                .unwrap();
            assert_eq!(
                store.change(&intent, Some(&second), |_, _| Ok(approval("c2"))),
                Err(Error::Storage)
            );
            store
                .connection
                .lock()
                .unwrap()
                .execute_batch("DROP TRIGGER refusal")
                .unwrap();
            assert_eq!(selected(&store), (1, 1, first.clone()));
            assert_eq!(store.audit().unwrap().len(), 1);
        }

        store
            .connection
            .lock()
            .unwrap()
            .execute_batch(
                "CREATE TEMP TABLE commit_parent(id INTEGER PRIMARY KEY);
             CREATE TEMP TABLE commit_child(
                 id INTEGER REFERENCES commit_parent(id) DEFERRABLE INITIALLY DEFERRED
             );
             CREATE TEMP TRIGGER commit_refusal AFTER UPDATE ON registry_head
             BEGIN INSERT INTO commit_child VALUES (1); END",
            )
            .unwrap();
        let commit_checks = Cell::new(0);
        assert_eq!(
            store.change(&intent, Some(&second), |_, _| {
                commit_checks.set(commit_checks.get() + 1);
                Ok(approval("c2"))
            }),
            Err(Error::Storage)
        );
        assert_eq!(commit_checks.get(), 2);
        store
            .connection
            .lock()
            .unwrap()
            .execute_batch("DROP TRIGGER commit_refusal")
            .unwrap();

        assert_eq!(
            store.change(&intent, Some(&second), |_, _| {
                let (mut approval, at) = approval("c2");
                approval.issued_at = at.plus_seconds(-2);
                Ok((approval, at.plus_seconds(-1)))
            }),
            Err(Error::Time)
        );
        let checks = Cell::new(0);
        assert_eq!(
            store.change(&intent, Some(&second), |_, _| {
                checks.set(checks.get() + 1);
                if checks.get() == 1 {
                    Ok(approval("c2"))
                } else {
                    Err(Error::Trust)
                }
            }),
            Err(Error::Trust)
        );
        assert_eq!(checks.get(), 2);
        let checks = Cell::new(0);
        assert_eq!(
            store.change(&intent, Some(&second), |_, _| {
                checks.set(checks.get() + 1);
                let (approval, first_time) = approval("c2");
                if checks.get() == 1 {
                    Ok((approval, first_time))
                } else {
                    let expired = approval.expires_at;
                    Ok((approval, expired))
                }
            }),
            Err(Error::Authority)
        );
        assert_eq!(checks.get(), 2);
        drop(store);
        let recovered = Store::open(&path, scope()).unwrap();
        assert_eq!(selected(&recovered), (1, 1, first));
        let audit = recovered.audit().unwrap();
        assert_eq!(audit.len(), 1);
        assert_eq!(audit[0].ceremony, "c1");
        change(&recovered, Operation::Publish, 1, 2, &second, "c2").unwrap();
        assert_eq!(selected(&recovered), (2, 2, second));
    }

    #[test]
    fn restart_refuses_missing_audit_corrupt_documents_or_mutated_provenance() {
        let first = publication("auth-1", "accept-1", "proof-1");
        for damage in [
            "DELETE FROM publication_audit",
            "UPDATE publications SET document = 'broken'",
            "UPDATE publication_audit SET human = 'other-publisher'",
            "DROP TABLE registry_head",
        ] {
            let scratch = Scratch::new();
            let path = scratch.database();
            let store = Store::create(&path, scope()).unwrap();
            assert!(matches!(Store::open(&path, scope()), Err(Error::Storage)));
            change(&store, Operation::Publish, 0, 1, &first, "c1").unwrap();
            drop(store);
            let wrong_scope = Scope::new("m6-lab", "another-project").unwrap();
            assert!(matches!(Store::open(&path, wrong_scope), Err(Error::Scope)));
            let raw = Connection::open(&path).unwrap();
            raw.execute_batch(damage).unwrap();
            drop(raw);
            assert!(matches!(Store::open(&path, scope()), Err(Error::Recovery)));
        }
    }

    #[test]
    fn restart_around_commit() {
        const PATH: &str = "WRIT_PUBLICATION_TEST_CHILD_DB";
        const BOUNDARY: &str = "WRIT_PUBLICATION_TEST_CHILD_BOUNDARY";
        let first = publication("auth-1", "accept-1", "proof-1");
        let second = publication("auth-2", "accept-2", "proof-2");
        if let Some(path) = std::env::var_os(PATH) {
            let before_commit = std::env::var(BOUNDARY).unwrap() == "before";
            let store = Store::open(Path::new(&path), scope()).unwrap();
            let intent = Intent::new(scope(), Operation::Publish, 1, 2, &second).unwrap();
            let calls = Cell::new(0);
            store
                .change(&intent, Some(&second), |_, _| {
                    calls.set(calls.get() + 1);
                    if before_commit && calls.get() == 2 {
                        std::process::exit(70);
                    }
                    Ok(approval("c2"))
                })
                .unwrap();
            std::process::exit(71);
        }

        let scratch = Scratch::new();
        let path = scratch.database();
        let store = Store::create(&path, scope()).unwrap();
        change(&store, Operation::Publish, 0, 1, &first, "c1").unwrap();
        drop(store);
        for (boundary, code, revision, content) in
            [("before", 70, 1, first), ("after", 71, 2, second)]
        {
            let child = Command::new(std::env::current_exe().unwrap())
                .args(["--exact", "store::tests::restart_around_commit"])
                .env(PATH, &path)
                .env(BOUNDARY, boundary)
                .output()
                .unwrap();
            assert_eq!(child.status.code(), Some(code), "{child:?}");
            let reopened = Store::open(&path, scope()).unwrap();
            assert_eq!(selected(&reopened), (revision, revision, content));
            assert_eq!(reopened.audit().unwrap().last().unwrap().revision, revision);
        }
    }
}
