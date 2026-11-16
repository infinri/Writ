//! In-process tests of the `fs.write` adapter through the broker (J-7.8): the normal path,
//! consumed `Failed` results, repeated identical content, later writes, R11, path races,
//! permission and simulated `ENOSPC` failures, ambiguous journal commits in both durable
//! states, journal I/O errors and `fsync` errors disabling the root (N3), garbage collection,
//! open-time refusals, descriptor inheritance, partial availability in reconciliation (L-f),
//! and the journal side of `fs_target_release` (J-11.9).

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::os::unix::fs::PermissionsExt;
use std::sync::{Arc, Mutex};

use writ_broker::effects::{EffectAdapter, EffectEvidence, EffectResult, Verify};
use writ_broker::execute::{Broker, Executed};
use writ_broker::refusal::Refusal;
use writ_broker::store::{StoreError, TxFailure, TxStage};
use writ_hostsys::fsfx::{FxError, EACCES, EIO, ENOSPC};
use writ_ledger_sqlite::{
    FaultCode, FaultStage, JournalFault, OpState, SqliteEffectStore, SqliteLedger,
};

use crate::dispose::{Completion, DecidedRelease, DispositionLedger, DispositionStatus};
use crate::test_support::*;
use crate::{Boundary, FsWriteAdapter, Step};

fn unavailable(code: &str) -> bool {
    matches!(code, "LEDGER_UNAVAILABLE")
}

fn fault(tx: &str, stage: FaultStage, code: FaultCode) -> JournalFault {
    JournalFault {
        transaction: tx.into(),
        stage,
        code,
    }
}

fn reason(a: &FsWriteAdapter<'_>, permit: &str) -> String {
    journal_of(a, permit)
        .1
        .map(|o| o.reason)
        .unwrap_or_default()
}

fn hook(a: &FsWriteAdapter<'_>, at: Boundary, f: impl Fn() + Send + Sync + 'static) {
    let f = Arc::new(f);
    a.set_hook(Some(Arc::new(move |b| {
        if b == at {
            f();
        }
    })));
}

// ---------------------------------------------------------------------------------------
// Normal path and consumed Failed results.

#[test]
fn create_only_applies_once_with_identity_evidence_and_replays() {
    let env = Env::new("create");
    let s = env.stores();
    let a = env.adapter(&s).unwrap();
    let b = broker(&s, &a);
    let req = env.create("k1", "sub/new.txt", "a.txt");
    let e = exec(&b, &req);
    assert_eq!(e.result, applied());
    assert_eq!(
        std::fs::read(env.tree("sub/new.txt")).unwrap(),
        env.snaps.bytes("a.txt")
    );
    assert_eq!(status(&s, "k1").as_deref(), Some("observed_success"));
    let p = permit(&e);
    let (op, outcome, target) = journal_of(&a, &p);
    let (op, outcome, target) = (op.unwrap(), outcome.unwrap(), target.unwrap());
    assert_eq!(op.state, OpState::Resolved);
    assert_eq!(
        (
            outcome.operation_evidence.as_str(),
            outcome.desired_state.as_str(),
            outcome.result.as_str()
        ),
        ("completed", "verified", "applied")
    );
    assert_eq!(target.last_identity, op.staged_identity);
    assert_eq!(target.blocked_by, None);
    assert!(staging_files(&env).is_empty());
    let ev = a.evidence(&p).unwrap().unwrap();
    assert_eq!(
        (ev.result, ev.reason.as_deref()),
        (EffectResult::Applied, Some("applied"))
    );
    // An identical retry replays; the permit cannot be presented again.
    assert!(matches!(exec(&b, &req).result, Ok(Executed::Replayed(_))));
    assert_eq!(
        b.present_permit(e.permit.as_ref().unwrap(), &req).code(),
        "PERMIT_REPLAYED"
    );
}

#[test]
fn replace_if_exchanges_and_removes_only_the_displaced_identity_after_commit() {
    let env = Env::new("replace");
    let s = env.stores();
    let a = env.adapter(&s).unwrap();
    let b = broker(&s, &a);
    assert_eq!(
        exec(&b, &env.create("k1", "t.txt", "a.txt")).result,
        applied()
    );
    let e = exec(
        &b,
        &env.replace("k2", "t.txt", "b.txt", &env.snaps.bytes("a.txt")),
    );
    assert_eq!(e.result, applied());
    assert_eq!(
        std::fs::read(env.tree("t.txt")).unwrap(),
        env.snaps.bytes("b.txt")
    );
    let (_, outcome, _) = journal_of(&a, &permit(&e));
    assert!(outcome.unwrap().displaced_identity.is_some());
    assert!(staging_files(&env).is_empty());
    // A wrong expected prior digest is a consumed precondition failure.
    let e = exec(&b, &env.replace("k3", "t.txt", "a.txt", b"not the prior"));
    assert_eq!(e.result, failed());
    assert_eq!(reason(&a, &permit(&e)), "TARGET_PRECONDITION_FAILED");
    assert_eq!(status(&s, "k3").as_deref(), Some("observed_failure"));
    let ev = a.evidence(&permit(&e)).unwrap().unwrap();
    assert_eq!(ev.reason.as_deref(), Some("TARGET_PRECONDITION_FAILED"));
}

