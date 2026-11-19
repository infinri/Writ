use std::{
    fs::{self, OpenOptions},
    io::Read,
    os::unix::fs::OpenOptionsExt,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    thread::{self, ThreadId},
    time::{Duration, Instant},
};

use rusqlite::{
    limits::Limit, params, Connection, OpenFlags, OptionalExtension, Row, TransactionBehavior,
};
use writ_kernel::json::{self, Digest};
use writ_policy_publication_lab::{Error as RegistryError, Intent, ManagedIntentSource};

use crate::{
    Change, Draft, DraftRef, Error, History, Mutation, Receipt, Result, Scope, Version,
    MAX_CANDIDATE,
};

const SCHEMA: &str = include_str!("store.sql");
const APPLICATION_ID: u32 = 0x5752_4431;
const SCHEMA_VERSION: u32 = 1;
const MAX_SCOPE_VERSIONS: i64 = 1024;
const MAX_STORE_VERSIONS: i64 = 8192;
const MAX_HISTORY: usize = 50;
const QUERY_TIMEOUT: Duration = Duration::from_secs(1);
const BUSY_TIMEOUT: Duration = Duration::from_millis(250);

/// A disposable draft database selected by the trusted application host.
#[derive(Clone)]
pub struct Store {
    path: PathBuf,
    dispatch: Arc<Mutex<Option<DispatchIdentity>>>,
}

#[derive(PartialEq, Eq)]
struct DispatchIdentity {
    fingerprint: Digest,
    presentation: Digest,
    thread: ThreadId,
}

impl DispatchIdentity {
    fn new(intent: &Intent, presentation: &[u8]) -> std::result::Result<Self, RegistryError> {
        Ok(Self {
            fingerprint: intent.fingerprint()?,
            presentation: Digest::of_bytes(presentation),
            thread: thread::current().id(),
        })
    }
}

struct LiveDispatch<'a>(&'a Mutex<Option<DispatchIdentity>>);

impl Drop for LiveDispatch<'_> {
    fn drop(&mut self) {
        if let Ok(mut active) = self.0.lock() {
            *active = None;
        }
    }
}

impl ManagedIntentSource for Store {
    fn verify(
        &self,
        intent: &Intent,
        presentation: &[u8],
    ) -> std::result::Result<(), RegistryError> {
        let identity = DispatchIdentity::new(intent, presentation)?;
        if self
            .dispatch
            .lock()
            .map_err(|_| RegistryError::Storage)?
            .as_ref()
            != Some(&identity)
        {
            return Err(RegistryError::Authority);
        }
        crate::publication::verify_registered_intent(self, intent).map_err(|error| match error {
            Error::RevisionConflict => RegistryError::Conflict,
            Error::Unauthorized | Error::NotFound => RegistryError::Authority,
            _ => RegistryError::Storage,
        })
    }
}

impl Store {
    pub(crate) fn pack_dispatch_identity(&self) -> Result<String> {
        let db = self.connection(false)?;
        let identity: String = db
            .query_row(
                "SELECT identity FROM pack_dispatch_identity WHERE singleton = 1",
                [],
                |row| row.get(0),
            )
            .map_err(unavailable)?;
        writ_ledger_sqlite::LedgerId::parse(&identity).ok_or(Error::Unavailable)?;
        Ok(identity)
    }

    /// Create a separate management database. Existing files are refused.
    pub fn create(path: &Path) -> Result<Self> {
        OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(path)
            .map_err(unavailable)?;
        let mut db = connect(path, OpenFlags::SQLITE_OPEN_READ_WRITE)?;
        let tx = db
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(unavailable)?;
        tx.execute_batch(SCHEMA).map_err(unavailable)?;
        tx.pragma_update(None, "application_id", APPLICATION_ID)
            .map_err(unavailable)?;
        tx.pragma_update(None, "user_version", SCHEMA_VERSION)
            .map_err(unavailable)?;
        tx.commit().map_err(unavailable)?;
        Ok(Self {
            path: path.into(),
            dispatch: Arc::default(),
        })
    }

    /// Reopen a previously created management database without migrating it.
    pub fn open(path: &Path) -> Result<Self> {
        let store = Self {
            path: path.into(),
            dispatch: Arc::default(),
        };
        if let Err(error) = store.connection(false) {
            if !recoverable(path, APPLICATION_ID, SCHEMA_VERSION)? {
                return Err(error);
            }
            store.connection(true)?;
        }
        Ok(store)
    }

