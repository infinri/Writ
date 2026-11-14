//! LEDGER-SQLITE.md required test 3, second process: two `proto` processes
//! open the same file with `--ledger-db`, one after the other.

use rusqlite::Connection;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

const SCENARIOS: &str = include_str!("data/persistence.jsonl");

fn proto(args: &[&str], stdin: &str) -> (i32, String) {
    let mut child = Command::new(env!("CARGO_BIN_EXE_proto"))
        .args(args)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    child.stdin.take().unwrap().write_all(stdin.as_bytes()).unwrap();
    let out = child.wait_with_output().unwrap();
    assert!(out.stderr.is_empty(), "stderr: {}", String::from_utf8_lossy(&out.stderr));
    (out.status.code().unwrap_or(-1), String::from_utf8(out.stdout).unwrap())
}

fn scenario(db: &Path, input: &str) -> String {
    let (code, out) = proto(&["broker-scenario", "--ledger", "sqlite", "--ledger-db", db.to_str().unwrap()], &format!("{{\"id\":\"p\",\"input\":{input}}}\n"));
    assert_eq!(code, 0);
    out
}

fn count(db: &Path, sql: &str) -> i64 {
    Connection::open(db).unwrap().query_row(sql, [], |r| r.get(0)).unwrap()
}

fn temp_dir(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("writ-proto-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

#[test]
fn second_process_continues_the_ledger() {
    let dir = temp_dir("persistence");
    let db = dir.join("ledger.db");
    let mut lines = SCENARIOS.lines();
    let (first, second) = (lines.next().unwrap(), lines.next().unwrap());

    // approve g1, g2; crash_after_reserve (g1); success; crash_after_dispatch;
    // bad seal; restart; replayed permit; re-execute the dispatched key.
    let out = scenario(&db, first);
    assert_eq!(
        out.trim_end(),
        r#"{"id":"p","output":{"final":{"consumed_permits":2,"effects_applied":2,"grant_uses":{"g1":1,"g2":0},"receipts":{"dispatched":1,"observed_success":1,"reserved":1},"refusals":3},"steps":[{"result":"grant_issued"},{"result":"grant_issued"},{"result":"crashed"},{"result":"effect_completed"},{"result":"crashed"},{"code":"INGRESS_UNAUTHENTICATED","result":"refused"},{"result":"restarted"},{"code":"PERMIT_REPLAYED","result":"refused"},{"code":"RECONCILIATION_REQUIRED","result":"refused"}]}}"#
    );
    let (memory_code, memory) = proto(&["broker-scenario"], &format!("{{\"id\":\"p\",\"input\":{first}}}\n"));
    assert_eq!((memory_code, memory), (0, out));
    assert_eq!(count(&db, "SELECT value FROM counters WHERE name = 'next_permit'"), 2);

    // A second process: the reserved receipt resumes without a second grant
    // use; the completed one replays; the dispatched one needs reconciliation
    // and is never dispatched again; g1 stays exhausted; refusals continue.
    let out = scenario(&db, second);
    assert_eq!(
        out.trim_end(),
        r#"{"id":"p","output":{"final":{"consumed_permits":3,"effects_applied":3,"grant_uses":{"g1":1,"g2":0},"receipts":{"observed_success":2,"unknown_reconcile":1},"refusals":5},"steps":[{"result":"effect_completed"},{"replayed":true,"result":"effect_completed"},{"code":"RECONCILIATION_REQUIRED","result":"refused"},{"code":"GRANT_EXHAUSTED","result":"refused"},{"count":1,"result":"reconciled"}]}}"#
    );
    assert_eq!(count(&db, "SELECT value FROM counters WHERE name = 'next_permit'"), 3);
    assert_eq!(count(&db, "SELECT count(*) FROM grant_uses"), 1);
    assert_eq!(count(&db, "SELECT count(*) FROM adapter_effects"), 3);
    assert_eq!(count(&db, "SELECT count(*) FROM receipt_events WHERE idempotency_key = 'idem-w2' AND cause = 'dispatch'"), 1);
    assert_eq!(count(&db, "SELECT count(*) FROM adapter_consumed_permits WHERE permit_id = 'permit-3'"), 1);
    std::fs::remove_dir_all(&dir).unwrap();
}

#[test]
fn ledger_flags_only_on_broker_scenario() {
    for args in [
        &["decide", "--ledger", "sqlite", "--ledger-dir", "/tmp"][..],
        &["broker-scenario", "--ledger", "sqlite"],
        &["broker-scenario", "--ledger-dir", "/tmp"],
        &["broker-scenario", "--ledger", "memory", "--ledger-dir", "/tmp"],
        &["broker-scenario", "--ledger", "sqlite", "--ledger-dir", "/tmp", "--ledger-db", "/tmp/x.db"],
        &["broker-scenario", "--ledger", "sqlite", "--ledger-dir"],
        &["sqlite-info", "--ledger", "sqlite"],
    ] {
        let out = Command::new(env!("CARGO_BIN_EXE_proto")).args(args).stdin(Stdio::null()).output().unwrap();
        assert_eq!(out.status.code(), Some(2), "{args:?}");
        assert!(out.stdout.is_empty());
    }
}

#[test]
fn sqlite_info_reports_the_pinned_engine_and_settings() {
    let (code, out) = proto(&["sqlite-info"], "");
    assert_eq!(code, 0);
    for part in [
        r#""sqlite_version":"3.54.1""#,
        r#""source_id":"2026-10-16 14:45:20 555e07ef"#,
        r#""settings":{"busy_timeout":5000,"foreign_keys":1,"journal_mode":"wal","synchronous":2,"trusted_schema":0}"#,
        r#""schema_version":"1""#,
        r#""driver":"rusqlite 0.40.2""#,
        r#""OMIT_LOAD_EXTENSION""#,
        r#""THREADSAFE=1""#,
        r#""DQS=0""#,
        r#""ENABLE_COLUMN_METADATA""#,
    ] {
        assert!(out.contains(part), "{part} not in {out}");
    }
}
