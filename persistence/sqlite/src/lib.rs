//! Writ V2 local ledger profile on SQLite (M3-01).
//!
//! **Single-owner coordination** (docs/implementation/M3-START-PLAN.md
//! "Coordination boundary"): opening a ledger takes an exclusive OS lock on
//! `<ledger>.owner` for the lifetime of the [`SqliteLedger`]; a second instance
//! (broker or reconciler) fails closed with [`StoreError::OwnedElsewhere`]
//! instead of waiting. The lock is released by the operating system when the
//! process exits or is killed. After locking, the instance increments
//! `meta.owner_epoch` and every write transaction checks, inside the
//! transaction, that the stored epoch is still its own ([`StoreError::Fenced`]
//! otherwise), so a stale instance cannot commit after another owner started.
//! Network filesystems are unsupported (not detected).
//!
//! Engine and settings follow D-023 (SQLite 3.54.1, WAL, `synchronous=FULL`,
//! `busy_timeout=5000`, `foreign_keys=ON`, `trusted_schema=OFF`).

use std::fs::{File, OpenOptions, TryLockError};
use std::path::{Path, PathBuf};

use rusqlite::{Connection, ErrorCode, Transaction, TransactionBehavior};
pub use writ_broker::store::{LedgerStore, StoreError};

/// The production schema, applied once to a new database inside one transaction.
pub const SCHEMA: &str = include_str!("schema.sql");
/// The schema version this crate writes and accepts.
pub const SCHEMA_VERSION: &str = "writ-ledger-1";
const TABLES: [&str; 9] = [
    "meta",
    "grants",
    "receipts",
    "receipt_events",
    "grant_uses",
    "audit_refusals",
    "counters",
    "adapter_consumed_permits",
    "adapter_effects",
];

/// A SQLite ledger owned by this process.
pub struct SqliteLedger {
    // Field order matters (no custom Drop): fields drop in declaration order, so the
    // connection closes before the ownership lock is released.
    conn: Connection,
    epoch: u64,
    _lock: Option<File>,
}

fn unavailable(category: &str, code: &str) -> StoreError {
    StoreError::Unavailable {
        category: category.to_string(),
        code: code.to_string(),
    }
}

fn code_name(e: &rusqlite::Error) -> &'static str {
    match e {
        rusqlite::Error::SqliteFailure(f, _) => match f.code {
            ErrorCode::DatabaseBusy => "SQLITE_BUSY",
            ErrorCode::DatabaseLocked => "SQLITE_LOCKED",
            ErrorCode::ReadOnly => "SQLITE_READONLY",
            ErrorCode::SystemIoFailure => "SQLITE_IOERR",
            ErrorCode::DatabaseCorrupt => "SQLITE_CORRUPT",
            ErrorCode::DiskFull => "SQLITE_FULL",
            ErrorCode::CannotOpen => "SQLITE_CANTOPEN",
            ErrorCode::NotADatabase => "SQLITE_NOTADB",
            ErrorCode::ConstraintViolation => "SQLITE_CONSTRAINT",
            _ => "SQLITE_ERROR",
        },
        _ => "SQLITE_ERROR",
    }
}

fn lock_path(path: &Path) -> PathBuf {
    let mut p = path.as_os_str().to_owned();
    p.push(".owner");
    PathBuf::from(p)
}

fn read_epoch(tx: &Transaction<'_>) -> rusqlite::Result<Option<u64>> {
    let v: Option<String> = tx
        .query_row(
            "SELECT value FROM meta WHERE key = 'owner_epoch'",
            [],
            |r| r.get(0),
        )
        .map(Some)
        .or_else(|e| match e {
            rusqlite::Error::QueryReturnedNoRows => Ok(None),
            other => Err(other),
        })?;
    Ok(v.and_then(|s| s.parse::<u64>().ok()))
}

