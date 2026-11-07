//! SQLite broker ledger (`--ledger sqlite`, m1/fixtures/broker/LEDGER-SQLITE.md,
//! D-023): connection settings, schema creation and one function per logical
//! transaction. Each write transaction is `BEGIN IMMEDIATE`; dropping a
//! `Transaction` without `commit` rolls it back.
//!
//! SIMULATED, not verified: the effect adapter's consumed permits and effect
//! counter live in the same file (no external system); power-loss durability
//! is not tested.

use crate::broker::{refused, replayed, result};
use crate::json::{obj, s, Value};
use crate::time::Instant;
use rusqlite::{Connection, OptionalExtension, Transaction, TransactionBehavior};
use std::fs::OpenOptions;
use std::io::ErrorKind;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

pub const SCHEMA: &str = include_str!("../../fixtures/broker/ledger.sql");
pub const DRIVER: &str = "rusqlite 0.40.2";

#[derive(Debug)]
pub struct DbError(pub String);

impl From<rusqlite::Error> for DbError {
    fn from(e: rusqlite::Error) -> Self {
        DbError(e.to_string())
    }
}

type R<T> = Result<T, DbError>;
type Key = (String, String);

/// Run a statement that may return rows (most pragmas) and discard them.
fn pragma(c: &Connection, sql: &str) -> R<()> {
    let mut st = c.prepare(sql)?;
    let mut rows = st.query([])?;
    while rows.next()?.is_some() {}
    Ok(())
}

/// Open a connection and apply the settings in the specified order.
pub fn open(path: &Path) -> R<Connection> {
    let c = Connection::open(path)?;
    let mode: String = c.query_row("PRAGMA journal_mode = WAL", [], |r| r.get(0))?;
    if mode != "wal" {
        return Err(DbError(format!("journal_mode is {mode}, not wal")));
    }
    for sql in ["PRAGMA synchronous = FULL", "PRAGMA busy_timeout = 5000", "PRAGMA foreign_keys = ON", "PRAGMA trusted_schema = OFF"] {
        pragma(&c, sql)?;
    }
    Ok(c)
}

fn begin(c: &Connection) -> R<Transaction<'_>> {
    Ok(Transaction::new_unchecked(c, TransactionBehavior::Immediate)?)
}

/// Create the schema in one transaction unless it already exists (a
/// `--ledger-db` file is never reset).
pub fn ensure_schema(c: &Connection) -> R<()> {
    let tx = begin(c)?;
    let present: i64 = tx.query_row("SELECT count(*) FROM sqlite_schema WHERE type = 'table' AND name = 'meta'", [], |r| r.get(0))?;
    if present == 0 {
        tx.execute_batch(SCHEMA)?;
    }
    tx.commit()?;
    Ok(())
}

static SEQUENCE: AtomicU64 = AtomicU64::new(0);

/// A new, previously nonexistent file `<pid>-<sequence>.db` in `dir`.
pub fn new_db_in(dir: &Path) -> R<PathBuf> {
    loop {
        let path = dir.join(format!("{}-{}.db", std::process::id(), SEQUENCE.fetch_add(1, Ordering::Relaxed)));
        match OpenOptions::new().write(true).create_new(true).open(&path) {
            Ok(_) => return Ok(path),
            Err(e) if e.kind() == ErrorKind::AlreadyExists => continue,
            Err(e) => return Err(DbError(format!("{}: {e}", path.display()))),
        }
    }
}

/// Grant row as read in T-reserve.
struct GrantRow {
    binding: String,
    expires: Instant,
    max_uses: i64,
    uses: i64,
}

