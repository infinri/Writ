//! M5a-06 immutable snapshot store tests (D-045 M5-D5, M5-D17; M5-INTERFACES J-4; INV-40):
//! ingest, every refusal, size and quota bounds, claimed-digest checks, tamper detection on
//! read, immutability triggers, ledger binding, owner-epoch fencing, ownership, injected
//! commit failures, and the trust rule for `immutable()`. The real SIGKILL tests live in
//! `kill_tests` (children run [`snapshot_kill_child`]).

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::path::{Path, PathBuf};
use std::sync::Arc;

use writ_broker::runner::{snapshot_digest, SnapshotContent, SnapshotFile};
use writ_broker::store::StoreError;
use writ_broker::workflow::{SnapshotCheck, SnapshotStore};
use writ_broker::writ_kernel::json::Digest;

use super::fault::Stage;
use super::snapshots::TestDeployment;
use super::{
    snapshot_id_for, IngestSession, LedgerId, SnapshotError, SnapshotLimits, SnapshotRefusal,
    SqliteSnapshotStore,
};

const LEDGER: &str = "0123456789abcdef0123456789abcdef";
const OTHER_LEDGER: &str = "fedcba9876543210fedcba9876543210";

fn ledger() -> LedgerId {
    LedgerId::parse(LEDGER).unwrap()
}