impl SqliteLedger {
    /// Take ownership of the ledger at `path` (created if absent) and open it.
    ///
    /// Order: ownership lock; connection-only settings; one `BEGIN IMMEDIATE`
    /// that creates the schema (new database) or verifies it, then increments
    /// the owner epoch; then `journal_mode=WAL` (read back) and
    /// `synchronous=FULL`. An existing file that fails verification is not
    /// switched to WAL or otherwise changed.
    pub fn open(path: &Path) -> Result<SqliteLedger, StoreError> {
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(lock_path(path))
            .map_err(|_| unavailable("ownership", "io"))?;
        match lock.try_lock() {
            Ok(()) => {}
            Err(TryLockError::WouldBlock) => return Err(StoreError::OwnedElsewhere),
            Err(TryLockError::Error(_)) => return Err(unavailable("ownership", "io")),
        }
        let mut conn = Connection::open(path).map_err(|e| unavailable("open", code_name(&e)))?;
        conn.execute_batch(
            "PRAGMA busy_timeout = 5000; PRAGMA foreign_keys = ON; PRAGMA trusted_schema = OFF;",
        )
        .map_err(|e| unavailable("open", code_name(&e)))?;
        let epoch = {
            let tx = conn
                .transaction_with_behavior(TransactionBehavior::Immediate)
                .map_err(|e| unavailable("initialize", code_name(&e)))?;
            let has_meta: bool = tx
                .query_row("SELECT EXISTS (SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta')", [], |r| r.get(0))
                .map_err(|e| unavailable("initialize", code_name(&e)))?;
            if has_meta {
                let version: Option<String> = tx
                    .query_row(
                        "SELECT value FROM meta WHERE key = 'schema_version'",
                        [],
                        |r| r.get(0),
                    )
                    .ok();
                if version.as_deref() != Some(SCHEMA_VERSION) {
                    return Err(unavailable("initialize", "schema"));
                }
                for t in TABLES {
                    let exists: bool = tx
                        .query_row("SELECT EXISTS (SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?1)", [t], |r| r.get(0))
                        .map_err(|e| unavailable("initialize", code_name(&e)))?;
                    if !exists {
                        return Err(unavailable("initialize", "schema"));
                    }
                }
            } else {
                tx.execute_batch(SCHEMA)
                    .map_err(|e| unavailable("initialize", code_name(&e)))?;
            }
            let current = read_epoch(&tx)
                .map_err(|e| unavailable("initialize", code_name(&e)))?
                .ok_or_else(|| unavailable("initialize", "schema"))?;
            let next = current
                .checked_add(1)
                .ok_or_else(|| unavailable("initialize", "schema"))?;
            tx.execute(
                "UPDATE meta SET value = ?1 WHERE key = 'owner_epoch'",
                [next.to_string()],
            )
            .map_err(|e| unavailable("initialize", code_name(&e)))?;
            tx.commit()
                .map_err(|e| unavailable("initialize", code_name(&e)))?;
            next
        };
        let mode: String = conn
            .query_row("PRAGMA journal_mode = WAL", [], |r| r.get(0))
            .map_err(|e| unavailable("initialize", code_name(&e)))?;
        if mode != "wal" {
            return Err(unavailable("initialize", "SQLITE_ERROR"));
        }
        conn.execute_batch("PRAGMA synchronous = FULL;")
            .map_err(|e| unavailable("initialize", code_name(&e)))?;
        Ok(SqliteLedger {
            conn,
            epoch,
            _lock: Some(lock),
        })
    }

    /// Run `body` in one fenced `BEGIN IMMEDIATE` transaction named `name`.
    ///
    /// The stored owner epoch is checked first, inside the transaction; a
    /// mismatch rolls back and returns [`StoreError::Fenced`]. A failing body
    /// or commit rolls back and returns [`StoreError::Unavailable`] with the
    /// category `transaction <name>`.
    pub fn write<T>(
        &mut self,
        name: &str,
        body: impl FnOnce(&Transaction<'_>) -> rusqlite::Result<T>,
    ) -> Result<T, StoreError> {
        let category = format!("transaction {name}");
        let tx = self
            .conn
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|e| unavailable(&category, code_name(&e)))?;
        let stored = read_epoch(&tx)
            .map_err(|e| unavailable(&category, code_name(&e)))?
            .ok_or_else(|| unavailable(&category, "schema"))?;
        if stored != self.epoch {
            return Err(StoreError::Fenced {
                mine: self.epoch,
                stored,
            });
        }
        let out = body(&tx).map_err(|e| unavailable(&category, code_name(&e)))?;
        tx.commit()
            .map_err(|e| unavailable(&category, code_name(&e)))?;
        Ok(out)
    }

    /// Read-only access for queries outside a write transaction.
    pub fn read<T>(
        &self,
        body: impl FnOnce(&Connection) -> rusqlite::Result<T>,
    ) -> Result<T, StoreError> {
        body(&self.conn).map_err(|e| unavailable("read", code_name(&e)))
    }
}