#[test]
fn preconditions_are_consumed_failed_results_not_refusals() {
    let env = Env::new("pre");
    let s = env.stores();
    let a = env.adapter(&s).unwrap();
    let b = broker(&s, &a);
    std::fs::write(env.tree("exists.txt"), b"x").unwrap();
    mkdir(&env.tree("dir"), 0o755);
    std::fs::write(env.tree("linked.txt"), b"y").unwrap();
    std::fs::hard_link(env.tree("linked.txt"), env.tree("sub/second-link")).unwrap();
    std::os::unix::fs::symlink(env.tree("exists.txt"), env.tree("sym.txt")).unwrap();
    std::os::unix::fs::symlink(env.tree("sub"), env.tree("symdir")).unwrap();
    let cases = [
        env.create("p1", "exists.txt", "a.txt"),
        env.create("p2", "missing/x.txt", "a.txt"),
        env.create("p3", "dir", "a.txt"),
        env.replace("p4", "dir", "a.txt", b"y"),
        env.replace("p5", "linked.txt", "a.txt", b"y"),
        env.create("p6", "sym.txt", "a.txt"),
        env.create("p7", "symdir/x.txt", "a.txt"),
        env.replace("p8", "absent.txt", "a.txt", b"y"),
    ];
    for (i, req) in cases.iter().enumerate() {
        let e = exec(&b, req);
        assert_eq!(e.result, failed(), "case {i}");
        assert_eq!(
            reason(&a, &permit(&e)),
            "TARGET_PRECONDITION_FAILED",
            "case {i}"
        );
    }
    assert_eq!(std::fs::read(env.tree("exists.txt")).unwrap(), b"x");
    assert!(staging_files(&env).is_empty());
    // An access ACL on a replace_if target refuses as a precondition.
    std::fs::write(env.tree("acl.txt"), b"z").unwrap();
    let ok = std::process::Command::new("setfacl")
        .args(["-m", "u::rw"])
        .arg(env.tree("acl.txt"))
        .status()
        .unwrap()
        .success();
    let st = std::process::Command::new("setfacl")
        .args(["-m", &format!("u:{}:r", writ_hostsys::fsfx::euid())])
        .arg(env.tree("acl.txt"))
        .status()
        .unwrap();
    assert!(
        ok && st.success(),
        "setfacl must work on the test filesystem"
    );
    let e = exec(&b, &env.replace("p9", "acl.txt", "a.txt", b"z"));
    assert_eq!(e.result, failed());
    assert_eq!(reason(&a, &permit(&e)), "TARGET_PRECONDITION_FAILED");
}

#[test]
fn malformed_or_unknown_root_requests_are_refused_before_anything_is_reserved() {
    let env = Env::new("route");
    let s = env.stores();
    let a = env.adapter(&s).unwrap();
    let b = broker(&s, &a);
    // The kernel already refuses a non-canonical resource; a bad attribute is the broker's.
    let bad = env.create("r0", "sub//x", "a.txt");
    assert!(matches!(
        exec(&b, &bad).result,
        Err(Refusal::Decision { .. })
    ));
    let bad = env.request("r1", "sub/x", "a.txt", "replace", None);
    assert_eq!(exec(&b, &bad).result, Err(Refusal::RequestScopeMismatch));
    let text = writ_broker::writ_kernel::json::canonical(&env.create("r2", "x", "a.txt"))
        .unwrap()
        .replace("/r1/x", "/r9/x");
    let other_root = writ_broker::writ_kernel::json::parse(text.as_bytes()).unwrap();
    assert_eq!(
        exec(&b, &other_root).result,
        Err(Refusal::RequestScopeMismatch)
    );
    assert_eq!(status(&s, "r1"), None);
    assert_eq!(status(&s, "r2"), None);
    // Without fs roots, an fs.write request never reaches the controlled adapter.
    let plain = Broker::new(&s.active, &s.clock, &s.ledger, &s.effects, &s.key);
    let e = plain.execute(
        &verify(&env.create("r3", "x", "a.txt")),
        writ_broker::writ_kernel::contracts::StateRevision::new(5).unwrap(),
        None,
    );
    assert_eq!(e.result, Err(Refusal::RequestScopeMismatch));
}

#[test]
fn repeated_identical_content_and_later_writes_are_evidenced_by_identity() {
    let env = Env::new("same");
    let s = env.stores();
    let a = env.adapter(&s).unwrap();
    let b = broker(&s, &a);
    let e1 = exec(&b, &env.create("s1", "x.txt", "a.txt"));
    let e2 = exec(&b, &env.create("s2", "y.txt", "same.txt"));
    assert_eq!(
        (e1.result.clone(), e2.result.clone()),
        (applied(), applied())
    );
    let id1 = journal_of(&a, &permit(&e1))
        .0
        .unwrap()
        .staged_identity
        .unwrap();
    let id2 = journal_of(&a, &permit(&e2))
        .0
        .unwrap()
        .staged_identity
        .unwrap();
    assert_ne!(id1, id2, "identical bytes never share evidence");
    // replace_if to the same bytes: a new identity, evidenced like any other.
    let bytes = env.snaps.bytes("a.txt");
    let e3 = exec(&b, &env.replace("s3", "x.txt", "same.txt", &bytes));
    assert_eq!(e3.result, applied());
    let id3 = journal_of(&a, &permit(&e3))
        .0
        .unwrap()
        .staged_identity
        .unwrap();
    assert_ne!(id1, id3);
    // Later legitimate writes after applied and after failed ops.
    let f = exec(&b, &env.create("s4", "x.txt", "b.txt"));
    assert_eq!(f.result, failed());
    assert_eq!(
        exec(&b, &env.replace("s5", "x.txt", "b.txt", &bytes)).result,
        applied()
    );
}