fn scratch(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("writ-snap-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn small() -> SnapshotLimits {
    SnapshotLimits {
        max_file_bytes: 64,
        max_snapshot_files: 4,
        max_snapshot_bytes: 128,
        max_path_bytes: 40,
        max_path_depth: 4,
        max_scope_snapshots: 3,
        max_scope_bytes: 200,
        max_open_sessions: 3,
        max_scope_sessions: 2,
    }
}

fn store_with(dir: &Path, limits: SnapshotLimits) -> Arc<SqliteSnapshotStore> {
    Arc::new(
        SqliteSnapshotStore::create(&dir.join("snapshots.db"), &ledger(), limits)
            .unwrap()
            .with_deployment(TestDeployment::verified()),
    )
}

fn store(name: &str) -> (PathBuf, Arc<SqliteSnapshotStore>) {
    let dir = scratch(name);
    let s = store_with(&dir, SnapshotLimits::REFERENCE);
    (dir, s)
}

fn files(list: &[(&str, &[u8])]) -> Vec<SnapshotFile> {
    list.iter()
        .map(|(p, b)| SnapshotFile {
            path: p.to_string(),
            bytes: b.to_vec(),
        })
        .collect()
}

/// Ingest `list` (each file's bytes in frames of at most 3 bytes).
fn ingest(
    s: &Arc<SqliteSnapshotStore>,
    list: &[(&str, &[u8])],
    claimed: Option<&str>,
) -> Result<super::Committed, SnapshotError> {
    let mut session = s.begin("t1", "p1")?;
    for (path, bytes) in list {
        session.file(path, bytes.len() as u64)?;
        for chunk in bytes.chunks(3) {
            session.data(chunk)?;
        }
    }
    session.commit(claimed)
}

fn refused(r: Result<impl std::fmt::Debug, SnapshotError>) -> SnapshotRefusal {
    match r {
        Err(SnapshotError::Refused(code)) => code,
        other => panic!("expected a refusal, got {other:?}"),
    }
}

fn count(s: &SqliteSnapshotStore, table: &str) -> i64 {
    s.db()
        .conn
        .query_row(&format!("SELECT count(*) FROM {table}"), [], |r| r.get(0))
        .unwrap()
}

fn assert_nothing_published(s: &SqliteSnapshotStore) {
    assert_eq!(count(s, "snapshots"), 0);
    assert_eq!(count(s, "snapshot_files"), 0);
    assert_eq!(count(s, "blobs"), 0);
    assert_eq!(count(s, "ingest_staging"), 0);
}

const A: &[(&str, &[u8])] = &[("src/main.rs", b"fn main() {}\n"), ("README", b"hello")];

#[test]
fn ingest_commits_a_verified_content_addressed_snapshot() {
    let (dir, s) = store("roundtrip");
    let c = ingest(&s, A, None).unwrap();
    assert!(c.created);
    let expected = snapshot_digest(&files(A)).unwrap();
    assert_eq!(
        c.manifest_digest, expected,
        "same format as the broker digest"
    );
    assert_eq!(
        Some(c.snapshot_id.clone()),
        snapshot_id_for("t1", "p1", &expected)
    );
    assert!(c.snapshot_id.starts_with("snap-") && c.snapshot_id.len() == 5 + 64);
    let read = s.read("t1", "p1", &c.snapshot_id).unwrap().unwrap();
    let mut want = files(A);
    want.sort_by(|a, b| a.path.cmp(&b.path));
    assert_eq!(read.files, want);
    assert_eq!(read.manifest_digest, expected);
    assert_eq!(count(&s, "ingest_staging"), 0);
    // Same content, other scope: another id.
    assert_ne!(
        snapshot_id_for("t1", "p2", &expected).unwrap(),
        c.snapshot_id
    );
    // Published content survives restart.
    drop(s);
    let s = SqliteSnapshotStore::open(
        &dir.join("snapshots.db"),
        &ledger(),
        SnapshotLimits::REFERENCE,
    )
    .unwrap();
    assert_eq!(
        s.read("t1", "p1", &c.snapshot_id).unwrap().unwrap().files,
        want
    );
    assert_eq!(
        s.read("t1", "p2", &c.snapshot_id).unwrap(),
        None,
        "scope-bound"
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn identical_reingest_answers_the_same_id_without_charging_the_quota() {
    let dir = scratch("idem");
    let s = store_with(&dir, small());
    let first = ingest(&s, &[("a", b"1")], None).unwrap();
    let again = ingest(&s, &[("a", b"1")], None).unwrap();
    assert_eq!(first.snapshot_id, again.snapshot_id);
    assert!(first.created && !again.created);
    assert_eq!(count(&s, "snapshots"), 1);
    assert_eq!(count(&s, "blobs"), 1);
    // Deduplicated blobs: a second snapshot sharing a file stores it once.
    ingest(&s, &[("a", b"1"), ("b", b"2")], None).unwrap();
    assert_eq!(count(&s, "blobs"), 2);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn broker_traits_answer_only_with_a_verified_deployment() {
    let dir = scratch("trust");
    let path = dir.join("snapshots.db");
    let id = {
        let s = Arc::new(
            SqliteSnapshotStore::create(&path, &ledger(), SnapshotLimits::REFERENCE).unwrap(),
        );
        // No proof: never immutable, and both interfaces refuse (fail closed).
        assert!(!s.immutable());
        let c = ingest(&s, A, None).unwrap();
        assert!(s.content("t1", "p1", &c.snapshot_id).is_err());
        assert!(s
            .check("t1", "p1", &c.snapshot_id, &c.manifest_digest)
            .is_err());
        c
    };
    let proof = TestDeployment::verified();
    let s = SqliteSnapshotStore::open(&path, &ledger(), SnapshotLimits::REFERENCE)
        .unwrap()
        .with_deployment(proof.clone());
    assert!(s.immutable());
    let content = s.content("t1", "p1", &id.snapshot_id).unwrap().unwrap();
    assert_eq!(snapshot_digest(&content).unwrap(), id.manifest_digest);
    assert_eq!(
        s.check("t1", "p1", &id.snapshot_id, &id.manifest_digest),
        Ok(SnapshotCheck::Matches)
    );
    let other = Digest::of_bytes(b"other").as_str().to_string();
    assert_eq!(
        s.check("t1", "p1", &id.snapshot_id, &other),
        Ok(SnapshotCheck::Differs)
    );
    assert_eq!(
        s.check("t1", "p1", "snap-unknown", &id.manifest_digest),
        Ok(SnapshotCheck::Unknown)
    );
    assert_eq!(s.content("t1", "p1", "snap-unknown"), Ok(None));
    assert_eq!(
        s.check("t2", "p1", &id.snapshot_id, &id.manifest_digest),
        Ok(SnapshotCheck::Unknown),
        "another tenant never sees the snapshot"
    );
    // A withdrawn proof (failed J-9 recheck) turns everything off again.
    proof.withdraw();
    assert!(!s.immutable());
    assert!(s.content("t1", "p1", &id.snapshot_id).is_err());
    assert!(s
        .check("t1", "p1", &id.snapshot_id, &id.manifest_digest)
        .is_err());
    drop(s);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn every_invalid_path_is_refused_and_nothing_is_published() {
    let (dir, s) = store("paths");
    let long_component = "c".repeat(256);
    let too_long = "x/".repeat(600) + "x";
    let too_deep = "a/".repeat(40) + "a";
    let cases: Vec<&str> = vec![
        "",
        ".",
        "..",
        "a/../b",
        "./a",
        "/abs",
        "a//b",
        "a/",
        "a\0b",
        "a\nb",
        "tab\there",
        "del\x7f",
        "caf\u{e9}",
        "e\u{301}",
        &long_component,
        &too_long,
        &too_deep,
    ];
    for path in cases {
        let mut session = s.begin("t1", "p1").unwrap();
        assert_eq!(
            refused(session.file(path, 1)),
            SnapshotRefusal::PathInvalid,
            "{path:?}"
        );
        // The session ended with the refusal.
        assert_eq!(
            refused(session.file("ok", 1)),
            SnapshotRefusal::SessionState
        );
    }
    assert_nothing_published(&s);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn duplicate_and_prefix_conflicting_paths_are_refused() {
    let (dir, s) = store("manifest");
    for pair in [
        ("a", "a"),
        ("a", "a/b"),
        ("a/b", "a"),
        ("x/y", "x/y/z"),
        ("x/y/z", "x/y"),
    ] {
        let mut session = s.begin("t1", "p1").unwrap();
        session.file(pair.0, 1).unwrap();
        session.data(b"1").unwrap();
        assert_eq!(
            refused(session.file(pair.1, 1)),
            SnapshotRefusal::ManifestInvalid,
            "{pair:?}"
        );
    }
    // Siblings sharing a name prefix are not conflicts.
    ingest(&s, &[("a", b"1"), ("a-b", b"2"), ("ab/c", b"3")], None).unwrap();
    // Empty manifest.
    let session = s.begin("t1", "p1").unwrap();
    assert_eq!(
        refused(session.commit(None)),
        SnapshotRefusal::ManifestInvalid
    );
    assert_eq!(count(&s, "snapshots"), 1);
    assert_eq!(count(&s, "ingest_staging"), 0);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn received_bytes_must_match_the_declared_size() {
    let (dir, s) = store("sizes");
    // More bytes than declared.
    let mut session = s.begin("t1", "p1").unwrap();
    session.file("a", 2).unwrap();
    assert_eq!(refused(session.data(b"123")), SnapshotRefusal::SizeMismatch);
    // A new file before the previous one is complete.
    let mut session = s.begin("t1", "p1").unwrap();
    session.file("a", 2).unwrap();
    session.data(b"1").unwrap();
    assert_eq!(refused(session.file("b", 1)), SnapshotRefusal::SizeMismatch);
    // Commit with an incomplete file.
    let mut session = s.begin("t1", "p1").unwrap();
    session.file("a", 2).unwrap();
    session.data(b"1").unwrap();
    assert_eq!(refused(session.commit(None)), SnapshotRefusal::SizeMismatch);
    // Data without a declared file.
    let mut session = s.begin("t1", "p1").unwrap();
    assert_eq!(refused(session.data(b"1")), SnapshotRefusal::SessionState);
    // Data after the declared file completed.
    let mut session = s.begin("t1", "p1").unwrap();
    session.file("a", 1).unwrap();
    session.data(b"1").unwrap();
    assert_eq!(refused(session.data(b"2")), SnapshotRefusal::SessionState);
    assert_nothing_published(&s);
    // Empty files are files.
    let c = ingest(&s, &[("empty", b"")], None).unwrap();
    assert_eq!(
        s.read("t1", "p1", &c.snapshot_id).unwrap().unwrap().files,
        files(&[("empty", b"")])
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn claimed_manifest_digest_is_checked_never_trusted() {
    let (dir, s) = store("claimed");
    let real = snapshot_digest(&files(A)).unwrap();
    let wrong = snapshot_digest(&files(&[("README", b"hello")])).unwrap();
    assert_eq!(
        refused(ingest(&s, A, Some(&wrong))),
        SnapshotRefusal::DigestMismatch
    );
    assert_eq!(
        refused(ingest(&s, A, Some("sha256:not-a-digest"))),
        SnapshotRefusal::DigestMismatch
    );
    assert_nothing_published(&s);
    let c = ingest(&s, A, Some(&real)).unwrap();
    assert_eq!(c.manifest_digest, real);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn size_count_and_quota_bounds_refuse_snapshot_limit() {
    let dir = scratch("limits");
    let s = store_with(&dir, small());
    let big = [7u8; 65];
    // Per file.
    let mut session = s.begin("t1", "p1").unwrap();
    assert_eq!(refused(session.file("a", 65)), SnapshotRefusal::Limit);
    assert_eq!(
        refused(ingest(&s, &[("a", &big)], None)),
        SnapshotRefusal::Limit
    );
    // Files per snapshot.
    let five: Vec<(String, Vec<u8>)> = (0..5).map(|i| (format!("f{i}"), vec![1])).collect();
    let five: Vec<(&str, &[u8])> = five
        .iter()
        .map(|(p, b)| (p.as_str(), b.as_slice()))
        .collect();
    assert_eq!(refused(ingest(&s, &five, None)), SnapshotRefusal::Limit);
    // Bytes per snapshot (3 x 60 > 128).
    let sixty = [1u8; 60];
    assert_eq!(
        refused(ingest(
            &s,
            &[("a", &sixty), ("b", &sixty), ("c", &sixty)],
            None
        )),
        SnapshotRefusal::Limit
    );
    assert_nothing_published(&s);
    // Bytes per scope: 2 x 100 = 200 fits, the next byte does not.
    let fifty = [2u8; 50];
    ingest(&s, &[("a", &fifty), ("b", &fifty)], None).unwrap();
    ingest(&s, &[("c", &fifty), ("d", &fifty)], None).unwrap();
    assert_eq!(
        refused(ingest(&s, &[("e", b"x")], None)),
        SnapshotRefusal::Limit
    );
    // Snapshots per scope (another scope with room in bytes).
    for i in 0..3u8 {
        let mut session = s.begin("t1", "p2").unwrap();
        session.file("a", 1).unwrap();
        session.data(&[i]).unwrap();
        session.commit(None).unwrap();
    }
    assert_eq!(refused(s.begin("t1", "p2")), SnapshotRefusal::Limit);
    // Other scopes are unaffected.
    s.begin("t9", "p9").unwrap().abort();
    assert_eq!(count(&s, "snapshots"), 5);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn open_sessions_are_bounded_per_store_and_per_scope() {
    let dir = scratch("sessions");
    let s = store_with(&dir, small());
    let a = s.begin("t1", "p1").unwrap();
    let b = s.begin("t1", "p1").unwrap();
    assert_eq!(refused(s.begin("t1", "p1")), SnapshotRefusal::Limit);
    let c = s.begin("t2", "p1").unwrap();
    assert_eq!(refused(s.begin("t3", "p1")), SnapshotRefusal::Limit);
    drop(a);
    let a2 = s.begin("t1", "p1").unwrap();
    // A refused session releases its slot.
    let mut d: IngestSession = b;
    assert_eq!(refused(d.file("..", 1)), SnapshotRefusal::PathInvalid);
    let b2 = s.begin("t1", "p1").unwrap();
    drop((a2, b2, c, d));
    assert_eq!(refused(s.begin("", "p1")), SnapshotRefusal::ScopeInvalid);
    assert_eq!(refused(s.begin("t 1", "p1")), SnapshotRefusal::ScopeInvalid);
    assert_eq!(
        refused(s.begin("t1", &"p".repeat(129))),
        SnapshotRefusal::ScopeInvalid
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn an_unfinished_session_leaves_no_staging_and_no_snapshot() {
    let (dir, s) = store("unfinished");
    let mut session = s.begin("t1", "p1").unwrap();
    session.file("a", 3).unwrap();
    session.data(b"abc").unwrap();
    session.file("b", 1).unwrap();
    session.data(b"d").unwrap();
    assert_eq!(count(&s, "ingest_staging"), 2);
    assert_eq!(
        count(&s, "snapshots"),
        0,
        "staged files are never a snapshot"
    );
    drop(session);
    assert_nothing_published(&s);
    let session = s.begin("t1", "p1").unwrap();
    session.abort();
    // Staging rows left by a dead owner are removed at open.
    s.db()
        .conn
        .execute(
            "INSERT INTO ingest_staging (session_id, path, digest, size, bytes) \
             VALUES ('0-99', 'x', 'sha256:00', 1, X'00')",
            [],
        )
        .unwrap();
    drop(s);
    let s = SqliteSnapshotStore::open(
        &dir.join("snapshots.db"),
        &ledger(),
        SnapshotLimits::REFERENCE,
    )
    .unwrap();
    assert_eq!(count(&s, "ingest_staging"), 0);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn injected_commit_failures_publish_nothing_and_a_retry_commits_once() {
    let (dir, s) = store("faults");
    for (stage, unknown) in [
        (Stage::Begin, false),
        (Stage::Body, false),
        (Stage::Commit, true),
    ] {
        s.inject("snapshot_commit", stage, rusqlite::ffi::SQLITE_IOERR, false);
        match ingest(&s, A, None) {
            Err(SnapshotError::Store(f)) => {
                assert_eq!(f.outcome_unknown, unknown, "{stage:?}");
                assert_eq!(f.error.code(), "SQLITE_IOERR");
            }
            other => panic!("{stage:?}: {other:?}"),
        }
        assert_nothing_published(&s);
    }
    // Staging failure.
    s.inject(
        "snapshot_stage",
        Stage::Body,
        rusqlite::ffi::SQLITE_FULL,
        false,
    );
    assert!(matches!(ingest(&s, A, None), Err(SnapshotError::Store(_))));
    assert_nothing_published(&s);
    let c = ingest(&s, A, None).unwrap();
    assert!(c.created);
    assert!(!ingest(&s, A, None).unwrap().created);
    assert_eq!(count(&s, "snapshots"), 1);
    let _ = std::fs::remove_dir_all(&dir);
}

/// Tamper with the stored database behind the API (bypassing the immutability triggers,
/// as an attacker with file access could) and expect every read to refuse.
fn tamper_then_expect_refusal(name: &str, sql: &str) {
    let dir = scratch(name);
    let s = store_with(&dir, SnapshotLimits::REFERENCE);
    let c = ingest(&s, A, None).unwrap();
    assert!(s.read("t1", "p1", &c.snapshot_id).unwrap().is_some());
    s.db()
        .conn
        .execute_batch(&sql.replace("$ID", &c.snapshot_id))
        .unwrap();
    let f = s.read("t1", "p1", &c.snapshot_id).unwrap_err();
    assert_eq!(f.error.code(), "integrity", "{name}");
    assert!(s.content("t1", "p1", &c.snapshot_id).is_err(), "{name}");
    assert!(
        s.check("t1", "p1", &c.snapshot_id, &c.manifest_digest)
            .is_err(),
        "{name}"
    );
    drop(s);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn tampered_content_is_detected_on_every_read() {
    // Same length, different bytes.
    tamper_then_expect_refusal(
        "tamper-bytes",
        "DROP TRIGGER blobs_immutable_update; \
         UPDATE blobs SET bytes = CAST('jello' AS BLOB) WHERE size = 5;",
    );
    // Blob replaced together with its recorded size.
    tamper_then_expect_refusal(
        "tamper-size",
        "DROP TRIGGER blobs_immutable_update; \
         UPDATE blobs SET size = 6, bytes = CAST('hello!' AS BLOB) WHERE size = 5;",
    );
    // A file removed from the snapshot.
    tamper_then_expect_refusal(
        "tamper-drop-file",
        "DROP TRIGGER snapshot_files_immutable_delete; \
         DELETE FROM snapshot_files WHERE path = 'README';",
    );
    // A file renamed to an unsafe path.
    tamper_then_expect_refusal(
        "tamper-path",
        "DROP TRIGGER snapshot_files_immutable_update; \
         UPDATE snapshot_files SET path = '../README' WHERE path = 'README';",
    );
    // File pointed at another valid blob (consistent digest and size, wrong manifest).
    tamper_then_expect_refusal(
        "tamper-repoint",
        "INSERT INTO blobs (digest, size, bytes) VALUES \
         ('sha256:2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824', 5, CAST('hello' AS BLOB)) \
         ON CONFLICT DO NOTHING; \
         DROP TRIGGER snapshot_files_immutable_update; \
         UPDATE snapshot_files SET blob_digest = \
         (SELECT digest FROM blobs WHERE size = 13) WHERE path = 'README';",
    );
    // Manifest digest rewritten (the snapshot id no longer derives from it).
    tamper_then_expect_refusal(
        "tamper-manifest",
        "DROP TRIGGER snapshots_immutable_update; \
         UPDATE snapshots SET manifest_digest = 'sha256:00';",
    );
    // Counts rewritten.
    tamper_then_expect_refusal(
        "tamper-count",
        "DROP TRIGGER snapshots_immutable_update; UPDATE snapshots SET total_size = 1;",
    );
}

#[test]
fn published_rows_cannot_be_updated_or_deleted_even_through_sql() {
    let (dir, s) = store("triggers");
    ingest(&s, A, None).unwrap();
    for sql in [
        "UPDATE blobs SET size = size",
        "DELETE FROM blobs",
        "UPDATE snapshots SET file_count = file_count",
        "DELETE FROM snapshots",
        "UPDATE snapshot_files SET size = size",
        "DELETE FROM snapshot_files",
    ] {
        let e = s.db().conn.execute(sql, []).unwrap_err();
        assert!(e.to_string().contains("immutable"), "{sql}: {e}");
    }
    assert_eq!(count(&s, "snapshots"), 1);
    assert_eq!(count(&s, "snapshot_files"), 2);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn an_existing_corrupted_blob_is_never_reused_by_a_new_snapshot() {
    let (dir, s) = store("reuse");
    ingest(&s, &[("a", b"hello")], None).unwrap();
    s.db()
        .conn
        .execute_batch(
            "DROP TRIGGER blobs_immutable_update; \
             UPDATE blobs SET bytes = CAST('jello' AS BLOB);",
        )
        .unwrap();
    match ingest(&s, &[("b", b"hello")], None) {
        Err(SnapshotError::Store(f)) => assert_eq!(f.error.code(), "integrity"),
        other => panic!("{other:?}"),
    }
    assert_eq!(count(&s, "snapshots"), 1);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn bound_to_one_ledger_and_owned_by_one_instance() {
    let (dir, s) = store("binding");
    let path = dir.join("snapshots.db");
    let other = LedgerId::parse(OTHER_LEDGER).unwrap();
    assert_eq!(
        SqliteSnapshotStore::open(&path, &ledger(), SnapshotLimits::REFERENCE)
            .err()
            .unwrap()
            .code(),
        "OWNED_ELSEWHERE"
    );
    drop(s);
    assert_eq!(
        SqliteSnapshotStore::open(&path, &other, SnapshotLimits::REFERENCE)
            .err()
            .unwrap()
            .code(),
        "LEDGER_MISMATCH"
    );
    assert_eq!(
        SqliteSnapshotStore::create(&path, &ledger(), SnapshotLimits::REFERENCE)
            .err()
            .unwrap()
            .code(),
        "EXISTS"
    );
    let mut zero = SnapshotLimits::REFERENCE;
    zero.max_file_bytes = 0;
    assert_eq!(
        SqliteSnapshotStore::open(&path, &ledger(), zero)
            .err()
            .unwrap()
            .code(),
        "limits"
    );
    // Opening is the only way to own it; the epoch moves forward each time.
    let epoch = |s: &SqliteSnapshotStore| -> String {
        s.db()
            .conn
            .query_row(
                "SELECT value FROM meta WHERE key = 'owner_epoch'",
                [],
                |r| r.get(0),
            )
            .unwrap()
    };
    let s = SqliteSnapshotStore::open(&path, &ledger(), SnapshotLimits::REFERENCE).unwrap();
    let first = epoch(&s);
    drop(s);
    let s = SqliteSnapshotStore::open(&path, &ledger(), SnapshotLimits::REFERENCE).unwrap();
    assert!(epoch(&s).parse::<u64>().unwrap() > first.parse::<u64>().unwrap());
    // A ledger file is not a snapshot store.
    drop(s);
    let ledger_dir = scratch("binding-ledger");
    let l = super::SqliteLedger::create(&ledger_dir.join("ledger.db")).unwrap();
    let id = l.ledger_id().clone();
    drop(l);
    assert_eq!(
        SqliteSnapshotStore::open(
            &ledger_dir.join("ledger.db"),
            &id,
            SnapshotLimits::REFERENCE
        )
        .err()
        .unwrap()
        .code(),
        "schema"
    );
    let _ = std::fs::remove_dir_all(&dir);
    let _ = std::fs::remove_dir_all(&ledger_dir);
}

#[test]
fn a_stale_owner_epoch_fences_every_write() {
    let (dir, s) = store("fenced");
    let mut session = s.begin("t1", "p1").unwrap();
    session.file("a", 1).unwrap();
    session.data(b"1").unwrap();
    // Another owner started after this one (simulated: the stored epoch moved on).
    s.db()
        .conn
        .execute(
            "UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'owner_epoch'",
            [],
        )
        .unwrap();
    match session.commit(None) {
        Err(SnapshotError::Store(f)) => {
            assert!(matches!(f.error, StoreError::Fenced { .. }), "{f:?}")
        }
        other => panic!("{other:?}"),
    }
    let mut session = s.begin("t1", "p1").unwrap();
    session.file("a", 1).unwrap();
    match session.data(b"1") {
        Err(SnapshotError::Store(f)) => assert_eq!(f.error.code(), "FENCED"),
        other => panic!("{other:?}"),
    }
    assert_eq!(count(&s, "snapshots"), 0);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn refusal_codes_map_to_the_generic_external_refusal() {
    for r in [
        SnapshotRefusal::Limit,
        SnapshotRefusal::PathInvalid,
        SnapshotRefusal::ManifestInvalid,
        SnapshotRefusal::SizeMismatch,
        SnapshotRefusal::DigestMismatch,
        SnapshotRefusal::ScopeInvalid,
        SnapshotRefusal::SessionState,
    ] {
        assert!(r.code().starts_with("SNAPSHOT_"));
        assert_eq!(r.external_code(), "PROMOTION_REFUSED", "{}", r.code());
    }
    assert_eq!(SnapshotRefusal::Limit.code(), "SNAPSHOT_LIMIT");
    let f = writ_broker::store::TxFailure::new(
        writ_broker::store::TxStage::Body,
        StoreError::OwnedElsewhere,
    );
    assert_eq!(
        SnapshotError::Store(f).external_code(),
        "LEDGER_UNAVAILABLE"
    );
}

#[test]
fn store_and_sessions_can_move_between_threads() {
    fn send_sync<T: Send + Sync>() {}
    fn send<T: Send>() {}
    send_sync::<SqliteSnapshotStore>();
    send::<IngestSession>();
}

// ---------------------------------------------------------------------------
// Real process kills (driven by `kill_tests`).

/// Child: open the store in `dir`, stage two files, then either announce `STAGED` and
/// block (`staged`) or block inside the open commit transaction (`commit`).
pub(super) fn snapshot_kill_child(dir: &Path, mode: &str) {
    use std::io::Write;
    let s = Arc::new(
        SqliteSnapshotStore::open(
            &dir.join("snapshots.db"),
            &ledger(),
            SnapshotLimits::REFERENCE,
        )
        .unwrap(),
    );
    let mut session = s.begin("t1", "p1").unwrap();
    for (path, bytes) in A {
        session.file(path, bytes.len() as u64).unwrap();
        session.data(bytes).unwrap();
    }
    if mode == "commit" {
        s.inject("snapshot_commit", Stage::Body, 0, true);
        let _ = session.commit(None);
    } else {
        let out = std::io::stdout();
        let mut out = out.lock();
        writeln!(out, "STAGED").unwrap();
        out.flush().unwrap();
        drop(out);
        loop {
            std::thread::sleep(std::time::Duration::from_secs(60));
        }
    }
}

/// A disposable store for a kill test.
pub(super) fn prepare_snapshot_kill(dir: &Path) {
    let _ = std::fs::remove_dir_all(dir);
    std::fs::create_dir_all(dir).unwrap();
    drop(
        SqliteSnapshotStore::create(
            &dir.join("snapshots.db"),
            &ledger(),
            SnapshotLimits::REFERENCE,
        )
        .unwrap(),
    );
}

/// After the kill: the reopened store has no snapshot and no staging; the staged rows
/// were durable before the kill (checked with a raw read-only connection first); a new
/// ingest commits exactly once.
pub(super) fn after_snapshot_kill(dir: &Path, mode: &str) {
    let path = dir.join("snapshots.db");
    {
        let raw = rusqlite::Connection::open_with_flags(
            &path,
            rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
        )
        .unwrap();
        let snaps: i64 = raw
            .query_row("SELECT count(*) FROM snapshots", [], |r| r.get(0))
            .unwrap();
        assert_eq!(snaps, 0, "{mode}: no partial snapshot visible");
        let staged: i64 = raw
            .query_row("SELECT count(*) FROM ingest_staging", [], |r| r.get(0))
            .unwrap();
        assert_eq!(staged, 2, "{mode}: staging rows left by the killed owner");
    }
    let s = Arc::new(
        SqliteSnapshotStore::open(&path, &ledger(), SnapshotLimits::REFERENCE)
            .unwrap()
            .with_deployment(TestDeployment::verified()),
    );
    assert_nothing_published(&s);
    let c = ingest(&s, A, None).unwrap();
    assert!(c.created);
    assert_eq!(
        s.check("t1", "p1", &c.snapshot_id, &c.manifest_digest),
        Ok(SnapshotCheck::Matches)
    );
    assert_eq!(count(&s, "snapshots"), 1);
}