    #[cfg(feature = "fixtures")]
    pub fn open_without_recovery(path: &Path) -> Result<Self> {
        if !sqlite_header_matches(path, APPLICATION_ID, SCHEMA_VERSION)? {
            return Err(Error::Unavailable);
        }
        let store = Self {
            path: path.into(),
            dispatch: Arc::default(),
        };
        store.connection(false)?;
        Ok(store)
    }

    pub(crate) fn with_publication_dispatch<T>(
        &self,
        intent: &Intent,
        presentation: &[u8],
        operation: impl FnOnce() -> Result<T>,
    ) -> Result<T> {
        let identity = DispatchIdentity::new(intent, presentation).map_err(unavailable)?;
        let mut active = self.dispatch.lock().map_err(unavailable)?;
        if active.is_some() {
            return Err(Error::Unavailable);
        }
        *active = Some(identity);
        drop(active);
        let _dispatch = LiveDispatch(&self.dispatch);
        operation()
    }

    pub(crate) fn connection(&self, write: bool) -> Result<Connection> {
        let flags = if write {
            OpenFlags::SQLITE_OPEN_READ_WRITE
        } else {
            OpenFlags::SQLITE_OPEN_READ_ONLY
        };
        let db = connect(&self.path, flags)?;
        let marker: u32 = db
            .pragma_query_value(None, "application_id", |row| row.get(0))
            .map_err(unavailable)?;
        let version: u32 = db
            .pragma_query_value(None, "user_version", |row| row.get(0))
            .map_err(unavailable)?;
        if marker != APPLICATION_ID || version != SCHEMA_VERSION {
            return Err(Error::Unavailable);
        }
        Ok(db)
    }

    pub(crate) fn get(&self, scope: &Scope, draft: &DraftRef) -> Result<Draft> {
        load(&self.connection(false)?, scope, draft)
    }

    pub(crate) fn history(
        &self,
        scope: &Scope,
        id: &str,
        after: i64,
        limit: usize,
    ) -> Result<History> {
        if limit == 0 || limit > MAX_HISTORY || after < 0 {
            return Err(Error::InvalidRequest);
        }
        let mut db = self.connection(false)?;
        let tx = db.transaction().map_err(unavailable)?;
        let current = current_revision(&tx, scope, id)?;
        if current == 0 {
            return Err(Error::NotFound);
        }
        if after >= current {
            return Ok(History {
                versions: Vec::new(),
                next: None,
            });
        }
        let mut query = tx
            .prepare(
                "SELECT v.draft, v.revision, v.source_draft, v.source_revision, a.content_digest
                 FROM draft_versions v LEFT JOIN draft_audit a USING (tenant, project, draft, revision)
                 WHERE v.tenant = ?1 AND v.project = ?2 AND v.draft = ?3 AND v.revision > ?4
                 ORDER BY v.revision LIMIT ?5",
            )
            .map_err(unavailable)?;
        let mut rows = query
            .query(params![
                scope.tenant,
                scope.project,
                id,
                after,
                limit as i64
            ])
            .map_err(unavailable)?;
        let mut versions = Vec::with_capacity(limit);
        while let Some(row) = rows.next().map_err(unavailable)? {
            let version = read_version(row)?;
            if version.draft.revision != after + 1 + versions.len() as i64 {
                return Err(Error::Unavailable);
            }
            versions.push(version);
        }
        let count = (current - after).min(limit as i64);
        if versions.len() as i64 != count {
            return Err(Error::Unavailable);
        }
        let next = (after + count < current).then_some(after + count);
        Ok(History { versions, next })
    }