#[test]
fn an_external_change_after_an_applied_write_taints_the_target_r11() {
    let env = Env::new("r11");
    let s = env.stores();
    let a = env.adapter(&s).unwrap();
    let b = broker(&s, &a);
    assert_eq!(
        exec(&b, &env.create("q1", "t.txt", "a.txt")).result,
        applied()
    );
    // A restored image or another writer: same bytes, another inode.
    let bytes = std::fs::read(env.tree("t.txt")).unwrap();
    std::fs::remove_file(env.tree("t.txt")).unwrap();
    std::fs::write(env.tree("t.txt"), &bytes).unwrap();
    let e = exec(&b, &env.replace("q2", "t.txt", "b.txt", &bytes));
    assert_eq!(e.result, failed());
    assert_eq!(reason(&a, &permit(&e)), "TARGET_TAINTED");
    let row = target_row(&a, "t.txt").unwrap();
    assert!(row.tainted);
    assert_eq!(row.taint_reason.as_deref(), Some("R11"));
    let e = exec(&b, &env.replace("q3", "t.txt", "b.txt", &bytes));
    assert_eq!(reason(&a, &permit(&e)), "TARGET_TAINTED");
    assert_eq!(std::fs::read(env.tree("t.txt")).unwrap(), bytes);
}

// ---------------------------------------------------------------------------------------
// Content equality is never evidence; races.

#[test]
fn equal_bytes_from_a_foreign_writer_yield_unknown_never_completed() {
    let env = Env::new("foreign");
    let s = env.stores();
    let a = env.adapter(&s).unwrap();
    let b = broker(&s, &a);
    let target = env.tree("sub/f.txt");
    let bytes = env.snaps.bytes("a.txt");
    hook(&a, Boundary::B8, move || {
        // After the rename, a foreign writer replaces the target with identical bytes.
        std::fs::remove_file(&target).unwrap();
        std::fs::write(&target, &bytes).unwrap();
    });
    let e = exec(&b, &env.create("f1", "sub/f.txt", "a.txt"));
    a.set_hook(None);
    assert!(matches!(&e.result, Err(Refusal::LedgerUnavailable(_))));
    let (_, f) = e.evidence.failure().unwrap();
    assert_eq!((f.stage, f.outcome_unknown), (TxStage::Effect, true));
    let p = permit(&e);
    let (_, outcome, row) = journal_of(&a, &p);
    let outcome = outcome.unwrap();
    assert_eq!(
        (outcome.result.as_str(), outcome.reason.as_str()),
        ("unknown", "R7")
    );
    assert!(row.unwrap().tainted);
    assert_eq!(
        std::fs::read(env.tree("sub/f.txt")).unwrap(),
        env.snaps.bytes("a.txt")
    );
    assert_eq!(a.evidence(&p).unwrap(), None);
    let r = b.reconcile().unwrap();
    assert_eq!((r.observed_success, r.unknown), (0, 1));
    assert_eq!(status(&s, "f1").as_deref(), Some("unknown_reconcile"));
    // Unknown is never revisited.
    assert!(a.recover().unwrap().is_empty());
}