impl LedgerStore for SqliteLedger {
    fn owner_epoch(&self) -> u64 {
        self.epoch
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;

    fn tmp(name: &str) -> PathBuf {
        let d =
            std::env::temp_dir().join(format!("writ-ledger-unit-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d.join("ledger.db")
    }

    #[test]
    fn second_open_in_the_same_process_is_refused_and_epochs_increase() {
        let db = tmp("same");
        let a = SqliteLedger::open(&db).unwrap();
        assert_eq!(a.owner_epoch(), 1);
        assert_eq!(
            SqliteLedger::open(&db).err(),
            Some(StoreError::OwnedElsewhere)
        );
        drop(a);
        let b = SqliteLedger::open(&db).unwrap();
        assert_eq!(b.owner_epoch(), 2);
    }

    #[test]
    fn stale_owner_is_fenced_after_a_new_owner_starts() {
        let db = tmp("fence");
        let mut a = SqliteLedger::open(&db).unwrap();
        a.write("audit", |tx| {
            tx.execute("INSERT INTO audit_refusals (code) VALUES ('X')", [])
        })
        .unwrap();
        // Simulate a lost lock (unsupported filesystem or manual interference).
        a._lock = None;
        let mut b = SqliteLedger::open(&db).unwrap();
        assert_eq!(b.owner_epoch(), 2);
        assert_eq!(
            a.write("audit", |tx| tx
                .execute("INSERT INTO audit_refusals (code) VALUES ('Y')", []))
                .err(),
            Some(StoreError::Fenced { mine: 1, stored: 2 })
        );
        b.write("audit", |tx| {
            tx.execute("INSERT INTO audit_refusals (code) VALUES ('Z')", [])
        })
        .unwrap();
        let codes: Vec<String> = b
            .read(|c| {
                let mut s = c.prepare("SELECT code FROM audit_refusals ORDER BY seq")?;
                let rows = s
                    .query_map([], |r| r.get(0))?
                    .collect::<rusqlite::Result<Vec<String>>>();
                rows
            })
            .unwrap();
        assert_eq!(codes, vec!["X".to_string(), "Z".to_string()]);
    }

    #[test]
    fn wrong_schema_is_refused_and_not_switched_to_wal() {
        let db = tmp("schema");
        {
            let c = Connection::open(&db).unwrap();
            c.execute_batch("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL); INSERT INTO meta VALUES ('schema_version', '1');").unwrap();
        }
        assert_eq!(
            SqliteLedger::open(&db).err(),
            Some(StoreError::Unavailable {
                category: "initialize".into(),
                code: "schema".into()
            })
        );
        let c = Connection::open(&db).unwrap();
        let mode: String = c
            .query_row("PRAGMA journal_mode", [], |r| r.get(0))
            .unwrap();
        assert_eq!(mode, "delete");
    }

    #[test]
    fn settings_are_the_d023_ones() {
        let db = tmp("settings");
        let l = SqliteLedger::open(&db).unwrap();
        let got: (String, i64, i64, i64, i64) = l
            .read(|c| {
                Ok((
                    c.query_row("PRAGMA journal_mode", [], |r| r.get(0))?,
                    c.query_row("PRAGMA synchronous", [], |r| r.get(0))?,
                    c.query_row("PRAGMA busy_timeout", [], |r| r.get(0))?,
                    c.query_row("PRAGMA foreign_keys", [], |r| r.get(0))?,
                    c.query_row("PRAGMA trusted_schema", [], |r| r.get(0))?,
                ))
            })
            .unwrap();
        assert_eq!(got, ("wal".to_string(), 2, 5000, 1, 0));
        let version: String = l
            .read(|c| c.query_row("SELECT sqlite_version()", [], |r| r.get(0)))
            .unwrap();
        assert_eq!(version, "3.54.1");
        // The pinned D-023 build, not a bundled copy: its own compile options.
        let opts: Vec<String> = l
            .read(|c| {
                let mut s = c.prepare("PRAGMA compile_options")?;
                let rows = s
                    .query_map([], |r| r.get(0))?
                    .collect::<rusqlite::Result<Vec<String>>>();
                rows
            })
            .unwrap();
        for want in ["OMIT_LOAD_EXTENSION", "DQS=0", "THREADSAFE=1"] {
            assert!(
                opts.iter().any(|o| o == want),
                "{want} missing from {opts:?}"
            );
        }
    }
}
