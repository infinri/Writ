//! The J-7.6 decision table constructed explicitly, row by row, and the row order (N1):
//! observations that match an unsafe row and a completing row decide the unsafe one. Real
//! filesystem constructions of the rows are in `adapter_tests` (races) and `kill_tests`
//! (SIGKILL at every boundary).

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use writ_broker::effects::FsWriteMode::{self, CreateOnly, ReplaceIf};
use writ_hostsys::fsfx::Observed;
use writ_ledger_sqlite::OpState::{self, Abandoned, Intent, Staged};

use crate::ident::{digest, Identity, Prior};
use crate::recover::{decide, result_of, Facts};
use crate::Slot;

const UUID: &str = "u";

fn obs(ino: u64, nlink: u32) -> Observed {
    Observed {
        dev: 1,
        ino,
        btime_sec: 1_700_000_000,
        btime_nsec: ino as u32,
        nlink,
        uid: 1000,
        mode: 0o100_600,
        size: 4,
        mnt_id: 9,
    }
}

fn file(ino: u64, nlink: u32, content: &[u8]) -> Slot {
    Slot::Present {
        o: obs(ino, nlink),
        digest: Some(digest(content)),
        acl: false,
    }
}

fn id(ino: u64, content: &[u8]) -> Identity {
    Identity::of(UUID, &obs(ino, 1), &digest(content))
}

const NEW: &[u8] = b"new!";
const OLD: &[u8] = b"old!";
const STAGED: u64 = 10;
const PRIOR: u64 = 20;
const OTHER: u64 = 30;

fn row(state: OpState, mode: FsWriteMode, t: &Slot, s: &Slot) -> &'static str {
    let prior = match mode {
        CreateOnly => Prior::Absent,
        ReplaceIf => Prior::File(id(PRIOR, OLD)),
    };
    let staged = id(STAGED, NEW);
    let content = digest(NEW);
    let expected = digest(OLD);
    let f = Facts {
        state,
        mode,
        prior: &prior,
        staged: (state != Intent).then_some(&staged),
        content_digest: &content,
        expected_prior_digest: (mode == ReplaceIf).then_some(expected.as_str()),
    };
    decide(&f, t, s)
}

#[test]
fn every_row_is_reached_by_its_defining_observation() {
    let a = Slot::Absent;
    let cases: Vec<(&str, OpState, FsWriteMode, Slot, Slot)> = vec![
        // intent
        ("R2", Intent, CreateOnly, file(OTHER, 1, NEW), a.clone()),
        ("R2", Intent, ReplaceIf, file(PRIOR, 1, b"chg!"), a.clone()),
        ("R2", Intent, ReplaceIf, a.clone(), a.clone()),
        ("R1a", Intent, CreateOnly, a.clone(), a.clone()),
        ("R1a", Intent, ReplaceIf, file(PRIOR, 1, OLD), a.clone()),
        (
            "R1b",
            Intent,
            CreateOnly,
            a.clone(),
            file(OTHER, 1, b"part"),
        ),
        (
            "R1b",
            Intent,
            ReplaceIf,
            file(PRIOR, 1, OLD),
            file(STAGED, 1, NEW),
        ),
        // abandoned
        ("R1c", Abandoned, CreateOnly, a.clone(), a.clone()),
        (
            "R1c",
            Abandoned,
            CreateOnly,
            a.clone(),
            file(STAGED, 1, NEW),
        ),
        (
            "R-other",
            Abandoned,
            CreateOnly,
            a.clone(),
            file(OTHER, 1, NEW),
        ),
        (
            "R-other",
            Abandoned,
            CreateOnly,
            file(OTHER, 1, NEW),
            a.clone(),
        ),
        // staged, create_only
        ("R8", Staged, CreateOnly, a.clone(), file(OTHER, 1, NEW)),
        ("R8", Staged, CreateOnly, file(STAGED, 2, NEW), a.clone()),
        ("R8", Staged, CreateOnly, a.clone(), file(STAGED, 2, NEW)),
        (
            "R4",
            Staged,
            CreateOnly,
            file(STAGED, 1, b"mod!"),
            a.clone(),
        ),
        (
            "R6",
            Staged,
            CreateOnly,
            file(OTHER, 1, NEW),
            file(STAGED, 1, NEW),
        ),
        ("R3", Staged, CreateOnly, file(STAGED, 1, NEW), a.clone()),
        ("R5", Staged, CreateOnly, a.clone(), file(STAGED, 1, NEW)),
        ("R7", Staged, CreateOnly, a.clone(), a.clone()),
        ("R7", Staged, CreateOnly, file(OTHER, 1, NEW), a.clone()),
        // staged, replace_if
        ("R3y", Staged, ReplaceIf, file(STAGED, 1, NEW), a.clone()),
        (
            "R3y",
            Staged,
            ReplaceIf,
            file(STAGED, 1, NEW),
            file(PRIOR, 1, b"chg!"),
        ),
        (
            "R3y",
            Staged,
            ReplaceIf,
            file(STAGED, 1, NEW),
            file(PRIOR, 2, OLD),
        ),
        (
            "R3x",
            Staged,
            ReplaceIf,
            file(STAGED, 1, NEW),
            file(PRIOR, 1, OLD),
        ),
        (
            "R5",
            Staged,
            ReplaceIf,
            file(PRIOR, 1, OLD),
            file(STAGED, 1, NEW),
        ),
        (
            "R6",
            Staged,
            ReplaceIf,
            file(OTHER, 1, OLD),
            file(STAGED, 1, NEW),
        ),
        (
            "R8",
            Staged,
            ReplaceIf,
            file(STAGED, 1, NEW),
            file(OTHER, 1, OLD),
        ),
        ("R-other", Staged, ReplaceIf, file(PRIOR, 1, OLD), a.clone()),
        ("R-other", Staged, ReplaceIf, a.clone(), a.clone()),
    ];
    for (want, state, mode, t, s) in cases {
        assert_eq!(
            row(state, mode, &t, &s),
            want,
            "{state:?} {mode:?} T={t:?} S={s:?}"
        );
    }
}