#[test]
fn path_races_between_recheck_and_rename_never_produce_false_evidence() {
    let env = Env::new("races");
    let s = env.stores();
    let a = env.adapter(&s).unwrap();
    let b = broker(&s, &a);
    // Replacement between recheck and rename (create_only): EEXIST, drift.
    let t = env.tree("sub/r1.txt");
    hook(&a, Boundary::B7, move || {
        std::fs::write(&t, b"foreign").unwrap()
    });
    let e = exec(&b, &env.create("x1", "sub/r1.txt", "a.txt"));
    assert_eq!(e.result, failed());
    assert_eq!(reason(&a, &permit(&e)), "drift");
    assert!(target_row(&a, "sub/r1.txt").unwrap().tainted);
    // Directory rename: the file lands in the moved directory: R7, unknown.
    mkdir(&env.tree("d"), 0o755);
    let (d, moved) = (env.tree("d"), env.tree("d-moved"));
    hook(&a, Boundary::B7, move || {
        std::fs::rename(&d, &moved).unwrap();
        mkdir(&d, 0o755);
    });
    let e = exec(&b, &env.create("x2", "d/x.txt", "a.txt"));
    assert!(
        e.result.is_err(),
        "{:?} {:?}",
        e.result,
        journal_of(&a, &permit(&e))
    );
    assert_eq!(reason(&a, &permit(&e)), "R7");
    assert!(env.tree("d-moved/x.txt").exists());
    // Symlink swap of the parent: R9 (unobservable), the op stays staged and blocks.
    mkdir(&env.tree("e"), 0o755);
    let (dir, aside) = (env.tree("e"), env.tree("e-aside"));
    let (dir2, aside2) = (dir.clone(), aside.clone());
    hook(&a, Boundary::B7, move || {
        std::fs::rename(&dir, &aside).unwrap();
        std::os::unix::fs::symlink(&aside, &dir).unwrap();
    });
    let e = exec(&b, &env.create("x3", "e/x.txt", "a.txt"));
    a.set_hook(None);
    assert!(e.result.is_err());
    let p3 = permit(&e);
    assert_eq!(journal_of(&a, &p3).0.unwrap().state, OpState::Staged);
    // Through the symlink every target below it fails its precondition (no symlinks).
    let e = exec(&b, &env.create("x4", "e/y.txt", "a.txt"));
    assert_eq!(reason(&a, &permit(&e)), "TARGET_PRECONDITION_FAILED");
    std::fs::remove_file(&dir2).unwrap();
    std::fs::rename(&aside2, &dir2).unwrap();
    let e = exec(
        &b,
        &env.replace("x5", "e/x.txt", "b.txt", &env.snaps.bytes("a.txt")),
    );
    assert_eq!(reason(&a, &permit(&e)), "TARGET_UNRESOLVED");
    assert_eq!(a.recover().unwrap(), vec![(format!("fsop-{p3}"), "R3")]);
    // Hard link after the rename: R8.
    let (t, l) = (env.tree("sub/h.txt"), env.tree("hl"));
    hook(&a, Boundary::B8, move || {
        std::fs::hard_link(&t, &l).unwrap()
    });
    let e = exec(&b, &env.create("x6", "sub/h.txt", "a.txt"));
    assert!(e.result.is_err());
    assert_eq!(reason(&a, &permit(&e)), "R8");
    // replace_if with the target replaced before the exchange: the exchange displaces a
    // foreign file (S other): R8 comes first in the row order (N1); the displaced file is kept.
    a.set_hook(None);
    assert_eq!(
        exec(&b, &env.create("x7", "sub/y.txt", "a.txt")).result,
        applied()
    );
    let t = env.tree("sub/y.txt");
    hook(&a, Boundary::B7, move || {
        let tmp = t.with_extension("tmp");
        std::fs::write(&tmp, b"other").unwrap();
        std::fs::rename(&tmp, &t).unwrap();
    });
    let e = exec(
        &b,
        &env.replace("x8", "sub/y.txt", "b.txt", &env.snaps.bytes("a.txt")),
    );
    a.set_hook(None);
    assert!(e.result.is_err());
    assert_eq!(reason(&a, &permit(&e)), "R8");
    let op = journal_of(&a, &permit(&e)).0.unwrap();
    assert!(staging_files(&env).contains(&op.staging_name));
    // replace_if whose displaced file vanishes after the exchange: R3y (L-d).
    assert_eq!(
        exec(&b, &env.create("x9", "sub/w.txt", "a.txt")).result,
        applied()
    );
    let staging = env.staging();
    let keep = staging_files(&env);
    hook(&a, Boundary::B8, move || {
        for f in std::fs::read_dir(&staging).unwrap() {
            let f = f.unwrap();
            if !keep.contains(&f.file_name().to_string_lossy().into_owned()) {
                std::fs::remove_file(f.path()).unwrap();
            }
        }
    });
    let e = exec(
        &b,
        &env.replace("x10", "sub/w.txt", "b.txt", &env.snaps.bytes("a.txt")),
    );
    a.set_hook(None);
    assert!(e.result.is_err());
    assert_eq!(reason(&a, &permit(&e)), "R3y");
    assert!(target_row(&a, "sub/w.txt").unwrap().tainted);
}

// ---------------------------------------------------------------------------------------
// Permission and full-disk failures; STATX_BTIME; st_dev.

#[test]
fn permission_failures_and_simulated_enospc_resolve_or_stay_unknown_by_the_table() {
    let env = Env::new("perm");
    let s = env.stores();
    let a = env.adapter(&s).unwrap();
    let b = broker(&s, &a);
    // Staging not writable: the exclusive create fails, no file: STAGE_FAILED.
    std::fs::set_permissions(env.staging(), std::fs::Permissions::from_mode(0o500)).unwrap();
    let e = exec(&b, &env.create("e1", "sub/a.txt", "a.txt"));
    std::fs::set_permissions(env.staging(), std::fs::Permissions::from_mode(0o700)).unwrap();
    assert_eq!(
        (e.result.clone(), reason(&a, &permit(&e))),
        (failed(), "STAGE_FAILED".into())
    );
    // Parent not writable: the rename fails (EACCES), the table decides R5.
    mkdir(&env.tree("ro"), 0o755);
    std::fs::set_permissions(env.tree("ro"), std::fs::Permissions::from_mode(0o555)).unwrap();
    let e = exec(&b, &env.create("e2", "ro/a.txt", "a.txt"));
    std::fs::set_permissions(env.tree("ro"), std::fs::Permissions::from_mode(0o755)).unwrap();
    assert_eq!(
        (e.result.clone(), reason(&a, &permit(&e))),
        (failed(), "R5".into())
    );
    assert!(staging_files(&env).is_empty());
    // An unreadable replace_if target cannot be observed at intent: nothing consumed.
    std::fs::write(env.tree("secret.txt"), b"s").unwrap();
    std::fs::set_permissions(
        env.tree("secret.txt"),
        std::fs::Permissions::from_mode(0o000),
    )
    .unwrap();
    let e = exec(&b, &env.replace("e3", "secret.txt", "a.txt", b"s"));
    assert!(matches!(&e.result, Err(Refusal::LedgerUnavailable(_))));
    assert_eq!(journal_of(&a, &permit(&e)).0, None);
    std::fs::set_permissions(
        env.tree("secret.txt"),
        std::fs::Permissions::from_mode(0o600),
    )
    .unwrap();
    // ENOSPC (simulated; the real case is verify-in-VM) at create, write and rename.
    a.fail_once(Step::Create, FxError::Errno(ENOSPC));
    let e = exec(&b, &env.create("e4", "sub/b.txt", "a.txt"));
    assert_eq!(reason(&a, &permit(&e)), "STAGE_FAILED");
    a.fail_once(Step::Write, FxError::Errno(ENOSPC));
    let e = exec(&b, &env.create("e5", "sub/c.txt", "a.txt"));
    assert_eq!(
        (e.result.clone(), reason(&a, &permit(&e))),
        (failed(), "STAGE_FAILED".into())
    );
    assert!(journal_of(&a, &permit(&e))
        .0
        .unwrap()
        .staged_identity
        .is_some());
    assert!(
        staging_files(&env).is_empty(),
        "the own abandoned file is removed"
    );
    a.fail_once(Step::Rename, FxError::Errno(ENOSPC));
    let e = exec(&b, &env.create("e6", "sub/d.txt", "a.txt"));
    assert_eq!(reason(&a, &permit(&e)), "R5");
    a.fail_once(Step::Rename, FxError::Errno(EACCES));
    let e = exec(&b, &env.create("e7", "sub/e.txt", "a.txt"));
    assert_eq!(reason(&a, &permit(&e)), "R5");
    assert!(!a.is_disabled());
}

