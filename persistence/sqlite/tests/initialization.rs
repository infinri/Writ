//! Safe creation versus open, and read-only schema verification (M3-01).
//! A refused existing file must be byte-for-byte unchanged and not switched to WAL.

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::fs;
use std::path::{Path, PathBuf};

use rusqlite::Connection;
use writ_ledger_sqlite::{LedgerStore, SqliteLedger, StoreError, SCHEMA, SCHEMA_V3};

/// A complete current (`writ-ledger-5`) ledger as `create` writes it (schema plus identity).
fn valid() -> String {
    format!("{SCHEMA}INSERT INTO meta (key, value) VALUES ('ledger_id', '0123456789abcdef0123456789abcdef');")
}

fn tmp(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("writ-ledger-init-{}-{name}", std::process::id()));
    let _ = fs::remove_dir_all(&d);
    fs::create_dir_all(&d).unwrap();
    d
}

fn schema_err() -> StoreError {
    StoreError::Unavailable {
        category: "initialize".into(),
        code: "schema".into(),
    }
}

/// Build a database with `sql`, close it, and return its bytes.
fn build(db: &Path, sql: &str, wal: bool) -> Vec<u8> {
    let c = Connection::open(db).unwrap();
    c.execute_batch(sql).unwrap();
    if wal {
        let m: String = c
            .query_row("PRAGMA journal_mode = WAL", [], |r| r.get(0))
            .unwrap();
        assert_eq!(m, "wal");
    }
    drop(c);
    fs::read(db).unwrap()
}

fn assert_refused_unchanged(db: &Path, before: &[u8], want: StoreError) {
    assert_eq!(SqliteLedger::open(db).err(), Some(want));
    assert_eq!(
        fs::read(db).unwrap(),
        before,
        "a refused ledger must not be modified"
    );
}

#[test]
fn create_initializes_a_new_ledger_and_open_verifies_it() {
    let d = tmp("ok");
    let db = d.join("ledger.db");
    let mut a = SqliteLedger::create(&db).unwrap();
    assert_eq!(a.owner_epoch(), 1);
    a.record_refusal("X").unwrap();
    drop(a);
    let b = SqliteLedger::open(&db).unwrap();
    assert_eq!((b.owner_epoch(), b.refusal_count().unwrap()), (2, 1));
}

#[test]
fn create_refuses_an_existing_file_and_open_refuses_a_missing_one() {
    let d = tmp("exists");
    let db = d.join("ledger.db");
    fs::write(&db, b"not mine").unwrap();
    assert_eq!(
        SqliteLedger::create(&db).err(),
        Some(StoreError::Unavailable {
            category: "open".into(),
            code: "EXISTS".into()
        })
    );
    assert_eq!(fs::read(&db).unwrap(), b"not mine");
    let missing = d.join("missing.db");
    assert!(SqliteLedger::open(&missing).is_err());
    assert!(!missing.exists(), "open must never create a ledger");
}

#[test]
fn unrelated_existing_database_is_refused_without_initialization() {
    let d = tmp("unrelated");
    let db = d.join("app.db");
    let before = build(&db, "CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT); INSERT INTO customers VALUES (1, 'a');", false);
    assert_refused_unchanged(&db, &before, schema_err());
    let c = Connection::open(&db).unwrap();
    let mode: String = c
        .query_row("PRAGMA journal_mode", [], |r| r.get(0))
        .unwrap();
    assert_eq!(mode, "delete", "not switched to WAL");
}

#[test]
fn non_database_file_is_refused_unchanged() {
    let d = tmp("notadb");
    let db = d.join("ledger.db");
    fs::write(&db, vec![b'x'; 4096]).unwrap();
    let before = fs::read(&db).unwrap();
    assert!(SqliteLedger::open(&db).is_err());
    assert_eq!(fs::read(&db).unwrap(), before);
}

#[test]
fn missing_metadata_is_refused() {
    for (name, remove) in [
        ("version", "DELETE FROM meta WHERE key = 'schema_version';"),
        ("epoch", "DELETE FROM meta WHERE key = 'owner_epoch';"),
        (
            "bad-epoch",
            "UPDATE meta SET value = 'x' WHERE key = 'owner_epoch';",
        ),
        (
            "counter",
            "DELETE FROM counters WHERE name = 'next_permit';",
        ),
        (
            "wrong-version",
            "UPDATE meta SET value = '1' WHERE key = 'schema_version';",
        ),
        (
            "previous-version",
            "UPDATE meta SET value = 'writ-ledger-1' WHERE key = 'schema_version';",
        ),
        (
            "writ-ledger-2",
            "UPDATE meta SET value = 'writ-ledger-2' WHERE key = 'schema_version';",
        ),
        (
            "writ-ledger-3",
            "UPDATE meta SET value = 'writ-ledger-3' WHERE key = 'schema_version';",
        ),
        ("ledger-id", "DELETE FROM meta WHERE key = 'ledger_id';"),
        (
            "bad-ledger-id",
            "UPDATE meta SET value = '0123456789ABCDEF0123456789ABCDEF' WHERE key = 'ledger_id';",
        ),
    ] {
        let d = tmp(&format!("meta-{name}"));
        let db = d.join("ledger.db");
        let before = build(&db, &format!("{}{remove}", valid()), true);
        assert_refused_unchanged(&db, &before, schema_err());
    }
}