#[test]
fn unsafe_rows_win_over_completing_rows_n1() {
    let a = Slot::Absent;
    // R3's observation plus nlink > 1 at the target: R8, not R3.
    assert_eq!(row(Staged, CreateOnly, &file(STAGED, 2, NEW), &a), "R8");
    // R3's observation plus a different digest: R4, not R3.
    assert_eq!(row(Staged, CreateOnly, &file(STAGED, 1, b"xxxx"), &a), "R4");
    // R3x's observation plus nlink > 1 at the target: R8.
    assert_eq!(
        row(
            Staged,
            ReplaceIf,
            &file(STAGED, 2, NEW),
            &file(PRIOR, 1, OLD)
        ),
        "R8"
    );
    // R3x's observation plus nlink > 1 at the staging name (the displaced file): R3y.
    assert_eq!(
        row(
            Staged,
            ReplaceIf,
            &file(STAGED, 1, NEW),
            &file(PRIOR, 2, OLD)
        ),
        "R3y"
    );
    // R3x's observation with a displaced digest different from expected_prior_digest (L-d).
    assert_eq!(
        row(
            Staged,
            ReplaceIf,
            &file(STAGED, 1, NEW),
            &file(PRIOR, 1, b"chg!")
        ),
        "R3y"
    );
    // S and T both the staged identity (a hard link): R8, never R3 or R5.
    assert_eq!(
        row(
            Staged,
            CreateOnly,
            &file(STAGED, 2, NEW),
            &file(STAGED, 2, NEW)
        ),
        "R8"
    );
    // R5's observation with a hard-linked staged file: R8, never R5.
    assert_eq!(row(Staged, CreateOnly, &a, &file(STAGED, 2, NEW)), "R8");
    // R4 is decided before R3y for replace_if.
    assert_eq!(
        row(
            Staged,
            ReplaceIf,
            &file(STAGED, 1, b"mod!"),
            &file(PRIOR, 1, OLD)
        ),
        "R4"
    );
}

#[test]
fn content_equality_alone_never_completes_an_op() {
    let a = Slot::Absent;
    // The target has the expected bytes under another identity: never R3.
    for s in [a.clone(), file(STAGED, 1, NEW)] {
        let r = row(Staged, CreateOnly, &file(OTHER, 1, NEW), &s);
        assert_ne!(result_of(r), Some("applied"), "{r}");
        assert_eq!(result_of(r), Some("unknown"), "{r}");
    }
    // Prior bytes at the target and no staged file do not prove "not completed".
    let r = row(Staged, ReplaceIf, &file(OTHER, 1, OLD), &a);
    assert_eq!(result_of(r), Some("unknown"));
    // Results of every row: unknown stays unknown; R9 records nothing.
    for (r, want) in [
        ("R1a", Some("failed")),
        ("R1b", Some("failed")),
        ("R1c", Some("failed")),
        ("R2", Some("unknown")),
        ("R3", Some("applied")),
        ("R3x", Some("applied")),
        ("R3y", Some("unknown")),
        ("R4", Some("unknown")),
        ("R5", Some("failed")),
        ("R6", Some("unknown")),
        ("R7", Some("unknown")),
        ("R8", Some("unknown")),
        ("R-other", Some("unknown")),
        ("R9", None),
    ] {
        assert_eq!(result_of(r), want, "{r}");
    }
}