#[test]
fn a_statx_without_birth_time_records_nothing_and_an_st_dev_change_concludes_nothing() {
    let env = Env::new("btime");
    let s = env.stores();
    let a = env.adapter(&s).unwrap();
    let b = broker(&s, &a);
    a.fail_once(Step::StatxStaged, FxError::NoBirthTime);
    let e = exec(&b, &env.create("b1", "sub/a.txt", "a.txt"));
    let (_, f) = e.evidence.failure().unwrap();
    assert_eq!(f.stage, TxStage::Effect);
    let p = permit(&e);
    assert_eq!(journal_of(&a, &p).0.unwrap().state, OpState::Intent);
    // An st_dev change within the lifetime: recovery concludes nothing (R9).
    let real = a.dev();
    a.set_dev(real ^ 1);
    assert_eq!(a.recover().unwrap(), vec![(format!("fsop-{p}"), "R9")]);
    let e = exec(&b, &env.create("b2", "sub/z.txt", "a.txt"));
    assert!(matches!(&e.result, Err(Refusal::LedgerUnavailable(_))));
    a.set_dev(real);
    // The unrecorded staged file: R1b, kept and the target blocked.
    assert_eq!(a.recover().unwrap(), vec![(format!("fsop-{p}"), "R1b")]);
    assert!(target_row(&a, "sub/a.txt").unwrap().blocked_by.is_some());
    assert_eq!(staging_files(&env).len(), 1);
}

// ---------------------------------------------------------------------------------------
// Ambiguous commits, journal I/O errors and fsync errors.

#[test]
fn ambiguous_commits_in_both_durable_states_never_lose_or_invent_progress() {
    let env = Env::new("ambig");
    let s = env.stores();
    let a = env.adapter(&s).unwrap();
    let b = broker(&s, &a);
    let busy = FaultCode::Busy;
    // T-intent lost: nothing durable, no filesystem step (F6).
    a.journal()
        .inject(fault("fs_intent", FaultStage::CommitLost, busy));
    let e = exec(&b, &env.create("c1", "sub/1.txt", "a.txt"));
    let (_, f) = e.evidence.failure().unwrap();
    assert_eq!((f.stage, f.outcome_unknown), (TxStage::Commit, true));
    assert_eq!(journal_of(&a, &permit(&e)).0, None);
    assert!(staging_files(&env).is_empty() && !env.tree("sub/1.txt").exists());
    // T-intent done: the op is durable, still no filesystem step; recovery R1a.
    a.journal()
        .inject(fault("fs_intent", FaultStage::CommitDone, busy));
    let e = exec(&b, &env.create("c2", "sub/2.txt", "a.txt"));
    assert!(staging_files(&env).is_empty() && !env.tree("sub/2.txt").exists());
    let p = permit(&e);
    assert_eq!(a.recover().unwrap(), vec![(format!("fsop-{p}"), "R1a")]);
    // T-staged lost: intent with an unrecorded file: R1b. Done: staged: R5.
    a.journal()
        .inject(fault("fs_staged", FaultStage::CommitLost, busy));
    let e = exec(&b, &env.create("c3", "sub/3.txt", "a.txt"));
    assert_eq!(
        a.recover().unwrap(),
        vec![(format!("fsop-{}", permit(&e)), "R1b")]
    );
    a.journal()
        .inject(fault("fs_staged", FaultStage::CommitDone, busy));
    let e = exec(&b, &env.create("c4", "sub/4.txt", "a.txt"));
    assert_eq!(
        a.recover().unwrap(),
        vec![(format!("fsop-{}", permit(&e)), "R5")]
    );
    // T-outcome lost: renamed and verified but unrecorded: R3. Done: resolved applied.
    a.journal()
        .inject(fault("fs_outcome", FaultStage::CommitLost, busy));
    let e = exec(&b, &env.create("c5", "sub/5.txt", "a.txt"));
    assert_eq!(
        a.recover().unwrap(),
        vec![(format!("fsop-{}", permit(&e)), "R3")]
    );
    a.journal()
        .inject(fault("fs_outcome", FaultStage::CommitDone, busy));
    let e = exec(&b, &env.create("c6", "sub/6.txt", "a.txt"));
    assert!(a.recover().unwrap().is_empty());
    assert_eq!(journal_of(&a, &permit(&e)).1.unwrap().result, "applied");
    let r = b.reconcile().unwrap();
    assert_eq!(
        (r.observed_success, r.observed_failure, r.unknown),
        (2, 3, 1)
    );
    assert!(!a.is_disabled());
}