#[test]
fn expected_names_with_wrong_definitions_or_extra_objects_are_refused() {
    let schema = valid();
    let cases = [
        // receipts without its status CHECK constraint: same table name, weaker definition
        ("weak-check", schema.replacen("CHECK (status IN", "/* removed */ CHECK (1 OR status IN", 1)),
        // grants without the uses <= max_uses constraint
        ("no-uses-bound", schema.replacen("CHECK (uses >= 0 AND uses <= max_uses)", "", 1)),
        // an extra trigger could rewrite state behind the broker
        ("extra-trigger", format!("{schema}CREATE TRIGGER t AFTER INSERT ON audit_refusals BEGIN DELETE FROM audit_refusals; END;")),
        // an extra table or index
        ("extra-table", format!("{schema}CREATE TABLE extra (x INTEGER);")),
        ("extra-index", format!("{schema}CREATE INDEX ix ON audit_refusals (code);")),
        // a missing table
        ("missing-table", format!("{schema}DROP TABLE grant_uses;")),
        // a writ-ledger-2 ledger still holding the simulated adapter tables (M3-04 moved
        // them to the effect adapter's own store)
        ("adapter-tables", format!("{schema}CREATE TABLE adapter_consumed_permits (permit_id TEXT PRIMARY KEY) STRICT;")),
        // the trusted-time floor without its single-row constraint
        ("multi-row-floor", schema.replacen("id       INTEGER PRIMARY KEY CHECK (id = 1)", "id       INTEGER PRIMARY KEY", 1)),
        // clock events accepting any kind
        ("open-event-kind", schema.replacen("kind      TEXT NOT NULL CHECK (kind IN", "kind      TEXT NOT NULL CHECK (1 OR kind IN", 1)),
        // a writ-ledger-1 ledger (no trusted-time tables)
        ("missing-floor", format!("{schema}DROP TABLE time_floor;")),
        ("missing-clock-events", format!("{schema}DROP TABLE clock_events;")),
        // writ-ledger-4 tables (D-035): missing, or weaker than defined
        ("missing-provenance", format!("{schema}DROP TABLE grant_provenance;")),
        ("missing-failure-records", format!("{schema}DROP TABLE failure_records;")),
        ("shared-permit-number", schema.replacen("permit_number   INTEGER NOT NULL UNIQUE", "permit_number   INTEGER NOT NULL", 1)),
        ("open-outcome", schema.replacen("outcome         TEXT NOT NULL CHECK (outcome IN", "outcome         TEXT NOT NULL CHECK (1 OR outcome IN", 1)),
    ];
    for (name, sql) in cases {
        assert_ne!(sql, schema, "{name}: mutation did not apply");
        let d = tmp(&format!("def-{name}"));
        let db = d.join("ledger.db");
        let before = build(&db, &sql, true);
        assert_eq!(SqliteLedger::open(&db).err(), Some(schema_err()), "{name}");
        assert_eq!(fs::read(&db).unwrap(), before, "{name}: modified");
    }
}

#[test]
fn valid_schema_not_in_wal_mode_is_refused_unchanged() {
    let d = tmp("not-wal");
    let db = d.join("ledger.db");
    let before = build(&db, &valid(), false);
    assert_refused_unchanged(&db, &before, schema_err());
}

#[test]
fn the_reference_ledger_of_these_tests_is_accepted() {
    // Positive control: the refusals above are caused by their mutation only.
    let d = tmp("control");
    let db = d.join("ledger.db");
    build(&db, &valid(), true);
    let l = SqliteLedger::open(&db).unwrap();
    assert_eq!(l.ledger_id().as_str(), "0123456789abcdef0123456789abcdef");
}

#[test]
fn an_exact_writ_ledger_3_ledger_is_refused_by_open_unchanged() {
    // writ-ledger-3 needs the explicit operator upgrade (D-035 item 5), never a silent one.
    let d = tmp("v3");
    let db = d.join("ledger.db");
    let before = build(&db, SCHEMA_V3, true);
    assert_refused_unchanged(&db, &before, schema_err());
}

#[test]
fn failed_creation_removes_the_files_it_created() {
    let d = tmp("cleanup");
    let db = d.join("ledger.db");
    // A directory where the -wal file must go makes creation fail after the exclusive create.
    fs::create_dir(d.join("ledger.db-wal")).unwrap();
    assert!(SqliteLedger::create(&db).is_err());
    assert!(!db.exists(), "the created ledger file is removed");
    assert!(!d.join("ledger.db-journal").exists());
    assert!(!d.join("ledger.db-shm").exists());
    assert!(
        d.join("ledger.db-wal").is_dir(),
        "a pre-existing path is never removed"
    );
}