    pub(crate) fn mutate(
        &self,
        scope: &Scope,
        subject: &str,
        mutation: Mutation,
        binding: &[u8],
        reauthorize: impl Fn() -> Result<()>,
    ) -> Result<Receipt> {
        let mut db = self.connection(!mutation.preview)?;
        let behavior = if mutation.preview {
            TransactionBehavior::Deferred
        } else {
            TransactionBehavior::Immediate
        };
        let tx = db
            .transaction_with_behavior(behavior)
            .map_err(unavailable)?;
        let key_digest = Digest::of_bytes(mutation.key.as_bytes());
        let request_digest = Digest::of_bytes(binding);

        if !mutation.preview {
            let replay: Option<(String, i64, i64)> = tx
                .query_row(
                    "SELECT request_digest, revision, audit_id FROM draft_audit
                     WHERE tenant = ?1 AND project = ?2 AND subject = ?3 AND key_digest = ?4",
                    params![scope.tenant, scope.project, subject, key_digest.as_str()],
                    |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
                )
                .optional()
                .map_err(unavailable)?;
            if let Some((prior_binding, revision, audit_id)) = replay {
                reauthorize()?;
                if prior_binding != request_digest.as_str() {
                    return Err(Error::IdempotencyConflict);
                }
                return Ok(Receipt {
                    revision,
                    audit_id: Some(audit_id),
                });
            }
            if crate::publication::key_used(&tx, scope, subject, key_digest.as_str())? {
                return Err(Error::IdempotencyConflict);
            }
        }

        let current = current_revision(&tx, scope, &mutation.id)?;
        if current == 0 && matches!(&mutation.change, Change::Edit(_)) {
            return Err(Error::NotFound);
        }
        if mutation.expected_revision != current {
            return Err(Error::RevisionConflict);
        }
        let (operation, document, source) = match mutation.change {
            Change::Create(candidate) if current == 0 => ("create", candidate, None),
            Change::Edit(candidate) if current > 0 => ("edit", candidate, None),
            Change::Clone(source) if current == 0 => {
                let draft = load(&tx, scope, &source)?;
                ("clone", draft.candidate, Some(source))
            }
            _ => return Err(Error::InvalidRequest),
        };
        let audit_id: i64 = tx
            .query_row(
                "SELECT COALESCE(MAX(audit_id), 0) FROM draft_audit WHERE tenant = ?1 AND project = ?2",
                params![scope.tenant, scope.project],
                |row| row.get(0),
            )
            .map_err(unavailable)?;
        let store_slot: i64 = tx
            .query_row(
                "SELECT COALESCE(MAX(store_slot), 0) FROM draft_audit",
                [],
                |row| row.get(0),
            )
            .map_err(unavailable)?;
        if audit_id < current || store_slot < audit_id {
            return Err(Error::Unavailable);
        }
        if audit_id >= MAX_SCOPE_VERSIONS || store_slot >= MAX_STORE_VERSIONS {
            return Err(Error::LimitExceeded);
        }
        let candidate = json::canonical(&document).map_err(|_| Error::InvalidRequest)?;
        if candidate.len() > MAX_CANDIDATE {
            return Err(Error::LimitExceeded);
        }
        let revision = current + 1;
        if mutation.preview {
            reauthorize()?;
            return Ok(Receipt {
                revision,
                audit_id: None,
            });
        }

        let content_digest = Digest::of_bytes(candidate.as_bytes());
        let audit_id = audit_id + 1;
        tx.execute(
            "INSERT INTO draft_audit
                (store_slot, audit_id, tenant, project, draft, revision, previous_revision,
                 operation, subject, key_digest, request_digest, content_digest)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12)",
            params![
                store_slot + 1,
                audit_id,
                scope.tenant,
                scope.project,
                mutation.id,
                revision,
                current,
                operation,
                subject,
                key_digest.as_str(),
                request_digest.as_str(),
                content_digest.as_str()
            ],
        )
        .map_err(unavailable)?;
        tx.execute(
            "INSERT INTO draft_versions
                (tenant, project, draft, revision, candidate, source_draft, source_revision)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
            params![
                scope.tenant,
                scope.project,
                mutation.id,
                revision,
                candidate,
                source.as_ref().map(|draft| draft.id.as_str()),
                source.as_ref().map(|draft| draft.revision)
            ],
        )
        .map_err(unavailable)?;
        reauthorize()?;
        tx.commit().map_err(unavailable)?;
        Ok(Receipt {
            revision,
            audit_id: Some(audit_id),
        })
    }
}

pub(crate) fn recoverable(path: &Path, application_id: u32, schema_version: u32) -> Result<bool> {
    let mut journal = path.as_os_str().to_owned();
    journal.push("-journal");
    if !fs::symlink_metadata(PathBuf::from(journal)).is_ok_and(|meta| meta.is_file()) {
        return Ok(false);
    }

    sqlite_header_matches(path, application_id, schema_version)
}

pub(crate) fn sqlite_header_matches(
    path: &Path,
    application_id: u32,
    schema_version: u32,
) -> Result<bool> {
    if !fs::symlink_metadata(path).is_ok_and(|meta| meta.is_file()) {
        return Ok(false);
    }

    let mut header = [0_u8; 72];
    match fs::File::open(path).and_then(|mut file| file.read_exact(&mut header)) {
        Ok(()) => Ok(&header[..16] == b"SQLite format 3\0"
            && header[18..20] == [1, 1]
            && header[60..64] == schema_version.to_be_bytes()
            && header[68..72] == application_id.to_be_bytes()),
        Err(error) if error.kind() == std::io::ErrorKind::UnexpectedEof => Ok(false),
        Err(error) => Err(unavailable(error)),
    }
}