#[test]
fn a_journal_io_error_marks_the_root_file_first_and_refuses_until_reboot() {
    let env = Env::new("ioerr");
    let s = env.stores();
    {
        let a = env.adapter(&s).unwrap();
        let b = broker(&s, &a);
        a.journal()
            .inject(fault("fs_intent", FaultStage::Body, FaultCode::IoErr));
        let e = exec(&b, &env.create("i1", "sub/1.txt", "a.txt"));
        assert!(e.result.is_err());
        assert!(a.is_disabled());
        assert!(env.cfg.state_dir.join("fs-unrecoverable-r1").exists());
        let marks = a.journal().read("t", |tx| tx.root_marks()).unwrap();
        assert_eq!(marks.len(), 1);
        // Every later operation: outer Begin failure, nothing consumed.
        let e = exec(&b, &env.create("i2", "sub/2.txt", "a.txt"));
        let (_, f) = e.evidence.failure().unwrap();
        assert_eq!(f.stage, TxStage::Begin);
        assert_eq!(f.error.code(), crate::ROOT_UNRECOVERABLE);
        assert!(unavailable(e.result.unwrap_err().code()));
    }
    let err = env.adapter(&s).err().unwrap();
    assert_eq!(err.code(), crate::ROOT_UNRECOVERABLE);
}

#[test]
fn an_fsync_error_marks_the_root_even_when_the_journal_refuses_the_mark() {
    let env = Env::new("fsync");
    let s = env.stores();
    {
        let a = env.adapter(&s).unwrap();
        let b = broker(&s, &a);
        a.journal()
            .inject(fault("fs_mark", FaultStage::Begin, FaultCode::Busy));
        a.fail_once(Step::FsyncFile, FxError::Errno(EIO));
        let e = exec(&b, &env.create("y1", "sub/1.txt", "a.txt"));
        let (_, f) = e.evidence.failure().unwrap();
        assert_eq!((f.stage, f.outcome_unknown), (TxStage::Effect, true));
        assert!(a.is_disabled());
        // The file mark exists although root_state could not be written.
        assert!(env.cfg.state_dir.join("fs-unrecoverable-r1").exists());
        assert!(a
            .journal()
            .read("t", |tx| tx.root_marks())
            .unwrap()
            .is_empty());
    }
    assert_eq!(
        env.adapter(&s).err().unwrap().code(),
        crate::ROOT_UNRECOVERABLE
    );
}

#[test]
fn a_mark_that_cannot_be_written_refuses_every_operation_and_every_reopen() {
    let env = Env::new("nomark");
    let s = env.stores();
    {
        let a = env.adapter(&s).unwrap();
        let b = broker(&s, &a);
        std::fs::set_permissions(&env.cfg.state_dir, std::fs::Permissions::from_mode(0o500))
            .unwrap();
        a.fail_once(Step::FsyncStaging, FxError::Errno(EIO));
        let _ = exec(&b, &env.create("n1", "sub/1.txt", "a.txt"));
        assert!(a.is_disabled());
        assert!(!env.cfg.state_dir.join("fs-unrecoverable-r1").exists());
        assert!(exec(&b, &env.create("n2", "sub/2.txt", "a.txt"))
            .result
            .is_err());
    }
    std::fs::set_permissions(&env.cfg.state_dir, std::fs::Permissions::from_mode(0o700)).unwrap();
    assert_eq!(
        env.adapter(&s).err().unwrap().code(),
        crate::ROOT_UNRECOVERABLE
    );
}

// ---------------------------------------------------------------------------------------
// Garbage collection, open refusals, descriptors, ownership.

#[test]
fn gc_removes_only_recorded_identities_of_applied_or_failed_untainted_ops() {
    let env = Env::new("gc");
    let s = env.stores();
    let displaced;
    {
        let a = env.adapter(&s).unwrap();
        let b = broker(&s, &a);
        assert_eq!(
            exec(&b, &env.create("g1", "t.txt", "a.txt")).result,
            applied()
        );
        a.fail_once(Step::Cleanup, FxError::Errno(EIO));
        let e = exec(
            &b,
            &env.replace("g2", "t.txt", "b.txt", &env.snaps.bytes("a.txt")),
        );
        assert_eq!(e.result, applied());
        displaced = journal_of(&a, &permit(&e)).0.unwrap().staging_name;
        // A tainted target's kept file (R6 by a foreign target).
        let t = env.tree("sub/r6.txt");
        hook(&a, Boundary::B6, move || {
            std::fs::write(&t, b"foreign").unwrap()
        });
        let e = exec(&b, &env.create("g3", "sub/r6.txt", "a.txt"));
        assert_eq!(reason(&a, &permit(&e)), "R6");
    }
    std::fs::write(env.staging().join("stray"), b"?").unwrap();
    assert!(staging_files(&env).contains(&displaced));
    let a = env.adapter(&s).unwrap();
    let files = staging_files(&env);
    assert!(
        !files.contains(&displaced),
        "the displaced identity is collected"
    );
    assert_eq!(files.len(), 2, "{files:?}");
    let mut kept = a.gc_kept();
    kept.sort();
    assert_eq!(kept, files);
}