/// T-reserve. `None`: the receipt is `reserved` (new or resumed) and the
/// operation continues with T-dispatch; `Some`: the operation's result.
pub fn reserve(c: &Connection, key: &Key, fingerprint: &str, grant: Option<&str>, binding: &str, now: Instant, crash_before: bool) -> R<Option<Value>> {
    let tx = begin(c)?;
    let found: Option<(String, Option<String>, String)> = tx
        .query_row(
            "SELECT fingerprint, grant_label, status FROM receipts WHERE tenant_id = ?1 AND idempotency_key = ?2",
            (&key.0, &key.1),
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .optional()?;
    if let Some((fp, label, status)) = found {
        if fp != fingerprint || label.as_deref() != grant {
            return Ok(Some(refused("IDEMPOTENCY_CONFLICT")));
        }
        return match status.as_str() {
            "observed_success" => Ok(Some(replayed("effect_completed"))),
            "observed_failure" => Ok(Some(replayed("effect_failed"))),
            "dispatched" | "unknown_reconcile" => Ok(Some(refused("RECONCILIATION_REQUIRED"))),
            "reserved" => Ok(None),
            other => Err(DbError(format!("unknown receipt status {other}"))),
        };
    }
    if crash_before {
        return Ok(Some(result("crashed")));
    }
    if let Some(label) = grant {
        let row = tx
            .query_row("SELECT binding, expires_s, expires_ns, max_uses, uses FROM grants WHERE label = ?1", [label], |r| {
                Ok(GrantRow { binding: r.get(0)?, expires: (r.get(1)?, r.get(2)?), max_uses: r.get(3)?, uses: r.get(4)? })
            })
            .optional()?;
        let Some(g) = row else { return Ok(Some(refused("GRANT_UNKNOWN"))) };
        if g.binding != binding {
            return Ok(Some(refused("GRANT_MISMATCH")));
        }
        if now >= g.expires {
            return Ok(Some(refused("GRANT_EXPIRED")));
        }
        if g.uses >= g.max_uses || tx.execute("UPDATE grants SET uses = uses + 1 WHERE label = ?1 AND uses < max_uses", [label])? != 1 {
            return Ok(Some(refused("GRANT_EXHAUSTED")));
        }
        tx.execute("INSERT INTO grant_uses (label, tenant_id, idempotency_key) VALUES (?1, ?2, ?3)", (label, &key.0, &key.1))?;
    }
    tx.execute(
        "INSERT INTO receipts (tenant_id, idempotency_key, fingerprint, grant_label, status) VALUES (?1, ?2, ?3, ?4, 'reserved')",
        (&key.0, &key.1, fingerprint, grant),
    )?;
    tx.execute("INSERT INTO receipt_events (tenant_id, idempotency_key, status, cause) VALUES (?1, ?2, 'reserved', 'reserve')", (&key.0, &key.1))?;
    tx.commit()?;
    Ok(None)
}

/// Move a receipt from `from` to `to` (exactly one row) and append its event.
fn transition(tx: &Transaction, key: &Key, from: &str, to: &str, cause: &str) -> R<()> {
    let n = tx.execute(
        "UPDATE receipts SET status = ?3 WHERE tenant_id = ?1 AND idempotency_key = ?2 AND status = ?4",
        (&key.0, &key.1, to, from),
    )?;
    if n != 1 {
        return Err(DbError(format!("receipt not {from}")));
    }
    tx.execute("INSERT INTO receipt_events (tenant_id, idempotency_key, status, cause) VALUES (?1, ?2, ?3, ?4)", (&key.0, &key.1, to, cause))?;
    Ok(())
}

/// T-dispatch: the new permit number.
pub fn dispatch(c: &Connection, key: &Key) -> R<u64> {
    let tx = begin(c)?;
    if tx.execute("UPDATE counters SET value = value + 1 WHERE name = 'next_permit'", [])? != 1 {
        return Err(DbError("next_permit counter missing".into()));
    }
    let n: i64 = tx.query_row("SELECT value FROM counters WHERE name = 'next_permit'", [], |r| r.get(0))?;
    transition(&tx, key, "reserved", "dispatched", "dispatch")?;
    tx.commit()?;
    u64::try_from(n).map_err(|_| DbError(format!("next_permit {n}")))
}

/// T-adapter (simulated): `check` is the permit verification (fingerprint,
/// MAC, expiry) giving the permit id. `Ok(true)` applied, `Ok(false)`
/// consumed without effect, `Err(code)` refused with nothing written.
pub fn adapter(c: &Connection, check: impl FnOnce() -> Result<String, &'static str>, fails: bool) -> R<Result<bool, &'static str>> {
    let tx = begin(c)?;
    let id = match check() {
        Ok(id) => id,
        Err(code) => return Ok(Err(code)),
    };
    let consumed: bool = tx.query_row("SELECT EXISTS (SELECT 1 FROM adapter_consumed_permits WHERE permit_id = ?1)", [&id], |r| r.get(0))?;
    if consumed {
        return Ok(Err("PERMIT_REPLAYED"));
    }
    tx.execute("INSERT INTO adapter_consumed_permits (permit_id) VALUES (?1)", [&id])?;
    if !fails {
        tx.execute("INSERT INTO adapter_effects (permit_id) VALUES (?1)", [&id])?;
    }
    tx.commit()?;
    Ok(Ok(!fails))
}

/// T-observe.
pub fn observe(c: &Connection, key: &Key, success: bool) -> R<()> {
    let tx = begin(c)?;
    transition(&tx, key, "dispatched", if success { "observed_success" } else { "observed_failure" }, "observe")?;
    tx.commit()?;
    Ok(())
}

/// approve's transaction: `false` if the label is already in use.
pub fn insert_grant(c: &Connection, label: &str, binding: &str, expires: Instant) -> R<bool> {
    let tx = begin(c)?;
    let exists: bool = tx.query_row("SELECT EXISTS (SELECT 1 FROM grants WHERE label = ?1)", [label], |r| r.get(0))?;
    if exists {
        return Ok(false);
    }
    tx.execute(
        "INSERT INTO grants (label, binding, expires_s, expires_ns, max_uses, uses) VALUES (?1, ?2, ?3, ?4, 1, 0)",
        (label, binding, expires.0, expires.1),
    )?;
    tx.commit()?;
    Ok(true)
}

/// reconcile: every `dispatched` receipt becomes `unknown_reconcile`.
pub fn reconcile(c: &Connection) -> R<i64> {
    let tx = begin(c)?;
    tx.execute(
        "INSERT INTO receipt_events (tenant_id, idempotency_key, status, cause) \
         SELECT tenant_id, idempotency_key, 'unknown_reconcile', 'reconcile' FROM receipts WHERE status = 'dispatched' ORDER BY tenant_id, idempotency_key",
        [],
    )?;
    let n = tx.execute("UPDATE receipts SET status = 'unknown_reconcile' WHERE status = 'dispatched'", [])?;
    tx.commit()?;
    Ok(n as i64)
}

/// One audited refusal.
pub fn refuse(c: &Connection, code: &str) -> R<()> {
    let tx = begin(c)?;
    tx.execute("INSERT INTO audit_refusals (code) VALUES (?1)", [code])?;
    tx.commit()?;
    Ok(())
}

fn count(c: &Connection, table: &str) -> R<i64> {
    Ok(c.query_row(&format!("SELECT count(*) FROM {table}"), [], |r| r.get(0))?)
}

fn pairs(c: &Connection, sql: &str) -> R<Vec<(String, Value)>> {
    let mut st = c.prepare(sql)?;
    let rows = st.query_map([], |r| Ok((r.get::<_, String>(0)?, Value::Int(r.get(1)?))))?;
    Ok(rows.collect::<Result<_, _>>()?)
}

/// `final` read from the database in one read transaction.
pub fn final_state(c: &Connection) -> R<Value> {
    let tx = Transaction::new_unchecked(c, TransactionBehavior::Deferred)?;
    let v = obj(vec![
        ("effects_applied", Value::Int(count(&tx, "adapter_effects")?)),
        ("grant_uses", Value::Obj(pairs(&tx, "SELECT label, uses FROM grants ORDER BY label")?)),
        ("receipts", Value::Obj(pairs(&tx, "SELECT status, count(*) FROM receipts GROUP BY status ORDER BY status")?)),
        ("consumed_permits", Value::Int(count(&tx, "adapter_consumed_permits")?)),
        ("refusals", Value::Int(count(&tx, "audit_refusals")?)),
    ]);
    tx.commit()?;
    Ok(v)
}

/// `proto sqlite-info`: engine identity and the settings read back from a
/// fresh connection to a new temporary database (removed afterwards).
pub fn info() -> R<Value> {
    let path = new_db_in(&std::env::temp_dir())?;
    let out = info_at(&path);
    let _ = std::fs::remove_file(&path);
    out
}

fn info_at(path: &Path) -> R<Value> {
    ensure_schema(&open(path)?)?;
    let c = open(path)?;
    let text = |sql: &str| -> R<String> { Ok(c.query_row(sql, [], |r| r.get(0))?) };
    let int = |sql: &str| -> R<Value> { Ok(Value::Int(c.query_row(sql, [], |r| r.get(0))?)) };
    let mut st = c.prepare("PRAGMA compile_options")?;
    let options = st.query_map([], |r| r.get::<_, String>(0))?.collect::<Result<Vec<_>, _>>()?;
    Ok(obj(vec![
        ("sqlite_version", s(&text("SELECT sqlite_version()")?)),
        ("source_id", s(&text("SELECT sqlite_source_id()")?)),
        ("compile_options", Value::Arr(options.iter().map(|o| s(o)).collect())),
        (
            "settings",
            obj(vec![
                ("journal_mode", s(&text("PRAGMA journal_mode")?)),
                ("synchronous", int("PRAGMA synchronous")?),
                ("busy_timeout", int("PRAGMA busy_timeout")?),
                ("foreign_keys", int("PRAGMA foreign_keys")?),
                ("trusted_schema", int("PRAGMA trusted_schema")?),
            ]),
        ),
        ("schema_version", s(&text("SELECT value FROM meta WHERE key = 'schema_version'")?)),
        ("driver", s(DRIVER)),
    ]))
}