fn connect(path: &Path, flags: OpenFlags) -> Result<Connection> {
    if !fs::symlink_metadata(path).map_err(unavailable)?.is_file() {
        return Err(Error::Unavailable);
    }
    let mut db = Connection::open_with_flags(path, flags | OpenFlags::SQLITE_OPEN_NOFOLLOW)
        .map_err(unavailable)?;
    db.busy_timeout(BUSY_TIMEOUT).map_err(unavailable)?;
    db.set_limit(Limit::SQLITE_LIMIT_LENGTH, 64 * 1024)
        .map_err(unavailable)?;
    db.set_limit(Limit::SQLITE_LIMIT_ATTACHED, 0)
        .map_err(unavailable)?;
    let started = Instant::now();
    let mut callbacks = 0;
    db.progress_handler(
        100,
        Some(move || {
            callbacks += 1;
            callbacks >= 200 || started.elapsed() >= QUERY_TIMEOUT
        }),
    )
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

pub(crate) fn current_revision(db: &Connection, scope: &Scope, id: &str) -> Result<i64> {
    let revision: i64 = db
        .query_row(
            "SELECT COALESCE(MAX(revision), 0) FROM draft_versions
             WHERE tenant = ?1 AND project = ?2 AND draft = ?3",
            params![scope.tenant, scope.project, id],
            |row| row.get(0),
        )
        .map_err(unavailable)?;
    if !(0..=MAX_SCOPE_VERSIONS).contains(&revision) {
        return Err(Error::Unavailable);
    }
    Ok(revision)
}

pub(crate) fn load(db: &Connection, scope: &Scope, draft: &DraftRef) -> Result<Draft> {
    let mut query = db
        .prepare(
            "SELECT v.draft, v.revision, v.source_draft, v.source_revision, a.content_digest, v.candidate
             FROM draft_versions v LEFT JOIN draft_audit a USING (tenant, project, draft, revision)
             WHERE v.tenant = ?1 AND v.project = ?2 AND v.draft = ?3 AND v.revision = ?4",
        )
        .map_err(unavailable)?;
    let mut rows = query
        .query(params![
            scope.tenant,
            scope.project,
            draft.id,
            draft.revision
        ])
        .map_err(unavailable)?;
    read_draft(rows.next().map_err(unavailable)?.ok_or(Error::NotFound)?)
}

pub(crate) fn authors(db: &Connection, scope: &Scope, draft: &DraftRef) -> Result<Vec<String>> {
    let mut query = db
        .prepare(
            "WITH RECURSIVE lineage(draft, revision) AS (
             SELECT ?3, ?4
             UNION
             SELECT v.source_draft, v.source_revision
             FROM lineage l JOIN draft_versions v ON v.draft = l.draft AND v.revision <= l.revision
             WHERE v.tenant = ?1 AND v.project = ?2 AND v.source_draft IS NOT NULL
         )
         SELECT DISTINCT a.subject FROM lineage l JOIN draft_audit a
           ON a.draft = l.draft AND a.revision <= l.revision
         WHERE a.tenant = ?1 AND a.project = ?2 ORDER BY a.subject",
        )
        .map_err(unavailable)?;
    let authors = query
        .query_map(
            params![scope.tenant, scope.project, draft.id, draft.revision],
            |row| row.get(0),
        )
        .map_err(unavailable)?
        .collect::<rusqlite::Result<Vec<String>>>()
        .map_err(unavailable)?;
    if authors.is_empty() || authors.iter().any(|author| !crate::valid_label(author)) {
        return Err(Error::Unavailable);
    }
    Ok(authors)
}

fn read_draft(row: &Row<'_>) -> Result<Draft> {
    let version = read_version(row)?;
    let candidate: String = row.get(5).map_err(unavailable)?;
    if candidate.len() > MAX_CANDIDATE
        || Digest::of_bytes(candidate.as_bytes()).as_str() != version.digest
    {
        return Err(Error::Unavailable);
    }
    Ok(Draft {
        id: version.draft.id,
        revision: version.draft.revision,
        candidate: json::parse(candidate.as_bytes()).map_err(unavailable)?,
        source: version.source,
    })
}