#[test]
fn open_refuses_unsafe_layouts_overlaps_second_owners_and_foreign_bindings() {
    let env = Env::new("open");
    let s = env.stores();
    let a = env.adapter(&s).unwrap();
    // A second adapter on the same root through another journal: the staging flock.
    let mut other = env.cfg.clone();
    other.journal = env.dir.join("other-journal.db");
    other.create_journal = true;
    assert_eq!(
        FsWriteAdapter::open(&other, s.ledger.ledger_id(), &env.snaps, &[])
            .err()
            .unwrap()
            .code(),
        "OWNED_ELSEWHERE"
    );
    drop(a);
    // The same journal while owned elsewhere (an out-of-process recovery tool) refuses.
    let owner = env.adapter(&s).unwrap();
    let again = FsWriteAdapter::open(&env.cfg, s.ledger.ledger_id(), &env.snaps, &[]);
    assert_eq!(again.err().unwrap().code(), "OWNED_ELSEWHERE");
    drop(owner);
    // Overlapping roots.
    let inner = env.tree("sub");
    for peer in [env.cfg.path.clone(), inner, env.dir.clone()] {
        let r = FsWriteAdapter::open(&env.cfg, s.ledger.ledger_id(), &env.snaps, &[peer]);
        assert_eq!(r.err().unwrap().code(), "OVERLAP");
    }
    // A wrong filesystem UUID.
    let mut bad = env.cfg.clone();
    bad.fs_uuid = "00000000-0000-0000-0000-000000000000".into();
    assert_eq!(
        FsWriteAdapter::open(&bad, s.ledger.ledger_id(), &env.snaps, &[])
            .err()
            .unwrap()
            .code(),
        "FS_UUID"
    );
    // Another ledger.
    let other_ledger = SqliteLedger::create(&env.dir.join("other-ledger.db")).unwrap();
    assert_eq!(
        FsWriteAdapter::open(&env.cfg, other_ledger.ledger_id(), &env.snaps, &[])
            .err()
            .unwrap()
            .code(),
        "LEDGER_MISMATCH"
    );
    // A default ACL on tree, group-writable staging.
    let st = std::process::Command::new("setfacl")
        .args(["-d", "-m", "u::rwx"])
        .arg(env.cfg.path.join("tree"))
        .status()
        .unwrap();
    assert!(st.success());
    assert_eq!(env.adapter(&s).err().unwrap().code(), "ACL");
    std::process::Command::new("setfacl")
        .args(["-k"])
        .arg(env.cfg.path.join("tree"))
        .status()
        .unwrap();
    std::fs::set_permissions(env.staging(), std::fs::Permissions::from_mode(0o770)).unwrap();
    assert_eq!(env.adapter(&s).err().unwrap().code(), "MODE");
    std::fs::set_permissions(env.staging(), std::fs::Permissions::from_mode(0o700)).unwrap();
    assert!(env.adapter(&s).is_ok());
}

#[test]
fn no_adapter_descriptor_reaches_a_spawned_child() {
    let env = Env::new("fds");
    let s = env.stores();
    let a = env.adapter(&s).unwrap();
    let out = std::process::Command::new("/bin/sh")
        .args(["-c", "for f in /proc/self/fd/*; do readlink $f; done"])
        .output()
        .unwrap();
    let text = String::from_utf8_lossy(&out.stdout);
    let root = env.dir.to_string_lossy().to_string();
    assert!(!text.contains(&root), "inherited: {text}");
    drop(a);
}

// ---------------------------------------------------------------------------------------
// Reconciliation with one root unreadable (L-f).

/// The controlled adapter whose transaction reports an unknown commit outcome after
/// committing: the receipt stays `dispatched` with durable evidence.
struct LostObserve<'a>(&'a SqliteEffectStore);

impl EffectAdapter for LostObserve<'_> {
    fn apply(&self, verify: &mut Verify<'_>) -> Result<Result<EffectResult, Refusal>, TxFailure> {
        let _ = self.0.apply(verify)?;
        Err(TxFailure::commit_unknown(StoreError::Unavailable {
            category: "test".into(),
            code: "SQLITE_IOERR".into(),
        }))
    }

    fn inspect(
        &self,
        verify: &mut Verify<'_>,
    ) -> Result<Result<Option<EffectEvidence>, Refusal>, TxFailure> {
        self.0.inspect(verify)
    }

    fn evidence(&self, permit_id: &str) -> Result<Option<EffectEvidence>, TxFailure> {
        self.0.evidence(permit_id)
    }
}

#[test]
fn one_unreadable_root_does_not_block_reconciliation_of_ruled_out_receipts() {
    let env = Env::new("partial");
    let s = env.stores();
    let a = env.adapter(&s).unwrap();
    let lost = LostObserve(&s.effects);
    let b = Broker::new(&s.active, &s.clock, &s.ledger, &lost, &s.key).with_fs_root("r1", &a);
    // An fs.write with an unknown outcome and a controlled write left dispatched.
    let t = env.tree("sub/u.txt");
    hook(&a, Boundary::B8, move || {
        std::fs::remove_file(&t).unwrap();
    });
    let e = exec(&b, &env.create("u1", "sub/u.txt", "a.txt"));
    a.set_hook(None);
    assert!(e.result.is_err());
    let text = writ_broker::writ_kernel::json::canonical(&env.create("u2", "x", "a.txt"))
        .unwrap()
        .replace("\"fs.write\"", "\"file.write\"")
        .replace(
            "file:fixture-tenant/fixture-project/r1/x",
            "workspace:fixture-tenant/fixture-project/x",
        );
    let controlled = writ_broker::writ_kernel::json::parse(text.as_bytes()).unwrap();
    assert!(exec(&b, &controlled).result.is_err());
    // The root becomes unreadable (disabled until reboot).
    a.mark_unrecoverable("test");
    let r = b.reconcile().unwrap();
    assert_eq!(r.observed_success, 1, "the controlled receipt is resolved");
    assert_eq!(r.duplicate_check_skipped, 1);
    assert_eq!(r.not_ruled_out, 1);
    assert_eq!(status(&s, "u2").as_deref(), Some("observed_success"));
    assert_eq!(status(&s, "u1").as_deref(), Some("unknown_reconcile"));
    let audited: i64 = s.ledger.refusal_count().unwrap() as i64;
    assert!(audited >= 1);
}

// ---------------------------------------------------------------------------------------
// fs_target_release, journal side (J-11.9).

struct MemLedger(Mutex<Vec<(DecidedRelease, Option<DispositionStatus>)>>);

impl DispositionLedger for MemLedger {
    fn decided(&self, _root_id: &str) -> Vec<DecidedRelease> {
        self.0
            .lock()
            .unwrap()
            .iter()
            .filter(|(_, s)| s.is_none())
            .map(|(d, _)| d.clone())
            .collect()
    }

    fn set_status(&self, id: &str, status: DispositionStatus) {
        for (d, s) in self.0.lock().unwrap().iter_mut() {
            if d.disposition_id == id {
                *s = Some(status);
            }
        }
    }
}

#[test]
fn release_requires_resolved_ops_and_worker_gone_evidence_and_is_idempotent() {
    let env = Env::new("release");
    let s = env.stores();
    let a = env.adapter(&s).unwrap();
    let b = broker(&s, &a);
    // A clean target is not applicable.
    assert_eq!(
        exec(&b, &env.create("d1", "sub/ok.txt", "a.txt")).result,
        applied()
    );
    assert_eq!(
        a.release_context("sub/ok.txt").err(),
        Some(Refusal::DispositionNotApplicable)
    );
    // An unknown op taints its target (R7).
    let t = env.tree("sub/t.txt");
    hook(&a, Boundary::B8, move || std::fs::remove_file(&t).unwrap());
    let e = exec(&b, &env.create("d2", "sub/t.txt", "a.txt"));
    a.set_hook(None);
    let p = permit(&e);
    assert_eq!(reason(&a, &p), "R7");
    // Worker-gone evidence: another process in the service cgroup refuses.
    std::fs::write(
        env.dir.join("cgroup.procs"),
        format!("{}\n1\n", std::process::id()),
    )
    .unwrap();
    assert_eq!(
        a.release_context("sub/t.txt").err(),
        Some(Refusal::DispositionPrecondition)
    );
    std::fs::write(
        env.dir.join("cgroup.procs"),
        format!("{}\n", std::process::id()),
    )
    .unwrap();
    let ctx = a.release_context("sub/t.txt").unwrap();
    assert!(ctx.target_id.starts_with("fsroot:r1:9:sub/t.txt"));
    assert_eq!(ctx.observed, "absent");
    let decided = DecidedRelease {
        disposition_id: "disp-1".into(),
        target: "sub/t.txt".into(),
        state_digest: ctx.state_digest.clone(),
        new_last_identity: ctx.observed.clone(),
    };
    // A state change between decision and journal step: lapsed, nothing applied.
    std::fs::write(env.tree("sub/t.txt"), b"late").unwrap();
    assert_eq!(a.apply_release(&decided).unwrap(), Completion::Lapsed);
    assert!(target_row(&a, "sub/t.txt").unwrap().tainted);
    std::fs::remove_file(env.tree("sub/t.txt")).unwrap();
    assert_eq!(a.apply_release(&decided).unwrap(), Completion::Applied);
    assert_eq!(
        a.apply_release(&decided).unwrap(),
        Completion::AlreadyApplied
    );
    let row = target_row(&a, "sub/t.txt").unwrap();
    assert!(!row.tainted && row.blocked_by.is_none());
    // The recorded outcome is unchanged; the receipt still needs reconciliation.
    assert_eq!(journal_of(&a, &p).1.unwrap().result, "unknown");
    assert_eq!(a.evidence(&p).unwrap(), None);
    // A later op is a new operation evaluated against the new baseline.
    assert_eq!(
        exec(&b, &env.create("d3", "sub/t.txt", "b.txt")).result,
        applied()
    );
    // Completion after a crash: decided ones are applied once or lapse.
    let ledger = MemLedger(Mutex::new(vec![(decided.clone(), None)]));
    assert_eq!(
        a.complete_dispositions(&ledger).unwrap(),
        vec![("disp-1".to_string(), Completion::AlreadyApplied)]
    );
    assert_eq!(
        ledger.0.lock().unwrap()[0].1,
        Some(DispositionStatus::Applied)
    );
    let stale = DecidedRelease {
        disposition_id: "disp-2".into(),
        ..decided
    };
    let ledger = MemLedger(Mutex::new(vec![(stale, None)]));
    assert_eq!(
        a.complete_dispositions(&ledger).unwrap(),
        vec![("disp-2".to_string(), Completion::Lapsed)]
    );
}

#[test]
fn release_refuses_while_an_op_on_the_target_is_unresolved() {
    let env = Env::new("unres");
    let s = env.stores();
    let a = env.adapter(&s).unwrap();
    let b = broker(&s, &a);
    a.fail_once(Step::StatxStaged, FxError::NoBirthTime);
    let e = exec(&b, &env.create("v1", "sub/v.txt", "a.txt"));
    assert!(e.result.is_err());
    assert_eq!(
        a.release_context("sub/v.txt").err(),
        Some(Refusal::DispositionPrecondition)
    );
}