fn read_version(row: &Row<'_>) -> Result<Version> {
    let source = match (
        row.get::<_, Option<String>>(2).map_err(unavailable)?,
        row.get::<_, Option<i64>>(3).map_err(unavailable)?,
    ) {
        (Some(id), Some(revision)) => Some(stored_ref(id, revision)?),
        (None, None) => None,
        _ => return Err(Error::Unavailable),
    };
    let digest: String = row.get(4).map_err(unavailable)?;
    let valid_digest = digest.strip_prefix("sha256:").is_some_and(|hex| {
        hex.len() == 64
            && hex
                .bytes()
                .all(|ch| ch.is_ascii_digit() || (b'a'..=b'f').contains(&ch))
    });
    if !valid_digest {
        return Err(Error::Unavailable);
    }
    Ok(Version {
        draft: stored_ref(
            row.get(0).map_err(unavailable)?,
            row.get(1).map_err(unavailable)?,
        )?,
        source,
        digest,
    })
}

fn stored_ref(id: String, revision: i64) -> Result<DraftRef> {
    if !crate::valid_label(&id) || !(1..=MAX_SCOPE_VERSIONS).contains(&revision) {
        return Err(Error::Unavailable);
    }
    Ok(DraftRef { id, revision })
}

fn unavailable<E>(_: E) -> Error {
    Error::Unavailable
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]

    use std::sync::atomic::{AtomicU64, Ordering};

    use super::*;
    use writ_kernel::json::Value;

    struct TempDb(PathBuf);

    impl TempDb {
        fn new() -> Self {
            static NEXT: AtomicU64 = AtomicU64::new(0);
            let dir = std::env::temp_dir().join(format!(
                "writ-draft-store-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir(&dir).unwrap();
            Self(dir)
        }

        fn path(&self) -> PathBuf {
            self.0.join("drafts.sqlite")
        }
    }

    impl Drop for TempDb {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn scope(project: &str) -> Scope {
        Scope {
            tenant: "tenant".into(),
            project: project.into(),
        }
    }

    fn create(preview: bool) -> Mutation {
        Mutation {
            id: "draft".into(),
            expected_revision: 0,
            key: "plain-idempotency-key".into(),
            preview,
            change: Change::Create(Value::Bool(true)),
        }
    }

    fn counts(db: &Connection) -> (i64, i64) {
        db.query_row(
            "SELECT (SELECT count(*) FROM draft_audit), (SELECT count(*) FROM draft_versions)",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .unwrap()
    }

    #[test]
    fn preview_failed_audit_and_commit_reauthorization_leave_no_rows() {
        let temp = TempDb::new();
        let path = temp.path();
        let store = Store::create(&path).unwrap();
        let db = store.connection(true).unwrap();
        let scope = scope("project");

        let preview = store
            .mutate(&scope, "author", create(true), b"binding", || Ok(()))
            .unwrap();
        assert_eq!(
            preview,
            Receipt {
                revision: 1,
                audit_id: None
            }
        );
        assert_eq!(counts(&db), (0, 0));

        db.execute_batch(
            "CREATE TRIGGER unavailable_audit BEFORE INSERT ON draft_audit
             BEGIN SELECT RAISE(ABORT, 'private diagnostic'); END;",
        )
        .unwrap();
        assert_eq!(
            store.mutate(&scope, "author", create(false), b"binding", || Ok(())),
            Err(Error::Unavailable)
        );
        assert_eq!(counts(&db), (0, 0));
        db.execute_batch("DROP TRIGGER unavailable_audit;").unwrap();

        db.execute_batch(
            "CREATE TRIGGER unavailable_version BEFORE INSERT ON draft_versions
             BEGIN SELECT RAISE(ABORT, 'private diagnostic'); END;",
        )
        .unwrap();
        assert_eq!(
            store.mutate(&scope, "author", create(false), b"binding", || Ok(())),
            Err(Error::Unavailable)
        );
        assert_eq!(counts(&db), (0, 0));
        db.execute_batch("DROP TRIGGER unavailable_version;")
            .unwrap();

        assert_eq!(
            store.mutate(&scope, "author", create(false), b"binding", || {
                Err(Error::Unauthorized)
            }),
            Err(Error::Unauthorized)
        );
        assert_eq!(counts(&db), (0, 0));

        let accepted = store
            .mutate(&scope, "author", create(false), b"binding", || Ok(()))
            .unwrap();
        assert_eq!(accepted.audit_id, Some(1));
        assert_eq!(counts(&db), (1, 1));
        drop(db);
        let restarted = Store::open(&path).unwrap();
        assert_eq!(
            restarted.mutate(&scope, "author", create(false), b"binding", || {
                Err(Error::Unauthorized)
            }),
            Err(Error::Unauthorized)
        );
        assert_eq!(
            restarted.mutate(&scope, "author", create(false), b"changed", || Ok(())),
            Err(Error::IdempotencyConflict)
        );
        assert_eq!(
            restarted
                .mutate(&scope, "author", create(false), b"binding", || Ok(()))
                .unwrap(),
            accepted
        );
        assert_eq!(counts(&restarted.connection(false).unwrap()), (1, 1));
    }

    #[test]
    fn audit_identity_and_clone_lookup_are_scope_local() {
        let temp = TempDb::new();
        let store = Store::create(&temp.path()).unwrap();
        let scope_a = scope("project-a");
        let scope_b = scope("project-b");
        let a = store
            .mutate(&scope_a, "author", create(false), b"binding", || Ok(()))
            .unwrap();
        let source = DraftRef {
            id: "draft".into(),
            revision: 1,
        };
        let clone = Mutation {
            id: "copy".into(),
            expected_revision: 0,
            key: "clone-idempotency-key".into(),
            preview: false,
            change: Change::Clone(source),
        };
        assert_eq!(
            store.mutate(&scope_b, "author", clone, b"clone-binding", || Ok(())),
            Err(Error::NotFound)
        );
        let b = store
            .mutate(&scope_b, "author", create(false), b"binding", || Ok(()))
            .unwrap();
        assert_eq!(a.audit_id, Some(1));
        assert_eq!(b.audit_id, Some(1));
        assert_eq!(counts(&store.connection(false).unwrap()), (2, 2));
    }

    #[test]
    fn restart_recovers_hot_journal_after_interrupted_writer() {
        const CHILD_PATH: &str = "WRIT_TEST_DRAFT_SQLITE_HOT_JOURNAL";
        const EXIT_DIRTY: i32 = 86;
        if let Some(path) = std::env::var_os(CHILD_PATH) {
            let store = Store::open(Path::new(&path)).unwrap();
            let db = store.connection(true).unwrap();
            db.execute_batch(
                "PRAGMA cache_size = 3; PRAGMA cache_spill = ON; BEGIN IMMEDIATE;
                 UPDATE draft_audit SET subject = 'uncommitted';",
            )
            .unwrap();
            let damaged = json::canonical(&Value::Str("x".repeat(32_000))).unwrap();
            db.execute("UPDATE draft_versions SET candidate = ?1", [damaged])
                .unwrap();
            std::process::exit(EXIT_DIRTY);
        }

        let temp = TempDb::new();
        let path = temp.path();
        let db = Store::create(&path).unwrap();
        let scope = scope("project");
        db.mutate(&scope, "author", create(false), b"binding", || Ok(()))
            .unwrap();
        let result = std::process::Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "store::tests::restart_recovers_hot_journal_after_interrupted_writer",
            ])
            .env(CHILD_PATH, &path)
            .output()
            .unwrap();
        assert_eq!(result.status.code(), Some(EXIT_DIRTY), "{result:?}");
        assert!(matches!(db.connection(false), Err(Error::Unavailable)));

        let restarted = Store::open(&path).unwrap();
        let source = DraftRef {
            id: "draft".into(),
            revision: 1,
        };
        assert_eq!(
            restarted.get(&scope, &source).unwrap().candidate,
            Value::Bool(true)
        );
        assert_eq!(
            restarted
                .mutate(&scope, "author", create(false), b"binding", || Ok(()))
                .unwrap()
                .audit_id,
            Some(1)
        );
        assert_eq!(counts(&restarted.connection(false).unwrap()), (1, 1));
    }

    #[test]
    fn foreign_database_and_missing_paths_are_refused_on_open() {
        let temp = TempDb::new();
        let path = temp.path();
        assert!(matches!(Store::open(&path), Err(Error::Unavailable)));
        assert!(!path.exists());

        let db = Connection::open(&path).unwrap();
        db.execute_batch("CREATE TABLE another_database (marker INTEGER);")
            .unwrap();
        assert!(matches!(Store::open(&path), Err(Error::Unavailable)));
        assert!(matches!(Store::create(&path), Err(Error::Unavailable)));
        db.execute("INSERT INTO another_database VALUES (1)", [])
            .unwrap();
    }
}
